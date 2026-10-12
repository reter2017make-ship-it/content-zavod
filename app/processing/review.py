"""Manual review queue: data/review holds links to the originals waiting for a decision."""
from __future__ import annotations

import os
from pathlib import Path

from app.config.settings import Settings
from app.database.models import PUBLISHABLE_RIGHTS, ContentItem, RightsStatus, Status, utcnow


def _link_path(settings: Settings, item: ContentItem) -> Path:
    name = item.original_filename or Path(item.original_file or "video.mp4").name
    return settings.data("review") / f"{item.id}_{name}"


def review_link(settings: Settings, item: ContentItem) -> None:
    link = _link_path(settings, item)
    link.parent.mkdir(parents=True, exist_ok=True)
    if item.original_file and not link.exists():
        try:
            os.symlink(item.original_file, link)
        except OSError:  # Windows allows symlinks only in developer mode; the link is just a convenience
            pass


def _unlink(settings: Settings, item: ContentItem) -> None:
    link = _link_path(settings, item)
    if link.is_symlink() or link.exists():
        link.unlink()


def approve(settings: Settings, item: ContentItem, note: str | None = None) -> None:
    """A person confirmed the content and its rights; it goes on to processing."""
    if item.status != Status.MANUAL_REVIEW:
        raise ValueError(f"#{item.id} не на проверке (статус {item.status.value})")
    if item.rights_status not in PUBLISHABLE_RIGHTS:
        item.rights_status = RightsStatus.VERIFIED
    item.status = Status.APPROVED
    item.reviewed_at = utcnow()
    item.decision_reason = "одобрено вручную" + (f": {note}" if note else "")
    _unlink(settings, item)


def reject(settings: Settings, item: ContentItem, note: str | None = None) -> None:
    if item.status != Status.MANUAL_REVIEW:
        raise ValueError(f"#{item.id} не на проверке (статус {item.status.value})")
    item.status = Status.REJECTED
    item.reviewed_at = utcnow()
    item.decision_reason = item.rejection_reason = "отклонено вручную" + (f": {note}" if note else "")
    _unlink(settings, item)
