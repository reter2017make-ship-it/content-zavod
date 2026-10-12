"""Owner's rule: the Reel's cover is the background chosen for the video in the editor
(a picture from «Фоны», the first frame of a video background, a colour or a gradient). 1080×1920 JPEG."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from app.database.models import EditAsset, EditProject

W, H = 1080, 1920


def _cover_crop(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    k = max(W / img.width, H / img.height)
    img = img.resize((max(W, round(img.width * k)), max(H, round(img.height * k))), Image.LANCZOS)
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def background_cover(settings, db, content_id: int) -> str | None:
    """Path of a cover made from the video's editor background, or None (blurred clip: Instagram's own frame)."""
    from app.editor import assets as assets_mod
    from app.editor.render import gradient_png
    with db.session() as s:
        p = s.get(EditProject, content_id)
        if p is None:
            return None
        bg = (json.loads(p.spec) or {}).get("background") or {}
        kind = bg.get("type")
        asset = s.get(EditAsset, bg.get("asset")) if bg.get("asset") else None
        src = assets_mod.preview_image(asset) if asset is not None and kind in ("image", "video") else None
    key = hashlib.sha256(json.dumps(bg, sort_keys=True).encode()).hexdigest()[:20]
    out = settings.data("ig_covers") / f"bg_{key}.jpg"
    if out.is_file():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if kind in ("image", "video"):
        if not src or not Path(src).is_file():
            return None
        img = _cover_crop(Image.open(src))
    elif kind == "color":
        img = Image.new("RGB", (W, H), bg.get("color") or "#101418")
    elif kind == "gradient":
        tmp = out.with_suffix(".png")
        gradient_png(bg.get("color") or "#101418", bg.get("color2") or "#4f46e5", float(bg.get("angle") or 160), tmp)
        img = Image.open(tmp).convert("RGB")
        tmp.unlink(missing_ok=True)
    else:
        return None
    img.save(out, "JPEG", quality=92)
    return str(out)
