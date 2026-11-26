"""Phase 2b Docling PDF extractor (digital-first with scanned-PDF OCR fallback)."""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    ReviewStatus,
    ValidationStatus,
)
from pdn_sync.extractors.dates import extract_source_effective_date
from pdn_sync.staging.semantic_hash import SemanticTextBlock

PDF_EXTRACTOR_NAME = "pdf_docling"
PDF_EXTRACTOR_VERSION = "1.0.0"

# Minimum average non-whitespace characters per page before falling back to OCR
MIN_CHARS_PER_PAGE_DIGITAL = 40


@dataclass(frozen=True, slots=True)
class PdfConversionOutput:
    title: str | None
    page_count: int
    text_blocks: list[SemanticTextBlock] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    used_ocr: bool = False


class PdfBackend(Protocol):
    """Injectable PDF conversion backend (Docling at runtime; mockable in offline tests)."""

    def convert(self, pdf_path: Path, *, do_ocr: bool) -> PdfConversionOutput: ...


class DefaultDoclingBackend:
    """Lazy-loading Docling backend using PdfPipelineOptions(do_table_structure=True)."""

    def __init__(self) -> None:
        self._converters: dict[bool, Any] = {}

    def _get_converter(self, *, do_ocr: bool) -> Any:
        if do_ocr in self._converters:
            return self._converters[do_ocr]

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = do_ocr
        opts.do_table_structure = True
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
        )
        self._converters[do_ocr] = converter
        return converter

    def convert(self, pdf_path: Path, *, do_ocr: bool) -> PdfConversionOutput:
        converter = self._get_converter(do_ocr=do_ocr)
        conv_res = converter.convert(pdf_path)
        doc = conv_res.document

        pages_dict = getattr(doc, "pages", None) or {}
        page_count = max(1, len(pages_dict))

        # Parse Docling Markdown output into structured heading/text blocks
        md_text = str(doc.export_to_markdown() or "")
        blocks: list[SemanticTextBlock] = []
        title: str | None = None
        order = 0
        for raw_para in md_text.split("\n\n"):
            para = raw_para.strip()
            if not para:
                continue
            # Skip raw markdown tables here since structured tables are extracted below
            if para.startswith("|") and "\n|" in para:
                continue
            heading_level: int | None = None
            text_val = para
            if para.startswith("#"):
                stripped = para.lstrip("#")
                lvl = len(para) - len(stripped)
                if 1 <= lvl <= 6 and stripped.startswith(" "):
                    heading_level = lvl
                    text_val = stripped.strip()
                    if title is None:
                        title = text_val
            blocks.append(
                SemanticTextBlock(order=order, heading_level=heading_level, text=text_val)
            )
            order += 1

        tables: list[ExtractedTable] = []
        raw_tables = getattr(doc, "tables", None) or []
        for idx, tbl in enumerate(raw_tables):
            try:
                df = tbl.export_to_dataframe(doc=doc)
            except TypeError:
                df = tbl.export_to_dataframe()
            headers = [str(col).strip() for col in df.columns.tolist()]
            rows = [[str(cell).strip() for cell in row] for row in df.values.tolist()]
            if not headers and not rows:
                continue
            col_count = max([len(headers), *(len(r) for r in rows)], default=0)
            tables.append(
                ExtractedTable(
                    table_index=idx,
                    caption=None,
                    headers=headers,
                    rows=rows,
                    units=[None] * col_count,
                    source_selector=f"docling_table:{idx}",
                )
            )

        return PdfConversionOutput(
            title=title,
            page_count=page_count,
            text_blocks=blocks,
            tables=tables,
            used_ocr=do_ocr,
        )


def _non_ws_char_count(out: PdfConversionOutput) -> int:
    total = sum(len("".join(b.text.split())) for b in out.text_blocks)
    for tbl in out.tables:
        total += sum(len("".join(c.split())) for row in tbl.rows for c in row)
    return total


def extract_pdf(
    *,
    raw_bytes: bytes,
    source_url: str,
    resource_id: str,
    resource_version_id: str,
    content_hash: str,
    category: DiscoveryCategory,
    captured_at_utc: datetime,
    policy_rule_id: str | None = None,
    content_type: str | None = "application/pdf",
    backend: PdfBackend | None = None,
) -> ExtractionResult:
    """Extract page-aware text and tables from approved PDF bytes using Docling."""
    now = datetime.now(UTC)
    ext = PurePosixPath(urlsplit(source_url).path).suffix.lower()
    eff_date, eff_prov = extract_source_effective_date(source_url=source_url)

    # Explicit handling of legacy binary .doc files (never silently drop or misparse)
    if ext == ".doc" or (content_type and "msword" in content_type.lower()):
        draft = ExtractionResult(
            artifact_id=f"art_{uuid.uuid4().hex[:16]}",
            resource_id=resource_id,
            resource_version_id=resource_version_id,
            source_url=source_url,
            resource_type=ResourceType.DOCUMENT,
            category=category,
            policy_rule_id=policy_rule_id,
            content_hash=content_hash,
            semantic_hash="",
            extractor_name=PDF_EXTRACTOR_NAME,
            extractor_version=PDF_EXTRACTOR_VERSION,
            captured_at_utc=captured_at_utc,
            extracted_at_utc=now,
            source_effective_date=eff_date,
            source_effective_date_provenance=eff_prov,
            content_type=content_type,
            title=PurePosixPath(urlsplit(source_url).path).name,
            warnings=["legacy_binary_doc_requires_libreoffice_conversion_or_exclusion"],
            validation_status=ValidationStatus.NEEDS_REVIEW,
            review_status=ReviewStatus.QUARANTINED,
            provenance={"legacy_doc_quarantined": True},
        )
        return draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})

    converter = backend or DefaultDoclingBackend()
    warnings: list[str] = []

    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="pdn_doc_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw_bytes)
        tmp_path = Path(tmp_name)

        # Pass 1: fast digital extraction without OCR
        output = converter.convert(tmp_path, do_ocr=False)
        chars_per_page = _non_ws_char_count(output) / max(1, output.page_count)

        # Pass 2: OCR fallback ONLY when embedded text is absent or insufficient
        if chars_per_page < MIN_CHARS_PER_PAGE_DIGITAL:
            output = converter.convert(tmp_path, do_ocr=True)
            warnings.append("scanned_pdf_ocr_fallback_used")
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    if not output.text_blocks and not output.tables:
        warnings.append("empty_pdf_extraction")

    if eff_date is None and output.text_blocks:
        eff_date, eff_prov = extract_source_effective_date(
            source_url=source_url,
            text_candidates=[b.text for b in output.text_blocks[:5]],
            text_provenance="pdf_text",
        )

    title = output.title or PurePosixPath(urlsplit(source_url).path).stem.replace("_", " ")
    draft = ExtractionResult(
        artifact_id=f"art_{uuid.uuid4().hex[:16]}",
        resource_id=resource_id,
        resource_version_id=resource_version_id,
        source_url=source_url,
        resource_type=ResourceType.DOCUMENT,
        category=category,
        policy_rule_id=policy_rule_id,
        content_hash=content_hash,
        semantic_hash="",
        extractor_name=PDF_EXTRACTOR_NAME,
        extractor_version=PDF_EXTRACTOR_VERSION,
        captured_at_utc=captured_at_utc,
        extracted_at_utc=now,
        source_effective_date=eff_date,
        source_effective_date_provenance=eff_prov,
        content_type=content_type or "application/pdf",
        title=title,
        language="ms",
        text_blocks=output.text_blocks,
        tables=output.tables,
        warnings=warnings,
        provenance={
            "page_count": output.page_count,
            "used_ocr_fallback": output.used_ocr,
        },
    )
    return draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
