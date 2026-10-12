import shutil

from sqlalchemy import select

from app.database.models import ContentItem, Status, Variant
from app.media.ffprobe import probe
from app.processing.dryrun import dry_run
from app.processing.pipeline import Pipeline


def _inbox(settings, clips, *names, rename=None):
    inbox = settings.data("inbox")
    for n in names:
        shutil.copy(clips[n], inbox / f"{(rename or {}).get(n, n)}.mp4")
    return inbox


def _items(db):
    with db.session() as s:
        return {i.original_filename or i.title: i for i in s.scalars(select(ContentItem))}


def _snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in root.rglob("*") if p.is_file())


def test_full_pipeline_to_final_mp4(settings, db, pipeline, clips):
    inbox = _inbox(settings, clips, "landscape")
    (inbox / "landscape.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nПривет\n", encoding="utf-8")
    report = pipeline.run_once()
    assert report.processed == 1 and report.failed == 0

    item = _items(db)["landscape.mp4"]
    assert item.status == Status.READY_TO_PUBLISH  # folder rights are USER_OWNED
    assert item.sha256 and item.phash and item.has_subtitles
    with db.session() as s:
        v = s.scalars(select(Variant)).one()
    info = probe(v.path)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert v.burned_subtitles
    final = settings.data("final")
    stem = v.path.rsplit("/", 1)[1][:-4]
    # No speech and almost no text: the category is "uncertain" rather than a guess.
    assert stem.split("_")[2] == "uncertain" and stem.endswith(f"_{item.id}")
    assert (final / f"{stem}.json").exists() and (settings.data("thumbnails") / f"{stem}.jpg").exists()
    assert not list(settings.data("temp").iterdir()) and not list(settings.data("processing").iterdir())


def test_rerun_creates_no_duplicates(settings, db, pipeline, clips):
    _inbox(settings, clips, "landscape")
    pipeline.run_once()
    before = _snapshot(settings.data("final"))
    report = pipeline.run_once()
    assert (report.discovered, report.processed) == (0, 0)
    assert _snapshot(settings.data("final")) == before
    assert len(_items(db)) == 1


def test_exact_and_visual_duplicates(settings, db, pipeline, clips, tmp_path):
    # Discovery is alphabetical, so a_original is stored first and the others are its copies.
    inbox = _inbox(settings, clips, "landscape", rename={"landscape": "a_original"})
    shutil.copy(clips["landscape"], inbox / "copy.mp4")
    from app.media.ffmpeg import run
    run(["-i", str(clips["landscape"]), "-vf", "scale=854:480", "-c:v", "libx264", "-crf", "30",
         "-c:a", "aac", str(inbox / "reencoded.mp4")])
    pipeline.run_once()
    items = _items(db)
    assert items["a_original.mp4"].status == Status.READY_TO_PUBLISH
    assert items["copy.mp4"].status == Status.DUPLICATE and "SHA-256" in items["copy.mp4"].rejection_reason
    assert items["reencoded.mp4"].status == Status.DUPLICATE and "pHash" in items["reencoded.mp4"].rejection_reason
    assert len(list(settings.data("final").glob("*.mp4"))) == 1


def test_bad_video_does_not_stop_others(settings, db, pipeline, clips):
    inbox = _inbox(settings, clips, "landscape", "short")
    (inbox / "broken.mp4").write_bytes(clips["landscape"].read_bytes()[:30000])
    for _ in range(settings.get("factory.max_retries")):
        pipeline.run_once()
    items = _items(db)
    assert items["landscape.mp4"].status == Status.READY_TO_PUBLISH
    assert items["short.mp4"].status == Status.REJECTED
    broken = items["broken"]
    assert broken.status == Status.FAILED and broken.retry_count == 3 and broken.last_error


def test_unknown_rights_are_not_ready_to_publish(settings, db, clips):
    settings.raw["sources"]["filesystem"]["folders"][0]["rights_status"] = "UNKNOWN"
    _inbox(settings, clips, "landscape")
    Pipeline(settings, db).run_once()
    item = _items(db)["landscape.mp4"]
    assert item.status == Status.MANUAL_REVIEW and "UNKNOWN" in item.decision_reason
    assert not list(settings.data("final").glob("*.mp4"))
    assert (settings.data("review") / f"{item.id}_landscape.mp4").is_symlink()


def test_dry_run_changes_nothing(settings, db, pipeline, clips):
    _inbox(settings, clips, "landscape")
    pipeline.run_once()
    _inbox(settings, clips, "other", "short")
    before = _snapshot(settings.root)
    lines = "\n".join(dry_run(settings, 10))
    assert "новых материалов: 2" in lines and "будет отклонён" in lines
    assert _snapshot(settings.root) == before


def test_recovery_after_crash_mid_processing(settings, db, pipeline, clips):
    _inbox(settings, clips, "landscape")
    report_stages = Pipeline(settings, db)
    from app.processing.pipeline import RunReport
    r = RunReport()
    report_stages.discover(10, r)
    report_stages.download(10, r)
    report_stages.analyze(10, r)
    orphan = settings.data("processing") / "orphan.part.mp4"
    orphan.write_bytes(b"half-written")
    with db.session() as s:
        item = s.scalars(select(ContentItem)).one()
        item.status, item.pending_output = Status.PROCESSING, str(orphan)

    report = pipeline.run_once()
    assert report.recovered == 1 and report.processed == 1
    assert not orphan.exists()
    assert _items(db)["landscape.mp4"].status == Status.READY_TO_PUBLISH


def test_status_transitions_are_atomic_on_render_failure(settings, db, pipeline, clips, monkeypatch):
    _inbox(settings, clips, "landscape")
    from app.media import ffmpeg as ff

    def boom(*a, **k):
        raise ff.FFmpegError("simulated")
    monkeypatch.setattr(ff, "render_vertical", boom)
    pipeline.run_once()
    item = _items(db)["landscape.mp4"]
    assert item.status == Status.APPROVED and item.retry_count == 1 and "simulated" in item.last_error
    assert item.pending_output is None and not list(settings.data("final").iterdir())
