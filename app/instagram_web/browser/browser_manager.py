"""One Chrome window (Playwright persistent context) for Instagram, driven from a single dedicated thread
(Playwright's sync API must stay in the thread that started it). Uses the installed Google Chrome
(channel="chrome"), so no browser is downloaded."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from app.instagram_web import selectors as S
from app.instagram_web.browser.profile_manager import ProfileManager


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class BrowserManager:
    def __init__(self, profile: ProfileManager, headless: bool = False, playwright_factory=None, log=None):
        self.profile, self.headless, self.log = profile, headless, log
        self._factory = playwright_factory  # tests pass a fake
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="instagram-browser")
        self._pw = None
        self.ctx = None
        self._closed_by_user = threading.Event()

    # ---------- thread plumbing ----------
    def call(self, fn, *args, timeout: float = 180, **kw):
        """Runs fn(self, ...) in the browser thread and waits for the result."""
        return self._pool.submit(fn, *args, **kw).result(timeout)

    # ---------- lifecycle (browser thread) ----------
    def _start(self, headless: bool | None = None):
        if self.ctx is not None and not self._closed_by_user.is_set():
            return self.ctx
        self.ctx = None
        if self._pw is None:
            if self._factory:
                self._pw = self._factory()
            else:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
        if self.profile.in_use_by_other_chrome():
            # a lock left by a crashed Chrome; a Chrome really running with this profile would refuse to start
            self.profile.clear_stale_lock()
        self.profile.ensure()
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile.path), channel="chrome", headless=self.headless if headless is None else headless,
                viewport={"width": 1400, "height": 900}, locale="ru-RU",
                chromium_sandbox=True,  # Playwright turns Chrome's sandbox off by default; keep it on
                args=["--no-first-run", "--no-default-browser-check"])
        except Exception as exc:
            raise BrowserError("BROWSER_FAILED", f"Chrome не запустился: {str(exc)[:300]}") from exc
        self._closed_by_user.clear()
        self.ctx.on("close", lambda *_: self._closed_by_user.set())
        if self.log:
            self.log.write("Chrome started (profile " + self.profile.path.name + ")")
        return self.ctx

    def _page(self):
        ctx = self._start()
        pages = [p for p in ctx.pages if not p.is_closed()]
        return pages[0] if pages else ctx.new_page()

    def _close(self):
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
        self.ctx = None

    @property
    def is_open(self) -> bool:
        return self.ctx is not None and not self._closed_by_user.is_set()

    # ---------- public (any thread) ----------
    def open(self, url: str = S.HOME) -> None:
        def go():
            page = self._page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.bring_to_front()
            except Exception:
                pass
        self.call(self._retry_closed, go)

    def close(self) -> None:
        self.call(self._close, timeout=30)

    def shutdown(self) -> None:
        try:
            self.call(self._close, timeout=30)
            if self._pw is not None:
                self.call(self._pw.stop, timeout=30)
        except Exception:
            pass
        self._pool.shutdown(wait=False)

    def check_auth(self) -> dict:
        """{"authorized", "username", "checkpoint", "reason"} — read only: looks at cookies and the page."""
        return self.call(self._retry_closed, self._check_auth, timeout=150)

    def _retry_closed(self, fn, *a):
        """The owner may close the Chrome window or tab at any time: start it again (same profile) and repeat once."""
        try:
            return fn(*a)
        except Exception as exc:
            if "has been closed" not in str(exc) and "Target closed" not in str(exc):
                raise
            self._close()
            return fn(*a)

    def _check_auth(self) -> dict:
        ctx = self._start()
        page = self._page()
        # a fresh load of the home page (only on «Я вошёл», so it never interrupts typing)
        page.goto(S.HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("domcontentloaded")
        checkpoint = bool(S.CHECKPOINT_URL.search(urlparse(page.url).path))
        if not checkpoint:
            try:
                d = page.locator(S.DIALOG).filter(visible=True)
                text = " ".join(d.nth(i).inner_text(timeout=2000)[:2000] for i in range(min(d.count(), 3)))
                checkpoint = bool(S.CHECKPOINT_TEXT.search(text))
            except Exception:
                pass
        names = {c["name"]: c for c in ctx.cookies(S.HOME)}
        signed_in = S.SESSION_COOKIE in names and "/accounts/login" not in page.url
        if checkpoint:
            return {"authorized": False, "username": None, "checkpoint": True,
                    "reason": "Instagram просит подтверждения: выполните его в окне Chrome"}
        if not signed_in:
            return {"authorized": False, "username": None, "checkpoint": False,
                    "reason": "в этом окне Chrome вход в Instagram не выполнен"}
        return {"authorized": True, "username": self._username(page), "checkpoint": False, "reason": None}

    def _username(self, page) -> str | None:
        # 1) the profile link in the side navigation: <a href="/name/"> with the profile picture
        try:
            for a in page.locator("a[href^='/']").all()[:200]:
                href = (a.get_attribute("href") or "").strip("/")
                if not href or "/" in href or href in S.RESERVED_PATHS:
                    continue
                img = a.locator("img")
                if img.count() and S.PROFILE_PICTURE_ALT.search(img.first.get_attribute("alt") or ""):
                    return href
        except Exception:
            pass
        # 2) the «edit profile» page
        try:
            page.goto(S.EDIT_PROFILE, wait_until="domcontentloaded", timeout=60000)
            box = page.locator(S.USERNAME_INPUT)
            box.first.wait_for(timeout=10000)
            name = box.first.input_value().strip()
            page.goto(S.HOME, wait_until="domcontentloaded", timeout=60000)
            return name or None
        except Exception:
            return None
