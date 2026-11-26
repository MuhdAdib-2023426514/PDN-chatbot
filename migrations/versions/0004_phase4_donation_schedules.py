"""Phase 4 structured blood donation schedule tables and SQLite FTS5 indexes.

Revision ID: 0004_phase4_donation_schedules
Revises: 0003_phase3_publication_and_fts5
Create Date: 2026-11-26 16:08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_phase4_donation_schedules"
down_revision: str | Sequence[str] | None = "0003_phase3_publication_and_fts5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "donation_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("upstream_event_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "publication_id",
            sa.String(length=64),
            sa.ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(length=64),
            sa.ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("event_date", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="listed"),
        sa.Column("last_verified_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_change", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version_seq", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_donation_events_date", "donation_events", ["event_date"])
    op.create_index(
        "ix_donation_events_status_date",
        "donation_events",
        ["status", "event_date"],
    )
    op.create_index("ix_donation_events_resource", "donation_events", ["resource_id"])

    op.create_table(
        "donation_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=64),
            sa.ForeignKey("donation_events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "publication_id",
            sa.String(length=64),
            sa.ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_identity", sa.String(length=64), nullable=False),
        sa.Column("organizer", sa.Text(), nullable=True),
        sa.Column("normalized_organizer", sa.Text(), nullable=False, server_default=""),
        sa.Column("location_name", sa.Text(), nullable=True),
        sa.Column("normalized_location", sa.Text(), nullable=False, server_default=""),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("event_date", sa.String(length=16), nullable=True),
        sa.Column("time_start", sa.String(length=16), nullable=True),
        sa.Column("time_end", sa.String(length=16), nullable=True),
        sa.Column("session_label", sa.String(length=64), nullable=True),
        sa.Column("target_donors", sa.Integer(), nullable=True),
        sa.Column("raw_cells_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="listed"),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("last_verified_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pending_change", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("superseded_by_session_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_donation_sessions_event_date",
        "donation_sessions",
        ["event_date", "is_current"],
    )
    op.create_index(
        "ix_donation_sessions_state_date",
        "donation_sessions",
        ["state", "event_date", "is_current"],
    )
    op.create_index(
        "ix_donation_sessions_identity",
        "donation_sessions",
        ["session_identity", "is_current"],
    )
    op.create_index(
        "ix_donation_sessions_norm_loc",
        "donation_sessions",
        ["normalized_location"],
    )
    op.create_index(
        "ix_donation_sessions_norm_org",
        "donation_sessions",
        ["normalized_organizer"],
    )

    op.execute(
        """
        CREATE VIRTUAL TABLE donation_sessions_fts_u61 USING fts5(
            session_id UNINDEXED,
            event_date UNINDEXED,
            state UNINDEXED,
            organizer,
            location_name,
            address,
            city,
            raw_text,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE donation_sessions_fts_tri USING fts5(
            session_id UNINDEXED,
            organizer,
            location_name,
            address,
            city,
            tokenize='trigram'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS donation_sessions_fts_tri")
    op.execute("DROP TABLE IF EXISTS donation_sessions_fts_u61")
    op.drop_table("donation_sessions")
    op.drop_table("donation_events")
