"""Unit tests for APScheduler single-worker setup and deterministic retrieval eval metrics."""

from __future__ import annotations

from pathlib import Path

from pdn_sync.config import AppSettings
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.indexing.embedder import DeterministicHashEmbedder
from pdn_sync.indexing.eval_runner import EvalQuestion, EvalSuite, evaluate_retrieval
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.persistence.db import create_sqlite_engine
from pdn_sync.scheduler.worker import build_scheduler
from pdn_sync.search.tables import StructuredTableSearchService
from tests.integration.test_phase3_publication_and_lifecycle import _setup_env, _stage_artifact


def test_build_scheduler_registers_all_profiles_with_safe_defaults(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)
    settings = AppSettings(
        sqlite_path=db_path,
        scope_file=scope_path,
        ingestion_policy_file=policy_path,
        data_dir=data_dir,
    )
    sched = build_scheduler(settings=settings, live=False)
    job_ids = {j.id for j in sched.get_jobs()}
    assert job_ids == {"pdn_homepage_check", "pdn_site_check", "pdn_discovery_audit"}
    for job in sched.get_jobs():
        assert job.max_instances == 1
        assert job.coalesce is True


def test_eval_runner_computes_hit_at_1_mrr_and_citation_accuracy(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)
    url = "https://pdn.gov.my/v2/images/SmartSlider3/WaktuOperasiApril2024.png"
    _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url=url,
        title="Waktu Operasi Pusat Darah Negara",
        body="Waktu operasi PDN Jalan Tun Razak Isnin hingga Jumaat 7.30 pagi hingga 8.00 malam.",
        resource_type=ResourceType.IMAGE_POSTER,
        category=DiscoveryCategory.POSTER_BANNER,
        source_effective_date="2024-04",
    )

    embedder = DeterministicHashEmbedder()
    with QdrantVectorStore(in_memory=True) as qdrant:
        pub = PublicationService(
            sqlite_path=db_path,
            scope_path=scope_path,
            policy_path=policy_path,
            qdrant_store=qdrant,
            embedder=embedder,
            settings=AppSettings(sqlite_path=db_path, data_dir=data_dir),
        )
        pub.publish_approved(dry_run=False)

        suite = EvalSuite(
            questions=[
                EvalQuestion(
                    id="q01",
                    lang="ms",
                    category="operating_hours",
                    query="Waktu operasi Pusat Darah Negara Jalan Tun Razak",
                    expected_urls=[url],
                    expected_facts=["7.30 pagi", "8.00 malam"],
                    search_target="knowledge",
                )
            ]
        )
        engine = create_sqlite_engine(db_path)
        try:
            report = evaluate_retrieval(
                suite=suite,
                embedder=embedder,
                qdrant_store=qdrant,
                table_search=StructuredTableSearchService(engine),
            )
        finally:
            engine.dispose()

        assert report.questions_total == 1
        assert report.hit_at_1 == 1.0
        assert report.hit_at_5 == 1.0
        assert report.mrr_at_5 == 1.0
        assert report.fact_recall_at_5 == 1.0
        assert report.citation_accuracy == 1.0
