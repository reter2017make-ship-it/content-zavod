"""Turns analysis results into APPROVED / MANUAL_REVIEW / REJECTED with the rule that decided it."""
from __future__ import annotations

from dataclasses import dataclass

from app.database.models import PUBLISHABLE_RIGHTS, RightsStatus, Status
from app.scoring import moderation


@dataclass
class Decision:
    status: Status
    rule: str
    reason: str


def decide(*, quality: float, gate_reason: str | None, viral: float, content: float, provider: str,
           flags: set[str], ai_action: str, rights: RightsStatus, thresholds: dict,
           ai_flags: set[str] | None = None, auto_approve: bool = False) -> Decision:
    """With auto_approve (owner's decision) nothing waits for a person: what would go to manual review
    is approved, and the record keeps why it would have been checked."""
    d = _decide(quality=quality, gate_reason=gate_reason, viral=viral, content=content, provider=provider,
                flags=flags, ai_action=ai_action, rights=rights, thresholds=thresholds, ai_flags=ai_flags)
    if auto_approve and d.status == Status.MANUAL_REVIEW:
        if rights == RightsStatus.REJECTED:
            return Decision(Status.REJECTED, "rights", "лицензия запрещает переработку")
        return Decision(Status.APPROVED, "auto_approved", f"одобрен автоматически (без проверки: {d.reason})")
    return d


def _decide(*, quality: float, gate_reason: str | None, viral: float, content: float, provider: str,
            flags: set[str], ai_action: str, rights: RightsStatus, thresholds: dict,
            ai_flags: set[str] | None = None) -> Decision:
    """Keyword flags can reject on their own; flags raised only by the AI model go to a person
    (on real Telegram posts the model flagged banter as "hate" and a news story as "nsfw")."""
    ai_only = (ai_flags or set()) - flags
    mod = moderation.verdict(flags)
    if mod == "reject" and not thresholds.get("moderation_reject", True):
        mod = None  # flags are kept on the video as marks only
    if mod == "reject":
        return Decision(Status.REJECTED, "moderation", "модерация: " + ", ".join(sorted(flags)))
    if gate_reason:
        return Decision(Status.REJECTED, "quality_gate", gate_reason)
    min_q = float(thresholds.get("min_quality_score", 60))
    if quality < min_q:
        return Decision(Status.REJECTED, "quality_score", f"качество {quality:.0f} < {min_q:.0f}")
    min_v = float(thresholds.get("min_viral_score_rules" if provider == "rules" else "min_viral_score", 70))
    if viral < min_v:
        return Decision(Status.REJECTED, "viral_score", f"вирусность {viral:.0f} < {min_v:.0f} ({provider})")
    min_c = float(thresholds.get("min_content_score", 55))
    if content < min_c:
        return Decision(Status.REJECTED, "content_score", f"итоговый балл {content:.0f} < {min_c:.0f}")
    if mod == "review":
        return Decision(Status.MANUAL_REVIEW, "moderation", "модерация, нужна проверка: " + ", ".join(sorted(flags)))
    if ai_only:
        return Decision(Status.MANUAL_REVIEW, "ai_moderation",
                        "модель отметила: " + ", ".join(sorted(ai_only)) + " — нужна проверка человеком")
    if ai_action == "reject":
        return Decision(Status.MANUAL_REVIEW, "ai_recommendation", "AI советует отклонить, нужна проверка")
    if rights not in PUBLISHABLE_RIGHTS:
        return Decision(Status.MANUAL_REVIEW, "rights", f"права {rights.value}: нужна ручная проверка")
    if ai_action == "review":
        return Decision(Status.MANUAL_REVIEW, "ai_recommendation", "AI советует ручную проверку")
    return Decision(Status.APPROVED, "passed", f"прошёл все проверки, итоговый балл {content:.0f}")
