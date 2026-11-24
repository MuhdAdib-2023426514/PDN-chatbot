"""Golden vector and invariance tests for deterministic semantic_hash."""

from __future__ import annotations

import unicodedata

from pdn_sync.staging.semantic_hash import (
    SemanticHashInput,
    SemanticStructuredEvent,
    SemanticTable,
    SemanticTextBlock,
    compute_semantic_hash,
)

# Golden hashes locked in docs/data-contracts.md
GOLDEN_V1 = "7975dc499419fd23d794db421d8213c8d4348371afbbfff3c7b21dd680964018"
GOLDEN_V2 = "314533fe0d61dea76b7466b787568455b9dc062c980bfdc49c073f11431a0c32"
GOLDEN_V3 = "2da4b197b9c977b319853d5d3dd29c5d8837966a9ced8340484acf8987df7c7b"


def test_golden_vector_v1_html_article_and_normalization_invariance() -> None:
    v1_messy = SemanticHashInput(
        title="  Kriteria   Kelayakan Penderma Darah  ",
        text_blocks=[
            SemanticTextBlock(
                order=1,
                heading_level=None,
                text="Berumur 18 hingga 60 tahun.\r\nBerat badan sekurang-kurangnya\t\t45 kg.",
            ),
            SemanticTextBlock(order=0, heading_level=1, text="Syarat\tAsas"),
        ],
    )
    v1_clean = SemanticHashInput(
        title="Kriteria Kelayakan Penderma Darah",
        text_blocks=[
            SemanticTextBlock(order=0, heading_level=1, text="Syarat Asas"),
            SemanticTextBlock(
                order=1,
                heading_level=None,
                text="Berumur 18 hingga 60 tahun.\nBerat badan sekurang-kurangnya 45 kg.",
            ),
        ],
    )

    assert compute_semantic_hash(v1_messy) == GOLDEN_V1
    assert compute_semantic_hash(v1_clean) == GOLDEN_V1


def test_golden_vector_v2_poster_ocr_and_case_digit_sensitivity() -> None:
    v2 = SemanticHashInput(
        title="Waktu Operasi PDN",
        ocr_lines=[
            "ISNIN - JUMAAT:  7.30 PAGI - 8.00 MALAM",
            "SABTU & AHAD: 8.00 PAGI - 4.00 PETANG",
        ],
    )
    assert compute_semantic_hash(v2) == GOLDEN_V2

    # Digit change alters semantic_hash
    v2_changed_time = SemanticHashInput(
        title="Waktu Operasi PDN",
        ocr_lines=[
            "ISNIN - JUMAAT: 8.00 PAGI - 8.00 MALAM",
            "SABTU & AHAD: 8.00 PAGI - 4.00 PETANG",
        ],
    )
    assert compute_semantic_hash(v2_changed_time) != GOLDEN_V2

    # Case change alters semantic_hash (case preserved per Section 7)
    v2_changed_case = SemanticHashInput(
        title="Waktu Operasi PDN",
        ocr_lines=[
            "isnin - jumaat: 7.30 pagi - 8.00 malam",
            "SABTU & AHAD: 8.00 PAGI - 4.00 PETANG",
        ],
    )
    assert compute_semantic_hash(v2_changed_case) != GOLDEN_V2


def test_golden_vector_v3_table_and_event() -> None:
    v3 = SemanticHashInput(
        title="Jadual Kempen Derma Darah",
        tables=[
            SemanticTable(
                headers=["PENGANJUR", "LOKASI", "SASARAN"],
                rows=[["PDN", "Mid Valley Megamall", "150"]],
                units=[None, None, "penderma"],
            )
        ],
        structured_events=[
            SemanticStructuredEvent(
                organizer="PDN",
                location_name="Mid Valley Megamall",
                event_date="2026-11-25",
                time_start="10:00",
                time_end="16:00",
                session_label="SESI 1",
                target_donors=150,
                raw_cells={"TAMAT": "4.00 PETANG", "MULA": "10.00 PAGI"},
            )
        ],
    )
    assert compute_semantic_hash(v3) == GOLDEN_V3


def test_unicode_nfd_and_nfc_produce_identical_hash() -> None:
    nfc_title = unicodedata.normalize("NFC", "Café Penderma")
    nfd_title = unicodedata.normalize("NFD", "Café Penderma")
    assert nfc_title != nfd_title

    h_nfc = compute_semantic_hash(SemanticHashInput(title=nfc_title))
    h_nfd = compute_semantic_hash(SemanticHashInput(title=nfd_title))
    assert h_nfc == h_nfd


def test_extra_metadata_fields_are_ignored_in_semantic_hash() -> None:
    base = SemanticHashInput.model_validate(
        {
            "title": "FAQ PDN",
            "extracted_at_utc": "2026-11-24T00:00:00Z",
            "run_id": "run-1",
            "extractor_version": "1.0.0",
        }
    )
    rerun = SemanticHashInput.model_validate(
        {
            "title": "FAQ PDN",
            "extracted_at_utc": "2026-11-25T12:00:00Z",
            "run_id": "run-2",
            "extractor_version": "2.0.0",
        }
    )
    assert compute_semantic_hash(base) == compute_semantic_hash(rerun)
