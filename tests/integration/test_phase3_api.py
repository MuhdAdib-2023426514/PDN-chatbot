"""Integration tests for the Phase 3 read-only FastAPI chatbot API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pdn_sync.api.app import create_app
from pdn_sync.config import AppSettings
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.extractors.contracts import ExtractedTable, ReviewStatus
from pdn_sync.indexing.embedder import DeterministicHashEmbedder
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from tests.integration.test_phase3_publication_and_lifecycle import _setup_env, _stage_artifact


def test_read_only_api_search_tables_status_and_healthz(tmp_path: Path) -> None:
    db_path, scope_path, policy_path, data_dir = _setup_env(tmp_path)

    # Stage 1 Malay hotline poster + table, 1 English deferral poster, 1 staff profile, 1 unapproved
    _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/images/hotline/Talian_Utama_PDN_Ogos_2025.png",
        title="Talian Utama Pusat Darah Negara",
        body="Talian Utama Pusat Darah Negara 03-2613 2688. Waktu operasi Isnin hingga Jumaat.",
        resource_type=ResourceType.IMAGE_POSTER,
        category=DiscoveryCategory.CONTACT_STATIC_CENTERS,
        source_effective_date="2025-08",
        tables=[
            ExtractedTable(
                table_index=0,
                caption="Senarai Talian Utama PDN",
                headers=["Unit", "Nombor Telefon"],
                rows=[
                    ["Talian Utama", "03-2613 2688"],
                    ["Kaunter Temujanji Aferesis", "03-2613 2682 / 03-2613 2683"],
                ],
            )
        ],
    )
    _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/images/SmartSlider3/Needle_Related_Procedures.png",
        title="Needle Related Procedures Deferral",
        body="Blood donors who had tattoo, body piercing, or acupuncture must defer for 6 months.",
        resource_type=ResourceType.IMAGE_POSTER,
        category=DiscoveryCategory.POSTER_BANNER,
    )
    _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/pengarah",
        title="Dr. Pengarah PDN",
        body="Pengarah Pusat Darah Negara, Kementerian Kesihatan Malaysia. E-mel pdn@moh.gov.my.",
        resource_type=ResourceType.PAGE,
        category=DiscoveryCategory.STAFF_DIRECTORY,
    )
    _stage_artifact(
        db_path=db_path,
        data_dir=data_dir,
        url="https://pdn.gov.my/v2/index.php/secret-unapproved",
        title="Unapproved Draft",
        body="SECRET_UNAPPROVED_TOKEN_XYZ",
        review_status=ReviewStatus.PENDING_REVIEW,
        approve_decision=False,
    )

    embedder = DeterministicHashEmbedder()
    settings = AppSettings(
        sqlite_path=db_path,
        scope_file=scope_path,
        ingestion_policy_file=policy_path,
        data_dir=data_dir,
    )
    with QdrantVectorStore(in_memory=True) as qdrant:
        pub = PublicationService(
            sqlite_path=db_path,
            scope_path=scope_path,
            policy_path=policy_path,
            qdrant_store=qdrant,
            embedder=embedder,
            settings=settings,
        )
        summary = pub.publish_approved(dry_run=False)
        assert summary.published == 3
        assert summary.tables_published == 1

        app = create_app(settings=settings, embedder=embedder, qdrant_store=qdrant)
        with TestClient(app) as client:
            # 1. /healthz
            h = client.get("/healthz")
            assert h.status_code == 200
            assert h.json()["status"] == "ok"
            assert h.json()["qdrant_published_points"] >= 3

            # 2. /v1/status
            st = client.get("/v1/status")
            assert st.status_code == 200
            st_data = st.json()
            assert st_data["published_documents"] == 3
            assert st_data["published_tables"] == 1
            assert st_data["pending_gate_b"] == 1

            # 3. /v1/knowledge/search (English + lang filter)
            res_en = client.get(
                "/v1/knowledge/search",
                params={"q": "tattoo piercing deferral 6 months", "lang": "en", "limit": 5},
            )
            assert res_en.status_code == 200
            hits_en = res_en.json()["results"]
            assert len(hits_en) >= 1
            assert "Needle_Related_Procedures" in hits_en[0]["source_url"]
            assert hits_en[0]["asset_url"] == hits_en[0]["source_url"]
            assert hits_en[0]["last_verified_at"]
            assert hits_en[0]["pending_change"] is False

            # 4. /v1/knowledge/search (Malay + staff_directory category filter)
            res_staff = client.get(
                "/v1/knowledge/search",
                params={"q": "Pengarah Pusat Darah Negara", "category": "staff_directory"},
            )
            assert res_staff.status_code == 200
            assert len(res_staff.json()["results"]) == 1
            assert "direktori-pegawai" in res_staff.json()["results"][0]["source_url"]

            # 5. Unapproved content must never appear
            res_secret = client.get(
                "/v1/knowledge/search", params={"q": "SECRET_UNAPPROVED_TOKEN_XYZ"}
            )
            assert all(
                "SECRET_UNAPPROVED_TOKEN_XYZ" not in h["text"] for h in res_secret.json()["results"]
            )

            # 6. /v1/tables (FTS5 unicode61 + typo-tolerant fallback + exact source_url)
            t_fts = client.get("/v1/tables", params={"q": "Temujanji Aferesis"})
            assert t_fts.status_code == 200
            assert t_fts.json()["total"] == 1
            assert t_fts.json()["results"][0]["source_effective_date"] == "2025-08"

            t_typo = client.get("/v1/tables", params={"q": "Talian Utma PDN"})
            assert t_typo.status_code == 200
            assert t_typo.json()["total"] == 1
            assert "03-2613 2688" in str(t_typo.json()["results"][0]["rows"])

            t_url = client.get(
                "/v1/tables",
                params={
                    "source_url": "https://pdn.gov.my/v2/images/hotline/Talian_Utama_PDN_Ogos_2025.png"
                },
            )
            assert t_url.status_code == 200
            assert t_url.json()["total"] == 1
