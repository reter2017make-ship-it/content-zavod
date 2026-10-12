"""Stage 7: editor spec → FFmpeg render (frame/preview/final), banner chroma key, templates, export to «Готовые»."""
import hashlib
import io
import shutil
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.database.models import ContentItem, RightsStatus, Status, Variant
from app.editor import assets as assets_mod
from app.editor.render import Asset, render
from app.editor.service import Editor
from app.editor.spec import normalize, total_duration
from app.media.ffprobe import probe


def _ready_item(settings, pipeline, clips, name="landscape"):
    shutil.copy(clips[name], settings.data("inbox") / f"{name}.mp4")
    pipeline.run_once()
    with pipeline.db.session() as s:
        return s.scalars(select(ContentItem).where(ContentItem.status == Status.READY_TO_PUBLISH)).one().id


def _banner(path, window=(90, 500, 990, 1300), color=(0, 255, 0)):
    img = Image.new("RGB", (1080, 1920), (230, 60, 140))
    ImageDraw.Draw(img).rectangle(window, fill=color)
    img.save(path)
    return path


def _px(path, xy):
    return Image.open(path).convert("RGB").getpixel(xy)


def _near(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_spec_is_clamped():
    s = normalize({"video": {"w": 5, "shape": "star", "zoom": 9}, "time": {"speed": 7, "start_delay": -3},
                   "background": {"type": "image"}, "texts": [{"text": "x" * 999, "size": 1}]})
    assert s["video"]["w"] == 40 and s["video"]["shape"] == "rect" and s["video"]["zoom"] == 4
    assert s["time"]["speed"] == 2 and s["time"]["start_delay"] == 0
    assert s["background"]["type"] == "blur"  # image without a picture falls back
    assert len(s["texts"][0]["text"]) == 300 and s["texts"][0]["size"] == 16


def test_delay_banner_window_and_text(tmp_path, clips):
    src = clips["landscape"]
    info = probe(src)
    spec = normalize({
        "background": {"type": "color", "color": "#203040"},
        "video": {"x": 90, "y": 500, "w": 900, "h": 800, "fit": "cover", "shape": "rounded", "radius": 40,
                  "border_width": 8, "shadow": True},
        "time": {"start_delay": 1.0, "appear": "fade", "appear_duration": 0.3, "speed": 1.5},
        "banner": {"asset": "b", "layer": "over", "chroma": {"enabled": True, "color": "#00ff00"}},
        "texts": [{"text": "Заголовок", "y": 150, "box": True}],
    }, info.duration)
    assets = {"b": Asset(_banner(tmp_path / "banner.png"), "image")}
    before, after = tmp_path / "before.jpg", tmp_path / "after.jpg"
    render(spec, src, info, assets, tmp_path / "w1", before, {}, mode="frame", at=0.5)
    render(spec, src, info, assets, tmp_path / "w2", after, {}, mode="frame", at=2.0)
    centre = (270, 450)  # 540×960 preview frame: the middle of the window
    assert _near(_px(before, centre), (32, 48, 64))       # only the background before the clip starts
    assert not _near(_px(after, centre), (32, 48, 64))    # the clip plays in the banner window
    assert not _near(_px(after, centre), (0, 255, 0), 60)  # and the green screen is keyed out
    assert _near(_px(after, (20, 900)), (230, 60, 140))   # the banner frame stays

    out = tmp_path / "final.mp4"
    render(spec, src, info, assets, tmp_path / "w3", out, {"preset": "ultrafast"}, mode="final")
    o = probe(out)
    assert (o.width, o.height) == (1080, 1920) and o.has_audio
    assert abs(o.duration - total_duration(spec, info.duration)) < 0.15 and abs(o.duration - 5.0) < 0.15


def test_preview_frame_matches_export(tmp_path, clips):
    src = clips["landscape"]
    info = probe(src)
    spec = normalize({"background": {"type": "gradient"}, "video": {"x": 0, "y": 400, "w": 1080, "h": 1000,
                                                                     "shape": "circle", "fit": "cover"}})
    frame = tmp_path / "f.jpg"
    render(spec, src, info, {}, tmp_path / "w1", frame, {}, mode="frame", at=1.5)
    final = tmp_path / "final.mp4"
    render(spec, src, info, {}, tmp_path / "w2", final, {"preset": "ultrafast", "crf": 12}, mode="final")
    grab = tmp_path / "grab.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(final), "-frames:v", "1",
                    "-vf", "scale=540:960:flags=bilinear", str(grab)], check=True)
    a = np.asarray(Image.open(frame).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(grab).convert("RGB"), dtype=np.int16)
    assert np.abs(a - b).mean() < 6  # same graph: only JPEG/x264 noise differs


def test_same_spec_same_result(tmp_path, clips):
    src = clips["other"]
    info = probe(src)
    spec = normalize({"background": {"type": "blur"}, "time": {"start_delay": 0.5, "appear": "slide_up"},
                      "texts": [{"text": "Один и тот же"}]})

    def md5(n):
        out = tmp_path / f"o{n}.mp4"
        render(spec, src, info, {}, tmp_path / f"w{n}", out, {"preset": "ultrafast"}, mode="final")
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:v", "-f", "framemd5", "-"],
                           capture_output=True, text=True, check=True)
        return hashlib.md5(r.stdout.encode()).hexdigest()

    assert md5(1) == md5(2)


def test_key_colour_and_window_detection(tmp_path):
    img = Image.open(_banner(tmp_path / "b.png", (100, 300, 700, 900), (20, 200, 40)))
    key = assets_mod.detect_key_color(img)
    assert key and _near(tuple(int(key[i:i + 2], 16) for i in (1, 3, 5)), (20, 200, 40), 10)
    x, y, w, h = assets_mod.key_window(img, key, 0.3)
    assert abs(x - 100) <= 8 and abs(y - 300) <= 8 and abs(w - 601) <= 10 and abs(h - 601) <= 10
    blue = Image.open(_banner(tmp_path / "blue.png", color=(10, 40, 230)))
    assert assets_mod.detect_key_color(blue).startswith("#0")
    assert assets_mod.detect_key_color(Image.new("RGB", (100, 100), (200, 200, 200))) is None


@pytest.fixture
def web(settings, db):
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    with TestClient(create_app(settings, db, FactoryWorker(settings, db), start_worker=False)) as client:
        yield client


def test_editor_api_export_and_templates(settings, db, pipeline, clips, web, tmp_path):
    settings.raw["processing"]["preset"] = "ultrafast"
    iid = _ready_item(settings, pipeline, clips)
    p = web.get(f"/api/editor/{iid}").json()
    assert p["saved"] is False and p["item"]["can_export"] and p["spec"]["video"]["w"] == 1080

    buf = io.BytesIO()
    Image.open(_banner(tmp_path / "b.png")).save(buf, "PNG")
    up = web.post("/api/editor/assets", files={"file": ("рамка.png", buf.getvalue(), "image/png")}).json()
    assert up["kind"] == "image" and up["key_color"] and web.get(up["preview"]).status_code == 200
    assert web.post("/api/editor/assets", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400

    spec = p["spec"]
    spec["banner"]["asset"] = up["id"]
    fitted = web.post("/api/editor/fit-window", json={"spec": spec}).json()["spec"]
    assert fitted["banner"]["chroma"]["enabled"] and abs(fitted["video"]["x"] - 90) <= 8
    assert abs(fitted["video"]["w"] - 900) <= 12 and fitted["video"]["fit"] == "cover"

    r = web.post(f"/api/editor/{iid}/frame", json={"spec": fitted, "at": 1.0})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    prev = web.post(f"/api/editor/{iid}/preview", json={"spec": fitted}).json()
    assert web.get(prev["url"]).status_code == 200

    assert web.post(f"/api/editor/{iid}/export", json={"spec": fitted}).json()["ok"]
    ed = web.app.state.worker.editor
    assert ed.pending() == [iid]
    ed.export(iid)
    st = web.get(f"/api/editor/{iid}").json()
    assert st["render_state"] == "done" and st["up_to_date"] and st["saved"]
    with db.session() as s:
        v = s.scalars(select(Variant).where(Variant.content_id == iid, Variant.profile == "edited")).one()
        item = s.get(ContentItem, iid)
        assert item.ready_path and open(item.ready_path, "rb").read() == open(v.path, "rb").read()
    o = probe(v.path)
    assert (o.width, o.height) == (1080, 1920)

    tid = web.post("/api/editor/templates", json={"name": "Рамка", "spec": fitted}).json()["id"]
    assert web.get("/api/editor/templates").json()["templates"][0]["name"] == "Рамка"
    res = web.post(f"/api/editor/templates/{tid}/apply", json={"item_ids": [iid, 9999], "export": True}).json()
    assert res["applied"] == [iid] and "9999" in res["errors"]
    assert web.get(f"/api/editor/{iid}").json()["template_id"] == tid
    assert web.get("/editor").status_code == 200


def test_export_needs_approved_rights(settings, db, pipeline, clips):
    iid = _ready_item(settings, pipeline, clips)
    with db.session() as s:
        s.get(ContentItem, iid).rights_status = RightsStatus.REQUIRES_REVIEW
    ed = Editor(settings, db)
    with pytest.raises(Exception, match="одобрите"):
        ed.queue_export(iid)


def test_ready_shows_the_edited_video_not_the_auto_render(settings, db, pipeline, clips):
    """Bug 2026-10-07: «Готовые» played the automatic render instead of the editor's export."""
    from app.web.server import create_app
    iid = _ready_item(settings, pipeline, clips)
    ed = Editor(settings, db)
    ed.save(iid, normalize({"background": {"type": "color", "color": "#ff0000"},
                            "video": {"x": 300, "y": 700, "w": 480, "h": 480, "fit": "cover"}}))
    ed.queue_export(iid)
    ed.export(iid)
    client = TestClient(create_app(settings, db, start_worker=False))
    with db.session() as s:
        edited = s.scalars(select(Variant).where(Variant.content_id == iid, Variant.profile == "edited")).one()
        ready_path = s.get(ContentItem, iid).ready_path
    served = client.get(f"/media/video/{iid}").content
    assert served == open(ready_path, "rb").read() == open(edited.path, "rb").read()
    thumb = client.get(f"/media/thumb/{iid}").content
    assert thumb == open(edited.thumbnail, "rb").read()
