"""Is there text in the picture (burned-in subtitles, captions, meme text, text watermarks)?
A few frames of the video go through macOS's built-in text recognition (Apple Vision, ru + en)."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

MIN_CONFIDENCE = 0.5
MIN_CHARS = 3  # single letters and digits from textures are not «text»


def available() -> bool:
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def _frames(video: Path, duration: float | None, n: int, out_dir: Path) -> list[Path]:
    from app.media.ffmpeg import ffmpeg_path
    dur = duration or 10.0
    paths = []
    for k in range(n):
        at = dur * (k + 1) / (n + 1)
        out = out_dir / f"f{k}.jpg"
        subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale='min(1080,iw)':-2", str(out)], capture_output=True, timeout=60)
        if out.is_file():
            paths.append(out)
    return paths


def recognize(image: Path) -> list[str]:
    import Quartz
    import Vision
    from Foundation import NSURL
    url = NSURL.fileURLWithPath_(str(image))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return []
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["ru-RU", "en-US"])
    req.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return []
    out = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if cand and cand[0].confidence() >= MIN_CONFIDENCE:
            t = str(cand[0].string()).strip()
            if sum(ch.isalpha() for ch in t) >= MIN_CHARS:
                out.append(t)
    return out


def detect(video: Path, duration: float | None = None, frames: int = 5) -> tuple[bool, str]:
    """(has text, the text found — short, for a tooltip)."""
    with tempfile.TemporaryDirectory() as tmp:
        found: list[str] = []
        for f in _frames(video, duration, frames, Path(tmp)):
            for t in recognize(f):
                if t not in found:
                    found.append(t)
    return bool(found), " · ".join(found)[:300]
