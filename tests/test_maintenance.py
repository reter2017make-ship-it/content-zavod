"""Stage 5 housekeeping: database backup, cache cleanup, freeing disk; the dashboard API."""
import os
import time

from fastapi.testclient import TestClient

from app.database.models import ContentItem, RightsStatus, Status
from app.processing import maintenance
from app.web.server import create_app


def test_backup_keeps_last_n_and_is_a_valid_db(settings, db):
    import sqlite3
    with db.session() as s:
        s.add(ContentItem(source="t", source_id="1", status=Status.DISCOVERED, rights_status=RightsStatus.UNKNOWN))
    outs = []
    for i in range(3):
        p = maintenance.backup_database(settings, keep=2)
        os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
        new = p.with_name(f"content_2026-01-0{i + 1}_0000.db")
        p.rename(new)
        outs.append(new)
    maintenance.backup_database(settings, keep=2)
    files = sorted(maintenance.backup_dir(settings).glob("content_*.db"))
    assert len(files) == 2
    con = sqlite3.connect(files[-1])
    assert con.execute("select count(*) from content_items").fetchone()[0] == 1


def test_daily_backup_and_cache_cleanup(settings, db):
    old = settings.data("editor_cache") / "old.jpg"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    fresh = settings.data("editor_cache") / "fresh.jpg"
    fresh.write_bytes(b"x")
    r = maintenance.run(settings, db)
    assert r["backup"] and r["cache_removed"] == 1 and not old.exists() and fresh.exists()
    assert maintenance.run(settings, db)["backup"] is None  # once a day


def test_free_disk_removes_only_rejected_originals(settings, db, monkeypatch):
    raw = settings.data("raw")
    raw.mkdir(parents=True, exist_ok=True)
    files = {}
    with db.session() as s:
        for n, st in (("rej", Status.REJECTED), ("ok", Status.READY_TO_PUBLISH), ("trash", Status.REJECTED)):
            f = raw / f"{n}.mp4"
            f.write_bytes(b"0" * 10)
            files[n] = f
            s.add(ContentItem(source="t", source_id=n, status=st, rights_status=RightsStatus.UNKNOWN,
                              original_file=str(f), trashed_from="READY_TO_PUBLISH" if n == "trash" else None))
    settings.raw.setdefault("storage", {})["cleanup_disk_percent"] = 85
    monkeypatch.setattr(maintenance, "disk_usage_percent", lambda p: 90.0)
    assert maintenance.free_disk(settings, db) == 1
    assert not files["rej"].exists() and files["ok"].exists() and files["trash"].exists()
    monkeypatch.setattr(maintenance, "disk_usage_percent", lambda p: 50.0)
    assert maintenance.free_disk(settings, db) == 0


def test_dashboard(settings, db):
    client = TestClient(create_app(settings, db, start_worker=False))
    d = client.get("/api/dashboard").json()
    assert set(d["tiles"]) >= {"downloaded", "in_ready", "published", "errors", "to_edit"}
    assert "next_scan" in d["worker"] and d["disk_percent"] > 0
    assert client.get("/").status_code == 200


def test_cleanup_actions_only_touch_what_they_say(settings, db, tmp_path):
    from datetime import timedelta
    from app.database.models import utcnow
    from app.processing import cleanup
    from app.processing.ready import ready_dir
    raw, rd = settings.data("raw"), ready_dir(settings)
    raw.mkdir(parents=True, exist_ok=True)
    rd.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"1")

    def mk(name, **kw):
        f = raw / f"{name}.mp4"
        f.write_bytes(b"0" * 100)
        return ContentItem(source="t", source_id=name, rights_status=RightsStatus.UNKNOWN, original_file=str(f), **kw)

    pub = rd / "pub.mp4"; pub.write_bytes(b"0" * 50); pub.with_suffix(".txt").write_text("d")
    fresh = rd / "fresh.mp4"; fresh.write_bytes(b"0" * 50)
    with db.session() as s:
        s.add_all([
            mk("ready", status=Status.READY_TO_PUBLISH, ready_path=str(fresh)),
            mk("trash", status=Status.REJECTED, trashed_from="READY_TO_PUBLISH"),
            mk("bad", status=Status.REJECTED),
            mk("dup", status=Status.DUPLICATE),
            mk("work", status=Status.READY_TO_PUBLISH),
            ContentItem(source="t", source_id="posted", status=Status.READY_TO_PUBLISH, rights_status=RightsStatus.UNKNOWN,
                        ready_path=str(pub), posted_at=utcnow() - timedelta(days=40), original_file=str(outside)),
        ])
    pv = cleanup.preview(settings, db, "rejected")
    assert pv["files"] == 2 and pv["bytes"] == 200 and (raw / "bad.mp4").exists()  # preview deletes nothing
    assert cleanup.run(settings, db, "rejected")["files"] == 2
    assert not (raw / "bad.mp4").exists() and not (raw / "dup.mp4").exists() and (raw / "trash.mp4").exists()
    assert cleanup.run(settings, db, "trash")["files"] == 1 and not (raw / "trash.mp4").exists()
    assert cleanup.run(settings, db, "ready_sources")["files"] == 1 and not (raw / "ready.mp4").exists()
    assert fresh.exists() and (raw / "work.mp4").exists()  # unpublished ready copy and work videos stay
    assert cleanup.preview(settings, db, "published", days=60)["files"] == 0
    assert cleanup.run(settings, db, "published", days=30)["files"] == 2 and not pub.exists() and fresh.exists()
    assert outside.exists()  # never anything outside data/ and «Готовые»
    rep = cleanup.report(settings, db)
    assert "Папка «Готовые»" in rep["sections"] and len(rep["actions"]) == 5


def test_work_lists_hide_ready_duplicates_and_trash(settings, db):
    client = TestClient(create_app(settings, db, start_worker=False))
    with db.session() as s:
        for n, kw in (("a", {}), ("b", {"ready_path": "/x.mp4"}), ("c", {"status": Status.DUPLICATE}),
                      ("d", {"status": Status.REJECTED, "trashed_from": "READY_TO_PUBLISH"})):
            s.add(ContentItem(source="t", source_id=n, title=n, rights_status=RightsStatus.UNKNOWN,
                              **({"status": Status.READY_TO_PUBLISH} | kw)))
    titles = lambda url: sorted(i["title"] for i in client.get(url).json()["items"])  # noqa: E731
    assert titles("/api/items") == ["a"]
    assert titles("/api/items?show_all=true") == ["a", "b", "c", "d"]
    r = client.post("/api/storage/clean", json={"action": "temp"}).json()
    assert "files" in r and "freed" not in r  # without confirm only the preview
