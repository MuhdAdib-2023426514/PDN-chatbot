"""Offline integration tests for Gate A SQLite control plane, YAML comment preservation, and CLI."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from pdn_sync.cli import app
from pdn_sync.config import AppSettings, load_crawl_scope
from pdn_sync.discovery.crawler import DiscoveryOptions, run_discovery
from pdn_sync.discovery.scope_manager import (
    GateABlockedError,
    ScopeManager,
    ScopeMutationError,
    assert_url_approved_for_content_crawl,
)
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import evaluate_scope
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.repository import ControlPlaneRepository
from tests.integration.test_discovery_offline import _make_mock_site

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_sqlite_migrations_and_wal_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "control.db"
    upgrade_database(db_path)

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            journal = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).all()
            }
    finally:
        engine.dispose()

    assert fk == 1
    assert str(journal).lower() == "wal"
    assert {
        "sync_runs",
        "crawl_resources",
        "discovered_urls",
        "scope_decisions",
        "compiled_scope_cache",
    }.issubset(tables)


MINIMAL_EMPTY_SCOPE_YAML = """# IMPORTANT: human-owned Gate A scope fixture
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
    # Hard Phase 0-3 JEvents exclusion
    - id: exclude-jevents-index-path
      pattern: "/v2/index.php/component/jevents/**"
      reason: deferred_to_structured_schedule_phase
    - id: exclude-jevents-query
      query_param: "option=com_jevents"
      reason: deferred_to_structured_schedule_phase
approvals:
  pages:
    urls: []
    patterns: []
  image_posters:
    urls: []
    patterns: []
  documents:
    urls: []
    patterns: []
"""


def test_gate_a_csv_import_comment_preservation_pattern_promotion_and_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "crawl_scope.yaml"
    policy_file = tmp_path / "ingestion_policy.yaml"
    scope_file.write_text(MINIMAL_EMPTY_SCOPE_YAML, encoding="utf-8")
    shutil.copyfile(REPO_ROOT / "config/ingestion_policy.yaml", policy_file)
    db_path = tmp_path / "pdn_sync.db"
    out_dir = tmp_path / "discovery"

    settings = AppSettings(
        contact_email="tester@pdn-sync.org",
        operator_id="review-owner",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
        scope_file=scope_file,
        ingestion_policy_file=policy_file,
        sqlite_path=db_path,
        data_dir=tmp_path,
    )

    transport, _ = _make_mock_site()
    run_discovery(
        options=DiscoveryOptions(
            live=True,
            enable_head=True,
            output_dir=out_dir,
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=transport,
        sleep_fn=lambda _s: None,
    )

    # 1. Before approval, Gate A blocks content crawl of homepage and JEvents
    with pytest.raises(GateABlockedError):
        assert_url_approved_for_content_crawl("https://pdn.gov.my/v2/", scope_path=scope_file)
    with pytest.raises(GateABlockedError):
        assert_url_approved_for_content_crawl(
            "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-",
            scope_path=scope_file,
        )

    # 2. Editing CSV without `scope import` has ZERO effect on scope authority
    csv_path = out_dir / "url_inventory.csv"
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
        fieldnames = list(rows[0].keys())

    for row in rows:
        if row["url"] == "https://pdn.gov.my/v2/":
            row["disposition"] = "approved"
            row["notes"] = "Approved homepage for Phase 2"
        if row["url"].endswith("99-encik-ali"):
            row["disposition"] = "excluded"
            row["notes"] = "PDPA staff profile excluded"

    edited_csv = tmp_path / "edited_inventory.csv"
    with edited_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    scope_before_import, _ = load_crawl_scope(scope_file)
    assert (
        evaluate_scope("https://pdn.gov.my/v2/", scope_before_import).disposition
        == Disposition.PENDING
    )

    # 3. `scope import --dry-run` previews without editing crawl_scope.yaml
    mgr = ScopeManager(scope_path=scope_file, sqlite_path=db_path)
    dry_summary = mgr.import_csv(csv_path=edited_csv, reviewer="review-owner", dry_run=True)
    assert "https://pdn.gov.my/v2/" in dry_summary.added_approvals
    assert any(u.endswith("99-encik-ali") for u in dry_summary.added_exclusions)
    assert (
        evaluate_scope("https://pdn.gov.my/v2/", load_crawl_scope(scope_file)[0]).disposition
        == Disposition.PENDING
    )

    # 4. Real `scope import` updates crawl_scope.yaml, preserves comments, and logs SQLite audit
    applied = mgr.import_csv(csv_path=edited_csv, reviewer="review-owner", dry_run=False)
    assert applied.scope_file_hash_after != applied.scope_file_hash_before
    yaml_text = scope_file.read_text(encoding="utf-8")
    assert "# IMPORTANT:" in yaml_text
    assert "# Hard Phase 0-3 JEvents exclusion" in yaml_text

    # Homepage is now Gate-A-approved; staff leaf profile is excluded
    assert (
        assert_url_approved_for_content_crawl("https://pdn.gov.my/v2/", scope_path=scope_file)
        == "https://pdn.gov.my/v2/"
    )
    with pytest.raises(GateABlockedError):
        assert_url_approved_for_content_crawl(
            "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it/item/99-encik-ali",
            scope_path=scope_file,
        )

    # 5. CSV trying to approve a JEvents URL is rejected cleanly
    bad_csv = tmp_path / "bad_jevents.csv"
    with bad_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["url", "resource_type", "disposition", "reason"])
        writer.writeheader()
        writer.writerow(
            {
                "url": "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-",
                "resource_type": "page",
                "disposition": "approved",
                "reason": "should fail",
            }
        )
    with pytest.raises(ScopeMutationError, match="JEvents"):
        mgr.import_csv(csv_path=bad_csv, reviewer="review-owner", dry_run=False)

    # 6. Promote rotating blood-stock pattern from crawl_scope.proposed.yaml via CLI
    proposed_yaml = out_dir / "crawl_scope.proposed.yaml"
    res_promote = runner.invoke(
        app,
        [
            "scope",
            "approve",
            "--pattern-id",
            "pattern-blood-stock-graphic-images-stokdarah-png",
            "--proposed-file",
            str(proposed_yaml),
            "--scope-file",
            str(scope_file),
            "--sqlite-path",
            str(db_path),
            "--reviewer",
            "review-owner",
        ],
    )
    assert res_promote.exit_code == 0, res_promote.output

    # A NEW future filename in /v2/images/stokdarah/ is now crawl-eligible under the pattern!
    assert (
        assert_url_approved_for_content_crawl(
            "https://pdn.gov.my/v2/images/stokdarah/1stockdecember2026.png",
            scope_path=scope_file,
            resource_type=ResourceType.IMAGE_POSTER,
        )
        == "https://pdn.gov.my/v2/images/stokdarah/1stockdecember2026.png"
    )

    # 7. Verify SQLite audit trail and CLI `inventory list` + `scope export` work
    engine = create_sqlite_engine(db_path)
    try:
        decisions = ControlPlaneRepository(engine).list_scope_decisions()
    finally:
        engine.dispose()
    assert len(decisions) >= 3

    res_list = runner.invoke(
        app,
        ["inventory", "list", "--category", "jevents_deferred", "--sqlite-path", str(db_path)],
    )
    assert res_list.exit_code == 0
    assert "jevents" in res_list.stdout.lower()

    res_export = runner.invoke(
        app,
        [
            "scope",
            "export",
            "--output-dir",
            str(out_dir),
            "--scope-file",
            str(scope_file),
            "--sqlite-path",
            str(db_path),
        ],
    )
    assert res_export.exit_code == 0
