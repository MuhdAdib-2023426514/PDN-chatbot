"""Unit tests for LocalBlobStore and Gate-A-enforced ApprovedContentFetcher."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pdn_sync.config import AppSettings, load_crawl_scope
from pdn_sync.crawling.fetcher import ApprovedContentFetcher, FetchBlockedError
from pdn_sync.crawling.robots import SubfolderRobotsPolicy
from pdn_sync.discovery.scope_manager import GateABlockedError
from pdn_sync.domain.policy import PolitenessPolicy, ResourceType
from pdn_sync.persistence.blob_store import LocalBlobStore


def _write_test_scope(path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    path.write_text(
        f"""
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
    - id: exclude-jevents
      pattern: "/v2/index.php/component/jevents/**"
      reason: deferred_to_structured_schedule_phase
approvals:
  pages:
    urls:
      - url: "https://pdn.gov.my/v2/"
        reason: approved
        check_interval: 3h
        approved_by: tester
        approved_at: "{now}"
      - url: "https://pdn.gov.my/v2/index.php/redirect-evil"
        reason: approved_for_redirect_test
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
      - url: "https://pdn.gov.my/v2/index.php/flaky"
        reason: approved_for_retry_test
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
    patterns: []
  image_posters:
    urls: []
    patterns: []
  documents:
    urls:
      - url: "https://pdn.gov.my/v2/images/dokumen/big.pdf"
        reason: approved_for_cap_test
        check_interval: 24h
        approved_by: tester
        approved_at: "{now}"
    patterns: []
""",
        encoding="utf-8",
    )


def test_local_blob_store_deduplicates_and_is_immutable(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    payload = b"Pusat Darah Negara immutable snapshot bytes"

    m1 = store.put_bytes(payload)
    m2 = store.put_bytes(payload)

    assert m1.sha256 == m2.sha256
    assert m1.deduplicated is False
    assert m2.deduplicated is True
    assert store.exists(m1.sha256)
    assert store.get_bytes(m1.sha256) == payload
    assert m1.path == tmp_path / "blobs" / m1.sha256[:2] / m1.sha256


def test_fetcher_enforces_gate_a_redirects_byte_caps_304_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "crawl_scope.yaml"
    _write_test_scope(scope_file)
    scope, _ = load_crawl_scope(scope_file)
    settings = AppSettings(
        contact_email="ops@pdn-sync.org",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
        scope_file=scope_file,
    )
    robots = SubfolderRobotsPolicy.parse("User-agent: *\nDisallow: /administrator/\n")
    store = LocalBlobStore(tmp_path / "blobs")

    calls: list[str] = []
    flaky_attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == "https://pdn.gov.my/v2/":
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304, headers={"etag": '"v1"'})
            return httpx.Response(
                200,
                content=b"<html><title>PDN</title></html>",
                headers={"content-type": "text/html", "etag": '"v1"'},
            )
        if url == "https://pdn.gov.my/v2/index.php/redirect-evil":
            return httpx.Response(302, headers={"location": "https://evil.example/payload"})
        if url == "https://pdn.gov.my/v2/images/dokumen/big.pdf":
            return httpx.Response(
                200, content=b"X" * 2048, headers={"content-type": "application/pdf"}
            )
        if url == "https://pdn.gov.my/v2/index.php/flaky":
            flaky_attempts["count"] += 1
            if flaky_attempts["count"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, content=b"<html><title>Recovered</title></html>")
        raise AssertionError(f"Unexpected request: {url}")

    politeness = PolitenessPolicy(max_document_bytes=1024)
    fetcher = ApprovedContentFetcher(
        settings=settings,
        scope=scope,
        scope_path=scope_file,
        politeness=politeness,
        robots=robots,
        blob_store=store,
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _s: None,
        resolve_dns=False,
    )

    # 1. Unapproved URL blocked before any network call
    with pytest.raises(GateABlockedError):
        fetcher.fetch("https://pdn.gov.my/v2/index.php/unapproved-page")
    assert calls == []

    # 2. JEvents URL blocked before any network call
    with pytest.raises(FetchBlockedError):
        fetcher.fetch("https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-")
    assert calls == []

    # 3. Approved homepage fetches & saves blob, then conditional GET gets 304 Not Modified
    res1 = fetcher.fetch("https://pdn.gov.my/v2/", resource_type=ResourceType.PAGE)
    assert res1.outcome == "fetched"
    assert res1.blob_sha256 is not None
    assert store.exists(res1.blob_sha256)

    res2 = fetcher.fetch(
        "https://pdn.gov.my/v2/",
        resource_type=ResourceType.PAGE,
        etag=res1.etag,
    )
    assert res2.outcome == "not_modified"
    assert res2.http_status == 304

    # 4. Off-domain redirect blocked at hop
    with pytest.raises(FetchBlockedError, match="Redirect blocked"):
        fetcher.fetch("https://pdn.gov.my/v2/index.php/redirect-evil")

    # 5. Streaming byte cap aborts oversized PDF (> 1024 bytes)
    big_res = fetcher.fetch(
        "https://pdn.gov.my/v2/images/dokumen/big.pdf",
        resource_type=ResourceType.DOCUMENT,
    )
    assert big_res.outcome == "exceeded_byte_cap"
    assert big_res.blob_sha256 is None

    # 6. Transient RemoteProtocolError recovers on retry
    flaky_res = fetcher.fetch("https://pdn.gov.my/v2/index.php/flaky")
    assert flaky_res.outcome == "fetched"
    assert flaky_attempts["count"] == 2
