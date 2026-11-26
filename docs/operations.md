# Operations Runbook — PDN Multimodal Knowledge Sync

> Practical, copy-pasteable operational procedures for maintaining freshness, Gate A/B reviews, SQLite online backups, WAL recovery, outage/DLQ replay, and alerts.

---

## 1. Daily & Weekly Operations Quick Reference

| Task | Command | Notes |
|---|---|---|
| Apply DB migrations | `uv run pdn-sync db upgrade` | Safe idempotent Alembic upgrade to head (`0005`). |
| Validate Gate A scope | `uv run pdn-sync scope validate` | Fails closed on malformed YAML or broad approval wildcards. |
| Start read-only API | `uv run pdn-sync api serve` | Binds `127.0.0.1:8000` (`GET /v1/*`, `/healthz`, `/metrics`). |
| Start scheduled worker | `uv run pdn-sync worker start --live` | Runs `homepage` (4h), `calendar` (6h), `site` (03:00 MYT), `discovery_audit` (Sun 02:00 MYT). |
| Run one profile on demand | `uv run pdn-sync worker run-once calendar --live` | Profiles: `homepage`, `calendar`, `site`, `discovery_audit`, `publish_sync`. |
| Check alerts (cron/CI) | `uv run pdn-sync ops check-alerts --fail-on critical` | Exits `0` when healthy, `2` on critical alerts (use `--json` for scripts). |
| Online SQLite backup | `uv run pdn-sync ops backup` | Uses `sqlite3.Connection.backup()` + SHA-256 manifest + integrity check. |
| Reconcile FTS5 / Outbox / Qdrant | `uv run pdn-sync ops reconcile` | Supports `--fts-only`, `--outbox-only`, `--qdrant-only`, `--dry-run`. |

> [!IMPORTANT]
> **Embedded Qdrant File-Lock Boundary:** Default `data/qdrant/` uses local embedded Qdrant (zero Docker required), which holds an exclusive OS file lock while open. If you run `pdn-sync worker start` **concurrently** with `pdn-sync api serve`, start the optional Qdrant container (`docker compose --profile qdrant up -d`) and set `PDN_QDRANT_URL=http://127.0.0.1:6333` in `.env`. When running `ops check-alerts` while the API holds an embedded Qdrant lock, pass `--skip-qdrant` (or query `GET http://127.0.0.1:8000/v1/status`).

---

## 2. Alert Triage Table (`GET /v1/status` & `pdn-sync ops check-alerts`)

<a id="alert-triage"></a>

| Alert Code | Severity | Meaning | Immediate Action |
|---|---|---|---|
| `qdrant_unavailable` | `critical` | Qdrant health check failed or embedded lock conflict. | See [Qdrant Outage Recovery](#qdrant-outage-recovery). |
| `dlq_non_empty` | `critical` | One or more `outbox_events` exhausted `max_attempts` and entered `dead_letter`. | See [Dead-Letter Queue (DLQ) Recovery](#dead-letter-queue-dlq-recovery). |
| `fts5_sync_mismatch` | `critical` / `warning` | Row count mismatch between active SQLite tables and FTS5 virtual tables. | Run `uv run pdn-sync ops reconcile --fts-only`. |
| `qdrant_sqlite_drift` | `warning` | Published Qdrant point count differs from active SQLite `knowledge_chunks`. | Run `uv run pdn-sync ops reconcile --outbox-only` then `--qdrant-only`. |
| `approved_unpublished_behind` | `warning` | Gate-B-approved artifacts have not been published yet. | Run `uv run pdn-sync publish --approved-only`. |
| `gate_b_backlog_count` / `gate_b_backlog_age` | `warning` | Pending Gate B review queue exceeds count or age threshold. | See [Gate B Review and Publication Backlog](#gate-b-review-and-publication-backlog). |
| `quarantined_backlog` | `warning` | Blood-stock charts or low-confidence critical-fact posters await manual verification. | Inspect via `pdn-sync stage list --status quarantined`; transcribe via `stage override` if needed. |
| `verification_overdue_<profile>` | `warning` | Last successful sync for `homepage`, `calendar`, `site`, or `discovery_audit` exceeded freshness SLA. | See [Verification Overdue](#verification-overdue). |
| `discovery_anomaly_halted` / `pattern_max_matches_hit` | `critical` / `warning` | Discovery halted due to sudden URL explosion or rotating-asset pattern hit `max_matches`. | See [Discovery Anomaly or Pattern Cap](#discovery-anomaly-or-pattern-cap). |
| `crawl_scope_invalid` | `critical` | `config/crawl_scope.yaml` failed strict schema validation (fail-closed). | Run `uv run pdn-sync scope validate` and fix the reported YAML error. |

---

## 3. Runbooks

### 3.1 SQLite Online Backup, Verification & Restore

**Never copy a live `.db` file with `cp` or `shutil.copy` while WAL mode is active.** Always use `pdn-sync ops backup`, which runs Python's `sqlite3.Connection.backup()` online backup API, converts the backup snapshot to standalone `DELETE` journal mode, verifies `PRAGMA integrity_check` and `PRAGMA foreign_key_check`, and writes a SHA-256 `manifest.json`.

```bash
# 1. Create verified online backup (stored under gitignored data/backups/)
uv run pdn-sync ops backup
# Optional: also snapshot embedded data/qdrant/ (stop API/worker first if using embedded Qdrant)
uv run pdn-sync ops backup --include-qdrant

# 2. Verify any backup directory independently
uv run pdn-sync ops verify-backup data/backups/backup_<TIMESTAMP>

# 3. Restore (stop API & worker first; --confirm is mandatory)
uv run pdn-sync ops restore data/backups/backup_<TIMESTAMP> --confirm
# Optional: restore Qdrant snapshot too
uv run pdn-sync ops restore data/backups/backup_<TIMESTAMP> --confirm --restore-qdrant
```

`ops restore` verifies the backup's SHA-256 checksums, `PRAGMA integrity_check`, and `PRAGMA foreign_key_check` **before** touching the target database, copies via the SQLite Backup API to a temporary sibling file, removes stale `-wal`/`-shm` sidecars, atomically swaps the file with `os.replace()`, and re-verifies the restored database.

### 3.2 WAL Recovery & Lock Contention

- All connections enforce `PRAGMA foreign_keys=ON`, `PRAGMA journal_mode=WAL`, and `PRAGMA busy_timeout=5000`.
- If a process crashed mid-write and left a large `data/pdn_sync.db-wal`:
  1. Stop `pdn-sync api serve` and `pdn-sync worker start`.
  2. Run a checkpoint and integrity check:
     ```bash
     uv run python -c "
     import sqlite3
     c = sqlite3.connect('data/pdn_sync.db')
     print('checkpoint:', c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall())
     print('integrity:', c.execute('PRAGMA integrity_check').fetchall())
     print('fk_check:', c.execute('PRAGMA foreign_key_check').fetchall())
     c.close()
     "
     ```
  3. If `integrity_check` is not `ok`, immediately restore from the latest verified backup in `data/backups/` (Section 3.1).

<a id="qdrant-outage-recovery"></a>
### 3.3 Qdrant Outage Recovery

- During a Qdrant outage, `PublicationService` retries up to `retry.max_attempts` with exponential backoff and then persists an idempotent `outbox_events` record (`qdrant_upsert_publication` / `qdrant_retire_publication`). **SQLite published tables, donation schedules, and previously indexed Qdrant vectors are never corrupted or deleted.**
- Once Qdrant is reachable again (or Docker `compose --profile qdrant` is restarted):
  ```bash
  uv run pdn-sync ops reconcile --outbox-only
  uv run pdn-sync ops reconcile --qdrant-only
  uv run pdn-sync ops check-alerts
  ```

<a id="dead-letter-queue-dlq-recovery"></a>
### 3.4 Dead-Letter Queue (DLQ) Recovery

```bash
# 1. Inspect dead-lettered events and last_error details
uv run pdn-sync ops dlq list --status dead_letter

# 2. Fix underlying root cause (e.g., Qdrant disk/network or corrupt artifact)

# 3a. Requeue for automatic replay (resets status=pending, attempt_count=0):
uv run pdn-sync ops dlq resolve obx_<EVENT_ID> --reason "root_cause_fixed" --requeue
uv run pdn-sync ops reconcile --outbox-only

# 3b. Or close permanently if superseded/obsolete (recorded with reviewer + audit note):
uv run pdn-sync ops dlq resolve obx_<EVENT_ID> --reason "superseded_by_newer_version"
```

<a id="fts5-and-qdrant-drift-reconciliation"></a>
### 3.5 FTS5 & Qdrant Drift Reconciliation

```bash
# Dry-run inspection (zero writes)
uv run pdn-sync ops reconcile --dry-run

# Rebuild all SQLite FTS5 indexes strictly from active published rows
uv run pdn-sync ops reconcile --fts-only

# Replay outbox + repair unsynced Qdrant chunks or retired statuses
uv run pdn-sync ops reconcile
```

<a id="gate-b-review-and-publication-backlog"></a>
### 3.6 Gate B Review and Publication Backlog

```bash
# Inspect pending & quarantined review queues
uv run pdn-sync stage list --status pending_review
uv run pdn-sync stage list --status quarantined

# Inspect individual artifact JSON / Markdown or diff against previous version
uv run pdn-sync stage show <ARTIFACT_ID>
uv run pdn-sync stage diff <ARTIFACT_ID>

# Safe batch-approve non-critical valid items (always dry-run first!)
uv run pdn-sync stage approve --all-valid --dry-run
uv run pdn-sync stage approve --all-valid --notes "owner verified"

# Manual review for critical-fact / quarantined items (or human transcription override)
uv run pdn-sync stage override <ARTIFACT_ID> --text-file <TRANSCRIPTION.md> --reason "manual_transcription"
uv run pdn-sync stage approve <ARTIFACT_ID> --notes "verified against source poster"

# Publish approved items (idempotent; skips already-published artifacts)
uv run pdn-sync publish --approved-only
```

<a id="verification-overdue"></a>
### 3.7 Verification Overdue

When `verification_overdue_<profile>` fires:
1. Check `PDN_LIVE_CRAWL_ENABLED=true`, `PDN_SOURCE_POLICY_ACKNOWLEDGED=true`, and `PDN_CONTACT_EMAIL` in `.env` via `uv run pdn-sync check-policy`.
2. Confirm `/v2/robots.txt` is reachable and no `403`/`429` blocks occurred in `task_attempts` or `sync_runs`.
3. Run the overdue profile manually:
   ```bash
   uv run pdn-sync worker run-once homepage --live
   uv run pdn-sync worker run-once calendar --live
   uv run pdn-sync worker run-once site --live
   uv run pdn-sync worker run-once discovery_audit --live
   ```

<a id="discovery-anomaly-or-pattern-cap"></a>
### 3.8 Discovery Anomaly or Pattern Cap (`max_matches`)

- If `discovery_anomaly_halted` fires, inspect `data/discovery/discovery_report.md` and `url_inventory.csv` to see whether PDN added a new section or an accidental URL trap (e.g., query-string calendar loop). Add an exclusion pattern via `pdn-sync scope exclude --pattern ... --id ...` before re-running discovery.
- If `pattern_max_matches_hit` fires, inspect newly matched URLs (`pdn-sync inventory list --disposition pending`), retire old assets if appropriate, or raise `max_matches` explicitly in `config/crawl_scope.yaml`.

<a id="scope-validation-errors"></a>
### 3.9 Scope Validation Errors

Run `uv run pdn-sync scope validate` to inspect the exact Pydantic error line. Remember: broad wildcards (`/**`) are allowed in `exclusions.patterns` but strictly forbidden in `approvals.*.patterns`.

---

## 4. Structured Logs & Prometheus Metrics

- **Structured JSON Logs (`structlog`):** Configure via `.env`:
  - `PDN_LOG_FORMAT=json` (default; set `console` for local interactive debugging)
  - `PDN_LOG_LEVEL=INFO`
  - Every structured event binds correlation context (`run_id`, `resource_id`, `version_id`, `task_id`, `correlation_id`) and automatically redacts secret/PII keys (`password`, `token`, `authorization`, `api_key`, `cookie`, `raw_html`, `raw_body`).
- **Prometheus Metrics (`GET http://127.0.0.1:8000/metrics`):**
  - Exposes `pdn_sync_published_documents`, `pdn_sync_published_chunks`, `pdn_sync_published_tables`, `pdn_sync_published_donation_events`, `pdn_sync_published_donation_sessions`, `pdn_sync_qdrant_points`, `pdn_sync_qdrant_drift_abs`, `pdn_sync_pending_gate_b`, `pdn_sync_quarantined_gate_b`, `pdn_sync_pending_gate_b_oldest_age_seconds`, `pdn_sync_approved_unpublished_artifacts`, `pdn_sync_fts5_mismatch_total{index}`, `pdn_sync_outbox_events{status}`, `pdn_sync_dlq_items`, `pdn_sync_active_alerts{severity}`, `pdn_sync_last_success_timestamp_seconds{profile}`, `pdn_sync_scope_exact_urls{disposition}`, `pdn_sync_api_requests_total{endpoint,status_code}`, and `pdn_sync_api_request_duration_seconds{endpoint}`.

---

## 5. Optional Docker Compose Qdrant Mode

```bash
# Start pinned non-root Qdrant server bound to 127.0.0.1:6333
docker compose --profile qdrant up -d

# Point CLI, worker, and API at remote Qdrant in .env:
# PDN_QDRANT_URL=http://127.0.0.1:6333

# Stop Qdrant container (durable volume qdrant_data is preserved)
docker compose --profile qdrant down
```
