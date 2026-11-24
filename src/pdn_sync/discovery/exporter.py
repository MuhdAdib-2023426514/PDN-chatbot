"""Atomic exporters for Phase 1 discovery deliverables (never touches config/crawl_scope.yaml)."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryEnvelope,
    InventoryItem,
    LinkStatus,
    ProposedPatternRule,
    RecommendedAction,
)
from pdn_sync.domain.policy import Disposition, ResourceType

CSV_FIELDNAMES: tuple[str, ...] = (
    "schema_version",
    "run_id",
    "canonical_key",
    "url",
    "original_url",
    "redirect_chain",
    "http_status",
    "link_status",
    "duplicate_of",
    "parent_url",
    "title_or_alt",
    "discovered_at_utc",
    "depth",
    "resource_type",
    "mime_hint",
    "content_length_bytes",
    "category",
    "discovery_method",
    "disposition",
    "recommended_action",
    "reason",
    "policy_rule_id",
    "monitoring_interval",
    "notes",
)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write UTF-8 text via a temporary file in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def compute_summary_counts(items: list[InventoryItem]) -> dict[str, Any]:
    """Compute structured summary counts across resource types, categories, and statuses."""
    by_resource_type = Counter(it.resource_type.value for it in items)
    by_category = Counter(it.category.value for it in items)
    by_disposition = Counter(it.disposition.value for it in items)
    by_link_status = Counter(it.link_status.value for it in items)
    by_recommended_action = Counter(it.recommended_action.value for it in items)

    duplicates = sum(1 for it in items if it.duplicate_of is not None)
    broken = sum(1 for it in items if it.link_status == LinkStatus.BROKEN)
    jevents = sum(1 for it in items if it.category == DiscoveryCategory.JEVENTS_DEFERRED)
    staff_dir = sum(1 for it in items if it.category == DiscoveryCategory.STAFF_DIRECTORY)

    return {
        "total_items": len(items),
        "duplicates": duplicates,
        "broken_links": broken,
        "jevents_excluded": jevents,
        "staff_directory_items": staff_dir,
        "by_resource_type": dict(sorted(by_resource_type.items())),
        "by_category": dict(sorted(by_category.items())),
        "by_disposition": dict(sorted(by_disposition.items())),
        "by_link_status": dict(sorted(by_link_status.items())),
        "by_recommended_action": dict(sorted(by_recommended_action.items())),
    }


def export_inventory_json(envelope: InventoryEnvelope, output_dir: Path) -> Path:
    path = output_dir / "url_inventory.json"
    content = envelope.model_dump_json(indent=2) + "\n"
    atomic_write_text(path, content)
    return path


def export_inventory_csv(items: list[InventoryItem], output_dir: Path) -> Path:
    path = output_dir / "url_inventory.csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_FIELDNAMES))
    writer.writeheader()
    for item in items:
        writer.writerow(
            {
                "schema_version": item.schema_version,
                "run_id": item.run_id,
                "canonical_key": item.canonical_key,
                "url": item.url,
                "original_url": item.original_url,
                "redirect_chain": json.dumps(item.redirect_chain),
                "http_status": "" if item.http_status is None else item.http_status,
                "link_status": item.link_status.value,
                "duplicate_of": item.duplicate_of or "",
                "parent_url": item.parent_url or "",
                "title_or_alt": item.title_or_alt or "",
                "discovered_at_utc": item.discovered_at_utc.isoformat(),
                "depth": item.depth,
                "resource_type": item.resource_type.value,
                "mime_hint": item.mime_hint or "",
                "content_length_bytes": (
                    "" if item.content_length_bytes is None else item.content_length_bytes
                ),
                "category": item.category.value,
                "discovery_method": item.discovery_method,
                "disposition": item.disposition.value,
                "recommended_action": item.recommended_action.value,
                "reason": item.reason,
                "policy_rule_id": item.policy_rule_id or "",
                "monitoring_interval": item.monitoring_interval,
                "notes": "",
            }
        )
    atomic_write_text(path, buf.getvalue())
    return path


def export_proposed_scope_yaml(
    *,
    run_id: str,
    scope_file_hash: str,
    items: list[InventoryItem],
    proposed_patterns: list[ProposedPatternRule],
    output_dir: Path,
) -> Path:
    """Write data/discovery/crawl_scope.proposed.yaml (NEVER overwrites config/crawl_scope.yaml)."""
    path = output_dir / "crawl_scope.proposed.yaml"

    def _url_entries(res_type: ResourceType) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for it in items:
            if (
                it.resource_type == res_type
                and it.disposition == Disposition.PENDING
                and it.recommended_action == RecommendedAction.RECOMMENDED_APPROVE
                and it.duplicate_of is None
                and it.link_status != LinkStatus.BROKEN
            ):
                entries.append(
                    {
                        "url": it.url,
                        "category": it.category.value,
                        "reason": it.reason,
                        "check_interval": it.monitoring_interval,
                    }
                )
        return entries

    def _pattern_entries(res_type: ResourceType) -> list[dict[str, Any]]:
        return [
            {
                "id": pat.id,
                "pattern": pat.pattern,
                "resource_type": pat.resource_type.value,
                "category": pat.category.value,
                "reason": pat.reason,
                "max_matches": pat.max_matches,
                "check_interval": pat.check_interval,
                "supersession": pat.supersession.value,
                "match_count": pat.match_count,
                "examples": pat.examples,
            }
            for pat in proposed_patterns
            if pat.resource_type == res_type
        ]

    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "source_scope_file_hash": scope_file_hash,
        "notice": (
            "PROPOSAL ONLY. Review and apply desired approvals into config/crawl_scope.yaml "
            "via `pdn-sync scope approve`, `pdn-sync scope approve --pattern-id <id>`, or "
            "`pdn-sync scope import data/discovery/url_inventory.csv`."
        ),
        "proposed_approvals": {
            "pages": {
                "urls": _url_entries(ResourceType.PAGE),
                "patterns": _pattern_entries(ResourceType.PAGE),
            },
            "image_posters": {
                "urls": _url_entries(ResourceType.IMAGE_POSTER),
                "patterns": _pattern_entries(ResourceType.IMAGE_POSTER),
            },
            "documents": {
                "urls": _url_entries(ResourceType.DOCUMENT),
                "patterns": _pattern_entries(ResourceType.DOCUMENT),
            },
        },
    }

    yaml = YAML()
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(payload, buf)
    atomic_write_text(path, buf.getvalue())
    return path


def export_discovery_report_md(
    *,
    envelope: InventoryEnvelope,
    proposed_patterns: list[ProposedPatternRule],
    bounds_hit: list[str],
    errors: list[str],
    output_dir: Path,
) -> Path:
    path = output_dir / "discovery_report.md"
    counts = envelope.summary_counts
    lines: list[str] = [
        "# PDN Discovery-Only Report (Phase 1 — Gate A Input)",
        "",
        f"- **Run ID:** `{envelope.run_id}`",
        f"- **Started (UTC):** `{envelope.started_at_utc.isoformat()}`",
        f"- **Finished (UTC):** `{envelope.finished_at_utc.isoformat()}`",
        f"- **Scope file SHA-256:** `{envelope.scope_file_hash}`",
        f"- **Total discovered items:** `{counts['total_items']}`",
        f"- **Duplicates flagged (`duplicate_of`):** `{counts['duplicates']}`",
        f"- **Broken links (`4xx`/`5xx`):** `{counts['broken_links']}`",
        f"- **JEvents links excluded (never fetched):** `{counts['jevents_excluded']}`",
        f"- **Staff directory (`recommended_exclude`, PDPA):** `{counts['staff_directory_items']}`",
        "",
        "## Counts by `resource_type`",
        "",
        "| Resource Type | Count |",
        "|---|---:|",
    ]
    for key, val in counts["by_resource_type"].items():
        lines.append(f"| `{key}` | {val} |")

    lines.extend(
        [
            "",
            "## Counts by `category`",
            "",
            "| Category | Count |",
            "|---|---:|",
        ]
    )
    for key, val in counts["by_category"].items():
        lines.append(f"| `{key}` | {val} |")

    lines.extend(
        [
            "",
            "## Counts by Current `disposition` (Joined Against `config/crawl_scope.yaml`)",
            "",
            "| Disposition | Count |",
            "|---|---:|",
        ]
    )
    for key, val in counts["by_disposition"].items():
        lines.append(f"| `{key}` | {val} |")

    lines.extend(["", "## Proposed Rotating-Asset Pattern Rules", ""])
    if proposed_patterns:
        lines.extend(
            [
                "| Pattern ID | Pattern | Type | Matches | Max Cap | Supersession | Example |",
                "|---|---|---|---:|---:|---|---|",
            ]
        )
        for pat in proposed_patterns:
            example = pat.examples[0] if pat.examples else ""
            lines.append(
                f"| `{pat.id}` | `{pat.pattern}` | `{pat.resource_type.value}` | "
                f"{pat.match_count} | {pat.max_matches} | `{pat.supersession.value}` | {example} |"
            )
    else:
        lines.append("_No rotating-asset pattern rules proposed in this run._")

    duplicates = [it for it in envelope.items if it.duplicate_of is not None]
    lines.extend(["", f"## Duplicate Routes Detected ({len(duplicates)})", ""])
    if duplicates:
        lines.extend(["| Alias URL | Canonical `duplicate_of` |", "|---|---|"])
        for it in duplicates[:50]:
            lines.append(f"| {it.url} | {it.duplicate_of} |")
        if len(duplicates) > 50:
            lines.append(f"\n_Showing first 50 of {len(duplicates)} duplicates._")
    else:
        lines.append("_None detected._")

    broken = [it for it in envelope.items if it.link_status == LinkStatus.BROKEN]
    lines.extend(["", f"## Broken Internal Links ({len(broken)})", ""])
    if broken:
        lines.extend(["| URL | HTTP Status | Parent URL |", "|---|---:|---|"])
        for it in broken[:50]:
            lines.append(f"| {it.url} | {it.http_status or '-'} | {it.parent_url or '-'} |")
        if len(broken) > 50:
            lines.append(f"\n_Showing first 50 of {len(broken)} broken links._")
    else:
        lines.append("_None detected._")

    lines.extend(["", "## Runtime Bounds & Errors", ""])
    lines.append(f"- **Configured bounds:** `{json.dumps(envelope.bounds, sort_keys=True)}`")
    lines.append(
        f"- **Bounds reached:** {', '.join(f'`{b}`' for b in bounds_hit) if bounds_hit else 'none'}"
    )
    if errors:
        lines.append("- **Discovery warnings / errors:**")
        for err in errors[:30]:
            lines.append(f"  - {err}")
    else:
        lines.append("- **Discovery warnings / errors:** none")

    lines.extend(
        [
            "",
            "## Next Step — Gate A Human Review",
            "",
            "1. Inspect `data/discovery/url_inventory.csv` and "
            "`data/discovery/crawl_scope.proposed.yaml`.",
            "2. Approve URLs (`uv run pdn-sync scope approve --url ...`), promote rotating "
            "patterns (`uv run pdn-sync scope approve --pattern-id ...`), or edit `disposition` "
            "in `url_inventory.csv` and import via "
            "`uv run pdn-sync scope import data/discovery/url_inventory.csv --dry-run`.",
            "3. Verify `config/crawl_scope.yaml` via `uv run pdn-sync scope validate`.",
            "",
        ]
    )

    atomic_write_text(path, "\n".join(lines))
    return path


def export_all_discovery_artifacts(
    *,
    envelope: InventoryEnvelope,
    proposed_patterns: list[ProposedPatternRule],
    bounds_hit: list[str],
    errors: list[str],
    output_dir: Path,
) -> dict[str, Path]:
    """Atomically write all 4 Phase 1 discovery deliverables."""
    return {
        "url_inventory_json": export_inventory_json(envelope, output_dir),
        "url_inventory_csv": export_inventory_csv(envelope.items, output_dir),
        "discovery_report_md": export_discovery_report_md(
            envelope=envelope,
            proposed_patterns=proposed_patterns,
            bounds_hit=bounds_hit,
            errors=errors,
            output_dir=output_dir,
        ),
        "crawl_scope_proposed_yaml": export_proposed_scope_yaml(
            run_id=envelope.run_id,
            scope_file_hash=envelope.scope_file_hash,
            items=envelope.items,
            proposed_patterns=proposed_patterns,
            output_dir=output_dir,
        ),
    }
