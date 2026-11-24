"""Five-point source-access clearance guardrail (AGENTS.md Section 2 Rule 1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pdn_sync.config import AppSettings, ConfigurationError
from pdn_sync.crawling.robots import SubfolderRobotsPolicy
from pdn_sync.domain.policy import PolitenessPolicy


class LiveCrawlBlockedError(RuntimeError):
    """Raised when live crawling is attempted without passing all clearance checks."""


@dataclass(frozen=True, slots=True)
class SourceAccessEvidence:
    """Verified evidence supporting Criteria 1-3 of the clearance checklist."""

    v2_robots_fetched: bool
    v2_robots_policy: SubfolderRobotsPolicy | None = None
    publicly_reachable_without_controls: bool = False
    no_prohibiting_terms_or_directives: bool = False
    verified_date: str | None = None


@dataclass(frozen=True, slots=True)
class ClearanceResult:
    cleared: bool
    user_agent: str | None
    blocking_reasons: list[str] = field(default_factory=list)


def evaluate_source_clearance(
    *,
    settings: AppSettings,
    politeness: PolitenessPolicy,
    evidence: SourceAccessEvidence,
    require_live_flag: bool = True,
) -> ClearanceResult:
    """Evaluate all 5 source-access clearance criteria and fail closed on any gap."""
    reasons: list[str] = []
    user_agent: str | None = None

    # Criterion 1: /v2/robots.txt fetched successfully
    if not evidence.v2_robots_fetched or evidence.v2_robots_policy is None:
        reasons.append("criterion_1_failed: /v2/robots.txt not fetched or unavailable")

    # Criterion 2: publicly reachable without login/CAPTCHA/access controls
    if not evidence.publicly_reachable_without_controls:
        reasons.append(
            "criterion_2_failed: target not verified publicly reachable without access controls"
        )

    # Criterion 3: no prohibiting site terms / X-Robots-Tag / meta robots
    if not evidence.no_prohibiting_terms_or_directives:
        reasons.append("criterion_3_failed: site terms / robots directives clearance not verified")

    # Criterion 4: politeness limits and identifiable User-Agent configured
    try:
        user_agent = settings.user_agent()
    except ConfigurationError as exc:
        reasons.append(f"criterion_4_failed: {exc}")

    if politeness.max_concurrency_per_host > 1:
        reasons.append(
            "criterion_4_failed: max_concurrency_per_host must be 1 for default polite crawling"
        )
    if politeness.delay_min_seconds < 2.0 or politeness.delay_max_seconds < 2.0:
        reasons.append("criterion_4_failed: request delay must be >= 2.0 seconds")

    # Criterion 5: human owner acknowledgement of docs/source-policy.md
    if not settings.source_policy_acknowledged:
        reasons.append(
            "criterion_5_failed: docs/source-policy.md not acknowledged "
            "(set PDN_SOURCE_POLICY_ACKNOWLEDGED=true after owner confirmation)"
        )

    # Explicit runtime switch
    if require_live_flag and not settings.live_crawl_enabled:
        reasons.append(
            "live_crawl_disabled: set PDN_LIVE_CRAWL_ENABLED=true to permit live requests"
        )

    return ClearanceResult(
        cleared=len(reasons) == 0,
        user_agent=user_agent,
        blocking_reasons=reasons,
    )


def require_live_clearance(
    *,
    settings: AppSettings,
    politeness: PolitenessPolicy,
    evidence: SourceAccessEvidence,
    require_live_flag: bool = True,
) -> str:
    """Assert clearance and return the validated User-Agent, or raise LiveCrawlBlockedError."""
    result = evaluate_source_clearance(
        settings=settings,
        politeness=politeness,
        evidence=evidence,
        require_live_flag=require_live_flag,
    )
    if not result.cleared or result.user_agent is None:
        joined = "; ".join(result.blocking_reasons)
        raise LiveCrawlBlockedError(f"Live crawling blocked: {joined}")
    return result.user_agent
