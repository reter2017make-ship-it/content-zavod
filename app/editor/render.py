"""Edit spec → one FFmpeg run from the original source (no intermediate re-encodes).

The browser preview (a frame or a short low-res clip) runs the very same filter graph with only a final
downscale added, so what the editor shows is what the export gives.

Layers, bottom to top: background → banner (if «under») → shadow → clip → border → banner (if «over»)
→ texts → subtitles. Drawn layers (gradient, masks, shadow, border, texts, subtitles) are Pillow PNGs:
the Homebrew FFmpeg has no drawtext/libass.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.editor.spec import CANVAS_H, CANVAS_W, clip_duration, total_duration
from app.media import subtitles
from app.media.ffmpeg import output_fps, run
from app.media.ffprobe import MediaInfo

PREVIEW_W, PREVIEW_H = 540, 960


@dataclass
class Asset:
    path: Path
    kind: str  # image | video | audio
    duration: float | None = None


VOICE = "__voice__"  # assets key of the synthesized voice-over


@dataclass
class _Graph:
    inputs: list[str] = field(default_factory=list)
    chains: list[str] = field(default_factory=list)
    n: int = 0

    def add_input(self, *args: str) -> int:
        self.inputs += list(args)
        self.n += 1
        return self.n - 1


def _rgb(hex_color: str) -> tuple[int, ...]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in range(0, len(h), 2))


def _ff_color(hex_color: str) -> str:
    return "0x" + hex_color.lstrip("#")[:6]


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


# ---------- drawn layers ----------
def gradient_png(c1: str, c2: str, angle: float, path: Path) -> Path:
    small = 64
    a = math.radians(angle)
    dx, dy = math.sin(a), -math.cos(a)
    img = Image.new("RGB", (small, small))
    p1, p2 = _rgb(c1)[:3], _rgb(c2)[:3]
    px = img.load()
    for y in range(small):
        for x in range(small):
            t = ((x / (small - 1) - .5) * dx + (y / (small - 1) - .5) * dy) / (abs(dx) * .5 + abs(dy) * .5 or 1) * .5 + .5
            t = max(0.0, min(1.0, t))
            px[x, y] = tuple(int(p1[i] + (p2[i] - p1[i]) * t) for i in range(3))
    img.resize((CANVAS_W, CANVAS_H), Image.BICUBIC).save(path)
    return path


def mask_png(w: int, h: int, shape: str, radius: int, path: Path) -> Path:
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    if shape == "circle":
        d.ellipse((0, 0, w - 1, h - 1), fill=255)
    else:
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=min(radius, w // 2, h // 2), fill=255)
    img.save(path)
    return path


def _outline(d: ImageDraw.ImageDraw, box, shape, radius, **kw):
    if shape == "circle":
        d.ellipse(box, **kw)
    elif shape == "rounded":
        d.rounded_rectangle(box, radius=radius, **kw)
    else:
        d.rectangle(box, **kw)


def shadow_png(box: tuple[int, int, int, int], shape: str, radius: int, path: Path) -> Path:
    x, y, w, h = box
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    off = max(8, w // 40)
    _outline(ImageDraw.Draw(img), (x + off, y + off * 2, x + w + off, y + h + off * 2), shape, radius,
             fill=(0, 0, 0, 150))
    img.filter(ImageFilter.GaussianBlur(max(10, w // 25))).save(path)
    return path


def border_png(box: tuple[int, int, int, int], shape: str, radius: int, width: int, color: str, path: Path) -> Path:
    x, y, w, h = box
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    half = width / 2
    _outline(ImageDraw.Draw(img), (x - half, y - half, x + w + half, y + h + half), shape, radius + int(half),
             outline=_rgb(color)[:3] + (255,), width=width)
    img.save(path)
    return path


def texts_png(texts: list[dict], path: Path) -> Path | None:
    # a plate is part of the template's look: drawn even before its words are typed
    texts = [t for t in texts if t["text"].strip() or t["box"]]
    if not texts:
        return None
    img = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for t in texts:
        font = subtitles._font(t["font"], t["size"])
        lines: list[str] = []
        for para in t["text"].split("\n"):
            words, cur = para.split(), ""
            for w in words:
                trial = (cur + " " + w).strip()
                if d.textlength(trial, font=font) <= t["w"] or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        line_h = int(t["size"] * 1.2)
        block_h = line_h * len(lines)
        if t["box"]:
            pad = t["size"] // 3
            widest = max(d.textlength(line, font=font) for line in lines)
            if not t["text"].strip():
                widest = t["w"]  # an empty plate spans its whole width
            x0 = {"left": t["x"], "right": t["x"] + t["w"] - widest}.get(t["align"], t["x"] + (t["w"] - widest) / 2)
            d.rounded_rectangle((x0 - pad, t["y"] - pad, x0 + widest + pad, t["y"] + block_h + pad // 2),
                                radius=pad, fill=_rgb(t["box_color"]) + ((255,) if len(_rgb(t["box_color"])) == 3 else ()))
        stroke = 0 if t["box"] else max(2, t["size"] // 14)
        for i, line in enumerate(lines):
            ax = {"left": (t["x"], "lm"), "right": (t["x"] + t["w"], "rm")}.get(t["align"], (t["x"] + t["w"] / 2, "mm"))
            d.text((ax[0], t["y"] + i * line_h + line_h / 2), line, font=font, anchor=ax[1],
                   fill=_rgb(t["color"])[:3] + (255,), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    img.save(path)
    return path


# ---------- geometry ----------
def video_geometry(spec: dict, src_w: int, src_h: int) -> dict:
    """Scaled clip size, crop and position inside the window («contain» / «cover» + zoom)."""
    v = spec["video"]
    bw, bh = v["w"], v["h"]
    k = (max if v["fit"] == "cover" else min)(bw / src_w, bh / src_h) * v["zoom"]
    sw, sh = _even(src_w * k), _even(src_h * k)
    cw, ch = min(sw, bw), min(sh, bh)
    cw, ch = _even(cw), _even(ch)
    return {"sw": sw, "sh": sh, "cw": cw, "ch": ch,
            "x": v["x"] + (bw - cw) // 2, "y": v["y"] + (bh - ch) // 2}


def shift_cues(cues: list[subtitles.Cue], spec: dict, clip_len: float) -> list[subtitles.Cue]:
    t = spec["time"]
    out = []
    for c in cues:
        s = (c.start - t["trim_start"]) / t["speed"]
        e = (c.end - t["trim_start"]) / t["speed"]
        if e <= 0 or s >= clip_len:
            continue
        out.append(subtitles.Cue(max(0.0, s) + t["start_delay"], min(e, clip_len) + t["start_delay"], c.text))
    return out


# ---------- the render ----------
def build_command(spec: dict, src: Path, info: MediaInfo, assets: dict[str, Asset], workdir: Path, out: Path,
                  pcfg: dict, cues: list[subtitles.Cue] | None = None, mode: str = "final",
                  at: float | None = None) -> list[str]:
    """mode: final (export quality) | preview (540×960 fast clip) | frame (one JPEG at `at` seconds)."""
    workdir.mkdir(parents=True, exist_ok=True)
    W, H = CANVAS_W, CANVAS_H
    fps = output_fps(info.fps, pcfg)
    t = spec["time"]
    used, clip_len = clip_duration(spec, info.duration)
    total = total_duration(spec, info.duration)
    delay, ad = t["start_delay"], t["appear_duration"]
    flags = str(pcfg.get("scale_flags", "lanczos"))

    g = _Graph()
    g.add_input("-ss", f"{t['trim_start']:.3f}", "-t", f"{used:.3f}", "-i", str(src))
    ch = g.chains

    # visibility of everything that belongs to the clip (clip, its shadow and border)
    fade_d = ad if t["appear"] == "fade" else 0.04
    slide = (f"if(lt(t,{delay + ad:.3f}),(1-(t-{delay:.3f})/{ad:.3f})*{int(H * 0.35)},0)"
             if t["appear"] == "slide_up" else "0")

    def png_layer(path: Path, label: str) -> str:
        i = g.add_input("-loop", "1", "-framerate", fps, "-i", str(path))
        ch.append(f"[{i}:v]format=rgba[{label}]")
        return label

    def clip_layer(path: Path, label: str) -> str:
        i = g.add_input("-loop", "1", "-framerate", fps, "-i", str(path))
        ch.append(f"[{i}:v]format=rgba,fade=t=in:st={delay:.3f}:d={fade_d:.3f}:alpha=1[{label}]")
        return label

    # background
    bg = dict(spec["background"])
    if bg["type"] in ("image", "video") and bg.get("asset") not in assets:
        bg["type"] = "blur"  # the picture was deleted from «Фоны»: fall back to the blurred clip
    speed_pts = f"setpts=(PTS-STARTPTS)/{t['speed']:.4f}"
    if bg["type"] == "blur":
        ch.append("[0:v]split=2[srcbg][srcfg]")
        bw_, bh_ = _even(W / 4), _even(H / 4)
        pad = f",tpad=start_duration={delay:.3f}:start_mode=clone" if delay else ""
        ch.append(f"[srcbg]{speed_pts},scale={bw_}:{bh_}:force_original_aspect_ratio=increase,crop={bw_}:{bh_},"
                  f"boxblur={bg['blur']}:2,eq=brightness={-bg['darken']:.3f},scale={W}:{H},setsar=1{pad}[bg]")
        fg_in = "srcfg"
    else:
        fg_in = "0:v"
        if bg["type"] == "color":
            i = g.add_input("-f", "lavfi", "-i", f"color=c={_ff_color(bg['color'])}:s={W}x{H}:r={fps}")
            ch.append(f"[{i}:v]setsar=1[bg]")
        elif bg["type"] == "gradient":
            i = g.add_input("-loop", "1", "-framerate", fps, "-i",
                            str(gradient_png(bg["color"], bg["color2"], bg["angle"], workdir / "gradient.png")))
            ch.append(f"[{i}:v]setsar=1[bg]")
        else:
            a = assets[bg["asset"]]
            i = (g.add_input("-stream_loop", "-1", "-i", str(a.path)) if a.kind == "video"
                 else g.add_input("-loop", "1", "-framerate", fps, "-i", str(a.path)))
            ch.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase:flags={flags},crop={W}:{H},"
                      f"fps={fps},setsar=1[bg]")
    base = "bg"

    def overlay(top: str, x: str = "0", y: str = "0", extra: str = "") -> None:
        nonlocal base
        out_label = f"l{len(ch)}"
        ch.append(f"[{base}][{top}]overlay=x='{x}':y='{y}':eval=frame:eof_action=pass{extra}[{out_label}]")
        base = out_label

    # banner
    ban = spec["banner"]
    ban_label = None
    if ban.get("asset") and ban["asset"] in assets:
        a = assets[ban["asset"]]
        i = (g.add_input("-stream_loop", "-1", "-i", str(a.path)) if a.kind == "video"
             else g.add_input("-loop", "1", "-framerate", fps, "-i", str(a.path)))
        key = ""
        if ban["chroma"]["enabled"]:
            c = ban["chroma"]
            key = f",colorkey={_ff_color(c['color'])}:{c['similarity']:.3f}:{c['blend']:.3f}"
        ch.append(f"[{i}:v]scale={_even(W * ban['scale'])}:-2:flags={flags},format=rgba{key},setsar=1[banner]")
        ban_label = "banner"
    if ban_label and ban["layer"] == "under":
        overlay(ban_label, str(ban["x"]), str(ban["y"]))

    # the clip in its window
    v = spec["video"]
    geo = video_geometry(spec, info.width, info.height)
    box = (geo["x"], geo["y"], geo["cw"], geo["ch"])
    if v["shadow"]:
        overlay(clip_layer(shadow_png(box, v["shape"], v["radius"], workdir / "shadow.png"), "shadow"), "0", slide)
    chain = (f"[{fg_in}]{speed_pts},scale={geo['sw']}:{geo['sh']}:flags={flags},"
             f"crop={geo['cw']}:{geo['ch']},setsar=1,format=rgba")
    if v["shape"] != "rect":
        mi = g.add_input("-loop", "1", "-i", str(mask_png(geo["cw"], geo["ch"], v["shape"], v["radius"],
                                                           workdir / "mask.png")))
        ch.append(f"[{mi}:v]format=gray[mask]")
        ch.append(chain + "[clipraw]")
        chain = "[clipraw][mask]alphamerge"
    if delay:
        chain += f",tpad=start_duration={delay:.3f}:color=black@0"
    if t["appear"] == "fade":
        chain += f",fade=t=in:st={delay:.3f}:d={ad:.3f}:alpha=1"
    ch.append(chain + "[clip]")
    overlay("clip", str(geo["x"]), f"{geo['y']}+{slide}" if slide != "0" else str(geo["y"]))
    if v["border_width"]:
        overlay(clip_layer(border_png(box, v["shape"], v["radius"], v["border_width"], v["border_color"],
                                      workdir / "border.png"), "border"), "0", slide)
    if ban_label and ban["layer"] == "over":
        overlay(ban_label, str(ban["x"]), str(ban["y"]))

    # texts and subtitles
    tp = texts_png(spec["texts"], workdir / "texts.png")
    if tp:
        overlay(png_layer(tp, "texts"))
    if spec["subtitles"]["enabled"] and cues:
        from app.editor import subs
        lst = subs.track(cues, spec, clip_len, total, workdir / "subs")
        if lst:
            i = g.add_input("-f", "concat", "-safe", "0", "-i", str(lst))
            ch.append(f"[{i}:v]fps={fps},format=rgba,setsar=1[subs]")
            overlay("subs")

    final = f"[{base}]fps={fps},format=yuv420p"
    if mode != "final":
        final += f",scale={PREVIEW_W}:{PREVIEW_H}:flags=bilinear"
    ch.append(final + "[vout]")

    # audio
    a_spec = spec["audio"]
    has_audio = mode != "frame"
    if has_audio:
        if info.has_audio:
            achain = f"[0:a]asetpts=PTS-STARTPTS,atempo={t['speed']:.4f},volume={a_spec['volume']:.3f}"
        else:
            si = g.add_input("-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000")
            achain = f"[{si}:a]anull"
        if delay:
            ms = int(delay * 1000)
            achain += f",adelay={ms}|{ms}"
        vo, voice = spec["voice"], assets.get(VOICE)
        if voice and voice.duration:
            # the original is lowered while the voice speaks
            vs, ve = delay, delay + voice.duration
            achain += f",volume='if(between(t,{vs:.3f},{ve:.3f}),{vo['duck']:.3f},1)':eval=frame"
        achain += ",aformat=sample_rates=48000:channel_layouts=stereo,apad"
        ch.append(achain + "[a0]")
        mix = ["a0"]
        if voice and voice.duration:
            vi = g.add_input("-i", str(voice.path))
            ms = int(delay * 1000)
            ch.append(f"[{vi}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vo['volume']:.3f}"
                      + (f",adelay={ms}|{ms}" if ms else "") + "[voice]")
            mix.append("voice")
        alabel = "a0"
        if a_spec.get("music") and a_spec["music"] in assets:
            mi = g.add_input("-stream_loop", "-1", "-i", str(assets[a_spec["music"]].path))
            ch.append(f"[{mi}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                      f"volume={a_spec['music_volume']:.3f}[music]")
            mix.append("music")
        if len(mix) > 1:
            ch.append("".join(f"[{m}]" for m in mix)
                      + f"amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0[amix]")
            alabel = "amix"
        if a_spec["normalize"] and mode == "final":
            ch.append(f"[{alabel}]loudnorm=I={float(pcfg.get('loudness_target', -14)):.1f}:TP=-1.5:LRA=11,"
                      f"aresample=48000[aout]")
        else:
            ch.append(f"[{alabel}]anull[aout]")

    (workdir / "graph.txt").write_text(";\n".join(ch), encoding="utf-8")
    cmd = [*g.inputs, "-filter_complex_script", str(workdir / "graph.txt"), "-map", "[vout]"]
    if mode == "frame":
        return cmd + ["-ss", f"{max(0.0, min(at or 0.0, total - 0.05)):.3f}", "-frames:v", "1", "-q:v", "3", str(out)]
    cmd += ["-map", "[aout]", "-t", f"{total:.3f}"]
    if mode == "preview":
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    else:
        cmd += ["-c:v", "libx264", "-preset", str(pcfg.get("preset", "slow")), "-crf", str(pcfg.get("crf", 17)),
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", fps,
                "-c:a", "aac", "-b:a", str(pcfg.get("audio_bitrate", "256k")), "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
                str(out)]
    return cmd


def render(spec: dict, src: Path, info: MediaInfo, assets: dict[str, Asset], workdir: Path, out: Path,
           pcfg: dict, cues=None, mode: str = "final", at: float | None = None, timeout: int = 3600) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(build_command(spec, src, info, assets, workdir, out, pcfg, cues, mode, at), timeout=timeout)
    return out
