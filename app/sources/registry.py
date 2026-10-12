from __future__ import annotations

from app.config.settings import Settings
from app.sources.base import SourceConnector
from app.sources.filesystem import FilesystemSource
from app.sources.pexels import PexelsSource
from app.sources.pixabay import PixabaySource
from app.sources.streams import KickSource, TwitchSource
from app.sources.telegram import TelegramSource
from app.sources.wikimedia import WikimediaSource


def build_sources(settings: Settings, db=None) -> dict[str, SourceConnector]:
    """Order = priority. Telegram channels live in the database (managed from the web UI)."""
    sources: list[SourceConnector] = [
        FilesystemSource(settings), TelegramSource(settings, db=db),
        PexelsSource(settings), PixabaySource(settings), WikimediaSource(settings),
        TwitchSource(settings), KickSource(settings),
    ]
    return {s.name: s for s in sources}
