from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app.config.settings import Settings
from app.database import repositories as repo
from app.database.database import Database
from app.database.models import (
    PUBLISHABLE_RIGHTS, TRANSIENT_ROLLBACK, ContentItem, RightsStatus, Status, Variant, utcnow,
)
from app.downloader.manager import DownloadManager, Downloaded
from app.ai.analyzer import Analyzer
from app.ai.base import AnalysisContext
from app.downloader.validators import check_filters
from app.logging_setup import log_event
from app.media import ffmpeg, subtitles
from app.media.ffprobe import probe
from app.media.metrics import measure
from app.processing.review import review_link
from app.processing.storage import disk_usage_percent
from app.scoring import moderation
from app.scoring.decision import decide
from app.scoring.duplicate import phash_distance, video_phash
from app.scoring.quality import quality_gate, quality_score
from app.scoring.virality import (
    RIGHTS_RISK, content_score, format_score, freshness_score, originality_from_duplicates,
)
from app.sources.base import SourceConnector
from app.sources.registry import build_sources

log = logging.getLogger("pipeline")
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ogv"}


def display_name(source: str, source_id: str, suffix: str) -> str:
    if source == "filesystem":
        return Path(source_id).name
    return source_id.replace(":", "_").replace("/", "_") + suffix


def video_suffix(ref: str) -> str:
    suffix = Path(ref.split("?", 1)[0]).suffix.lower()
    return suffix if suffix in VIDEO_SUFFIXES else ".mp4"


@dataclass
class RunReport:
    discovered: int = 0
    downloaded: int = 0
    duplicates: int = 0
    rejected: int = 0
    approved: int = 0
    review: int = 0
    processed: int = 0
    failed: int = 0
    retrying: int = 0
    recovered: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"найдено {self.discovered}, скачано {self.downloaded}, дублей {self.duplicates}, "
                f"отклонено {self.rejected}, на проверку {self.review}, одобрено {self.approved}, обработано {self.processed}, "
                f"ошибок {self.failed}, отложено на повтор {self.retrying}, восстановлено {self.recovered}")


AUTO_RIGHTS = (RightsStatus.UNKNOWN, RightsStatus.REQUIRES_REVIEW)


def auto_approve_enabled(settings: Settings) -> bool:
    return bool(settings.get("rights.auto_approve", False))


def approve_waiting(db: Database) -> int:
    """Owner's decision: nothing waits for a manual check. Waiting items are approved (they go on to
    rendering); rendered ones with unchecked rights become ready."""
    n = 0
    with db.session() as s:
        for item in s.query(ContentItem).filter(ContentItem.status.in_([Status.MANUAL_REVIEW, Status.PROCESSED])):
            if item.rights_status == RightsStatus.REJECTED:
                continue
            if item.rights_status in AUTO_RIGHTS:
                item.rights_status = RightsStatus.AUTO_APPROVED
            if item.status == Status.MANUAL_REVIEW:
                item.status = Status.APPROVED
                item.decision_reason = f"одобрен автоматически (было: {item.decision_reason or 'ручная проверка'})"
            elif item.rights_status in PUBLISHABLE_RIGHTS:
                item.status = Status.READY_TO_PUBLISH
            n += 1
    return n


class Pipeline:
    def __init__(self, settings: Settings, db: Database, sources: dict[str, SourceConnector] | None = None):
        self.s = settings
        self.db = db
        self.sources = sources if sources is not None else build_sources(settings, db)
        self.max_retries = int(settings.get("factory.max_retries", 3))
        self.retry_delay = int(settings.get("factory.retry_delay_minutes", 15))
        self.downloader = DownloadManager(settings)
        self.analyzer = Analyzer(settings)
        self._transcriber = None
        self.auto_approve = auto_approve_enabled(settings)

    @property
    def transcriber(self):
        if self._transcriber is None:
            from app.transcription.whisper import WhisperTranscriber
            self._transcriber = WhisperTranscriber(self.s.section("transcription"))
        return self._transcriber

    # ---------- recovery ----------
    def recover(self, report: RunReport) -> None:
        """Items left in a transient state by a crash go back to where they started."""
        if self.auto_approve:
            approve_waiting(self.db)
        with self.db.session() as s:
            for item in repo.stuck(s):
                if item.status == Status.PROCESSING and item.pending_output:
                    Path(item.pending_output).unlink(missing_ok=True)
                back = TRANSIENT_ROLLBACK[item.status]
                log_event(log, "RECOVER", "SUCCESS", item.id, **{"from": item.status.value, "to": back.value})
                item.status = back
                item.pending_output = None
                report.recovered += 1
        for leftover in self.s.data("processing").glob("*.part.mp4"):
            leftover.unlink(missing_ok=True)

    # ---------- discover ----------
    def discover(self, limit: int, report: RunReport) -> None:
        for name, src in self.sources.items():
            ready, why = src.configured()
            if not ready:
                report.notes.append(f"источник {name}: {why}")
                continue
            try:
                with self.db.session() as s:
                    known = repo.known_source_ids(s, name)
                found = src.discover(known, limit - report.discovered)
            except Exception as exc:
                log_event(log, "DISCOVER", "FAILED", source=name, error=str(exc))
                report.notes.append(f"источник {name}: ошибка {exc}")
                continue
            self.store_discovered(found)
            for d in found:
                log_event(log, "DISCOVER", "SUCCESS", source=name, source_id=d.source_id)
            report.discovered += len(found)
            if report.discovered >= limit:
                break

    def store_discovered(self, found: list) -> list[int]:
        """Inserts discovered items in one transaction; returns ids of the ones to download."""
        rows = []
        with self.db.session() as s:
            for d in found:
                if repo.exists(s, d.source, d.source_id):
                    continue
                meta = {k: v for k, v in d.extra.items() if k != "source_score"}
                row = ContentItem(
                    source=d.source, source_id=d.source_id, source_url=d.source_url, title=d.title,
                    author=d.author, channel=d.channel, category=d.category, caption=d.caption,
                    published_at=d.published_at, license=d.license, license_url=d.license_url,
                    attribution=d.attribution, media_url=d.media_url, duration=d.duration,
                    source_score=d.extra.get("source_score"),
                    source_metadata=json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
                    rights_status=RightsStatus(d.rights_status),
                    status=Status.DISCOVERY_ONLY if d.discovery_only else Status.DISCOVERED,
                    decision_reason="источник только для обнаружения: ссылка и метаданные сохранены"
                    if d.discovery_only else None,
                )
                s.add(row)
                rows.append(row)
            s.flush()
            return [r.id for r in rows if r.status == Status.DISCOVERED]

    # ---------- download ----------
    def disk_full(self) -> str | None:
        stop = float(self.s.get("storage.stop_disk_percent", 90))
        usage = disk_usage_percent(self.s.data_dir)
        if usage >= stop:
            return f"диск заполнен на {usage:.0f}% (порог {stop:.0f}%), новые загрузки остановлены"
        return None

    def download(self, limit: int, report: RunReport) -> None:
        msg = self.disk_full()
        if msg:
            log_event(log, "DOWNLOAD", "SKIPPED", error=msg)
            report.notes.append(msg)
            return
        with self.db.session() as s:
            ids = [i.id for i in repo.due(s, Status.DISCOVERED, limit)]
        for item_id in ids:
            self.download_one(item_id, report)

    def download_one(self, item_id: int, report: RunReport) -> str:
        """Thread-safe: every step uses its own short transaction."""
        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            item.status = Status.DOWNLOADING
            source, source_id, media_url = item.source, item.source_id, item.media_url
            suffix = video_suffix(media_url or source_id)

        connector = self.sources.get(source)
        try:
            if connector is None:
                raise RuntimeError(f"источник {source} не подключён")
            with self.db.session() as s:
                result = self.downloader.fetch(
                    connector, source_id, suffix, media_url,
                    lambda sha: repo.by_sha256(s, sha, exclude_id=item_id) is not None,
                )
        except Exception as exc:
            with self.db.session() as s:
                item = s.get(ContentItem, item_id)
                repo.register_failure(item, Status.DISCOVERED, str(exc), self.max_retries, self.retry_delay)
                final = item.status
            log_event(log, "DOWNLOAD", "FAILED" if final == Status.FAILED else "RETRY", item_id,
                      source=source, error=str(exc))
            report.failed += final == Status.FAILED
            report.retrying += final != Status.FAILED
            return final.value

        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            if isinstance(result, str):
                original = repo.by_sha256(s, result, exclude_id=item_id)
                item.sha256 = result
                item.original_filename = display_name(source, source_id, suffix)
                item.status = Status.DUPLICATE
                item.duplicate_of_id = original.id if original else None
                item.duplicate_score = 100.0
                item.rejection_reason = f"точная копия #{item.duplicate_of_id} (SHA-256)"
                report.duplicates += 1
                status = "DUPLICATE"
            else:
                self._apply_download(item, result)
                report.downloaded += 1
                status = "SUCCESS"
        log_event(log, "DOWNLOAD", status, item_id, source=source)
        return status

    @staticmethod
    def _apply_download(item: ContentItem, d: Downloaded) -> None:
        i = d.info
        item.original_file = str(d.path)
        item.original_filename = display_name(item.source, item.source_id, d.path.suffix)
        item.sha256 = d.sha256
        item.duration, item.width, item.height, item.fps = i.duration, i.width, i.height, i.fps
        item.codec, item.bitrate, item.filesize, item.has_audio = i.codec, i.bitrate, i.filesize, i.has_audio
        item.subtitle_file = str(d.subtitle_path) if d.subtitle_path else None
        item.has_subtitles = bool(d.subtitle_path) or i.has_subtitle_stream
        item.downloaded_at = utcnow()
        item.status = Status.DOWNLOADED
        repo.clear_errors(item)

    # ---------- analyze ----------
    def analyze(self, limit: int, report: RunReport) -> None:
        with self.db.session() as s:
            ids = [i.id for i in repo.due(s, Status.DOWNLOADED, limit)]
        for item_id in ids:
            with self.db.session() as s:
                item = s.get(ContentItem, item_id)
                item.status = Status.ANALYZING
            try:
                outcome = self._analyze_one(item_id)
            except Exception as exc:
                with self.db.session() as s:
                    item = s.get(ContentItem, item_id)
                    repo.register_failure(item, Status.DOWNLOADED, str(exc), self.max_retries, self.retry_delay)
                    final = item.status
                log_event(log, "ANALYZE", "FAILED" if final == Status.FAILED else "RETRY", item_id, error=str(exc))
                report.failed += final == Status.FAILED
                report.retrying += final != Status.FAILED
                continue
            status, reason, extra = outcome
            if status == Status.REJECTED:
                report.rejected += 1
            elif status == Status.DUPLICATE:
                report.duplicates += 1
            elif status == Status.MANUAL_REVIEW:
                report.review += 1
            else:
                report.approved += 1
            log_event(log, "ANALYZE", status.value, item_id, reason=reason, **extra)

    def _analyze_one(self, item_id: int) -> tuple[Status, str | None, dict]:
        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            path = Path(item.original_file)
            ctx_base = dict(title=item.title, caption=item.caption, source=item.source, channel=item.channel,
                            source_category=item.category, rights_status=item.rights_status.value)
            rights, published_at, source_score = item.rights_status, item.published_at, item.source_score
            sub_file = item.subtitle_file
            # A folder category is the owner's explicit choice; a channel category is only a hint.
            fixed_category = (item.category if item.source == "filesystem" and item.category not in (None, "other")
                              else None)
            hint = item.category if item.source != "filesystem" and item.category not in (None, "other") else None
            manual_category = item.category_source == "manual"

        info = probe(path)
        reason = check_filters(info, self.s.section("filters"))
        if reason:
            return self._finish(item_id, Status.REJECTED, reason, {"rule": "filters", "reason": reason})

        dup = self._duplicate_check(item_id, path, info.duration)
        if dup:
            return dup

        metrics = measure(path, info.duration, info.has_audio)
        quality, quality_parts = quality_score(info, metrics)
        gate = quality_gate(metrics, self.s.get("scores.quality_gates", {}) or {})

        transcript = None
        if gate is None and info.has_audio and self.s.get("transcription.enabled", True):
            transcript = self.transcriber.transcribe(path)

        ctx = AnalysisContext(
            **ctx_base, duration=info.duration, width=info.width, height=info.height, has_audio=info.has_audio,
            transcript=transcript.text if transcript else None,
            transcript_language=transcript.language if transcript else None,
            quality_score=quality, quality_parts=quality_parts, metrics=metrics.as_dict(),
            category_hint=hint,
        )
        ai = self.analyzer.analyze(ctx) if gate is None else None

        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            dup_score = item.duplicate_score
        flags, ai_flags = set(), set()
        if self.s.get("moderation.enabled", True):
            flags = moderation.keyword_flags(ctx.title, ctx.caption, ctx.transcript)
            if ai:
                ai_flags = set(ai.moderation_flags)
        originality = originality_from_duplicates(dup_score)
        if ai and ai.originality_score is not None:
            originality = min(originality, ai.originality_score)
        rights_risk = float(RIGHTS_RISK.get(rights, 70))
        if ai and ai.rights_risk_score is not None:
            rights_risk = max(rights_risk, ai.rights_risk_score)
        scores = {
            "viral": ai.viral_score if ai else 0.0,
            "quality": quality,
            "originality": originality,
            "source": float(source_score if source_score is not None else 50),
            "freshness": freshness_score(published_at),
            "format": format_score(info.width, info.height),
        }
        weights = self.s.get("scores.weights", {}) or {}
        content = content_score(scores, weights)
        decision = decide(
            quality=quality, gate_reason=gate, viral=scores["viral"], content=content,
            provider=ai.provider if ai else "none", flags=flags, ai_flags=ai_flags,
            ai_action=ai.recommended_action if ai else "approve", rights=rights,
            thresholds=self.s.section("scores"), auto_approve=self.auto_approve,
        )
        record = {
            "decision": {"status": decision.status.value, "rule": decision.rule, "reason": decision.reason},
            "scores": scores, "content_score": content, "weights": weights,
            "thresholds": {k: v for k, v in self.s.section("scores").items() if k.startswith("min_")},
            "quality_parts": quality_parts, "quality_gate": gate, "metrics": metrics.as_dict(),
            "rights": {"status": rights.value, "risk": rights_risk},
            "moderation_flags": sorted(flags), "ai_moderation_flags": sorted(ai_flags),
            "ai": asdict(ai) if ai else None,
            "transcribed": transcript is not None,
        }

        srt_path = None
        if (transcript and transcript.segments and not sub_file
                and self.s.get("subtitles.auto_from_transcript", True)):
            srt_path = path.with_suffix(".srt")
            subtitles.write_srt([subtitles.Cue(sg.start, sg.end, sg.text) for sg in transcript.segments], srt_path)

        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            if self.auto_approve and decision.status == Status.APPROVED and item.rights_status in AUTO_RIGHTS:
                item.rights_status = RightsStatus.AUTO_APPROVED
            item.quality_score, item.viral_score = quality, scores["viral"]
            item.originality_score, item.rights_risk_score = originality, rights_risk
            item.freshness_score, item.format_score = scores["freshness"], scores["format"]
            item.source_score, item.content_score = scores["source"], content
            item.moderation_flags = ",".join(sorted(flags | ai_flags)) or None
            if transcript:
                item.transcript = transcript.text or None
                item.transcript_language = transcript.language
                item.transcript_segments = json.dumps(transcript.segments_json(), ensure_ascii=False)
                item.language = transcript.language
            if srt_path:
                item.subtitle_file, item.has_subtitles = str(srt_path), True
            if ai:
                item.ai_provider = ai.provider
                item.subcategory, item.summary, item.hook = ai.subcategory, ai.summary, ai.hook
            if manual_category:
                pass
            elif fixed_category:
                item.category, item.category_confidence = fixed_category, 100.0
                item.category_reason, item.category_source = "категория папки-источника", "folder"
            elif ai:
                item.category, item.category_confidence = ai.category, ai.category_confidence
                item.category_reason = ai.category_reason
                item.category_source = "rules" if ai.provider == "rules" else "ai"
        return self._finish(item_id, decision.status, decision.reason, record)

    def _duplicate_check(self, item_id: int, path: Path, duration: float):
        frames = int(self.s.get("duplicates.phash_frames", 5))
        max_dist = float(self.s.get("duplicates.phash_max_distance", 10))
        ph = video_phash(path, duration, frames)
        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            item.phash = ph
            match, dist = None, 64.0
            for other in repo.with_phash(s, item_id):
                d = phash_distance(ph, other.phash)
                if d < dist:
                    match, dist = other, d
            item.duplicate_score = round(100 * (1 - dist / 64), 1)
            if match is None or dist > max_dist:
                return None
            item.duplicate_of_id = match.id
            reason = f"визуально совпадает с #{match.id} (pHash, расстояние {dist:.1f})"
            if not self.s.get("duplicates.visual_reject", True):
                return None  # only a mark (duplicate_of_id); the owner decides himself
        return self._finish(item_id, Status.DUPLICATE, reason, {"rule": "phash", "reason": reason})

    def _finish(self, item_id: int, status: Status, reason: str, record: dict) -> tuple[Status, str, dict]:
        with self.db.session() as s:
            item = s.get(ContentItem, item_id)
            item.status = status
            item.decision_reason = reason
            item.rejection_reason = reason if status in (Status.REJECTED, Status.DUPLICATE) else None
            item.analysis_json = json.dumps(record, ensure_ascii=False, default=str)
            repo.clear_errors(item)
            name = item.original_filename
            if status == Status.MANUAL_REVIEW:
                review_link(self.s, item)
        extra = {"content_score": record.get("content_score"), "file": name}
        return status, reason, extra

    # ---------- process ----------
    def process(self, limit: int, report: RunReport) -> None:
        pcfg, scfg = self.s.section("processing"), self.s.section("subtitles")
        final_dir, proc_dir = self.s.data("final"), self.s.data("processing")
        thumbs_dir = self.s.data("thumbnails")
        with self.db.session() as s:
            ids = [i.id for i in repo.due(s, Status.APPROVED, limit)]
        for item_id in ids:
            with self.db.session() as s:
                item = s.get(ContentItem, item_id)
                stem = self._final_stem(item)
                tmp_out = proc_dir / f"{stem}.part.mp4"
                item.status = Status.PROCESSING
                item.pending_output = str(tmp_out)
                src, sub_file = Path(item.original_file), item.subtitle_file
            workdir = self.s.data("temp") / f"job_{item_id}"
            try:
                workdir.mkdir(parents=True, exist_ok=True)
                info = probe(src)
                cues = None
                if scfg.get("enabled", True) and sub_file and Path(sub_file).is_file():
                    cues = subtitles.load_srt(Path(sub_file)) or None
                result = ffmpeg.render_vertical(src, tmp_out, info, pcfg, scfg, cues, workdir)
                out_info = self._verify(tmp_out, pcfg)

                final_mp4 = final_dir / f"{stem}.mp4"
                thumb = thumbs_dir / f"{stem}.jpg"
                ffmpeg.thumbnail(tmp_out, thumb, min(1.0, out_info.duration / 2), int(pcfg.get("thumbnail_width", 540)))
                shutil.move(str(tmp_out), final_mp4)
                srt_copy = None
                if cues:
                    srt_copy = final_dir / f"{stem}.srt"
                    subtitles.write_srt(cues, srt_copy)

                with self.db.session() as s:
                    item = s.get(ContentItem, item_id)
                    meta_path = final_dir / f"{stem}.json"
                    meta_path.write_text(json.dumps(self._metadata(item, out_info, result.profile, final_mp4, thumb),
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
                    variant = s.query(Variant).filter_by(content_id=item_id, profile=result.profile).one_or_none()
                    if variant is None:
                        variant = Variant(content_id=item_id, profile=result.profile, path="")
                        s.add(variant)
                    variant.path, variant.thumbnail, variant.metadata_path = str(final_mp4), str(thumb), str(meta_path)
                    variant.width, variant.height, variant.duration = out_info.width, out_info.height, out_info.duration
                    variant.has_audio, variant.burned_subtitles = out_info.has_audio, result.burned_subtitles
                    item.pending_output = None
                    item.status = (Status.READY_TO_PUBLISH if item.rights_status in PUBLISHABLE_RIGHTS
                                   else Status.PROCESSED)
                    repo.clear_errors(item)
                    final_status = item.status.value
                report.processed += 1
                log_event(log, "PROCESS", "SUCCESS", item_id, profile=result.profile, output=final_mp4.name,
                          next=final_status)
            except Exception as exc:
                tmp_out.unlink(missing_ok=True)
                with self.db.session() as s:
                    item = s.get(ContentItem, item_id)
                    item.pending_output = None
                    repo.register_failure(item, Status.APPROVED, str(exc), self.max_retries, self.retry_delay)
                    final = item.status
                log_event(log, "PROCESS", "FAILED" if final == Status.FAILED else "RETRY", item_id, error=str(exc))
                report.failed += final == Status.FAILED
                report.retrying += final != Status.FAILED
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _final_stem(item: ContentItem) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        category = (item.category or "other").replace(" ", "_")
        return f"{stamp}_{category}_{int(item.viral_score or 0)}_{item.id}"

    @staticmethod
    def _verify(path: Path, pcfg: dict):
        info = probe(path)
        w, h = int(pcfg["width"]), int(pcfg["height"])
        if (info.width, info.height) != (w, h):
            raise RuntimeError(f"итоговый файл {info.width}x{info.height}, ожидалось {w}x{h}")
        if info.codec != "h264":
            raise RuntimeError(f"итоговый кодек {info.codec}, ожидался h264")
        if info.duration <= 0:
            raise RuntimeError("итоговый файл пустой")
        return info

    @staticmethod
    def _metadata(item: ContentItem, info, profile: str, video: Path, thumb: Path) -> dict:
        return {
            "id": item.id,
            "category": item.category,
            "viral_score": item.viral_score,
            "source": item.source,
            "source_url": item.source_url,
            "original_filename": item.original_filename,
            "duration": info.duration,
            "width": info.width,
            "height": info.height,
            "has_audio": info.has_audio,
            "rights_status": item.rights_status.value,
            "license": item.license,
            "processed_profile": profile,
            "video": video.name,
            "thumbnail": thumb.name,
            "sha256": item.sha256,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

    # ---------- full cycle ----------
    def run_once(self, limit: int | None = None) -> RunReport:
        limit = limit or int(self.s.get("factory.max_items_per_run", 100))
        report = RunReport()
        self.recover(report)
        for stage in (self.discover, self.download, self.analyze, self.process):
            try:
                stage(limit, report)
            except Exception as exc:  # a broken stage must not kill the factory
                log.exception("stage %s crashed", stage.__name__)
                report.notes.append(f"этап {stage.__name__} упал: {exc}")
        for src in self.sources.values():
            try:
                src.close()
            except Exception:
                log.exception("closing source %s", src.name)
        log_event(log, "RUN", "SUCCESS", summary=report.summary().replace(" ", "_"))
        return report
