"""Gate A crawl-scope schema (config/crawl_scope.yaml) and precedence evaluator."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pdn_sync.crawling.url_policy import is_in_allowed_scope, normalize_url
from pdn_sync.domain.policy import Disposition, ResourceType, SupersessionPolicy


class ExclusionPatternRule(BaseModel):
    """Pattern or query exclusion rule. Broad /** wildcards ARE allowed here."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    pattern: str | None = None
    query_param: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_matcher_present(self) -> ExclusionPatternRule:
        if not self.pattern and not self.query_param:
            msg = "ExclusionPatternRule must specify at least one of 'pattern' or 'query_param'"
            raise ValueError(msg)
        if self.pattern is not None and not self.pattern.startswith("/"):
            msg = f"Exclusion pattern must be path-anchored starting with '/': {self.pattern!r}"
            raise ValueError(msg)
        if self.query_param is not None and "=" not in self.query_param:
            msg = f"query_param exclusion must be in 'key=value' format: {self.query_param!r}"
            raise ValueError(msg)
        return self


class ExclusionsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(default_factory=list)
    patterns: list[ExclusionPatternRule] = Field(default_factory=list)


class ApprovalUrlRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    check_interval: str = Field(default="24h", min_length=2)
    approved_by: str = Field(min_length=1)
    approved_at: datetime


class ApprovalPatternRule(BaseModel):
    """Bounded pattern approval rule. Broad /** wildcards are FORBIDDEN here."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    resource_type: ResourceType
    reason: str = Field(min_length=1)
    max_matches: int = Field(ge=1, le=500)
    check_interval: str = Field(default="24h", min_length=2)
    supersession: SupersessionPolicy = Field(default=SupersessionPolicy.NONE)
    approved_by: str = Field(min_length=1)
    approved_at: datetime

    @field_validator("pattern")
    @classmethod
    def reject_unbounded_wildcards(cls, value: str) -> str:
        if "**" in value:
            msg = (
                "Approval patterns must not use unbounded cross-directory wildcards ('**'); "
                "use a bounded single-directory glob instead."
            )
            raise ValueError(msg)
        if not value.startswith("/"):
            msg = f"Approval pattern must be path-anchored starting with '/': {value!r}"
            raise ValueError(msg)
        return value


class ResourceApprovals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    urls: list[ApprovalUrlRule] = Field(default_factory=list)
    patterns: list[ApprovalPatternRule] = Field(default_factory=list)


class ApprovalsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: ResourceApprovals = Field(default_factory=ResourceApprovals)
    image_posters: ResourceApprovals = Field(default_factory=ResourceApprovals)
    documents: ResourceApprovals = Field(default_factory=ResourceApprovals)


class CrawlScopeConfig(BaseModel):
    """Schema for config/crawl_scope.yaml (single source of truth for Gate A)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    allowed_host: str = Field(default="pdn.gov.my", min_length=1)
    allowed_path_prefix: str = Field(default="/v2", min_length=1)
    exclusions: ExclusionsSection = Field(default_factory=ExclusionsSection)
    approvals: ApprovalsSection = Field(default_factory=ApprovalsSection)

    @field_validator("allowed_path_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            msg = "allowed_path_prefix must start with '/'"
            raise ValueError(msg)
        return value.rstrip("/") or "/"

    @model_validator(mode="after")
    def validate_all_approval_rules_in_scope(self) -> CrawlScopeConfig:
        seen_ids: set[str] = set()
        for excl in self.exclusions.patterns:
            if excl.id in seen_ids:
                msg = f"Duplicate rule id in crawl_scope.yaml: {excl.id!r}"
                raise ValueError(msg)
            seen_ids.add(excl.id)

        prefix = self.allowed_path_prefix
        sections = [
            (ResourceType.PAGE, self.approvals.pages),
            (ResourceType.IMAGE_POSTER, self.approvals.image_posters),
            (ResourceType.DOCUMENT, self.approvals.documents),
        ]
        for expected_type, section in sections:
            for url_rule in section.urls:
                if not is_in_allowed_scope(
                    url_rule.url,
                    allowed_host=self.allowed_host,
                    allowed_path_prefix=prefix,
                ):
                    msg = (
                        f"Approved URL {url_rule.url!r} is outside "
                        f"https://{self.allowed_host}{prefix}"
                    )
                    raise ValueError(msg)

            for pat_rule in section.patterns:
                if pat_rule.id in seen_ids:
                    msg = f"Duplicate rule id in crawl_scope.yaml: {pat_rule.id!r}"
                    raise ValueError(msg)
                seen_ids.add(pat_rule.id)

                if pat_rule.resource_type != expected_type:
                    msg = (
                        f"Pattern rule {pat_rule.id!r} has resource_type={pat_rule.resource_type} "
                        f"inside {expected_type} section"
                    )
                    raise ValueError(msg)

                if not (pat_rule.pattern == prefix or pat_rule.pattern.startswith(f"{prefix}/")):
                    msg = f"Approval pattern {pat_rule.pattern!r} must be anchored under {prefix!r}"
                    raise ValueError(msg)

                # Reject top-level wildcard like `/v2/*` that effectively approves the whole prefix
                if pat_rule.pattern.rstrip("/") == f"{prefix}/*":
                    msg = (
                        f"Approval pattern {pat_rule.pattern!r} is too broad; "
                        "anchor to a specific subdirectory."
                    )
                    raise ValueError(msg)

        return self


@dataclass(frozen=True, slots=True)
class ScopeEvaluation:
    disposition: Disposition
    reason: str
    rule_id: str | None = None
    normalized_url: str | None = None


def _glob_to_regex(pattern: str, *, allow_double_star: bool) -> re.Pattern[str]:
    """Convert path glob to anchored regex.

    When allow_double_star=True, `/**` matches zero or more path segments.
    Single `*` matches only within a single path segment (no `/`).
    """
    if allow_double_star:
        # Handle trailing `/**` to also match the directory itself
        placeholder = "__DOUBLE_STAR__"
        escaped = pattern.replace("/**", placeholder)
        parts: list[str] = []
        i = 0
        while i < len(escaped):
            if escaped.startswith(placeholder, i):
                parts.append("(?:/.*)?")
                i += len(placeholder)
            elif escaped[i] == "*":
                parts.append("[^/]*")
                i += 1
            elif escaped[i] == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(escaped[i]))
                i += 1
        return re.compile("^" + "".join(parts) + "$", flags=re.IGNORECASE)

    # Single-segment glob only (`*` never crosses `/`)
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append("[^/]*")
        elif ch == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(ch))
    return re.compile("^" + "".join(parts) + "$", flags=re.IGNORECASE)


def _matches_query_exclusion(query_string: str, query_rule: str) -> bool:
    rule_key, rule_val = query_rule.split("=", 1)
    rule_key_l = rule_key.lower()
    rule_val_l = rule_val.lower()
    for key, val in parse_qsl(query_string, keep_blank_values=True):
        if key.lower() == rule_key_l and fnmatch.fnmatch(val.lower(), rule_val_l):
            return True
    return False


def _exact_url_key(normalized_url: str, *, allowed_path_prefix: str) -> str:
    """Compare exact URLs ignoring redundant trailing slashes on subpath directories."""
    parts = urlsplit(normalized_url)
    prefix = allowed_path_prefix.rstrip("/")
    path = parts.path
    if path not in {"/", f"{prefix}/"} and path.endswith("/"):
        path = path.rstrip("/")
    query_part = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc}{path}{query_part}"


def evaluate_scope(
    raw_url: str,
    scope: CrawlScopeConfig,
    *,
    resource_type: ResourceType | None = None,
) -> ScopeEvaluation:
    """Evaluate a URL against config/crawl_scope.yaml with strict precedence.

    Precedence (highest first):
    1. Out-of-scope host/prefix -> excluded
    2. Explicit URL exclusion
    3. Pattern / query exclusion
    4. Explicit URL approval
    5. Bounded pattern approval
    6. Default: pending
    """
    try:
        normalized = normalize_url(
            raw_url,
            allowed_host=scope.allowed_host,
            allowed_path_prefix=scope.allowed_path_prefix,
        )
    except Exception as exc:
        return ScopeEvaluation(
            disposition=Disposition.EXCLUDED,
            reason=f"invalid_url:{exc}",
        )

    if not is_in_allowed_scope(
        normalized,
        allowed_host=scope.allowed_host,
        allowed_path_prefix=scope.allowed_path_prefix,
    ):
        return ScopeEvaluation(
            disposition=Disposition.EXCLUDED,
            reason="outside_allowed_host_or_prefix",
            normalized_url=normalized,
        )

    parts = urlsplit(normalized)
    path = parts.path
    query = parts.query

    # 1. Explicit URL exclusions
    norm_key = _exact_url_key(normalized, allowed_path_prefix=scope.allowed_path_prefix)
    normalized_excl_urls = {
        _exact_url_key(
            normalize_url(
                u,
                allowed_host=scope.allowed_host,
                allowed_path_prefix=scope.allowed_path_prefix,
            ),
            allowed_path_prefix=scope.allowed_path_prefix,
        )
        for u in scope.exclusions.urls
    }
    if norm_key in normalized_excl_urls:
        return ScopeEvaluation(
            disposition=Disposition.EXCLUDED,
            reason="explicit_url_exclusion",
            normalized_url=normalized,
        )

    # 2. Pattern & query exclusions
    for excl_rule in scope.exclusions.patterns:
        path_ok = True
        query_ok = True
        if excl_rule.pattern is not None:
            regex = _glob_to_regex(excl_rule.pattern, allow_double_star=True)
            path_ok = regex.match(path) is not None
        if excl_rule.query_param is not None:
            query_ok = _matches_query_exclusion(query, excl_rule.query_param)
        if path_ok and query_ok:
            return ScopeEvaluation(
                disposition=Disposition.EXCLUDED,
                reason=excl_rule.reason,
                rule_id=excl_rule.id,
                normalized_url=normalized,
            )

    # Determine which approval sections to inspect
    sections: list[ResourceApprovals]
    if resource_type == ResourceType.PAGE:
        sections = [scope.approvals.pages]
    elif resource_type == ResourceType.IMAGE_POSTER:
        sections = [scope.approvals.image_posters]
    elif resource_type == ResourceType.DOCUMENT:
        sections = [scope.approvals.documents]
    else:
        sections = [
            scope.approvals.pages,
            scope.approvals.image_posters,
            scope.approvals.documents,
        ]

    # 3. Explicit URL approvals
    for section in sections:
        for url_rule in section.urls:
            rule_norm = normalize_url(
                url_rule.url,
                allowed_host=scope.allowed_host,
                allowed_path_prefix=scope.allowed_path_prefix,
            )
            if norm_key == _exact_url_key(rule_norm, allowed_path_prefix=scope.allowed_path_prefix):
                return ScopeEvaluation(
                    disposition=Disposition.APPROVED,
                    reason=url_rule.reason,
                    normalized_url=normalized,
                )

    # 4. Bounded pattern approvals
    for section in sections:
        for pat_rule in section.patterns:
            regex = _glob_to_regex(pat_rule.pattern, allow_double_star=False)
            if regex.match(path) is not None:
                return ScopeEvaluation(
                    disposition=Disposition.APPROVED,
                    reason=pat_rule.reason,
                    rule_id=pat_rule.id,
                    normalized_url=normalized,
                )

    # 5. Default: pending
    return ScopeEvaluation(
        disposition=Disposition.PENDING,
        reason="unmatched_default_pending",
        normalized_url=normalized,
    )
