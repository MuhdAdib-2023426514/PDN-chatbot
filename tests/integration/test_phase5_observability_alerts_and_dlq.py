"""Integration tests for Phase 5 outage recovery, outbox/DLQ, FTS5 repair, /metrics, and alerts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from pdn_sync.api.app import create_app
from pdn_sync.cli import app as cli_app
from pdn_sync.config import AppSettings
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType, RetryPolicy
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantChunkPoint, QdrantVectorStore
from pdn_sync.ops.reconcile import reconcile_fts5, reconcile_outbox
from pdn_sync.persistence.db import create_session_factory, create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import (
    CrawlResourceRow,
    ResourceVersionRow,
    StagedArtifactRow,
    SyncRunRow,
)
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.staging_repo import StagingRepository
from pdn_sync.staging.semantic_hash import SemanticTextBlock
from pdn_sync.telemetry.alerts import evaluate_alerts


class _DeterministicFakeEmbedder:
    """Fast deterministic 8-dim embedder for offline Phase 5 reliability integration tests."""

    def __init__(self, model_name: str = "fake/bge-m3-test") -> None:
        self.model_name = model_name
        self.dimension = 8

    def _vec(self, text_val: str) -> list[float]:
        low = text_val.lower()
        return [
            1.0 if "darah" in low or "blood" in low else 0.1,
            1.0 if "waktu" in low or "hours" in low else 0.1,
            1.0 if "syarat" in low or "eligib" in low else 0.1,
            1.0 if "midvalley" in low else 0.1,
            0.2,
            0.2,
            0.2,
            0.2,
        ]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vec(query)


class _FlakyQdrantStore(QdrantVectorStore):
    """QdrantVectorStore wrapper that can simulate transient or persistent outages."""

    def __init__(self, *, collection_name: str, path: Path) -> None:
        super().__init__(collection_name=collection_name, path=path)
        self.fail_upserts_remaining: int = 0
        self.force_health_down: bool = False

    def upsert_points(self, points: Sequence[QdrantChunkPoint], *, batch_size: int = 64) -> int:
        if self.fail_upserts_remaining > 0:
            self.fail_upserts_remaining -= 1
            raise ConnectionError("Simulated Qdrant network outage")
        return super().upsert_points(points, batch_size=batch_size)

    def health_check(self) -> tuple[bool, int]:
        if self.force_health_down:
            return False, 0
        return super().health_check()


def _write_test_scope_and_policy(
    tmp_path: Path, *, retrieval_mode: str = "dense"
) -> tuple[Path, Path]:
    scope_path = tmp_path / "crawl_scope.yaml"
    scope_path.write_text(
        """
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2/
exclusions:
  urls: []
  patterns: []
approvals:
  pages:
    urls:
      - url: https://pdn.gov.my/v2/index.php/kelayakan
        reason: test_approved
        check_interval: 24h
        approved_by: qa-owner
        approved_at: "2026-11-26T00:00:00Z"
    patterns: []
  image_posters:
    urls: []
    patterns: []
  documents:
    urls: []
    patterns: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy_path = tmp_path / "ingestion_policy.yaml"
    policy_path.write_text(
        f"""
schema_version: 1
approval_mode: manual
retirement_consecutive_runs: 2
domain_timezone: Asia/Kuala_Lumpur
indexing:
  embedding_model: fake/bge-m3-test
  retrieval_mode: {retrieval_mode}
retry:
  max_attempts: 2
  backoff_base_seconds: 0.01
  backoff_max_seconds: 0.05
  jitter_ratio: 0.0
observability:
  homepage_overdue_hours: 12
  calendar_overdue_hours: 18
  site_overdue_hours: 48
  discovery_overdue_days: 14
  max_pending_gate_b: 5
  max_pending_gate_b_age_hours: 24
  max_quarantined_gate_b: 0
  max_dlq_items: 0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return scope_path, policy_path


def _stage_and_approve_one_article(*, db_path: Path, staging_dir: Path, suffix: str = "1") -> str:
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    now = datetime.now(UTC)
    rid = f"res_p5_{suffix}"
    vid = f"ver_p5_{suffix}"
    aid = f"art_p5_{suffix}"
    url = f"https://pdn.gov.my/v2/index.php/kelayakan-{suffix}"
    try:
        sf = create_session_factory(engine)
        staging_dir.mkdir(parents=True, exist_ok=True)
        json_path = staging_dir / f"{aid}.json"
        md_path = staging_dir / f"{aid}.md"
        result = ExtractionResult(
            artifact_id=aid,
            resource_id=rid,
            resource_version_id=vid,
            extractor_name="html_bs4",
            extractor_version="1.0.0",
            source_url=url,
            resource_type=ResourceType.PAGE,
            category=DiscoveryCategory.FAQ,
            captured_at_utc=now,
            extracted_at_utc=now,
            language="ms",
            content_type="text/html",
            title="Syarat Kelayakan Derma Darah",
            text_blocks=[
                SemanticTextBlock(
                    order=0,
                    heading_level=2,
                    text="Syarat Asas",
                ),
                SemanticTextBlock(
                    order=1,
                    text="Penderma darah mesti berumur 18 hingga 60 tahun dan berat minimum 45 kg.",
                ),
            ],
            tables=[
                ExtractedTable(
                    table_index=0,
                    caption="Had Umur Penderma",
                    headers=["Kategori", "Had"],
                    rows=[["Penderma Baru", "18-60 tahun"]],
                    units=[None, None],
                )
            ],
            content_hash=f"ch_p5_{suffix}",
            semantic_hash="placeholder",
            validation_status=ValidationStatus.VALID,
        )
        real_sh = result.recompute_semantic_hash()
        result = result.model_copy(update={"semantic_hash": real_sh})
        json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        md_path.write_text("# Syarat Kelayakan Derma Darah\n", encoding="utf-8")

        with sf() as session:
            session.add(
                CrawlResourceRow(
                    resource_id=rid,
                    canonical_key=f"ckey_p5_{suffix}",
                    url=url,
                    resource_type=ResourceType.PAGE.value,
                    category=DiscoveryCategory.FAQ.value,
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
                    content_hash=f"ch_p5_{suffix}",
                    semantic_hash=real_sh,
                    captured_at_utc=now,
                    last_verified_at_utc=now,
                    extractor_name="html_bs4",
                    extractor_version="1.0.0",
                )
            )
            session.flush()
            session.add(
                StagedArtifactRow(
                    artifact_id=aid,
                    resource_id=rid,
                    resource_version_id=vid,
                    source_url=url,
                    resource_type=ResourceType.PAGE.value,
                    category=DiscoveryCategory.FAQ.value,
                    content_hash=f"ch_p5_{suffix}",
                    semantic_hash=real_sh,
                    extractor_name="html_bs4",
                    extractor_version="1.0.0",
                    json_path=str(json_path),
                    markdown_path=str(md_path),
                    validation_status=ValidationStatus.VALID.value,
                    review_status=ReviewStatus.PENDING_REVIEW.value,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            session.commit()

        stage_repo = StagingRepository(engine)
        stage_repo.record_review_decision(
            artifact_id=aid,
            reviewer="qa-owner",
            decision="approved",
            new_review_status=ReviewStatus.APPROVED,
            policy_mode="manual",
            notes="phase5 test approval",
        )
        return aid
    finally:
        engine.dispose()


def test_qdrant_outage_enqueues_outbox_recovers_and_dlq_cli_works(tmp_path: Path) -> None:
    db_path = tmp_path / "outage.db"
    qdrant_dir = tmp_path / "qdrant"
    scope_path, policy_path = _write_test_scope_and_policy(tmp_path)
    aid = _stage_and_approve_one_article(db_path=db_path, staging_dir=tmp_path / "staged")

    embedder = _DeterministicFakeEmbedder()
    flaky_qd = _FlakyQdrantStore(collection_name="pdn_p5_test", path=qdrant_dir)
    # Force outage for all 2 retries during publish
    flaky_qd.fail_upserts_remaining = 10

    pub_svc = PublicationService(
        sqlite_path=db_path,
        scope_path=scope_path,
        policy_path=policy_path,
        qdrant_store=flaky_qd,
        embedder=embedder,
        sleep_fn=lambda _s: None,
    )
    summary = pub_svc.publish_approved(dry_run=False)
    # SQLite published state succeeded cleanly; Qdrant sync deferred to durable outbox!
    assert summary.published == 1
    assert summary.tables_published == 1
    assert summary.chunks_upserted == 0
    assert summary.outbox_enqueued == 1

    engine = create_sqlite_engine(db_path)
    try:
        ops_repo = OpsRepository(engine)
        attempts = ops_repo.list_task_attempts(task_type="publish_qdrant_upsert")
        assert len(attempts) == 2
        assert attempts[0].status == "dead_lettered"

        outbox_rows = ops_repo.list_outbox_events(statuses=("failed",))
        assert len(outbox_rows) == 1
        obx_id = outbox_rows[0].event_id

        # Simulate persistent failure during reconcile -> transitions to dead_letter!
        rec_fail = reconcile_outbox(
            engine=engine,
            qdrant=flaky_qd,
            embedder=embedder,
            retry_policy=RetryPolicy(
                max_attempts=2, backoff_base_seconds=0.01, backoff_max_seconds=0.05
            ),
            now_utc=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert rec_fail.dead_lettered == 1

        # Verify critical dlq_non_empty alert fires
        alert_rep = evaluate_alerts(
            engine=engine,
            policy_path=policy_path,
            scope_path=scope_path,
            qdrant_ok=True,
            qdrant_published_points=0,
        )
        codes = {a.code for a in alert_rep.alerts}
        assert "dlq_non_empty" in codes
        assert "qdrant_sqlite_drift" in codes
        assert alert_rep.critical_count >= 1

        # Operator resolves via CLI with --requeue once Qdrant outage is fixed
        runner = CliRunner()
        list_out = runner.invoke(cli_app, ["ops", "dlq", "list", "--sqlite-path", str(db_path)])
        assert list_out.exit_code == 0
        assert obx_id in list_out.stdout

        res_out = runner.invoke(
            cli_app,
            [
                "ops",
                "dlq",
                "resolve",
                obx_id,
                "--reason",
                "qdrant_restored",
                "--requeue",
                "--sqlite-path",
                str(db_path),
            ],
        )
        assert res_out.exit_code == 0

        # Outage clears -> replay outbox cleanly with zero duplicate points
        flaky_qd.fail_upserts_remaining = 0
        rec_ok = reconcile_outbox(
            engine=engine,
            qdrant=flaky_qd,
            embedder=embedder,
            now_utc=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert rec_ok.dispatched == 1
        ok, pts = flaky_qd.health_check()
        assert ok is True
        assert pts == 2  # 1 prose chunk + 1 small informational table chunk

        # Re-running reconcile is idempotent (0 pending)
        rec_again = reconcile_outbox(engine=engine, qdrant=flaky_qd, embedder=embedder)
        assert rec_again.considered == 0
        assert aid in summary.published_ids
    finally:
        flaky_qd.close()
        engine.dispose()


def test_fts5_drift_repair_prometheus_metrics_alerts_and_hybrid_api(tmp_path: Path) -> None:
    db_path = tmp_path / "obs.db"
    qdrant_dir = tmp_path / "qdrant_obs"
    scope_path, policy_path = _write_test_scope_and_policy(tmp_path, retrieval_mode="hybrid")
    _stage_and_approve_one_article(db_path=db_path, staging_dir=tmp_path / "staged_obs")

    embedder = _DeterministicFakeEmbedder()
    qd = _FlakyQdrantStore(collection_name="pdn_obs_test", path=qdrant_dir)

    pub_svc = PublicationService(
        sqlite_path=db_path,
        scope_path=scope_path,
        policy_path=policy_path,
        qdrant_store=qd,
        embedder=embedder,
        sleep_fn=lambda _s: None,
    )
    pub_svc.publish_approved(dry_run=False)

    # Record recent completed sync runs so freshness alerts are clear
    engine = create_sqlite_engine(db_path)
    try:
        sf = create_session_factory(engine)
        now = datetime.now(UTC)
        with sf() as session:
            for prof in ("homepage", "site", "discovery_audit"):
                session.add(
                    SyncRunRow(
                        run_id=f"run_{prof}_ok",
                        profile=prof,
                        status="completed",
                        scope_file_hash="hash1",
                        started_at_utc=now - timedelta(minutes=10),
                        finished_at_utc=now - timedelta(minutes=5),
                        bounds_json="{}",
                        summary_counts_json="{}",
                    )
                )
            session.commit()

        # Deliberately corrupt FTS5 tables to test drift detection & auto-repair
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM structured_tables_fts_u61"))
            conn.execute(text("DELETE FROM knowledge_chunks_fts_u61"))

        rep_drift = evaluate_alerts(
            engine=engine,
            policy_path=policy_path,
            scope_path=scope_path,
            qdrant_ok=True,
            qdrant_published_points=2,
        )
        assert any(a.code == "fts5_sync_mismatch" for a in rep_drift.alerts)
        assert rep_drift.fts5_mismatches["structured_tables"] == 1
        assert rep_drift.fts5_mismatches["knowledge_chunks"] == 2

        # Auto-repair via reconcile_fts5
        fts_rec = reconcile_fts5(engine)
        assert fts_rec.after_mismatches == {
            "structured_tables": 0,
            "donation_sessions": 0,
            "knowledge_chunks": 0,
        }

        # Verify FastAPI /metrics, /v1/status alerts[], and hybrid /v1/knowledge/search
        settings = AppSettings(
            sqlite_path=db_path,
            scope_file=scope_path,
            ingestion_policy_file=policy_path,
            qdrant_path=qdrant_dir,
            qdrant_collection="pdn_obs_test",
        )
        api_app = create_app(settings=settings, embedder=embedder, qdrant_store=qd)
        with TestClient(api_app) as client:
            st_resp = client.get("/v1/status")
            assert st_resp.status_code == 200
            assert "X-Request-ID" in st_resp.headers
            st_data: dict[str, Any] = st_resp.json()
            assert st_data["retrieval_mode"] == "hybrid"
            assert st_data["dlq_count"] == 0
            assert st_data["alerts"] == []

            search_resp = client.get("/v1/knowledge/search", params={"q": "syarat kelayakan 45 kg"})
            assert search_resp.status_code == 200
            s_data = search_resp.json()
            assert s_data["total"] == 2
            assert (
                s_data["results"][0]["source_url"] == "https://pdn.gov.my/v2/index.php/kelayakan-1"
            )

            m_resp = client.get("/metrics")
            assert m_resp.status_code == 200
            assert "text/plain" in m_resp.headers["content-type"]
            body = m_resp.text
            assert "pdn_sync_published_documents 1.0" in body
            assert "pdn_sync_published_chunks 2.0" in body
            assert "pdn_sync_dlq_items 0.0" in body
            assert "pdn_sync_api_requests_total" in body

        # CLI check-alerts exits 0 when healthy
        runner = CliRunner()
        ok_cli = runner.invoke(
            cli_app,
            ["ops", "check-alerts", "--json", "--skip-qdrant", "--sqlite-path", str(db_path)],
        )
        assert ok_cli.exit_code == 0
    finally:
        qd.close()
        engine.dispose()
