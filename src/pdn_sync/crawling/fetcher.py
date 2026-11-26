"""Gate-A-enforced polite conditional HTTP fetcher saving immutable bytes to BlobStore."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

from pdn_sync.config import AppSettings
from pdn_sync.crawling.robots import SubfolderRobotsPolicy
from pdn_sync.crawling.url_policy import (
    UrlPolicyError,
    is_in_allowed_scope,
    is_jevents_url,
    normalize_url,
    validate_ssrf_safe,
)
from pdn_sync.discovery.scope_manager import (
    GateABlockedError,
    assert_url_approved_for_content_crawl,
)
from pdn_sync.domain.policy import PolitenessPolicy, ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig
from pdn_sync.persistence.blob_store import BlobStore

MAX_REDIRECT_HOPS = 5
MAX_TRANSIENT_RETRIES = 3


class FetchBlockedError(RuntimeError):
    """Raised when a fetch is blocked by Gate A, robots, SSRF, or byte-cap policy."""


@dataclass(frozen=True, slots=True)
class FetchCaptureResult:
    outcome: str  # fetched | not_modified | fetch_failed | exceeded_byte_cap | blocked
    url: str
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    blob_sha256: str | None = None
    deduplicated: bool = False
    latency_ms: int = 0
    fetched_at_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    error_message: str | None = None


class ApprovedContentFetcher:
    """Fetches ONLY Gate-A-approved URLs with SSRF, redirect, byte-cap, and ETag/IMS guards."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        scope: CrawlScopeConfig,
        scope_path: Path,
        politeness: PolitenessPolicy,
        robots: SubfolderRobotsPolicy,
        blob_store: BlobStore,
        transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        resolve_dns: bool = True,
        allow_jevents: bool = False,
    ) -> None:
        self.settings = settings
        self.scope = scope
        self.scope_path = scope_path
        self.politeness = politeness
        self.robots = robots
        self.blob_store = blob_store
        self.transport = transport
        self.sleep_fn = sleep_fn if sleep_fn is not None else time.sleep
        self.resolve_dns = resolve_dns
        self.allow_jevents = allow_jevents
        self._user_agent = settings.user_agent()
        self._has_requested = False

    def _polite_delay(self) -> None:
        if not self._has_requested:
            self._has_requested = True
            return
        delay = random.uniform(
            self.politeness.delay_min_seconds,
            self.politeness.delay_max_seconds,
        )
        self.sleep_fn(delay)

    def _validate_hop(self, url: str, resource_type: ResourceType | None) -> str:
        """Enforce SSRF, host/prefix scope, JEvents exclusion, /v2/robots.txt, and Gate A."""
        validate_ssrf_safe(url, resolve_dns=self.resolve_dns)
        normalized = normalize_url(
            url,
            allowed_host=self.scope.allowed_host,
            allowed_path_prefix=self.scope.allowed_path_prefix,
        )
        if not is_in_allowed_scope(
            normalized,
            allowed_host=self.scope.allowed_host,
            allowed_path_prefix=self.scope.allowed_path_prefix,
        ):
            raise FetchBlockedError(f"Redirect/target escaped allowed scope: {normalized}")
        if is_jevents_url(normalized) and not self.allow_jevents:
            raise FetchBlockedError(
                f"JEvents URL blocked in Track 1 non-calendar mode: {normalized}"
            )
        if not self.robots.is_allowed(normalized):
            raise FetchBlockedError(f"Disallowed by /v2/robots.txt: {normalized}")
        assert_url_approved_for_content_crawl(
            normalized,
            scope_path=self.scope_path,
            resource_type=resource_type,
        )
        return normalized

    def fetch(
        self,
        raw_url: str,
        *,
        resource_type: ResourceType | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchCaptureResult:
        """Politely fetch a Gate-A-approved resource and persist immutable bytes to BlobStore."""
        # Gate A + safety check BEFORE any network request
        start_url = self._validate_hop(raw_url, resource_type)

        headers: dict[str, str] = {"User-Agent": self._user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        client_kwargs: dict[str, object] = {
            "headers": headers,
            "timeout": self.politeness.request_timeout_seconds,
            "follow_redirects": False,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        max_bytes = self.politeness.max_document_bytes
        t0 = time.monotonic()

        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            current = start_url
            redirect_chain: list[str] = []

            for _hop in range(MAX_REDIRECT_HOPS + 1):
                self._polite_delay()
                last_exc: Exception | None = None

                for attempt in range(1, MAX_TRANSIENT_RETRIES + 1):
                    try:
                        with client.stream("GET", current) as resp:
                            status = resp.status_code

                            # Handle redirects safely (re-validate every hop!)
                            if status in {301, 302, 303, 307, 308}:
                                location = resp.headers.get("location")
                                if not location:
                                    return FetchCaptureResult(
                                        outcome="fetch_failed",
                                        url=start_url,
                                        final_url=current,
                                        redirect_chain=redirect_chain,
                                        http_status=status,
                                        latency_ms=int((time.monotonic() - t0) * 1000),
                                        error_message="redirect_missing_location",
                                    )
                                redirect_chain.append(current)
                                next_raw = urljoin(current, location.strip())
                                try:
                                    current = self._validate_hop(next_raw, resource_type)
                                except (UrlPolicyError, GateABlockedError, FetchBlockedError) as e:
                                    raise FetchBlockedError(
                                        f"Redirect blocked ({current} -> {next_raw}): {e}"
                                    ) from e
                                last_exc = None
                                break

                            # Handle conditional 304 Not Modified
                            if status == 304:
                                return FetchCaptureResult(
                                    outcome="not_modified",
                                    url=start_url,
                                    final_url=current,
                                    redirect_chain=redirect_chain,
                                    http_status=304,
                                    etag=resp.headers.get("etag") or etag,
                                    last_modified=resp.headers.get("last-modified")
                                    or last_modified,
                                    latency_ms=int((time.monotonic() - t0) * 1000),
                                )

                            # Stop/backoff on 403/429
                            if status in {403, 429}:
                                retry_after = resp.headers.get("retry-after")
                                if retry_after and retry_after.strip().isdigit():
                                    self.sleep_fn(min(float(retry_after.strip()), 60.0))
                                return FetchCaptureResult(
                                    outcome="fetch_failed",
                                    url=start_url,
                                    final_url=current,
                                    redirect_chain=redirect_chain,
                                    http_status=status,
                                    latency_ms=int((time.monotonic() - t0) * 1000),
                                    error_message=f"http_{status}_rate_limit_or_forbidden",
                                )

                            # Retry transient 5xx
                            if status >= 500 and attempt < MAX_TRANSIENT_RETRIES:
                                self.sleep_fn(float(2**attempt))
                                continue

                            if status >= 400:
                                return FetchCaptureResult(
                                    outcome="fetch_failed",
                                    url=start_url,
                                    final_url=current,
                                    redirect_chain=redirect_chain,
                                    http_status=status,
                                    latency_ms=int((time.monotonic() - t0) * 1000),
                                    error_message=f"http_{status}",
                                )

                            # Content-Length pre-check before reading stream
                            cl_hdr = resp.headers.get("content-length")
                            if cl_hdr and cl_hdr.strip().isdigit():
                                declared_len = int(cl_hdr.strip())
                                if declared_len > max_bytes:
                                    return FetchCaptureResult(
                                        outcome="exceeded_byte_cap",
                                        url=start_url,
                                        final_url=current,
                                        redirect_chain=redirect_chain,
                                        http_status=status,
                                        byte_size=declared_len,
                                        latency_ms=int((time.monotonic() - t0) * 1000),
                                        error_message=(
                                            f"Content-Length {declared_len} exceeds cap {max_bytes}"
                                        ),
                                    )

                            # Stream chunks while enforcing hard byte cap
                            chunks: list[bytes] = []
                            total_read = 0
                            for chunk in resp.iter_bytes():
                                total_read += len(chunk)
                                if total_read > max_bytes:
                                    return FetchCaptureResult(
                                        outcome="exceeded_byte_cap",
                                        url=start_url,
                                        final_url=current,
                                        redirect_chain=redirect_chain,
                                        http_status=status,
                                        byte_size=total_read,
                                        latency_ms=int((time.monotonic() - t0) * 1000),
                                        error_message=(
                                            f"Streamed response exceeded max_document_bytes "
                                            f"({max_bytes})"
                                        ),
                                    )
                                chunks.append(chunk)

                            raw_bytes = b"".join(chunks)
                            blob = self.blob_store.put_bytes(raw_bytes)
                            ct = (resp.headers.get("content-type") or "").split(";", 1)[
                                0
                            ].strip().lower() or None
                            return FetchCaptureResult(
                                outcome="fetched",
                                url=start_url,
                                final_url=current,
                                redirect_chain=redirect_chain,
                                http_status=status,
                                etag=resp.headers.get("etag"),
                                last_modified=resp.headers.get("last-modified"),
                                content_type=ct,
                                byte_size=blob.byte_size,
                                blob_sha256=blob.sha256,
                                deduplicated=blob.deduplicated,
                                latency_ms=int((time.monotonic() - t0) * 1000),
                            )

                    except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
                        last_exc = e
                        if attempt < MAX_TRANSIENT_RETRIES:
                            self.sleep_fn(float(2**attempt))
                            continue
                        return FetchCaptureResult(
                            outcome="fetch_failed",
                            url=start_url,
                            final_url=current,
                            redirect_chain=redirect_chain,
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            error_message=f"{type(e).__name__}: {e}",
                        )
                    except httpx.HTTPError as e:
                        return FetchCaptureResult(
                            outcome="fetch_failed",
                            url=start_url,
                            final_url=current,
                            redirect_chain=redirect_chain,
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            error_message=f"{type(e).__name__}: {e}",
                        )

                if last_exc is not None:
                    return FetchCaptureResult(
                        outcome="fetch_failed",
                        url=start_url,
                        final_url=current,
                        redirect_chain=redirect_chain,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        error_message=str(last_exc),
                    )

        return FetchCaptureResult(
            outcome="fetch_failed",
            url=start_url,
            redirect_chain=redirect_chain,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error_message="too_many_redirects",
        )
