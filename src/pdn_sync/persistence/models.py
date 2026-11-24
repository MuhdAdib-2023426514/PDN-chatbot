"""Phase 1 SQLite control-plane ORM models (single-writer design)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
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
