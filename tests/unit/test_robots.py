"""Unit tests for subfolder-scoped /v2/robots.txt parsing and evaluation."""

from __future__ import annotations

import pytest

from pdn_sync.crawling.robots import (
    RobotsFetchOutcome,
    RobotsUnavailableError,
    SubfolderRobotsPolicy,
)

OBSERVED_V2_ROBOTS = """
# Observed from https://pdn.gov.my/v2/robots.txt on 2026-11-24
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
"""


def test_subfolder_robots_scopes_rules_under_v2_and_allows_public_paths() -> None:
    policy = SubfolderRobotsPolicy.parse(OBSERVED_V2_ROBOTS, scope_prefix="/v2")

    assert not policy.is_allowed("https://pdn.gov.my/v2/administrator/")
    assert not policy.is_allowed("https://pdn.gov.my/v2/administrator/index.php")
    assert not policy.is_allowed("https://pdn.gov.my/v2/bin/script")
    assert not policy.is_allowed("https://pdn.gov.my/v2/components/com_foo")

    assert policy.is_allowed("https://pdn.gov.my/v2/")
    assert policy.is_allowed("https://pdn.gov.my/v2/index.php/perkhidmatan")
    assert policy.is_allowed("https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png")
    # Note: Joomla SEF /v2/index.php/component/... is distinct from /v2/components/
    assert policy.is_allowed("https://pdn.gov.my/v2/index.php/component/k2/item/76")


def test_root_403_tolerated_when_v2_robots_returns_200() -> None:
    outcome = RobotsFetchOutcome(
        root_robots_status=403,
        v2_robots_status=200,
        v2_robots_text=OBSERVED_V2_ROBOTS,
    )
    policy = SubfolderRobotsPolicy.from_fetch_outcome(outcome, scope_prefix="/v2")
    assert not policy.is_allowed("https://pdn.gov.my/v2/tmp/x")
    assert policy.is_allowed("https://pdn.gov.my/v2/")


@pytest.mark.parametrize("bad_status", [403, 404, 500, 503])
def test_v2_robots_unavailable_fails_closed(bad_status: int) -> None:
    outcome = RobotsFetchOutcome(
        root_robots_status=403,
        v2_robots_status=bad_status,
        v2_robots_text=None,
    )
    with pytest.raises(RobotsUnavailableError):
        SubfolderRobotsPolicy.from_fetch_outcome(outcome, scope_prefix="/v2")


def test_specific_user_agent_and_allow_longest_match_precedence() -> None:
    robots_text = """
User-agent: *
Disallow: /

User-agent: PDN-Knowledge-Sync
Disallow: /private/
Allow: /private/public-notice
Crawl-delay: 3
"""
    policy = SubfolderRobotsPolicy.parse(
        robots_text,
        scope_prefix="/v2",
        user_agent_token="PDN-Knowledge-Sync",
    )
    assert policy.crawl_delay == 3.0
    assert policy.is_allowed("https://pdn.gov.my/v2/")
    assert not policy.is_allowed("https://pdn.gov.my/v2/private/secret")
    assert policy.is_allowed("https://pdn.gov.my/v2/private/public-notice")


def test_empty_disallow_allows_all() -> None:
    policy = SubfolderRobotsPolicy.parse("User-agent: *\nDisallow:\n", scope_prefix="/v2")
    assert policy.is_allowed("https://pdn.gov.my/v2/anything")
