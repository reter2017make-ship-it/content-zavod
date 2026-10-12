"""«Фоны»: library upload, used marks, random unused for a batch, cleanup with warnings."""
import io
import json
import random
import shutil

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.database.models import ContentItem, EditAsset, EditProject, Status
from app.editor import backgrounds as bg
from app.web.server import create_app


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (90, 160), color).save(buf, "PNG")
    return buf.getvalue()


def test_library_used_marks_random_and_cleanup(settings, db):
    client = TestClient(create_app(settings, db, start_worker=False))
    files = [("files", (f"b{i}.png", _png((i * 40, 10, 10)), "image/png")) for i in range(4)]
    r = client.post("/api/backgrounds?tag=спорт", files=files).json()
    assert len(r["added"]) == 4 and not r["errors"] and r["added"][0]["tag"] == "спорт"
    bad = client.post("/api/backgrounds", files=[("files", ("a.mp3", b"ID3", "audio/mpeg"))]).json()
    assert bad["errors"] and not bad["added"]
    ids = [a["id"] for a in r["added"]]
    bg.mark_used(db, ids[0])
    bg.mark_used(db, ids[0])
    lst = client.get("/api/backgrounds?used=used").json()
    assert [i["id"] for i in lst["items"]] == [ids[0]] and lst["items"][0]["use_count"] == 2
    assert len(client.get("/api/backgrounds?used=unused").json()["items"]) == 3
    assert client.get("/api/backgrounds").json()["tags"] == ["спорт"]
    # a batch of 3 gets the 3 unused ones, no repeats
    picked = bg.pick_unused(db, 3, rng=random.Random(1))
    assert sorted(picked) == sorted(ids[1:])
    # cleanup: preview, warning for a background used in an unfinished project, then delete
    with db.session() as s:
        s.add(ContentItem(id=1, source="t", source_id="1", status=Status.READY_TO_PUBLISH))
        s.add(EditProject(content_id=1, spec=json.dumps({"background": {"type": "image", "asset": ids[0]}})))
    pv = client.post("/api/backgrounds/delete", json={"all_used": True}).json()
    assert pv["count"] == 1 and pv["bytes"] > 0 and pv["in_unfinished"] and "deleted" not in pv
    assert client.post("/api/backgrounds/delete", json={"all_used": True, "confirm": True}).json()["deleted"]
    with db.session() as s:
        assert s.get(EditAsset, ids[0]) is None
    assert client.get("/backgrounds").status_code == 200


def test_batch_template_with_random_backgrounds(settings, db, pipeline, clips):
    from app.editor.service import Editor
    for i in range(2):
        shutil.copy(clips["landscape" if i == 0 else "other"], settings.data("inbox") / f"v{i}.mp4")
    pipeline.run_once()
    with db.session() as s:
        items = list(s.scalars(select(ContentItem.id).where(ContentItem.status == Status.READY_TO_PUBLISH)))
    for i in range(3):
        bg.add(settings, db, f"x{i}.png", _png((0, i * 60, 200)))
    ed = Editor(settings, db)
    ed.ensure_builtin_templates()
    tid = ed.templates()[0]["id"]
    res = ed.apply_template(tid, items, export=False, background="random")
    assert sorted(res["applied"]) == sorted(items)
    chosen = [ed.project(i)["spec"]["background"] for i in items]
    assert all(c["type"] == "image" and c["asset"] for c in chosen) and len({c["asset"] for c in chosen}) == len(items)
    # export marks the background as used
    ed.queue_export(items[0], to_ready=False)
    ed.export(items[0])
    with db.session() as s:
        assert s.get(EditAsset, chosen[0]["asset"]).use_count == 1


def test_download_to_reels_counter_trash_and_restore(settings, db, pipeline, clips, tmp_path):
    import hashlib
    from datetime import timedelta
    from app.database.models import utcnow
    from app.processing import cleanup, ready
    settings.raw["ready"] = {"dir": "Готовые", "download_dir": str(tmp_path / "Reels"), "trash_days": 7}
    for i in range(2):
        shutil.copy(clips["landscape" if i == 0 else "other"], settings.data("inbox") / f"d{i}.mp4")
    pipeline.run_once()
    with db.session() as s:
        ids = sorted(s.scalars(select(ContentItem.id).where(ContentItem.status == Status.READY_TO_PUBLISH)))
        s.get(ContentItem, ids[0]).post_description = "Описание #1"
    from app.ai.analyzer import category_labels
    for i in ids:  # sent to «Готовые» from the editor
        ready.add(settings, db, i, category_labels())
    client = TestClient(create_app(settings, db, start_worker=False))
    import subprocess
    orig = subprocess.run
    subprocess.run = lambda *a, **k: None  # do not open Finder in tests
    try:
        r = client.post("/api/ready/download", json={"ids": [ids[0]]}).json()
        r2 = client.post("/api/ready/download", json={"ids": [ids[1]]}).json()
    finally:
        subprocess.run = orig
    assert [f["file"] for f in r["files"]] == ["reels 1.mp4"] and [f["file"] for f in r2["files"]] == ["reels 2.mp4"]
    out = tmp_path / "Reels"
    with db.session() as s:
        src = ready.rendered_video(s, ids[1])
    assert hashlib.sha256((out / "reels 2.mp4").read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest()
    assert not list(out.glob("*.txt"))  # only the videos, no description files
    # both left «Готовые» for the trash; restore brings one back
    assert {i["id"] for i in client.get("/api/ready").json()["items"]} & set(ids) == set()
    assert client.post("/api/items/restore-many", json={"ids": [ids[0]]}).json()["restored"] == 1
    assert ids[0] in {i["id"] for i in client.get("/api/ready").json()["items"]}
    # the counter never repeats, even if files were removed by hand
    (out / "reels 1.mp4").unlink()
    r3 = ready.download(settings, db, [ids[0]])
    assert r3["files"][0]["file"] == "reels 3.mp4"
    # the trash empties itself after N days
    assert cleanup.run(settings, db, "trash", older_than=timedelta(days=7))["files"] == 0
    with db.session() as s:
        for i in ids:
            s.get(ContentItem, i).trashed_at = utcnow() - timedelta(days=8)
    assert cleanup.run(settings, db, "trash", older_than=timedelta(days=7))["files"] > 0
    with db.session() as s:
        assert all(s.get(ContentItem, i).trashed_from is None and s.get(ContentItem, i).original_file is None for i in ids)
