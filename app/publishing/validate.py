"""Pre-flight checks of a Reel against the Instagram Graph API spec (developers.facebook.com, IG User Media,
«Reels specifications», checked 2026-10). Limits live in config so they can follow Meta's changes."""
from __future__ import annotations

import re
from pathlib import Path

from app.media.ffprobe import ProbeError, probe

HASHTAG = re.compile(r"(?<!\w)#\w+")
MENTION = re.compile(r"(?<![\w@])@[\w.]+")


def caption_problems(caption: str, cfg: dict) -> list[str]:
    caption = (caption or "").strip()
    out = []
    if not caption:
        out.append("описание пустое")
    if len(caption) > int(cfg.get("max_caption_chars", 2200)):
        out.append(f"описание длиннее {cfg.get('max_caption_chars', 2200)} символов ({len(caption)})")
    if len(HASHTAG.findall(caption)) > int(cfg.get("max_hashtags", 30)):
        out.append(f"хэштегов больше {cfg.get('max_hashtags', 30)}")
    if len(MENTION.findall(caption)) > int(cfg.get("max_mentions", 20)):
        out.append(f"упоминаний @ больше {cfg.get('max_mentions', 20)}")
    return out


def _moov_before_mdat(path: Path) -> bool:
    """MP4 top-level atoms: Instagram wants moov at the front (ffmpeg -movflags +faststart)."""
    with open(path, "rb") as fh:
        pos, size_total = 0, path.stat().st_size
        while pos < size_total:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                return False
            size, kind = int.from_bytes(head[:4], "big"), head[4:8]
            if size == 1:
                size = int.from_bytes(head[8:16], "big")
            if kind == b"moov":
                return True
            if kind == b"mdat" or size < 8:
                return False
            pos += size
    return False


def video_problems(path: str | Path | None, cfg: dict) -> list[str]:
    if not path or not Path(path).is_file():
        return ["файла готового ролика нет"]
    path = Path(path)
    try:
        info = probe(path)
    except ProbeError as exc:
        return [str(exc)]
    out = []
    if path.suffix.lower() not in (".mp4", ".mov"):
        out.append(f"контейнер {path.suffix}: нужен MP4 или MOV")
    if info.codec not in ("h264", "hevc"):
        out.append(f"видеокодек {info.codec}: нужен H.264 или HEVC")
    if not info.has_audio:
        out.append("нет звуковой дорожки")
    elif info.audio_codec != "aac":
        out.append(f"аудиокодек {info.audio_codec}: нужен AAC")
    else:
        if (info.sample_rate or 0) > 48000:
            out.append(f"частота звука {info.sample_rate} Гц: максимум 48 кГц")
        if (info.audio_channels or 0) > 2:
            out.append(f"{info.audio_channels} канала звука: максимум 2")
    if not 23 <= info.fps <= 60:
        out.append(f"{info.fps:g} кадров/с: нужно 23–60")
    if info.width > int(cfg.get("max_width", 1920)):
        out.append(f"ширина {info.width}: максимум {cfg.get('max_width', 1920)}")
    if info.height and abs(info.width / info.height - 9 / 16) > 0.02:
        out.append(f"пропорции {info.width}×{info.height}: рекомендуется 9:16")
    if info.duration < float(cfg.get("min_duration", 3)):
        out.append(f"длительность {info.duration:.1f} с: минимум {cfg.get('min_duration', 3)} с")
    if info.duration > float(cfg.get("max_duration", 900)):
        out.append(f"длительность {info.duration:.0f} с: максимум {cfg.get('max_duration', 900)} с")
    if info.filesize > float(cfg.get("max_file_mb", 300)) * 1024 * 1024:
        out.append(f"размер {info.filesize / 1048576:.0f} МБ: максимум {cfg.get('max_file_mb', 300)} МБ")
    if info.bitrate and info.bitrate > float(cfg.get("max_bitrate_mbps", 25)) * 1_000_000:
        out.append(f"битрейт {info.bitrate / 1e6:.1f} Мбит/с: максимум {cfg.get('max_bitrate_mbps', 25)}")
    if path.suffix.lower() == ".mp4" and not _moov_before_mdat(path):
        out.append("moov-атом не в начале файла (нужен faststart)")
    return out
