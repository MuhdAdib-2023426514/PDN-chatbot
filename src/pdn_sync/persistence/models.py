"""Phase 1 SQLite control-plane ORM models (single-writer design)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
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
