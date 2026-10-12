"""Publication queue: enqueue (only with a person's rights confirmation), limits and pauses, retries without
duplicate posts, dry-run plans, account checks and token refresh. Used by the web worker and the CLI."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError

from app.config.envfile import set_env_values
from app.database.models import (
    PUBLISHABLE_RIGHTS, AppSetting, ContentItem, Publication, PublicationStatus as PS, Status, Variant, utcnow,
)
from app.logging_setup import log_event
from app.publishing.instagram import REQUIRED_PERMISSIONS, GraphError, InstagramClient
from app.publishing.validate import caption_problems, video_problems

log = logging.getLogger("publishing")

LIVE = (PS.QUEUED, PS.PROCESSING, PS.FAILED, PS.WAITING_USER)
EDITABLE = (PS.QUEUED, PS.FAILED, PS.WAITING_USER)


class QueueError(ValueError):
    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.http_status = http_status


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _event(pub: Publication, text: str) -> None:
    rows = json.loads(pub.log) if pub.log else []
    rows.append({"at": _iso(utcnow()), "event": text})
    pub.log = json.dumps(rows[-50:], ensure_ascii=False)


class PublishQueue:
    PLATFORM = "instagram"

    def __init__(self, settings, db, transport: httpx.BaseTransport | None = None, sleep=None):
        self.s, self.db = settings, db
        self.cfg = settings.get("publishing", {}) or {}
        self.ig = self.cfg.get("instagram", {}) or {}
        self.tz = ZoneInfo(self.cfg.get("timezone", "Europe/Moscow"))
        self.transport = transport
        self._sleep = sleep
        self._run_lock = threading.Lock()

    # ---------- runtime switches (stored in the DB, changed from the UI) ----------
    def _setting(self, key: str, default: str | None = None) -> str | None:
        try:
            with self.db.session() as s:
                row = s.get(AppSetting, key)
                return row.value if row and row.value is not None else default
        except OperationalError:  # read-only dry-run on a database made before stage 4
            return default

    def _set(self, key: str, value: str | None) -> None:
        with self.db.session() as s:
            row = s.get(AppSetting, key) or AppSetting(key=key)
            row.value = value
            s.add(row)

    @property
    def paused(self) -> bool:
        return self._setting("publishing.paused", "0") == "1"

    def set_paused(self, paused: bool) -> None:
        self._set("publishing.paused", "1" if paused else "0")
        log_event(log, "PUBLISH", "PAUSED" if paused else "RESUMED")

    @property
    def live(self) -> bool:
        """False = dry-run: the whole path is checked, nothing is sent to Instagram."""
        default = "dry_run" if self.ig.get("dry_run", True) else "live"
        return self._setting("instagram.mode", default) == "live"

    def set_live(self, live: bool) -> None:
        if live:
            acc = self.account_state()
            if not acc.get("ok"):
                raise QueueError("сначала подключите Instagram: «Проверить подключение» должно пройти без ошибок")
        self._set("instagram.mode", "live" if live else "dry_run")
        log_event(log, "PUBLISH", "LIVE" if live else "DRY_RUN", platform=self.PLATFORM)

    # ---------- credentials ----------
    @property
    def token(self) -> str:
        return self.s.env("META_ACCESS_TOKEN")

    @property
    def account_id(self) -> str:
        return self.s.env("INSTAGRAM_ACCOUNT_ID")

    def missing(self) -> list[str]:
        out = []
        if not self.token:
            out.append("META_ACCESS_TOKEN")
        if not self.account_id:
            out.append("INSTAGRAM_ACCOUNT_ID")
        return out

    def client(self) -> InstagramClient:
        return InstagramClient(self.token, self.account_id, self.ig.get("api_version", "v25.0"),
                               transport=self.transport)

    def account_state(self) -> dict:
        raw = self._setting("instagram.account")
        state = json.loads(raw) if raw else {}
        state["missing"] = self.missing()
        if state["missing"]:
            state["ok"] = False
        state["can_refresh"] = bool(self.s.env("META_APP_ID") and self.s.env("META_APP_SECRET"))
        return state

    def save_credentials(self, token: str | None, account_id: str | None) -> None:
        values = {}
        if token:
            values["META_ACCESS_TOKEN"] = token.strip()
        if account_id:
            if not account_id.strip().isdigit():
                raise QueueError("ID аккаунта Instagram состоит из цифр", 400)
            values["INSTAGRAM_ACCOUNT_ID"] = account_id.strip()
        if values:
            set_env_values(self.s.env_file, values)
            self._set("instagram.account", None)
            log_event(log, "INSTAGRAM", "CREDENTIALS_SAVED", fields=",".join(values))

    def check_connection(self) -> dict:
        """«Проверить подключение»: token, account, permissions, publishing availability."""
        steps: list[dict] = []
        state: dict = {"checked_at": _iso(utcnow()), "steps": steps}

        def step(name: str, ok: bool, detail: str = "") -> bool:
            steps.append({"name": name, "ok": ok, "detail": detail})
            return ok

        if self.missing():
            step("Ключи в .env", False, "не хватает: " + ", ".join(self.missing()))
            state["ok"] = False
            self._set("instagram.account", json.dumps(state, ensure_ascii=False))
            return self.account_state()
        cl = self.client()
        try:
            try:
                me = cl.me()
                step("Токен действует", True, me.get("name") or me.get("id", ""))
            except GraphError as exc:
                step("Токен действует", False, str(exc))
                raise
            if self.s.env("META_APP_ID") and self.s.env("META_APP_SECRET"):
                try:
                    info = cl.debug_token(self.s.env("META_APP_ID"), self.s.env("META_APP_SECRET"))
                    exp = int(info.get("expires_at") or 0)
                    state["token_expires_at"] = _iso(datetime.fromtimestamp(exp, timezone.utc).replace(tzinfo=None)) if exp else None
                    state["token_type"] = info.get("type")
                except GraphError as exc:
                    state["token_expires_note"] = f"срок токена не узнать: {exc}"
            try:
                perms = cl.permissions()
                lacking = [p for p in REQUIRED_PERMISSIONS if perms.get(p) != "granted"]
                step("Разрешения", not lacking, "не хватает: " + ", ".join(lacking) if lacking
                     else ", ".join(REQUIRED_PERMISSIONS))
            except GraphError as exc:
                step("Разрешения", False, f"не удалось прочитать: {exc}")
            try:
                acc = cl.account()
                state.update(username=acc.get("username"), name=acc.get("name"),
                             followers=acc.get("followers_count"), picture=acc.get("profile_picture_url"))
                step("Аккаунт Instagram найден", True, "@" + (acc.get("username") or acc.get("id", "")))
            except GraphError as exc:
                step("Аккаунт Instagram найден", False, str(exc))
                raise
            try:
                used, total = cl.publishing_limit()
                state.update(quota_usage=used, quota_total=total)
                step("Публикация Reels доступна", True, f"использовано {used} из {total or 100} за 24 ч")
            except GraphError as exc:
                step("Публикация Reels доступна", False, str(exc))
        except GraphError:
            pass
        finally:
            cl.close()
        state["ok"] = all(x["ok"] for x in steps)
        self._set("instagram.account", json.dumps(state, ensure_ascii=False))
        log_event(log, "INSTAGRAM", "CHECK_OK" if state["ok"] else "CHECK_FAILED",
                  error=None if state["ok"] else "; ".join(f"{x['name']}: {x['detail']}" for x in steps if not x["ok"]))
        return self.account_state()

    def find_accounts(self) -> list[dict]:
        if not self.token:
            raise QueueError("нужен токен META_ACCESS_TOKEN", 400)
        cl = self.client()
        try:
            return cl.find_accounts()
        except GraphError as exc:
            raise QueueError(f"Meta ответила: {exc}", 502) from exc
        finally:
            cl.close()

    def refresh_token(self, force: bool = False) -> dict:
        """Exchanges the long-lived user token for a fresh one and writes it to .env."""
        app_id, secret = self.s.env("META_APP_ID"), self.s.env("META_APP_SECRET")
        if not (app_id and secret):
            raise QueueError("для обновления токена нужны META_APP_ID и META_APP_SECRET в .env", 400)
        if not self.token:
            raise QueueError("нужен токен META_ACCESS_TOKEN", 400)
        state = self.account_state()
        exp = state.get("token_expires_at")
        if not force and exp is None and state.get("token_type"):
            return {"refreshed": False, "reason": "токен бессрочный, обновлять не нужно"}
        cl = self.client()
        try:
            data = cl.exchange_token(app_id, secret)
        except GraphError as exc:
            log_event(log, "INSTAGRAM", "TOKEN_REFRESH_FAILED", error=str(exc))
            raise QueueError(f"Meta не обновила токен: {exc}", 502) from exc
        finally:
            cl.close()
        set_env_values(self.s.env_file, {"META_ACCESS_TOKEN": data["access_token"]})
        expires = utcnow() + timedelta(seconds=int(data["expires_in"])) if data.get("expires_in") else None
        raw = self._setting("instagram.account")
        st = json.loads(raw) if raw else {}
        st["token_expires_at"], st["token_refreshed_at"] = _iso(expires), _iso(utcnow())
        self._set("instagram.account", json.dumps(st, ensure_ascii=False))
        log_event(log, "INSTAGRAM", "TOKEN_REFRESHED", expires=_iso(expires))
        return {"refreshed": True, "expires_at": _iso(expires)}

    def refresh_token_if_due(self) -> None:
        exp = self.account_state().get("token_expires_at")
        if not exp or not self.account_state().get("can_refresh"):
            return
        left = datetime.fromisoformat(exp.rstrip("Z")) - utcnow()
        if left < timedelta(days=float(self.ig.get("token_refresh_days_before", 10))):
            try:
                self.refresh_token()
            except QueueError as exc:
                log.warning("token refresh: %s", exc)

    # ---------- queue ----------
    @staticmethod
    def _video_path(s, item_id: int) -> str | None:
        v = s.scalars(select(Variant).where(Variant.content_id == item_id).order_by(Variant.id.desc())).first()
        return v.path if v else None

    def enqueue(self, item_id: int, caption: str, scheduled_at: datetime | None, rights_confirmed: bool,
                note: str | None = None) -> int:
        caption = (caption or "").strip()
        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None:
                raise QueueError("нет такого ролика", 404)
            if item.rights_status not in PUBLISHABLE_RIGHTS:
                raise QueueError(f"права {item.rights_status.value}: сначала одобрите ролик на ручной проверке")
            if item.status != Status.READY_TO_PUBLISH:
                raise QueueError(f"ролик не готов к публикации (статус {item.status.value})")
            if not rights_confirmed:
                raise QueueError("подтвердите права на ролик: без ручного одобрения в очередь нельзя", 400)
            problems = caption_problems(caption, self.ig)
            if problems:
                raise QueueError("; ".join(problems), 400)
            path = self._video_path(s, item_id)
            if not path or not Path(path).is_file():
                raise QueueError("файла готового ролика нет")
            dup = s.scalar(select(Publication.id).where(
                Publication.content_id == item_id, Publication.platform == self.PLATFORM,
                Publication.status != PS.CANCELLED))
            if dup:
                raise QueueError(f"ролик уже в очереди или опубликован (публикация #{dup})")
            pub = Publication(content_id=item_id, platform=self.PLATFORM, caption=caption, video_path=path,
                              scheduled_at=scheduled_at, status=PS.QUEUED, rights_confirmed_at=utcnow(),
                              rights_note=note, account=self.account_id or None)
            _event(pub, f"в очереди; права подтверждены вручную ({item.rights_status.value})")
            s.add(pub)
            item.post_description = caption
            s.flush()
            pid = pub.id
        log_event(log, "PUBLISH", "QUEUED", item_id, publication=pid, platform=self.PLATFORM)
        return pid

    def update(self, pub_id: int, caption: str | None = None, scheduled_at: datetime | None = None,
               clear_schedule: bool = False) -> None:
        with self.db.session() as s:
            pub = self._get(s, pub_id)
            if pub.status not in EDITABLE:
                raise QueueError(f"публикацию в статусе {pub.status.value} менять нельзя")
            if caption is not None:
                caption = caption.strip()
                problems = caption_problems(caption, self.ig)
                if problems:
                    raise QueueError("; ".join(problems), 400)
                if caption != pub.caption:
                    pub.caption = caption
                    pub.content.post_description = caption
                    # The caption is fixed at container creation: an unpublished container is dropped (expires in 24 h).
                    pub.container_id, pub.uploaded_at = None, None
                    _event(pub, "описание изменено")
            if scheduled_at is not None or clear_schedule:
                pub.scheduled_at = scheduled_at
                _event(pub, "время: " + (_iso(scheduled_at) if scheduled_at else "как можно скорее"))
            pub.dry_run_at = pub.dry_run_report = None

    def cancel(self, pub_id: int) -> None:
        with self.db.session() as s:
            pub = self._get(s, pub_id)
            if pub.status not in EDITABLE:
                raise QueueError(f"публикацию в статусе {pub.status.value} отменить нельзя")
            pub.status = PS.CANCELLED
            _event(pub, "отменено вручную")
        log_event(log, "PUBLISH", "CANCELLED", publication=pub_id)

    def retry(self, pub_id: int) -> None:
        with self.db.session() as s:
            pub = self._get(s, pub_id)
            if pub.status not in (PS.FAILED, PS.WAITING_USER):
                raise QueueError("повторить можно только публикацию с ошибкой")
            pub.status, pub.attempts, pub.next_retry_at = PS.QUEUED, 0, None
            pub.dry_run_at = pub.dry_run_report = None
            _event(pub, "повтор запрошен вручную")

    def publish_now(self, pub_id: int) -> None:
        """Drops the planned time: the publication goes as soon as limits and pauses allow."""
        self.update(pub_id, clear_schedule=True)

    @staticmethod
    def _get(s, pub_id: int) -> Publication:
        pub = s.get(Publication, pub_id)
        if pub is None:
            raise QueueError("нет такой публикации", 404)
        return pub

    def recent_descriptions(self, limit: int = 10) -> list[str]:
        with self.db.session() as s:
            rows = list(s.scalars(select(Publication.caption).order_by(Publication.id.desc()).limit(100)))
            rows += list(s.scalars(select(ContentItem.post_description)
                                   .where(ContentItem.post_description.is_not(None))
                                   .order_by(ContentItem.updated_at.desc()).limit(100)))
        out: list[str] = []
        for c in rows:
            c = (c or "").strip()
            if c and c not in out:
                out.append(c)
        return out[:limit]

    # ---------- limits ----------
    def _local_day_start(self, now_utc: datetime) -> datetime:
        local = now_utc.replace(tzinfo=timezone.utc).astimezone(self.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).replace(tzinfo=None)

    def limits(self, s, now: datetime | None = None) -> dict:
        now = now or utcnow()
        day_start = self._local_day_start(now)
        today = len(list(s.scalars(select(Publication.id).where(
            Publication.platform == self.PLATFORM,
            or_(Publication.status == PS.PROCESSING, Publication.published_at >= day_start)))))
        last = s.scalar(select(Publication.published_at).where(Publication.published_at.is_not(None))
                        .order_by(Publication.published_at.desc()).limit(1))
        return {"today": today, "daily_limit": int(self.ig.get("daily_limit", 5)),
                "min_interval_minutes": float(self.ig.get("min_interval_minutes", 60)),
                "last_published_at": last, "day_start": day_start}

    def schedule(self, s, now: datetime | None = None) -> dict[int, datetime]:
        """Expected send time of every waiting publication, honouring time, retries, daily limit and pause."""
        now = now or utcnow()
        lim = self.limits(s, now)
        interval = timedelta(minutes=lim["min_interval_minutes"])
        count, day_start = lim["today"], lim["day_start"]
        prev = lim["last_published_at"]
        out: dict[int, datetime] = {}
        for pub in self._waiting(s):
            t = max(now, pub.scheduled_at or now, pub.next_retry_at or now)
            if prev:
                t = max(t, prev + interval)
            while True:
                ds = self._local_day_start(t)
                if ds != day_start:
                    day_start, count = ds, 0
                if count < lim["daily_limit"]:
                    break
                t = self._local_day_start(t + timedelta(days=1)) + timedelta(seconds=1)
            out[pub.id] = t
            prev, count = t, count + 1
        return out

    @staticmethod
    def _waiting(s) -> list[Publication]:
        rows = list(s.scalars(select(Publication).where(Publication.platform == PublishQueue.PLATFORM,
                                                        Publication.status.in_([PS.QUEUED, PS.FAILED]))))
        return sorted(rows, key=lambda p: (p.next_retry_at or p.scheduled_at or p.created_at, p.id))

    def local(self, dt: datetime) -> str:
        return dt.replace(tzinfo=timezone.utc).astimezone(self.tz).strftime("%d.%m %H:%M")

    # ---------- checks shared by dry-run and the real run ----------
    def preflight(self, s, pub: Publication) -> list[dict]:
        item = pub.content
        checks = []

        def check(name: str, ok: bool, detail: str = ""):
            checks.append({"name": name, "ok": ok, "detail": detail})

        path = pub.video_path or self._video_path(s, item.id)
        vp = video_problems(path, self.ig)
        check("Видео готово", bool(path and Path(path).is_file()), path or "нет файла")
        check("Формат Reels", not vp, "; ".join(vp) or "MP4, H.264/AAC, 9:16, длительность и размер в норме")
        check("Права подтверждены", bool(pub.rights_confirmed_at) and item.rights_status in PUBLISHABLE_RIGHTS,
              f"{item.rights_status.value}, подтверждено вручную " + (self.local(pub.rights_confirmed_at)
                                                                     if pub.rights_confirmed_at else "— нет"))
        cp = caption_problems(pub.caption, self.ig)
        check("Описание готово", not cp, "; ".join(cp) or f"{len(pub.caption)} символов")
        miss = self.missing()
        acc = self.account_state()
        check("Instagram подключён", not miss and bool(acc.get("ok")),
              ("нужен " + ", ".join(miss)) if miss else
              (f"@{acc.get('username')}" if acc.get("ok") else "подключение не проверено или с ошибкой"))
        return checks

    def plan(self, s, pub: Publication, eta: datetime | None) -> dict:
        checks = self.preflight(s, pub)
        acc = self.account_state()
        v = self.ig.get("api_version", "v25.0")
        ig = self.account_id or "<INSTAGRAM_ACCOUNT_ID>"
        return {
            "publication": pub.id, "content_id": pub.content_id, "title": pub.content.title,
            "video": pub.video_path, "where": f"Instagram Reels @{acc['username']}" if acc.get("username")
            else "Instagram Reels (аккаунт не подключён)",
            "when": _iso(eta), "when_local": self.local(eta) if eta else None,
            "caption": pub.caption, "checks": checks,
            "ready": all(c["ok"] for c in checks if c["name"] != "Instagram подключён"),
            "requests": [
                f"POST graph.facebook.com/{v}/{ig}/media  media_type=REELS upload_type=resumable caption=…",
                f"POST rupload.facebook.com/ig-api-upload/{v}/<container>  файл {Path(pub.video_path or '').name}",
                f"GET  graph.facebook.com/{v}/<container>?fields=status_code  до FINISHED",
                f"POST graph.facebook.com/{v}/{ig}/media_publish  creation_id=<container>",
            ],
            "sent": False,
        }

    def dry_run(self, pub_id: int | None = None, record: bool = True) -> list[dict]:
        """Plans for waiting publications. Sends nothing; with record=False writes nothing either."""
        with self.db.session() as s:
            eta = self.schedule(s)
            pubs = [self._get(s, pub_id)] if pub_id else self._waiting(s)
            plans = [self.plan(s, p, eta.get(p.id)) for p in pubs]
            if record:
                for p, pl in zip(pubs, plans):
                    p.dry_run_at, p.dry_run_report = utcnow(), json.dumps(pl, ensure_ascii=False)
            else:
                s.rollback()
        return plans

    # ---------- the real run ----------
    def recover(self) -> int:
        """PROCESSING left by a crash goes back to the queue; container_id keeps the retry duplicate-free."""
        with self.db.session() as s:
            stuck = list(s.scalars(select(Publication).where(Publication.platform == self.PLATFORM,
                                                             Publication.status == PS.PROCESSING)))
            for p in stuck:
                p.status = PS.QUEUED
                p.attempts = max(0, (p.attempts or 0) - 1)
                _event(p, "восстановлено после сбоя")
        return len(stuck)

    def tick(self) -> str:
        """One scheduler step: at most one publication is sent. Returns what happened (for logs/tests)."""
        if not self.cfg.get("enabled", True) or not self.ig.get("enabled", True):
            return "publishing disabled"
        if not self._run_lock.acquire(blocking=False):
            return "busy"
        try:
            if self.paused:
                return "paused"
            now = utcnow()
            with self.db.session() as s:
                eta = self.schedule(s, now)
                due = [pid for pid, t in eta.items() if t <= now]
                if not due:
                    return "nothing due"
                pid = due[0]
                if not self.live:
                    # Dry-run: every due publication gets one recorded plan (again after an edit); status stays.
                    for did in due:
                        pub = s.get(Publication, did)
                        if pub.dry_run_at is None:
                            pl = self.plan(s, pub, eta[did])
                            pub.dry_run_at, pub.dry_run_report = utcnow(), json.dumps(pl, ensure_ascii=False)
                            _event(pub, "dry-run: проверено, в Instagram не отправлялось")
                            log_event(log, "PUBLISH", "DRY_RUN", pub.content_id, publication=did,
                                      ready=pl["ready"])
                    return "dry-run"
            if self.missing():
                return "no token"
            self.process(pid)
            return f"processed {pid}"
        finally:
            self._run_lock.release()

    def _fail(self, pub_id: int, message: str, err: GraphError | None = None, retry: bool = True,
              keep_attempt: bool = False, delay_minutes: float | None = None) -> None:
        max_retries = int(self.cfg.get("max_retries", 3))
        with self.db.session() as s:
            pub = s.get(Publication, pub_id)
            if keep_attempt:
                pub.attempts = max(0, pub.attempts - 1)
            pub.error, pub.error_at = message[:2000], utcnow()
            pub.error_http_status = err.http_status if err else None
            pub.error_code = err.code_label if err else None
            pub.error_request_id = err.request_id if err else None
            if retry and pub.attempts < max_retries:
                delay = delay_minutes if delay_minutes is not None else \
                    float(self.cfg.get("retry_delay_minutes", 5)) * 3 ** max(0, pub.attempts - 1)
                pub.status, pub.next_retry_at = PS.FAILED, utcnow() + timedelta(minutes=delay)
                _event(pub, f"ошибка, повтор через {delay:g} мин: {message[:200]}")
            else:
                pub.status, pub.next_retry_at = PS.WAITING_USER, None
                _event(pub, f"нужно решение человека: {message[:200]}")
            status, cid = pub.status.value, pub.content_id
        log_event(log, "PUBLISH", status, cid, publication=pub_id, error=message[:300])

    def process(self, pub_id: int) -> None:
        with self.db.session() as s:
            pub = self._get(s, pub_id)
            checks = self.preflight(s, pub)
            pub.status, pub.started_at, pub.attempts = PS.PROCESSING, utcnow(), (pub.attempts or 0) + 1
            pub.account = self.account_id
            _event(pub, f"отправка, попытка {pub.attempts}")
            caption, video, container, uploaded = pub.caption, pub.video_path, pub.container_id, pub.uploaded_at
        bad = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"] and c["name"] != "Instagram подключён"]
        if bad:
            self._fail(pub_id, "; ".join(bad), retry=False)
            return
        cl = self.client()
        try:
            self._send(cl, pub_id, caption, Path(video), container, uploaded)
        except GraphError as exc:
            kind = exc.kind
            msg = {"auth": "токен недействителен или истёк: обновите токен",
                   "permission": "не хватает разрешения (нужно instagram_content_publish)"}.get(kind, "")
            msg = f"{msg + ': ' if msg else ''}{exc}"
            if kind == "rate":
                self._fail(pub_id, f"лимит запросов Meta: {exc}", exc, keep_attempt=True, delay_minutes=60)
            else:
                self._fail(pub_id, msg, exc, retry=kind in ("transient", "not_ready"))
        except Exception as exc:  # file errors etc.
            log.exception("publish %s", pub_id)
            self._fail(pub_id, f"{exc.__class__.__name__}: {exc}")
        finally:
            cl.close()

    def _save(self, pub_id: int, **fields) -> None:
        note = fields.pop("note", None)
        with self.db.session() as s:
            pub = s.get(Publication, pub_id)
            for k, v in fields.items():
                setattr(pub, k, v)
            if note:
                _event(pub, note)

    def _send(self, cl: InstagramClient, pub_id: int, caption: str, video: Path,
              container: str | None, uploaded: datetime | None) -> None:
        used, total = cl.publishing_limit()
        quota = int(total or self.ig.get("api_quota_per_24h", 100))
        if used >= quota:
            raise GraphError(f"исчерпан лимит Instagram: {used} из {quota} публикаций за 24 ч", code=9)

        if container:
            state = cl.container_state(container)
            if state.status_code == "PUBLISHED":  # the previous attempt did publish: never post twice
                self._adopt_published(cl, pub_id, caption)
                return
            if state.status_code in ("ERROR", "EXPIRED"):
                self._save(pub_id, container_id=None, uploaded_at=None,
                           note=f"контейнер {container} {state.status_code}, создаю новый")
                container = uploaded = None
        if not container:
            container = cl.create_container(caption, bool(self.ig.get("share_to_feed", True)),
                                            self.ig.get("thumb_offset_ms"))
            uploaded = None
            self._save(pub_id, container_id=container, note=f"контейнер {container} создан")
        if not uploaded:
            offset = cl.uploaded_bytes(container)
            if offset < video.stat().st_size:
                cl.upload(container, video, offset)
            self._save(pub_id, uploaded_at=utcnow(),
                       note="видео загружено" + (f" (докачка с {offset} байт)" if offset else ""))

        state = cl.wait_finished(container, float(self.ig.get("poll_seconds", 10)),
                                 float(self.ig.get("processing_timeout_minutes", 15)) * 60,
                                 **({"sleep": self._sleep} if self._sleep else {}))
        if state.status_code == "PUBLISHED":
            self._adopt_published(cl, pub_id, caption)
            return
        if state.status_code == "IN_PROGRESS":
            raise GraphError("Instagram ещё обрабатывает видео, проверю позже", transient=True)
        if state.status_code != "FINISHED":
            self._save(pub_id, container_id=None, uploaded_at=None)
            raise GraphError(f"Instagram не принял видео ({state.status_code}): {state.detail or 'без описания'}")

        media_id = cl.publish(container)
        self._mark_published(cl, pub_id, media_id)

    def _adopt_published(self, cl: InstagramClient, pub_id: int, caption: str) -> None:
        """Container already PUBLISHED (e.g. the answer to media_publish was lost): find that post."""
        with self.db.session() as s:
            started = s.get(Publication, pub_id).created_at
        for m in cl.recent_media():
            ts = m.get("timestamp", "")
            try:
                when = datetime.fromisoformat(ts.replace("+0000", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                when = None
            if (m.get("caption") or "").strip() == caption.strip() and (when is None or when >= started - timedelta(hours=1)):
                self._mark_published(cl, pub_id, m["id"], m.get("permalink"), "найден уже опубликованный пост")
                return
        self._mark_published(cl, pub_id, None, None, "контейнер опубликован, но ID поста не найден")

    def _mark_published(self, cl: InstagramClient, pub_id: int, media_id: str | None,
                        permalink: str | None = None, note: str = "опубликовано") -> None:
        if media_id and not permalink:
            try:
                permalink = cl.media(media_id).get("permalink")
            except GraphError as exc:
                log.warning("permalink of %s: %s", media_id, exc)
        with self.db.session() as s:
            pub = s.get(Publication, pub_id)
            pub.status, pub.external_id, pub.permalink = PS.PUBLISHED, media_id, permalink
            pub.published_at, pub.next_retry_at = utcnow(), None
            pub.error = pub.error_http_status = pub.error_code = pub.error_request_id = None
            _event(pub, f"{note}: {media_id or '—'}")
            pub.content.status = Status.PUBLISHED
            cid = pub.content_id
        log_event(log, "PUBLISH", "PUBLISHED", cid, publication=pub_id, platform=self.PLATFORM, media_id=media_id)

    # ---------- for the UI ----------
    def as_json(self, pub: Publication, eta: datetime | None = None) -> dict:
        item = pub.content
        return {
            "id": pub.id, "content_id": pub.content_id, "title": item.title, "platform": pub.platform,
            "caption": pub.caption, "status": pub.status.value, "attempts": pub.attempts,
            "max_retries": int(self.cfg.get("max_retries", 3)),
            "scheduled_at": _iso(pub.scheduled_at), "eta": _iso(eta), "next_retry_at": _iso(pub.next_retry_at),
            "published_at": _iso(pub.published_at), "external_id": pub.external_id, "permalink": pub.permalink,
            "error": pub.error, "error_http_status": pub.error_http_status, "error_code": pub.error_code,
            "error_request_id": pub.error_request_id, "error_at": _iso(pub.error_at),
            "rights": item.rights_status.value, "rights_confirmed_at": _iso(pub.rights_confirmed_at),
            "dry_run_at": _iso(pub.dry_run_at),
            "dry_run": json.loads(pub.dry_run_report) if pub.dry_run_report else None,
            "log": json.loads(pub.log) if pub.log else [],
            "thumbnail": f"/media/thumb/{pub.content_id}", "video": f"/media/video/{pub.content_id}",
            "duration": item.duration,
        }

    def overview(self) -> dict:
        with self.db.session() as s:
            eta = self.schedule(s)
            pubs = list(s.scalars(select(Publication).where(Publication.platform == self.PLATFORM)
                                  .order_by(Publication.id.desc()).limit(300)))
            lim = self.limits(s)
            queued_ids = select(Publication.content_id).where(Publication.status != PS.CANCELLED)
            ready = list(s.scalars(select(ContentItem).where(
                ContentItem.status == Status.READY_TO_PUBLISH, ContentItem.id.not_in(queued_ids))
                .order_by(ContentItem.id.desc()).limit(200)))
            return {
                "mode": "live" if self.live else "dry_run", "paused": self.paused,
                "enabled": bool(self.cfg.get("enabled", True) and self.ig.get("enabled", True)),
                "timezone": str(self.tz),
                "limits": {"today": lim["today"], "daily_limit": lim["daily_limit"],
                           "min_interval_minutes": lim["min_interval_minutes"],
                           "last_published_at": _iso(lim["last_published_at"]),
                           "max_retries": int(self.cfg.get("max_retries", 3))},
                "account": self.account_state(),
                "publications": [self.as_json(p, eta.get(p.id)) for p in pubs],
                "ready": [{"id": i.id, "title": i.title, "rights": i.rights_status.value, "duration": i.duration,
                           "category": i.category, "description": i.post_description,
                           "thumbnail": f"/media/thumb/{i.id}"} for i in ready],
            }
