"""Unit tests for Gate A crawl_scope.yaml validation and precedence rules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdn_sync.config import load_crawl_scope
from pdn_sync.domain.policy import Disposition, ResourceType, SupersessionPolicy
from pdn_sync.domain.scope import (
    ApprovalPatternRule,
    ApprovalsSection,
    ApprovalUrlRule,
    CrawlScopeConfig,
    ExclusionPatternRule,
    ExclusionsSection,
    ResourceApprovals,
    evaluate_scope,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_repo_scope_excludes_jevents_and_system_and_defaults_others_pending() -> None:
    scope, _ = load_crawl_scope(REPO_ROOT / "config/crawl_scope.yaml")

    # Phase 4 bounded monthcalendar and eventdetail are approved under bounded patterns
    jevents_month = evaluate_scope(
        "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-?Itemid=0",
        scope,
        resource_type=ResourceType.PAGE,
    )
    assert jevents_month.disposition == Disposition.APPROVED

    jevents_detail = evaluate_scope(
        "https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/123/-/kempen",
        scope,
        resource_type=ResourceType.PAGE,
    )
    assert jevents_detail.disposition == Disposition.APPROVED

    # Runaway / non-target JEvents views remain strictly excluded
    jevents_week = evaluate_scope(
        "https://pdn.gov.my/v2/index.php/component/jevents/eventsbyweek/2026/11/24/-",
        scope,
    )
    assert jevents_week.disposition == Disposition.EXCLUDED
    assert jevents_week.reason == "non_target_jevents_view"

    jevents_query = evaluate_scope(
        "https://pdn.gov.my/v2/index.php?option=com_jevents&task=day.listevents",
        scope,
    )
    assert jevents_query.disposition == Disposition.EXCLUDED
    assert jevents_query.reason == "non_target_jevents_view"

    admin = evaluate_scope("https://pdn.gov.my/v2/administrator/index.php", scope)
    assert admin.disposition == Disposition.EXCLUDED

    feed = evaluate_scope("https://pdn.gov.my/v2/index.php/informasi?format=feed", scope)
    assert feed.disposition == Disposition.EXCLUDED

    # Unmatched / newly seen URLs must always default to pending
    unmatched = evaluate_scope("https://pdn.gov.my/v2/index.php/unseen-new-section-2099", scope)
    assert unmatched.disposition == Disposition.PENDING

    # Off-domain URL is always excluded
    off_domain = evaluate_scope("https://example.org/v2/", scope)
    assert off_domain.disposition == Disposition.EXCLUDED


def test_approval_pattern_rejects_broad_double_star_wildcard() -> None:
    with pytest.raises(ValidationError, match="cross-directory wildcards"):
        ApprovalPatternRule(
            id="too-broad",
            pattern="/v2/**",
            resource_type=ResourceType.PAGE,
            reason="invalid broad approval",
            max_matches=50,
            approved_by="project-owner",
            approved_at=datetime.now(UTC),
        )


def test_approval_pattern_rejects_top_level_prefix_wildcard() -> None:
    with pytest.raises(ValidationError, match="too broad"):
        CrawlScopeConfig(
            approvals=ApprovalsSection(
                pages=ResourceApprovals(
                    patterns=[
                        ApprovalPatternRule(
                            id="too-broad-top",
                            pattern="/v2/*",
                            resource_type=ResourceType.PAGE,
                            reason="too broad",
                            max_matches=50,
                            approved_by="project-owner",
                            approved_at=datetime.now(UTC),
                        )
                    ]
                )
            )
        )


def test_exclusions_override_approvals_and_bounded_pattern_matches_single_directory() -> None:
    now = datetime.now(UTC)
    scope = CrawlScopeConfig(
        exclusions=ExclusionsSection(
            urls=["https://pdn.gov.my/v2/images/stokdarah/corrupt.png"],
            patterns=[
                ExclusionPatternRule(
                    id="excl-jevents",
                    pattern="/v2/index.php/component/jevents/**",
                    reason="deferred_to_structured_schedule_phase",
                )
            ],
        ),
        approvals=ApprovalsSection(
            pages=ResourceApprovals(
                urls=[
                    ApprovalUrlRule(
                        url="https://pdn.gov.my/v2/",
                        reason="approved homepage",
                        approved_by="project-owner",
                        approved_at=now,
                    )
                ]
            ),
            image_posters=ResourceApprovals(
                patterns=[
                    ApprovalPatternRule(
                        id="blood-stock-rotating",
                        pattern="/v2/images/stokdarah/*.png",
                        resource_type=ResourceType.IMAGE_POSTER,
                        reason="Rotating monthly blood stock infographic",
                        max_matches=24,
                        check_interval="3h",
                        supersession=SupersessionPolicy.LATEST_WINS,
                        approved_by="project-owner",
                        approved_at=now,
                    )
                ]
            ),
        ),
    )

    # Homepage alias normalizes and matches exact URL approval
    res_home = evaluate_scope("https://pdn.gov.my/v2/index.php", scope)
    assert res_home.disposition == Disposition.APPROVED

    # Bounded single-directory rotating pattern matches PNG inside stokdarah/
    res_stock = evaluate_scope(
        "https://pdn.gov.my/v2/images/stokdarah/1stocknovember2026.png",
        scope,
        resource_type=ResourceType.IMAGE_POSTER,
    )
    assert res_stock.disposition == Disposition.APPROVED
    assert res_stock.rule_id == "blood-stock-rotating"

    # Single '*' must NOT cross subdirectories
    res_nested = evaluate_scope(
        "https://pdn.gov.my/v2/images/stokdarah/nested/2026.png",
        scope,
        resource_type=ResourceType.IMAGE_POSTER,
    )
    assert res_nested.disposition == Disposition.PENDING

    # Trailing-slash directory redirects match exact approved URLs cleanly
    res_slash = evaluate_scope(
        "https://pdn.gov.my/v2/kalkulator_tarikh_pendermaan_darah_utuh/",
        CrawlScopeConfig(
            approvals=ApprovalsSection(
                pages=ResourceApprovals(
                    urls=[
                        ApprovalUrlRule(
                            url="https://pdn.gov.my/v2/kalkulator_tarikh_pendermaan_darah_utuh",
                            reason="standalone tool",
                            approved_by="project-owner",
                            approved_at=now,
                        )
                    ]
                )
            )
        ),
    )
    assert res_slash.disposition == Disposition.APPROVED

    # Explicit URL exclusion overrides matching approval pattern
    res_excluded = evaluate_scope(
        "https://pdn.gov.my/v2/images/stokdarah/corrupt.png",
        scope,
        resource_type=ResourceType.IMAGE_POSTER,
    )
    assert res_excluded.disposition == Disposition.EXCLUDED
    assert res_excluded.reason == "explicit_url_exclusion"
