"""Lightweight SQLite FTS5 BM25 + Reciprocal Rank Fusion (RRF) for optional hybrid retrieval."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select, text

from pdn_sync.indexing.qdrant_store import QdrantSearchHit
from pdn_sync.persistence.db import create_session_factory
from pdn_sync.persistence.models import (
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    PublicationVersionRow,
)

_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")


@dataclass(frozen=True, slots=True)
class ChunkSearchCandidate:
    """Unified published chunk candidate produced by Dense Qdrant, SQLite FTS5 BM25, or RRF."""

    chunk_id: str
    score: float
    payload: dict[str, Any]
    source: str = "dense"


def build_chunk_fts_query(raw_q: str) -> str:
    """Safely convert free-form Malay/English query into quoted FTS5 OR tokens."""
    tokens = _FTS_TOKEN_RE.findall(raw_q.strip())
    if not tokens:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for tok in tokens[:16]:
        lowered = tok.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        escaped = tok.replace('"', "")
        if len(escaped) >= 3:
            parts.append(f'"{escaped}"*')
        else:
            parts.append(f'"{escaped}"')
    return " OR ".join(parts)


class ChunkBM25SearchService:
    """Queries published knowledge chunks via SQLite FTS5 `knowledge_chunks_fts_u61` BM25."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    def search_chunks(
        self,
        *,
        q: str,
        lang: str = "any",
        category: str | None = None,
        limit: int = 20,
    ) -> list[ChunkSearchCandidate]:
        """Return published, non-superseded, non-retired chunks ranked by FTS5 BM25."""
        fts_q = build_chunk_fts_query(q)
        if not fts_q:
            return []

        clamped = max(1, min(limit, 100))
        fetch_cap = clamped * 3

        with self._session_factory() as session:
            sql = """
                SELECT chunk_id, bm25(knowledge_chunks_fts_u61, 1.0, 1.0, 1.0, 1.0, 3.0, 2.0, 1.0)
                FROM knowledge_chunks_fts_u61
                WHERE knowledge_chunks_fts_u61 MATCH :fts_q
            """
            params: dict[str, Any] = {"fts_q": fts_q, "lim": fetch_cap}
            if lang in {"ms", "en"}:
                sql += " AND language = :lang"
                params["lang"] = lang
            if category:
                sql += " AND category = :cat"
                params["cat"] = category
            sql += (
                " ORDER BY bm25(knowledge_chunks_fts_u61, "
                "1.0, 1.0, 1.0, 1.0, 3.0, 2.0, 1.0) ASC LIMIT :lim"
            )

            rows = session.execute(text(sql), params).all()
            if not rows:
                return []

            ordered_ids = [str(r[0]) for r in rows]
            bm25_map = {str(r[0]): float(r[1]) for r in rows}

            # Hydrate ONLY currently published chunks (hard Gate B & retirement guard)
            hydrated = session.execute(
                select(KnowledgeChunkRow, KnowledgeDocumentRow, PublicationVersionRow)
                .join(
                    KnowledgeDocumentRow,
                    KnowledgeDocumentRow.doc_id == KnowledgeChunkRow.doc_id,
                )
                .join(
                    PublicationVersionRow,
                    PublicationVersionRow.publication_id == KnowledgeChunkRow.publication_id,
                )
                .where(
                    KnowledgeChunkRow.chunk_id.in_(ordered_ids),
                    PublicationVersionRow.status == "published",
                    KnowledgeDocumentRow.status == "published",
                )
            ).all()

            by_cid = {ch.chunk_id: (ch, doc, pub) for ch, doc, pub in hydrated}

            results: list[ChunkSearchCandidate] = []
            for cid in ordered_ids:
                item = by_cid.get(cid)
                if item is None:
                    continue
                ch, doc, _pub = item
                # Convert negative SQLite BM25 score (smaller = better) to positive monotonic score
                raw_bm25 = bm25_map.get(cid, 0.0)
                positive_score = (
                    1.0 / (1.0 + max(0.0, raw_bm25 + 20.0)) if raw_bm25 >= 0 else abs(raw_bm25)
                )
                payload: dict[str, Any] = {
                    "chunk_id": ch.chunk_id,
                    "document_id": doc.doc_id,
                    "publication_id": ch.publication_id,
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
                    "valid_until": (
                        doc.valid_until_utc.isoformat() if doc.valid_until_utc else None
                    ),
                    "pending_change": bool(doc.pending_change),
                    "status": "published",
                    "text": ch.text,
                }
                results.append(
                    ChunkSearchCandidate(
                        chunk_id=ch.chunk_id,
                        score=round(positive_score, 6),
                        payload=payload,
                        source="bm25",
                    )
                )
                if len(results) >= clamped:
                    break
            return results


def fuse_rrf(
    *,
    dense_hits: Sequence[QdrantSearchHit],
    bm25_hits: Sequence[ChunkSearchCandidate],
    k: int = 60,
    limit: int = 5,
    dense_weight: float = 1.0,
    bm25_weight: float = 1.0,
) -> list[ChunkSearchCandidate]:
    """Deterministically fuse dense vector hits and FTS5 BM25 hits via Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}
    tie_rank: dict[str, tuple[int, int]] = {}

    for rank, hit in enumerate(dense_hits, start=1):
        cid = str(hit.payload.get("chunk_id") or hit.point_id)
        scores[cid] = scores.get(cid, 0.0) + (dense_weight / float(k + rank))
        payloads.setdefault(cid, dict(hit.payload))
        sources.setdefault(cid, set()).add("dense")
        prev = tie_rank.get(cid, (10_000, 10_000))
        tie_rank[cid] = (min(prev[0], rank), prev[1])

    for rank, bhit in enumerate(bm25_hits, start=1):
        cid = bhit.chunk_id
        scores[cid] = scores.get(cid, 0.0) + (bm25_weight / float(k + rank))
        payloads.setdefault(cid, dict(bhit.payload))
        sources.setdefault(cid, set()).add("bm25")
        prev = tie_rank.get(cid, (10_000, 10_000))
        tie_rank[cid] = (prev[0], min(prev[1], rank))

    ranked_ids = sorted(
        scores.keys(),
        key=lambda cid: (-scores[cid], tie_rank[cid][0], tie_rank[cid][1], cid),
    )

    fused: list[ChunkSearchCandidate] = []
    for cid in ranked_ids[: max(1, limit)]:
        src_label = "+".join(sorted(sources[cid]))
        fused.append(
            ChunkSearchCandidate(
                chunk_id=cid,
                score=round(scores[cid], 6),
                payload=payloads[cid],
                source=src_label,
            )
        )
    return fused
