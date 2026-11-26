"""Approved-only Phase 3 publication orchestrator (SQLite + FTS5 + Qdrant + lifecycle)."""

from __future__ import annotations

import fnmatch
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pdn_sync.config import AppSettings, load_crawl_scope, load_ingestion_policy
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType, SupersessionPolicy
from pdn_sync.domain.scope import ApprovalPatternRule, CrawlScopeConfig
from pdn_sync.extractors.contracts import ExtractionResult
from pdn_sync.indexing.chunker import build_chunks_for_artifact
from pdn_sync.indexing.embedder import EmbeddingBackend, create_embedder
from pdn_sync.indexing.qdrant_store import QdrantChunkPoint, QdrantVectorStore
from pdn_sync.ops.retry import RetryExhaustedError, execute_with_retry
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.publication_repo import (
    PublicationRepository,
    RetirementReconcileResult,
    StaleVersionPublishError,
)
from pdn_sync.persistence.schedule_repo import ScheduleRepository
from pdn_sync.persistence.staging_repo import (
    GateBPublishBlockedError,
    StageQueueFilter,
    StagingRepository,
)


@dataclass(slots=True)
class PublishRunSummary:
    """Aggregated outcome of an approved-only publication pass."""

    dry_run: bool
    embedding_model: str
    qdrant_collection: str
    considered: int = 0
    published: int = 0
    skipped_idempotent: int = 0
    skipped_stale: int = 0
    blocked: int = 0
    chunks_upserted: int = 0
    tables_published: int = 0
    schedule_sessions_published: int = 0
    superseded_publications: int = 0
    pending_change_updates: int = 0
    outbox_enqueued: int = 0
    published_ids: list[str] = field(default_factory=list)
    blocked_details: list[tuple[str, str]] = field(default_factory=list)
    retirement: RetirementReconcileResult | None = None


def _matches_pattern_rule(url: str, rule: ApprovalPatternRule) -> bool:
    return fnmatch.fnmatch(urlsplit(url).path, rule.pattern)


def resolve_latest_wins_rule_id(
    *,
    source_url: str,
    resource_type: ResourceType,
    recorded_policy_rule_id: str | None,
    scope: CrawlScopeConfig,
) -> str | None:
    """Return the pattern rule ID if this resource belongs to a supersession=latest_wins rule."""
    if resource_type == ResourceType.PAGE:
        rules = scope.approvals.pages.patterns
    elif resource_type == ResourceType.IMAGE_POSTER:
        rules = scope.approvals.image_posters.patterns
    elif resource_type == ResourceType.DOCUMENT:
        rules = scope.approvals.documents.patterns
    else:
        return None

    for rule in rules:
        if rule.supersession != SupersessionPolicy.LATEST_WINS:
            continue
        if recorded_policy_rule_id and rule.id == recorded_policy_rule_id:
            return rule.id
        if _matches_pattern_rule(source_url, rule):
            return rule.id
    return None


class PublicationService:
    """Single-writer publisher syncing Gate-B-approved artifacts to SQLite, FTS5, and Qdrant."""

    def __init__(
        self,
        *,
        sqlite_path: Path,
        scope_path: Path,
        policy_path: Path,
        qdrant_store: QdrantVectorStore | None = None,
        embedder: EmbeddingBackend | None = None,
        settings: AppSettings | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.scope_path = scope_path
        self.policy_path = policy_path
        self.settings = settings or AppSettings()
        self._external_qdrant = qdrant_store
        self._external_embedder = embedder
        self._sleep_fn = sleep_fn
        upgrade_database(sqlite_path)

    def _open_qdrant(self, collection_name: str) -> tuple[QdrantVectorStore, bool]:
        if self._external_qdrant is not None:
            return self._external_qdrant, False
        store = QdrantVectorStore(
            collection_name=collection_name,
            url=self.settings.qdrant_url,
            path=self.settings.qdrant_path,
        )
        return store, True

    def publish_approved(
        self,
        *,
        dry_run: bool = False,
        artifact_ids: Sequence[str] | None = None,
        embedding_model: str | None = None,
        qdrant_collection: str | None = None,
        reconcile_retirement: bool = False,
        raise_on_blocked: bool = False,
        raise_on_stale: bool = False,
        progress_cb: Callable[[str], None] | None = None,
    ) -> PublishRunSummary:
        """Publish Gate-B-approved artifacts idempotently into SQLite + FTS5 + Qdrant."""
        scope, _ = load_crawl_scope(self.scope_path)
        policy = load_ingestion_policy(self.policy_path)
        model_name = embedding_model or policy.indexing.embedding_model
        collection = qdrant_collection or self.settings.qdrant_collection

        engine = create_sqlite_engine(self.sqlite_path)
        stage_repo = StagingRepository(engine)
        pub_repo = PublicationRepository(engine)
        sched_repo = ScheduleRepository(engine)
        ops_repo = OpsRepository(engine)
        corr_id = f"pubrun_{uuid.uuid4().hex[:12]}"

        summary = PublishRunSummary(
            dry_run=dry_run,
            embedding_model=model_name,
            qdrant_collection=collection,
        )

        try:
            if dry_run and artifact_ids is None:
                all_rows = stage_repo.list_staged_artifacts(flt=StageQueueFilter(limit=0))
                summary.considered = len(all_rows)
                for row in all_rows:
                    try:
                        stage_repo.assert_artifact_approved_for_publish(row.artifact_id)
                        summary.published += 1
                        summary.published_ids.append(row.artifact_id)
                    except GateBPublishBlockedError as exc:
                        summary.blocked += 1
                        summary.blocked_details.append((row.artifact_id, str(exc)))
                return summary

            candidates = pub_repo.list_publishable_artifact_ids(artifact_ids=artifact_ids)
            summary.considered = len(candidates)

            if dry_run:
                for aid in candidates:
                    try:
                        stage_repo.assert_artifact_approved_for_publish(aid)
                        summary.published += 1
                        summary.published_ids.append(aid)
                    except GateBPublishBlockedError as exc:
                        summary.blocked += 1
                        summary.blocked_details.append((aid, str(exc)))
                        if raise_on_blocked:
                            raise
                return summary

            embedder: EmbeddingBackend | None = self._external_embedder
            qdrant, should_close_qdrant = self._open_qdrant(collection)
            collection_ready = False
            try:
                for idx, aid in enumerate(candidates, start=1):
                    try:
                        art_row = stage_repo.assert_artifact_approved_for_publish(aid)
                    except GateBPublishBlockedError as exc:
                        summary.blocked += 1
                        summary.blocked_details.append((aid, str(exc)))
                        if raise_on_blocked:
                            raise
                        continue

                    active_model_name = embedder.model_name if embedder is not None else model_name
                    # Fast-path idempotency check before loading JSON or computing embeddings
                    existing_pub = pub_repo.get_publication_by_artifact(aid)
                    if (
                        existing_pub is not None
                        and existing_pub.status == "published"
                        and existing_pub.embedding_model == active_model_name
                        and existing_pub.qdrant_collection == collection
                    ):
                        summary.skipped_idempotent += 1
                        continue

                    staged = ExtractionResult.model_validate_json(
                        Path(art_row.json_path).read_text(encoding="utf-8")
                    )
                    chunking = build_chunks_for_artifact(staged, policy.indexing)
                    latest_wins_rule = resolve_latest_wins_rule_id(
                        source_url=staged.source_url,
                        resource_type=staged.resource_type,
                        recorded_policy_rule_id=staged.policy_rule_id,
                        scope=scope,
                    )

                    # Lazily load the embedding model ONLY if there are chunks to embed
                    # (so pure structured JEvents schedule publishes stay lightweight & fast!)
                    vectors: list[list[float]] = []
                    if chunking.chunks:
                        if embedder is None:
                            embedder = create_embedder(model_name)
                        vectors = embedder.embed_documents([c.text for c in chunking.chunks])

                    effective_model_name = (
                        embedder.model_name if embedder is not None else model_name
                    )

                    now = datetime.now(UTC)
                    asset_url = (
                        staged.source_url
                        if staged.resource_type == ResourceType.IMAGE_POSTER
                        else None
                    )
                    try:
                        outcome = pub_repo.publish_artifact_atomic(
                            artifact_id=aid,
                            title=staged.title,
                            language=chunking.document_language,
                            content_type=staged.content_type,
                            asset_url=asset_url,
                            chunks=chunking.chunks,
                            tables=staged.tables,
                            embedding_model=effective_model_name,
                            qdrant_collection=collection,
                            supersede_policy_rule_id=latest_wins_rule,
                            now_utc=now,
                            raise_on_stale=raise_on_stale,
                        )
                    except StaleVersionPublishError:
                        summary.skipped_stale += 1
                        if raise_on_stale:
                            raise
                        continue

                    if outcome.action == "skipped_idempotent":
                        summary.skipped_idempotent += 1
                        continue
                    if outcome.action == "skipped_stale_version":
                        summary.skipped_stale += 1
                        continue

                    # Phase 4: Reconcile approved blood donation schedules into SQLite + FTS5
                    if (
                        staged.category == DiscoveryCategory.DONATION_SCHEDULE
                        or staged.structured_events
                    ):
                        sched_out = sched_repo.publish_schedule_artifact_atomic(
                            artifact_id=aid,
                            publication_id=outcome.publication_id,
                            staged=staged,
                            now_utc=now,
                        )
                        summary.schedule_sessions_published += len(sched_out.session_ids)

                    # Upsert approved chunk vectors into Qdrant with full AGENTS.md payload
                    if chunking.chunks:
                        last_verified_iso = now.isoformat()
                        valid_from_iso = staged.captured_at_utc.isoformat()
                        points: list[QdrantChunkPoint] = []
                        for ch, vec in zip(chunking.chunks, vectors, strict=True):
                            payload = {
                                "chunk_id": ch.chunk_id,
                                "document_id": outcome.doc_id,
                                "publication_id": outcome.publication_id,
                                "resource_id": staged.resource_id,
                                "resource_version_id": staged.resource_version_id,
                                "artifact_id": staged.artifact_id,
                                "source_url": staged.source_url,
                                "asset_url": asset_url,
                                "title": staged.title,
                                "section": ch.section,
                                "category": staged.category.value,
                                "language": ch.language,
                                "content_type": staged.content_type,
                                "last_verified_at": last_verified_iso,
                                "source_effective_date": staged.source_effective_date,
                                "valid_from": valid_from_iso,
                                "valid_until": None,
                                "pending_change": False,
                                "status": "published",
                                "text": ch.text,
                            }
                            points.append(
                                QdrantChunkPoint(
                                    point_id=ch.point_id,
                                    vector=vec,
                                    payload=payload,
                                )
                            )
                        try:
                            if not collection_ready and embedder is not None:
                                dim_val = embedder.dimension

                                def _ensure_col(dim: int = dim_val) -> None:
                                    qdrant.ensure_collection(dim)

                                execute_with_retry(
                                    _ensure_col,
                                    task_type="qdrant_ensure_collection",
                                    policy=policy.retry,
                                    ops_repo=ops_repo,
                                    resource_id=staged.resource_id,
                                    version_id=staged.resource_version_id,
                                    correlation_id=corr_id,
                                    sleep_fn=self._sleep_fn,
                                )
                                collection_ready = True

                            def _upsert_pts(pts: list[QdrantChunkPoint] = points) -> int:
                                return qdrant.upsert_points(pts)

                            upserted = execute_with_retry(
                                _upsert_pts,
                                task_type="publish_qdrant_upsert",
                                policy=policy.retry,
                                ops_repo=ops_repo,
                                resource_id=staged.resource_id,
                                version_id=staged.resource_version_id,
                                correlation_id=corr_id,
                                sleep_fn=self._sleep_fn,
                            )
                            summary.chunks_upserted += upserted
                            pub_repo.mark_chunks_qdrant_synced(outcome.publication_id, now)
                        except RetryExhaustedError as exc:
                            # SQLite published state stays intact; defer Qdrant sync via outbox
                            ops_repo.enqueue_outbox_event(
                                event_type="qdrant_upsert_publication",
                                dedupe_key=f"qdrant_upsert:{outcome.publication_id}",
                                payload={
                                    "publication_id": outcome.publication_id,
                                    "qdrant_collection": collection,
                                },
                                correlation_id=corr_id,
                                resource_id=staged.resource_id,
                                artifact_id=staged.artifact_id,
                                publication_id=outcome.publication_id,
                                max_attempts=policy.retry.max_attempts,
                                initial_status="failed",
                                initial_error=str(exc),
                                initial_attempt_count=1,
                                now_utc=now,
                            )
                            summary.outbox_enqueued += 1

                    # Deactivate any superseded prior publications in Qdrant
                    now_iso = now.isoformat()
                    for old_pub_id in outcome.superseded_publication_ids:
                        try:

                            def _mark_superseded(pid: str = old_pub_id, vu: str = now_iso) -> None:
                                qdrant.mark_publication_status(
                                    pid,
                                    status="superseded",
                                    valid_until=vu,
                                )

                            execute_with_retry(
                                _mark_superseded,
                                task_type="publish_qdrant_supersede",
                                policy=policy.retry,
                                ops_repo=ops_repo,
                                resource_id=staged.resource_id,
                                correlation_id=corr_id,
                                sleep_fn=self._sleep_fn,
                            )
                            summary.superseded_publications += 1
                        except RetryExhaustedError as exc:
                            ops_repo.enqueue_outbox_event(
                                event_type="qdrant_retire_publication",
                                dedupe_key=f"qdrant_retire:{old_pub_id}:superseded",
                                payload={
                                    "publication_id": old_pub_id,
                                    "status": "superseded",
                                    "valid_until": now_iso,
                                },
                                correlation_id=corr_id,
                                resource_id=staged.resource_id,
                                publication_id=old_pub_id,
                                max_attempts=policy.retry.max_attempts,
                                initial_status="failed",
                                initial_error=str(exc),
                                initial_attempt_count=1,
                                now_utc=now,
                            )
                            summary.outbox_enqueued += 1

                    summary.published += 1
                    summary.tables_published += len(outcome.table_ids)
                    summary.published_ids.append(aid)

                    if progress_cb and (idx % 25 == 0 or idx == len(candidates)):
                        progress_cb(
                            f"[{idx}/{len(candidates)}] published={summary.published} "
                            f"chunks={summary.chunks_upserted} tables={summary.tables_published}"
                        )

                # Sync pending_change flags across SQLite, schedules, and Qdrant
                pending_updates = pub_repo.sync_pending_change_flags()
                for pub_id, is_pending in pending_updates:
                    qdrant.update_pending_change(pub_id, pending_change=is_pending)
                sched_pending = sched_repo.sync_schedule_pending_change_flags()
                summary.pending_change_updates = len(pending_updates) + sched_pending

                # Optional retirement reconciliation (linked -> no_longer_linked -> retired)
                if reconcile_retirement:
                    ret = pub_repo.reconcile_retirements(
                        consecutive_threshold=policy.retirement_consecutive_runs
                    )
                    ret_iso = datetime.now(UTC).isoformat()
                    for pub_id in ret.retired_publication_ids:
                        qdrant.mark_publication_status(
                            pub_id,
                            status="retired",
                            valid_until=ret_iso,
                        )
                    summary.retirement = ret

                return summary
            finally:
                if should_close_qdrant:
                    qdrant.close()
        finally:
            engine.dispose()

    def reconcile_retirement(
        self,
        *,
        run_id: str | None = None,
        qdrant_collection: str | None = None,
    ) -> RetirementReconcileResult:
        """Run lifecycle retirement reconciliation and mirror retired status into Qdrant."""
        policy = load_ingestion_policy(self.policy_path)
        collection = qdrant_collection or self.settings.qdrant_collection
        engine = create_sqlite_engine(self.sqlite_path)
        pub_repo = PublicationRepository(engine)
        try:
            ret = pub_repo.reconcile_retirements(
                run_id=run_id,
                consecutive_threshold=policy.retirement_consecutive_runs,
            )
            if ret.retired_publication_ids:
                qdrant, should_close = self._open_qdrant(collection)
                try:
                    ret_iso = datetime.now(UTC).isoformat()
                    for pub_id in ret.retired_publication_ids:
                        qdrant.mark_publication_status(
                            pub_id,
                            status="retired",
                            valid_until=ret_iso,
                        )
                finally:
                    if should_close:
                        qdrant.close()
            return ret
        finally:
            engine.dispose()
