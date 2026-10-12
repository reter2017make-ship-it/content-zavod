"""«Готовые видео»: the person publishes by hand. A ready video is copied to a plain folder with a readable
name (дата_категория_канал_id.mp4); the description stays in the base and the UI (no .txt, owner's choice). Nothing is posted anywhere."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.database.models import ContentItem, Status, Variant, rights_ok, utcnow


class ReadyError(ValueError):
    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.http_status = http_status


def ready_dir(settings) -> Path:
    return settings.path(settings.get("ready.dir", "Готовые"))


def _slug(text: str | None, fallback: str) -> str:
    text = re.sub(r"[^\w-]+", "-", (text or "").strip(), flags=re.UNICODE).strip("-_")
    return (text or fallback)[:40]


def file_name(item: ContentItem, labels: dict[str, str]) -> str:
    date = (item.ready_at or utcnow()).strftime("%Y-%m-%d")
    cat = _slug(labels.get(item.category or "", item.category), "без-категории")
    channel = item.channel or item.source
    if channel and "/" in channel:  # local folders store the path as the channel
        channel = Path(channel).name
    src = _slug(channel, "источник")
    return f"{date}_{cat}_{src}_{item.id}.mp4"


def rendered_video(s, item_id: int) -> Path | None:
    """The editor's export if there is one, otherwise the automatic render."""
    rows = list(s.scalars(select(Variant).where(Variant.content_id == item_id).order_by(Variant.id.desc())))
    rows.sort(key=lambda v: v.profile != "edited")
    for v in rows:
        if v.path and Path(v.path).is_file():
            return Path(v.path)
    return None


def _remove_copy(item: ContentItem) -> None:
    if item.ready_path:
        p = Path(item.ready_path)
        p.unlink(missing_ok=True)
        p.with_suffix(".txt").unlink(missing_ok=True)


def add(settings, db, item_id: int, labels: dict[str, str], description: str | None = None) -> str:
    """Copies the rendered video (and its description) to the ready folder. Repeating it refreshes the copy."""
    with db.session() as s:
        item = s.get(ContentItem, item_id)
        if item is None:
            raise ReadyError("нет такого ролика", 404)
        if not rights_ok(settings, item.rights_status):
            raise ReadyError(f"права {item.rights_status.value}: сначала одобрите ролик на ручной проверке")
        if item.status not in (Status.READY_TO_PUBLISH, Status.PUBLISHED) and rendered_video(s, item_id) is None:
            raise ReadyError(f"ролик ещё не смонтирован (статус {item.status.value})")
        src = rendered_video(s, item_id)
        if src is None:
            raise ReadyError("файла готового ролика нет")
        if description is not None:
            item.post_description = description.strip() or None
        _remove_copy(item)
        item.ready_at = utcnow()
        folder = ready_dir(settings)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / file_name(item, labels)
        tmp = dest.with_suffix(".part")
        shutil.copy2(src, tmp)
        tmp.replace(dest)
        item.ready_path = str(dest)
        return str(dest)


def write_description(db, item_id: int, description: str) -> None:
    """Saves the description (in the base only; the old .txt next to a ready video is removed)."""
    with db.session() as s:
        item = s.get(ContentItem, item_id)
        if item is None:
            raise ReadyError("нет такого ролика", 404)
        item.post_description = description.strip() or None
        if item.ready_path:
            Path(item.ready_path).with_suffix(".txt").unlink(missing_ok=True)


def remove(db, item_id: int) -> None:
    """«Убрать из готовых»: deletes only the copy in the ready folder; the factory's own files stay."""
    with db.session() as s:
        item = s.get(ContentItem, item_id)
        if item is None:
            raise ReadyError("нет такого ролика", 404)
        _remove_copy(item)
        item.ready_path = item.ready_at = None


def mark_posted(db, item_id: int, posted: bool, when: datetime | None = None, note: str | None = None) -> None:
    with db.session() as s:
        item = s.get(ContentItem, item_id)
        if item is None:
            raise ReadyError("нет такого ролика", 404)
        if posted:
            item.posted_at, item.posted_note = when or utcnow(), (note or "").strip() or None
        else:
            item.posted_at = item.posted_note = None


def items(s) -> list[ContentItem]:
    """Only what the owner sent to «Готовые» (from the editor); rendered-but-not-chosen videos wait in the editor."""
    return list(s.scalars(select(ContentItem).where(
        ContentItem.ready_path.is_not(None), ContentItem.trashed_from.is_(None))
        .order_by(ContentItem.ready_at.desc(), ContentItem.id.desc())))


COUNTER_KEY = "reels_counter"


def download_dir(settings) -> Path:
    import os
    return Path(os.environ.get("FACTORY_DOWNLOAD_DIR") or str(settings.get("ready.download_dir") or "~/Downloads/Reels")).expanduser()


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(settings, db, item_ids: list[int], to_trash: bool = True) -> dict:
    """Copies the finished videos byte for byte to the download folder as «reels N.mp4» (N never repeats,
    the counter lives in the base), only the video (no description file); checks size and SHA-256. Copied videos
    leave «Готовые» for the trash (restorable; the trash empties itself after a week)."""
    from app.database.models import AppSetting
    dest = download_dir(settings)
    dest.mkdir(parents=True, exist_ok=True)
    done, errors = [], {}
    with db.session() as s:
        row = s.get(AppSetting, COUNTER_KEY)
        n = int(row.value) if row and row.value else 0
    for iid in item_ids:
        with db.session() as s:
            item = s.get(ContentItem, iid)
            if item is None:
                errors[iid] = "нет такого ролика"
                continue
            src = Path(item.ready_path) if item.ready_path and Path(item.ready_path).is_file() else rendered_video(s, iid)
        if src is None or not src.is_file():
            errors[iid] = "файла готового ролика нет"
            continue
        n += 1
        while (dest / f"reels {n}.mp4").exists():
            n += 1
        out, tmp = dest / f"reels {n}.mp4", dest / f".reels {n}.part"
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != src.stat().st_size or _sha256(tmp) != _sha256(src):
            tmp.unlink(missing_ok=True)
            errors[iid] = "копия не совпала с оригиналом"
            continue
        tmp.replace(out)
        with db.session() as s:
            row = s.get(AppSetting, COUNTER_KEY) or AppSetting(key=COUNTER_KEY)
            row.value = str(n)
            s.add(row)
            if to_trash:
                item = s.get(ContentItem, iid)
                item.trashed_from, item.trashed_at = item.status.value, utcnow()
                item.status = Status.REJECTED
                item.decision_reason = f"скачан как «{out.name}» и убран в корзину"
        done.append({"id": iid, "file": out.name})
    return {"folder": str(dest), "files": done, "errors": {str(k): v for k, v in errors.items()}}
