"""Command-line interface for PDN Multimodal Knowledge Sync (Phase 0 foundation)."""

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
from pdn_sync.crawling.clearance import SourceAccessEvidence, evaluate_source_clearance

app = typer.Typer(
    name="pdn-sync",
    help="Two-gate multimodal knowledge ingestion pipeline for Pusat Darah Negara (PDN).",
    no_args_is_help=True,
)
scope_app = typer.Typer(help="Gate A crawl-scope validation and management commands.")
app.add_typer(scope_app, name="scope")


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

    # Offline check does not fetch live /v2/robots.txt; reports current readiness accurately.
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
