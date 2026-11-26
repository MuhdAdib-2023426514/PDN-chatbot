# ADR-001: Phase 3 Orchestration, Storage & Vector Store Architecture

- **Status:** Accepted (owner-approved via Phase 3 implementation plan on `2026-11-26`)
- **Date:** `2026-11-26`
- **Decision Makers:** Project Owner
- **Scope:** Phase 3–4 scheduling, single-writer SQLite ownership, blob storage, and Qdrant deployment mode

---

## 1. Context & Measured Phase 2 Workload

Per `AGENTS.md` Section 3, before activating distributed orchestration infrastructure (Airflow + PostgreSQL + RabbitMQ + Celery + FastAPI DB-owner + MinIO), we must evaluate measured Phase 2 numbers from the real Pusat Darah Negara (`https://pdn.gov.my/v2/`) corpus.

### Measured Full-Corpus Metrics (`ext_20261126T001234Z_f1afbfcd` + Gate B Triage)

| Metric | Measured Value |
|---|---:|
| Gate-A approved targets | `736` (`426` pages, `68` PDFs, `242` images) |
| Staged distinct resources | `721` (`420` pages, `68` PDFs, `233` images; `14` HTTP 404s, `1` 12000×12000 PNG transcribed) |
| Gate-B approved publish-ready corpus | **`531`** (`317` pages, `151` images/posters, `63` PDFs) |
| Total approved text volume | **`1,610,396` chars** (`~402,600` tokens ≈ `1,000–1,600` chunks) |
| Total approved structured tables | **`1,021`** (`985` small `≤ 30` rows, `36` large `> 30` rows) |
| Mean extract / OCR latency | HTML `0.045 s`, Docling PDF `13.85 s`, PP-OCRv6 CPU `23.18 s` |
| Cold full-corpus single-threaded wall clock | `8,002.8 s` (`~2h 13m`) at `1` concurrent request/host (`2–5 s` polite delay) |
| Cold full-corpus peak RSS (Docling + RapidOCR + PP-OCRv6 in one process) | `4,255.5 MB` (`~4.25 GB`) |
| Typical incremental re-check duration | **Seconds to `~2 min`** (conditional `ETag`/`Last-Modified` + unchanged-blob skip + `semantic_hash` carry-forward) |
| Target developer environment | macOS Apple Silicon (`arm64`), Python `3.12.14` (`uv`), SQLite `3.53.1` (FTS5 `unicode61` + `trigram`), **no Docker installed locally** |

### Key Constraints

1. **Politeness bottleneck (`1` concurrent request/host, `2–5 s` jittered delay):** Adding distributed Celery workers cannot speed up live HTTP fetching against `pdn.gov.my` because host concurrency is strictly capped at `1`.
2. **Incremental workload dominates steady-state:** After the one-time cold capture, scheduled checks inspect conditional validators (`ETag`/`Last-Modified`) and `LocalBlobStore` SHA-256 digests, only running Docling or PaddleOCR on the handful of posters/pages that actually changed.
3. **Single-writer SQLite simplicity:** A single worker process owns SQLite directly (`PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout`), eliminating network-serialized DB-owner hops and multi-container write-lock coordination.
4. **Zero-Docker local reproducibility:** Keeping core development runnable with `uv` alone (SQLite + local `BlobStore` + embedded `qdrant-client` local disk) ensures any contributor can run, test, benchmark, and serve the chatbot API locally without Docker overhead, while retaining an optional pinned `compose.yaml` profile for server deployments.

---

## 2. Options Considered

| Dimension | Option A (Selected): Single Scheduled Worker + SQLite WAL + Local `BlobStore` + Dual-Mode Qdrant | Option B (Deferred): Airflow 3 + PostgreSQL + RabbitMQ + Celery + FastAPI DB-Owner + MinIO |
|---|---|---|
| **Runtime services** | 1 worker CLI (`APScheduler 3.11.3` / cron) + 1 read-only FastAPI process | 6+ containers (Airflow scheduler/webserver, PostgreSQL, RabbitMQ, Celery worker(s), FastAPI DB-owner, Qdrant, MinIO) |
| **Idle RAM overhead** | `< 150 MB` idle (`~1–2 GB` during API/embeddings) | `3–5 GB+` idle before any ML models load |
| **SQLite ownership** | Direct single-writer (`WAL`, `foreign_keys=ON`, `busy_timeout`) | Workers banned from SQLite; must serialize all writes over internal HTTP DB-owner API + outbox relay |
| **Blob storage** | Local content-addressed `BlobStore` (`data/blobs/<sha256[:2]>/<sha256>`) behind clean interface | MinIO container + S3 credentials + bucket lifecycle |
| **Vector store** | Dual-mode `qdrant-client==1.20.0`: embedded local disk (`data/qdrant/`) default, remote `QDRANT_URL` (`qdrant/qdrant:v1.20.2-unprivileged`) when running concurrent worker + API | Mandatory Docker Qdrant container |
| **Docker requirement** | Optional (`compose.yaml` profiles provided for server deployment) | Mandatory for every local dev & test run |
| **Phase 4 JEvents fit** | Excellent — 4 months of JEvents pages (`~50–150` pages) parse in `< 3 min` politely | Over-provisioned for single-host bounded calendar sync |

---

## 3. Decision

Adopt **Option A: Single Scheduled Worker Process (`APScheduler 3.11.3` / CLI cron) + Single-Writer SQLite WAL + Local `BlobStore` + Dual-Mode Qdrant**.

1. **Framework-agnostic pipeline functions:** Every sync profile (`homepage`, `site`, `discovery_audit`, `publish_sync`, and Phase 4 `calendar`) remains a plain Python function callable directly via `pdn-sync worker run-once --profile <name>` or scheduled by `pdn-sync worker start` (`BlockingScheduler(timezone="Asia/Kuala_Lumpur")`, `max_instances=1`, `coalesce=True`).
2. **Direct single-writer SQLite:** The CLI/worker process is the sole SQLite writer. FastAPI serves strictly **read-only** `GET` endpoints over SQLite WAL snapshots.
3. **Local content-addressed `BlobStore`:** Continue using `LocalBlobStore` (`data/blobs/`). MinIO is deferred until multi-host blob sharing is actually required.
4. **Dual-mode Qdrant:** Default to `QdrantClient(path="data/qdrant")` for zero-Docker local development, evaluation, and single-process publishing. When running the background worker concurrently with the FastAPI server (since embedded Qdrant holds an OS file lock), configure `PDN_QDRANT_URL=http://127.0.0.1:6333` against the pinned `qdrant/qdrant:v1.20.2-unprivileged` container in `compose.yaml`.
5. **No Airflow, PostgreSQL, RabbitMQ, Celery, or MinIO in Phase 3.**

---

## 4. Consequences & Revisit Triggers

### Positive
- Zero container orchestration tax; full local suite runs cleanly on macOS ARM64 with `uv`.
- Eliminates dual-database metadata drift (no Airflow PostgreSQL instance to operate or back up).
- Keeps full upgrade path open: because pipeline functions are plain Python and `BlobStore` / `QdrantVectorStore` are behind clean interfaces, moving to distributed workers later requires only adding a thin task wrapper.

### Trade-offs & Operational Guardrails
- **Embedded Qdrant file lock:** Do not run `pdn-sync publish --execute` and `pdn-sync api serve` simultaneously against the same local `data/qdrant/` directory; either stop the API during CLI publish or switch `PDN_QDRANT_URL` to the pinned Docker Qdrant server (`compose.yaml --profile qdrant`).
- **Peak RSS during rare full re-extraction:** Run `homepage` / `site` checks sequentially (`max_instances=1`) so Docling and PaddleOCR never overlap in parallel workers.

### Quantitative Triggers to Revisit Option B (Distributed Orchestration)
Re-open this ADR and consider Airflow + RabbitMQ + Celery + MinIO **only if** one or more of the following are measured in production:
1. Approved active corpus exceeds **`5,000` resources** or daily changed OCR/PDF count exceeds **`200` items/day** (single-process CPU queue > 1 hour/day).
2. Ingestion expands to **multiple independent hosts** requiring concurrent multi-host fetching beyond `1` host slot.
3. SQLite WAL write contention exceeds **`100 ms` p95** or multi-machine worker deployment requires shared network storage.
