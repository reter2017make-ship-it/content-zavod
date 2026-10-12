"""HTTP API of the module, mounted by app/web/server.py only when instagram_web.enabled is on."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.instagram_web.queue.queue_manager import QueueError


class AddBody(BaseModel):
    item_ids: list[int]
    caption: str | None = None       # None: each video's own description
    captions: dict[int, str] | None = None  # per video (typed in the dialog)
    account: str | None = None              # @username to publish to (default: the active one)
    cover_path: str | None = None
    when: str = "now"                # now | scheduled
    scheduled_at: str | None = None  # local time "YYYY-MM-DDTHH:MM"


class SettingsBody(BaseModel):
    max_posts_per_day: int | None = None
    interval_minutes: int | None = None
    window_start: str | None = None
    window_end: str | None = None


class PatchBody(BaseModel):
    caption: str | None = None
    scheduled_at: str | None = None
    asap: bool = False
    cover_path: str | None = None


def router(service) -> APIRouter:
    r = APIRouter(prefix="/api/instagram-web")
    q = service.queue

    def local_to_utc(value: str | None):
        if not value:
            return None
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(400, "время: формат ГГГГ-ММ-ДДTЧЧ:ММ") from exc
        if local.tzinfo is None:
            local = local.replace(tzinfo=ZoneInfo(service.cfg.timezone))
        return local.astimezone(timezone.utc).replace(tzinfo=None)

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except QueueError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    # ---------- connection ----------
    @r.get("/status")
    def status():
        return service.status()

    @r.post("/connect")
    def connect():
        return service.connect()

    @r.post("/confirm")
    def confirm():
        return service.confirm()

    @r.post("/open")
    def open_chrome():
        return service.open_chrome()

    @r.post("/close")
    def close_chrome():
        return service.close_chrome()

    @r.post("/disconnect")
    def disconnect():
        return service.disconnect()

    # ---------- queue ----------
    @r.get("/queue")
    def queue_list():
        return q.listing()

    @r.post("/queue")
    def queue_add(body: AddBody):
        when = local_to_utc(body.scheduled_at) if body.when == "scheduled" else None
        if body.when == "scheduled" and when is None:
            raise HTTPException(400, "укажите дату и время публикации")
        account = body.account or (service.account or {}).get("username")
        try:
            service._check_allowed(account)
        except QueueError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc
        return call(q.add, body.item_ids, body.caption, body.cover_path, when, body.captions, account)

    @r.post("/queue/cover")
    async def queue_cover(file: UploadFile = File(...)):
        return {"cover_path": call(q.save_cover, file.filename or "cover", await file.read())}

    @r.get("/cover-preview/{content_id}")
    def cover_preview(content_id: int):
        """The cover a video will get: its editor background (404 = Instagram's own frame)."""
        from pathlib import Path
        from fastapi.responses import FileResponse
        from app.instagram_web.queue.cover_from_background import background_cover
        path = background_cover(service.s, service.db, content_id)
        if not path or not Path(path).is_file():
            raise HTTPException(404, "фона нет")
        return FileResponse(path)

    @r.get("/queue/{pid}/cover")
    def queue_cover_image(pid: int):
        from pathlib import Path
        from fastapi.responses import FileResponse
        from app.database.models import Publication
        with service.db.session() as s:
            p = s.get(Publication, pid)
            path = Path(p.cover_path) if p and p.cover_path else None
        if not path or not path.is_file():
            raise HTTPException(404, "обложки нет")
        return FileResponse(path)

    @r.patch("/queue/{pid}")
    def queue_patch(pid: int, body: PatchBody):
        call(q.update, pid, body.caption, local_to_utc(body.scheduled_at), body.asap, body.cover_path)
        return {"ok": True}

    @r.delete("/queue/{pid}")
    def queue_remove(pid: int):
        call(q.remove, pid)
        return {"ok": True}

    @r.post("/queue/{pid}/pause")
    def queue_pause_item(pid: int):
        call(q.pause_item, pid)
        return {"ok": True}

    @r.post("/queue/{pid}/resume")
    def queue_resume_item(pid: int):
        call(q.resume_item, pid)
        return {"ok": True}

    @r.post("/retry/{pid}")
    def queue_retry(pid: int):
        call(q.retry, pid)
        return {"ok": True}

    @r.post("/pause")
    def pause():
        q.set_paused(True)
        return service.status()

    @r.post("/resume")
    def resume():
        return service.resume()

    @r.put("/settings")
    def settings_put(body: SettingsBody):
        return call(service.update_settings, body.model_dump())

    @r.post("/accounts/refresh")
    def accounts_refresh():
        return service.refresh_accounts()

    @r.post("/accounts/switch")
    def accounts_switch(body: dict):
        name = str(body.get("username") or "").strip().lstrip("@")
        if not name:
            raise HTTPException(400, "укажите @username")
        return call(service.switch_account, name)

    @r.post("/accounts/login")
    def accounts_login(body: dict):
        name = str(body.get("username") or "").strip().lstrip("@")
        if not name:
            raise HTTPException(400, "укажите @username")
        return service.manual_login(name)

    @r.post("/accounts/exclude")
    def accounts_exclude(body: dict):
        name = str(body.get("username") or "").strip().lstrip("@")
        if not name:
            raise HTTPException(400, "укажите @username")
        return service.set_excluded(name, bool(body.get("excluded", True)))

    @r.post("/publish-all")
    def publish_all(body: dict):
        return call(service.publish_all_ready, body.get("account") or None, body.get("caption"),
                    bool(body.get("use_own", True)), body.get("ids"))

    @r.post("/autopost")
    def autopost(body: dict):
        return service.set_autopost(bool(body.get("on")))

    # ---------- «Автозалив в кружке» ----------
    from app.instagram_web import autofill

    def allowed_accounts(names):
        names = [str(n).lstrip("@") for n in names or [] if n]
        for n in names:
            service._check_allowed(n)
        if not names:
            raise HTTPException(400, "выберите хотя бы один аккаунт")
        return names

    @r.post("/autofill/preview")
    def autofill_preview(body: dict):
        accounts = call(allowed_accounts, body.get("accounts"))
        n = int(body.get("per_account") or autofill.cfg_of(service.s)["per_account"])
        res = autofill.choose(service.s, service.db, accounts, n, with_center=bool(body.get("center", True)))
        return {**res, "backgrounds": autofill.backgrounds_for(service.db, accounts),
                "banner": (lambda b: {"id": b.id, "name": b.name} if b else None)(autofill.banner_asset(service.db))}

    @r.post("/autofill/backgrounds")
    def autofill_backgrounds(body: dict):
        accounts = call(allowed_accounts, body.get("accounts"))
        return {"backgrounds": autofill.backgrounds_for(service.db, accounts),
                "banner": (lambda b: {"id": b.id, "name": b.name} if b else None)(autofill.banner_asset(service.db))}

    @r.post("/autofill/start")
    def autofill_start(body: dict):
        accounts = call(allowed_accounts, body.get("accounts"))
        if service.editor is None:
            raise HTTPException(409, "редактор недоступен")
        n = max(1, min(100, int(body.get("per_account") or 20)))
        res = autofill.start(service.s, service.db, service.editor, accounts, body.get("backgrounds") or {}, n,
                             body.get("caption"))
        service.set_autopost(True)
        return {**res, "progress": autofill.progress(service.db)}

    @r.get("/autofill/progress")
    def autofill_progress():
        return autofill.progress(service.db)

    @r.post("/autofill/stop")
    def autofill_stop():
        n = autofill.stop(service.db, service.editor)
        service.stop()
        return {"stopped": n, "progress": autofill.progress(service.db)}

    @r.post("/stop")
    def stop():
        return service.stop()

    @r.post("/publish/{pid}")
    def publish_now(pid: int):
        return call(service.publish_now, pid)

    @r.get("/queued-ids")
    def queued_ids():
        return {"ids": sorted(q.queued_content_ids())}

    return r
