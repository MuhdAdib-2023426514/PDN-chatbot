"""Unit tests for URL normalization, host/path scope, SSRF guards, and JEvents exclusion."""

from __future__ import annotations

import pytest

from pdn_sync.crawling.url_policy import (
    UrlPolicyError,
    is_in_allowed_scope,
    is_jevents_url,
    normalize_url,
    validate_ssrf_safe,
    validate_target_url,
)


def test_normalize_url_collapses_slashes_and_homepage_aliases() -> None:
    assert normalize_url("https://pdn.gov.my/v2") == "https://pdn.gov.my/v2/"
    assert normalize_url("https://PDN.GOV.MY:443/v2/") == "https://pdn.gov.my/v2/"
    assert normalize_url("https://pdn.gov.my/v2/index.php#top") == "https://pdn.gov.my/v2/"
    assert (
        normalize_url("https://pdn.gov.my/v2/images//dokumen//2026/policy.pdf")
        == "https://pdn.gov.my/v2/images/dokumen/2026/policy.pdf"
    )


def test_normalize_url_strips_tracking_and_preserves_joomla_query_params() -> None:
    raw = (
        "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2"
        "?utm_source=fb&start=20&Itemid=101&fbclid=abc123#section"
    )
    assert (
        normalize_url(raw)
        == "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2?Itemid=101&start=20"
    )


def test_host_and_prefix_scope_boundaries() -> None:
    assert is_in_allowed_scope("https://pdn.gov.my/v2/")
    assert is_in_allowed_scope("https://pdn.gov.my/v2/index.php/perkhidmatan")
    assert not is_in_allowed_scope("https://pdn.gov.my/v20/index.php")
    assert not is_in_allowed_scope("https://pdn.gov.my/")
    assert not is_in_allowed_scope("https://evil-pdn.gov.my/v2/")
    assert not is_in_allowed_scope("https://pdn.gov.my.evil.com/v2/")


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://pdn.gov.my/v2/file.pdf",
        "https://user:pass@pdn.gov.my/v2/",
        "http://localhost/v2/",
        "http://127.0.0.1/v2/",
        "http://10.0.0.5/v2/",
        "http://172.16.0.1/v2/",
        "http://192.168.1.10/v2/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/v2/",
        "http://[::ffff:127.0.0.1]/v2/",
        "http://metadata.internal/v2/",
    ],
)
def test_ssrf_blocks_disallowed_schemes_hosts_and_private_ips(unsafe_url: str) -> None:
    with pytest.raises(UrlPolicyError):
        validate_ssrf_safe(unsafe_url)


def test_ssrf_blocks_dns_rebinding_to_private_ip() -> None:
    with pytest.raises(UrlPolicyError, match="ssrf_blocked_resolved_ip"):
        validate_ssrf_safe(
            "https://pdn.gov.my/v2/",
            resolve_dns=True,
            resolver=lambda _host: ["127.0.0.1"],
        )


def test_ssrf_allows_public_resolved_ip() -> None:
    validate_ssrf_safe(
        "https://pdn.gov.my/v2/",
        resolve_dns=True,
        resolver=lambda _host: ["1.1.1.1"],
    )


@pytest.mark.parametrize(
    "jevents_url",
    [
        "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-?Itemid=0",
        "https://pdn.gov.my/v2/index.php/component/jevents/eventdetail/123/-/kempen",
        "https://pdn.gov.my/v2/component/jevents/eventsbyweek/2026/11/24",
        "https://pdn.gov.my/v2/index.php?option=com_jevents&task=month.calendar",
    ],
)
def test_jevents_urls_are_detected_and_blocked_in_phase_0_to_3(jevents_url: str) -> None:
    assert is_jevents_url(jevents_url)
    with pytest.raises(UrlPolicyError, match="deferred_to_structured_schedule_phase"):
        validate_target_url(jevents_url, allow_jevents=False)


def test_validate_target_url_allows_valid_non_jevents_url() -> None:
    result = validate_target_url("https://pdn.gov.my/v2/index.php/perkhidmatan/derma-darah")
    assert result == "https://pdn.gov.my/v2/index.php/perkhidmatan/derma-darah"
