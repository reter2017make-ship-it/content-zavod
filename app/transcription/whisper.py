from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

import numpy as np

from app.media.ffmpeg import FFmpegError, ffmpeg_path

from app.transcription.models import Segment, Transcript

log = logging.getLogger("transcription")


class WhisperTranscriber:
    """faster-whisper on CPU (int8). The model is downloaded to the Hugging Face cache on first use."""

    def __init__(self, cfg: dict):
        self.model_name = cfg.get("model", "small")
        self.compute_type = cfg.get("compute_type", "int8")
        self.language = cfg.get("language") or None
        self.vad = bool(cfg.get("vad_filter", True))
        self.beam_size = int(cfg.get("beam_size", 5))
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info("loading whisper model %s (%s)", self.model_name, self.compute_type)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type)
        return self._model

    @staticmethod
    def _audio(media: Path) -> np.ndarray:
        """16 kHz mono float32 decoded by FFmpeg (faster-whisper's own PyAV path breaks with newer av)."""
        cmd = [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(media),
               "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
        proc = subprocess.run(cmd, capture_output=True, timeout=900)
        if proc.returncode != 0:
            raise FFmpegError(f"не удалось извлечь звук: {proc.stderr.decode(errors='replace')[-300:]}")
        return np.frombuffer(proc.stdout, dtype=np.float32)

    def transcribe(self, media: Path) -> Transcript:
        audio = self._audio(media)
        if audio.size < 1600:
            return Transcript(text="", language=None)
        segments, info = self._load().transcribe(
            audio, language=self.language, vad_filter=self.vad, beam_size=self.beam_size,
        )
        out = [
            Segment(round(s.start, 2), round(s.end, 2), s.text.strip(),
                    round(math.exp(s.avg_logprob), 3) if s.avg_logprob is not None else None)
            for s in segments if s.text.strip()
        ]
        return Transcript(
            text=" ".join(s.text for s in out),
            language=info.language if out else None,
            segments=out,
            language_probability=round(info.language_probability, 3) if out else None,
        )
