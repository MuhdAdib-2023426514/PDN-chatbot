"""Actionable alert evaluation engine for GET /v1/status and `pdn-sync ops check-alerts`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from pdn_sync.config import load_crawl_scope, load_ingestion_policy
from pdn_sync.domain.policy import IngestionPolicyConfig
from pdn_sync.persistence.ops_repo import OpsRepository
from pdn_sync.persistence.publication_repo import PublicationRepository
from pdn_sync.persistence.schedule_repo import ScheduleRepository

AlertSeverity = Literal["warning", "critical"]


class AlertItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: AlertSeverity
    summary: str
    details: dict[str, Any] = {}
    runbook_ref: str = "docs/operations.md#alert-triage"


@dataclass(slots=True)
class AlertReport:
    """Complete operational health snapshot and evaluated alert list."""

    checked_at_utc: datetime
    alerts: list[AlertItem] = field(default_factory=list)
    dlq_count: int = 0
    outbox_pending_count: int = 0
    outbox_failed_count: int = 0
    fts5_mismatches: dict[str, int] = field(default_factory=dict)
    oldest_pending_gate_b_age_hours: float | None = None
    qdrant_ok: bool = True
    qdrant_published_points: int = 0
    qdrant_drift_abs: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == "warning")


def _lookup_last_profile_finish(
    runs_by_profile: dict[str, dict[str, Any]],
    candidates: tuple[str, ...],
) -> datetime | None:
    best: datetime | None = None
    for key in candidates:
        info = runs_by_profile.get(key)
        if not info:
            continue
        dt = info.get("finished_at_utc") or info.get("started_at_utc")
        if isinstance(dt, datetime) and (best is None or dt > best):
            best = dt
    return best


def evaluate_alerts(
    *,
    engine: Engine,
    policy: IngestionPolicyConfig | None = None,
    policy_path: Path | None = None,
    scope_path: Path | None = None,
    qdrant_ok: bool = True,
    qdrant_published_points: int | None = None,
    now_utc: datetime | None = None,
) -> AlertReport:
    """Evaluate all AGENTS.md §11 reliability, backlog, drift, and freshness alerts."""
    now = now_utc or datetime.now(UTC)
    if policy is None:
        if policy_path is not None and policy_path.is_file():
            policy = load_ingestion_policy(policy_path)
        else:
            policy = IngestionPolicyConfig()
    obs = policy.observability

    pub_repo = PublicationRepository(engine)
    sched_repo = ScheduleRepository(engine)
    ops_repo = OpsRepository(engine)

    status = pub_repo.get_status_summary()
    _ev_cnt, _ses_cnt = sched_repo.count_published_schedules()
    outbox_counts = ops_repo.count_outbox_by_status()
    fts_mismatches = ops_repo.get_fts5_mismatch_counts()
    oldest_pending_hours = ops_repo.get_oldest_pending_gate_b_age_hours(now_utc=now)
    runs_detailed = ops_repo.get_last_completed_runs_detailed()
    disc_signals = ops_repo.get_latest_discovery_run_signals()

    published_chunks = int(status["published_chunks"])
    qdrant_pts = (
        int(qdrant_published_points) if qdrant_published_points is not None else published_chunks
    )
    qdrant_drift = abs(published_chunks - qdrant_pts)

    report = AlertReport(
        checked_at_utc=now,
        dlq_count=outbox_counts["dead_letter"],
        outbox_pending_count=outbox_counts["pending"],
        outbox_failed_count=outbox_counts["failed"],
        fts5_mismatches=fts_mismatches,
        oldest_pending_gate_b_age_hours=oldest_pending_hours,
        qdrant_ok=qdrant_ok,
        qdrant_published_points=qdrant_pts,
        qdrant_drift_abs=qdrant_drift,
    )

    # 1. Qdrant availability (critical)
    if not qdrant_ok:
        report.alerts.append(
            AlertItem(
                code="qdrant_unavailable",
                severity="critical",
                summary="Qdrant vector store health check failed.",
                details={"qdrant_published_points": qdrant_pts},
                runbook_ref="docs/operations.md#qdrant-outage-recovery",
            )
        )

    # 2. Dead-letter queue non-empty (critical)
    if report.dlq_count > obs.max_dlq_items:
        report.alerts.append(
            AlertItem(
                code="dlq_non_empty",
                severity="critical",
                summary=f"{report.dlq_count} outbox event(s) in dead_letter state.",
                details={"dlq_count": report.dlq_count, "threshold": obs.max_dlq_items},
                runbook_ref="docs/operations.md#dead-letter-queue-dlq-recovery",
            )
        )

    # 3. FTS5 sync mismatch (warning / critical if structured schedules or tables diverge)
    total_fts_mismatch = sum(fts_mismatches.values())
    if total_fts_mismatch > 0:
        sev: AlertSeverity = (
            "critical"
            if (
                fts_mismatches.get("structured_tables", 0) > 0
                or fts_mismatches.get("donation_sessions", 0) > 0
            )
            else "warning"
        )
        report.alerts.append(
            AlertItem(
                code="fts5_sync_mismatch",
                severity=sev,
                summary=f"SQLite FTS5 index drift detected ({total_fts_mismatch} mismatched rows).",
                details={"mismatches": fts_mismatches},
                runbook_ref="docs/operations.md#fts5-and-qdrant-drift-reconciliation",
            )
        )

    # 4. Qdrant <-> SQLite chunk drift
    if qdrant_ok and qdrant_published_points is not None and qdrant_drift > 0:
        report.alerts.append(
            AlertItem(
                code="qdrant_sqlite_drift",
                severity="warning",
                summary=(
                    f"Qdrant published point count ({qdrant_pts}) differs from SQLite active "
                    f"chunks ({published_chunks}) by {qdrant_drift}."
                ),
                details={
                    "sqlite_published_chunks": published_chunks,
                    "qdrant_published_points": qdrant_pts,
                    "drift_abs": qdrant_drift,
                },
                runbook_ref="docs/operations.md#fts5-and-qdrant-drift-reconciliation",
            )
        )

    # 5. Approved artifacts waiting to be published (indexing lag)
    approved_unpub = int(status["approved_unpublished_artifacts"])
    if approved_unpub > 0:
        report.alerts.append(
            AlertItem(
                code="approved_unpublished_behind",
                severity="warning",
                summary=(
                    f"{approved_unpub} Gate-B-approved artifact(s) have not been published yet."
                ),
                details={"approved_unpublished_artifacts": approved_unpub},
                runbook_ref="docs/operations.md#gate-b-review-and-publication-backlog",
            )
        )

    # 6. Gate B pending backlog count & age
    pending_gate_b = int(status["pending_gate_b"])
    if pending_gate_b > obs.max_pending_gate_b:
        report.alerts.append(
            AlertItem(
                code="gate_b_backlog_count",
                severity="warning",
                summary=(
                    f"Gate B pending review queue ({pending_gate_b}) exceeds threshold "
                    f"({obs.max_pending_gate_b})."
                ),
                details={
                    "pending_gate_b": pending_gate_b,
                    "threshold": obs.max_pending_gate_b,
                },
                runbook_ref="docs/operations.md#gate-b-review-and-publication-backlog",
            )
        )
    if oldest_pending_hours is not None and oldest_pending_hours > float(
        obs.max_pending_gate_b_age_hours
    ):
        report.alerts.append(
            AlertItem(
                code="gate_b_backlog_age",
                severity="warning",
                summary=(
                    f"Oldest pending Gate B item is {oldest_pending_hours:.1f}h old "
                    f"(threshold {obs.max_pending_gate_b_age_hours}h)."
                ),
                details={
                    "oldest_pending_gate_b_age_hours": oldest_pending_hours,
                    "threshold_hours": obs.max_pending_gate_b_age_hours,
                },
                runbook_ref="docs/operations.md#gate-b-review-and-publication-backlog",
            )
        )

    # 7. Quarantined critical-fact items
    quarantined = int(status["quarantined_gate_b"])
    if quarantined > obs.max_quarantined_gate_b:
        report.alerts.append(
            AlertItem(
                code="quarantined_backlog",
                severity="warning",
                summary=f"{quarantined} quarantined artifact(s) require manual review/override.",
                details={
                    "quarantined_gate_b": quarantined,
                    "threshold": obs.max_quarantined_gate_b,
                },
                runbook_ref="docs/operations.md#gate-b-review-and-publication-backlog",
            )
        )

    # 8. Discovery anomaly / pattern max_matches alerts
    if disc_signals.get("anomaly_halted"):
        report.alerts.append(
            AlertItem(
                code="discovery_anomaly_halted",
                severity="critical",
                summary=f"Discovery run {disc_signals.get('run_id')} halted due to growth anomaly.",
                details={
                    "run_id": disc_signals.get("run_id"),
                    "error_summary": disc_signals.get("error_summary"),
                },
                runbook_ref="docs/operations.md#discovery-anomaly-or-pattern-cap",
            )
        )
    if disc_signals.get("pattern_cap_hits"):
        report.alerts.append(
            AlertItem(
                code="pattern_max_matches_hit",
                severity="warning",
                summary="One or more rotating-asset pattern rules hit max_matches cap.",
                details={"pattern_cap_hits": disc_signals["pattern_cap_hits"]},
                runbook_ref="docs/operations.md#discovery-anomaly-or-pattern-cap",
            )
        )

    # 9. Verification freshness overdue per scheduled profile (only when DB has published content)
    has_published = int(status["published_documents"]) > 0
    if has_published:
        profile_checks: list[tuple[str, tuple[str, ...], float]] = [
            (
                "homepage",
                ("homepage", "extract_stage", "extract", "site"),
                float(obs.homepage_overdue_hours),
            ),
            ("calendar", ("calendar", "calendar_sync"), float(obs.calendar_overdue_hours)),
            ("site", ("site", "extract_stage", "extract"), float(obs.site_overdue_hours)),
            (
                "discovery_audit",
                ("discovery_audit", "discovery"),
                float(obs.discovery_overdue_days) * 24.0,
            ),
        ]
        for label, keys, max_hours in profile_checks:
            last_dt = _lookup_last_profile_finish(runs_detailed, keys)
            if last_dt is None:
                # Only warn if this profile is relevant (e.g. calendar when events exist)
                if label == "calendar" and _ev_cnt == 0:
                    continue
                report.alerts.append(
                    AlertItem(
                        code=f"verification_overdue_{label}",
                        severity="warning",
                        summary=f"No completed '{label}' verification run recorded yet.",
                        details={"profile": label, "threshold_hours": max_hours},
                        runbook_ref="docs/operations.md#verification-overdue",
                    )
                )
                continue
            age_hours = (now - last_dt).total_seconds() / 3600.0
            if age_hours > max_hours:
                report.alerts.append(
                    AlertItem(
                        code=f"verification_overdue_{label}",
                        severity="warning",
                        summary=(
                            f"Profile '{label}' last verified {age_hours:.1f}h ago "
                            f"(threshold {max_hours:.0f}h)."
                        ),
                        details={
                            "profile": label,
                            "last_completed_at_utc": last_dt.isoformat(),
                            "age_hours": round(age_hours, 2),
                            "threshold_hours": max_hours,
                        },
                        runbook_ref="docs/operations.md#verification-overdue",
                    )
                )

    # Optional scope sanity validation alert if scope_path provided
    if scope_path is not None and scope_path.is_file():
        try:
            load_crawl_scope(scope_path)
        except Exception as exc:
            report.alerts.append(
                AlertItem(
                    code="crawl_scope_invalid",
                    severity="critical",
                    summary=f"config/crawl_scope.yaml failed validation: {exc}",
                    details={"scope_path": str(scope_path)},
                    runbook_ref="docs/operations.md#scope-validation-errors",
                )
            )

    return report
