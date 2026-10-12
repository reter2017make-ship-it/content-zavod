import json
import shutil
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.database.models import ContentItem, RightsStatus, Status
from app.media.ffmpeg import run as ffmpeg_run
from app.media.ffprobe import probe
from app.processing.pipeline import Pipeline
from app.sources.filesystem import FilesystemSource
from app.sources.http import RateLimitedClient, SourceHTTPError
from app.sources.pexels import PexelsSource
from app.sources.pixabay import PixabaySource
from app.sources.registry import build_sources
from app.sources.telegram import ChannelSpec, TelegramBlocked, TelegramSource, parse_links, parse_page
from app.sources.wikimedia import WikimediaSource, map_license


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("app.sources.http.time.sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def keys(monkeypatch):
    for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "WIKIMEDIA_CONTACT"):
        monkeypatch.setenv(k, f"test-{k.lower()}")


def _enable(settings, name, **cfg):
    settings.raw["sources"][name] = {"enabled": True, "requests_per_minute": 6000, **cfg}


def _items(db):
    with db.session() as s:
        return list(s.scalars(select(ContentItem).order_by(ContentItem.id)))


# ---------- HTTP client ----------

def test_rate_limited_client_retries_429_and_honours_retry_after(no_sleep):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    c = RateLimitedClient(requests_per_minute=6000, retries=3, transport=httpx.MockTransport(handler))
    assert c.get_json("https://api.test/x") == {"ok": True}
    assert len(calls) == 3 and 7 in no_sleep and 2 in no_sleep  # Retry-After, then exponential backoff


def test_rate_limited_client_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, text="nope")

    c = RateLimitedClient(requests_per_minute=6000, transport=httpx.MockTransport(handler))
    with pytest.raises(SourceHTTPError) as e:
        c.get_json("https://api.test/x")
    assert e.value.status == 404 and len(calls) == 1


def test_rate_limit_spaces_requests(no_sleep):
    c = RateLimitedClient(requests_per_minute=60, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    for _ in range(3):
        c.get_json("https://api.test/x")
    assert len([s for s in no_sleep if s > 0.5]) == 2  # ~1 s between requests at 60/min


def test_download_size_limit(tmp_path):
    big = b"x" * 5000
    c = RateLimitedClient(requests_per_minute=6000, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big)))
    with pytest.raises(SourceHTTPError):
        c.download("https://cdn.test/v.mp4", tmp_path / "v.mp4", max_bytes=1000)


# ---------- without keys nothing breaks ----------

def test_connectors_without_keys_report_and_factory_keeps_working(settings, db, clips, monkeypatch):
    for k in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "WIKIMEDIA_CONTACT"):
        monkeypatch.delenv(k, raising=False)
    for name in ("pexels", "pixabay", "wikimedia", "telegram"):
        _enable(settings, name)
    sources = build_sources(settings, db)
    for name in ("pexels", "pixabay", "wikimedia"):
        ready, why = sources[name].configured()
        assert not ready and "нет ключей" in why, name
    assert "нет каналов" in sources["telegram"].configured()[1]
    assert "не реализовано" in sources["twitch"].configured()[1]
    shutil.copy(clips["landscape"], settings.data("inbox") / "a.mp4")
    report = Pipeline(settings, db, sources).run_once()
    assert report.processed == 1
    assert any("pexels" in n and "PEXELS_API_KEY" in n for n in report.notes)


# ---------- Pexels: discovery, licence, download, cross-source duplicate ----------

def _pexels_handler(media: bytes, seen: list):
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.host == "api.pexels.com":
            o = request.url.params["orientation"]
            vid = {"portrait": 1, "landscape": 2, "square": 3}[o]
            return httpx.Response(200, json={"videos": [{
                "id": vid, "url": f"https://www.pexels.com/video/{vid}/", "duration": 6,
                "width": 1080, "height": 1920, "user": {"name": "Anna", "url": "https://pexels.com/@anna"},
                "video_files": [
                    {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://cdn.test/4k.mp4"},
                    {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": f"https://cdn.test/{vid}.mp4"},
                    {"file_type": "video/mp4", "width": 540, "height": 960, "link": "https://cdn.test/sd.mp4"},
                ]}]})
        return httpx.Response(200, content=media)
    return handler


def test_pexels_discovery_prefers_portrait_and_saves_licence(settings, keys):
    _enable(settings, "pexels", per_query=1, queries=[{"query": "cooking", "category": "recipes"}])
    seen = []
    src = PexelsSource(settings, transport=httpx.MockTransport(_pexels_handler(b"", seen)))
    found = src.discover(set(), 10)
    assert [r.url.params["orientation"] for r in seen] == ["portrait", "landscape", "square"]
    first = found[0]
    assert first.source_id == "pexels:1" and first.media_url == "https://cdn.test/1.mp4"
    assert first.rights_status == "LICENSED" and first.license == "Pexels License"
    assert "Anna" in first.attribution and seen[0].headers["Authorization"] == "test-pexels_api_key"
    assert src.discover({"pexels:1", "pexels:2", "pexels:3"}, 10) == []


def test_pexels_through_pipeline_and_cross_source_duplicate(settings, db, keys, clips):
    _enable(settings, "pexels", per_query=1, orientations=["portrait"], queries=[{"query": "x", "category": "food"}])
    media = clips["landscape"].read_bytes()
    pexels = PexelsSource(settings, transport=httpx.MockTransport(_pexels_handler(media, [])))
    # The very same file also lies in the local inbox: must be caught by SHA-256 across sources.
    shutil.copy(clips["landscape"], settings.data("inbox") / "same.mp4")
    sources = {"pexels": pexels, "filesystem": FilesystemSource(settings)}
    Pipeline(settings, db, sources).run_once()
    items = {i.source: i for i in _items(db)}
    px, fs = items["pexels"], items["filesystem"]
    assert px.status == Status.READY_TO_PUBLISH and px.rights_status == RightsStatus.LICENSED
    assert px.license == "Pexels License" and px.attribution and px.source_url.startswith("https://www.pexels.com")
    assert json.loads(px.source_metadata)["pexels_video_id"] == 1
    assert fs.status == Status.DUPLICATE and fs.duplicate_of_id == px.id and "SHA-256" in fs.rejection_reason


def test_pixabay_discovery(settings, keys):
    _enable(settings, "pixabay", per_query=3, queries=[{"query": "food", "category": "food"}])

    def handler(request):
        assert request.url.params["key"] == "test-pixabay_api_key"
        return httpx.Response(200, json={"hits": [
            {"id": 7, "pageURL": "https://pixabay.com/videos/id-7/", "tags": "food, soup", "duration": 12,
             "user": "bob", "videos": {"large": {"url": "", "width": 0}, "medium": {"url": "https://cdn.test/m.mp4",
                                                                               "width": 1280, "height": 720}}},
            {"id": 8, "duration": 600, "videos": {"medium": {"url": "https://cdn.test/long.mp4", "width": 1}}},
        ]})
    found = PixabaySource(settings, transport=httpx.MockTransport(handler)).discover(set(), 10)
    assert len(found) == 1  # 600 s is over max_duration
    assert (found[0].media_url, found[0].license, found[0].category) == \
        ("https://cdn.test/m.mp4", "Pixabay Content License", "food")


# ---------- Wikimedia ----------

@pytest.mark.parametrize("name,expected", [
    ("CC0", "CC0"), ("Public domain", "PUBLIC_DOMAIN"), ("CC BY 4.0", "CC_BY"), ("CC BY-SA 3.0", "CC_BY_SA"),
    ("CC BY-NC 2.0", "REJECTED"), ("CC BY-ND 4.0", "REJECTED"), ("GFDL", "REQUIRES_REVIEW"), (None, "REQUIRES_REVIEW"),
])
def test_wikimedia_license_mapping(name, expected):
    assert map_license(name)[0] == expected


def test_wikimedia_discovery_blocks_noncommercial(settings, keys):
    _enable(settings, "wikimedia", per_query=5, queries=[{"query": "cat", "category": "animals"}])

    def page(pid, lic, mime="video/webm"):
        return {"pageid": pid, "title": f"File:Cat {pid}.webm", "imageinfo": [{
            "url": f"https://upload.test/{pid}.webm", "descriptionurl": f"https://commons.test/{pid}",
            "mime": mime, "size": 1000, "duration": 20, "user": "uploader",
            "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": "<a href='x'>Jane</a>"},
                            "LicenseUrl": {"value": "https://cc.test"}}}]}

    def handler(request):
        assert "ContentFactory" in request.headers["User-Agent"] and "test-wikimedia_contact" in request.headers["User-Agent"]
        return httpx.Response(200, json={"query": {"pages": [
            page(1, "CC BY-SA 4.0"), page(2, "CC BY-NC 2.0"), page(3, "CC0"), page(4, "CC0", mime="image/jpeg")]}})
    found = WikimediaSource(settings, transport=httpx.MockTransport(handler)).discover(set(), 10)
    assert [(f.source_id, f.rights_status) for f in found] == [("wikimedia:1", "CC_BY_SA"), ("wikimedia:3", "CC0")]
    assert found[0].author == "Jane" and "CC BY-SA 4.0" in found[0].attribution
    assert found[0].extra["commercial_use_allowed"] and found[0].extra["attribution_required"]


# ---------- Telegram: public web preview t.me/s ----------

def tg_page(channel: str, posts: list[dict], title: str = "Кухня") -> str:
    """Minimal copy of the t.me/s markup (the parts the parser relies on)."""
    out = [f'<html><body><div class="tgme_channel_info_header_title"><span>{title}</span></div>']
    for p in posts:
        pid = p["id"]
        media = ""
        if p.get("video") == "ok":
            media = (f'<a class="tgme_widget_message_video_player js-message_video_player" href="https://t.me/{channel}/{pid}">'
                     f'<video src="https://cdn.test/{channel}_{pid}.mp4?token=abc" class="tgme_widget_message_video"></video>'
                     f'<time class="message_video_duration js-message_video_duration">{p.get("dur", "0:06")}</time></a>')
        elif p.get("video") == "big":
            media = (f'<a class="tgme_widget_message_video_player not_supported js-message_video_player" '
                     f'href="https://t.me/{channel}/{pid}"><div class="message_media_not_supported">'
                     f'Media is too big</div><time class="message_video_duration">3:49</time></a>')
        out.append(f'<div class="tgme_widget_message" data-post="{channel}/{pid}">{media}'
                   f'<div class="tgme_widget_message_text">{p.get("text", "")}</div>'
                   f'<span class="tgme_widget_message_views">1.2K</span>'
                   f'<time datetime="2026-10-05T10:00:00+00:00"></time></div>')
    return "".join(out) + "</body></html>"


class FakeTelegramWeb:
    """Serves channel pages (newest first, 3 posts per page) and CDN files."""

    def __init__(self, channels: dict, media: bytes, page_size: int = 3):
        self.channels, self.media, self.page_size = channels, media, page_size
        self.requests: list[httpx.Request] = []
        self.blocked = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "cdn.test":
            media = self.media
            if isinstance(media, dict):  # per-channel files: cdn.test/<channel>_<post>.mp4
                media = media[request.url.path.strip("/").rsplit("_", 1)[0]]
            return httpx.Response(200, content=media)
        if self.blocked:
            return httpx.Response(429)
        parts = request.url.path.strip("/").split("/")
        if parts[0] == "s":
            ch = parts[1]
            if ch not in self.channels:  # no public preview: Telegram redirects to the channel card
                return httpx.Response(302, headers={"Location": f"https://t.me/{ch}"})
            posts = sorted(self.channels[ch], key=lambda p: p["id"], reverse=True)
            before = int(request.url.params.get("before", 10**9))
            page = [p for p in posts if p["id"] < before][: self.page_size]
            return httpx.Response(200, text=tg_page(ch, sorted(page, key=lambda p: p["id"])))
        if len(parts) == 1:
            return httpx.Response(200, text="<html>channel card</html>")
        ch, pid = parts
        post = next(p for p in self.channels[ch] if p["id"] == int(pid))
        return httpx.Response(200, text=tg_page(ch, [post]))


def test_parse_links_accepts_all_link_forms():
    text = "https://t.me/min2ru, t.me/s/lovecooking_daily\n@Twitch_NewsRu https://telegram.me/daniya_alt/123 " \
           "https://t.me/joinchat/xxxx t.me/min2ru"
    assert parse_links(text) == ["min2ru", "lovecooking_daily", "Twitch_NewsRu", "daniya_alt"]


def test_parse_page_videos_and_too_big():
    html = tg_page("ch", [{"id": 5, "video": "ok", "dur": "1:05", "text": "Рецепт пасты"},
                          {"id": 6, "video": "big"}, {"id": 7, "text": "просто текст"}])
    posts, oldest, title, newest = parse_page(html, "ch")
    assert (oldest, newest, title) == (5, 7, "Кухня")
    ok, big = posts
    assert (ok.post_id, ok.duration, ok.video_url, ok.text) == (5, 65.0, "https://cdn.test/ch_5.mp4?token=abc", "Рецепт пасты")
    assert big.video_url is None and "только превью" in big.skip_reason


def _tg_settings(settings, **cfg):
    settings.raw["sources"]["telegram"] = {"enabled": True, "page_interval_seconds": 0.01, "initial_pages": 3,
                                           "max_pages": 10, "per_channel": 20, **cfg}


def test_telegram_incremental_scan(settings, clips):
    _tg_settings(settings)
    web = FakeTelegramWeb({"ch": [{"id": i, "video": "ok", "text": f"пост {i}"} for i in range(1, 8)]
                                 + [{"id": 8, "video": "big"}, {"id": 9, "text": "текст"}]}, b"")
    src = TelegramSource(settings, transport=httpx.MockTransport(web))
    first = src.scan(ChannelSpec(None, "ch"), set())
    # 3 pages × 3 posts: posts 9..1, 8 is too big for the web preview
    assert [i.source_id for i in first.items] == [f"telegram:ch/{i}" for i in range(7, 0, -1)]
    assert first.newest_post_id == 9 and len(first.skipped) == 1 and "ch/8" in first.skipped[0]
    assert all(i.rights_status == "REQUIRES_REVIEW" for i in first.items)

    web.channels["ch"] += [{"id": 10, "video": "ok"}, {"id": 11, "video": "ok"}]
    web.requests.clear()
    second = src.scan(ChannelSpec(None, "ch", last_seen_post_id=9), set())
    assert [i.source_id for i in second.items] == ["telegram:ch/11", "telegram:ch/10"]
    assert len([r for r in web.requests if r.url.path.startswith("/s/")]) == 1  # stopped at the last seen post


def test_telegram_blocked_and_no_preview(settings):
    _tg_settings(settings)
    web = FakeTelegramWeb({}, b"")
    src = TelegramSource(settings, transport=httpx.MockTransport(web))
    with pytest.raises(SourceHTTPError, match="нет публичной веб-версии"):
        src.scan(ChannelSpec(None, "private_one"), set())
    web.channels["ch"] = [{"id": 1, "video": "ok"}]
    web.blocked = True
    with pytest.raises(TelegramBlocked):
        src.scan(ChannelSpec(None, "ch"), set())


def test_telegram_through_pipeline_with_db_channels(settings, db, clips):
    from app.database.models import Channel
    _tg_settings(settings)
    with db.session() as s:
        s.add_all([Channel(username="ch", category="recipes", mode="import"),
                   Channel(username="watch", category="stream_clips", mode="discovery"),
                   Channel(username="off", enabled=False)])
    web = FakeTelegramWeb({"ch": [{"id": 1, "video": "ok", "text": "Готовим пасту с чесноком"}],
                           "watch": [{"id": 3, "video": "ok"}], "off": [{"id": 1, "video": "ok"}]},
                          clips["landscape"].read_bytes())
    src = TelegramSource(settings, transport=httpx.MockTransport(web), db=db)
    Pipeline(settings, db, {"telegram": src}).run_once()
    by_id = {i.source_id: i for i in _items(db)}
    assert set(by_id) == {"telegram:ch/1", "telegram:watch/3"}
    assert by_id["telegram:ch/1"].status == Status.MANUAL_REVIEW  # REQUIRES_REVIEW → manual review
    assert by_id["telegram:watch/3"].status == Status.DISCOVERY_ONLY
    with db.session() as s:
        ch = s.scalars(select(Channel).where(Channel.username == "ch")).one()
        assert (ch.state, ch.last_seen_post_id, ch.found_count, ch.title) == ("ok", 1, 1, "Кухня")


def test_telegram_expired_cdn_link_is_refreshed(settings, clips, tmp_path):
    _tg_settings(settings)
    web = FakeTelegramWeb({"ch": [{"id": 4, "video": "ok"}]}, clips["landscape"].read_bytes())
    calls = {"n": 0}

    def handler(request):
        if request.url.host == "cdn.test" and request.url.params.get("token") == "old":
            calls["n"] += 1
            return httpx.Response(403)
        return web(request)
    src = TelegramSource(settings, transport=httpx.MockTransport(handler))
    dest = tmp_path / "v.mp4"
    src.download("telegram:ch/4", dest, "https://cdn.test/ch_4.mp4?token=old")
    assert calls["n"] == 1 and probe(dest).has_audio


def test_metadata_methods(settings, keys):
    _tg_settings(settings)
    web = FakeTelegramWeb({"ch": [{"id": 4, "video": "ok", "text": "Пост"}, {"id": 5, "video": "big"}]}, b"")
    tg = TelegramSource(settings, transport=httpx.MockTransport(web))
    m = tg.metadata("telegram:ch/4")
    assert (m["url"], m["text"], m["available_on_web"], m["channel_title"]) == ("https://t.me/ch/4", "Пост", True, "Кухня")
    assert tg.metadata("telegram:ch/5")["available_on_web"] is False

    _enable(settings, "pexels")
    px = PexelsSource(settings, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "id": 9, "url": "https://www.pexels.com/video/9/", "duration": 7, "user": {"name": "Anna"}})))
    assert px.metadata("pexels:9") == {"url": "https://www.pexels.com/video/9/", "author": "Anna", "duration": 7,
                                       "width": None, "height": None, "license": "Pexels License"}
