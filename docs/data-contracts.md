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

All endpoints expose **only** published, non-superseded, non-retired records:
- `GET /v1/knowledge/search?q=<query>&lang=<ms|en|any>&category=<optional>&limit=<1..20>`
- `GET /v1/tables?q=<optional>&source_url=<optional>&limit=<1..20>`
- `GET /v1/events?date=<YYYY-MM-DD>&date_from=&date_to=&state=&q=&limit=<1..50>` *(Phase 4)*
- `GET /healthz` and `GET /v1/status`
