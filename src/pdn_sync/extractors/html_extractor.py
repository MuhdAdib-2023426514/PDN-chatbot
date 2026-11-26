"""Phase 2a deterministic HTML & table extractor (Warp, Joomla/K2, standalone, PDPA staff)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.domain.policy import ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig, evaluate_scope
from pdn_sync.extractors.contracts import (
    ExtractedTable,
    ExtractionResult,
    MediaRef,
)
from pdn_sync.extractors.dates import extract_source_effective_date
from pdn_sync.staging.semantic_hash import SemanticTextBlock

HTML_EXTRACTOR_NAME = "html_bs4_lxml"
HTML_EXTRACTOR_VERSION = "1.0.0"

_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_PHONE_RE = re.compile(r"(?:\+?60|0)\d[\d\s\-/()]{6,18}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_CHROME_SELECTORS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "nav",
    "header",
    "footer",
    "form",
    ".uk-offcanvas",
    "#offcanvas",
    ".tm-navbar",
    ".tm-footer",
    ".tm-toolbar",
    ".uk-breadcrumb",
    ".breadcrumb",
)

_WARP_HOMEPAGE_SELECTORS: tuple[str, ...] = (
    ".tm-features",
    ".tm-top-b",
    ".tm-fullwidth-top",
    ".tm-fullwidth-bottom",
    ".tm-bottom-b",
)

_HEADING_TAGS: dict[str, int] = {
    "h1": 1,
    "h2": 2,
    "h3": 3,
    "h4": 4,
    "h5": 5,
    "h6": 6,
}


def _clean_inline_text(value: str) -> str:
    return _WS_RE.sub(" ", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def _render_inline_with_links(node: Tag, *, base_url: str) -> str:
    """Render text inside a block preserving Markdown links (including external Drive links)."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            txt = str(child)
            if txt:
                parts.append(txt)
        elif isinstance(child, Tag):
            name = (child.name or "").lower()
            if name in {"script", "style", "noscript", "svg", "img"}:
                continue
            if name == "br":
                parts.append("\n")
            elif name == "a":
                href = child.get("href")
                label = _clean_inline_text(child.get_text(" ", strip=True))
                if isinstance(href, str) and href.strip():
                    raw_href = href.strip()
                    lower_href = raw_href.lower()
                    if lower_href.startswith(("http://", "https://", "/")):
                        resolved = urljoin(base_url, raw_href)
                        if label:
                            parts.append(f"[{label}]({resolved})")
                        else:
                            parts.append(resolved)
                    elif lower_href.startswith(("mailto:", "tel:")):
                        parts.append(label or raw_href)
                    elif label:
                        parts.append(label)
                elif label:
                    parts.append(label)
            else:
                parts.append(_render_inline_with_links(child, base_url=base_url))

    joined = "".join(parts)
    lines = [_clean_inline_text(ln) for ln in joined.splitlines()]
    compact = "\n".join(ln for ln in lines if ln)
    return _MULTI_NL_RE.sub("\n\n", compact).strip()


def _extract_tables_from_root(root: Tag, *, base_url: str) -> list[ExtractedTable]:
    tables: list[ExtractedTable] = []
    for idx, table_tag in enumerate(root.find_all("table")):
        if not isinstance(table_tag, Tag):
            continue
        # Skip nested layout tables if parent already captured them
        if table_tag.find_parent("table") is not None:
            continue

        caption_tag = table_tag.find("caption")
        caption = (
            _clean_inline_text(caption_tag.get_text(" ", strip=True))
            if isinstance(caption_tag, Tag)
            else None
        )

        headers: list[str] = []
        rows: list[list[str]] = []

        thead = table_tag.find("thead")
        if isinstance(thead, Tag):
            header_tr = thead.find("tr")
            if isinstance(header_tr, Tag):
                headers = [
                    _render_inline_with_links(cell, base_url=base_url)
                    for cell in header_tr.find_all(["th", "td"], recursive=False)
                    if isinstance(cell, Tag)
                ]

        tr_list = [tr for tr in table_tag.find_all("tr") if isinstance(tr, Tag)]
        for row_idx, tr in enumerate(tr_list):
            if isinstance(thead, Tag) and tr.find_parent("thead") is not None:
                continue
            cells = [
                cell for cell in tr.find_all(["th", "td"], recursive=False) if isinstance(cell, Tag)
            ]
            if not cells:
                continue
            rendered = [_render_inline_with_links(c, base_url=base_url) for c in cells]
            if not any(rendered):
                continue

            if not headers and row_idx == 0 and all((c.name or "").lower() == "th" for c in cells):
                headers = rendered
            else:
                rows.append(rendered)

        if not headers and not rows:
            continue

        # Normalize row widths to header/max width
        max_cols = max([len(headers), *(len(r) for r in rows)], default=0)
        if headers and len(headers) < max_cols:
            headers.extend([""] * (max_cols - len(headers)))
        padded_rows = [r + [""] * (max_cols - len(r)) for r in rows]
        tables.append(
            ExtractedTable(
                table_index=idx,
                caption=caption,
                headers=headers,
                rows=padded_rows,
                units=[None] * max_cols,
                source_selector=f"table:nth-of-type({idx + 1})",
            )
        )
    return tables


def _extract_media_refs(
    roots: list[Tag],
    *,
    base_url: str,
    scope: CrawlScopeConfig,
) -> list[MediaRef]:
    refs: list[MediaRef] = []
    seen_urls: set[str] = set()
    position = 0
    for root in roots:
        for img in root.find_all("img"):
            if not isinstance(img, Tag):
                continue
            raw_src: str | None = None
            for attr in ("src", "data-src", "data-lazy-src", "data-desktop"):
                val = img.get(attr)
                if isinstance(val, str) and val.strip():
                    raw_src = val.strip()
                    break
            if not raw_src or raw_src.lower().startswith("data:"):
                continue
            resolved = urljoin(base_url, raw_src)
            if urlsplit(resolved).scheme.lower() not in {"http", "https"}:
                continue
            if resolved in seen_urls:
                continue
            seen_urls.add(resolved)
            ev = evaluate_scope(resolved, scope, resource_type=ResourceType.IMAGE_POSTER)
            alt_attr = img.get("alt")
            alt = _clean_inline_text(alt_attr) if isinstance(alt_attr, str) else None
            refs.append(
                MediaRef(
                    url=ev.normalized_url or resolved,
                    alt=alt or None,
                    position=position,
                    scope_status=ev.disposition,
                )
            )
            position += 1
    return refs


def _extract_blocks_from_roots(roots: list[Tag], *, base_url: str) -> list[SemanticTextBlock]:
    blocks: list[SemanticTextBlock] = []
    seen_texts: set[tuple[int | None, str]] = set()
    order = 0

    for root in roots:
        for elem in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote"]):
            if not isinstance(elem, Tag):
                continue
            # Avoid duplicating table cell text (tables are captured faithfully in tables[])
            if elem.find_parent("table") is not None:
                continue
            # Avoid duplicating parent li/p when nested inside another extracted block
            if elem.name == "p" and elem.find_parent("li") is not None:
                continue

            text = _render_inline_with_links(elem, base_url=base_url)
            if not text:
                continue
            heading_level = _HEADING_TAGS.get((elem.name or "").lower())
            key = (heading_level, text)
            if key in seen_texts:
                continue
            seen_texts.add(key)
            blocks.append(
                SemanticTextBlock(
                    order=order,
                    heading_level=heading_level,
                    text=text,
                )
            )
            order += 1

    # Fallback: if a simple page used bare <div> text without <p>/<h*>
    if not blocks:
        for root in roots:
            raw = _render_inline_with_links(root, base_url=base_url)
            if raw:
                for line in [ln.strip() for ln in raw.splitlines() if ln.strip()]:
                    blocks.append(SemanticTextBlock(order=order, heading_level=None, text=line))
                    order += 1

    return blocks


def _extract_staff_directory_pdpa(
    roots: list[Tag],
    *,
    base_url: str,
    title: str | None,
) -> tuple[list[SemanticTextBlock], list[ExtractedTable], dict[str, object]]:
    """PDPA-safe staff_directory extractor: strips all officer photos, extracts official fields."""
    # Hard PDPA invariant: strip every <img> tag so officer photos are never retained or referenced
    for root in roots:
        for img in root.find_all("img"):
            img.decompose()

    tables = []
    for root in roots:
        tables.extend(_extract_tables_from_root(root, base_url=base_url))

    blocks: list[SemanticTextBlock] = []
    order = 0
    if title:
        blocks.append(SemanticTextBlock(order=order, heading_level=1, text=title))
        order += 1

    # Parse Joomla contact/profile key-value pairs (Name, Designation, Unit, Office Phone, Email)
    kv_rows: list[list[str]] = []
    official_fields = {
        "name": "",
        "designation": "",
        "unit": "",
        "office_phone": "",
        "office_email": "",
    }

    for root in roots:
        # Check definition lists (<dt>/<dd>) used by Joomla com_contact
        dts = root.find_all("dt")
        dds = root.find_all("dd")
        if dts and dds:
            for dt, dd in zip(dts, dds, strict=False):
                if not isinstance(dt, Tag) or not isinstance(dd, Tag):
                    continue
                k = _clean_inline_text(dt.get_text(" ", strip=True)).rstrip(":")
                v = _clean_inline_text(dd.get_text(" ", strip=True))
                if not v:
                    continue
                kl = k.lower()
                if any(w in kl for w in ("jawatan", "position", "designation")):
                    official_fields["designation"] = v
                elif any(w in kl for w in ("bahagian", "unit", "seksyen", "department")):
                    official_fields["unit"] = v
                elif any(w in kl for w in ("telefon", "phone", "tel", "samb")):
                    official_fields["office_phone"] = v
                elif any(w in kl for w in ("emel", "e-mel", "email")):
                    official_fields["office_email"] = v
                elif any(w in kl for w in ("nama", "name")):
                    official_fields["name"] = v

        # Fallback: scan concise lines for official phone/email & heading name
        heading = root.find(["h1", "h2", "h3"])
        if isinstance(heading, Tag) and not official_fields["name"]:
            official_fields["name"] = _clean_inline_text(heading.get_text(" ", strip=True))

        full_text = _clean_inline_text(root.get_text(" ", strip=True))
        if not official_fields["office_email"]:
            em = _EMAIL_RE.search(full_text)
            if em:
                official_fields["office_email"] = em.group(0)
        if not official_fields["office_phone"]:
            ph = _PHONE_RE.search(full_text)
            if ph:
                official_fields["office_phone"] = _clean_inline_text(ph.group(0))

    if any(official_fields.values()) and not tables:
        kv_rows.append(
            [
                official_fields["name"] or (title or ""),
                official_fields["designation"],
                official_fields["unit"],
                official_fields["office_phone"],
                official_fields["office_email"],
            ]
        )
        tables.append(
            ExtractedTable(
                table_index=0,
                caption="Official PDN Directory Record (PDPA Minimized)",
                headers=["Name", "Designation", "Unit", "Office Phone", "Office Email"],
                rows=kv_rows,
                units=[None, None, None, None, None],
                source_selector="staff_directory_pdpa",
            )
        )
        summary_parts = [
            f"{label}: {val}"
            for label, val in [
                ("Name", official_fields["name"]),
                ("Designation", official_fields["designation"]),
                ("Unit", official_fields["unit"]),
                ("Office Phone", official_fields["office_phone"]),
                ("Office Email", official_fields["office_email"]),
            ]
            if val
        ]
        if summary_parts:
            blocks.append(
                SemanticTextBlock(order=order, heading_level=None, text=" | ".join(summary_parts))
            )
            order += 1
    else:
        # Directory listing page with links/tables — preserve clean non-image blocks
        for blk in _extract_blocks_from_roots(roots, base_url=base_url):
            blocks.append(
                SemanticTextBlock(
                    order=order,
                    heading_level=blk.heading_level,
                    text=blk.text,
                )
            )
            order += 1

    return (
        blocks,
        tables,
        {
            "pdpa_minimized": True,
            "officer_photos_stripped": True,
            "allowed_fields": ["name", "designation", "unit", "office_phone", "office_email"],
        },
    )


def _select_content_roots(
    soup: BeautifulSoup, category: DiscoveryCategory
) -> tuple[list[Tag], str]:
    """Select meaningful content containers for Warp homepage, Joomla/K2, or standalone pages."""
    if category == DiscoveryCategory.HOMEPAGE:
        warp_nodes = [
            node
            for sel in _WARP_HOMEPAGE_SELECTORS
            for node in soup.select(sel)
            if isinstance(node, Tag)
        ]
        if warp_nodes:
            return warp_nodes, "warp_homepage_sections"

    for selector in ("#k2Container", "main", ".tm-main", ".item-page", "article"):
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            return [node], f"selector:{selector}"

    body = soup.find("body")
    if isinstance(body, Tag):
        return [body], "fallback:body"
    return [soup], "fallback:document"


def extract_html(
    *,
    raw_html: bytes,
    source_url: str,
    resource_id: str,
    resource_version_id: str,
    content_hash: str,
    category: DiscoveryCategory,
    scope: CrawlScopeConfig,
    captured_at_utc: datetime,
    policy_rule_id: str | None = None,
    content_type: str | None = "text/html",
) -> ExtractionResult:
    """Deterministically extract text blocks, tables, and media_refs from approved HTML bytes."""
    decoded = raw_html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "lxml")

    title_tag = soup.find("title")
    title = (
        _clean_inline_text(title_tag.get_text(" ", strip=True))
        if isinstance(title_tag, Tag)
        else None
    )

    # Strip non-semantic chrome before extracting content
    for selector in _CHROME_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    roots, layout_mode = _select_content_roots(soup, category)
    provenance: dict[str, object] = {"layout_mode": layout_mode}

    if category == DiscoveryCategory.STAFF_DIRECTORY:
        text_blocks, tables, pdpa_prov = _extract_staff_directory_pdpa(
            roots, base_url=source_url, title=title
        )
        media_refs: list[MediaRef] = []
        provenance.update(pdpa_prov)
    else:
        media_refs = _extract_media_refs(roots, base_url=source_url, scope=scope)
        tables = []
        for root in roots:
            tables.extend(_extract_tables_from_root(root, base_url=source_url))
        # Re-index table indices sequentially across multiple Warp sections
        tables = [tbl.model_copy(update={"table_index": i}) for i, tbl in enumerate(tables)]
        text_blocks = _extract_blocks_from_roots(roots, base_url=source_url)

    # Try extracting source_effective_date from title/first blocks (never invent valid_until)
    date_candidates = [title] if title else []
    date_candidates.extend(b.text for b in text_blocks[:5])
    eff_date, eff_prov = extract_source_effective_date(
        source_url=source_url,
        text_candidates=date_candidates,
        text_provenance="page_text",
    )

    warnings: list[str] = []
    if not text_blocks and not tables:
        warnings.append("empty_html_extraction")

    now = datetime.now(UTC)
    draft = ExtractionResult(
        artifact_id=f"art_{uuid.uuid4().hex[:16]}",
        resource_id=resource_id,
        resource_version_id=resource_version_id,
        source_url=source_url,
        resource_type=ResourceType.PAGE,
        category=category,
        policy_rule_id=policy_rule_id,
        content_hash=content_hash,
        semantic_hash="",
        extractor_name=HTML_EXTRACTOR_NAME,
        extractor_version=HTML_EXTRACTOR_VERSION,
        captured_at_utc=captured_at_utc,
        extracted_at_utc=now,
        source_effective_date=eff_date,
        source_effective_date_provenance=eff_prov,
        content_type=content_type,
        title=title,
        language="ms",
        text_blocks=text_blocks,
        tables=tables,
        media_refs=media_refs,
        warnings=warnings,
        provenance=provenance,
    )
    return draft.model_copy(update={"semantic_hash": draft.recompute_semantic_hash()})
