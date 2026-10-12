"""Technical quality score 0–100 from FFprobe data and sampled-frame metrics."""
from __future__ import annotations

from app.media.ffprobe import MediaInfo
from app.media.metrics import MediaMetrics


def _lin(x: float, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi (works for hi < lo too), clamped."""
    if hi == lo:
        return 1.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def quality_score(info: MediaInfo, m: MediaMetrics) -> tuple[float, dict]:
    short = min(info.width, info.height)
    parts = {}
    parts["resolution"] = 20 if short >= 1080 else 16 if short >= 720 else 12 if short >= 540 else 6 if short >= 360 else 0
    bpp = (info.bitrate or 0) / max(1, info.width * info.height * (info.fps or 30))
    parts["bitrate"] = round(10 * _lin(bpp, 0.0, 0.03), 1)
    if m.blur is None:
        # No measurable edges: smooth footage keeps partial credit, a frozen flat frame gets none.
        parts["sharpness"] = 12 if m.frozen_ratio < 0.5 else 0
    else:
        parts["sharpness"] = round(20 * _lin(m.blur, 20, 6), 1)
    parts["motion"] = round(15 * (1 - m.frozen_ratio), 1)
    parts["not_black"] = round(15 * (1 - m.black_ratio), 1)
    if not info.has_audio:
        parts["audio"] = 0
    elif (m.mean_volume_db or -91) < -50 or (m.silence_ratio or 0) > 0.9:
        parts["audio"] = 3
    else:
        parts["audio"] = round(20 * (1 - max(0.0, (m.silence_ratio or 0) - 0.3)), 1)
    return round(sum(parts.values()), 1), parts


def quality_gate(m: MediaMetrics, gates: dict) -> str | None:
    """Hard rejections that no score can compensate for."""
    if m.black_ratio > float(gates.get("max_black_ratio", 0.5)):
        return f"почти весь ролик чёрный ({m.black_ratio:.0%})"
    if m.frozen_ratio > float(gates.get("max_frozen_ratio", 0.8)):
        return f"картинка почти не меняется ({m.frozen_ratio:.0%} времени)"
    if m.blur is not None and m.blur > float(gates.get("max_blur", 15)):
        return f"сильно размыто (blur {m.blur:.1f})"
    return None
