"""Housekeeping while the server runs: daily database backup, old temp/cache files, and — only when the
disk is fuller than `storage.cleanup_disk_percent` — originals of rejected videos. Nothing outside data/
is touched; originals of videos that are ready, in «Готовые» or published are always kept."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.database.models import ContentItem, Status, utcnow
from app.logging_setup import log_event
from app.processing.storage import disk_usage_percent

log = logging.getLogger("maintenance")
CACHE_DIRS = {"temp": 1, "editor_cache": 3, "tts_cache": 30, "review": 30}  # folder → keep days


def backup_dir(settings) -> Path:
    return settings.data("backups")


def last_backup(settings) -> Path | None:
    files = sorted(backup_dir(settings).glob("content_*.db"))
    return files[-1] if files else None


def backup_database(settings, keep: int | None = None) -> Path:
    """A consistent copy through SQLite's online backup (safe while the server writes)."""
    keep = keep or int(settings.get("storage.backup_keep", 14))
    folder = backup_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"content_{datetime.now():%Y-%m-%d_%H%M}.db"
    src = sqlite3.connect(str(settings.database_path))
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(folder.glob("content_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    log_event(log, "BACKUP", "SUCCESS", None, file=out.name)
    return out


def clean_caches(settings, now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    for sub, days in CACHE_DIRS.items():
        folder = settings.data(sub)
        if not folder.exists():
            continue
        for p in folder.iterdir():
            try:
                if now - p.lstat().st_mtime < days * 86400:
                    continue
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def free_disk(settings, db) -> int:
    """Deletes originals of rejected/duplicate videos (oldest first) while the disk is above the threshold."""
    limit = float(settings.get("storage.cleanup_disk_percent", 85))
    if disk_usage_percent(settings.data_dir) < limit:
        return 0
    removed = 0
    with db.session() as s:
        rows = list(s.scalars(select(ContentItem).where(
            ContentItem.status.in_([Status.REJECTED, Status.DUPLICATE, Status.FAILED]),
            ContentItem.original_file.is_not(None), ContentItem.ready_path.is_(None),
            ContentItem.trashed_from.is_(None))
            .order_by(ContentItem.updated_at)))
        for item in rows:
            if disk_usage_percent(settings.data_dir) < limit:
                break
            p = Path(item.original_file)
            if p.is_file() and settings.data_dir in p.parents:
                p.unlink()
                removed += 1
            item.original_file = None
    if removed:
        log_event(log, "CLEANUP", "SUCCESS", None, originals=removed)
    return removed


def run(settings, db, force_backup: bool = False) -> dict:
    """One maintenance round: backup once a day, then cleanup."""
    lb = last_backup(settings)
    backed = None
    if force_backup or lb is None or time.time() - lb.stat().st_mtime > 24 * 3600:
        backed = backup_database(settings).name
    from datetime import timedelta
    from app.processing import cleanup
    trash = cleanup.run(settings, db, "trash", older_than=timedelta(days=float(settings.get("ready.trash_days", 7))))
    return {"backup": backed, "cache_removed": clean_caches(settings), "originals_removed": free_disk(settings, db),
            "trash_emptied": trash["files"],
            "at": utcnow().isoformat() + "Z"}
