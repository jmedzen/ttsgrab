"""Database models. One schema, works on SQLite and PostgreSQL.

Cost is stored as integer micro-USD (1 µUSD = 1e-6 USD) so monthly sums never drift.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}  # noqa: RUF012 - SQLAlchemy reads this


class SynthRecord(Base):
    """History: one row per synthesis attempt that produced audio."""

    __tablename__ = "synth_history"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    text: Mapped[str] = mapped_column(Text)
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    voice_id: Mapped[str] = mapped_column(String(128))
    requested_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    format: Mapped[str] = mapped_column(String(8), default="mp3")
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_byte_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    unsupported: Mapped[list[str]] = mapped_column(JSON, default=list)
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    billed_chars: Mapped[int] = mapped_column(Integer, default=0)
    used_free_tier: Mapped[bool] = mapped_column(Boolean, default=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    client: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sentence-level timing for the split path, stored instead of rendered subtitles so
    # SRT and WebVTT (or anything added later) are derived on read rather than frozen at
    # synthesis time.
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    __table_args__ = (Index("ix_history_created_provider", "created_at", "provider"),)


class ApiKey(Base):
    """Bearer tokens issued by the gateway. The key itself is never stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    # sha256(token) — the plaintext token is shown once at creation and never persisted.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))  # e.g. "ttsg_ab12" for display
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)  # empty = all
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_char_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_cost_quota_micro_usd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class UsageEvent(Base):
    """Metering ledger. Every synthesis writes one row so monthly rollups are cheap."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), index=True)
    voice_id: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    characters: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    free_tier: Mapped[bool] = mapped_column(Boolean, default=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("ix_usage_month_provider", "created_at", "provider"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(64))  # api_key:<id> | ui | system
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    webhook_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    webhook_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class IdempotencyRecord(Base):
    """What an Idempotency-Key resolved to, so a retry returns the first result.

    Network retries are normal; paying twice for one sentence is not. The row stores the
    history id rather than the audio: the audio already lives on disk with the history
    record, and copying it here would double every file.

    Scoped by API key id, because an Idempotency-Key is the client's token, not a global
    name — two clients may pick the same UUID without wanting each other's audio.
    """

    __tablename__ = "idempotency"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    api_key_id: Mapped[str] = mapped_column(String(32), primary_key=True, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    # Hash of the request body. A reused key with a *different* body must be rejected
    # rather than silently returning unrelated audio.
    request_hash: Mapped[str] = mapped_column(String(64))
    history_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")


class WorkerNode(Base):
    """A registered local-model worker (Kokoro / GPT-SoVITS / CosyVoice2 / any OpenAI-ish)."""

    __tablename__ = "worker_nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(32), default="tts")  # tts | openai_compat
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Health state
    healthy: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms_ewma: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Per-1M-char notional price used for cost estimates on self-hosted nodes.
    price_micro_usd_per_mchar: Mapped[int] = mapped_column(BigInteger, default=0)
    auth_header: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReferenceAudio(Base):
    """Uploaded voice-cloning reference clips."""

    __tablename__ = "reference_audio"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    __table_args__ = (UniqueConstraint("name", name="uq_reference_name"),)


class ApiKeyUsage(Base):  # pragma: no cover - superseded by UsageEvent rollups
    __tablename__ = "api_key_usage_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[str] = mapped_column(String(32), ForeignKey("api_keys.id"))
    day: Mapped[str] = mapped_column(String(10), index=True)
    characters: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class FavouriteVoice(Base):
    """A voice someone starred, stored server-side.

    Deliberately NOT per-browser storage. This gateway is deployed by one person or a small
    team against a shared instance; keeping favourites in localStorage means they vanish
    when you switch from laptop to phone, which is the opposite of what "favourite" implies.
    The table is small (tens of rows), has no secrets, and is shared by everyone who can
    reach the UI — the same model as the voice catalogue itself.

    Ordering is explicit rather than by timestamp so a user can arrange the row.
    """

    __tablename__ = "favourite_voices"

    voice_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Denormalised so the favourites list still renders when a provider is disabled and its
    # voices drop out of /v1/voices. Without this an unavailable favourite would be an
    # anonymous id in the UI.
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
