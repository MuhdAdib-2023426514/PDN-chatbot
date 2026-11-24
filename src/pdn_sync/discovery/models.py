"""Pydantic domain contracts for Phase 1 discovery inventory and proposed scope patterns."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pdn_sync.domain.policy import Disposition, ResourceType, SupersessionPolicy

INVENTORY_SCHEMA_VERSION = 1


class LinkStatus(StrEnum):
    OK = "ok"
    BROKEN = "broken"
    UNFETCHED_ASSET = "unfetched_asset"
    SKIPPED_EXCLUDED = "skipped_excluded"


class DiscoveryCategory(StrEnum):
    """Exact 18-category fixed classification taxonomy from AGENTS.md Section 6."""

    JEVENTS_DEFERRED = "jevents_deferred"
    SYSTEM = "system"
    EXTERNAL = "external"
    HOMEPAGE = "homepage"
    CORPORATE = "corporate"
    SERVICES = "services"
    FAQ = "faq"
    PUBLICATIONS = "publications"
    CONTACT_STATIC_CENTERS = "contact_static_centers"
    STANDALONE_TOOL = "standalone_tool"
    STAFF_DIRECTORY = "staff_directory"
    BLOOD_STOCK_GRAPHIC = "blood_stock_graphic"
    POSTER_BANNER = "poster_banner"
    FACILITY_PHOTO = "facility_photo"
    DECORATIVE = "decorative"
    DOCUMENT_PDF = "document_pdf"
    DOCUMENT_OFFICE = "document_office"
    UNCATEGORIZED = "uncategorized"


class RecommendedAction(StrEnum):
    EXCLUDED = "excluded"
    RECOMMENDED_APPROVE = "recommended_approve"
    RECOMMENDED_EXCLUDE = "recommended_exclude"
    REVIEW = "review"


def make_canonical_key(canonical_url: str) -> str:
    """Deterministic 32-hex key derived from a canonical URL."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:32]


class InventoryItem(BaseModel):
    """Single URL/asset discovery record matching docs/data-contracts.md Section 2."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=INVENTORY_SCHEMA_VERSION)
    run_id: str
    canonical_key: str
    url: str
    original_url: str
    redirect_chain: list[str] = Field(default_factory=list)
    http_status: int | None = None
    link_status: LinkStatus
    duplicate_of: str | None = None
    parent_url: str | None = None
    title_or_alt: str | None = None
    discovered_at_utc: datetime
    depth: int = Field(ge=0)
    resource_type: ResourceType
    mime_hint: str | None = None
    content_length_bytes: int | None = None
    category: DiscoveryCategory
    discovery_method: str
    disposition: Disposition
    recommended_action: RecommendedAction
    reason: str
    policy_rule_id: str | None = None
    monitoring_interval: str


class ProposedPatternRule(BaseModel):
    """Candidate bounded pattern rule proposed by discovery for rotating assets."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pattern: str
    resource_type: ResourceType
    category: DiscoveryCategory
    reason: str
    max_matches: int = Field(ge=1, le=500)
    check_interval: str
    supersession: SupersessionPolicy = Field(default=SupersessionPolicy.NONE)
    match_count: int = Field(ge=1)
    examples: list[str] = Field(default_factory=list)


class InventoryEnvelope(BaseModel):
    """Top-level envelope for data/discovery/url_inventory.json."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=INVENTORY_SCHEMA_VERSION)
    run_id: str
    scope_file_hash: str
    started_at_utc: datetime
    finished_at_utc: datetime
    bounds: dict[str, Any]
    summary_counts: dict[str, Any]
    items: list[InventoryItem]
