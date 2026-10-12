"""One Reel, start to finish: create → upload → (OK) → crop → cover → caption → checks → Share → confirmation.
Runs in the browser thread. A post counts as published only when Instagram confirms it."""
from __future__ import annotations

from app.instagram_web import selectors as S
from app.instagram_web.automation.caption_handler import set_caption
from app.instagram_web.automation.cover_handler import set_cover
from app.instagram_web.automation.errors import (
    CROP_FAILED, PAGE_CHANGED, PUBLISH_FAILED, STOPPED, UPLOAD_FAILED, VIDEO_PROCESSING_TIMEOUT, WRONG_ACCOUNT, PublishError,
)
from app.instagram_web.automation.navigator import Navigator


class Publisher:
    def __init__(self, page, log, processing_timeout: float = 120, share_timeout: float = 300,
                 stop_requested=lambda: False):
        self.page, self.log = page, log
        self.nav = Navigator(page, log, stop_requested)
        self.processing_timeout, self.share_timeout = processing_timeout, share_timeout
        self.stop_requested = stop_requested

    def _stop_point(self) -> None:
        if self.stop_requested():
            raise PublishError(STOPPED, "остановлено пользователем до публикации", retry=True)

    def run(self, video: str, caption: str, cover: str | None, username: str | None, on_share) -> dict:
        nav, page = self.nav, self.page
        nav.open_create()
        self._stop_point()
        page.locator(S.FILE_INPUT).first.set_input_files(video)
        self.log.write("Video selected, uploading")

        def after_upload():
            dtext = nav.dialogs_text()
            if S.UPLOAD_FAILED.search(dtext):
                raise PublishError(UPLOAD_FAILED, "Instagram не принял файл: " + dtext[:120].replace("\n", " "))
            return nav.button(S.TEXT_OK) or nav.button(S.TEXT_NEXT, S.DIALOG)
        nav.wait_for(after_upload, self.processing_timeout, "обработка видео", VIDEO_PROCESSING_TIMEOUT)
        if nav.button(S.TEXT_OK):
            nav.press(S.TEXT_OK, "OK (Reels)", 10)
        self.log.write("Video uploaded")
        self.keep_original_size()
        nav.press(S.TEXT_NEXT, "Далее (обрезка)", self.processing_timeout)
        nav.wait_for(lambda: S.STEP_EDIT.search(self._step()) or nav.button(S.TEXT_NEXT, S.DIALOG), 30, "шаг «Редактировать»")
        set_cover(page, cover, self.log)
        self._stop_point()
        nav.press(S.TEXT_NEXT, "Далее (редактирование)", self.processing_timeout)
        nav.wait_for(lambda: nav.button(S.TEXT_SHARE, S.DIALOG), self.processing_timeout, "шаг с подписью и «Поделиться»")
        set_caption(page, caption, self.log)
        # checks before Share (ТЗ §18)
        dialog_text = nav.dialog_title()
        if not S.STEP_FINAL.search(dialog_text):
            raise PublishError(PAGE_CHANGED, "не тот шаг создания Reel: " + dialog_text[:120])
        if username and username.lower() not in dialog_text.lower():
            raise PublishError(WRONG_ACCOUNT, f"в окне не аккаунт @{username}", retry=False)
        if S.SHARE_FAILED.search(nav.dialogs_text()):
            raise PublishError(PUBLISH_FAILED, "Instagram показывает ошибку до публикации")
        self._stop_point()
        on_share()  # from here on a retry could post twice: the queue knows Share was pressed
        nav.press(S.TEXT_SHARE, "Поделиться", 10)
        self.log.write("Publishing")

        def confirmed():
            text = nav.dialogs_text()
            if S.SHARE_DONE.search(text):
                return "ok"
            if S.SHARE_FAILED.search(text):
                raise PublishError(PUBLISH_FAILED, "Instagram: " + text[:150].replace("\n", " "), after_share=True)
            return None
        try:
            nav.wait_for(confirmed, self.share_timeout, "подтверждение публикации", PUBLISH_FAILED, poll=1.0)
        except PublishError as exc:
            exc.after_share = True
            if exc.code == PUBLISH_FAILED and "не дождался" in str(exc):
                exc.retry = False  # unknown outcome: a person checks the profile, no automatic second post
            raise
        self.log.write("Published successfully")
        self.close_dialog()
        return {"permalink": None}

    def keep_original_size(self) -> None:
        """Crop step: «Оригинал» (else 9:16) instead of Instagram's default square; checked on the preview."""
        nav, page = self.nav, self.page
        icon = nav.icon_link(S.ICON_CROP_SIZE)
        if icon is None or not icon.count():
            cand = [page.locator(f"svg[aria-label='{x}']").first for x in S.ICON_CROP_SIZE]
            cand = [c for c in cand if c.count()]
            icon = cand[0].locator("xpath=ancestor::*[@role='button' or self::button][1]") if cand else None
        if icon is None or not icon.count():
            raise PublishError(CROP_FAILED, "не нашёл выбор размера на шаге «Обрезать»", retry=False)
        icon.first.click()
        opt = nav.wait_for(lambda: nav.button(S.TEXT_ORIGINAL, S.DIALOG) or nav.button(S.TEXT_9_16, S.DIALOG), 10,
                           "варианты размера «Оригинал / 9:16»", CROP_FAILED)
        opt.click()
        page.wait_for_timeout(1200)
        ratio = page.evaluate(S.VISIBLE_RATIO_JS)
        if ratio is None or ratio > 0.7:
            raise PublishError(CROP_FAILED, f"превью не стало вертикальным (ширина/высота {ratio})", retry=False)
        self.log.write(f"Crop: original size (preview {ratio:.2f})")

    def _step(self) -> str:
        try:
            return self.page.locator(S.DIALOG).filter(visible=True).first.get_attribute("aria-label") or ""
        except Exception:
            return ""

    def close_dialog(self) -> None:
        """Leaves the create dialog (after success, or discarding an unfinished draft)."""
        try:
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(800)
            b = self.nav.button(S.TEXT_DISCARD)
            if b is not None:
                b.click()
        except Exception:
            pass
