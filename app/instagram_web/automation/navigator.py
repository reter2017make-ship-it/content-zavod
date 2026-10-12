"""Moves through Instagram's «create» dialog by roles and texts (selectors.py). Waits for the page state,
never for a fixed time only. Every step checks for Instagram's own checks (CAPTCHA, «это вы?») first."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from app.instagram_web import selectors as S
from app.instagram_web.automation.errors import NeedsPerson, PublishError, PAGE_CHANGED, SESSION_EXPIRED


class Navigator:
    def __init__(self, page, log, stop_requested=lambda: False):
        self.page, self.log, self.stop_requested = page, log, stop_requested

    # ---------- page state ----------
    def body(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=3000)[:6000]
        except Exception:
            return ""

    def dialogs_text(self) -> str:
        """Text of open dialogs only (the feed behind them may contain any words)."""
        try:
            d = self.page.locator(S.DIALOG).filter(visible=True)
            return " | ".join(d.nth(i).inner_text(timeout=1000)[:3000] for i in range(min(d.count(), 5)))
        except Exception:
            return ""

    def guard(self) -> None:
        """Stops at once when Instagram wants a person or the session is gone."""
        path = urlparse(self.page.url).path
        if S.CHECKPOINT_URL.search(path) or S.CHECKPOINT_TEXT.search(self.dialogs_text()):
            raise NeedsPerson("Instagram просит подтверждения (проверка безопасности или вход). Выполните его в окне Chrome")
        if "/accounts/login" in path:
            raise PublishError(SESSION_EXPIRED, "сессия Instagram истекла: войдите заново на странице «Instagram»", retry=False)

    def button(self, texts, scope: str | None = None):
        """A visible element with role=button (or <button>) whose own text is exactly one of `texts`."""
        root = self.page.locator(scope) if scope else self.page
        cand = root.locator("[role=button], button").filter(visible=True)
        for i in range(min(cand.count(), 80)):
            el = cand.nth(i)
            try:
                if el.inner_text(timeout=1000).strip() in texts:
                    return el
            except Exception:
                continue
        return None

    def icon_link(self, labels):
        """A link that shows only an icon (collapsed side navigation): found by the icon's aria-label."""
        for label in labels:
            loc = self.page.locator(f"svg[aria-label='{label}']").filter(visible=True)
            if loc.count():
                return loc.first.locator("xpath=ancestor::*[self::a or @role='link' or @role='button' or self::button][1]")
        return None

    def link(self, texts):
        cand = self.page.locator("a, [role=link]").filter(visible=True)
        for i in range(min(cand.count(), 120)):
            el = cand.nth(i)
            try:
                if el.inner_text(timeout=1000).strip() in texts:
                    return el
            except Exception:
                continue
        return None

    def wait_for(self, check, timeout: float, what: str, code: str = PAGE_CHANGED, poll: float = 0.5):
        """Polls `check()` (returns something truthy when the page is there) until timeout."""
        end = time.time() + timeout
        while time.time() < end:
            self.guard()
            res = check()
            if res:
                return res
            self.page.wait_for_timeout(int(poll * 1000))
        raise PublishError(code, f"не дождался: {what} ({int(timeout)} с)")

    def dialog_title(self) -> str:
        d = self.page.locator(S.DIALOG).filter(visible=True)
        out = []
        for i in range(min(d.count(), 5)):
            out.append((d.nth(i).get_attribute("aria-label") or "") + " " + d.nth(i).inner_text(timeout=1000)[:80])
        return " | ".join(out)

    # ---------- steps ----------
    def open_create(self) -> None:
        self.page.goto(S.HOME, wait_until="domcontentloaded", timeout=60000)
        self.guard()
        create = self.wait_for(lambda: self.link(S.TEXT_CREATE) or self.icon_link(S.ICON_CREATE), 30, "кнопка «Создать»")
        create.click()
        self.log.detail("clicked Создать")
        # some accounts get a menu («Публикация / Прямой эфир…»), others open the upload dialog at once
        found = self.wait_for(lambda: ("dialog" if self.page.locator(S.FILE_INPUT).count() else None)
                              or self.link(S.TEXT_POST), 15, "пункт «Публикация» или окно загрузки")
        if found != "dialog":
            found.click()
            self.wait_for(lambda: self.page.locator(S.FILE_INPUT).count() > 0, 30, "окно создания публикации")

    def press(self, texts, what: str, timeout: float = 30):
        b = self.wait_for(lambda: self.button(texts, S.DIALOG) or self.button(texts), timeout, what)
        b.click()
        self.log.detail(f"clicked {what}")
        return b
