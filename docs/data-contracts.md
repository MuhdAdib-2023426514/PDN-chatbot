# Data Contracts — PDN Multimodal Knowledge Sync

> Schema version `1` (Phase 0 baseline). Changing the `semantic_hash` definition requires an ADR and a migration that recomputes hashes for approved versions without altering approval status.

## 1. Deterministic `semantic_hash` Contract

`semantic_hash` is the lowercase SHA-256 hex digest of a canonical UTF-8 JSON object (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`) computed strictly over **meaningful content fields**:

- **Included fields:**
  - `title` (`str | null`)
  - `text_blocks` (list sorted by `order`: `{heading_level, order, text}`)
  - `tables` (list of `{headers, rows, units}`)
  - `ocr_lines` (list of normalized strings in reading order)
  - `structured_events` (list of `{address, city, event_date, location_name, organizer, raw_cells, session_label, state, target_donors, time_end, time_start}` with `raw_cells` keys sorted)
- **Text normalization applied before hashing:**
  1. Unicode **NFC** normalization
  2. Line endings normalized (`\r\n` and `\r` → `\n`)
  3. Runs of horizontal whitespace (`[ \t]+`) collapsed to a single ASCII space (` `)
  4. Leading and trailing whitespace stripped per block/cell
  5. **Letter case and punctuation preserved** (e.g., `10.00 PAGI` vs `11.00 PAGI` or acronym changes always produce a different digest)
- **Excluded fields (never hashed):**
  - `extracted_at_utc`, `captured_at_utc`, `run_id`, `extractor_name`, `extractor_version`, HTTP headers, `last_verified_at`, hit counters, CSRF tokens, and non-semantic page chrome.

### Golden Test Vectors (Locked by `tests/unit/test_semantic_hash.py`)

#### Vector V1 — HTML Article (Whitespace, CRLF & Block-Order Normalization)

Canonical JSON:
```json
{"ocr_lines":[],"structured_events":[],"tables":[],"text_blocks":[{"heading_level":1,"order":0,"text":"Syarat Asas"},{"heading_level":null,"order":1,"text":"Berumur 18 hingga 60 tahun.\nBerat badan sekurang-kurangnya 45 kg."}],"title":"Kriteria Kelayakan Penderma Darah"}
```

Expected `semantic_hash`:
```text
7975dc499419fd23d794db421d8213c8d4348371afbbfff3c7b21dd680964018
```

#### Vector V2 — Poster OCR (Operating Hours, Case & Digit Sensitive)

Canonical JSON:
```json
{"ocr_lines":["ISNIN - JUMAAT: 7.30 PAGI - 8.00 MALAM","SABTU & AHAD: 8.00 PAGI - 4.00 PETANG"],"structured_events":[],"tables":[],"text_blocks":[],"title":"Waktu Operasi PDN"}
```

Expected `semantic_hash`:
```text
314533fe0d61dea76b7466b787568455b9dc062c980bfdc49c073f11431a0c32
```

#### Vector V3 — Structured Table + Donation Schedule Event (Raw Cells & Units Preserved)

Canonical JSON:
```json
{"ocr_lines":[],"structured_events":[{"address":null,"city":null,"event_date":"2026-11-25","location_name":"Mid Valley Megamall","organizer":"PDN","raw_cells":{"MULA":"10.00 PAGI","TAMAT":"4.00 PETANG"},"session_label":"SESI 1","state":null,"target_donors":150,"time_end":"16:00","time_start":"10:00"}],"tables":[{"headers":["PENGANJUR","LOKASI","SASARAN"],"rows":[["PDN","Mid Valley Megamall","150"]],"units":[null,null,"penderma"]}],"text_blocks":[],"title":"Jadual Kempen Derma Darah"}
```

Expected `semantic_hash`:
```text
2da4b197b9c977b319853d5d3dd29c5d8837966a9ced8340484acf8987df7c7b
```

## 2. Phase 1 Discovery Inventory Schema (`data/discovery/url_inventory.json`)

Top-level envelope:
- `schema_version`, `run_id`, `scope_file_hash`, `started_at_utc`, `finished_at_utc`, `bounds`, `summary_counts`, `items[]`

Each item in `items[]` (and row in `url_inventory.csv`):
- `schema_version`, `run_id`, `canonical_key`, `url`, `original_url`, `redirect_chain`, `http_status`, `link_status` (`ok` | `broken` | `unfetched_asset` | `skipped_excluded`), `duplicate_of`, `parent_url`, `title_or_alt`, `discovered_at_utc`, `depth`, `resource_type` (`page` | `image_poster` | `document` | `external_ref`), `mime_hint`, `content_length_bytes`, `category`, `discovery_method`, `disposition` (`pending` | `approved` | `excluded`), `recommended_action`, `reason`, `policy_rule_id`, `monitoring_interval`.

## 3. Phase 2 Staged Extraction Schema (`ExtractionResult`)

One immutable UTF-8 JSON artifact + Markdown (`.md`) companion per approved resource version under `data/extracted/{articles,posters,documents,tables,donation_schedules}/`:
- Provenance & identity: `resource_id`, `resource_version_id`, `source_url`, `content_hash`, `semantic_hash`, `extractor_name`, `extractor_version`, `captured_at_utc`, `extracted_at_utc`, `source_effective_date` (nullable, with provenance).
- Content: `content_type`, `title`, `language`, `text_blocks`, `tables`, `media_refs` (recording unapproved inline images with `scope_status: pending|excluded`), `ocr_lines` (with boxes & confidences when applicable), `structured_events`.
- Quality & review: `confidence`, `critical_fact_flags`, `warnings`, `validation_status` (`valid` | `invalid` | `needs_review`), `review_status` (`pending_review` | `approved` | `rejected` | `quarantined` | `published` | `superseded` | `retired`).

## 4. Phase 3–4 Read-Only Chatbot API Contract

All endpoints expose **only** published, non-superseded, non-retired records (`status == "published"`), bound to `127.0.0.1:8000` by default:

### `GET /v1/knowledge/search?q=<query>&lang=<ms|en|any>&category=<optional>&limit=<1..20>`
Dense multilingual vector retrieval (`BAAI/bge-m3`, 1024d cosine, Qdrant `pdn_knowledge_v1`):
- Response envelope: `{query, lang, category, total, results[]}`
- Each item in `results[]`:
  - `chunk_id`, `text`, `score`, `title`, `section`, `category`, `language`, `source_url`, `asset_url`, `content_type`, `last_verified_at`, `source_effective_date`, `valid_until`, `pending_change`.
- Language filter semantics: `lang=ms` matches `ms` and `ms+en`; `lang=en` matches `en` and `ms+en`; `lang=any` matches all.

### `GET /v1/tables?q=<optional>&source_url=<optional>&limit=<1..20>`
Authoritative SQLite + FTS5 (`unicode61 remove_diacritics 2` + `trigram` + bounded `RapidFuzz` fallback) structured table retrieval:
- Response envelope: `{query, source_url, total, results[]}`
- Each item in `results[]`:
  - `table_id`, `caption`, `headers[]`, `rows[][]`, `units[]`, `source_url`, `category`, `last_verified_at`, `source_effective_date`, `valid_until`, `pending_change`.

### `GET /v1/events?date=<YYYY-MM-DD>&date_from=&date_to=&state=&q=&limit=<1..50>`
Authoritative SQLite + FTS5 (`unicode61 remove_diacritics 2` + `trigram` + bounded `RapidFuzz` candidate discovery, never overriding authoritative date/state SQL filters) blood donation schedule retrieval (zero Qdrant schedule vectors by default):
- Response envelope: `{date, date_from, date_to, state, query, total, results[]}`
- Each item in `results[]`:
  - `event_id`, `session_id`, `organizer`, `location_name`, `address`, `city`, `state`, `event_date`, `time_start`, `time_end`, `session_label`, `target_donors`, `raw_cells`, `status` (`listed` | `amended` | `not_confirmed` | `removed_from_source` | `canceled_if_explicit`), `source_url`, `last_verified_at`, `pending_change`.

### `GET /healthz` and `GET /v1/status`
- `/healthz`: `{status, sqlite, qdrant, qdrant_mode, qdrant_published_points}`
- `/v1/status`: `{published_documents, published_chunks, published_tables, superseded_documents, retired_documents, pending_change_documents, approved_unpublished_artifacts, pending_gate_b, quarantined_gate_b, active_embedding_model, active_qdrant_collection, qdrant_mode, qdrant_published_points, last_completed_runs_by_profile}`

### Qdrant Point Payload Contract (`pdn_knowledge_v1`)
Deterministic UUIDv5 point ID derived from `pdn-sync:{resource_id}:{resource_version_id}:{chunk_index}` with payload fields:
`chunk_id`, `document_id`, `publication_id`, `resource_id`, `resource_version_id`, `artifact_id`, `source_url`, `asset_url`, `title`, `section`, `category`, `language`, `content_type`, `last_verified_at`, `source_effective_date`, `valid_from`, `valid_until`, `pending_change`, `status`, `text`.
