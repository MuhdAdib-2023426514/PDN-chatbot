# PDN Multimodal Knowledge Sync

A production-oriented, locally reproducible **continuous knowledge ingestion system** for the publicly accessible Pusat Darah Negara (PDN) website (`https://pdn.gov.my/v2/`).

## Design Principles

- **Two-track content separation:**
  - **Track 1 (Phases 0–3):** Non-JEvents website pages, HTML tables, image posters/banners (including rotating blood-stock and SmartSlider assets), and linked documents (`.pdf`, `.doc`/`.docx`). All JEvents endpoints are hard-excluded until Phase 4.
  - **Track 2 (Phase 4):** Bounded JEvents blood donation schedules parsed into SQLite + FTS5 (`unicode61 remove_diacritics 2` + `trigram`), without vectorizing raw schedule rows by default.
- **Two mandatory code-enforced gates:**
  - **Gate A (Crawl Scope):** Discovery-only enumeration exports inspectable inventories without overwriting [config/crawl_scope.yaml](config/crawl_scope.yaml). Only explicitly approved URLs or bounded patterns can enter content scraping.
  - **Gate B (Content Publication):** Extracted JSON + Markdown artifacts are staged and validated first. Publishing requires explicit approval bound to the immutable artifact hash and `semantic_hash`.
- **Safe defaults:** Live requests fail closed unless `PDN_CONTACT_EMAIL` is configured, `/v2/robots.txt` is respected, `docs/source-policy.md` is acknowledged, and `PDN_LIVE_CRAWL_ENABLED=true` is explicitly enabled.

## Quickstart (Phases 0–3 — Python 3.12 via `uv`, Zero Docker Required)

```bash
# 1. Sync Python 3.12 environment (Docling PDF, PaddleOCR PP-OCRv6, sentence-transformers)
uv sync --all-extras
uv run pdn-sync db upgrade

# 2. Configure local environment (never commit .env)
cp .env.example .env
# Edit .env and set PDN_CONTACT_EMAIL=<your-contact-email>

# 3. Validate configs and offline policy status
uv run pdn-sync scope validate
uv run pdn-sync check-policy

# 4. Phase 1: Bounded discovery-only scan & Gate A scope management
PDN_LIVE_CRAWL_ENABLED=true uv run pdn-sync discover --live
uv run pdn-sync inventory list --category poster_banner
uv run pdn-sync scope approve --url "https://pdn.gov.my/v2/" --resource-type page --check-interval 3h
uv run pdn-sync scope import data/discovery/url_inventory.csv --dry-run
uv run pdn-sync scope export

# 5. Phase 2: Approved multimodal extraction & Gate B staging/review
PDN_LIVE_CRAWL_ENABLED=true uv run pdn-sync extract --live --pilot
uv run pdn-sync stage list
uv run pdn-sync stage show <artifact_id>
uv run pdn-sync stage diff <artifact_id>
uv run pdn-sync stage approve --all-valid --dry-run
uv run pdn-sync stage override <artifact_id> --file verified_transcription.md
uv run pdn-sync reextract --extractor html --dry-run
uv run pdn-sync fixtures capture "https://pdn.gov.my/v2/"

# 6. Phase 3: Approved-only publication, retrieval eval, read-only API, and scheduled worker
uv run pdn-sync publish --approved-only --dry-run
uv run pdn-sync publish --approved-only --execute
uv run pdn-sync status
uv run pdn-sync eval run --questions tests/eval/questions.yaml
uv run pdn-sync api serve --host 127.0.0.1 --port 8000
uv run pdn-sync worker run-once --profile publish_sync
# Optional: docker compose --profile qdrant up -d  (set PDN_QDRANT_URL=http://127.0.0.1:6333)

# 7. Phase 4: Bounded JEvents blood donation schedules (SQLite + FTS5 + /v1/events)
PDN_LIVE_CRAWL_ENABLED=true uv run pdn-sync calendar sync --live --months-ahead 3
uv run pdn-sync stage list --category donation_schedule
uv run pdn-sync stage approve --category donation_schedule --dry-run
uv run pdn-sync publish --approved-only --execute
uv run pdn-sync worker run-once --profile calendar --live

# 8. Phase 5: Reliability, observability, online backup/restore, alerts & hybrid eval
uv run pdn-sync ops check-alerts --fail-on critical
uv run pdn-sync ops backup
uv run pdn-sync ops verify-backup data/backups/backup_<TIMESTAMP>
uv run pdn-sync ops restore data/backups/backup_<TIMESTAMP> --confirm
uv run pdn-sync ops reconcile --dry-run
uv run pdn-sync ops dlq list --status dead_letter
uv run pdn-sync eval run --mode compare --questions tests/eval/questions.yaml

# 9. Run quality checks & test suite
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy --strict src tests
uv run pytest -v
```

## Key Documents & Configs

- [AGENTS.md](AGENTS.md) — Architecture, non-negotiable rules, contracts, and phased rollout gates
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Stage checklist, verified package versions, Phase 2–5 benchmarks, and risks
- [docs/architecture.md](docs/architecture.md) — System architecture diagrams, two-gate/two-track boundaries, storage ownership, and API summary
- [docs/operations.md](docs/operations.md) — Operator runbooks: alert triage, SQLite online backup/restore, WAL recovery, outage/DLQ replay, and Docker Qdrant
- [docs/decisions/ADR-orchestration.md](docs/decisions/ADR-orchestration.md) — Phase 3 orchestration decision record (single worker + SQLite WAL + dual-mode Qdrant)
- [docs/source-policy.md](docs/source-policy.md) — Live verification evidence, PDPA guidance, and 5-point clearance checklist
- [docs/crawl-scope-review.md](docs/crawl-scope-review.md) — Gate A reviewer workflow for `config/crawl_scope.yaml`
- [docs/extraction-review.md](docs/extraction-review.md) — Gate B staging, batch approval, quarantine override, and `reextract` guide
- [docs/data-contracts.md](docs/data-contracts.md) — `semantic_hash` specification + golden vectors, inventory, staging, Qdrant payload, and API/metrics contracts
- [config/crawl_scope.yaml](config/crawl_scope.yaml) — Human-owned Gate A scope authority (never overwritten by discovery)
- [config/ingestion_policy.yaml](config/ingestion_policy.yaml) — Gate B review mode, critical-fact rules, OCR thresholds, hybrid retrieval, retry & alert policies
- [tests/eval/questions.yaml](tests/eval/questions.yaml) — Owner-approved 40-question Malay/English retrieval evaluation suite
