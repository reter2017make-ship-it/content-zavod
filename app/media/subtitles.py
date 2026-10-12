"""SRT parsing and subtitle burn-in.

The Homebrew FFmpeg build has no libass/drawtext, so subtitles are drawn with Pillow
into transparent PNGs (one per cue) and overlaid by FFmpeg with timed `enable`.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = {
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/arialbd.ttf",  # Windows
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",  # Docker / Linux
    ],
    "heavy": [
        "/System/Library/Fonts/Supplemental/Arial Black.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/ariblk.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
}

PRESETS: dict[str, dict] = {
    "default": {"font": "regular", "scale": 1.0, "fill": "#FFFFFF", "stroke": "#000000", "stroke_ratio": 0.08, "box": None},
    "bold":    {"font": "heavy", "scale": 1.15, "fill": "#FFFFFF", "stroke": "#000000", "stroke_ratio": 0.11, "box": None},
    "minimal": {"font": "regular", "scale": 0.85, "fill": "#FFFFFF", "stroke": "#000000", "stroke_ratio": 0.04, "box": None},
    "gaming":  {"font": "heavy", "scale": 1.1, "fill": "#7CFF4F", "stroke": "#000000", "stroke_ratio": 0.10, "box": None},
    "recipe":  {"font": "regular", "scale": 1.0, "fill": "#FFFFFF", "stroke": None, "stroke_ratio": 0.0, "box": (0, 0, 0, 170)},
    "sports":  {"font": "heavy", "scale": 1.1, "fill": "#FFD600", "stroke": "#000000", "stroke_ratio": 0.10, "box": None},
}


@dataclass
class Cue:
    start: float
    end: float
    text: str


_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def _seconds(stamp: str) -> float:
    m = _TIME.search(stamp)
    if not m:
        raise ValueError(f"bad SRT time: {stamp!r}")
    h, mnt, s, ms = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip().lstrip("﻿")):
        lines = [l for l in block.splitlines() if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        start_s, end_s = lines[idx].split("-->", 1)
        body = " ".join(re.sub(r"<[^>]+>|\{[^}]+\}", "", l).strip() for l in lines[idx + 1:]).strip()
        if body:
            cues.append(Cue(_seconds(start_s), _seconds(end_s), body))
    return sorted(cues, key=lambda c: c.start)


def load_srt(path: Path) -> list[Cue]:
    return parse_srt(path.read_text(encoding="utf-8-sig", errors="replace"))


def write_srt(cues: list[Cue], path: Path) -> None:
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    blocks = [f"{i}\n{fmt(c.start)} --> {fmt(c.end)}\n{c.text}" for i, c in enumerate(cues, 1)]
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def split_for_screen(cues: list[Cue], max_chars: int, max_lines: int) -> list[tuple[Cue, list[str]]]:
    """Wrap each cue; cues longer than max_lines are split into consecutive parts by time share."""
    out: list[tuple[Cue, list[str]]] = []
    for cue in cues:
        lines = textwrap.wrap(cue.text, width=max_chars, break_long_words=True) or [cue.text]
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        total = sum(len(" ".join(c)) for c in chunks) or 1
        t = cue.start
        for chunk in chunks:
            share = (cue.end - cue.start) * len(" ".join(chunk)) / total
            out.append((Cue(t, t + share, " ".join(chunk)), chunk))
            t += share
    return out


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES.get(kind, []) + FONT_CANDIDATES["regular"]:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size)


def render_cue_png(lines: list[str], width: int, font_size: int, style: str, path: Path) -> tuple[int, int]:
    preset = PRESETS.get(style, PRESETS["default"])
    size = int(font_size * preset["scale"])
    font = _font(preset["font"], size)
    stroke = int(size * preset["stroke_ratio"]) if preset["stroke"] else 0
    line_h = int(size * 1.25)
    pad = max(stroke * 2, size // 3)
    height = line_h * len(lines) + pad * 2
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if preset["box"]:
        widest = max(draw.textlength(l, font=font) for l in lines)
        x0 = (width - widest) / 2 - pad
        draw.rounded_rectangle((x0, 0, width - x0, height), radius=pad, fill=preset["box"])
    for i, line in enumerate(lines):
        draw.text(
            (width / 2, pad + i * line_h + line_h / 2), line, font=font, anchor="mm",
            fill=preset["fill"], stroke_width=stroke, stroke_fill=preset["stroke"],
        )
    img.save(path)
    return img.size


def y_position(position: str, frame_h: int, img_h: int) -> int:
    if position == "top":
        return int(frame_h * 0.15)
    if position == "bottom":
        # Keep clear of the platform UI at the bottom of the screen.
        return int(frame_h * 0.72) - img_h
    return int(frame_h * 0.62) - img_h // 2


def build_overlays(cues: list[Cue], workdir: Path, frame_w: int, frame_h: int, cfg: dict) -> list[tuple[Path, float, float, int]]:
    """Renders PNGs for every cue. Returns (png, start, end, y) for each."""
    workdir.mkdir(parents=True, exist_ok=True)
    margin = int(frame_w * 0.06)
    result = []
    parts = split_for_screen(cues, int(cfg.get("max_chars_per_line", 32)), int(cfg.get("max_lines", 2)))
    for i, (cue, lines) in enumerate(parts):
        png = workdir / f"sub_{i:04d}.png"
        _, h = render_cue_png(lines, frame_w - 2 * margin, int(cfg.get("font_size", 52)), cfg.get("style", "default"), png)
        result.append((png, cue.start, cue.end, y_position(cfg.get("position", "center"), frame_h, h)))
    return result


def overlay_graph(overlays: list[tuple[Path, float, float, int]], frame_w: int, first_input: int, src_label: str) -> tuple[str, str]:
    """Chain of timed overlays starting from `src_label`. Returns (graph, output label)."""
    graph, label = [], src_label
    for n, (png, start, end, y) in enumerate(overlays):
        out = f"sub{n}"
        x = "(W-w)/2"
        graph.append(f"[{label}][{first_input + n}:v]overlay={x}:{y}:enable='between(t,{start:.3f},{end:.3f})'[{out}]")
        label = out
    return ";".join(graph), label
