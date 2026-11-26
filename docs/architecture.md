# Architecture — PDN Multimodal Knowledge Sync

> Single-writer, two-gate, two-track continuous knowledge ingestion system for Pusat Darah Negara (`https://pdn.gov.my/v2/`).
> Orchestration decisions are documented in [ADR-orchestration.md](decisions/ADR-orchestration.md); source-access and PDPA rules in [source-policy.md](source-policy.md); typed schemas in [data-contracts.md](data-contracts.md).

---

## 1. High-Level System Architecture

```mermaid
flowchart TD
    PDN["PDN Public Website (https://pdn.gov.my/v2/)"]

    subgraph Track1["Track 1: Unstructured & General Knowledge (Phases 1-3)"]
        DISC["Discovery-Only Crawler (Non-JEvents BFS)"]
        INV["Inventory Exports (url_inventory.json / .csv / discovery_report.md)"]
        GATEA{"Gate A: Human Scope Approval (config/crawl_scope.yaml)"}
        FETCH1["Polite Gate-A Fetcher (1 req/host, 2-5s jitter, 50 MiB cap)"]
        EX_HTML["2a HTML & Tables (BeautifulSoup + lxml)"]
        EX_PDF["2b PDFs & Docs (Docling 2.152.0)"]
        EX_OCR["2c Posters & Banners (PaddleOCR 3.7.0 PP-OCRv6 CPU)"]
    end

    subgraph Track2["Track 2: Structured Donation Schedules (Phase 4)"]
        CAL_SYNC["Bounded JEvents Calendar Sync (current + 3 months)"]
        EX_JEV["JEvents Multi-Session Parser (jevents_html_bs4 v1.1.0)"]
    end

    subgraph Staging["Immutable Evidence & Gate B Staging"]
        BLOB["Local Content-Addressed BlobStore (data/blobs/<sha256[:2]>/<sha256>)"]
        STAGE["Staged JSON + Markdown (data/extracted/) + semantic_hash"]
        GATEB{"Gate B: Human Review & Audit (review_decisions)"}
    end

    subgraph Storage["Authoritative Single-Writer Knowledge Stores"]
        SQLITE["SQLite WAL + FTS5 (data/pdn_sync.db)\n- Control plane, staging, outbox_events, task_attempts\n- Published structured_tables + FTS5 (u61 + trigram)\n- Published donation_events & donation_sessions + FTS5\n- Published knowledge_chunks + FTS5 (u61)"]
        QDRANT["Qdrant Vector Store (BAAI/bge-m3 1024d)\n- Embedded data/qdrant/ or Docker compose --profile qdrant\n- Published prose, FAQs, PDFs, poster OCR, small tables\n- NO JEvents donation schedule rows"]
    end

    subgraph Serving["Read-Only API & Observability (127.0.0.1)"]
        API["FastAPI Read-Only Service\n/v1/knowledge/search | /v1/tables | /v1/events\n/v1/status | /healthz | /metrics"]
        WORKER["APScheduler Single Worker (Asia/Kuala_Lumpur, max_instances=1)"]
        OPS["Ops CLI (pdn-sync ops backup/restore/reconcile/check-alerts/dlq)"]
    end

    PDN --> DISC --> INV --> GATEA
    GATEA -->|"Approved non-JEvents"| FETCH1
    GATEA -->|"Bounded JEvents patterns"| CAL_SYNC
    FETCH1 --> BLOB
    CAL_SYNC --> BLOB
    FETCH1 --> EX_HTML --> STAGE
    FETCH1 --> EX_PDF --> STAGE
    FETCH1 --> EX_OCR --> STAGE
    CAL_SYNC --> EX_JEV --> STAGE
    STAGE --> GATEB
    GATEB -->|"Approved only"| SQLITE
    GATEB -->|"Unstructured chunks only"| QDRANT
    SQLITE --> API
    QDRANT --> API
    WORKER --> FETCH1
    WORKER --> CAL_SYNC
    WORKER --> DISC
    OPS --> SQLITE
    OPS --> QDRANT
```

---

## 2. Two Mandatory Approval Gates (Code-Enforced)

1. **Gate A (`config/crawl_scope.yaml` — Single Source of Truth):**
   - Discovery (`pdn-sync discover`) enumerates candidate pages, posters, and documents without persisting page bodies or downloading asset bytes. Discovery never overwrites `config/crawl_scope.yaml`.
   - Every content fetch (`ApprovedContentFetcher`, `run_calendar_sync_pipeline`) compiles `config/crawl_scope.yaml` and fails closed on any `pending`, `excluded`, off-domain, private-IP, or oversized (>50 MiB) URL.
   - Explicit exclusions always override approvals. Runaway JEvents views (`eventsbyweek`, `eventsbyday`, `eventsbyyear`, `icalrepeat`, `search`, `?option=com_jevents*`) remain hard-excluded.
2. **Gate B (`staged_artifacts` + `review_decisions`):**
   - Every extractor emits typed `ExtractionResult` JSON + human-readable Markdown under `data/extracted/` and computes a deterministic `semantic_hash` (normalizing whitespace/chrome while preserving meaningful text, tables, OCR lines, and structured schedule fields).
   - `PublicationRepository.publish_artifact_atomic()` and `StagingRepository.assert_artifact_approved_for_publish()` re-verify the staged JSON file's recomputed `semantic_hash` against the recorded human `review_decisions` hash inside the write transaction.
   - Pending, rejected, quarantined, tampered, or stale (`version_seq < active`) versions are blocked at the database layer and can never enter active SQLite tables, FTS5 indexes, or Qdrant.
   - Unchanged meaning across re-fetches or extractor upgrades carries approval forward with an audited `carried_forward` decision; meaningful changes invalidate prior approval and flag active published records with `pending_change=true` until reviewed.

---

## 3. Publish, Outage Retry, and Outbox Reconciliation Sequence

```mermaid
sequenceDiagram
    participant Op as Operator / Worker
    participant Pub as PublicationService
    participant DB as SQLite (Single-Writer WAL)
    participant QD as Qdrant (BAAI/bge-m3)
    participant Rec as pdn-sync ops reconcile

    Op->>Pub: publish_approved()
    Pub->>DB: assert_artifact_approved_for_publish()
    Pub->>DB: publish_artifact_atomic() (docs, chunks, tables, FTS5, schedules)
    DB-->>Pub: Committed (SQLite authoritative state updated)
    Pub->>QD: execute_with_retry(upsert_points)
    alt Qdrant Healthy
        QD-->>Pub: Upsert OK
        Pub->>DB: mark_chunks_qdrant_synced() + record task_attempt(succeeded)
    else Qdrant Outage After max_attempts
        QD--xPub: RetryExhaustedError
        Pub->>DB: record task_attempt(dead_lettered)
        Pub->>DB: enqueue_outbox_event(qdrant_upsert_publication)
        Note over DB: Published SQLite tables & schedules remain intact
    end
    Op->>Rec: pdn-sync ops reconcile
    Rec->>DB: Fetch due outbox_events + check FTS5 counts
    Rec->>QD: Replay idempotent upsert / retirement
    QD-->>Rec: OK
    Rec->>DB: mark_outbox_dispatched() + mark_chunks_qdrant_synced()
```

---

## 4. Storage Ownership & Boundary Rules

| Store | Location | Authoritative Contents | Never Stored Here |
|---|---|---|---|
| **Crawl Scope YAML** | `config/crawl_scope.yaml` | Gate A exact URLs, bounded rotating-asset patterns, explicit exclusions | Runtime transient state |
| **Ingestion Policy YAML** | `config/ingestion_policy.yaml` | Politeness, discovery bounds, OCR thresholds, critical-fact keywords, indexing/retrieval mode, retry & alert thresholds | Credentials or secrets |
| **BlobStore** | `data/blobs/<sha256[:2]>/<sha256>` | Immutable raw bytes of Gate-A-approved HTML, PDFs, and posters | Unapproved assets, discovery HTML |
| **Staging Artifacts** | `data/extracted/<category>/` | Reviewable JSON + Markdown (`ExtractionResult`) | Active serving indexes |
| **SQLite (WAL + FTS5)** | `data/pdn_sync.db` | Control plane (`sync_runs`, `crawl_resources`, `scope_decisions`), staging & audit (`staged_artifacts`, `review_decisions`), reliability (`task_attempts`, `outbox_events`), published `structured_tables`, published `donation_events`/`donation_sessions`, FTS5 (`unicode61 remove_diacritics 2` + `trigram`) | Unapproved rows inside active/FTS5 tables |
| **Qdrant** | `data/qdrant/` or `http://127.0.0.1:6333` | Approved unstructured chunks (`BAAI/bge-m3`, 1024d): articles, FAQs, PDFs, poster OCR, small informational tables (≤30 rows) | Pending/rejected/quarantined/retired artifacts, JEvents donation schedule rows |

---

## 5. Read-Only Chatbot HTTP API Contract (`127.0.0.1:8000`)

All serving endpoints are strictly read-only (`GET`), bind to `127.0.0.1` by default, propagate `X-Request-ID` correlation headers, and return **only** `status == 'published'` (non-superseded, non-retired) records with canonical `source_url`, `last_verified_at`, `source_effective_date`, and `pending_change`:

- `GET /v1/knowledge/search?q=...&lang=ms|en|any&category=...&limit=1..20` — Unstructured knowledge search (dense `BAAI/bge-m3` or hybrid Dense + SQLite FTS5 BM25 RRF per `config/ingestion_policy.yaml`).
- `GET /v1/tables?q=...&source_url=...&limit=1..20` — Published structured HTML/PDF/poster tables (SQLite FTS5 `unicode61` → `trigram` → RapidFuzz).
- `GET /v1/events?date=YYYY-MM-DD&date_from=...&date_to=...&state=...&q=...&limit=1..50` — Published JEvents blood donation sessions (SQLite B-tree filters + FTS5 `unicode61`/`trigram` + RapidFuzz; raw cells preserved).
- `GET /healthz` — SQLite + Qdrant liveness/readiness and active point count.
- `GET /v1/status` — Published counts, Gate B backlog counts/age, FTS5 mismatches, DLQ count, active `alerts[]`, and last completed sync runs per profile.
- `GET /metrics` — Localhost-only Prometheus text exposition (`pdn_sync_*` operational gauges + API request counters/latency histograms).
