"""Command-line interface for PDN Multimodal Knowledge Sync (Phases 0-1)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
from pdn_sync.discovery.scope_manager import ScopeManager, ScopeMutationError
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.repository import ControlPlaneRepository, InventoryFilter

app = typer.Typer(
    name="pdn-sync",
    help="Two-gate multimodal knowledge ingestion pipeline for Pusat Darah Negara (PDN).",
    no_args_is_help=True,
)
db_app = typer.Typer(help="SQLite control-plane migration commands.")
inventory_app = typer.Typer(help="Inspect discovered URL and asset inventories.")
scope_app = typer.Typer(help="Gate A crawl-scope validation and management commands.")
app.add_typer(db_app, name="db")
app.add_typer(inventory_app, name="inventory")
app.add_typer(scope_app, name="scope")


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
    url: Annotated[
        str | None,
        typer.Option("--url", help="Exact PDN URL to approve"),
    ] = None,
    resource_type: Annotated[
        ResourceType,
        typer.Option("--resource-type", help="Resource type for exact URL approval"),
    ] = ResourceType.PAGE,
    pattern_id: Annotated[
        str | None,
        typer.Option("--pattern-id", help="Promote a proposed pattern ID from proposed YAML"),
    ] = None,
    proposed_file: Annotated[
        Path | None,
        typer.Option("--proposed-file", help="Path to crawl_scope.proposed.yaml"),
    ] = None,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Human review reason"),
    ] = "gate_a_manual_approval",
    check_interval: Annotated[
        str,
        typer.Option("--check-interval", help="Monitoring cadence (e.g. 3h, 24h)"),
    ] = "24h",
    reviewer: Annotated[
        str | None,
        typer.Option("--reviewer", help="Operator handle for audit trail"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
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
    url: Annotated[
        str | None,
        typer.Option("--url", help="Exact PDN URL to exclude"),
    ] = None,
    pattern: Annotated[
        str | None,
        typer.Option("--pattern", help="Path exclusion glob (broad /** allowed)"),
    ] = None,
    rule_id: Annotated[
        str | None,
        typer.Option("--id", help="Rule ID for pattern exclusion"),
    ] = None,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Reason for exclusion"),
    ] = "gate_a_manual_exclusion",
    reviewer: Annotated[
        str | None,
        typer.Option("--reviewer", help="Operator handle for audit trail"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
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
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview diff without modifying config/crawl_scope.yaml"),
    ] = False,
    reviewer: Annotated[
        str | None,
        typer.Option("--reviewer", help="Operator handle for audit trail"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
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
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Target directory for joined inventory exports"),
    ] = None,
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    sqlite_path: Annotated[
        Path | None,
        typer.Option("--sqlite-path", help="Path to SQLite control-plane DB"),
    ] = None,
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


@app.command("check-policy")
def check_policy(
    scope_file: Annotated[
        Path | None,
        typer.Option("--scope-file", help="Path to config/crawl_scope.yaml"),
    ] = None,
    policy_file: Annotated[
        Path | None,
        typer.Option("--policy-file", help="Path to config/ingestion_policy.yaml"),
    ] = None,
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
