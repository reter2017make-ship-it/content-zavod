"""Files for the editor (banners, backgrounds, music) and the chroma-key helpers."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from app.database.models import EditAsset
from app.media.ffmpeg import ffmpeg_path
from app.media.ffprobe import ProbeError, probe

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
MAX_BYTES = 500 * 1024 * 1024


class AssetError(ValueError):
    pass


def assets_dir(settings) -> Path:
    return settings.data("assets")


def save_upload(settings, db, filename: str, data: bytes) -> EditAsset:
    ext = Path(filename).suffix.lower()
    if len(data) > MAX_BYTES:
        raise AssetError("файл больше 500 МБ")
    kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else None
    if kind is None:
        raise AssetError("поддерживаются картинки (png, jpg, webp), видео (mp4, mov, webm) и звук (mp3, m4a, wav)")
    aid = hashlib.sha256(data).hexdigest()[:24]
    folder = assets_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    with db.session() as s:
        existing = s.get(EditAsset, aid)
        if existing and Path(existing.path).is_file():
            return existing
    # images are stored as PNG (keeps transparency; FFmpeg reads it reliably)
    path = folder / f"{aid}{'.png' if kind == 'image' else ext}"
    width = height = duration = None
    key = None
    if kind == "image":
        try:
            from io import BytesIO
            img = Image.open(BytesIO(data))
            img = img.convert("RGBA")
        except Exception as exc:
            raise AssetError(f"не получилось открыть картинку: {exc}") from exc
        img.save(path)
        width, height = img.size
        key = detect_key_color(img)
    else:
        path.write_bytes(data)
        try:
            info = probe(path) if kind == "video" else None
        except ProbeError as exc:
            path.unlink(missing_ok=True)
            raise AssetError(str(exc)) from exc
        if info:
            width, height, duration = info.width, info.height, info.duration
            first = first_frame(path)
            key = detect_key_color(first) if first is not None else None
        else:
            duration = _audio_duration(path)
    with db.session() as s:
        a = s.get(EditAsset, aid) or EditAsset(id=aid)
        a.kind, a.name, a.path = kind, Path(filename).name[:255], str(path)
        a.width, a.height, a.duration, a.key_color = width, height, duration, key
        s.add(a)
        s.flush()
        s.expunge(a)
        return a


def _audio_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def first_frame(path: Path) -> Image.Image | None:
    out = path.with_suffix(".first.png")
    subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1",
                    str(out)], capture_output=True, timeout=60)
    if not out.is_file():
        return None
    img = Image.open(out).convert("RGBA")
    img.load()
    return img


def preview_image(asset: EditAsset) -> Path | None:
    """What the editor draws for this asset (the picture, or the first frame of a video)."""
    p = Path(asset.path)
    if asset.kind == "image":
        return p
    if asset.kind == "video":
        fp = p.with_suffix(".first.png")
        if not fp.is_file():
            first_frame(p)
        return fp if fp.is_file() else None
    return None


def _key_masks(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    r, g, b = (rgb[..., i].astype(np.int32) for i in range(3))
    green = (g > 90) & (g > r * 1.4) & (g > b * 1.4)
    blue = (b > 90) & (b > r * 1.4) & (b > g * 1.2)
    return green, blue


def detect_key_color(img: Image.Image) -> str | None:
    """Dominant chroma-key colour (green or blue screen) if it covers a noticeable part of the picture."""
    small = img.convert("RGBA")
    small.thumbnail((320, 320))
    arr = np.asarray(small)
    rgb, alpha = arr[..., :3], arr[..., 3]
    green, blue = _key_masks(rgb)
    green &= alpha > 200
    blue &= alpha > 200
    mask = green if green.sum() >= blue.sum() else blue
    if mask.sum() < 0.01 * mask.size:
        return None
    med = np.median(rgb[mask], axis=0).astype(int)
    return "#%02x%02x%02x" % tuple(med)


def key_window(img: Image.Image, color: str, similarity: float) -> tuple[int, int, int, int] | None:
    """Bounding box (x, y, w, h, in image pixels) of the largest keyed area: the «window» for the clip."""
    arr = np.asarray(img.convert("RGB")).astype(np.int32)
    target = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)])
    dist = np.sqrt(((arr - target) ** 2).sum(axis=2))
    mask = dist <= max(30.0, similarity * 255)
    if mask.sum() < 0.005 * mask.size:
        return None
    # largest connected area: label 4-connected components on a downscaled mask
    scale = max(1, max(mask.shape) // 400)
    m = mask[::scale, ::scale]
    labels = np.zeros(m.shape, dtype=np.int32)
    best, best_size, cur = None, 0, 0
    h, w = m.shape
    for y in range(h):
        for x in range(w):
            if m[y, x] and not labels[y, x]:
                cur += 1
                stack, pts = [(y, x)], []
                labels[y, x] = cur
                while stack:
                    cy, cx = stack.pop()
                    pts.append((cy, cx))
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and m[ny, nx] and not labels[ny, nx]:
                            labels[ny, nx] = cur
                            stack.append((ny, nx))
                if len(pts) > best_size:
                    ys, xs = zip(*pts)
                    best, best_size = (min(xs), min(ys), max(xs), max(ys)), len(pts)
    if best is None:
        return None
    x0, y0, x1, y1 = best
    return x0 * scale, y0 * scale, (x1 - x0 + 1) * scale, (y1 - y0 + 1) * scale
