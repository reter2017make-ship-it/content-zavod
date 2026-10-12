"""logs/instagram/<date>.log (one line per action) and screenshots of failures in logs/instagram/errors/."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path


class IGLog:
    def __init__(self, settings, debug: bool = False):
        self.dir = settings.logs_dir / "instagram"
        self.errors = self.dir / "errors"
        self.debug = debug
        self._log = logging.getLogger("instagram_web")

    def _file(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / f"{datetime.now():%Y-%m-%d}.log"

    def write(self, text: str, level: str = "INFO") -> None:
        line = f"{datetime.now():%H:%M:%S} {'' if level == 'INFO' else level + ' '}{text}"
        with open(self._file(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        getattr(self._log, "warning" if level != "INFO" else "info")(text)

    def detail(self, text: str) -> None:
        """Only in debug mode: every single action."""
        if self.debug:
            self.write(text, "DEBUG")

    def error(self, name: str, code: str, message: str) -> None:
        self.write(f"{name}\nERROR_CODE: {code} {message}", "ERROR")

    def screenshot(self, page, name: str) -> Path | None:
        if page is None:
            return None
        self.errors.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w.-]+", "_", Path(name).stem)[:60] or "page"
        out = self.errors / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            page.screenshot(path=str(out), full_page=False)
            return out
        except Exception:
            return None
