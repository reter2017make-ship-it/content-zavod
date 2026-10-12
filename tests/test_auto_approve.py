"""Owner's decision (2026-10-07): no rights check, nothing waits for a person."""
import shutil

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.models import ContentItem, RightsStatus, Status
from app.processing.pipeline import Pipeline, approve_waiting
from app.scoring.decision import decide
from app.web.server import create_app


def _d(**kw):
    base = dict(quality=80, gate_reason=None, viral=80, content=80, provider="rules", flags=set(),
                ai_action="approve", rights=RightsStatus.REQUIRES_REVIEW, thresholds={})
    return decide(**(base | kw))


def test_decision_auto_approves_what_used_to_wait():
    assert _d().status == Status.MANUAL_REVIEW
    d = _d(auto_approve=True)
    assert d.status == Status.APPROVED and d.rule == "auto_approved" and "REQUIRES_REVIEW" in d.reason
    assert _d(auto_approve=True, ai_action="review").status == Status.APPROVED
    assert _d(auto_approve=True, quality=10).status == Status.REJECTED  # quality filters still work
    assert _d(auto_approve=True, rights=RightsStatus.REJECTED).status == Status.REJECTED


def test_foreign_video_goes_all_the_way_to_ready(settings, db, clips):
    settings.raw["rights"] = {"auto_approve": True}
    settings.raw["sources"]["filesystem"]["folders"] = [
        {"path": "data/inbox", "category": "other", "rights_status": "UNKNOWN", "source_score": 50}]
    shutil.copy(clips["landscape"], settings.data("inbox") / "x.mp4")
    Pipeline(settings, db).run_once()
    with db.session() as s:
        item = s.scalars(select(ContentItem)).one()
        assert item.status == Status.READY_TO_PUBLISH and item.rights_status == RightsStatus.AUTO_APPROVED
        iid = item.id
    client = TestClient(create_app(settings, db, start_worker=False))
    assert client.post(f"/api/ready/{iid}").status_code == 200  # «В готовые» works without any approval
    assert client.get(f"/api/editor/{iid}").json()["item"]["can_export"]


def test_waiting_items_are_approved_on_start(db):
    with db.session() as s:
        s.add_all([
            ContentItem(source="t", source_id="1", status=Status.MANUAL_REVIEW, rights_status=RightsStatus.REQUIRES_REVIEW),
            ContentItem(source="t", source_id="2", status=Status.PROCESSED, rights_status=RightsStatus.UNKNOWN),
            ContentItem(source="t", source_id="3", status=Status.MANUAL_REVIEW, rights_status=RightsStatus.REJECTED),
        ])
    assert approve_waiting(db) == 2
    with db.session() as s:
        got = {i.source_id: (i.status, i.rights_status) for i in s.scalars(select(ContentItem))}
    assert got["1"] == (Status.APPROVED, RightsStatus.AUTO_APPROVED)
    assert got["2"] == (Status.READY_TO_PUBLISH, RightsStatus.AUTO_APPROVED)
    assert got["3"][0] == Status.MANUAL_REVIEW


def test_any_downloaded_video_can_go_to_editor_and_ready(settings, db, clips):
    """Owner: every downloaded video — whatever the analysis said — opens in the editor and goes to «Готовые»."""
    import shutil
    from app.editor.service import Editor
    settings.raw["rights"] = {"auto_approve": True}
    raw = settings.data("raw")
    raw.mkdir(parents=True, exist_ok=True)
    f = raw / "x.mp4"
    shutil.copy(clips["landscape"], f)
    with db.session() as s:
        it = ContentItem(source="telegram", source_id="t/1", channel="c", status=Status.REJECTED,
                         rights_status=RightsStatus.REQUIRES_REVIEW, original_file=str(f), duration=6.0,
                         width=1280, height=720, decision_reason="вирусность 20 < 50")
        s.add(it)
        s.flush()
        iid = it.id
    ed = Editor(settings, db)
    ed.ensure_builtin_templates()
    client = TestClient(create_app(settings, db, start_worker=False))
    assert iid in [i["id"] for i in client.get("/api/editor/items").json()["items"]]
    assert client.get(f"/api/editor/{iid}").json()["item"]["can_export"]
    r = client.post(f"/api/items/{iid}/to-ready", json={"description": "Моё описание"}).json()
    assert r["mode"] == "render"  # no render yet: made from the source with «Размытый фон»
    ed.export(iid)
    with db.session() as s:
        item = s.get(ContentItem, iid)
        assert item.status == Status.READY_TO_PUBLISH and item.ready_path and item.post_description == "Моё описание"
    assert iid in [i["id"] for i in client.get("/api/ready").json()["items"]]
