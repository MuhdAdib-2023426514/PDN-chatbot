"""Gate A scope manager: comment-preserving YAML updates, CSV import, and SQLite audit trail."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from pdn_sync.config import compute_file_sha256, load_crawl_scope
from pdn_sync.crawling.url_policy import is_in_allowed_scope, is_jevents_url, normalize_url
from pdn_sync.discovery.exporter import (
    atomic_write_text,
    compute_summary_counts,
    export_discovery_report_md,
    export_inventory_csv,
    export_inventory_json,
)
from pdn_sync.discovery.models import InventoryEnvelope, InventoryItem
from pdn_sync.discovery.patterns import propose_rotating_asset_patterns
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig, evaluate_scope
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.repository import ControlPlaneRepository, InventoryFilter


class ScopeMutationError(ValueError):
    """Raised when a Gate A mutation violates scope, JEvents, or schema safety rules."""


class GateABlockedError(PermissionError):
    """Raised when content crawling targets a URL not approved in config/crawl_scope.yaml."""


@dataclass(slots=True)
class CsvImportSummary:
    dry_run: bool
    added_approvals: list[str] = field(default_factory=list)
    added_exclusions: list[str] = field(default_factory=list)
    skipped_unchanged: int = 0
    scope_file_hash_before: str = ""
    scope_file_hash_after: str = ""


def _section_key_for_resource_type(resource_type: ResourceType) -> str:
    if resource_type == ResourceType.PAGE:
        return "pages"
    if resource_type == ResourceType.IMAGE_POSTER:
        return "image_posters"
    if resource_type == ResourceType.DOCUMENT:
        return "documents"
    raise ScopeMutationError(f"Cannot approve resource_type={resource_type.value!r}")


def _make_roundtrip_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _dump_and_validate_rt_yaml(yaml: YAML, doc: Any) -> tuple[str, CrawlScopeConfig]:
    """Serialize round-trip YAML and validate against CrawlScopeConfig (fail closed)."""
    buf = io.StringIO()
    yaml.dump(doc, buf)
    rendered = buf.getvalue()

    safe_yaml = YAML(typ="safe")
    parsed = safe_yaml.load(rendered)
    try:
        validated = CrawlScopeConfig.model_validate(parsed)
    except ValidationError as exc:
        raise ScopeMutationError(f"Resulting crawl_scope.yaml failed validation:\n{exc}") from exc
    return rendered, validated


def assert_url_approved_for_content_crawl(
    raw_url: str,
    *,
    scope_path: Path,
    resource_type: ResourceType | None = None,
) -> str:
    """Gate A guard: allow ONLY URLs explicitly approved in config/crawl_scope.yaml."""
    scope, _ = load_crawl_scope(scope_path)
    evaluation = evaluate_scope(raw_url, scope, resource_type=resource_type)
    if evaluation.disposition != Disposition.APPROVED or evaluation.normalized_url is None:
        raise GateABlockedError(
            f"Gate A blocked content fetch for {raw_url!r}: "
            f"disposition={evaluation.disposition.value} ({evaluation.reason})"
        )
    return evaluation.normalized_url


class ScopeManager:
    """Manages comment-preserving Gate A updates to config/crawl_scope.yaml + SQLite audit log."""

    def __init__(self, *, scope_path: Path, sqlite_path: Path) -> None:
        self.scope_path = scope_path
        self.sqlite_path = sqlite_path
        upgrade_database(sqlite_path)

    def _open_rt_doc(self) -> tuple[YAML, Any, CrawlScopeConfig, str]:
        scope_cfg, hash_before = load_crawl_scope(self.scope_path)
        yaml = _make_roundtrip_yaml()
        doc = yaml.load(self.scope_path.read_text(encoding="utf-8"))
        return yaml, doc, scope_cfg, hash_before

    def _commit_doc(
        self,
        *,
        yaml: YAML,
        doc: Any,
        hash_before: str,
        audit_rows: list[dict[str, Any]],
    ) -> str:
        rendered, validated_cfg = _dump_and_validate_rt_yaml(yaml, doc)
        atomic_write_text(self.scope_path, rendered)
        hash_after = compute_file_sha256(self.scope_path)

        engine = create_sqlite_engine(self.sqlite_path)
        try:
            repo = ControlPlaneRepository(engine)
            repo.sync_compiled_scope_cache(scope=validated_cfg, scope_file_hash=hash_after)
            for row in audit_rows:
                repo.record_scope_decision(
                    reviewer=str(row["reviewer"]),
                    action=str(row["action"]),
                    target=str(row["target"]),
                    resource_type=row.get("resource_type"),
                    previous_disposition=row.get("previous_disposition"),
                    new_disposition=str(row["new_disposition"]),
                    reason=str(row["reason"]),
                    notes=row.get("notes"),
                    scope_file_hash_before=hash_before,
                    scope_file_hash_after=hash_after,
                )
        finally:
            engine.dispose()

        return hash_after

    def approve_url(
        self,
        *,
        raw_url: str,
        resource_type: ResourceType,
        reason: str,
        reviewer: str,
        check_interval: str = "24h",
        notes: str | None = None,
    ) -> str:
        yaml, doc, scope_cfg, hash_before = self._open_rt_doc()
        normalized = normalize_url(
            raw_url,
            allowed_host=scope_cfg.allowed_host,
            allowed_path_prefix=scope_cfg.allowed_path_prefix,
        )
        if not is_in_allowed_scope(
            normalized,
            allowed_host=scope_cfg.allowed_host,
            allowed_path_prefix=scope_cfg.allowed_path_prefix,
        ):
            raise ScopeMutationError(f"URL is outside allowed host/prefix: {normalized!r}")
        if is_jevents_url(normalized):
            raise ScopeMutationError(f"Cannot approve JEvents URL in Phases 0-3: {normalized!r}")

        current_eval = evaluate_scope(normalized, scope_cfg, resource_type=resource_type)
        if current_eval.disposition == Disposition.EXCLUDED:
            raise ScopeMutationError(
                f"URL {normalized!r} matches an active exclusion ({current_eval.reason}); "
                "exclusions always override approvals."
            )

        section_key = _section_key_for_resource_type(resource_type)
        urls_list = doc["approvals"][section_key]["urls"]
        existing_urls = {
            normalize_url(
                entry["url"],
                allowed_host=scope_cfg.allowed_host,
                allowed_path_prefix=scope_cfg.allowed_path_prefix,
            )
            for entry in urls_list
        }
        if normalized in existing_urls:
            return hash_before

        urls_list.append(
            {
                "url": normalized,
                "reason": reason,
                "check_interval": check_interval,
                "approved_by": reviewer,
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
        return self._commit_doc(
            yaml=yaml,
            doc=doc,
            hash_before=hash_before,
            audit_rows=[
                {
                    "reviewer": reviewer,
                    "action": "approve_url",
                    "target": normalized,
                    "resource_type": resource_type.value,
                    "previous_disposition": current_eval.disposition.value,
                    "new_disposition": Disposition.APPROVED.value,
                    "reason": reason,
                    "notes": notes,
                }
            ],
        )

    def approve_pattern_from_proposed(
        self,
        *,
        pattern_id: str,
        proposed_yaml_path: Path,
        reviewer: str,
        reason_override: str | None = None,
    ) -> str:
        if not proposed_yaml_path.is_file():
            raise ScopeMutationError(f"Proposed scope file not found: {proposed_yaml_path}")
        safe_yaml = YAML(typ="safe")
        proposed = safe_yaml.load(proposed_yaml_path.read_text(encoding="utf-8"))
        if not isinstance(proposed, dict):
            raise ScopeMutationError(f"Invalid proposed YAML file: {proposed_yaml_path}")

        found_rule: dict[str, Any] | None = None
        found_section: str | None = None
        approvals_node = proposed.get("proposed_approvals", {})
        for sec_key in ("pages", "image_posters", "documents"):
            for pat in approvals_node.get(sec_key, {}).get("patterns", []):
                if pat.get("id") == pattern_id:
                    found_rule = pat
                    found_section = sec_key
                    break
            if found_rule is not None:
                break

        if found_rule is None or found_section is None:
            raise ScopeMutationError(
                f"Proposed pattern id {pattern_id!r} not found in {proposed_yaml_path}"
            )

        yaml, doc, _scope_cfg, hash_before = self._open_rt_doc()
        patterns_list = doc["approvals"][found_section]["patterns"]
        if any(entry.get("id") == pattern_id for entry in patterns_list):
            return hash_before

        reason = reason_override or str(found_rule["reason"])
        patterns_list.append(
            {
                "id": found_rule["id"],
                "pattern": found_rule["pattern"],
                "resource_type": found_rule["resource_type"],
                "reason": reason,
                "max_matches": int(found_rule["max_matches"]),
                "check_interval": str(found_rule.get("check_interval", "24h")),
                "supersession": str(found_rule.get("supersession", "none")),
                "approved_by": reviewer,
                "approved_at": datetime.now(UTC).isoformat(),
            }
        )
        return self._commit_doc(
            yaml=yaml,
            doc=doc,
            hash_before=hash_before,
            audit_rows=[
                {
                    "reviewer": reviewer,
                    "action": "approve_pattern",
                    "target": f"{found_rule['id']}:{found_rule['pattern']}",
                    "resource_type": str(found_rule["resource_type"]),
                    "previous_disposition": Disposition.PENDING.value,
                    "new_disposition": Disposition.APPROVED.value,
                    "reason": reason,
                    "notes": f"promoted from {proposed_yaml_path.name}",
                }
            ],
        )

    def exclude_url(
        self,
        *,
        raw_url: str,
        reason: str,
        reviewer: str,
        notes: str | None = None,
    ) -> str:
        yaml, doc, scope_cfg, hash_before = self._open_rt_doc()
        normalized = normalize_url(
            raw_url,
            allowed_host=scope_cfg.allowed_host,
            allowed_path_prefix=scope_cfg.allowed_path_prefix,
        )
        current_eval = evaluate_scope(normalized, scope_cfg)
        excl_urls = doc["exclusions"]["urls"]
        existing = {
            normalize_url(
                u,
                allowed_host=scope_cfg.allowed_host,
                allowed_path_prefix=scope_cfg.allowed_path_prefix,
            )
            for u in excl_urls
        }
        if normalized in existing:
            return hash_before

        excl_urls.append(normalized)
        return self._commit_doc(
            yaml=yaml,
            doc=doc,
            hash_before=hash_before,
            audit_rows=[
                {
                    "reviewer": reviewer,
                    "action": "exclude_url",
                    "target": normalized,
                    "resource_type": None,
                    "previous_disposition": current_eval.disposition.value,
                    "new_disposition": Disposition.EXCLUDED.value,
                    "reason": reason,
                    "notes": notes,
                }
            ],
        )

    def exclude_pattern(
        self,
        *,
        rule_id: str,
        pattern: str,
        reason: str,
        reviewer: str,
        notes: str | None = None,
    ) -> str:
        yaml, doc, _scope_cfg, hash_before = self._open_rt_doc()
        excl_patterns = doc["exclusions"]["patterns"]
        if any(entry.get("id") == rule_id for entry in excl_patterns):
            return hash_before

        excl_patterns.append({"id": rule_id, "pattern": pattern, "reason": reason})
        return self._commit_doc(
            yaml=yaml,
            doc=doc,
            hash_before=hash_before,
            audit_rows=[
                {
                    "reviewer": reviewer,
                    "action": "exclude_pattern",
                    "target": f"{rule_id}:{pattern}",
                    "resource_type": None,
                    "previous_disposition": None,
                    "new_disposition": Disposition.EXCLUDED.value,
                    "reason": reason,
                    "notes": notes,
                }
            ],
        )

    def import_csv(
        self,
        *,
        csv_path: Path,
        reviewer: str,
        dry_run: bool = False,
    ) -> CsvImportSummary:
        """Additive merge of human-edited url_inventory.csv into config/crawl_scope.yaml."""
        if not csv_path.is_file():
            raise ScopeMutationError(f"CSV file not found: {csv_path}")

        yaml, doc, scope_cfg, hash_before = self._open_rt_doc()
        summary = CsvImportSummary(
            dry_run=dry_run,
            scope_file_hash_before=hash_before,
            scope_file_hash_after=hash_before,
        )

        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            required_cols = {"url", "resource_type", "disposition"}
            if reader.fieldnames is None or not required_cols.issubset(set(reader.fieldnames)):
                raise ScopeMutationError(
                    f"CSV {csv_path} missing required columns: {sorted(required_cols)}"
                )

            existing_excl_urls = {
                normalize_url(
                    u,
                    allowed_host=scope_cfg.allowed_host,
                    allowed_path_prefix=scope_cfg.allowed_path_prefix,
                )
                for u in doc["exclusions"]["urls"]
            }
            existing_appr_urls: dict[str, set[str]] = {}
            for sec_key in ("pages", "image_posters", "documents"):
                existing_appr_urls[sec_key] = {
                    normalize_url(
                        entry["url"],
                        allowed_host=scope_cfg.allowed_host,
                        allowed_path_prefix=scope_cfg.allowed_path_prefix,
                    )
                    for entry in doc["approvals"][sec_key]["urls"]
                }

            audit_rows: list[dict[str, Any]] = []
            now_iso = datetime.now(UTC).isoformat()

            for row_num, row in enumerate(reader, start=2):
                raw_url = (row.get("url") or "").strip()
                raw_disp = (row.get("disposition") or "").strip().lower()
                raw_type = (row.get("resource_type") or "").strip().lower()
                raw_reason = (row.get("reason") or "").strip() or "csv_scope_import"
                raw_notes = (row.get("notes") or "").strip() or None
                raw_interval = (row.get("monitoring_interval") or "").strip()
                check_interval = raw_interval if raw_interval and raw_interval != "none" else "24h"

                if not raw_url:
                    raise ScopeMutationError(f"CSV row {row_num}: empty url")
                try:
                    disp = Disposition(raw_disp)
                except ValueError as exc:
                    raise ScopeMutationError(
                        f"CSV row {row_num}: invalid disposition {raw_disp!r}"
                    ) from exc
                try:
                    res_type = ResourceType(raw_type)
                except ValueError as exc:
                    raise ScopeMutationError(
                        f"CSV row {row_num}: invalid resource_type {raw_type!r}"
                    ) from exc

                try:
                    normalized = normalize_url(
                        raw_url,
                        allowed_host=scope_cfg.allowed_host,
                        allowed_path_prefix=scope_cfg.allowed_path_prefix,
                    )
                except Exception as exc:
                    raise ScopeMutationError(
                        f"CSV row {row_num}: invalid URL {raw_url!r}: {exc}"
                    ) from exc

                current_eval = evaluate_scope(normalized, scope_cfg, resource_type=res_type)

                if disp == Disposition.PENDING:
                    summary.skipped_unchanged += 1
                    continue

                if disp == Disposition.APPROVED:
                    if not is_in_allowed_scope(
                        normalized,
                        allowed_host=scope_cfg.allowed_host,
                        allowed_path_prefix=scope_cfg.allowed_path_prefix,
                    ):
                        raise ScopeMutationError(
                            f"CSV row {row_num}: cannot approve out-of-scope URL {normalized!r}"
                        )
                    if is_jevents_url(normalized):
                        raise ScopeMutationError(
                            f"CSV row {row_num}: cannot approve JEvents URL {normalized!r}"
                        )
                    if current_eval.disposition == Disposition.EXCLUDED:
                        raise ScopeMutationError(
                            f"CSV row {row_num}: URL {normalized!r} is excluded "
                            f"({current_eval.reason}) and cannot be approved via CSV import"
                        )
                    sec_key = _section_key_for_resource_type(res_type)
                    if (
                        current_eval.disposition == Disposition.APPROVED
                        or normalized in existing_appr_urls[sec_key]
                    ):
                        summary.skipped_unchanged += 1
                        continue
                    existing_appr_urls[sec_key].add(normalized)
                    doc["approvals"][sec_key]["urls"].append(
                        {
                            "url": normalized,
                            "reason": raw_reason,
                            "check_interval": check_interval,
                            "approved_by": reviewer,
                            "approved_at": now_iso,
                        }
                    )
                    summary.added_approvals.append(normalized)
                    audit_rows.append(
                        {
                            "reviewer": reviewer,
                            "action": "import_csv",
                            "target": normalized,
                            "resource_type": res_type.value,
                            "previous_disposition": current_eval.disposition.value,
                            "new_disposition": Disposition.APPROVED.value,
                            "reason": raw_reason,
                            "notes": raw_notes,
                        }
                    )
                elif disp == Disposition.EXCLUDED:
                    if (
                        current_eval.disposition == Disposition.EXCLUDED
                        or normalized in existing_excl_urls
                    ):
                        summary.skipped_unchanged += 1
                        continue
                    existing_excl_urls.add(normalized)
                    doc["exclusions"]["urls"].append(normalized)
                    summary.added_exclusions.append(normalized)
                    audit_rows.append(
                        {
                            "reviewer": reviewer,
                            "action": "import_csv",
                            "target": normalized,
                            "resource_type": res_type.value,
                            "previous_disposition": current_eval.disposition.value,
                            "new_disposition": Disposition.EXCLUDED.value,
                            "reason": raw_reason,
                            "notes": raw_notes,
                        }
                    )

        # Always validate resulting YAML even during --dry-run
        _dump_and_validate_rt_yaml(yaml, doc)

        if not dry_run and (summary.added_approvals or summary.added_exclusions):
            summary.scope_file_hash_after = self._commit_doc(
                yaml=yaml,
                doc=doc,
                hash_before=hash_before,
                audit_rows=audit_rows,
            )

        return summary

    def export_joined_inventory(self, *, output_dir: Path) -> dict[str, Path]:
        """Re-join the latest SQLite discovery inventory with current config/crawl_scope.yaml."""
        scope_cfg, scope_hash = load_crawl_scope(self.scope_path)
        engine = create_sqlite_engine(self.sqlite_path)
        try:
            repo = ControlPlaneRepository(engine)
            repo.sync_compiled_scope_cache(scope=scope_cfg, scope_file_hash=scope_hash)
            run_row, items = repo.list_inventory_items(flt=InventoryFilter(limit=0))
        finally:
            engine.dispose()

        if run_row is None:
            raise ScopeMutationError(
                "No discovery run found in SQLite; run `pdn-sync discover` first."
            )

        rejoined: list[InventoryItem] = []
        for item in items:
            ev = evaluate_scope(item.url, scope_cfg, resource_type=item.resource_type)
            rejoined.append(
                item.model_copy(
                    update={
                        "disposition": ev.disposition,
                        "policy_rule_id": ev.rule_id,
                    }
                )
            )

        summary_counts = compute_summary_counts(rejoined)
        proposed_patterns = propose_rotating_asset_patterns(
            rejoined,
            allowed_path_prefix=scope_cfg.allowed_path_prefix,
        )
        envelope = InventoryEnvelope(
            run_id=run_row.run_id,
            scope_file_hash=scope_hash,
            started_at_utc=run_row.started_at_utc,
            finished_at_utc=run_row.finished_at_utc or run_row.started_at_utc,
            bounds=json.loads(run_row.bounds_json),
            summary_counts=summary_counts,
            items=rejoined,
        )
        return {
            "url_inventory_json": export_inventory_json(envelope, output_dir),
            "url_inventory_csv": export_inventory_csv(rejoined, output_dir),
            "discovery_report_md": export_discovery_report_md(
                envelope=envelope,
                proposed_patterns=proposed_patterns,
                bounds_hit=[],
                errors=[run_row.error_summary] if run_row.error_summary else [],
                output_dir=output_dir,
            ),
        }
