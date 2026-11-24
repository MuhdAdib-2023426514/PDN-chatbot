# IMPLEMENTATION_PLAN.md — PDN Multimodal Knowledge Sync

> Tracks phased rollout progress, verified dependency versions, acceptance evidence, and open risks.

## Phase Progress Checklist

- [x] **Phase 0 — Project foundation and source access policy** *(completed and owner-acknowledged 2026-11-24; ready for Phase 1)*
- [ ] **Phase 1 — Discovery-only URL & asset inventory + Gate A approval (JEvents excluded)**
- [ ] **Phase 2 — Approved multimodal extraction & Gate B staging (2a HTML, 2b PDFs/docs, 2c Posters/OCR)**
- [ ] **Phase 3 — Orchestration ADR, approved publication & chatbot read API**
- [ ] **Phase 4 — Structured blood donation schedules (JEvents → SQLite + FTS5)**
- [ ] **Phase 5 — Reliability, observability, and operations**

---

## Verified Runtime & Package Versions

Verified via `uv sync` on macOS Apple Silicon (`arm64`) on `2026-11-24` and locked in `uv.lock`:

| Component | Verified Version | Notes |
|---|---|---|
| Python | `3.12.14` | Pinned via `.python-version` (`3.12`) and `requires-python = ">=3.12,<3.13"` for ML/OCR wheel compatibility (notably PaddlePaddle in Phase 2c) |
| `uv` | `0.12.21` | Package manager and lockfile (`uv.lock`) |
| `pydantic` | `2.14.4` | Schema contracts (`extra="forbid"` on configs) |
| `pydantic-settings` | `2.16.3` | `.env` / environment loading |
| `ruamel.yaml` | `0.19.1` | Safe YAML reading and future comment-preserving round-trip writes |
| `httpx` | `0.28.1` | Polite HTTP client (Phase 0 verification / Phase 1 discovery) |
| `typer` | `0.29.2` | CLI entrypoint (`pdn-sync`) |
| `pytest` | `9.2.0` | Unit & fixture test runner |
| `ruff` | `0.17.6` | Linter & formatter |
| `mypy` | `2.4.1` | Strict static type checker |

*Later-phase dependencies (SQLAlchemy, Alembic, BeautifulSoup/lxml, Docling, PaddleOCR, Qdrant, FastAPI, Airflow/Celery or APScheduler) will be verified against official release channels before pinning in their respective phases.*

---

## Phase 0 Acceptance Evidence (2026-11-24)

1. **Prototype archival:** Pre-existing exploratory files (`crawler.py`, `requirements.txt`, partial `data/`) reside under `archive/prototype-2026-11-24/` and are excluded from version control via `.gitignore` (never used as fixtures or knowledge sources).
2. **Version control & scaffolding:** Initialized Git (`main` branch), `.gitignore`, `.python-version`, `.env.example`, `pyproject.toml`, `uv.lock`, `config/crawl_scope.yaml`, `config/ingestion_policy.yaml`, `docs/source-policy.md`, `docs/data-contracts.md`, and `README.md`.
3. **Polite live verification (zero crawling):** Checked `https://pdn.gov.my/robots.txt` (`403`), `https://pdn.gov.my/v2/robots.txt` (`200`, 14 Joomla `Disallow` paths), `https://pdn.gov.my/v2/sitemap.xml` (`404`), and `https://pdn.gov.my/v2/` (`200`, no `X-Robots-Tag`, no `<meta name="robots">`, no anti-automation terms link). Recorded dated evidence in `docs/source-policy.md`.
4. **Policy guardrails implemented & unit-tested:**
   - Fail-closed `User-Agent` validation (`PDN_CONTACT_EMAIL` required)
   - 5-point source-access clearance check (Criterion 5 defaults to `false` until owner acknowledges `docs/source-policy.md`)
   - URL normalization, host/prefix scope bounds, SSRF/private-IP/DNS guards, hard Phase 0–3 JEvents exclusion
   - Subfolder `/v2/robots.txt` parser with root `403` tolerance and `/v2/robots.txt` fail-closed handling
   - Gate A `crawl_scope.yaml` schema validator & precedence evaluator (rejects `/**` in approvals, allows `/**` in exclusions)
   - Deterministic `semantic_hash` with locked golden vectors (`V1`, `V2`, `V3`)
   - CLI verification via `uv run pdn-sync scope validate` and `uv run pdn-sync check-policy`

---

## Open Blockers & Risks Before Phase 1

- **Phase 0 gate cleared:** Owner acknowledgement (`PDN_SOURCE_POLICY_ACKNOWLEDGED=true`) verified on `2026-11-24`. Keep `PDN_LIVE_CRAWL_ENABLED=false` during Phase 1 implementation and offline fixture tests, then opt in (`PDN_LIVE_CRAWL_ENABLED=true`) only when running the bounded live discovery-only scan.
- **Future verification items:** Confirm exact Apple Silicon (`arm64`) & Linux container compatibility for Docling (Phase 2b) and PaddleOCR (Phase 2c) before pinning.
