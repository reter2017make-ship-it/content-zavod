"""Local Russian voice-over: Piper voices (data/voices/*.onnx, offline, free) and the macOS voice
via `say` as a fallback. Results are cached by (voice, speed, text)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
from pathlib import Path

from app.media.ffmpeg import ffmpeg_path

PIPER_NAMES = {"irina": "Ирина", "denis": "Денис", "dmitri": "Дмитрий", "ruslan": "Руслан"}
MAX_CHARS = 3000


class TTSError(ValueError):
    pass


class Voices:
    def __init__(self, settings):
        self.dir = settings.data("voices")
        self.cache = settings.data("tts_cache")
        self._loaded: dict[str, object] = {}
        self._lock = threading.Lock()

    def _piper_files(self) -> dict[str, Path]:
        out = {}
        for p in sorted(self.dir.glob("ru_RU-*.onnx")):
            key = p.stem.split("-")[1]  # ru_RU-irina-medium → irina
            if p.with_suffix(".onnx.json").is_file():
                out[key] = p
        return out

    @staticmethod
    def _say_voices() -> list[str]:
        if not shutil.which("say"):
            return []
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return []
        return [line.split()[0] for line in out.splitlines() if "ru_RU" in line]

    def list(self) -> list[dict]:
        voices = [{"id": k, "name": PIPER_NAMES.get(k, k.capitalize()), "engine": "piper"} for k in self._piper_files()]
        voices += [{"id": "say:" + v, "name": f"{v} (macOS)", "engine": "say"} for v in self._say_voices()]
        return voices

    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> Path:
        """WAV (48 kHz stereo) of the text. Speed 1 = normal, 1.3 = faster."""
        text = " ".join((text or "").split())[:MAX_CHARS]
        if not text:
            raise TTSError("нет текста для озвучки")
        speed = max(0.5, min(2.0, float(speed)))
        key = hashlib.sha256(f"{voice}|{speed:.2f}|{text}".encode()).hexdigest()[:24]
        out = self.cache / f"{key}.wav"
        if out.is_file():
            return out
        self.cache.mkdir(parents=True, exist_ok=True)
        raw = out.with_suffix(".raw.wav")
        if voice.startswith("say:"):
            name = voice[4:]
            if name not in self._say_voices():
                raise TTSError(f"голоса {name} нет в системе")
            aiff = out.with_suffix(".aiff")
            subprocess.run(["say", "-v", name, "-r", str(int(175 * speed)), "-o", str(aiff), text],
                           check=True, timeout=300)
            src = aiff
        else:
            model = self._piper_files().get(voice)
            if model is None:
                raise TTSError(f"голос «{voice}» не установлен (data/voices)")
            from piper import PiperVoice, SynthesisConfig
            import wave
            with self._lock:
                pv = self._loaded.get(voice) or PiperVoice.load(str(model))
                self._loaded[voice] = pv
                with wave.open(str(raw), "wb") as wf:
                    pv.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.0 / speed))
            src = raw
        tmp = out.with_suffix(".part.wav")
        subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-ar", "48000", "-ac", "2", str(tmp)], check=True, timeout=300)
        tmp.replace(out)
        src.unlink(missing_ok=True)
        return out
