from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from app.config.settings import Settings
from app.sources.base import DiscoveredItem, SourceConnector


class FilesystemSource(SourceConnector):
    """Watches local folders. A sidecar `<name>.srt` next to a video is used as its subtitles."""

    name = "filesystem"

    def __init__(self, settings: Settings):
        cfg = settings.get("sources.filesystem", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.extensions = {e.lower() for e in cfg.get("extensions", [".mp4", ".mov"])}
        self.folders = [
            {**f, "path": settings.path(f["path"])} for f in cfg.get("folders", [])
        ]

    def configured(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "выключен в config.yaml"
        missing = [str(f["path"]) for f in self.folders if not f["path"].is_dir()]
        if not self.folders:
            return False, "не задано ни одной папки"
        if missing:
            return False, "нет папок: " + ", ".join(missing)
        return True, ""

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        found: list[DiscoveredItem] = []
        if not self.enabled:
            return found
        for folder in self.folders:
            root: Path = folder["path"]
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(found) >= limit:
                    return found
                if not path.is_file() or path.suffix.lower() not in self.extensions or path.name.startswith("."):
                    continue
                source_id = str(path.resolve())
                if source_id in known_ids:
                    continue
                found.append(DiscoveredItem(
                    source=self.name,
                    source_id=source_id,
                    source_url=path.resolve().as_uri(),
                    title=path.stem,
                    channel=str(root),
                    category=folder.get("category", "other"),
                    rights_status=folder.get("rights_status", "UNKNOWN"),
                    license=folder.get("license"),
                    published_at=datetime.fromtimestamp(path.stat().st_mtime),
                    extra={"source_score": folder.get("source_score", 60)},
                ))
        return found

    def download(self, item_source_id: str, dest: Path, media_url: str | None = None) -> Path:
        src = Path(item_source_id)
        if not src.is_file():
            raise FileNotFoundError(f"исходный файл пропал: {src}")
        shutil.copyfile(src, dest)
        return dest

    def metadata(self, item_source_id: str) -> dict:
        p = Path(item_source_id)
        return {"path": str(p), "size": p.stat().st_size if p.exists() else None}

    def companion_subtitles(self, item_source_id: str) -> Path | None:
        srt = Path(item_source_id).with_suffix(".srt")
        return srt if srt.is_file() else None
