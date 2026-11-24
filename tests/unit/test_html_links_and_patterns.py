"""Unit tests for transient HTML link extraction and rotating-asset pattern proposals."""

from __future__ import annotations

from datetime import UTC, datetime

from pdn_sync.discovery.html_links import extract_page_discovery_metadata
from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryItem,
    LinkStatus,
    RecommendedAction,
    make_canonical_key,
)
from pdn_sync.discovery.patterns import propose_rotating_asset_patterns
from pdn_sync.domain.policy import Disposition, ResourceType, SupersessionPolicy


def test_extract_page_links_images_srcset_smartslider_and_css_bg() -> None:
    html = """
    <html>
      <head>
        <title>  Pusat Darah   Negara  </title>
        <link rel="canonical" href="https://pdn.gov.my/v2/" />
        <style>.banner { background-image: url('/v2/images/it_pdn/2026/bg-notice.png'); }</style>
      </head>
      <body>
        <main id="k2Container">
          <style>.inline-noise { color: red; }</style>
          <h1>Selamat Datang</h1>
          <a href="/v2/index.php/perkhidmatan">Perkhidmatan</a>
          <a href="mailto:info@pdn.gov.my">Email</a>
          <a href="javascript:void(0)">JS</a>
          <img src="/v2/images/stokdarah/1stockseptember2026.png" alt="Status Bekalan Darah" />
          <img data-desktop="/v2/images/SmartSlider3/2025/WaktuOperasiApril2024.png"
               srcset="/v2/images/SmartSlider3/2025/slide1.jpg 1x,
                       /v2/images/SmartSlider3/2025/slide2.jpg 2x" />
          <div style="background-image: url('/v2/images/it_pdn/2026/inline-poster.jpg')"></div>
          <form><input type="hidden" name="csrf" value="token-123" /></form>
        </main>
      </body>
    </html>
    """
    meta = extract_page_discovery_metadata(html, base_url="https://pdn.gov.my/v2/")
    assert meta.title == "Pusat Darah Negara"
    assert meta.canonical_url == "https://pdn.gov.my/v2/"
    assert meta.normalized_content_sha256 is not None

    urls = {link.resolved_url for link in meta.outbound_links}
    assert "https://pdn.gov.my/v2/index.php/perkhidmatan" in urls
    assert "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png" in urls
    assert "https://pdn.gov.my/v2/images/SmartSlider3/2025/WaktuOperasiApril2024.png" in urls
    assert "https://pdn.gov.my/v2/images/SmartSlider3/2025/slide2.jpg" in urls
    assert "https://pdn.gov.my/v2/images/it_pdn/2026/bg-notice.png" in urls
    assert "https://pdn.gov.my/v2/images/it_pdn/2026/inline-poster.jpg" in urls
    assert not any(u.startswith("mailto:") or u.startswith("javascript:") for u in urls)

    # Changing inline <style> or hidden CSRF form token does not change dedup hash
    html_mutated = html.replace("color: red;", "color: blue;").replace("token-123", "token-999")
    meta2 = extract_page_discovery_metadata(html_mutated, base_url="https://pdn.gov.my/v2/")
    assert meta2.normalized_content_sha256 == meta.normalized_content_sha256


def test_propose_rotating_asset_patterns_generates_bounded_rules() -> None:
    now = datetime.now(UTC)

    def _item(url: str, cat: DiscoveryCategory, rtype: ResourceType) -> InventoryItem:
        return InventoryItem(
            run_id="run-1",
            canonical_key=make_canonical_key(url),
            url=url,
            original_url=url,
            link_status=LinkStatus.OK,
            discovered_at_utc=now,
            depth=1,
            resource_type=rtype,
            category=cat,
            discovery_method="html_img",
            disposition=Disposition.PENDING,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="test",
            monitoring_interval="3h",
        )

    items = [
        _item(
            "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png",
            DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            ResourceType.IMAGE_POSTER,
        ),
        _item(
            "https://pdn.gov.my/v2/images/SmartSlider3/2025/banner1.png",
            DiscoveryCategory.POSTER_BANNER,
            ResourceType.IMAGE_POSTER,
        ),
        _item(
            "https://pdn.gov.my/v2/images/SmartSlider3/2025/banner2.png",
            DiscoveryCategory.POSTER_BANNER,
            ResourceType.IMAGE_POSTER,
        ),
        _item(
            "https://pdn.gov.my/v2/images/sampledata/logos/logo.png",
            DiscoveryCategory.DECORATIVE,
            ResourceType.IMAGE_POSTER,
        ),
    ]

    proposals = propose_rotating_asset_patterns(items)
    patterns = {p.pattern: p for p in proposals}

    assert "/v2/images/stokdarah/*.png" in patterns
    assert patterns["/v2/images/stokdarah/*.png"].supersession == SupersessionPolicy.LATEST_WINS
    assert "/v2/images/SmartSlider3/2025/*.png" in patterns
    assert patterns["/v2/images/SmartSlider3/2025/*.png"].match_count == 2
    # Decorative assets must not be proposed, and no broad /** wildcards are emitted
    assert all("sampledata" not in p.pattern and "**" not in p.pattern for p in proposals)
