"""Single-writer SQLite repository for Phase 3 active publication, FTS5 sync, and lifecycle."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, desc, func, select, text
from sqlalchemy.orm import Session

from pdn_sync.extractors.contracts import ExtractedTable, ReviewStatus
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    CrawlResourceRow,
    DiscoveredUrlRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    PublicationVersionRow,
    ResourceVersionRow,
    StagedArtifactRow,
    StructuredTableRow,
    StructuredTableRowItem,
    SyncRunRow,
)
from pdn_sync.persistence.staging_repo import StagingRepository


class StaleVersionPublishError(RuntimeError):
    """Raised when an older resource version attempts to overwrite a newer published version."""


@dataclass(frozen=True, slots=True)
class PublicationChunkSpec:
    """Deterministic chunk input persisted to knowledge_chunks and upserted to Qdrant."""

    chunk_id: str
    point_id: str
    chunk_index: int
    section: str | None
    language: str
    text: str


@dataclass(slots=True)
class PublishWriteOutcome:
    """Result of publishing one approved artifact into SQLite + FTS5."""

    action: str  # "published" | "skipped_idempotent" | "skipped_stale_version"
    artifact_id: str
    publication_id: str
    doc_id: str
    chunk_ids: list[str] = field(default_factory=list)
    table_ids: list[str] = field(default_factory=list)
    superseded_publication_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetirementReconcileResult:
    """Result of reconciling linked -> no_longer_linked -> retired after a discovery run."""

    run_id: str | None = None
    skipped_incomplete_run: bool = False
    reason: str | None = None
    linked_count: int = 0
    no_longer_linked_count: int = 0
    retired_publication_ids: list[str] = field(default_factory=list)
    retired_resource_ids: list[str] = field(default_factory=list)


def _deterministic_id(prefix: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _delete_table_from_fts(session: Session, table_id: str) -> None:
    session.execute(
        text("DELETE FROM structured_tables_fts_u61 WHERE table_id = :tid"),
        {"tid": table_id},
    )
    session.execute(
        text("DELETE FROM structured_tables_fts_tri WHERE table_id = :tid"),
        {"tid": table_id},
    )


def _insert_table_into_fts(
    session: Session,
    *,
    table_id: str,
    source_url: str,
    category: str,
    caption: str,
    headers_text: str,
    rows_text: str,
) -> None:
    _delete_table_from_fts(session, table_id)
    session.execute(
        text(
            """
            INSERT INTO structured_tables_fts_u61
                (table_id, source_url, category, caption, headers_text, rows_text)
            VALUES (:tid, :url, :cat, :cap, :hdr, :rows)
            """
        ),
        {
            "tid": table_id,
            "url": source_url,
            "cat": category,
            "cap": caption,
            "hdr": headers_text,
            "rows": rows_text,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO structured_tables_fts_tri
                (table_id, caption, headers_text, rows_text)
            VALUES (:tid, :cap, :hdr, :rows)
            """
        ),
        {
            "tid": table_id,
            "cap": caption,
            "hdr": headers_text,
            "rows": rows_text,
        },
    )


class PublicationRepository:
    """Single-writer repository managing active SQLite knowledge, FTS5 sync, and lifecycle."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)
        self._staging_repo = StagingRepository(engine)

    def list_publishable_artifact_ids(
        self,
        *,
        artifact_ids: Sequence[str] | None = None,
    ) -> list[str]:
        """Return approved/published artifact IDs to consider for publication.

        When artifact_ids is None, returns the latest approved/published version per resource_id
        (ordered deterministically by resource_id) so superseded older versions are skipped.
        """
        if artifact_ids is not None:
            return list(artifact_ids)

        with self._session_factory() as session:
            rows = session.execute(
                select(
                    StagedArtifactRow.artifact_id,
                    StagedArtifactRow.resource_id,
                    ResourceVersionRow.version_seq,
                    StagedArtifactRow.created_at_utc,
                )
                .join(
                    ResourceVersionRow,
                    ResourceVersionRow.version_id == StagedArtifactRow.resource_version_id,
                )
                .where(
                    StagedArtifactRow.review_status.in_(
                        [ReviewStatus.APPROVED.value, ReviewStatus.PUBLISHED.value]
                    )
                )
                .order_by(
                    StagedArtifactRow.resource_id.asc(),
                    ResourceVersionRow.version_seq.desc(),
                    StagedArtifactRow.created_at_utc.desc(),
                )
            ).all()

        selected: list[str] = []
        seen_resources: set[str] = set()
        for aid, rid, _vseq, _created in rows:
            if rid not in seen_resources:
                seen_resources.add(rid)
                selected.append(str(aid))
        return selected

    def get_publication_by_artifact(self, artifact_id: str) -> PublicationVersionRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(PublicationVersionRow).where(
                    PublicationVersionRow.artifact_id == artifact_id
                )
            )

    def get_active_publication_for_resource(self, resource_id: str) -> PublicationVersionRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(PublicationVersionRow)
                .where(
                    PublicationVersionRow.resource_id == resource_id,
                    PublicationVersionRow.status == "published",
                )
                .order_by(desc(PublicationVersionRow.version_seq))
                .limit(1)
            )

    def publish_artifact_atomic(
        self,
        *,
        artifact_id: str,
        title: str | None,
        language: str,
        content_type: str | None,
        asset_url: str | None,
        chunks: Sequence[PublicationChunkSpec],
        tables: Sequence[ExtractedTable],
        embedding_model: str,
        qdrant_collection: str,
        supersede_policy_rule_id: str | None = None,
        valid_until_utc: datetime | None = None,
        now_utc: datetime | None = None,
        raise_on_stale: bool = True,
    ) -> PublishWriteOutcome:
        """Atomically verify Gate B and write active SQLite + FTS5 rows for an approved artifact."""
        # Hard Gate B enforcement inside the single-writer repository layer!
        self._staging_repo.assert_artifact_approved_for_publish(artifact_id)
        now = now_utc or datetime.now(UTC)

        with self._session_factory() as session:
            art = session.get(StagedArtifactRow, artifact_id)
            assert art is not None
            ver = session.get(ResourceVersionRow, art.resource_version_id)
            if ver is None:
                raise RuntimeError(
                    f"Missing resource_version {art.resource_version_id} for {artifact_id}"
                )

            existing = session.scalar(
                select(PublicationVersionRow).where(
                    PublicationVersionRow.artifact_id == artifact_id
                )
            )
            if (
                existing is not None
                and existing.status == "published"
                and existing.embedding_model == embedding_model
                and existing.qdrant_collection == qdrant_collection
            ):
                doc = session.scalar(
                    select(KnowledgeDocumentRow).where(
                        KnowledgeDocumentRow.publication_id == existing.publication_id
                    )
                )
                existing_chunk_ids = list(
                    session.scalars(
                        select(KnowledgeChunkRow.chunk_id).where(
                            KnowledgeChunkRow.publication_id == existing.publication_id
                        )
                    ).all()
                )
                existing_table_ids = list(
                    session.scalars(
                        select(StructuredTableRow.table_id).where(
                            StructuredTableRow.publication_id == existing.publication_id
                        )
                    ).all()
                )
                return PublishWriteOutcome(
                    action="skipped_idempotent",
                    artifact_id=artifact_id,
                    publication_id=existing.publication_id,
                    doc_id=doc.doc_id if doc else "",
                    chunk_ids=existing_chunk_ids,
                    table_ids=existing_table_ids,
                )

            # Monotonic version guard: never allow an older version_seq to overwrite a newer one
            max_pub_seq = session.scalar(
                select(func.max(PublicationVersionRow.version_seq)).where(
                    PublicationVersionRow.resource_id == art.resource_id,
                    PublicationVersionRow.status == "published",
                )
            )
            if max_pub_seq is not None and ver.version_seq < int(max_pub_seq):
                if raise_on_stale:
                    raise StaleVersionPublishError(
                        f"Refusing stale publish for {art.resource_id}: "
                        f"version_seq={ver.version_seq} < active version_seq={max_pub_seq}"
                    )
                return PublishWriteOutcome(
                    action="skipped_stale_version",
                    artifact_id=artifact_id,
                    publication_id="",
                    doc_id="",
                )

            # If re-publishing the exact same artifact under a new embedding model/collection,
            # clean up the prior publication rows for this artifact_id cleanly.
            superseded_pub_ids: list[str] = []
            if existing is not None:
                superseded_pub_ids.append(existing.publication_id)
                old_tables = list(
                    session.scalars(
                        select(StructuredTableRow.table_id).where(
                            StructuredTableRow.publication_id == existing.publication_id
                        )
                    ).all()
                )
                for tid in old_tables:
                    _delete_table_from_fts(session, tid)
                session.delete(existing)
                session.flush()

            pub_id = _deterministic_id(
                "pub",
                f"{art.artifact_id}:{embedding_model}:{qdrant_collection}",
            )
            doc_id = _deterministic_id("doc", f"{pub_id}:{art.resource_id}")

            # Supersede prior active publications for the SAME resource_id
            prior_same_res = list(
                session.scalars(
                    select(PublicationVersionRow).where(
                        PublicationVersionRow.resource_id == art.resource_id,
                        PublicationVersionRow.status == "published",
                    )
                ).all()
            )
            for old_pub in prior_same_res:
                self._deactivate_publication(
                    session,
                    old_pub,
                    new_status="superseded",
                    valid_until_utc=now,
                    superseded_by_pub_id=pub_id,
                )
                superseded_pub_ids.append(old_pub.publication_id)

            # Supersede prior active publications in the SAME latest_wins pattern rule group
            if supersede_policy_rule_id:
                prior_pattern_pubs = list(
                    session.scalars(
                        select(PublicationVersionRow).where(
                            PublicationVersionRow.policy_rule_id == supersede_policy_rule_id,
                            PublicationVersionRow.status == "published",
                            PublicationVersionRow.resource_id != art.resource_id,
                        )
                    ).all()
                )
                for old_pub in prior_pattern_pubs:
                    self._deactivate_publication(
                        session,
                        old_pub,
                        new_status="superseded",
                        valid_until_utc=now,
                        superseded_by_pub_id=pub_id,
                    )
                    superseded_pub_ids.append(old_pub.publication_id)

            # Insert new PublicationVersionRow
            pub_row = PublicationVersionRow(
                publication_id=pub_id,
                resource_id=art.resource_id,
                artifact_id=art.artifact_id,
                resource_version_id=art.resource_version_id,
                version_seq=ver.version_seq,
                semantic_hash=art.semantic_hash,
                policy_rule_id=art.policy_rule_id,
                embedding_model=embedding_model,
                qdrant_collection=qdrant_collection,
                status="published",
                valid_from_utc=ver.captured_at_utc,
                valid_until_utc=valid_until_utc,
                published_at_utc=now,
                superseded_by_pub_id=None,
                pending_change=False,
            )
            session.add(pub_row)
            session.flush()

            # Insert KnowledgeDocumentRow
            doc_row = KnowledgeDocumentRow(
                doc_id=doc_id,
                publication_id=pub_id,
                resource_id=art.resource_id,
                artifact_id=art.artifact_id,
                resource_version_id=art.resource_version_id,
                source_url=art.source_url,
                asset_url=asset_url,
                resource_type=art.resource_type,
                category=art.category,
                title=title,
                language=language,
                content_type=content_type,
                last_verified_at_utc=ver.last_verified_at_utc,
                source_effective_date=art.source_effective_date,
                valid_from_utc=ver.captured_at_utc,
                valid_until_utc=valid_until_utc,
                status="published",
                pending_change=False,
            )
            session.add(doc_row)
            session.flush()

            # Insert KnowledgeChunkRows
            chunk_ids: list[str] = []
            for ch in chunks:
                session.add(
                    KnowledgeChunkRow(
                        chunk_id=ch.chunk_id,
                        point_id=ch.point_id,
                        doc_id=doc_id,
                        publication_id=pub_id,
                        chunk_index=ch.chunk_index,
                        section=ch.section,
                        language=ch.language,
                        text=ch.text,
                        char_count=len(ch.text),
                        qdrant_synced_at_utc=None,
                    )
                )
                chunk_ids.append(ch.chunk_id)

            # Insert StructuredTableRows + StructuredTableRowItems + FTS5 entries
            table_ids: list[str] = []
            for tbl in tables:
                table_id = _deterministic_id("tbl", f"{pub_id}:{tbl.table_index}")
                tbl_row = StructuredTableRow(
                    table_id=table_id,
                    publication_id=pub_id,
                    resource_id=art.resource_id,
                    artifact_id=art.artifact_id,
                    source_url=art.source_url,
                    category=art.category,
                    table_index=tbl.table_index,
                    caption=tbl.caption,
                    headers_json=json.dumps(tbl.headers, ensure_ascii=False),
                    units_json=json.dumps(tbl.units, ensure_ascii=False),
                    row_count=len(tbl.rows),
                    last_verified_at_utc=ver.last_verified_at_utc,
                    source_effective_date=art.source_effective_date,
                    valid_from_utc=ver.captured_at_utc,
                    valid_until_utc=valid_until_utc,
                    status="published",
                    pending_change=False,
                )
                session.add(tbl_row)
                session.flush()

                row_texts: list[str] = []
                for r_idx, cells in enumerate(tbl.rows):
                    row_joined = " | ".join(str(c).strip() for c in cells if str(c).strip())
                    row_texts.append(row_joined)
                    session.add(
                        StructuredTableRowItem(
                            row_id=f"row_{uuid.uuid4().hex[:16]}",
                            table_id=table_id,
                            row_index=r_idx,
                            cells_json=json.dumps(cells, ensure_ascii=False),
                            row_text=row_joined,
                            source_selector=tbl.source_selector,
                        )
                    )

                headers_text = " | ".join(str(h).strip() for h in tbl.headers if str(h).strip())
                caption_text = (tbl.caption or title or "").strip()
                _insert_table_into_fts(
                    session,
                    table_id=table_id,
                    source_url=art.source_url,
                    category=art.category,
                    caption=caption_text,
                    headers_text=headers_text,
                    rows_text="\n".join(row_texts),
                )
                table_ids.append(table_id)

            # Transition staged artifact to 'published' (immutable review_decisions stay intact)
            art.review_status = ReviewStatus.PUBLISHED.value
            art.updated_at_utc = now

            session.commit()
            return PublishWriteOutcome(
                action="published",
                artifact_id=artifact_id,
                publication_id=pub_id,
                doc_id=doc_id,
                chunk_ids=chunk_ids,
                table_ids=table_ids,
                superseded_publication_ids=superseded_pub_ids,
            )

    def _deactivate_publication(
        self,
        session: Session,
        pub_row: PublicationVersionRow,
        *,
        new_status: str,
        valid_until_utc: datetime,
        superseded_by_pub_id: str | None = None,
    ) -> None:
        pub_row.status = new_status
        pub_row.valid_until_utc = valid_until_utc
        if superseded_by_pub_id is not None:
            pub_row.superseded_by_pub_id = superseded_by_pub_id

        docs = list(
            session.scalars(
                select(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.publication_id == pub_row.publication_id
                )
            ).all()
        )
        for d in docs:
            d.status = new_status
            d.valid_until_utc = valid_until_utc

        tables = list(
            session.scalars(
                select(StructuredTableRow).where(
                    StructuredTableRow.publication_id == pub_row.publication_id
                )
            ).all()
        )
        for t in tables:
            t.status = new_status
            t.valid_until_utc = valid_until_utc
            _delete_table_from_fts(session, t.table_id)

        art = session.get(StagedArtifactRow, pub_row.artifact_id)
        if art is not None and art.review_status == ReviewStatus.PUBLISHED.value:
            art.review_status = new_status
            art.updated_at_utc = valid_until_utc

    def mark_chunks_qdrant_synced(
        self,
        publication_id: str,
        synced_at_utc: datetime | None = None,
    ) -> None:
        now = synced_at_utc or datetime.now(UTC)
        with self._session_factory() as session:
            chunks = list(
                session.scalars(
                    select(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.publication_id == publication_id
                    )
                ).all()
            )
            for ch in chunks:
                ch.qdrant_synced_at_utc = now
            session.commit()

    def sync_pending_change_flags(self) -> list[tuple[str, bool]]:
        """Update pending_change on published docs/tables whenever a newer staged change awaits."""
        changes: list[tuple[str, bool]] = []
        with self._session_factory() as session:
            active_pubs = list(
                session.scalars(
                    select(PublicationVersionRow).where(PublicationVersionRow.status == "published")
                ).all()
            )
            for pub in active_pubs:
                newer_pending = session.scalar(
                    select(func.count(StagedArtifactRow.artifact_id))
                    .join(
                        ResourceVersionRow,
                        ResourceVersionRow.version_id == StagedArtifactRow.resource_version_id,
                    )
                    .where(
                        StagedArtifactRow.resource_id == pub.resource_id,
                        ResourceVersionRow.version_seq > pub.version_seq,
                        StagedArtifactRow.review_status.in_(
                            [ReviewStatus.PENDING_REVIEW.value, ReviewStatus.QUARANTINED.value]
                        ),
                    )
                )
                has_pending = int(newer_pending or 0) > 0
                if pub.pending_change != has_pending:
                    pub.pending_change = has_pending
                    for d in session.scalars(
                        select(KnowledgeDocumentRow).where(
                            KnowledgeDocumentRow.publication_id == pub.publication_id
                        )
                    ).all():
                        d.pending_change = has_pending
                    for t in session.scalars(
                        select(StructuredTableRow).where(
                            StructuredTableRow.publication_id == pub.publication_id
                        )
                    ).all():
                        t.pending_change = has_pending
                    changes.append((pub.publication_id, has_pending))
            session.commit()
        return changes

    def reconcile_retirements(
        self,
        *,
        run_id: str | None = None,
        consecutive_threshold: int = 2,
        now_utc: datetime | None = None,
    ) -> RetirementReconcileResult:
        """Advance linked -> no_longer_linked -> retired strictly on complete error-free runs."""
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            if run_id is not None:
                run_row = session.get(SyncRunRow, run_id)
            else:
                run_row = session.scalar(
                    select(SyncRunRow)
                    .where(SyncRunRow.profile.in_(["discovery_audit", "discovery"]))
                    .order_by(desc(SyncRunRow.started_at_utc))
                    .limit(1)
                )

            if run_row is None:
                return RetirementReconcileResult(
                    skipped_incomplete_run=True,
                    reason="No discovery run found",
                )

            if run_row.status != "completed" or bool((run_row.error_summary or "").strip()):
                return RetirementReconcileResult(
                    run_id=run_row.run_id,
                    skipped_incomplete_run=True,
                    reason=(
                        f"Discovery run {run_row.run_id} not clean "
                        f"(status={run_row.status!r}, error_summary={run_row.error_summary!r})"
                    ),
                )

            seen_keys = set(
                session.scalars(
                    select(DiscoveredUrlRow.canonical_key).where(
                        DiscoveredUrlRow.run_id == run_row.run_id,
                        DiscoveredUrlRow.link_status != "broken",
                    )
                ).all()
            )

            # Reconcile all resources that currently have a published version or are tracked
            published_res_ids = set(
                session.scalars(
                    select(PublicationVersionRow.resource_id).where(
                        PublicationVersionRow.status == "published"
                    )
                ).all()
            )
            resources = list(
                session.scalars(
                    select(CrawlResourceRow).where(
                        CrawlResourceRow.resource_id.in_(published_res_ids)
                        | (CrawlResourceRow.lifecycle_state == "no_longer_linked")
                    )
                ).all()
            )

            result = RetirementReconcileResult(run_id=run_row.run_id)
            for res in resources:
                if res.canonical_key in seen_keys:
                    res.missing_consecutive_runs = 0
                    res.lifecycle_state = "linked"
                    res.last_seen_at_utc = now
                    result.linked_count += 1
                    continue

                res.missing_consecutive_runs += 1
                if res.missing_consecutive_runs >= consecutive_threshold:
                    res.lifecycle_state = "retired"
                    result.retired_resource_ids.append(res.resource_id)
                    active_pubs = list(
                        session.scalars(
                            select(PublicationVersionRow).where(
                                PublicationVersionRow.resource_id == res.resource_id,
                                PublicationVersionRow.status == "published",
                            )
                        ).all()
                    )
                    for pub in active_pubs:
                        self._deactivate_publication(
                            session,
                            pub,
                            new_status="retired",
                            valid_until_utc=now,
                        )
                        result.retired_publication_ids.append(pub.publication_id)
                else:
                    res.lifecycle_state = "no_longer_linked"
                    result.no_longer_linked_count += 1

            session.commit()
            return result

    def get_status_summary(self) -> dict[str, Any]:
        """Return operational status counts for GET /v1/status and CLI."""
        with self._session_factory() as session:
            pub_docs = int(
                session.scalar(
                    select(func.count(KnowledgeDocumentRow.doc_id)).where(
                        KnowledgeDocumentRow.status == "published"
                    )
                )
                or 0
            )
            pub_chunks = int(
                session.scalar(
                    select(func.count(KnowledgeChunkRow.chunk_id))
                    .join(
                        PublicationVersionRow,
                        PublicationVersionRow.publication_id == KnowledgeChunkRow.publication_id,
                    )
                    .where(PublicationVersionRow.status == "published")
                )
                or 0
            )
            pub_tables = int(
                session.scalar(
                    select(func.count(StructuredTableRow.table_id)).where(
                        StructuredTableRow.status == "published"
                    )
                )
                or 0
            )
            pending_change_docs = int(
                session.scalar(
                    select(func.count(KnowledgeDocumentRow.doc_id)).where(
                        KnowledgeDocumentRow.status == "published",
                        KnowledgeDocumentRow.pending_change.is_(True),
                    )
                )
                or 0
            )
            pending_gate_b = int(
                session.scalar(
                    select(func.count(StagedArtifactRow.artifact_id)).where(
                        StagedArtifactRow.review_status == ReviewStatus.PENDING_REVIEW.value
                    )
                )
                or 0
            )
            quarantined_gate_b = int(
                session.scalar(
                    select(func.count(StagedArtifactRow.artifact_id)).where(
                        StagedArtifactRow.review_status == ReviewStatus.QUARANTINED.value
                    )
                )
                or 0
            )
            approved_unpublished = int(
                session.scalar(
                    select(func.count(StagedArtifactRow.artifact_id)).where(
                        StagedArtifactRow.review_status == ReviewStatus.APPROVED.value
                    )
                )
                or 0
            )
            retired_docs = int(
                session.scalar(
                    select(func.count(KnowledgeDocumentRow.doc_id)).where(
                        KnowledgeDocumentRow.status == "retired"
                    )
                )
                or 0
            )
            superseded_docs = int(
                session.scalar(
                    select(func.count(KnowledgeDocumentRow.doc_id)).where(
                        KnowledgeDocumentRow.status == "superseded"
                    )
                )
                or 0
            )

            # Latest successful sync run per profile
            runs = list(
                session.scalars(
                    select(SyncRunRow).order_by(desc(SyncRunRow.started_at_utc)).limit(50)
                ).all()
            )
            last_runs: dict[str, dict[str, Any]] = {}
            for run in runs:
                if run.profile not in last_runs and run.status == "completed":
                    last_runs[run.profile] = {
                        "run_id": run.run_id,
                        "status": run.status,
                        "started_at_utc": run.started_at_utc.isoformat(),
                        "finished_at_utc": (
                            run.finished_at_utc.isoformat() if run.finished_at_utc else None
                        ),
                    }

            # Latest active embedding model recorded
            latest_pub = session.scalar(
                select(PublicationVersionRow)
                .where(PublicationVersionRow.status == "published")
                .order_by(desc(PublicationVersionRow.published_at_utc))
                .limit(1)
            )

            return {
                "published_documents": pub_docs,
                "published_chunks": pub_chunks,
                "published_tables": pub_tables,
                "superseded_documents": superseded_docs,
                "retired_documents": retired_docs,
                "pending_change_documents": pending_change_docs,
                "approved_unpublished_artifacts": approved_unpublished,
                "pending_gate_b": pending_gate_b,
                "quarantined_gate_b": quarantined_gate_b,
                "active_embedding_model": latest_pub.embedding_model if latest_pub else None,
                "active_qdrant_collection": latest_pub.qdrant_collection if latest_pub else None,
                "last_completed_runs_by_profile": last_runs,
            }
