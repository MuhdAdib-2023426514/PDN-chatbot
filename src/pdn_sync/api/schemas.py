"""Pydantic response schemas for the Phase 3 read-only chatbot API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pdn_sync.telemetry.alerts import AlertItem


class KnowledgeChunkHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    text: str
    score: float
    title: str | None = None
    section: str | None = None
    category: str
    language: str
    source_url: str
    asset_url: str | None = None
    content_type: str | None = None
    last_verified_at: str
    source_effective_date: str | None = None
    valid_until: str | None = None
    pending_change: bool = False


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    lang: str
    category: str | None = None
    total: int
    results: list[KnowledgeChunkHit]


class PublishedTableHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    caption: str | None = None
    headers: list[str]
    rows: list[list[str]]
    units: list[str | None]
    source_url: str
    category: str
    last_verified_at: str
    source_effective_date: str | None = None
    valid_until: str | None = None
    pending_change: bool = False


class TableSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    source_url: str | None = None
    total: int
    results: list[PublishedTableHit]


class DonationSessionHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    session_id: str
    organizer: str | None = None
    location_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    event_date: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    session_label: str | None = None
    target_donors: int | None = None
    raw_cells: dict[str, str]
    status: str
    source_url: str
    last_verified_at: str
    pending_change: bool = False


class DonationEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    state: str | None = None
    query: str | None = None
    total: int
    results: list[DonationSessionHit]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    sqlite: str
    qdrant: str
    qdrant_mode: str
    qdrant_published_points: int


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published_documents: int
    published_chunks: int
    published_tables: int
    published_donation_events: int = 0
    published_donation_sessions: int = 0
    superseded_documents: int
    retired_documents: int
    pending_change_documents: int
    approved_unpublished_artifacts: int
    pending_gate_b: int
    quarantined_gate_b: int
    active_embedding_model: str | None = None
    active_qdrant_collection: str | None = None
    retrieval_mode: str = "dense"
    qdrant_mode: str
    qdrant_published_points: int
    dlq_count: int = 0
    outbox_pending_count: int = 0
    fts5_mismatches: dict[str, int] = Field(default_factory=dict)
    oldest_pending_gate_b_age_hours: float | None = None
    alerts: list[AlertItem] = Field(default_factory=list)
    last_completed_runs_by_profile: dict[str, dict[str, Any]]
