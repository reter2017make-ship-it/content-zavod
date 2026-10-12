"""`run --dry-run`: shows what a real run would do without writing files, the database or publishing."""
from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.database import repositories as repo
from app.database.database import Database
from app.database.models import Status
from app.downloader.validators import check_filters
from app.media.ffprobe import probe
from app.sources.registry import build_sources


def dry_run(settings: Settings, limit: int) -> list[str]:
    lines = ["DRY-RUN: ничего не скачивается, не обрабатывается, не публикуется и не записывается в базу."]
    db = Database(settings.database_path, readonly=True) if settings.database_path.exists() else None
    try:
        known: dict[str, set[str]] = {}
        if db:
            with db.session() as s:
                counts = repo.count_by_status(s)
                for name in build_sources(settings, db):
                    known[name] = repo.known_source_ids(s, name)
            pending = {st: counts.get(st.value, 0) for st in (Status.DISCOVERED, Status.DOWNLOADED, Status.APPROVED)}
            lines.append(
                f"В базе ждут: скачивания {pending[Status.DISCOVERED]}, анализа {pending[Status.DOWNLOADED]}, "
                f"обработки {pending[Status.APPROVED]}."
            )
        else:
            lines.append("Базы ещё нет: она будет создана при первом настоящем запуске.")

        filters = settings.section("filters")
        for name, src in build_sources(settings, db).items():
            ready, why = src.configured()
            if not ready:
                lines.append(f"[{name}] пропущен: {why}")
                continue
            try:
                found = src.discover(known.get(name, set()), limit)
            except Exception as exc:
                lines.append(f"[{name}] ошибка: {exc}")
                continue
            finally:
                src.close()
            lines.append(f"[{name}] новых материалов: {len(found)}")
            for d in found:
                verdict = "будет скачан"
                path = Path(d.source_id)
                if path.is_file():
                    try:
                        reason = check_filters(probe(path), filters)
                        verdict = f"будет отклонён: {reason}" if reason else "пройдёт фильтры и будет обработан"
                    except Exception as exc:
                        verdict = f"ошибка чтения: {exc}"
                lines.append(f"  - {d.title} ({d.category}, права {d.rights_status}): {verdict}")
        publishing = settings.get("publishing.enabled", False)
        lines.append(f"Публикация: {'включена' if publishing else 'выключена'}; в dry-run не выполняется никогда.")
    finally:
        if db:
            db.dispose()
    return lines
