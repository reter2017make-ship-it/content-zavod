from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None


@dataclass
class Transcript:
    text: str
    language: str | None
    segments: list[Segment] = field(default_factory=list)
    language_probability: float | None = None

    @property
    def has_speech(self) -> bool:
        return bool(self.text.strip())

    def segments_json(self) -> list[dict]:
        return [asdict(s) for s in self.segments]
