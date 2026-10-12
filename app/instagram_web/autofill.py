"""«Автозалив в кружке»: the best not yet used videos, N per Instagram account, edited with the
«Кружок + баннер» template (the account's own background, the video in a circle a bit above the centre with a
thin white border, the editor's banner at the bottom with chroma key), sent to «Готовые», then queued to
that account as soon as each render is ready."""
from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy import select

from app.database.models import (
    AppSetting, ContentItem, EditAsset, EditProject, Publication, PublicationStatus as PS, Status, utcnow,
)
from app.editor.spec import CANVAS_H, CANVAS_W

JOBS_KEY = "autofill.jobs"
BG_KEY = "autofill.backgrounds"
DEFAULTS = {
    "per_account": 20, "min_duration": 5, "max_duration": 60, "min_short_side": 480, "require_audio": True,
    "weights": {"no_text": 30, "viral": 30, "center": 20, "fresh": 10, "quality": 10},
    "circle": {"size": 860, "y": 300, "border": 8},
    "banner": {"similarity": 0.08, "blend": 0.23},
}


def cfg_of(settings) -> dict:
    raw = settings.get("autofill", {}) or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if not isinstance(v, dict)}}
    for k in ("weights", "circle", "banner"):
        out[k] = {**DEFAULTS[k], **(raw.get(k) or {})}
    return out


# ---------- AppSetting helpers ----------
def _get(db, key, default):
    with db.session() as s:
        row = s.get(AppSetting, key)
        return json.loads(row.value) if row and row.value else default


def _set(db, key, value) -> None:
    with db.session() as s:
        row = s.get(AppSetting, key) or AppSetting(key=key)
        row.value = json.dumps(value)
        s.add(row)


# ---------- choosing the videos ----------
def center_score(video: Path, duration: float | None, frames: int = 3) -> float:
    """0..1: how much of the detail (edges) is in the middle of the frame — what a circle keeps."""
    from app.media.ffmpeg import ffmpeg_path
    vals = []
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(frames):
            out = Path(tmp) / f"{k}.png"
            at = (duration or 6) * (k + 1) / (frames + 1)
            subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=160:-2", str(out)], capture_output=True, timeout=60)
            if not out.is_file():
                continue
            g = np.asarray(Image.open(out).convert("L"), dtype=np.float32)
            e = np.abs(np.diff(g, axis=0))[:, :-1] + np.abs(np.diff(g, axis=1))[:-1, :]
            h, w = e.shape
            r = min(h, w) * 0.42  # the circle that will be kept, around the centre
            yy, xx = np.mgrid[0:h, 0:w]
            inside = ((yy - h / 2) ** 2 + (xx - w / 2) ** 2) <= r * r
            total = e.sum() or 1.0
            vals.append(float(e[inside].sum() / total) / max(inside.mean(), 1e-6))
    if not vals:
        return 0.0
    return max(0.0, min(1.0, (np.mean(vals) - 0.6) / 1.2))  # ≈0 detail on the edges … 1 detail in the middle


def candidates(settings, db) -> list[ContentItem]:
    c = cfg_of(settings)
    with db.session() as s:
        used = set(s.scalars(select(Publication.content_id).where(Publication.platform == "instagram_web",
                                                                   Publication.status != PS.CANCELLED)))
        jobs = {j["content_id"] for j in _get(db, JOBS_KEY, [])}
        rows = []
        for it in s.scalars(select(ContentItem).where(
                ContentItem.original_file.is_not(None), ContentItem.trashed_from.is_(None),
                ContentItem.posted_at.is_(None), ContentItem.ready_path.is_(None),
                ContentItem.status.not_in([Status.DUPLICATE, Status.DOWNLOADING, Status.FAILED]))):
            if it.id in used or it.id in jobs or not Path(it.original_file).is_file():
                continue
            if it.duration is None or not (c["min_duration"] <= it.duration <= c["max_duration"]):
                continue
            if min(it.width or 0, it.height or 0) < c["min_short_side"]:
                continue
            if c["require_audio"] and it.has_audio is False:
                continue
            if it.duplicate_of_id is not None:  # a visual copy of another video
                continue
            s.expunge(it)
            rows.append(it)
    return rows


def score(it: ContentItem, w: dict, center: float | None, now: datetime) -> float:
    no_text = 0.0 if it.has_screen_text else 1.0 if it.has_screen_text is False else 0.5
    viral = (it.viral_score or it.content_score or 0) / 100
    quality = (it.quality_score or 0) / 100
    age_days = max(0.0, (now - (it.published_at or it.downloaded_at or it.created_at or now)).total_seconds() / 86400)
    fresh = max(0.0, 1 - age_days / 30)
    return (w["no_text"] * no_text + w["viral"] * viral + w["center"] * (center if center is not None else 0.5)
            + w["fresh"] * fresh + w["quality"] * quality)


def choose(settings, db, accounts: list[str], per_account: int, with_center: bool = True) -> dict:
    """{account: [content ids]} — the best videos, each video in one account only, in round robin."""
    c = cfg_of(settings)
    now = utcnow()
    pool = candidates(settings, db)
    pool.sort(key=lambda it: score(it, c["weights"], None, now), reverse=True)
    need = per_account * len(accounts)
    pool = pool[: max(need * 2, need + 10)]  # the centre check looks at frames: only for the top
    centers = {}
    if with_center and c["weights"].get("center"):
        for it in pool:
            try:
                centers[it.id] = center_score(Path(it.original_file), it.duration)
            except Exception:
                centers[it.id] = None
    pool.sort(key=lambda it: score(it, c["weights"], centers.get(it.id), now), reverse=True)
    out = {a: [] for a in accounts}
    for i, it in enumerate(pool[:need]):
        out[accounts[i % len(accounts)]].append(it.id)
    detail = {it.id: {"title": it.title, "channel": it.channel, "duration": it.duration, "has_text": it.has_screen_text,
                      "viral": it.viral_score, "center": centers.get(it.id),
                      "score": round(score(it, c["weights"], centers.get(it.id), now), 1)} for it in pool[:need]}
    return {"plan": out, "detail": detail, "available": len(candidates(settings, db))}


# ---------- the look ----------
def banner_asset(db) -> EditAsset | None:
    """«The one banner we have»: the uploaded file that is not part of «Фоны»."""
    with db.session() as s:
        a = s.scalars(select(EditAsset).where(EditAsset.role.is_(None), EditAsset.kind.in_(["image", "video"]))
                      .order_by(EditAsset.created_at.desc())).first()
        if a is not None:
            s.expunge(a)
        return a


def circle_spec(settings, background_id: str | None, banner: EditAsset | None, bg_kind: str = "image") -> dict:
    c = cfg_of(settings)
    size, y, border = int(c["circle"]["size"]), int(c["circle"]["y"]), int(c["circle"]["border"])
    spec = {
        "background": {"type": bg_kind if background_id else "blur", "asset": background_id, "blur": 30, "darken": 0.2},
        "video": {"x": (CANVAS_W - size) // 2, "y": y, "w": size, "h": size, "fit": "cover", "shape": "circle",
                  "border_width": border, "border_color": "#ffffff", "zoom": 1.0, "shadow": False},
        "texts": [], "subtitles": {"enabled": False}, "voice": {"enabled": False},
        "audio": {"volume": 1.0, "music": None, "normalize": True},
    }
    if banner is not None and banner.width:
        bh = round(CANVAS_W * banner.height / banner.width / 2) * 2
        spec["banner"] = {"asset": banner.id, "x": 0, "y": CANVAS_H - bh, "scale": 1.0, "layer": "over",
                          "chroma": {"enabled": True, "color": banner.key_color or "#00ff00",
                                     "similarity": float(c["banner"]["similarity"]), "blend": float(c["banner"]["blend"])}}
    return spec


# ---------- running ----------
def backgrounds_for(db, accounts: list[str]) -> dict[str, str | None]:
    """The account's remembered background; new accounts get different ones (least used first)."""
    from app.editor import backgrounds as bgm
    saved = _get(db, BG_KEY, {})
    out = {a: saved.get(a) for a in accounts}
    with db.session() as s:
        exists = {a.id for a in s.scalars(select(EditAsset).where(EditAsset.role == "background"))}
    out = {a: (b if b in exists else None) for a, b in out.items()}
    missing = [a for a, b in out.items() if not b]
    if missing and exists:
        taken = {b for b in out.values() if b}
        try:
            picks = [p for p in bgm.pick_unused(db, len(missing) + len(taken)) if p not in taken]
        except Exception:
            picks = []
        for a, p in zip(missing, picks):
            out[a] = p
    return out


def remember_backgrounds(db, choice: dict[str, str]) -> None:
    saved = _get(db, BG_KEY, {})
    saved.update({a: b for a, b in choice.items() if b})
    _set(db, BG_KEY, saved)


def start(settings, db, editor, accounts: list[str], backgrounds: dict[str, str], per_account: int,
          caption: str | None) -> dict:
    """Plans, saves the editor projects and queues the renders; the queue part happens in `tick`."""
    remember_backgrounds(db, backgrounds)
    chosen = choose(settings, db, accounts, per_account)
    banner = banner_asset(db)
    with db.session() as s:
        kinds = {a.id: a.kind for a in s.scalars(select(EditAsset).where(EditAsset.id.in_([b for b in backgrounds.values() if b])))}
    jobs = _get(db, JOBS_KEY, [])
    for acc, ids in chosen["plan"].items():
        spec = circle_spec(settings, backgrounds.get(acc), banner, kinds.get(backgrounds.get(acc), "image"))
        for cid in ids:
            editor.save(cid, spec)
            editor.queue_export(cid, to_ready=True)
            jobs.append({"content_id": cid, "account": acc, "caption": caption or "", "state": "render",
                         "at": utcnow().isoformat() + "Z"})
    _set(db, JOBS_KEY, jobs)
    return {"plan": chosen["plan"], "available": chosen["available"],
            "short": {a: per_account - len(v) for a, v in chosen["plan"].items() if len(v) < per_account}}


def tick(db, queue) -> int:
    """Rendered and in «Готовые» → into the Instagram queue of its account (caption, cover from the background)."""
    jobs = _get(db, JOBS_KEY, [])
    changed = moved = 0
    for j in jobs:
        if j["state"] != "render":
            continue
        with db.session() as s:
            p = s.get(EditProject, j["content_id"])
            it = s.get(ContentItem, j["content_id"])
            state, ready = (p.render_state if p else None), bool(it and it.ready_path)
        if state == "done" and ready:
            res = queue.add([j["content_id"]], captions={j["content_id"]: j["caption"]}, account=j["account"])
            j["state"] = "queued" if res["added"] else "error"
            j["error"] = None if res["added"] else "; ".join(res["errors"].values())
            changed += 1
            moved += bool(res["added"])
        elif state == "error":
            j["state"], j["error"] = "error", "монтаж не получился"
            changed += 1
    if changed:
        _set(db, JOBS_KEY, jobs)
    return moved


def progress(db) -> dict:
    jobs = _get(db, JOBS_KEY, [])
    with db.session() as s:
        posted = {p.content_id for p in s.scalars(select(Publication).where(
            Publication.platform == "instagram_web", Publication.status == PS.PUBLISHED))}
    out = {}
    for j in jobs:
        a = out.setdefault(j["account"], {"total": 0, "rendered": 0, "published": 0, "errors": 0})
        a["total"] += 1
        a["rendered"] += j["state"] in ("queued",)
        a["published"] += j["content_id"] in posted
        a["errors"] += j["state"] == "error"
    return {"accounts": out, "active": any(j["state"] == "render" for j in jobs)}


def stop(db, editor) -> int:
    """Not rendered yet → dropped (their editor projects stay); queued ones are paused by the queue's own stop."""
    from app.database.models import EditProject as EP
    jobs = _get(db, JOBS_KEY, [])
    n = 0
    with db.session() as s:
        for j in jobs:
            if j["state"] == "render":
                p = s.get(EP, j["content_id"])
                if p is not None and p.render_state == "queued":
                    p.render_state = None
                j["state"], n = "stopped", n + 1
    _set(db, JOBS_KEY, jobs)
    return n


def clear_finished(db) -> None:
    _set(db, JOBS_KEY, [j for j in _get(db, JOBS_KEY, []) if j["state"] == "render"])
