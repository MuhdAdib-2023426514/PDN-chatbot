"""Unit tests for the fixed 18-row URL/asset classification table (AGENTS.md Section 6)."""

from __future__ import annotations

import pytest

from pdn_sync.discovery.classifier import (
    classify_url,
    extract_k2_item_id,
    is_staff_directory_leaf_profile,
)
from pdn_sync.discovery.models import DiscoveryCategory, RecommendedAction
from pdn_sync.domain.policy import ResourceType


@pytest.mark.parametrize(
    ("url", "expected_cat", "expected_type", "expected_action"),
    [
        (
            "https://pdn.gov.my/v2/index.php/component/jevents/monthcalendar/2026/11/-?Itemid=0",
            DiscoveryCategory.JEVENTS_DEFERRED,
            ResourceType.PAGE,
            RecommendedAction.EXCLUDED,
        ),
        (
            "https://pdn.gov.my/v2/index.php?option=com_jevents&task=month.calendar",
            DiscoveryCategory.JEVENTS_DEFERRED,
            ResourceType.PAGE,
            RecommendedAction.EXCLUDED,
        ),
        (
            "https://pdn.gov.my/v2/administrator/index.php",
            DiscoveryCategory.SYSTEM,
            ResourceType.PAGE,
            RecommendedAction.EXCLUDED,
        ),
        (
            "https://pdn.gov.my/v2/index.php/informasi?format=feed",
            DiscoveryCategory.SYSTEM,
            ResourceType.PAGE,
            RecommendedAction.EXCLUDED,
        ),
        (
            "https://moh.gov.my/index.php",
            DiscoveryCategory.EXTERNAL,
            ResourceType.EXTERNAL_REF,
            RecommendedAction.EXCLUDED,
        ),
        (
            "https://pdn.gov.my/v2/",
            DiscoveryCategory.HOMEPAGE,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/index.php/maklumat-korporat/profil-pdn",
            DiscoveryCategory.CORPORATE,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/index.php/perkhidmatan/pendermaan-darah",
            DiscoveryCategory.SERVICES,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2/soalan-lazim",
            DiscoveryCategory.FAQ,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/index.php/penerbitan/laporan-tahunan",
            DiscoveryCategory.PUBLICATIONS,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/index.php/hubungi-kami/pusat-derma-darah-statik",
            DiscoveryCategory.CONTACT_STATIC_CENTERS,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/kalkulator_tarikh_pendermaan_darah_utuh/",
            DiscoveryCategory.STANDALONE_TOOL,
            ResourceType.PAGE,
            RecommendedAction.REVIEW,
        ),
        (
            "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it",
            DiscoveryCategory.STAFF_DIRECTORY,
            ResourceType.PAGE,
            RecommendedAction.RECOMMENDED_EXCLUDE,
        ),
        (
            "https://pdn.gov.my/v2/images/stokdarah/1stockseptember2026.png",
            DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            ResourceType.IMAGE_POSTER,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/images/SmartSlider3/2025/WaktuOperasiApril2024.png",
            DiscoveryCategory.POSTER_BANNER,
            ResourceType.IMAGE_POSTER,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/images/it_pdn/2026/notis-mpox.jpg",
            DiscoveryCategory.POSTER_BANNER,
            ResourceType.IMAGE_POSTER,
            RecommendedAction.RECOMMENDED_APPROVE,
        ),
        (
            "https://pdn.gov.my/v2/images/Kemudahan_PDN/ruang-menunggu.jpg",
            DiscoveryCategory.FACILITY_PHOTO,
            ResourceType.IMAGE_POSTER,
            RecommendedAction.REVIEW,
        ),
        (
            "https://pdn.gov.my/v2/images/sampledata/logos/pdn-logo.png",
            DiscoveryCategory.DECORATIVE,
            ResourceType.IMAGE_POSTER,
            RecommendedAction.RECOMMENDED_EXCLUDE,
        ),
        (
            "https://pdn.gov.my/v2/images/dokumen/2026/policy.pdf",
            DiscoveryCategory.DOCUMENT_PDF,
            ResourceType.DOCUMENT,
            RecommendedAction.REVIEW,
        ),
        (
            "https://pdn.gov.my/v2/images/dokumen/2026/borang.doc",
            DiscoveryCategory.DOCUMENT_OFFICE,
            ResourceType.DOCUMENT,
            RecommendedAction.REVIEW,
        ),
        (
            "https://pdn.gov.my/v2/index.php/some-new-section/page",
            DiscoveryCategory.UNCATEGORIZED,
            ResourceType.PAGE,
            RecommendedAction.REVIEW,
        ),
    ],
)
def test_fixed_classification_table(
    url: str,
    expected_cat: DiscoveryCategory,
    expected_type: ResourceType,
    expected_action: RecommendedAction,
) -> None:
    result = classify_url(url)
    assert result.category == expected_cat
    assert result.resource_type == expected_type
    assert result.recommended_action == expected_action


def test_legacy_doc_is_explicitly_flagged() -> None:
    res = classify_url("https://pdn.gov.my/v2/images/dokumen/2026/borang.doc")
    assert res.category == DiscoveryCategory.DOCUMENT_OFFICE
    assert "legacy_binary_doc" in res.reason


def test_staff_directory_listing_vs_leaf_profile() -> None:
    # Listing pages -> False (traverse to inventory links)
    assert not is_staff_directory_leaf_profile(
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai"
    )
    assert not is_staff_directory_leaf_profile(
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it"
    )
    assert not is_staff_directory_leaf_profile(
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it?start=20"
    )
    # Leaf officer profile pages -> True (never fetch during discovery)
    assert is_staff_directory_leaf_profile(
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it/item/99-dr-ali"
    )
    assert is_staff_directory_leaf_profile(
        "https://pdn.gov.my/v2/index.php/hubungi-kami/direktori-pegawai/bahagian-it/dr-ali"
    )


def test_extract_k2_item_id() -> None:
    assert extract_k2_item_id("https://pdn.gov.my/v2/index.php/component/k2/item/76") == "76"
    assert (
        extract_k2_item_id(
            "https://pdn.gov.my/v2/index.php/informasi/bahan-pendidikan-2/item/76-soalan-lazim"
        )
        == "76"
    )
    assert extract_k2_item_id("https://pdn.gov.my/v2/index.php/perkhidmatan") is None
