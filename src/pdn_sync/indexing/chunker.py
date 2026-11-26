"""Source-aware prose and small-table chunking with deterministic UUIDv5 point IDs."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import IndexingPolicy, ResourceType
from pdn_sync.extractors.contracts import ExtractedTable, ExtractionResult
from pdn_sync.indexing.lang import detect_chunk_language
from pdn_sync.persistence.publication_repo import PublicationChunkSpec


@dataclass(frozen=True, slots=True)
class ChunkingResult:
    """Chunks and document-level language produced from an approved ExtractionResult."""

    document_language: str
    chunks: list[PublicationChunkSpec]


def make_deterministic_point_id(
    *,
    resource_id: str,
    resource_version_id: str,
    chunk_index: int,
) -> str:
    """Return a deterministic UUIDv5 string suitable as a Qdrant point ID."""
    seed = f"pdn-sync:{resource_id}:{resource_version_id}:{chunk_index}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def make_chunk_id(
    *,
    resource_id: str,
    resource_version_id: str,
    chunk_index: int,
) -> str:
    seed = f"{resource_id}:{resource_version_id}:{chunk_index}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"chk_{digest}"


def _format_table_markdown(table: ExtractedTable, *, heading_context: str | None) -> str:
    lines: list[str] = []
    if heading_context:
        lines.append(f"## {heading_context}")
    if table.caption:
        lines.append(f"**{table.caption}**")

    headers = [h.strip() for h in table.headers] if table.headers else []
    if not headers and table.rows:
        headers = [f"Col {i + 1}" for i in range(len(table.rows[0]))]

    if headers:
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in table.rows:
        cells = [str(c).replace("\n", " ").strip() for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines).strip()


def _split_long_text(text: str, *, target_chars: int, overlap_chars: int) -> list[str]:
    """Split an oversized text block cleanly near sentence/word boundaries with overlap."""
    cleaned = text.strip()
    if len(cleaned) <= target_chars:
        return [cleaned] if cleaned else []

    pieces: list[str] = []
    start = 0
    n = len(cleaned)
    while start < n:
        end = min(start + target_chars, n)
        if end < n:
            # Prefer paragraph, sentence, or space boundary in the last 25% of the window
            window_floor = start + int(target_chars * 0.65)
            split_pos = -1
            for sep in ("\n\n", "\n", ". ", "; ", " "):
                pos = cleaned.rfind(sep, window_floor, end)
                if pos != -1:
                    split_pos = pos + len(sep)
                    break
            if split_pos > start:
                end = split_pos
        segment = cleaned[start:end].strip()
        if segment:
            pieces.append(segment)
        if end >= n:
            break
        next_start = max(end - overlap_chars, start + 1)
        # Advance to next word boundary so we don't slice mid-word
        if next_start < n and cleaned[next_start - 1] not in " \n\t":
            space = cleaned.find(" ", next_start, end)
            if space != -1:
                next_start = space + 1
        start = next_start
    return pieces


def build_chunks_for_artifact(
    result: ExtractionResult,
    policy: IndexingPolicy,
) -> ChunkingResult:
    """Build deterministic Qdrant-ready chunks from an approved ExtractionResult."""
    # Exclude JEvents donation schedules from Qdrant by default (AGENTS.md Section 9)
    if (
        result.category == DiscoveryCategory.JEVENTS_DEFERRED
        and not policy.embed_donation_schedule_rows
    ):
        return ChunkingResult(document_language=result.language or "ms", chunks=[])

    if (
        result.category == DiscoveryCategory.STAFF_DIRECTORY
        and not policy.include_staff_directory_in_qdrant
    ):
        return ChunkingResult(document_language=result.language or "ms", chunks=[])

    title = (result.title or "").strip()
    target = policy.chunk_target_chars
    overlap = policy.chunk_overlap_chars

    raw_chunks: list[tuple[str | None, str]] = []

    # 1. Group prose text_blocks by heading hierarchy
    if result.text_blocks:
        ordered_blocks = sorted(result.text_blocks, key=lambda b: b.order)
        current_section: str | None = title or None
        buf_parts: list[str] = []
        buf_len = 0

        def _flush_buffer() -> None:
            nonlocal buf_parts, buf_len
            if not buf_parts:
                return
            combined = "\n\n".join(buf_parts).strip()
            buf_parts = []
            buf_len = 0
            if not combined:
                return
            prefix = (
                f"{current_section}\n\n"
                if current_section and not combined.startswith(current_section)
                else ""
            )
            if len(prefix) + len(combined) <= target:
                raw_chunks.append((current_section, f"{prefix}{combined}".strip()))
            else:
                for piece in _split_long_text(
                    combined, target_chars=max(target - len(prefix), 250), overlap_chars=overlap
                ):
                    raw_chunks.append((current_section, f"{prefix}{piece}".strip()))

        for blk in ordered_blocks:
            blk_text = blk.text.strip()
            if not blk_text:
                continue
            if blk.heading_level is not None and blk.heading_level <= 3:
                _flush_buffer()
                if title and blk_text.lower() != title.lower():
                    current_section = f"{title} > {blk_text}"
                else:
                    current_section = blk_text
                continue

            if buf_parts and buf_len + len(blk_text) + 2 > target:
                _flush_buffer()
            buf_parts.append(blk_text)
            buf_len += len(blk_text) + 2

        _flush_buffer()

    # 2. Fallback to OCR lines if an image_poster has ocr_lines but no text_blocks
    if not raw_chunks and result.ocr_lines:
        ocr_joined = "\n".join(line.text.strip() for line in result.ocr_lines if line.text.strip())
        if ocr_joined:
            prefix = f"{title}\n\n" if title else ""
            for piece in _split_long_text(
                ocr_joined, target_chars=max(target - len(prefix), 250), overlap_chars=overlap
            ):
                raw_chunks.append((title or None, f"{prefix}{piece}".strip()))

    # 3. Small non-schedule informational tables -> Markdown chunks for semantic recall
    if (
        policy.small_table_max_rows_for_qdrant > 0
        and result.tables
        and result.category != DiscoveryCategory.JEVENTS_DEFERRED
    ):
        for tbl in result.tables:
            if len(tbl.rows) == 0 or len(tbl.rows) > policy.small_table_max_rows_for_qdrant:
                continue
            sec = tbl.caption or (f"{title} (Jadual {tbl.table_index + 1})" if title else None)
            md_table = _format_table_markdown(tbl, heading_context=title or None)
            if not md_table:
                continue
            if len(md_table) <= target * 2:
                raw_chunks.append((sec, md_table))
            else:
                for piece in _split_long_text(md_table, target_chars=target, overlap_chars=overlap):
                    raw_chunks.append((sec, piece))

    # 4. If an approved artifact still produced no chunks (e.g., title-only page), emit title
    if not raw_chunks and title and result.resource_type != ResourceType.EXTERNAL_REF:
        raw_chunks.append((title, title))

    specs: list[PublicationChunkSpec] = []
    all_text_for_doc_lang: list[str] = []
    for idx, (sec, body) in enumerate(raw_chunks):
        chunk_lang = detect_chunk_language(body, default=result.language or "ms")
        all_text_for_doc_lang.append(body)
        specs.append(
            PublicationChunkSpec(
                chunk_id=make_chunk_id(
                    resource_id=result.resource_id,
                    resource_version_id=result.resource_version_id,
                    chunk_index=idx,
                ),
                point_id=make_deterministic_point_id(
                    resource_id=result.resource_id,
                    resource_version_id=result.resource_version_id,
                    chunk_index=idx,
                ),
                chunk_index=idx,
                section=sec,
                language=chunk_lang,
                text=body,
            )
        )

    doc_lang = (
        detect_chunk_language("\n".join(all_text_for_doc_lang), default=result.language or "ms")
        if all_text_for_doc_lang
        else (result.language or "ms")
    )
    return ChunkingResult(document_language=doc_lang, chunks=specs)
