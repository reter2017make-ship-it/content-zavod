from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    pass


class LLMProvider(ABC):
    """Any text model. Switching providers is a config change (`ai.provider`)."""

    name: str = "base"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(ready, reason if not)."""

    @abstractmethod
    def generate(self, system: str, prompt: str, max_tokens: int = 700) -> str:
        """Plain completion text."""


@dataclass
class AnalysisContext:
    title: str | None
    caption: str | None
    source: str
    channel: str | None
    source_category: str | None
    rights_status: str
    duration: float
    width: int
    height: int
    has_audio: bool
    transcript: str | None
    transcript_language: str | None
    quality_score: float
    quality_parts: dict
    metrics: dict
    category_hint: str | None = None      # channel default: a hint for the model, not an answer


@dataclass
class AnalysisResult:
    provider: str
    category: str
    category_confidence: float = 0.0
    category_reason: str | None = None
    subcategory: str | None = None
    summary: str | None = None
    hook: str | None = None
    language: str | None = None
    emotion: str | None = None
    viral_score: float = 0.0
    originality_score: float | None = None
    rights_risk_score: float | None = None
    has_narrative: bool = False
    has_dialogue: bool = False
    has_joke: bool = False
    has_payoff: bool = False
    moderation_flags: list[str] = field(default_factory=list)
    recommended_action: str = "review"
    reason: str = ""
    recommended_duration: float | None = None
    recommended_crop: str | None = None
    needs_subtitles: bool = False
    needs_background: bool = False
    notes: list[str] = field(default_factory=list)
