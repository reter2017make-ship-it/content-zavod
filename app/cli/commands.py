from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from app.config.settings import load_settings
from app.database import repositories as repo
from app.main import AlreadyRunning, bootstrap, factory_lock
from app.processing.pipeline import Pipeline, RunReport


def _print_report(report: RunReport) -> None:
    print("Итог: " + report.summary())
    for note in report.notes:
        print("  ! " + note)


def cmd_run(args) -> int:
    if args.dry_run:
        from app.processing.dryrun import dry_run
        settings = load_settings(args.config)
        print("\n".join(dry_run(settings, args.limit or int(settings.get("factory.max_items_per_run", 100)))))
        return 0

    settings, db = bootstrap(args.config)
    pipeline = Pipeline(settings, db)
    try:
        with factory_lock(settings):
            while True:
                _print_report(pipeline.run_once(args.limit))
                if args.once:
                    return 0
                interval = int(settings.get("factory.loop_interval_minutes", 10))
                print(f"Следующий цикл через {interval} мин. Остановить: Ctrl+C")
                time.sleep(interval * 60)
    except AlreadyRunning as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Остановлено.")
        return 0


def _stage(name: str):
    def handler(args) -> int:
        settings, db = bootstrap(args.config)
        pipeline = Pipeline(settings, db)
        report = RunReport()
        limit = args.limit or int(settings.get("factory.max_items_per_run", 100))
        try:
            with factory_lock(settings):
                pipeline.recover(report)
                getattr(pipeline, name)(limit, report)
        except AlreadyRunning as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
        _print_report(report)
        return 0
    return handler


def cmd_status(args) -> int:
    settings, db = bootstrap(args.config)
    with db.session() as s:
        counts = repo.count_by_status(s)
        items = repo.recent(s, args.limit or 10)
        print("Материалы по статусам:")
        if not counts:
            print("  (пусто)")
        for status, n in sorted(counts.items()):
            print(f"  {status:<17} {n}")
        if items:
            print("\nПоследние:")
            for i in items:
                extra = i.rejection_reason or i.last_error or ""
                print(f"  #{i.id:<5} {i.status.value:<17} {i.category or '-':<10} {i.rights_status.value:<15} "
                      f"{(i.original_filename or i.title or '')[:40]:<40} {extra[:80]}")
    return 0


def _describe(item) -> str:
    lines = [
        f"#{item.id}  {item.original_filename}",
        f"  видео:     {item.original_file}",
        f"  источник:  {item.source} {item.channel or ''}",
        f"  категория: {item.category}  длительность {item.duration or 0:.1f}s  {item.width}x{item.height}",
        f"  баллы:     итог {item.content_score}  вирусность {item.viral_score}  качество {item.quality_score}"
        f"  оригинальность {item.originality_score}  (оценка: {item.ai_provider})",
        f"  права:     {item.rights_status.value}  риск {item.rights_risk_score}",
        f"  причина:   {item.decision_reason}",
    ]
    if item.moderation_flags:
        lines.append(f"  модерация: {item.moderation_flags}")
    if item.transcript:
        lines.append(f"  речь:      {item.transcript[:200]}")
    return "\n".join(lines)


def cmd_review(args) -> int:
    from sqlalchemy import select

    from app.database.models import ContentItem, Status
    from app.processing import review

    settings, db = bootstrap(args.config)
    with db.session() as s:
        if args.approve or args.reject:
            item = s.get(ContentItem, args.approve or args.reject)
            if item is None:
                print("Нет такого материала", file=sys.stderr)
                return 1
            try:
                (review.approve if args.approve else review.reject)(settings, item, args.note)
            except ValueError as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 1
            print(f"#{item.id}: {item.decision_reason}")
            return 0

        items = list(s.scalars(select(ContentItem).where(ContentItem.status == Status.MANUAL_REVIEW)
                               .order_by(ContentItem.id)))
        if not items:
            print("На ручной проверке ничего нет.")
            return 0
        if args.list or not sys.stdin.isatty():
            for item in items:
                print(_describe(item) + "\n")
            print(f"Всего: {len(items)}. Решение: review --approve ID | --reject ID [--note текст]")
            return 0
        for item in items:
            print("\n" + _describe(item))
            while True:
                answer = input("  [a]pprove / [r]eject / [s]kip / [q]uit > ").strip().lower()
                if answer in ("a", "approve"):
                    review.approve(settings, item)
                elif answer in ("r", "reject"):
                    review.reject(settings, item, input("  причина (можно пусто): ").strip() or None)
                elif answer in ("q", "quit"):
                    return 0
                elif answer not in ("s", "skip"):
                    continue
                s.commit()
                break
    return 0


def cmd_health(args) -> int:
    from app.cli.health import ERROR, checks, format_results, worst
    settings, _ = bootstrap(args.config)
    results = checks(settings)
    print(format_results(results))
    overall = worst(results)
    print(f"\nОбщий статус: {overall}")
    return 1 if overall == ERROR else 0


def cmd_make_test_video(args) -> int:
    from app.media.ffmpeg import make_test_video
    settings, _ = bootstrap(args.config)
    inbox = settings.data("inbox")
    out = make_test_video(inbox / "test_landscape.mp4", 1920, 1080, args.duration)
    (inbox / "test_landscape.srt").write_text(
        "1\n00:00:00,500 --> 00:00:03,500\nПривет! Это тестовый ролик контент-завода\n\n"
        "2\n00:00:04,000 --> 00:00:07,500\nСубтитры наложены автоматически\n", encoding="utf-8")
    print(f"Создано: {out} (+ субтитры {out.with_suffix('.srt').name})")
    return 0


def cmd_make_samples(args) -> int:
    from app.media.samples import make_samples
    settings, _ = bootstrap(args.config)
    for path, expected in make_samples(settings.data("inbox") / "samples"):
        print(f"{expected:>4}  {path}")
    return 0


def cmd_web(args) -> int:
    import uvicorn

    from app.web.server import create_app
    settings, db = bootstrap(args.config)
    # in Docker the server listens on all interfaces of the container (compose publishes the port to 127.0.0.1 only)
    host = os.environ.get("FACTORY_WEB_HOST") or settings.get("web.host", "127.0.0.1")
    port = args.port or int(settings.get("web.port", 8765))
    try:
        with factory_lock(settings):  # the web worker is the factory while the server runs
            print(f"Контент-завод: http://{host}:{port}  (остановить: Ctrl+C)")
            uvicorn.run(create_app(settings, db), host=host, port=port, log_level="warning")
    except AlreadyRunning as exc:
        print(f"Ошибка: {exc}. Остановите `run`, затем запустите веб-интерфейс.", file=sys.stderr)
        return 2
    return 0


def cmd_cleanup(args) -> int:
    settings, _ = bootstrap(args.config)
    removed = 0
    try:
        with factory_lock(settings):
            for sub in ("temp", "processing"):
                for p in settings.data(sub).iterdir():
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    removed += 1
    except AlreadyRunning as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    print(f"Удалено временных объектов: {removed}. Оригиналы и готовые ролики не тронуты.")
    return 0


def cmd_publish(args) -> int:
    from app.publishing.queue import PublishQueue
    if args.dry_run:
        from app.database.database import Database
        settings = load_settings(args.config)
        if not settings.database_path.exists():
            print("Базы ещё нет: публиковать нечего.")
            return 0
        db = Database(settings.database_path, readonly=True)
        try:
            pq = PublishQueue(settings, db)
            plans = pq.dry_run(record=False)
        finally:
            db.dispose()
        print("DRY-RUN публикации: ничего не отправляется в Instagram и не записывается в базу.")
        print(f"Режим завода: {'реальная публикация' if pq.live else 'dry-run'}"
              f"{', пауза' if pq.paused else ''}. Часовой пояс: {pq.tz}.")
        if not plans:
            print("В очереди ничего нет. Поставить ролик: веб-интерфейс → «Публикация».")
        for pl in plans:
            print(f"\n#{pl['publication']}  ролик #{pl['content_id']}  {pl['title'] or ''}")
            print(f"  куда:     {pl['where']}")
            print(f"  когда:    {pl['when_local'] or '—'} ({pq.tz})")
            print(f"  файл:     {pl['video']}")
            print(f"  описание: {pl['caption'][:300]}")
            for c in pl["checks"]:
                print(f"  {'✓' if c['ok'] else '✕'} {c['name']}: {c['detail']}")
        return 0

    settings, db = bootstrap(args.config)
    pq = PublishQueue(settings, db)
    try:
        with factory_lock(settings):
            pq.recover()
            print(pq.tick())
    except AlreadyRunning as exc:
        print(f"Ошибка: {exc}. Пока работает веб-интерфейс, публикует он сам.", file=sys.stderr)
        return 2
    return 0


def cmd_instagram(args) -> int:
    from app.publishing.queue import PublishQueue, QueueError
    settings, db = bootstrap(args.config)
    pq = PublishQueue(settings, db)
    if args.action == "refresh-token":
        try:
            print(pq.refresh_token(force=True))
        except QueueError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        return 0
    st = pq.check_connection()
    for step in st.get("steps", []):
        print(f"{'✓' if step['ok'] else '✕'} {step['name']}: {step['detail']}")
    print("Instagram подключён" if st.get("ok") else "Instagram не подключён")
    return 0 if st.get("ok") else 1


PIPER_VOICES = ("ru_RU-irina-medium", "ru_RU-denis-medium", "ru_RU-dmitri-medium", "ru_RU-ruslan-medium")


def cmd_download_models(args) -> int:
    """First start on a new computer (or in Docker): local AI model (~4.7 GB), speech recognition (~0.5 GB),
    Russian voices (~0.25 GB). Already downloaded files are skipped."""
    settings, _ = bootstrap(args.config)
    ok = True
    voices = settings.data("voices")
    voices.mkdir(parents=True, exist_ok=True)
    missing = [v for v in PIPER_VOICES if not (voices / f"{v}.onnx").is_file()]
    if missing:
        print(f"Голоса озвучки: {', '.join(missing)} …")
        import subprocess
        r = subprocess.run([sys.executable, "-m", "piper.download_voices", "--data-dir", str(voices), *missing])
        ok &= r.returncode == 0
    print("Голоса озвучки: готово" if ok else "Голоса озвучки: ошибка")
    if settings.get("transcription.enabled", True):
        print(f"Распознавание речи ({settings.get('transcription.model', 'small')}) …")
        try:
            from app.transcription.whisper import WhisperTranscriber
            WhisperTranscriber(settings.section("transcription"))._load()
            print("Распознавание речи: готово")
        except Exception as exc:
            ok = False
            print(f"Распознавание речи: ошибка: {exc}")
    if settings.get("ai.provider") == "llamacpp":
        print("Локальная нейросеть (Qwen 7B, ~4.7 ГБ, это долго) …")
        try:
            from app.ai.llamacpp import LlamaCppProvider
            LlamaCppProvider(settings.section("ai").get("llamacpp", {}))._resolve_path(download=True)
            print("Локальная нейросеть: готово")
        except Exception as exc:
            ok = False
            print(f"Локальная нейросеть: ошибка: {exc}")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Контент-завод")
    p.add_argument("--config", help="путь к config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="полный цикл: discover → download → analyze → process")
    run.add_argument("--once", action="store_true", help="один цикл и выход")
    run.add_argument("--dry-run", action="store_true", help="показать план, ничего не меняя")
    run.add_argument("--limit", type=int, help="максимум материалов на этап")
    run.set_defaults(func=cmd_run)

    for name, help_text in (("discover", "найти новые материалы"), ("download", "скачать найденное"),
                            ("analyze", "проверить фильтры и дубли"), ("process", "сделать 9:16 MP4")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--limit", type=int)
        sp.set_defaults(func=_stage(name))

    st = sub.add_parser("status", help="состояние базы")
    st.add_argument("--limit", type=int)
    st.set_defaults(func=cmd_status)

    rv = sub.add_parser("review", help="ручная проверка: approve / reject / skip")
    rv.add_argument("--list", action="store_true", help="только показать очередь")
    rv.add_argument("--approve", type=int, metavar="ID")
    rv.add_argument("--reject", type=int, metavar="ID")
    rv.add_argument("--note", help="комментарий к решению")
    rv.set_defaults(func=cmd_review)

    sub.add_parser("health", help="проверка окружения").set_defaults(func=cmd_health)

    tv = sub.add_parser("make-test-video", help="сгенерировать тестовый ролик в data/inbox")
    tv.add_argument("--duration", type=float, default=8.0)
    tv.set_defaults(func=cmd_make_test_video)

    sm = sub.add_parser("make-samples", help="10 тестовых роликов (5 хороших, 5 плохих) в data/inbox/samples")
    sm.set_defaults(func=cmd_make_samples)

    web = sub.add_parser("web", help="веб-интерфейс на http://127.0.0.1:8765 (с фоновым воркером)")
    web.add_argument("--port", type=int)
    web.set_defaults(func=cmd_web)

    sub.add_parser("cleanup", help="очистить временные файлы").set_defaults(func=cmd_cleanup)
    sub.add_parser("download-models", help="скачать модели: нейросеть, распознавание речи, голоса")\
        .set_defaults(func=cmd_download_models)

    pb = sub.add_parser("publish", help="очередь публикации Instagram Reels")
    pb.add_argument("--dry-run", action="store_true", help="что, куда, когда и с каким описанием; ничего не отправлять")
    pb.set_defaults(func=cmd_publish)

    ig = sub.add_parser("instagram", help="подключение Instagram: check | refresh-token")
    ig.add_argument("action", choices=["check", "refresh-token"])
    ig.set_defaults(func=cmd_instagram)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
