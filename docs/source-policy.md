# Source Access Policy — Pusat Darah Negara (`https://pdn.gov.my/v2/`)

> **Last verified:** 2026-11-24
> **Primary operator handle:** `project-owner`
> **Live crawl clearance status:** **CLEARED (Owner acknowledged 2026-11-24)** — all 5 clearance criteria satisfied; runtime live requests remain gated behind explicit `PDN_LIVE_CRAWL_ENABLED=true` and live `/v2/robots.txt` check at run start.

## 1. Target Source & Scope Boundary

- **Authorized host:** `pdn.gov.my`
- **Authorized path prefix:** `/v2` (`https://pdn.gov.my/v2/`)
- **Off-domain links, protocol handlers, local files, loopback/private IPs:** strictly prohibited (enforced in code).
- **Phase 0–3 hard JEvents exclusion:** all JEvents endpoints (`/v2/index.php/component/jevents/**`, `/v2/component/jevents/**`, and `?option=com_jevents*`) are excluded during Phases 0–3 and deferred to Phase 4 (structured schedule track). Encountered links are logged as `excluded` (`deferred_to_structured_schedule_phase`) without fetching or traversing.

## 2. Live Verification Evidence (2026-11-24)

Minimal, polite, read-only verification performed on `2026-11-24` using `1` concurrent request, `2.5s` inter-request delay, `20s` timeout, and identifiable `User-Agent: PDN-Knowledge-Sync/0.1 (+contact: <PDN_CONTACT_EMAIL>)` (no crawling, no page bodies saved):

| URL | HTTP Status | Content-Type | `X-Robots-Tag` | Notes |
|---|---:|---|---|---|
| `https://pdn.gov.my/robots.txt` | `403` | `text/html; charset=iso-8859-1` | None | Root `robots.txt` returns `403`; do not interpret as permission to ignore subfolder rules. |
| `https://pdn.gov.my/v2/robots.txt` | `200` | `text/plain` | None | Standard Joomla subfolder `robots.txt` (see exact rules below). |
| `https://pdn.gov.my/v2/sitemap.xml` | `404` | `text/html; charset=iso-8859-1` | None | No XML sitemap at `/v2/sitemap.xml`; discovery must seed from `/v2/` and navigation menus. |
| `https://pdn.gov.my/v2/` | `200` | `text/html; charset=utf-8` | None | Publicly reachable without login or CAPTCHA. No `<meta name="robots">` tag detected. |

### Observed `/v2/robots.txt` Rules

```text
User-agent: *
Disallow: /administrator/
Disallow: /bin/
Disallow: /cache/
Disallow: /cli/
Disallow: /components/
Disallow: /includes/
Disallow: /installation/
Disallow: /language/
Disallow: /layouts/
Disallow: /libraries/
Disallow: /logs/
Disallow: /modules/
Disallow: /plugins/
Disallow: /tmp/
```

As documented in Joomla's header comment in `/v2/robots.txt`, all relative `Disallow` rules are scoped under the `/v2/` subfolder prefix (e.g., `/v2/administrator/`, `/v2/bin/`, etc.) and enforced by `src/pdn_sync/crawling/robots.py` as well as explicit exclusions in `config/crawl_scope.yaml`.

### Site Terms, Notices & Robots Directives Check

- Checked HTTP response headers (`X-Robots-Tag`) across all four probed URLs: **none present**.
- Checked `<meta name="robots">` tags on `https://pdn.gov.my/v2/`: **none present**.
- Scanned homepage `<a>` links for terms of use, disclaimer, privacy policy, or anti-automation notices (`penafian`, `privasi`, `dasar`, `keselamatan`, `terms`, `privacy`, `disclaimer`, `policy`, `hakcipta`, `copyright`): no site-terms or automated-access prohibition links were found on `/v2/` (the only match was a clinical PDF link, *National Policy Transfusion Medicine Services In Malaysia*).

## 3. Politeness, Identification & Safety Guardrails

Enforced in code (`src/pdn_sync/config.py`, `src/pdn_sync/crawling/`):

- **User-Agent:** `PDN-Knowledge-Sync/0.1 (+contact: <PDN_CONTACT_EMAIL>)`, loaded from `.env`. Live requests **fail closed** if `PDN_CONTACT_EMAIL` is missing, blank, or a placeholder.
- **Concurrency:** `1` concurrent request per host.
- **Request delay:** `2.0` to `5.0` seconds with jitter between requests.
- **Timeouts & byte cap:** `20s` per-request timeout; `50 MiB` (`52,428,800` bytes) maximum response/document size.
- **Backoff & stop signals:** HTTP `403`/`429` treated as stop/backoff signals; honor `Retry-After` headers; never bypass login, CAPTCHA, or access controls.
- **SSRF & redirect safety:** only `http`/`https` to `pdn.gov.my` under `/v2`; block loopback, private, link-local, multicast, and reserved IPs; re-validate scope and host after redirects.

## 4. Privacy & PDPA Guidance (Malaysia Personal Data Protection Act)

- Do not collect donor identities, form submissions, authentication data, or unrelated personal photographs.
- Staff directory pages (`/v2/index.php/hubungi-kami/direktori-pegawai/**`) are classified as `staff_directory` with default `recommended_action: recommended_exclude`.
- **Gate A Owner Approval Record (2026-11-24):** On `2026-11-24`, `project-owner` explicitly approved all 4 official PDN staff directory sections (`31-direktori-pengurusan-tertinggi`, `8-direktori-pegawai`, `29-direktori-ketua-seksyen`, `30-direktori-ketua-unit` plus the root directory index) in `config/crawl_scope.yaml`.
  - **Documented purpose:** Enable the PDN chatbot to answer public inquiries about official PDN leadership, division/section/unit heads, and official office contact channels.
  - **Strict PDPA minimization safeguards (enforced in Phase 2+):**
    1. Extract and retain **only** official directory fields needed by the chatbot: officer name, official designation, division/section/unit, and official office contact (office phone/email/extension).
    2. **Never** download, store, OCR, or index officer profile photographs by default.
    3. **Never** capture `staff_directory` pages into `tests/fixtures/` (enforced by `pdn-sync fixtures capture`).
    4. Keep officer records auditable and subject to retirement if removed from the live PDN directory.

## 5. Source-Access Clearance Checklist (Rule 1)

Live crawling (including Phase 1 live discovery) is **cleared only when all 5 criteria pass**:

- [x] **1. `/v2/robots.txt` fetched & respected:** Verified HTTP `200` on `2026-11-24`; all disallowed paths scoped under `/v2/` and enforced in code and `config/crawl_scope.yaml`.
- [x] **2. Publicly reachable without access controls:** Verified `https://pdn.gov.my/v2/` returns HTTP `200` with no login wall, CAPTCHA, or anti-bot challenge.
- [x] **3. No prohibiting terms or robots directives:** Verified no `X-Robots-Tag`, no `<meta name="robots">`, and no published anti-automation terms on `https://pdn.gov.my/v2/` as of `2026-11-24`.
- [x] **4. Politeness limits & identifiable `User-Agent` enforced in code:** Verified in `src/pdn_sync/config.py` and `src/pdn_sync/crawling/clearance.py` with unit tests.
- [x] **5. Human owner acknowledgement:** Confirmed by `project-owner` on `2026-11-24` (`PDN_SOURCE_POLICY_ACKNOWLEDGED=true` verified in `.env` via `uv run pdn-sync check-policy`).

## 6. Automatic Disable Conditions

Live crawling must be **automatically disabled** and reviewed if any of the following occur:

1. `/v2/robots.txt` becomes unreachable (`403`, `5xx`, timeout) or disallows previously targeted paths.
2. Repeated `403` or `429` responses or a new CAPTCHA/login gate appears.
3. Published site terms, `X-Robots-Tag`, or `meta robots` directives change to prohibit automated access.
4. `PDN_CONTACT_EMAIL` is missing/invalid or `config/crawl_scope.yaml` fails schema validation.
