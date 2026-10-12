"""Settings of the module (config.yaml → instagram_web) with defaults."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class IGConfig:
    enabled: bool = True
    autopost: bool = False
    window_start: str = "10:00"
    window_end: str = "23:00"
    interval_minutes: int = 40
    max_posts_per_day: int = 15
    max_attempts: int = 3
    retry_failed: bool = True
    auto_enqueue: bool = False
    debug: bool = False
    headless: bool = False          # visible Chrome by default: the owner sees what happens
    processing_timeout_s: int = 120
    share_timeout_s: int = 300      # waiting for Instagram's «опубликовано» after «Поделиться»
    profiles_dir: str = "browser_profiles"
    account: str = "default"
    timezone: str = "Europe/Moscow"

    @classmethod
    def from_settings(cls, settings) -> "IGConfig":
        raw = settings.get("instagram_web", {}) or {}
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        cfg = cls(**known)
        if "timezone" not in raw:
            cfg.timezone = settings.get("schedule.timezone") or settings.get("publishing.timezone", cfg.timezone)
        return cfg

    def profile_dir(self, settings) -> Path:
        return settings.path(self.profiles_dir) / f"instagram_{self.account}"
