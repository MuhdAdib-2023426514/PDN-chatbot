# Gate A — Crawl Scope Review Guide (`config/crawl_scope.yaml`)

> **Single Source of Truth:** [config/crawl_scope.yaml](../config/crawl_scope.yaml) is the ONLY authority for what may enter Phase 2 content scraping and extraction. Discovery never overwrites it.

## 1. Inspect Discovery Outputs

After running Phase 1 discovery (`uv run pdn-sync discover --live`), review the generated artifacts under `data/discovery/`:

- `data/discovery/discovery_report.md` — High-level counts by `resource_type`, `category`, `disposition`, duplicates, broken `4xx`/`5xx` links, staff directory counts, excluded JEvents/system URLs, and proposed rotating-asset pattern rules.
- `data/discovery/url_inventory.csv` — Spreadsheet-friendly table of every discovered page, poster/banner, and document (with `recommended_action`, `duplicate_of`, `http_status`, `content_length_bytes`, and a blank `notes` column for bulk review).
- `data/discovery/url_inventory.json` — Machine-readable inventory envelope (`schema_version`, `run_id`, `scope_file_hash`, `bounds`, `summary_counts`, `items[]`).
- `data/discovery/crawl_scope.proposed.yaml` — Candidate exact URLs and bounded single-directory rotating-asset patterns proposed for review.

You can also filter the latest inventory from the terminal:

```bash
uv run pdn-sync inventory list --category blood_stock_graphic
uv run pdn-sync inventory list --category poster_banner
uv run pdn-sync inventory list --resource-type document
uv run pdn-sync inventory list --category staff_directory
uv run pdn-sync inventory list --duplicates-only
uv run pdn-sync inventory list --broken-only
```

## 2. Review Guidelines by Category

- **Core informational pages (`homepage`, `corporate`, `services`, `faq`, `publications`, `contact_static_centers`):** Review non-duplicate `200 OK` pages (`duplicate_of` blank) and approve those relevant to chatbot Q&A. Skip alias rows where `duplicate_of` is populated so each K2 article is approved once.
- **Rotating posters & blood-stock graphics (`blood_stock_graphic`, `poster_banner`):** Filenames rotate monthly/per announcement (e.g., `/v2/images/stokdarah/*.png`, `/v2/images/SmartSlider3/2025/*.png`, `/v2/images/it_pdn/2026/*.jpg`). Prefer approving the **bounded single-directory pattern rule** from `crawl_scope.proposed.yaml` using `--pattern-id`:
  ```bash
  uv run pdn-sync scope approve --pattern-id <pattern-id>
  ```
  Blood-stock patterns use `supersession: latest_wins` and always quarantine at Gate B for human verification.
- **Documents (`document_pdf`, `document_office`):** Check `content_length_bytes` (must be under `50 MiB`) and review legacy binary `.doc` files explicitly (exclude unless needed and approved for local LibreOffice conversion in Phase 2b).
- **Staff directory (`staff_directory`, PDPA):** Defaults to `recommended_exclude`. Keep excluded unless the chatbot specifically requires official directory lookup — and if approved, document the purpose in [docs/source-policy.md](source-policy.md) first (photos are never captured, and staff pages are never added to `tests/fixtures/`).
- **JEvents (`jevents_deferred`):** Hard-excluded throughout Phases 0–3; deferred to Phase 4's structured SQLite + FTS5 schedule track. Any attempt to approve a JEvents URL in Phases 0–3 is rejected by code.

## 3. Apply Gate A Decisions Safely

### Option A — CLI Commands (Single URLs & Proposed Patterns)

```bash
# Approve an individual page, poster, or document
uv run pdn-sync scope approve \
  --url "https://pdn.gov.my/v2/" \
  --resource-type page \
  --check-interval 3h \
  --reason "Approved PDN homepage"

# Promote a proposed rotating-asset pattern rule
uv run pdn-sync scope approve --pattern-id pattern-blood-stock-graphic-images-stokdarah-png

# Exclude a URL or directory pattern (broad /** wildcards are allowed in exclusions only)
uv run pdn-sync scope exclude \
  --id exclude-staff-directory \
  --pattern "/v2/index.php/hubungi-kami/direktori-pegawai/**" \
  --reason "PDPA staff directory excluded at Gate A"
```

### Option B — Bulk CSV Review & Import

1. Open `data/discovery/url_inventory.csv`, change `disposition` from `pending` to `approved` or `excluded` on the rows you want to decide, and optionally add comments in `notes`.
2. Preview the exact diff (validates every row without touching `config/crawl_scope.yaml`):
   ```bash
   uv run pdn-sync scope import data/discovery/url_inventory.csv --dry-run
   ```
3. Apply the additive merge (preserves YAML comments and logs every decision to SQLite `scope_decisions`):
   ```bash
   uv run pdn-sync scope import data/discovery/url_inventory.csv
   ```

## 4. Validate & Refresh Views

```bash
uv run pdn-sync scope validate
uv run pdn-sync scope export
```

Rule precedence is always: **explicit URL exclusion → pattern exclusion → explicit URL approval → bounded pattern approval → `pending`**. Broad cross-directory wildcards (`/**`) are rejected in approvals by `scope validate`.
