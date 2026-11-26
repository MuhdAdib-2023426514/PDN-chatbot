"""Phase 2 SQLite staging, fetch provenance, versions, media assets, and Gate B review tables.

Revision ID: 0002_phase2_staging
Revises: 0001_phase1_control_plane
Create Date: 2026-11-25 11:05:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase2_staging"
down_revision: str | Sequence[str] | None = "0001_phase1_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_fetches",
        sa.Column("fetch_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("sync_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("redirect_chain_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("fetched_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("etag", sa.String(length=256), nullable=True),
        sa.Column("last_modified", sa.String(length=256), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("blob_sha256", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("fetch_outcome", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_resource_fetches_resource_id", "resource_fetches", ["resource_id"])
    op.create_index("ix_resource_fetches_fetched_at", "resource_fetches", ["fetched_at_utc"])

    op.create_table(
        "resource_versions",
        sa.Column("version_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column(
            "fetch_id",
            sa.String(length=64),
            sa.ForeignKey("resource_fetches.fetch_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_effective_date", sa.String(length=32), nullable=True),
        sa.Column("source_effective_date_provenance", sa.String(length=32), nullable=True),
        sa.Column("extractor_name", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="current"),
        sa.UniqueConstraint("resource_id", "version_seq", name="uq_resource_version_seq"),
    )
    op.create_index("ix_resource_versions_resource_id", "resource_versions", ["resource_id"])
    op.create_index("ix_resource_versions_semantic_hash", "resource_versions", ["semantic_hash"])

    op.create_table(
        "media_assets",
        sa.Column("asset_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("blob_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_effective_date", sa.String(length=32), nullable=True),
        sa.Column("usage_notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_media_assets_resource_id", "media_assets", ["resource_id"])
    op.create_index("ix_media_assets_blob_sha256", "media_assets", ["blob_sha256"])

    op.create_table(
        "staged_artifacts",
        sa.Column("artifact_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_version_id",
            sa.String(length=64),
            sa.ForeignKey("resource_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("policy_rule_id", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("extractor_name", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("json_path", sa.Text(), nullable=False),
        sa.Column("markdown_path", sa.Text(), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("critical_fact_flags_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_effective_date", sa.String(length=32), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_staged_artifacts_review_status",
        "staged_artifacts",
        ["review_status", "created_at_utc"],
    )
    op.create_index(
        "ix_staged_artifacts_resource_semantic",
        "staged_artifacts",
        ["resource_id", "semantic_hash"],
    )

    op.create_table(
        "review_decisions",
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("batch_id", sa.String(length=64), nullable=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=48), nullable=False),
        sa.Column("policy_mode", sa.String(length=32), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(length=64),
            sa.ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_version_id", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("carried_from_decision_id", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_review_decisions_artifact_id", "review_decisions", ["artifact_id"])
    op.create_index("ix_review_decisions_decided_at", "review_decisions", ["decided_at_utc"])


def downgrade() -> None:
    op.drop_index("ix_review_decisions_decided_at", table_name="review_decisions")
    op.drop_index("ix_review_decisions_artifact_id", table_name="review_decisions")
    op.drop_table("review_decisions")

    op.drop_index("ix_staged_artifacts_resource_semantic", table_name="staged_artifacts")
    op.drop_index("ix_staged_artifacts_review_status", table_name="staged_artifacts")
    op.drop_table("staged_artifacts")

    op.drop_index("ix_media_assets_blob_sha256", table_name="media_assets")
    op.drop_index("ix_media_assets_resource_id", table_name="media_assets")
    op.drop_table("media_assets")

    op.drop_index("ix_resource_versions_semantic_hash", table_name="resource_versions")
    op.drop_index("ix_resource_versions_resource_id", table_name="resource_versions")
    op.drop_table("resource_versions")

    op.drop_index("ix_resource_fetches_fetched_at", table_name="resource_fetches")
    op.drop_index("ix_resource_fetches_resource_id", table_name="resource_fetches")
    op.drop_table("resource_fetches")
