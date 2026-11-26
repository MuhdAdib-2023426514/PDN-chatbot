"""Phase 5 reliability tables (task_attempts, outbox_events) and chunk FTS5 index.

Revision ID: 0005_phase5_reliability_and_ops
Revises: 0004_phase4_donation_schedules
Create Date: 2026-11-26 17:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_phase5_reliability_and_ops"
down_revision: str | Sequence[str] | None = "0004_phase4_donation_schedules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_attempts",
        sa.Column("attempt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("sync_runs.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("task_type", sa.String(length=48), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_class", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("next_retry_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_attempts_correlation_id", "task_attempts", ["correlation_id"])
    op.create_index("ix_task_attempts_run_created", "task_attempts", ["run_id", "created_at_utc"])
    op.create_index(
        "ix_task_attempts_type_status",
        "task_attempts",
        ["task_type", "status", "created_at_utc"],
    )
    op.create_index(
        "ix_task_attempts_resource_created",
        "task_attempts",
        ["resource_id", "created_at_utc"],
    )

    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_id", sa.String(length=64), nullable=True),
        sa.Column("publication_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_retry_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_outbox_events_resource_id", "outbox_events", ["resource_id"])
    op.create_index(
        "ix_outbox_events_status_retry", "outbox_events", ["status", "next_retry_at_utc"]
    )
    op.create_index("ix_outbox_events_type_status", "outbox_events", ["event_type", "status"])

    op.execute(
        """
        CREATE VIRTUAL TABLE knowledge_chunks_fts_u61 USING fts5(
            chunk_id UNINDEXED,
            publication_id UNINDEXED,
            category UNINDEXED,
            language UNINDEXED,
            title,
            section,
            text,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_chunks_fts_u61")
    op.drop_table("outbox_events")
    op.drop_table("task_attempts")
