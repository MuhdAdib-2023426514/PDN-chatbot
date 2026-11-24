"""Unit tests for Phase 0 CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pdn_sync.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_cli_scope_validate_succeeds_on_repo_scope() -> None:
    result = runner.invoke(
        app,
        ["scope", "validate", "--scope-file", str(REPO_ROOT / "config/crawl_scope.yaml")],
    )
    assert result.exit_code == 0
    assert "is valid" in result.stdout
    assert "https://pdn.gov.my/v2" in result.stdout


def test_cli_scope_validate_fails_closed_on_broad_approval_pattern(tmp_path: Path) -> None:
    bad_scope = tmp_path / "bad_scope.yaml"
    bad_scope.write_text(
        """
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns: []
approvals:
  pages:
    urls: []
    patterns:
      - id: bad-wildcard
        pattern: "/v2/**"
        resource_type: page
        reason: invalid
        max_matches: 10
        approved_by: tester
        approved_at: "2026-11-24T00:00:00Z"
  image_posters:
    urls: []
    patterns: []
  documents:
    urls: []
    patterns: []
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["scope", "validate", "--scope-file", str(bad_scope)])
    assert result.exit_code == 1
    assert "INVALID SCOPE" in result.output


def test_cli_check_policy_runs_offline_and_reports_safe_default() -> None:
    result = runner.invoke(
        app,
        [
            "check-policy",
            "--scope-file",
            str(REPO_ROOT / "config/crawl_scope.yaml"),
            "--policy-file",
            str(REPO_ROOT / "config/ingestion_policy.yaml"),
        ],
    )
    assert result.exit_code == 0
    assert "approval_mode=manual" in result.stdout
    assert "BLOCKED (safe default)" in result.stdout
