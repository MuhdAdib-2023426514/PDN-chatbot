"""Phase 1 SQLite control-plane ORM models (single-writer design)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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


class Base(DeclarativeBase):
    """Base metadata class for PDN Sync SQLite tables."""


class SyncRunRow(Base):
    __tablename__ = "sync_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bounds_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class CrawlResourceRow(Base):
    __tablename__ = "crawl_resources"

    resource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    first_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="linked")
    missing_consecutive_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DiscoveredUrlRow(Base):
    __tablename__ = "discovered_urls"
    __table_args__ = (
        UniqueConstraint("run_id", "canonical_key", name="uq_discovered_run_canonical"),
        Index("ix_discovered_run_category", "run_id", "category"),
        Index("ix_discovered_run_disposition", "run_id", "disposition"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sync_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_key: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_chain_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_status: Mapped[str] = mapped_column(String(32), nullable=False)
    duplicate_of: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_or_alt: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_length_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    k2_item_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    discovery_method: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    recommended_action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    monitoring_interval: Mapped[str] = mapped_column(String(32), nullable=False)


class ScopeDecisionRow(Base):
    __tablename__ = "scope_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decided_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    previous_disposition: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_file_hash_before: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_file_hash_after: Mapped[str] = mapped_column(String(64), nullable=False)


class CompiledScopeCacheRow(Base):
    __tablename__ = "compiled_scope_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    compiled_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    compiled_json: Mapped[str] = mapped_column(Text, nullable=False)


class ResourceFetchRow(Base):
    __tablename__ = "resource_fetches"

    fetch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sync_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_chain_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fetched_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blob_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetch_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ResourceVersionRow(Base):
    __tablename__ = "resource_versions"
    __table_args__ = (
        UniqueConstraint("resource_id", "version_seq", name="uq_resource_version_seq"),
    )

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    fetch_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("resource_fetches.fetch_id", ondelete="SET NULL"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_effective_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_effective_date_provenance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extractor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="current")


class MediaAssetRow(Base):
    __tablename__ = "media_assets"

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    blob_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_effective_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    usage_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class StagedArtifactRow(Base):
    __tablename__ = "staged_artifacts"
    __table_args__ = (
        Index("ix_staged_artifacts_review_status", "review_status", "created_at_utc"),
        Index("ix_staged_artifacts_resource_semantic", "resource_id", "semantic_hash"),
    )

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("resource_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    json_path: Mapped[str] = mapped_column(Text, nullable=False)
    markdown_path: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    critical_fact_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_effective_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewDecisionRow(Base):
    __tablename__ = "review_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(48), nullable=False)
    policy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    carried_from_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class PublicationVersionRow(Base):
    __tablename__ = "publication_versions"
    __table_args__ = (
        Index("ix_pub_versions_resource_status", "resource_id", "status"),
        Index("ix_pub_versions_policy_status", "policy_rule_id", "status"),
        Index("ix_pub_versions_resource_seq", "resource_id", "version_seq"),
    )

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    resource_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("resource_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    semantic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    qdrant_collection: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published")
    valid_from_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_by_pub_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class KnowledgeDocumentRow(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        Index("ix_knowledge_docs_status_category", "status", "category"),
        Index("ix_knowledge_docs_source_url", "source_url"),
    )

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    asset_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_effective_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published")
    pending_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("publication_id", "chunk_index", name="uq_chunk_pub_index"),)

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    point_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    doc_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_documents.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    publication_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    qdrant_synced_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StructuredTableRow(Base):
    __tablename__ = "structured_tables"
    __table_args__ = (
        UniqueConstraint("publication_id", "table_index", name="uq_table_pub_index"),
        Index("ix_structured_tables_status_category", "status", "category"),
        Index("ix_structured_tables_source_url", "source_url"),
    )

    table_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    publication_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    table_index: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_json: Mapped[str] = mapped_column(Text, nullable=False)
    units_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_effective_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published")
    pending_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StructuredTableRowItem(Base):
    __tablename__ = "structured_table_rows"
    __table_args__ = (
        UniqueConstraint("table_id", "row_index", name="uq_table_row_index"),
        Index("ix_structured_table_rows_table_idx", "table_id", "row_index"),
    )

    row_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    table_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("structured_tables.table_id", ondelete="CASCADE"),
        nullable=False,
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    cells_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_selector: Mapped[str | None] = mapped_column(String(128), nullable=True)


class DonationEventRow(Base):
    __tablename__ = "donation_events"
    __table_args__ = (
        Index("ix_donation_events_date", "event_date"),
        Index("ix_donation_events_status_date", "status", "event_date"),
        Index("ix_donation_events_resource", "resource_id"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    upstream_event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    resource_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("crawl_resources.resource_id", ondelete="CASCADE"),
        nullable=False,
    )
    publication_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("staged_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="listed")
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pending_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class DonationSessionRow(Base):
    __tablename__ = "donation_sessions"
    __table_args__ = (
        Index("ix_donation_sessions_event_date", "event_date", "is_current"),
        Index("ix_donation_sessions_state_date", "state", "event_date", "is_current"),
        Index("ix_donation_sessions_identity", "session_identity", "is_current"),
        Index("ix_donation_sessions_norm_loc", "normalized_location"),
        Index("ix_donation_sessions_norm_org", "normalized_organizer"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("donation_events.event_id", ondelete="CASCADE"),
        nullable=False,
    )
    publication_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("publication_versions.publication_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    organizer: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_organizer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    location_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_location: Mapped[str] = mapped_column(Text, nullable=False, default="")
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_date: Mapped[str | None] = mapped_column(String(16), nullable=True)
    time_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    time_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_donors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_cells_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="listed")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    last_verified_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pending_change: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_by_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TaskAttemptRow(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (
        Index("ix_task_attempts_run_created", "run_id", "created_at_utc"),
        Index("ix_task_attempts_type_status", "task_type", "status", "created_at_utc"),
        Index("ix_task_attempts_resource_created", "resource_id", "created_at_utc"),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("sync_runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    task_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_retry_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_status_retry", "status", "next_retry_at_utc"),
        Index("ix_outbox_events_type_status", "event_type", "status"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publication_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_note: Mapped[str | None] = mapped_column(Text, nullable=True)
