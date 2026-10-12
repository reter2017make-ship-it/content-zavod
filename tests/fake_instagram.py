"""A tiny stand-in for Instagram Web's «create» dialog, enough for the publisher: links, buttons, dialogs,
file inputs, the caption editor. Scenario switches make Instagram slow, ask for a check or fail."""
from __future__ import annotations

from app.instagram_web import selectors as S


class El:
    def __init__(self, page, kind, text="", aria=None, on_click=None, accept=None):
        self.page, self.kind, self.text, self.aria, self.on_click, self.accept = page, kind, text, aria, on_click, accept


class Loc:
    def __init__(self, page, items):
        self.page, self.items = page, items

    def filter(self, **kw):
        return self

    def count(self):
        return len(self.items)

    def nth(self, i):
        return Loc(self.page, [self.items[i]])

    @property
    def first(self):
        return Loc(self.page, self.items[:1])

    def _one(self):
        if not self.items:
            raise TimeoutError("no element")
        return self.items[0]

    def inner_text(self, timeout=None):
        if self.items and self.items[0].kind == "body":
            return self.page.body_text()
        return self._one().text if self.items[0].kind != "caption" else self.page.caption

    def get_attribute(self, name):
        e = self._one()
        return e.aria if name == "aria-label" else e.accept

    def click(self):
        e = self._one()
        self.page.clicks.append(e.text or e.kind)
        if e.on_click:
            e.on_click()

    def set_input_files(self, path):
        e = self._one()
        self.page.files.append((e.kind, str(path)))
        e.on_click and e.on_click()

    def wait_for(self, state=None, timeout=None):
        self._one()

    def locator(self, sel):
        if sel.startswith("xpath=ancestor"):
            return self
        return self.page.locator(sel, scoped=True)

    def input_value(self):
        return ""


class Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        if key == "Escape":
            self.page.escape()
        elif key == "Shift+Enter":
            self.page.caption += "\n"
        elif key == "Backspace":
            self.page.caption = ""

    def insert_text(self, text):
        self.page.caption += text


class FakeInstagram:
    """step: home → menu → dialog → reels_info → crop → edit → final → sharing → done"""

    def __init__(self, username="tester", **scenario):
        self.username, self.sc = username, scenario
        self.url, self.step, self.caption = "https://www.instagram.com/", "home", ""
        self.clicks, self.files, self.shared, self.polls = [], [], 0, 0
        self.crop_menu, self.crop = False, "1:1"
        self.keyboard = Keyboard(self)

    # ---------- what the publisher sees ----------
    def body_text(self):
        return "лента"

    def _dialog_text(self):
        if self.sc.get("checkpoint_at") == self.step:
            return "Подтвердите, что это вы"
        return {"dialog": "Создание публикации Выбрать на компьютере", "reels_info": "Теперь видеопубликациями можно делиться OK",
                "crop": "Обрезать Далее", "edit": "Редактировать Далее Фото обложки", "final": f"Новое видео Reels Поделиться {self.username}",
                "sharing": "Публикация…", "done": "Видео Reels опубликовано", "failed": "Не удалось поделиться публикацией"}.get(self.step, "")

    def locator(self, sel, scoped=False):
        st, E = self.step, lambda *a, **k: El(self, *a, **k)
        if sel == "body":
            return Loc(self, [E("body")])
        if sel == S.DIALOG:
            if st in ("home", "menu"):
                return Loc(self, [])
            aria = {"crop": "Обрезать", "edit": "Редактировать"}.get(st, "Создание публикации")
            return Loc(self, [E("dialog", self._dialog_text(), aria=aria)])
        if sel == "a, [role=link]":
            links = [E("link", "Главная"), E("link", "Создать", on_click=self._menu)]
            if st == "menu":
                links.append(E("link", "Публикация", on_click=self._open_dialog))
            return Loc(self, links)
        if sel.startswith("svg[aria-label="):
            if st == "crop" and "размер" in sel:
                return Loc(self, [E("icon", on_click=lambda: setattr(self, "crop_menu", True))])
            return Loc(self, [])
        if sel == "[role=button], button" and st == "crop" and self.crop_menu:
            return Loc(self, [E("button", t, on_click=(lambda t=t: setattr(self, "crop", t))) for t in ("Оригинал", "1:1", "9:16", "16:9")]
                       + [E("button", "Далее", on_click=self._edit)])
        if sel == "[role=button], button":
            b = {"reels_info": [("OK", self._crop)], "crop": [("Далее", self._edit)], "edit": [("Далее", self._final)],
                 "final": [("Поделиться", self._share)]}.get(st, [])
            if st == "processing":
                b = []
            if self.sc.get("checkpoint_at") == st:
                b = []
            return Loc(self, [E("button", t, on_click=f) for t, f in b])
        if sel == S.FILE_INPUT:
            return Loc(self, [E("file", accept="video/mp4", on_click=self._uploaded)] if st == "dialog" else
                       [E("file", accept="image/jpeg", on_click=lambda: None)] if st == "edit" else [])
        if sel == S.COVER_INPUT:
            return Loc(self, [E("cover", accept="image/jpeg")] if st == "edit" else [])
        if sel == S.CAPTION_BOX:
            return Loc(self, [E("caption")] if st == "final" else [])
        return Loc(self, [])

    # ---------- transitions ----------
    def _menu(self):
        self.step = "dialog" if self.sc.get("no_menu") else "menu"

    def _open_dialog(self):
        self.step = "dialog"

    def _uploaded(self):
        self.step = "processing" if self.sc.get("never_processed") else "reels_info"

    def _crop(self):
        self.step = "crop"

    def _edit(self):
        self.step = "edit"

    def _final(self):
        self.step = "final"

    def _share(self):
        self.shared += 1
        self.step = "failed" if self.sc.get("share_fails") else ("sharing" if self.sc.get("never_confirms") else "done")

    def escape(self):
        if self.step not in ("done", "home"):
            self.step = "discard"
        else:
            self.step = "home"

    # ---------- page API ----------
    def goto(self, url, **kw):
        self.url = url
        if self.sc.get("logged_out"):
            self.url = "https://www.instagram.com/accounts/login/"
        self.step = "home"

    def wait_for_timeout(self, ms):
        self.polls += 1

    def evaluate(self, js, args=None):
        if args is None:  # the visible preview ratio of the crop step
            if self.sc.get("crop_stuck"):
                return 1.0
            return 0.5625 if self.crop in ("Оригинал", "9:16") else 1.0
        if not self.sc.get("paste_ignored"):
            self.caption = args[1]
        return True

    def wait_for_load_state(self, *a, **k):
        pass

    def is_closed(self):
        return False

    def bring_to_front(self):
        pass

    def screenshot(self, path, full_page=False):
        open(path, "wb").write(b"png")
