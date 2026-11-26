"""Bounded retry helper with exponential backoff, jitter, and task_attempts audit trail."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pdn_sync.domain.policy import RetryPolicy
from pdn_sync.persistence.ops_repo import OpsRepository


class RetryExhaustedError(RuntimeError):
    """Raised when a retryable operation exhausts all configured attempts."""

    def __init__(self, message: str, *, attempts: int, last_error: Exception) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


def compute_backoff_seconds(
    attempt_number: int,
    *,
    policy: RetryPolicy,
    rng: random.Random | None = None,
) -> float:
    """Compute bounded exponential backoff with deterministic optional jitter."""
    raw = min(
        policy.backoff_max_seconds,
        policy.backoff_base_seconds * (2.0 ** max(0, attempt_number - 1)),
    )
    if policy.jitter_ratio <= 0.0:
        return round(raw, 4)
    gen = rng or random
    jitter_span = raw * policy.jitter_ratio
    return round(max(0.0, raw + gen.uniform(-jitter_span, jitter_span)), 4)


def execute_with_retry[T](
    fn: Callable[[], T],
    *,
    task_type: str,
    policy: RetryPolicy | None = None,
    ops_repo: OpsRepository | None = None,
    run_id: str | None = None,
    resource_id: str | None = None,
    version_id: str | None = None,
    correlation_id: str | None = None,
    is_retryable: Callable[[Exception], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Execute `fn` with bounded exponential backoff and optional SQLite task_attempts audit."""
    cfg = policy or RetryPolicy()
    corr_id = correlation_id or f"corr_{uuid.uuid4().hex[:12]}"
    last_exc: Exception | None = None

    for attempt in range(1, cfg.max_attempts + 1):
        t0 = time.monotonic()
        try:
            result = fn()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if ops_repo is not None:
                ops_repo.record_task_attempt(
                    task_type=task_type,
                    status="succeeded",
                    correlation_id=corr_id,
                    attempt_number=attempt,
                    run_id=run_id,
                    resource_id=resource_id,
                    version_id=version_id,
                    latency_ms=elapsed_ms,
                )
            return result
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            last_exc = exc
            can_retry = (is_retryable(exc) if is_retryable is not None else True) and (
                attempt < cfg.max_attempts
            )
            delay_s = compute_backoff_seconds(attempt, policy=cfg, rng=rng) if can_retry else 0.0
            next_retry_dt = datetime.now(UTC) + timedelta(seconds=delay_s) if can_retry else None
            if ops_repo is not None:
                ops_repo.record_task_attempt(
                    task_type=task_type,
                    status="retryable_failed" if can_retry else "dead_lettered",
                    correlation_id=corr_id,
                    attempt_number=attempt,
                    run_id=run_id,
                    resource_id=resource_id,
                    version_id=version_id,
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                    latency_ms=elapsed_ms,
                    next_retry_at_utc=next_retry_dt,
                )
            if not can_retry:
                if is_retryable is not None and not is_retryable(exc):
                    raise
                raise RetryExhaustedError(
                    f"{task_type} failed after {attempt} attempt(s): {exc}",
                    attempts=attempt,
                    last_error=exc,
                ) from exc
            if delay_s > 0:
                sleep_fn(delay_s)

    assert last_exc is not None
    raise RetryExhaustedError(
        f"{task_type} exhausted retries: {last_exc}",
        attempts=cfg.max_attempts,
        last_error=last_exc,
    ) from last_exc
