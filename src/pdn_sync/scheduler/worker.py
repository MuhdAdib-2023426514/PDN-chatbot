"""Single scheduled worker process (APScheduler 3.11.x) calling framework-agnostic pipelines."""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from pdn_sync.config import AppSettings, load_ingestion_policy
from pdn_sync.discovery.crawler import DiscoveryOptions, run_discovery
from pdn_sync.discovery.models import DiscoveryCategory
from pdn_sync.indexing.publisher import PublicationService
from pdn_sync.staging.gate_b import ExtractionOptions, run_extraction_pipeline

logger = logging.getLogger(__name__)

WorkerProfile = Literal["homepage", "site", "discovery_audit", "publish_sync"]


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """Default polite schedule cadence in Asia/Kuala_Lumpur (ADR-orchestration.md)."""

    timezone: str = "Asia/Kuala_Lumpur"
    homepage_interval_hours: int = 4
    site_cron_hour: int = 3
    site_cron_minute: int = 0
    discovery_audit_day_of_week: str = "sun"
    discovery_audit_hour: int = 2
    discovery_audit_minute: int = 0
    jitter_seconds: int = 120


def run_profile_once(
    profile: WorkerProfile,
    *,
    settings: AppSettings | None = None,
    live: bool = False,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one framework-agnostic sync profile directly (usable via CLI, cron, or worker)."""
    cfg = settings or AppSettings()

    if profile == "homepage":
        categories = [
            DiscoveryCategory.HOMEPAGE,
            DiscoveryCategory.BLOOD_STOCK_GRAPHIC,
            DiscoveryCategory.POSTER_BANNER,
        ]
        results: dict[str, Any] = {"profile": "homepage", "categories": {}}
        for cat in categories:
            summary = run_extraction_pipeline(
                options=ExtractionOptions(live=live, category=cat),
                settings=cfg,
                progress_cb=progress_cb,
            )
            results["categories"][cat.value] = {
                "run_id": summary.run_id,
                "fetched": summary.fetched,
                "not_modified": summary.not_modified,
                "unchanged_blob": summary.unchanged_blob,
                "carried_forward": summary.carried_forward,
                "pending": summary.staged_pending,
                "quarantined": summary.staged_quarantined,
                "failed": summary.failed,
            }
        # Refresh pending_change freshness flags without bypassing Gate B
        pub_svc = PublicationService(
            sqlite_path=cfg.sqlite_path,
            scope_path=cfg.scope_file,
            policy_path=cfg.ingestion_policy_file,
            settings=cfg,
        )
        pub_svc.publish_approved(dry_run=True)
        return results

    if profile == "site":
        summary = run_extraction_pipeline(
            options=ExtractionOptions(live=live),
            settings=cfg,
            progress_cb=progress_cb,
        )
        return {
            "profile": "site",
            "run_id": summary.run_id,
            "fetched": summary.fetched,
            "not_modified": summary.not_modified,
            "unchanged_blob": summary.unchanged_blob,
            "carried_forward": summary.carried_forward,
            "pending": summary.staged_pending,
            "quarantined": summary.staged_quarantined,
            "failed": summary.failed,
        }

    if profile == "discovery_audit":
        disc = run_discovery(
            options=DiscoveryOptions(live=live),
            settings=cfg,
            progress_cb=progress_cb,
        )
        pub_svc = PublicationService(
            sqlite_path=cfg.sqlite_path,
            scope_path=cfg.scope_file,
            policy_path=cfg.ingestion_policy_file,
            settings=cfg,
        )
        ret = pub_svc.reconcile_retirement(run_id=disc.run_id)
        return {
            "profile": "discovery_audit",
            "run_id": disc.run_id,
            "status": disc.status,
            "items": len(disc.items),
            "retirement_skipped": ret.skipped_incomplete_run,
            "no_longer_linked": ret.no_longer_linked_count,
            "retired_publications": len(ret.retired_publication_ids),
        }

    if profile == "publish_sync":
        pub_svc = PublicationService(
            sqlite_path=cfg.sqlite_path,
            scope_path=cfg.scope_file,
            policy_path=cfg.ingestion_policy_file,
            settings=cfg,
        )
        pub_sum = pub_svc.publish_approved(dry_run=False, progress_cb=progress_cb)
        return {
            "profile": "publish_sync",
            "published": pub_sum.published,
            "skipped_idempotent": pub_sum.skipped_idempotent,
            "chunks_upserted": pub_sum.chunks_upserted,
            "tables_published": pub_sum.tables_published,
            "pending_change_updates": pub_sum.pending_change_updates,
        }

    raise ValueError(f"Unsupported worker profile: {profile!r}")


def build_scheduler(
    *,
    settings: AppSettings | None = None,
    schedule: ScheduleConfig | None = None,
    live: bool = False,
) -> BlockingScheduler:
    """Create the single-process APScheduler BlockingScheduler (max_instances=1, coalesce=True)."""
    cfg = settings or AppSettings()
    policy = load_ingestion_policy(cfg.ingestion_policy_file)
    sched_cfg = schedule or ScheduleConfig(timezone=policy.domain_timezone)
    tz = ZoneInfo(sched_cfg.timezone)

    scheduler = BlockingScheduler(
        timezone=tz,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 600,
        },
    )
    scheduler.add_job(
        run_profile_once,
        trigger=IntervalTrigger(
            hours=sched_cfg.homepage_interval_hours,
            timezone=tz,
            jitter=sched_cfg.jitter_seconds,
        ),
        args=["homepage"],
        kwargs={"settings": cfg, "live": live},
        id="pdn_homepage_check",
        name="PDN homepage & poster incremental check",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_profile_once,
        trigger=CronTrigger(
            hour=sched_cfg.site_cron_hour,
            minute=sched_cfg.site_cron_minute,
            timezone=tz,
            jitter=sched_cfg.jitter_seconds,
        ),
        args=["site"],
        kwargs={"settings": cfg, "live": live},
        id="pdn_site_check",
        name="PDN approved site incremental check",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_profile_once,
        trigger=CronTrigger(
            day_of_week=sched_cfg.discovery_audit_day_of_week,
            hour=sched_cfg.discovery_audit_hour,
            minute=sched_cfg.discovery_audit_minute,
            timezone=tz,
            jitter=sched_cfg.jitter_seconds,
        ),
        args=["discovery_audit"],
        kwargs={"settings": cfg, "live": live},
        id="pdn_discovery_audit",
        name="PDN weekly non-JEvents discovery audit & retirement reconciliation",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    return scheduler


def start_worker(*, settings: AppSettings | None = None, live: bool = False) -> None:
    """Start the single scheduled worker with clean SIGTERM/SIGINT shutdown."""
    scheduler = build_scheduler(settings=settings, live=live)

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; shutting down PDN worker cleanly.", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    scheduler.start()
