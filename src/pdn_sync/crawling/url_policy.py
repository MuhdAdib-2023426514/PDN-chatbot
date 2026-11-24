"""URL canonicalization, host/path scoping, JEvents exclusion, and SSRF guards."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

TRACKING_QUERY_PREFIXES: tuple[str, ...] = ("utm_",)
TRACKING_QUERY_KEYS: frozenset[str] = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "_ga"}
)

BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = (".local", ".localhost", ".internal", ".home.arpa")

_MULTI_SLASH_RE = re.compile(r"/{2,}")


class UrlPolicyError(ValueError):
    """Raised when a URL violates scheme, SSRF, host/path scope, or JEvents rules."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True, slots=True)
class UrlCheckResult:
    allowed: bool
    normalized_url: str | None
    reason: str


DnsResolver = Callable[[str], list[str]]


def _default_dns_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_disallowed_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return _is_disallowed_ip(str(ip_obj.ipv4_mapped))

    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    )


def validate_ssrf_safe(
    raw_url: str,
    *,
    resolve_dns: bool = False,
    resolver: DnsResolver | None = None,
) -> None:
    """Validate that a URL uses http/https and does not target local/private addresses."""
    parts = urlsplit(raw_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError("disallowed_scheme", f"Scheme {scheme!r} is not allowed")

    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError("userinfo_not_allowed", "URL userinfo credentials are not permitted")

    hostname = (parts.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise UrlPolicyError("missing_host", "URL must include a hostname")

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise UrlPolicyError("ssrf_blocked_hostname", f"Host {hostname!r} is blocked")

    # Direct IP literal check
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True

    if is_ip_literal and _is_disallowed_ip(hostname):
        raise UrlPolicyError("ssrf_blocked_ip", f"IP address {hostname!r} is non-global/private")

    if resolve_dns:
        resolve_fn = resolver or _default_dns_resolver
        try:
            resolved_ips = resolve_fn(hostname)
        except OSError as exc:
            raise UrlPolicyError(
                "dns_resolution_failed", f"Could not resolve {hostname!r}: {exc}"
            ) from exc

        if not resolved_ips:
            raise UrlPolicyError("dns_resolution_empty", f"No IPs resolved for {hostname!r}")

        for ip_str in resolved_ips:
            if _is_disallowed_ip(ip_str):
                raise UrlPolicyError(
                    "ssrf_blocked_resolved_ip",
                    f"Host {hostname!r} resolved to non-global IP {ip_str!r}",
                )


def normalize_url(
    raw_url: str,
    *,
    allowed_host: str = "pdn.gov.my",
    allowed_path_prefix: str = "/v2",
) -> str:
    """Canonicalize an HTTP(S) URL deterministically.

    - Enforces http/https and strips userinfo/fragments
    - Lowercases scheme and host, removes default ports
    - Collapses redundant slashes in path (e.g., `/v2/images//dokumen/a.pdf`)
    - Normalizes homepage aliases (`/v2`, `/v2/`, `/v2/index.php` with no query) to `/v2/`
    - Strips tracking parameters (`utm_*`, `fbclid`, `gclid`) while keeping meaningful
      Joomla/K2 query parameters in sorted order
    """
    validate_ssrf_safe(raw_url, resolve_dns=False)
    parts = urlsplit(raw_url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"{host}:{port}" if port is not None else host

    # Collapse redundant slashes
    path = _MULTI_SLASH_RE.sub("/", parts.path or "/")
    if not path.startswith("/"):
        path = "/" + path

    # Filter tracking query parameters and sort deterministically
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    filtered_pairs = [
        (k, v)
        for k, v in query_pairs
        if k.lower() not in TRACKING_QUERY_KEYS
        and not any(k.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    filtered_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    normalized_query = urlencode(filtered_pairs, doseq=True)

    # Canonicalize PDN homepage aliases when no query parameters are present
    prefix = allowed_path_prefix.rstrip("/")
    if host == allowed_host.lower() and not normalized_query:
        homepage_aliases = {prefix, f"{prefix}/", f"{prefix}/index.php"}
        if path in homepage_aliases:
            path = f"{prefix}/"

    return urlunsplit((scheme, netloc, path, normalized_query, ""))


def is_in_allowed_scope(
    url: str,
    *,
    allowed_host: str = "pdn.gov.my",
    allowed_path_prefix: str = "/v2",
) -> bool:
    """Return True iff the URL matches the exact host and boundary-aware path prefix."""
    try:
        normalized = normalize_url(
            url,
            allowed_host=allowed_host,
            allowed_path_prefix=allowed_path_prefix,
        )
    except UrlPolicyError:
        return False

    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    if host != allowed_host.lower():
        return False

    prefix = allowed_path_prefix.rstrip("/")
    path = parts.path
    return path == prefix or path.startswith(f"{prefix}/")


def is_jevents_url(url: str) -> bool:
    """Return True if the URL targets Joomla JEvents routes or query parameters."""
    parts = urlsplit(url.strip())
    path = _MULTI_SLASH_RE.sub("/", parts.path or "").lower()

    jevents_prefixes = (
        "/v2/index.php/component/jevents",
        "/v2/component/jevents",
        "/index.php/component/jevents",
        "/component/jevents",
    )
    for prefix in jevents_prefixes:
        if path == prefix or path.startswith(f"{prefix}/"):
            return True

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() == "option" and value.lower().startswith("com_jevents"):
            return True

    return False


def validate_target_url(
    raw_url: str,
    *,
    allowed_host: str = "pdn.gov.my",
    allowed_path_prefix: str = "/v2",
    allow_jevents: bool = False,
    resolve_dns: bool = False,
    resolver: DnsResolver | None = None,
) -> str:
    """Validate SSRF safety, host/path scope, and Phase 0-3 JEvents exclusion.

    Returns the canonical normalized URL or raises UrlPolicyError.
    """
    validate_ssrf_safe(raw_url, resolve_dns=resolve_dns, resolver=resolver)
    normalized = normalize_url(
        raw_url,
        allowed_host=allowed_host,
        allowed_path_prefix=allowed_path_prefix,
    )

    if not is_in_allowed_scope(
        normalized,
        allowed_host=allowed_host,
        allowed_path_prefix=allowed_path_prefix,
    ):
        raise UrlPolicyError(
            "out_of_scope",
            f"URL {normalized!r} is outside https://{allowed_host}{allowed_path_prefix}",
        )

    if not allow_jevents and is_jevents_url(normalized):
        raise UrlPolicyError(
            "deferred_to_structured_schedule_phase",
            f"JEvents URL {normalized!r} is excluded in Phases 0-3",
        )

    return normalized
