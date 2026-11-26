"""Structured JSON logging with correlation contextvars and secret/PII redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import structlog
from structlog.contextvars import bound_contextvars, merge_contextvars
from structlog.typing import EventDict, WrappedLogger

_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|secret|token|authorization|api[_-]?key|cookie|credential|raw_html|raw_body)",
    re.IGNORECASE,
)
_MAX_LOG_VALUE_CHARS = 500


def redact_sensitive_fields(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Scrub secret/PII payload keys and clamp overly long strings before rendering."""
    sanitized: EventDict = {}
    for key, value in event_dict.items():
        if _SENSITIVE_KEY_RE.search(str(key)):
            sanitized[key] = "[REDACTED]"
            continue
        if isinstance(value, str) and len(value) > _MAX_LOG_VALUE_CHARS:
            sanitized[key] = f"{value[:_MAX_LOG_VALUE_CHARS]}...<truncated:{len(value)}>"
        else:
            sanitized[key] = value
    return sanitized


def configure_logging(
    *,
    log_format: Literal["json", "console"] = "json",
    log_level: str = "INFO",
    stream: Any = None,
) -> None:
    """Configure structlog for deterministic JSON (or dev console) structured logs."""
    level_num = getattr(logging, log_level.upper(), logging.INFO)
    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    target_stream = stream if stream is not None else sys.stderr

    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_sensitive_fields,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=target_stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "pdn_sync") -> structlog.stdlib.BoundLogger | Any:
    """Return a structlog logger bound with the module name."""
    return structlog.get_logger(name)


@contextmanager
def log_context(
    *,
    run_id: str | None = None,
    resource_id: str | None = None,
    version_id: str | None = None,
    task_id: str | None = None,
    correlation_id: str | None = None,
    **extra: Any,
) -> Iterator[None]:
    """Bind standard AGENTS.md §11 correlation fields for the current execution scope."""
    fields: dict[str, Any] = {}
    if run_id is not None:
        fields["run_id"] = run_id
    if resource_id is not None:
        fields["resource_id"] = resource_id
    if version_id is not None:
        fields["version_id"] = version_id
    if task_id is not None:
        fields["task_id"] = task_id
    if correlation_id is not None:
        fields["correlation_id"] = correlation_id
    fields.update({k: v for k, v in extra.items() if v is not None})
    with bound_contextvars(**fields):
        yield
