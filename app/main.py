from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import Iterator

from app.config.settings import Settings, load_settings
from app.database.database import Database
from app.logging_setup import setup_logging


class AlreadyRunning(RuntimeError):
    pass


def bootstrap(config_path: str | None = None, console: bool = True) -> tuple[Settings, Database]:
    settings = load_settings(config_path)
    settings.ensure_dirs()
    setup_logging(settings.logs_dir, settings.get("logging.level", "INFO"),
                  int(settings.get("logging.backup_days", 14)), console=console)
    db = Database(settings.database_path)
    db.create_all()
    return settings, db


def _lock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(str(exc)) from exc
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def factory_lock(settings: Settings) -> Iterator[None]:
    """Only one factory run at a time; the OS releases the lock if the process dies."""
    fh = open(settings.lock_path, "a+")
    try:
        _lock(fh)
    except BlockingIOError as exc:
        fh.close()
        raise AlreadyRunning("завод уже запущен другим процессом") from exc
    try:
        # on Windows the locked first byte cannot be written, so the pid goes after it
        start = 1 if sys.platform == "win32" else 0
        fh.seek(start)
        fh.truncate(start)
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        _unlock(fh)
        fh.close()
