"""Application configuration loading and fail-closed User-Agent / YAML loaders."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from pdn_sync.domain.policy import IngestionPolicyConfig
from pdn_sync.domain.scope import CrawlScopeConfig

USER_AGENT_PRODUCT = "PDN-Knowledge-Sync/0.1"

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PLACEHOLDER_EMAILS: frozenset[str] = frozenset(
    {
        "your-email@example.com",
        "you@example.com",
        "example@example.com",
        "contact@example.com",
        "admin@example.com",
        "changeme@example.com",
        "<contact_email>",
    }
)


class ConfigurationError(RuntimeError):
    """Raised when required configuration or YAML policy files are missing or invalid."""


def build_user_agent(contact_email: str | None) -> str:
    """Build the identifiable User-Agent header, failing closed if contact email is invalid."""
    cleaned = (contact_email or "").strip()
    if not cleaned:
        raise ConfigurationError("PDN_CONTACT_EMAIL is required for live requests (fail-closed).")
    if cleaned.lower() in _PLACEHOLDER_EMAILS or "<" in cleaned or ">" in cleaned:
        raise ConfigurationError(
            f"PDN_CONTACT_EMAIL appears to be a placeholder ({cleaned!r}); configure a real email."
        )
    if not _EMAIL_RE.match(cleaned):
        raise ConfigurationError(f"PDN_CONTACT_EMAIL is not a valid email address: {cleaned!r}")
    return f"{USER_AGENT_PRODUCT} (+contact: {cleaned})"


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables and optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="PDN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    contact_email: str | None = Field(default=None)
    operator_id: str = Field(default="project-owner", min_length=1)

    source_policy_acknowledged: bool = Field(default=False)
    live_crawl_enabled: bool = Field(default=False)

    scope_file: Path = Field(default=Path("config/crawl_scope.yaml"))
    ingestion_policy_file: Path = Field(default=Path("config/ingestion_policy.yaml"))
    data_dir: Path = Field(default=Path("data"))
    sqlite_path: Path = Field(default=Path("data/pdn_sync.db"))

    max_concurrency_per_host: int = Field(default=1, ge=1, le=2)
    delay_min_seconds: float = Field(default=2.0, ge=2.0)
    delay_max_seconds: float = Field(default=5.0, ge=2.0)
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    max_document_bytes: int = Field(default=50 * 1024 * 1024, gt=0)

    def user_agent(self) -> str:
        """Return validated identifiable User-Agent string, or fail closed."""
        return build_user_agent(self.contact_email)


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file on disk."""
    if not path.is_file():
        raise ConfigurationError(f"Required configuration file not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")

    yaml = YAML(typ="safe")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise ConfigurationError(f"Failed to parse YAML file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"YAML file {path} must contain a top-level mapping")
    return raw


def load_crawl_scope(path: Path) -> tuple[CrawlScopeConfig, str]:
    """Load and strictly validate config/crawl_scope.yaml (fail closed)."""
    file_hash = compute_file_sha256(path)
    raw = _read_yaml_mapping(path)
    try:
        config = CrawlScopeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid crawl scope in {path}:\n{exc}") from exc
    return config, file_hash


def load_ingestion_policy(path: Path) -> IngestionPolicyConfig:
    """Load and strictly validate config/ingestion_policy.yaml (fail closed)."""
    raw = _read_yaml_mapping(path)
    try:
        return IngestionPolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid ingestion policy in {path}:\n{exc}") from exc
