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

## Quickstart (Phases 0–1 — Python 3.12 via `uv`)

```bash
# 1. Sync Python 3.12 environment and initialize SQLite control plane
uv sync
uv run pdn-sync db upgrade

# 2. Configure local environment (never commit .env)
cp .env.example .env
# Edit .env and set PDN_CONTACT_EMAIL=<your-contact-email>

# 3. Validate configs and offline policy status
uv run pdn-sync scope validate
uv run pdn-sync check-policy

# 4. Run Phase 1 bounded discovery-only scan (JEvents excluded; ~15-30 min polite run)
PDN_LIVE_CRAWL_ENABLED=true uv run pdn-sync discover --live

# 5. Inspect inventory & apply Gate A decisions (never overwrites YAML comments)
uv run pdn-sync inventory list --category poster_banner
uv run pdn-sync scope approve --url "https://pdn.gov.my/v2/" --resource-type page --check-interval 3h
uv run pdn-sync scope approve --pattern-id pattern-blood-stock-graphic-images-stokdarah-png
uv run pdn-sync scope import data/discovery/url_inventory.csv --dry-run
uv run pdn-sync scope export

# 6. Run quality checks & test suite
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest -v
```

## Key Documents & Configs

- [AGENTS.md](AGENTS.md) — Architecture, non-negotiable rules, contracts, and phased rollout gates
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Stage checklist, verified package versions, evidence, and risks
- [docs/source-policy.md](docs/source-policy.md) — Live verification evidence, PDPA guidance, and 5-point clearance checklist
- [docs/crawl-scope-review.md](docs/crawl-scope-review.md) — Gate A reviewer workflow for `config/crawl_scope.yaml`
- [docs/data-contracts.md](docs/data-contracts.md) — `semantic_hash` specification + golden vectors, inventory, staging, and API contracts
- [config/crawl_scope.yaml](config/crawl_scope.yaml) — Human-owned Gate A scope authority (never overwritten by discovery)
- [config/ingestion_policy.yaml](config/ingestion_policy.yaml) — Gate B review mode, critical-fact rules, OCR thresholds, and bounds
