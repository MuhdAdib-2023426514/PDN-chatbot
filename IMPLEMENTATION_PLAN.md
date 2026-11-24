# IMPLEMENTATION_PLAN.md — PDN Multimodal Knowledge Sync

> Tracks phased rollout progress, verified dependency versions, acceptance evidence, and open risks.

## Phase Progress Checklist

- [x] **Phase 0 — Project foundation and source access policy** *(completed and owner-acknowledged 2026-11-24)*
- [x] **Phase 1 — Discovery-only URL & asset inventory + Gate A approval (JEvents excluded)** *(code, offline suite, live discovery `disc_20261124T055806Z_da8f19c7`, and Gate A human approvals completed 2026-11-24; ready for Phase 2)*
- [ ] **Phase 2 — Approved multimodal extraction & Gate B staging (2a HTML, 2b PDFs/docs, 2c Posters/OCR)**
- [ ] **Phase 3 — Orchestration ADR, approved publication & chatbot read API**
- [ ] **Phase 4 — Structured blood donation schedules (JEvents → SQLite + FTS5)**
- [ ] **Phase 5 — Reliability, observability, and operations**

---

## Verified Runtime & Package Versions

Verified via `uv sync` on macOS Apple Silicon (`arm64`) on `2026-11-24` and locked in `uv.lock`:

| Component | Verified Version | Phase | Notes |
|---|---|---|---|
| Python | `3.12.14` | Phase 0 | Pinned via `.python-version` (`3.12`) and `requires-python = ">=3.12,<3.13"` |
| `uv` | `0.12.21` | Phase 0 | Package manager and lockfile (`uv.lock`) |
| `pydantic` | `2.14.4` | Phase 0 | Strict schema contracts (`extra="forbid"` on configs) |
| `pydantic-settings` | `2.16.3` | Phase 0 | `.env` / environment loading |
| `ruamel.yaml` | `0.19.1` | Phase 0/1 | Safe YAML reads + comment-preserving Gate A round-trip writes |
| `httpx` | `0.28.1` | Phase 0/1 | Polite HTTP client & mock transport for offline tests |
| `typer` | `0.29.2` | Phase 0/1 | CLI entrypoint (`pdn-sync`) |
| `sqlalchemy` | `2.1.9` | Phase 1 | Single-writer SQLite control-plane ORM (`WAL`, `foreign_keys=ON`) |
| `alembic` | `1.20.1` | Phase 1 | SQLite batch migrations (`render_as_batch=True`) |
| `beautifulsoup4` | `4.15.0` | Phase 1 | Transient HTML link/asset metadata extraction |
| `lxml` | `6.1.3` | Phase 1 | Fast HTML parser backend |
| `pytest` | `9.2.0` | Phase 0/1 | Unit & offline integration test runner |
| `ruff` | `0.17.6` | Phase 0/1 | Linter & formatter |
| `mypy` | `2.4.1` | Phase 0/1 | Strict static type checker |
| `types-beautifulsoup4` | `4.12.0.20250516` | Phase 1 | Type stubs for `mypy --strict` |

*Later-phase dependencies (Docling, PaddleOCR, Qdrant, FastAPI, Airflow/Celery or APScheduler) will be verified against official release channels before pinning in their respective phases.*

---

## Phase 0 Acceptance Evidence (2026-11-24)

1. **Prototype archival:** Pre-existing exploratory files (`crawler.py`, `requirements.txt`, partial `data/`) reside under `archive/prototype-2026-11-24/` and are gitignored.
2. **Polite live verification (zero crawling):** Checked `/robots.txt` (`403`), `/v2/robots.txt` (`200`, 14 Joomla `Disallow` paths), `/v2/sitemap.xml` (`404`), and `/v2/` (`200`, no `X-Robots-Tag`, no `<meta name="robots">`, no anti-automation terms link). Recorded in `docs/source-policy.md`.
3. **Owner clearance:** Owner acknowledged `docs/source-policy.md` (`PDN_SOURCE_POLICY_ACKNOWLEDGED=true`).
4. **Tests & commit:** `60/60` Phase 0 unit tests passing; pushed as commit `91b6f9e`.

---

## Phase 1 Acceptance Evidence (2026-11-24)

1. **SQLite Control Plane & Alembic (`0001_phase1_control_plane`):** Migrated `sync_runs`, `crawl_resources`, `discovered_urls`, `scope_decisions`, and `compiled_scope_cache` with `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, and `busy_timeout=5000`.
2. **Offline Verification Suite:** `91/91` unit and integration tests passing (`ruff format --check`, `ruff check`, `mypy --strict` across 39 files, `alembic upgrade head`, `pytest -v`), proving JEvents exclusion, staff-directory leaf protection, selective `HEAD`-only asset probing, K2 deduplication, anomaly-growth abort, YAML comment preservation, and Gate A content-crawl blocking.
3. **Bounded Live Discovery Run (`disc_20261124T055806Z_da8f19c7`):**
   - Runtime: `2026-11-24T05:58:06Z` → `06:28:33Z` (~30 min polite scan, 1 req/host, 2–5 s jitter, selective `HEAD` on 99 candidate posters/docs).
   - Total candidates inventoried: **`851`** (`444` pages, `278` image posters, `70` documents [`68` PDF + `2` Office], `59` external refs).
   - Duplicates flagged (`duplicate_of`): **`241`**; broken links: **`17`**; staff directory (`recommended_exclude`, PDPA): **`91`**; JEvents links excluded without fetching: **`1`**.
   - Proposed rotating-asset / grouped document bounded patterns: **`20`** (including `stokdarah/*.png` with `latest_wins`, `SmartSlider3/2025/*.png`, `it_pdn/2026/*.png`, `dokumen/*.pdf`).
   - Verified `config/crawl_scope.yaml` untouched (`git diff config/crawl_scope.yaml` empty; `scope_file_hash` identical).
   - Artifacts generated: `data/discovery/url_inventory.json`, `data/discovery/url_inventory.csv`, `data/discovery/discovery_report.md`, `data/discovery/crawl_scope.proposed.yaml`.

---

## Open Blockers & Risks Before Phase 2

- **Gate A completed (2026-11-24):** `config/crawl_scope.yaml` finalized via audited comment-preserving `ScopeManager` (`409` approval rules, `19` URL + `33` pattern exclusions; joined inventory: **`736` approved**, **`115` excluded**, **`0` pending**). Approves all `426` live in-scope `/v2/` pages (including core pages, all 4 `direktori-pegawai` sections with PDPA safeguards in `docs/source-policy.md`, `/v2/tender`, `/v2/pengurusan-tenaga`, and 241 K2 aliases for Gate B filtering), `242` images/posters (for Gate B OCR filtering), and all `68` live PDFs. Keeps strictly excluded: `59` external/non-`/v2/` URLs, `1` JEvents URL (Phase 0–3), `2` password-reset system endpoints, `14` confirmed `404` dead links, `2` legacy `.doc` files, and `37` decorative logos/icons/templates.
- **Transient server disconnects during live discovery:** 3 of ~455 requests hit `Server disconnected without sending a response` (`item/60`, `item/99`, `Pusat_Darah_Negara.jpg`); Phase 2's approved fetcher should include bounded retry-with-backoff on `RemoteProtocolError`.
- **Future verification items:** Confirm exact Apple Silicon (`arm64`) & Linux container compatibility for Docling (Phase 2b) and PaddleOCR (Phase 2c) before pinning.
