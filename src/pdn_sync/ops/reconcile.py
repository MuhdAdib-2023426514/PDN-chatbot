"""Drift detection and reconciliation across SQLite FTS5, durable outbox, and Qdrant."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, select, text

from pdn_sync.domain.policy import RetryPolicy
from pdn_sync.indexing.qdrant_store import QdrantChunkPoint, QdrantVectorStore
from pdn_sync.ops.retry import compute_backoff_seconds
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    DonationSessionRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    PublicationVersionRow,
    StructuredTableRow,
    StructuredTableRowItem,
)
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.publication_repo import PublicationRepository

if TYPE_CHECKING:
    from pdn_sync.indexing.embedder import EmbeddingBackend


@dataclass(slots=True)
class FtsReconcileResult:
    structured_tables_synced: int = 0
    donation_sessions_synced: int = 0
    knowledge_chunks_synced: int = 0
    before_mismatches: dict[str, int] = field(default_factory=dict)
    after_mismatches: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class OutboxReconcileResult:
    considered: int = 0
    dispatched: int = 0
    failed_retryable: int = 0
    dead_lettered: int = 0
    dry_run: bool = False
    event_outcomes: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class QdrantReconcileResult:
    sqlite_published_chunks: int = 0
    qdrant_published_points_before: int = 0
    qdrant_published_points_after: int = 0
    repaired_publications: int = 0
    deactivated_publications: int = 0
    dry_run: bool = False


def reconcile_fts5(engine: Engine) -> FtsReconcileResult:
    """Rebuild SQLite FTS5 virtual tables atomically from authoritative published SQLite rows."""
    ops_repo = OpsRepository(engine)
    before = ops_repo.get_fts5_mismatch_counts()
    session_factory = create_session_factory(engine)

    tables_count = 0
    sessions_count = 0
    chunks_count = 0

    with session_factory() as session:
        # 1. Structured tables FTS5 (only status == 'published')
        session.execute(text("DELETE FROM structured_tables_fts_u61"))
        session.execute(text("DELETE FROM structured_tables_fts_tri"))
        pub_tables = list(
            session.scalars(
                select(StructuredTableRow).where(StructuredTableRow.status == "published")
            ).all()
        )
        for tbl in pub_tables:
            row_items = list(
                session.scalars(
                    select(StructuredTableRowItem)
                    .where(StructuredTableRowItem.table_id == tbl.table_id)
                    .order_by(StructuredTableRowItem.row_index.asc())
                ).all()
            )
            headers_list = json.loads(tbl.headers_json or "[]")
            headers_text = " | ".join(str(h).strip() for h in headers_list if str(h).strip())
            rows_text = "\n".join(r.row_text for r in row_items if r.row_text)
            caption_text = (tbl.caption or "").strip()
            session.execute(
                text(
                    """
                    INSERT INTO structured_tables_fts_u61
                        (table_id, source_url, category, caption, headers_text, rows_text)
                    VALUES (:tid, :url, :cat, :cap, :hdr, :rows)
                    """
                ),
                {
                    "tid": tbl.table_id,
                    "url": tbl.source_url,
                    "cat": tbl.category,
                    "cap": caption_text,
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
                    "tid": tbl.table_id,
                    "cap": caption_text,
                    "hdr": headers_text,
                    "rows": rows_text,
                },
            )
            tables_count += 1

        # 2. Donation sessions FTS5 (only is_current == True)
        session.execute(text("DELETE FROM donation_sessions_fts_u61"))
        session.execute(text("DELETE FROM donation_sessions_fts_tri"))
        cur_sessions = list(
            session.scalars(
                select(DonationSessionRow).where(DonationSessionRow.is_current.is_(True))
            ).all()
        )
        for ses in cur_sessions:
            raw_cells = json.loads(ses.raw_cells_json or "{}")
            raw_text = " | ".join(f"{k}: {v}" for k, v in raw_cells.items() if str(v).strip())
            session.execute(
                text(
                    """
                    INSERT INTO donation_sessions_fts_u61
                        (session_id, event_date, state, organizer, location_name,
                         address, city, raw_text)
                    VALUES (:sid, :edate, :st, :org, :loc, :addr, :city, :raw)
                    """
                ),
                {
                    "sid": ses.session_id,
                    "edate": ses.event_date or "",
                    "st": ses.state or "",
                    "org": ses.organizer or "",
                    "loc": ses.location_name or "",
                    "addr": ses.address or "",
                    "city": ses.city or "",
                    "raw": raw_text,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO donation_sessions_fts_tri
                        (session_id, organizer, location_name, address, city)
                    VALUES (:sid, :org, :loc, :addr, :city)
                    """
                ),
                {
                    "sid": ses.session_id,
                    "org": ses.organizer or "",
                    "loc": ses.location_name or "",
                    "addr": ses.address or "",
                    "city": ses.city or "",
                },
            )
            sessions_count += 1

        # 3. Knowledge chunks FTS5 (only published PublicationVersionRow)
        session.execute(text("DELETE FROM knowledge_chunks_fts_u61"))
        chunk_rows = session.execute(
            select(
                KnowledgeChunkRow.chunk_id,
                KnowledgeChunkRow.publication_id,
                KnowledgeChunkRow.section,
                KnowledgeChunkRow.language,
                KnowledgeChunkRow.text,
                KnowledgeDocumentRow.category,
                KnowledgeDocumentRow.title,
            )
            .join(
                KnowledgeDocumentRow,
                KnowledgeDocumentRow.doc_id == KnowledgeChunkRow.doc_id,
            )
            .join(
                PublicationVersionRow,
                PublicationVersionRow.publication_id == KnowledgeChunkRow.publication_id,
            )
            .where(PublicationVersionRow.status == "published")
        ).all()

        for cid, pid, sec, lang, txt, cat, title in chunk_rows:
            session.execute(
                text(
                    """
                    INSERT INTO knowledge_chunks_fts_u61
                        (chunk_id, publication_id, category, language, title, section, text)
                    VALUES (:cid, :pid, :cat, :lang, :title, :sec, :txt)
                    """
                ),
                {
                    "cid": str(cid),
                    "pid": str(pid),
                    "cat": str(cat or "uncategorized"),
                    "lang": str(lang or "ms"),
                    "title": str(title or "").strip(),
                    "sec": str(sec or "").strip(),
                    "txt": str(txt or ""),
                },
            )
            chunks_count += 1

        session.commit()

    after = ops_repo.get_fts5_mismatch_counts()
    return FtsReconcileResult(
        structured_tables_synced=tables_count,
        donation_sessions_synced=sessions_count,
        knowledge_chunks_synced=chunks_count,
        before_mismatches=before,
        after_mismatches=after,
    )


def _upsert_publication_chunks_to_qdrant(
    *,
    engine: Engine,
    publication_id: str,
    qdrant: QdrantVectorStore,
    embedder: EmbeddingBackend,
) -> int:
    """Load active published SQLite chunks for publication_id, embed, and upsert to Qdrant."""
    session_factory = create_session_factory(engine)
    pub_repo = PublicationRepository(engine)
    with session_factory() as session:
        pub = session.get(PublicationVersionRow, publication_id)
        if pub is None or pub.status != "published":
            return 0
        doc = session.scalar(
            select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.publication_id == publication_id
            )
        )
        chunks = list(
            session.scalars(
                select(KnowledgeChunkRow)
                .where(KnowledgeChunkRow.publication_id == publication_id)
                .order_by(KnowledgeChunkRow.chunk_index.asc())
            ).all()
        )
        if doc is None or not chunks:
            return 0

        vectors = embedder.embed_documents([ch.text for ch in chunks])
        qdrant.ensure_collection(embedder.dimension)
        points: list[QdrantChunkPoint] = []
        for ch, vec in zip(chunks, vectors, strict=True):
            payload: dict[str, Any] = {
                "chunk_id": ch.chunk_id,
                "document_id": doc.doc_id,
                "publication_id": pub.publication_id,
                "resource_id": doc.resource_id,
                "resource_version_id": doc.resource_version_id,
                "artifact_id": doc.artifact_id,
                "source_url": doc.source_url,
                "asset_url": doc.asset_url,
                "title": doc.title,
                "section": ch.section,
                "category": doc.category,
                "language": ch.language,
                "content_type": doc.content_type,
                "last_verified_at": doc.last_verified_at_utc.isoformat(),
                "source_effective_date": doc.source_effective_date,
                "valid_from": doc.valid_from_utc.isoformat(),
                "valid_until": doc.valid_until_utc.isoformat() if doc.valid_until_utc else None,
                "pending_change": bool(doc.pending_change),
                "status": "published",
                "text": ch.text,
            }
            points.append(QdrantChunkPoint(point_id=ch.point_id, vector=vec, payload=payload))

    count = qdrant.upsert_points(points)
    pub_repo.mark_chunks_qdrant_synced(publication_id, datetime.now(UTC))
    return count


def reconcile_outbox(
    *,
    engine: Engine,
    qdrant: QdrantVectorStore | None = None,
    embedder: EmbeddingBackend | None = None,
    retry_policy: RetryPolicy | None = None,
    dry_run: bool = False,
    now_utc: datetime | None = None,
) -> OutboxReconcileResult:
    """Replay due pending/failed outbox events idempotently; dead-letter exhausted items."""
    ops_repo = OpsRepository(engine)
    policy = retry_policy or RetryPolicy()
    now = now_utc or datetime.now(UTC)

    due_events = ops_repo.list_outbox_events(
        statuses=("pending", "failed"),
        due_only=True,
        now_utc=now,
    )
    res = OutboxReconcileResult(considered=len(due_events), dry_run=dry_run)
    if dry_run:
        for ev in due_events:
            res.event_outcomes.append((ev.event_id, f"would_replay:{ev.event_type}"))
        return res

    for ev in due_events:
        payload = json.loads(ev.payload_json or "{}")
        try:
            if ev.event_type == "qdrant_upsert_publication":
                if qdrant is None or embedder is None:
                    raise RuntimeError("Qdrant store and embedder are required to replay upserts.")
                pub_id = str(ev.publication_id or payload.get("publication_id") or "")
                _upsert_publication_chunks_to_qdrant(
                    engine=engine,
                    publication_id=pub_id,
                    qdrant=qdrant,
                    embedder=embedder,
                )
            elif ev.event_type == "qdrant_retire_publication":
                if qdrant is None:
                    raise RuntimeError("Qdrant store is required to replay retire/supersede.")
                pub_id = str(ev.publication_id or payload.get("publication_id") or "")
                target_status = str(payload.get("status") or "retired")
                valid_until = str(payload.get("valid_until") or now.isoformat())
                qdrant.mark_publication_status(
                    pub_id,
                    status=target_status,
                    valid_until=valid_until,
                )
            elif ev.event_type == "fts_resync":
                reconcile_fts5(engine)
            else:
                raise RuntimeError(f"Unsupported outbox event_type: {ev.event_type!r}")

            ops_repo.mark_outbox_dispatched(ev.event_id, now_utc=now)
            ops_repo.record_task_attempt(
                task_type=f"outbox:{ev.event_type}",
                status="succeeded",
                correlation_id=ev.correlation_id,
                attempt_number=ev.attempt_count + 1,
                resource_id=ev.resource_id,
                now_utc=now,
            )
            res.dispatched += 1
            res.event_outcomes.append((ev.event_id, "dispatched"))
        except Exception as exc:
            delay_s = compute_backoff_seconds(ev.attempt_count + 1, policy=policy)
            next_retry = now + timedelta(seconds=delay_s)
            new_status = ops_repo.mark_outbox_failure(
                ev.event_id,
                error_message=str(exc),
                next_retry_at_utc=next_retry,
                now_utc=now,
            )
            ops_repo.record_task_attempt(
                task_type=f"outbox:{ev.event_type}",
                status="dead_lettered" if new_status == "dead_letter" else "retryable_failed",
                correlation_id=ev.correlation_id,
                attempt_number=ev.attempt_count + 1,
                resource_id=ev.resource_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
                next_retry_at_utc=None if new_status == "dead_letter" else next_retry,
                now_utc=now,
            )
            if new_status == "dead_letter":
                res.dead_lettered += 1
            else:
                res.failed_retryable += 1
            res.event_outcomes.append((ev.event_id, new_status))

    return res


def reconcile_qdrant(
    *,
    engine: Engine,
    qdrant: QdrantVectorStore,
    embedder: EmbeddingBackend | None = None,
    dry_run: bool = False,
) -> QdrantReconcileResult:
    """Repair unsynced published chunks (`qdrant_synced_at_utc IS NULL`) and deactivated pubs."""
    pub_repo = PublicationRepository(engine)
    session_factory = create_session_factory(engine)
    summary = pub_repo.get_status_summary()
    sqlite_chunks = int(summary["published_chunks"])
    _ok, before_pts = qdrant.health_check()

    result = QdrantReconcileResult(
        sqlite_published_chunks=sqlite_chunks,
        qdrant_published_points_before=before_pts,
        qdrant_published_points_after=before_pts,
        dry_run=dry_run,
    )

    with session_factory() as session:
        unsynced_pub_ids = list(
            dict.fromkeys(
                str(pid)
                for pid in session.scalars(
                    select(KnowledgeChunkRow.publication_id)
                    .join(
                        PublicationVersionRow,
                        PublicationVersionRow.publication_id == KnowledgeChunkRow.publication_id,
                    )
                    .where(
                        PublicationVersionRow.status == "published",
                        KnowledgeChunkRow.qdrant_synced_at_utc.is_(None),
                    )
                ).all()
            )
        )
        inactive_pubs = list(
            session.execute(
                select(
                    PublicationVersionRow.publication_id,
                    PublicationVersionRow.status,
                    PublicationVersionRow.valid_until_utc,
                ).where(PublicationVersionRow.status.in_(["superseded", "retired"]))
            ).all()
        )

    if dry_run:
        result.repaired_publications = len(unsynced_pub_ids)
        result.deactivated_publications = len(inactive_pubs)
        return result

    if unsynced_pub_ids:
        if embedder is None:
            raise RuntimeError(
                f"Embedder required to repair {len(unsynced_pub_ids)} unsynced publication(s)."
            )
        for pid in unsynced_pub_ids:
            _upsert_publication_chunks_to_qdrant(
                engine=engine,
                publication_id=pid,
                qdrant=qdrant,
                embedder=embedder,
            )
            result.repaired_publications += 1

    for pid, st, valid_until in inactive_pubs:
        qdrant.mark_publication_status(
            str(pid),
            status=str(st),
            valid_until=valid_until.isoformat() if valid_until else None,
        )
        result.deactivated_publications += 1

    _ok2, after_pts = qdrant.health_check()
    result.qdrant_published_points_after = after_pts
    return result
