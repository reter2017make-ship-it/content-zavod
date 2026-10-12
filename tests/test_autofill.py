"""«Автозалив в кружке»: choosing the best videos per account, the circle + banner look, queueing after render."""
import io
import shutil

import pytest
from PIL import Image
from sqlalchemy import select

from app.database.models import ContentItem, EditProject, Publication, RightsStatus, Status
from app.editor import backgrounds as bgm
from app.editor.service import Editor
from app.instagram_web import autofill
from app.instagram_web.queue.queue_manager import IGQueue
from app.instagram_web.config import IGConfig


def _items(settings, db, clips, n=8, over=None):
    over = over or {}
    raw = settings.data("raw")
    raw.mkdir(parents=True, exist_ok=True)
    ids = []
    with db.session() as s:
        for i in range(n):
            f = raw / f"v{i}.mp4"
            shutil.copy(clips["landscape"], f)
            kw = dict(source="telegram", source_id=f"c/{i}", channel="c", status=Status.APPROVED,
                      rights_status=RightsStatus.AUTO_APPROVED, original_file=str(f), duration=6.0, width=1280,
                      height=720, has_audio=True, has_screen_text=(i % 2 == 0), viral_score=10 * i)
            kw.update(over.get(i, {}))
            it = ContentItem(**kw)
            s.add(it)
            s.flush()
            ids.append(it.id)
    return ids


def test_choose_filters_ranks_and_never_repeats(settings, db, clips):
    ids = _items(settings, db, clips, 8, {0: {"duration": 2.0}, 1: {"width": 300, "height": 400}, 2: {"has_audio": False}})
    res = autofill.choose(settings, db, ["acc_a", "acc_b"], 2, with_center=False)
    picked = res["plan"]["acc_a"] + res["plan"]["acc_b"]
    assert len(picked) == 4 and len(set(picked)) == 4  # different videos in every account
    assert not {ids[0], ids[1], ids[2]} & set(picked)  # too short, too small, no sound
    best = [ids[7], ids[5]]  # no text + highest viral first
    assert set(best) <= set(picked)
    many = autofill.choose(settings, db, ["acc_a"], 20, with_center=False)
    assert len(many["plan"]["acc_a"]) == res["available"] == 5  # fewer than asked: all there are


def test_center_score_prefers_middle(tmp_path):
    import subprocess
    from PIL import ImageDraw
    from app.media.ffmpeg import ffmpeg_path
    for name, box in (("mid", (220, 380, 500, 900)), ("edge", (0, 0, 120, 1280))):
        img = Image.new("RGB", (720, 1280), (40, 40, 40))
        d = ImageDraw.Draw(img)
        for k in range(0, 60, 6):
            d.rectangle((box[0] + k, box[1] + k, box[2] - k, box[3] - k), outline=(255, 255, 255), width=2)
        img.save(tmp_path / f"{name}.png")
        subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-loop", "1", "-i", str(tmp_path / f"{name}.png"), "-t", "2",
                        "-pix_fmt", "yuv420p", str(tmp_path / f"{name}.mp4")], check=True)
    assert autofill.center_score(tmp_path / "mid.mp4", 2.0) > autofill.center_score(tmp_path / "edge.mp4", 2.0)


def test_circle_banner_look_and_queue_after_render(settings, db, clips):
    from app.editor.spec import normalize
    from app.editor import assets as assets_mod
    ids = _items(settings, db, clips, 3)
    buf = io.BytesIO(); Image.new("RGB", (400, 700), (20, 60, 200)).save(buf, "PNG")
    bg = bgm.add(settings, db, "bg.png", buf.getvalue())
    ban = io.BytesIO(); Image.new("RGB", (1280, 720), (0, 255, 0)).save(ban, "PNG")
    assets_mod.save_upload(settings, db, "banner.png", ban.getvalue())
    banner = autofill.banner_asset(db)
    spec = normalize(autofill.circle_spec(settings, bg["id"], banner))
    v, b = spec["video"], spec["banner"]
    assert v["shape"] == "circle" and v["w"] == v["h"] == 860 and v["border_width"] == 8 and v["border_color"] == "#ffffff"
    assert v["y"] + v["h"] / 2 < 960  # a bit above the centre
    assert b["asset"] == banner.id and b["y"] + 608 == 1920 and (b["chroma"]["similarity"], b["chroma"]["blend"]) == (0.08, 0.23)
    assert spec["texts"] == [] and not spec["subtitles"]["enabled"] and spec["background"]["asset"] == bg["id"]
    ed = Editor(settings, db)
    cfg = IGConfig()
    q = IGQueue(settings, db, cfg)
    res = autofill.start(settings, db, ed, ["acc_a"], {"acc_a": bg["id"]}, 2, "Подпись")
    assert len(res["plan"]["acc_a"]) == 2
    assert autofill.tick(db, q) == 0  # nothing rendered yet: nothing queued
    for cid in res["plan"]["acc_a"]:
        ed.export(cid)
    assert autofill.tick(db, q) == 2
    with db.session() as s:
        pubs = list(s.scalars(select(Publication)))
        assert {p.account for p in pubs} == {"acc_a"} and all(p.caption == "Подпись" and p.cover_path for p in pubs)
    pr = autofill.progress(db)["accounts"]["acc_a"]
    assert pr == {"total": 2, "rendered": 2, "published": 0, "errors": 0}
    again = autofill.choose(settings, db, ["acc_b"], 5, with_center=False)
    assert not set(again["plan"]["acc_b"]) & set(res["plan"]["acc_a"])  # used ones never come again
    assert autofill.backgrounds_for(db, ["acc_a"])["acc_a"] == bg["id"]  # remembered per account


def test_autofill_api_refuses_personal_account(settings, db):
    from fastapi.testclient import TestClient
    from app.instagram_web.service import InstagramService
    from app.web.server import create_app
    svc = InstagramService(settings, db)
    svc.set_excluded("personal_acc", True)
    with TestClient(create_app(settings, db, start_worker=False, instagram=svc)) as web:
        assert web.post("/api/instagram-web/autofill/start", json={"accounts": ["personal_acc"]}).status_code == 403
        assert web.post("/api/instagram-web/autofill/preview", json={"accounts": []}).status_code == 400
