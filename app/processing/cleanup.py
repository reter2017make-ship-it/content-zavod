"""«Место на диске»: sizes by section and cleanup actions started by the owner from the UI.

Every action first shows what it would delete (count and size), then deletes only after confirmation.
Only files inside data/ and the «Готовые» folder are ever removed. Unpublished videos in «Готовые» are
never touched; originals of ready videos go only with the explicit «исходники готовых роликов» action.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.database.models import ContentItem, Status, Variant, utcnow
from app.logging_setup import log_event
from app.processing.ready import ready_dir

import logging

log = logging.getLogger("cleanup")
TEMP_DIRS = ("temp", "processing", "editor_cache", "tts_cache", "review")
ACTIONS = {
    "ready_sources": "Исходники роликов, которые уже в «Готовых»",
    "trash": "Корзина",
    "rejected": "Отклонённые, брак и дубли",
    "temp": "Временные файлы и кэш",
    "published": "Опубликованные ролики в папке «Готовые»",
}


@dataclass
class Plan:
    files: list[Path] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files if p.is_file())


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def _allowed(settings, p: Path) -> bool:
    p = p.resolve()
    roots = [settings.data_dir.resolve(), ready_dir(settings).resolve()]
    return any(r == p or r in p.parents for r in roots)


def _item_files(s, item: ContentItem) -> list[Path]:
    """The factory's own files of a video: original and renders (not the copy in «Готовые»)."""
    files = [Path(item.original_file)] if item.original_file else []
    for v in s.scalars(select(Variant).where(Variant.content_id == item.id)):
        files += [Path(x) for x in (v.path, v.thumbnail, v.metadata_path) if x]
    return [f for f in files if f.is_file()]


def plan(settings, db, action: str, days: int = 30, older_than: timedelta | None = None) -> Plan:
    if action not in ACTIONS:
        raise ValueError("нет такой очистки")
    out = Plan()
    if action == "temp":
        now = time.time()
        for sub in TEMP_DIRS:
            folder = settings.data(sub)
            if not folder.exists():
                continue
            for p in folder.rglob("*"):
                # files of a render that is running right now are younger than 2 hours
                if (p.is_file() or p.is_symlink()) and (sub != "processing" or now - p.lstat().st_mtime > 7200):
                    out.files.append(p)
        return out
    with db.session() as s:
        if action == "ready_sources":
            rows = s.scalars(select(ContentItem).where(ContentItem.ready_path.is_not(None),
                                                       ContentItem.original_file.is_not(None)))
            for it in rows:
                if Path(it.original_file).is_file():
                    out.files.append(Path(it.original_file))
                    out.item_ids.append(it.id)
        elif action in ("trash", "rejected"):
            q = select(ContentItem).where(ContentItem.status.in_([Status.REJECTED, Status.DUPLICATE, Status.FAILED]))
            if action == "rejected":
                q = q.where(ContentItem.ready_path.is_(None))
            q = q.where(ContentItem.trashed_from.is_not(None) if action == "trash" else ContentItem.trashed_from.is_(None))
            if older_than is not None:
                q = q.where(ContentItem.trashed_at <= utcnow() - older_than)
            for it in s.scalars(q):
                files = _item_files(s, it)
                if action == "trash" and it.ready_path:  # downloaded videos: their copy in «Готовые» too
                    rp = Path(it.ready_path)
                    files += [f for f in (rp, rp.with_suffix(".txt")) if f.is_file()]
                if files or action == "trash":
                    out.files += files
                    out.item_ids.append(it.id)
        elif action == "published":
            edge = utcnow() - timedelta(days=max(0, days))
            for it in s.scalars(select(ContentItem).where(ContentItem.posted_at.is_not(None),
                                                          ContentItem.posted_at <= edge,
                                                          ContentItem.ready_path.is_not(None))):
                p = Path(it.ready_path)
                out.files += [f for f in (p, p.with_suffix(".txt")) if f.is_file()]
                out.item_ids.append(it.id)
    out.files = [f for f in out.files if _allowed(settings, f)]
    return out


def preview(settings, db, action: str, days: int = 30) -> dict:
    p = plan(settings, db, action, days)
    return {"action": action, "label": ACTIONS[action], "files": len(p.files), "bytes": p.bytes, "items": len(p.item_ids)}


def run(settings, db, action: str, days: int = 30, older_than: timedelta | None = None) -> dict:
    p = plan(settings, db, action, days, older_than)
    freed = p.bytes
    for f in p.files:
        try:
            f.unlink()
        except OSError:
            pass
    with db.session() as s:
        for iid in p.item_ids:
            it = s.get(ContentItem, iid)
            if it is None:
                continue
            if action == "ready_sources":
                it.original_file = None
            elif action in ("trash", "rejected"):
                it.original_file = None
                for v in s.scalars(select(Variant).where(Variant.content_id == iid)):
                    s.delete(v)
                if action == "trash":
                    it.trashed_from = it.trashed_at = it.ready_path = None  # emptied: cannot be restored any more
            elif action == "published":
                it.ready_path = None
    if action == "temp":
        for sub in TEMP_DIRS:  # empty folders left by renders
            for d in sorted(settings.data(sub).rglob("*"), reverse=True) if settings.data(sub).exists() else []:
                if d.is_dir() and not any(d.iterdir()):
                    shutil.rmtree(d, ignore_errors=True)
    log_event(log, "CLEANUP", "SUCCESS", None, action=action, files=len(p.files), freed_mb=round(freed / 2 ** 20))
    return {"action": action, "files": len(p.files), "freed": freed}


def report(settings, db, days: int = 30) -> dict:
    from app.processing.storage import disk_usage_percent
    sections = {
        "Исходники (скачанные видео)": _size(settings.data("raw")),
        "Смонтированные ролики": _size(settings.data("final")),
        "Папка «Готовые»": _size(ready_dir(settings)),
        "Временные файлы и кэш": sum(_size(settings.data(x)) for x in TEMP_DIRS),
        "Копии базы": _size(settings.data("backups")),
        "Голоса озвучки": _size(settings.data("voices")),
        "Фоны и баннеры": _size(settings.data("assets")),
    }
    usage = shutil.disk_usage(settings.data_dir)
    return {"sections": sections, "disk_percent": round(disk_usage_percent(settings.data_dir), 1),
            "disk_free": usage.free, "actions": [preview(settings, db, a, days) for a in ACTIONS], "days": days}


UNUSED_STATES = (Status.DISCOVERED, Status.DISCOVERY_ONLY, Status.DOWNLOADED, Status.APPROVED, Status.MANUAL_REVIEW,
                 Status.FAILED, Status.REJECTED, Status.DUPLICATE)


def drop_channel_videos(settings, db, channel: str) -> dict:
    """A channel was deleted from the factory: its videos that were never used (not rendered, not in «Готовые»,
    not opened in the editor) leave the lists and their files are deleted. Rendered and ready ones stay."""
    from app.database.models import EditProject
    edited = select(EditProject.content_id)
    files, n = [], 0
    with db.session() as s:
        rows = list(s.scalars(select(ContentItem).where(
            ContentItem.channel == channel, ContentItem.status.in_(UNUSED_STATES), ContentItem.ready_path.is_(None),
            ContentItem.posted_at.is_(None), ContentItem.id.not_in(edited))))
        for it in rows:
            files += [f for f in _item_files(s, it) if _allowed(settings, f)]
            for v in s.scalars(select(Variant).where(Variant.content_id == it.id)):
                s.delete(v)
            it.original_file = None
            if it.status != Status.DUPLICATE:
                it.status = Status.REJECTED
            it.decision_reason = it.rejection_reason = "канал удалён из завода"
            it.trashed_from = it.trashed_at = None
            n += 1
    freed = sum(f.stat().st_size for f in files if f.is_file())
    for f in files:
        f.unlink(missing_ok=True)
    log_event(log, "CHANNEL_DROP", "SUCCESS", None, channel=channel, items=n, freed_mb=round(freed / 2 ** 20))
    return {"items": n, "freed": freed}
