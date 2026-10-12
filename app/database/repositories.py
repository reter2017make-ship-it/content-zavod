from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database.models import TRANSIENT_ROLLBACK, ContentItem, Status, utcnow


def exists(session: Session, source: str, source_id: str) -> bool:
    return session.scalar(
        select(ContentItem.id).where(ContentItem.source == source, ContentItem.source_id == source_id)
    ) is not None


def known_source_ids(session: Session, source: str) -> set[str]:
    return set(session.scalars(select(ContentItem.source_id).where(ContentItem.source == source)))


def by_sha256(session: Session, sha: str, exclude_id: int | None = None) -> ContentItem | None:
    q = select(ContentItem).where(ContentItem.sha256 == sha, ContentItem.status != Status.DUPLICATE)
    if exclude_id is not None:
        q = q.where(ContentItem.id != exclude_id)
    return session.scalars(q.order_by(ContentItem.id)).first()


def with_phash(session: Session, exclude_id: int) -> list[ContentItem]:
    return list(session.scalars(
        select(ContentItem).where(
            ContentItem.phash.is_not(None), ContentItem.id != exclude_id,
            ContentItem.status != Status.DUPLICATE,
        ).order_by(ContentItem.id)
    ))


def due(session: Session, status: Status, limit: int) -> list[ContentItem]:
    """Items in `status` whose retry delay (if any) has passed."""
    now = utcnow()
    return list(session.scalars(
        select(ContentItem)
        .where(ContentItem.status == status,
               or_(ContentItem.next_retry_at.is_(None), ContentItem.next_retry_at <= now))
        .order_by(ContentItem.id)
        .limit(limit)
    ))


def count_by_status(session: Session) -> dict[str, int]:
    rows = session.execute(select(ContentItem.status, func.count()).group_by(ContentItem.status))
    return {s.value: n for s, n in rows}


def recent(session: Session, limit: int = 10) -> list[ContentItem]:
    return list(session.scalars(select(ContentItem).order_by(ContentItem.updated_at.desc()).limit(limit)))


def stuck(session: Session) -> list[ContentItem]:
    return list(session.scalars(select(ContentItem).where(ContentItem.status.in_(list(TRANSIENT_ROLLBACK)))))


def register_failure(item: ContentItem, back_to: Status, error: str, max_retries: int, delay_minutes: int) -> None:
    """Retry later by returning to `back_to`, or give up with FAILED after max_retries."""
    item.retry_count = (item.retry_count or 0) + 1
    item.last_error = error[:2000]
    if item.retry_count >= max_retries:
        item.status = Status.FAILED
        item.next_retry_at = None
    else:
        item.status = back_to
        item.next_retry_at = utcnow() + timedelta(minutes=delay_minutes * 2 ** (item.retry_count - 1))


def clear_errors(item: ContentItem) -> None:
    item.retry_count = 0
    item.last_error = None
    item.next_retry_at = None

