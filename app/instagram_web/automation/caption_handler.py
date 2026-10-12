"""Caption exactly as written (Russian text, emoji, line breaks, links, hashtags): pasted into Instagram's editor
as one block, so hashtag suggestions do not grab keystrokes; then read back and compared."""
from __future__ import annotations

import re

from app.instagram_web import selectors as S
from app.instagram_web.automation.errors import CAPTION_FAILED, PublishError

PASTE_JS = """(args) => { const [sel, text] = args; const el = document.querySelector(sel); if (!el) return false;
  el.focus(); const dt = new DataTransfer(); dt.setData('text/plain', text);
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); return true; }"""


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").replace("​", "")).strip()


def set_caption(page, caption: str, log) -> None:
    if not caption:
        return
    box = page.locator(S.CAPTION_BOX).first
    try:
        box.wait_for(state="visible", timeout=15000)
        box.click()
    except Exception as exc:
        raise PublishError(CAPTION_FAILED, "поле подписи не найдено") from exc
    page.evaluate(PASTE_JS, [S.CAPTION_BOX, caption])
    page.wait_for_timeout(500)
    if _norm(box.inner_text()) != _norm(caption):
        # the editor ignored the paste: type it line by line (Shift+Enter keeps it one caption)
        page.keyboard.press("Meta+A")
        page.keyboard.press("Backspace")
        for i, line in enumerate(caption.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        page.wait_for_timeout(500)
    got = box.inner_text()
    if _norm(got) != _norm(caption):
        raise PublishError(CAPTION_FAILED, f"подпись вставилась не так: «{_norm(got)[:80]}»")
    log.write("Caption inserted")
