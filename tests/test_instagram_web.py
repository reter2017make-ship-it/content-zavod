"""Instagram AutoPoster, stage 2: separate Chrome profile, manual sign-in, session check. A fake Playwright
stands in for Chrome: no browser, no network, no account."""
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.instagram_web.service import InstagramService
from app.web.server import create_app


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def inner_text(self, timeout=None):
        return self.page.body

    def all(self):
        return [FakeLink(h, alt) for h, alt in self.page.links] if self.sel.startswith("a[") else []

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None):
        if self.sel.startswith("input") and not self.page.username_field:
            raise TimeoutError("no field")

    def input_value(self):
        return self.page.username_field or ""

    def count(self):
        return 1


class FakeLink:
    def __init__(self, href, alt):
        self.href, self.alt = href, alt

    def get_attribute(self, name):
        return self.href if name == "href" else self.alt

    def locator(self, sel):
        return SimpleNamespace(count=lambda: 1 if self.alt is not None else 0,
                               first=SimpleNamespace(get_attribute=lambda n: self.alt))


class FakePage:
    def __init__(self, account):
        self.account, self.url, self.closed = account, "about:blank", False
        self.visited = []

    @property
    def body(self):
        return "Подтвердите, что это вы" if self.account.get("checkpoint") else "Главная"

    @property
    def links(self):
        if not self.account.get("signed_in"):
            return []
        return [("/explore/", None), ("/reels/", None)] + ([("/" + self.account["name"] + "/", "Фото профиля " + self.account["name"])]
                                                            if self.account.get("nav") else [])

    @property
    def username_field(self):
        return self.account.get("name") if self.account.get("signed_in") and self.url.endswith("/accounts/edit/") else None

    def goto(self, url, **kw):
        self.visited.append(url)
        self.url = url if self.account.get("signed_in") or "login" in url else "https://www.instagram.com/accounts/login/"
        if self.account.get("checkpoint"):
            self.url = "https://www.instagram.com/challenge/123/"

    def wait_for_load_state(self, *a, **k):
        pass

    def locator(self, sel):
        return FakeLocator(self, sel)

    def is_closed(self):
        return self.closed

    def bring_to_front(self):
        pass

    def screenshot(self, path, full_page=False):
        open(path, "wb").write(b"png")


class FakeContext:
    def __init__(self, account):
        self.account = account
        self.pages = [FakePage(account)]
        self.handlers = {}

    def cookies(self, url):
        base = [{"name": "csrftoken"}, {"name": "mid"}]
        return base + ([{"name": "sessionid"}] if self.account.get("signed_in") else [])

    def on(self, event, fn):
        self.handlers[event] = fn

    def new_page(self):
        self.pages.append(FakePage(self.account))
        return self.pages[-1]

    def close(self):
        if "close" in self.handlers:
            self.handlers["close"]()


class FakePlaywright:
    def __init__(self, account):
        self.account, self.launches = account, []
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    def _launch(self, user_data_dir, **kw):
        self.launches.append((user_data_dir, kw))
        return FakeContext(self.account)

    def stop(self):
        pass


def _service(settings, db, account):
    pw = FakePlaywright(account)
    return InstagramService(settings, db, playwright_factory=lambda: pw), pw


def test_connect_manual_login_and_username(settings, db):
    account = {"signed_in": False, "name": "test_reels"}
    svc, pw = _service(settings, db, account)
    assert svc.status()["state"] == "DISCONNECTED"
    st = svc.connect()
    assert st["state"] == "CONNECTING" and st["chrome_open"]
    profile, kw = pw.launches[0]
    assert profile.endswith("browser_profiles/instagram_default") and kw["channel"] == "chrome"  # separate profile, installed Chrome
    import os, stat
    if sys.platform != "win32":  # Windows has no unix file modes
        assert stat.S_IMODE(os.stat(profile).st_mode) == 0o700
    # «Я вошёл» before signing in: honest «не удалось определить»
    st = svc.confirm()
    assert st["state"] == "DISCONNECTED" and "Не удалось определить авторизацию" in st["message"]
    # the owner signs in by hand in that window
    account["signed_in"] = True
    st = svc.confirm()
    assert st["state"] == "READY" and st["account"]["username"] == "test_reels"  # from the «edit profile» field
    account["nav"] = True
    assert svc.confirm()["account"]["username"] == "test_reels"  # from the side navigation link
    # the account survives a restart of the factory (the session itself lives in the Chrome profile)
    again, _ = _service(settings, db, account)
    assert again.status()["state"] == "READY" and again.status()["account"]["username"] == "test_reels"
    log = (settings.logs_dir / "instagram").glob("*.log")
    assert "Instagram session OK: @test_reels" in next(log).read_text(encoding="utf-8")


def test_checkpoint_waits_for_a_person(settings, db):
    account = {"signed_in": True, "name": "x", "checkpoint": True}
    svc, _ = _service(settings, db, account)
    svc.connect()
    st = svc.confirm()
    assert st["state"] == "WAITING_USER" and "подтверждения" in st["message"]
    account["checkpoint"] = False  # the owner passed the check in Chrome, then «Продолжить»
    assert svc.confirm()["state"] == "READY"


def test_closed_window_reopens_and_disconnect_deletes_profile(settings, db):
    account = {"signed_in": True, "name": "x"}
    svc, pw = _service(settings, db, account)
    svc.connect()
    svc.browser.call(lambda: svc.browser.ctx.close())  # the owner closed the Chrome window
    assert not svc.status()["chrome_open"]
    svc.open_chrome()
    assert len(pw.launches) == 2 and svc.status()["chrome_open"]
    svc.confirm()
    st = svc.disconnect()
    assert st["state"] == "DISCONNECTED" and st["account"] is None and not svc.profile.path.exists()


def test_api_and_switch_off(settings, db):
    account = {"signed_in": True, "name": "api_user"}
    svc, _ = _service(settings, db, account)
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        assert web.get("/api/instagram-web/enabled").json() == {"enabled": True}
        assert web.post("/api/instagram-web/connect").json()["state"] == "CONNECTING"
        assert web.post("/api/instagram-web/confirm").json()["account"]["username"] == "api_user"
        assert web.get("/instagram").status_code == 200
    settings.raw["instagram_web"] = {"enabled": False}
    with TestClient(create_app(settings, db, start_worker=False)) as web:
        assert web.get("/api/instagram-web/enabled").json() == {"enabled": False}
        assert web.get("/api/instagram-web/status").status_code == 404  # the module is not loaded at all
        assert web.get("/api/dashboard").status_code == 200           # the factory works as before


# ---------- stage 3: queue ----------
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select


def _ready_items(settings, db, n=3):
    from app.database.models import ContentItem, RightsStatus, Status
    folder = settings.root / "Готовые"
    folder.mkdir(exist_ok=True)
    ids = []
    with db.session() as s:
        for i in range(n):
            f = folder / f"v{i}.mp4"
            f.write_bytes(b"0" * 100)
            it = ContentItem(source="t", source_id=f"v{i}", title=f"Ролик {i}", status=Status.READY_TO_PUBLISH,
                             rights_status=RightsStatus.AUTO_APPROVED, ready_path=str(f),
                             post_description=f"Описание {i} 🔥\n#мем")
            s.add(it)
            s.flush()
            ids.append(it.id)
    return ids


def test_planner_window_interval_and_daily_limit(settings):
    from app.instagram_web.config import IGConfig
    from app.instagram_web.queue.scheduler import Planner
    cfg = IGConfig(window_start="10:00", window_end="23:00", interval_minutes=40, max_posts_per_day=3,
                   timezone="Europe/Moscow")
    p = Planner(cfg)
    now = datetime(2026, 10, 7, 4, 0)  # 07:00 Moscow: before the window
    plan = p.plan([(1, None), (2, None), (3, None), (4, None)], now, 0, None)
    msk = {k: p.local(v).strftime("%d %H:%M") for k, v in plan.items()}
    assert msk == {1: "07 10:00", 2: "07 10:40", 3: "07 11:20", 4: "08 10:00"}  # 4th: limit 3 → next day
    late = datetime(2026, 10, 7, 19, 50)  # 22:50 Moscow, last post 22:30
    plan = p.plan([(1, None), (2, None)], late, 1, datetime(2026, 10, 7, 19, 30))
    assert [p.local(v).strftime("%d %H:%M") for v in plan.values()] == ["08 10:00", "08 10:40"]
    fixed = p.plan([(1, datetime(2026, 10, 7, 15, 30))], now, 0, None)  # 18:30 Moscow requested
    assert p.local(fixed[1]).strftime("%H:%M") == "18:30"


def test_queue_add_no_duplicates_pause_retry_remove(settings, db):
    from app.database.models import Publication, PublicationStatus as PS
    ids = _ready_items(settings, db)
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    q = svc.queue
    r = q.add(ids[:2])
    assert len(r["added"]) == 2 and not r["errors"]
    with db.session() as s:
        p0 = s.scalars(select(Publication).where(Publication.content_id == ids[0])).one()
        assert p0.platform == "instagram_web" and p0.caption == "Описание 0 🔥\n#мем"  # caption untouched
        assert p0.video_path.endswith("v0.mp4")  # the factory's own file, no copy
    again = q.add(ids)
    assert again["added"] and len(again["errors"]) == 2 and "уже в очереди" in list(again["errors"].values())[0]
    assert q.add([99999])["errors"]
    lst = q.listing()
    from app.instagram_web.queue.queue_manager import QueueError
    with pytest.raises(QueueError, match="2200"):
        q.update(lst["items"][0]["id"], caption="x" * 2201)
    assert lst["today"]["queued"] == 3 and all(i["eta"] for i in lst["items"])
    pid = lst["items"][0]["id"]
    q.pause_item(pid)
    assert {i["id"]: i["status"] for i in q.listing()["items"]}[pid] == "paused"
    assert q.listing()["today"]["queued"] == 2
    q.resume_item(pid)
    with db.session() as s:
        s.get(Publication, pid).status, s.get(Publication, pid).error = PS.WAITING_USER, "Instagram session expired"
    assert {i["id"]: i["status"] for i in q.listing()["items"]}[pid] == "failed"
    q.retry(pid)
    with db.session() as s:
        assert s.get(Publication, pid).status == PS.QUEUED and s.get(Publication, pid).error is None
    q.remove(pid)
    assert pid not in {i["id"] for i in q.listing()["items"]}
    q.set_paused(True)
    assert q.listing()["paused"] and q.listing()["today"]["next_at"] is None and svc.status()["paused"]


def test_queue_api_cover_schedule_and_old_queue_untouched(settings, db):
    import io
    from PIL import Image
    from app.database.models import Publication
    from app.publishing.queue import PublishQueue
    ids = _ready_items(settings, db, 2)
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        buf = io.BytesIO(); Image.new("RGB", (60, 100), (200, 0, 0)).save(buf, "PNG")
        cover = web.post("/api/instagram-web/queue/cover", files={"file": ("c.png", buf.getvalue(), "image/png")}).json()["cover_path"]
        r = web.post("/api/instagram-web/queue", json={"item_ids": [ids[0]], "caption": "Свой текст 😀", "cover_path": cover,
                                                       "when": "scheduled", "scheduled_at": "2030-01-05T18:30"}).json()
        assert len(r["added"]) == 1
        assert web.post("/api/instagram-web/queue", json={"item_ids": [ids[1]], "when": "scheduled"}).status_code == 400
        item = web.get("/api/instagram-web/queue").json()["items"][0]
        assert item["cover"] and item["caption"] == "Свой текст 😀" and item["scheduled_at"].startswith("2030-01-05T15:30")
        assert web.get("/api/instagram-web/queued-ids").json()["ids"] == [ids[0]]
        pid = item["id"]
        assert web.post(f"/api/instagram-web/queue/{pid}/pause").status_code == 200
        assert web.post(f"/api/instagram-web/retry/{pid}").status_code == 409  # not failed
        assert web.delete(f"/api/instagram-web/queue/{pid}").status_code == 200
        assert web.post("/api/instagram-web/pause").json()["paused"]
    # the old Graph API queue does not see these posts
    pq = PublishQueue(settings, db)
    with db.session() as s:
        assert pq._waiting(s) == [] and s.scalars(select(Publication)).first().platform == "instagram_web"
    assert pq.recover() == 0


def test_recover_after_crash(settings, db):
    from app.database.models import Publication, PublicationStatus as PS
    ids = _ready_items(settings, db, 1)
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    pid = svc.queue.add(ids)["added"][0]
    with db.session() as s:
        s.get(Publication, pid).status = PS.PROCESSING
    again, _ = _service(settings, db, {"signed_in": True, "name": "x"})  # factory restarted
    with db.session() as s:
        assert s.get(Publication, pid).status == PS.QUEUED


# ---------- stage 4: one Reel through the create dialog (fake Instagram) ----------
from tests.fake_instagram import FakeInstagram


class _Log:
    def __init__(self):
        self.lines = []

    def write(self, t, level="INFO"):
        self.lines.append(t)

    detail = write


def _publish(fake, caption="Привет 😀\nвторая строка #мем https://t.me/x", cover=None, **kw):
    from app.instagram_web.automation.publisher import Publisher
    shared = []
    p = Publisher(fake, _Log(), processing_timeout=kw.pop("timeout", 2), share_timeout=kw.pop("share_timeout", 2), **kw)
    return p.run("/v.mp4", caption, cover, "tester", lambda: shared.append(1)), shared, p


def test_publisher_happy_path_caption_exact_and_confirmation():
    fake = FakeInstagram()
    res, shared, pub = _publish(fake, cover=__file__)
    assert fake.shared == 1 and shared == [1]
    assert fake.caption == "Привет 😀\nвторая строка #мем https://t.me/x"  # caption untouched
    assert ("file", "/v.mp4") in fake.files and ("cover", __file__) in fake.files
    assert fake.clicks[:2] == ["Создать", "Публикация"] and "Поделиться" in fake.clicks
    assert fake.crop == "Оригинал"  # not Instagram's default square
    assert "Published successfully" in pub.log.lines


def test_publisher_account_without_create_menu():
    """@acc_two: «Создать» opens the upload dialog at once, without the «Публикация» menu."""
    fake = FakeInstagram(no_menu=True)
    _publish(fake)
    assert fake.shared == 1 and "Публикация" not in fake.clicks


def test_publisher_types_caption_when_paste_is_ignored():
    fake = FakeInstagram(paste_ignored=True)
    _publish(fake, caption="строка 1\nстрока 2")
    assert fake.caption == "строка 1\nстрока 2" and fake.shared == 1


@pytest.mark.parametrize("scenario, code, shared, retry", [
    ({"never_processed": True}, "VIDEO_PROCESSING_TIMEOUT", 0, True),
    ({"checkpoint_at": "final"}, "WAITING_USER", 0, False),
    ({"share_fails": True}, "PUBLISH_FAILED", 1, True),
    ({"never_confirms": True}, "PUBLISH_FAILED", 1, False),   # unknown outcome: no blind retry
    ({"crop_stuck": True}, "CROP_FAILED", 0, False),          # still square: never posted cropped
])
def test_publisher_errors(scenario, code, shared, retry):
    from app.instagram_web.automation.errors import PublishError
    fake = FakeInstagram(**scenario)
    with pytest.raises(PublishError) as e:
        _publish(fake, timeout=0.3, share_timeout=0.3)
    assert e.value.code == code and fake.shared == shared and e.value.retry == retry


def test_publisher_refuses_wrong_account_and_stops_before_share():
    from app.instagram_web.automation.errors import PublishError
    fake = FakeInstagram(username="someone_else")
    with pytest.raises(PublishError) as e:
        _publish(fake)
    assert e.value.code == "WRONG_ACCOUNT" and fake.shared == 0
    fake = FakeInstagram()
    with pytest.raises(PublishError) as e:
        _publish(fake, stop_requested=lambda: True)
    assert e.value.code == "STOPPED" and fake.shared == 0


class _IGContext:
    def __init__(self, page, signed_in=True):
        self.pages, self.signed_in = [page], signed_in

    def cookies(self, url):
        return [{"name": "sessionid"}] if self.signed_in else []

    def on(self, *a):
        pass

    def close(self):
        pass


def _ig_service(settings, db, fake, signed_in=True):
    settings.raw.setdefault("instagram_web", {}).update(processing_timeout_s=1, share_timeout_s=1)
    ctx = _IGContext(fake, signed_in)
    pw = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *a, **k: ctx), stop=lambda: None)
    svc = InstagramService(settings, db, playwright_factory=lambda: pw)
    svc._save_account({"username": "tester"})
    return svc


def test_service_publish_now_success_and_retry_rules(settings, db):
    from app.database.models import ContentItem, Publication, PublicationStatus as PS
    ids = _ready_items(settings, db, 3)
    svc = _ig_service(settings, db, FakeInstagram())
    pids = svc.queue.add(ids)["added"]
    svc.publish_now(pids[0], wait=True)
    with db.session() as s:
        p = s.get(Publication, pids[0])
        assert p.status == PS.PUBLISHED and p.published_at and p.shared_at and p.attempts == 1
        assert s.get(ContentItem, ids[0]).posted_at  # also marked «опубликовано» in «Готовые»
    assert svc.queue.listing()["today"]["published"] == 1
    # a temporary failure before Share: retry later, up to 3 attempts, then «failed» for good
    svc.browser.ctx = None
    svc2 = _ig_service(settings, db, FakeInstagram(never_processed=True))
    for n in range(3):
        svc2.publish_now(pids[1], wait=True)
        with db.session() as s:
            p = s.get(Publication, pids[1])
            assert p.error_code == "VIDEO_PROCESSING_TIMEOUT" and p.shared_at is None
            assert p.status == (PS.FAILED if n < 2 else PS.WAITING_USER)
    shots = list((settings.logs_dir / "instagram" / "errors").glob("*.png"))
    assert shots  # a screenshot of every failure
    log = next((settings.logs_dir / "instagram").glob("*.log")).read_text(encoding="utf-8")
    assert "ERROR_CODE: VIDEO_PROCESSING_TIMEOUT" in log and "Published successfully" in log


def test_service_checkpoint_pauses_queue_and_session_loss(settings, db):
    from app.database.models import Publication, PublicationStatus as PS
    ids = _ready_items(settings, db, 2)
    svc = _ig_service(settings, db, FakeInstagram(checkpoint_at="final"))
    pid = svc.queue.add(ids[:1])["added"][0]
    svc.publish_now(pid, wait=True)
    st = svc.status()
    assert st["state"] == "WAITING_USER" and st["paused"]
    with db.session() as s:
        p = s.get(Publication, pid)
        assert p.status == PS.QUEUED and p.attempts == 0  # nothing posted, the attempt does not count
    assert svc.resume()["state"] == "READY" and not svc.status()["paused"]
    svc3 = _ig_service(settings, db, FakeInstagram(), signed_in=False)
    pid2 = svc3.queue.add(ids[1:])["added"][0]
    svc3.publish_now(pid2, wait=True)
    with db.session() as s:
        assert s.get(Publication, pid2).error_code == "AUTH_REQUIRED" and s.get(Publication, pid2).status == PS.QUEUED
    assert svc3.status()["state"] == "ERROR" and svc3.status()["paused"]


def test_unknown_outcome_after_share_waits_for_person(settings, db):
    from app.database.models import Publication, PublicationStatus as PS
    ids = _ready_items(settings, db, 1)
    svc = _ig_service(settings, db, FakeInstagram(never_confirms=True))
    pid = svc.queue.add(ids)["added"][0]
    svc.publish_now(pid, wait=True)
    with db.session() as s:
        p = s.get(Publication, pid)
        assert p.status == PS.WAITING_USER and p.shared_at and "проверьте профиль" in p.error


def test_publish_api_and_stop(settings, db):
    ids = _ready_items(settings, db, 1)
    svc = _ig_service(settings, db, FakeInstagram())
    pid = svc.queue.add(ids)["added"][0]
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        assert web.post("/api/instagram-web/publish/99999").status_code == 404
        assert web.post(f"/api/instagram-web/publish/{pid}").status_code == 200
        import time
        for _ in range(50):
            if web.get("/api/instagram-web/queue").json()["items"][0]["status"] == "published":
                break
            time.sleep(0.1)
        assert web.get("/api/instagram-web/queue").json()["items"][0]["status"] == "published"
        assert web.post(f"/api/instagram-web/publish/{pid}").status_code == 409  # already published
        st = web.post("/api/instagram-web/stop").json()
        assert st["state"] == "PAUSED" and st["paused"]


def test_settings_from_the_page_persist(settings, db):
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    st = svc.update_settings({"max_posts_per_day": 5, "interval_minutes": 60, "window_start": "09:30"})
    assert st["settings"]["max_posts_per_day"] == 5 and st["settings"]["window_start"] == "09:30"
    from app.instagram_web.queue.queue_manager import QueueError
    with pytest.raises(QueueError):
        svc.update_settings({"max_posts_per_day": -1})
    with pytest.raises(QueueError):
        svc.update_settings({"window_end": "25:00"})
    again, _ = _service(settings, db, {"signed_in": True, "name": "x"})  # restart keeps them
    assert again.cfg.max_posts_per_day == 5 and again.queue.planner.limit == 5 and again.cfg.interval_minutes == 60


def test_own_description_per_video_saved_to_the_card(settings, db):
    from app.database.models import ContentItem, Publication
    ids = _ready_items(settings, db, 3)
    with db.session() as s:
        s.get(ContentItem, ids[2]).post_description = None
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        r = web.post("/api/instagram-web/queue", json={"item_ids": ids, "captions": {str(ids[0]): "Своё 1 🔥\n#a", str(ids[1]): "",
                                                                                     str(ids[2]): "Новое для пустого"}}).json()
        assert len(r["added"]) == 3
        pid = {i["content_id"]: i["id"] for i in web.get("/api/instagram-web/queue").json()["items"]}
        assert web.patch(f"/api/instagram-web/queue/{pid[ids[1]]}", json={"caption": "Дописал потом"}).status_code == 200
    with db.session() as s:
        caps = {p.content_id: p.caption for p in s.scalars(select(Publication))}
        cards = {i.id: i.post_description for i in s.scalars(select(ContentItem))}
    assert caps == {ids[0]: "Своё 1 🔥\n#a", ids[1]: "Дописал потом", ids[2]: "Новое для пустого"}
    assert cards[ids[0]] == "Своё 1 🔥\n#a" and cards[ids[2]] == "Новое для пустого" and cards[ids[1]] == "Дописал потом"


def test_leaving_ready_drops_waiting_instagram_posts(settings, db):
    ids = _ready_items(settings, db, 2)
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    svc.queue.add(ids)
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        assert web.delete(f"/api/ready/{ids[0]}").status_code == 200     # ↩ Убрать из готовых
        assert web.post(f"/api/items/{ids[1]}/trash").status_code == 200  # 🗑 Удалить
        assert web.get("/api/instagram-web/queue").json()["items"] == []
        assert web.get("/api/ready").json()["items"] == []


# ---------- linked accounts ----------
def test_daily_limit_is_per_account(settings, db):
    ids = _ready_items(settings, db, 4)
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    svc.update_settings({"max_posts_per_day": 1})
    svc.queue.add(ids[:2], account="acc_a")
    svc.queue.add(ids[2:], account="acc_b")
    with db.session() as s:
        plan = svc.queue.plan(s)
    days = sorted(svc.queue.planner.local(t).date() for t in plan.values())
    assert days[0] == days[1] and days[2] == days[3] and days[0] != days[2]  # one today per account, one tomorrow each


def test_publish_refuses_when_switch_fails(settings, db):
    from app.database.models import Publication, PublicationStatus as PS
    ids = _ready_items(settings, db, 1)
    fake = FakeInstagram()
    svc = _ig_service(settings, db, fake)
    pid = svc.queue.add(ids, account="other_acc")["added"][0]
    svc._switch_in_browser = lambda name: {"ok": False, "username": "tester", "reason": "просит пароль"}
    svc.publish_now(pid, wait=True)
    with db.session() as s:
        p = s.get(Publication, pid)
        assert p.status == PS.WAITING_USER and p.error_code == "WRONG_ACCOUNT"
    assert fake.shared == 0  # never posted to the wrong account


class _AccPage:
    """The «Переключение между аккаунтами» dialog: current account has no button, others are buttons."""
    def __init__(self, accounts, current, password_for=()):
        self.accounts, self.current, self.password_for = accounts, current, set(password_for)
        self.open = False
        self.url = "https://www.instagram.com/"
        self.keyboard = SimpleNamespace(press=lambda k: setattr(self, "open", False))
        self.asked_password = False

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        page = self

        class L:
            def __init__(self, items):
                self.items = items
            def filter(self, **kw):
                return self
            def count(self):
                return len(self.items)
            def nth(self, i):
                return L([self.items[i]])
            @property
            def first(self):
                return L(self.items[:1])
            def inner_text(self, timeout=None):
                return self.items[0][0]
            def click(self):
                self.items[0][1]()
            def locator(self, s2):
                return page.locator(s2)
        if sel == "[role=dialog]":
            return L([("Переключение между аккаунтами\n" + "\n".join(page.accounts), None)] if page.open else [])
        if sel.startswith("[role=button]"):
            if not page.open:
                return L([("Переключиться", lambda: setattr(page, "open", True))])
            return L([(a, (lambda a=a: page._pick(a))) for a in page.accounts if a != page.current] +
                     [("Войти в существующий аккаунт", lambda: None)])
        if sel.startswith("input[type=password]"):
            return L([("", None)] if page.asked_password else [])
        return L([])

    def _pick(self, a):
        if a in self.password_for:
            self.asked_password = True
            return
        self.current, self.open = a, False


def test_switch_accounts_dialog_logic():
    from app.instagram_web.browser import accounts as acc
    page = _AccPage(["acc_main", "acc_two", "acc_three"], "acc_main", password_for=["acc_three"])
    lst = acc.list_accounts(page)
    assert lst[0] == {"username": "acc_main", "current": True} and {a["username"] for a in lst} == {"acc_main", "acc_two", "acc_three"}
    r = acc.switch_to(page, "acc_two", lambda p: p.current)
    assert r["ok"] and page.current == "acc_two"
    r = acc.switch_to(page, "acc_three", lambda p: p.current)
    assert not r["ok"] and r.get("need_login")  # a password is never typed
    assert acc.switch_to(page, "nobody", lambda p: p.current)["ok"] is False


def test_excluded_personal_account_is_untouchable(settings, db):
    from app.instagram_web.queue.queue_manager import QueueError
    ids = _ready_items(settings, db, 1)
    svc = _ig_service(settings, db, FakeInstagram())
    svc.set_excluded("personal_acc", True)
    with pytest.raises(QueueError):
        svc.switch_account("personal_acc")
    with pytest.raises(QueueError):
        svc.manual_login("@PERSONAL_ACC")
    assert svc._switch_in_browser("personal_acc")["ok"] is False
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        r = web.post("/api/instagram-web/queue", json={"item_ids": ids, "account": "personal_acc"})
        assert r.status_code == 403


def test_cover_is_the_editor_background(settings, db):
    import io, json as _json
    from PIL import Image
    from app.database.models import EditProject, Publication
    from app.editor import backgrounds as bgm
    ids = _ready_items(settings, db, 3)
    buf = io.BytesIO(); Image.new("RGB", (400, 400), (10, 120, 200)).save(buf, "PNG")
    asset = bgm.add(settings, db, "sky.png", buf.getvalue())
    with db.session() as s:
        s.add(EditProject(content_id=ids[0], spec=_json.dumps({"background": {"type": "image", "asset": asset["id"]}})))
        s.add(EditProject(content_id=ids[1], spec=_json.dumps({"background": {"type": "color", "color": "#ff0000"}})))
        s.add(EditProject(content_id=ids[2], spec=_json.dumps({"background": {"type": "blur"}})))
    svc, _ = _service(settings, db, {"signed_in": True, "name": "x"})
    svc.queue.add(ids)
    with db.session() as s:
        covers = {p.content_id: p.cover_path for p in s.scalars(select(Publication))}
    img = Image.open(covers[ids[0]])
    assert img.size == (1080, 1920) and img.getpixel((540, 960))[2] > 150  # the picture, cropped to 9:16
    assert Image.open(covers[ids[1]]).getpixel((10, 10))[:3] == (254, 0, 0) or Image.open(covers[ids[1]]).getpixel((10, 10))[0] > 240
    assert covers[ids[2]] is None  # blurred clip: Instagram's own frame
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        assert web.get(f"/api/instagram-web/cover-preview/{ids[0]}").status_code == 200
        assert web.get(f"/api/instagram-web/cover-preview/{ids[2]}").status_code == 404
        item = next(i for i in web.get("/api/instagram-web/queue").json()["items"] if i["content_id"] == ids[0])
        assert item["cover_url"] and web.get(item["cover_url"]).status_code == 200


def test_publish_all_ready_and_autopost_one_by_one(settings, db):
    from datetime import timedelta
    from app.database.models import ContentItem, Publication, PublicationStatus as PS, utcnow
    ids = _ready_items(settings, db, 3)
    with db.session() as s:
        s.get(ContentItem, ids[1]).post_description = "Своё описание"
        s.get(ContentItem, ids[0]).post_description = s.get(ContentItem, ids[2]).post_description = None
    fake = FakeInstagram()
    svc = _ig_service(settings, db, fake)
    svc.update_settings({"window_start": "00:00", "window_end": "23:59", "interval_minutes": 40, "max_posts_per_day": 15})
    r = svc.publish_all_ready(None, "Общее описание", use_own=True)
    assert len(r["added"]) == 3 and svc.autopost and r["last_eta"]
    with db.session() as s:
        caps = {p.content_id: p.caption for p in s.scalars(select(Publication))}
    assert caps[ids[1]] == "Своё описание" and caps[ids[0]] == "Общее описание"
    assert svc.publish_all_ready(None, "x")["added"] == []  # nothing twice
    now = utcnow()
    first = svc.autopost_tick(now)
    assert first and fake.shared == 1
    assert svc.autopost_tick(now) is None  # the interval keeps the next one waiting
    assert svc.autopost_tick(now + timedelta(minutes=41)) and fake.shared == 2
    svc.stop()
    assert svc.autopost_tick(now + timedelta(minutes=90)) is None and fake.shared == 2  # paused: nothing goes out


def test_publish_selected_ready_in_click_order(settings, db):
    import pytest
    from app.database.models import Publication
    from app.instagram_web.queue.queue_manager import QueueError
    ids = _ready_items(settings, db, 4)
    svc = _ig_service(settings, db, FakeInstagram())
    r = svc.publish_all_ready(None, "Одно на всех", use_own=False, ids=[ids[2], ids[0], ids[2]])
    assert len(r["added"]) == 2 and svc.autopost
    with db.session() as s:
        pubs = list(s.scalars(select(Publication).order_by(Publication.id)))
    assert [p.content_id for p in pubs] == [ids[2], ids[0]] and {p.caption for p in pubs} == {"Одно на всех"}
    with pytest.raises(QueueError):
        svc.publish_all_ready(None, "x", ids=[ids[0]])  # already queued: not twice
    assert len(svc.publish_all_ready(None, "x", ids=[ids[1], 999999])["added"]) == 1


def test_no_limits_mode(settings):
    from app.instagram_web.config import IGConfig
    from app.instagram_web.queue.scheduler import Planner
    p = Planner(IGConfig(window_start="00:00", window_end="23:59", interval_minutes=0, max_posts_per_day=0, timezone="Europe/Moscow"))
    now = datetime(2026, 10, 7, 23, 30)  # 02:30 Moscow at night
    plan = p.plan([(i, None) for i in range(30)], now, 100, None)
    assert all(t == now for t in plan.values())  # all due at once, no limit, no window, no pause
