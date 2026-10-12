"""Ten synthetic clips for checking the analysis stage: 5 good, 5 deliberately bad.

Speech comes from the built-in macOS `say` voice (Milena, Russian); video from FFmpeg test sources.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.media.ffmpeg import run

SAMPLES = [
    # name, expected, video source, speech text (None = no speech), extra audio
    ("good_recipe", "good", "testsrc2=size=1920x1080:rate=30",
     "Сегодня готовим самую быструю пасту за десять минут. Берём чеснок, оливковое масло и спелые помидоры. "
     "Смотрите до конца, результат вас удивит!"),
    ("good_funny_cat", "good", "mandelbrot=size=1080x1920:rate=30",
     "Вы не поверите, что сделал мой кот, когда увидел огурец. Он подпрыгнул почти на метр! "
     "Смотри до конца, это нужно видеть."),
    ("good_fact", "good", "life=size=1080x1920:mold=10:rate=30:ratio=0.1:death_color=#C83232:life_color=#00ff00",
     "Знаете ли вы, что у осьминога три сердца и голубая кровь? А ещё он умеет менять цвет за доли секунды."),
    ("good_sports", "good", "testsrc=size=1280x720:rate=30",
     "Невероятный гол на последней минуте! Вратарь даже не успел среагировать. "
     "Этот момент болельщики будут помнить ещё много лет."),
    ("good_puppy", "good", "gradients=size=1080x1080:rate=30:speed=0.05",
     "Этот щенок впервые увидел снег. Посмотрите на его реакцию, это самое милое видео за сегодня."),
    ("bad_black", "bad", "color=c=black:size=1920x1080:rate=30",
     "Здесь ничего не видно, просто чёрный экран и голос за кадром. Камера так и не включилась, поэтому смотреть тут совершенно нечего."),
    ("bad_frozen_silent", "bad", "color=c=gray:size=1080x1920:rate=30", None),
    ("bad_blurry", "bad", "testsrc2=size=640x360:rate=30,boxblur=12:3", None),
    ("bad_moderation", "bad", "testsrc2=size=1920x1080:rate=30",
     "Сейчас покажу, как сделать бомбу в домашних условиях и где купить наркотики без рецепта."),
    ("bad_noise", "bad", "color=c=gray:size=720x1280:rate=30,noise=alls=100:allf=t", None),
]


def _say(text: str, out: Path) -> Path:
    if not shutil.which("say"):
        raise RuntimeError("нет команды say (нужен macOS)")
    subprocess.run(["say", "-v", "Milena", "-o", str(out), text], check=True, timeout=120)
    return out


def make_samples(dest: Path, only: list[str] | None = None) -> list[tuple[Path, str]]:
    dest.mkdir(parents=True, exist_ok=True)
    made = []
    for name, expected, video, speech in SAMPLES:
        if only and name not in only:
            continue
        out = dest / f"{name}.mp4"
        if speech:
            aiff = dest / f".{name}.aiff"
            _say(speech, aiff)
            args = ["-f", "lavfi", "-i", video, "-i", str(aiff), "-shortest",
                    "-af", "apad=pad_dur=1", "-c:a", "aac"]
        elif name == "bad_noise":
            args = ["-f", "lavfi", "-i", video, "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.3",
                    "-t", "12", "-c:a", "aac"]
        elif name == "bad_blurry":
            args = ["-f", "lavfi", "-i", video, "-f", "lavfi", "-i", "sine=frequency=60", "-t", "12",
                    "-c:a", "aac", "-b:v", "60k"]
        else:
            args = ["-f", "lavfi", "-i", video, "-t", "12"]
        run([*args, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)])
        if speech:
            aiff.unlink(missing_ok=True)
        made.append((out, expected))
    return made
