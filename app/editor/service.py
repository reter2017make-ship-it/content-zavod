"""Editor: projects (spec per video), templates, frame/clip previews and exports to «Готовые»."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from app.database.models import (
    ContentItem, EditAsset, EditProject, EditTemplate, Status, Variant, rights_ok, utcnow,
)
from app.editor import assets as assets_mod
from app.editor.render import VOICE, Asset, render
from app.editor.tts import TTSError, Voices
from app.editor.spec import CANVAS_W, as_template, normalize, total_duration
from app.logging_setup import log_event
from app.media import subtitles
from app.media.ffprobe import probe

log = logging.getLogger("editor")


def probe_duration(path: Path) -> float | None:
    import subprocess
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None
EDITED_PROFILE = "edited"


class EditorError(ValueError):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Editor:
    def __init__(self, settings, db):
        self.s, self.db = settings, db
        self.pcfg = settings.section("processing")
        self.cache = settings.data("editor_cache")
        self._render_lock = threading.Lock()  # one heavy export at a time
        self._preview_lock = threading.Lock()
        self.voices = Voices(settings)

    # ---------- sources ----------
    def _item(self, s, item_id: int) -> ContentItem:
        item = s.get(ContentItem, item_id)
        if item is None:
            raise EditorError("нет такого ролика", 404)
        if not item.original_file or not Path(item.original_file).is_file():
            raise EditorError("исходник ролика ещё не скачан", 409)
        return item

    def source(self, item_id: int) -> tuple[Path, object, list[subtitles.Cue]]:
        with self.db.session() as s:
            item = self._item(s, item_id)
            src, sub_file, segs = Path(item.original_file), item.subtitle_file, item.transcript_segments
        info = probe(src)
        cues: list[subtitles.Cue] = []
        if sub_file and Path(sub_file).is_file():
            cues = subtitles.load_srt(Path(sub_file))
        elif segs:
            cues = [subtitles.Cue(float(x["start"]), float(x["end"]), x["text"]) for x in json.loads(segs)]
        return src, info, cues

    def assets_for(self, spec: dict, with_voice: bool = False) -> dict[str, Asset]:
        ids = {spec["background"].get("asset"), spec["banner"].get("asset"), spec["audio"].get("music")} - {None}
        out = {}
        with self.db.session() as s:
            for a in s.scalars(select(EditAsset).where(EditAsset.id.in_(ids))):
                if Path(a.path).is_file():
                    out[a.id] = Asset(Path(a.path), a.kind)
        vo = spec.get("voice") or {}
        if with_voice and vo.get("enabled") and vo.get("text", "").strip():
            try:
                wav = self.voices.synthesize(vo["text"], vo["voice"], vo["speed"])
            except TTSError as exc:
                raise EditorError(f"озвучка: {exc}") from exc
            out[VOICE] = Asset(wav, "audio", probe_duration(wav))
        return out

    def tts(self, text: str, voice: str, speed: float) -> Path:
        try:
            return self.voices.synthesize(text, voice, speed)
        except TTSError as exc:
            raise EditorError(str(exc)) from exc

    # ---------- projects ----------
    def default_spec(self, item_id: int) -> dict:
        with self.db.session() as s:
            item = self._item(s, item_id)
            has_subs = bool(item.subtitle_file or item.transcript_segments)
        return normalize({"subtitles": {"enabled": has_subs}})

    def project(self, item_id: int) -> dict:
        with self.db.session() as s:
            item = self._item(s, item_id)
            p = s.get(EditProject, item_id)
            dur = item.duration
            spec = normalize(json.loads(p.spec), dur) if p else None
            state = {"render_state": p.render_state if p else None, "render_error": p.render_error if p else None,
                     "rendered_at": p.rendered_at.isoformat() + "Z" if p and p.rendered_at else None,
                     "up_to_date": bool(p and p.rendered_spec_hash and spec and p.rendered_spec_hash == spec_hash(spec)),
                     "template_id": p.template_id if p else None}
            info = {"id": item.id, "title": item.title, "duration": dur, "width": item.width, "height": item.height,
                    "fps": item.fps, "rights": item.rights_status.value, "status": item.status.value,
                    "category": item.category, "channel": item.channel, "source": item.source,
                    "can_export": rights_ok(self.s, item.rights_status),
                    "has_subtitles": bool(item.subtitle_file or item.transcript_segments),
                    "ready_path": item.ready_path, "transcript": item.transcript,
                    "description": item.post_description, "caption": item.caption}
        cues = []
        try:
            cues = [{"start": c.start, "end": c.end, "text": c.text} for c in self.source(item_id)[2]]
        except Exception:
            pass
        return {"item": info, "spec": spec or self.default_spec(item_id), "saved": spec is not None,
                "cues": cues, "source_url": f"/media/original/{item_id}", **state}

    def save(self, item_id: int, spec: dict, template_id: int | None = None) -> dict:
        with self.db.session() as s:
            item = self._item(s, item_id)
            spec = normalize(spec, item.duration)
            p = s.get(EditProject, item_id) or EditProject(content_id=item_id)
            p.spec = json.dumps(spec, ensure_ascii=False)
            if template_id is not None:
                p.template_id = template_id
            s.add(p)
        return spec

    def reset(self, item_id: int) -> None:
        with self.db.session() as s:
            p = s.get(EditProject, item_id)
            if p:
                s.delete(p)

    # ---------- previews (same graph as the export, smaller) ----------
    def _cache_path(self, item_id: int, spec: dict, suffix: str, extra: str = "") -> Path:
        h = hashlib.sha256((spec_hash(spec) + extra + str(item_id)).encode()).hexdigest()[:20]
        return self.cache / f"{item_id}_{h}{suffix}"

    def frame(self, item_id: int, spec: dict, at: float) -> Path:
        src, info, cues = self.source(item_id)
        spec = normalize(spec, info.duration)
        at = max(0.0, min(float(at), total_duration(spec, info.duration)))
        out = self._cache_path(item_id, spec, ".jpg", f"{at:.2f}")
        if not out.is_file():
            with self._preview_lock:
                render(spec, src, info, self.assets_for(spec), out.with_suffix(".work"), out, self.pcfg,
                       cues, mode="frame", at=at, timeout=120)
            shutil.rmtree(out.with_suffix(".work"), ignore_errors=True)
        self._trim_cache()
        return out

    def preview(self, item_id: int, spec: dict) -> Path:
        src, info, cues = self.source(item_id)
        spec = normalize(spec, info.duration)
        out = self._cache_path(item_id, spec, ".mp4")
        if not out.is_file():
            with self._preview_lock:
                tmp = out.with_suffix(".part.mp4")
                render(spec, src, info, self.assets_for(spec, True), out.with_suffix(".work"), tmp, self.pcfg,
                       cues, mode="preview", timeout=600)
                tmp.replace(out)
            shutil.rmtree(out.with_suffix(".work"), ignore_errors=True)
        return out

    def _trim_cache(self, keep: int = 400) -> None:
        files = sorted(self.cache.glob("*.*"), key=lambda p: p.stat().st_mtime)
        for p in files[:-keep]:
            p.unlink(missing_ok=True)

    # ---------- export ----------
    def queue_export(self, item_id: int, to_ready: bool = True) -> None:
        with self.db.session() as s:
            item = self._item(s, item_id)
            if to_ready and not rights_ok(self.s, item.rights_status):
                raise EditorError(f"права {item.rights_status.value}: сначала одобрите ролик на ручной проверке", 409)
            p = s.get(EditProject, item_id)
            if p is None:
                p = EditProject(content_id=item_id, spec=json.dumps(self.default_spec(item_id), ensure_ascii=False))
                s.add(p)
            p.render_state, p.render_error, p.send_to_ready = "queued", None, to_ready

    def pending(self) -> list[int]:
        with self.db.session() as s:
            return list(s.scalars(select(EditProject.content_id).where(EditProject.render_state == "queued")
                                  .order_by(EditProject.updated_at)))

    def recover(self) -> None:
        with self.db.session() as s:
            for p in s.scalars(select(EditProject).where(EditProject.render_state == "rendering")):
                p.render_state = "queued"

    def export(self, item_id: int) -> Path:
        """Full-quality render from the original, registered as the «edited» version; then to «Готовые»."""
        with self._render_lock:
            with self.db.session() as s:
                p = s.get(EditProject, item_id)
                spec_json, to_ready = (p.spec, p.send_to_ready) if p else (None, True)
                if p:
                    p.render_state = "rendering"
            try:
                src, info, cues = self.source(item_id)
                spec = normalize(json.loads(spec_json) if spec_json else self.default_spec(item_id), info.duration)
                final_dir = self.s.data("final")
                out = final_dir / f"edited_{item_id}.mp4"
                tmp = self.s.data("processing") / f"edited_{item_id}.part.mp4"
                work = self.s.data("temp") / f"edit_{item_id}"
                render(spec, src, info, self.assets_for(spec, True), work, tmp, self.pcfg, cues, mode="final")
                out_info = probe(tmp)
                if (out_info.width, out_info.height) != (CANVAS_W, 1920):
                    raise RuntimeError(f"итоговый файл {out_info.width}x{out_info.height}")
                thumb = self.s.data("thumbnails") / f"edited_{item_id}.jpg"
                from app.media.ffmpeg import thumbnail
                thumbnail(tmp, thumb, min(spec["time"]["start_delay"] + 1.0, out_info.duration / 2),
                          int(self.pcfg.get("thumbnail_width", 540)))
                tmp.replace(out)
                shutil.rmtree(work, ignore_errors=True)
                with self.db.session() as s:
                    item = s.get(ContentItem, item_id)
                    v = s.scalars(select(Variant).where(Variant.content_id == item_id,
                                                        Variant.profile == EDITED_PROFILE)).first()
                    if v is None:
                        v = Variant(content_id=item_id, profile=EDITED_PROFILE, path="")
                        s.add(v)
                    v.path, v.thumbnail = str(out), str(thumb)
                    v.width, v.height, v.duration = out_info.width, out_info.height, out_info.duration
                    v.has_audio, v.burned_subtitles = out_info.has_audio, bool(spec["subtitles"]["enabled"] and cues)
                    if item.status != Status.PUBLISHED and item.trashed_from is None and rights_ok(self.s, item.rights_status):
                        # the owner edited it himself: it is ready, whatever the analysis said
                        item.status = Status.READY_TO_PUBLISH
                    p = s.get(EditProject, item_id)
                    if p:
                        p.render_state, p.render_error = "done", None
                        p.rendered_spec_hash, p.rendered_at = spec_hash(spec), utcnow()
                log_event(log, "EDIT_EXPORT", "SUCCESS", item_id, output=out.name)
                if spec["background"]["type"] in ("image", "video"):
                    from app.editor import backgrounds
                    backgrounds.mark_used(self.db, spec["background"].get("asset"))
                if to_ready:
                    from app.processing import ready
                    from app.ai.analyzer import category_labels
                    ready.add(self.s, self.db, item_id, category_labels())
                return out
            except Exception as exc:
                with self.db.session() as s:
                    p = s.get(EditProject, item_id)
                    if p:
                        p.render_state, p.render_error = "error", str(exc)[:1000]
                log_event(log, "EDIT_EXPORT", "FAILED", item_id, error=str(exc)[:300])
                raise

    # ---------- templates ----------
    def ensure_builtin_templates(self) -> int:
        """Adds the ready-made templates that are not in the base yet (a deleted one comes back on restart)."""
        from app.editor.templates import builtin_specs
        added = 0
        with self.db.session() as s:
            have = set(s.scalars(select(EditTemplate.builtin).where(EditTemplate.builtin.is_not(None))))
            if "circle_banner" not in have:  # «Кружок + баннер» (the «Автозалив» look) with the current banner
                from app.instagram_web.autofill import banner_asset, circle_spec
                s.add(EditTemplate(name="Кружок + баннер снизу", builtin="circle_banner",
                                   spec=json.dumps(as_template(normalize(circle_spec(self.s, None, banner_asset(self.db)))),
                                                   ensure_ascii=False)))
                added += 1
            for key, name, spec in builtin_specs():
                if key not in have:
                    s.add(EditTemplate(name=name, builtin=key,
                                       spec=json.dumps(as_template(normalize(spec)), ensure_ascii=False)))
                    added += 1
        return added

    def templates(self) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(select(EditTemplate).order_by(EditTemplate.builtin.is_(None), EditTemplate.id))
            return [{"id": t.id, "name": t.name, "builtin": t.builtin, "spec": json.loads(t.spec)} for t in rows]

    def save_template(self, name: str, spec: dict, template_id: int | None = None) -> int:
        name = (name or "").strip()[:128]
        if not name:
            raise EditorError("введите название шаблона")
        with self.db.session() as s:
            t = s.get(EditTemplate, template_id) if template_id else None
            if t is None:
                t = EditTemplate(name=name, spec="{}")
                s.add(t)
            t.name, t.spec = name, json.dumps(as_template(normalize(spec)), ensure_ascii=False)
            s.flush()
            return t.id

    def delete_template(self, template_id: int) -> None:
        with self.db.session() as s:
            t = s.get(EditTemplate, template_id)
            if t:
                s.delete(t)

    def apply_template(self, template_id: int, item_ids: list[int], export: bool = True,
                       background: str | None = None, background_tag: str | None = None) -> dict:
        """background: None = as in the template, an asset id = that one for all, "random" = a different
        unused background from «Фоны» for every video."""
        from app.editor import backgrounds
        with self.db.session() as s:
            t = s.get(EditTemplate, template_id)
            if t is None:
                raise EditorError("нет такого шаблона", 404)
            tspec = json.loads(t.spec)
        bgs: list[str | None] = [None] * len(item_ids)
        if background == "random":
            try:
                bgs = backgrounds.pick_unused(self.db, len(item_ids), background_tag)
            except backgrounds.BackgroundError as exc:
                raise EditorError(str(exc)) from exc
        elif background:
            bgs = [background] * len(item_ids)
        kinds = {}
        if any(bgs):
            with self.db.session() as s:
                kinds = {a.id: a.kind for a in s.scalars(select(EditAsset).where(EditAsset.id.in_([b for b in bgs if b])))}
        done, errors = [], {}
        for iid, bg in zip(item_ids, bgs):
            try:
                spec = json.loads(json.dumps(tspec))
                if bg and bg in kinds:
                    spec.setdefault("background", {}).update(type=kinds[bg], asset=bg)
                self.save(iid, spec, template_id)
                if export:
                    self.queue_export(iid)
                done.append(iid)
            except EditorError as exc:
                errors[iid] = str(exc)
        return {"applied": done, "errors": errors}

    # ---------- volume work: auto templates by category, queue progress ----------
    AUTO_KEY = "auto_templates"

    def auto_templates(self) -> dict[str, int]:
        from app.database.models import AppSetting
        with self.db.session() as s:
            row = s.get(AppSetting, self.AUTO_KEY)
            return {k: int(v) for k, v in json.loads(row.value).items()} if row and row.value else {}

    def set_auto_templates(self, mapping: dict[str, int | None]) -> dict[str, int]:
        from app.database.models import AppSetting
        with self.db.session() as s:
            ids = set(s.scalars(select(EditTemplate.id)))
            clean = {k: int(v) for k, v in mapping.items() if v and int(v) in ids}
            row = s.get(AppSetting, self.AUTO_KEY) or AppSetting(key=self.AUTO_KEY)
            row.value = json.dumps(clean)
            s.add(row)
        return clean

    def apply_auto_templates(self) -> list[int]:
        """Ready videos of a category with an auto template, never opened in the editor: template + export
        to «Готовые». Runs in the background after each rendering round."""
        mapping = self.auto_templates()
        if not mapping:
            return []
        with self.db.session() as s:
            edited = select(EditProject.content_id)
            ids = [(i.id, i.category) for i in s.scalars(
                select(ContentItem).where(ContentItem.status == Status.READY_TO_PUBLISH,
                                          ContentItem.category.in_(list(mapping)), ContentItem.id.not_in(edited),
                                          ContentItem.original_file.is_not(None)))]
        done = []
        for iid, cat in ids:
            res = self.apply_template(mapping[cat], [iid], export=True)
            done += res["applied"]
        if done:
            log_event(log, "AUTO_TEMPLATE", "SUCCESS", None, items=len(done))
        return done

    def queue_status(self) -> dict:
        with self.db.session() as s:
            rows = list(s.execute(select(EditProject.content_id, EditProject.render_state)))
        by = {}
        for _, st in rows:
            by[st or "draft"] = by.get(st or "draft", 0) + 1
        return {"queued": by.get("queued", 0), "rendering": by.get("rendering", 0), "done": by.get("done", 0),
                "error": by.get("error", 0),
                "items": {cid: st for cid, st in rows if st in ("queued", "rendering", "error")}}

    # ---------- banner helpers ----------
    def asset_list(self) -> list[dict]:
        with self.db.session() as s:
            return [{"id": a.id, "kind": a.kind, "name": a.name, "width": a.width, "height": a.height,
                     "duration": a.duration, "key_color": a.key_color, "role": a.role, "use_count": a.use_count or 0,
                     "tag": a.tag,
                     "preview": f"/api/editor/assets/{a.id}/preview" if a.kind != "audio" else None,
                     "file": f"/api/editor/assets/{a.id}/file"}
                    for a in s.scalars(select(EditAsset).order_by(EditAsset.created_at.desc()))]

    def fit_to_window(self, spec: dict) -> dict:
        """Puts the clip into the banner's chroma window (the keyed area) and keys the banner out."""
        spec = normalize(spec)
        b = spec["banner"]
        if not b.get("asset"):
            raise EditorError("сначала выберите баннер")
        with self.db.session() as s:
            a = s.get(EditAsset, b["asset"])
            if a is None:
                raise EditorError("баннер не найден", 404)
            img_path = assets_mod.preview_image(a)
            key = b["chroma"]["color"] if b["chroma"]["enabled"] else (a.key_color or b["chroma"]["color"])
        if img_path is None:
            raise EditorError("не удалось прочитать баннер")
        img = Image.open(img_path)
        win = assets_mod.key_window(img, key, b["chroma"]["similarity"])
        if win is None:
            raise EditorError("на баннере не нашлось области цвета хромакея: укажите цвет вручную")
        k = CANVAS_W * b["scale"] / img.width
        x, y, w, h = win
        spec["video"].update(x=int(b["x"] + x * k), y=int(b["y"] + y * k), w=int(w * k), h=int(h * k), fit="cover")
        b["chroma"].update(enabled=True, color=key)
        b["layer"] = "over"
        return normalize(spec)
