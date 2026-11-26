"""End-to-end offline tests for Phase 2 extraction, Gate B review, override, and reextract."""

from __future__ import annotations

import io
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from PIL import Image

from pdn_sync.config import AppSettings
from pdn_sync.discovery.scope_manager import GateABlockedError
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    OcrLine,
    ReviewStatus,
)
from pdn_sync.extractors.pdf_docling import PdfConversionOutput
from pdn_sync.persistence.db import create_sqlite_engine
from pdn_sync.persistence.staging_repo import (
    GateBPublishBlockedError,
    StageQueueFilter,
    StagingRepository,
)
from pdn_sync.staging.fixtures import FixtureCaptureBlockedError, capture_safe_fixture
from pdn_sync.staging.gate_b import (
    ExtractionOptions,
    GateBService,
    run_extraction_pipeline,
)
from pdn_sync.staging.semantic_hash import SemanticTextBlock

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 50), color=(240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


class _StubPdfBackend:
    def convert(self, pdf_path: Path, *, do_ocr: bool) -> PdfConversionOutput:
        return PdfConversionOutput(
            title="Panduan Penderma",
            page_count=1,
            text_blocks=[
                SemanticTextBlock(order=0, heading_level=1, text="Panduan Penderma Darah PDN")
            ],
            tables=[
                ExtractedTable(
                    table_index=0,
                    headers=["Item", "Nilai"],
                    rows=[["Berat Minimum", "45 kg"]],
                    units=[None, "kg"],
                )
            ],
            used_ocr=do_ocr,
        )


class _StubOcrBackend:
    def recognize(self, image_path: Path) -> list[OcrLine]:
        return [OcrLine(text="STATUS BEKALAN DARAH O+", confidence=0.92, box=[0, 0, 50, 20])]


def _write_phase2_scope(path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    path.write_text(
        f"""
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
    - id: exclude-jevents
      pattern: "/v2/index.php/component/jevents/**"
      reason: deferred_to_structured_schedule_phase
approvals:
  pages:
    urls:
      - url: "https://pdn.gov.my/v2/index.php/maklumat-korporat/profil"
        reason: approved_corporate
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
      - url: "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/31-direktori-pengurusan-tertinggi/2-dr-test"
        reason: approved_pdpa_staff
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
    patterns: []
  image_posters:
    urls: []
    patterns:
      - id: blood-stock-png
        pattern: "/v2/images/stokdarah/*.png"
        resource_type: image_poster
        reason: rotating_blood_stock
        max_matches: 24
        check_interval: 3h
        supersession: latest_wins
        approved_by: tester
        approved_at: "{now}"
  documents:
    urls:
      - url: "https://pdn.gov.my/v2/images/dokumen/panduan.pdf"
        reason: approved_pdf
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
    patterns: []
""",
        encoding="utf-8",
    )


def test_phase2_end_to_end_staging_gate_b_carry_forward_override_reextract_and_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "crawl_scope.yaml"
    policy_file = tmp_path / "ingestion_policy.yaml"
    _write_phase2_scope(scope_file)
    shutil.copyfile(REPO_ROOT / "config/ingestion_policy.yaml", policy_file)

    db_path = tmp_path / "pdn_sync.db"
    settings = AppSettings(
        contact_email="ops@pdn-sync.org",
        operator_id="review-owner",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
        scope_file=scope_file,
        ingestion_policy_file=policy_file,
        sqlite_path=db_path,
        data_dir=tmp_path,
    )

    current_html = [
        b"<html><head><title>Profil PDN</title></head><body><main>"
        b"<h1>Profil Pusat Darah Negara</h1><p>Visi dan misi PDN.</p>"
        b"<form><input type='hidden' name='csrf' value='tok-1' /></form>"
        b"</main></body></html>"
    ]
    current_etag = ['"p1"']
    png_bytes = _make_png()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://pdn.gov.my/v2/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /administrator/\n")
        if url == "https://pdn.gov.my/v2/index.php/maklumat-korporat/profil":
            return httpx.Response(
                200,
                content=current_html[0],
                headers={"content-type": "text/html", "etag": current_etag[0]},
            )
        if url == "https://pdn.gov.my/v2/images/dokumen/panduan.pdf":
            return httpx.Response(
                200, content=b"%PDF-1.4 content", headers={"content-type": "application/pdf"}
            )
        if url == "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png":
            return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected request: {url}")

    transport = httpx.MockTransport(handler)
    urls = [
        "https://pdn.gov.my/v2/index.php/maklumat-korporat/profil",
        "https://pdn.gov.my/v2/images/dokumen/panduan.pdf",
        "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png",
    ]

    # 1. Run multimodal extraction pipeline -> stages JSON+MD + immutable blobs
    run1 = run_extraction_pipeline(
        options=ExtractionOptions(
            live=True,
            urls=urls,
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            data_dir=tmp_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=transport,
        sleep_fn=lambda _s: None,
        pdf_backend=_StubPdfBackend(),
        ocr_backend=_StubOcrBackend(),
    )
    assert run1.fetched == 3
    assert run1.staged_pending == 2  # corporate page + pdf
    assert run1.staged_quarantined == 1  # blood_stock_graphic
    assert (tmp_path / "extracted/manifest.json").is_file()
    assert (tmp_path / "extracted/phase2_benchmark.json").is_file()

    svc = GateBService(
        sqlite_path=db_path,
        policy_path=policy_file,
        scope_path=scope_file,
        data_dir=tmp_path,
    )

    # 2. Priority queue puts critical/quarantined blood-stock graphic FIRST
    queue = svc.list_queue()
    assert len(queue) == 3
    assert queue[0].category == "blood_stock_graphic"
    assert queue[0].review_status == ReviewStatus.QUARANTINED.value

    # 3. Before Gate B approval, publisher layer blocks ALL 3 artifacts
    ready_before, blocked_before = svc.check_publish_eligibility()
    assert ready_before == []
    assert len(blocked_before) == 3

    # 4. Batch approve --dry-run and apply: approves page + PDF, excludes quarantined blood-stock
    dry = svc.batch_approve(flt=StageQueueFilter(limit=0), reviewer="review-owner", dry_run=True)
    assert len(dry.approved_ids) == 2
    assert len(dry.excluded_critical_or_invalid) == 1

    applied = svc.batch_approve(
        flt=StageQueueFilter(limit=0), reviewer="review-owner", dry_run=False
    )
    assert len(applied.approved_ids) == 2
    assert applied.batch_id is not None

    ready_after, blocked_after = svc.check_publish_eligibility()
    assert len(ready_after) == 2
    assert len(blocked_after) == 1

    # 5. Human transcription override on quarantined blood-stock creates a new unapproved version
    stock_art_id = queue[0].artifact_id
    override_md = tmp_path / "verified_stock.md"
    override_md.write_text(
        "# Status Bekalan Darah September 2026\n\nO+: Paras Selamat\n", encoding="utf-8"
    )
    override_row = svc.override(
        artifact_id=stock_art_id,
        override_file=override_md,
        reviewer="review-owner",
        notes="Human verified blood stock chart",
    )
    assert override_row.extractor_name == "human_transcription"
    assert override_row.review_status == ReviewStatus.PENDING_REVIEW.value
    assert override_row.content_hash == queue[0].content_hash
    assert override_row.semantic_hash != queue[0].semantic_hash

    # Still blocked until explicitly approved!
    engine = create_sqlite_engine(db_path)
    try:
        repo = StagingRepository(engine)
        with pytest.raises(GateBPublishBlockedError):
            repo.assert_artifact_approved_for_publish(override_row.artifact_id)
    finally:
        engine.dispose()

    svc.approve_single(
        artifact_id=override_row.artifact_id, reviewer="review-owner", notes="Verified"
    )
    ready_with_override, _ = svc.check_publish_eligibility()
    assert override_row.artifact_id in ready_with_override

    # 6. Carry-forward on unchanged semantic_hash (CSRF token changes, meaning identical!)
    current_html[0] = (
        b"<html><head><title>Profil PDN</title></head><body><main>"
        b"<h1>Profil Pusat Darah Negara</h1><p>Visi dan misi PDN.</p>"
        b"<form><input type='hidden' name='csrf' value='tok-999-rotated' /></form>"
        b"</main></body></html>"
    )
    current_etag[0] = '"p2"'
    run2 = run_extraction_pipeline(
        options=ExtractionOptions(
            live=True,
            urls=["https://pdn.gov.my/v2/index.php/maklumat-korporat/profil"],
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            data_dir=tmp_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=transport,
        sleep_fn=lambda _s: None,
    )
    assert run2.carried_forward == 1
    assert run2.staged_pending == 0

    # 7. Meaningful text change invalidates carry-forward -> pending_review + non-empty stage diff
    current_html[0] = (
        b"<html><head><title>Profil PDN</title></head><body><main>"
        b"<h1>Profil Pusat Darah Negara</h1><p>Visi dan misi PDN yang dikemaskini 2026.</p>"
        b"</main></body></html>"
    )
    current_etag[0] = '"p3"'
    run3 = run_extraction_pipeline(
        options=ExtractionOptions(
            live=True,
            urls=["https://pdn.gov.my/v2/index.php/maklumat-korporat/profil"],
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            data_dir=tmp_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=transport,
        sleep_fn=lambda _s: None,
    )
    assert run3.staged_pending == 1
    assert run3.carried_forward == 0
    changed_art_id = run3.artifact_ids[0]
    diff_text = svc.diff_artifact(changed_art_id)
    assert "dikemaskini 2026" in diff_text

    # 8. Tampering with staged JSON on disk is caught by publisher Gate B guard
    pdf_art_id = next(aid for aid in applied.approved_ids if aid != queue[0].artifact_id)
    pdf_row, _, _ = svc.show_artifact(pdf_art_id)
    tampered_json = json.loads(Path(pdf_row.json_path).read_text(encoding="utf-8"))
    tampered_json["title"] = "TAMPERED TITLE"
    Path(pdf_row.json_path).write_text(json.dumps(tampered_json), encoding="utf-8")

    engine2 = create_sqlite_engine(db_path)
    try:
        with pytest.raises(GateBPublishBlockedError, match="mismatch"):
            StagingRepository(engine2).assert_artifact_approved_for_publish(pdf_art_id)
    finally:
        engine2.dispose()

    # 9. Offline reextract --dry-run and apply with carry-forward
    re_dry = svc.reextract(extractor="html", reviewer="review-owner", dry_run=True)
    assert re_dry.total_considered == 1

    # 10. Safe fixture capture refuses unapproved URL and approved staff_directory URL (PDPA)
    with pytest.raises(GateABlockedError):
        capture_safe_fixture(
            "https://pdn.gov.my/v2/index.php/not-approved",
            settings=settings,
            scope_file=scope_file,
            policy_file=policy_file,
            fixtures_dir=tmp_path / "fixtures",
            transport=transport,
            resolve_dns=False,
        )
    with pytest.raises(FixtureCaptureBlockedError, match="PDPA"):
        capture_safe_fixture(
            "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/31-direktori-pengurusan-tertinggi/2-dr-test",
            settings=settings,
            scope_file=scope_file,
            policy_file=policy_file,
            fixtures_dir=tmp_path / "fixtures",
            transport=transport,
            resolve_dns=False,
        )

    # Captures approved public HTML page and strips hidden CSRF input
    cap_path = capture_safe_fixture(
        "https://pdn.gov.my/v2/index.php/maklumat-korporat/profil",
        settings=settings,
        scope_file=scope_file,
        policy_file=policy_file,
        fixtures_dir=tmp_path / "fixtures",
        transport=transport,
        resolve_dns=False,
    )
    assert cap_path.is_file()
    assert "csrf" not in cap_path.read_text(encoding="utf-8").lower()
