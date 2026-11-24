"""Transient HTML metadata, outbound link, and media reference extractor for discovery.

Discovery HTML is parsed in-memory and discarded immediately; page bodies are NEVER persisted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\"\)\s]+)['\"]?\s*\)", flags=re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_IGNORED_URL_PREFIXES: tuple[str, ...] = (
    "mailto:",
    "tel:",
    "javascript:",
    "data:",
    "blob:",
    "#",
)


@dataclass(frozen=True, slots=True)
class ExtractedLink:
    raw_url: str
    resolved_url: str
    discovery_method: str
    title_or_alt: str | None = None


@dataclass(frozen=True, slots=True)
class PageDiscoveryMetadata:
    title: str | None
    canonical_url: str | None
    breadcrumbs: list[str] = field(default_factory=list)
    normalized_content_sha256: str | None = None
    outbound_links: list[ExtractedLink] = field(default_factory=list)


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _WS_RE.sub(" ", value).strip()
    return cleaned or None


def _resolve_href(base_url: str, raw_value: str | None) -> str | None:
    if not raw_value:
        return None
    stripped = raw_value.strip()
    if not stripped:
        return None
    lower = stripped.lower()
    if any(lower.startswith(prefix) for prefix in _IGNORED_URL_PREFIXES):
        return None
    resolved = urljoin(base_url, stripped)
    scheme = urlsplit(resolved).scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    return resolved


def _parse_srcset(srcset_value: str) -> list[str]:
    candidates: list[str] = []
    for entry in srcset_value.split(","):
        parts = entry.strip().split()
        if parts:
            candidates.append(parts[0])
    return candidates


def _compute_normalized_content_hash(soup: BeautifulSoup) -> str | None:
    """Hash meaningful page text (ignoring scripts, styles, nav/footer chrome, CSRF inputs)."""
    # Prefer Joomla/K2 main container if present, otherwise body
    raw_container = (
        soup.select_one("#k2Container")
        or soup.find("main")
        or soup.select_one(".tm-main")
        or soup.find("body")
    )
    if not isinstance(raw_container, Tag):
        return None
    container: Tag = raw_container

    # Parse a fresh copy of just the container so we do not mutate the main soup
    clone = BeautifulSoup(str(container), "lxml")
    for tag in clone.find_all(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    text = _WS_RE.sub(" ", clone.get_text(separator=" ")).strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_page_discovery_metadata(html: str, *, base_url: str) -> PageDiscoveryMetadata:
    """Extract links, asset references, title, breadcrumbs, and dedup hash without storing HTML."""
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = _clean_text(title_tag.get_text()) if isinstance(title_tag, Tag) else None

    canonical_url: str | None = None
    canonical_tag = soup.find("link", attrs={"rel": lambda r: bool(r and "canonical" in r)})
    if isinstance(canonical_tag, Tag):
        href = canonical_tag.get("href")
        if isinstance(href, str):
            canonical_url = _resolve_href(base_url, href)

    breadcrumbs: list[str] = []
    for bc_item in soup.select(".uk-breadcrumb li, .breadcrumb li"):
        bc_text = _clean_text(bc_item.get_text())
        if bc_text:
            breadcrumbs.append(bc_text)

    content_hash = _compute_normalized_content_hash(soup)

    links: list[ExtractedLink] = []
    seen_pairs: set[tuple[str, str]] = set()

    def _add_link(raw_val: str | None, method: str, label: str | None) -> None:
        resolved = _resolve_href(base_url, raw_val)
        if resolved is None or raw_val is None:
            return
        key = (resolved, method)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        links.append(
            ExtractedLink(
                raw_url=raw_val.strip(),
                resolved_url=resolved,
                discovery_method=method,
                title_or_alt=_clean_text(label),
            )
        )

    # 1. Anchor links (<a href>)
    for a_tag in soup.find_all("a", href=True):
        if not isinstance(a_tag, Tag):
            continue
        href = a_tag.get("href")
        if isinstance(href, str):
            title_attr = a_tag.get("title")
            label = a_tag.get_text() or (title_attr if isinstance(title_attr, str) else None)
            _add_link(href, "html_a_href", label)

    # 2. Image tags (<img src / data-src / srcset / SmartSlider3 attributes>)
    for img_tag in soup.find_all("img"):
        if not isinstance(img_tag, Tag):
            continue
        alt_val = img_tag.get("alt")
        title_val = img_tag.get("title")
        label = (
            alt_val
            if isinstance(alt_val, str) and alt_val.strip()
            else (title_val if isinstance(title_val, str) else None)
        )
        for attr in ("src", "data-src", "data-lazy-src", "data-desktop", "data-mobile"):
            val = img_tag.get(attr)
            if isinstance(val, str):
                method = (
                    "smartslider_attr" if attr in {"data-desktop", "data-mobile"} else "html_img"
                )
                _add_link(val, method, label)
        srcset = img_tag.get("srcset")
        if isinstance(srcset, str):
            for candidate in _parse_srcset(srcset):
                _add_link(candidate, "html_img_srcset", label)

    # 3. <source srcset> (responsive picture elements)
    for src_tag in soup.find_all("source", srcset=True):
        if not isinstance(src_tag, Tag):
            continue
        srcset = src_tag.get("srcset")
        if isinstance(srcset, str):
            for candidate in _parse_srcset(srcset):
                _add_link(candidate, "html_source_srcset", None)

    # 4. SmartSlider3 non-img elements with data-desktop / data-mobile / data-bg
    for tag in soup.find_all(attrs={"data-desktop": True}):
        if isinstance(tag, Tag) and tag.name != "img":
            val = tag.get("data-desktop")
            if isinstance(val, str):
                _add_link(val, "smartslider_attr", None)

    # 5. Inline CSS background-image: url(...)
    for styled_tag in soup.find_all(style=True):
        if not isinstance(styled_tag, Tag):
            continue
        style_val = styled_tag.get("style")
        if isinstance(style_val, str) and "url(" in style_val.lower():
            for match in _CSS_URL_RE.findall(style_val):
                _add_link(match, "css_background_image", None)

    # 6. Embedded <style> blocks referencing /v2/images/... or images/...
    for style_tag in soup.find_all("style"):
        if not isinstance(style_tag, Tag):
            continue
        css_text = style_tag.get_text() or ""
        if "url(" in css_text.lower():
            for match in _CSS_URL_RE.findall(css_text):
                if "/images/" in match.lower() or match.lower().startswith("images/"):
                    _add_link(match, "css_background_image", None)

    return PageDiscoveryMetadata(
        title=title,
        canonical_url=canonical_url,
        breadcrumbs=breadcrumbs,
        normalized_content_sha256=content_hash,
        outbound_links=links,
    )
