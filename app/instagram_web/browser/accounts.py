"""Linked accounts of the signed-in Instagram (the «Переключиться» dialog). Read the list, switch, verify.
A password is never typed: if Instagram asks for one, the account is marked «нужен вход вручную»."""
from __future__ import annotations

import time

from app.instagram_web import selectors as S


def _visible_buttons(page, scope=None):
    root = page.locator(scope) if scope else page
    return root.locator("[role=button], button, a").filter(visible=True)


def _open_switcher(page) -> bool:
    page.goto(S.HOME, wait_until="domcontentloaded", timeout=60000)
    end = time.time() + 20
    while time.time() < end:
        btns = _visible_buttons(page)
        for i in range(min(btns.count(), 150)):
            b = btns.nth(i)
            try:
                if b.inner_text(timeout=500).strip() in S.TEXT_SWITCH:
                    b.click()
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False


def _rows(page) -> list[tuple[str, object]]:
    rows = []
    btns = _visible_buttons(page, S.DIALOG)
    for i in range(min(btns.count(), 40)):
        b = btns.nth(i)
        try:
            t = b.inner_text(timeout=500).strip()
        except Exception:
            continue
        if t and "\n" not in t and " " not in t and t not in S.TEXT_ADD_ACCOUNT:
            rows.append((t, b))
    return rows


def _current_in_dialog(page) -> str | None:
    """The active account is the one without a button (it carries the check mark)."""
    try:
        text = page.locator(S.DIALOG).filter(visible=True).first.inner_text(timeout=2000)
    except Exception:
        return None
    clickable = {name for name, _ in _rows(page)}
    for line in text.split("\n"):
        line = line.strip()
        if line and " " not in line and line not in clickable and not S.SWITCH_DIALOG.search(line):
            return line
    return None


def list_accounts(page) -> list[dict]:
    if not _open_switcher(page):
        return []
    current = _current_in_dialog(page)
    out = ([{"username": current, "current": True}] if current else []) + \
          [{"username": n, "current": False} for n, _ in _rows(page) if n != current]
    page.keyboard.press("Escape")
    return out


def open_login_for(page, username: str) -> bool:
    """Opens Instagram's own sign-in for a linked account and leaves it to the person (no typing here)."""
    if not _open_switcher(page):
        return False
    target = next((b for n, b in _rows(page) if n.lower() == username.lower()), None)
    if target is None:
        return False
    target.click()
    try:
        page.bring_to_front()
    except Exception:
        pass
    return True


def switch_to(page, username: str, read_username) -> dict:
    """{"ok", "username", "reason"}. read_username(page) tells who is signed in after the switch."""
    if not _open_switcher(page):
        return {"ok": False, "username": None, "reason": "не нашёл кнопку «Переключиться» на главной"}
    target = next((b for n, b in _rows(page) if n.lower() == username.lower()), None)
    if target is None:
        current = _current_in_dialog(page)
        page.keyboard.press("Escape")
        if current and current.lower() == username.lower():
            return {"ok": True, "username": current, "reason": None}
        return {"ok": False, "username": None, "reason": f"@{username} нет среди связанных аккаунтов"}
    target.click()
    end = time.time() + 40
    while time.time() < end:
        page.wait_for_timeout(1000)
        if page.locator(S.PASSWORD_INPUT).filter(visible=True).count():
            return {"ok": False, "username": None, "need_login": True,
                    "reason": f"для @{username} Instagram просит пароль: войдите в него вручную в окне Chrome"}
        if "/accounts/login" not in page.url and not page.locator(S.DIALOG).filter(visible=True).count():
            break
    who = None
    for _ in range(4):  # Instagram reloads after a switch: read the active @name from a fresh home page
        page.wait_for_timeout(2000)
        try:
            page.goto(S.HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        except Exception:
            continue
        who = read_username(page)
        if who:
            break
    return {"ok": bool(who and who.lower() == username.lower()), "username": who,
            "reason": None if who and who.lower() == username.lower() else f"после переключения активен @{who or '?'}"}
