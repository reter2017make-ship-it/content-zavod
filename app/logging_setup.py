from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False


def setup_logging(logs_dir: Path, level: str = "INFO", backup_days: int = 14, console: bool = True) -> None:
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    root.setLevel(level)
    fmt = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = TimedRotatingFileHandler(
        logs_dir / "factory.log", when="midnight", backupCount=backup_days, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(logging.WARNING)
        root.addHandler(stream)
    # Third-party chatter (e.g. "unauthenticated requests to the HF Hub") stays out of the console.
    for noisy in ("huggingface_hub", "httpx", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    _configured = True


def log_event(logger: logging.Logger, operation: str, status: str, content_id: int | None = None,
              error: str | None = None, **fields) -> None:
    """One line per action: operation, content_id, status, error and any extra key=value fields."""
    parts = [operation.upper()]
    if content_id is not None:
        parts.append(f"content_id={content_id}")
    parts.extend(f"{k}={v}" for k, v in fields.items() if v is not None)
    parts.append(f"status={status}")
    if error:
        parts.append(f"error={error}")
    level = logging.ERROR if status in ("FAILED", "ERROR") else logging.INFO
    logger.log(level, " ".join(parts))
