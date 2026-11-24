"""Subfolder-scoped robots.txt parser and evaluator for https://pdn.gov.my/v2/robots.txt."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class RobotsUnavailableError(RuntimeError):
    """Raised when /v2/robots.txt cannot be fetched or is unavailable (fail closed)."""


@dataclass(frozen=True, slots=True)
class RobotsRule:
    allow: bool
    raw_pattern: str
    scoped_pattern: str


@dataclass(frozen=True, slots=True)
class RobotsFetchOutcome:
    root_robots_status: int | None
    v2_robots_status: int
    v2_robots_text: str | None


def _robots_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert an RFC 9309 robots.txt path pattern (* and trailing $) to regex."""
    anchor_end = pattern.endswith("$")
    body = pattern[:-1] if anchor_end else pattern
    escaped_parts = [re.escape(part) for part in body.split("*")]
    regex_str = "^" + ".*".join(escaped_parts) + ("$" if anchor_end else "")
    return re.compile(regex_str)


def _scope_robots_path(raw_path: str, scope_prefix: str) -> str:
    """Prefix a subfolder robots.txt rule with scope_prefix (e.g. /v2)."""
    prefix = scope_prefix.rstrip("/")
    cleaned = raw_path.strip()
    if not cleaned:
        return ""
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    if not prefix:
        return cleaned
    if cleaned == prefix or cleaned.startswith(f"{prefix}/"):
        return cleaned
    return f"{prefix}{cleaned}"


class SubfolderRobotsPolicy:
    """RFC 9309-aligned evaluator that scopes subfolder robots.txt rules under /v2/."""

    def __init__(
        self,
        *,
        scope_prefix: str,
        rules: list[RobotsRule],
        crawl_delay: float | None = None,
    ) -> None:
        self.scope_prefix = scope_prefix.rstrip("/")
        self.rules = tuple(rules)
        self.crawl_delay = crawl_delay

    @classmethod
    def parse(
        cls,
        robots_text: str,
        *,
        scope_prefix: str = "/v2",
        user_agent_token: str = "PDN-Knowledge-Sync",
    ) -> SubfolderRobotsPolicy:
        """Parse robots.txt text and select the most specific matching User-Agent group."""
        groups: list[tuple[list[str], list[tuple[bool, str]], float | None]] = []
        current_agents: list[str] = []
        current_directives: list[tuple[bool, str]] = []
        current_delay: float | None = None
        seen_directive = False

        for raw_line in robots_text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field_l = field.lower()

            if field_l == "user-agent":
                if seen_directive and current_agents:
                    groups.append((current_agents, current_directives, current_delay))
                    current_agents = []
                    current_directives = []
                    current_delay = None
                    seen_directive = False
                if value:
                    current_agents.append(value.lower())
            elif field_l == "disallow":
                seen_directive = True
                if value:
                    current_directives.append((False, value))
            elif field_l == "allow":
                seen_directive = True
                if value:
                    current_directives.append((True, value))
            elif field_l == "crawl-delay":
                seen_directive = True
                with contextlib.suppress(ValueError):
                    current_delay = float(value)

        if current_agents:
            groups.append((current_agents, current_directives, current_delay))

        # Select specific product-token groups over wildcard '*'
        token_l = user_agent_token.lower()
        specific_rules: list[RobotsRule] = []
        wildcard_rules: list[RobotsRule] = []
        specific_delay: float | None = None
        wildcard_delay: float | None = None

        for agents, directives, delay in groups:
            is_specific = any(
                agent != "*" and (token_l.startswith(agent) or agent in token_l) for agent in agents
            )
            is_wildcard = "*" in agents

            target_list: list[RobotsRule] | None = None
            if is_specific:
                target_list = specific_rules
                if delay is not None:
                    specific_delay = delay
            elif is_wildcard:
                target_list = wildcard_rules
                if delay is not None:
                    wildcard_delay = delay

            if target_list is not None:
                for is_allow, raw_path in directives:
                    scoped = _scope_robots_path(raw_path, scope_prefix)
                    if scoped:
                        target_list.append(
                            RobotsRule(
                                allow=is_allow,
                                raw_pattern=raw_path,
                                scoped_pattern=scoped,
                            )
                        )

        chosen_rules = specific_rules if specific_rules else wildcard_rules
        chosen_delay = specific_delay if specific_rules else wildcard_delay
        return cls(scope_prefix=scope_prefix, rules=chosen_rules, crawl_delay=chosen_delay)

    @classmethod
    def from_fetch_outcome(
        cls,
        outcome: RobotsFetchOutcome,
        *,
        scope_prefix: str = "/v2",
        user_agent_token: str = "PDN-Knowledge-Sync",
    ) -> SubfolderRobotsPolicy:
        """Build policy from fetch statuses.

        Root /robots.txt returning 403 is tolerated when /v2/robots.txt returns 200;
        if /v2/robots.txt is unreachable or non-200, fail closed.
        """
        if outcome.v2_robots_status != 200 or outcome.v2_robots_text is None:
            raise RobotsUnavailableError(
                f"Subfolder robots.txt at {scope_prefix}/robots.txt unavailable "
                f"(status={outcome.v2_robots_status}); live crawling disabled (fail-closed)."
            )
        return cls.parse(
            outcome.v2_robots_text,
            scope_prefix=scope_prefix,
            user_agent_token=user_agent_token,
        )

    def is_allowed(self, url_or_path: str) -> bool:
        """Return True iff the URL or path is allowed under RFC 9309 longest-match precedence."""
        parts = urlsplit(url_or_path)
        path = parts.path or "/"
        target = f"{path}?{parts.query}" if parts.query else path

        best_match: RobotsRule | None = None
        best_len = -1

        for rule in self.rules:
            regex = _robots_pattern_to_regex(rule.scoped_pattern)
            if regex.match(target) is not None:
                pattern_len = len(rule.scoped_pattern)
                if pattern_len > best_len or (
                    pattern_len == best_len and rule.allow and best_match and not best_match.allow
                ):
                    best_len = pattern_len
                    best_match = rule

        if best_match is None:
            return True
        return best_match.allow
