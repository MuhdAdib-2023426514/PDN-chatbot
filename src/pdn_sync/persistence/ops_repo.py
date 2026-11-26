"""Single-writer SQLite repository for Phase 5 reliability (attempts, outbox/DLQ, ops stats)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, desc, func, select, text

from pdn_sync.extractors.contracts import ReviewStatus
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    DonationSessionRow,
    KnowledgeChunkRow,
    OutboxEventRow,
    PublicationVersionRow,
    StagedArtifactRow,
    StructuredTableRow,
    SyncRunRow,
    TaskAttemptRow,
)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class OpsRepository:
    """Single-writer repository for task attempts, durable outbox/DLQ, and ops telemetry queries."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    # ── Task attempts ────────────────────────────────────────────────────────

    def record_task_attempt(
        self,
        *,
        task_type: str,
        status: str,
        correlation_id: str,
        attempt_number: int = 1,
        run_id: str | None = None,
        resource_id: str | None = None,
        version_id: str | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        latency_ms: int | None = None,
        next_retry_at_utc: datetime | None = None,
        now_utc: datetime | None = None,
    ) -> str:
        """Persist one structured task execution attempt for auditability."""
        now = now_utc or datetime.now(UTC)
        attempt_id = f"att_{uuid.uuid4().hex[:16]}"
        safe_msg = error_message[:1000] if error_message else None
        with self._session_factory() as session:
            # Verify run_id exists if supplied (avoid FK failure in ad-hoc/unit runs)
            valid_run_id = run_id
            if valid_run_id is not None and session.get(SyncRunRow, valid_run_id) is None:
                valid_run_id = None
            session.add(
                TaskAttemptRow(
                    attempt_id=attempt_id,
                    run_id=valid_run_id,
                    task_type=task_type,
                    resource_id=resource_id,
                    version_id=version_id,
                    correlation_id=correlation_id,
                    attempt_number=attempt_number,
                    status=status,
                    error_class=error_class,
                    error_message=safe_msg,
                    latency_ms=latency_ms,
                    next_retry_at_utc=next_retry_at_utc,
                    created_at_utc=now,
                )
            )
            session.commit()
        return attempt_id

    def list_task_attempts(
        self,
        *,
        task_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 50,
    ) -> list[TaskAttemptRow]:
        with self._session_factory() as session:
            stmt = select(TaskAttemptRow).order_by(desc(TaskAttemptRow.created_at_utc))
            if task_type:
                stmt = stmt.where(TaskAttemptRow.task_type == task_type)
            if resource_id:
                stmt = stmt.where(TaskAttemptRow.resource_id == resource_id)
            if limit > 0:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt).all())

    # ── Durable Outbox & Dead-Letter Queue (DLQ) ─────────────────────────────

    def enqueue_outbox_event(
        self,
        *,
        event_type: str,
        dedupe_key: str,
        payload: dict[str, Any],
        correlation_id: str,
        resource_id: str | None = None,
        artifact_id: str | None = None,
        publication_id: str | None = None,
        max_attempts: int = 3,
        initial_status: str = "pending",
        initial_error: str | None = None,
        initial_attempt_count: int = 0,
        next_retry_at_utc: datetime | None = None,
        now_utc: datetime | None = None,
    ) -> OutboxEventRow:
        """Idempotently enqueue or update an outbox event keyed by dedupe_key."""
        now = now_utc or datetime.now(UTC)
        payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        safe_err = initial_error[:1000] if initial_error else None
        effective_status = initial_status
        if (
            effective_status not in {"dispatched", "resolved"}
            and initial_attempt_count >= max_attempts
        ):
            effective_status = "dead_letter"

        with self._session_factory() as session:
            existing = session.scalar(
                select(OutboxEventRow).where(OutboxEventRow.dedupe_key == dedupe_key)
            )
            if existing is not None:
                if existing.status in {"dispatched", "resolved"} and effective_status in {
                    "pending",
                    "failed",
                    "dead_letter",
                }:
                    existing.status = effective_status
                    existing.attempt_count = initial_attempt_count
                    existing.max_attempts = max_attempts
                    existing.payload_json = payload_str
                    existing.last_error = safe_err
                    existing.next_retry_at_utc = next_retry_at_utc
                    existing.correlation_id = correlation_id
                    existing.updated_at_utc = now
                    session.commit()
                    session.refresh(existing)
                    return existing
                if safe_err:
                    existing.last_error = safe_err
                    existing.updated_at_utc = now
                    session.commit()
                    session.refresh(existing)
                return existing

            row = OutboxEventRow(
                event_id=f"obx_{uuid.uuid4().hex[:16]}",
                event_type=event_type,
                dedupe_key=dedupe_key,
                resource_id=resource_id,
                artifact_id=artifact_id,
                publication_id=publication_id,
                correlation_id=correlation_id,
                payload_json=payload_str,
                status=effective_status,
                attempt_count=initial_attempt_count,
                max_attempts=max_attempts,
                next_retry_at_utc=next_retry_at_utc,
                last_error=safe_err,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_outbox_events(
        self,
        *,
        statuses: Sequence[str] | None = None,
        due_only: bool = False,
        now_utc: datetime | None = None,
        limit: int = 100,
    ) -> list[OutboxEventRow]:
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            stmt = select(OutboxEventRow).order_by(OutboxEventRow.created_at_utc.asc())
            if statuses:
                stmt = stmt.where(OutboxEventRow.status.in_(list(statuses)))
            if due_only:
                stmt = stmt.where(
                    (OutboxEventRow.next_retry_at_utc.is_(None))
                    | (OutboxEventRow.next_retry_at_utc <= now)
                )
            if limit > 0:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt).all())

    def mark_outbox_dispatched(
        self,
        event_id: str,
        *,
        now_utc: datetime | None = None,
    ) -> None:
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(OutboxEventRow, event_id)
            if row is None:
                return
            row.status = "dispatched"
            row.attempt_count += 1
            row.next_retry_at_utc = None
            row.last_error = None
            row.updated_at_utc = now
            session.commit()

    def mark_outbox_failure(
        self,
        event_id: str,
        *,
        error_message: str,
        next_retry_at_utc: datetime | None = None,
        now_utc: datetime | None = None,
    ) -> str:
        """Increment attempt count and transition to 'failed' or 'dead_letter'."""
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(OutboxEventRow, event_id)
            if row is None:
                return "missing"
            row.attempt_count += 1
            row.last_error = error_message[:1000]
            row.updated_at_utc = now
            if row.attempt_count >= row.max_attempts:
                row.status = "dead_letter"
                row.next_retry_at_utc = None
            else:
                row.status = "failed"
                row.next_retry_at_utc = next_retry_at_utc
            new_status = row.status
            session.commit()
            return new_status

    def resolve_outbox_event(
        self,
        event_id: str,
        *,
        reviewer: str,
        note: str,
        requeue: bool = False,
        now_utc: datetime | None = None,
    ) -> OutboxEventRow:
        """Resolve a dead-lettered/failed outbox event or requeue it for fresh replay."""
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(OutboxEventRow, event_id)
            if row is None:
                raise KeyError(f"Outbox event not found: {event_id}")
            row.resolved_by = reviewer
            row.resolved_note = note
            row.updated_at_utc = now
            if requeue:
                row.status = "pending"
                row.attempt_count = 0
                row.next_retry_at_utc = now
                row.last_error = None
            else:
                row.status = "resolved"
                row.next_retry_at_utc = None
            session.commit()
            session.refresh(row)
            return row

    def count_outbox_by_status(self) -> dict[str, int]:
        counts = {"pending": 0, "dispatched": 0, "failed": 0, "dead_letter": 0, "resolved": 0}
        with self._session_factory() as session:
            rows = session.execute(
                select(OutboxEventRow.status, func.count(OutboxEventRow.event_id)).group_by(
                    OutboxEventRow.status
                )
            ).all()
            for status, cnt in rows:
                counts[str(status)] = int(cnt or 0)
        return counts

    # ── Observability & Drift Queries ────────────────────────────────────────

    def get_oldest_pending_gate_b_age_hours(
        self, *, now_utc: datetime | None = None
    ) -> float | None:
        now = now_utc or datetime.now(UTC)
        with self._session_factory() as session:
            oldest = session.scalar(
                select(func.min(StagedArtifactRow.created_at_utc)).where(
                    StagedArtifactRow.review_status == ReviewStatus.PENDING_REVIEW.value
                )
            )
            oldest_utc = _as_utc(oldest)
            if oldest_utc is None:
                return None
            delta_hours = max(0.0, (now - oldest_utc).total_seconds() / 3600.0)
            return round(delta_hours, 2)

    def get_fts5_mismatch_counts(self) -> dict[str, int]:
        """Compute exact count mismatches between active SQLite rows and FTS5 virtual tables."""
        with self._session_factory() as session:
            active_tables = int(
                session.scalar(
                    select(func.count(StructuredTableRow.table_id)).where(
                        StructuredTableRow.status == "published"
                    )
                )
                or 0
            )
            fts_tables_u61 = int(
                session.execute(text("SELECT count(*) FROM structured_tables_fts_u61")).scalar()
                or 0
            )
            fts_tables_tri = int(
                session.execute(text("SELECT count(*) FROM structured_tables_fts_tri")).scalar()
                or 0
            )

            active_sessions = int(
                session.scalar(
                    select(func.count(DonationSessionRow.session_id)).where(
                        DonationSessionRow.is_current.is_(True)
                    )
                )
                or 0
            )
            fts_sessions_u61 = int(
                session.execute(text("SELECT count(*) FROM donation_sessions_fts_u61")).scalar()
                or 0
            )
            fts_sessions_tri = int(
                session.execute(text("SELECT count(*) FROM donation_sessions_fts_tri")).scalar()
                or 0
            )

            active_chunks = int(
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
            fts_chunks_u61 = int(
                session.execute(text("SELECT count(*) FROM knowledge_chunks_fts_u61")).scalar() or 0
            )

            return {
                "structured_tables": max(
                    abs(active_tables - fts_tables_u61),
                    abs(active_tables - fts_tables_tri),
                ),
                "donation_sessions": max(
                    abs(active_sessions - fts_sessions_u61),
                    abs(active_sessions - fts_sessions_tri),
                ),
                "knowledge_chunks": abs(active_chunks - fts_chunks_u61),
            }

    def get_last_completed_runs_detailed(self) -> dict[str, dict[str, Any]]:
        """Return latest completed SyncRun metadata per normalized profile key."""
        with self._session_factory() as session:
            runs = list(
                session.scalars(
                    select(SyncRunRow)
                    .where(SyncRunRow.status == "completed")
                    .order_by(desc(SyncRunRow.started_at_utc))
                    .limit(100)
                ).all()
            )
            by_profile: dict[str, dict[str, Any]] = {}
            for run in runs:
                finished = _as_utc(run.finished_at_utc) or _as_utc(run.started_at_utc)
                entry = {
                    "run_id": run.run_id,
                    "profile": run.profile,
                    "status": run.status,
                    "started_at_utc": _as_utc(run.started_at_utc),
                    "finished_at_utc": finished,
                    "summary": json.loads(run.summary_counts_json or "{}"),
                    "error_summary": run.error_summary,
                }
                if run.profile not in by_profile:
                    by_profile[run.profile] = entry
            return by_profile

    def get_latest_discovery_run_signals(self) -> dict[str, Any]:
        """Inspect latest discovery/discovery_audit run for anomaly halts or pattern cap alerts."""
        with self._session_factory() as session:
            run = session.scalar(
                select(SyncRunRow)
                .where(SyncRunRow.profile.in_(["discovery", "discovery_audit"]))
                .order_by(desc(SyncRunRow.started_at_utc))
                .limit(1)
            )
            if run is None:
                return {"present": False, "anomaly_halted": False, "pattern_cap_hits": []}
            summary = json.loads(run.summary_counts_json or "{}")
            err = (run.error_summary or "").lower()
            anomaly_halted = (
                run.status == "halted_anomaly"
                or "anomaly" in err
                or bool(summary.get("anomaly_halted"))
            )
            pattern_cap_hits = list(summary.get("pattern_cap_hits", []))
            if not pattern_cap_hits and "max_matches" in err:
                pattern_cap_hits = ["max_matches_exceeded"]
            return {
                "present": True,
                "run_id": run.run_id,
                "status": run.status,
                "anomaly_halted": anomaly_halted,
                "pattern_cap_hits": pattern_cap_hits,
                "error_summary": run.error_summary,
            }
