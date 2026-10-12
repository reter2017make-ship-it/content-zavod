"""Instagram queue (platform "instagram_web") in the factory's own `publications` table: no second database,
no copies of the videos — a post points at the ready video of the factory."""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, select

from app.database.models import AppSetting, ContentItem, Publication, PublicationStatus as PS, utcnow
from app.instagram_web.queue.scheduler import Planner

PLATFORM = "instagram_web"
PAUSE_KEY = "instagram_web.paused"
MAX_CAPTION = 2200
UI_STATUS = {PS.QUEUED: "queued", PS.PROCESSING: "publishing", PS.PUBLISHED: "published", PS.FAILED: "failed",
             PS.WAITING_USER: "failed", PS.PAUSED: "paused", PS.CANCELLED: "cancelled"}
WAITING = (PS.QUEUED, PS.FAILED)


class QueueError(ValueError):
    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.http_status = http_status


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _event(p: Publication, text: str) -> None:
    log = json.loads(p.log) if p.log else []
    log.append({"at": _iso(utcnow()), "event": text})
    p.log = json.dumps(log[-50:], ensure_ascii=False)


class IGQueue:
    def __init__(self, settings, db, cfg):
        self.s, self.db, self.cfg = settings, db, cfg
        self.planner = Planner(cfg)

    # ---------- the video of a ready item (the factory's own file) ----------
    @staticmethod
    def video_of(s, item: ContentItem) -> Path | None:
        from app.processing.ready import rendered_video
        if item.ready_path and Path(item.ready_path).is_file():
            return Path(item.ready_path)
        return rendered_video(s, item.id)

    # ---------- global pause ----------
    @property
    def paused(self) -> bool:
        with self.db.session() as s:
            row = s.get(AppSetting, PAUSE_KEY)
            return bool(row and row.value == "1")

    def set_paused(self, value: bool) -> None:
        with self.db.session() as s:
            row = s.get(AppSetting, PAUSE_KEY) or AppSetting(key=PAUSE_KEY)
            row.value = "1" if value else "0"
            s.add(row)

    # ---------- add / change ----------
    def save_cover(self, filename: str, data: bytes) -> str:
        from PIL import Image
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:
            raise QueueError("обложка: нужна картинка jpg, png или webp", 400) from exc
        folder = self.s.data("ig_covers")
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{hashlib.sha256(data).hexdigest()[:20]}.jpg"
        img.save(out, "JPEG", quality=92)
        return str(out)

    def add(self, item_ids: list[int], caption: str | None = None, cover_path: str | None = None,
            scheduled_at: datetime | None = None, captions: dict[int, str] | None = None,
            account: str | None = None) -> dict:
        """One queue entry per video. Caption: captions[id] (typed for this video; also saved as the video's
        description in the factory), else the common caption, else the video's own description."""
        captions = {int(k): v for k, v in (captions or {}).items()}
        if cover_path and not Path(cover_path).is_file():
            raise QueueError("файл обложки не найден", 400)
        added, errors = [], {}
        for iid in item_ids:
            with self.db.session() as s:
                item = s.get(ContentItem, iid)
                if item is None:
                    errors[iid] = "нет такого ролика"
                    continue
                video = self.video_of(s, item)
                if video is None:
                    errors[iid] = "у ролика нет готового файла"
                    continue
                live = s.scalar(select(Publication.id).where(Publication.content_id == iid, Publication.platform == PLATFORM,
                                                             Publication.status != PS.CANCELLED))
                if live:
                    errors[iid] = "уже в очереди Instagram или опубликован"
                    continue
                if iid in captions:
                    text = (captions[iid] or "").strip()
                    if text and len(text) <= MAX_CAPTION:
                        item.post_description = text
                else:
                    text = (caption if caption is not None else item.post_description or "").strip()
                if len(text) > MAX_CAPTION:
                    errors[iid] = f"описание длиннее {MAX_CAPTION} символов"
                    continue
                cover = cover_path if len(item_ids) == 1 else None
                if not cover:  # owner's rule: the background chosen in the editor is the cover
                    from app.instagram_web.queue.cover_from_background import background_cover
                    cover = background_cover(self.s, self.db, iid)
                p = Publication(content_id=iid, platform=PLATFORM, account=account or self.cfg.account, caption=text,
                                video_path=str(video), cover_path=cover,
                                scheduled_at=scheduled_at, status=PS.QUEUED)
                _event(p, "добавлен в очередь" + (" (по расписанию)" if scheduled_at else ""))
                s.add(p)
                s.flush()
                added.append(p.id)
        return {"added": added, "errors": {str(k): v for k, v in errors.items()}}

    def _get(self, s, pid: int) -> Publication:
        p = s.get(Publication, pid)
        if p is None or p.platform != PLATFORM:
            raise QueueError("нет такой публикации", 404)
        return p

    def update(self, pid: int, caption: str | None = None, scheduled_at: datetime | None = None,
               clear_schedule: bool = False, cover_path: str | None = None) -> None:
        with self.db.session() as s:
            p = self._get(s, pid)
            if p.status in (PS.PROCESSING, PS.PUBLISHED):
                raise QueueError("публикацию уже не изменить")
            if caption is not None:
                if len(caption) > MAX_CAPTION:
                    raise QueueError(f"описание длиннее {MAX_CAPTION} символов", 400)
                p.caption = caption.strip()
                item = s.get(ContentItem, p.content_id)
                if item is not None and p.caption:
                    item.post_description = p.caption
            if scheduled_at is not None or clear_schedule:
                p.scheduled_at = None if clear_schedule else scheduled_at
            if cover_path is not None:
                p.cover_path = cover_path or None
            _event(p, "изменён")

    def remove(self, pid: int) -> None:
        with self.db.session() as s:
            p = self._get(s, pid)
            if p.status == PS.PROCESSING:
                raise QueueError("ролик сейчас публикуется — дождитесь окончания")
            if p.status == PS.PUBLISHED:
                raise QueueError("ролик уже опубликован")
            p.status = PS.CANCELLED
            _event(p, "убран из очереди")

    def pause_item(self, pid: int) -> None:
        with self.db.session() as s:
            p = self._get(s, pid)
            if p.status not in WAITING:
                raise QueueError("на паузу ставится только ролик в очереди")
            p.status = PS.PAUSED
            _event(p, "пауза")

    def resume_item(self, pid: int) -> None:
        with self.db.session() as s:
            p = self._get(s, pid)
            if p.status != PS.PAUSED:
                raise QueueError("ролик не на паузе")
            p.status = PS.QUEUED
            _event(p, "снят с паузы")

    def retry(self, pid: int) -> None:
        with self.db.session() as s:
            p = self._get(s, pid)
            if p.status not in (PS.FAILED, PS.WAITING_USER):
                raise QueueError("повторить можно только ролик с ошибкой")
            p.status, p.attempts, p.next_retry_at = PS.QUEUED, 0, None
            p.error = p.error_code = None
            _event(p, "повтор вручную")

    def recover(self) -> int:
        """A crash during publishing: back to the queue (the next attempt first checks the profile, stage 4+)."""
        with self.db.session() as s:
            stuck = list(s.scalars(select(Publication).where(Publication.platform == PLATFORM,
                                                             Publication.status == PS.PROCESSING)))
            for p in stuck:
                p.status = PS.QUEUED
                _event(p, "восстановлено после сбоя завода")
        return len(stuck)

    # ---------- plan and view ----------
    def _today(self, s, now: datetime, account: str | None = None) -> tuple[int, datetime | None]:
        """Posts today and the last post time — of one account (the daily limit is per account)."""
        day = self.planner.day_start_utc(now)
        acc = [Publication.account == account] if account is not None else []
        n = len(list(s.scalars(select(Publication.id).where(
            Publication.platform == PLATFORM, *acc,
            or_(Publication.status == PS.PROCESSING, Publication.published_at >= day)))))
        last = s.scalar(select(Publication.published_at).where(Publication.platform == PLATFORM, *acc,
                                                               Publication.published_at.is_not(None))
                        .order_by(Publication.published_at.desc()).limit(1))
        return n, last

    def plan(self, s, now: datetime | None = None) -> dict[int, datetime]:
        now = now or utcnow()
        rows = list(s.scalars(select(Publication).where(Publication.platform == PLATFORM,
                                                        Publication.status.in_(WAITING))))
        rows.sort(key=lambda p: (p.scheduled_at or p.created_at, p.id))
        out: dict[int, datetime] = {}
        for acc in sorted({p.account or "" for p in rows}):  # every account has its own limit and interval
            group = [p for p in rows if (p.account or "") == acc]
            n, last = self._today(s, now, acc or None)
            out.update(self.planner.plan([(p.id, max(filter(None, [p.scheduled_at, p.next_retry_at]), default=None))
                                          for p in group], now, n, last))
        return out

    def as_json(self, p: Publication, item: ContentItem | None, eta: datetime | None) -> dict:
        return {"id": p.id, "content_id": p.content_id, "status": UI_STATUS.get(p.status, p.status.value.lower()),
                "raw_status": p.status.value, "caption": p.caption, "video": Path(p.video_path).name if p.video_path else None,
                "title": item.title if item else None, "thumbnail": f"/media/thumb/{p.content_id}",
                "cover": bool(p.cover_path), "account": p.account,
                "cover_url": f"/api/instagram-web/queue/{p.id}/cover" if p.cover_path else None, "scheduled_at": _iso(p.scheduled_at), "eta": _iso(eta),
                "attempts": p.attempts or 0, "published_at": _iso(p.published_at), "permalink": p.permalink,
                "error": p.error, "error_code": p.error_code, "failed_at": _iso(p.failed_at),
                "log": json.loads(p.log) if p.log else []}

    def listing(self) -> dict:
        now = utcnow()
        with self.db.session() as s:
            eta = self.plan(s, now)
            rows = list(s.scalars(select(Publication).where(Publication.platform == PLATFORM,
                                                            Publication.status != PS.CANCELLED)
                                  .order_by(Publication.id.desc()).limit(500)))
            items = {i.id: i for i in s.scalars(select(ContentItem).where(ContentItem.id.in_([p.content_id for p in rows])))}
            out = [self.as_json(p, items.get(p.content_id), eta.get(p.id)) for p in rows]
            n, _ = self._today(s, now)
            day = self.planner.day_start_utc(now)
            failed_today = len(list(s.scalars(select(Publication.id).where(
                Publication.platform == PLATFORM, Publication.failed_at >= day))))
        order = {"publishing": 0, "queued": 1, "failed": 2, "paused": 3, "published": 4}
        out.sort(key=lambda x: (order.get(x["status"], 9), x["eta"] or "", -(x["id"])))
        nxt = min(eta.values()) if eta and not self.paused else None
        return {"items": out, "paused": self.paused,
                "today": {"published": n, "failed": failed_today, "queued": len(eta), "limit": self.cfg.max_posts_per_day,
                          "next_at": _iso(nxt)}}

    def cancel_for(self, content_id: int, why: str) -> int:
        """The video left «Готовые»: its waiting Instagram posts are dropped (published ones stay)."""
        n = 0
        with self.db.session() as s:
            for p in s.scalars(select(Publication).where(Publication.platform == PLATFORM, Publication.content_id == content_id,
                                                         Publication.status.in_([PS.QUEUED, PS.FAILED, PS.PAUSED, PS.WAITING_USER]))):
                p.status = PS.CANCELLED
                _event(p, why)
                n += 1
        return n

    def queued_content_ids(self) -> set[int]:
        with self.db.session() as s:
            return set(s.scalars(select(Publication.content_id).where(Publication.platform == PLATFORM,
                                                                       Publication.status != PS.CANCELLED)))
