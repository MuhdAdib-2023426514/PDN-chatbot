"""Dual-mode Qdrant vector store adapter (embedded local disk default + remote HTTP optional)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

from pdn_sync.indexing.lang import language_filter_values


@dataclass(frozen=True, slots=True)
class QdrantChunkPoint:
    """One approved unstructured chunk ready for Qdrant upsert."""

    point_id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QdrantSearchHit:
    """Ranked chunk hit returned from Qdrant vector search."""

    point_id: str
    score: float
    payload: dict[str, Any]


class QdrantVectorStore:
    """Manages approved-only PDN knowledge points in embedded local disk or remote Qdrant."""

    def __init__(
        self,
        *,
        collection_name: str = "pdn_knowledge_v1",
        url: str | None = None,
        path: Path | None = None,
        in_memory: bool = False,
        timeout: int = 30,
    ) -> None:
        self.collection_name = collection_name
        if in_memory:
            self.mode = "memory"
            self._client = QdrantClient(":memory:")
        elif url:
            self.mode = "remote"
            self._client = QdrantClient(url=url, timeout=timeout)
        else:
            self.mode = "local"
            target_path = path or Path("data/qdrant")
            target_path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(target_path))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> QdrantVectorStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def ensure_collection(self, vector_size: int) -> None:
        """Create the collection if missing, or verify vector dimensionality matches."""
        if not self._client.collection_exists(self.collection_name):
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            return

        info = self._client.get_collection(self.collection_name)
        vectors_cfg = info.config.params.vectors
        existing_size: int | None = None
        if isinstance(vectors_cfg, VectorParams):
            existing_size = int(vectors_cfg.size)
        elif isinstance(vectors_cfg, dict) and vectors_cfg:
            first_cfg = next(iter(vectors_cfg.values()))
            if isinstance(first_cfg, VectorParams):
                existing_size = int(first_cfg.size)

        if existing_size is not None and existing_size != vector_size:
            raise ValueError(
                f"Qdrant collection {self.collection_name!r} has vector size {existing_size}, "
                f"but embedding model produces {vector_size}. Use a new collection name or rebuild."
            )

    def upsert_points(self, points: Sequence[QdrantChunkPoint], *, batch_size: int = 64) -> int:
        """Upsert approved chunk points in bounded batches."""
        if not points:
            return 0
        total = 0
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            structs = [
                PointStruct(id=p.point_id, vector=p.vector, payload=p.payload) for p in batch
            ]
            self._client.upsert(
                collection_name=self.collection_name,
                points=structs,
                wait=True,
            )
            total += len(batch)
        return total

    def mark_publication_status(
        self,
        publication_id: str,
        *,
        status: str,
        valid_until: str | None,
    ) -> None:
        """Mark all points belonging to a superseded or retired publication as inactive."""
        if not self._client.collection_exists(self.collection_name):
            return
        selector = FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(
                        key="publication_id",
                        match=MatchValue(value=publication_id),
                    )
                ]
            )
        )
        self._client.set_payload(
            collection_name=self.collection_name,
            payload={"status": status, "valid_until": valid_until},
            points=selector,
            wait=True,
        )

    def update_pending_change(
        self,
        publication_id: str,
        *,
        pending_change: bool,
    ) -> None:
        """Update freshness pending_change flag on active points without re-embedding."""
        if not self._client.collection_exists(self.collection_name):
            return
        selector = FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(
                        key="publication_id",
                        match=MatchValue(value=publication_id),
                    )
                ]
            )
        )
        self._client.set_payload(
            collection_name=self.collection_name,
            payload={"pending_change": pending_change},
            points=selector,
            wait=True,
        )

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int = 5,
        lang: str = "any",
        category: str | None = None,
    ) -> list[QdrantSearchHit]:
        """Dense vector search strictly restricted to active status=='published' chunks."""
        if not self._client.collection_exists(self.collection_name):
            return []

        must_conditions: list[Condition] = [
            FieldCondition(key="status", match=MatchValue(value="published")),
        ]
        if category:
            must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
        lang_vals = language_filter_values(lang)
        if lang_vals is not None:
            must_conditions.append(FieldCondition(key="language", match=MatchAny(any=lang_vals)))

        response = self._client.query_points(
            collection_name=self.collection_name,
            query=list(query_vector),
            query_filter=Filter(must=must_conditions),
            limit=limit,
            with_payload=True,
        )
        hits: list[QdrantSearchHit] = []
        for pt in response.points:
            hits.append(
                QdrantSearchHit(
                    point_id=str(pt.id),
                    score=float(pt.score),
                    payload=dict(pt.payload or {}),
                )
            )
        return hits

    def health_check(self) -> tuple[bool, int]:
        """Return (is_healthy, published_or_total_points_count)."""
        try:
            if not self._client.collection_exists(self.collection_name):
                return True, 0
            count_res = self._client.count(
                collection_name=self.collection_name,
                count_filter=Filter(
                    must=[FieldCondition(key="status", match=MatchValue(value="published"))]
                ),
                exact=True,
            )
            return True, int(count_res.count)
        except Exception:
            return False, 0
