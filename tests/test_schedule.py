"""Parsing calendar, ready-made templates, chroma defaults and the editor's live-preview endpoints."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database.models import Channel, EditTemplate, ScanSchedule
from app.editor.service import Editor
from app.editor.spec import normalize
from app.sources.schedule import ParseCalendar, ScheduleError
from app.web.server import create_app
from app.web.worker import FactoryWorker


def utc(s: str) -> datetime:  # Moscow is UTC+3 all year
    return datetime.fromisoformat(s)


def _channels(db, *names):
    with db.session() as s:
        for n in names:
            s.add(Channel(username=n))
    with db.session() as s:
        return {c.username: c.id for c in s.scalars(select(Channel))}


def test_slots_and_catch_up(settings, db):
    cal = ParseCalendar(settings, db)
    ids = _channels(db, "a", "b")
    with db.session() as s:
        # Monday and Wednesday at 10:00 Moscow = 07:00 UTC; last run long ago
        s.add(ScanSchedule(days="0,2", times="10:00", last_run_at=utc("2026-01-01T00:00")))
    monday_0659, monday_0705 = utc("2026-10-05T06:59"), utc("2026-10-05T07:05")
    assert cal.due(monday_0659) == []
    (sid, chans), = cal.due(monday_0705)
    assert sorted(chans) == sorted(ids.values())
    cal.mark_run(sid, monday_0705)
    assert cal.due(utc("2026-10-05T08:00")) == []          # already done for this slot
    assert cal.due(utc("2026-10-06T07:05")) == []          # Tuesday is not in the calendar
    assert cal.due(utc("2026-10-07T11:30")) == []          # Wednesday slot missed by more than 3 h: skipped
    assert cal.due(utc("2026-10-07T09:30"))                # but caught up within 3 h
    assert cal.next_run(monday_0705) == utc("2026-10-07T07:00")


def test_own_channel_calendar_replaces_the_common_one(settings, db):
    cal = ParseCalendar(settings, db)
    ids = _channels(db, "a", "b", "off")
    with db.session() as s:
        s.get(Channel, ids["off"]).enabled = False
        s.add(ScanSchedule(days="0,1,2,3,4,5,6", times="10:00", last_run_at=utc("2026-01-01T00:00")))
        s.add(ScanSchedule(channel_id=ids["b"], days="0,1,2,3,4,5,6", times="10:00", last_run_at=utc("2026-01-01T00:00")))
        s.add(ScanSchedule(channel_id=ids["a"], days="0", times="23:00", enabled=False))
    got = dict(cal.due(utc("2026-10-05T07:01")))
    assert sorted(got.values()) == [[ids["a"]], [ids["b"]]]  # common: a (own one is off) — b only by its own


def test_validation_and_new_calendar_waits_for_next_slot(settings, db):
    cal = ParseCalendar(settings, db)
    with pytest.raises(ScheduleError):
        cal.create(None, [], ["10:00"])
    with pytest.raises(ScheduleError):
        cal.create(None, [0], ["25:00"])
    sid = cal.create(None, [0, 1, 2, 3, 4, 5, 6], ["9:05", "09:05", "18:30"])
    with db.session() as s:
        assert s.get(ScanSchedule, sid).times == "09:05,18:30"
    _channels(db, "a")
    assert cal.due() == []  # created now: does not fire for a slot that has already passed


def test_worker_runs_due_calendars_and_default_is_created(settings, db):
    worker = FactoryWorker(settings, db)
    queued = []
    worker.enqueue_channel = lambda cid: queued.append(cid) or True
    worker.calendar.ensure_default()
    worker.calendar.ensure_default()
    with db.session() as s:
        default, = s.scalars(select(ScanSchedule)).all()
        assert default.channel_id is None and default.times == "10:00,19:00" and default.days == "0,1,2,3,4,5,6"
        default.last_run_at = utc("2026-01-01T00:00")
    ids = _channels(db, "a")
    assert worker.run_due_schedules(utc("2026-10-05T07:10")) == 1 and queued == [ids["a"]]
    assert worker.run_due_schedules(utc("2026-10-05T07:20")) == 0
    assert worker.status()["next_scan"]


def test_calendar_api(settings, db):
    client = TestClient(create_app(settings, db, start_worker=False))
    ids = _channels(db, "a")
    r = client.post("/api/schedules", json={"days": [0, 4], "times": ["08:00"]})
    assert r.status_code == 200 and r.json()["schedules"][0]["days"] == [0, 4]
    sid = r.json()["id"]
    assert client.post("/api/schedules", json={"days": [0], "times": ["8"]}).status_code == 400
    r = client.patch(f"/api/schedules/{sid}", json={"enabled": False, "times": ["07:30", "21:00"]})
    s0 = r.json()["schedules"][0]
    assert not s0["enabled"] and s0["times"] == ["07:30", "21:00"] and s0["next_run"] is None
    r = client.post("/api/schedules", json={"channel_id": ids["a"], "days": [6], "times": ["12:00"]})
    assert [x["channel"] for x in r.json()["schedules"]] == [None, "a"]
    assert client.delete(f"/api/schedules/{r.json()['id']}").json()["schedules"][-1]["channel"] is None
    assert client.get("/calendar").status_code == 200


def test_builtin_templates_and_chroma_defaults(settings, db):
    ed = Editor(settings, db)
    assert ed.ensure_builtin_templates() >= 6
    assert ed.ensure_builtin_templates() == 0
    tpls = ed.templates()
    names = " ".join(t["name"] for t in tpls)
    assert "Спорт" in names and "Стрим" in names and "Мем" in names and "Круг" in names
    shapes = {t["spec"]["video"]["shape"] for t in tpls}
    assert shapes >= {"rect", "rounded", "circle"}
    for t in tpls:
        c = t["spec"]["banner"]["chroma"]
        assert (c["similarity"], c["blend"]) == (0.11, 0.25)
        assert all(x["text"] == "" for x in t["spec"]["texts"])  # no placeholder ever gets exported
    c = normalize({})["banner"]["chroma"]
    assert (c["similarity"], c["blend"]) == (0.11, 0.25)
    with db.session() as s:  # a deleted ready-made template comes back, the owner's own ones are untouched
        s.delete(s.scalars(select(EditTemplate).where(EditTemplate.builtin == "circle")).one())
    assert ed.ensure_builtin_templates() == 1


def test_live_preview_endpoints(settings, db, pipeline, clips):
    import shutil
    from app.database.models import ContentItem, Status
    shutil.copy(clips["landscape"], settings.data("inbox") / "landscape.mp4")
    pipeline.run_once()
    with db.session() as s:
        iid = s.scalars(select(ContentItem.id).where(ContentItem.status == Status.READY_TO_PUBLISH)).one()
    client = TestClient(create_app(settings, db, start_worker=False))
    p = client.get(f"/api/editor/{iid}").json()
    assert p["source_url"] == f"/media/original/{iid}" and isinstance(p["cues"], list)
    r = client.get(p["source_url"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100  # the browser can seek in the video
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 200), (0, 255, 0)).save(buf, "PNG")
    a = client.post("/api/editor/assets", files={"file": ("g.png", buf.getvalue(), "image/png")}).json()
    assert client.get(a["file"]).status_code == 200
    assert client.get("/api/editor/assets").json()["assets"][0]["file"] == a["file"]


def test_subtitle_styles_karaoke_and_voice_over(tmp_path, settings, db, clips):
    """Karaoke gives one picture per word; the voice-over is mixed in and the original is lowered under it."""
    from app.editor import subs
    from app.editor.render import VOICE, Asset, render
    from app.editor.tts import Voices
    from app.media.ffprobe import probe
    from app.media.subtitles import Cue
    import subprocess
    spec = normalize({"subtitles": {"preset": "karaoke", "karaoke": True, "font": "impact", "uppercase": True, "y": 400},
                      "voice": {"enabled": True, "text": "Проверка", "duck": 0.2}})
    cues = [Cue(0.2, 2.0, "раз два три четыре пять шесть семь восемь девять десять одиннадцать")]
    ov = subs.overlays(cues, spec, 4.0, tmp_path / "subs")
    words = len(cues[0].text.split())
    assert len(ov) == words  # one state per word
    assert all(abs(y + 0 - 400) < 200 for _, _, _, y in ov)
    assert ov[0][1] == pytest.approx(0.2) and ov[-1][2] == pytest.approx(2.0)
    plain = normalize({"subtitles": {"karaoke": False}})
    assert len(subs.overlays(cues, plain, 4.0, tmp_path / "p")) == 2  # 11 words → two screens of 2 lines
    # legacy spec still loads
    old = normalize({"subtitles": {"style": "recipe", "position": "top", "font_size": 50}})["subtitles"]
    assert old["box"] and old["y"] == 420 and old["size"] == 50

    from app.config.settings import PROJECT_ROOT
    voices = Voices(settings)
    if (PROJECT_ROOT / "data" / "voices").is_dir():
        voices.dir = PROJECT_ROOT / "data" / "voices"  # Piper models are big: use the installed ones
    if not voices.list():
        pytest.skip("нет голосов")
    voice_id = voices.list()[0]["id"]
    src = clips["landscape"]
    info = probe(src)
    wav = voices.synthesize("Проверка озвучки", voice_id, 1.0)
    assert voices.synthesize("Проверка озвучки", voice_id, 1.0) == wav  # cached
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
                               capture_output=True, text=True).stdout)
    out = tmp_path / "v.mp4"
    render(spec, src, info, {VOICE: Asset(wav, "audio", dur)}, tmp_path / "w", out, {"preset": "ultrafast"}, cues, mode="final")
    o = probe(out)
    assert o.has_audio and (o.width, o.height) == (1080, 1920)


def test_subtitles_are_burned_into_frames(tmp_path, clips):
    """Regression: subtitle pictures must stay on screen for the whole phrase, not one frame."""
    from PIL import Image as Img, ImageChops
    from app.editor.render import render
    from app.media.ffprobe import probe
    from app.media.subtitles import Cue
    src = clips["landscape"]
    info = probe(src)
    cues = [Cue(0.0, 4.0, "Большие белые буквы на экране")]
    base = normalize({"background": {"type": "color", "color": "#203040"}, "subtitles": {"enabled": False}})
    on = normalize({"background": {"type": "color", "color": "#203040"},
                    "subtitles": {"enabled": True, "karaoke": True, "y": 200, "size": 90}})
    for at in (0.5, 2.5):
        a, b = tmp_path / f"a{at}.jpg", tmp_path / f"b{at}.jpg"
        render(base, src, info, {}, tmp_path / f"wa{at}", a, {}, cues, mode="frame", at=at)
        render(on, src, info, {}, tmp_path / f"wb{at}", b, {}, cues, mode="frame", at=at)
        band = (0, 50, 540, 160)  # preview frame is 540×960: subtitles centred at y=200 → 100
        diff = ImageChops.difference(Img.open(a).convert("L").crop(band), Img.open(b).convert("L").crop(band))
        assert max(diff.getdata()) > 120, f"no subtitles at {at}s"


def test_volume_work_trash_auto_templates_queue(settings, db, pipeline, clips):
    import shutil
    from app.database.models import ContentItem, EditProject, Status
    shutil.copy(clips["landscape"], settings.data("inbox") / "landscape.mp4")
    pipeline.run_once()
    with db.session() as s:
        item = s.scalars(select(ContentItem).where(ContentItem.status == Status.READY_TO_PUBLISH)).one()
        iid = item.id
        item.category = "sports"
    worker = FactoryWorker(settings, db)
    client = TestClient(create_app(settings, db, worker=worker, start_worker=False))
    # trash and restore
    assert client.post(f"/api/items/{iid}/trash").status_code == 200
    assert iid not in [i["id"] for i in client.get("/api/editor/items").json()["items"]]
    assert client.post(f"/api/items/{iid}/restore").status_code == 200
    assert iid in [i["id"] for i in client.get("/api/editor/items?only_new=true").json()["items"]]
    # auto template by category: applied once, queued for export to «Готовые»
    worker.editor.ensure_builtin_templates()
    tid = worker.editor.templates()[0]["id"]
    assert client.put("/api/editor/auto-templates", json={"mapping": {"sports": tid, "memes": None, "x": 99999}}).json() \
        == {"mapping": {"sports": tid}}
    assert worker.editor.apply_auto_templates() == [iid]
    assert worker.editor.apply_auto_templates() == []  # already has a project
    with db.session() as s:
        assert s.get(EditProject, iid).render_state == "queued"
    q = client.get("/api/editor/queue").json()
    assert q["queued"] == 1 and q["items"][str(iid)] == "queued"


def test_every_builtin_template_renders(tmp_path, clips):
    from app.editor.render import render
    from app.editor.templates import builtin_specs
    from app.media.ffprobe import probe
    from app.media.subtitles import Cue
    info = probe(clips["landscape"])
    for key, name, spec in builtin_specs():
        s = normalize(spec, info.duration)
        assert (s["banner"]["chroma"]["similarity"], s["banner"]["chroma"]["blend"]) == (0.11, 0.25)
        out = tmp_path / f"{key}.jpg"
        render(s, clips["landscape"], info, {}, tmp_path / key, out, {}, [Cue(0, 3, "Проверка подписи")], mode="frame", at=1.0)
        assert out.is_file() and out.stat().st_size > 1000, key
