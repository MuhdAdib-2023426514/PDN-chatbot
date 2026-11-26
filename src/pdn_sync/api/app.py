"""Read-only FastAPI chatbot knowledge service (AGENTS.md Section 9)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query
from sqlalchemy import text

from pdn_sync.api.schemas import (
    DonationEventsResponse,
    DonationSessionHit,
    HealthResponse,
    KnowledgeChunkHit,
    KnowledgeSearchResponse,
    PublishedTableHit,
    StatusResponse,
    TableSearchResponse,
)
from pdn_sync.config import AppSettings, load_ingestion_policy
from pdn_sync.indexing.embedder import EmbeddingBackend, create_embedder
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.publication_repo import PublicationRepository
from pdn_sync.persistence.schedule_repo import ScheduleRepository
from pdn_sync.search.events import DonationEventSearchService
from pdn_sync.search.tables import StructuredTableSearchService


def create_app(
    *,
    settings: AppSettings | None = None,
    embedder: EmbeddingBackend | None = None,
    qdrant_store: QdrantVectorStore | None = None,
) -> FastAPI:
    """Create the strictly read-only versioned chatbot HTTP API."""
    cfg = settings or AppSettings()
    upgrade_database(cfg.sqlite_path)
    engine = create_sqlite_engine(cfg.sqlite_path)
    pub_repo = PublicationRepository(engine)
    sched_repo = ScheduleRepository(engine)
    table_search = StructuredTableSearchService(engine)
    event_search = DonationEventSearchService(engine)

    owns_qdrant = qdrant_store is None
    active_qdrant: QdrantVectorStore | None = qdrant_store
    active_embedder: EmbeddingBackend | None = embedder

    def _get_qdrant() -> QdrantVectorStore:
        nonlocal active_qdrant
        if active_qdrant is None:
            active_qdrant = QdrantVectorStore(
                collection_name=cfg.qdrant_collection,
                url=cfg.qdrant_url,
                path=cfg.qdrant_path,
            )
        return active_qdrant

    def _get_embedder() -> EmbeddingBackend:
        nonlocal active_embedder
        if active_embedder is None:
            status = pub_repo.get_status_summary()
            model_name = status.get("active_embedding_model")
            if not model_name:
                try:
                    model_name = load_ingestion_policy(
                        cfg.ingestion_policy_file
                    ).indexing.embedding_model
                except Exception:
                    model_name = cfg.embedding_model
            active_embedder = create_embedder(str(model_name))
        return active_embedder

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _get_qdrant()
        try:
            yield
        finally:
            if owns_qdrant and active_qdrant is not None:
                active_qdrant.close()
            engine.dispose()

    app = FastAPI(
        title="PDN Multimodal Knowledge API",
        version="0.1.0",
        description="Read-only chatbot retrieval API serving Gate-B-approved PDN knowledge.",
        lifespan=lifespan,
    )

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        sqlite_ok = True
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            sqlite_ok = False

        qd = _get_qdrant()
        qd_ok, qd_count = qd.health_check()
        overall = "ok" if (sqlite_ok and qd_ok) else "degraded"
        return HealthResponse(
            status=overall,
            sqlite="ok" if sqlite_ok else "error",
            qdrant="ok" if qd_ok else "error",
            qdrant_mode=qd.mode,
            qdrant_published_points=qd_count,
        )

    @app.get("/v1/status", response_model=StatusResponse)
    def v1_status() -> StatusResponse:
        summary = pub_repo.get_status_summary()
        ev_count, ses_count = sched_repo.count_published_schedules()
        qd = _get_qdrant()
        _, qd_count = qd.health_check()
        return StatusResponse(
            **summary,
            published_donation_events=ev_count,
            published_donation_sessions=ses_count,
            qdrant_mode=qd.mode,
            qdrant_published_points=qd_count,
        )

    @app.get("/v1/knowledge/search", response_model=KnowledgeSearchResponse)
    def search_knowledge(
        q: Annotated[str, Query(min_length=1, max_length=500, description="Search query")],
        lang: Annotated[Literal["ms", "en", "any"], Query()] = "any",
        category: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 5,
    ) -> KnowledgeSearchResponse:
        emb = _get_embedder()
        qd = _get_qdrant()
        q_vec = emb.embed_query(q)
        raw_hits = qd.search(q_vec, limit=limit, lang=lang, category=category)

        results: list[KnowledgeChunkHit] = []
        for hit in raw_hits:
            p = hit.payload
            results.append(
                KnowledgeChunkHit(
                    chunk_id=str(p.get("chunk_id") or hit.point_id),
                    text=str(p.get("text") or ""),
                    score=round(float(hit.score), 6),
                    title=p.get("title"),
                    section=p.get("section"),
                    category=str(p.get("category") or "uncategorized"),
                    language=str(p.get("language") or "ms"),
                    source_url=str(p.get("source_url") or ""),
                    asset_url=p.get("asset_url"),
                    content_type=p.get("content_type"),
                    last_verified_at=str(p.get("last_verified_at") or ""),
                    source_effective_date=p.get("source_effective_date"),
                    valid_until=p.get("valid_until"),
                    pending_change=bool(p.get("pending_change", False)),
                )
            )
        return KnowledgeSearchResponse(
            query=q,
            lang=lang,
            category=category,
            total=len(results),
            results=results,
        )

    @app.get("/v1/tables", response_model=TableSearchResponse)
    def search_tables(
        q: Annotated[str | None, Query(max_length=500)] = None,
        source_url: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ) -> TableSearchResponse:
        hits = table_search.search_tables(q=q, source_url=source_url, limit=limit)
        items = [
            PublishedTableHit(
                table_id=h.table_id,
                caption=h.caption,
                headers=h.headers,
                rows=h.rows,
                units=h.units,
                source_url=h.source_url,
                category=h.category,
                last_verified_at=h.last_verified_at,
                source_effective_date=h.source_effective_date,
                valid_until=h.valid_until,
                pending_change=h.pending_change,
            )
            for h in hits
        ]
        return TableSearchResponse(
            query=q,
            source_url=source_url,
            total=len(items),
            results=items,
        )

    @app.get("/v1/events", response_model=DonationEventsResponse)
    def search_events(
        date: Annotated[
            str | None,
            Query(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Exact date YYYY-MM-DD"),
        ] = None,
        date_from: Annotated[
            str | None,
            Query(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Start date YYYY-MM-DD"),
        ] = None,
        date_to: Annotated[
            str | None,
            Query(pattern=r"^\d{4}-\d{2}-\d{2}$", description="End date YYYY-MM-DD"),
        ] = None,
        state: Annotated[str | None, Query(max_length=64)] = None,
        q: Annotated[str | None, Query(max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> DonationEventsResponse:
        hits = event_search.search_events(
            date=date,
            date_from=date_from,
            date_to=date_to,
            state=state,
            q=q,
            limit=limit,
        )
        items = [
            DonationSessionHit(
                event_id=h.event_id,
                session_id=h.session_id,
                organizer=h.organizer,
                location_name=h.location_name,
                address=h.address,
                city=h.city,
                state=h.state,
                event_date=h.event_date,
                time_start=h.time_start,
                time_end=h.time_end,
                session_label=h.session_label,
                target_donors=h.target_donors,
                raw_cells=h.raw_cells,
                status=h.status,
                source_url=h.source_url,
                last_verified_at=h.last_verified_at,
                pending_change=h.pending_change,
            )
            for h in hits
        ]
        return DonationEventsResponse(
            date=date,
            date_from=date_from,
            date_to=date_to,
            state=state,
            query=q,
            total=len(items),
            results=items,
        )

    return app
