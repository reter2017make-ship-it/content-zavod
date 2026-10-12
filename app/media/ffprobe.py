from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    bitrate: int | None
    filesize: int
    has_audio: bool
    audio_codec: str | None = None
    audio_channels: int | None = None
    sample_rate: int | None = None
    has_subtitle_stream: bool = False

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise ProbeError("ffprobe не найден в PATH")
    return path


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return round(float(num) / float(den), 3) if float(den) else 0.0
    return float(rate)


def probe(path: str | Path, timeout: int = 60) -> MediaInfo:
    path = Path(path)
    cmd = [ffprobe_path(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timeout: {path.name}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe не смог прочитать {path.name}: {proc.stderr.strip()[:300]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ProbeError(f"в файле нет видеопотока: {path.name}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = abs(int(side["rotation"]))
    if rotation in (90, 270):
        width, height = height, width
    bitrate = fmt.get("bit_rate")
    return MediaInfo(
        duration=round(duration, 3),
        width=width,
        height=height,
        fps=_fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")),
        codec=video.get("codec_name", ""),
        bitrate=int(bitrate) if bitrate else None,
        filesize=path.stat().st_size,
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        has_subtitle_stream=any(s.get("codec_type") == "subtitle" for s in streams),
    )
