"""Single-writer SQLite repository for Phase 2 fetches, versions, staging, and Gate B review."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, desc, func, select

from pdn_sync.discovery.models import make_canonical_key
from pdn_sync.extractors.contracts import (
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    CrawlResourceRow,
    MediaAssetRow,
    ResourceFetchRow,
    ResourceVersionRow,
    ReviewDecisionRow,
    StagedArtifactRow,
)

APPROVED_DECISION_KINDS: frozenset[str] = frozenset(
    {"approved", "carried_forward", "carried_forward_extractor_upgrade"}
)


class GateBPublishBlockedError(PermissionError):
    """Raised inside the DB/publisher layer when an unapproved or altered artifact is published."""


@dataclass(frozen=True, slots=True)
class StageQueueFilter:
    review_status: ReviewStatus | None = None
    category: str | None = None
    resource_type: str | None = None
    pattern_id: str | None = None
    source_prefix: str | None = None
    critical_only: bool = False
    limit: int = 200


class StagingRepository:
    """Single-writer repository managing Phase 2 fetch provenance, versions, and Gate B review."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    def ensure_crawl_resource(
        self,
        *,
        canonical_url: str,
        resource_type: str,
        category: str,
        now_utc: datetime | None = None,
    ) -> CrawlResourceRow:
        canon_key = make_canonical_key(canonical_url)
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            existing = session.scalar(
                select(CrawlResourceRow).where(CrawlResourceRow.canonical_key == canon_key)
            )
            if existing is not None:
                existing.last_seen_at_utc = now
                existing.category = category
                existing.resource_type = resource_type
                session.commit()
                return existing

            row = CrawlResourceRow(
                resource_id=f"res_{canon_key[:16]}",
                canonical_key=canon_key,
                url=canonical_url,
                resource_type=resource_type,
                category=category,
                first_seen_at_utc=now,
                last_seen_at_utc=now,
                lifecycle_state="linked",
                missing_consecutive_runs=0,
            )
            session.add(row)
            session.commit()
            return row

    def get_latest_successful_fetch(self, resource_id: str) -> ResourceFetchRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(ResourceFetchRow)
                .where(
                    ResourceFetchRow.resource_id == resource_id,
                    ResourceFetchRow.fetch_outcome == "fetched",
                    ResourceFetchRow.blob_sha256.is_not(None),
                )
                .order_by(desc(ResourceFetchRow.fetched_at_utc))
                .limit(1)
            )

    def record_fetch(
        self,
        *,
        run_id: str,
        resource_id: str,
        url: str,
        final_url: str | None,
        redirect_chain: list[str],
        fetched_at_utc: datetime,
        http_status: int | None,
        etag: str | None,
        last_modified: str | None,
        content_type: str | None,
        byte_size: int | None,
        blob_sha256: str | None,
        latency_ms: int | None,
        fetch_outcome: str,
        error_message: str | None = None,
    ) -> ResourceFetchRow:
        fetch_id = f"fch_{uuid.uuid4().hex[:16]}"
        row = ResourceFetchRow(
            fetch_id=fetch_id,
            run_id=run_id,
            resource_id=resource_id,
            url=url,
            final_url=final_url,
            redirect_chain_json=json.dumps(redirect_chain),
            fetched_at_utc=fetched_at_utc,
            http_status=http_status,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            byte_size=byte_size,
            blob_sha256=blob_sha256,
            latency_ms=latency_ms,
            fetch_outcome=fetch_outcome,
            error_message=error_message,
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            return row

    def touch_last_verified(self, resource_id: str, verified_at_utc: datetime) -> None:
        """Update last_verified_at_utc on the current resource version (e.g. HTTP 304)."""
        with self._session_factory() as session:
            current = session.scalar(
                select(ResourceVersionRow)
                .where(
                    ResourceVersionRow.resource_id == resource_id,
                    ResourceVersionRow.state == "current",
                )
                .order_by(desc(ResourceVersionRow.version_seq))
                .limit(1)
            )
            if current is not None:
                current.last_verified_at_utc = verified_at_utc
                session.commit()

    def next_version_seq(self, resource_id: str) -> int:
        with self._session_factory() as session:
            max_seq = session.scalar(
                select(func.max(ResourceVersionRow.version_seq)).where(
                    ResourceVersionRow.resource_id == resource_id
                )
            )
            return int(max_seq or 0) + 1

    def get_latest_approved_artifact(
        self, resource_id: str
    ) -> tuple[StagedArtifactRow, ReviewDecisionRow] | None:
        with self._session_factory() as session:
            artifact = session.scalar(
                select(StagedArtifactRow)
                .where(
                    StagedArtifactRow.resource_id == resource_id,
                    StagedArtifactRow.review_status.in_(
                        [ReviewStatus.APPROVED.value, ReviewStatus.PUBLISHED.value]
                    ),
                )
                .order_by(desc(StagedArtifactRow.created_at_utc))
                .limit(1)
            )
            if artifact is None:
                return None
            decision = session.scalar(
                select(ReviewDecisionRow)
                .where(
                    ReviewDecisionRow.artifact_id == artifact.artifact_id,
                    ReviewDecisionRow.decision.in_(list(APPROVED_DECISION_KINDS)),
                )
                .order_by(desc(ReviewDecisionRow.decided_at_utc))
                .limit(1)
            )
            if decision is None:
                return None
            return artifact, decision

    def get_latest_artifact_for_resource(self, resource_id: str) -> StagedArtifactRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(StagedArtifactRow)
                .where(StagedArtifactRow.resource_id == resource_id)
                .order_by(desc(StagedArtifactRow.created_at_utc))
                .limit(1)
            )

    def get_artifact(self, artifact_id: str) -> StagedArtifactRow | None:
        with self._session_factory() as session:
            return session.get(StagedArtifactRow, artifact_id)

    def save_staged_version(
        self,
        *,
        result: ExtractionResult,
        version_seq: int,
        fetch_id: str | None,
        json_path: Path,
        markdown_path: Path,
        media_meta: dict[str, Any] | None = None,
    ) -> tuple[ResourceVersionRow, StagedArtifactRow]:
        """Atomically persist immutable ResourceVersionRow, StagedArtifactRow, and MediaAssetRow."""
        now = datetime.now(UTC)
        with self._session_factory() as session:
            # Mark prior 'current' version as superseded only if we are creating a newer capture;
            # keep prior versions intact for full audit history.
            prior_current = session.scalars(
                select(ResourceVersionRow).where(
                    ResourceVersionRow.resource_id == result.resource_id,
                    ResourceVersionRow.state == "current",
                )
            ).all()
            for row in prior_current:
                row.state = "superseded"

            ver_row = ResourceVersionRow(
                version_id=result.resource_version_id,
                resource_id=result.resource_id,
                version_seq=version_seq,
                fetch_id=fetch_id,
                content_hash=result.content_hash,
                semantic_hash=result.semantic_hash,
                captured_at_utc=result.captured_at_utc,
                last_verified_at_utc=now,
                source_effective_date=result.source_effective_date,
                source_effective_date_provenance=result.source_effective_date_provenance,
                extractor_name=result.extractor_name,
                extractor_version=result.extractor_version,
                state="current",
            )
            session.add(ver_row)

            art_row = StagedArtifactRow(
                artifact_id=result.artifact_id,
                resource_id=result.resource_id,
                resource_version_id=result.resource_version_id,
                source_url=result.source_url,
                resource_type=result.resource_type.value,
                category=result.category.value,
                policy_rule_id=result.policy_rule_id,
                content_hash=result.content_hash,
                semantic_hash=result.semantic_hash,
                extractor_name=result.extractor_name,
                extractor_version=result.extractor_version,
                json_path=str(json_path),
                markdown_path=str(markdown_path),
                validation_status=result.validation_status.value,
                review_status=result.review_status.value,
                critical_fact_flags_json=json.dumps(result.critical_fact_flags),
                warnings_json=json.dumps(result.warnings),
                confidence=result.confidence,
                source_effective_date=result.source_effective_date,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(art_row)

            if media_meta is not None:
                session.add(
                    MediaAssetRow(
                        asset_id=f"med_{uuid.uuid4().hex[:16]}",
                        resource_id=result.resource_id,
                        blob_sha256=result.content_hash,
                        source_url=result.source_url,
                        mime_type=media_meta.get("mime_type"),
                        byte_size=int(media_meta.get("byte_size", 0)),
                        width=media_meta.get("width"),
                        height=media_meta.get("height"),
                        captured_at_utc=result.captured_at_utc,
                        source_effective_date=result.source_effective_date,
                        usage_notes=media_meta.get("usage_notes"),
                    )
                )

            session.commit()
            return ver_row, art_row

    def record_review_decision(
        self,
        *,
        artifact_id: str,
        reviewer: str,
        decision: str,
        new_review_status: ReviewStatus,
        policy_mode: str,
        notes: str | None = None,
        batch_id: str | None = None,
        carried_from_decision_id: str | None = None,
    ) -> ReviewDecisionRow:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            art = session.get(StagedArtifactRow, artifact_id)
            if art is None:
                msg = f"Unknown staged artifact: {artifact_id}"
                raise ValueError(msg)

            art.review_status = new_review_status.value
            art.updated_at_utc = now

            dec_row = ReviewDecisionRow(
                decision_id=f"dec_{uuid.uuid4().hex[:16]}",
                batch_id=batch_id,
                decided_at_utc=now,
                reviewer=reviewer,
                decision=decision,
                policy_mode=policy_mode,
                artifact_id=art.artifact_id,
                resource_version_id=art.resource_version_id,
                semantic_hash=art.semantic_hash,
                carried_from_decision_id=carried_from_decision_id,
                notes=notes,
            )
            session.add(dec_row)
            session.commit()
            return dec_row

    def list_staged_artifacts(
        self,
        *,
        flt: StageQueueFilter | None = None,
    ) -> list[StagedArtifactRow]:
        """Return staged artifacts ordered by review priority (critical facts -> new -> changed)."""
        f = flt or StageQueueFilter()
        with self._session_factory() as session:
            stmt = select(StagedArtifactRow)
            if f.review_status is not None:
                stmt = stmt.where(StagedArtifactRow.review_status == f.review_status.value)
            if f.category is not None:
                stmt = stmt.where(StagedArtifactRow.category == f.category)
            if f.resource_type is not None:
                stmt = stmt.where(StagedArtifactRow.resource_type == f.resource_type)
            if f.pattern_id is not None:
                stmt = stmt.where(StagedArtifactRow.policy_rule_id == f.pattern_id)
            if f.source_prefix is not None:
                stmt = stmt.where(StagedArtifactRow.source_url.startswith(f.source_prefix))

            rows = list(session.scalars(stmt).all())
            # Count versions per resource to distinguish new (1) vs changed (>1)
            ver_counts: dict[str, int] = dict(
                session.execute(
                    select(
                        ResourceVersionRow.resource_id,
                        func.count(ResourceVersionRow.version_id),
                    ).group_by(ResourceVersionRow.resource_id)
                ).all()
            )

        if f.critical_only:
            rows = [
                r
                for r in rows
                if json.loads(r.critical_fact_flags_json)
                or r.review_status == ReviewStatus.QUARANTINED.value
            ]

        def _priority_key(row: StagedArtifactRow) -> tuple[int, int, str]:
            has_critical = bool(json.loads(row.critical_fact_flags_json))
            is_quarantined = row.review_status == ReviewStatus.QUARANTINED.value
            if has_critical or is_quarantined:
                tier = 0
            elif ver_counts.get(row.resource_id, 1) == 1:
                tier = 1  # new resource
            else:
                tier = 2  # changed resource
            return (tier, -int(row.created_at_utc.timestamp()), row.source_url)

        rows.sort(key=_priority_key)
        if f.limit > 0:
            return rows[: f.limit]
        return rows

    def list_latest_artifacts_by_extractor(self, extractor_prefix: str) -> list[StagedArtifactRow]:
        """Return the latest staged artifact per resource matching an extractor family."""
        with self._session_factory() as session:
            all_rows = list(
                session.scalars(
                    select(StagedArtifactRow)
                    .where(StagedArtifactRow.extractor_name.startswith(extractor_prefix))
                    .order_by(desc(StagedArtifactRow.created_at_utc))
                ).all()
            )
        latest_by_res: dict[str, StagedArtifactRow] = {}
        for row in all_rows:
            if row.resource_id not in latest_by_res:
                latest_by_res[row.resource_id] = row
        return list(latest_by_res.values())

    def list_review_decisions(self, artifact_id: str | None = None) -> list[ReviewDecisionRow]:
        with self._session_factory() as session:
            stmt = select(ReviewDecisionRow).order_by(desc(ReviewDecisionRow.decided_at_utc))
            if artifact_id is not None:
                stmt = stmt.where(ReviewDecisionRow.artifact_id == artifact_id)
            return list(session.scalars(stmt).all())

    def assert_artifact_approved_for_publish(self, artifact_id: str) -> StagedArtifactRow:
        """Publisher-layer Gate B guard: enforce validation, approval, and exact semantic_hash."""
        with self._session_factory() as session:
            art = session.get(StagedArtifactRow, artifact_id)
            if art is None:
                raise GateBPublishBlockedError(f"Artifact not found: {artifact_id}")

            if art.validation_status != ValidationStatus.VALID.value:
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: validation_status={art.validation_status!r}"
                )

            if art.review_status not in {
                ReviewStatus.APPROVED.value,
                ReviewStatus.PUBLISHED.value,
            }:
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: review_status={art.review_status!r} "
                    "(must be 'approved' or 'published')"
                )

            decision = session.scalar(
                select(ReviewDecisionRow)
                .where(
                    ReviewDecisionRow.artifact_id == artifact_id,
                    ReviewDecisionRow.decision.in_(list(APPROVED_DECISION_KINDS)),
                )
                .order_by(desc(ReviewDecisionRow.decided_at_utc))
                .limit(1)
            )
            if decision is None:
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: missing immutable review_decisions record"
                )

            if decision.semantic_hash != art.semantic_hash:
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: approved semantic_hash "
                    f"{decision.semantic_hash[:12]} != current {art.semantic_hash[:12]}"
                )

            # Tamper-evident verification against the staged JSON file on disk
            json_file = Path(art.json_path)
            if not json_file.is_file():
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: staged JSON file missing at {json_file}"
                )
            staged = ExtractionResult.model_validate_json(json_file.read_text(encoding="utf-8"))
            recomputed = staged.recompute_semantic_hash()
            if recomputed != decision.semantic_hash:
                raise GateBPublishBlockedError(
                    f"Gate B blocked {artifact_id}: staged JSON semantic_hash mismatch "
                    f"(recomputed {recomputed[:12]} != approved {decision.semantic_hash[:12]})"
                )

            return art
