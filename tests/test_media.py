from pathlib import Path

import pytest

from app.media import ffmpeg, subtitles
from app.media.background import is_vertical
from app.media.ffprobe import ProbeError, probe
from app.scoring.duplicate import phash_distance, sha256_file, video_phash


def test_probe_reads_streams(clips):
    info = probe(clips["landscape"])
    assert (info.width, info.height) == (1280, 720)
    assert info.has_audio and 5.9 < info.duration < 6.1
    assert not probe(clips["vertical_silent"]).has_audio


def test_probe_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(ProbeError):
        probe(bad)


def test_is_vertical():
    assert is_vertical(1080, 1920, 1080, 1920, 0.08)
    assert is_vertical(720, 1280, 1080, 1920, 0.08)
    assert not is_vertical(1080, 1080, 1080, 1920, 0.08)
    assert not is_vertical(1920, 1080, 1080, 1920, 0.08)


def test_srt_roundtrip_and_split(tmp_path):
    cues = subtitles.parse_srt(
        "1\n00:00:01,000 --> 00:00:03,000\n<i>Первая</i> строка\n\n"
        "2\n00:00:04,500 --> 00:00:09,000\n" + "очень длинный текст " * 8 + "\n"
    )
    assert [c.start for c in cues] == [1.0, 4.5]
    assert cues[0].text == "Первая строка"
    parts = subtitles.split_for_screen(cues, max_chars=32, max_lines=2)
    assert len(parts) > 2
    assert all(len(lines) <= 2 and all(len(l) <= 32 for l in lines) for _, lines in parts)
    assert parts[-1][0].end == pytest.approx(9.0)
    out = tmp_path / "x.srt"
    subtitles.write_srt(cues, out)
    assert subtitles.load_srt(out)[1].start == 4.5


def test_render_landscape_to_vertical_with_subtitles(tmp_path, clips, base_config):
    out = tmp_path / "out.mp4"
    cues = [subtitles.Cue(0.5, 3.0, "Тест субтитров")]
    res = ffmpeg.render_vertical(clips["landscape"], out, probe(clips["landscape"]),
                                 base_config["processing"] | {"preset": "ultrafast"},
                                 base_config["subtitles"], cues, tmp_path)
    info = probe(out)
    assert (info.width, info.height, info.codec) == (1080, 1920, "h264")
    assert info.has_audio and info.audio_codec == "aac"
    assert res.profile == "blurred+subtitles" and res.burned_subtitles


def test_render_adds_silent_audio(tmp_path, clips, base_config):
    out = tmp_path / "out.mp4"
    res = ffmpeg.render_vertical(clips["vertical_silent"], out, probe(clips["vertical_silent"]),
                                 base_config["processing"] | {"preset": "ultrafast"},
                                 base_config["subtitles"], None, tmp_path)
    assert res.profile == "vertical_original"
    assert probe(out).has_audio


def test_phash_detects_reencode_not_other_video(tmp_path, clips):
    src = clips["landscape"]
    small = tmp_path / "small.mp4"
    ffmpeg.run(["-i", str(src), "-vf", "scale=640:360", "-c:v", "libx264", "-crf", "30", "-c:a", "aac", str(small)])
    a, b = video_phash(src, 6), video_phash(small, 6)
    c = video_phash(clips["other"], 6)
    assert sha256_file(src) != sha256_file(small)
    assert phash_distance(a, b) <= 10
    assert phash_distance(a, c) > 10


def test_thumbnail(tmp_path, clips):
    thumb = tmp_path / "t.jpg"
    ffmpeg.thumbnail(clips["landscape"], thumb, 1.0, 270)
    assert thumb.stat().st_size > 0


def test_make_test_video(tmp_path):
    p = ffmpeg.make_test_video(tmp_path / "v.mp4", 320, 240, 1.5)
    assert isinstance(p, Path) and probe(p).duration == pytest.approx(1.5, abs=0.1)


def test_screen_text_detection(tmp_path):
    """Text burned into the picture is found (macOS text recognition); a plain picture has none."""
    import pytest
    from PIL import Image, ImageDraw
    from app.media import screen_text
    from app.media.subtitles import _font
    from app.media.ffmpeg import ffmpeg_path
    import subprocess
    if not screen_text.available():
        pytest.skip("нет Apple Vision")
    img = Image.new("RGB", (720, 1280), (30, 30, 40))
    ImageDraw.Draw(img).text((60, 560), "Когда пятница", font=_font("heavy", 80), fill=(255, 255, 255))
    img.save(tmp_path / "t.png")
    Image.new("RGB", (720, 1280), (30, 90, 40)).save(tmp_path / "p.png")
    for name in ("t", "p"):
        subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-loop", "1", "-i", str(tmp_path / f"{name}.png"), "-t", "3",
                        "-pix_fmt", "yuv420p", str(tmp_path / f"{name}.mp4")], check=True)
    has, text = screen_text.detect(tmp_path / "t.mp4", 3.0, frames=2)
    assert has and "пятница" in text.lower()
    assert screen_text.detect(tmp_path / "p.mp4", 3.0, frames=2) == (False, "")
