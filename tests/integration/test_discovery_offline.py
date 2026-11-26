"""Offline integration tests for Phase 1 discovery-only crawler and exporters."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import httpx
import pytest

from pdn_sync.config import AppSettings, compute_file_sha256
from pdn_sync.discovery.crawler import DiscoveryOptions, run_discovery
from pdn_sync.discovery.models import DiscoveryCategory, LinkStatus
from pdn_sync.domain.policy import Disposition

REPO_ROOT = Path(__file__).resolve().parents[2]

V2_ROBOTS = """
User-agent: *
Disallow: /administrator/
Disallow: /bin/
"""

HOMEPAGE_HTML = """
<html>
  <head><title>Laman Utama PDN</title></head>
  <body>
    <div class="tm-top-b">
      <img src="/v2/images/stokdarah/1stockseptember2026.png" alt="Status Bekalan Darah" />
      <img data-desktop="/v2/images/SmartSlider3/2025/WaktuOperasiApril2024.png"
           alt="Waktu Operasi" />
      <img src="/v2/images/sampledata/logos/pdn-logo.png" alt="Logo" />
    </div>
    <nav>
      <a href="/v2/index.php/informasi/bahan-pendidikan-2/item/76-soalan-lazim">Soalan Lazim</a>
      <a href="/v2/index.php/component/k2/item/76">FAQ Alias</a>
      <a href="/v2/index.php/informasi/bahan-pendidikan?start=10">Page 2</a>
      <a href="/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it">Direktori IT</a>
      <a href="/v2/index.php/component/jevents/monthcalendar/2026/11/-?Itemid=0">Takwim Kempen</a>
      <a href="/v2/index.php?option=com_jevents&task=month.calendar">JEvents Query</a>
      <a href="/v2/index.php?option=com_content&view=article&id=3&Itemid=106">Broken Link</a>
      <a href="/v2/images//dokumen/2026/guide.pdf">Garis Panduan PDF</a>
      <a href="/v2/images/dokumen/2026/borang.doc">Borang Legacy DOC</a>
      <a href="/v2/index.php/redirect-escape">Redirect Escape</a>
      <a href="https://moh.gov.my/">KKM External</a>
    </nav>
  </body>
</html>
"""

FAQ_HTML = """
<html>
  <head><title>Soalan Lazim Pendermaan Darah</title></head>
  <body>
    <main id="k2Container">
      <h1>Soalan Lazim</h1>
      <p>Syarat asas pendermaan darah adalah berumur 18 hingga 60 tahun.</p>
    </main>
  </body>
</html>
"""

STAFF_LISTING_HTML = """
<html>
  <head><title>Direktori Pegawai Bahagian IT</title></head>
  <body>
    <main>
      <a href="/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it/item/99-encik-ali">
        Encik Ali
      </a>
    </main>
  </body>
</html>
"""

PUB_PAGE_2_HTML = """
<html>
  <head><title>Bahan Pendidikan - Muka 2</title></head>
  <body>
    <main>
      <a href="/v2/index.php/informasi/bahan-pendidikan?start=20">Page 3</a>
    </main>
  </body>
</html>
"""


def _make_mock_site() -> tuple[httpx.MockTransport, list[tuple[str, str]]]:
    recorded_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        recorded_requests.append((request.method, url))

        if url == "https://pdn.gov.my/robots.txt":
            return httpx.Response(403, text="Forbidden")
        if url == "https://pdn.gov.my/v2/robots.txt":
            return httpx.Response(200, text=V2_ROBOTS, headers={"content-type": "text/plain"})
        if url == "https://pdn.gov.my/v2/sitemap.xml":
            return httpx.Response(404, text="Not Found")

        if url == "https://pdn.gov.my/v2/":
            return httpx.Response(200, text=HOMEPAGE_HTML, headers={"content-type": "text/html"})
        if url in {
            "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2/item/76-soalan-lazim",
            "https://pdn.gov.my/v2/index.php/component/k2/item/76",
        }:
            return httpx.Response(200, text=FAQ_HTML, headers={"content-type": "text/html"})
        if url == "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it":
            return httpx.Response(
                200, text=STAFF_LISTING_HTML, headers={"content-type": "text/html"}
            )
        if url == "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan?start=10":
            return httpx.Response(200, text=PUB_PAGE_2_HTML, headers={"content-type": "text/html"})
        if url == "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan?start=20":
            return httpx.Response(
                200,
                text="<html><title>Page 3</title></html>",
                headers={"content-type": "text/html"},
            )
        if url == "https://pdn.gov.my/v2/index.php/redirect-escape":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        if "option=com_content" in url:
            return httpx.Response(404, text="Not Found", headers={"content-type": "text/html"})

        # Selective HEAD responses for candidate posters & documents (fail test if GET is used!)
        if url.endswith(".png"):
            assert request.method == "HEAD", f"Discovery must never GET image bytes: {url}"
            return httpx.Response(
                200, headers={"content-type": "image/png", "content-length": "42000"}
            )
        if url.endswith(".pdf"):
            assert request.method == "HEAD", f"Discovery must never GET PDF bytes: {url}"
            return httpx.Response(
                200, headers={"content-type": "application/pdf", "content-length": "125000"}
            )
        if url.endswith(".doc"):
            assert request.method == "HEAD", f"Discovery must never GET DOC bytes: {url}"
            return httpx.Response(
                200, headers={"content-type": "application/msword", "content-length": "84000"}
            )

        raise AssertionError(f"Unexpected request during discovery: {request.method} {url}")

    return httpx.MockTransport(handler), recorded_requests


_PHASE1_BASELINE_SCOPE_YAML = """\
schema_version: 1
allowed_host: pdn.gov.my
allowed_path_prefix: /v2
exclusions:
  urls: []
  patterns:
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


def _setup_temp_env(tmp_path: Path) -> tuple[Path, Path, Path, Path, AppSettings]:
    scope_file = tmp_path / "crawl_scope.yaml"
    policy_file = tmp_path / "ingestion_policy.yaml"
    scope_file.write_text(_PHASE1_BASELINE_SCOPE_YAML, encoding="utf-8")
    shutil.copyfile(REPO_ROOT / "config/ingestion_policy.yaml", policy_file)
    db_path = tmp_path / "pdn_sync.db"
    out_dir = tmp_path / "discovery"

    settings = AppSettings(
        contact_email="tester@pdn-sync.org",
        operator_id="test-owner",
        source_policy_acknowledged=True,
        live_crawl_enabled=True,
        scope_file=scope_file,
        ingestion_policy_file=policy_file,
        sqlite_path=db_path,
        data_dir=tmp_path,
    )
    return scope_file, policy_file, db_path, out_dir, settings


def test_discovery_only_full_offline_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file, policy_file, db_path, out_dir, settings = _setup_temp_env(tmp_path)
    scope_hash_before = compute_file_sha256(scope_file)

    transport, requests = _make_mock_site()
    result = run_discovery(
        options=DiscoveryOptions(
            live=True,
            enable_head=True,
            max_listing_pages=1,
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

    assert result.status == "completed"
    # Discovery NEVER modifies config/crawl_scope.yaml
    assert compute_file_sha256(scope_file) == scope_hash_before

    # All 4 required deliverables exist
    assert (out_dir / "url_inventory.json").is_file()
    assert (out_dir / "url_inventory.csv").is_file()
    assert (out_dir / "discovery_report.md").is_file()
    assert (out_dir / "crawl_scope.proposed.yaml").is_file()

    # Verify strict request invariants
    requested_urls = [u for _, u in requests]
    assert not any("jevents" in u.lower() for u in requested_urls)
    assert not any("99-encik-ali" in u.lower() for u in requested_urls)
    assert not any("pdn-logo.png" in u.lower() for u in requested_urls)
    assert not any("moh.gov.my" in u.lower() for u in requested_urls)
    assert not any("evil.example" in u.lower() for u in requested_urls)

    # Candidate posters & PDFs were checked via HEAD only
    head_urls = {u for m, u in requests if m == "HEAD"}
    assert "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png" in head_urls
    assert "https://pdn.gov.my/v2/images/dokumen/2026/guide.pdf" in head_urls
    assert "https://pdn.gov.my/v2/images/dokumen/2026/borang.doc" in head_urls

    # Inspect JSON envelope & items
    envelope_data = json.loads((out_dir / "url_inventory.json").read_text(encoding="utf-8"))
    assert envelope_data["schema_version"] == 1
    assert envelope_data["run_id"] == result.run_id
    assert envelope_data["scope_file_hash"] == scope_hash_before

    items_by_url = {it.url: it for it in result.items}

    # JEvents logged as excluded without fetching
    jevents_item = items_by_url[
        "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-?Itemid=0"
    ]
    assert jevents_item.category == DiscoveryCategory.JEVENTS_DEFERRED
    assert jevents_item.disposition == Disposition.EXCLUDED
    assert jevents_item.link_status == LinkStatus.SKIPPED_EXCLUDED

    # K2 duplicate alias points to human-readable menu URL
    k2_alias = items_by_url["https://pdn.gov.my/v2/index.php/component/k2/item/76"]
    assert (
        k2_alias.duplicate_of
        == "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2/item/76-soalan-lazim"
    )

    # Broken 404 link captured
    broken = items_by_url[
        "https://pdn.gov.my/v2/index.php?Itemid=106&id=3&option=com_content&view=article"
    ]
    assert broken.http_status == 404
    assert broken.link_status == LinkStatus.BROKEN

    # Staff leaf profile inventoried as staff_directory without fetching
    officer = items_by_url[
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it/item/99-encik-ali"
    ]
    assert officer.category == DiscoveryCategory.STAFF_DIRECTORY
    assert officer.link_status == LinkStatus.UNFETCHED_ASSET
    assert officer.http_status is None

    # max_listing_pages=1 stopped pagination before fetching start=20
    assert "max_listing_pages" in result.bounds_hit
    assert not any("start=20" in u for u in requested_urls)

    # Verify CSV contains all required headers
    with (out_dir / "url_inventory.csv").open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(result.items)


def test_discovery_no_head_option_skips_all_head_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file, policy_file, db_path, out_dir, settings = _setup_temp_env(tmp_path)
    transport, requests = _make_mock_site()

    result = run_discovery(
        options=DiscoveryOptions(
            live=True,
            enable_head=False,
            max_pages=2,
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
    assert result.status == "completed"
    assert not any(m == "HEAD" for m, _ in requests)
    assert "max_pages" in result.bounds_hit


def test_discovery_anomaly_growth_factor_aborts_runaway_rediscovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    scope_file, policy_file, db_path, out_dir, settings = _setup_temp_env(tmp_path)

    # Run 1: small site with 1 page (homepage only, no links)
    def small_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt") and "/v2/" not in url:
            return httpx.Response(403)
        if url.endswith("/v2/robots.txt"):
            return httpx.Response(200, text=V2_ROBOTS)
        if url.endswith("/v2/sitemap.xml"):
            return httpx.Response(404)
        return httpx.Response(200, text="<html><title>Small</title></html>")

    r1 = run_discovery(
        options=DiscoveryOptions(
            live=True,
            enable_head=False,
            output_dir=out_dir,
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=httpx.MockTransport(small_handler),
        sleep_fn=lambda _s: None,
    )
    assert r1.status == "completed"
    assert len(r1.items) == 1

    # Run 2: sudden explosion (> 3x previous count of 1) triggers aborted_anomaly
    transport2, _ = _make_mock_site()
    r2 = run_discovery(
        options=DiscoveryOptions(
            live=True,
            enable_head=False,
            anomaly_growth_factor=3.0,
            output_dir=out_dir,
            scope_file=scope_file,
            policy_file=policy_file,
            sqlite_path=db_path,
            resolve_dns=False,
        ),
        settings=settings,
        transport=transport2,
        sleep_fn=lambda _s: None,
    )
    assert r2.status == "aborted_anomaly"
    assert "anomaly_growth_factor" in r2.bounds_hit
