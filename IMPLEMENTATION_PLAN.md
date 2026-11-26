# IMPLEMENTATION_PLAN.md — PDN Multimodal Knowledge Sync

> Tracks phased rollout progress, verified dependency versions, acceptance evidence, measured Phase 2 benchmarks, and open risks.

## Phase Progress Checklist

- [x] **Phase 0 — Project foundation and source access policy** *(completed and owner-acknowledged 2026-11-24)*
- [x] **Phase 1 — Discovery-only URL & asset inventory + Gate A approval (JEvents excluded)** *(live discovery `disc_20261124T055806Z_da8f19c7` and Gate A completed 2026-11-24)*
- [x] **Phase 2 — Approved multimodal extraction & Gate B staging (2a HTML, 2b PDFs/docs, 2c Posters/OCR)** *(code, 102/102 tests, pilot `ext_20261125T031828Z_7fc76696`, full 736-item resumable run `ext_20261126T001234Z_f1afbfcd` [721 staged, 14 HTTP 404s, 1 PNG transcribed], and Gate B human triage [531 approved, 214 rejected, 0 unresolved] completed 2026-11-26)*
- [x] **Phase 3 — Orchestration ADR, approved publication & chatbot read API** *(completed 2026-11-26: `docs/decisions/ADR-orchestration.md` approved; Alembic `0003_phase3_publication_and_fts5`; 40-question Malay/English `tests/eval/questions.yaml` approved; `BAAI/bge-m3` won benchmark [Hit@1 72.5%, Hit@5 85.0%, MRR@5 0.7875, Fact Recall@5 92.5%, Citation 100%] over `multilingual-e5-small`; all 531 approved artifacts published [4,730 Qdrant points, 1,021 SQLite+FTS5 tables]; live read-only FastAPI API & 112/112 tests passing)*
- [x] **Phase 4 — Structured blood donation schedules (JEvents → SQLite + FTS5)** *(completed & live-published 2026-11-26: Gate A bounded JEvents rules in `config/crawl_scope.yaml`; Alembic `0004_phase4_donation_schedules`; `jevents_html_bs4 v1.1.0` multi-session parser + `ScheduleRepository` + `GET /v1/events` + 6h `calendar` worker; live bounded sync `cal_20261126T081755Z_e74139f6` + Gate B approval + idempotent publish populated `47` `donation_events` / `376` `donation_sessions` [`2026-10-31`..`2026-12-06`] with `0` Qdrant schedule vectors added; `120/120` tests passing)*
- [x] **Phase 5 — Reliability, observability, and operations** *(completed 2026-11-26: Alembic `0005_phase5_reliability_and_ops` [`task_attempts`, `outbox_events`, `knowledge_chunks_fts_u61`]; bounded retry & durable outbox/DLQ replay; SQLite online backup/verify/restore with WAL recovery; `structlog==26.1.0` JSON logging + `prometheus-client==0.26.0` localhost `GET /metrics` + actionable `alerts[]` in `/v1/status` & `pdn-sync ops check-alerts`; Dense vs Hybrid RRF benchmark on `tests/eval/questions.yaml` proved Hybrid winner [Hit@5 `87.5% → 95.0%`, Fact Recall@5 `92.5% → 97.5%`, MRR@5 `0.7917 → 0.8071`, Citation `100%`], enabled `retrieval_mode: hybrid`; 14 Phase 2 HTTP-404 URLs excluded; `docs/architecture.md` & `docs/operations.md` authored; `130/130` tests, `mypy --strict` across `93` files, `ruff` clean)*

---

## Verified Runtime & Package Versions

Verified via `uv sync --all-extras` on macOS Apple Silicon (`arm64`) on `2026-11-24`–`2026-11-25` and locked in `uv.lock`:

| Component | Verified Version | Phase | Notes |
|---|---|---|---|
| Python | `3.12.14` | Phase 0 | Pinned via `.python-version` (`3.12`) and `requires-python = ">=3.12,<3.13"` |
| `uv` | `0.12.21` | Phase 0 | Package manager and lockfile (`uv.lock`) |
| `pydantic` | `2.14.4` | Phase 0 | Strict schema contracts (`extra="forbid"` on configs) |
| `pydantic-settings` | `2.16.3` | Phase 0 | `.env` / environment loading |
| `ruamel.yaml` | `0.19.1` | Phase 0/1 | Safe YAML reads + comment-preserving Gate A round-trip writes |
| `httpx` | `0.28.1` | Phase 0–2 | Polite conditional HTTP client (`ETag`/`IMS`, 50 MiB stream cap) |
| `typer` | `0.27.3` | Phase 0–2 | CLI entrypoint (`pdn-sync`) |
| `sqlalchemy` | `2.1.9` | Phase 1–2 | Single-writer SQLite control-plane ORM (`WAL`, `foreign_keys=ON`) |
| `alembic` | `1.20.1` | Phase 1–2 | SQLite batch migrations (`0001_phase1_control_plane`, `0002_phase2_staging`) |
| `beautifulsoup4` | `4.15.0` | Phase 1–2 | HTML link parsing + Phase 2a HTML/table/PDPA staff extractor |
| `lxml` | `6.1.3` | Phase 1–2 | Fast HTML parser backend |
| `pillow` | `13.0.0` | Phase 2 | Image dimension & MIME verification |
| `docling` (`[pdf]`) | `2.152.0` | Phase 2b | Digital-first PDF + TableFormer extraction + scanned-PDF OCR fallback |
| `paddlepaddle` (`[ocr]`) | `3.3.1` | Phase 2c | Apple Silicon `arm64` CPU runtime (co-installs cleanly with `torch==2.15.0`) |
| `paddleocr` (`[ocr]`) | `3.7.0` | Phase 2c | `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec` CPU (50-language unified model) |
| `psutil` | `7.2.2` | Phase 2 | Peak RSS memory instrumentation for Phase 3 ADR |
| `qdrant-client` | `1.20.0` | Phase 3 | Dual-mode Qdrant client (embedded local disk `data/qdrant/` default + remote HTTP `QDRANT_URL`, modern `query_points()`) |
| `qdrant/qdrant` (optional Docker) | `v1.20.2-unprivileged` | Phase 3 | Pinned non-root official image in optional `compose.yaml` (`--profile qdrant`) |
| `fastapi` / `uvicorn` | `0.165.0` / `0.54.0` | Phase 3 | Read-only versioned chatbot HTTP API (`127.0.0.1:8000` default) |
| `sentence-transformers` | `6.3.0` | Phase 3 | Local multilingual embeddings (`BAAI/bge-m3` pinned winner, fp16 on Apple Silicon MPS) |
| `apscheduler` | `3.11.3` | Phase 3 | Single-process scheduled worker (`BlockingScheduler`, `Asia/Kuala_Lumpur`, `max_instances=1`) |
| `rapidfuzz` | `3.14.6` | Phase 3 | Bounded typo-tolerant fallback ranking for Malay/English SQLite FTS5 table/event search |
| `prometheus-client` | `0.26.0` | Phase 5 | Isolated `CollectorRegistry` + localhost-only `GET /metrics` exposition |
| `structlog` | `26.1.0` | Phase 5 | Structured JSON logging with correlation `contextvars` and PII/secret redaction |
| `pytest` / `ruff` / `mypy` | `9.2.0` / `0.17.6` / `2.4.1` | Phase 0–5 | Unit/integration tests (`130/130`), lint/format, strict static type checking (`93` files) |

---

## Measured Phase 2 Benchmarks (Input for Phase 3 `ADR-orchestration.md`)

Captured from real live full-corpus extraction (`ext_20261126T001234Z_f1afbfcd` resuming after pilot `ext_20261125T031828Z_7fc76696`, stored in `data/extracted/phase2_benchmark.json`) on Apple Silicon (`arm64`) CPU with polite `2–5 s` jittered delay and `1` concurrent request/host:

| Track | Approved Targets | Staged (Distinct) | Mean Fetch Latency (incl. delay) | Mean Extract / OCR Time | Max Extract / OCR Time | Measured Batch Extract CPU |
|---|---:|---:|---:|---:|---:|---:|
| **2a — HTML & Tables** | `426` pages | **`420`** (`6` HTTP 404s) | `4.18 s` | **`0.045 s`** | `0.128 s` | **`~19 s`** total CPU |
| **2b — PDFs (Docling `2.152.0`)** | `68` PDFs | **`68`** (`100%`!) | `3.65 s` | **`13.85 s`** (`55.6 s` on large clinical handbooks) | `120.07 s` | **`~17.3 min`** total CPU |
| **2c — Posters (PP-OCRv6 CPU)** | `242` images | **`233`** (`8` HTTP 404s, `1` PNG `RecursionError`) | `3.69 s` | **`23.18 s`** | `222.04 s` (`5334×4000` infographic) | **`~80.0 min`** total CPU |
| **Full Corpus Totals** | **`736` items** | **`721` staged** (`626` batch-eligible, `83` critical-flagged, `34` quarantined, `613` unique `semantic_hash`) | — | **Resumed wall clock: `8,002.8 s` (~`2h 13m`)** | — | **Start RSS: `90.9 MB` → Peak RSS: `4,255.5 MB` (~`4.25 GB`)** |

**Key Phase 3 ADR Takeaway:**
- Incremental re-checks (conditional `ETag`/`Last-Modified` + `BlobStore` unchanged-blob short-circuit + `semantic_hash` carry-forward) only re-extract *changed* resources, which takes seconds to a couple of minutes per scheduled run.
- Cold full-corpus extraction across all `721` live resources completes in ~`2.5 hours` single-threaded with ~`4.25 GB` peak RSS (when Docling + RapidOCR + PaddleOCR PP-OCRv6 are all loaded in one process). This real measurement will anchor the single scheduled worker vs. Airflow + RabbitMQ + Celery comparison in `docs/decisions/ADR-orchestration.md`.

---

## Phase Acceptance Evidence

- **Phase 0 (2026-11-24):** Prototype archived under `archive/prototype-2026-11-24/`; polite live check of `/v2/robots.txt` recorded in `docs/source-policy.md`; owner acknowledged (`PDN_SOURCE_POLICY_ACKNOWLEDGED=true`); commit `91b6f9e`.
- **Phase 1 + Gate A (2026-11-24):** Alembic `0001_phase1_control_plane`; live non-JEvents discovery `disc_20261124T055806Z_da8f19c7` (`851` items, `20` proposed patterns, JEvents excluded); finalized Gate A in `config/crawl_scope.yaml` (`736` approved, `115` excluded, `0` pending; PDPA safeguards documented in `docs/source-policy.md`); commits `9aa733b` & `9ad40cd`.
- **Phase 2 (2026-11-25):**
  - Alembic `0002_phase2_staging` (`resource_fetches`, `resource_versions`, `media_assets`, `staged_artifacts`, `review_decisions`) + `LocalBlobStore`.
  - Offline checks: `ruff format --check`, `ruff check`, `mypy --strict` (55 files, 0 issues), `pytest` (`102/102` passed).
  - Live pilot `ext_20261125T031828Z_7fc76696`: `43/46` fetched (`3` root images returned `404`), `41` `pending_review`, `2` `quarantined` (`1stockseptember2026.png` blood stock + `St_John_Ambulance_1_Utama.jpg` low-confidence critical operating-hours field).
  - Live `WaktuOperasiApril2024.png` OCR confidence: **`0.9917`**, `source_effective_date=2024-04 (filename)`, critical-fact flagged & excluded from `stage approve --all-valid --dry-run`.
  - Live `publish --approved-only --dry-run`: `eligible_approved=0 blocked=64` (Gate B enforced at publisher layer).
- **Phase 3 (2026-11-26):**
  - Owner-approved `docs/decisions/ADR-orchestration.md` (single scheduled worker `APScheduler 3.11.3` + single-writer SQLite WAL + local `BlobStore` + dual-mode Qdrant).
  - Alembic `0003_phase3_publication_and_fts5` (`publication_versions`, `knowledge_documents`, `knowledge_chunks`, `structured_tables`, `structured_table_rows`, `structured_tables_fts_u61`, `structured_tables_fts_tri`).
  - Owner-approved `tests/eval/questions.yaml` (40 grounded Malay/English questions).
  - Measured multilingual embedding benchmark across all `531` approved PDN artifacts (`4,730` chunks, `1,021` tables):
    - `intfloat/multilingual-e5-small` (384d): Hit@1 `57.5%`, Hit@5 `77.5%`, MRR@5 `0.6558`, Fact Recall@5 `87.5%`, Citation `100.0%`, p50 `43.0 ms`, p95 `51.5 ms`, cold index `108.8 s`.
    - **`BAAI/bge-m3` (1024d, fp16 MPS — Pinned Winner):** Hit@1 **`72.5%`**, Hit@5 **`85.0%`**, MRR@5 **`0.7875`**, Fact Recall@5 **`92.5%`**, Citation **`100.0%`**, p50 **`71.4 ms`**, p95 **`101.1 ms`**, cold index `730.7 s`.
  - Production publication verified idempotent (`published=531`, `chunks=4730`, `tables=1021`, re-run `skipped_idempotent=531`, `blocked=0`).
  - Live FastAPI smoke tests passed across `/healthz`, `/v1/status`, `/v1/knowledge/search`, and `/v1/tables`.
  - Static checks & tests: `ruff format --check`, `ruff check`, `mypy --strict src tests` (`74` files, `0` issues), `pytest` (`112/112` passed).
- **Phase 4 (2026-11-26):**
  - Dedicated Gate A JEvents boundary applied to `config/crawl_scope.yaml` (bounded `monthcalendar` current + 3 months & `eventdetail` patterns; runaway views `eventsbyweek/day/year/month`, `icalrepeat`, `search`, `?option=com_jevents*` remain hard-excluded).
  - Alembic `0004_phase4_donation_schedules` (`donation_events`, `donation_sessions`, B-tree indexes verified via `EXPLAIN QUERY PLAN`, `donation_sessions_fts_u61`, `donation_sessions_fts_tri`).
  - Live bounded sync (`cal_20261126T081755Z_e74139f6`) across `2026/11`–`2027/02`: `4/4` months fetched cleanly, `47/47` `eventdetail` pages captured into `LocalBlobStore`. Live audit caught & fixed 3 real PDN calendar quirks in `jevents_html_bs4 v1.1.0` (spaced two-digit hours `1 1 .00 PAGI` / `1 0 .00 PAGI`, authoritative `.jev_evdt_summary` date overriding body typo on `/2840/`, and multi-day `From..To` range expansion on `/2780/`). All `47` artifacts (`376` sessions across `2026-10-31`..`2026-12-06`, `0` time anomalies) were approved at Gate B and published cleanly to SQLite + FTS5 (`0` Qdrant schedule points added; idempotent re-run `skipped_idempotent=578`).
  - Static checks & tests: `ruff format --check`, `ruff check`, `mypy --strict src tests` (`80` files, `0` issues), `pytest` (`120/120` passed).
- **Phase 5 (2026-11-26):**
  - Alembic `0005_phase5_reliability_and_ops` (`task_attempts`, `outbox_events`, `knowledge_chunks_fts_u61`) applied live; `ops reconcile --fts-only` synced `4,730` chunks, `1,068` tables, and `376` donation sessions with `0` FTS5 mismatches.
  - Measured Dense vs. Hybrid RRF retrieval benchmark on the live `40`-question `tests/eval/questions.yaml` suite (`BAAI/bge-m3`, fp16 MPS, `4,730` chunks, zero extra ML models):
    - **Dense only (`BAAI/bge-m3`):** Hit@1 `72.5%`, Hit@5 `87.5%`, MRR@5 `0.7917`, Fact Recall@5 `92.5%`, Citation `100.0%`, p50 `69.0 ms`, p95 `181.8 ms`.
    - **Hybrid RRF (`BAAI/bge-m3` + SQLite FTS5 BM25 `k=60` — Enabled Winner):** Hit@1 `70.0%`, **Hit@5 `95.0%` (`+7.5%`)**, **MRR@5 `0.8071` (`+0.0154`)**, **Fact Recall@5 `97.5%` (`+5.0%`)**, Citation **`100.0%`**, p50 **`78.9 ms`**, p95 **`130.1 ms`**.
  - Excluded `14` confirmed Phase 2 HTTP-404 URLs (`9` images + `5` staff profiles) in `config/crawl_scope.yaml` via `ScopeManager` (`scope validate` passed).
  - Verified live SQLite online backup (`data/backups/backup_20261126T093058Z`, `31.75 MB`, `integrity_ok=true`, `foreign_key_errors=0`, SHA-256 verified), `GET /metrics`, `GET /v1/status` (`alerts[]`), and `pdn-sync ops check-alerts`.
  - Authored `docs/architecture.md` and `docs/operations.md`; updated `README.md`, `docs/data-contracts.md`, and `.env.example`.
  - Static checks & tests: `ruff format --check src tests`, `ruff check src tests`, `mypy --strict src tests` (`93` files, `0` issues), `pytest` (`130/130` passed).

---

## Operational Notes & Known Boundaries

- **All 5 phases (0–5) complete.** All explicit acceptance criteria in `AGENTS.md` §15 are verified with deterministic tests and live evidence.
- **Embedded Qdrant file-lock boundary:** use `compose.yaml --profile qdrant` with `PDN_QDRANT_URL=http://127.0.0.1:6333` when running `pdn-sync worker start` concurrently with `pdn-sync api serve` (or pass `--skip-qdrant` to `pdn-sync ops check-alerts`).
- **Live crawl defaults remain fail-closed** unless `PDN_LIVE_CRAWL_ENABLED=true`, `PDN_SOURCE_POLICY_ACKNOWLEDGED=true`, and `PDN_CONTACT_EMAIL` are configured in `.env`.
