"""Integration tests for Gate B publish guard, idempotency, supersession, FTS5, and retirement."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from pdn_sync.config import AppSettings
from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryItem,
    LinkStatus,
    RecommendedAction,
    make_canonical_key,
)
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.indexing.embedder import DeterministicHashEmbedder
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.publication_repo import StaleVersionPublishError
from pdn_sync.persistence.repository import ControlPlaneRepository
from pdn_sync.persistence.staging_repo import GateBPublishBlockedError, StagingRepository
from pdn_sync.search.tables import StructuredTableSearchService
from pdn_sync.staging.exporter import export_staged_artifact
from pdn_sync.staging.semantic_hash import SemanticTextBlock

_SCOPE_YAML = """\
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
    - id: exclude_jevents
      pattern: /v2/index.php/component/jevents/**
      reason: deferred_to_structured_schedule_phase
approvals:
  pages:
    urls:
      - url: https://pdn.gov.my/v2/index.php/hubungi-kami/talian-utama
        reason: approved_test
        approved_by: owner
        approved_at: "2026-11-26T00:00:00Z"
    patterns: []
  image_posters:
    urls: []
    patterns:
      - id: pat_stokdarah
        pattern: /v2/images/stokdarah/*.png
        resource_type: image_poster
        reason: rotating_blood_stock
        max_matches: 20
        check_interval: 4h
        supersession: latest_wins
        approved_by: owner
        approved_at: "2026-11-26T00:00:00Z"
  documents:
    urls: []
    patterns: []
"""

_POLICY_YAML = """\
schema_version: 1
approval_mode: manual
retirement_consecutive_runs: 2
domain_timezone: Asia/Kuala_Lumpur
indexing:
  small_table_max_rows_for_qdrant: 30
  embed_donation_schedule_rows: false
  include_staff_directory_in_qdrant: true
  chunk_target_chars: 800
  chunk_overlap_chars: 100
  embedding_model: deterministic-hash-384
"""


def _setup_env(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    db_path = tmp_path / "test.db"
    scope_path = tmp_path / "crawl_scope.yaml"
    policy_path = tmp_path / "ingestion_policy.yaml"
    data_dir = tmp_path / "data"
    scope_path.write_text(_SCOPE_YAML, encoding="utf-8")
    policy_path.write_text(_POLICY_YAML, encoding="utf-8")
    upgrade_database(db_path)
    return db_path, scope_path, policy_path, data_dir


def _stage_artifact(
    *,
    db_path: Path,
    data_dir: Path,
    url: str,
    title: str,
    body: str,
    resource_type: ResourceType = ResourceType.PAGE,
    category: DiscoveryCategory = DiscoveryCategory.CONTACT_STATIC_CENTERS,
    policy_rule_id: str | None = None,
    tables: list[ExtractedTable] | None = None,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    validation_status: ValidationStatus = ValidationStatus.VALID,
    source_effective_date: str | None = None,
    approve_decision: bool = True,
) -> tuple[str, str]:
    engine = create_sqlite_engine(db_path)
    repo = StagingRepository(engine)
    try:
        res_row = repo.ensure_crawl_resource(
            canonical_url=url,
            resource_type=resource_type.value,
            category=category.value,
        )
        ver_seq = repo.next_version_seq(res_row.resource_id)
        ver_id = f"ver_{res_row.resource_id}_{ver_seq:03d}_{uuid.uuid4().hex[:6]}"
        now = datetime.now(UTC)
        draft = ExtractionResult(
            artifact_id=f"art_{uuid.uuid4().hex[:16]}",
            resource_id=res_row.resource_id,
            resource_version_id=ver_id,
            source_url=url,
            resource_type=resource_type,
            category=category,
            policy_rule_id=policy_rule_id,
            content_hash=uuid.uuid4().hex * 2,
            semantic_hash="",
            extractor_name="human_transcription"
            if resource_type == ResourceType.IMAGE_POSTER
            else "html_bs4_lxml",
            extractor_version="1.0.0",
            captured_at_utc=now,
            extracted_at_utc=now,
            source_effective_date=source_effective_date,
            title=title,
            language="ms",
            text_blocks=[SemanticTextBlock(order=0, text=body)],
            tables=tables or [],
            validation_status=validation_status,
            review_status=ReviewStatus.PENDING_REVIEW,
        )
        draft = draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
        json_path, md_path = export_staged_artifact(
            result=draft, version_seq=ver_seq, data_dir=data_dir
        )
        repo.save_staged_version(
            result=draft,
            version_seq=ver_seq,
            fetch_id=None,
            json_path=json_path,
            markdown_path=md_path,
        )
        if approve_decision and review_status == ReviewStatus.APPROVED:
            repo.record_review_decision(
                artifact_id=draft.artifact_id,
                reviewer="owner",
                decision="approved",
                new_review_status=ReviewStatus.APPROVED,
                policy_mode="manual",
                notes="test approval",
            )
        elif review_status != ReviewStatus.PENDING_REVIEW:
            repo.record_review_decision(
                artifact_id=draft.artifact_id,
                reviewer="owner",
                decision=review_status.value,
                new_review_status=review_status,
                policy_mode="manual",
                notes="test status",
            )
        return draft.artifact_id, res_row.resource_id
    finally:
        engine.dispose()


def test_gate_b_guard_blocks_unapproved_rejected_quarantined_and_tampered(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)
    pending_id, _ = _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/index.php/hubungi-kami/talian-utama",
        title="Pending",
        body="Talian utama 03-2613 2688",
        review_status=ReviewStatus.PENDING_REVIEW,
        approve_decision=False,
    )
    rejected_id, _ = _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/index.php/hubungi-kami/rejected",
        title="Rejected",
        body="Rejected content",
        review_status=ReviewStatus.REJECTED,
        approve_decision=False,
    )
    quarantined_id, _ = _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/images/stokdarah/q.png",
        title="Quarantined",
        body="Low confidence",
        resource_type=ResourceType.IMAGE_POSTER,
        category=DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
        review_status=ReviewStatus.QUARANTINED,
        approve_decision=False,
    )

    embedder = DeterministicHashEmbedder()
    with QdrantVectorStore(in_memory=True) as qdrant:
        svc = PublicationService(
            sqlite_path=db_path,
            scope_path=scope_path,
            policy_path=policy_path,
            qdrant_store=qdrant,
            embedder=embedder,
            settings=AppSettings(sqlite_path=db_path, data_dir=data_dir),
        )
        for bad_id in (pending_id, rejected_id, quarantined_id):
            with pytest.raises(GateBPublishBlockedError):
                svc.publish_approved(artifact_ids=[bad_id], raise_on_blocked=True)

        # Approved artifact whose JSON on disk is tampered after approval must also fail
        approved_id, _ = _stage_artifact(
            db_path=db_path,
            data_dir=data_dir,
            url="https://pdn.gov.my/v2/index.php/hubungi-kami/tampered",
            title="Original",
            body="Original phone 03-2613 2688",
            review_status=ReviewStatus.APPROVED,
        )
        engine = create_sqlite_engine(db_path)
        art_row = StagingRepository(engine).get_artifact(approved_id)
        engine.dispose()
        assert art_row is not None
        payload = json.loads(Path(art_row.json_path).read_text(encoding="utf-8"))
        payload["text_blocks"][0]["text"] = "TAMPERED phone 999"
        Path(art_row.json_path).write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(GateBPublishBlockedError, match="semantic_hash mismatch"):
            svc.publish_approved(artifact_ids=[approved_id], raise_on_blocked=True)

        # Confirm zero points leaked into Qdrant
        _, count = qdrant.health_check()
        assert count == 0


def test_idempotency_monotonic_version_guard_and_pending_change_flag(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)
    url = "https://pdn.gov.my/v2/index.php/hubungi-kami/talian-utama"
    tbl = ExtractedTable(
        table_index=0,
        caption="Senarai Talian Utama",
        headers=["Unit", "Telefon"],
        rows=[["Talian Utama", "03-2613 2688"]],
    )
    art_v1, _ = _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url=url,
        title="Talian Utama PDN v1",
        body="Talian utama Pusat Darah Negara 03-2613 2688",
        tables=[tbl],
    )

    embedder = DeterministicHashEmbedder()
    with QdrantVectorStore(in_memory=True) as qdrant:
        svc = PublicationService(
            sqlite_path=db_path,
            scope_path=scope_path,
            policy_path=policy_path,
            qdrant_store=qdrant,
            embedder=embedder,
            settings=AppSettings(sqlite_path=db_path, data_dir=data_dir),
        )
        s1 = svc.publish_approved(dry_run=False)
        assert s1.published == 1
        assert s1.tables_published == 1

        # Double publish is strictly idempotent
        s2 = svc.publish_approved(dry_run=False)
        assert s2.published == 0
        assert s2.skipped_idempotent == 1

        # Stage a newer v2 still awaiting Gate B -> sets pending_change=True on live v1!
        art_v2_pending, _ = _stage_artifact(
            db_path=db_path,
            data_dir=data_dir,
            url=url,
            title="Talian Utama PDN v2",
            body="Nombor baharu 03-2613 9999",
            review_status=ReviewStatus.PENDING_REVIEW,
            approve_decision=False,
        )
        svc.publish_approved(dry_run=False)

        hits = qdrant.search(embedder.embed_query("Talian Utama"), limit=5)
        assert len(hits) >= 1
        assert hits[0].payload["pending_change"] is True
        assert hits[0].payload["artifact_id"] == art_v1

        # Now approve v2 -> v2 publishes, supersedes v1, clears pending_change, and stale v1 blocked
        engine = create_sqlite_engine(db_path)
        StagingRepository(engine).record_review_decision(
            artifact_id=art_v2_pending,
            reviewer="owner",
            decision="approved",
            new_review_status=ReviewStatus.APPROVED,
            policy_mode="manual",
        )
        engine.dispose()

        s3 = svc.publish_approved(dry_run=False)
        assert s3.published == 1
        assert s3.superseded_publications == 1

        hits_after = qdrant.search(embedder.embed_query("Nombor baharu 03-2613 9999"), limit=5)
        assert all(h.payload["artifact_id"] == art_v2_pending for h in hits_after)
        assert hits_after[0].payload["pending_change"] is False

        # Re-approve art_v1 to simulate stale out-of-order worker trying to republish v1
        engine = create_sqlite_engine(db_path)
        StagingRepository(engine).record_review_decision(
            artifact_id=art_v1,
            reviewer="owner",
            decision="approved",
            new_review_status=ReviewStatus.APPROVED,
            policy_mode="manual",
        )
        engine.dispose()

        with pytest.raises(StaleVersionPublishError):
            svc.publish_approved(artifact_ids=[art_v1], raise_on_stale=True)


def test_latest_wins_supersession_retirement_and_explain_query_plan(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)
    url_sep = "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png"
    url_oct = "https://pdn.gov.my/v2/images/stokdarah/2stockoktober2026.png"

    art_sep, _ = _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url=url_sep,
        title="Stok Darah September 2026",
        body="Stok darah September 2026 kumpulan O A B AB paras selamat.",
        resource_type=ResourceType.IMAGE_POSTER,
        category=DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
        policy_rule_id="pat_stokdarah",
        tables=[
            ExtractedTable(
                table_index=0,
                caption="Stok Darah September",
                headers=["Kumpulan", "Status"],
                rows=[["O", "Paras Selamat"]],
            )
        ],
        source_effective_date="2026-09",
    )

    embedder = DeterministicHashEmbedder()
    with QdrantVectorStore(in_memory=True) as qdrant:
        svc = PublicationService(
            sqlite_path=db_path,
            scope_path=scope_path,
            policy_path=policy_path,
            qdrant_store=qdrant,
            embedder=embedder,
            settings=AppSettings(sqlite_path=db_path, data_dir=data_dir),
        )
        svc.publish_approved(artifact_ids=[art_sep])

        # Publish October graphic matching same latest_wins pattern -> supersedes September!
        art_oct, _ = _stage_artifact(
            db_path=db_path,
            data_dir=data_dir,
            url=url_oct,
            title="Stok Darah Oktober 2026",
            body="Stok darah Oktober 2026 kumpulan O paras rendah.",
            resource_type=ResourceType.IMAGE_POSTER,
            category=DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            policy_rule_id="pat_stokdarah",
            tables=[
                ExtractedTable(
                    table_index=0,
                    caption="Stok Darah Oktober",
                    headers=["Kumpulan", "Status"],
                    rows=[["O", "Paras Rendah"]],
                )
            ],
            source_effective_date="2026-10",
        )
        s_oct = svc.publish_approved(artifact_ids=[art_oct])
        assert s_oct.superseded_publications == 1

        hits = qdrant.search(embedder.embed_query("Stok darah kumpulan O"), limit=5)
        assert hits
        assert all(h.payload["source_url"] == url_oct for h in hits)

        # FTS5 tables only contain October, not superseded September
        engine = create_sqlite_engine(db_path)
        tsearch = StructuredTableSearchService(engine)
        t_hits = tsearch.search_tables(q="Stok Darah", limit=5)
        assert [t.source_url for t in t_hits] == [url_oct]

        # Verify EXPLAIN QUERY PLAN uses B-tree indexes
        with engine.connect() as conn:
            plan_tables = conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT table_id FROM structured_tables "
                    "WHERE status = 'published' AND category = 'blood_stock_graphic'"
                )
            ).fetchall()
            plan_docs = conn.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT doc_id FROM knowledge_documents "
                    "WHERE status = 'published' AND category = 'blood_stock_graphic'"
                )
            ).fetchall()
            assert any("ix_structured_tables_status_category" in str(r) for r in plan_tables)
            assert any("ix_knowledge_docs_status_category" in str(r) for r in plan_docs)

        # Retirement reconciliation: failed discovery run must NEVER advance retirement!
        ctrl = ControlPlaneRepository(engine)
        now = datetime.now(UTC)
        ctrl.create_sync_run(
            run_id="disc_failed_01",
            profile="discovery_audit",
            scope_file_hash="0" * 64,
            started_at_utc=now,
            bounds={},
        )
        ctrl.save_discovery_results(
            run_id="disc_failed_01",
            status="failed",
            finished_at_utc=now,
            summary_counts={},
            items=[],
            content_hashes={},
            k2_item_ids={},
            error_summary="timeout",
        )
        r_fail = svc.reconcile_retirement(run_id="disc_failed_01")
        assert r_fail.skipped_incomplete_run is True

        # Clean discovery run #1 missing url_oct -> no_longer_linked (NOT retired yet, N=2)
        for run_idx in (1, 2):
            rid = f"disc_clean_0{run_idx}"
            ctrl.create_sync_run(
                run_id=rid,
                profile="discovery_audit",
                scope_file_hash="0" * 64,
                started_at_utc=now,
                bounds={},
            )
            other_url = "https://pdn.gov.my/v2/"
            other_item = InventoryItem(
                run_id=rid,
                canonical_key=make_canonical_key(other_url),
                url=other_url,
                original_url=other_url,
                http_status=200,
                link_status=LinkStatus.OK,
                discovered_at_utc=now,
                depth=0,
                resource_type=ResourceType.PAGE,
                category=DiscoveryCategory.HOMEPAGE,
                discovery_method="seed",
                disposition=Disposition.APPROVED,
                recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
                reason="homepage",
                monitoring_interval="4h",
            )
            ctrl.save_discovery_results(
                run_id=rid,
                status="completed",
                finished_at_utc=now,
                summary_counts={"total": 1},
                items=[other_item],
                content_hashes={},
                k2_item_ids={},
            )
            ret = svc.reconcile_retirement(run_id=rid)
            if run_idx == 1:
                assert ret.no_longer_linked_count == 1
                assert ret.retired_publication_ids == []
                assert len(qdrant.search(embedder.embed_query("Stok darah"), limit=5)) >= 1
            else:
                assert len(ret.retired_publication_ids) == 1
                assert qdrant.search(embedder.embed_query("Stok darah"), limit=5) == []
                assert tsearch.search_tables(q="Stok Darah", limit=5) == []

        engine.dispose()
