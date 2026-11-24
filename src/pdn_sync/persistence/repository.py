"""SQLite control-plane repository for discovery runs, scope cache, and Gate A audit trail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, desc, func, select
from sqlalchemy.orm import Session

from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryItem,
    LinkStatus,
    RecommendedAction,
)
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    CompiledScopeCacheRow,
    CrawlResourceRow,
    DiscoveredUrlRow,
    ScopeDecisionRow,
    SyncRunRow,
)


@dataclass(frozen=True, slots=True)
class InventoryFilter:
    resource_type: ResourceType | None = None
    category: DiscoveryCategory | None = None
    disposition: Disposition | None = None
    duplicates_only: bool = False
    broken_only: bool = False
    search: str | None = None
    limit: int = 200


class ControlPlaneRepository:
    """Single-writer repository over the Phase 1 SQLite control-plane tables."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    def sync_compiled_scope_cache(
        self,
        *,
        scope: CrawlScopeConfig,
        scope_file_hash: str,
    ) -> None:
        """Rebuild or verify compiled_scope_cache from config/crawl_scope.yaml."""
        compiled_json = scope.model_dump_json()
        now = datetime.now(UTC)
        with self._session_factory() as session:
            existing = session.scalar(
                select(CompiledScopeCacheRow).where(
                    CompiledScopeCacheRow.scope_file_hash == scope_file_hash
                )
            )
            if existing is None:
                session.add(
                    CompiledScopeCacheRow(
                        scope_file_hash=scope_file_hash,
                        compiled_at_utc=now,
                        compiled_json=compiled_json,
                    )
                )
                session.commit()

    def get_previous_completed_discovery_count(self) -> int | None:
        """Return candidate item count from the most recent completed discovery run."""
        with self._session_factory() as session:
            prev_run = session.scalar(
                select(SyncRunRow)
                .where(
                    SyncRunRow.profile == "discovery_audit",
                    SyncRunRow.status == "completed",
                )
                .order_by(desc(SyncRunRow.started_at_utc))
                .limit(1)
            )
            if prev_run is None:
                return None
            count = session.scalar(
                select(func.count(DiscoveredUrlRow.id)).where(
                    DiscoveredUrlRow.run_id == prev_run.run_id
                )
            )
            return int(count or 0)

    def create_sync_run(
        self,
        *,
        run_id: str,
        profile: str,
        scope_file_hash: str,
        started_at_utc: datetime,
        bounds: dict[str, Any],
    ) -> None:
        with self._session_factory() as session:
            session.add(
                SyncRunRow(
                    run_id=run_id,
                    profile=profile,
                    status="running",
                    scope_file_hash=scope_file_hash,
                    started_at_utc=started_at_utc,
                    finished_at_utc=None,
                    bounds_json=json.dumps(bounds, sort_keys=True),
                    summary_counts_json="{}",
                    error_summary=None,
                )
            )
            session.commit()

    def save_discovery_results(
        self,
        *,
        run_id: str,
        status: str,
        finished_at_utc: datetime,
        summary_counts: dict[str, Any],
        items: list[InventoryItem],
        content_hashes: dict[str, str | None],
        k2_item_ids: dict[str, str | None],
        error_summary: str | None = None,
    ) -> None:
        """Persist discovered_urls, upsert crawl_resources, and finalize sync_runs atomically."""
        with self._session_factory() as session:
            run_row = session.get(SyncRunRow, run_id)
            if run_row is None:
                msg = f"Unknown sync_run: {run_id}"
                raise ValueError(msg)

            run_row.status = status
            run_row.finished_at_utc = finished_at_utc
            run_row.summary_counts_json = json.dumps(summary_counts, sort_keys=True)
            run_row.error_summary = error_summary

            for item in items:
                session.add(
                    DiscoveredUrlRow(
                        run_id=run_id,
                        canonical_key=item.canonical_key,
                        url=item.url,
                        original_url=item.original_url,
                        redirect_chain_json=json.dumps(item.redirect_chain),
                        http_status=item.http_status,
                        link_status=item.link_status.value,
                        duplicate_of=item.duplicate_of,
                        parent_url=item.parent_url,
                        title_or_alt=item.title_or_alt,
                        discovered_at_utc=item.discovered_at_utc,
                        depth=item.depth,
                        resource_type=item.resource_type.value,
                        mime_hint=item.mime_hint,
                        content_length_bytes=item.content_length_bytes,
                        normalized_content_sha256=content_hashes.get(item.canonical_key),
                        k2_item_id=k2_item_ids.get(item.canonical_key),
                        category=item.category.value,
                        discovery_method=item.discovery_method,
                        disposition=item.disposition.value,
                        recommended_action=item.recommended_action.value,
                        reason=item.reason,
                        policy_rule_id=item.policy_rule_id,
                        monitoring_interval=item.monitoring_interval,
                    )
                )

                if item.resource_type != ResourceType.EXTERNAL_REF:
                    self._upsert_crawl_resource(session, item)

            session.commit()

    def _upsert_crawl_resource(self, session: Session, item: InventoryItem) -> None:
        existing = session.scalar(
            select(CrawlResourceRow).where(CrawlResourceRow.canonical_key == item.canonical_key)
        )
        if existing is None:
            session.add(
                CrawlResourceRow(
                    resource_id=f"res_{item.canonical_key[:16]}",
                    canonical_key=item.canonical_key,
                    url=item.url,
                    resource_type=item.resource_type.value,
                    category=item.category.value,
                    first_seen_at_utc=item.discovered_at_utc,
                    last_seen_at_utc=item.discovered_at_utc,
                    lifecycle_state="linked",
                    missing_consecutive_runs=0,
                )
            )
        else:
            existing.url = item.url
            existing.resource_type = item.resource_type.value
            existing.category = item.category.value
            existing.last_seen_at_utc = item.discovered_at_utc
            existing.lifecycle_state = "linked"
            existing.missing_consecutive_runs = 0

    def get_latest_discovery_run(self) -> SyncRunRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(SyncRunRow)
                .where(SyncRunRow.profile == "discovery_audit")
                .order_by(desc(SyncRunRow.started_at_utc))
                .limit(1)
            )

    def list_inventory_items(
        self,
        *,
        run_id: str | None = None,
        flt: InventoryFilter | None = None,
    ) -> tuple[SyncRunRow | None, list[InventoryItem]]:
        effective_filter = flt or InventoryFilter()
        with self._session_factory() as session:
            if run_id is None:
                run_row = session.scalar(
                    select(SyncRunRow)
                    .where(SyncRunRow.profile == "discovery_audit")
                    .order_by(desc(SyncRunRow.started_at_utc))
                    .limit(1)
                )
            else:
                run_row = session.get(SyncRunRow, run_id)

            if run_row is None:
                return None, []

            stmt = select(DiscoveredUrlRow).where(DiscoveredUrlRow.run_id == run_row.run_id)
            if effective_filter.resource_type is not None:
                stmt = stmt.where(
                    DiscoveredUrlRow.resource_type == effective_filter.resource_type.value
                )
            if effective_filter.category is not None:
                stmt = stmt.where(DiscoveredUrlRow.category == effective_filter.category.value)
            if effective_filter.disposition is not None:
                stmt = stmt.where(
                    DiscoveredUrlRow.disposition == effective_filter.disposition.value
                )
            if effective_filter.duplicates_only:
                stmt = stmt.where(DiscoveredUrlRow.duplicate_of.is_not(None))
            if effective_filter.broken_only:
                stmt = stmt.where(DiscoveredUrlRow.link_status == LinkStatus.BROKEN.value)
            if effective_filter.search:
                pattern = f"%{effective_filter.search.lower()}%"
                stmt = stmt.where(
                    func.lower(DiscoveredUrlRow.url).like(pattern)
                    | func.lower(func.coalesce(DiscoveredUrlRow.title_or_alt, "")).like(pattern)
                )

            stmt = stmt.order_by(DiscoveredUrlRow.depth, DiscoveredUrlRow.url)
            if effective_filter.limit > 0:
                stmt = stmt.limit(effective_filter.limit)

            rows = list(session.scalars(stmt).all())
            return run_row, [self._row_to_item(r) for r in rows]

    def record_scope_decision(
        self,
        *,
        reviewer: str,
        action: str,
        target: str,
        resource_type: str | None,
        previous_disposition: str | None,
        new_disposition: str,
        reason: str,
        notes: str | None,
        scope_file_hash_before: str,
        scope_file_hash_after: str,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                ScopeDecisionRow(
                    decided_at_utc=datetime.now(UTC),
                    reviewer=reviewer,
                    action=action,
                    target=target,
                    resource_type=resource_type,
                    previous_disposition=previous_disposition,
                    new_disposition=new_disposition,
                    reason=reason,
                    notes=notes,
                    scope_file_hash_before=scope_file_hash_before,
                    scope_file_hash_after=scope_file_hash_after,
                )
            )
            session.commit()

    def list_scope_decisions(self, *, limit: int = 100) -> list[ScopeDecisionRow]:
        with self._session_factory() as session:
            stmt = select(ScopeDecisionRow).order_by(desc(ScopeDecisionRow.id)).limit(limit)
            return list(session.scalars(stmt).all())

    @staticmethod
    def _row_to_item(row: DiscoveredUrlRow) -> InventoryItem:
        return InventoryItem(
            run_id=row.run_id,
            canonical_key=row.canonical_key,
            url=row.url,
            original_url=row.original_url,
            redirect_chain=json.loads(row.redirect_chain_json),
            http_status=row.http_status,
            link_status=LinkStatus(row.link_status),
            duplicate_of=row.duplicate_of,
            parent_url=row.parent_url,
            title_or_alt=row.title_or_alt,
            discovered_at_utc=row.discovered_at_utc,
            depth=row.depth,
            resource_type=ResourceType(row.resource_type),
            mime_hint=row.mime_hint,
            content_length_bytes=row.content_length_bytes,
            category=DiscoveryCategory(row.category),
            discovery_method=row.discovery_method,
            disposition=Disposition(row.disposition),
            recommended_action=RecommendedAction(row.recommended_action),
            reason=row.reason,
            policy_rule_id=row.policy_rule_id,
            monitoring_interval=row.monitoring_interval,
        )
