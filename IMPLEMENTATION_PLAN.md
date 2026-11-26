# IMPLEMENTATION_PLAN.md — PDN Multimodal Knowledge Sync

> Tracks phased rollout progress, verified dependency versions, acceptance evidence, measured Phase 2 benchmarks, and open risks.

## Phase Progress Checklist

- [x] **Phase 0 — Project foundation and source access policy** *(completed and owner-acknowledged 2026-11-24)*
- [x] **Phase 1 — Discovery-only URL & asset inventory + Gate A approval (JEvents excluded)** *(live discovery `disc_20261124T055806Z_da8f19c7` and Gate A completed 2026-11-24)*
- [x] **Phase 2 — Approved multimodal extraction & Gate B staging (2a HTML, 2b PDFs/docs, 2c Posters/OCR)** *(code, 102/102 tests, pilot `ext_20261125T031828Z_7fc76696`, full 736-item resumable run `ext_20261126T001234Z_f1afbfcd` [721 staged, 14 HTTP 404s, 1 PNG transcribed], and Gate B human triage [531 approved, 214 rejected, 0 unresolved] completed 2026-11-26)*
- [x] **Phase 3 — Orchestration ADR, approved publication & chatbot read API** *(completed 2026-11-26: `docs/decisions/ADR-orchestration.md` approved; Alembic `0003_phase3_publication_and_fts5`; 40-question Malay/English `tests/eval/questions.yaml` approved; `BAAI/bge-m3` won benchmark [Hit@1 72.5%, Hit@5 85.0%, MRR@5 0.7875, Fact Recall@5 92.5%, Citation 100%] over `multilingual-e5-small`; all 531 approved artifacts published [4,730 Qdrant points, 1,021 SQLite+FTS5 tables]; live read-only FastAPI API & 112/112 tests passing)*
- [ ] **Phase 4 — Structured blood donation schedules (JEvents → SQLite + FTS5)**
- [ ] **Phase 5 — Reliability, observability, and operations**

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
| `rapidfuzz` | `3.14.6` | Phase 3 | Bounded typo-tolerant fallback ranking for Malay/English SQLite FTS5 table search |
| `pytest` / `ruff` / `mypy` | `9.2.0` / `0.17.6` / `2.4.1` | Phase 0–3 | Unit/integration tests (`112/112`), lint/format, strict static type checking (`74` files) |

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

---

## Open Blockers & Risks Before Phase 4

- **14 previously-approved URLs returned HTTP `404` during Phase 2 live extraction** (`5` staff-directory leaf URLs + `9` images); can optionally be excluded in `config/crawl_scope.yaml`.
- **Embedded Qdrant file-lock boundary:** `QdrantClient(path="data/qdrant")` allows one process at a time; run `compose.yaml --profile qdrant` with `PDN_QDRANT_URL=http://127.0.0.1:6333` when running `pdn-sync worker start` concurrently with `pdn-sync api serve`.
- **Phase 4 prerequisite:** Lift the Phase-1 JEvents exclusion via a dedicated Gate A review for bounded `monthcalendar` (current + next 3 months) and `eventdetail` pattern rules before scraping blood donation schedules into SQLite + FTS5.
