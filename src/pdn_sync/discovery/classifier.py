"""Fixed 18-row URL/asset classification table (AGENTS.md Section 6)."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, urlsplit

from pdn_sync.crawling.url_policy import is_in_allowed_scope, is_jevents_url
from pdn_sync.discovery.models import DiscoveryCategory, RecommendedAction
from pdn_sync.domain.policy import ResourceType

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp", ".avif"}
)
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
OFFICE_EXTENSIONS: frozenset[str] = frozenset({".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"})

_SYSTEM_PREFIXES: tuple[str, ...] = (
    "/administrator",
    "/bin",
    "/cache",
    "/cli",
    "/components",
    "/includes",
    "/installation",
    "/language",
    "/layouts",
    "/libraries",
    "/logs",
    "/modules",
    "/plugins",
    "/tmp",
    "/index.php/registration",
    "/index.php/component/search",
    "/index.php/component/users",
    "/index.php/component/mailto",
    "/component/search",
    "/component/users",
    "/component/mailto",
)

_SYSTEM_QUERY_EXACT: frozenset[tuple[str, str]] = frozenset(
    {
        ("format", "feed"),
        ("format", "opensearch"),
        ("tmpl", "component"),
        ("print", "1"),
    }
)

_IT_PDN_YEAR_RE = re.compile(r"^/images/it_pdn/\d{4}/", flags=re.IGNORECASE)
_K2_ITEM_ID_RE = re.compile(r"/(?:component/k2/)?item/(\d+)(?:-[^/?#]*)?", flags=re.IGNORECASE)
_DECORATIVE_ICON_RE = re.compile(
    r"(?:favicon|apple-touch-icon|social|facebook|twitter|instagram|youtube|tiktok|whatsapp|icon|logo|spinner|loader)",
    flags=re.IGNORECASE,
)

# Categories whose assets should receive a selective polite HEAD check during discovery
SELECTIVE_HEAD_CATEGORIES: frozenset[DiscoveryCategory] = frozenset(
    {
        DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
        DiscoveryCategory.POSTER_BANNER,
        DiscoveryCategory.FACILITY_PHOTO,
        DiscoveryCategory.DOCUMENT_PDF,
        DiscoveryCategory.DOCUMENT_OFFICE,
    }
)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category: DiscoveryCategory
    resource_type: ResourceType
    recommended_action: RecommendedAction
    reason: str
    monitoring_interval: str
    mime_hint: str | None


def _strip_v2_prefix(path: str, prefix: str = "/v2") -> str:
    clean_prefix = prefix.rstrip("/")
    if path == clean_prefix:
        return "/"
    if path.startswith(f"{clean_prefix}/"):
        return path[len(clean_prefix) :]
    return path


def _path_matches_prefix(rel_path: str, target_prefix: str) -> bool:
    rel_l = rel_path.lower()
    pref_l = target_prefix.rstrip("/").lower()
    return rel_l == pref_l or rel_l.startswith(f"{pref_l}/")


def infer_mime_hint(url: str) -> str | None:
    path = urlsplit(url).path
    guessed, _ = mimetypes.guess_type(path)
    return guessed


def extract_k2_item_id(url: str) -> str | None:
    """Extract numeric K2 item ID from /component/k2/item/<id> or menu-aliased .../item/<id>."""
    path = urlsplit(url).path
    match = _K2_ITEM_ID_RE.search(path)
    return match.group(1) if match else None


def is_staff_directory_leaf_profile(
    url: str,
    *,
    allowed_path_prefix: str = "/v2",
) -> bool:
    """Return True if a staff_directory URL looks like an individual officer profile page.

    Listing pages (/direktori-pegawai, /direktori-pegawai/<unit>, or paginated ?start=...)
    return False so discovery traverses listings and inventories every officer URL without
    fetching individual officer profiles.
    """
    parts = urlsplit(url)
    rel = _strip_v2_prefix(parts.path, allowed_path_prefix).rstrip("/").lower()
    base = "/index.php/hubungi-kami/direktori-pegawai"
    alt_base = "/hubungi-kami/direktori-pegawai"
    if rel.startswith(base):
        suffix = rel[len(base) :].strip("/")
    elif rel.startswith(alt_base):
        suffix = rel[len(alt_base) :].strip("/")
    else:
        return False

    if not suffix:
        return False

    segments = [seg for seg in suffix.split("/") if seg]
    if "item" in segments:
        return True

    # Leaf officer profile under unit directory (e.g. <unit>/<id-or-officer-slug>)
    # while keeping paginated unit listings (?start=...) as listings
    query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if "start" in query_keys or "limitstart" in query_keys:
        return False

    return len(segments) >= 2


def classify_url(
    url: str,
    *,
    allowed_host: str = "pdn.gov.my",
    allowed_path_prefix: str = "/v2",
) -> ClassificationResult:
    """Classify a URL deterministically using the fixed 18-row table (first match wins)."""
    parts = urlsplit(url)
    mime_hint = infer_mime_hint(url)

    # 1. Hard Phase 1-3 JEvents exclusion
    if is_jevents_url(url):
        return ClassificationResult(
            category=DiscoveryCategory.JEVENTS_DEFERRED,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.EXCLUDED,
            reason="deferred_to_structured_schedule_phase",
            monitoring_interval="none",
            mime_hint=mime_hint or "text/html",
        )

    # 3. External host or outside /v2 (checked before rel_path rules so external URLs never leak in)
    if not is_in_allowed_scope(
        url,
        allowed_host=allowed_host,
        allowed_path_prefix=allowed_path_prefix,
    ):
        return ClassificationResult(
            category=DiscoveryCategory.EXTERNAL,
            resource_type=ResourceType.EXTERNAL_REF,
            recommended_action=RecommendedAction.EXCLUDED,
            reason="external_host_or_outside_v2",
            monitoring_interval="none",
            mime_hint=mime_hint,
        )

    rel_path = _strip_v2_prefix(parts.path or "/", allowed_path_prefix)
    rel_lower = rel_path.lower()
    query_pairs = [
        (k.lower(), v.lower()) for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    ext = PurePosixPath(rel_lower).suffix

    # 2. System endpoints (robots-disallowed, auth/search/mailto, feeds/print)
    if any(_path_matches_prefix(rel_lower, p) for p in _SYSTEM_PREFIXES) or any(
        pair in _SYSTEM_QUERY_EXACT for pair in query_pairs
    ):
        return ClassificationResult(
            category=DiscoveryCategory.SYSTEM,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.EXCLUDED,
            reason="system_or_robots_disallowed_endpoint",
            monitoring_interval="none",
            mime_hint=mime_hint or "text/html",
        )

    # Check PDF/Office extensions before page prefixes so docs under any path stay documents
    if ext in PDF_EXTENSIONS:
        return ClassificationResult(
            category=DiscoveryCategory.DOCUMENT_PDF,
            resource_type=ResourceType.DOCUMENT,
            recommended_action=RecommendedAction.REVIEW,
            reason="pdf_document_candidate",
            monitoring_interval="24h",
            mime_hint=mime_hint or "application/pdf",
        )

    if ext in OFFICE_EXTENSIONS:
        reason = (
            "legacy_binary_doc_requires_conversion_or_exclusion"
            if ext == ".doc"
            else "office_document_candidate"
        )
        return ClassificationResult(
            category=DiscoveryCategory.DOCUMENT_OFFICE,
            resource_type=ResourceType.DOCUMENT,
            recommended_action=RecommendedAction.REVIEW,
            reason=reason,
            monitoring_interval="24h",
            mime_hint=mime_hint,
        )

    # 12-15. Image & visual asset categories
    if _path_matches_prefix(rel_lower, "/images/stokdarah"):
        return ClassificationResult(
            category=DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            resource_type=ResourceType.IMAGE_POSTER,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="blood_stock_infographic_always_gate_b_quarantine",
            monitoring_interval="3h",
            mime_hint=mime_hint,
        )

    if _path_matches_prefix(rel_lower, "/images/smartslider3") or _IT_PDN_YEAR_RE.match(rel_lower):
        return ClassificationResult(
            category=DiscoveryCategory.POSTER_BANNER,
            resource_type=ResourceType.IMAGE_POSTER,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="informational_poster_or_banner_candidate",
            monitoring_interval="3h",
            mime_hint=mime_hint,
        )

    if rel_lower.startswith("/images/kemudahan_") or rel_lower.startswith(
        "/images/donation_suite_"
    ):
        return ClassificationResult(
            category=DiscoveryCategory.FACILITY_PHOTO,
            resource_type=ResourceType.IMAGE_POSTER,
            recommended_action=RecommendedAction.REVIEW,
            reason="facility_photo_candidate",
            monitoring_interval="24h",
            mime_hint=mime_hint,
        )

    if (
        _path_matches_prefix(rel_lower, "/images/sampledata/logos")
        or _path_matches_prefix(rel_lower, "/templates")
        or (ext in IMAGE_EXTENSIONS and _DECORATIVE_ICON_RE.search(PurePosixPath(rel_lower).name))
    ):
        return ClassificationResult(
            category=DiscoveryCategory.DECORATIVE,
            resource_type=ResourceType.IMAGE_POSTER,
            recommended_action=RecommendedAction.RECOMMENDED_EXCLUDE,
            reason="decorative_logo_template_or_ui_icon",
            monitoring_interval="none",
            mime_hint=mime_hint,
        )

    # 4. Homepage
    if rel_lower in {"/", "/index.php"} and not query_pairs:
        return ClassificationResult(
            category=DiscoveryCategory.HOMEPAGE,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="pdn_homepage",
            monitoring_interval="3h",
            mime_hint="text/html",
        )

    # 5. Corporate
    if _path_matches_prefix(rel_lower, "/index.php/maklumat-korporat") or _path_matches_prefix(
        rel_lower, "/maklumat-korporat"
    ):
        return ClassificationResult(
            category=DiscoveryCategory.CORPORATE,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="corporate_information_page",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 6. Services
    if _path_matches_prefix(rel_lower, "/index.php/perkhidmatan") or _path_matches_prefix(
        rel_lower, "/perkhidmatan"
    ):
        return ClassificationResult(
            category=DiscoveryCategory.SERVICES,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="pdn_services_page",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 7. FAQ
    if _path_matches_prefix(
        rel_lower, "/index.php/informasi/bahan-pendidikan-2"
    ) or _path_matches_prefix(rel_lower, "/informasi/bahan-pendidikan-2"):
        return ClassificationResult(
            category=DiscoveryCategory.FAQ,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="faq_educational_page",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 8. Publications
    if (
        _path_matches_prefix(rel_lower, "/index.php/informasi/bahan-pendidikan")
        or _path_matches_prefix(rel_lower, "/informasi/bahan-pendidikan")
        or _path_matches_prefix(rel_lower, "/index.php/penerbitan")
        or _path_matches_prefix(rel_lower, "/penerbitan")
    ):
        return ClassificationResult(
            category=DiscoveryCategory.PUBLICATIONS,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="publications_or_educational_material",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 9. Contact & static centers
    if (
        rel_lower.startswith("/index.php/hubungi-kami/pusat-")
        or rel_lower.startswith("/hubungi-kami/pusat-")
        or "talian-utama" in rel_lower
        or "hotline" in rel_lower
    ):
        return ClassificationResult(
            category=DiscoveryCategory.CONTACT_STATIC_CENTERS,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_APPROVE,
            reason="contact_or_static_donation_center",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 10. Standalone non-Joomla tools
    if rel_lower.startswith("/kalkulator_tarikh_pendermaan_darah_utuh") or rel_lower.startswith(
        "/media_sosial_pdn"
    ):
        return ClassificationResult(
            category=DiscoveryCategory.STANDALONE_TOOL,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.REVIEW,
            reason="standalone_non_joomla_page",
            monitoring_interval="24h",
            mime_hint="text/html",
        )

    # 11. Staff directory (PDPA sensitive — default recommended_exclude)
    if _path_matches_prefix(
        rel_lower, "/index.php/hubungi-kami/direktori-pegawai"
    ) or _path_matches_prefix(rel_lower, "/hubungi-kami/direktori-pegawai"):
        return ClassificationResult(
            category=DiscoveryCategory.STAFF_DIRECTORY,
            resource_type=ResourceType.PAGE,
            recommended_action=RecommendedAction.RECOMMENDED_EXCLUDE,
            reason="pdpa_staff_directory_default_exclude",
            monitoring_interval="none",
            mime_hint="text/html",
        )

    # 18. Uncategorized fallback (infer resource_type from extension)
    inferred_type = ResourceType.IMAGE_POSTER if ext in IMAGE_EXTENSIONS else ResourceType.PAGE
    return ClassificationResult(
        category=DiscoveryCategory.UNCATEGORIZED,
        resource_type=inferred_type,
        recommended_action=RecommendedAction.REVIEW,
        reason="uncategorized_in_scope_resource",
        monitoring_interval="24h",
        mime_hint=mime_hint or ("text/html" if inferred_type == ResourceType.PAGE else None),
    )
