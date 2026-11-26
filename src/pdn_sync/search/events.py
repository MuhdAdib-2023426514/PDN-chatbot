"""Authoritative SQLite SQL + FTS5 (unicode61 + trigram) + RapidFuzz donation schedule search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import DonationSessionRow

_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")


@dataclass(frozen=True, slots=True)
class PublishedDonationSession:
    """Authoritative blood donation session returned by GET /v1/events."""

    event_id: str
    session_id: str
    organizer: str | None
    location_name: str | None
    address: str | None
    city: str | None
    state: str | None
    event_date: str | None
    time_start: str | None
    time_end: str | None
    session_label: str | None
    target_donors: int | None
    raw_cells: dict[str, str]
    status: str
    source_url: str
    last_verified_at: str
    pending_change: bool


def _build_u61_query(raw_q: str) -> str:
    tokens = _FTS_TOKEN_RE.findall(raw_q.strip())
    if not tokens:
        return ""
    parts: list[str] = []
    for tok in tokens[:12]:
        clean = tok.replace('"', "")
        if len(clean) >= 3:
            parts.append(f'"{clean}"*')
        else:
            parts.append(f'"{clean}"')
    return " OR ".join(parts)


def _build_trigram_query(raw_q: str) -> str:
    cleaned = " ".join(_FTS_TOKEN_RE.findall(raw_q.strip()))
    if len(cleaned) < 3:
        return ""
    return f'"{cleaned}"'


class DonationEventSearchService:
    """Deterministic SQL + FTS5 search over published blood donation sessions.

    FTS5/RapidFuzz are used strictly for venue/organizer candidate discovery and NEVER
    override authoritative date, date-range, state, or status SQL filters.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    def _to_result(self, row: DonationSessionRow) -> PublishedDonationSession:
        return PublishedDonationSession(
            event_id=row.event_id,
            session_id=row.session_id,
            organizer=row.organizer,
            location_name=row.location_name,
            address=row.address,
            city=row.city,
            state=row.state,
            event_date=row.event_date,
            time_start=row.time_start,
            time_end=row.time_end,
            session_label=row.session_label,
            target_donors=row.target_donors,
            raw_cells=json.loads(row.raw_cells_json),
            status=row.status,
            source_url=row.source_url,
            last_verified_at=row.last_verified_at_utc.isoformat(),
            pending_change=bool(row.pending_change),
        )

    def _apply_authoritative_filters(
        self,
        stmt: object,
        *,
        date: str | None,
        date_from: str | None,
        date_to: str | None,
        state: str | None,
        include_removed: bool,
    ) -> object:
        # Typing-friendly helper applied inline below
        raise NotImplementedError

    def _filter_rows_sql(
        self,
        session: Session,
        *,
        candidate_ids: list[str] | None,
        date: str | None,
        date_from: str | None,
        date_to: str | None,
        state: str | None,
        include_removed: bool,
        limit: int,
    ) -> list[DonationSessionRow]:
        stmt = select(DonationSessionRow).where(DonationSessionRow.is_current.is_(True))
        if not include_removed:
            stmt = stmt.where(DonationSessionRow.status != "removed_from_source")
        if candidate_ids is not None:
            if not candidate_ids:
                return []
            stmt = stmt.where(DonationSessionRow.session_id.in_(candidate_ids))
        if date:
            stmt = stmt.where(DonationSessionRow.event_date == date)
        if date_from:
            stmt = stmt.where(DonationSessionRow.event_date >= date_from)
        if date_to:
            stmt = stmt.where(DonationSessionRow.event_date <= date_to)
        if state:
            stmt = stmt.where(func.lower(DonationSessionRow.state) == state.strip().lower())

        stmt = stmt.order_by(
            DonationSessionRow.event_date.asc(),
            DonationSessionRow.time_start.asc(),
            DonationSessionRow.location_name.asc(),
        ).limit(limit)
        return list(session.scalars(stmt).all())

    def search_events(
        self,
        *,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        state: str | None = None,
        q: str | None = None,
        include_removed: bool = False,
        limit: int = 20,
    ) -> list[PublishedDonationSession]:
        """Search published blood donation sessions with authoritative date/state filtering."""
        clamped_limit = max(1, min(limit, 50))
        cleaned_q = (q or "").strip()
        cleaned_date = (date or "").strip() or None
        cleaned_from = (date_from or "").strip() or None
        cleaned_to = (date_to or "").strip() or None
        cleaned_state = (state or "").strip() or None

        with self._session_factory() as session:
            # 1. Pure structured SQL query when no free-text q is given
            if not cleaned_q:
                rows = self._filter_rows_sql(
                    session,
                    candidate_ids=None,
                    date=cleaned_date,
                    date_from=cleaned_from,
                    date_to=cleaned_to,
                    state=cleaned_state,
                    include_removed=include_removed,
                    limit=clamped_limit,
                )
                return [self._to_result(r) for r in rows]

            candidate_ids: list[str] = []
            seen: set[str] = set()

            # 2. Primary FTS5 unicode61 remove_diacritics 2
            u61_q = _build_u61_query(cleaned_q)
            if u61_q:
                u61_rows = session.execute(
                    text(
                        """
                        SELECT f.session_id
                        FROM donation_sessions_fts_u61 AS f
                        JOIN donation_sessions AS ds ON ds.session_id = f.session_id
                        WHERE donation_sessions_fts_u61 MATCH :q
                          AND ds.is_current = 1
                        ORDER BY bm25(
                            donation_sessions_fts_u61, 0.0, 0.0, 0.0, 2.0, 3.0, 1.5, 1.5, 1.0
                        )
                        LIMIT 200
                        """
                    ),
                    {"q": u61_q},
                ).all()
                for (sid,) in u61_rows:
                    s_str = str(sid)
                    if s_str not in seen:
                        seen.add(s_str)
                        candidate_ids.append(s_str)

            # 3. Auxiliary FTS5 trigram substring recall
            if len(candidate_ids) < clamped_limit:
                tri_q = _build_trigram_query(cleaned_q)
                if tri_q:
                    tri_rows = session.execute(
                        text(
                            """
                            SELECT f.session_id
                            FROM donation_sessions_fts_tri AS f
                            JOIN donation_sessions AS ds ON ds.session_id = f.session_id
                            WHERE donation_sessions_fts_tri MATCH :q
                              AND ds.is_current = 1
                            ORDER BY bm25(donation_sessions_fts_tri)
                            LIMIT 200
                            """
                        ),
                        {"q": tri_q},
                    ).all()
                    for (sid,) in tri_rows:
                        s_str = str(sid)
                        if s_str not in seen:
                            seen.add(s_str)
                            candidate_ids.append(s_str)

            # 4. Bounded RapidFuzz fallback for Malay/English venue/organizer typos
            if len(candidate_ids) < clamped_limit:
                pool = self._filter_rows_sql(
                    session,
                    candidate_ids=None,
                    date=cleaned_date,
                    date_from=cleaned_from,
                    date_to=cleaned_to,
                    state=cleaned_state,
                    include_removed=include_removed,
                    limit=500,
                )
                q_low = cleaned_q.lower()
                scored: list[tuple[float, str]] = []
                for row in pool:
                    if row.session_id in seen:
                        continue
                    hay = " ".join(
                        filter(
                            None,
                            [row.organizer, row.location_name, row.address, row.city, row.state],
                        )
                    ).lower()
                    score = max(
                        float(fuzz.partial_ratio(q_low, hay)),
                        float(fuzz.token_set_ratio(q_low, hay)),
                    )
                    if score >= 74.0:
                        scored.append((score, row.session_id))
                scored.sort(key=lambda pair: pair[0], reverse=True)
                for _score, sid in scored[:200]:
                    seen.add(sid)
                    candidate_ids.append(sid)

            # 5. Enforce authoritative SQL date/date_from/date_to/state/status constraints!
            filtered = self._filter_rows_sql(
                session,
                candidate_ids=candidate_ids,
                date=cleaned_date,
                date_from=cleaned_from,
                date_to=cleaned_to,
                state=cleaned_state,
                include_removed=include_removed,
                limit=clamped_limit,
            )
            return [self._to_result(r) for r in filtered]
