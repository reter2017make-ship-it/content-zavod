"""«Готовые видео»: manual publishing folder, descriptions, posted marks, render quality preset."""
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.models import ContentItem, RightsStatus, Status, Variant
from app.media.ffmpeg import output_fps
from app.media.ffprobe import probe
from app.processing import ready


@pytest.fixture
def web(settings, db):
    from app.web.server import create_app
    from app.web.worker import FactoryWorker
    with TestClient(create_app(settings, db, FactoryWorker(settings, db), start_worker=False)) as client:
        yield client


def _ready_item(settings, pipeline, clips, name="landscape"):
    shutil.copy(clips[name], settings.data("inbox") / f"{name}.mp4")
    pipeline.run_once()
    with pipeline.db.session() as s:
        item = s.scalars(select(ContentItem).where(ContentItem.status == Status.READY_TO_PUBLISH)).one()
        return item.id


def test_output_fps_rule():
    cfg = {"fps": "source", "max_fps": 60, "fallback_fps": 30}
    assert output_fps(25.0, cfg) == "25"
    assert output_fps(29.97, cfg) == "29.97"
    assert output_fps(60.0, cfg) == "60"
    assert output_fps(120.0, cfg) == "30" and output_fps(0, cfg) == "30"
    assert output_fps(25.0, {"fps": 30}) == "30"


def test_render_quality_preset(settings, pipeline, clips, db):
    settings.raw["processing"]["preset"] = "veryfast"  # tests default to ultrafast, which drops High-profile tools
    iid = _ready_item(settings, pipeline, clips)
    with db.session() as s:
        path = s.scalars(select(Variant).where(Variant.content_id == iid)).one().path
    info = probe(path)
    assert (info.width, info.height, info.codec) == (1080, 1920, "h264")
    assert abs(info.fps - 30) < 0.1 and info.sample_rate == 48000
    import json, subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", path], capture_output=True, text=True)
    streams = json.loads(out.stdout)["streams"]
    v = next(x for x in streams if x["codec_type"] == "video")
    a = next(x for x in streams if x["codec_type"] == "audio")
    assert v["profile"] == "High" and v["pix_fmt"] == "yuv420p"
    assert int(a["bit_rate"]) > 160_000


def test_add_to_ready_folder_and_back(settings, db, pipeline, clips, web):
    iid = _ready_item(settings, pipeline, clips)
    r = web.post(f"/api/ready/{iid}", json={"description": "Смотрите до конца #рецепт"})
    assert r.status_code == 200
    path = settings.path(r.json()["path"])
    assert path.parent == settings.root / "Готовые" and path.is_file()
    assert path.name.endswith(f"_{iid}.mp4") and path.name.count("_") >= 3
    assert not path.with_suffix(".txt").exists()  # no description file, it lives in the base
    with db.session() as s:
        rendered = s.scalars(select(Variant).where(Variant.content_id == iid)).one().path
    assert path.read_bytes() == open(rendered, "rb").read()  # copied, not re-encoded

    # the description is kept in the base; repeating "В готовые" keeps one copy
    web.put(f"/api/items/{iid}/description", json={"description": "Новое описание"})
    assert not path.with_suffix(".txt").exists()
    with db.session() as s:
        assert s.get(ContentItem, iid).post_description == "Новое описание"
    web.post(f"/api/ready/{iid}", json={})
    assert len(list((settings.root / "Готовые").glob("*.mp4"))) == 1

    items = web.get("/api/ready").json()["items"]
    assert items[0]["ready_name"] == path.name and items[0]["src"]["width"] == 1280
    assert web.get("/api/publishing/descriptions").json()["descriptions"][0] == "Новое описание"

    d = web.get(f"/media/download/{iid}")
    from urllib.parse import quote
    assert d.status_code == 200 and quote(path.name) in d.headers["content-disposition"]
    assert "_inbox_" in path.name

    assert web.post(f"/api/ready/{iid}/posted", json={"date": "2026-10-06", "note": "выложил"}).json()["ok"]
    it = web.get("/api/ready").json()["items"][0]
    assert it["posted_at"].startswith("2026-10-06") and it["posted_note"] == "выложил"
    web.post(f"/api/ready/{iid}/posted", json={"posted": False})
    assert web.get("/api/ready").json()["items"][0]["posted_at"] is None

    assert web.delete(f"/api/ready/{iid}").json()["ok"]
    assert not path.exists() and not path.with_suffix(".txt").exists()
    assert open(rendered, "rb")  # the factory's own render stays
    assert web.get("/ready").status_code == 200


def test_unapproved_rights_cannot_go_to_ready(settings, db, pipeline, clips, web):
    iid = _ready_item(settings, pipeline, clips)
    for rights in (RightsStatus.UNKNOWN, RightsStatus.REQUIRES_REVIEW, RightsStatus.REJECTED):
        with db.session() as s:
            s.get(ContentItem, iid).rights_status = rights
        r = web.post(f"/api/ready/{iid}", json={})
        assert r.status_code == 409 and "ручной проверке" in r.json()["detail"]
    assert not (settings.root / "Готовые").exists() or not list((settings.root / "Готовые").iterdir())


def test_file_name_is_readable(settings, db, pipeline, clips):
    iid = _ready_item(settings, pipeline, clips)
    with db.session() as s:
        item = s.get(ContentItem, iid)
        item.channel, item.category = "min2ru", "recipes"
        name = ready.file_name(item, {"recipes": "Рецепты"})
    assert name.endswith(f"_Рецепты_min2ru_{iid}.mp4") and name[:4].isdigit()


def test_ready_shows_only_what_was_sent_from_the_editor(settings, db, pipeline, clips, web):
    """Owner's rule: a rendered video is not «ready» until he sends it there himself."""
    iid = _ready_item(settings, pipeline, clips)
    assert iid not in [i["id"] for i in web.get("/api/ready").json()["items"]]
    web.post(f"/api/ready/{iid}", json={})
    assert iid in [i["id"] for i in web.get("/api/ready").json()["items"]]
