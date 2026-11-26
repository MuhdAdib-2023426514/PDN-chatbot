"""Typed ExtractionResult contracts shared across 2a HTML, 2b PDF, 2c OCR, and Gate B staging."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.staging.semantic_hash import (
    SemanticHashInput,
    SemanticStructuredEvent,
    SemanticTable,
    SemanticTextBlock,
    compute_semantic_hash,
)

EXTRACTION_SCHEMA_VERSION = 1


class ValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    NEEDS_REVIEW = "needs_review"


class ReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class MediaRef(BaseModel):
    """Reference to an image embedded inside an approved page (never fetched unless approved)."""

    model_config = ConfigDict(extra="forbid")

    url: str
    alt: str | None = None
    position: int = Field(ge=0)
    scope_status: Disposition


class OcrLine(BaseModel):
    """Single OCR line detected in reading order with confidence and optional polygon/bbox."""

    model_config = ConfigDict(extra="forbid")

    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    box: list[float] = Field(default_factory=list)


class ExtractedTable(BaseModel):
    """Faithful structured table representation preserving headers, rows, units, and provenance."""

    model_config = ConfigDict(extra="forbid")

    table_index: int = Field(ge=0)
    caption: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    units: list[str | None] = Field(default_factory=list)
    source_selector: str | None = None


class ExtractionResult(BaseModel):
    """Immutable typed JSON artifact produced by every Phase 2 extractor before Gate B review."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=EXTRACTION_SCHEMA_VERSION)
    artifact_id: str
    resource_id: str
    resource_version_id: str
    source_url: str
    resource_type: ResourceType
    category: DiscoveryCategory
    policy_rule_id: str | None = None
    content_hash: str
    semantic_hash: str
    extractor_name: str
    extractor_version: str
    captured_at_utc: datetime
    extracted_at_utc: datetime
    source_effective_date: str | None = None
    source_effective_date_provenance: str | None = None
    content_type: str | None = None
    title: str | None = None
    language: str = Field(default="ms")
    text_blocks: list[SemanticTextBlock] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    media_refs: list[MediaRef] = Field(default_factory=list)
    ocr_lines: list[OcrLine] = Field(default_factory=list)
    structured_events: list[SemanticStructuredEvent] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    critical_fact_flags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    validation_status: ValidationStatus = Field(default=ValidationStatus.VALID)
    review_status: ReviewStatus = Field(default=ReviewStatus.PENDING_REVIEW)
    provenance: dict[str, Any] = Field(default_factory=dict)

    def to_semantic_hash_input(self) -> SemanticHashInput:
        """Project meaningful fields only into the deterministic semantic_hash input."""
        return SemanticHashInput(
            title=self.title,
            text_blocks=self.text_blocks,
            tables=[
                SemanticTable(
                    headers=t.headers,
                    rows=t.rows,
                    units=t.units,
                )
                for t in self.tables
            ],
            ocr_lines=[line.text for line in self.ocr_lines],
            structured_events=self.structured_events,
        )

    def recompute_semantic_hash(self) -> str:
        return compute_semantic_hash(self.to_semantic_hash_input())
