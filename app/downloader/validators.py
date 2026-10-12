from __future__ import annotations

from app.media.ffprobe import MediaInfo


def check_filters(info: MediaInfo, filters: dict) -> str | None:
    """Reason for rejection, or None if the media passes the configured filters."""
    min_d, max_d = float(filters.get("min_duration", 0)), float(filters.get("max_duration", 1e9))
    if info.duration < min_d:
        return f"слишком короткое: {info.duration:.1f}s < {min_d:g}s"
    if info.duration > max_d:
        return f"слишком длинное: {info.duration:.1f}s > {max_d:g}s"
    if info.width < int(filters.get("min_width", 0)) or info.height < int(filters.get("min_height", 0)):
        return f"низкое разрешение: {info.width}x{info.height}"
    max_mb = filters.get("max_filesize_mb")
    if max_mb and info.filesize > float(max_mb) * 1024 * 1024:
        return f"слишком большой файл: {info.filesize / 1048576:.0f} MB"
    return None
