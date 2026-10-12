"""Basic keyword moderation over title/caption/transcript. Crude by design: AI flags are added on top."""
from __future__ import annotations

import re

# flag → patterns (Russian and English word stems)
PATTERNS: dict[str, list[str]] = {
    "illegal": [r"наркотик", r"кокаин", r"героин", r"мефедрон", r"закладк", r"\bбомб(а|у|ы|ой|е)?\b",
                r"взрывчатк", r"\bdrugs?\b", r"\bcocaine\b", r"\bbomb\b", r"купить оружие"],
    "sexual": [r"порно", r"\bсекс", r"эротик", r"\bporn", r"\bnude", r"\bnsfw\b", r"onlyfans"],
    "gore": [r"расчлен", r"кровищ", r"\bgore\b"],
    "hate": [r"\bнацист", r"\bгеноцид", r"\bhate speech\b"],
    "extreme_violence": [r"убийств", r"избиени", r"\bрастерзал", r"\bkilling\b"],
    "dangerous": [r"не повторяйте", r"опасный трюк", r"\bdangerous stunt\b"],
}

REJECT_FLAGS = {"illegal", "sexual", "nsfw", "gore", "hate"}
REVIEW_FLAGS = {"extreme_violence", "dangerous"}


def keyword_flags(*texts: str | None) -> set[str]:
    blob = " ".join(t for t in texts if t).lower()
    return {flag for flag, pats in PATTERNS.items() if any(re.search(p, blob) for p in pats)}


def verdict(flags: set[str]) -> str | None:
    """'reject', 'review' or None."""
    if flags & REJECT_FLAGS:
        return "reject"
    if flags & REVIEW_FLAGS or flags - REJECT_FLAGS - REVIEW_FLAGS:
        return "review"
    return None
