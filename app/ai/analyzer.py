from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from app.ai import rules
from app.ai.base import AnalysisContext, AnalysisResult, LLMError, LLMProvider
from app.config.settings import Settings

log = logging.getLogger("ai")
PROMPTS = Path(__file__).parent / "prompts"
CATEGORIES_FILE = Path(__file__).resolve().parents[1] / "config" / "categories.yaml"


def load_category_table() -> dict[str, dict]:
    with open(CATEGORIES_FILE, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("categories", {})


def load_categories() -> dict[str, list[str]]:
    """category → keyword stems."""
    return {k: v.get("keywords") or [] for k, v in load_category_table().items()}


def category_labels() -> dict[str, str]:
    return {k: v.get("label", k) for k, v in load_category_table().items()}


def build_provider(settings: Settings) -> LLMProvider | None:
    """None means rule-based analysis."""
    cfg = settings.section("ai")
    name = cfg.get("provider", "rules")
    if name == "rules":
        return None
    if name == "openai":
        from app.ai.openai import OpenAICompatibleProvider
        return OpenAICompatibleProvider(cfg.get("openai", {}), settings.env("OPENAI_API_KEY"),
                                        settings.env("OPENAI_BASE_URL"))
    if name == "llamacpp":
        from app.ai.llamacpp import LlamaCppProvider
        return LlamaCppProvider(cfg.get("llamacpp", {}))
    raise ValueError(f"неизвестный ai.provider: {name} (доступны: rules, openai, llamacpp)")


def _num(v, lo=0.0, hi=100.0) -> float | None:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return None


def parse_llm_json(text: str, categories: list[str], provider: str) -> AnalysisResult:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("в ответе модели нет JSON")
    data = json.loads(m.group(0))
    viral = _num(data.get("viral_score"))
    if viral is None:
        raise LLMError("в ответе нет viral_score")
    category = str(data.get("category") or "uncertain").strip().lower()
    action = str(data.get("recommended_action") or "review").lower()
    return AnalysisResult(
        provider=provider,
        category=category if category in categories else "uncertain",
        category_confidence=_num(data.get("category_confidence")) or 0.0,
        category_reason=data.get("category_reason"),
        subcategory=data.get("subcategory"),
        summary=data.get("summary"),
        hook=data.get("hook"),
        language=data.get("language"),
        emotion=data.get("emotion"),
        viral_score=viral,
        originality_score=_num(data.get("originality_score")),
        rights_risk_score=_num(data.get("rights_risk_score")),
        has_narrative=bool(data.get("has_narrative")),
        has_dialogue=bool(data.get("has_dialogue")),
        has_joke=bool(data.get("has_joke")),
        has_payoff=bool(data.get("has_payoff")),
        moderation_flags=[str(f).lower() for f in data.get("moderation_flags") or []],
        recommended_action=action if action in ("approve", "reject", "review") else "review",
        reason=str(data.get("reason") or ""),
        recommended_duration=_num(data.get("recommended_duration"), 0, 600),
        recommended_crop=data.get("recommended_crop"),
        needs_subtitles=bool(data.get("needs_subtitles")),
        needs_background=bool(data.get("needs_background")),
    )


class Analyzer:
    def __init__(self, settings: Settings):
        self.categories = load_categories()
        self.provider: LLMProvider | None = build_provider(settings)
        self.max_transcript_chars = int(settings.get("ai.max_transcript_chars", 4000))
        self.min_confidence = float(settings.get("ai.min_category_confidence", 60))
        self.system = (PROMPTS / "analyze_system.txt").read_text(encoding="utf-8").replace(
            "{categories}", ", ".join(self.categories))

    @property
    def provider_name(self) -> str:
        return self.provider.name if self.provider else "rules"

    def _prompt(self, ctx: AnalysisContext) -> str:
        payload = {
            "title": ctx.title, "caption": ctx.caption, "source": ctx.source, "channel": ctx.channel,
            "channel_default_category": ctx.category_hint, "rights_status": ctx.rights_status,
            "duration_sec": ctx.duration, "resolution": f"{ctx.width}x{ctx.height}", "has_audio": ctx.has_audio,
            "transcript_language": ctx.transcript_language,
            "transcript": (ctx.transcript or "")[: self.max_transcript_chars] or None,
            "technical_quality_score": ctx.quality_score, "technical_metrics": ctx.metrics,
        }
        return "Video data:\n" + json.dumps(payload, ensure_ascii=False, indent=1)

    def analyze(self, ctx: AnalysisContext) -> AnalysisResult:
        return finalize_category(self._analyze(ctx), ctx, self.min_confidence)

    def _analyze(self, ctx: AnalysisContext) -> AnalysisResult:
        if self.provider is None:
            return rules.analyze(ctx, self.categories)
        ready, why = self.provider.available()
        if not ready:
            res = rules.analyze(ctx, self.categories)
            res.notes.append(f"AI-провайдер {self.provider.name} не готов ({why}), использованы правила")
            return res
        last_error = None
        for _ in range(2):
            try:
                text = self.provider.generate(self.system, self._prompt(ctx))
                return parse_llm_json(text, list(self.categories), self.provider.name)
            except (LLMError, json.JSONDecodeError) as exc:
                last_error = exc
                log.warning("AI analysis attempt failed: %s", exc)
        res = rules.analyze(ctx, self.categories)
        res.notes.append(f"AI не дал корректный ответ ({last_error}), использованы правила")
        return res


def finalize_category(res: AnalysisResult, ctx: AnalysisContext, min_confidence: float) -> AnalysisResult:
    """Same rules for every provider: thin data or low confidence → "uncertain" (не разобрался)."""
    words = len(" ".join(x for x in (ctx.title, ctx.caption) if x).split())
    has_speech = bool((ctx.transcript or "").strip())
    if not has_speech and words < 5:
        guess = f" (предполагалось: {res.category})" if res.category not in ("uncertain", "other") else ""
        res.category, res.category_confidence = "uncertain", 0.0
        res.category_reason = "мало данных: нет речи и почти нет текста" + guess
    elif res.category != "uncertain" and res.category_confidence < min_confidence:
        res.category_reason = (f"уверенность {res.category_confidence:.0f}% ниже порога {min_confidence:.0f}%, "
                               f"предполагалось: {res.category}. " + (res.category_reason or ""))
        res.category = "uncertain"
    return res
