"""Telegram through the official MTProto API (Telethon, the owner's user session). Optional.

The owner enters api_id / api_hash (my.telegram.org) and signs in from the web UI with the code Telegram
sends; the session stays in credentials/ (git-ignored, mode 600). Then:
  * channels in «API» mode are scanned through the API;
  * videos the public web preview does not serve («Media is too big») are downloaded through the API.
Protected content (the owner disabled saving/forwarding) is skipped, never worked around. FloodWait stops
polling like a web block does. Telethon is asyncio-only: one background thread owns its event loop.
"""
from __future__ import annotations

import asyncio
import re
import json
import logging
import os
import threading
import time
from pathlib import Path

from app.sources.base import DiscoveredItem

log = logging.getLogger("sources.telegram_api")

SESSION_FILE = "credentials/telegram"
LOGIN_FILE = "credentials/telegram_login.json"  # phone + phone_code_hash between «Получить код» and «Войти»
LOGIN_TTL = 15 * 60


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


def is_video(msg) -> bool:
    if getattr(msg, "video", None):
        return True
    f = getattr(msg, "file", None)
    return bool(f and str(getattr(f, "mime_type", "") or "").startswith("video/"))


def post_url(channel: str, post_id: int) -> str:
    m = re.fullmatch(r"id(\d+)", channel)
    return f"https://t.me/c/{m.group(1)}/{post_id}" if m else f"https://t.me/{channel}/{post_id}"


class TelegramAPI:
    def __init__(self, settings, client_factory=None):
        self.settings = settings
        self.session_path = settings.path(SESSION_FILE)
        self.max_bytes = float(settings.get("filters.max_filesize_mb", 2048)) * 1024 * 1024
        self._factory = client_factory
        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.login_path = settings.path(LOGIN_FILE)
        # kept on disk next to the session: a server restart between «Получить код» and «Войти» must not lose it
        self._login: dict = self._load_login()
        self._authorized: bool | None = None
        self._me: str | None = None

    # ---------- plumbing ----------
    @property
    def api_id(self) -> str:
        return self.settings.env("TELEGRAM_API_ID")

    @property
    def api_hash(self) -> str:
        return self.settings.env("TELEGRAM_API_HASH")

    @property
    def has_keys(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True, name="telegram-api").start()
            return self._loop

    def _run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    async def _get_client(self):
        if self._client is None:
            if self._factory:
                self._client = self._factory()
            else:
                if not self.has_keys:
                    raise TelegramAPIError("нет TELEGRAM_API_ID и TELEGRAM_API_HASH")
                from telethon import TelegramClient
                self.session_path.parent.mkdir(parents=True, exist_ok=True)
                self._client = TelegramClient(str(self.session_path), int(self.api_id), self.api_hash)
            await self._client.connect()
        return self._client

    def _load_login(self) -> dict:
        try:
            data = json.loads(self.login_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if time.time() - float(data.get("at", 0)) > LOGIN_TTL:
            self.login_path.unlink(missing_ok=True)
            return {}
        return data

    def _set_login(self, data: dict) -> None:
        self._login = data
        if data:
            self.login_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.login_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**data, "at": time.time()}), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.login_path)
        else:
            self.login_path.unlink(missing_ok=True)

    def _protect_session(self) -> None:
        f = Path(str(self.session_path) + ".session")
        if f.exists():
            os.chmod(f, 0o600)

    def reset(self) -> None:
        """New keys were saved: drop the old client."""
        if self._client is not None:
            try:
                self._run(self._client.disconnect(), 10)
            except Exception:
                pass
        self._client, self._authorized, self._me = None, None, None
        self._set_login({})

    # ---------- state ----------
    def authorized(self, refresh: bool = False) -> bool:
        if not self.has_keys and not self._factory:
            return False
        if self._authorized is None or refresh:
            try:
                self._authorized = self._run(self._check(), 30)
            except Exception as exc:
                log.warning("telegram api: %s", exc)
                return False
        return bool(self._authorized)

    async def _check(self) -> bool:
        cl = await self._get_client()
        ok = await cl.is_user_authorized()
        if ok and not self._me:
            me = await cl.get_me()
            self._me = getattr(me, "username", None) or getattr(me, "first_name", None) or str(getattr(me, "id", ""))
        return bool(ok)

    def status(self) -> dict:
        missing = [k for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH") if not self.settings.env(k)]
        if missing and not self._factory:
            return {"state": "no_keys", "missing": missing, "label": "не подключено: нужны api_id и api_hash"}
        if self.authorized():
            return {"state": "ok", "missing": [], "user": self._me, "label": f"подключено: {self._me or 'аккаунт'}"}
        if not self._login:
            self._login = self._load_login()
        step = self._login.get("step")
        if step == "code":
            wait = max(0, int(self._login.get("timeout") or 0) - int(time.time() - float(self._login.get("sent_at") or 0)))
            return {"state": "code_sent", "missing": [], "phone": self._login.get("phone_masked"),
                    "delivery": self._login.get("delivery"), "next": self._login.get("next"), "resend_in": wait,
                    "label": "код отправлен"}
        if step == "password":
            return {"state": "password", "missing": [], "label": "нужен облачный пароль (двухэтапная проверка)"}
        return {"state": "need_login", "missing": [], "label": "ключи сохранены, нужен вход по номеру"}

    # ---------- login from the web UI ----------
    def send_code(self, phone: str) -> dict:
        phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        if len(phone) < 8:
            raise TelegramAPIError("номер в международном формате, например +79991234567")

        async def go():
            cl = await self._get_client()
            return await cl.send_code_request(phone)

        try:
            sent = self._run(go(), 60)
        except TelegramAPIError:
            raise
        except Exception as exc:
            log.warning("TELEGRAM_API send_code failed: %s", exc.__class__.__name__)
            raise TelegramAPIError(self._explain(exc)) from exc
        info = self._sent_info(sent)
        log.info("TELEGRAM_API code requested: %s, next %s, timeout %s", getattr(sent, "type", None).__class__.__name__,
                 getattr(sent, "next_type", None).__class__.__name__, info["timeout"])
        self._set_login({"step": "code", "phone": phone, "hash": sent.phone_code_hash,
                         "phone_masked": phone[:3] + "•••" + phone[-2:], **info, "sent_at": time.time()})
        return self.status()

    def sign_in(self, code: str | None = None, password: str | None = None) -> dict:
        from telethon import errors

        async def go():
            cl = await self._get_client()
            if password:
                await cl.sign_in(password=password)
            else:
                await cl.sign_in(phone=self._login["phone"], code=code, phone_code_hash=self._login["hash"])

        if not self._login:
            self._login = self._load_login()
        if password is None and self._login.get("step") not in ("code",):
            if self._login.get("step") == "password":
                raise TelegramAPIError("код уже принят: введите облачный пароль")
            raise TelegramAPIError("код устарел или не запрашивался: нажмите «Получить код» ещё раз")
        try:
            self._run(go(), 60)
        except errors.SessionPasswordNeededError:
            self._set_login({**self._login, "step": "password"})
            return self.status()
        except Exception as exc:
            log.warning("TELEGRAM_API sign_in failed: %s", exc.__class__.__name__)  # never the code itself
            if exc.__class__.__name__ in ("PhoneCodeExpiredError",):
                self._set_login({})
            raise TelegramAPIError(self._explain(exc)) from exc
        self._set_login({})
        self._authorized = None
        self._protect_session()
        log.info("TELEGRAM_API login ok")
        return self.status()

    def logout(self) -> dict:
        async def go():
            cl = await self._get_client()
            await cl.log_out()

        try:
            self._run(go(), 30)
        except Exception as exc:
            log.warning("telegram logout: %s", exc)
        self._client, self._authorized, self._me = None, None, None
        self._set_login({})
        Path(str(self.session_path) + ".session").unlink(missing_ok=True)
        return self.status()

    @staticmethod
    def _delivery(kind) -> str | None:
        name = kind.__class__.__name__ if kind is not None else ""
        return {
            "SentCodeTypeApp": "в приложение Telegram: чат «Telegram» на телефоне или компьютере, где вы уже вошли",
            "SentCodeTypeSms": "по SMS",
            "SentCodeTypeCall": "звонком: робот продиктует код",
            "SentCodeTypeFlashCall": "коротким звонком: код — последние цифры номера",
            "SentCodeTypeMissedCall": "пропущенным звонком: код — последние цифры номера",
            "SentCodeTypeFragmentSms": "по SMS через Fragment",
            "SentCodeTypeEmailCode": "на почту, привязанную к Telegram",
            "SentCodeTypeFirebaseSms": "по SMS",
        }.get(name, None) if name else None

    def _sent_info(self, sent) -> dict:
        return {"delivery": self._delivery(getattr(sent, "type", None)) or "способ доставки Telegram не сообщил",
                "next": self._delivery(getattr(sent, "next_type", None)),
                "timeout": int(getattr(sent, "timeout", 0) or 0)}

    def resend_code(self) -> dict:
        """«Отправить другим способом» (SMS / звонок), когда Telegram это разрешает."""
        if not self._login:
            self._login = self._load_login()
        if self._login.get("step") != "code":
            raise TelegramAPIError("сначала нажмите «Получить код»")

        async def go():
            from telethon import functions
            cl = await self._get_client()
            return await cl(functions.auth.ResendCodeRequest(self._login["phone"], self._login["hash"]))

        try:
            sent = self._run(go(), 60)
        except Exception as exc:
            log.warning("TELEGRAM_API resend_code failed: %s", exc.__class__.__name__)
            raise TelegramAPIError(self._explain(exc)) from exc
        log.info("TELEGRAM_API code re-sent: %s", getattr(sent, "type", None).__class__.__name__)
        self._set_login({**self._login, "hash": sent.phone_code_hash, **self._sent_info(sent), "sent_at": time.time()})
        return self.status()

    @staticmethod
    def _explain(exc: Exception) -> str:
        name = exc.__class__.__name__
        return {
            "PhoneNumberInvalidError": "Telegram не принял номер",
            "PhoneCodeInvalidError": "неверный код",
            "PhoneCodeExpiredError": "Telegram считает код устаревшим: запросите новый и введите его сразу. "
                                     "Код перестаёт действовать, если его переслать или отправить в любой чат",
            "PasswordHashInvalidError": "неверный облачный пароль",
            "ApiIdInvalidError": "неверные api_id / api_hash",
            "FloodWaitError": f"Telegram просит подождать {getattr(exc, 'seconds', '?')} с перед новой попыткой",
            "PhoneNumberBannedError": "этот номер заблокирован в Telegram",
            "PhoneNumberFloodError": "слишком много попыток входа с этого номера, Telegram просит подождать",
            "SendCodeUnavailableError": "Telegram больше не может отправить код другим способом: подождите и попробуйте позже",
        }.get(name, f"{name}: {exc}")

    # ---------- channels of the signed-in account (read only: nothing is joined, sent or changed) ----------
    @staticmethod
    async def _entity(cl, name: str):
        """@username, or «id123» for a channel without a public name (known from the account's dialogs)."""
        m = re.fullmatch(r"id(\d+)", name)
        if m:
            from telethon.tl.types import PeerChannel
            return await cl.get_entity(PeerChannel(int(m.group(1))))
        return await cl.get_entity(name)

    def dialogs(self) -> list[dict]:
        if not self.authorized():
            raise TelegramAPIError("Telegram API не подключён: войдите на странице «Источники»", 409)
        try:
            return self._run(self._dialogs(), 300)
        except Exception as exc:
            if exc.__class__.__name__ == "FloodWaitError":
                raise TelegramAPIError(f"Telegram просит подождать {getattr(exc, 'seconds', '?')} с", 429) from exc
            raise

    async def _dialogs(self) -> list[dict]:
        cl = await self._get_client()
        out = []
        async for d in cl.iter_dialogs():
            e = d.entity
            if not getattr(d, "is_channel", False):
                continue  # people, bots and small groups are not sources
            out.append({"tg_id": int(e.id), "username": getattr(e, "username", None),
                        "title": getattr(e, "title", None) or str(e.id),
                        "members": getattr(e, "participants_count", None),
                        "kind": "channel" if getattr(e, "broadcast", False) else "group",
                        "noforwards": bool(getattr(e, "noforwards", False))})
        return out

    def avatar(self, name: str, dest: Path) -> Path | None:
        async def go():
            cl = await self._get_client()
            return await cl.download_profile_photo(await self._entity(cl, name), file=str(dest), download_big=False)
        res = self._run(go(), 60)
        return Path(res) if res else None

    # ---------- discovery and download ----------
    def scan(self, spec, known_ids: set[str], limit: int, cfg: dict, filters: dict):
        """Same item format and ids as the web scan, so both modes see the same posts as one."""
        try:
            return self._run(self._scan(spec, known_ids, limit, cfg, filters), 600)
        except Exception as exc:
            if exc.__class__.__name__ == "FloodWaitError":
                from app.sources.telegram import TelegramBlocked
                raise TelegramBlocked(f"Telegram API: подождать {getattr(exc, 'seconds', '?')} с") from exc
            raise

    async def _scan(self, spec, known_ids, limit, cfg, filters):
        from app.sources.telegram import ScanResult
        cl = await self._get_client()
        entity = await self._entity(cl, spec.username)
        title = getattr(entity, "title", None) or spec.username
        if getattr(entity, "noforwards", False):
            return ScanResult([], [f"t.me/{spec.username}: владелец канала запретил сохранение контента"],
                              spec.last_seen_post_id, title)
        min_d, max_d = float(filters.get("min_duration", 0)), float(filters.get("max_duration", 1e9))
        scan_limit = int(cfg.get("api_scan_messages", 100))
        items, skipped, newest = [], [], spec.last_seen_post_id
        videos, group_first, group_text = [], {}, {}
        async for msg in cl.iter_messages(entity, limit=scan_limit, min_id=spec.last_seen_post_id or 0):
            newest = max(newest or 0, msg.id)
            group = getattr(msg, "grouped_id", None)
            if group:
                group_first[group] = min(group_first.get(group, msg.id), msg.id)
                if msg.message:
                    group_text.setdefault(group, msg.message)
            if is_video(msg):
                videos.append(msg)
        # Web ids: an album is one post (its first message id); its 2nd, 3rd … video get -2, -3 …
        by_group: dict[int, list] = {}
        for m in videos:
            if getattr(m, "grouped_id", None):
                by_group.setdefault(m.grouped_id, []).append(m)
        for m in sorted(videos, key=lambda x: x.id, reverse=True):
            group = getattr(m, "grouped_id", None)
            if group:
                k = sorted(x.id for x in by_group[group]).index(m.id)
                post_id = group_first[group]
                sid = f"telegram:{spec.username}/{post_id}" + (f"-{k + 1}" if k else "")
            else:
                post_id, sid = m.id, f"telegram:{spec.username}/{m.id}"
            url = post_url(spec.username, m.id)
            if sid in known_ids:
                continue
            if getattr(m, "noforwards", False):
                skipped.append(f"{url}: защищённый контент (запрет сохранения)")
                continue
            duration = getattr(m.file, "duration", None)
            size = getattr(m.file, "size", None)
            if duration is not None and not (min_d <= duration <= max_d):
                skipped.append(f"{url}: длительность {duration:.0f} с вне фильтра {min_d:g}–{max_d:g} с")
                continue
            if size and size > self.max_bytes:
                skipped.append(f"{url}: файл {size / 1048576:.0f} МБ больше лимита")
                continue
            if len(items) >= limit:
                continue
            text = m.message or None
            if group and not text:
                text = group_text.get(group)
            items.append(DiscoveredItem(
                source="telegram", source_id=sid, source_url=url,
                title=(text or "").split("\n", 1)[0][:200] or f"{spec.username} #{post_id}",
                author=title, channel=spec.username, category=spec.category, caption=text,
                published_at=m.date.replace(tzinfo=None) if getattr(m, "date", None) else None,
                rights_status="REQUIRES_REVIEW", media_url=None, duration=duration,
                discovery_only=spec.mode == "discovery", attribution=f"{title} (t.me/{spec.username})",
                extra={"source_score": spec.source_score, "post_id": post_id, "views": getattr(m, "views", None),
                       "via": "api", "width": getattr(m.file, "width", None),
                       "height": getattr(m.file, "height", None), "file_size": size},
            ))
        return ScanResult(items, skipped, newest, title)

    def download(self, item_source_id: str, dest: Path) -> Path:
        return self._run(self._download(item_source_id, dest), 3600)

    async def _download(self, item_source_id: str, dest: Path) -> Path:
        channel, ref = item_source_id.removeprefix("telegram:").rsplit("/", 1)
        post_id, _, idx = ref.partition("-")
        cl = await self._get_client()
        channel = await self._entity(cl, channel)
        msg = await cl.get_messages(channel, ids=int(post_id))
        if msg is not None and getattr(msg, "grouped_id", None):
            # album: post_id is its first message; item k is its k-th video (as in the web preview)
            around = await cl.get_messages(channel, ids=list(range(int(post_id), int(post_id) + 12)))
            album = sorted((m for m in around if m is not None and getattr(m, "grouped_id", None) == msg.grouped_id
                            and is_video(m)), key=lambda m: m.id)
            k = int(idx) if idx else 1
            msg = album[k - 1] if len(album) >= k else None
        if msg is None or not is_video(msg):
            raise RuntimeError("сообщение недоступно или без видео")
        if getattr(msg, "noforwards", False):
            raise RuntimeError("контент защищён владельцем канала, скачивание запрещено")
        size = getattr(msg.file, "size", None)
        if size and size > self.max_bytes:
            raise RuntimeError(f"файл больше лимита: {size / 1048576:.0f} МБ")
        result = await cl.download_media(msg, file=str(dest))
        if not result:
            raise RuntimeError("Telegram не отдал файл")
        if Path(result) != dest:
            Path(result).replace(dest)
        return dest
