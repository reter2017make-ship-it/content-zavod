"""HTTP for source APIs: per-source rate limit, retries with exponential backoff, streamed downloads."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("sources.http")

USER_AGENT = "ContentFactory/0.3 (local content pipeline; python-httpx)"
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class SourceHTTPError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimitedClient:
    def __init__(self, requests_per_minute: float = 30, retries: int = 3, timeout: float = 30,
                 headers: dict | None = None, transport: httpx.BaseTransport | None = None,
                 sleep=None):
        self.min_interval = 60.0 / max(requests_per_minute, 0.1)
        self.retries = retries
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._last = 0.0
        self._lock = threading.Lock()
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, transport=transport,
                                   headers={"User-Agent": USER_AGENT, **(headers or {})})

    def _wait_turn(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                self._sleep(delay)
            self._last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last_error = None
        for attempt in range(self.retries + 1):
            self._wait_turn()
            try:
                r = self.client.request(method, url, **kw)
            except httpx.TransportError as exc:
                last_error = SourceHTTPError(f"сеть: {exc}")
            else:
                if r.status_code < 400:
                    return r
                if r.status_code not in RETRY_STATUSES:
                    raise SourceHTTPError(f"HTTP {r.status_code}: {r.text[:200]}", r.status_code)
                last_error = SourceHTTPError(f"HTTP {r.status_code}", r.status_code)
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < self.retries:
                    self._sleep(min(int(retry_after), 300))
                    continue
            if attempt < self.retries:
                backoff = min(60, 2 ** attempt)
                log.warning("%s %s failed (%s), retry in %ss", method, url.split("?")[0], last_error, backoff)
                self._sleep(backoff)
        raise last_error

    def get_json(self, url: str, **kw) -> dict:
        r = self.request("GET", url, **kw)
        try:
            return r.json()
        except ValueError as exc:
            raise SourceHTTPError(f"не JSON: {r.text[:200]}") from exc

    def download(self, url: str, dest: Path, max_bytes: float, **kw) -> Path:
        """Streams to dest; aborts when the file grows past max_bytes."""
        self._wait_turn()
        written = 0
        with self.client.stream("GET", url, **kw) as r:
            if r.status_code >= 400:
                raise SourceHTTPError(f"скачивание: HTTP {r.status_code}", r.status_code)
            declared = int(r.headers.get("Content-Length") or 0)
            if declared and declared > max_bytes:
                raise SourceHTTPError(f"файл больше лимита: {declared / 1048576:.0f} MB")
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    written += len(chunk)
                    if written > max_bytes:
                        raise SourceHTTPError("файл больше лимита")
                    fh.write(chunk)
        return dest

    def close(self) -> None:
        self.client.close()
