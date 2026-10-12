"""Public Telegram channels through the official web preview (https://t.me/s/<channel>), no login, no keys.

Only what the public page shows is used. Nothing is worked around: videos the web preview does not
serve ("too big", "view in Telegram") are skipped with a reason; a channel without a public preview is
skipped; on HTTP 429, a block or a captcha page the source stops for this run and logs it.
Everything found is REQUIRES_REVIEW.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from app.sources.base import ApiConnector, DiscoveredItem
from app.sources.http import SourceHTTPError

log = logging.getLogger("sources.telegram")
BASE = "https://t.me"


class TelegramBlocked(RuntimeError):
    """Rate limit, block or captcha: stop polling Telegram for this run."""


@dataclass
class WebPost:
    channel: str
    post_id: int
    url: str
    text: str | None
    published_at: datetime | None
    views: str | None
    video_url: str | None
    duration: float | None
    skip_reason: str | None  # set when the post has a video the web preview does not serve
    index: int = 0           # position inside an album

    @property
    def source_id(self) -> str:
        return f"telegram:{self.channel}/{self.post_id}" + (f"-{self.index + 1}" if self.index else "")


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    parts = [int(p) for p in re.findall(r"\d+", text)]
    total = 0
    for p in parts:
        total = total * 60 + p
    return float(total) if parts else None


def parse_page(html: str, channel: str) -> tuple[list[WebPost], int | None, str | None, int | None]:
    """→ (posts with video, oldest post id for ?before= paging, channel title, newest post id)."""
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(strip=True) if title_el else None
    posts: list[WebPost] = []
    ids: list[int] = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        ch, _, pid = msg["data-post"].rpartition("/")
        if not pid.isdigit():
            continue
        ids.append(int(pid))
        players = msg.select(".tgme_widget_message_video_player")
        if not players:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        time_el = msg.select_one("time[datetime]")
        views_el = msg.select_one(".tgme_widget_message_views")
        published = None
        if time_el:
            try:
                published = datetime.fromisoformat(time_el["datetime"]).astimezone().replace(tzinfo=None)
            except ValueError:
                pass
        # An album holds several players in one post; each video gets its own index.
        for index, player in enumerate(players):
            video = player.select_one("video")
            dur_el = player.select_one(".message_video_duration")
            video_url = video.get("src") if video is not None else None
            reason = None
            # Every player carries a hidden "not supported" fallback block; only the class on the
            # player itself says the web preview refuses to serve this file.
            if "not_supported" in (player.get("class") or []):
                reason = "веб-версия показывает только превью: «смотреть в Telegram» (видео слишком большое)"
            elif not video_url:
                reason = "веб-версия не отдаёт это видео"
            posts.append(WebPost(
                channel=ch or channel, post_id=int(pid), url=f"{BASE}/{ch or channel}/{pid}",
                text=text_el.get_text("\n", strip=True) if text_el else None, published_at=published,
                views=views_el.get_text(strip=True) if views_el else None,
                video_url=video_url if reason is None else None,
                duration=parse_duration(dur_el.get_text() if dur_el else None),
                skip_reason=reason, index=index,
            ))
    return posts, (min(ids) if ids else None), title, (max(ids) if ids else None)


def looks_blocked(html: str) -> bool:
    low = html.lower()
    return "captcha" in low or "too many requests" in low


LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(?P<u1>[A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#][^\s]*)?"
    r"|(?<![\w/])@(?P<u2>[A-Za-z][A-Za-z0-9_]{3,31})\b")
RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "login", "c"}


def parse_links(text: str) -> list[str]:
    """Channel usernames from pasted text: t.me/x, t.me/s/x, telegram.me/x, @x (several at once)."""
    out: list[str] = []
    for m in LINK_RE.finditer(text or ""):
        name = (m.group("u1") or m.group("u2") or "").strip()
        if name and name.lower() not in RESERVED and name.lower() not in [o.lower() for o in out]:
            out.append(name)
    return out


@dataclass
class ChannelSpec:
    id: int | None
    username: str
    category: str = "other"
    mode: str = "import"
    last_seen_post_id: int | None = None
    source_score: float = 50
    via: str = "web"  # web | api


@dataclass
class ScanResult:
    items: list[DiscoveredItem]
    skipped: list[str]
    newest_post_id: int | None
    title: str | None


class TelegramSource(ApiConnector):
    name = "telegram"
    required_env = ()

    def __init__(self, settings, transport=None, db=None, persist: bool = True, api=None):
        super().__init__(settings, transport)
        self.transport = transport
        self.db = db
        self.persist = persist and db is not None and not getattr(db, "readonly", False)
        if api is None:
            from app.sources.telegram_api import TelegramAPI
            api = TelegramAPI(settings)
        self.api = api

    def api_ready(self) -> bool:
        """The official API is used only after the owner signed in from «Источники»."""
        try:
            return self.api is not None and self.api.authorized()
        except Exception:
            return False

    def default_headers(self) -> dict:
        return {"Accept-Language": "ru,en;q=0.8"}

    def _page_client(self):
        """Own client per channel scan: channels can be scanned in parallel, each with its own pause."""
        from app.sources.http import RateLimitedClient
        return RateLimitedClient(
            requests_per_minute=60.0 / max(float(self.cfg.get("page_interval_seconds", 3)), 0.01),
            retries=int(self.cfg.get("retries", 3)), headers=self.default_headers(), transport=self.transport)

    def channels(self) -> list[ChannelSpec]:
        if self.db is None:
            return []
        from sqlalchemy import select

        from app.database.models import Channel
        with self.db.session() as s:
            rows = s.scalars(select(Channel).where(Channel.platform == "telegram", Channel.enabled.is_(True))
                             .order_by(Channel.id))
            return [ChannelSpec(c.id, c.username, c.category, c.mode, c.last_seen_post_id,
                                self.cfg.get("source_score", 50), c.via or "web") for c in rows]

    def configured(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "выключен в config.yaml"
        if not self.channels():
            return False, "нет каналов: добавьте ссылки на странице «Источники»"
        return True, ""

    def _page(self, client, channel: str, before: int | None) -> str:
        params = {"before": before} if before else None
        try:
            r = client.request("GET", f"{BASE}/s/{channel}", params=params)
        except SourceHTTPError as exc:
            if exc.status in (403, 429) or exc.status is None:
                raise TelegramBlocked(f"Telegram ограничил доступ ({exc}); опрос остановлен до следующей проверки") from exc
            raise
        if looks_blocked(r.text):
            raise TelegramBlocked("Telegram показал проверку/ограничение; опрос остановлен до следующей проверки")
        if f"/s/{channel}".lower() not in str(r.url).lower():
            raise SourceHTTPError(f"у канала @{channel} нет публичной веб-версии")
        return r.text

    def scan(self, spec: ChannelSpec, known_ids: set[str], limit: int | None = None) -> ScanResult:
        """New posts only: pages go back until the last post seen in the previous scan.

        With the Telegram API signed in: «API» channels are scanned through it, and in web mode the videos the
        web preview refuses («too big») are kept and later downloaded through the API instead of skipped."""
        api_ok = self.api_ready()
        if spec.via == "api" and api_ok:
            return self.api.scan(spec, known_ids, limit or int(self.cfg.get("per_channel", 20)), self.cfg, self.filters)
        min_d, max_d = float(self.filters.get("min_duration", 0)), float(self.filters.get("max_duration", 1e9))
        first_scan = spec.last_seen_post_id is None
        max_pages = int(self.cfg.get("initial_pages" if first_scan else "max_pages", 3 if first_scan else 10))
        # the first look at a new channel takes only a few recent videos, so adding many channels at once
        # does not flood downloads and analysis; later checks pick up everything new
        per_scan = limit or int(self.cfg.get("initial_per_channel" if first_scan else "per_channel",
                                             self.cfg.get("per_channel", 20)))
        client = self._page_client()
        items: list[DiscoveredItem] = []
        skipped: list[str] = []
        newest, title, before = spec.last_seen_post_id, None, None
        try:
            for _ in range(max_pages):
                posts, oldest, page_title, page_newest = parse_page(self._page(client, spec.username, before),
                                                                    spec.username)
                title = title or page_title
                if page_newest:
                    newest = max(newest or 0, page_newest)
                for p in sorted(posts, key=lambda x: (x.post_id, -x.index), reverse=True):
                    newest = max(newest or 0, p.post_id)
                    sid = p.source_id
                    if (spec.last_seen_post_id and p.post_id <= spec.last_seen_post_id) or sid in known_ids:
                        continue
                    if p.skip_reason and not api_ok:
                        skipped.append(f"{p.url}: {p.skip_reason}")
                        continue
                    if p.duration is not None and not (min_d <= p.duration <= max_d):
                        skipped.append(f"{p.url}: длительность {p.duration:.0f} с вне фильтра {min_d:g}–{max_d:g} с")
                        continue
                    if len(items) >= per_scan:
                        continue
                    items.append(DiscoveredItem(
                        source=self.name, source_id=sid, source_url=p.url,
                        title=(p.text or "").split("\n", 1)[0][:200] or f"{spec.username} #{p.post_id}",
                        author=title or spec.username, channel=spec.username, category=spec.category,
                        caption=p.text, published_at=p.published_at, rights_status="REQUIRES_REVIEW",
                        media_url=p.video_url, duration=p.duration, discovery_only=spec.mode == "discovery",
                        attribution=f"{title or spec.username} (t.me/{spec.username})",
                        extra={"source_score": spec.source_score, "post_id": p.post_id, "views": p.views,
                               **({"via": "api"} if p.skip_reason else {})},
                    ))
                if oldest is None:
                    newest = newest or 0
                    break
                if oldest <= 1 or oldest == before or (spec.last_seen_post_id and oldest <= spec.last_seen_post_id):
                    break
                before = oldest
        finally:
            client.close()
        return ScanResult(items, skipped, newest, title)

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        """CLI path (`run`): scan every enabled channel in turn and record its stats."""
        found: list[DiscoveredItem] = []
        for spec in self.channels():
            if len(found) >= limit:
                break
            try:
                res = self.scan(spec, known_ids, limit - len(found))
            except TelegramBlocked as exc:
                self.record(spec.id, error=str(exc))
                raise
            except Exception as exc:
                self.record(spec.id, error=str(exc))
                log.warning("TELEGRAM channel %s: %s", spec.username, exc)
                continue
            self.record(spec.id, result=res)
            found.extend(res.items)
        return found

    def record(self, channel_id: int | None, result: ScanResult | None = None, error: str | None = None) -> None:
        if not self.persist or channel_id is None:
            return
        import json as _json

        from app.database.models import Channel, utcnow
        with self.db.session() as s:
            ch = s.get(Channel, channel_id)
            if ch is None:
                return
            ch.last_checked_at = utcnow()
            if error:
                ch.state, ch.last_error = "error", error[:500]
                return
            ch.state, ch.last_error = "ok", None
            ch.title = result.title or ch.title
            ch.last_seen_post_id = result.newest_post_id or ch.last_seen_post_id
            ch.found_count = (ch.found_count or 0) + len(result.items)
            ch.skipped_count = (ch.skipped_count or 0) + len(result.skipped)
            if result.skipped:
                ch.last_skipped = _json.dumps(result.skipped[-10:], ensure_ascii=False)

    def metadata(self, item_source_id: str) -> dict:
        channel, post_ref = item_source_id.removeprefix("telegram:").rsplit("/", 1)
        r = self.client.request("GET", f"{BASE}/{channel}/{post_ref.split('-', 1)[0]}",
                                params={"embed": "1", "mode": "tme"})
        posts, _, title, _ = parse_page(r.text, channel)
        post = next((p for p in posts if p.source_id == item_source_id), None)
        if post is None:
            return {}
        return {"url": post.url, "channel": channel, "channel_title": title, "text": post.text,
                "published_at": post.published_at, "views": post.views, "duration": post.duration,
                "available_on_web": post.skip_reason is None, "skip_reason": post.skip_reason}

    def _fresh_video_url(self, source_id: str) -> str | None:
        """CDN links carry a token that expires; the single-post embed page gives a fresh one."""
        channel, post_ref = source_id.removeprefix("telegram:").rsplit("/", 1)
        post_id = post_ref.split("-", 1)[0]
        r = self.client.request("GET", f"{BASE}/{channel}/{post_id}", params={"embed": "1", "mode": "tme"})
        posts, _, _, _ = parse_page(r.text, channel)
        post = next((p for p in posts if p.source_id == source_id), None)
        if post is None or post.skip_reason:
            raise RuntimeError(post.skip_reason if post else "пост больше не показывает видео")
        return post.video_url

    def download(self, item_source_id: str, dest: Path, media_url: str | None = None) -> Path:
        if not media_url and self.api_ready():  # found through the API or too big for the web preview
            return self.api.download(item_source_id, dest)
        try:
            if not media_url:
                raise SourceHTTPError("нет ссылки", 404)
            return self.client.download(media_url, dest, self.max_bytes)
        except SourceHTTPError as exc:
            if exc.status not in (403, 404, 410):
                raise
        try:
            fresh = self._fresh_video_url(item_source_id)
        except RuntimeError:
            if self.api_ready():
                return self.api.download(item_source_id, dest)
            raise RuntimeError("видео недоступно в веб-версии; большие видео качаются через Telegram API "
                               "(«Источники» → Telegram API)") from None
        return self.client.download(fresh, dest, self.max_bytes)
