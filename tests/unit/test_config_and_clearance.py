"""Unit tests for config loading, User-Agent fail-closed checks, and 5-point clearance."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdn_sync.config import (
    AppSettings,
    ConfigurationError,
    build_user_agent,
    load_crawl_scope,
    load_ingestion_policy,
)
from pdn_sync.crawling.clearance import (
    LiveCrawlBlockedError,
    SourceAccessEvidence,
    evaluate_source_clearance,
    require_live_clearance,
)
from pdn_sync.crawling.robots import SubfolderRobotsPolicy
from pdn_sync.domain.policy import ApprovalMode, PolitenessPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "bad_email",
    [
        None,
        "",
        "   ",
        "not-an-email",
        "your-email@example.com",
        "example@example.com",
        "<CONTACT_EMAIL>",
        "user@localhost",
    ],
)
def test_build_user_agent_fails_closed_on_missing_or_placeholder_email(
    bad_email: str | None,
) -> None:
    with pytest.raises(ConfigurationError):
        build_user_agent(bad_email)


def test_build_user_agent_formats_valid_email() -> None:
    ua = build_user_agent("maintainer@pdn-sync.org")
    assert ua == "PDN-Knowledge-Sync/0.1 (+contact: maintainer@pdn-sync.org)"


def test_repo_config_files_load_and_validate() -> None:
    scope, scope_hash = load_crawl_scope(REPO_ROOT / "config/crawl_scope.yaml")
    policy = load_ingestion_policy(REPO_ROOT / "config/ingestion_policy.yaml")

    assert scope.allowed_host == "pdn.gov.my"
    assert scope.allowed_path_prefix == "/v2"
    assert len(scope_hash) == 64
    assert policy.approval_mode == ApprovalMode.MANUAL
    assert policy.retirement_consecutive_runs == 2
    assert policy.domain_timezone == "Asia/Kuala_Lumpur"
    assert policy.indexing.embed_donation_schedule_rows is False


def test_load_crawl_scope_fails_closed_on_invalid_yaml(tmp_path: Path) -> None:
    bad_file = tmp_path / "crawl_scope.yaml"
    bad_file.write_text("allowed_host: [unterminated", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_crawl_scope(bad_file)


def test_load_crawl_scope_fails_closed_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_crawl_scope(tmp_path / "does_not_exist.yaml")


def test_load_ingestion_policy_fails_closed_on_unknown_field(tmp_path: Path) -> None:
    bad_policy = tmp_path / "ingestion_policy.yaml"
    bad_policy.write_text("schema_version: 1\nunknown_option: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_ingestion_policy(bad_policy)


def _valid_robots_policy() -> SubfolderRobotsPolicy:
    return SubfolderRobotsPolicy.parse("User-agent: *\nDisallow: /administrator/\n")


def test_clearance_blocks_when_email_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PDN_CONTACT_EMAIL", raising=False)
    settings = AppSettings(
        contact_email=None,
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
    )
    evidence = SourceAccessEvidence(
        v2_robots_fetched=True,
        v2_robots_policy=_valid_robots_policy(),
        publicly_reachable_without_controls=True,
        no_prohibiting_terms_or_directives=True,
    )
    result = evaluate_source_clearance(
        settings=settings,
        politeness=PolitenessPolicy(),
        evidence=evidence,
    )
    assert not result.cleared
    assert any("criterion_4_failed" in r for r in result.blocking_reasons)


def test_clearance_blocks_when_v2_robots_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        contact_email="ops@pdn-sync.org",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
    )
    evidence = SourceAccessEvidence(
        v2_robots_fetched=False,
        v2_robots_policy=None,
        publicly_reachable_without_controls=True,
        no_prohibiting_terms_or_directives=True,
    )
    with pytest.raises(LiveCrawlBlockedError, match="criterion_1_failed"):
        require_live_clearance(
            settings=settings,
            politeness=PolitenessPolicy(),
            evidence=evidence,
        )


def test_clearance_blocks_when_owner_acknowledgement_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        contact_email="ops@pdn-sync.org",
        source_policy_acknowledged=False,
        live_crawl_enabled=True,
    )
    evidence = SourceAccessEvidence(
        v2_robots_fetched=True,
        v2_robots_policy=_valid_robots_policy(),
        publicly_reachable_without_controls=True,
        no_prohibiting_terms_or_directives=True,
    )
    with pytest.raises(LiveCrawlBlockedError, match="criterion_5_failed"):
        require_live_clearance(
            settings=settings,
            politeness=PolitenessPolicy(),
            evidence=evidence,
        )


def test_clearance_succeeds_only_when_all_five_criteria_and_live_flag_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        contact_email="ops@pdn-sync.org",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
    )
    evidence = SourceAccessEvidence(
        v2_robots_fetched=True,
        v2_robots_policy=_valid_robots_policy(),
        publicly_reachable_without_controls=True,
        no_prohibiting_terms_or_directives=True,
        verified_date="2026-11-24",
    )
    ua = require_live_clearance(
        settings=settings,
        politeness=PolitenessPolicy(),
        evidence=evidence,
    )
    assert ua == "PDN-Knowledge-Sync/0.1 (+contact: ops@pdn-sync.org)"
