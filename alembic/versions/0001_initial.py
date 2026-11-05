"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-05

Generated from ``Base.metadata`` rather than written by hand, so it cannot drift from the
models. ``create_all()`` still runs on boot for a fresh install; this migration is what an
existing deployment uses to upgrade deliberately.

Downgrade drops every table, which is correct for an initial revision and destructive by
design — back up ``data/`` first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("provider_allowlist", sa.JSON(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
        sa.Column("monthly_char_quota", sa.Integer(), nullable=True),
        sa.Column("monthly_cost_quota_micro_usd", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_keys_key_hash"), "api_keys", ["key_hash"], unique=True)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"], unique=False)
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("webhook_url", sa.String(length=1024), nullable=True),
        sa.Column("webhook_status", sa.Integer(), nullable=True),
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_table(
        "reference_audio",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("consent_note", sa.Text(), nullable=True),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_reference_name"),
    )
    op.create_table(
        "synth_history",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("voice_id", sa.String(length=128), nullable=False),
        sa.Column("requested_provider", sa.String(length=64), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("audio_path", sa.String(length=512), nullable=True),
        sa.Column("audio_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("first_byte_ms", sa.Integer(), nullable=True),
        sa.Column("from_cache", sa.Boolean(), nullable=False),
        sa.Column("unsupported", sa.JSON(), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("billed_chars", sa.Integer(), nullable=False),
        sa.Column("used_free_tier", sa.Boolean(), nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
        sa.Column("client", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_history_created_provider", "synth_history", ["created_at", "provider"], unique=False
    )
    op.create_index(
        op.f("ix_synth_history_api_key_id"), "synth_history", ["api_key_id"], unique=False
    )
    op.create_index(
        op.f("ix_synth_history_created_at"), "synth_history", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_synth_history_provider"), "synth_history", ["provider"], unique=False)
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("voice_id", sa.String(length=128), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=True),
        sa.Column("characters", sa.Integer(), nullable=False),
        sa.Column("cost_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("free_tier", sa.Boolean(), nullable=False),
        sa.Column("cached", sa.Boolean(), nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_usage_events_api_key_id"), "usage_events", ["api_key_id"], unique=False
    )
    op.create_index(
        op.f("ix_usage_events_created_at"), "usage_events", ["created_at"], unique=False
    )
    op.create_index(op.f("ix_usage_events_provider"), "usage_events", ["provider"], unique=False)
    op.create_index(
        "ix_usage_month_provider", "usage_events", ["created_at", "provider"], unique=False
    )
    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("consecutive_successes", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("latency_ms_ewma", sa.Float(), nullable=True),
        sa.Column("model_hint", sa.String(length=128), nullable=True),
        sa.Column("price_micro_usd_per_mchar", sa.BigInteger(), nullable=False),
        sa.Column("auth_header", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "api_key_usage_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("characters", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_api_key_usage_daily_day"), "api_key_usage_daily", ["day"], unique=False
    )


def downgrade() -> None:
    # Reverse dependency order: children first.
    for table in (
        "worker_nodes",
        "usage_events",
        "synth_history",
        "reference_audio",
        "jobs",
        "audit_log",
        "api_keys",
        "api_key_usage_daily",
    ):
        op.drop_table(table)
