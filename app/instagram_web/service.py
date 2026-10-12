"""Instagram AutoPoster service: connection state of the account (stage 2). The queue and publishing are
added on top of it in the next stages."""
from __future__ import annotations

import threading
from datetime import timedelta

from app.database.models import AppSetting, utcnow
from app.instagram_web import selectors as S
from app.instagram_web.browser.browser_manager import BrowserError, BrowserManager
from app.instagram_web.browser.profile_manager import ProfileManager
from app.instagram_web.config import IGConfig
from app.instagram_web.monitoring.logger import IGLog
from app.instagram_web.queue.queue_manager import IGQueue

DISCONNECTED, CONNECTING, READY, PUBLISHING, PAUSED, WAITING_USER, ERROR, STOPPING = (
    "DISCONNECTED", "CONNECTING", "READY", "PUBLISHING", "PAUSED", "WAITING_USER", "ERROR", "STOPPING")
ACCOUNT_KEY = "instagram_web.account"


class InstagramService:
    def __init__(self, settings, db, playwright_factory=None):
        self.s, self.db = settings, db
        self.cfg = IGConfig.from_settings(settings)
        self.log = IGLog(settings, self.cfg.debug)
        self.profile = ProfileManager(self.cfg.profile_dir(settings))
        self.browser = BrowserManager(self.profile, headless=self.cfg.headless and not self.cfg.debug,
                                      playwright_factory=playwright_factory, log=self.log)
        self._load_ui_settings()
        self.editor = None  # set by the web app (the factory's editor renders the «Автозалив» videos)
        self.queue = IGQueue(settings, db, self.cfg)
        self.queue.recover()
        self._lock = threading.Lock()
        self.state = DISCONNECTED if not self.account else READY
        self.message: str | None = None
        self.stop_requested = False
        self.current: int | None = None  # publication being published now
        self._pub_lock = threading.Lock()  # one Reel at a time, never parallel
        self._loop_stop = threading.Event()
        self._loop: threading.Thread | None = None

    # ---------- saved account (only the @username and when it was checked; the session lives in the profile) ----------
    @property
    def account(self) -> dict | None:
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, ACCOUNT_KEY)
            return json.loads(row.value) if row and row.value else None

    def _save_account(self, data: dict | None) -> None:
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, ACCOUNT_KEY) or AppSetting(key=ACCOUNT_KEY)
            row.value = json.dumps(data) if data else None
            s.add(row)

    # ---------- settings changed on the «Instagram» page (kept in the base, config.yaml stays as is) ----------
    UI_KEYS = {"max_posts_per_day": (0, 1000), "interval_minutes": (0, 1440), "window_start": None, "window_end": None}
    SETTINGS_KEY = "instagram_web.settings"

    def _load_ui_settings(self) -> None:
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, self.SETTINGS_KEY)
            data = json.loads(row.value) if row and row.value else {}
        for k, v in data.items():
            if k in self.UI_KEYS:
                setattr(self.cfg, k, v)

    def update_settings(self, values: dict) -> dict:
        import json
        import re
        from app.instagram_web.queue.queue_manager import QueueError
        from app.instagram_web.queue.scheduler import Planner
        clean = {}
        for k, v in values.items():
            if k not in self.UI_KEYS or v is None:
                continue
            rng = self.UI_KEYS[k]
            if rng:
                try:
                    v = int(v)
                except (TypeError, ValueError) as exc:
                    raise QueueError(f"{k}: нужно число", 400) from exc
                if not rng[0] <= v <= rng[1]:
                    raise QueueError(f"{k}: от {rng[0]} до {rng[1]}", 400)
            elif not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(v)):
                raise QueueError(f"{k}: время ЧЧ:ММ", 400)
            clean[k] = v
        with self.db.session() as s:
            row = s.get(AppSetting, self.SETTINGS_KEY) or AppSetting(key=self.SETTINGS_KEY)
            data = {**(json.loads(row.value) if row.value else {}), **clean}
            row.value = json.dumps(data)
            s.add(row)
        for k, v in clean.items():
            setattr(self.cfg, k, v)
        self.queue.planner = Planner(self.cfg)
        self.log.write(f"Settings changed: {clean}")
        return self.status()

    # ---------- connection ----------
    def connect(self) -> dict:
        """«Подключить Instagram»: opens the separate Chrome on Instagram; the owner signs in there by hand."""
        with self._lock:
            self.state, self.message = CONNECTING, "Войдите в Instagram в открывшемся окне Chrome, затем нажмите «Я вошёл»"
        try:
            self.browser.open(S.HOME)
        except BrowserError as exc:
            return self._fail(exc.code, str(exc))
        self.log.write("Connect: Chrome opened, waiting for the owner to sign in")
        return self.status()

    def confirm(self) -> dict:
        """«Я вошёл»: checks that this Chrome profile really is signed in, and reads the @username."""
        try:
            res = self.browser.check_auth()
        except BrowserError as exc:
            return self._fail(exc.code, str(exc))
        except Exception as exc:
            return self._fail("UNKNOWN_ERROR", f"не получилось проверить вход: {str(exc)[:200]}")
        if res["checkpoint"]:
            self.state, self.message = WAITING_USER, res["reason"]
            self.log.write("Instagram asks for a manual check (checkpoint)", "WARN")
        elif res["authorized"]:
            self._save_account({"username": res["username"], "checked_at": utcnow().isoformat() + "Z"})
            items = self.accounts
            for a in items:
                a["current"] = bool(res["username"]) and a["username"].lower() == res["username"].lower()
                if a["current"]:
                    a.pop("need_login", None)
            self._save_accounts(items)
            self.state, self.message = READY, None
            self.log.write(f"Instagram session OK: @{res['username'] or '?'}")
        else:
            self.state, self.message = DISCONNECTED, "Не удалось определить авторизацию: " + res["reason"]
            self.log.write("Instagram session not found", "WARN")
        return self.status()

    # ---------- linked accounts ----------
    ACCOUNTS_KEY = "instagram_web.accounts"

    @property
    def accounts(self) -> list[dict]:
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, self.ACCOUNTS_KEY)
            return json.loads(row.value) if row and row.value else []

    def _save_accounts(self, items: list[dict]) -> None:
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, self.ACCOUNTS_KEY) or AppSetting(key=self.ACCOUNTS_KEY)
            row.value = json.dumps(items)
            s.add(row)

    EXCLUDED_KEY = "instagram_web.excluded"

    @property
    def excluded(self) -> set[str]:
        """Accounts the owner closed for the factory (personal ones): no switching, no queue, no posts."""
        import json
        with self.db.session() as s:
            row = s.get(AppSetting, self.EXCLUDED_KEY)
            return {x.lower() for x in (json.loads(row.value) if row and row.value else [])}

    def set_excluded(self, username: str, excluded: bool) -> dict:
        import json
        names = self.excluded
        (names.add if excluded else names.discard)(username.lower().lstrip("@"))
        with self.db.session() as s:
            row = s.get(AppSetting, self.EXCLUDED_KEY) or AppSetting(key=self.EXCLUDED_KEY)
            row.value = json.dumps(sorted(names))
            s.add(row)
        self.log.write(f"Excluded accounts: {sorted(names)}")
        return self.status()

    def _check_allowed(self, username: str | None) -> None:
        from app.instagram_web.queue.queue_manager import QueueError
        if username and username.lower().lstrip("@") in self.excluded:
            raise QueueError(f"@{username} исключён из завода (личный аккаунт)", 403)

    def refresh_accounts(self) -> dict:
        """«↻ Обновить список»: the linked accounts from Instagram's «Переключиться» dialog (read only)."""
        from app.instagram_web.browser import accounts as acc
        try:
            items = self.browser.call(lambda: acc.list_accounts(self.browser._page()), timeout=120)
        except Exception as exc:
            return self._fail("PAGE_CHANGED", f"не получилось прочитать аккаунты: {str(exc)[:200]}")
        old = {a["username"]: a for a in self.accounts}
        for a in items:
            if old.get(a["username"], {}).get("need_login"):
                a["need_login"] = True
        self._save_accounts(items)
        cur = next((a["username"] for a in items if a["current"]), None)
        if cur:
            self._save_account({"username": cur, "checked_at": utcnow().isoformat() + "Z"})
        self.log.write(f"Accounts: {', '.join('@' + a['username'] for a in items)}")
        return self.status()

    def switch_account(self, username: str) -> dict:
        from app.instagram_web.queue.queue_manager import QueueError
        self._check_allowed(username)
        if self._pub_lock.locked():
            raise QueueError("сейчас идёт публикация — переключитесь после неё")
        res = self.browser.call(self._switch_in_browser, username, timeout=180)
        if not res["ok"]:
            self.message = res["reason"]
            if res.get("need_login"):
                items = self.accounts
                for a in items:
                    if a["username"].lower() == username.lower():
                        a["need_login"] = True
                self._save_accounts(items)
        return self.status()

    def manual_login(self, username: str) -> dict:
        """«Войти вручную»: Chrome shows Instagram's sign-in for this account; the person types the password,
        then «Я вошёл» (confirm) reads who is active."""
        self._check_allowed(username)
        from app.instagram_web.browser import accounts as acc
        ok = self.browser.call(lambda: acc.open_login_for(self.browser._page(), username), timeout=120)
        self.state = CONNECTING if ok else self.state
        self.message = (f"Введите пароль @{username} в окне Chrome, затем нажмите «Я вошёл»" if ok
                        else f"@{username} нет среди связанных аккаунтов")
        return self.status()

    def _switch_in_browser(self, username: str) -> dict:
        """Browser thread: switch and verify; remembers who is active now."""
        if username.lower().lstrip("@") in self.excluded:
            return {"ok": False, "username": None, "reason": f"@{username} исключён из завода"}
        from app.instagram_web.browser import accounts as acc
        self.browser._start()
        res = acc.switch_to(self.browser._page(), username, self.browser._username)
        if res.get("username"):
            self._save_account({"username": res["username"], "checked_at": utcnow().isoformat() + "Z"})
            items = self.accounts
            for a in items:
                a["current"] = a["username"].lower() == res["username"].lower()
                if a["current"] and res["ok"]:
                    a.pop("need_login", None)
            self._save_accounts(items)
        self.log.write(f"Switch to @{username}: {'OK' if res['ok'] else res['reason']}")
        if res["ok"]:
            self.message = None
        return res

    def open_chrome(self) -> dict:
        try:
            self.browser.open(S.HOME)
        except BrowserError as exc:
            return self._fail(exc.code, str(exc))
        return self.status()

    def close_chrome(self) -> dict:
        self.browser.close()
        return self.status()

    def disconnect(self) -> dict:
        """«Отключить»: closes Chrome and deletes the profile with the session (the account itself is not touched)."""
        self.browser.close()
        self.profile.forget()
        self._save_account(None)
        self.state, self.message = DISCONNECTED, None
        self.log.write("Disconnected: profile deleted")
        return self.status()

    def _fail(self, code: str, message: str) -> dict:
        self.state, self.message = ERROR, message
        self.log.error("browser", code, message)
        return self.status()

    def status(self) -> dict:
        acc = self.account
        state = PAUSED if self.state == READY and self.queue.paused else self.state
        excl = self.excluded
        accs = [{**a, "excluded": a["username"].lower() in excl} for a in self.accounts]
        return {"state": state, "paused": self.queue.paused, "publishing": self.current, "accounts": accs,
                "autopost": self.autopost, "message": self.message, "chrome_open": self.browser.is_open,
                "account": acc, "profile": str(self.profile.path.relative_to(self.s.root))
                if self.profile.path.is_relative_to(self.s.root) else str(self.profile.path),
                "settings": {k: getattr(self.cfg, k) for k in ("autopost", "window_start", "window_end",
                             "interval_minutes", "max_posts_per_day", "max_attempts", "auto_enqueue", "debug")}}

    # ---------- publishing one Reel ----------
    def publish_now(self, pid: int, wait: bool = False) -> dict:
        """«Опубликовать сейчас»: starts the publication in the background (one at a time)."""
        from app.database.models import Publication, PublicationStatus as PS
        from app.instagram_web.queue.queue_manager import PLATFORM, QueueError
        with self.db.session() as s:
            p = s.get(Publication, pid)
            if p is None or p.platform != PLATFORM:
                raise QueueError("нет такой публикации", 404)
            if p.status not in (PS.QUEUED, PS.FAILED, PS.PAUSED):
                raise QueueError("опубликовать сейчас можно только ролик из очереди")
        if not self.account:
            raise QueueError("сначала подключите Instagram")
        with self.db.session() as s:
            self._check_allowed(s.get(Publication, pid).account)
        if self._pub_lock.locked():
            raise QueueError("сейчас уже публикуется другой ролик — дождитесь окончания")
        t = threading.Thread(target=self._publish, args=(pid,), daemon=True, name=f"ig-publish-{pid}")
        t.start()
        if wait:
            t.join()
        return self.status()

    def _publish(self, pid: int) -> None:
        from app.database.models import ContentItem, Publication, PublicationStatus as PS, utcnow as now
        from app.instagram_web.automation.errors import NeedsPerson, PublishError, UNKNOWN_ERROR
        from app.instagram_web.queue.queue_manager import _event
        if not self._pub_lock.acquire(blocking=False):
            return
        self.stop_requested = False
        name = f"#{pid}"
        try:
            with self.db.session() as s:
                p = s.get(Publication, pid)
                item = s.get(ContentItem, p.content_id)
                video = self.queue.video_of(s, item) if item else None
                caption, cover = p.caption or "", p.cover_path
                if video is None:
                    p.status, p.error_code, p.error, p.failed_at = PS.WAITING_USER, "UPLOAD_FAILED", "файл ролика не найден", now()
                    return
                name = video.name
                p.status, p.started_at, p.attempts = PS.PROCESSING, now(), (p.attempts or 0) + 1
                p.error = p.error_code = None
                _event(p, f"публикация, попытка {p.attempts}")
                attempts = p.attempts
            self.current, self.state, self.message = pid, PUBLISHING, None
            self.log.write(f"Starting {name}")
            with self.db.session() as s:
                target = s.get(Publication, pid).account
            current = (self.account or {}).get("username")
            username = target if target and target != self.cfg.account else current
            if username and username.lower() in self.excluded:
                from app.instagram_web.automation.errors import PublishError
                self._after_error(pid, PublishError("WRONG_ACCOUNT", f"@{username} исключён из завода", retry=False), name, attempts)
                return
            try:
                self.browser.call(self._run_in_browser, pid, str(video), caption, cover, username, timeout=1200)
            except NeedsPerson as exc:
                self._after_error(pid, exc, name, attempts)
                self.queue.set_paused(True)
                self.state, self.message = WAITING_USER, str(exc)
                return
            except PublishError as exc:
                self._after_error(pid, exc, name, attempts)
                return
            except Exception as exc:
                self._after_error(pid, PublishError(UNKNOWN_ERROR, str(exc)[:300]), name, attempts)
                return
            with self.db.session() as s:
                p = s.get(Publication, pid)
                p.status, p.published_at, p.error, p.error_code = PS.PUBLISHED, now(), None, None
                _event(p, "опубликовано (Instagram подтвердил)")
                item = s.get(ContentItem, p.content_id)
                if item and not item.posted_at:
                    item.posted_at, item.posted_note = now(), "Instagram (автопостер)"
            self.state = PAUSED if self.queue.paused else READY
        finally:
            self.current = None
            if self.state == STOPPING:
                self.state = PAUSED
            self._pub_lock.release()

    def _run_in_browser(self, pid, video, caption, cover, username):
        """Browser thread: check the session, then one Reel through the create dialog."""
        from app.database.models import Publication, utcnow as now
        from app.instagram_web.automation.errors import AUTH_REQUIRED, PublishError
        from app.instagram_web.automation.publisher import Publisher
        self.browser._start()
        page = self.browser._page()
        if S.SESSION_COOKIE not in {c["name"] for c in self.browser.ctx.cookies(S.HOME)}:
            raise PublishError(AUTH_REQUIRED, "Instagram не авторизован: подключите его заново", retry=False)
        current = (self.account or {}).get("username")
        if username and current and username.lower() != current.lower():
            from app.instagram_web.automation.errors import WRONG_ACCOUNT
            res = self._switch_in_browser(username)
            if not res["ok"]:
                raise PublishError(WRONG_ACCOUNT, f"не получилось переключиться на @{username}: {res['reason']}", retry=False)

        def on_share():
            with self.db.session() as s:
                s.get(Publication, pid).shared_at = now()
        pub = Publisher(page, self.log, processing_timeout=self.cfg.processing_timeout_s,
                        share_timeout=self.cfg.share_timeout_s,
                        stop_requested=lambda: self.stop_requested)
        try:
            try:
                return pub.run(video, caption, cover, username, on_share)
            except Exception as exc:
                with self.db.session() as s:
                    shared = s.get(Publication, pid).shared_at
                if ("has been closed" in str(exc) or "Target closed" in str(exc)) and not shared:
                    # the Chrome window was closed by hand: open it again and start over (nothing was posted)
                    self.log.write("Chrome window was closed, starting again")
                    self.browser._close()
                    self.browser._start()
                    page = self.browser._page()
                    pub = Publisher(page, self.log, processing_timeout=self.cfg.processing_timeout_s,
                                    share_timeout=self.cfg.share_timeout_s, stop_requested=lambda: self.stop_requested)
                    return pub.run(video, caption, cover, username, on_share)
                raise
        except Exception:
            self.log.screenshot(page, video)
            pub.close_dialog()
            raise

    def _after_error(self, pid: int, exc, name: str, attempts: int) -> None:
        from app.database.models import Publication, PublicationStatus as PS, utcnow as now
        from app.instagram_web.automation.errors import AUTH_REQUIRED, SESSION_EXPIRED, STOPPED
        from app.instagram_web.queue.queue_manager import _event
        code = getattr(exc, "code", "UNKNOWN_ERROR")
        self.log.error(name, code, str(exc))
        with self.db.session() as s:
            p = s.get(Publication, pid)
            p.error, p.error_code, p.failed_at = str(exc)[:1000], code, now()
            if code == STOPPED or code == "WAITING_USER":
                # nothing was posted: back to the queue, this attempt does not count
                p.status, p.attempts = PS.QUEUED, max(0, (p.attempts or 1) - 1)
            elif code in (AUTH_REQUIRED, SESSION_EXPIRED):
                p.status, p.attempts = PS.QUEUED, max(0, (p.attempts or 1) - 1)
            elif getattr(exc, "after_share", False) and not exc.retry:
                p.status = PS.WAITING_USER
                p.error = (str(exc) + ". «Поделиться» было нажато: проверьте профиль, опубликован ли ролик, "
                           "и только потом нажимайте «Повторить»")[:1000]
            elif exc.retry and self.cfg.retry_failed and attempts < self.cfg.max_attempts:
                p.status = PS.FAILED
                p.next_retry_at = now() + timedelta(minutes=5 * 3 ** (attempts - 1))
            else:
                p.status = PS.WAITING_USER
            _event(p, f"ошибка {code}: {str(exc)[:200]}")
        if code in (AUTH_REQUIRED, SESSION_EXPIRED):
            self.queue.set_paused(True)
            self.state, self.message = ERROR, str(exc)
        elif code == STOPPED:
            self.state = PAUSED
        else:
            self.state = PAUSED if self.queue.paused else READY

    def stop(self) -> dict:
        """«Остановить»: no new posts; the current one finishes its safe step (before Share it is dropped
        back to the queue, after Share it waits for Instagram's answer)."""
        self.queue.set_paused(True)
        if self._pub_lock.locked():
            self.stop_requested = True
            self.state = STOPPING
        else:
            self.state = PAUSED
        self.log.write("Stop requested")
        return self.status()

    def resume(self) -> dict:
        self.queue.set_paused(False)
        self.stop_requested = False
        if self.state in (PAUSED, STOPPING, WAITING_USER):
            self.state = READY if self.account else DISCONNECTED
        return self.status()

    # ---------- autoposting: the queue goes out one by one (interval, window, daily limit) ----------
    AUTOPOST_KEY = "instagram_web.autopost"

    @property
    def autopost(self) -> bool:
        with self.db.session() as s:
            row = s.get(AppSetting, self.AUTOPOST_KEY)
            return bool(row and row.value == "1")

    def set_autopost(self, on: bool) -> dict:
        with self.db.session() as s:
            row = s.get(AppSetting, self.AUTOPOST_KEY) or AppSetting(key=self.AUTOPOST_KEY)
            row.value = "1" if on else "0"
            s.add(row)
        if on:
            self.queue.set_paused(False)
            self.start_loop()
        self.log.write("Autopost " + ("ON" if on else "OFF"))
        return self.status()

    def start_loop(self) -> None:
        if self._loop is None or not self._loop.is_alive():
            self._loop_stop.clear()
            self._loop = threading.Thread(target=self._autopost_loop, daemon=True, name="ig-autopost")
            self._loop.start()

    def autopost_tick(self, now=None) -> int | None:
        """Publishes the first due Reel (if any); returns its id. One at a time."""
        from app.database.models import utcnow as _now
        from app.instagram_web import autofill
        try:
            autofill.tick(self.db, self.queue)  # «Автозалив»: rendered videos go into their account's queue
        except Exception as exc:
            self.log.write(f"autofill: {exc}", "ERROR")
        if not self.autopost or self.queue.paused or not self.account or self._pub_lock.locked():
            return None
        if self.state in (WAITING_USER, ERROR, STOPPING):
            return None
        now = now or _now()
        with self.db.session() as s:
            plan = self.queue.plan(s, now)
        due = sorted((t, pid) for pid, t in plan.items() if t <= now)
        if not due:
            return None
        pid = due[0][1]
        self._publish(pid)
        return pid

    def _autopost_loop(self) -> None:
        wait = 10
        while not self._loop_stop.wait(wait):
            try:
                # right after a post the next one starts at once (interval 0 = one after another)
                wait = 2 if self.autopost_tick() else 10
            except Exception as exc:
                self.log.write(f"autopost loop: {exc}", "ERROR")
                wait = 10

    def publish_all_ready(self, account: str | None, caption: str | None, use_own: bool = True,
                          ids: list[int] | None = None) -> dict:
        """«🚀 Опубликовать все из Готовых»: everything in «Готовые» that is not queued or published yet goes
        into the queue (in the order of «Готовые»), autoposting starts. With ids: only those, in that order."""
        from sqlalchemy import select
        from app.database.models import ContentItem
        from app.processing import ready as ready_mod
        from app.instagram_web.queue.queue_manager import QueueError
        self._check_allowed(account)
        queued = self.queue.queued_content_ids()
        with self.db.session() as s:
            items = [i for i in ready_mod.items(s) if not i.posted_at and i.id not in queued]
            captions = {i.id: (i.post_description if use_own and (i.post_description or "").strip() else caption or "")
                        for i in items}
            if ids is not None:
                avail = {i.id for i in items}
                ids = [int(i) for i in dict.fromkeys(ids) if int(i) in avail]
                if not ids:
                    raise QueueError("выбранные ролики уже в очереди или опубликованы", 400)
            else:
                ids = [i.id for i in reversed(items)]  # oldest sent to «Готовые» first
        res = self.queue.add(ids, captions=captions, account=account or (self.account or {}).get("username"))
        self.set_autopost(True)
        lst = self.queue.listing()
        etas = [i["eta"] for i in lst["items"] if i["id"] in set(res["added"]) and i["eta"]]
        return {**res, "last_eta": max(etas) if etas else None, "status": self.status()}

    def shutdown(self) -> None:
        self._loop_stop.set()
        self.browser.shutdown()
