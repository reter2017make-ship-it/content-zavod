"""Analysis without a language model: keyword categories and a rough viral estimate.

It cannot judge humour or payoff, so its viral_score is only a weak signal and
`scores.min_viral_score_rules` is used as the threshold instead of `min_viral_score`.
"""
from __future__ import annotations

import re

from app.ai.base import AnalysisContext, AnalysisResult

HOOK_PATTERNS = [r"смотри", r"не поверите", r"знаете ли", r"никто", r"удив", r"невероят", r"секрет",
                 r"впервые", r"\?", r"!", r"watch till", r"you won't believe"]


def keyword_hits(text: str, categories: dict[str, list[str]]) -> dict[str, list[str]]:
    """Keywords are word stems: they must start a word ("кот" matches "котик", not "наркотик")."""
    text = text.lower()
    out = {}
    for name, words in categories.items():
        hit = [w for w in words if w and re.search(r"(?<!\w)" + re.escape(w.lower()), text)]
        if hit:
            out[name] = hit
    return out


def guess_category(text: str, categories: dict[str, list[str]], hint: str | None = None) -> tuple[str, float, str]:
    """→ (category, confidence 0–100, reason). No keyword evidence → uncertain, never a guess."""
    hits = keyword_hits(text, categories)
    if not hits:
        return "uncertain", 0.0, "нет ключевых слов ни одной категории"
    best = max(hits, key=lambda k: (len(hits[k]), k == hint))
    conf = min(90.0, 40.0 + 20.0 * len(hits[best]))
    runner_up = max((len(v) for k, v in hits.items() if k != best), default=0)
    if runner_up == len(hits[best]):
        conf -= 20  # a tie between categories
    if hint and best == hint:
        conf = min(95.0, conf + 10)
    return best, conf, f"ключевые слова: {', '.join(hits[best][:5])}" + (" (совпадает с категорией канала)" if hint == best else "")


def analyze(ctx: AnalysisContext, categories: dict[str, list[str]]) -> AnalysisResult:
    transcript = (ctx.transcript or "").strip()
    words = transcript.split()
    blob = " ".join(x for x in (ctx.title, ctx.caption, transcript) if x)

    category, confidence, cat_reason = guess_category(blob, categories, ctx.category_hint)

    viral, why = 20.0, ["база 20"]
    if words:
        viral += 25
        why.append("есть речь +25")
        density = len(words) / max(ctx.duration, 1)
        if 1.2 <= density <= 4.5:
            viral += 10
            why.append("темп речи +10")
        opening = " ".join(words[:15]).lower()
        if any(re.search(p, opening) for p in HOOK_PATTERNS):
            viral += 10
            why.append("крючок в начале +10")
    if 7 <= ctx.duration <= 60:
        viral += 5
        why.append("длительность 7–60 с +5")
    if ctx.width <= ctx.height * 1.05:
        viral += 5
        why.append("вертикальный/квадратный +5")
    viral = min(viral, 80.0)

    first_sentence = re.split(r"(?<=[.!?])\s", transcript, maxsplit=1)[0] if transcript else None
    return AnalysisResult(
        provider="rules",
        category=category,
        category_confidence=confidence,
        category_reason=cat_reason,
        summary=first_sentence,
        hook=first_sentence,
        language=ctx.transcript_language,
        viral_score=viral,
        has_dialogue=bool(words),
        recommended_action="approve",
        reason="оценка по правилам: " + ", ".join(why),
        recommended_crop="blur",
        needs_subtitles=bool(words),
    )
