"""Optional official Telegram API (Telethon): login from the UI, big videos via API, API channel scans.
A fake client stands in for Telethon: no network, no real account."""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from telethon import errors

from app.sources.telegram import ChannelSpec, TelegramSource
from app.sources.telegram_api import TelegramAPI, TelegramAPIError
from tests.test_sources import FakeTelegramWeb


def _msg(mid, video=True, text="", group=None, noforwards=False, duration=20.0, size=50 * 1024 * 1024):
    f = SimpleNamespace(mime_type="video/mp4" if video else "image/jpeg", duration=duration if video else None,
                        size=size, width=1080, height=1920)
    return SimpleNamespace(id=mid, video=video or None, file=f, message=text, grouped_id=group,
                           noforwards=noforwards, date=datetime(2026, 10, 5, 10), views=100)


class FakeTelethon:
    def __init__(self, messages=None, two_fa=False, authorized=False, payload=b"BIGVIDEO"):
        self.messages = {m.id: m for m in (messages or [])}
        self.two_fa, self.authorized, self.payload = two_fa, authorized, payload
        self.downloaded: list[int] = []
        self.code_requests = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(username="owner", id=1)

    async def send_code_request(self, phone):
        self.code_requests += 1
        from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeSms
        return SimpleNamespace(phone_code_hash="HASH", type=SentCodeTypeApp(length=5),
                               next_type=SentCodeTypeSms(length=5), timeout=0)

    async def __call__(self, request):  # auth.ResendCodeRequest
        from telethon.tl.types.auth import SentCodeTypeSms
        return SimpleNamespace(phone_code_hash="HASH", type=SentCodeTypeSms(length=5), next_type=None, timeout=0)

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != "secret":
                raise errors.PasswordHashInvalidError(request=None)
            self.authorized = True
            return
        if code != "12345" or phone_code_hash != "HASH":
            raise errors.PhoneCodeInvalidError(request=None)
        if self.two_fa:
            raise errors.SessionPasswordNeededError(request=None)
        self.authorized = True

    async def log_out(self):
        self.authorized = False

    async def get_entity(self, username):
        return SimpleNamespace(title="Кухня", noforwards=False)

    async def iter_messages(self, entity, limit=100, min_id=0):
        for mid in sorted(self.messages, reverse=True)[:limit]:
            if mid > min_id:
                yield self.messages[mid]

    async def get_messages(self, channel, ids):
        if isinstance(ids, list):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)

    async def download_media(self, msg, file):
        self.downloaded.append(msg.id)
        Path(file).write_bytes(self.payload + str(msg.id).encode())
        return file


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")


def test_login_with_code_and_cloud_password(settings, keys):
    fake = FakeTelethon(two_fa=True)
    api = TelegramAPI(settings, client_factory=lambda: fake)
    assert api.status()["state"] == "need_login"
    with pytest.raises(TelegramAPIError, match="Получить код"):
        api.sign_in("12345")
    with pytest.raises(TelegramAPIError, match="международном"):
        api.send_code("123")
    st = api.send_code("+7 999 123-45-67")
    assert st["state"] == "code_sent" and "•••" in st["phone"]
    assert "приложение Telegram" in st["delivery"] and st["next"] == "по SMS"
    st = api.resend_code()
    assert st["delivery"] == "по SMS" and st["next"] is None
    with pytest.raises(TelegramAPIError, match="неверный код"):
        api.sign_in("00000")
    assert api.sign_in("12345")["state"] == "password"
    with pytest.raises(TelegramAPIError, match="облачный пароль"):
        api.sign_in(password="wrong")
    st = api.sign_in(password="secret")
    assert st["state"] == "ok" and st["user"] == "owner"
    assert api.logout()["state"] == "need_login"


def test_without_keys_api_stays_off(settings, monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    api = TelegramAPI(settings)
    assert api.status()["state"] == "no_keys" and not api.authorized()


def _web(settings, db, api, channels, media):
    settings.raw["sources"]["telegram"].update(page_interval_seconds=0.01)
    settings.raw["filters"]["max_duration"] = 600
    return TelegramSource(settings, transport=httpx.MockTransport(FakeTelegramWeb(channels, media)), db=db, api=api)


def test_big_video_is_skipped_without_api_and_downloaded_with_it(settings, db, keys, tmp_path, monkeypatch):
    monkeypatch.setattr("app.sources.http.time.sleep", lambda s: None)
    channels = {"cooking": [{"id": 1, "video": "ok", "text": "Маленькое"}, {"id": 2, "video": "big", "text": "Большое"}]}
    spec = ChannelSpec(1, "cooking")

    off = TelegramAPI(settings, client_factory=lambda: FakeTelethon(authorized=False))
    res = _web(settings, db, off, channels, b"small").scan(spec, set())
    assert [i.source_id for i in res.items] == ["telegram:cooking/1"] and "слишком большое" in res.skipped[0]

    fake = FakeTelethon(messages=[_msg(1), _msg(2, text="Большое")], authorized=True)
    src = _web(settings, db, TelegramAPI(settings, client_factory=lambda: fake), channels, b"small")
    res = src.scan(spec, set())
    by_id = {i.source_id: i for i in res.items}
    assert set(by_id) == {"telegram:cooking/1", "telegram:cooking/2"} and res.skipped == []
    big = by_id["telegram:cooking/2"]
    assert big.media_url is None and big.extra["via"] == "api" and big.rights_status == "REQUIRES_REVIEW"

    dest = tmp_path / "big.mp4"
    src.download(big.source_id, dest, big.media_url)
    assert dest.read_bytes() == b"BIGVIDEO2" and fake.downloaded == [2]
    small = tmp_path / "small.mp4"
    src.download("telegram:cooking/1", small, by_id["telegram:cooking/1"].media_url)
    assert small.read_bytes() == b"small" and fake.downloaded == [2]  # small ones still come from the web


def test_api_channel_scan_matches_web_ids(settings, db, keys):
    fake = FakeTelethon(messages=[
        _msg(10, text="Один"), _msg(11, video=False, text="фото"),
        _msg(20, video=False, group=7, text="Альбом"), _msg(21, group=7), _msg(22, group=7),
        _msg(30, noforwards=True), _msg(31, duration=900), _msg(32, text="Уже есть"),
    ], authorized=True)
    src = TelegramSource(settings, db=db, api=TelegramAPI(settings, client_factory=lambda: fake))
    res = src.scan(ChannelSpec(1, "cooking", via="api"), {"telegram:cooking/32"})
    ids = [i.source_id for i in res.items]
    assert ids == ["telegram:cooking/20-2", "telegram:cooking/20", "telegram:cooking/10"]
    assert res.items[1].caption == "Альбом" and res.newest_post_id == 32 and res.title == "Кухня"
    assert any("защищённый" in s for s in res.skipped) and any("900" in s for s in res.skipped)

    # the 2nd video of the album downloads the right message
    dest = Path(settings.data("temp")) / "a.mp4"
    src.download("telegram:cooking/20-2", dest)
    assert fake.downloaded == [22]


def test_api_channel_falls_back_to_web_without_login(settings, db, keys, monkeypatch):
    monkeypatch.setattr("app.sources.http.time.sleep", lambda s: None)
    src = _web(settings, db, TelegramAPI(settings, client_factory=lambda: FakeTelethon(authorized=False)),
               {"cooking": [{"id": 1, "video": "ok"}]}, b"x")
    res = src.scan(ChannelSpec(1, "cooking", via="api"), set())
    assert [i.source_id for i in res.items] == ["telegram:cooking/1"]


def test_web_endpoints_store_keys_without_echo(settings, db, monkeypatch):
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    (settings.root / ".env").write_text("TELEGRAM_API_ID=\n", encoding="utf-8")
    worker = FactoryWorker(settings, db)
    fake = FakeTelethon()
    with TestClient(create_app(settings, db, worker, start_worker=False)) as web:
        assert web.get("/api/telegram-api").json()["state"] == "no_keys"
        assert web.post("/api/telegram-api/keys", json={"api_id": "abc", "api_hash": "x"}).status_code == 400
        worker.telegram.api._factory = lambda: fake
        r = web.post("/api/telegram-api/keys", json={"api_id": "123", "api_hash": "0123456789abcdef0123456789abcdef"})
        assert r.status_code == 200 and r.json()["state"] == "need_login" and "0123456789abcdef" not in r.text
        env = (settings.root / ".env").read_text()
        assert "TELEGRAM_API_ID=123" in env and "TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef" in env
        assert web.post("/api/telegram-api/send-code", json={"phone": "+79991234567"}).json()["state"] == "code_sent"
        assert web.post("/api/telegram-api/sign-in", json={"code": "1"}).status_code == 400
        assert web.post("/api/telegram-api/sign-in", json={"code": "12345"}).json()["state"] == "ok"

        from app.database.models import Channel
        with db.session() as s:  # added directly: the API route would start a real scan
            ch = Channel(username="cooking")
            s.add(ch)
            s.flush()
            cid = ch.id
        assert web.patch(f"/api/channels/{cid}", json={"via": "api"}).json()["ok"]
        assert web.get("/api/channels").json()["channels"][0]["via"] == "api"
        assert web.patch(f"/api/channels/{cid}", json={"via": "x"}).status_code == 400


def test_login_survives_server_restart(settings, keys):
    fake = FakeTelethon()
    api = TelegramAPI(settings, client_factory=lambda: fake)
    api.send_code("+79991234567")
    login_file = settings.path("credentials/telegram_login.json")
    assert login_file.exists()
    if sys.platform != "win32":  # Windows has no unix file modes
        assert oct(login_file.stat().st_mode)[-3:] == "600"
    import json as _json
    assert "code" not in _json.loads(login_file.read_text())  # the code itself is never stored
    restarted = TelegramAPI(settings, client_factory=lambda: fake)  # new process, same session file
    assert restarted.status()["state"] == "code_sent"
    assert restarted.sign_in("12345")["state"] == "ok"
    assert not login_file.exists()
    with pytest.raises(TelegramAPIError, match="Получить код"):
        TelegramAPI(settings, client_factory=lambda: FakeTelethon()).sign_in("12345")


class FakeAccount(FakeTelethon):
    """A signed-in account with subscriptions: two public channels, a private one, a group, a person."""
    DIALOGS = [
        SimpleNamespace(is_channel=True, entity=SimpleNamespace(id=101, username="cooking", title="Кухня", participants_count=5000, broadcast=True)),
        SimpleNamespace(is_channel=True, entity=SimpleNamespace(id=102, username="memes", title="Мемы", participants_count=1200, broadcast=True)),
        SimpleNamespace(is_channel=True, entity=SimpleNamespace(id=103, username=None, title="Закрытый спорт", participants_count=40, broadcast=True)),
        SimpleNamespace(is_channel=True, entity=SimpleNamespace(id=104, username="chat", title="Чат", participants_count=10, broadcast=False, megagroup=True)),
        SimpleNamespace(is_channel=False, entity=SimpleNamespace(id=5, username="friend", first_name="Друг")),
    ]

    def __init__(self, **kw):
        super().__init__(authorized=True, **kw)
        self.calls: list[str] = []

    async def iter_dialogs(self):
        self.calls.append("iter_dialogs")
        for d in self.DIALOGS:
            yield d

    async def get_entity(self, peer):
        self.calls.append(f"get_entity:{getattr(peer, 'channel_id', peer)}")
        return SimpleNamespace(title="Канал", noforwards=False)


def test_account_channels_ticks_and_private_scan(settings, db, keys):
    from app.database.models import Channel
    from app.sources import tg_account
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    with db.session() as s:  # added earlier by link: must not be duplicated
        s.add(Channel(username="Cooking", category="recipes", enabled=True, added_by="link"))
    fake = FakeAccount(messages=[_msg(7, text="Видео")])
    worker = FactoryWorker(settings, db)
    worker.telegram.api._factory = lambda: fake
    queued = []
    worker.enqueue_channel = lambda cid: queued.append(cid) or True
    with TestClient(create_app(settings, db, worker, start_worker=False)) as web:
        r = web.post("/api/telegram-api/channels/refresh").json()
        assert r["found"] == 4 and r["added"] == 3  # the person is not a source; @cooking already existed
        chans = {c["username"]: c for c in r["channels"]}
        assert set(chans) == {"Cooking", "memes", "id103", "chat"}
        assert chans["Cooking"]["enabled"] and not chans["memes"]["enabled"] and not chans["id103"]["public"]
        # unticked account channels are not shown among the factory's channels
        assert {c["username"] for c in web.get("/api/channels").json()["channels"] if c["added_by"] != "account" or c["enabled"]} == {"Cooking"}
        r = web.post("/api/telegram-api/channels/select", json={"ids": [chans["memes"]["id"], chans["id103"]["id"]], "enabled": True}).json()
        assert {c["username"] for c in r["channels"] if c["enabled"]} == {"Cooking", "memes", "id103"}
        assert sorted(queued) == sorted([chans["memes"]["id"], chans["id103"]["id"]])  # parsed at once
        web.post("/api/telegram-api/channels/refresh")  # refresh keeps the ticks
        assert {c["username"] for c in web.get("/api/telegram-api/channels").json()["channels"] if c["enabled"]} == {"Cooking", "memes", "id103"}
    with db.session() as s:
        priv = s.scalars(__import__("sqlalchemy").select(Channel).where(Channel.username == "id103")).one()
        assert priv.via == "api" and priv.tg_id == 103
    # a private channel is read through its id; links point to t.me/c/
    spec = ChannelSpec(priv.id, "id103", "other", "import", None, 50, "api")
    res = worker.telegram.api.scan(spec, set(), 10, {}, {})
    assert "get_entity:103" in fake.calls and res.items[0].source_url == "https://t.me/c/103/7"
    # read only: the fake has no join/send methods, and none were needed
    assert not any(c.startswith(("join", "send")) for c in fake.calls)
    # unsubscribed in Telegram: the channel leaves the account list and stops being parsed
    FakeAccount.DIALOGS, saved = FakeAccount.DIALOGS[:1], FakeAccount.DIALOGS
    try:
        tg_account.refresh(db, worker.telegram.api)
    finally:
        FakeAccount.DIALOGS = saved
    with db.session() as s:
        memes = s.scalars(__import__("sqlalchemy").select(Channel).where(Channel.username == "memes")).one()
        assert not memes.in_account and not memes.enabled


def test_deleted_channel_stays_gone(settings, db, keys):
    import sqlalchemy as sa
    from app.database.models import Channel, ContentItem, RightsStatus, Status
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    fake = FakeAccount()
    worker = FactoryWorker(settings, db)
    worker.telegram.api._factory = lambda: fake
    worker.enqueue_channel = lambda cid: True
    raw = settings.data("raw")
    raw.mkdir(parents=True, exist_ok=True)
    unused, ready = raw / "u.mp4", raw / "r.mp4"
    unused.write_bytes(b"0" * 1000)
    ready.write_bytes(b"0" * 1000)
    with db.session() as s:
        s.add_all([
            ContentItem(source="telegram", source_id="a", channel="memes", status=Status.APPROVED,
                        rights_status=RightsStatus.AUTO_APPROVED, original_file=str(unused)),
            ContentItem(source="telegram", source_id="b", channel="memes", status=Status.READY_TO_PUBLISH,
                        rights_status=RightsStatus.AUTO_APPROVED, original_file=str(ready)),
        ])
    with TestClient(create_app(settings, db, worker, start_worker=False)) as web:
        chans = {c["username"]: c["id"] for c in web.post("/api/telegram-api/channels/refresh").json()["channels"]}
        web.post("/api/telegram-api/channels/select", json={"ids": [chans["memes"]], "enabled": True})
        r = web.post("/api/channels/delete-many", json={"ids": [chans["memes"], chans["chat"]]}).json()
        assert r["channels"] == 2 and r["items"] == 1 and r["freed"] == 1000
        assert not unused.exists() and ready.exists()  # rendered videos stay
        web.post("/api/telegram-api/channels/refresh")  # the account still has them: they must not come back
        names = {c["username"] for c in web.get("/api/telegram-api/channels").json()["channels"]}
        assert "memes" not in names and "chat" not in names
        assert "memes" not in {c["username"] for c in web.get("/api/channels").json()["channels"]}
        assert {c["username"] for c in web.get("/api/channels/hidden").json()["channels"]} == {"memes", "chat"}
        with db.session() as s:
            assert not s.scalars(sa.select(Channel).where(Channel.username == "memes")).one().enabled
            st = {i.source_id: i.status for i in s.scalars(sa.select(ContentItem))}
        assert st == {"a": Status.REJECTED, "b": Status.READY_TO_PUBLISH}
        # «Вернуть» puts it back in the account list, unticked; adding the link again restores it too
        web.post(f"/api/channels/{chans['memes']}/restore")
        back = {c["username"]: c for c in web.get("/api/telegram-api/channels").json()["channels"]}
        assert "memes" in back and not back["memes"]["enabled"]
        r = web.post("/api/channels", json={"links": "t.me/chat"}).json()
        assert r["restored"] == ["chat"] and web.get("/api/channels/hidden").json()["channels"] == []
