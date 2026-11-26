"""Domain policy models for ingestion, review, politeness, and bounds."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    HYBRID = "hybrid"
    AUTOMATIC = "automatic"


class ResourceType(StrEnum):
    PAGE = "page"
    IMAGE_POSTER = "image_poster"
    DOCUMENT = "document"
    EXTERNAL_REF = "external_ref"


class Disposition(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXCLUDED = "excluded"


class SupersessionPolicy(StrEnum):
    NONE = "none"
    LATEST_WINS = "latest_wins"


class PolitenessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency_per_host: int = Field(default=1, ge=1, le=2)
    delay_min_seconds: float = Field(default=2.0, ge=2.0)
    delay_max_seconds: float = Field(default=5.0, ge=2.0)
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    max_document_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=200 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_delay_bounds(self) -> PolitenessPolicy:
        if self.delay_max_seconds < self.delay_min_seconds:
            msg = "delay_max_seconds must be >= delay_min_seconds"
            raise ValueError(msg)
        return self


class DiscoveryBounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=1500, ge=1, le=10000)
    max_depth: int = Field(default=8, ge=1, le=20)
    max_listing_pages: int = Field(default=50, ge=1, le=500)
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    max_run_duration_seconds: int = Field(default=7200, ge=60, le=28800)
    anomaly_growth_factor: float = Field(default=3.0, gt=1.0, le=20.0)


class OcrPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confidence_threshold_overall: float = Field(default=0.75, ge=0.0, le=1.0)
    confidence_threshold_critical: float = Field(default=0.85, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> OcrPolicy:
        if self.confidence_threshold_critical < self.confidence_threshold_overall:
            msg = "confidence_threshold_critical must be >= confidence_threshold_overall"
            raise ValueError(msg)
        return self


class CriticalFactsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[str] = Field(default_factory=lambda: ["blood_stock_graphic"])
    keywords: list[str] = Field(default_factory=list)


class IndexingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    small_table_max_rows_for_qdrant: int = Field(default=30, ge=0, le=200)
    embed_donation_schedule_rows: bool = Field(default=False)
    include_staff_directory_in_qdrant: bool = Field(default=True)
    chunk_target_chars: int = Field(default=1000, ge=200, le=4000)
    chunk_overlap_chars: int = Field(default=150, ge=0, le=1000)
    embedding_model: str = Field(default="BAAI/bge-m3", min_length=1)
    retrieval_mode: Literal["dense", "hybrid"] = Field(default="dense")
    hybrid_rrf_k: int = Field(default=60, ge=1, le=500)
    hybrid_candidate_multiplier: int = Field(default=4, ge=1, le=20)

    @model_validator(mode="after")
    def validate_chunk_overlap(self) -> IndexingPolicy:
        if self.chunk_overlap_chars >= self.chunk_target_chars:
            msg = "chunk_overlap_chars must be < chunk_target_chars"
            raise ValueError(msg)
        return self


class CalendarPolicy(BaseModel):
    """Phase 4 bounded JEvents structured donation schedule policy."""

    model_config = ConfigDict(extra="forbid")

    months_ahead: int = Field(default=3, ge=0, le=12)
    max_eventdetail_pages_per_run: int = Field(default=200, ge=1, le=500)
    calendar_interval_hours: int = Field(default=6, ge=1, le=24)
    allow_past_days_grace: int = Field(default=1, ge=0, le=31)


class RetryPolicy(BaseModel):
    """Phase 5 bounded retry & backoff policy for transient outages."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_base_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    backoff_max_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    jitter_ratio: float = Field(default=0.25, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_backoff_bounds(self) -> RetryPolicy:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            msg = "backoff_max_seconds must be >= backoff_base_seconds"
            raise ValueError(msg)
        return self


class ObservabilityPolicy(BaseModel):
    """Phase 5 alert thresholds for freshness, review backlogs, and sync drift."""

    model_config = ConfigDict(extra="forbid")

    homepage_overdue_hours: int = Field(default=12, ge=1, le=720)
    calendar_overdue_hours: int = Field(default=18, ge=1, le=720)
    site_overdue_hours: int = Field(default=48, ge=1, le=720)
    discovery_overdue_days: int = Field(default=14, ge=1, le=180)
    max_pending_gate_b: int = Field(default=50, ge=0, le=10000)
    max_pending_gate_b_age_hours: int = Field(default=168, ge=1, le=8760)
    max_quarantined_gate_b: int = Field(default=0, ge=0, le=1000)
    max_dlq_items: int = Field(default=0, ge=0, le=1000)


class IngestionPolicyConfig(BaseModel):
    """Schema for config/ingestion_policy.yaml."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    approval_mode: ApprovalMode = Field(default=ApprovalMode.MANUAL)
    retirement_consecutive_runs: int = Field(default=2, ge=1, le=10)
    domain_timezone: str = Field(default="Asia/Kuala_Lumpur")
    politeness: PolitenessPolicy = Field(default_factory=PolitenessPolicy)
    discovery_bounds: DiscoveryBounds = Field(default_factory=DiscoveryBounds)
    ocr: OcrPolicy = Field(default_factory=OcrPolicy)
    critical_facts: CriticalFactsPolicy = Field(default_factory=CriticalFactsPolicy)
    indexing: IndexingPolicy = Field(default_factory=IndexingPolicy)
    calendar: CalendarPolicy = Field(default_factory=CalendarPolicy)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    observability: ObservabilityPolicy = Field(default_factory=ObservabilityPolicy)

    @field_validator("domain_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = f"Invalid IANA timezone: {value!r}"
            raise ValueError(msg) from exc
        return value
