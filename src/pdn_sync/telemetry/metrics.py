"""Isolated Prometheus CollectorRegistry and SQLite-backed gauge refresher for GET /metrics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import Engine

from pdn_sync.config import load_crawl_scope
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.publication_repo import PublicationRepository
from pdn_sync.persistence.schedule_repo import ScheduleRepository
from pdn_sync.telemetry.alerts import AlertReport


class MetricsManager:
    """Owns an isolated Prometheus CollectorRegistry for localhost-only GET /metrics."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)

        # Live HTTP API metrics
        self.api_requests_total = Counter(
            "pdn_sync_api_requests_total",
            "Total HTTP requests served by the read-only PDN knowledge API.",
            labelnames=("endpoint", "status_code"),
            registry=self.registry,
        )
        self.api_request_duration_seconds = Histogram(
            "pdn_sync_api_request_duration_seconds",
            "HTTP request duration in seconds for the PDN knowledge API.",
            labelnames=("endpoint",),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
            registry=self.registry,
        )

        # Published corpus & freshness gauges
        self.published_documents = Gauge(
            "pdn_sync_published_documents",
            "Active published knowledge documents in SQLite.",
            registry=self.registry,
        )
        self.published_chunks = Gauge(
            "pdn_sync_published_chunks",
            "Active published knowledge chunks in SQLite.",
            registry=self.registry,
        )
        self.published_tables = Gauge(
            "pdn_sync_published_tables",
            "Active published structured tables in SQLite.",
            registry=self.registry,
        )
        self.published_donation_events = Gauge(
            "pdn_sync_published_donation_events",
            "Active published blood donation events in SQLite.",
            registry=self.registry,
        )
        self.published_donation_sessions = Gauge(
            "pdn_sync_published_donation_sessions",
            "Active current blood donation sessions in SQLite.",
            registry=self.registry,
        )
        self.qdrant_points = Gauge(
            "pdn_sync_qdrant_points",
            "Published Qdrant vector points reported by health check.",
            registry=self.registry,
        )
        self.qdrant_drift_abs = Gauge(
            "pdn_sync_qdrant_drift_abs",
            "Absolute difference between SQLite published chunks and Qdrant published points.",
            registry=self.registry,
        )
        self.pending_gate_b = Gauge(
            "pdn_sync_pending_gate_b",
            "Staged artifacts awaiting Gate B human review.",
            registry=self.registry,
        )
        self.quarantined_gate_b = Gauge(
            "pdn_sync_quarantined_gate_b",
            "Staged artifacts quarantined for critical facts or low OCR confidence.",
            registry=self.registry,
        )
        self.pending_gate_b_oldest_age_seconds = Gauge(
            "pdn_sync_pending_gate_b_oldest_age_seconds",
            "Age in seconds of the oldest pending Gate B artifact (0 when empty).",
            registry=self.registry,
        )
        self.approved_unpublished_artifacts = Gauge(
            "pdn_sync_approved_unpublished_artifacts",
            "Gate-B-approved artifacts not yet published to SQLite/Qdrant.",
            registry=self.registry,
        )
        self.pending_change_documents = Gauge(
            "pdn_sync_pending_change_documents",
            "Published documents flagged with an unreviewed newer version.",
            registry=self.registry,
        )
        self.fts5_mismatch_total = Gauge(
            "pdn_sync_fts5_mismatch_total",
            "Row count mismatch between active SQLite tables and FTS5 virtual tables.",
            labelnames=("index",),
            registry=self.registry,
        )
        self.outbox_events = Gauge(
            "pdn_sync_outbox_events",
            "Outbox event count by lifecycle status.",
            labelnames=("status",),
            registry=self.registry,
        )
        self.dlq_items = Gauge(
            "pdn_sync_dlq_items",
            "Number of dead-lettered outbox events requiring operator intervention.",
            registry=self.registry,
        )
        self.active_alerts = Gauge(
            "pdn_sync_active_alerts",
            "Active operational alerts by severity.",
            labelnames=("severity",),
            registry=self.registry,
        )
        self.last_success_timestamp_seconds = Gauge(
            "pdn_sync_last_success_timestamp_seconds",
            "Unix timestamp of the last completed sync run per profile.",
            labelnames=("profile",),
            registry=self.registry,
        )
        self.scope_exact_urls = Gauge(
            "pdn_sync_scope_exact_urls",
            "Exact URL counts configured in config/crawl_scope.yaml by disposition.",
            labelnames=("disposition",),
            registry=self.registry,
        )

    def observe_request(self, *, endpoint: str, status_code: int, duration_seconds: float) -> None:
        self.api_requests_total.labels(endpoint=endpoint, status_code=str(status_code)).inc()
        self.api_request_duration_seconds.labels(endpoint=endpoint).observe(duration_seconds)

    def refresh_from_state(
        self,
        *,
        engine: Engine,
        alert_report: AlertReport,
        scope_path: Path | None = None,
    ) -> None:
        """Refresh all snapshot gauges deterministically from SQLite + AlertReport."""
        pub_repo = PublicationRepository(engine)
        sched_repo = ScheduleRepository(engine)
        ops_repo = OpsRepository(engine)

        summary = pub_repo.get_status_summary()
        ev_cnt, ses_cnt = sched_repo.count_published_schedules()
        outbox_counts = ops_repo.count_outbox_by_status()
        runs_detailed = ops_repo.get_last_completed_runs_detailed()

        self.published_documents.set(float(summary["published_documents"]))
        self.published_chunks.set(float(summary["published_chunks"]))
        self.published_tables.set(float(summary["published_tables"]))
        self.published_donation_events.set(float(ev_cnt))
        self.published_donation_sessions.set(float(ses_cnt))
        self.qdrant_points.set(float(alert_report.qdrant_published_points))
        self.qdrant_drift_abs.set(float(alert_report.qdrant_drift_abs))
        self.pending_gate_b.set(float(summary["pending_gate_b"]))
        self.quarantined_gate_b.set(float(summary["quarantined_gate_b"]))
        self.pending_gate_b_oldest_age_seconds.set(
            float(alert_report.oldest_pending_gate_b_age_hours or 0.0) * 3600.0
        )
        self.approved_unpublished_artifacts.set(float(summary["approved_unpublished_artifacts"]))
        self.pending_change_documents.set(float(summary["pending_change_documents"]))

        for idx_name, mismatch in alert_report.fts5_mismatches.items():
            self.fts5_mismatch_total.labels(index=idx_name).set(float(mismatch))

        for st, cnt in outbox_counts.items():
            self.outbox_events.labels(status=st).set(float(cnt))
        self.dlq_items.set(float(alert_report.dlq_count))

        self.active_alerts.labels(severity="warning").set(float(alert_report.warning_count))
        self.active_alerts.labels(severity="critical").set(float(alert_report.critical_count))

        for profile, info in runs_detailed.items():
            dt = info.get("finished_at_utc") or info.get("started_at_utc")
            if isinstance(dt, datetime):
                self.last_success_timestamp_seconds.labels(profile=profile).set(dt.timestamp())

        if scope_path is not None and scope_path.is_file():
            try:
                scope, _ = load_crawl_scope(scope_path)
                approved_cnt = (
                    len(scope.approvals.pages.urls)
                    + len(scope.approvals.image_posters.urls)
                    + len(scope.approvals.documents.urls)
                )
                excluded_cnt = len(scope.exclusions.urls)
                self.scope_exact_urls.labels(disposition="approved").set(float(approved_cnt))
                self.scope_exact_urls.labels(disposition="excluded").set(float(excluded_cnt))
            except Exception:
                pass

    def render_prometheus(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
