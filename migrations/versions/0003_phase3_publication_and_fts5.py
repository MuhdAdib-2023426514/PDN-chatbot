"""Phase 3 active publication tables, structured tables, and SQLite FTS5 indexes.

Revision ID: 0003_phase3_publication_and_fts5
Revises: 0002_phase2_staging
Create Date: 2026-11-26 14:28:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase3_publication_and_fts5"
down_revision: str | Sequence[str] | None = "0002_phase2_staging"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publication_versions",
        sa.Column("publication_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(length=64),
            sa.ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "resource_version_id",
            sa.String(length=64),
            sa.ForeignKey("resource_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_rule_id", sa.String(length=128), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("qdrant_collection", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="published"),
        sa.Column("valid_from_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by_pub_id", sa.String(length=64), nullable=True),
        sa.Column("pending_change", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_pub_versions_resource_status",
        "publication_versions",
        ["resource_id", "status"],
    )
    op.create_index(
        "ix_pub_versions_policy_status",
        "publication_versions",
        ["policy_rule_id", "status"],
    )
    op.create_index(
        "ix_pub_versions_resource_seq",
        "publication_versions",
        ["resource_id", "version_seq"],
    )

    op.create_table(
        "knowledge_documents",
        sa.Column("doc_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "publication_id",
            sa.String(length=64),
            sa.ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(length=64),
            sa.ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_version_id", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("asset_url", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("last_verified_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_effective_date", sa.String(length=32), nullable=True),
        sa.Column("valid_from_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="published"),
        sa.Column("pending_change", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_knowledge_docs_resource_id", "knowledge_documents", ["resource_id"])
    op.create_index(
        "ix_knowledge_docs_status_category",
        "knowledge_documents",
        ["status", "category"],
    )
    op.create_index("ix_knowledge_docs_source_url", "knowledge_documents", ["source_url"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(length=64), primary_key=True),
        sa.Column("point_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "doc_id",
            sa.String(length=64),
            sa.ForeignKey("knowledge_documents.doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "publication_id",
            sa.String(length=64),
            sa.ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("qdrant_synced_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("publication_id", "chunk_index", name="uq_chunk_pub_index"),
    )
    op.create_index("ix_knowledge_chunks_doc_id", "knowledge_chunks", ["doc_id"])
    op.create_index("ix_knowledge_chunks_pub_id", "knowledge_chunks", ["publication_id"])

    op.create_table(
        "structured_tables",
        sa.Column("table_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "publication_id",
            sa.String(length=64),
            sa.ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_id",
            sa.String(length=64),
            sa.ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(length=64),
            sa.ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("table_index", sa.Integer(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("headers_json", sa.Text(), nullable=False),
        sa.Column("units_json", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("last_verified_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_effective_date", sa.String(length=32), nullable=True),
        sa.Column("valid_from_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="published"),
        sa.Column("pending_change", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("publication_id", "table_index", name="uq_table_pub_index"),
    )
    op.create_index("ix_structured_tables_pub_id", "structured_tables", ["publication_id"])
    op.create_index("ix_structured_tables_resource_id", "structured_tables", ["resource_id"])
    op.create_index(
        "ix_structured_tables_status_category",
        "structured_tables",
        ["status", "category"],
    )
    op.create_index("ix_structured_tables_source_url", "structured_tables", ["source_url"])

    op.create_table(
        "structured_table_rows",
        sa.Column("row_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "table_id",
            sa.String(length=64),
            sa.ForeignKey("structured_tables.table_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("cells_json", sa.Text(), nullable=False),
        sa.Column("row_text", sa.Text(), nullable=False),
        sa.Column("source_selector", sa.String(length=128), nullable=True),
        sa.UniqueConstraint("table_id", "row_index", name="uq_table_row_index"),
    )
    op.create_index(
        "ix_structured_table_rows_table_idx",
        "structured_table_rows",
        ["table_id", "row_index"],
    )

    # SQLite FTS5 indexes for Malay/English token search + typo-tolerant trigram matching
    op.execute(
        """
        CREATE VIRTUAL TABLE structured_tables_fts_u61 USING fts5(
            table_id UNINDEXED,
            source_url UNINDEXED,
            category UNINDEXED,
            caption,
            headers_text,
            rows_text,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE structured_tables_fts_tri USING fts5(
            table_id UNINDEXED,
            caption,
            headers_text,
            rows_text,
            tokenize='trigram'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS structured_tables_fts_tri")
    op.execute("DROP TABLE IF EXISTS structured_tables_fts_u61")
    op.drop_table("structured_table_rows")
    op.drop_table("structured_tables")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("publication_versions")
