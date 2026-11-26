"""Gate B content validation and critical-fact tagging (AGENTS.md Section 7)."""

from __future__ import annotations

import re

from pdn_sync.domain.policy import IngestionPolicyConfig
from pdn_sync.extractors.contracts import (
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)

_BLOOD_GROUP_RE = re.compile(
    r"\b(?:O|A|B|AB)\s*[+\-]"
    r"|\b(?:kumpulan darah|bekalan darah|stok darah|paras selamat|paras rendah)\b",
    flags=re.IGNORECASE,
)
_OPERATING_HOURS_RE = re.compile(
    r"\b\d{1,2}[.:]\d{2}\s*(?:pagi|petang|malam|t/hari|am|pm)\b|\bwaktu\s+operasi\b",
    flags=re.IGNORECASE,
)
_DEFERRAL_RE = re.compile(
    r"\b\d+\s*(?:bulan|minggu|hari|tahun|months?|weeks?|days?|years?)\b.*\b(?:derma|tatu|tattoo|tindik|vaksin|pembedahan|defer)\b"
    r"|\b(?:tatu|tattoo|tindik|akupunktur|bekam|vCJD|mpox)\b",
    flags=re.IGNORECASE,
)

_QUARANTINE_WARNINGS: frozenset[str] = frozenset(
    {
        "empty_html_extraction",
        "empty_pdf_extraction",
        "no_text_detected",
        "unreadable_or_corrupt_image_bytes",
        "legacy_binary_doc_requires_libreoffice_conversion_or_exclusion",
    }
)


def validate_and_tag(
    result: ExtractionResult,
    policy: IngestionPolicyConfig,
) -> ExtractionResult:
    """Apply schema/completeness validation, critical-fact flags, and quarantine rules.

    Keeps validation_status strictly separate from review_status.
    """
    flags: list[str] = list(result.critical_fact_flags)
    warnings: list[str] = list(result.warnings)

    def _add_flag(flag: str) -> None:
        if flag not in flags:
            flags.append(flag)

    # Human transcription overrides bypass OCR-specific quarantine but still require Gate B approval
    if result.extractor_name == "human_transcription":
        has_content = bool(result.text_blocks or result.tables)
        val_status = ValidationStatus.VALID if has_content else ValidationStatus.INVALID
        rev_status = ReviewStatus.PENDING_REVIEW if has_content else ReviewStatus.QUARANTINED
        return result.model_copy(
            update={
                "validation_status": val_status,
                "review_status": rev_status,
                "critical_fact_flags": flags,
                "warnings": warnings,
            }
        )

    must_quarantine = False
    validation_status = ValidationStatus.VALID
    review_status = ReviewStatus.PENDING_REVIEW

    # 1. Critical categories (default: blood_stock_graphic -> always quarantined)
    if result.category.value in set(policy.critical_facts.categories):
        _add_flag(f"critical_category:{result.category.value}")
        validation_status = ValidationStatus.NEEDS_REVIEW
        must_quarantine = True

    # 2. Operational-rule keywords in URL, title, or OCR lines
    haystack_parts = [result.source_url, result.title or ""]
    haystack_parts.extend(line.text for line in result.ocr_lines)
    haystack_lower = " ".join(haystack_parts).lower()
    for kw in policy.critical_facts.keywords:
        kw_l = kw.strip().lower()
        if kw_l and kw_l in haystack_lower:
            _add_flag(f"critical_keyword:{kw_l}")

    # 3. Deterministic detectors inside OCR output
    if result.ocr_lines:
        ocr_joined = "\n".join(line.text for line in result.ocr_lines)
        if _BLOOD_GROUP_RE.search(ocr_joined):
            _add_flag("ocr_detector:blood_group_inventory")
        if _OPERATING_HOURS_RE.search(ocr_joined):
            _add_flag("ocr_detector:operating_hours")
        if _DEFERRAL_RE.search(ocr_joined):
            _add_flag("ocr_detector:donor_deferral")

        # 4. OCR confidence thresholds
        overall_conf = result.confidence if result.confidence is not None else 0.0
        if overall_conf < policy.ocr.confidence_threshold_overall:
            _add_flag("low_ocr_confidence:overall")
            validation_status = ValidationStatus.NEEDS_REVIEW
            must_quarantine = True

        for line in result.ocr_lines:
            is_critical_line = bool(
                _BLOOD_GROUP_RE.search(line.text)
                or _OPERATING_HOURS_RE.search(line.text)
                or _DEFERRAL_RE.search(line.text)
            )
            if is_critical_line and line.confidence < policy.ocr.confidence_threshold_critical:
                _add_flag("low_ocr_confidence:critical_field")
                validation_status = ValidationStatus.NEEDS_REVIEW
                must_quarantine = True
                break

    # 5. Completeness & error warnings
    if "unreadable_or_corrupt_image_bytes" in warnings:
        validation_status = ValidationStatus.INVALID
        must_quarantine = True
    elif any(w in _QUARANTINE_WARNINGS for w in warnings):
        validation_status = ValidationStatus.NEEDS_REVIEW
        must_quarantine = True
    elif not result.text_blocks and not result.tables and not result.ocr_lines:
        if "empty_extraction" not in warnings:
            warnings.append("empty_extraction")
        validation_status = ValidationStatus.NEEDS_REVIEW
        must_quarantine = True

    if must_quarantine:
        review_status = ReviewStatus.QUARANTINED

    return result.model_copy(
        update={
            "critical_fact_flags": flags,
            "warnings": warnings,
            "validation_status": validation_status,
            "review_status": review_status,
        }
    )
