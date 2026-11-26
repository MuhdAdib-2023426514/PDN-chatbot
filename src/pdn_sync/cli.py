"""Command-line interface for PDN Multimodal Knowledge Sync (Phases 0-2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from pdn_sync.config import (
    AppSettings,
    ConfigurationError,
    load_crawl_scope,
    load_ingestion_policy,
)
from pdn_sync.crawling.clearance import (
    LiveCrawlBlockedError,
    SourceAccessEvidence,
    evaluate_source_clearance,
)
from pdn_sync.crawling.robots import RobotsUnavailableError
from pdn_sync.discovery.crawler import DEFAULT_SEED_URL, DiscoveryOptions, run_discovery
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.discovery.scope_manager import (
    GateABlockedError,
    ScopeManager,
    ScopeMutationError,
)
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.extractors.contracts import ReviewStatus
from pdn_sync.indexing.embedder import create_embedder
from pdn_sync.indexing.eval_runner import (
    benchmark_embedding_models,
    evaluate_retrieval,
    load_eval_suite,
)
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.indexing.qdrant_store import QdrantVectorStore
from pdn_sync.ops.reconcile import reconcile_fts5, reconcile_outbox, reconcile_qdrant
from pdn_sync.persistence.backup import (
    BackupVerificationError,
    create_online_backup,
    restore_online_backup,
    verify_backup,
)
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.publication_repo import PublicationRepository
from pdn_sync.persistence.repository import ControlPlaneRepository, InventoryFilter
from pdn_sync.persistence.staging_repo import GateBPublishBlockedError, StageQueueFilter
from pdn_sync.scheduler.worker import WorkerProfile, run_profile_once, start_worker
from pdn_sync.search.hybrid import ChunkBM25SearchService
from pdn_sync.search.tables import StructuredTableSearchService
from pdn_sync.staging.calendar_sync import CalendarSyncOptions, run_calendar_sync_pipeline
from pdn_sync.staging.fixtures import FixtureCaptureBlockedError, capture_safe_fixture
from pdn_sync.staging.gate_b import (
    ExtractionOptions,
    GateBService,
    run_extraction_pipeline,
)
from pdn_sync.telemetry.alerts import evaluate_alerts

app = typer.Typer(
    name="pdn-sync",
    help="Two-gate multimodal knowledge ingestion pipeline for Pusat Darah Negara (PDN).",
    no_args_is_help=True,
)
db_app = typer.Typer(help="SQLite control-plane migration commands.")
inventory_app = typer.Typer(help="Inspect discovered URL and asset inventories.")
scope_app = typer.Typer(help="Gate A crawl-scope validation and management commands.")
stage_app = typer.Typer(help="Gate B staged extraction review, diff, approval, and override.")
fixtures_app = typer.Typer(help="Safe fixture capture commands (Gate-A & PDPA enforced).")
api_app = typer.Typer(help="Read-only chatbot HTTP API commands (Phase 3+).")
worker_app = typer.Typer(help="Single-process scheduled worker commands (ADR-orchestration.md).")
eval_app = typer.Typer(help="Retrieval evaluation and multilingual embedding benchmarks.")
calendar_app = typer.Typer(help="Phase 4 bounded JEvents blood donation schedule commands.")
ops_app = typer.Typer(
    help="Phase 5 reliability & operations (backup, restore, alerts, reconcile, DLQ)."
)
dlq_app = typer.Typer(help="Inspect and resolve outbox / dead-letter queue events.")
ops_app.add_typer(dlq_app, name="dlq")

app.add_typer(db_app, name="db")
app.add_typer(inventory_app, name="inventory")
app.add_typer(scope_app, name="scope")
app.add_typer(stage_app, name="stage")
app.add_typer(fixtures_app, name="fixtures")
app.add_typer(api_app, name="api")
app.add_typer(worker_app, name="worker")
app.add_typer(eval_app, name="eval")
app.add_typer(calendar_app, name="calendar")
app.add_typer(ops_app, name="ops")


@db_app.command("upgrade")
def db_upgrade(
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite database file"),
    ] = None,
) -> None:
    """Apply Alembic migrations to head."""
    settings = AppSettings()
    target = sqlite_path or settings.sqlite_path
    upgrade_database(target)
    typer.secho(f"OK: SQLite database migrated to head at {target}", fg=typer.colors.GREEN)


@app.command("discover")
def discover(
    live: Annotated[
        bool,
        typer.Option("--live", help="Run live discovery against https://pdn.gov.my/v2/"),
    ] = False,
    seed_url: Annotated[
        str,
        typer.Option("--seed-url", help="Seed URL for bounded discovery"),
    ] = DEFAULT_SEED_URL,
    no_head: Annotated[
        bool,
        typer.Option("--no-head", help="Skip selective polite HEAD probes on candidate assets"),
    ] = False,
    max_pages: Annotated[
        int | None,
        typer.Option("--max-pages", help="Override max HTML pages per discovery run"),
    ] = None,
    max_depth: Annotated[
        int | None,
        typer.Option("--max-depth", help="Override max BFS depth from seed URL"),
    ] = None,
    max_listing_pages: Annotated[
        int | None,
        typer.Option("--max-listing-pages", help="Override max paginated listing pages"),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Directory for discovery inventory outputs"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Path to config/ingestion_policy.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
) -> None:
    """Run Phase 1 discovery-only scan (JEvents excluded; never edits crawl_scope.yaml)."""
    if not live:
        typer.secho(
            "Refusing live network scan without --live flag (safe default). "
            "Pass --live with PDN_LIVE_CRAWL_ENABLED=true when ready.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = AppSettings()
    opts = DiscoveryOptions(
        seed_url=seed_url,
        live=True,
        enable_head=not no_head,
        max_pages=max_pages,
        max_depth=max_depth,
        max_listing_pages=max_listing_pages,
        output_dir=output_dir,
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
    )
    try:
        result = run_discovery(
            options=opts,
            settings=settings,
            progress_cb=lambda msg: typer.echo(f"[discover] {msg}"),
        )
    except (ConfigurationError, LiveCrawlBlockedError, RobotsUnavailableError) as exc:
        typer.secho(f"DISCOVERY BLOCKED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"Discovery {result.status}: run_id={result.run_id} items={len(result.items)} "
        f"proposed_patterns={len(result.proposed_patterns)}",
        fg=typer.colors.GREEN if result.status == "completed" else typer.colors.YELLOW,
    )
    for label, path in result.exported_files.items():
        typer.echo(f"  {label}: {path}")


@app.command("extract")
def extract(
    live: Annotated[
        bool,
        typer.Option("--live", help="Fetch Gate-A-approved resources live and stage for Gate B"),
    ] = False,
    pilot: Annotated[
        bool,
        typer.Option(
            "--pilot", help="Run representative pilot batch (~25 pages, ~6 PDFs, ~14 posters)"
        ),
    ] = False,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Skip resources that already have a staged artifact"),
    ] = False,
    resource_type: Annotated[
        ResourceType | None,
        typer.Option("--resource-type", help="Filter approved targets by resource_type"),
    ] = None,
    category: Annotated[
        DiscoveryCategory | None,
        typer.Option("--category", help="Filter approved targets by category"),
    ] = None,
    url: Annotated[
        list[str] | None,
        typer.Option("--url", help="Explicit Gate-A-approved URL(s) to extract"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum approved targets to process"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Path to config/ingestion_policy.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
) -> None:
    """Fetch Gate-A-approved pages, PDFs, and posters into BlobStore and Gate B staging."""
    if not live:
        typer.secho(
            "Refusing live extraction without --live flag (safe default). "
            "Pass --live with PDN_LIVE_CRAWL_ENABLED=true when ready.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = AppSettings()
    opts = ExtractionOptions(
        live=True,
        pilot=pilot,
        resume=resume,
        urls=list(url or []),
        resource_type=resource_type,
        category=category,
        limit=limit,
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
    )
    try:
        summary = run_extraction_pipeline(
            options=opts,
            settings=settings,
            progress_cb=lambda msg: typer.echo(f"[extract] {msg}"),
        )
    except (
        ConfigurationError,
        LiveCrawlBlockedError,
        RobotsUnavailableError,
        GateABlockedError,
    ) as exc:
        typer.secho(f"EXTRACTION BLOCKED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"Extraction complete: run_id={summary.run_id} skipped_resumed={summary.skipped_resumed} "
        f"fetched={summary.fetched} not_modified={summary.not_modified} "
        f"unchanged_blob={summary.unchanged_blob} failed={summary.failed} "
        f"pending={summary.staged_pending} quarantined={summary.staged_quarantined} "
        f"carried_forward={summary.carried_forward}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  benchmark: {json.dumps(summary.benchmark)}")


@inventory_app.command("list")
def inventory_list(
    resource_type: Annotated[
        ResourceType | None,
        typer.Option("--resource-type", help="Filter by resource_type"),
    ] = None,
    category: Annotated[
        DiscoveryCategory | None,
        typer.Option("--category", help="Filter by fixed category"),
    ] = None,
    disposition: Annotated[
        Disposition | None,
        typer.Option("--disposition", help="Filter by disposition"),
    ] = None,
    duplicates_only: Annotated[
        bool,
        typer.Option("--duplicates-only", help="Show only duplicate routes"),
    ] = False,
    broken_only: Annotated[
        bool,
        typer.Option("--broken-only", help="Show only broken links"),
    ] = False,
    search: Annotated[
        str | None,
        typer.Option("--search", help="Substring search on URL or title/alt"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum rows to display"),
    ] = 50,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
) -> None:
    """List and filter items from the latest discovery inventory."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        repo = ControlPlaneRepository(engine)
        run_row, items = repo.list_inventory_items(
            flt=InventoryFilter(
                resource_type=resource_type,
                category=category,
                disposition=disposition,
                duplicates_only=duplicates_only,
                broken_only=broken_only,
                search=search,
                limit=limit,
            )
        )
    finally:
        engine.dispose()

    if run_row is None:
        typer.secho("No discovery runs recorded yet.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    typer.echo(
        f"Latest run: {run_row.run_id} (status={run_row.status}) — showing {len(items)} items"
    )
    for item in items:
        dup_flag = f" dup_of={item.duplicate_of}" if item.duplicate_of else ""
        typer.echo(
            f"[{item.disposition.value:8s}] [{item.resource_type.value:12s}] "
            f"[{item.category.value:22s}] [{item.link_status.value:16s}] {item.url}{dup_flag}"
        )


def _make_gate_b_service(
    *,
    scope_file: Path | None,
    policy_file: Path | None,
    sqlite_path: Path | None,
    data_dir: Path | None,
) -> tuple[AppSettings, GateBService]:
    settings = AppSettings()
    svc = GateBService(
        sqlite_path=sqlite_path or settings.sqlite_path,
        policy_path=policy_file or settings.ingestion_policy_file,
        scope_path=scope_file or settings.scope_file,
        data_dir=data_dir or settings.data_dir,
    )
    return settings, svc


@stage_app.command("list")
def stage_list(
    status: Annotated[
        ReviewStatus | None,
        typer.Option("--status", help="Filter by review_status"),
    ] = None,
    category: Annotated[
        str | None,
        typer.Option("--category", help="Filter by category"),
    ] = None,
    resource_type: Annotated[
        ResourceType | None,
        typer.Option("--resource-type", help="Filter by resource_type"),
    ] = None,
    critical_only: Annotated[
        bool,
        typer.Option("--critical-only", help="Show only critical-fact or quarantined items"),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Max staged items to show"),
    ] = 50,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """List staged artifacts ordered by Gate B review priority (critical facts first)."""
    _, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    rows = svc.list_queue(
        StageQueueFilter(
            review_status=status,
            category=category,
            resource_type=resource_type.value if resource_type else None,
            critical_only=critical_only,
            limit=limit,
        )
    )
    if not rows:
        typer.secho("No staged artifacts match the filter.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    typer.echo(f"Showing {len(rows)} staged artifacts (priority ordered):")
    for r in rows:
        flags = json.loads(r.critical_fact_flags_json)
        flag_str = f" flags={','.join(flags)}" if flags else ""
        typer.echo(
            f"{r.artifact_id} [{r.review_status:14s}] [{r.validation_status:12s}] "
            f"[{r.resource_type:12s}] [{r.category:20s}] {r.source_url}{flag_str}"
        )


@stage_app.command("show")
def stage_show(
    artifact_id: Annotated[str, typer.Argument(help="Staged artifact ID")],
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Display the Markdown staging view of a single artifact."""
    _, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    try:
        _, _, md = svc.show_artifact(artifact_id)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(md)


@stage_app.command("diff")
def stage_diff(
    artifact_id: Annotated[str, typer.Argument(help="Staged artifact ID")],
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Show unified diff between staged artifact and previously approved version."""
    _, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    try:
        diff_text = svc.diff_artifact(artifact_id)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(diff_text)


@stage_app.command("approve")
def stage_approve(
    artifact_id: Annotated[str | None, typer.Argument(help="Single artifact ID to approve")] = None,
    all_valid: Annotated[
        bool,
        typer.Option("--all-valid", help="Batch-approve valid non-critical pending artifacts"),
    ] = False,
    category: Annotated[str | None, typer.Option("--category")] = None,
    resource_type: Annotated[ResourceType | None, typer.Option("--resource-type")] = None,
    pattern_id: Annotated[str | None, typer.Option("--pattern-id")] = None,
    source_prefix: Annotated[str | None, typer.Option("--source-prefix")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview batch approval without writing decisions"),
    ] = False,
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Approve a single staged artifact or safely batch-approve valid non-critical artifacts."""
    settings, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    actor = reviewer or settings.operator_id

    if artifact_id is not None:
        try:
            row = svc.approve_single(artifact_id=artifact_id, reviewer=actor, notes=notes)
        except ValueError as exc:
            typer.secho(f"APPROVE FAILED: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        typer.secho(
            f"Approved {row.artifact_id} (semantic_hash={row.semantic_hash[:12]}...)",
            fg=typer.colors.GREEN,
        )
        return

    if not any([all_valid, category, resource_type, pattern_id, source_prefix]):
        typer.secho(
            "Provide an artifact_id or batch filter (--all-valid / --category / --resource-type).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    summary = svc.batch_approve(
        flt=StageQueueFilter(
            category=category,
            resource_type=resource_type.value if resource_type else None,
            pattern_id=pattern_id,
            source_prefix=source_prefix,
            limit=0,
        ),
        reviewer=actor,
        dry_run=dry_run,
        notes=notes,
    )
    mode = "DRY-RUN" if dry_run else "APPLIED"
    typer.secho(
        f"[{mode}] approved={len(summary.approved_ids)} "
        f"excluded_critical_or_invalid={len(summary.excluded_critical_or_invalid)}",
        fg=typer.colors.GREEN,
    )
    for aid in summary.approved_ids:
        typer.echo(f"  + approve : {aid}")
    for aid in summary.excluded_critical_or_invalid:
        typer.echo(f"  ! skipped (critical/quarantined/invalid): {aid}")


@stage_app.command("reject")
def stage_reject(
    artifact_id: Annotated[str, typer.Argument(help="Staged artifact ID to reject")],
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Reject a staged artifact at Gate B."""
    settings, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    try:
        row = svc.reject(
            artifact_id=artifact_id,
            reviewer=reviewer or settings.operator_id,
            notes=notes,
        )
    except ValueError as exc:
        typer.secho(f"REJECT FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(f"Rejected {row.artifact_id}", fg=typer.colors.YELLOW)


@stage_app.command("override")
def stage_override(
    artifact_id: Annotated[str, typer.Argument(help="Quarantined/staged artifact ID")],
    file: Annotated[Path, typer.Option("--file", help="Markdown or JSON human transcription")],
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    notes: Annotated[str | None, typer.Option("--notes")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Create a new audited human_transcription staged version linked to the same raw blob."""
    settings, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    try:
        new_row = svc.override(
            artifact_id=artifact_id,
            override_file=file,
            reviewer=reviewer or settings.operator_id,
            notes=notes,
        )
    except ValueError as exc:
        typer.secho(f"OVERRIDE FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(
        f"Created human_transcription version {new_row.artifact_id} "
        f"(semantic_hash={new_row.semantic_hash[:12]}..., status={new_row.review_status}). "
        "Approve explicitly with `pdn-sync stage approve`.",
        fg=typer.colors.GREEN,
    )


@app.command("reextract")
def reextract(
    extractor: Annotated[
        str,
        typer.Option("--extractor", help="Extractor family to re-run: html, pdf, or ocr"),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report carry-forward vs changed counts without writing"),
    ] = False,
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Re-run an extractor offline against stored blobs with semantic_hash carry-forward."""
    settings, svc = _make_gate_b_service(
        scope_file=scope_file,
        policy_file=policy_file,
        sqlite_path=sqlite_path,
        data_dir=data_dir,
    )
    try:
        summary = svc.reextract(
            extractor=extractor,
            reviewer=reviewer or settings.operator_id,
            dry_run=dry_run,
        )
    except ValueError as exc:
        typer.secho(f"REEXTRACT FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    mode = "DRY-RUN" if dry_run else "APPLIED"
    typer.secho(
        f"[{mode}] extractor={summary.extractor} total={summary.total_considered} "
        f"carry_forward={summary.unchanged_carry_forward} "
        f"changed_pending={summary.changed_pending_review} "
        f"quarantined={summary.quarantined}",
        fg=typer.colors.GREEN,
    )


@fixtures_app.command("capture")
def fixtures_capture(
    url: Annotated[str, typer.Argument(help="Gate-A-approved, non-PDPA URL to sanitize")],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Target fixtures directory"),
    ] = Path("tests/fixtures"),
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
) -> None:
    """Safely capture a sanitized fixture (refuses unapproved and staff_directory URLs)."""
    try:
        out_path = capture_safe_fixture(
            url,
            scope_file=scope_file,
            policy_file=policy_file,
            fixtures_dir=output_dir,
        )
    except (
        GateABlockedError,
        FixtureCaptureBlockedError,
        ConfigurationError,
        RobotsUnavailableError,
        RuntimeError,
    ) as exc:
        typer.secho(f"FIXTURE CAPTURE BLOCKED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(f"Captured sanitized fixture: {out_path}", fg=typer.colors.GREEN)


@app.command("publish")
def publish(
    approved_only: Annotated[
        bool,
        typer.Option("--approved-only", help="Enforce Gate B approved-only publication check"),
    ] = True,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Verify Gate B publish eligibility without writing"),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Publish Gate-B-approved artifacts to SQLite/FTS5/Qdrant"),
    ] = False,
    reconcile_retirement: Annotated[
        bool,
        typer.Option(
            "--reconcile-retirement",
            help="Also reconcile linked -> no_longer_linked -> retired from latest clean discovery",
        ),
    ] = False,
    embedding_model: Annotated[
        str | None,
        typer.Option("--embedding-model", help="Override embedding model name"),
    ] = None,
    qdrant_collection: Annotated[
        str | None,
        typer.Option("--qdrant-collection", help="Override Qdrant collection name"),
    ] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
) -> None:
    """Verify or execute Gate-B-approved publication into SQLite + FTS5 + Qdrant."""
    if not approved_only:
        typer.secho(
            "Refusing publish without --approved-only (Gate B mandatory).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    settings = AppSettings()
    effective_dry_run = False if execute else dry_run
    pub_svc = PublicationService(
        sqlite_path=sqlite_path or settings.sqlite_path,
        scope_path=scope_file or settings.scope_file,
        policy_path=policy_file or settings.ingestion_policy_file,
        settings=settings,
    )
    try:
        summary = pub_svc.publish_approved(
            dry_run=effective_dry_run,
            embedding_model=embedding_model,
            qdrant_collection=qdrant_collection,
            reconcile_retirement=reconcile_retirement,
            progress_cb=lambda msg: typer.echo(f"[publish] {msg}"),
        )
    except (ConfigurationError, GateBPublishBlockedError, ValueError) as exc:
        typer.secho(f"PUBLISH FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if effective_dry_run:
        typer.secho(
            f"[DRY-RUN] Gate B publish check: eligible_approved={summary.published} "
            f"blocked={summary.blocked}",
            fg=typer.colors.GREEN,
        )
        return

    typer.secho(
        f"[PUBLISHED] model={summary.embedding_model} collection={summary.qdrant_collection} "
        f"published={summary.published} skipped_idempotent={summary.skipped_idempotent} "
        f"chunks={summary.chunks_upserted} tables={summary.tables_published} "
        f"schedule_sessions={summary.schedule_sessions_published} "
        f"superseded={summary.superseded_publications} blocked={summary.blocked}",
        fg=typer.colors.GREEN,
    )


@app.command("status")
def status_cmd(
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Display operational readiness, published counts, and Gate B review backlog."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        pub_repo = PublicationRepository(engine)
        summary = pub_repo.get_status_summary()
    finally:
        engine.dispose()

    with QdrantVectorStore(
        collection_name=settings.qdrant_collection,
        url=settings.qdrant_url,
        path=settings.qdrant_path,
    ) as qdrant:
        qd_ok, qd_points = qdrant.health_check()
        summary["qdrant_mode"] = qdrant.mode
        summary["qdrant_healthy"] = qd_ok
        summary["qdrant_published_points"] = qd_points

    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


@api_app.command("serve")
def api_serve(
    host: Annotated[
        str | None, typer.Option("--host", help="Bind host (default 127.0.0.1)")
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port (default 8000)")] = None,
) -> None:
    """Start the read-only FastAPI chatbot knowledge API."""
    import uvicorn

    from pdn_sync.api.app import create_app

    settings = AppSettings()
    bind_host = host or settings.api_host
    bind_port = port or settings.api_port
    typer.secho(
        f"Starting PDN read-only chatbot API on http://{bind_host}:{bind_port}",
        fg=typer.colors.GREEN,
    )
    uvicorn.run(create_app(settings=settings), host=bind_host, port=bind_port)


@worker_app.command("run-once")
def worker_run_once(
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Profile to run: homepage, calendar, site, discovery_audit, or publish_sync",
        ),
    ],
    live: Annotated[bool, typer.Option("--live", help="Enable live network fetching")] = False,
) -> None:
    """Run one sync profile once in the foreground (framework-agnostic)."""
    valid_profiles: tuple[WorkerProfile, ...] = (
        "homepage",
        "calendar",
        "site",
        "discovery_audit",
        "publish_sync",
    )
    if profile not in valid_profiles:
        typer.secho(
            f"Invalid profile {profile!r}. Choose from: {', '.join(valid_profiles)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = AppSettings()
    profile_map: dict[str, WorkerProfile] = {
        "homepage": "homepage",
        "calendar": "calendar",
        "site": "site",
        "discovery_audit": "discovery_audit",
        "publish_sync": "publish_sync",
    }
    typed_profile = profile_map[profile]
    try:
        out = run_profile_once(
            typed_profile,
            settings=settings,
            live=live,
            progress_cb=lambda msg: typer.echo(f"[worker:{profile}] {msg}"),
        )
    except (ConfigurationError, LiveCrawlBlockedError, RobotsUnavailableError, ValueError) as exc:
        typer.secho(f"WORKER FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@worker_app.command("start")
def worker_start(
    live: Annotated[bool, typer.Option("--live", help="Enable live network fetching")] = False,
) -> None:
    """Start the single-process APScheduler worker (Asia/Kuala_Lumpur, max_instances=1)."""
    settings = AppSettings()
    typer.secho(
        f"Starting PDN single-process scheduled worker (live={live})...",
        fg=typer.colors.GREEN,
    )
    start_worker(settings=settings, live=live)


@eval_app.command("run")
def eval_run(
    questions: Annotated[
        Path,
        typer.Option("--questions", help="Path to tests/eval/questions.yaml"),
    ] = Path("tests/eval/questions.yaml"),
    mode: Annotated[
        Literal["dense", "hybrid", "compare"],
        typer.Option("--mode", help="Retrieval mode: dense, hybrid, or compare (both)"),
    ] = "dense",
    embedding_model: Annotated[
        str | None,
        typer.Option("--embedding-model", help="Override embedding model"),
    ] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Evaluate retrieval accuracy against currently published Qdrant + SQLite FTS5 stores."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    suite = load_eval_suite(questions)
    policy = load_ingestion_policy(settings.ingestion_policy_file)
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        pub_repo = PublicationRepository(engine)
        status = pub_repo.get_status_summary()
        model_name = (
            embedding_model
            or status.get("active_embedding_model")
            or policy.indexing.embedding_model
        )
        embedder = create_embedder(str(model_name))
        table_search = StructuredTableSearchService(engine)
        bm25_search = ChunkBM25SearchService(engine)
        with QdrantVectorStore(
            collection_name=settings.qdrant_collection,
            url=settings.qdrant_url,
            path=settings.qdrant_path,
        ) as qdrant:
            if mode == "compare":
                dense_rep = evaluate_retrieval(
                    suite=suite,
                    embedder=embedder,
                    qdrant_store=qdrant,
                    table_search=table_search,
                    retrieval_mode="dense",
                )
                hybrid_rep = evaluate_retrieval(
                    suite=suite,
                    embedder=embedder,
                    qdrant_store=qdrant,
                    table_search=table_search,
                    bm25_search=bm25_search,
                    retrieval_mode="hybrid",
                    rrf_k=policy.indexing.hybrid_rrf_k,
                    hybrid_candidate_multiplier=policy.indexing.hybrid_candidate_multiplier,
                )
                typer.echo(
                    json.dumps(
                        {
                            "dense": dense_rep.to_dict(),
                            "hybrid_rrf": hybrid_rep.to_dict(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return

            report = evaluate_retrieval(
                suite=suite,
                embedder=embedder,
                qdrant_store=qdrant,
                table_search=table_search,
                bm25_search=bm25_search if mode == "hybrid" else None,
                retrieval_mode=mode,
                rrf_k=policy.indexing.hybrid_rrf_k,
                hybrid_candidate_multiplier=policy.indexing.hybrid_candidate_multiplier,
            )
    finally:
        engine.dispose()
    typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))


@eval_app.command("benchmark")
def eval_benchmark(
    models: Annotated[
        str,
        typer.Option(
            "--models",
            help="Comma-separated candidate embedding models to benchmark",
        ),
    ] = "intfloat/multilingual-e5-small,BAAI/bge-m3",
    questions: Annotated[
        Path,
        typer.Option("--questions", help="Path to tests/eval/questions.yaml"),
    ] = Path("tests/eval/questions.yaml"),
    output_json: Annotated[
        Path | None,
        typer.Option("--output-json", help="Optional path to save full JSON benchmark report"),
    ] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Benchmark candidate multilingual embedding models in isolated temporary stores."""
    settings = AppSettings()
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    reports = benchmark_embedding_models(
        models=model_list,
        questions_path=questions,
        source_sqlite_path=sqlite_path or settings.sqlite_path,
        scope_path=settings.scope_file,
        policy_path=settings.ingestion_policy_file,
        settings=settings,
    )
    payload = [r.to_dict() for r in reports]
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(rendered + "\n", encoding="utf-8")
    typer.echo(rendered)


@scope_app.command("validate")
def scope_validate(
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
) -> None:
    """Validate config/crawl_scope.yaml against strict Gate A schema rules."""
    settings = AppSettings()
    target = scope_file or settings.scope_file
    try:
        scope_cfg, scope_hash = load_crawl_scope(target)
    except ConfigurationError as exc:
        typer.secho(f"INVALID SCOPE ({target}): {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    total_approvals = (
        len(scope_cfg.approvals.pages.urls)
        + len(scope_cfg.approvals.pages.patterns)
        + len(scope_cfg.approvals.image_posters.urls)
        + len(scope_cfg.approvals.image_posters.patterns)
        + len(scope_cfg.approvals.documents.urls)
        + len(scope_cfg.approvals.documents.patterns)
    )
    typer.secho(f"OK: {target} is valid", fg=typer.colors.GREEN)
    typer.echo(f"  scope_file_hash : {scope_hash}")
    typer.echo(
        f"  allowed_scope   : https://{scope_cfg.allowed_host}{scope_cfg.allowed_path_prefix}"
    )
    typer.echo(
        f"  exclusions      : {len(scope_cfg.exclusions.urls)} urls, "
        f"{len(scope_cfg.exclusions.patterns)} patterns"
    )
    typer.echo(f"  approvals       : {total_approvals} total rules")


@scope_app.command("approve")
def scope_approve(
    url: Annotated[str | None, typer.Option("--url")] = None,
    resource_type: Annotated[ResourceType, typer.Option("--resource-type")] = ResourceType.PAGE,
    pattern_id: Annotated[str | None, typer.Option("--pattern-id")] = None,
    proposed_file: Annotated[Path | None, typer.Option("--proposed-file")] = None,
    reason: Annotated[str, typer.Option("--reason")] = "gate_a_manual_approval",
    check_interval: Annotated[str, typer.Option("--check-interval")] = "24h",
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Approve an exact URL or promote a proposed pattern into config/crawl_scope.yaml."""
    if (url is None) == (pattern_id is None):
        typer.secho("Provide exactly one of --url or --pattern-id.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    settings = AppSettings()
    mgr = ScopeManager(
        scope_path=scope_file or settings.scope_file,
        sqlite_path=sqlite_path or settings.sqlite_path,
    )
    actor = reviewer or settings.operator_id
    try:
        if url is not None:
            new_hash = mgr.approve_url(
                raw_url=url,
                resource_type=resource_type,
                reason=reason,
                reviewer=actor,
                check_interval=check_interval,
            )
            typer.secho(
                f"Approved URL {url} (scope_hash={new_hash[:12]}...)", fg=typer.colors.GREEN
            )
        else:
            assert pattern_id is not None
            prop_path = proposed_file or (settings.data_dir / "discovery/crawl_scope.proposed.yaml")
            new_hash = mgr.approve_pattern_from_proposed(
                pattern_id=pattern_id,
                proposed_yaml_path=prop_path,
                reviewer=actor,
                reason_override=None if reason == "gate_a_manual_approval" else reason,
            )
            typer.secho(
                f"Approved pattern {pattern_id} (scope_hash={new_hash[:12]}...)",
                fg=typer.colors.GREEN,
            )
    except (ConfigurationError, ScopeMutationError) as exc:
        typer.secho(f"SCOPE APPROVE FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@scope_app.command("exclude")
def scope_exclude(
    url: Annotated[str | None, typer.Option("--url")] = None,
    pattern: Annotated[str | None, typer.Option("--pattern")] = None,
    rule_id: Annotated[str | None, typer.Option("--id")] = None,
    reason: Annotated[str, typer.Option("--reason")] = "gate_a_manual_exclusion",
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Exclude an exact URL or path pattern in config/crawl_scope.yaml."""
    if (url is None) == (pattern is None):
        typer.secho("Provide exactly one of --url or --pattern.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    settings = AppSettings()
    mgr = ScopeManager(
        scope_path=scope_file or settings.scope_file,
        sqlite_path=sqlite_path or settings.sqlite_path,
    )
    actor = reviewer or settings.operator_id
    try:
        if url is not None:
            new_hash = mgr.exclude_url(raw_url=url, reason=reason, reviewer=actor)
            typer.secho(
                f"Excluded URL {url} (scope_hash={new_hash[:12]}...)", fg=typer.colors.GREEN
            )
        else:
            assert pattern is not None
            if not rule_id:
                raise ScopeMutationError("--id is required when adding a pattern exclusion")
            new_hash = mgr.exclude_pattern(
                rule_id=rule_id,
                pattern=pattern,
                reason=reason,
                reviewer=actor,
            )
            typer.secho(
                f"Excluded pattern {rule_id}:{pattern} (scope_hash={new_hash[:12]}...)",
                fg=typer.colors.GREEN,
            )
    except (ConfigurationError, ScopeMutationError) as exc:
        typer.secho(f"SCOPE EXCLUDE FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


@scope_app.command("import")
def scope_import(
    csv_path: Annotated[Path, typer.Argument(help="Reviewer-edited url_inventory.csv path")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Import approved/excluded rows from edited url_inventory.csv into config/crawl_scope.yaml."""
    settings = AppSettings()
    mgr = ScopeManager(
        scope_path=scope_file or settings.scope_file,
        sqlite_path=sqlite_path or settings.sqlite_path,
    )
    actor = reviewer or settings.operator_id
    try:
        summary = mgr.import_csv(csv_path=csv_path, reviewer=actor, dry_run=dry_run)
    except (ConfigurationError, ScopeMutationError) as exc:
        typer.secho(f"SCOPE IMPORT FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    mode_label = "DRY-RUN" if dry_run else "APPLIED"
    typer.secho(
        f"[{mode_label}] approvals_added={len(summary.added_approvals)} "
        f"exclusions_added={len(summary.added_exclusions)} "
        f"skipped_unchanged={summary.skipped_unchanged}",
        fg=typer.colors.GREEN,
    )
    for u in summary.added_approvals:
        typer.echo(f"  + approve : {u}")
    for u in summary.added_exclusions:
        typer.echo(f"  + exclude : {u}")


@scope_app.command("export")
def scope_export(
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Re-export discovery inventory joined with current config/crawl_scope.yaml decisions."""
    settings = AppSettings()
    mgr = ScopeManager(
        scope_path=scope_file or settings.scope_file,
        sqlite_path=sqlite_path or settings.sqlite_path,
    )
    target_dir = output_dir or (settings.data_dir / "discovery")
    try:
        written = mgr.export_joined_inventory(output_dir=target_dir)
    except (ConfigurationError, ScopeMutationError) as exc:
        typer.secho(f"SCOPE EXPORT FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho("Exported joined inventory views:", fg=typer.colors.GREEN)
    for label, path in written.items():
        typer.echo(f"  {label}: {path}")


@calendar_app.command("sync")
def calendar_sync(
    live: Annotated[
        bool,
        typer.Option("--live", help="Fetch bounded JEvents monthcalendar + eventdetail live"),
    ] = False,
    months_ahead: Annotated[
        int | None,
        typer.Option("--months-ahead", help="Months ahead of current month (default 3)"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum eventdetail pages to process"),
    ] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Discover and stage bounded JEvents blood donation schedules for Gate B review."""
    if not live:
        typer.secho(
            "Refusing live calendar sync without --live flag (safe default). "
            "Pass --live with PDN_LIVE_CRAWL_ENABLED=true when ready.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = AppSettings()
    try:
        summary = run_calendar_sync_pipeline(
            options=CalendarSyncOptions(
                live=True,
                months_ahead=months_ahead,
                limit=limit,
                scope_file=scope_file,
                policy_file=policy_file,
                sqlite_path=sqlite_path,
            ),
            settings=settings,
            progress_cb=lambda msg: typer.echo(f"[calendar] {msg}"),
        )
    except (
        ConfigurationError,
        LiveCrawlBlockedError,
        RobotsUnavailableError,
        GateABlockedError,
    ) as exc:
        typer.secho(f"CALENDAR SYNC BLOCKED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"Calendar sync complete: run_id={summary.run_id} "
        f"months={summary.months_succeeded}/{summary.months_attempted} "
        f"discovered={summary.eventdetail_discovered} fetched={summary.fetched} "
        f"unchanged={summary.unchanged_blob} pending={summary.staged_pending} "
        f"quarantined={summary.staged_quarantined} carried_forward={summary.carried_forward} "
        f"sessions={summary.sessions_extracted_total} removed={summary.marked_removed_events} "
        f"failed={summary.failed}",
        fg=typer.colors.GREEN,
    )


@app.command("check-policy")
def check_policy(
    scope_file: Annotated[Path | None, typer.Option("--scope-file")] = None,
    policy_file: Annotated[Path | None, typer.Option("--policy-file")] = None,
) -> None:
    """Validate local configs and report offline source-access clearance status."""
    settings = AppSettings()
    target_scope = scope_file or settings.scope_file
    target_policy = policy_file or settings.ingestion_policy_file

    try:
        _, scope_hash = load_crawl_scope(target_scope)
        policy_cfg = load_ingestion_policy(target_policy)
    except ConfigurationError as exc:
        typer.secho(f"CONFIG ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    evidence = SourceAccessEvidence(
        v2_robots_fetched=False,
        v2_robots_policy=None,
        publicly_reachable_without_controls=True,
        no_prohibiting_terms_or_directives=True,
        verified_date="2026-11-24",
    )
    clearance = evaluate_source_clearance(
        settings=settings,
        politeness=policy_cfg.politeness,
        evidence=evidence,
        require_live_flag=True,
    )

    typer.echo(f"Scope file      : {target_scope} (sha256={scope_hash[:12]}...)")
    typer.echo(f"Ingestion policy: {target_policy} (approval_mode={policy_cfg.approval_mode})")
    typer.echo(f"Operator ID     : {settings.operator_id}")
    typer.echo(f"User-Agent set  : {'yes' if clearance.user_agent else 'NO (fail-closed)'}")
    typer.echo(f"Owner ack flag  : {settings.source_policy_acknowledged}")
    typer.echo(f"Live crawl flag : {settings.live_crawl_enabled}")
    if clearance.blocking_reasons:
        typer.secho("Live crawl status: BLOCKED (safe default)", fg=typer.colors.YELLOW)
        for reason in clearance.blocking_reasons:
            typer.echo(f"  - {reason}")
    else:
        typer.secho("Live crawl status: CLEARED", fg=typer.colors.GREEN)


# ── Phase 5 Operations CLI (`pdn-sync ops ...`) ──────────────────────────────


@ops_app.command("backup")
def ops_backup(
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Backup root directory (default PDN_BACKUP_DIR)"),
    ] = None,
    include_qdrant: Annotated[
        bool,
        typer.Option("--include-qdrant", help="Also snapshot embedded data/qdrant/ directory"),
    ] = False,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Create a verified SQLite Online Backup (`sqlite3.Connection.backup`) + SHA-256 manifest."""
    settings = AppSettings()
    target_db = sqlite_path or settings.sqlite_path
    dest_root = output_dir or settings.backup_dir
    try:
        res = create_online_backup(
            sqlite_path=target_db,
            backup_root=dest_root,
            include_qdrant=include_qdrant,
            qdrant_path=settings.qdrant_path,
        )
    except (FileNotFoundError, BackupVerificationError) as exc:
        typer.secho(f"BACKUP FAILED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(f"OK: Online backup created at {res.backup_dir}", fg=typer.colors.GREEN)
    typer.echo(
        json.dumps(
            {
                "backup_dir": str(res.backup_dir),
                "manifest_path": str(res.manifest_path),
                "sqlite_sha256": res.sqlite_sha256,
                "sqlite_bytes": res.sqlite_bytes,
                "alembic_revision": res.alembic_revision,
                "integrity_ok": res.integrity_ok,
                "foreign_key_errors": res.foreign_key_errors,
                "wal_checkpoint": res.wal_checkpoint,
                "table_counts": res.table_counts,
                "qdrant_included": res.qdrant_included,
                "qdrant_file_count": res.qdrant_file_count,
            },
            indent=2,
            sort_keys=True,
        )
    )


@ops_app.command("verify-backup")
def ops_verify_backup(
    backup_dir: Annotated[Path, typer.Argument(help="Path to backup_<timestamp> directory")],
) -> None:
    """Verify a backup's SHA-256 manifest, PRAGMA integrity_check, and foreign_key_check."""
    res = verify_backup(backup_dir)
    payload = {
        "valid": res.valid,
        "backup_dir": str(res.backup_dir),
        "sqlite_sha256_ok": res.sqlite_sha256_ok,
        "integrity_ok": res.integrity_ok,
        "foreign_key_errors": res.foreign_key_errors,
        "qdrant_files_checked": res.qdrant_files_checked,
        "errors": res.errors,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not res.valid:
        raise typer.Exit(code=1)


@ops_app.command("restore")
def ops_restore(
    backup_dir: Annotated[Path, typer.Argument(help="Verified backup directory to restore from")],
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Required explicit confirmation before replacing SQLite DB"),
    ] = False,
    restore_qdrant: Annotated[
        bool,
        typer.Option("--restore-qdrant", help="Also restore Qdrant snapshot if present"),
    ] = False,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Verify and atomically restore SQLite (+ optional Qdrant) while cleaning stale WAL/SHM."""
    settings = AppSettings()
    target_db = sqlite_path or settings.sqlite_path
    try:
        res = restore_online_backup(
            backup_dir=backup_dir,
            target_sqlite_path=target_db,
            confirm=confirm,
            restore_qdrant=restore_qdrant,
            target_qdrant_path=settings.qdrant_path,
        )
    except (ValueError, BackupVerificationError, FileNotFoundError) as exc:
        typer.secho(f"RESTORE REFUSED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"OK: Restored verified backup into {res.target_sqlite_path}", fg=typer.colors.GREEN
    )
    typer.echo(
        json.dumps(
            {
                "backup_dir": str(res.backup_dir),
                "target_sqlite_path": str(res.target_sqlite_path),
                "restored_sha256": res.restored_sha256,
                "integrity_ok": res.integrity_ok,
                "qdrant_restored": res.qdrant_restored,
            },
            indent=2,
            sort_keys=True,
        )
    )


@ops_app.command("check-alerts")
def ops_check_alerts(
    as_json: Annotated[bool, typer.Option("--json", help="Emit structured JSON output")] = False,
    fail_on: Annotated[
        Literal["none", "critical", "warning"],
        typer.Option("--fail-on", help="Exit non-zero when alerts at/above this severity fire"),
    ] = "critical",
    skip_qdrant: Annotated[
        bool,
        typer.Option(
            "--skip-qdrant", help="Skip opening embedded Qdrant (e.g. while API holds lock)"
        ),
    ] = False,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Evaluate freshness, backlog, FTS5/Qdrant drift, and DLQ alerts (cron/CI ready)."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    qd_ok = True
    qd_points: int | None = None
    try:
        if not skip_qdrant:
            try:
                with QdrantVectorStore(
                    collection_name=settings.qdrant_collection,
                    url=settings.qdrant_url,
                    path=settings.qdrant_path,
                ) as qdrant:
                    qd_ok, qd_points = qdrant.health_check()
            except Exception:
                qd_ok = False
                qd_points = 0

        report = evaluate_alerts(
            engine=engine,
            policy_path=settings.ingestion_policy_file,
            scope_path=settings.scope_file,
            qdrant_ok=qd_ok,
            qdrant_published_points=qd_points,
        )
    finally:
        engine.dispose()

    payload = {
        "checked_at_utc": report.checked_at_utc.isoformat(),
        "critical_count": report.critical_count,
        "warning_count": report.warning_count,
        "dlq_count": report.dlq_count,
        "outbox_pending_count": report.outbox_pending_count,
        "fts5_mismatches": report.fts5_mismatches,
        "oldest_pending_gate_b_age_hours": report.oldest_pending_gate_b_age_hours,
        "qdrant_ok": report.qdrant_ok,
        "qdrant_published_points": report.qdrant_published_points,
        "qdrant_drift_abs": report.qdrant_drift_abs,
        "alerts": [a.model_dump() for a in report.alerts],
    }

    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not report.alerts:
            typer.secho("OK: 0 active alerts.", fg=typer.colors.GREEN)
        else:
            for a in report.alerts:
                color = typer.colors.RED if a.severity == "critical" else typer.colors.YELLOW
                typer.secho(f"[{a.severity.upper()}] {a.code}: {a.summary}", fg=color)
                typer.echo(f"  runbook: {a.runbook_ref}")

    if fail_on == "critical" and report.critical_count > 0:
        raise typer.Exit(code=2)
    if fail_on == "warning" and (report.critical_count > 0 or report.warning_count > 0):
        raise typer.Exit(code=2)


@ops_app.command("reconcile")
def ops_reconcile(
    fts_only: Annotated[
        bool, typer.Option("--fts-only", help="Reconcile SQLite FTS5 only")
    ] = False,
    outbox_only: Annotated[
        bool, typer.Option("--outbox-only", help="Replay durable outbox events only")
    ] = False,
    qdrant_only: Annotated[
        bool, typer.Option("--qdrant-only", help="Repair Qdrant<->SQLite sync drift only")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Inspect without mutating")] = False,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Reconcile SQLite FTS5 indexes, durable outbox retries, and Qdrant sync drift."""
    settings = AppSettings()
    policy = load_ingestion_policy(settings.ingestion_policy_file)
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    run_all = not (fts_only or outbox_only or qdrant_only)
    out: dict[str, object] = {}

    try:
        if run_all or fts_only:
            if dry_run:
                out["fts5"] = {
                    "dry_run": True,
                    "mismatches": OpsRepository(engine).get_fts5_mismatch_counts(),
                }
            else:
                fts_res = reconcile_fts5(engine)
                out["fts5"] = {
                    "structured_tables_synced": fts_res.structured_tables_synced,
                    "donation_sessions_synced": fts_res.donation_sessions_synced,
                    "knowledge_chunks_synced": fts_res.knowledge_chunks_synced,
                    "before_mismatches": fts_res.before_mismatches,
                    "after_mismatches": fts_res.after_mismatches,
                }

        if run_all or outbox_only or qdrant_only:
            with QdrantVectorStore(
                collection_name=settings.qdrant_collection,
                url=settings.qdrant_url,
                path=settings.qdrant_path,
            ) as qdrant:
                embedder = None
                if not dry_run:
                    pub_repo = PublicationRepository(engine)
                    ops_repo = OpsRepository(engine)
                    needs_embed = any(
                        ev.event_type == "qdrant_upsert_publication"
                        for ev in ops_repo.list_outbox_events(
                            statuses=("pending", "failed"), due_only=True
                        )
                    )
                    if needs_embed or qdrant_only:
                        status = pub_repo.get_status_summary()
                        model_name = (
                            status.get("active_embedding_model") or policy.indexing.embedding_model
                        )
                        embedder = create_embedder(str(model_name))

                if run_all or outbox_only:
                    ob_res = reconcile_outbox(
                        engine=engine,
                        qdrant=qdrant,
                        embedder=embedder,
                        retry_policy=policy.retry,
                        dry_run=dry_run,
                    )
                    out["outbox"] = {
                        "considered": ob_res.considered,
                        "dispatched": ob_res.dispatched,
                        "failed_retryable": ob_res.failed_retryable,
                        "dead_lettered": ob_res.dead_lettered,
                        "dry_run": ob_res.dry_run,
                        "event_outcomes": ob_res.event_outcomes,
                    }

                if run_all or qdrant_only:
                    qd_res = reconcile_qdrant(
                        engine=engine,
                        qdrant=qdrant,
                        embedder=embedder,
                        dry_run=dry_run,
                    )
                    out["qdrant"] = {
                        "sqlite_published_chunks": qd_res.sqlite_published_chunks,
                        "qdrant_published_points_before": qd_res.qdrant_published_points_before,
                        "qdrant_published_points_after": qd_res.qdrant_published_points_after,
                        "repaired_publications": qd_res.repaired_publications,
                        "deactivated_publications": qd_res.deactivated_publications,
                        "dry_run": qd_res.dry_run,
                    }
    finally:
        engine.dispose()

    typer.echo(json.dumps(out, indent=2, sort_keys=True))


@dlq_app.command("list")
def ops_dlq_list(
    status: Annotated[
        Literal["dead_letter", "failed", "pending", "all"],
        typer.Option("--status", help="Filter outbox events by status"),
    ] = "dead_letter",
    limit: Annotated[int, typer.Option("--limit")] = 50,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """List outbox / dead-letter queue events."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        ops_repo = OpsRepository(engine)
        statuses = None if status == "all" else (status,)
        rows = ops_repo.list_outbox_events(statuses=statuses, limit=limit)
        payload = [
            {
                "event_id": r.event_id,
                "event_type": r.event_type,
                "status": r.status,
                "dedupe_key": r.dedupe_key,
                "publication_id": r.publication_id,
                "resource_id": r.resource_id,
                "attempt_count": r.attempt_count,
                "max_attempts": r.max_attempts,
                "last_error": r.last_error,
                "updated_at_utc": r.updated_at_utc.isoformat(),
                "resolved_by": r.resolved_by,
                "resolved_note": r.resolved_note,
            }
            for r in rows
        ]
    finally:
        engine.dispose()
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@dlq_app.command("resolve")
def ops_dlq_resolve(
    event_id: Annotated[str, typer.Argument(help="Outbox event ID (e.g. obx_...)")],
    reason: Annotated[str, typer.Option("--reason", help="Audit note for resolution")] = "resolved",
    requeue: Annotated[
        bool,
        typer.Option("--requeue", help="Reset to pending with attempt_count=0 for replay"),
    ] = False,
    reviewer: Annotated[str | None, typer.Option("--reviewer")] = None,
    sqlite_path: Annotated[Path | None, typer.Option("--sqlite-path")] = None,
) -> None:
    """Mark a dead-lettered/failed outbox event as resolved or requeue it for replay."""
    settings = AppSettings()
    db_path = sqlite_path or settings.sqlite_path
    upgrade_database(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        ops_repo = OpsRepository(engine)
        try:
            row = ops_repo.resolve_outbox_event(
                event_id,
                reviewer=reviewer or settings.operator_id,
                note=reason,
                requeue=requeue,
            )
        except KeyError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    typer.secho(
        f"OK: {row.event_id} -> status={row.status} (by {row.resolved_by})",
        fg=typer.colors.GREEN,
    )
