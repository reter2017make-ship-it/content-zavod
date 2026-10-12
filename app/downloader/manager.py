from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.media.ffprobe import MediaInfo, probe
from app.scoring.duplicate import sha256_file
from app.sources.base import SourceConnector


class DownloadError(RuntimeError):
    pass


@dataclass
class Downloaded:
    path: Path
    sha256: str
    info: MediaInfo
    subtitle_path: Path | None


class DownloadManager:
    """temp file → size check → FFprobe → SHA-256 → permanent storage (data/raw)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.temp_dir = settings.data("temp")
        self.raw_dir = settings.data("raw")
        self.max_bytes = float(settings.get("filters.max_filesize_mb", 2048)) * 1024 * 1024

    def fetch(self, connector: SourceConnector, source_id: str, suffix: str, media_url: str | None,
              is_known_sha) -> Downloaded | str:
        """Returns Downloaded, or the sha256 of an already stored identical file (nothing is kept)."""
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.temp_dir / f"{uuid.uuid4().hex}{suffix}.part"
        try:
            connector.download(source_id, tmp, media_url)
            size = tmp.stat().st_size if tmp.exists() else 0
            if size == 0:
                raise DownloadError("пустой файл")
            if size > self.max_bytes:
                raise DownloadError(f"файл больше лимита: {size / 1048576:.0f} MB")
            info = probe(tmp)
            sha = sha256_file(tmp)
            if is_known_sha(sha):
                return sha

            dest_dir = self.raw_dir / sha[:2]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{sha}{suffix.lower()}"
            if dest.exists():
                tmp.unlink()
            else:
                shutil.move(str(tmp), dest)

            sub_dest = None
            sub_src = connector.companion_subtitles(source_id)
            if sub_src:
                sub_dest = dest.with_suffix(".srt")
                shutil.copyfile(sub_src, sub_dest)
            return Downloaded(path=dest, sha256=sha, info=info, subtitle_path=sub_dest)
        finally:
            tmp.unlink(missing_ok=True)
