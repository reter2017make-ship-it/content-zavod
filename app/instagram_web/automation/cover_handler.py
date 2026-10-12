"""The cover chosen by the owner (on the «Редактировать» step). Without one Instagram keeps its own frame."""
from __future__ import annotations

from pathlib import Path

from app.instagram_web import selectors as S
from app.instagram_web.automation.errors import COVER_FAILED, PublishError


def set_cover(page, cover_path: str | None, log) -> None:
    if not cover_path:
        return
    if not Path(cover_path).is_file():
        raise PublishError(COVER_FAILED, "файл обложки не найден", retry=False)
    inp = page.locator(S.COVER_INPUT)
    if not inp.count():
        raise PublishError(COVER_FAILED, "на шаге «Редактировать» нет выбора обложки")
    inp.first.set_input_files(cover_path)
    page.wait_for_timeout(1500)
    log.write("Cover set")
