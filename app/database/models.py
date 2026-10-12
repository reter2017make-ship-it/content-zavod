from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Status(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    # Source allows only discovery (link + metadata): nothing is downloaded or processed.
    DISCOVERY_ONLY = "DISCOVERY_ONLY"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    ANALYZING = "ANALYZING"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


# Transient states: if the factory dies while an item is in one of these,
# recovery moves it back to the state it started from.
TRANSIENT_ROLLBACK = {
    Status.DOWNLOADING: Status.DISCOVERED,
    Status.ANALYZING: Status.DOWNLOADED,
    Status.PROCESSING: Status.APPROVED,
}


class RightsStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    REJECTED = "REJECTED"
    USER_OWNED = "USER_OWNED"
    LICENSED = "LICENSED"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    CC0 = "CC0"
    CC_BY = "CC_BY"
    CC_BY_SA = "CC_BY_SA"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    # Set only by a person in `review` after checking the source.
    VERIFIED = "VERIFIED"
    # Owner's decision (rights.auto_approve, 2026-10-07): rights are not checked, the owner answers for them.
    AUTO_APPROVED = "AUTO_APPROVED"


PUBLISHABLE_RIGHTS = {
    RightsStatus.USER_OWNED, RightsStatus.LICENSED, RightsStatus.PUBLIC_DOMAIN, RightsStatus.CC0,
    RightsStatus.CC_BY, RightsStatus.CC_BY_SA, RightsStatus.PERMISSION_GRANTED, RightsStatus.VERIFIED,
    RightsStatus.AUTO_APPROVED,
}


def rights_ok(settings, rights) -> bool:
    """Owner's decision (rights.auto_approve): every downloaded video may be edited and sent to «Готовые»."""
    return bool(settings.get("rights.auto_approve", False)) or rights in PUBLISHABLE_RIGHTS


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255))
    channel: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    # «Описание» для публикации: пишет человек (подписи и хэштеги не генерируются).
    post_description: Mapped[str | None] = mapped_column(Text)
    # «Готовые видео»: копия в папке для ручной публикации и отметка, что человек её выложил.
    ready_path: Mapped[str | None] = mapped_column(Text)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    posted_note: Mapped[str | None] = mapped_column(Text)

    original_file: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    subtitle_file: Mapped[str | None] = mapped_column(Text)

    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    codec: Mapped[str | None] = mapped_column(String(32))
    bitrate: Mapped[int | None] = mapped_column(Integer)
    filesize: Mapped[int | None] = mapped_column(Integer)

    has_audio: Mapped[bool | None] = mapped_column(Boolean)
    has_subtitles: Mapped[bool | None] = mapped_column(Boolean)
    language: Mapped[str | None] = mapped_column(String(16))

    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)

    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    phash: Mapped[str | None] = mapped_column(Text)

    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_language: Mapped[str | None] = mapped_column(String(16))
    transcript_segments: Mapped[str | None] = mapped_column(Text)  # JSON list of {start, end, text}

    category_confidence: Mapped[float | None] = mapped_column(Float)
    category_reason: Mapped[str | None] = mapped_column(Text)
    category_source: Mapped[str | None] = mapped_column(String(16))  # ai | rules | folder | manual
    subcategory: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    hook: Mapped[str | None] = mapped_column(Text)

    quality_score: Mapped[float | None] = mapped_column(Float)
    viral_score: Mapped[float | None] = mapped_column(Float)
    originality_score: Mapped[float | None] = mapped_column(Float)
    duplicate_score: Mapped[float | None] = mapped_column(Float)
    rights_risk_score: Mapped[float | None] = mapped_column(Float)
    source_score: Mapped[float | None] = mapped_column(Float)
    freshness_score: Mapped[float | None] = mapped_column(Float)
    format_score: Mapped[float | None] = mapped_column(Float)
    content_score: Mapped[float | None] = mapped_column(Float, index=True)
    moderation_flags: Mapped[str | None] = mapped_column(Text)  # comma-separated
    ai_provider: Mapped[str | None] = mapped_column(String(64))
    # Full record of what the decision was based on: metrics, scores, thresholds, rule that fired.
    analysis_json: Mapped[str | None] = mapped_column(Text)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)

    rights_status: Mapped[RightsStatus] = mapped_column(
        Enum(RightsStatus, native_enum=False, length=32), default=RightsStatus.UNKNOWN
    )
    license: Mapped[str | None] = mapped_column(String(255))
    license_url: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    source_metadata: Mapped[str | None] = mapped_column(Text)  # JSON from the source API (score, views, ...)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("content_items.id"))

    status: Mapped[Status] = mapped_column(
        Enum(Status, native_enum=False, length=32), default=Status.DISCOVERED, index=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    trashed_from: Mapped[str | None] = mapped_column(String(32))  # status before «в корзину» (to restore it)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime)  # the trash empties itself after N days
    # text in the picture of the source (Apple Vision on a few frames): None = not checked yet
    has_screen_text: Mapped[bool | None] = mapped_column(Boolean)
    screen_text: Mapped[str | None] = mapped_column(Text)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Where the current PROCESSING run will write its result; lets recovery adopt a
    # finished file if the factory crashed between rendering and committing.
    pending_output: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    variants: Mapped[list["Variant"]] = relationship(back_populates="content", cascade="all, delete-orphan")


class Variant(Base):
    """A rendered version of one source video (original, blurred, smart_crop, subtitles, ...)."""

    __tablename__ = "variants"
    __table_args__ = (UniqueConstraint("content_id", "profile", name="uq_variant_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    profile: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    thumbnail: Mapped[str | None] = mapped_column(Text)
    metadata_path: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    has_audio: Mapped[bool | None] = mapped_column(Boolean)
    burned_subtitles: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    content: Mapped[ContentItem] = relationship(back_populates="variants")


class Performance(Base):
    """Post-publication stats (filled from stage 4 on); training data for a future ranking model."""

    __tablename__ = "performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    watch_time: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    performance_score: Mapped[float | None] = mapped_column(Float)


class Channel(Base):
    """A followed source channel (added from the web UI). Stats are updated by the background worker."""

    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("platform", "username", name="uq_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), default="telegram")
    username: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), default="other")
    mode: Mapped[str] = mapped_column(String(16), default="import")  # import | discovery
    via: Mapped[str | None] = mapped_column(String(8), default="web")  # web (t.me/s) | api (Telegram API)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # From the signed-in Telegram account's subscriptions («Мои каналы»); enabled = «брать видео»
    in_account: Mapped[bool | None] = mapped_column(Boolean, default=False)
    added_by: Mapped[str | None] = mapped_column(String(16), default="link")  # link | account
    tg_id: Mapped[int | None] = mapped_column(Integer)
    members: Mapped[int | None] = mapped_column(Integer)
    # «Удалить» in the UI: the channel is not parsed and not shown, and a refresh of the account list
    # does not bring it back (the row stays only to remember that)
    hidden: Mapped[bool | None] = mapped_column(Boolean, default=False)

    state: Mapped[str] = mapped_column(String(16), default="new")  # new | queued | scanning | ok | error
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_post_id: Mapped[int | None] = mapped_column(Integer)
    found_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    last_skipped: Mapped[str | None] = mapped_column(Text)  # JSON list of the latest skip reasons

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScanSchedule(Base):
    """When channels are parsed: weekdays + times (local time). channel_id empty = all channels;
    a channel with its own enabled schedule is parsed only by that one."""

    __tablename__ = "scan_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    days: Mapped[str] = mapped_column(String(32), default="0,1,2,3,4,5,6")  # 0 = Monday
    times: Mapped[str] = mapped_column(String(255), default="10:00,19:00")  # HH:MM, comma separated
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicationStatus(str, enum.Enum):
    QUEUED = "QUEUED"              # ждёт своего времени, лимита или паузы
    PROCESSING = "PROCESSING"      # идёт отправка
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"              # ошибка, будет повтор в next_retry_at
    WAITING_USER = "WAITING_USER"  # повторы кончились или нужна правка человека
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"              # остановлена человеком (Instagram через браузер)


ACTIVE_PUBLICATION = (PublicationStatus.QUEUED, PublicationStatus.PROCESSING, PublicationStatus.FAILED,
                      PublicationStatus.WAITING_USER, PublicationStatus.PUBLISHED)


class Publication(Base):
    """One video on one platform. A partial unique index forbids a second live publication of the same video."""

    __tablename__ = "publications"
    __table_args__ = (
        Index("uq_publication_live", "content_id", "platform", unique=True,
              sqlite_where=text("status != 'CANCELLED'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32), default="instagram")
    account: Mapped[str | None] = mapped_column(String(128))
    caption: Mapped[str] = mapped_column(Text, default="")
    video_path: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)  # UTC; пусто = как можно скорее
    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, native_enum=False, length=16), default=PublicationStatus.QUEUED, index=True)
    # A person confirmed the rights for this exact publication.
    rights_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    rights_note: Mapped[str | None] = mapped_column(Text)
    # Instagram through the browser (platform "instagram_web")
    cover_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(32))  # AUTH_REQUIRED, UPLOAD_FAILED, VIDEO_PROCESSING_TIMEOUT…
    failed_at: Mapped[datetime | None] = mapped_column(DateTime)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime)  # «Поделиться» pressed: no blind retry after this

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Idempotency: the container survives retries, so a retry never creates a second post.
    container_id: Mapped[str | None] = mapped_column(String(64))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    external_id: Mapped[str | None] = mapped_column(String(64))
    permalink: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    error: Mapped[str | None] = mapped_column(Text)
    error_http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_request_id: Mapped[str | None] = mapped_column(String(128))
    error_at: Mapped[datetime | None] = mapped_column(DateTime)
    dry_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    dry_run_report: Mapped[str | None] = mapped_column(Text)  # JSON
    log: Mapped[str | None] = mapped_column(Text)  # JSON list of {at, event}

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    content: Mapped[ContentItem] = relationship()


class AppSetting(Base):
    """Runtime switches changed from the UI (publishing pause, real/dry-run mode). Secrets never go here."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EditAsset(Base):
    """A file uploaded to the editor: banner, background, music."""

    __tablename__ = "edit_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # sha256 prefix of the content
    kind: Mapped[str] = mapped_column(String(8))  # image | video | audio
    name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    key_color: Mapped[str | None] = mapped_column(String(9))  # detected chroma key colour, if any
    # «Фоны» library: role = "background", a folder/tag, and how often it went into a finished video
    role: Mapped[str | None] = mapped_column(String(16))
    tag: Mapped[str | None] = mapped_column(String(64))
    use_count: Mapped[int | None] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EditProject(Base):
    """Editor state of one video: the spec and its last export."""

    __tablename__ = "edit_projects"

    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), primary_key=True)
    spec: Mapped[str] = mapped_column(Text)
    template_id: Mapped[int | None] = mapped_column(Integer)
    render_state: Mapped[str | None] = mapped_column(String(16))  # queued | rendering | done | error
    render_error: Mapped[str | None] = mapped_column(Text)
    rendered_spec_hash: Mapped[str | None] = mapped_column(String(64))
    rendered_at: Mapped[datetime | None] = mapped_column(DateTime)
    send_to_ready: Mapped[bool | None] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class EditTemplate(Base):
    __tablename__ = "edit_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(Text)
    builtin: Mapped[str | None] = mapped_column(String(32))  # key of a ready-made template (app/editor/templates.py)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
