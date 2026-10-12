from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class DiscoveredItem:
    source: str
    source_id: str
    source_url: str | None = None
    title: str | None = None
    author: str | None = None
    channel: str | None = None
    category: str | None = None
    caption: str | None = None
    published_at: datetime | None = None
    rights_status: str = "UNKNOWN"
    license: str | None = None
    license_url: str | None = None
    attribution: str | None = None
    media_url: str | None = None
    duration: float | None = None
    discovery_only: bool = False
    extra: dict = field(default_factory=dict)  # source_score + raw API facts (score, views, ...)


class SourceConnector(ABC):
    """Each content source implements discover → download → metadata."""

    name: str = "base"

    @abstractmethod
    def configured(self) -> tuple[bool, str]:
        """(ready, human-readable reason if not)."""

    @abstractmethod
    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        """New items not in known_ids, at most `limit`."""

    @abstractmethod
    def download(self, item_source_id: str, dest: Path, media_url: str | None = None) -> Path:
        """Fetch the media into `dest` (a temp path) and return it."""

    def metadata(self, item_source_id: str) -> dict:
        return {}

    def companion_subtitles(self, item_source_id: str) -> Path | None:
        return None

    def close(self) -> None:
        pass


class ApiConnector(SourceConnector):
    """Common plumbing for HTTP API sources: config section, keys from .env, rate-limited client."""

    required_env: tuple[str, ...] = ()

    def __init__(self, settings, transport=None):
        from app.sources.http import RateLimitedClient

        self.settings = settings
        self.cfg = settings.get(f"sources.{self.name}", {}) or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.keys = {k: settings.env(k) for k in self.required_env}
        self.filters = settings.section("filters")
        max_mb = self.cfg.get("max_size_mb") or self.filters.get("max_filesize_mb", 2048)
        self.max_bytes = float(max_mb) * 1024 * 1024
        self.client = RateLimitedClient(
            requests_per_minute=float(self.cfg.get("requests_per_minute", 30)),
            retries=int(self.cfg.get("retries", 3)),
            headers=self.default_headers(),
            transport=transport,
        )

    def default_headers(self) -> dict:
        return {}

    def configured(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "выключен в config.yaml"
        missing = [k for k, v in self.keys.items() if not v]
        if missing:
            return False, "нет ключей в .env: " + ", ".join(missing)
        return True, ""

    def duration_ok(self, duration: float | None) -> bool:
        if duration is None:
            return True
        return float(self.filters.get("min_duration", 0)) <= duration <= float(self.filters.get("max_duration", 1e9))

    def download(self, item_source_id: str, dest: Path, media_url: str | None = None) -> Path:
        if not media_url:
            raise RuntimeError(f"{self.name}: нет ссылки на файл для {item_source_id}")
        return self.client.download(media_url, dest, self.max_bytes)

    def close(self) -> None:
        self.client.close()
