"""Unit tests for Phase 3 source-aware chunking and Malay/English language tagging."""

from __future__ import annotations

from datetime import UTC, datetime

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import IndexingPolicy, ResourceType
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    OcrLine,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.indexing.chunker import build_chunks_for_artifact, make_deterministic_point_id
from pdn_sync.indexing.lang import detect_chunk_language, language_filter_values
from pdn_sync.staging.semantic_hash import SemanticTextBlock


def _sample_result(
    *,
    category: DiscoveryCategory = DiscoveryCategory.FAQ,
    resource_type: ResourceType = ResourceType.PAGE,
    text_blocks: list[SemanticTextBlock] | None = None,
    tables: list[ExtractedTable] | None = None,
    ocr_lines: list[OcrLine] | None = None,
    title: str = "Kriteria Kelayakan Penderma Darah",
) -> ExtractionResult:
    now = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    res = ExtractionResult(
        artifact_id="art_unit_001",
        resource_id="res_unit_001",
        resource_version_id="ver_unit_001",
        source_url="https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2/kriteria",
        resource_type=resource_type,
        category=category,
        content_hash="a" * 64,
        semantic_hash="b" * 64,
        extractor_name="html_bs4_lxml",
        extractor_version="1.0.0",
        captured_at_utc=now,
        extracted_at_utc=now,
        title=title,
        language="ms",
        text_blocks=text_blocks or [],
        tables=tables or [],
        ocr_lines=ocr_lines or [],
        validation_status=ValidationStatus.VALID,
        review_status=ReviewStatus.APPROVED,
    )
    return res.model_copy(update={"semantic_hash": res.recompute_semantic_hash()})


def test_detect_chunk_language_malay_english_and_bilingual() -> None:
    ms_text = "Waktu operasi Pusat Darah Negara adalah Isnin hingga Jumaat untuk penderma darah."
    en_text = "Blood donor eligibility criteria and operating hours before donation at the centre."
    bi_text = (
        "Waktu Operasi Isnin - Jumaat 11.00 pagi - 7.00 malam. "
        "Blood Donation Suite Everyday 11.00 am - 7.00 pm at the shopping centre."
    )
    assert detect_chunk_language(ms_text) == "ms"
    assert detect_chunk_language(en_text) == "en"
    assert detect_chunk_language(bi_text) == "ms+en"
    assert language_filter_values("ms") == ["ms", "ms+en"]
    assert language_filter_values("en") == ["en", "ms+en"]
    assert language_filter_values("any") is None


def test_chunker_preserves_headings_and_splits_long_prose_deterministically() -> None:
    long_para = " ".join(["Penderma darah hendaklah sihat dan berumur 18 hingga 60 tahun."] * 25)
    result = _sample_result(
        text_blocks=[
            SemanticTextBlock(order=0, heading_level=2, text="Syarat Asas"),
            SemanticTextBlock(order=1, heading_level=None, text=long_para),
        ]
    )
    policy = IndexingPolicy(chunk_target_chars=400, chunk_overlap_chars=80)
    out1 = build_chunks_for_artifact(result, policy)
    out2 = build_chunks_for_artifact(result, policy)

    assert len(out1.chunks) >= 3
    assert out1.document_language == "ms"
    assert all(
        ch.section == "Kriteria Kelayakan Penderma Darah > Syarat Asas" for ch in out1.chunks
    )
    assert [c.point_id for c in out1.chunks] == [c.point_id for c in out2.chunks]
    assert out1.chunks[0].point_id == make_deterministic_point_id(
        resource_id="res_unit_001",
        resource_version_id="ver_unit_001",
        chunk_index=0,
    )


def test_chunker_embeds_small_tables_and_skips_large_or_jevents_tables() -> None:
    small_tbl = ExtractedTable(
        table_index=0,
        caption="Talian Utama PDN",
        headers=["Unit", "Telefon"],
        rows=[["Talian Utama", "03-2613 2688"], ["Aferesis", "03-2613 2682"]],
    )
    large_tbl = ExtractedTable(
        table_index=1,
        caption="Senarai Panjang",
        headers=["Nama", "Kod"],
        rows=[[f"Item {i}", f"K{i}"] for i in range(45)],
    )
    res = _sample_result(tables=[small_tbl, large_tbl])
    out = build_chunks_for_artifact(res, IndexingPolicy(small_table_max_rows_for_qdrant=30))
    assert len(out.chunks) == 1
    assert "03-2613 2688" in out.chunks[0].text
    assert "Item 40" not in out.chunks[0].text

    jevents_res = _sample_result(
        category=DiscoveryCategory.JEVENTS_DEFERRED,
        tables=[small_tbl],
    )
    jevents_out = build_chunks_for_artifact(jevents_res, IndexingPolicy())
    assert jevents_out.chunks == []


def test_chunker_respects_staff_directory_toggle_and_ocr_fallback() -> None:
    staff_res = _sample_result(
        category=DiscoveryCategory.STAFF_DIRECTORY,
        title="Dr. Ahmad",
        text_blocks=[SemanticTextBlock(order=0, text="Pengarah Bahagian Perubatan Transfusi")],
    )
    assert (
        len(
            build_chunks_for_artifact(
                staff_res, IndexingPolicy(include_staff_directory_in_qdrant=True)
            ).chunks
        )
        == 1
    )
    assert (
        build_chunks_for_artifact(
            staff_res, IndexingPolicy(include_staff_directory_in_qdrant=False)
        ).chunks
        == []
    )

    poster_res = _sample_result(
        category=DiscoveryCategory.POSTER_BANNER,
        resource_type=ResourceType.IMAGE_POSTER,
        title="Needle Related Procedures",
        ocr_lines=[
            OcrLine(
                text="Deferral period is 6 months after tattoo or piercing.",
                confidence=0.98,
            )
        ],
    )
    poster_out = build_chunks_for_artifact(poster_res, IndexingPolicy())
    assert len(poster_out.chunks) == 1
    assert poster_out.chunks[0].language == "en"
