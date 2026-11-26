"""Atomic JSON + Markdown staging exporters under data/extracted/, validated/, and rejected/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pdn_sync.discovery.exporter import atomic_write_text
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.extractors.contracts import ExtractedTable, ExtractionResult


def _folder_for_artifact(result: ExtractionResult) -> str:
    if result.category == DiscoveryCategory.DONATION_SCHEDULE or result.structured_events:
        return "donation_schedules"
    if result.resource_type == ResourceType.PAGE:
        return "articles"
    if result.resource_type == ResourceType.IMAGE_POSTER:
        return "posters"
    if result.resource_type == ResourceType.DOCUMENT:
        return "documents"
    return "articles"


def _render_markdown_table(table: ExtractedTable) -> list[str]:
    lines: list[str] = []
    if table.caption:
        lines.append(f"**Table {table.table_index + 1}: {table.caption}**")
        lines.append("")

    col_count = max([len(table.headers), *(len(r) for r in table.rows)], default=0)
    if col_count == 0:
        return lines

    headers = table.headers if table.headers else [f"Col {i + 1}" for i in range(col_count)]
    headers = [h.replace("|", "\\|").replace("\n", " ") for h in headers]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in table.rows:
        escaped = [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.append("")
    return lines


def render_extraction_markdown(result: ExtractionResult) -> str:
    """Render a human-inspectable Markdown companion for a staged ExtractionResult."""
    lines: list[str] = [
        f"# {result.title or result.source_url}",
        "",
        "## Provenance & Gate B Metadata",
        "",
        f"- **Artifact ID:** `{result.artifact_id}`",
        f"- **Resource ID:** `{result.resource_id}`",
        f"- **Version ID:** `{result.resource_version_id}`",
        f"- **Source URL:** {result.source_url}",
        f"- **Category / Type:** `{result.category.value}` / `{result.resource_type.value}`",
        f"- **Raw Blob SHA-256:** `{result.content_hash}`",
        f"- **Semantic Hash:** `{result.semantic_hash}`",
        f"- **Extractor:** `{result.extractor_name}` (`{result.extractor_version}`)",
        f"- **Captured (UTC):** `{result.captured_at_utc.isoformat()}`",
        f"- **Source Effective Date:** `{result.source_effective_date or 'unknown'}`"
        + (
            f" (`{result.source_effective_date_provenance}`)"
            if result.source_effective_date_provenance
            else ""
        ),
        f"- **Validation Status:** `{result.validation_status.value}`",
        f"- **Review Status:** `{result.review_status.value}`",
        f"- **Confidence:** `{result.confidence if result.confidence is not None else 'n/a'}`",
        "- **Critical Fact Flags:** "
        + (
            ", ".join(f"`{f}`" for f in result.critical_fact_flags)
            if result.critical_fact_flags
            else "none"
        ),
        "- **Warnings:** "
        + (", ".join(f"`{w}`" for w in result.warnings) if result.warnings else "none"),
        "",
    ]

    if result.text_blocks:
        lines.extend(["## Extracted Content", ""])
        for blk in sorted(result.text_blocks, key=lambda b: b.order):
            if blk.heading_level is not None:
                prefix = "#" * min(6, max(2, blk.heading_level + 1))
                lines.append(f"{prefix} {blk.text}")
                lines.append("")
            else:
                lines.append(blk.text)
                lines.append("")

    if result.structured_events:
        lines.extend(["## Structured Donation Sessions (JSON Authoritative)", ""])
        header_row = (
            "| Date | Session | Start | End | Organizer | Location | State | Target | Raw Cells |"
        )
        lines.extend(
            [
                header_row,
                "|---|---|---|---|---|---|---|---:|---|",
            ]
        )
        for ev in result.structured_events:
            raw_summary = "; ".join(f"{k}={v}" for k, v in sorted(ev.raw_cells.items()))
            cells = [
                ev.event_date or "-",
                ev.session_label or "-",
                ev.time_start or "-",
                ev.time_end or "-",
                ev.organizer or "-",
                ev.location_name or "-",
                ev.state or "-",
                str(ev.target_donors) if ev.target_donors is not None else "-",
                raw_summary or "-",
            ]
            escaped_cells = [c.replace("|", "\\|").replace("\n", " ") for c in cells]
            lines.append("| " + " | ".join(escaped_cells) + " |")
        lines.append("")

    if result.tables:
        lines.extend(["## Extracted Tables", ""])
        for tbl in result.tables:
            lines.extend(_render_markdown_table(tbl))

    if result.ocr_lines:
        lines.extend(["## OCR Lines (Reading Order)", ""])
        lines.extend(["| # | Confidence | Text |", "|---:|---:|---|"])
        for idx, line in enumerate(result.ocr_lines, start=1):
            escaped = line.text.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {idx} | {line.confidence:.3f} | {escaped} |")
        lines.append("")

    if result.media_refs:
        lines.extend(["## Embedded Media References (Not Fetched Unless Approved)", ""])
        lines.extend(["| Pos | Scope Status | Alt | URL |", "|---:|---|---|---|"])
        for ref in result.media_refs:
            alt = (ref.alt or "-").replace("|", "\\|")
            lines.append(f"| {ref.position} | `{ref.scope_status.value}` | {alt} | {ref.url} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _update_manifest(
    *,
    manifest_path: Path,
    result: ExtractionResult,
    json_path: Path,
    md_path: Path,
    table_md_path: Path | None,
) -> None:
    manifest: dict[str, Any] = {"schema_version": 1, "artifacts": {}}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("artifacts"), dict):
                manifest = loaded
        except json.JSONDecodeError:
            pass

    manifest["artifacts"][result.artifact_id] = {
        "resource_id": result.resource_id,
        "resource_version_id": result.resource_version_id,
        "source_url": result.source_url,
        "content_hash": result.content_hash,
        "semantic_hash": result.semantic_hash,
        "extractor_name": result.extractor_name,
        "extractor_version": result.extractor_version,
        "validation_status": result.validation_status.value,
        "review_status": result.review_status.value,
        "json_path": str(json_path),
        "markdown_path": str(md_path),
        "table_markdown_path": str(table_md_path) if table_md_path else None,
    }
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def export_staged_artifact(
    *,
    result: ExtractionResult,
    version_seq: int,
    data_dir: Path,
) -> tuple[Path, Path]:
    """Atomically write immutable JSON + Markdown staging artifacts and update manifest."""
    extracted_root = data_dir / "extracted"
    sub_dir = extracted_root / _folder_for_artifact(result)
    stem = f"{result.resource_id}_v{version_seq:03d}_{result.semantic_hash[:12]}"

    json_path = sub_dir / f"{stem}.json"
    md_path = sub_dir / f"{stem}.md"

    atomic_write_text(json_path, result.model_dump_json(indent=2) + "\n")
    atomic_write_text(md_path, render_extraction_markdown(result))

    table_md_path: Path | None = None
    if result.tables:
        tables_dir = extracted_root / "tables"
        table_md_path = tables_dir / f"{stem}_tables.md"
        tbl_lines = [
            f"# Tables — {result.title or result.source_url}",
            "",
            f"Source: {result.source_url}",
            "",
        ]
        for tbl in result.tables:
            tbl_lines.extend(_render_markdown_table(tbl))
        atomic_write_text(table_md_path, "\n".join(tbl_lines).rstrip() + "\n")

    _update_manifest(
        manifest_path=extracted_root / "manifest.json",
        result=result,
        json_path=json_path,
        md_path=md_path,
        table_md_path=table_md_path,
    )
    return json_path, md_path


def export_review_snapshot(
    *,
    result: ExtractionResult,
    approved: bool,
    data_dir: Path,
) -> Path:
    """Export an inspectable snapshot under data/validated/ or data/rejected/ after review."""
    target_dir = data_dir / ("validated" if approved else "rejected")
    snap_path = target_dir / f"{result.artifact_id}_{result.semantic_hash[:12]}.json"
    atomic_write_text(snap_path, result.model_dump_json(indent=2) + "\n")
    return snap_path
