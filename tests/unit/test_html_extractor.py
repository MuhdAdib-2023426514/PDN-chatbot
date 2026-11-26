"""Unit tests for Phase 2a HTML extractor (Warp, Joomla/K2, links, media_refs, PDPA staff)."""

from __future__ import annotations

from datetime import UTC, datetime

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import Disposition
from pdn_sync.domain.scope import CrawlScopeConfig
from pdn_sync.extractors.html_extractor import extract_html


def test_html_extractor_strips_styles_preserves_tables_drive_links_and_unapproved_media_refs() -> (
    None
):
    html = b"""
    <html>
      <head><title>Perkhidmatan H&amp;I PDN</title></head>
      <body>
        <header><nav><a href="/v2/">Menu Chrome</a></nav></header>
        <main id="k2Container">
          <style>.faq-box { color: red; }</style>
          <script>console.log('ignore me');</script>
          <h1>Bahagian Histokompatibiliti &amp; Immunogenetik</h1>
          <p>
            Muat turun
            <a href="https://drive.google.com/file/d/abc123/view">Garis Panduan H&amp;I</a>.
          </p>
          <img src="/v2/images/unapproved_chart.png" alt="Carta Ujian" />
          <table>
            <thead><tr><th>Ujian</th><th>Tempoh</th></tr></thead>
            <tbody><tr><td>HLA Typing</td><td>7 hari bekerja</td></tr></tbody>
          </table>
          <form><input type="hidden" name="csrf" value="secret-token" /></form>
        </main>
        <footer>Copyright PDN</footer>
      </body>
    </html>
    """
    scope = CrawlScopeConfig()
    res = extract_html(
        raw_html=html,
        source_url="https://pdn.gov.my/v2/index.php/perkhidmatan/hni",
        resource_id="res_hni",
        resource_version_id="ver_1",
        content_hash="a" * 64,
        category=DiscoveryCategory.SERVICES,
        scope=scope,
        captured_at_utc=datetime.now(UTC),
    )

    joined_text = "\n".join(b.text for b in res.text_blocks)
    assert "color: red" not in joined_text
    assert "console.log" not in joined_text
    assert "Menu Chrome" not in joined_text
    assert "[Garis Panduan H&I](https://drive.google.com/file/d/abc123/view)" in joined_text

    assert len(res.tables) == 1
    assert res.tables[0].headers == ["Ujian", "Tempoh"]
    assert res.tables[0].rows == [["HLA Typing", "7 hari bekerja"]]

    # Unapproved inline image recorded as media_ref with scope_status=pending (never fetched)
    assert len(res.media_refs) == 1
    assert res.media_refs[0].url == "https://pdn.gov.my/v2/images/unapproved_chart.png"
    assert res.media_refs[0].scope_status == Disposition.PENDING


def test_html_extractor_handles_warp_homepage_sections() -> None:
    html = b"""
    <html>
      <head><title>Pusat Darah Negara</title></head>
      <body>
        <div class="tm-top-b">
          <h2>Status Bekalan Darah</h2>
          <img src="/v2/images/stokdarah/1stockseptember2026.png" alt="Stok Darah" />
        </div>
        <div class="tm-bottom-b">
          <p>Hubungi talian utama PDN: 03-2613 2688</p>
        </div>
      </body>
    </html>
    """
    res = extract_html(
        raw_html=html,
        source_url="https://pdn.gov.my/v2/",
        resource_id="res_home",
        resource_version_id="ver_1",
        content_hash="b" * 64,
        category=DiscoveryCategory.HOMEPAGE,
        scope=CrawlScopeConfig(),
        captured_at_utc=datetime.now(UTC),
    )
    assert res.provenance["layout_mode"] == "warp_homepage_sections"
    assert any("03-2613 2688" in b.text for b in res.text_blocks)
    assert len(res.media_refs) == 1


def test_html_extractor_pdpa_staff_directory_strips_photos_and_minimizes_fields() -> None:
    html = b"""
    <html>
      <head><title>Dr. Contoh Pegawai</title></head>
      <body>
        <main>
          <h1>Dr. Contoh Pegawai</h1>
          <img src="/v2/images/staff/dr-contoh-photo.jpg" alt="Officer Portrait" />
          <dl>
            <dt>Jawatan</dt><dd>Ketua Seksyen Pengurusan Penderma</dd>
            <dt>Bahagian</dt><dd>Bahagian Perolehan Darah</dd>
            <dt>Telefon</dt><dd>03-2613 2600</dd>
            <dt>Emel</dt><dd>contoh@moh.gov.my</dd>
          </dl>
        </main>
      </body>
    </html>
    """
    res = extract_html(
        raw_html=html,
        source_url="https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/29-direktori-ketua-seksyen/1-dr-contoh",
        resource_id="res_staff",
        resource_version_id="ver_1",
        content_hash="c" * 64,
        category=DiscoveryCategory.STAFF_DIRECTORY,
        scope=CrawlScopeConfig(),
        captured_at_utc=datetime.now(UTC),
    )
    assert res.provenance["pdpa_minimized"] is True
    assert res.provenance["officer_photos_stripped"] is True
    assert res.media_refs == []
    assert len(res.tables) == 1
    assert res.tables[0].rows[0][0] == "Dr. Contoh Pegawai"
    assert res.tables[0].rows[0][1] == "Ketua Seksyen Pengurusan Penderma"
    assert res.tables[0].rows[0][4] == "contoh@moh.gov.my"
