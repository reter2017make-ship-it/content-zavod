"""Local web UI (FastAPI on 127.0.0.1). Stage 3 part: «Источники» and «Ролики»; stage 6 extends this app."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from app.ai.analyzer import category_labels
from app.database.models import Channel, ContentItem, Publication, PublicationStatus, Status, Variant, utcnow
from app.processing import ready as ready_mod
from app.publishing.queue import QueueError
from app.sources.telegram import parse_links
from app.web.worker import FactoryWorker

STATIC = Path(__file__).parent / "static"
MODES = ("import", "discovery")
NO_CACHE = {"Cache-Control": "no-cache"}  # pages change with updates; always revalidate


class AddChannels(BaseModel):
    links: str
    category: str = "other"
    mode: str = "import"


class ChannelPatch(BaseModel):
    enabled: bool | None = None
    category: str | None = None
    mode: str | None = None
    via: str | None = None


class TelegramKeys(BaseModel):
    api_id: str
    api_hash: str


class TelegramPhone(BaseModel):
    phone: str


class TelegramCode(BaseModel):
    code: str | None = None
    password: str | None = None


class ItemPatch(BaseModel):
    category: str | None = None
    description: str | None = None


class EnqueueBody(BaseModel):
    content_id: int
    caption: str
    scheduled_at: str | None = None  # local time of publishing.timezone, "YYYY-MM-DDTHH:MM"; empty = ASAP
    rights_confirmed: bool = False
    note: str | None = None


class PublicationPatch(BaseModel):
    caption: str | None = None
    scheduled_at: str | None = None
    asap: bool = False


class DescriptionBody(BaseModel):
    description: str | None = None


class PostedBody(BaseModel):
    posted: bool = True
    date: str | None = None  # YYYY-MM-DD (local); empty = now
    note: str | None = None


class SpecBody(BaseModel):
    spec: dict
    at: float | None = None
    to_ready: bool = True


class TemplateBody(BaseModel):
    name: str
    spec: dict


class ApplyBody(BaseModel):
    item_ids: list[int]
    export: bool = True
    background: str | None = None      # asset id, "random" or empty (as in the template)
    background_tag: str | None = None


class BgDelete(BaseModel):
    ids: list[str] = []
    all_used: bool = False
    confirm: bool = False


class BgTag(BaseModel):
    ids: list[str]
    tag: str | None = None


class ScheduleBody(BaseModel):
    channel_id: int | None = None
    days: list[int] | None = None
    times: list[str] | None = None
    enabled: bool | None = None


class TTSBody(BaseModel):
    text: str
    voice: str = "irina"
    speed: float = 1.0


class AutoTemplates(BaseModel):
    mapping: dict[str, int | None]


class CleanBody(BaseModel):
    action: str
    days: int = 30
    confirm: bool = False  # without it only the preview (count and size) is returned


class SelectBody(BaseModel):
    ids: list[int]
    enabled: bool = True


class IdsBody(BaseModel):
    ids: list[int]


class Flag(BaseModel):
    value: bool


class Credentials(BaseModel):
    token: str | None = None
    account_id: str | None = None


def open_in_finder(path: str, reveal: bool = False) -> bool:
    """Finder / Explorer: only when the server runs on the computer itself (not in Docker)."""
    import subprocess
    import sys
    if sys.platform == "win32":
        p = str(Path(path))
        subprocess.run(["explorer", f"/select,{p}"] if reveal else ["explorer", p], check=False, timeout=10)
        return True
    if sys.platform != "darwin":
        return False
    subprocess.run(["open", *(["-R"] if reveal else []), path], check=False, timeout=10)
    return True


def _channel_json(ch: Channel, counts: dict) -> dict:
    c = counts.get(ch.username, {})
    return {
        "id": ch.id, "username": ch.username, "title": ch.title, "url": f"https://t.me/c/{ch.tg_id}" if ch.username == f"id{ch.tg_id}" else f"https://t.me/{ch.username}",
        "category": ch.category, "mode": ch.mode, "via": ch.via or "web", "enabled": ch.enabled, "state": ch.state,
        "last_error": ch.last_error,
        "last_checked_at": ch.last_checked_at.isoformat() + "Z" if ch.last_checked_at else None,
        "last_seen_post_id": ch.last_seen_post_id,
        "found": ch.found_count, "skipped": ch.skipped_count,
        "added_by": ch.added_by or "link", "in_account": bool(ch.in_account),
        "downloaded": c.get("downloaded", 0), "ready": c.get("ready", 0), "review": c.get("review", 0),
        "last_skipped": json.loads(ch.last_skipped) if ch.last_skipped else [],
    }


def create_app(settings, db, worker: FactoryWorker | None = None, start_worker: bool = True,
               instagram=None) -> FastAPI:
    worker = worker or FactoryWorker(settings, db)
    labels = category_labels()
    # Instagram AutoPoster: an isolated module, only loaded when switched on in config.yaml
    if instagram is None and settings.get("instagram_web.enabled", False):
        from app.instagram_web.service import InstagramService
        instagram = InstagramService(settings, db)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
            if instagram is not None:
                instagram.start_loop()
        yield
        worker.stop()
        if instagram is not None:
            instagram.shutdown()

    app = FastAPI(title="Контент-завод", lifespan=lifespan)
    app.state.worker = worker
    app.state.instagram = instagram
    if instagram is not None:
        instagram.editor = worker.editor
        from app.instagram_web.web import router as instagram_router
        app.include_router(instagram_router(instagram))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def check_category(cat: str) -> str:
        if cat not in labels:
            raise HTTPException(400, f"неизвестная категория: {cat}")
        return cat

    # ---------- pages ----------
    @app.get("/")
    def index():
        return FileResponse(STATIC / "dashboard.html", headers=NO_CACHE)

    @app.get("/sources")
    def sources_page():
        return FileResponse(STATIC / "sources.html", headers=NO_CACHE)

    @app.get("/videos")
    def videos_page():
        return FileResponse(STATIC / "videos.html", headers=NO_CACHE)

    @app.get("/publish")
    def publish_page():
        # Instagram API publishing is not used (the person posts by hand from «Готовые видео»).
        return RedirectResponse("/ready")

    @app.get("/editor")
    def editor_page():
        return FileResponse(STATIC / "editor.html", headers=NO_CACHE)

    @app.get("/instagram")
    def instagram_page():
        return FileResponse(STATIC / "instagram.html", headers=NO_CACHE)

    @app.get("/api/instagram-web/enabled")
    def instagram_enabled():
        return {"enabled": instagram is not None}

    @app.get("/backgrounds")
    def backgrounds_page():
        return FileResponse(STATIC / "backgrounds.html", headers=NO_CACHE)

    @app.get("/calendar")
    def calendar_page():
        return FileResponse(STATIC / "calendar.html", headers=NO_CACHE)

    @app.get("/ready")
    def ready_page():
        return FileResponse(STATIC / "ready.html", headers=NO_CACHE)

    # ---------- channels ----------
    def counts_by_channel(s) -> dict:
        rows = s.execute(select(ContentItem.channel, ContentItem.status, func.count())
                         .where(ContentItem.source == "telegram")
                         .group_by(ContentItem.channel, ContentItem.status))
        out: dict = {}
        downloaded_states = {Status.DOWNLOADED, Status.ANALYZING, Status.APPROVED, Status.MANUAL_REVIEW,
                             Status.PROCESSING, Status.PROCESSED, Status.READY_TO_PUBLISH, Status.PUBLISHED,
                             Status.REJECTED, Status.DUPLICATE}
        for channel, status, n in rows:
            c = out.setdefault(channel, {"downloaded": 0, "ready": 0, "review": 0})
            if status in downloaded_states:
                c["downloaded"] += n
            if status == Status.READY_TO_PUBLISH:
                c["ready"] += n
            if status == Status.MANUAL_REVIEW:
                c["review"] += n
        return out

    @app.get("/api/channels")
    def list_channels():
        with db.session() as s:
            counts = counts_by_channel(s)
            chans = s.scalars(select(Channel).where(Channel.hidden.is_not(True)).order_by(Channel.id)).all()
            return {"channels": [_channel_json(c, counts) for c in chans], "worker": worker.status()}

    @app.post("/api/channels")
    def add_channels(body: AddChannels):
        names = parse_links(body.links)
        if not names:
            raise HTTPException(400, "не нашёл ни одной ссылки вида t.me/канал или @канал")
        if body.mode not in MODES:
            raise HTTPException(400, "mode: import или discovery")
        check_category(body.category)
        added, existing, restored = [], [], []
        with db.session() as s:
            have = {c.username.lower(): c for c in s.scalars(select(Channel).where(Channel.platform == "telegram"))}
            for n in names:
                if n.lower() in have:
                    ch = have[n.lower()]
                    if ch.hidden:  # added again by link: back in the factory
                        ch.hidden, ch.enabled, ch.added_by = False, True, "link"
                        restored.append(ch.username)
                    existing.append(ch.id)
                    continue
                ch = Channel(username=n, category=body.category, mode=body.mode, state="new")
                s.add(ch)
                s.flush()
                added.append(ch.id)
        for cid in added + existing:
            worker.enqueue_channel(cid)
        return {"added": len(added), "already_there": len(existing), "queued": len(added) + len(existing),
                "restored": restored}

    @app.patch("/api/channels/{cid}")
    def patch_channel(cid: int, body: ChannelPatch):
        with db.session() as s:
            ch = s.get(Channel, cid)
            if ch is None:
                raise HTTPException(404, "нет такого канала")
            if body.category is not None:
                ch.category = check_category(body.category)
            if body.mode is not None:
                if body.mode not in MODES:
                    raise HTTPException(400, "mode: import или discovery")
                ch.mode = body.mode
            if body.via is not None:
                if body.via not in ("web", "api"):
                    raise HTTPException(400, "via: web или api")
                ch.via = body.via
            if body.enabled is not None:
                ch.enabled = body.enabled
            enabled = ch.enabled
        if body.enabled:
            worker.enqueue_channel(cid)
        return {"ok": True, "enabled": enabled}

    @app.delete("/api/channels/{cid}")
    def delete_channel(cid: int):
        """The channel leaves the factory for good: not parsed, not listed, not brought back by
        «Обновить список». Videos found earlier stay; adding the link again restores the channel."""
        with db.session() as s:
            ch = s.get(Channel, cid)
            if ch is None:
                raise HTTPException(404, "нет такого канала")
            ch.hidden, ch.enabled, ch.state = True, False, "ok"
            name = ch.username
        from app.processing.cleanup import drop_channel_videos
        res = drop_channel_videos(settings, db, name)
        return {"ok": True, **res}

    @app.post("/api/channels/delete-many")
    def delete_channels(body: IdsBody):
        total = {"channels": 0, "items": 0, "freed": 0}
        for cid in body.ids:
            try:
                r = delete_channel(cid)
            except HTTPException:
                continue
            total["channels"] += 1
            total["items"] += r["items"]
            total["freed"] += r["freed"]
        return total

    @app.get("/api/channels/hidden")
    def hidden_channels():
        return {"channels": tg_account.hidden(db)}

    @app.post("/api/channels/{cid}/restore")
    def restore_channel(cid: int):
        with db.session() as s:
            ch = s.get(Channel, cid)
            if ch is None:
                raise HTTPException(404, "нет такого канала")
            ch.hidden = False
            enable = ch.added_by != "account"  # a channel from the account comes back unticked
            ch.enabled = enable
        if enable:
            worker.enqueue_channel(cid)
        return {"ok": True}

    @app.post("/api/channels/{cid}/check")
    def check_channel(cid: int):
        with db.session() as s:
            ch = s.get(Channel, cid)
            if ch is None:
                raise HTTPException(404, "нет такого канала")
            if not ch.enabled:
                raise HTTPException(409, "канал выключен")
        return {"queued": worker.enqueue_channel(cid)}

    @app.post("/api/channels/check-all")
    def check_all():
        return {"queued": worker.enqueue_all()}

    # ---------- parsing calendar ----------
    from app.sources.schedule import ScheduleError
    cal = worker.calendar

    def c_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ScheduleError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/schedules")
    def schedules():
        return cal.list()

    @app.post("/api/schedules")
    def schedule_add(body: ScheduleBody):
        sid = c_call(cal.create, body.channel_id, body.days or [], body.times or [],
                     True if body.enabled is None else body.enabled)
        return {"id": sid, **cal.list()}

    @app.patch("/api/schedules/{sid}")
    def schedule_patch(sid: int, body: ScheduleBody):
        c_call(cal.update, sid, body.days, body.times, body.enabled)
        return cal.list()

    @app.delete("/api/schedules/{sid}")
    def schedule_delete(sid: int):
        cal.delete(sid)
        return cal.list()

    @app.get("/api/worker")
    def worker_status():
        return worker.status()

    # ---------- videos ----------
    @app.get("/api/categories")
    def categories():
        return {"categories": [{"id": k, "label": v} for k, v in labels.items()]}

    @app.get("/api/dashboard")
    def dashboard():
        from datetime import timedelta
        from app.database.models import utcnow
        from app.processing import maintenance
        from app.processing.storage import disk_usage_percent
        day = utcnow() - timedelta(hours=24)
        with db.session() as s:
            by = dict(s.execute(select(ContentItem.status, func.count()).group_by(ContentItem.status)).all())
            cnt = lambda *w: s.scalar(select(func.count()).select_from(ContentItem).where(*w)) or 0  # noqa: E731
            g = lambda *st: sum(by.get(x, 0) for x in st)  # noqa: E731
            tiles = {
                "channels": s.scalar(select(func.count()).select_from(Channel)) or 0,
                "downloaded": cnt(ContentItem.original_file.is_not(None)),
                "in_work": g(Status.DISCOVERED, Status.DOWNLOADING, Status.DOWNLOADED, Status.ANALYZING,
                             Status.APPROVED, Status.PROCESSING),
                "review": g(Status.MANUAL_REVIEW),
                "to_edit": cnt(ContentItem.status == Status.READY_TO_PUBLISH, ContentItem.ready_path.is_(None)),
                "in_ready": cnt(ContentItem.ready_path.is_not(None), ContentItem.posted_at.is_(None)),
                "published": cnt(ContentItem.posted_at.is_not(None)),
                "errors": g(Status.FAILED),
                "downloaded_24h": cnt(ContentItem.downloaded_at >= day),
                "ready_24h": cnt(ContentItem.ready_at >= day),
            }
            errors = [{"id": r.id, "title": r.title, "channel": r.channel, "error": (r.last_error or "")[:200]}
                      for r in s.scalars(select(ContentItem).where(ContentItem.status == Status.FAILED)
                                         .order_by(ContentItem.id.desc()).limit(5))]
            bad_channels = [{"username": c.username, "error": (c.last_error or "")[:200]}
                            for c in s.scalars(select(Channel).where(Channel.state == "error"))]
            recent = [{"id": r.id, "title": r.title, "thumbnail": f"/media/thumb/{r.id}", "category": r.category,
                       "category_label": labels.get(r.category or "", r.category)}
                      for r in s.scalars(select(ContentItem).where(ContentItem.ready_path.is_not(None))
                                         .order_by(ContentItem.ready_at.desc()).limit(8))]
        lb = maintenance.last_backup(settings)
        from datetime import datetime as _dt
        return {"tiles": tiles, "errors": errors, "bad_channels": bad_channels, "recent_ready": recent,
                "worker": worker.status(), "editor_queue": ed_queue(),
                "disk_percent": round(disk_usage_percent(settings.data_dir), 1),
                "last_backup": _dt.fromtimestamp(lb.stat().st_mtime).isoformat() if lb else None}

    def ed_queue():
        try:
            q = worker.editor.queue_status()
            return {"queued": q["queued"] + q["rendering"], "error": q["error"]}
        except Exception:
            return None

    # ---------- disk space ----------
    from app.processing import cleanup as cleanup_mod

    @app.get("/api/storage")
    def storage(days: int = 30):
        return cleanup_mod.report(settings, db, days)

    @app.post("/api/storage/clean")
    def storage_clean(body: CleanBody):
        if body.action not in cleanup_mod.ACTIONS:
            raise HTTPException(404, "нет такой очистки")
        if not body.confirm:
            return cleanup_mod.preview(settings, db, body.action, body.days)
        return cleanup_mod.run(settings, db, body.action, body.days)

    @app.get("/api/summary")
    def summary():
        with db.session() as s:
            by_status = dict(s.execute(select(ContentItem.status, func.count()).group_by(ContentItem.status)).all())
            channels = s.scalar(select(func.count()).select_from(Channel)) or 0
        g = lambda *st: sum(by_status.get(x, 0) for x in st)  # noqa: E731
        return {"channels": channels, "total": sum(by_status.values()),
                "in_work": g(Status.DISCOVERED, Status.DOWNLOADING, Status.DOWNLOADED, Status.ANALYZING,
                             Status.APPROVED, Status.PROCESSING),
                "review": g(Status.MANUAL_REVIEW), "ready": g(Status.READY_TO_PUBLISH, Status.PROCESSED),
                "rejected": g(Status.REJECTED, Status.DUPLICATE, Status.FAILED), "worker": worker.status()}

    @app.get("/api/items")
    def list_items(category: str | None = None, status: str | None = None, channel: str | None = None,
                   q: str | None = None, limit: int = 100, offset: int = 0, show_all: bool = False):
        search = q
        with db.session() as s:
            q = select(ContentItem)
            if not show_all and not status:
                # videos already in «Готовые» (or posted), duplicates and the trash are not shown in the work lists
                q = q.where(ContentItem.ready_path.is_(None), ContentItem.posted_at.is_(None),
                            ContentItem.status != Status.DUPLICATE, ContentItem.trashed_from.is_(None))
            if search:
                like = f"%{search}%"
                q = q.where(ContentItem.title.ilike(like) | ContentItem.caption.ilike(like)
                            | ContentItem.transcript.ilike(like) | ContentItem.channel.ilike(like))
            if category:
                q = q.where(ContentItem.category == category)
            if status:
                q = q.where(ContentItem.status == Status(status))
            if channel:
                q = q.where(ContentItem.channel == channel)
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            rows = s.scalars(q.order_by(ContentItem.id.desc()).limit(min(limit, 500)).offset(offset)).all()
            thumbs = {v.content_id for v in s.scalars(
                select(Variant).where(Variant.content_id.in_([r.id for r in rows])))}
            thumbs |= {r.id for r in rows if r.original_file}
            base = select(ContentItem.category, func.count())
            if not show_all and not status:
                base = base.where(ContentItem.ready_path.is_(None), ContentItem.posted_at.is_(None),
                                  ContentItem.status != Status.DUPLICATE, ContentItem.trashed_from.is_(None))
            cat_counts = dict(s.execute(base.group_by(ContentItem.category)).all())
            return {
                "total": total,
                "category_counts": {k or "none": v for k, v in cat_counts.items()},
                "items": [{
                    "id": r.id, "title": r.title, "source": r.source, "channel": r.channel,
                    "source_url": r.source_url, "status": r.status.value, "rights": r.rights_status.value,
                    "category": r.category, "category_label": labels.get(r.category or "", r.category),
                    "category_confidence": r.category_confidence, "category_reason": r.category_reason,
                    "category_source": r.category_source, "content_score": r.content_score,
                    "decision_reason": r.decision_reason or r.last_error, "duration": r.duration,
                    "thumbnail": f"/media/thumb/{r.id}" if r.id in thumbs else None,
                    "has_video": r.id in thumbs,
                } for r in rows],
            }

    @app.get("/api/items/{item_id}")
    def item_detail(item_id: int):
        with db.session() as s:
            r = s.get(ContentItem, item_id)
            if r is None:
                raise HTTPException(404, "нет такого ролика")
            rendered = s.scalars(select(Variant).where(Variant.content_id == item_id)).first()
            rec = json.loads(r.analysis_json) if r.analysis_json else {}
            pub = s.scalars(select(Publication).where(Publication.content_id == item_id,
                                                      Publication.status != PublicationStatus.CANCELLED)).first()
            return {
                "id": r.id, "title": r.title, "caption": r.caption, "source": r.source, "channel": r.channel,
                "source_url": r.source_url, "status": r.status.value, "rights": r.rights_status.value,
                "license": r.license, "attribution": r.attribution,
                "category": r.category, "category_label": labels.get(r.category or "", r.category),
                "category_confidence": r.category_confidence, "category_reason": r.category_reason,
                "category_source": r.category_source, "decision_reason": r.decision_reason or r.last_error,
                "transcript": r.transcript, "summary": r.summary, "hook": r.hook, "duration": r.duration,
                "scores": {"Итог": r.content_score, "Вирусность": r.viral_score, "Качество": r.quality_score,
                           "Оригинальность": r.originality_score, "Риск прав": r.rights_risk_score},
                "moderation_flags": r.moderation_flags, "ai_provider": r.ai_provider,
                "decision_rule": (rec.get("decision") or {}).get("rule"),
                "video": f"/media/video/{r.id}" if (rendered or r.original_file) else None,
                "video_kind": "готовый 9:16" if rendered else ("оригинал" if r.original_file else None),
                "can_review": r.status == Status.MANUAL_REVIEW,
                "description": r.post_description,
                "source_quality": {"width": r.width, "height": r.height, "fps": r.fps, "codec": r.codec,
                                   "bitrate": r.bitrate, "filesize": r.filesize},
                "ready_path": r.ready_path, "ready_at": r.ready_at.isoformat() + "Z" if r.ready_at else None,
                "posted_at": r.posted_at.isoformat() + "Z" if r.posted_at else None, "posted_note": r.posted_note,
                "can_publish": r.status == Status.READY_TO_PUBLISH and pub is None,
                "can_ready": r.status == Status.READY_TO_PUBLISH or bool(r.ready_path),
                "publication": {"id": pub.id, "status": pub.status.value, "permalink": pub.permalink}
                if pub else None,
            }

    @app.post("/api/items/{item_id}/to-ready")
    def item_to_ready(item_id: int, body: DescriptionBody | None = None):
        """«В готовые» for any downloaded video: copies the edited/rendered file, or — when there is none —
        renders it from the source now (its editor settings, else the «Размытый фон» template)."""
        from app.database.models import EditProject, EditTemplate
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None or not item.original_file:
                raise HTTPException(404, "ролик ещё не скачан")
            if body and body.description is not None:
                item.post_description = body.description.strip() or None
            project = s.get(EditProject, item_id)
            tpl = s.scalars(select(EditTemplate).where(EditTemplate.builtin == "blur_center")).first()
        if project is not None and project.render_state == "done":
            return {"ok": True, "mode": "copy", "path": r_call(ready_mod.add, settings, db, item_id, labels)}
        if project is None and tpl is not None:
            e_call(ed.save, item_id, json.loads(tpl.spec), tpl.id)
        e_call(ed.queue_export, item_id, True)
        return {"ok": True, "mode": "render"}

    @app.post("/api/items/restore-many")
    def restore_many(body: IdsBody):
        n = 0
        with db.session() as s:
            for item in s.scalars(select(ContentItem).where(ContentItem.id.in_(body.ids), ContentItem.trashed_from.is_not(None))):
                item.status, item.trashed_from, item.trashed_at = Status(item.trashed_from), None, None
                item.decision_reason, item.rejection_reason = "возвращён из корзины", None
                n += 1
        return {"restored": n}

    @app.post("/api/ready/download")
    def ready_download(body: IdsBody):
        import subprocess
        if not body.ids:
            raise HTTPException(400, "не выбрано ни одного ролика")
        res = ready_mod.download(settings, db, body.ids)
        if res["files"]:
            open_in_finder(res["folder"])
        return res

    @app.post("/api/items/{item_id}/trash")
    def trash_item(item_id: int):
        """«В корзину»: the video leaves the lists (status REJECTED); restore puts it back."""
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None:
                raise HTTPException(404, "нет такого ролика")
            drop_from_instagram(item_id, "ролик удалён в корзину")
            if item.status != Status.REJECTED:
                item.trashed_from, item.trashed_at = item.status.value, utcnow()
                item.status = Status.REJECTED
                item.decision_reason = item.rejection_reason = "убран в корзину"
        return {"ok": True}

    @app.post("/api/items/{item_id}/restore")
    def restore_item(item_id: int):
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None or not item.trashed_from:
                raise HTTPException(409, "ролик не в корзине")
            item.status, item.trashed_from, item.trashed_at = Status(item.trashed_from), None, None
            item.decision_reason, item.rejection_reason = "возвращён из корзины", None
        return {"ok": True}

    class ReviewBody(BaseModel):
        note: str | None = None

    @app.post("/api/items/{item_id}/{action}")
    def review_item(item_id: int, action: str, body: ReviewBody | None = None):
        from app.processing import review
        if action not in ("approve", "reject"):
            raise HTTPException(404, "нет такого действия")
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None:
                raise HTTPException(404, "нет такого ролика")
            try:
                (review.approve if action == "approve" else review.reject)(settings, item, body.note if body else None)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            status = item.status.value
        if action == "approve":
            worker.wake()
        return {"ok": True, "status": status}

    @app.get("/media/video/{item_id}")
    def video(item_id: int):
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            # what «Готовые» holds, else the editor's export, else the automatic render, else the source
            rendered = ready_mod.rendered_video(s, item_id)
            candidates = [item.ready_path if item else None, str(rendered) if rendered else None,
                          item.original_file if item else None]
        for c in candidates:
            if c and Path(c).is_file():
                return FileResponse(c, media_type="video/mp4" if c.endswith(".mp4") else None)
        raise HTTPException(404, "видео нет")

    @app.get("/media/original/{item_id}")
    def original(item_id: int):
        """The downloaded source, for the editor's live preview in the browser."""
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            path = Path(item.original_file) if item and item.original_file else None
        if not path or not path.is_file():
            raise HTTPException(404, "исходника нет")
        return FileResponse(path, media_type="video/mp4" if path.suffix.lower() in (".mp4", ".m4v") else None)

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: int, body: ItemPatch):
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None:
                raise HTTPException(404, "нет такого ролика")
            if body.category is not None:
                item.category = check_category(body.category)
                item.category_source, item.category_confidence = "manual", 100.0
                item.category_reason = "указано вручную"
            if body.description is not None:
                item.post_description = body.description.strip() or None
        return {"ok": True}

    @app.get("/media/thumb/{item_id}")
    def thumbnail(item_id: int):
        """The file in «Готовые» if the video is there (so the preview is exactly what will be posted),
        else the editor's/automatic render thumbnail, else a frame of the downloaded original."""
        with db.session() as s:
            it0 = s.get(ContentItem, item_id)
            ready_file = Path(it0.ready_path) if it0 and it0.ready_path else None
        if ready_file and ready_file.is_file():
            from app.media.ffmpeg import thumbnail as make_thumb
            thumb = settings.data("thumbnails") / f"ready_{item_id}_{int(ready_file.stat().st_mtime)}.jpg"
            if not thumb.is_file():
                for old in settings.data("thumbnails").glob(f"ready_{item_id}_*.jpg"):
                    old.unlink(missing_ok=True)
                try:
                    make_thumb(ready_file, thumb, 1.0, 540)
                except Exception:
                    thumb = None
            if thumb and thumb.is_file():
                return FileResponse(thumb, headers={"Cache-Control": "no-cache"})
        with db.session() as s:
            # the editor's export first: its thumbnail shows the edited look
            v = s.scalars(select(Variant).where(Variant.content_id == item_id)
                          .order_by((Variant.profile == "edited").desc(), Variant.id.desc())).first()
            item = s.get(ContentItem, item_id)
            path = Path(v.thumbnail) if v and v.thumbnail else None
            original = Path(item.original_file) if item and item.original_file else None
            duration = item.duration if item else None
        if (not path or not path.is_file()) and original and original.is_file():
            path = settings.data("thumbnails") / f"preview_{item_id}.jpg"
            if not path.is_file():
                from app.media.ffmpeg import thumbnail as make_thumb
                try:
                    make_thumb(original, path, min(1.0, (duration or 2) / 2), 270)
                except Exception:
                    path = None
        if not path or not path.is_file():
            raise HTTPException(404, "миниатюры нет")
        return FileResponse(path)

    # ---------- Telegram API (optional; the owner signs in here, keys and session stay on this Mac) ----------
    from app.sources.telegram_api import TelegramAPIError
    tg_api = worker.telegram.api

    def t_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except TelegramAPIError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    @app.get("/api/telegram-api")
    def telegram_api_status():
        return tg_api.status()

    @app.post("/api/telegram-api/keys")
    def telegram_api_keys(body: TelegramKeys):
        from app.config.envfile import set_env_values
        api_id, api_hash = body.api_id.strip(), body.api_hash.strip()
        if not api_id.isdigit() or len(api_hash) < 16:
            raise HTTPException(400, "api_id — число, api_hash — строка из 32 символов (my.telegram.org)")
        set_env_values(settings.env_file, {"TELEGRAM_API_ID": api_id, "TELEGRAM_API_HASH": api_hash})
        tg_api.reset()
        return tg_api.status()

    @app.post("/api/telegram-api/send-code")
    def telegram_api_send_code(body: TelegramPhone):
        return t_call(tg_api.send_code, body.phone)

    @app.post("/api/telegram-api/resend")
    def telegram_api_resend():
        return t_call(tg_api.resend_code)

    @app.post("/api/telegram-api/sign-in")
    def telegram_api_sign_in(body: TelegramCode):
        if not (body.code or body.password):
            raise HTTPException(400, "введите код из Telegram")
        return t_call(tg_api.sign_in, body.code.strip() if body.code else None, body.password)

    # «Мои каналы»: subscriptions of the signed-in account, ticked ones are parsed
    from app.sources import tg_account

    @app.get("/api/telegram-api/channels")
    def tg_channels():
        return {"channels": tg_account.listing(db), "status": tg_api.status()}

    @app.post("/api/telegram-api/channels/refresh")
    def tg_channels_refresh():
        res = t_call(tg_account.refresh, db, tg_api)
        return {**res, "channels": tg_account.listing(db)}

    @app.post("/api/telegram-api/channels/select")
    def tg_channels_select(body: SelectBody):
        for cid in tg_account.select_channels(db, body.ids, body.enabled):
            worker.enqueue_channel(cid)
        return {"channels": tg_account.listing(db)}

    @app.get("/api/telegram-api/avatar/{cid}")
    def tg_avatar(cid: int):
        folder = settings.data("tg_avatars")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{cid}.jpg"
        if not path.is_file() and not (folder / f"{cid}.none").exists():
            with db.session() as s:
                ch = s.get(Channel, cid)
                name = ch.username if ch else None
            got = None
            if name and tg_api.authorized():
                try:
                    got = tg_api.avatar(name, path)
                except Exception:
                    got = None
            if got and got != path and got.is_file():
                got.replace(path)
            if not path.is_file():
                (folder / f"{cid}.none").touch()  # no photo: do not ask Telegram again
        if not path.is_file():
            raise HTTPException(404, "нет аватарки")
        return FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/telegram-api/logout")
    def telegram_api_logout():
        return tg_api.logout()

    # ---------- editor (stage 7) ----------
    from app.editor import assets as assets_mod
    from app.editor.service import EditorError, probe_duration
    ed = worker.editor

    def e_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except EditorError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc
        except assets_mod.AssetError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ready_mod.ReadyError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    @app.get("/api/editor/items")
    def editor_items(category: str | None = None, only_new: bool = False, checked_channels: bool = False):
        from app.database.models import EditProject
        with db.session() as s:
            # every downloaded video (not in the trash, not an exact copy of another file)
            q = select(ContentItem).where(ContentItem.original_file.is_not(None), ContentItem.trashed_from.is_(None),
                                          ContentItem.status.not_in([Status.DUPLICATE, Status.DOWNLOADING]))
            if category:
                q = q.where(ContentItem.category == category)
            if only_new:
                q = q.where(ContentItem.ready_path.is_(None), ContentItem.posted_at.is_(None))
            if checked_channels:  # only videos of the channels ticked right now
                q = q.where(ContentItem.channel.in_(select(Channel.username).where(Channel.enabled.is_(True))))
            rows = list(s.scalars(q.order_by(ContentItem.id.desc()).limit(500)))
            projects = {p.content_id: p for p in s.scalars(select(EditProject))}
            return {"items": [{
                "id": r.id, "title": r.title, "category": r.category,
                "category_label": labels.get(r.category or "", r.category), "status": r.status.value,
                "rights": r.rights_status.value, "duration": r.duration, "thumbnail": f"/media/thumb/{r.id}",
                "channel": r.channel, "source": r.source, "in_ready": bool(r.ready_path),
                "edit_state": projects[r.id].render_state if r.id in projects else None,
                "has_text": r.has_screen_text, "screen_text": r.screen_text,
                "edited": r.id in projects,
            } for r in rows]}

    @app.get("/api/editor/assets")
    def editor_assets():
        return {"assets": ed.asset_list()}

    @app.post("/api/editor/assets")
    async def editor_upload(file: UploadFile = File(...)):
        data = await file.read()
        a = e_call(assets_mod.save_upload, settings, db, file.filename or "file", data)
        return {"id": a.id, "kind": a.kind, "name": a.name, "width": a.width, "height": a.height,
                "duration": a.duration, "key_color": a.key_color,
                "preview": f"/api/editor/assets/{a.id}/preview" if a.kind != "audio" else None,
                     "file": f"/api/editor/assets/{a.id}/file"}

    @app.get("/api/editor/assets/{aid}/preview")
    def editor_asset_preview(aid: str):
        from app.database.models import EditAsset
        with db.session() as s:
            a = s.get(EditAsset, aid)
            p = assets_mod.preview_image(a) if a else None
        if not p or not p.is_file():
            raise HTTPException(404, "нет превью")
        return FileResponse(p)

    @app.get("/api/editor/assets/{aid}/file")
    def editor_asset_file(aid: str):
        from app.database.models import EditAsset
        with db.session() as s:
            a = s.get(EditAsset, aid)
            path = Path(a.path) if a else None
        if not path or not path.is_file():
            raise HTTPException(404, "файла нет")
        return FileResponse(path)

    # ---------- «Фоны» ----------
    from app.editor import backgrounds as bg_mod

    @app.get("/api/backgrounds")
    def backgrounds_list(used: str | None = None, tag: str | None = None):
        return bg_mod.listing(db, used, tag)

    @app.post("/api/backgrounds")
    async def backgrounds_upload(files: list[UploadFile] = File(...), tag: str | None = None):
        added, errors = [], {}
        for f in files:
            try:
                added.append(bg_mod.add(settings, db, f.filename or "file", await f.read(), tag))
            except (assets_mod.AssetError, bg_mod.BackgroundError) as exc:
                errors[f.filename or "?"] = str(exc)
        return {"added": added, "errors": errors}

    @app.post("/api/backgrounds/delete")
    def backgrounds_delete(body: BgDelete):
        return bg_mod.delete(db, body.ids, body.all_used, body.confirm)

    @app.post("/api/backgrounds/tag")
    def backgrounds_tag(body: BgTag):
        return {"updated": bg_mod.set_tag(db, body.ids, body.tag)}

    @app.get("/api/editor/auto-templates")
    def auto_templates_get():
        return {"mapping": ed.auto_templates()}

    @app.put("/api/editor/auto-templates")
    def auto_templates_put(body: AutoTemplates):
        mapping = ed.set_auto_templates(body.mapping)
        worker.wake()
        return {"mapping": mapping}

    @app.get("/api/editor/queue")
    def editor_queue():
        return {**ed.queue_status(), "exporting": worker.exporting}

    @app.get("/api/editor/voices")
    def editor_voices():
        return {"voices": ed.voices.list()}

    @app.post("/api/editor/tts")
    def editor_tts(body: TTSBody):
        p = e_call(ed.tts, body.text, body.voice, body.speed)
        return {"url": f"/api/editor/tts/{p.name}", "duration": probe_duration(p)}

    @app.get("/api/editor/tts/{name}")
    def editor_tts_file(name: str):
        p = ed.voices.cache / Path(name).name
        if not p.is_file():
            raise HTTPException(404, "озвучки нет")
        return FileResponse(p, media_type="audio/wav")

    @app.get("/api/editor/templates")
    def editor_templates():
        return {"templates": ed.templates()}

    @app.post("/api/editor/templates")
    def editor_template_save(body: TemplateBody, template_id: int | None = None):
        return {"id": e_call(ed.save_template, body.name, body.spec, template_id)}

    @app.delete("/api/editor/templates/{tid}")
    def editor_template_delete(tid: int):
        ed.delete_template(tid)
        return {"ok": True}

    @app.post("/api/editor/templates/{tid}/apply")
    def editor_template_apply(tid: int, body: ApplyBody):
        res = e_call(ed.apply_template, tid, body.item_ids, body.export, body.background, body.background_tag)
        return res

    @app.post("/api/editor/fit-window")
    def editor_fit_window(body: SpecBody):
        return {"spec": e_call(ed.fit_to_window, body.spec)}

    @app.get("/api/editor/cache/{name}")
    def editor_cache_file(name: str):
        p = ed.cache / Path(name).name
        if not p.is_file():
            raise HTTPException(404, "превью устарело")
        return FileResponse(p)

    @app.get("/api/editor/{item_id}")
    def editor_project(item_id: int):
        return e_call(ed.project, item_id)

    @app.put("/api/editor/{item_id}")
    def editor_save(item_id: int, body: SpecBody):
        return {"spec": e_call(ed.save, item_id, body.spec)}

    @app.delete("/api/editor/{item_id}")
    def editor_reset(item_id: int):
        ed.reset(item_id)
        return {"ok": True}

    @app.post("/api/editor/{item_id}/frame")
    def editor_frame(item_id: int, body: SpecBody):
        try:
            p = e_call(ed.frame, item_id, body.spec, body.at or 0.0)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"не получилось построить кадр: {str(exc)[-300:]}") from exc
        return Response(p.read_bytes(), media_type="image/jpeg")

    @app.post("/api/editor/{item_id}/preview")
    def editor_preview(item_id: int, body: SpecBody):
        try:
            p = e_call(ed.preview, item_id, body.spec)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"не получилось собрать превью: {str(exc)[-300:]}") from exc
        return {"url": f"/api/editor/cache/{p.name}"}

    @app.post("/api/editor/{item_id}/export")
    def editor_export(item_id: int, body: SpecBody):
        e_call(ed.save, item_id, body.spec)
        e_call(ed.queue_export, item_id, body.to_ready)
        return {"ok": True}

    # ---------- «Готовые видео»: manual publishing ----------
    def r_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ready_mod.ReadyError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    def ready_json(r: ContentItem, rendered: dict) -> dict:
        exists = bool(r.ready_path and Path(r.ready_path).is_file())
        return {
            "id": r.id, "title": r.title, "channel": r.channel, "source": r.source, "category": r.category,
            "category_label": labels.get(r.category or "", r.category), "rights": r.rights_status.value,
            "duration": r.duration, "description": r.post_description,
            "src": {"width": r.width, "height": r.height, "fps": r.fps},
            "rendered": rendered.get(r.id, False),
            "ready_path": r.ready_path, "ready_name": Path(r.ready_path).name if r.ready_path else None,
            "ready_missing": bool(r.ready_path) and not exists,
            "ready_at": r.ready_at.isoformat() + "Z" if r.ready_at else None,
            "posted_at": r.posted_at.isoformat() + "Z" if r.posted_at else None, "posted_note": r.posted_note,
            "thumbnail": f"/media/thumb/{r.id}?v={int(r.ready_at.timestamp()) if r.ready_at else 0}",
            "video": f"/media/video/{r.id}?v={int(r.ready_at.timestamp()) if r.ready_at else 0}",
            "has_text": r.has_screen_text, "screen_text": r.screen_text,
        }

    @app.get("/api/ready")
    def ready_list():
        with db.session() as s:
            rows = ready_mod.items(s)
            rendered = {v.content_id: True for v in s.scalars(
                select(Variant).where(Variant.content_id.in_([r.id for r in rows])))}
            return {"folder": str(ready_mod.ready_dir(settings)), "items": [ready_json(r, rendered) for r in rows]}

    @app.post("/api/ready/{item_id}")
    def ready_add(item_id: int, body: DescriptionBody | None = None):
        path = r_call(ready_mod.add, settings, db, item_id, labels, body.description if body else None)
        return {"ok": True, "path": path}

    def drop_from_instagram(item_id: int, why: str) -> None:
        if instagram is not None:
            instagram.queue.cancel_for(item_id, why)

    @app.delete("/api/ready/{item_id}")
    def ready_remove(item_id: int):
        """«↩ Убрать из готовых»: the copy leaves the folder, the video goes back to the editor."""
        r_call(ready_mod.remove, db, item_id)
        drop_from_instagram(item_id, "убран из «Готовых»")
        return {"ok": True}

    @app.put("/api/items/{item_id}/description")
    def put_description(item_id: int, body: DescriptionBody):
        r_call(ready_mod.write_description, db, item_id, body.description or "")
        return {"ok": True}

    @app.post("/api/ready/{item_id}/posted")
    def ready_posted(item_id: int, body: PostedBody):
        from datetime import datetime, time as dtime, timezone
        from zoneinfo import ZoneInfo
        when = None
        if body.posted and body.date:
            try:
                day = datetime.fromisoformat(body.date)
            except ValueError as exc:
                raise HTTPException(400, "дата: формат ГГГГ-ММ-ДД") from exc
            if day.time() == dtime(0):
                day = day.replace(hour=12)
            tz = ZoneInfo(settings.get("publishing.timezone", "Europe/Moscow"))
            when = day.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
        r_call(ready_mod.mark_posted, db, item_id, body.posted, when, body.note)
        return {"ok": True}

    @app.post("/api/ready/{item_id}/reveal")
    def ready_reveal(item_id: int):
        """«Показать в Finder» (the server runs on this Mac)."""
        import subprocess
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            path = item.ready_path if item else None
        if not path or not Path(path).is_file():
            raise HTTPException(404, "файла в папке готовых нет")
        open_in_finder(path, reveal=True)
        return {"ok": True}

    @app.post("/api/ready-folder/open")
    def ready_open_folder():
        import subprocess
        folder = ready_mod.ready_dir(settings)
        folder.mkdir(parents=True, exist_ok=True)
        open_in_finder(str(folder))
        return {"ok": True}

    @app.get("/media/download/{item_id}")
    def download(item_id: int):
        with db.session() as s:
            item = s.get(ContentItem, item_id)
            if item is None:
                raise HTTPException(404, "нет такого ролика")
            path = Path(item.ready_path) if item.ready_path and Path(item.ready_path).is_file() else ready_mod.rendered_video(s, item_id)
            name = path.name if path and item.ready_path else (ready_mod.file_name(item, labels) if path else None)
        if not path:
            raise HTTPException(404, "готового ролика нет")
        return FileResponse(path, media_type="video/mp4", filename=name)

    # ---------- publishing (stage 4, Instagram API: not used, kept for later) ----------
    pq = worker.publisher

    def local_to_utc(value: str | None):
        if not value:
            return None
        from datetime import datetime, timezone
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(400, "время: формат ГГГГ-ММ-ДДTЧЧ:ММ") from exc
        if local.tzinfo is None:
            local = local.replace(tzinfo=pq.tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def q_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except QueueError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    @app.get("/api/publishing")
    def publishing_overview():
        return pq.overview()

    @app.get("/api/publishing/descriptions")
    def recent_descriptions(limit: int = 10):
        return {"descriptions": pq.recent_descriptions(min(limit, 50))}

    @app.post("/api/publishing/queue")
    def enqueue(body: EnqueueBody):
        pid = q_call(pq.enqueue, body.content_id, body.caption, local_to_utc(body.scheduled_at),
                     body.rights_confirmed, body.note)
        return {"ok": True, "id": pid}

    @app.patch("/api/publishing/{pid}")
    def patch_publication(pid: int, body: PublicationPatch):
        q_call(pq.update, pid, body.caption, local_to_utc(body.scheduled_at), clear_schedule=body.asap)
        return {"ok": True}

    @app.post("/api/publishing/{pid}/{action}")
    def publication_action(pid: int, action: str):
        if action == "dry-run":
            return {"plan": q_call(pq.dry_run, pid)[0]}
        fn = {"cancel": pq.cancel, "retry": pq.retry, "now": pq.publish_now}.get(action)
        if fn is None:
            raise HTTPException(404, "нет такого действия")
        q_call(fn, pid)
        return {"ok": True}

    @app.post("/api/publishing-dry-run")
    def dry_run_all():
        return {"plans": pq.dry_run()}

    @app.post("/api/publishing-settings/pause")
    def set_pause(body: Flag):
        pq.set_paused(body.value)
        return {"paused": pq.paused}

    @app.post("/api/publishing-settings/live")
    def set_live(body: Flag):
        q_call(pq.set_live, body.value)
        return {"mode": "live" if pq.live else "dry_run"}

    @app.post("/api/instagram/check")
    def instagram_check():
        return pq.check_connection()

    @app.post("/api/instagram/credentials")
    def instagram_credentials(body: Credentials):
        if not (body.token or body.account_id):
            raise HTTPException(400, "вставьте токен или ID аккаунта")
        q_call(pq.save_credentials, body.token, body.account_id)
        return pq.check_connection()

    @app.get("/api/instagram/accounts")
    def instagram_accounts():
        return {"accounts": q_call(pq.find_accounts)}

    @app.post("/api/instagram/refresh-token")
    def instagram_refresh():
        return q_call(pq.refresh_token, True)

    return app
