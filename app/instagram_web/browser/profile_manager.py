"""The separate Chrome profile of one Instagram account (browser_profiles/instagram_<account>/).

It is not the owner's everyday Chrome profile: other tabs and cookies are never touched. The profile keeps
the Instagram session the owner created by signing in by hand; no login or password is stored by the factory."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


class ProfileManager:
    def __init__(self, path: Path):
        self.path = path

    def ensure(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)  # the session in it is access to the account
        return self.path

    @property
    def exists(self) -> bool:
        return self.path.is_dir() and any(self.path.iterdir())

    def in_use_by_other_chrome(self) -> bool:
        """Chrome leaves SingletonLock while a window with this profile is open (e.g. opened by hand)."""
        lock = self.path / "SingletonLock"
        return lock.is_symlink() or lock.exists()

    def clear_stale_lock(self) -> None:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = self.path / name
            if p.is_symlink() or p.exists():
                p.unlink(missing_ok=True)

    def forget(self) -> None:
        """«Отключить Instagram»: the session is deleted together with the profile."""
        shutil.rmtree(self.path, ignore_errors=True)
