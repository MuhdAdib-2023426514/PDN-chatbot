"""Unit tests for Phase 5 hybrid RRF retrieval, FTS5 chunk BM25, structured logging, and policy."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdn_sync.domain.policy import IngestionPolicyConfig, ObservabilityPolicy, RetryPolicy
from pdn_sync.indexing.qdrant_store import QdrantSearchHit
from pdn_sync.ops.reconcile import reconcile_fts5
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import (
    CrawlResourceRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    PublicationVersionRow,
    ResourceVersionRow,
    StagedArtifactRow,
)
from pdn_sync.persistence.publication_repo import PublicationRepository
from pdn_sync.search.hybrid import (
    ChunkBM25SearchService,
    ChunkSearchCandidate,
    build_chunk_fts_query,
    fuse_rrf,
)
from pdn_sync.telemetry.logging import configure_logging, get_logger, log_context


def test_retry_and_observability_policy_validation() -> None:
    cfg = IngestionPolicyConfig()
    assert cfg.indexing.retrieval_mode == "dense"
    assert cfg.retry.max_attempts == 3
    assert cfg.observability.max_dlq_items == 0

    with pytest.raises(ValidationError):
        RetryPolicy(backoff_base_seconds=10.0, backoff_max_seconds=5.0)

    obs = ObservabilityPolicy(homepage_overdue_hours=6, max_pending_gate_b=10)
    assert obs.homepage_overdue_hours == 6
    assert obs.max_pending_gate_b == 10


def test_build_chunk_fts_query_handles_special_chars_and_deduplicates() -> None:
    q = build_chunk_fts_query('  "Waktu" operasi: PDN! (waktu) O+  ')
    assert '"Waktu"*' in q
    assert '"operasi"*' in q
    assert '"PDN"*' in q
    assert '"O"' in q
    # Duplicate "waktu" collapsed
    assert q.lower().count("waktu") == 1
    assert build_chunk_fts_query("   !!!   ") == ""


def test_fuse_rrf_combines_dense_and_bm25_deterministically() -> None:
    dense = [
        QdrantSearchHit(point_id="p1", score=0.92, payload={"chunk_id": "c1", "text": "A"}),
        QdrantSearchHit(point_id="p2", score=0.81, payload={"chunk_id": "c2", "text": "B"}),
        QdrantSearchHit(point_id="p3", score=0.70, payload={"chunk_id": "c3", "text": "C"}),
    ]
    bm25 = [
        ChunkSearchCandidate(chunk_id="c2", score=9.5, payload={"chunk_id": "c2", "text": "B"}),
        ChunkSearchCandidate(chunk_id="c4", score=6.0, payload={"chunk_id": "c4", "text": "D"}),
        ChunkSearchCandidate(chunk_id="c1", score=4.2, payload={"chunk_id": "c1", "text": "A"}),
    ]
    fused = fuse_rrf(dense_hits=dense, bm25_hits=bm25, k=60, limit=3)
    assert len(fused) == 3
    # c1 (dense#1 + bm25#3) vs c2 (dense#2 + bm25#1): c2 has slightly higher RRF score!
    assert fused[0].chunk_id == "c2"
    assert fused[1].chunk_id == "c1"
    assert fused[0].source == "bm25+dense"
    assert fused[2].chunk_id == "c4"
    assert fused[2].source == "bm25"


def test_chunk_bm25_search_only_returns_published_and_filters_lang_category(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bm25.db"
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        from pdn_sync.persistence.db import create_session_factory

        now = datetime.now(UTC)
        sf = create_session_factory(engine)
        with sf() as session:
            for idx, (status, lang, cat, title, text) in enumerate(
                [
                    (
                        "published",
                        "ms",
                        "eligibility",
                        "Syarat Kelayakan Derma Darah",
                        "Penderma darah perlu berumur 18 hingga 60 tahun dan berat minimum 45 kg.",
                    ),
                    (
                        "retired",
                        "ms",
                        "eligibility",
                        "Notis Lama Retired",
                        "Syarat kelayakan lama penderma darah yang sudah bersara.",
                    ),
                    (
                        "published",
                        "en",
                        "operating_hours",
                        "PDN Operating Hours",
                        "National Blood Centre operating hours are Mon-Fri 7:30am to 8:00pm.",
                    ),
                ],
                start=1,
            ):
                rid = f"res_{idx}"
                vid = f"ver_{idx}"
                aid = f"art_{idx}"
                pid = f"pub_{idx}"
                did = f"doc_{idx}"
                cid = f"chk_{idx}"
                session.add(
                    CrawlResourceRow(
                        resource_id=rid,
                        canonical_key=f"key_{idx}",
                        url=f"https://pdn.gov.my/v2/page-{idx}",
                        resource_type="page",
                        category=cat,
                        first_seen_at_utc=now,
                        last_seen_at_utc=now,
                    )
                )
                session.flush()
                session.add(
                    ResourceVersionRow(
                        version_id=vid,
                        resource_id=rid,
                        version_seq=1,
                        content_hash=f"ch_{idx}",
                        semantic_hash=f"sh_{idx}",
                        captured_at_utc=now,
                        last_verified_at_utc=now,
                        extractor_name="html",
                        extractor_version="1.0",
                    )
                )
                session.flush()
                session.add(
                    StagedArtifactRow(
                        artifact_id=aid,
                        resource_id=rid,
                        resource_version_id=vid,
                        source_url=f"https://pdn.gov.my/v2/page-{idx}",
                        resource_type="page",
                        category=cat,
                        content_hash=f"ch_{idx}",
                        semantic_hash=f"sh_{idx}",
                        extractor_name="html",
                        extractor_version="1.0",
                        json_path="/tmp/a.json",
                        markdown_path="/tmp/a.md",
                        validation_status="valid",
                        review_status=status,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                session.flush()
                session.add(
                    PublicationVersionRow(
                        publication_id=pid,
                        resource_id=rid,
                        artifact_id=aid,
                        resource_version_id=vid,
                        version_seq=1,
                        semantic_hash=f"sh_{idx}",
                        embedding_model="BAAI/bge-m3",
                        qdrant_collection="pdn_test",
                        status=status,
                        valid_from_utc=now,
                        published_at_utc=now,
                    )
                )
                session.flush()
                session.add(
                    KnowledgeDocumentRow(
                        doc_id=did,
                        publication_id=pid,
                        resource_id=rid,
                        artifact_id=aid,
                        resource_version_id=vid,
                        source_url=f"https://pdn.gov.my/v2/page-{idx}",
                        resource_type="page",
                        category=cat,
                        title=title,
                        language=lang,
                        last_verified_at_utc=now,
                        valid_from_utc=now,
                        status=status,
                    )
                )
                session.flush()
                session.add(
                    KnowledgeChunkRow(
                        chunk_id=cid,
                        point_id=f"pt_{idx}",
                        doc_id=did,
                        publication_id=pid,
                        chunk_index=0,
                        section=title,
                        language=lang,
                        text=text,
                        char_count=len(text),
                    )
                )
            session.commit()

        rec = reconcile_fts5(engine)
        assert rec.knowledge_chunks_synced == 2  # retired chunk excluded!
        assert rec.after_mismatches["knowledge_chunks"] == 0

        bm25 = ChunkBM25SearchService(engine)
        ms_hits = bm25.search_chunks(q="syarat kelayakan penderma", lang="ms")
        assert len(ms_hits) == 1
        assert ms_hits[0].chunk_id == "chk_1"
        assert ms_hits[0].payload["source_url"] == "https://pdn.gov.my/v2/page-1"

        en_hits = bm25.search_chunks(q="operating hours", lang="en", category="operating_hours")
        assert len(en_hits) == 1
        assert en_hits[0].chunk_id == "chk_3"

        # Unused import guard check
        assert PublicationRepository(engine).get_status_summary()["published_chunks"] == 2
    finally:
        engine.dispose()


def test_structured_logging_json_format_and_redaction() -> None:
    buf = io.StringIO()
    configure_logging(log_format="json", log_level="INFO", stream=buf)
    log = get_logger("pdn_sync.test")

    with log_context(
        run_id="run_123",
        resource_id="res_abc",
        version_id="ver_1",
        task_id="task_9",
        correlation_id="corr_xyz",
    ):
        log.info(
            "fetch_completed",
            api_key="super-secret-token",
            authorization="Bearer xyz",
            raw_html="<html><body>sensitive</body></html>",
            long_field="x" * 700,
            status_code=200,
        )

    line = buf.getvalue().strip()
    parsed = json.loads(line)
    assert parsed["event"] == "fetch_completed"
    assert parsed["level"] == "info"
    assert parsed["run_id"] == "run_123"
    assert parsed["resource_id"] == "res_abc"
    assert parsed["correlation_id"] == "corr_xyz"
    assert parsed["api_key"] == "[REDACTED]"
    assert parsed["authorization"] == "[REDACTED]"
    assert parsed["raw_html"] == "[REDACTED]"
    assert "<truncated:700>" in parsed["long_field"]
    assert parsed["status_code"] == 200
