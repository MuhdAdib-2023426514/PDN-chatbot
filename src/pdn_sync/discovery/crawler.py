"""Discovery-only non-JEvents URL & asset enumerator (Phase 1 — Gate A).

Strict invariants:
- Fetches candidate HTML pages solely to extract links/assets/lightweight metadata; discards bodies.
- Never follows or fetches JEvents URLs (/component/jevents, option=com_jevents).
- Never fetches leaf officer profile pages under /direktori-pegawai/** (traverses listings only).
- At most selective polite HEAD on candidate posters & documents (never GET/download bytes).
- Never modifies config/crawl_scope.yaml.
"""

from __future__ import annotations

import random
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from pdn_sync.config import AppSettings, load_crawl_scope, load_ingestion_policy
from pdn_sync.crawling.clearance import SourceAccessEvidence, require_live_clearance
from pdn_sync.crawling.robots import RobotsFetchOutcome, SubfolderRobotsPolicy
from pdn_sync.crawling.url_policy import (
    UrlPolicyError,
    is_in_allowed_scope,
    is_jevents_url,
    normalize_url,
    validate_ssrf_safe,
)
from pdn_sync.discovery.classifier import (
    SELECTIVE_HEAD_CATEGORIES,
    classify_url,
    extract_k2_item_id,
    is_staff_directory_leaf_profile,
)
from pdn_sync.discovery.exporter import compute_summary_counts, export_all_discovery_artifacts
from pdn_sync.discovery.html_links import extract_page_discovery_metadata
from pdn_sync.discovery.models import (
    DiscoveryCategory,
    InventoryEnvelope,
    InventoryItem,
    LinkStatus,
    ProposedPatternRule,
    RecommendedAction,
    make_canonical_key,
)
from pdn_sync.discovery.patterns import propose_rotating_asset_patterns
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import CrawlScopeConfig, evaluate_scope
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.repository import ControlPlaneRepository

DEFAULT_SEED_URL = "https://pdn.gov.my/v2/"
MAX_REDIRECT_HOPS = 5
MAX_CONSECUTIVE_BLOCK_SIGNALS = 3


@dataclass(frozen=True, slots=True)
class DiscoveryOptions:
    seed_url: str = DEFAULT_SEED_URL
    live: bool = False
    enable_head: bool = True
    max_pages: int | None = None
    max_depth: int | None = None
    max_listing_pages: int | None = None
    max_run_duration_seconds: int | None = None
    anomaly_growth_factor: float | None = None
    output_dir: Path | None = None
    scope_file: Path | None = None
    policy_file: Path | None = None
    sqlite_path: Path | None = None
    resolve_dns: bool = True


@dataclass(slots=True)
class DiscoveryRunResult:
    run_id: str
    status: str
    items: list[InventoryItem]
    proposed_patterns: list[ProposedPatternRule]
    summary_counts: dict[str, object]
    bounds_hit: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exported_files: dict[str, Path] = field(default_factory=dict)


@dataclass(slots=True)
class _CandidateState:
    canonical_key: str
    url: str
    original_url: str
    parent_url: str | None
    title_or_alt: str | None
    depth: int
    discovery_method: str
    discovered_at_utc: datetime
    redirect_chain: list[str] = field(default_factory=list)
    http_status: int | None = None
    link_status: LinkStatus = LinkStatus.UNFETCHED_ASSET
    duplicate_of: str | None = None
    mime_hint: str | None = None
    content_length_bytes: int | None = None
    normalized_content_sha256: str | None = None
    k2_item_id: str | None = None
    category: DiscoveryCategory = DiscoveryCategory.UNCATEGORIZED
    resource_type: ResourceType = ResourceType.PAGE
    disposition: Disposition = Disposition.PENDING
    recommended_action: RecommendedAction = RecommendedAction.REVIEW
    reason: str = ""
    policy_rule_id: str | None = None
    monitoring_interval: str = "24h"


def _pagination_base_key(url: str) -> str | None:
    """Return base path when URL is a paginated listing (?start= or ?limitstart=)."""
    parts = urlsplit(url)
    query_keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if "start" in query_keys or "limitstart" in query_keys:
        return parts.path.lower()
    return None


def _parse_retry_after(header_val: str | None) -> float | None:
    if not header_val:
        return None
    try:
        seconds = float(header_val.strip())
    except ValueError:
        return None
    return max(0.0, min(seconds, 60.0))


def _canonical_rank(state: _CandidateState) -> tuple[int, int, int, str]:
    """Lower rank wins as canonical representative when deduplicating K2 routes."""
    path_l = urlsplit(state.url).path.lower()
    is_raw_k2_route = 1 if "/component/k2/item/" in path_l else 0
    has_query = 1 if urlsplit(state.url).query else 0
    return (is_raw_k2_route, has_query, state.depth, state.url)


def _resolve_duplicates(states: dict[str, _CandidateState]) -> None:
    """Group duplicate K2 routes by K2 item ID and normalized main-content hash."""
    by_k2: dict[str, list[_CandidateState]] = defaultdict(list)
    by_hash: dict[str, list[_CandidateState]] = defaultdict(list)

    for st in states.values():
        if st.resource_type != ResourceType.PAGE or st.link_status != LinkStatus.OK:
            continue
        if st.k2_item_id:
            by_k2[st.k2_item_id].append(st)
        if st.normalized_content_sha256:
            by_hash[st.normalized_content_sha256].append(st)

    for group in (*by_k2.values(), *by_hash.values()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_canonical_rank)
        canonical = ordered[0]
        for alias in ordered[1:]:
            if alias.url != canonical.url and alias.duplicate_of is None:
                alias.duplicate_of = canonical.url


def _request_with_safe_redirects(
    *,
    client: httpx.Client,
    method: str,
    start_url: str,
    scope: CrawlScopeConfig,
    robots: SubfolderRobotsPolicy,
    resolve_dns: bool,
) -> tuple[httpx.Response | None, list[str], str | None]:
    """Execute GET/HEAD validating SSRF, host, /v2 scope, JEvents, and robots at every hop."""
    current = start_url
    chain: list[str] = []

    for _ in range(MAX_REDIRECT_HOPS + 1):
        try:
            validate_ssrf_safe(current, resolve_dns=resolve_dns)
        except UrlPolicyError as exc:
            return None, chain, f"redirect_ssrf_blocked:{exc.reason}"

        if not is_in_allowed_scope(
            current,
            allowed_host=scope.allowed_host,
            allowed_path_prefix=scope.allowed_path_prefix,
        ):
            return None, chain, "redirect_escaped_allowed_scope"

        if is_jevents_url(current):
            return None, chain, "redirect_to_jevents_excluded"

        if not robots.is_allowed(current):
            return None, chain, "redirect_disallowed_by_v2_robots"

        resp = client.request(method, current)
        if resp.status_code in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location")
            if not location:
                return resp, chain, None
            next_url = urljoin(current, location.strip())
            chain.append(current)
            try:
                current = normalize_url(
                    next_url,
                    allowed_host=scope.allowed_host,
                    allowed_path_prefix=scope.allowed_path_prefix,
                )
            except UrlPolicyError as exc:
                return None, chain, f"redirect_invalid_url:{exc.reason}"
            continue

        return resp, chain, None

    return None, chain, "too_many_redirects"


def run_discovery(
    *,
    options: DiscoveryOptions | None = None,
    settings: AppSettings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> DiscoveryRunResult:
    """Execute bounded discovery-only enumeration, persist to SQLite, and export artifacts."""
    opts = options or DiscoveryOptions()
    cfg = settings or AppSettings()
    do_sleep = sleep_fn if sleep_fn is not None else time.sleep

    scope_path = opts.scope_file or cfg.scope_file
    policy_path = opts.policy_file or cfg.ingestion_policy_file
    sqlite_path = opts.sqlite_path or cfg.sqlite_path
    output_dir = opts.output_dir or (cfg.data_dir / "discovery")

    # 1. Fail closed on invalid configs and sync SQLite control plane
    scope, scope_file_hash = load_crawl_scope(scope_path)
    policy = load_ingestion_policy(policy_path)

    upgrade_database(sqlite_path)
    engine = create_sqlite_engine(sqlite_path)
    repo = ControlPlaneRepository(engine)
    repo.sync_compiled_scope_cache(scope=scope, scope_file_hash=scope_file_hash)

    max_pages = opts.max_pages or policy.discovery_bounds.max_pages
    max_depth = opts.max_depth or policy.discovery_bounds.max_depth
    max_listing_pages = opts.max_listing_pages or policy.discovery_bounds.max_listing_pages
    max_duration = opts.max_run_duration_seconds or policy.discovery_bounds.max_run_duration_seconds
    anomaly_factor = opts.anomaly_growth_factor or policy.discovery_bounds.anomaly_growth_factor
    timeout_sec = policy.discovery_bounds.request_timeout_seconds

    bounds_dict: dict[str, object] = {
        "max_pages": max_pages,
        "max_depth": max_depth,
        "max_listing_pages": max_listing_pages,
        "request_timeout_seconds": timeout_sec,
        "max_run_duration_seconds": max_duration,
        "anomaly_growth_factor": anomaly_factor,
        "enable_head": opts.enable_head,
    }

    user_agent = cfg.user_agent()
    headers = {"User-Agent": user_agent}

    run_id = f"disc_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    monotonic_start = time.monotonic()

    prev_completed_count = repo.get_previous_completed_discovery_count()

    bounds_hit: list[str] = []
    errors: list[str] = []
    states: dict[str, _CandidateState] = {}
    page_queue: deque[str] = deque()
    queued_keys: set[str] = set()
    listing_page_counts: dict[str, int] = defaultdict(int)
    pages_fetched = 0
    consecutive_blocks = 0
    status = "completed"

    def _polite_pause() -> None:
        delay = random.uniform(
            policy.politeness.delay_min_seconds,
            policy.politeness.delay_max_seconds,
        )
        do_sleep(delay)

    def _register_candidate(
        *,
        raw_url: str,
        parent_url: str | None,
        depth: int,
        discovery_method: str,
        title_or_alt: str | None,
    ) -> _CandidateState | None:
        try:
            normalized = normalize_url(
                raw_url,
                allowed_host=scope.allowed_host,
                allowed_path_prefix=scope.allowed_path_prefix,
            )
        except UrlPolicyError:
            return None

        canon_key = make_canonical_key(normalized)
        if canon_key in states:
            return states[canon_key]

        cls_res = classify_url(
            normalized,
            allowed_host=scope.allowed_host,
            allowed_path_prefix=scope.allowed_path_prefix,
        )
        scope_eval = evaluate_scope(
            normalized,
            scope,
            resource_type=cls_res.resource_type,
        )

        # Determine initial link_status and reason
        if (
            cls_res.category == DiscoveryCategory.JEVENTS_DEFERRED
            or scope_eval.disposition == Disposition.EXCLUDED
        ):
            link_status = LinkStatus.SKIPPED_EXCLUDED
            reason = (
                "deferred_to_structured_schedule_phase"
                if cls_res.category == DiscoveryCategory.JEVENTS_DEFERRED
                else scope_eval.reason
            )
        else:
            link_status = LinkStatus.UNFETCHED_ASSET
            reason = cls_res.reason

        state = _CandidateState(
            canonical_key=canon_key,
            url=normalized,
            original_url=raw_url,
            parent_url=parent_url,
            title_or_alt=title_or_alt,
            depth=depth,
            discovery_method=discovery_method,
            discovered_at_utc=datetime.now(UTC),
            link_status=link_status,
            mime_hint=cls_res.mime_hint,
            k2_item_id=extract_k2_item_id(normalized),
            category=cls_res.category,
            resource_type=cls_res.resource_type,
            disposition=scope_eval.disposition,
            recommended_action=cls_res.recommended_action,
            reason=reason,
            policy_rule_id=scope_eval.rule_id,
            monitoring_interval=cls_res.monitoring_interval,
        )
        states[canon_key] = state
        return state

    def _maybe_enqueue_page(state: _CandidateState, robots: SubfolderRobotsPolicy) -> None:
        if state.canonical_key in queued_keys:
            return
        if state.resource_type != ResourceType.PAGE:
            return
        if state.category in {
            DiscoveryCategory.JEVENTS_DEFERRED,
            DiscoveryCategory.SYSTEM,
            DiscoveryCategory.EXTERNAL,
        }:
            return
        if state.disposition == Disposition.EXCLUDED:
            return
        if not robots.is_allowed(state.url):
            state.link_status = LinkStatus.SKIPPED_EXCLUDED
            state.disposition = Disposition.EXCLUDED
            state.reason = "disallowed_by_v2_robots_txt"
            return
        # Staff directory: traverse listings, NEVER fetch individual leaf officer profile pages
        if state.category == DiscoveryCategory.STAFF_DIRECTORY and is_staff_directory_leaf_profile(
            state.url, allowed_path_prefix=scope.allowed_path_prefix
        ):
            state.link_status = LinkStatus.UNFETCHED_ASSET
            state.reason = "pdpa_staff_directory_leaf_profile_unfetched"
            return
        if state.depth > max_depth:
            if "max_depth" not in bounds_hit:
                bounds_hit.append("max_depth")
            return
        pag_base = _pagination_base_key(state.url)
        if pag_base is not None:
            if listing_page_counts[pag_base] >= max_listing_pages:
                if "max_listing_pages" not in bounds_hit:
                    bounds_hit.append("max_listing_pages")
                return
            listing_page_counts[pag_base] += 1

        queued_keys.add(state.canonical_key)
        page_queue.append(state.canonical_key)

    client_kwargs: dict[str, object] = {
        "headers": headers,
        "timeout": timeout_sec,
        "follow_redirects": False,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    try:
        with httpx.Client(**client_kwargs) as client:  # type: ignore[arg-type]
            # Preflight: root /robots.txt (403 expected) + /v2/robots.txt (must be 200)
            base_origin = f"https://{scope.allowed_host}"
            root_robots_status: int | None = None
            try:
                root_resp = client.get(f"{base_origin}/robots.txt")
                root_robots_status = root_resp.status_code
            except httpx.HTTPError:
                root_robots_status = None

            _polite_pause()
            v2_robots_resp = client.get(f"{base_origin}{scope.allowed_path_prefix}/robots.txt")
            robots_policy = SubfolderRobotsPolicy.from_fetch_outcome(
                RobotsFetchOutcome(
                    root_robots_status=root_robots_status,
                    v2_robots_status=v2_robots_resp.status_code,
                    v2_robots_text=v2_robots_resp.text
                    if v2_robots_resp.status_code == 200
                    else None,
                ),
                scope_prefix=scope.allowed_path_prefix,
            )

            # Enforce 5-point source-access clearance before scanning
            evidence = SourceAccessEvidence(
                v2_robots_fetched=True,
                v2_robots_policy=robots_policy,
                publicly_reachable_without_controls=True,
                no_prohibiting_terms_or_directives=True,
                verified_date=started_at.strftime("%Y-%m-%d"),
            )
            require_live_clearance(
                settings=cfg,
                politeness=policy.politeness,
                evidence=evidence,
                require_live_flag=opts.live,
            )

            # Record sync_run only after preflight clearance succeeds
            repo.create_sync_run(
                run_id=run_id,
                profile="discovery_audit",
                scope_file_hash=scope_file_hash,
                started_at_utc=started_at,
                bounds=bounds_dict,
            )

            # Graceful check of /v2/sitemap.xml (404 expected on PDN)
            _polite_pause()
            try:
                sitemap_resp = client.get(f"{base_origin}{scope.allowed_path_prefix}/sitemap.xml")
                if sitemap_resp.status_code != 404 and progress_cb:
                    progress_cb(f"Note: /v2/sitemap.xml returned HTTP {sitemap_resp.status_code}")
            except httpx.HTTPError as exc:
                errors.append(f"sitemap_probe_error: {exc}")

            seed_state = _register_candidate(
                raw_url=opts.seed_url,
                parent_url=None,
                depth=0,
                discovery_method="seed",
                title_or_alt=None,
            )
            if seed_state is not None:
                _maybe_enqueue_page(seed_state, robots_policy)

            # Phase 1A: Polite BFS over non-JEvents candidate pages
            while page_queue:
                if pages_fetched >= max_pages:
                    if "max_pages" not in bounds_hit:
                        bounds_hit.append("max_pages")
                    break
                if (time.monotonic() - monotonic_start) >= max_duration:
                    if "max_run_duration_seconds" not in bounds_hit:
                        bounds_hit.append("max_run_duration_seconds")
                    break
                if (
                    prev_completed_count is not None
                    and prev_completed_count > 0
                    and len(states) > prev_completed_count * anomaly_factor
                ):
                    status = "aborted_anomaly"
                    if "anomaly_growth_factor" not in bounds_hit:
                        bounds_hit.append("anomaly_growth_factor")
                    errors.append(
                        f"anomaly_growth_abort: discovered {len(states)} candidates "
                        f"(>{anomaly_factor}x previous {prev_completed_count})"
                    )
                    break

                canon_key = page_queue.popleft()
                state = states[canon_key]

                _polite_pause()
                try:
                    resp, redirect_chain, redirect_err = _request_with_safe_redirects(
                        client=client,
                        method="GET",
                        start_url=state.url,
                        scope=scope,
                        robots=robots_policy,
                        resolve_dns=opts.resolve_dns,
                    )
                except httpx.HTTPError as exc:
                    state.link_status = LinkStatus.BROKEN
                    state.reason = f"fetch_error:{type(exc).__name__}"
                    errors.append(f"GET {state.url} failed: {exc}")
                    continue

                pages_fetched += 1
                state.redirect_chain = redirect_chain

                if redirect_err is not None or resp is None:
                    state.link_status = LinkStatus.SKIPPED_EXCLUDED
                    state.reason = redirect_err or "redirect_blocked"
                    continue

                state.http_status = resp.status_code
                content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip()
                if content_type:
                    state.mime_hint = content_type.lower()

                if resp.status_code in {403, 429}:
                    consecutive_blocks += 1
                    state.link_status = LinkStatus.BROKEN
                    state.reason = f"http_{resp.status_code}_backoff_signal"
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    if retry_after:
                        do_sleep(retry_after)
                    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCK_SIGNALS:
                        errors.append(
                            "rate_limit_or_block_stop: 3 consecutive 403/429 responses; stopping."
                        )
                        bounds_hit.append("consecutive_block_stop")
                        break
                    continue

                consecutive_blocks = 0

                if resp.status_code >= 400:
                    state.link_status = LinkStatus.BROKEN
                    state.recommended_action = RecommendedAction.RECOMMENDED_EXCLUDE
                    state.reason = f"http_{resp.status_code}_broken_link"
                    continue

                state.link_status = LinkStatus.OK
                if progress_cb and (pages_fetched == 1 or pages_fetched % 25 == 0):
                    progress_cb(
                        f"Fetched {pages_fetched} pages | queue={len(page_queue)} | "
                        f"total_candidates={len(states)}"
                    )

                # Only parse links from HTML responses; discard HTML body immediately
                if "html" not in (state.mime_hint or "text/html"):
                    continue

                final_url = str(resp.url)
                meta = extract_page_discovery_metadata(resp.text, base_url=final_url)
                if meta.title:
                    state.title_or_alt = meta.title
                state.normalized_content_sha256 = meta.normalized_content_sha256

                for link in meta.outbound_links:
                    child = _register_candidate(
                        raw_url=link.resolved_url,
                        parent_url=state.url,
                        depth=state.depth + 1,
                        discovery_method=link.discovery_method,
                        title_or_alt=link.title_or_alt,
                    )
                    if child is not None:
                        _maybe_enqueue_page(child, robots_policy)

            # Phase 1B: Selective polite HEAD on candidate posters & documents (NEVER GET)
            if (
                opts.enable_head
                and status == "completed"
                and "consecutive_block_stop" not in bounds_hit
            ):
                head_candidates = [
                    st
                    for st in states.values()
                    if st.category in SELECTIVE_HEAD_CATEGORIES
                    and st.disposition != Disposition.EXCLUDED
                    and robots_policy.is_allowed(st.url)
                ]
                if progress_cb and head_candidates:
                    progress_cb(
                        f"Running selective polite HEAD on {len(head_candidates)} assets..."
                    )
                for idx, asset_st in enumerate(head_candidates, start=1):
                    if (time.monotonic() - monotonic_start) >= max_duration:
                        if "max_run_duration_seconds" not in bounds_hit:
                            bounds_hit.append("max_run_duration_seconds")
                        break
                    _polite_pause()
                    try:
                        head_resp, r_chain, r_err = _request_with_safe_redirects(
                            client=client,
                            method="HEAD",
                            start_url=asset_st.url,
                            scope=scope,
                            robots=robots_policy,
                            resolve_dns=opts.resolve_dns,
                        )
                    except httpx.HTTPError as exc:
                        asset_st.link_status = LinkStatus.BROKEN
                        asset_st.reason = f"head_error:{type(exc).__name__}"
                        errors.append(f"HEAD {asset_st.url} failed: {exc}")
                        continue

                    asset_st.redirect_chain = r_chain
                    if r_err is not None or head_resp is None:
                        asset_st.link_status = LinkStatus.SKIPPED_EXCLUDED
                        asset_st.reason = r_err or "head_redirect_blocked"
                        continue

                    asset_st.http_status = head_resp.status_code
                    ct = (head_resp.headers.get("content-type") or "").split(";", 1)[0].strip()
                    if ct:
                        asset_st.mime_hint = ct.lower()
                    cl = head_resp.headers.get("content-length")
                    if cl and cl.isdigit():
                        asset_st.content_length_bytes = int(cl)

                    if head_resp.status_code in {403, 429}:
                        consecutive_blocks += 1
                        asset_st.link_status = LinkStatus.BROKEN
                        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCK_SIGNALS:
                            errors.append("head_consecutive_403_429_stop")
                            bounds_hit.append("consecutive_block_stop")
                            break
                        continue

                    consecutive_blocks = 0
                    if head_resp.status_code >= 400:
                        asset_st.link_status = LinkStatus.BROKEN
                        asset_st.recommended_action = RecommendedAction.RECOMMENDED_EXCLUDE
                        asset_st.reason = f"http_{head_resp.status_code}_broken_asset"
                    else:
                        asset_st.link_status = LinkStatus.OK

                    if progress_cb and idx % 25 == 0:
                        progress_cb(f"HEAD checked {idx}/{len(head_candidates)} assets")

        # Post-pass: resolve K2 duplicates and finalize models
        _resolve_duplicates(states)
        finished_at = datetime.now(UTC)

        ordered_states = sorted(states.values(), key=lambda s: (s.depth, s.url))
        items = [
            InventoryItem(
                run_id=run_id,
                canonical_key=st.canonical_key,
                url=st.url,
                original_url=st.original_url,
                redirect_chain=st.redirect_chain,
                http_status=st.http_status,
                link_status=st.link_status,
                duplicate_of=st.duplicate_of,
                parent_url=st.parent_url,
                title_or_alt=st.title_or_alt,
                discovered_at_utc=st.discovered_at_utc,
                depth=st.depth,
                resource_type=st.resource_type,
                mime_hint=st.mime_hint,
                content_length_bytes=st.content_length_bytes,
                category=st.category,
                discovery_method=st.discovery_method,
                disposition=st.disposition,
                recommended_action=st.recommended_action,
                reason=st.reason,
                policy_rule_id=st.policy_rule_id,
                monitoring_interval=st.monitoring_interval,
            )
            for st in ordered_states
        ]

        summary_counts = compute_summary_counts(items)
        proposed_patterns = propose_rotating_asset_patterns(
            items,
            allowed_path_prefix=scope.allowed_path_prefix,
        )

        repo.save_discovery_results(
            run_id=run_id,
            status=status,
            finished_at_utc=finished_at,
            summary_counts=summary_counts,
            items=items,
            content_hashes={s.canonical_key: s.normalized_content_sha256 for s in ordered_states},
            k2_item_ids={s.canonical_key: s.k2_item_id for s in ordered_states},
            error_summary="; ".join(errors) if errors else None,
        )

        envelope = InventoryEnvelope(
            run_id=run_id,
            scope_file_hash=scope_file_hash,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            bounds=bounds_dict,
            summary_counts=summary_counts,
            items=items,
        )
        exported_files = export_all_discovery_artifacts(
            envelope=envelope,
            proposed_patterns=proposed_patterns,
            bounds_hit=bounds_hit,
            errors=errors,
            output_dir=output_dir,
        )

        return DiscoveryRunResult(
            run_id=run_id,
            status=status,
            items=items,
            proposed_patterns=proposed_patterns,
            summary_counts=summary_counts,
            bounds_hit=bounds_hit,
            errors=errors,
            exported_files=exported_files,
        )
    finally:
        engine.dispose()
