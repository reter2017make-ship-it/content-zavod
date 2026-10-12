"""Editor subtitles: style presets, colours, position, karaoke (word highlight).

Word times are not recognised separately: inside a phrase each word gets a share of the phrase time by
its length (close enough for short Reels phrases). The same layout is drawn in the browser
(static/compose.js), so the live preview and the export match.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.editor.spec import CANVAS_H, CANVAS_W
from app.media.subtitles import Cue

FONTS = {
    # macOS fonts first, then Windows; in Docker (Linux) the open look-alikes from fonts-liberation2 / fonts-dejavu-core
    "regular": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"],
    "heavy": ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "C:/Windows/Fonts/ariblk.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "impact": ["/System/Library/Fonts/Supplemental/Impact.ttf", "/System/Library/Fonts/Supplemental/Arial Black.ttf",
               "C:/Windows/Fonts/impact.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"],
    "rounded": ["/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"],
}
MAX_LINES = 2


def font(kind: str, size: int):
    for p in FONTS.get(kind, []) + FONTS["regular"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def _rgba(hex_color: str) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    v = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
    return tuple(v + [255] * (4 - len(v)))  # type: ignore[return-value]


@dataclass
class Part:
    start: float
    end: float
    lines: list[list[str]]
    word_times: list[tuple[float, float]]  # one per word, in reading order


def wrap(words: list[str], max_chars: int) -> list[list[str]]:
    """Greedy wrap by characters (long words are cut), like Python's textwrap."""
    lines: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        while len(w) > max_chars:
            if cur:
                lines.append(cur)
                cur = []
            lines.append([w[:max_chars]])
            w = w[max_chars:]
        if not w:
            continue
        if not cur or len(" ".join(cur + [w])) <= max_chars:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    if cur:
        lines.append(cur)
    return lines


def parts(cues: list[Cue], spec: dict, clip_len: float) -> list[Part]:
    t, sub = spec["time"], spec["subtitles"]
    out: list[Part] = []
    for c in cues:
        s = (c.start - t["trim_start"]) / t["speed"]
        e = (c.end - t["trim_start"]) / t["speed"]
        if e <= 0 or s >= clip_len:
            continue
        start, end = max(0.0, s) + t["start_delay"], min(e, clip_len) + t["start_delay"]
        lines = wrap(c.text.split(), int(sub["max_chars"]))
        if not lines:
            continue
        chunks = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)]
        weight = lambda ch: sum(len(w) + 1 for line in ch for w in line)  # noqa: E731
        total = sum(weight(ch) for ch in chunks) or 1
        at = start
        for ch in chunks:
            dur = (end - start) * weight(ch) / total
            words = [w for line in ch for w in line]
            wt, wat, wtotal = [], at, sum(len(w) + 1 for w in words) or 1
            for w in words:
                wd = dur * (len(w) + 1) / wtotal
                wt.append((wat, wat + wd))
                wat += wd
            out.append(Part(at, at + dur, ch, wt))
            at += dur
    return out


def layout(sub: dict) -> dict:
    size = int(sub["size"])
    stroke = int(size * sub["stroke"]) if sub["stroke"] > 0 else 0
    line_h = int(size * 1.25)
    return {"size": size, "stroke": stroke, "line_h": line_h, "pad": max(stroke * 2, size // 3)}


def render_png(lines: list[list[str]], sub: dict, highlight: int | None, path: Path) -> int:
    """One subtitle state as a full-width PNG; returns its height. `highlight` = word index (karaoke)."""
    L = layout(sub)
    f = font(sub["font"], L["size"])
    up = (lambda s: s.upper()) if sub["uppercase"] else (lambda s: s)
    height = L["line_h"] * len(lines) + L["pad"] * 2
    img = Image.new("RGBA", (CANVAS_W, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    texts = [up(" ".join(line)) for line in lines]
    widths = [d.textlength(t, font=f) for t in texts]
    if sub["box"]:
        widest = max(widths)
        x0 = (CANVAS_W - widest) / 2 - L["pad"]
        d.rounded_rectangle((x0, 0, CANVAS_W - x0, height), radius=L["pad"], fill=_rgba(sub["box_color"]))
    n = 0
    for i, line in enumerate(lines):
        x = (CANVAS_W - widths[i]) / 2
        y = L["pad"] + i * L["line_h"] + L["line_h"] / 2
        for j, w in enumerate(line):
            prefix = up(" ".join(line[:j]) + (" " if j else ""))
            color = sub["highlight"] if highlight == n else sub["color"]
            d.text((x + d.textlength(prefix, font=f), y), up(w), font=f, anchor="lm", fill=_rgba(color),
                   stroke_width=L["stroke"], stroke_fill=_rgba(sub["stroke_color"]))
            n += 1
    img.save(path)
    return height


def overlays(cues: list[Cue], spec: dict, clip_len: float, workdir: Path) -> list[tuple[Path, float, float, int]]:
    """(png, start, end, y) for every subtitle state: one per phrase part, or one per word with karaoke."""
    sub = spec["subtitles"]
    workdir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, p in enumerate(parts(cues, spec, clip_len)):
        states = [(None, p.start, p.end)] if not sub["karaoke"] else \
            [(j, s, e) for j, (s, e) in enumerate(p.word_times)]
        for hl, s, e in states:
            png = workdir / f"sub_{i:04d}_{0 if hl is None else hl + 1:03d}.png"
            h = render_png(p.lines, sub, hl, png)
            y = max(0, min(CANVAS_H - h, int(sub["y"]) - h // 2))
            out.append((png, s, e, y))
    return out


def track(cues: list[Cue], spec: dict, clip_len: float, total: float, workdir: Path) -> Path | None:
    """All subtitle states as ONE input for FFmpeg: a concat list of full-frame PNGs with durations
    (gaps are a transparent frame). One input instead of one per word keeps karaoke fast."""
    states = overlays(cues, spec, clip_len, workdir)
    if not states:
        return None
    blank = workdir / "blank.png"
    Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0)).save(blank)
    lines, at = [], 0.0

    def add(path: Path, dur: float) -> None:
        if dur > 0.001:
            lines.extend([f"file '{path}'", f"duration {dur:.3f}"])

    for png, s, e, y in sorted(states, key=lambda x: x[1]):
        s, e = max(s, at), min(e, total)
        if e <= s:
            continue
        add(blank, s - at)
        full = png.with_name(png.stem + "_full.png")
        band = Image.open(png)
        frame = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        frame.paste(band, (0, y))
        frame.save(full)
        add(full, e - s)
        at = e
    add(blank, max(0.05, total - at))
    lines.append(f"file '{blank}'")  # concat needs the last file twice for its duration to count
    out = workdir / "subs.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
