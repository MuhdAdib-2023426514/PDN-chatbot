"""Deterministic semantic_hash computation (AGENTS.md Section 7).

Included meaningful fields ONLY:
- title
- text_blocks (normalized text + heading_level + order)
- tables (normalized headers + rows + units)
- ocr_lines (normalized text in reading order)
- structured_events (normalized schedule fields + raw cells)

Excluded metadata fields (never hashed):
- extracted_at_utc, captured_at_utc, run_id, extractor_name, extractor_version,
  HTTP headers, last_verified_at, hit counters, CSRF tokens, page chrome.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")


def normalize_semantic_text(value: str) -> str:
    """Normalize text per AGENTS.md Section 7 while preserving case and punctuation.

    Steps:
    1. Unicode NFC normalization
    2. Normalize CRLF / CR line endings to LF ('\\n')
    3. Collapse horizontal whitespace runs ([ \\t]+) to a single ASCII space
    4. Strip leading and trailing whitespace
    """
    nfc = unicodedata.normalize("NFC", value)
    lf = nfc.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = _HORIZONTAL_WS_RE.sub(" ", lf)
    return collapsed.strip()


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_semantic_text(value)


class SemanticTextBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order: int = Field(ge=0)
    heading_level: int | None = Field(default=None, ge=1, le=6)
    text: str


class SemanticTable(BaseModel):
    model_config = ConfigDict(extra="ignore")

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    units: list[str | None] = Field(default_factory=list)


class SemanticStructuredEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    raw_cells: dict[str, str] = Field(default_factory=dict)


class SemanticHashInput(BaseModel):
    """Meaningful-content projection used for deterministic semantic_hash."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    text_blocks: list[SemanticTextBlock] = Field(default_factory=list)
    tables: list[SemanticTable] = Field(default_factory=list)
    ocr_lines: list[str] = Field(default_factory=list)
    structured_events: list[SemanticStructuredEvent] = Field(default_factory=list)


def canonical_semantic_payload(data: SemanticHashInput) -> dict[str, Any]:
    """Build the normalized dictionary hashed into semantic_hash."""
    sorted_blocks = sorted(data.text_blocks, key=lambda blk: blk.order)
    return {
        "ocr_lines": [normalize_semantic_text(line) for line in data.ocr_lines],
        "structured_events": [
            {
                "address": _normalize_optional_text(ev.address),
                "city": _normalize_optional_text(ev.city),
                "event_date": _normalize_optional_text(ev.event_date),
                "location_name": _normalize_optional_text(ev.location_name),
                "organizer": _normalize_optional_text(ev.organizer),
                "raw_cells": {
                    normalize_semantic_text(k): normalize_semantic_text(v)
                    for k, v in sorted(ev.raw_cells.items())
                },
                "session_label": _normalize_optional_text(ev.session_label),
                "state": _normalize_optional_text(ev.state),
                "target_donors": ev.target_donors,
                "time_end": _normalize_optional_text(ev.time_end),
                "time_start": _normalize_optional_text(ev.time_start),
            }
            for ev in data.structured_events
        ],
        "tables": [
            {
                "headers": [normalize_semantic_text(h) for h in tbl.headers],
                "rows": [[normalize_semantic_text(cell) for cell in row] for row in tbl.rows],
                "units": [_normalize_optional_text(u) for u in tbl.units],
            }
            for tbl in data.tables
        ],
        "text_blocks": [
            {
                "heading_level": blk.heading_level,
                "order": blk.order,
                "text": normalize_semantic_text(blk.text),
            }
            for blk in sorted_blocks
        ],
        "title": _normalize_optional_text(data.title),
    }


def canonical_semantic_json(data: SemanticHashInput) -> str:
    """Serialize normalized meaningful fields to canonical UTF-8 JSON."""
    payload = canonical_semantic_payload(data)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_semantic_hash(data: SemanticHashInput) -> str:
    """Compute deterministic SHA-256 hex digest over normalized meaningful content."""
    canonical = canonical_semantic_json(data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
