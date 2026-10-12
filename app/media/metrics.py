"""Technical quality measurements from one FFmpeg pass over frames sampled at 1 fps."""
from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.media.ffmpeg import FFmpegError, ffmpeg_path


@dataclass
class MediaMetrics:
    sampled_frames: int
    black_ratio: float          # share of duration that is (almost) black
    frozen_ratio: float         # share of duration with no visual change
    blur: float | None          # blurdetect mean: higher = blurrier
    mean_volume_db: float | None
    silence_ratio: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _sum(pattern: str, text: str) -> float:
    return sum(float(x) for x in re.findall(pattern, text))


def measure(path: Path, duration: float, has_audio: bool, sample_fps: float = 1.0) -> MediaMetrics:
    vf = (f"fps={sample_fps},scale=640:-2,blurdetect=block_width=32:block_height=32,"
          "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2")
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "info", "-i", str(path), "-vf", vf]
    if has_audio:
        cmd += ["-af", "volumedetect,silencedetect=n=-45dB:d=0.5"]
    else:
        cmd += ["-an"]
    cmd += ["-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise FFmpegError(f"не удалось измерить качество: {proc.stderr.strip()[-300:]}")
    log = proc.stderr
    duration = max(duration, 0.001)

    black = _sum(r"black_duration:\s*([\d.]+)", log)
    frozen = _sum(r"freeze_duration:\s*([\d.]+)", log)
    # freezedetect only reports a freeze once it ends; a clip frozen to the very end has no freeze_end.
    starts = re.findall(r"freeze_start:\s*([\d.]+)", log)
    ends = re.findall(r"freeze_end:\s*([\d.]+)", log)
    if len(starts) > len(ends):
        frozen += duration - float(starts[-1])

    blur_m = re.search(r"blur mean:\s*([\d.]+)", log)
    vol_m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", log)
    silence = _sum(r"silence_duration:\s*([\d.]+)", log)
    s_starts = re.findall(r"silence_start:\s*(-?[\d.]+)", log)
    s_ends = re.findall(r"silence_end:\s*([\d.]+)", log)
    if len(s_starts) > len(s_ends):
        silence += duration - max(0.0, float(s_starts[-1]))

    frames = re.findall(r"frame=\s*(\d+)", log)
    mean_volume = None
    if vol_m:
        mean_volume = -91.0 if vol_m.group(1) == "-inf" else float(vol_m.group(1))
    return MediaMetrics(
        sampled_frames=int(frames[-1]) if frames else 0,
        black_ratio=round(min(black / duration, 1.0), 3),
        frozen_ratio=round(min(frozen / duration, 1.0), 3),
        blur=round(float(blur_m.group(1)), 3) if blur_m else None,
        mean_volume_db=mean_volume,
        silence_ratio=round(min(silence / duration, 1.0), 3) if has_audio else None,
    )
