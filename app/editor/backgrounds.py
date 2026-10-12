"""«Фоны»: a library of background pictures and videos. Each background remembers how many finished videos
used it; unused ones can be picked at random for a batch, used ones can be cleaned up."""
from __future__ import annotations

import json
import random
from pathlib import Path

from sqlalchemy import select

from app.database.models import EditAsset, EditProject, utcnow
from app.editor import assets as assets_mod

ROLE = "background"


class BackgroundError(ValueError):
    pass


def _files(a: EditAsset) -> list[Path]:
    p = Path(a.path)
    return [f for f in (p, p.with_suffix(".first.png")) if f.is_file()]


def as_json(a: EditAsset, in_use: set[str] | None = None) -> dict:
    return {"id": a.id, "kind": a.kind, "name": a.name, "width": a.width, "height": a.height, "duration": a.duration,
            "tag": a.tag, "use_count": a.use_count or 0,
            "last_used_at": a.last_used_at.isoformat() + "Z" if a.last_used_at else None,
            "size": sum(f.stat().st_size for f in _files(a)),
            "preview": f"/api/editor/assets/{a.id}/preview", "file": f"/api/editor/assets/{a.id}/file",
            "in_unfinished": bool(in_use and a.id in in_use)}


def unfinished_refs(db) -> set[str]:
    """Backgrounds chosen in editor projects that are not exported yet."""
    out = set()
    with db.session() as s:
        for spec, state in s.execute(select(EditProject.spec, EditProject.render_state)):
            if state == "done":
                continue
            try:
                aid = json.loads(spec).get("background", {}).get("asset")
            except (ValueError, AttributeError):
                aid = None
            if aid:
                out.add(aid)
    return out


def add(settings, db, filename: str, data: bytes, tag: str | None = None) -> dict:
    a = assets_mod.save_upload(settings, db, filename, data)
    if a.kind not in ("image", "video"):
        raise BackgroundError("фоном может быть только картинка или видео")
    with db.session() as s:
        row = s.get(EditAsset, a.id)
        row.role = ROLE
        row.tag = (tag or "").strip()[:64] or row.tag
        row.use_count = row.use_count or 0
        s.flush()
        return as_json(row)


def listing(db, used: str | None = None, tag: str | None = None) -> dict:
    refs = unfinished_refs(db)
    with db.session() as s:
        rows = list(s.scalars(select(EditAsset).where(EditAsset.role == ROLE).order_by(EditAsset.created_at.desc())))
        tags = sorted({r.tag for r in rows if r.tag})
        if tag:
            rows = [r for r in rows if r.tag == tag]
        if used == "used":
            rows = [r for r in rows if r.use_count]
        elif used == "unused":
            rows = [r for r in rows if not r.use_count]
        items = [as_json(r, refs) for r in rows]
    return {"items": items, "tags": tags, "total": len(items), "bytes": sum(i["size"] for i in items)}


def set_tag(db, ids: list[str], tag: str | None) -> int:
    with db.session() as s:
        n = 0
        for a in s.scalars(select(EditAsset).where(EditAsset.id.in_(ids), EditAsset.role == ROLE)):
            a.tag, n = (tag or "").strip()[:64] or None, n + 1
        return n


def delete(db, ids: list[str] | None = None, all_used: bool = False, confirm: bool = False) -> dict:
    """Without `confirm` only says what would go (count, size, which are in unfinished editor projects)."""
    refs = unfinished_refs(db)
    with db.session() as s:
        q = select(EditAsset).where(EditAsset.role == ROLE)
        q = q.where(EditAsset.use_count > 0) if all_used else q.where(EditAsset.id.in_(ids or []))
        rows = list(s.scalars(q))
        files = [f for r in rows for f in _files(r)]
        res = {"count": len(rows), "bytes": sum(f.stat().st_size for f in files),
               "in_unfinished": [r.name for r in rows if r.id in refs]}
        if not confirm:
            return res
        for f in files:
            f.unlink(missing_ok=True)
        for r in rows:
            s.delete(r)
    return {**res, "deleted": True}


def mark_used(db, asset_id: str | None) -> None:
    if not asset_id:
        return
    with db.session() as s:
        a = s.get(EditAsset, asset_id)
        if a is not None and a.role == ROLE:
            a.use_count = (a.use_count or 0) + 1
            a.last_used_at = utcnow()


def pick_unused(db, n: int, tag: str | None = None, rng: random.Random | None = None) -> list[str]:
    """n backgrounds for a batch: unused ones first (in random order), then the least used; no repeats
    while there are enough."""
    rng = rng or random.Random()
    with db.session() as s:
        rows = list(s.scalars(select(EditAsset).where(EditAsset.role == ROLE)))
    if tag:
        rows = [r for r in rows if r.tag == tag] or rows
    if not rows:
        raise BackgroundError("в разделе «Фоны» пока нет ни одного фона")
    rng.shuffle(rows)
    rows.sort(key=lambda r: r.use_count or 0)
    return [rows[i % len(rows)].id for i in range(n)]
