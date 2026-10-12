"""Background worker of the web server.

Adding a channel only queues a scan (the UI answers at once). Channel scans run in parallel (bounded,
each with its own pause between page requests), new videos are downloaded in parallel, and one
thread runs analysis + rendering. Rechecks fetch only posts newer than the last one seen.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from app.database import repositories as repo
from app.database.models import Channel, ContentItem, Status, utcnow
from app.editor.service import Editor
from app.processing.pipeline import Pipeline, RunReport
from app.publishing.queue import PublishQueue
from app.sources.registry import build_sources
from app.sources.schedule import ParseCalendar
from app.sources.telegram import ChannelSpec, TelegramBlocked, TelegramSource

log = logging.getLogger("worker")


class FactoryWorker:
    def __init__(self, settings, db, telegram: TelegramSource | None = None,
                 publisher: PublishQueue | None = None):
        self.s, self.db = settings, db
        self.publisher = publisher or PublishQueue(settings, db)
        self.editor = Editor(settings, db)
        self.exporting: int | None = None
        self.publish_interval = float(settings.get("publishing.check_interval_seconds", 30))
        self.last_publish_tick: str | None = None
        cfg = settings.get("sources.telegram", {}) or {}
        self.telegram = telegram or TelegramSource(settings, db=db)
        sources = build_sources(settings, db)
        sources["telegram"] = self.telegram
        self.pipeline = Pipeline(settings, db, sources)
        self.scan_pool = ThreadPoolExecutor(max_workers=int(cfg.get("parallel_channels", 3)), thread_name_prefix="scan")
        self.download_pool = ThreadPoolExecutor(max_workers=int(cfg.get("parallel_downloads", 3)),
                                                thread_name_prefix="download")
        self.calendar = ParseCalendar(settings, db)
        self.last_scheduled_run: str | None = None
        self.last_maintenance: dict | None = None
        self.block_pause = timedelta(minutes=float(cfg.get("block_pause_minutes", 15)))
        self._lock = threading.Lock()
        self._queued: set[int] = set()
        self._scanning: set[int] = set()
        self._downloading: set[int] = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.processing = False
        self.blocked_until = None
        self.last_process_report: str | None = None
        self._threads: list[threading.Thread] = []

    # ---------- lifecycle ----------
    def start(self) -> None:
        self.pipeline.recover(RunReport())
        self.publisher.recover()
        self.editor.recover()
        self.editor.ensure_builtin_templates()
        self.calendar.ensure_default()
        with self.db.session() as s:  # states left over from a previous server run
            for ch in s.scalars(select(Channel).where(Channel.state.in_(["queued", "scanning"]))):
                ch.state = "new"
        for target in (self._process_loop, self._schedule_loop, self._publish_loop, self._export_loop,
                       self._maintenance_loop, self._screen_text_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)
        self.enqueue_pending_downloads()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.scan_pool.shutdown(wait=False, cancel_futures=True)
        self.download_pool.shutdown(wait=False, cancel_futures=True)

    # ---------- scans ----------
    def enqueue_channel(self, channel_id: int) -> bool:
        with self._lock:
            if channel_id in self._queued or channel_id in self._scanning:
                return False
            self._queued.add(channel_id)
        with self.db.session() as s:
            ch = s.get(Channel, channel_id)
            if ch is None:
                with self._lock:
                    self._queued.discard(channel_id)
                return False
            ch.state = "queued"
        self.scan_pool.submit(self._scan_job, channel_id)
        return True

    def enqueue_all(self) -> int:
        with self.db.session() as s:
            ids = list(s.scalars(select(Channel.id).where(Channel.enabled.is_(True))))
        return sum(self.enqueue_channel(i) for i in ids)

    def _scan_job(self, channel_id: int) -> None:
        with self._lock:
            self._queued.discard(channel_id)
            self._scanning.add(channel_id)
        try:
            with self.db.session() as s:
                ch = s.get(Channel, channel_id)
                if ch is None or not ch.enabled:
                    if ch is not None:
                        ch.state = "ok" if ch.last_checked_at else "new"
                    return
                if self.blocked_until and utcnow() < self.blocked_until:
                    ch.state, ch.last_error = "error", (
                        f"Telegram ограничил запросы, пауза до {self.blocked_until:%H:%M} UTC")
                    return
                ch.state = "scanning"
                spec = ChannelSpec(ch.id, ch.username, ch.category, ch.mode, ch.last_seen_post_id,
                                   self.telegram.cfg.get("source_score", 50), ch.via or "web")
                known = repo.known_source_ids(s, "telegram")
            try:
                result = self.telegram.scan(spec, known)
            except TelegramBlocked as exc:
                self.blocked_until = utcnow() + self.block_pause
                self.telegram.record(channel_id, error=str(exc))
                log.warning("TELEGRAM blocked, pausing until %s: %s", self.blocked_until, exc)
                return
            except Exception as exc:
                self.telegram.record(channel_id, error=str(exc))
                log.warning("TELEGRAM scan %s failed: %s", spec.username, exc)
                return
            new_ids = self.pipeline.store_discovered(result.items)
            self.telegram.record(channel_id, result=result)
            log.info("TELEGRAM scan %s: %d new, %d skipped", spec.username, len(result.items), len(result.skipped))
            for item_id in new_ids:
                self.enqueue_download(item_id)
        finally:
            with self._lock:
                self._scanning.discard(channel_id)

    # ---------- downloads ----------
    def enqueue_download(self, item_id: int) -> None:
        with self._lock:
            if item_id in self._downloading:
                return
            self._downloading.add(item_id)
        self.download_pool.submit(self._download_job, item_id)

    def enqueue_pending_downloads(self) -> None:
        with self.db.session() as s:
            ids = [i.id for i in repo.due(s, Status.DISCOVERED, 1000)]
        for i in ids:
            self.enqueue_download(i)

    def _download_job(self, item_id: int) -> None:
        try:
            if self.pipeline.disk_full():
                return
            self.pipeline.download_one(item_id, RunReport())
        except Exception:
            log.exception("download %s crashed", item_id)
        finally:
            with self._lock:
                self._downloading.discard(item_id)
            self._wake.set()

    # ---------- analysis + rendering (one at a time: CPU heavy) ----------
    def _process_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=60)
            self._wake.clear()
            if self._stop.is_set():
                return
            report = RunReport()
            self.processing = True
            try:
                self.pipeline.analyze(10, report)
                # owner's decision: nothing is rendered until he picks it in the editor (export renders from the source)
                if self.s.get("processing.auto_render", True):
                    self.pipeline.process(10, report)
            except Exception:
                log.exception("processing loop")
            finally:
                self.processing = False
            try:
                self.editor.apply_auto_templates()
            except Exception:
                log.exception("auto templates")
            if report.approved or report.processed or report.rejected or report.review or report.duplicates:
                self.last_process_report = report.summary()
                self._wake.set()  # there may be more

    def run_due_schedules(self, now=None) -> int:
        """Parses the channels whose calendar slot has come. Returns how many scans were queued."""
        queued = 0
        for sid, channel_ids in self.calendar.due(now):
            queued += sum(self.enqueue_channel(c) for c in channel_ids)
            self.calendar.mark_run(sid, now)
            self.last_scheduled_run = utcnow().isoformat() + "Z"
            log.info("SCHEDULE %s: %d channels queued", sid, len(channel_ids))
        return queued

    def _schedule_loop(self) -> None:
        """Parsing by the calendar (checked every 30 s); failed downloads are retried every 30 min."""
        last_pending = time.time()
        while not self._stop.wait(timeout=30):
            try:
                self.run_due_schedules()
            except Exception:
                log.exception("schedule loop")
            if time.time() - last_pending > 1800:
                last_pending = time.time()
                self.enqueue_pending_downloads()

    def _maintenance_loop(self) -> None:
        """Hourly: database backup once a day, old temp/cache files, originals of rejected videos if the disk fills."""
        from app.processing import maintenance
        while not self._stop.wait(timeout=60 if self.last_maintenance is None else 3600):
            try:
                self.last_maintenance = maintenance.run(self.s, self.db)
            except Exception:
                log.exception("maintenance")
                self.last_maintenance = {"error": True}

    def _screen_text_loop(self) -> None:
        """Marks every downloaded video «есть текст в кадре / нет» (macOS text recognition), one at a time."""
        from app.media import screen_text
        if not screen_text.available():
            return
        while not self._stop.wait(timeout=3):
            with self.db.session() as s:
                row = s.scalars(select(ContentItem).where(ContentItem.original_file.is_not(None),
                                                          ContentItem.has_screen_text.is_(None))
                                .order_by(ContentItem.id.desc()).limit(1)).first()
                job = (row.id, row.original_file, row.duration) if row else None
            if job is None:
                self._stop.wait(timeout=60)
                continue
            iid, path, dur = job
            try:
                from pathlib import Path
                has, text = screen_text.detect(Path(path), dur) if Path(path).is_file() else (False, "")
            except Exception as exc:
                log.warning("screen text %s: %s", iid, exc)
                has, text = False, ""
            with self.db.session() as s:
                item = s.get(ContentItem, iid)
                if item:
                    item.has_screen_text, item.screen_text = has, text or None

    def _publish_loop(self) -> None:
        """Publication scheduler: every 30 s sends at most one due Reel (or records a dry-run plan)."""
        last_token_check = 0.0
        while not self._stop.wait(timeout=self.publish_interval):
            try:
                if time.time() - last_token_check > 12 * 3600:
                    last_token_check = time.time()
                    self.publisher.refresh_token_if_due()
                self.last_publish_tick = self.publisher.tick()
            except Exception:
                log.exception("publishing loop")

    def _export_loop(self) -> None:
        """Editor exports (full quality, one at a time)."""
        while not self._stop.wait(timeout=2):
            for item_id in self.editor.pending():
                if self._stop.is_set():
                    return
                self.exporting = item_id
                try:
                    self.editor.export(item_id)
                except Exception:
                    log.exception("export %s", item_id)
                finally:
                    self.exporting = None

    def wake(self) -> None:
        """Something changed (e.g. a review was approved): run analysis/rendering soon."""
        self._wake.set()

    # ---------- status for the UI ----------
    def status(self) -> dict:
        with self._lock:
            return {
                "queued_channels": len(self._queued), "scanning_channels": len(self._scanning),
                "downloads_in_progress": len(self._downloading), "processing": self.processing,
                "blocked_until": self.blocked_until.isoformat() + "Z" if self.blocked_until else None,
                "last_process_report": self.last_process_report,
                "next_scan": self._next_scan(),
                "exporting": self.exporting,
            }

    def _next_scan(self) -> str | None:
        try:
            nxt = self.calendar.next_run()
        except Exception:
            return None
        return nxt.isoformat() + "Z" if nxt else None

    def wait_idle(self, timeout: float = 60) -> bool:
        """For tests and the CLI: wait until queues are empty and processing is done."""
        end = time.time() + timeout
        while time.time() < end:
            st = self.status()
            with self.db.session() as s:
                pending = s.scalar(select(ContentItem.id).where(
                    ContentItem.status.in_([Status.DOWNLOADED, Status.APPROVED, Status.ANALYZING,
                                            Status.PROCESSING])).limit(1))
            if not (st["queued_channels"] or st["scanning_channels"] or st["downloads_in_progress"]
                    or st["processing"] or pending):
                return True
            self._wake.set()
            time.sleep(0.2)
        return False
