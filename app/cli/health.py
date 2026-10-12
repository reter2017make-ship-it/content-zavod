from __future__ import annotations

import shutil
import socket
import subprocess
import sys

from app.config.settings import Settings
from app.database.database import Database
from app.processing.storage import dir_size_gb, disk_usage_percent
from app.sources.registry import build_sources

OK, WARNING, ERROR, DISABLED = "OK", "WARNING", "ERROR", "DISABLED"

# Integrations planned for later stages: (name, env vars it needs, stage).
NOT_PLANNED = ["YouTube upload", "TikTok"]  # по решению пользователя публикация ручная


def _version(binary: str) -> str:
    out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=15).stdout
    return out.splitlines()[0] if out else "?"


def checks(settings: Settings) -> list[tuple[str, str, str]]:
    res: list[tuple[str, str, str]] = []

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    py_ok = sys.version_info >= (3, 12)
    res.append(("Python", OK if py_ok and in_venv else (WARNING if py_ok else ERROR),
                f"{sys.version.split()[0]} {'(.venv)' if in_venv else '(НЕ .venv)'}"))

    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        res.append((binary.upper(), OK if path else ERROR, _version(path) if path else "не найден в PATH"))

    try:
        db = Database(settings.database_path)
        db.create_all()
        with db.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db.dispose()
        res.append(("Database", OK, str(settings.database_path)))
    except Exception as exc:
        res.append(("Database", ERROR, str(exc)))

    usage = disk_usage_percent(settings.data_dir)
    warn, stop = float(settings.get("storage.warn_disk_percent", 80)), float(settings.get("storage.stop_disk_percent", 90))
    free_gb = shutil.disk_usage(settings.root).free / 1024 ** 3
    level = ERROR if usage >= stop else WARNING if usage >= warn else OK
    res.append(("Disk", level, f"занято {usage:.0f}%, свободно {free_gb:.0f} GB"))
    temp_gb, max_temp = dir_size_gb(settings.data("temp")), float(settings.get("storage.max_temp_gb", 50))
    res.append(("Temp", WARNING if temp_gb > max_temp else OK, f"{temp_gb:.2f} GB из {max_temp:g} GB"))

    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        res.append(("Internet", OK, "есть соединение"))
    except OSError:
        res.append(("Internet", WARNING, "нет соединения (локальная обработка работает)"))

    for name, src in build_sources(settings, Database(settings.database_path)).items():
        ready, why = src.configured()
        level = OK if ready else DISABLED if ("не реализовано" in why or "выключен" in why) else WARNING
        res.append((f"Source: {name}", level, "готов" if ready else why))

    res.extend(_ai_checks(settings))

    from app.sources.telegram_api import SESSION_FILE
    if not (settings.env("TELEGRAM_API_ID") and settings.env("TELEGRAM_API_HASH")):
        res.append(("Telegram API", DISABLED, "не подключено (необязательно): «Источники» → Telegram API"))
    elif not settings.path(SESSION_FILE + ".session").exists():
        res.append(("Telegram API", WARNING, "ключи есть, нужен вход по номеру в «Источниках»"))
    else:
        res.append(("Telegram API", OK, "сессия сохранена; большие видео качаются через API"))
    missing = [v for v in ("META_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID") if not settings.env(v)]
    if not settings.get("publishing.instagram.enabled", True):
        res.append(("Instagram", DISABLED, "выключен в config.yaml"))
    elif missing:
        res.append(("Instagram", WARNING, "нужен токен: " + ", ".join(missing) + " (очередь работает в dry-run)"))
    else:
        res.append(("Instagram", OK, "ключи заданы; подключение проверяется в «Публикация» или `instagram check`"))
    for name in NOT_PLANNED:
        res.append((name, DISABLED, "не делается: публикация ручная из «Готовых видео»"))
    return res


def worst(results: list[tuple[str, str, str]]) -> str:
    levels = [r[1] for r in results]
    return ERROR if ERROR in levels else WARNING if WARNING in levels else OK


def format_results(results: list[tuple[str, str, str]]) -> str:
    width = max(len(r[0]) for r in results)
    return "\n".join(f"{name.ljust(width)}  {level.ljust(8)}  {detail}" for name, level, detail in results)



def _ai_checks(settings: Settings) -> list[tuple[str, str, str]]:
    from app.ai.analyzer import build_provider
    out = []
    try:
        provider = build_provider(settings)
    except ValueError as exc:
        return [("AI provider", ERROR, str(exc))]
    if provider is None:
        out.append(("AI provider", WARNING, "rules: оценка по правилам, без языковой модели"))
    else:
        ready, why = provider.available()
        out.append(("AI provider", OK if ready else ERROR, f"{provider.name}: {'готов' if ready else why}"))
    if settings.get("transcription.enabled", True):
        try:
            import faster_whisper  # noqa: F401
            out.append(("Whisper", OK, f"faster-whisper, модель {settings.get('transcription.model', 'small')}"))
        except ImportError:
            out.append(("Whisper", ERROR, "faster-whisper не установлен в .venv"))
    else:
        out.append(("Whisper", DISABLED, "выключен в config.yaml"))
    return out
