"""Safe fixture capture utility (refuses unapproved and PDPA staff_directory URLs)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from pdn_sync.config import AppSettings, load_crawl_scope, load_ingestion_policy
from pdn_sync.crawling.fetcher import ApprovedContentFetcher
from pdn_sync.crawling.robots import RobotsFetchOutcome, SubfolderRobotsPolicy
from pdn_sync.discovery.classifier import classify_url
from pdn_sync.discovery.exporter import atomic_write_text
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.discovery.scope_manager import assert_url_approved_for_content_crawl
from pdn_sync.persistence.blob_store import LocalBlobStore

_SENSITIVE_CATEGORIES: frozenset[DiscoveryCategory] = frozenset({DiscoveryCategory.STAFF_DIRECTORY})
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._\-]+")


class FixtureCaptureBlockedError(PermissionError):
    """Raised when `pdn-sync fixtures capture` refuses an unapproved or PII-sensitive URL."""


def sanitize_html_fixture(raw_html: bytes) -> str:
    """Strip transient CSRF tokens and hidden form fields from HTML fixtures."""
    soup = BeautifulSoup(raw_html.decode("utf-8", errors="replace"), "lxml")
    for hidden in soup.select("input[type='hidden']"):
        hidden.decompose()
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"csrf|token", re.I)}):
        meta.decompose()
    return str(soup)


def capture_safe_fixture(
    raw_url: str,
    *,
    settings: AppSettings | None = None,
    scope_file: Path | None = None,
    policy_file: Path | None = None,
    fixtures_dir: Path = Path("tests/fixtures"),
    transport: httpx.BaseTransport | None = None,
    resolve_dns: bool = True,
) -> Path:
    """Capture a sanitized fixture ONLY for Gate-A-approved, non-PII URLs."""
    cfg = settings or AppSettings()
    s_path = scope_file or cfg.scope_file
    p_path = policy_file or cfg.ingestion_policy_file

    scope, _ = load_crawl_scope(s_path)
    policy = load_ingestion_policy(p_path)

    # 1. Enforce Gate A approval first
    cls_res = classify_url(
        raw_url,
        allowed_host=scope.allowed_host,
        allowed_path_prefix=scope.allowed_path_prefix,
    )
    normalized = assert_url_approved_for_content_crawl(
        raw_url,
        scope_path=s_path,
        resource_type=cls_res.resource_type,
    )

    # 2. Hard PDPA / PII block on staff_directory (even if approved for production!)
    if cls_res.category in _SENSITIVE_CATEGORIES:
        raise FixtureCaptureBlockedError(
            f"Refusing fixture capture for PDPA-sensitive category {cls_res.category.value!r}: "
            f"{normalized}"
        )

    # 3. Fetch robots + asset politely
    headers = {"User-Agent": cfg.user_agent()}
    client_kwargs: dict[str, object] = {
        "headers": headers,
        "timeout": policy.politeness.request_timeout_seconds,
        "follow_redirects": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
        v2_robots = client.get(
            f"https://{scope.allowed_host}{scope.allowed_path_prefix}/robots.txt"
        )
        robots_policy = SubfolderRobotsPolicy.from_fetch_outcome(
            RobotsFetchOutcome(
                root_robots_status=403,
                v2_robots_status=v2_robots.status_code,
                v2_robots_text=v2_robots.text if v2_robots.status_code == 200 else None,
            ),
            scope_prefix=scope.allowed_path_prefix,
        )

    blob_store = LocalBlobStore(cfg.data_dir / "blobs")
    fetcher = ApprovedContentFetcher(
        settings=cfg,
        scope=scope,
        scope_path=s_path,
        politeness=policy.politeness,
        robots=robots_policy,
        blob_store=blob_store,
        transport=transport,
        sleep_fn=(lambda _s: None) if transport is not None else None,
        resolve_dns=resolve_dns,
    )
    capture = fetcher.fetch(normalized, resource_type=cls_res.resource_type)
    if capture.outcome != "fetched" or capture.blob_sha256 is None:
        raise RuntimeError(
            f"Fixture capture fetch failed ({capture.outcome}): {capture.error_message}"
        )

    raw_bytes = blob_store.get_bytes(capture.blob_sha256)
    url_path = PurePosixPath(urlsplit(normalized).path)
    stem = _SLUG_RE.sub("_", url_path.name or "homepage").strip("_") or "homepage"

    if capture.content_type and "html" in capture.content_type:
        sanitized = sanitize_html_fixture(raw_bytes)
        out_path = fixtures_dir / "html" / f"{stem}.html"
        atomic_write_text(out_path, sanitized)
        return out_path

    ext = url_path.suffix or ".bin"
    out_path = fixtures_dir / "assets" / f"{stem}{ext if not stem.endswith(ext) else ''}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw_bytes)
    return out_path
