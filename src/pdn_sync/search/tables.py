"""Authoritative SQLite + FTS5 (unicode61 + trigram) + RapidFuzz structured table search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import StructuredTableRow, StructuredTableRowItem

_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")


@dataclass(frozen=True, slots=True)
class PublishedTableResult:
    """Published structured table record returned by GET /v1/tables."""

    table_id: str
    caption: str | None
    headers: list[str]
    rows: list[list[str]]
    units: list[str | None]
    source_url: str
    category: str
    last_verified_at: str
    source_effective_date: str | None
    valid_until: str | None
    pending_change: bool
    match_source: str = "exact"


def _build_u61_fts_query(raw_q: str) -> str:
    """Safely convert free-form Malay/English query into quoted FTS5 prefix tokens."""
    tokens = _FTS_TOKEN_RE.findall(raw_q.strip())
    if not tokens:
        return ""
    # Prefix-match tokens >= 3 chars for Malay affix/prefix recall (e.g. derma* -> dermawan/derma)
    parts: list[str] = []
    for tok in tokens[:12]:
        escaped = tok.replace('"', "")
        if len(escaped) >= 3:
            parts.append(f'"{escaped}"*')
        else:
            parts.append(f'"{escaped}"')
    return " OR ".join(parts)


def _build_trigram_query(raw_q: str) -> str:
    """Build a safe quoted trigram FTS5 query (requires >= 3 chars)."""
    cleaned = " ".join(_FTS_TOKEN_RE.findall(raw_q.strip()))
    if len(cleaned) < 3:
        return ""
    return f'"{cleaned}"'


class StructuredTableSearchService:
    """Queries published, non-superseded, non-retired structured tables in SQLite + FTS5."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    def _hydrate_tables(
        self,
        session: Session,
        ordered_table_ids: list[tuple[str, str]],
        *,
        source_url: str | None = None,
    ) -> list[PublishedTableResult]:
        if not ordered_table_ids:
            return []

        ids = [tid for tid, _ in ordered_table_ids]
        stmt = select(StructuredTableRow).where(
            StructuredTableRow.table_id.in_(ids),
            StructuredTableRow.status == "published",
        )
        if source_url:
            stmt = stmt.where(StructuredTableRow.source_url == source_url)

        table_map = {row.table_id: row for row in session.scalars(stmt).all()}
        if not table_map:
            return []

        row_items = list(
            session.scalars(
                select(StructuredTableRowItem)
                .where(StructuredTableRowItem.table_id.in_(list(table_map.keys())))
                .order_by(StructuredTableRowItem.table_id, StructuredTableRowItem.row_index)
            ).all()
        )
        rows_by_table: dict[str, list[list[str]]] = {}
        for item in row_items:
            rows_by_table.setdefault(item.table_id, []).append(json.loads(item.cells_json))

        results: list[PublishedTableResult] = []
        for tid, match_src in ordered_table_ids:
            trow = table_map.get(tid)
            if trow is None:
                continue
            results.append(
                PublishedTableResult(
                    table_id=trow.table_id,
                    caption=trow.caption,
                    headers=json.loads(trow.headers_json),
                    rows=rows_by_table.get(trow.table_id, []),
                    units=json.loads(trow.units_json),
                    source_url=trow.source_url,
                    category=trow.category,
                    last_verified_at=trow.last_verified_at_utc.isoformat(),
                    source_effective_date=trow.source_effective_date,
                    valid_until=(
                        trow.valid_until_utc.isoformat() if trow.valid_until_utc else None
                    ),
                    pending_change=bool(trow.pending_change),
                    match_source=match_src,
                )
            )
        return results

    def search_tables(
        self,
        *,
        q: str | None = None,
        source_url: str | None = None,
        limit: int = 10,
    ) -> list[PublishedTableResult]:
        """Search published structured tables using FTS5 unicode61 -> trigram -> RapidFuzz."""
        clamped_limit = max(1, min(limit, 50))
        cleaned_q = (q or "").strip()
        cleaned_url = (source_url or "").strip() or None

        with self._session_factory() as session:
            # 1. No text query: deterministic SQL filter by source_url / recent published
            if not cleaned_q:
                stmt = (
                    select(StructuredTableRow.table_id)
                    .where(StructuredTableRow.status == "published")
                    .order_by(StructuredTableRow.source_url, StructuredTableRow.table_index)
                    .limit(clamped_limit)
                )
                if cleaned_url:
                    stmt = stmt.where(StructuredTableRow.source_url == cleaned_url)
                ids = [(str(tid), "sql_filter") for tid in session.scalars(stmt).all()]
                return self._hydrate_tables(session, ids, source_url=cleaned_url)

            ranked: list[tuple[str, str]] = []
            seen: set[str] = set()

            # 2. Primary FTS5 unicode61 remove_diacritics 2 (BM25 ranked)
            u61_q = _build_u61_fts_query(cleaned_q)
            if u61_q:
                u61_rows = session.execute(
                    text(
                        """
                        SELECT f.table_id
                        FROM structured_tables_fts_u61 AS f
                        JOIN structured_tables AS st ON st.table_id = f.table_id
                        WHERE structured_tables_fts_u61 MATCH :q
                          AND st.status = 'published'
                          AND (:url IS NULL OR st.source_url = :url)
                        ORDER BY bm25(structured_tables_fts_u61, 0.0, 0.0, 0.0, 3.0, 2.0, 1.0)
                        LIMIT :lim
                        """
                    ),
                    {"q": u61_q, "url": cleaned_url, "lim": clamped_limit},
                ).all()
                for (tid,) in u61_rows:
                    t_str = str(tid)
                    if t_str not in seen:
                        seen.add(t_str)
                        ranked.append((t_str, "fts5_unicode61"))

            # 3. Auxiliary FTS5 trigram substring search (if more candidates needed)
            if len(ranked) < clamped_limit:
                tri_q = _build_trigram_query(cleaned_q)
                if tri_q:
                    tri_rows = session.execute(
                        text(
                            """
                            SELECT f.table_id
                            FROM structured_tables_fts_tri AS f
                            JOIN structured_tables AS st ON st.table_id = f.table_id
                            WHERE structured_tables_fts_tri MATCH :q
                              AND st.status = 'published'
                              AND (:url IS NULL OR st.source_url = :url)
                            ORDER BY bm25(structured_tables_fts_tri)
                            LIMIT :lim
                            """
                        ),
                        {"q": tri_q, "url": cleaned_url, "lim": clamped_limit},
                    ).all()
                    for (tid,) in tri_rows:
                        t_str = str(tid)
                        if t_str not in seen:
                            seen.add(t_str)
                            ranked.append((t_str, "fts5_trigram"))

            # 4. Bounded RapidFuzz fallback for Malay/English typos when FTS5 yields < limit
            if len(ranked) < clamped_limit:
                cand_rows = session.execute(
                    text(
                        """
                        SELECT f.table_id, f.caption, f.headers_text, f.rows_text
                        FROM structured_tables_fts_u61 AS f
                        JOIN structured_tables AS st ON st.table_id = f.table_id
                        WHERE st.status = 'published'
                          AND (:url IS NULL OR st.source_url = :url)
                        LIMIT 500
                        """
                    ),
                    {"url": cleaned_url},
                ).all()
                q_lower = cleaned_q.lower()
                scored_fuzzy: list[tuple[float, str]] = []
                for tid, cap, hdr, rtxt in cand_rows:
                    t_str = str(tid)
                    if t_str in seen:
                        continue
                    haystack = f"{cap or ''} {hdr or ''} {str(rtxt or '')[:1500]}".lower()
                    score = max(
                        float(fuzz.partial_ratio(q_lower, haystack)),
                        float(fuzz.token_set_ratio(q_lower, haystack)),
                    )
                    if score >= 74.0:
                        scored_fuzzy.append((score, t_str))
                scored_fuzzy.sort(key=lambda pair: pair[0], reverse=True)
                for _score, t_str in scored_fuzzy[: clamped_limit - len(ranked)]:
                    seen.add(t_str)
                    ranked.append((t_str, "rapidfuzz"))

            return self._hydrate_tables(session, ranked[:clamped_limit], source_url=cleaned_url)
