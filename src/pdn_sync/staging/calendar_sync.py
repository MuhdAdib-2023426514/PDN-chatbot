"""Phase 4 bounded JEvents calendar discovery, fetch, parse, and Gate B staging pipeline."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pdn_sync.config import AppSettings, load_crawl_scope, load_ingestion_policy
from pdn_sync.crawling.clearance import SourceAccessEvidence, require_live_clearance
from pdn_sync.crawling.fetcher import (
    ApprovedContentFetcher,
    FetchBlockedError,
    FetchCaptureResult,
)
from pdn_sync.crawling.robots import RobotsFetchOutcome, SubfolderRobotsPolicy
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.discovery.scope_manager import GateABlockedError
from pdn_sync.domain.policy import Disposition, ResourceType
from pdn_sync.domain.scope import evaluate_scope
from pdn_sync.extractors.contracts import ReviewStatus, ValidationStatus
from pdn_sync.extractors.jevents import (
    build_monthcalendar_urls,
    extract_eventdetail_links_from_monthcalendar,
    extract_jevents_eventdetail,
    extract_upstream_event_id,
)
from pdn_sync.persistence.blob_store import LocalBlobStore
from pdn_sync.persistence.db import create_sqlite_engine, upgrade_database
from pdn_sync.persistence.models import SyncRunRow
from pdn_sync.persistence.repository import ControlPlaneRepository
from pdn_sync.persistence.schedule_repo import ScheduleRepository
from pdn_sync.persistence.staging_repo import StagingRepository
from pdn_sync.staging.exporter import export_review_snapshot, export_staged_artifact
from pdn_sync.staging.validator import validate_and_tag


@dataclass(frozen=True, slots=True)
class CalendarSyncOptions:
    live: bool = False
    months_ahead: int | None = None
    limit: int | None = None
    now_kl: datetime | None = None
    scope_file: Path | None = None
    policy_file: Path | None = None
    sqlite_path: Path | None = None
    data_dir: Path | None = None
    resolve_dns: bool = True


@dataclass(slots=True)
class CalendarSyncSummary:
    run_id: str
    months_attempted: int = 0
    months_succeeded: int = 0
    months_failed: int = 0
    eventdetail_discovered: int = 0
    fetched: int = 0
    not_modified: int = 0
    unchanged_blob: int = 0
    failed: int = 0
    staged_pending: int = 0
    staged_quarantined: int = 0
    carried_forward: int = 0
    sessions_extracted_total: int = 0
    marked_removed_events: int = 0
    artifact_ids: list[str] = field(default_factory=list)


def run_calendar_sync_pipeline(
    *,
    options: CalendarSyncOptions | None = None,
    settings: AppSettings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> CalendarSyncSummary:
    """Discover bounded JEvents monthcalendar + eventdetail pages and stage for Gate B."""
    opts = options or CalendarSyncOptions()
    cfg = settings or AppSettings()

    scope_path = opts.scope_file or cfg.scope_file
    policy_path = opts.policy_file or cfg.ingestion_policy_file
    sqlite_path = opts.sqlite_path or cfg.sqlite_path
    data_dir = opts.data_dir or cfg.data_dir

    scope, scope_hash = load_crawl_scope(scope_path)
    policy = load_ingestion_policy(policy_path)
    upgrade_database(sqlite_path)

    months_ahead = (
        opts.months_ahead if opts.months_ahead is not None else policy.calendar.months_ahead
    )
    max_events = opts.limit or policy.calendar.max_eventdetail_pages_per_run

    engine = create_sqlite_engine(sqlite_path)
    ctrl_repo = ControlPlaneRepository(engine)
    stage_repo = StagingRepository(engine)
    sched_repo = ScheduleRepository(engine)
    blob_store = LocalBlobStore(data_dir / "blobs")

    run_id = f"cal_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(UTC)
    summary = CalendarSyncSummary(run_id=run_id)

    try:
        ctrl_repo.sync_compiled_scope_cache(scope=scope, scope_file_hash=scope_hash)

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

        require_live_clearance(
            settings=cfg,
            politeness=policy.politeness,
            evidence=SourceAccessEvidence(
                v2_robots_fetched=True,
                v2_robots_policy=robots_policy,
                publicly_reachable_without_controls=True,
                no_prohibiting_terms_or_directives=True,
                verified_date=started_at.strftime("%Y-%m-%d"),
            ),
            require_live_flag=opts.live,
        )

        ctrl_repo.create_sync_run(
            run_id=run_id,
            profile="calendar",
            scope_file_hash=scope_hash,
            started_at_utc=started_at,
            bounds={"months_ahead": months_ahead, "max_events": max_events},
        )

        fetcher = ApprovedContentFetcher(
            settings=cfg,
            scope=scope,
            scope_path=scope_path,
            politeness=policy.politeness,
            robots=robots_policy,
            blob_store=blob_store,
            transport=transport,
            sleep_fn=sleep_fn,
            resolve_dns=opts.resolve_dns,
            allow_jevents=True,
        )

        # 1. Fetch bounded monthcalendar seeds (current + next N months)
        month_seeds = build_monthcalendar_urls(
            opts.now_kl,
            months_ahead=months_ahead,
            base_url=f"https://{scope.allowed_host}{scope.allowed_path_prefix}",
        )
        summary.months_attempted = len(month_seeds)

        successful_months: list[tuple[int, int]] = []
        seen_upstream_ids: set[str] = set()
        event_targets: list[tuple[str, str]] = []  # (eventdetail_url, month_seed_url)

        for year, month, m_url in month_seeds:
            m_res = stage_repo.ensure_crawl_resource(
                canonical_url=m_url,
                resource_type=ResourceType.PAGE.value,
                category=DiscoveryCategory.DONATION_SCHEDULE.value,
            )
            try:
                cap = fetcher.fetch(m_url, resource_type=ResourceType.PAGE)
            except (FetchBlockedError, GateABlockedError) as exc:
                cap = FetchCaptureResult(
                    outcome="blocked_gate_a", url=m_url, error_message=str(exc)
                )

            stage_repo.record_fetch(
                run_id=run_id,
                resource_id=m_res.resource_id,
                url=m_url,
                final_url=cap.final_url,
                redirect_chain=cap.redirect_chain,
                fetched_at_utc=cap.fetched_at_utc,
                http_status=cap.http_status,
                etag=cap.etag,
                last_modified=cap.last_modified,
                content_type=cap.content_type,
                byte_size=cap.byte_size,
                blob_sha256=cap.blob_sha256,
                latency_ms=cap.latency_ms,
                fetch_outcome=cap.outcome,
                error_message=cap.error_message,
            )

            if cap.outcome != "fetched" or cap.blob_sha256 is None:
                summary.months_failed += 1
                if progress_cb:
                    progress_cb(
                        f"[month {year:04d}-{month:02d}] FAILED ({cap.outcome}): "
                        f"{cap.error_message}"
                    )
                continue

            summary.months_succeeded += 1
            successful_months.append((year, month))
            m_bytes = blob_store.get_bytes(cap.blob_sha256)
            links = extract_eventdetail_links_from_monthcalendar(
                m_bytes,
                page_url=cap.final_url or m_url,
                allowed_host=scope.allowed_host,
                allowed_path_prefix=scope.allowed_path_prefix,
            )
            for ev_url in links:
                up_id = extract_upstream_event_id(ev_url)
                if up_id in seen_upstream_ids:
                    continue
                seen_upstream_ids.add(up_id)
                ev_scope = evaluate_scope(ev_url, scope, resource_type=ResourceType.PAGE)
                if ev_scope.disposition == Disposition.APPROVED and ev_scope.normalized_url:
                    event_targets.append((ev_scope.normalized_url, m_url))

            if progress_cb:
                progress_cb(
                    f"[month {year:04d}-{month:02d}] ok — found {len(links)} eventdetail links"
                )

        event_targets = event_targets[:max_events]
        summary.eventdetail_discovered = len(event_targets)

        # 2. Fetch, extract, validate, and stage each approved eventdetail page
        for idx, (ev_url, seed_url) in enumerate(event_targets, start=1):
            ev_eval = evaluate_scope(ev_url, scope, resource_type=ResourceType.PAGE)
            res_row = stage_repo.ensure_crawl_resource(
                canonical_url=ev_url,
                resource_type=ResourceType.PAGE.value,
                category=DiscoveryCategory.DONATION_SCHEDULE.value,
            )
            latest_staged = stage_repo.get_latest_artifact_for_resource(res_row.resource_id)
            last_fetch = stage_repo.get_latest_successful_fetch(res_row.resource_id)

            try:
                cap = fetcher.fetch(
                    ev_url,
                    resource_type=ResourceType.PAGE,
                    etag=last_fetch.etag if last_fetch else None,
                    last_modified=last_fetch.last_modified if last_fetch else None,
                )
            except (FetchBlockedError, GateABlockedError) as exc:
                cap = FetchCaptureResult(
                    outcome="blocked_gate_a", url=ev_url, error_message=str(exc)
                )

            fetch_row = stage_repo.record_fetch(
                run_id=run_id,
                resource_id=res_row.resource_id,
                url=ev_url,
                final_url=cap.final_url,
                redirect_chain=cap.redirect_chain,
                fetched_at_utc=cap.fetched_at_utc,
                http_status=cap.http_status,
                etag=cap.etag,
                last_modified=cap.last_modified,
                content_type=cap.content_type,
                byte_size=cap.byte_size,
                blob_sha256=cap.blob_sha256,
                latency_ms=cap.latency_ms,
                fetch_outcome=cap.outcome,
                error_message=cap.error_message,
            )

            if cap.outcome == "not_modified":
                summary.not_modified += 1
                stage_repo.touch_last_verified(res_row.resource_id, cap.fetched_at_utc)
                continue

            if cap.outcome != "fetched" or cap.blob_sha256 is None:
                summary.failed += 1
                if progress_cb:
                    progress_cb(
                        f"[{idx}/{len(event_targets)}] FAILED {ev_url}: {cap.error_message}"
                    )
                continue

            summary.fetched += 1
            if latest_staged is not None and latest_staged.content_hash == cap.blob_sha256:
                summary.unchanged_blob += 1
                stage_repo.touch_last_verified(res_row.resource_id, cap.fetched_at_utc)
                continue

            raw_bytes = blob_store.get_bytes(cap.blob_sha256)
            version_seq = stage_repo.next_version_seq(res_row.resource_id)
            version_id = f"ver_{res_row.resource_id}_{version_seq:03d}_{uuid.uuid4().hex[:6]}"

            extracted = extract_jevents_eventdetail(
                raw_html=raw_bytes,
                source_url=ev_url,
                resource_id=res_row.resource_id,
                resource_version_id=version_id,
                content_hash=cap.blob_sha256,
                captured_at_utc=cap.fetched_at_utc,
                policy_rule_id=ev_eval.rule_id,
                month_seed_url=seed_url,
                content_type=cap.content_type,
            )
            validated = validate_and_tag(extracted, policy)

            prior_approved = stage_repo.get_latest_approved_artifact(res_row.resource_id)
            should_carry_forward = (
                prior_approved is not None
                and prior_approved[0].semantic_hash == validated.semantic_hash
                and validated.validation_status == ValidationStatus.VALID
                and validated.review_status != ReviewStatus.QUARANTINED
            )
            if should_carry_forward:
                validated = validated.model_copy(update={"review_status": ReviewStatus.APPROVED})

            json_path, md_path = export_staged_artifact(
                result=validated,
                version_seq=version_seq,
                data_dir=data_dir,
            )
            stage_repo.save_staged_version(
                result=validated,
                version_seq=version_seq,
                fetch_id=fetch_row.fetch_id,
                json_path=json_path,
                markdown_path=md_path,
            )

            if should_carry_forward and prior_approved is not None:
                stage_repo.record_review_decision(
                    artifact_id=validated.artifact_id,
                    reviewer="system:carry_forward",
                    decision="carried_forward",
                    new_review_status=ReviewStatus.APPROVED,
                    policy_mode=policy.approval_mode.value,
                    carried_from_decision_id=prior_approved[1].decision_id,
                    notes="semantic_hash unchanged from approved schedule version",
                )
                export_review_snapshot(result=validated, approved=True, data_dir=data_dir)
                summary.carried_forward += 1
            elif validated.review_status == ReviewStatus.QUARANTINED:
                summary.staged_quarantined += 1
            else:
                summary.staged_pending += 1

            summary.sessions_extracted_total += len(validated.structured_events)
            summary.artifact_ids.append(validated.artifact_id)
            if progress_cb:
                progress_cb(
                    f"[{idx}/{len(event_targets)}] [{validated.review_status.value:14s}] "
                    f"sessions={len(validated.structured_events)} "
                    f"date={validated.source_effective_date} {ev_url}"
                )

        # 3. Reconcile missing events ONLY for months whose fetch succeeded cleanly!
        missing_res = sched_repo.reconcile_missing_calendar_events(
            successful_months=successful_months,
            seen_upstream_event_ids=seen_upstream_ids,
        )
        summary.marked_removed_events = len(missing_res.marked_removed_event_ids)
        sched_repo.sync_schedule_pending_change_flags()

        finished_at = datetime.now(UTC)
        summary_dict = {
            "months_attempted": summary.months_attempted,
            "months_succeeded": summary.months_succeeded,
            "months_failed": summary.months_failed,
            "eventdetail_discovered": summary.eventdetail_discovered,
            "fetched": summary.fetched,
            "not_modified": summary.not_modified,
            "unchanged_blob": summary.unchanged_blob,
            "failed": summary.failed,
            "staged_pending": summary.staged_pending,
            "staged_quarantined": summary.staged_quarantined,
            "carried_forward": summary.carried_forward,
            "sessions_extracted_total": summary.sessions_extracted_total,
            "marked_removed_events": summary.marked_removed_events,
        }
        with stage_repo._session_factory() as session:
            run_row = session.get(SyncRunRow, run_id)
            if run_row is not None:
                run_row.status = "completed" if summary.months_succeeded > 0 else "failed"
                run_row.finished_at_utc = finished_at
                run_row.summary_counts_json = json.dumps(summary_dict, sort_keys=True)
                session.commit()

        return summary
    finally:
        engine.dispose()
