"""Unit tests for Phase 2b Docling PDF, Phase 2c Poster OCR, and critical-fact validation."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import IngestionPolicyConfig
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    OcrLine,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.extractors.pdf_docling import PdfConversionOutput, extract_pdf
from pdn_sync.extractors.poster_ocr import extract_poster_ocr
from pdn_sync.staging.semantic_hash import SemanticTextBlock
from pdn_sync.staging.validator import validate_and_tag


def _make_png_bytes(width: int = 120, height: int = 60) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


class _FakePdfBackend:
    def __init__(self, digital_text: str, ocr_text: str) -> None:
        self.digital_text = digital_text
        self.ocr_text = ocr_text
        self.calls: list[bool] = []

    def convert(self, pdf_path: Path, *, do_ocr: bool) -> PdfConversionOutput:
        self.calls.append(do_ocr)
        text = self.ocr_text if do_ocr else self.digital_text
        blocks = [SemanticTextBlock(order=0, heading_level=1, text=text)] if text else []
        tables = [
            ExtractedTable(
                table_index=0,
                headers=["Kriteria", "Syarat"],
                rows=[["Umur", "18 - 60 tahun"]],
                units=[None, None],
            )
        ]
        return PdfConversionOutput(
            title="Garis Panduan Pendermaan",
            page_count=1,
            text_blocks=blocks,
            tables=tables if text else [],
            used_ocr=do_ocr,
        )


def test_pdf_extractor_digital_skips_ocr_and_scanned_triggers_fallback() -> None:
    digital_backend = _FakePdfBackend(
        digital_text="Garis Panduan Pendermaan Darah Pusat Darah Negara Malaysia yang lengkap.",
        ocr_text="",
    )
    res_digital = extract_pdf(
        raw_bytes=b"%PDF-1.4 fake",
        source_url="https://pdn.gov.my/v2/images/dokumen/2026/guide.pdf",
        resource_id="res_pdf1",
        resource_version_id="ver_1",
        content_hash="d" * 64,
        category=DiscoveryCategory.DOCUMENT_PDF,
        captured_at_utc=datetime.now(UTC),
        backend=digital_backend,
    )
    assert digital_backend.calls == [False]
    assert len(res_digital.tables) == 1
    assert "scanned_pdf_ocr_fallback_used" not in res_digital.warnings

    scanned_backend = _FakePdfBackend(
        digital_text="",
        ocr_text="Teks hasil OCR untuk PDF imbasan Pusat Darah Negara.",
    )
    res_scanned = extract_pdf(
        raw_bytes=b"%PDF-1.4 scanned",
        source_url="https://pdn.gov.my/v2/images/dokumen/2026/scanned.pdf",
        resource_id="res_pdf2",
        resource_version_id="ver_1",
        content_hash="e" * 64,
        category=DiscoveryCategory.DOCUMENT_PDF,
        captured_at_utc=datetime.now(UTC),
        backend=scanned_backend,
    )
    assert scanned_backend.calls == [False, True]
    assert "scanned_pdf_ocr_fallback_used" in res_scanned.warnings


def test_legacy_doc_is_explicitly_quarantined() -> None:
    res = extract_pdf(
        raw_bytes=b"\xd0\xcf\x11\xe0 legacy doc",
        source_url="https://pdn.gov.my/v2/images/dokumen/borang.doc",
        resource_id="res_doc",
        resource_version_id="ver_1",
        content_hash="f" * 64,
        category=DiscoveryCategory.DOCUMENT_OFFICE,
        captured_at_utc=datetime.now(UTC),
    )
    assert res.review_status == ReviewStatus.QUARANTINED
    assert res.validation_status == ValidationStatus.NEEDS_REVIEW
    assert any("legacy_binary_doc" in w for w in res.warnings)


class _FakeOcrBackend:
    def __init__(self, lines: list[OcrLine]) -> None:
        self.lines = lines

    def recognize(self, image_path: Path) -> list[OcrLine]:
        assert image_path.is_file()
        return self.lines


def test_poster_ocr_blood_stock_quarantine_and_source_effective_date() -> None:
    policy = IngestionPolicyConfig()
    backend = _FakeOcrBackend(
        [
            OcrLine(text="STATUS BEKALAN DARAH", confidence=0.96, box=[10, 10, 100, 30]),
            OcrLine(
                text="Kumpulan Darah O+: Paras Selamat", confidence=0.91, box=[10, 40, 100, 60]
            ),
        ]
    )
    raw_res, media_meta = extract_poster_ocr(
        raw_bytes=_make_png_bytes(),
        source_url="https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png",
        resource_id="res_stock",
        resource_version_id="ver_1",
        content_hash="1" * 64,
        category=DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
        captured_at_utc=datetime.now(UTC),
        backend=backend,
    )
    validated = validate_and_tag(raw_res, policy)

    assert media_meta["width"] == 120
    assert validated.source_effective_date == "2026-09"
    assert validated.source_effective_date_provenance == "filename"
    assert "critical_category:blood_stock_graphic" in validated.critical_fact_flags
    assert validated.review_status == ReviewStatus.QUARANTINED
    assert validated.validation_status == ValidationStatus.NEEDS_REVIEW


def test_poster_ocr_low_confidence_critical_field_quarantines() -> None:
    policy = IngestionPolicyConfig()
    backend = _FakeOcrBackend(
        [
            OcrLine(text="WAKTU OPERASI PDN", confidence=0.95, box=[10, 10, 100, 30]),
            OcrLine(text="7.30 PAGI - 8.00 MALAM", confidence=0.78, box=[10, 40, 100, 60]),
        ]
    )
    raw_res, _ = extract_poster_ocr(
        raw_bytes=_make_png_bytes(),
        source_url="https://pdn.gov.my/v2/images/SmartSlider3/2025/WaktuOperasiApril2024.png",
        resource_id="res_hours",
        resource_version_id="ver_1",
        content_hash="2" * 64,
        category=DiscoveryCategory.POSTER_BANNER,
        captured_at_utc=datetime.now(UTC),
        backend=backend,
    )
    validated = validate_and_tag(raw_res, policy)

    assert validated.source_effective_date == "2024-04"
    assert "ocr_detector:operating_hours" in validated.critical_fact_flags
    assert "low_ocr_confidence:critical_field" in validated.critical_fact_flags
    assert validated.review_status == ReviewStatus.QUARANTINED


def test_poster_ocr_empty_photo_is_quarantined() -> None:
    policy = IngestionPolicyConfig()
    raw_res, _ = extract_poster_ocr(
        raw_bytes=_make_png_bytes(),
        source_url="https://pdn.gov.my/v2/images/Kemudahan_2025/Surau.jpg",
        resource_id="res_photo",
        resource_version_id="ver_1",
        content_hash="3" * 64,
        category=DiscoveryCategory.FACILITY_PHOTO,
        captured_at_utc=datetime.now(UTC),
        backend=_FakeOcrBackend([]),
    )
    validated = validate_and_tag(raw_res, policy)
    assert "no_text_detected" in validated.warnings
    assert validated.review_status == ReviewStatus.QUARANTINED
