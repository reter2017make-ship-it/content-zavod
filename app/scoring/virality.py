"""Supporting scores and the final content score (section 22 of the spec)."""
from __future__ import annotations

from datetime import datetime

from app.database.models import RightsStatus

DEFAULT_WEIGHTS = {"viral": 0.40, "quality": 0.20, "originality": 0.15,
                   "source": 0.10, "freshness": 0.10, "format": 0.05}

RIGHTS_RISK = {
    RightsStatus.USER_OWNED: 0, RightsStatus.CC0: 5, RightsStatus.PUBLIC_DOMAIN: 5,
    RightsStatus.LICENSED: 10, RightsStatus.CC_BY: 10, RightsStatus.CC_BY_SA: 15,
    RightsStatus.PERMISSION_GRANTED: 10, RightsStatus.VERIFIED: 15,
    RightsStatus.REQUIRES_REVIEW: 60, RightsStatus.UNKNOWN: 70, RightsStatus.REJECTED: 100,
}


def freshness_score(published_at: datetime | None, now: datetime | None = None) -> float:
    """Fresh material ranks higher; evergreen never drops below 30."""
    if published_at is None:
        return 50.0
    days = max(0.0, ((now or datetime.now()) - published_at).total_seconds() / 86400)
    return round(max(30.0, 100.0 - 3.0 * days), 1)


def format_score(width: int, height: int) -> float:
    ratio = width / height if height else 1
    if ratio <= 0.65:
        return 100.0
    if ratio <= 1.05:
        return 70.0
    return 60.0


def originality_from_duplicates(duplicate_score: float | None) -> float:
    """duplicate_score is visual similarity to the closest stored item (≈50 for unrelated clips)."""
    if duplicate_score is None:
        return 100.0
    return round(max(0.0, min(100.0, 100.0 - max(0.0, duplicate_score - 50.0) * 2)), 1)


def content_score(scores: dict[str, float], weights: dict | None = None) -> float:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total = sum(w.values()) or 1
    return round(sum(scores.get(k, 0.0) * v for k, v in w.items()) / total, 1)
