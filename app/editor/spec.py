"""Edit spec: everything the editor can change, as plain JSON. One spec → one deterministic FFmpeg render.

Coordinates are in output pixels (1080×1920 canvas, origin top-left). Times are in seconds of the result,
except trim_start/trim_end, which are in the source video.
"""
from __future__ import annotations

import copy
import re

CANVAS_W, CANVAS_H = 1080, 1920
CHROMA_SIMILARITY, CHROMA_BLEND = 0.11, 0.25  # owner's defaults for every banner and template

DEFAULT_SPEC: dict = {
    "version": 1,
    "background": {
        "type": "blur",          # blur | color | gradient | image | video
        "color": "#101418", "color2": "#4f46e5", "angle": 160,
        "asset": None,           # image/video asset id for type image/video
        "blur": 20, "darken": 0.15,
    },
    "video": {
        "x": 0, "y": 0, "w": CANVAS_W, "h": CANVAS_H,  # the window the clip is placed in
        "fit": "contain",        # contain (whole frame visible) | cover (fill the window, crop edges)
        "zoom": 1.0,             # >1 zooms into the centre of the clip
        "shape": "rect",         # rect | rounded | circle
        "radius": 48,
        "border_width": 0, "border_color": "#ffffff",
        "shadow": False,
    },
    "time": {
        "trim_start": 0.0, "trim_end": None,
        "speed": 1.0,            # 0.5–2
        "start_delay": 0.0,      # seconds of background/banner before the clip starts
        "appear": "none",        # none | fade | slide_up
        "appear_duration": 0.5,
    },
    "banner": {
        "asset": None,           # image or video
        "x": 0, "y": 0, "scale": 1.0,   # banner width = canvas width × scale
        "layer": "over",         # over the clip (a frame with a window) | under it
        "chroma": {"enabled": False, "color": "#00ff00", "similarity": CHROMA_SIMILARITY, "blend": CHROMA_BLEND},
    },
    "texts": [],                 # [{text, x, y, w, size, color, box, box_color, font}]
    "audio": {"volume": 1.0, "music": None, "music_volume": 0.25, "normalize": True},
    "subtitles": {
        "enabled": True, "preset": "classic",
        "font": "heavy",         # regular | heavy | impact | rounded
        "size": 64, "color": "#ffffff", "stroke": 0.10, "stroke_color": "#000000",
        "box": False, "box_color": "#000000a0",
        "karaoke": False, "highlight": "#ffd600",  # karaoke: the word being said gets this colour
        "uppercase": False,
        "y": 1300,               # vertical centre, px
        "max_chars": 24,
    },
    # Voice-over (local Russian TTS): starts with the clip; the original sound is lowered under it.
    "voice": {"enabled": False, "text": "", "voice": "irina", "speed": 1.0, "volume": 1.0, "duck": 0.25},
}

# Old editor subtitles (style + position) → the new fields.
_OLD_SUB_Y = {"top": 420, "center": 1190, "bottom": 1300}
_OLD_SUB_STYLE = {
    "default": {"font": "regular", "stroke": 0.08}, "bold": {"font": "heavy", "stroke": 0.11},
    "minimal": {"font": "regular", "stroke": 0.04}, "gaming": {"font": "heavy", "color": "#7cff4f", "stroke": 0.10},
    "recipe": {"font": "regular", "stroke": 0.0, "box": True}, "sports": {"font": "heavy", "color": "#ffd600", "stroke": 0.10},
}

TEXT_DEFAULT = {"text": "", "x": 60, "y": 160, "w": CANVAS_W - 120, "size": 72, "color": "#ffffff",
                "box": False, "box_color": "#000000b0", "font": "heavy", "align": "center"}

HEX = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _color(v, default):
    return v if isinstance(v, str) and HEX.match(v) else default


def normalize(spec: dict | None, duration: float | None = None) -> dict:
    """Fills defaults and clamps every value, so a spec from the browser can be rendered safely."""
    spec = dict(spec or {})
    old = spec.get("subtitles") or {}
    if isinstance(old, dict) and "style" in old and "preset" not in old:
        new = {"enabled": old.get("enabled", True), "preset": "custom", "y": _OLD_SUB_Y.get(old.get("position"), 1300),
               "size": old.get("font_size", 56), **_OLD_SUB_STYLE.get(old.get("style"), {})}
        spec["subtitles"] = new
    s = _merge(DEFAULT_SPEC, spec)
    bg, v, t, b, a, sub = s["background"], s["video"], s["time"], s["banner"], s["audio"], s["subtitles"]
    bg["type"] = bg["type"] if bg["type"] in ("blur", "color", "gradient", "image", "video") else "blur"
    bg["color"], bg["color2"] = _color(bg["color"], "#101418"), _color(bg["color2"], "#4f46e5")
    bg["angle"] = _num(bg["angle"], 0, 360, 160)
    bg["blur"], bg["darken"] = int(_num(bg["blur"], 0, 60, 20)), _num(bg["darken"], 0, 0.9, 0.15)
    if bg["type"] in ("image", "video") and not bg.get("asset"):
        bg["type"] = "blur"

    v["w"] = int(_num(v["w"], 40, CANVAS_W * 3, CANVAS_W)) // 2 * 2
    v["h"] = int(_num(v["h"], 40, CANVAS_H * 3, CANVAS_H)) // 2 * 2
    v["x"] = int(_num(v["x"], -CANVAS_W * 2, CANVAS_W * 2, 0))
    v["y"] = int(_num(v["y"], -CANVAS_H * 2, CANVAS_H * 2, 0))
    v["fit"] = v["fit"] if v["fit"] in ("contain", "cover") else "contain"
    v["zoom"] = _num(v["zoom"], 1.0, 4.0, 1.0)
    v["shape"] = v["shape"] if v["shape"] in ("rect", "rounded", "circle") else "rect"
    v["radius"] = int(_num(v["radius"], 0, 600, 48))
    v["border_width"] = int(_num(v["border_width"], 0, 60, 0))
    v["border_color"] = _color(v["border_color"], "#ffffff")
    v["shadow"] = bool(v["shadow"])

    t["trim_start"] = _num(t["trim_start"], 0, duration or 1e6, 0.0)
    end = t.get("trim_end")
    t["trim_end"] = None if end in (None, "", 0) else _num(end, t["trim_start"] + 0.5, duration or 1e6, None)
    t["speed"] = _num(t["speed"], 0.5, 2.0, 1.0)
    t["start_delay"] = _num(t["start_delay"], 0, 10, 0.0)
    t["appear"] = t["appear"] if t["appear"] in ("none", "fade", "slide_up") else "none"
    t["appear_duration"] = _num(t["appear_duration"], 0.1, 3, 0.5)

    b["x"], b["y"] = int(_num(b["x"], -CANVAS_W, CANVAS_W * 2, 0)), int(_num(b["y"], -CANVAS_H, CANVAS_H * 2, 0))
    b["scale"] = _num(b["scale"], 0.05, 3, 1.0)
    b["layer"] = b["layer"] if b["layer"] in ("over", "under") else "over"
    c = b["chroma"]
    c["enabled"], c["color"] = bool(c["enabled"]), _color(c["color"], "#00ff00")
    c["similarity"], c["blend"] = _num(c["similarity"], 0.01, 1, CHROMA_SIMILARITY), _num(c["blend"], 0, 1, CHROMA_BLEND)

    s["texts"] = [_merge(TEXT_DEFAULT, x) for x in (s.get("texts") or [])[:6] if isinstance(x, dict)]
    for x in s["texts"]:
        x["text"] = str(x["text"])[:300]
        x["size"] = int(_num(x["size"], 16, 220, 72))
        x["x"], x["y"] = int(_num(x["x"], -500, CANVAS_W, 60)), int(_num(x["y"], -500, CANVAS_H, 160))
        x["w"] = int(_num(x["w"], 100, CANVAS_W * 2, CANVAS_W - 120))
        x["color"], x["box_color"] = _color(x["color"], "#ffffff"), _color(x["box_color"], "#000000b0")
        x["box"] = bool(x["box"])
        x["font"] = x["font"] if x["font"] in ("regular", "heavy") else "heavy"
        x["align"] = x["align"] if x["align"] in ("left", "center", "right") else "center"

    a["volume"] = _num(a["volume"], 0, 3, 1.0)
    a["music_volume"] = _num(a["music_volume"], 0, 3, 0.25)
    a["normalize"] = bool(a["normalize"])
    sub["enabled"] = bool(sub["enabled"])
    sub["preset"] = str(sub.get("preset") or "custom")[:32]
    sub["font"] = sub["font"] if sub["font"] in ("regular", "heavy", "impact", "rounded") else "heavy"
    sub["size"] = int(_num(sub["size"], 24, 160, 64))
    sub["color"], sub["highlight"] = _color(sub["color"], "#ffffff"), _color(sub["highlight"], "#ffd600")
    sub["stroke"] = _num(sub["stroke"], 0, 0.25, 0.10)
    sub["stroke_color"], sub["box_color"] = _color(sub["stroke_color"], "#000000"), _color(sub["box_color"], "#000000a0")
    sub["box"], sub["karaoke"], sub["uppercase"] = bool(sub["box"]), bool(sub["karaoke"]), bool(sub["uppercase"])
    sub["y"] = int(_num(sub["y"], 100, CANVAS_H - 100, 1300))
    sub["max_chars"] = int(_num(sub["max_chars"], 8, 40, 24))
    for k in ("style", "position", "font_size"):
        sub.pop(k, None)

    vo = s["voice"]
    vo["enabled"] = bool(vo["enabled"])
    vo["text"] = str(vo["text"] or "")[:3000]
    vo["voice"] = str(vo["voice"] or "irina")[:32]
    vo["speed"] = _num(vo["speed"], 0.5, 2.0, 1.0)
    vo["volume"] = _num(vo["volume"], 0, 3, 1.0)
    vo["duck"] = _num(vo["duck"], 0, 1, 0.25)
    return s


def as_template(spec: dict) -> dict:
    """A template keeps the look, not the clip-specific cut."""
    s = copy.deepcopy(spec)
    s["time"]["trim_start"], s["time"]["trim_end"] = 0.0, None
    if "voice" in s:
        s["voice"]["text"] = ""  # the words belong to one clip; the voice and its settings stay
    return s


def clip_duration(spec: dict, source_duration: float) -> tuple[float, float]:
    """(source seconds used, seconds of the clip in the result)."""
    t = spec["time"]
    end = min(t["trim_end"] or source_duration, source_duration)
    used = max(0.1, end - t["trim_start"])
    return used, used / t["speed"]


def total_duration(spec: dict, source_duration: float) -> float:
    return spec["time"]["start_delay"] + clip_duration(spec, source_duration)[1]
