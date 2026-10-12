"""Stage 4: publication queue and Instagram Reels connector against a simulated Graph API (no real account)."""
import sys
import hashlib
import json
import shutil
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.models import ContentItem, Publication, PublicationStatus as PS, RightsStatus, Status, utcnow
from app.publishing.queue import PublishQueue, QueueError
from app.publishing.validate import caption_problems, video_problems

IG = "17841400000000001"


class FakeGraph:
    """Minimal Graph API: containers, resumable upload, processing, media_publish, limits, account calls."""

    def __init__(self, processing_polls: int = 1):
        self.containers: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.calls: list[str] = []
        self.publish_calls = 0
        self.processing_polls = processing_polls
        self.fail: dict[str, list] = {}  # "POST media_publish" -> [(status, error_dict, effective)]
        self.quota_usage = 0
        self.permissions = ["instagram_basic", "instagram_content_publish", "pages_read_engagement"]

    def _err(self, status, code, msg="boom", transient=False):
        return httpx.Response(status, json={"error": {"message": msg, "code": code, "fbtrace_id": "TRACE123",
                                                      "is_transient": transient}})

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        parts = path.strip("/").split("/")[1:]  # drop the version
        if host == "rupload.facebook.com":
            parts = parts[1:]
        self.calls.append(f"{req.method} {host}{path}")
        if host == "rupload.facebook.com":
            assert req.headers["Authorization"] == "OAuth TOKEN"
            cid = parts[-1]
            c = self.containers[cid]
            assert int(req.headers["offset"]) == len(c["data"])
            c["data"] += req.content
            c["size"] = int(req.headers["file_size"])
            return httpx.Response(200, json={"success": True, "message": "Upload successful."})

        form = dict(httpx.QueryParams(req.content.decode())) if req.method == "POST" else dict(req.url.params)
        token = form.get("access_token", "")
        if parts == ["oauth", "access_token"]:
            return httpx.Response(200, json={"access_token": "NEWTOKEN", "token_type": "bearer", "expires_in": 5184000})
        if parts == ["debug_token"]:
            return httpx.Response(200, json={"data": {"type": "USER", "expires_at": 0, "is_valid": True}})
        if token != "TOKEN":
            return self._err(400, 190, "Error validating access token")
        queued = self.fail.get(f"{req.method} {parts[-1]}")
        effective = False
        if queued:
            status, code, effective = queued.pop(0)
            if not effective:
                return self._err(status, code, transient=status >= 500)

        if parts == ["me"]:
            return httpx.Response(200, json={"id": "1", "name": "Тест"})
        if parts == ["me", "permissions"]:
            return httpx.Response(200, json={"data": [{"permission": p, "status": "granted"} for p in self.permissions]})
        if parts == ["me", "accounts"]:
            return httpx.Response(200, json={"data": [{"name": "Страница", "instagram_business_account":
                                                       {"id": IG, "username": "zavod_test"}}]})
        if parts == [IG]:
            return httpx.Response(200, json={"id": IG, "username": "zavod_test", "name": "Завод"})
        if parts == [IG, "content_publishing_limit"]:
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota_usage, "config": {"quota_total": 100}}]})
        if parts == [IG, "media"] and req.method == "POST":
            assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
            cid = f"c{len(self.containers) + 1}"
            self.containers[cid] = {"caption": form["caption"], "data": b"", "polls": 0, "status": "IN_PROGRESS"}
            return httpx.Response(200, json={"id": cid, "uri": f"https://rupload.facebook.com/ig-api-upload/v25.0/{cid}"})
        if parts == [IG, "media"]:
            return httpx.Response(200, json={"data": [{"id": m, "caption": d["caption"], "permalink": d["permalink"],
                                                       "timestamp": d["timestamp"]} for m, d in self.media.items()]})
        if parts == [IG, "media_publish"]:
            self.publish_calls += 1
            c = self.containers[form["creation_id"]]
            assert c["status"] == "FINISHED"
            mid = f"m{len(self.media) + 1}"
            c["status"] = "PUBLISHED"
            self.media[mid] = {"caption": c["caption"], "permalink": f"https://www.instagram.com/reel/{mid}/",
                               "timestamp": utcnow().strftime("%Y-%m-%dT%H:%M:%S+0000")}
            if effective:  # published, but the answer is lost
                return self._err(500, 1, "An unknown error occurred", transient=True)
            return httpx.Response(200, json={"id": mid})
        if len(parts) == 1 and parts[0] in self.containers:
            c = self.containers[parts[0]]
            if form.get("fields") == "video_status":
                return httpx.Response(200, json={"video_status": {"uploading_phase": {"bytes_transferred": len(c["data"])}}})
            if c["status"] == "IN_PROGRESS" and c.get("size") and len(c["data"]) == c["size"]:
                c["polls"] += 1
                if c["polls"] > self.processing_polls:
                    c["status"] = "FINISHED"
            return httpx.Response(200, json={"status_code": c["status"], "id": parts[0]})
        if len(parts) == 1 and parts[0] in self.media:
            return httpx.Response(200, json={"id": parts[0], "permalink": self.media[parts[0]]["permalink"]})
        return httpx.Response(404, json={"error": {"message": f"unknown {path}", "code": 100}})


@pytest.fixture
def graph():
    return FakeGraph()


@pytest.fixture
def creds(monkeypatch, settings):
    (settings.root / ".env").write_text("# test\nMETA_ACCESS_TOKEN=\nINSTAGRAM_ACCOUNT_ID=\n", encoding="utf-8")
    monkeypatch.setenv("META_ACCESS_TOKEN", "TOKEN")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", IG)
    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)


@pytest.fixture
def pq(settings, db, graph):
    # Instagram API publishing is switched off in config.yaml (manual publishing); these tests keep it working.
    settings.raw["publishing"]["enabled"] = True
    settings.raw["publishing"]["instagram"].update(enabled=True, poll_seconds=0, min_interval_minutes=0)
    return PublishQueue(settings, db, transport=httpx.MockTransport(graph), sleep=lambda s: None)


def _ready(settings, pipeline, clips, *names):
    for n in names:
        shutil.copy(clips[n], settings.data("inbox") / f"{n}.mp4")
    pipeline.run_once()
    with pipeline.db.session() as s:
        items = list(s.scalars(select(ContentItem).where(ContentItem.status == Status.READY_TO_PUBLISH)
                               .order_by(ContentItem.id)))
    assert len(items) == len(names)
    return [i.id for i in items]


def _pub(db, pid) -> Publication:
    with db.session() as s:
        p = s.get(Publication, pid)
        p.content  # load
        return p


def _db_digest(settings):
    return hashlib.sha256(settings.database_path.read_bytes()).hexdigest()


# ---------- validation ----------
def test_rendered_video_passes_reels_spec(settings, pipeline, clips, db):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    with db.session() as s:
        path = PublishQueue._video_path(s, iid)
    assert video_problems(path, settings.get("publishing.instagram")) == []
    assert "нет звуковой" in " ".join(video_problems(clips["vertical_silent"], {}))
    assert video_problems(None, {}) == ["файла готового ролика нет"]


def test_caption_limits():
    cfg = {"max_caption_chars": 2200, "max_hashtags": 30, "max_mentions": 20}
    assert caption_problems("Смотрите! #еда @друг", cfg) == []
    assert caption_problems("  ", cfg) == ["описание пустое"]
    assert "хэштегов" in caption_problems(" ".join(f"#t{i}" for i in range(31)), cfg)[0]
    assert "2200" in caption_problems("а" * 2201, cfg)[0]


# ---------- queue rules ----------
def test_enqueue_requires_manual_rights_approval(settings, db, pipeline, clips, pq):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    with pytest.raises(QueueError, match="подтвердите права"):
        pq.enqueue(iid, "Описание", None, rights_confirmed=False)
    with pytest.raises(QueueError, match="описание пустое"):
        pq.enqueue(iid, "  ", None, rights_confirmed=True)

    for rights in (RightsStatus.UNKNOWN, RightsStatus.REQUIRES_REVIEW, RightsStatus.REJECTED):
        with db.session() as s:
            s.get(ContentItem, iid).rights_status = rights
        with pytest.raises(QueueError, match="ручной проверке"):
            pq.enqueue(iid, "Описание", None, rights_confirmed=True)
    with db.session() as s:
        s.get(ContentItem, iid).rights_status = RightsStatus.USER_OWNED

    pid = pq.enqueue(iid, "Первое описание #тест", None, rights_confirmed=True)
    with pytest.raises(QueueError, match="уже в очереди"):
        pq.enqueue(iid, "Ещё раз", None, rights_confirmed=True)
    assert _pub(db, pid).rights_confirmed_at is not None
    pq.cancel(pid)
    assert pq.enqueue(iid, "Второе описание", None, rights_confirmed=True)  # cancelled one frees the slot
    assert pq.recent_descriptions()[:2] == ["Второе описание", "Первое описание #тест"]


def test_item_in_manual_review_cannot_be_queued(settings, db, pipeline, clips, pq):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    with db.session() as s:
        item = s.get(ContentItem, iid)
        item.status, item.rights_status = Status.MANUAL_REVIEW, RightsStatus.UNKNOWN
    with pytest.raises(QueueError):
        pq.enqueue(iid, "Описание", None, rights_confirmed=True)


# ---------- dry-run ----------
def test_dry_run_goes_whole_path_without_sending(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Пробный ролик #завод", None, rights_confirmed=True)
    before = _db_digest(settings)
    plans = pq.dry_run(record=False)
    assert _db_digest(settings) == before  # CLI dry-run writes nothing
    [plan] = plans
    assert plan["caption"] == "Пробный ролик #завод" and plan["when"] and plan["sent"] is False
    assert plan["ready"] and "Instagram Reels" in plan["where"]
    assert {c["name"] for c in plan["checks"] if c["ok"]} >= {"Видео готово", "Формат Reels", "Права подтверждены",
                                                              "Описание готово"}
    assert not pq.live
    assert pq.tick() == "dry-run"
    p = _pub(db, pid)
    assert p.status == PS.QUEUED and p.dry_run_at and not p.external_id
    assert graph.calls == []  # nothing went to Meta


def test_cli_publish_dry_run(settings, db, pipeline, clips, pq, capsys, monkeypatch):
    from app.cli import commands
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pq.enqueue(iid, "Описание для CLI", None, rights_confirmed=True)
    monkeypatch.setattr(commands, "load_settings", lambda _: settings)
    before = _db_digest(settings)
    assert commands.main(["publish", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Описание для CLI" in out and "куда:" in out and "когда:" in out and "ничего не отправляется" in out
    assert _db_digest(settings) == before


# ---------- real path against the simulated API ----------
def _go_live(pq):
    assert pq.check_connection()["ok"]
    pq.set_live(True)


def test_publish_records_media_id(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Настоящий путь #reels", None, rights_confirmed=True)
    _go_live(pq)
    assert pq.tick() == f"processed {pid}"
    p = _pub(db, pid)
    assert p.status == PS.PUBLISHED and p.external_id == "m1" and p.permalink.endswith("/m1/")
    assert p.content.status == Status.PUBLISHED and p.container_id == "c1"
    assert graph.containers["c1"]["data"] == open(p.video_path, "rb").read()
    assert graph.publish_calls == 1


def test_live_mode_needs_successful_check(pq, settings, monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    with pytest.raises(QueueError, match="подключите Instagram"):
        pq.set_live(True)
    st = pq.check_connection()
    assert not st["ok"] and "META_ACCESS_TOKEN" in st["missing"]


def test_lost_publish_answer_is_not_published_twice(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Ответ потерялся", None, rights_confirmed=True)
    _go_live(pq)
    graph.fail["POST media_publish"] = [(500, 1, True)]  # Meta published, but answered 500
    pq.tick()
    p = _pub(db, pid)
    assert p.status == PS.FAILED and p.error_http_status == 500 and p.error_request_id == "TRACE123"
    with db.session() as s:
        s.get(Publication, pid).next_retry_at = utcnow() - timedelta(seconds=1)
    pq.tick()
    p = _pub(db, pid)
    assert p.status == PS.PUBLISHED and p.external_id == "m1"
    assert graph.publish_calls == 1 and len(graph.media) == 1 and len(graph.containers) == 1


def test_transient_errors_retry_then_wait_for_user(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Сбои API", None, rights_confirmed=True)
    _go_live(pq)
    graph.fail["POST media"] = [(503, 2, False)] * 3
    for attempt in range(1, 4):
        pq.tick()
        p = _pub(db, pid)
        assert p.attempts == attempt
        with db.session() as s:
            s.get(Publication, pid).next_retry_at = utcnow() - timedelta(seconds=1)
    assert p.status == PS.WAITING_USER and p.error_http_status == 503
    pq.retry(pid)
    pq.tick()
    assert _pub(db, pid).status == PS.PUBLISHED and graph.publish_calls == 1


def test_auth_error_waits_for_user_without_retries(settings, db, pipeline, clips, pq, graph, creds, monkeypatch):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Токен истёк", None, rights_confirmed=True)
    _go_live(pq)
    monkeypatch.setenv("META_ACCESS_TOKEN", "EXPIRED")
    pq.tick()
    p = _pub(db, pid)
    assert p.status == PS.WAITING_USER and "токен" in p.error and p.error_code == "190"
    assert "EXPIRED" not in p.error


def test_crash_mid_upload_resumes_without_new_post(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Сбой посреди отправки", None, rights_confirmed=True)
    _go_live(pq)
    graph.fail["POST media_publish"] = [(500, 1, False)]
    pq.tick()
    with db.session() as s:  # as if the process died while PROCESSING
        p = s.get(Publication, pid)
        p.status, p.next_retry_at = PS.PROCESSING, None
    assert pq.recover() == 1
    pq.tick()
    p = _pub(db, pid)
    assert p.status == PS.PUBLISHED and len(graph.containers) == 1 and graph.publish_calls == 1


def test_daily_limit_and_pause(settings, db, pipeline, clips, pq, graph, creds, tmp_path):
    from app.media.ffmpeg import make_test_video
    settings.raw["publishing"]["instagram"].update(daily_limit=2, min_interval_minutes=0)
    clips = {**clips, "third": make_test_video(tmp_path / "third.mp4", 720, 1280, 5, pattern="cellauto", freq=900)}
    ids = _ready(settings, pipeline, clips, "landscape", "other", "third")
    pids = [pq.enqueue(i, f"Ролик {n}", None, rights_confirmed=True) for n, i in enumerate(ids)]
    _go_live(pq)
    pq.set_paused(True)
    assert pq.tick() == "paused"
    pq.set_paused(False)
    for _ in range(4):
        pq.tick()
    statuses = [_pub(db, p).status for p in pids]
    assert statuses.count(PS.PUBLISHED) == 2 and statuses.count(PS.QUEUED) == 1
    with db.session() as s:
        eta = pq.schedule(s)
    assert eta[pids[2]] > utcnow() + timedelta(minutes=1)  # tomorrow (in Europe/Moscow)


def test_min_interval_between_posts(settings, db, pipeline, clips, pq, graph, creds):
    settings.raw["publishing"]["instagram"].update(daily_limit=10, min_interval_minutes=60)
    ids = _ready(settings, pipeline, clips, "landscape", "other")
    pids = [pq.enqueue(i, f"Интервал {n}", None, rights_confirmed=True) for n, i in enumerate(ids)]
    _go_live(pq)
    pq.tick()
    assert pq.tick() == "nothing due"
    with db.session() as s:
        eta = pq.schedule(s)
    assert timedelta(minutes=59) < eta[pids[1]] - utcnow() <= timedelta(minutes=60)


def test_api_quota_reached_postpones(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Квота", None, rights_confirmed=True)
    _go_live(pq)
    graph.quota_usage = 100
    pq.tick()
    p = _pub(db, pid)
    assert p.status == PS.FAILED and p.attempts == 0 and "лимит" in p.error and graph.containers == {}


def test_rights_revoked_after_queueing_blocks_publication(settings, db, pipeline, clips, pq, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    pid = pq.enqueue(iid, "Права отозваны", None, rights_confirmed=True)
    _go_live(pq)
    with db.session() as s:
        s.get(ContentItem, iid).rights_status = RightsStatus.REJECTED
    pq.tick()
    assert _pub(db, pid).status == PS.WAITING_USER and graph.containers == {}


# ---------- account and token ----------
def test_check_connection_reports_missing_permission(pq, graph, creds):
    graph.permissions = ["instagram_basic"]
    st = pq.check_connection()
    assert not st["ok"]
    bad = [x for x in st["steps"] if not x["ok"]]
    assert bad[0]["name"] == "Разрешения" and "instagram_content_publish" in bad[0]["detail"]


def test_token_refresh_writes_env(pq, settings, creds, monkeypatch):
    with pytest.raises(QueueError, match="META_APP_ID"):
        pq.refresh_token()
    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    res = pq.refresh_token(force=True)
    assert res["refreshed"] and res["expires_at"]
    env = (settings.root / ".env").read_text()
    assert "META_ACCESS_TOKEN=NEWTOKEN" in env and env.startswith("# test")
    if sys.platform != "win32":  # Windows has no unix file modes
        assert oct((settings.root / ".env").stat().st_mode)[-3:] == "600"


# ---------- web ----------
@pytest.fixture
def web(settings, db, pq):
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    worker = FactoryWorker(settings, db, publisher=pq)
    with TestClient(create_app(settings, db, worker, start_worker=False)) as client:
        yield client


def test_web_publishing_flow(settings, db, pipeline, clips, web, graph, creds):
    [iid] = _ready(settings, pipeline, clips, "landscape")
    item = web.get(f"/api/items/{iid}").json()
    assert item["can_publish"] and item["publication"] is None
    assert web.patch(f"/api/items/{iid}", json={"description": "Черновик"}).json()["ok"]
    assert web.get("/api/publishing/descriptions").json()["descriptions"] == ["Черновик"]

    r = web.post("/api/publishing/queue", json={"content_id": iid, "caption": "Готово", "rights_confirmed": False})
    assert r.status_code == 400
    r = web.post("/api/publishing/queue", json={"content_id": iid, "caption": "Готово #рилс",
                                                "rights_confirmed": True, "scheduled_at": "2030-01-01T10:00"})
    pid = r.json()["id"]
    ov = web.get("/api/publishing").json()
    assert ov["mode"] == "dry_run" and ov["ready"] == [] and ov["publications"][0]["status"] == "QUEUED"
    assert ov["publications"][0]["scheduled_at"].startswith("2030-01-01T07:00")  # 10:00 Moscow = 07:00 UTC

    plan = web.post(f"/api/publishing/{pid}/dry-run").json()["plan"]
    assert plan["sent"] is False and plan["when_local"] == "01.01 10:00"
    assert web.patch(f"/api/publishing/{pid}", json={"asap": True}).json()["ok"]
    assert web.post("/api/publishing-settings/live", json={"value": True}).status_code == 409  # not checked yet
    assert web.post("/api/instagram/check").json()["ok"]
    assert web.post("/api/publishing-settings/live", json={"value": True}).json()["mode"] == "live"
    assert web.post(f"/api/publishing/{pid}/now").json()["ok"]
    web.app.state.worker.publisher.tick()
    pub = web.get("/api/publishing").json()["publications"][0]
    assert pub["status"] == "PUBLISHED" and pub["external_id"] == "m1"
    assert web.post(f"/api/publishing/{pid}/cancel").status_code == 409
    assert web.get("/publish", follow_redirects=False).status_code == 307
    acc = web.get("/api/publishing").json()["account"]
    assert "TOKEN" not in json.dumps(acc)


def test_web_saves_token_without_echoing_it(settings, web, graph, creds, monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN")
    r = web.post("/api/instagram/credentials", json={"token": "TOKEN", "account_id": IG})
    assert r.status_code == 200 and r.json()["ok"] and "TOKEN" not in r.text
    assert "META_ACCESS_TOKEN=TOKEN" in (settings.root / ".env").read_text()
    assert web.get("/api/instagram/accounts").json()["accounts"][0]["username"] == "zavod_test"
