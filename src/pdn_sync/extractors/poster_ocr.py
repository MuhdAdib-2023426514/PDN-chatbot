"""Phase 2c PaddleOCR PP-OCRv6 (CPU) poster and infographic extractor."""

from __future__ import annotations

import io
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.extractors.contracts import (
    ExtractionResult,
    OcrLine,
)
from pdn_sync.extractors.dates import extract_source_effective_date
from pdn_sync.staging.semantic_hash import SemanticTextBlock

OCR_EXTRACTOR_NAME = "poster_paddleocr_ppocrv6"
OCR_EXTRACTOR_VERSION = "1.0.0"


class OcrBackend(Protocol):
    """Injectable OCR backend (PaddleOCR 3.x PP-OCRv6 CPU at runtime; mockable in tests)."""

    def recognize(self, image_path: Path) -> list[OcrLine]: ...


class DefaultPaddleOcrBackend:
    """Lazy-loading singleton wrapper around PaddleOCR 3.x PP-OCRv6 CPU."""

    def __init__(self) -> None:
        self._engine: Any | None = None

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine

        # Avoid slow remote hoster checks on every invocation when models are cached locally
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR

        self._engine = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
        )
        return self._engine

    def recognize(self, image_path: Path) -> list[OcrLine]:
        engine = self._get_engine()
        results = engine.predict(str(image_path))
        lines: list[OcrLine] = []

        for res in results:
            rec_texts = (
                list(res.get("rec_texts", [])) if isinstance(res, dict) else list(res["rec_texts"])
            )
            rec_scores = (
                list(res.get("rec_scores", []))
                if isinstance(res, dict)
                else list(res["rec_scores"])
            )
            raw_boxes = (
                list(res.get("rec_boxes", []))
                if isinstance(res, dict)
                else list(res.get("rec_boxes", []))
                if hasattr(res, "get")
                else []
            )

            for idx, (text, score) in enumerate(zip(rec_texts, rec_scores, strict=False)):
                cleaned = str(text).strip()
                if not cleaned:
                    continue
                conf = max(0.0, min(1.0, float(score)))
                box_coords: list[float] = []
                if idx < len(raw_boxes):
                    box_val = raw_boxes[idx]
                    if hasattr(box_val, "tolist"):
                        box_val = box_val.tolist()
                    if isinstance(box_val, (list, tuple)):
                        for item in box_val:
                            if isinstance(item, (list, tuple)):
                                box_coords.extend(float(v) for v in item)
                            else:
                                box_coords.append(float(item))
                lines.append(OcrLine(text=cleaned, confidence=conf, box=box_coords))

        return _sort_reading_order(lines)


def _sort_reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    """Sort OCR lines top-to-bottom (20px row buckets) then left-to-right."""

    def _key(line: OcrLine) -> tuple[int, float]:
        if len(line.box) >= 2:
            x0, y0 = line.box[0], line.box[1]
            return (int(y0 // 20), x0)
        return (0, 0.0)

    return sorted(lines, key=_key)


def _inspect_image_bytes(raw_bytes: bytes) -> tuple[int | None, int | None, str | None]:
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            width, height = img.size
            fmt = (img.format or "").lower()
            mime = f"image/{fmt}" if fmt else None
            return width, height, mime
    except (UnidentifiedImageError, OSError):
        return None, None, None


def extract_poster_ocr(
    *,
    raw_bytes: bytes,
    source_url: str,
    resource_id: str,
    resource_version_id: str,
    content_hash: str,
    category: DiscoveryCategory,
    captured_at_utc: datetime,
    policy_rule_id: str | None = None,
    content_type: str | None = None,
    backend: OcrBackend | None = None,
) -> tuple[ExtractionResult, dict[str, Any]]:
    """Run PaddleOCR PP-OCRv6 (CPU) on approved poster bytes and return result + media metadata."""
    now = datetime.now(UTC)
    width, height, detected_mime = _inspect_image_bytes(raw_bytes)
    mime_type = content_type or detected_mime

    ext = PurePosixPath(urlsplit(source_url).path).suffix.lower() or ".png"
    runner = backend or DefaultPaddleOcrBackend()
    warnings: list[str] = []
    ocr_lines: list[OcrLine] = []

    if width is None or height is None:
        warnings.append("unreadable_or_corrupt_image_bytes")
    else:
        fd, tmp_name = tempfile.mkstemp(suffix=ext, prefix="pdn_ocr_")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw_bytes)
            ocr_lines = runner.recognize(Path(tmp_name))
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    if not ocr_lines:
        warnings.append("no_text_detected")

    mean_conf = (
        round(sum(line.confidence for line in ocr_lines) / len(ocr_lines), 4) if ocr_lines else 0.0
    )

    text_blocks = [
        SemanticTextBlock(order=idx, heading_level=None, text=line.text)
        for idx, line in enumerate(ocr_lines)
    ]

    # Filename or OCR date is recorded strictly as source_effective_date, NEVER valid_until
    eff_date, eff_prov = extract_source_effective_date(
        source_url=source_url,
        text_candidates=[line.text for line in ocr_lines[:10]],
        text_provenance="ocr",
    )

    title = (
        PurePosixPath(urlsplit(source_url).path).stem.replace("_", " ").replace("-", " ").strip()
    )
    draft = ExtractionResult(
        artifact_id=f"art_{uuid.uuid4().hex[:16]}",
        resource_id=resource_id,
        resource_version_id=resource_version_id,
        source_url=source_url,
        resource_type=ResourceType.IMAGE_POSTER,
        category=category,
        policy_rule_id=policy_rule_id,
        content_hash=content_hash,
        semantic_hash="",
        extractor_name=OCR_EXTRACTOR_NAME,
        extractor_version=OCR_EXTRACTOR_VERSION,
        captured_at_utc=captured_at_utc,
        extracted_at_utc=now,
        source_effective_date=eff_date,
        source_effective_date_provenance=eff_prov,
        content_type=mime_type,
        title=title or None,
        language="ms",
        text_blocks=text_blocks,
        ocr_lines=ocr_lines,
        confidence=mean_conf,
        warnings=warnings,
        provenance={
            "image_width": width,
            "image_height": height,
            "ocr_line_count": len(ocr_lines),
            "device": "cpu",
        },
    )
    result = draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
    media_meta = {
        "mime_type": mime_type,
        "byte_size": len(raw_bytes),
        "width": width,
        "height": height,
        "usage_notes": f"Gate-A approved ({category.value}); source_url={source_url}",
    }
    return result, media_meta
