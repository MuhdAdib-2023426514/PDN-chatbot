# Gate B — Extracted Content Review Guide

> **Mandatory Publication Gate:** Approving a URL in [config/crawl_scope.yaml](../config/crawl_scope.yaml) (Gate A) permits fetching and extraction into staging only. Nothing enters Qdrant or active SQLite chatbot tables until it passes validation **and** Gate B approval bound to its immutable `semantic_hash`.

## 1. Staging Layout

- **Immutable raw bytes:** `data/blobs/<sha256[:2]>/<sha256>` (content-addressed, deduplicated across URLs).
- **Staged JSON + Markdown:**
  - `data/extracted/articles/` (HTML pages & PDPA-minimized staff directory records)
  - `data/extracted/documents/` (Docling PDFs)
  - `data/extracted/posters/` (PaddleOCR PP-OCRv6 posters & infographics)
  - `data/extracted/tables/` (Standalone Markdown table views)
  - `data/extracted/manifest.json` (Maps `artifact_id` ↔ `blob_sha256` ↔ `semantic_hash` ↔ paths)
- **Review snapshots:** `data/validated/` (approved) and `data/rejected/` (rejected).

## 2. Inspect the Priority Review Queue

`pdn-sync stage list` orders items with **critical facts & quarantined artifacts first**, then new resources, then changed resources:

```bash
uv run pdn-sync stage list
uv run pdn-sync stage list --critical-only
uv run pdn-sync stage show <artifact_id>
uv run pdn-sync stage diff <artifact_id>
```

## 3. Approve, Batch-Approve, Reject, or Override

### Safe Batch Approval (Excludes Critical, Quarantined & Invalid Items)

```bash
# Always preview first
uv run pdn-sync stage approve --all-valid --dry-run
uv run pdn-sync stage approve --category faq --dry-run

# Apply batch approval (records one shared batch_id across all approved artifact hashes)
uv run pdn-sync stage approve --all-valid --notes "Batch reviewed non-critical pages"
```

### Individual Review & Rejection

```bash
uv run pdn-sync stage approve <artifact_id> --notes "Verified operating hours"
uv run pdn-sync stage reject <artifact_id> --notes "Pure photo without useful OCR text"
```

### Human Transcription Override (`STATUS BEKALAN DARAH` & Quarantined Infographics)

For quarantined blood-stock gauge graphics or unreadable charts, write verified Markdown or JSON to a file and create a new immutable `human_transcription` version linked to the same raw blob hash:

```bash
uv run pdn-sync stage override <artifact_id> --file verified_blood_stock.md --notes "Transcribed from September 2026 chart"
uv run pdn-sync stage show <new_artifact_id>
uv run pdn-sync stage approve <new_artifact_id> --notes "Verified transcription approved for publication"
```

## 4. Diff-Based Carry-Forward & Offline Extractor Upgrades

- Re-fetching an approved URL whose meaningful content (`semantic_hash`) is **unchanged** automatically carries approval forward (`carried_forward`) and updates `last_verified_at`.
- When upgrading an extractor, preview impact offline against stored blobs first (zero live network requests):

```bash
uv run pdn-sync reextract --extractor html --dry-run
uv run pdn-sync reextract --extractor html
```

## 5. Verify Publisher-Layer Enforcement

```bash
uv run pdn-sync publish --approved-only --dry-run
```
