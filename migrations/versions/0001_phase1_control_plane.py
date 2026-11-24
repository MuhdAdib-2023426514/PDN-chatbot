"""Phase 1 SQLite control-plane tables.

Revision ID: 0001_phase1_control_plane
Revises:
Create Date: 2026-11-24 13:47:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase1_control_plane"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("profile", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scope_file_hash", sa.String(length=64), nullable=False),
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bounds_json", sa.Text(), nullable=False),
        sa.Column("summary_counts_json", sa.Text(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )
    op.create_index("ix_sync_runs_profile", "sync_runs", ["profile"])

    op.create_table(
        "crawl_resources",
        sa.Column("resource_id", sa.String(length=64), primary_key=True),
        sa.Column("canonical_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False, server_default="linked"),
        sa.Column("missing_consecutive_runs", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_crawl_resources_canonical_key", "crawl_resources", ["canonical_key"])
    op.create_index("ix_crawl_resources_category", "crawl_resources", ["category"])

    op.create_table(
        "discovered_urls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("sync_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("canonical_key", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("redirect_chain_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("link_status", sa.String(length=32), nullable=False),
        sa.Column("duplicate_of", sa.Text(), nullable=True),
        sa.Column("parent_url", sa.Text(), nullable=True),
        sa.Column("title_or_alt", sa.Text(), nullable=True),
        sa.Column("discovered_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("mime_hint", sa.String(length=128), nullable=True),
        sa.Column("content_length_bytes", sa.Integer(), nullable=True),
        sa.Column("normalized_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("k2_item_id", sa.String(length=32), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("discovery_method", sa.String(length=64), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("recommended_action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_rule_id", sa.String(length=128), nullable=True),
        sa.Column("monitoring_interval", sa.String(length=32), nullable=False),
        sa.UniqueConstraint("run_id", "canonical_key", name="uq_discovered_run_canonical"),
    )
    op.create_index("ix_discovered_urls_run_id", "discovered_urls", ["run_id"])
    op.create_index("ix_discovered_run_category", "discovered_urls", ["run_id", "category"])
    op.create_index("ix_discovered_run_disposition", "discovered_urls", ["run_id", "disposition"])

    op.create_table(
        "scope_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("previous_disposition", sa.String(length=32), nullable=True),
        sa.Column("new_disposition", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("scope_file_hash_before", sa.String(length=64), nullable=False),
        sa.Column("scope_file_hash_after", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_scope_decisions_decided_at_utc", "scope_decisions", ["decided_at_utc"])

    op.create_table(
        "compiled_scope_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_file_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("compiled_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("compiled_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("compiled_scope_cache")
    op.drop_index("ix_scope_decisions_decided_at_utc", table_name="scope_decisions")
    op.drop_table("scope_decisions")
    op.drop_index("ix_discovered_run_disposition", table_name="discovered_urls")
    op.drop_index("ix_discovered_run_category", table_name="discovered_urls")
    op.drop_index("ix_discovered_urls_run_id", table_name="discovered_urls")
    op.drop_table("discovered_urls")
    op.drop_index("ix_crawl_resources_category", table_name="crawl_resources")
    op.drop_index("ix_crawl_resources_canonical_key", table_name="crawl_resources")
    op.drop_table("crawl_resources")
    op.drop_index("ix_sync_runs_profile", table_name="sync_runs")
    op.drop_table("sync_runs")
