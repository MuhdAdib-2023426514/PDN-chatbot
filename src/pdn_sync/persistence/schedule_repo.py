"""Single-writer SQLite repository for Phase 4 blood donation schedules and FTS5 sync."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from pdn_sync.extractors.contracts import ExtractionResult, ReviewStatus
from pdn_sync.extractors.jevents import (
    extract_upstream_event_id,
    make_session_identity,
    normalize_identity_token,
)
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    DonationEventRow,
    DonationSessionRow,
    ResourceVersionRow,
    StagedArtifactRow,
)
from pdn_sync.persistence.staging_repo import StagingRepository


@dataclass(slots=True)
class ScheduleReconcileOutcome:
    """Result of transactionally reconciling one approved JEvents schedule into SQLite + FTS5."""

    event_id: str
    upstream_event_id: str
    listed: int = 0
    amended: int = 0
    unchanged: int = 0
    removed_from_source: int = 0
    canceled_explicit: int = 0
    session_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MissingCalendarEventsResult:
    """Result of reconciling events missing from clean monthcalendar fetches."""

    checked_months: list[str] = field(default_factory=list)
    marked_removed_event_ids: list[str] = field(default_factory=list)


def _deterministic_event_id(upstream_event_id: str) -> str:
    digest = hashlib.sha256(f"jevents:{upstream_event_id}".encode()).hexdigest()[:16]
    return f"evt_{digest}"


def _delete_session_from_fts(session: Session, session_id: str) -> None:
    session.execute(
        text("DELETE FROM donation_sessions_fts_u61 WHERE session_id = :sid"),
        {"sid": session_id},
    )
    session.execute(
        text("DELETE FROM donation_sessions_fts_tri WHERE session_id = :sid"),
        {"sid": session_id},
    )


def _insert_session_into_fts(session: Session, row: DonationSessionRow) -> None:
    _delete_session_from_fts(session, row.session_id)
    if not row.is_current or row.status == "removed_from_source":
        return
    raw_cells: dict[str, str] = json.loads(row.raw_cells_json)
    raw_text = " ".join(f"{k} {v}" for k, v in sorted(raw_cells.items()))
    session.execute(
        text(
            """
            INSERT INTO donation_sessions_fts_u61
                (session_id, event_date, state, organizer, location_name, address, city, raw_text)
            VALUES (:sid, :dt, :st, :org, :loc, :addr, :city, :raw)
            """
        ),
        {
            "sid": row.session_id,
            "dt": row.event_date or "",
            "st": row.state or "",
            "org": row.organizer or "",
            "loc": row.location_name or "",
            "addr": row.address or "",
            "city": row.city or "",
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
            "sid": row.session_id,
            "org": row.organizer or "",
            "loc": row.location_name or "",
            "addr": row.address or "",
            "city": row.city or "",
        },
    )


def _session_meaning_tuple(row: DonationSessionRow) -> tuple[object, ...]:
    return (
        row.organizer,
        row.location_name,
        row.address,
        row.city,
        row.state,
        row.event_date,
        row.time_start,
        row.time_end,
        row.session_label,
        row.target_donors,
        row.raw_cells_json,
    )


class ScheduleRepository:
    """Single-writer repository reconciling approved donation schedules into SQLite + FTS5."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)
        self._staging_repo = StagingRepository(engine)

    def publish_schedule_artifact_atomic(
        self,
        *,
        artifact_id: str,
        publication_id: str,
        staged: ExtractionResult,
        now_utc: datetime | None = None,
    ) -> ScheduleReconcileOutcome:
        """Reconcile an approved JEvents ExtractionResult into donation_events/sessions + FTS5."""
        # Hard Gate B enforcement inside the single-writer schedule repository
        self._staging_repo.assert_artifact_approved_for_publish(artifact_id)
        now = now_utc or datetime.now(UTC)

        upstream_id = str(
            staged.provenance.get("upstream_event_id")
            or extract_upstream_event_id(staged.source_url)
        )
        canceled_set = {
            str(x) for x in staged.provenance.get("explicit_cancellation_identities", [])
        }
        event_id = _deterministic_event_id(upstream_id)

        with self._session_factory() as session:
            ver = session.get(ResourceVersionRow, staged.resource_version_id)
            version_seq = ver.version_seq if ver else 1
            last_verified = ver.last_verified_at_utc if ver else now

            event_date = staged.source_effective_date or next(
                (ev.event_date for ev in staged.structured_events if ev.event_date),
                None,
            )

            ev_row = session.get(DonationEventRow, event_id)
            is_event_update = ev_row is not None
            if ev_row is None:
                ev_row = DonationEventRow(
                    event_id=event_id,
                    upstream_event_id=upstream_id,
                    resource_id=staged.resource_id,
                    publication_id=publication_id,
                    artifact_id=artifact_id,
                    source_url=staged.source_url,
                    title=staged.title,
                    event_date=event_date,
                    status="listed",
                    last_verified_at_utc=last_verified,
                    published_at_utc=now,
                    pending_change=False,
                    version_seq=version_seq,
                )
                session.add(ev_row)
                session.flush()
            else:
                ev_row.publication_id = publication_id
                ev_row.artifact_id = artifact_id
                ev_row.source_url = staged.source_url
                ev_row.title = staged.title
                ev_row.event_date = event_date
                ev_row.last_verified_at_utc = last_verified
                ev_row.published_at_utc = now
                ev_row.pending_change = False
                ev_row.version_seq = version_seq

            # Load existing active sessions for this event keyed by stable session_identity
            existing_current = list(
                session.scalars(
                    select(DonationSessionRow).where(
                        DonationSessionRow.event_id == event_id,
                        DonationSessionRow.is_current.is_(True),
                    )
                ).all()
            )
            existing_by_identity: dict[str, DonationSessionRow] = {
                r.session_identity: r for r in existing_current
            }

            outcome = ScheduleReconcileOutcome(
                event_id=event_id,
                upstream_event_id=upstream_id,
            )
            seen_identities: set[str] = set()
            has_any_amendment = False
            all_canceled = bool(staged.structured_events)

            for ev in staged.structured_events:
                base_ident = make_session_identity(
                    upstream_event_id=upstream_id,
                    event_date=ev.event_date or event_date,
                    location_name=ev.location_name,
                    session_label=ev.session_label,
                )
                # Guard against identical duplicate rows within the same event table
                ident = base_ident
                dup_idx = 1
                while ident in seen_identities:
                    dup_idx += 1
                    ident = f"{base_ident}_{dup_idx}"
                seen_identities.add(ident)

                is_canceled = base_ident in canceled_set
                if not is_canceled:
                    all_canceled = False

                raw_json = json.dumps(ev.raw_cells, ensure_ascii=False, sort_keys=True)
                target_tuple = (
                    ev.organizer,
                    ev.location_name,
                    ev.address,
                    ev.city,
                    ev.state,
                    ev.event_date or event_date,
                    ev.time_start,
                    ev.time_end,
                    ev.session_label,
                    ev.target_donors,
                    raw_json,
                )

                prev = existing_by_identity.get(ident)
                if prev is not None and _session_meaning_tuple(prev) == target_tuple:
                    desired_status = "canceled_if_explicit" if is_canceled else prev.status
                    if prev.status == "removed_from_source":
                        desired_status = "canceled_if_explicit" if is_canceled else "listed"
                    prev.publication_id = publication_id
                    prev.last_verified_at_utc = last_verified
                    prev.pending_change = False
                    prev.status = desired_status
                    _insert_session_into_fts(session, prev)
                    outcome.unchanged += 1
                    outcome.session_ids.append(prev.session_id)
                    continue

                new_session_id = f"ses_{ident[:16]}_{uuid.uuid4().hex[:6]}"
                if prev is not None:
                    new_status = "canceled_if_explicit" if is_canceled else "amended"
                    prev.is_current = False
                    prev.superseded_by_session_id = new_session_id
                    _delete_session_from_fts(session, prev.session_id)
                    has_any_amendment = True
                    if is_canceled:
                        outcome.canceled_explicit += 1
                    else:
                        outcome.amended += 1
                else:
                    new_status = "canceled_if_explicit" if is_canceled else "listed"
                    if is_canceled:
                        outcome.canceled_explicit += 1
                    else:
                        outcome.listed += 1

                new_row = DonationSessionRow(
                    session_id=new_session_id,
                    event_id=event_id,
                    publication_id=publication_id,
                    session_identity=ident,
                    organizer=ev.organizer,
                    normalized_organizer=normalize_identity_token(ev.organizer),
                    location_name=ev.location_name,
                    normalized_location=normalize_identity_token(ev.location_name),
                    address=ev.address,
                    city=ev.city,
                    state=ev.state,
                    event_date=ev.event_date or event_date,
                    time_start=ev.time_start,
                    time_end=ev.time_end,
                    session_label=ev.session_label,
                    target_donors=ev.target_donors,
                    raw_cells_json=raw_json,
                    status=new_status,
                    source_url=staged.source_url,
                    last_verified_at_utc=last_verified,
                    pending_change=False,
                    is_current=True,
                    superseded_by_session_id=None,
                )
                session.add(new_row)
                session.flush()
                _insert_session_into_fts(session, new_row)
                outcome.session_ids.append(new_session_id)

            # Any prior active session missing from this approved page -> removed_from_source
            for old_ident, old_row in existing_by_identity.items():
                if old_ident not in seen_identities and old_row.status != "removed_from_source":
                    old_row.status = "removed_from_source"
                    old_row.last_verified_at_utc = last_verified
                    old_row.pending_change = False
                    _delete_session_from_fts(session, old_row.session_id)
                    outcome.removed_from_source += 1
                    has_any_amendment = True

            if all_canceled:
                ev_row.status = "canceled_if_explicit"
            elif is_event_update and has_any_amendment:
                ev_row.status = "amended"
            else:
                ev_row.status = "listed"

            session.commit()
            return outcome

    def reconcile_missing_calendar_events(
        self,
        *,
        successful_months: Sequence[tuple[int, int]],
        seen_upstream_event_ids: set[str],
        now_utc: datetime | None = None,
    ) -> MissingCalendarEventsResult:
        """Mark events absent from clean month fetches as removed_from_source (never canceled).

        Months whose fetch failed must NOT be included in successful_months, ensuring failed
        fetches never delete or alter valid events.
        """
        now = now_utc or datetime.now(UTC)
        month_prefixes = [f"{y:04d}-{m:02d}" for y, m in successful_months]
        res = MissingCalendarEventsResult(checked_months=month_prefixes)
        if not month_prefixes:
            return res

        with self._session_factory() as session:
            active_events = list(
                session.scalars(
                    select(DonationEventRow).where(
                        DonationEventRow.status.in_(["listed", "amended", "not_confirmed"])
                    )
                ).all()
            )
            for ev in active_events:
                if not ev.event_date or ev.event_date[:7] not in month_prefixes:
                    continue
                if ev.upstream_event_id in seen_upstream_event_ids:
                    ev.last_verified_at_utc = now
                    continue

                # Absent from a clean, successful month fetch -> removed_from_source (NOT canceled!)
                ev.status = "removed_from_source"
                ev.last_verified_at_utc = now
                res.marked_removed_event_ids.append(ev.event_id)
                sessions = list(
                    session.scalars(
                        select(DonationSessionRow).where(
                            DonationSessionRow.event_id == ev.event_id,
                            DonationSessionRow.is_current.is_(True),
                        )
                    ).all()
                )
                for srow in sessions:
                    srow.status = "removed_from_source"
                    srow.last_verified_at_utc = now
                    _delete_session_from_fts(session, srow.session_id)

            session.commit()
            return res

    def sync_schedule_pending_change_flags(self) -> int:
        """Flag pending_change=True on published schedules when a newer staged artifact awaits."""
        updated = 0
        with self._session_factory() as session:
            events = list(session.scalars(select(DonationEventRow)).all())
            for ev in events:
                newer_pending = session.scalar(
                    select(func.count(StagedArtifactRow.artifact_id))
                    .join(
                        ResourceVersionRow,
                        ResourceVersionRow.version_id == StagedArtifactRow.resource_version_id,
                    )
                    .where(
                        StagedArtifactRow.resource_id == ev.resource_id,
                        ResourceVersionRow.version_seq > ev.version_seq,
                        StagedArtifactRow.review_status.in_(
                            [ReviewStatus.PENDING_REVIEW.value, ReviewStatus.QUARANTINED.value]
                        ),
                    )
                )
                has_pending = int(newer_pending or 0) > 0
                if ev.pending_change != has_pending:
                    ev.pending_change = has_pending
                    updated += 1
                    for srow in session.scalars(
                        select(DonationSessionRow).where(
                            DonationSessionRow.event_id == ev.event_id,
                            DonationSessionRow.is_current.is_(True),
                        )
                    ).all():
                        srow.pending_change = has_pending
            session.commit()
        return updated

    def count_published_schedules(self) -> tuple[int, int]:
        """Return (active_events_count, active_sessions_count) for /v1/status."""
        with self._session_factory() as session:
            ev_count = int(
                session.scalar(
                    select(func.count(DonationEventRow.event_id)).where(
                        DonationEventRow.status != "removed_from_source"
                    )
                )
                or 0
            )
            ses_count = int(
                session.scalar(
                    select(func.count(DonationSessionRow.session_id)).where(
                        DonationSessionRow.is_current.is_(True),
                        DonationSessionRow.status != "removed_from_source",
                    )
                )
                or 0
            )
            return ev_count, ses_count
