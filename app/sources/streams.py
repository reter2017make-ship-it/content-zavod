"""Twitch, Kick and other stream platforms: interface only for now (stage 3 plan)."""
from __future__ import annotations

from pathlib import Path

from app.sources.base import DiscoveredItem, SourceConnector


class StreamConnector(SourceConnector):
    """Base for clip sources. Concrete implementations are not written yet; they report so honestly."""

    platform = "stream"

    def __init__(self, settings):
        self.enabled = bool((settings.get(f"sources.{self.name}", {}) or {}).get("enabled", False))

    def configured(self) -> tuple[bool, str]:
        return False, f"{self.platform}: не реализовано (только интерфейс)"

    def discover(self, known_ids: set[str], limit: int) -> list[DiscoveredItem]:
        return []

    def download(self, item_source_id: str, dest: Path, media_url: str | None = None) -> Path:
        raise NotImplementedError(f"{self.platform}: не реализовано")


class TwitchSource(StreamConnector):
    name = "twitch"
    platform = "Twitch (Helix API, клипы)"


class KickSource(StreamConnector):
    name = "kick"
    platform = "Kick"
