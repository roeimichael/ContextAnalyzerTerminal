"""TOML configuration loader for context-pulse."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any, get_origin

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("context_pulse.config")


def get_config_dir() -> Path:
    """Return the config directory, respecting ``CONTEXT_PULSE_CONFIG_DIR`` env var."""
    env_dir = os.environ.get("CONTEXT_PULSE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".context-pulse"


def get_config_path() -> Path:
    """Return the path to ``config.toml``."""
    return get_config_dir() / "config.toml"


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class CollectorConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7821
    db_path: str = "~/.context-pulse/context_pulse.db"


class AnomalyConfig(BaseModel):
    z_score_threshold: float = 2.0
    min_sample_count: int = 5
    cooldown_seconds: int = 60
    task_types_ignored: list[str] = Field(default_factory=list)
    baseline_window: int = 20  # number of recent samples for rolling stats


class ClassifierConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 150
    cache_results: bool = True


class NotificationsConfig(BaseModel):
    statusline: bool = True
    system_notification: bool = True
    in_session_alert: bool = True
    webhook_url: str = ""


class HooksConfig(BaseModel):
    # Timeout in seconds for hook HTTP calls to the collector
    timeout_seconds: float = 2.0
    # Approximate characters per token for hook-side token estimation.
    # This is a rough heuristic (~4 chars/token for English text).
    chars_per_token_estimate: int = 4


class ServerConfig(BaseModel):
    # Session idle cleanup threshold in milliseconds (default: 1 hour)
    session_idle_cleanup_ms: int = 3_600_000
    # Session restore lookback window in milliseconds (default: 30 minutes)
    session_restore_lookback_ms: int = 1_800_000
    # Seconds between baseline update flushes
    baseline_update_interval: int = 5


class RetentionConfig(BaseModel):
    # Days to keep data (0 = keep forever)
    retention_days: int = 30


class DashboardConfig(BaseModel):
    # Seconds between TUI refreshes
    refresh_rate: float = 2.0


_ENV_PREFIX = "CONTEXT_PULSE_"


class ContextPulseConfig(BaseModel):
    """Root configuration model. Maps 1:1 to config.toml sections."""

    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)

    @model_validator(mode="after")
    def _apply_env_overrides(self) -> ContextPulseConfig:
        """Override fields from environment variables.

        Pattern: ``CONTEXT_PULSE_{SECTION}_{KEY}`` (uppercase).
        For example ``CONTEXT_PULSE_COLLECTOR_PORT=7822`` sets
        ``collector.port`` to ``7822``.
        """
        sections: dict[str, BaseModel] = {
            "collector": self.collector,
            "anomaly": self.anomaly,
            "classifier": self.classifier,
            "notifications": self.notifications,
            "hooks": self.hooks,
            "server": self.server,
            "retention": self.retention,
            "dashboard": self.dashboard,
        }
        for section_name, section in sections.items():
            for field_name, field_info in type(section).model_fields.items():
                env_key = f"{_ENV_PREFIX}{section_name.upper()}_{field_name.upper()}"
                env_val = os.environ.get(env_key)
                if env_val is None:
                    continue
                annotation = field_info.annotation
                if annotation is None:
                    continue
                try:
                    coerced: Any
                    if annotation is bool:
                        coerced = env_val.lower() in ("1", "true", "yes")
                    elif annotation is int:
                        coerced = int(env_val)
                    elif annotation is float:
                        coerced = float(env_val)
                    elif get_origin(annotation) is list:
                        coerced = [
                            s.strip()
                            for s in env_val.split(",")
                            if s.strip()
                        ]
                    else:
                        coerced = env_val
                    setattr(section, field_name, coerced)
                    logger.debug(
                        "Env override %s -> %s.%s = %r",
                        env_key,
                        section_name,
                        field_name,
                        coerced,
                    )
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Ignoring invalid env var %s=%r: %s",
                        env_key,
                        env_val,
                        exc,
                    )
        return self


# ---------------------------------------------------------------------------
# Default TOML template (with comments)
# ---------------------------------------------------------------------------

_DEFAULT_TOML_TEMPLATE = """\
# context-pulse configuration
# Location: ~/.context-pulse/config.toml (or CONTEXT_PULSE_CONFIG_DIR)
# All values can be overridden via environment variables:
#   CONTEXT_PULSE_{SECTION}_{KEY} (uppercase)

[collector]
# Host and port for the collector HTTP server
host = "127.0.0.1"
port = 7821
# Path to SQLite database (~ is expanded)
db_path = "~/.context-pulse/context_pulse.db"

[anomaly]
# Z-score threshold for anomaly detection
z_score_threshold = 2.0
# Minimum samples before anomaly detection activates
min_sample_count = 5
# Seconds before re-alerting for the same session
cooldown_seconds = 60
# Task types to exclude from anomaly detection
task_types_ignored = []
# Number of recent samples for rolling baseline
baseline_window = 20

[classifier]
# Enable LLM-based root cause classification (requires 'anthropic' package)
enabled = true
# Model to use for classification
model = "claude-haiku-4-5-20251001"
# Max tokens for classifier response
max_tokens = 150
# Cache classifier results to avoid redundant calls
cache_results = true

[notifications]
# Show context-pulse data in Claude Code statusline
statusline = true
# Fire OS-level notifications on anomalies
system_notification = true
# Inject alerts into Claude Code via additionalContext
in_session_alert = true
# Webhook URL for Slack/Discord/custom (empty = disabled)
webhook_url = ""

[hooks]
# Timeout in seconds for hook HTTP calls to the collector
timeout_seconds = 2.0
# Approximate characters per token for hook-side estimation.
# This is a rough heuristic (~4 chars/token for English text).
# The actual delta computation uses real statusline data, not this estimate.
chars_per_token_estimate = 4

[server]
# Session idle cleanup threshold in milliseconds (default: 1 hour)
session_idle_cleanup_ms = 3600000
# Session restore lookback window in milliseconds (default: 30 minutes)
session_restore_lookback_ms = 1800000
# Seconds between baseline update flushes
baseline_update_interval = 5

[retention]
# Days to keep data in the database (0 = keep forever)
retention_days = 30

[dashboard]
# Seconds between TUI dashboard refreshes
refresh_rate = 2.0
"""

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def load_config(config_path: Path | None = None) -> ContextPulseConfig:
    """Load config from a TOML file.

    1. If *config_path* is ``None``, use :func:`get_config_path`.
    2. If the file does not exist, return ``ContextPulseConfig()`` (all defaults).
    3. Parse TOML, validate with Pydantic.
    4. Expand ``~`` in ``db_path``.

    Raises:
        ValueError: If the TOML content is malformed.
    """
    path = config_path if config_path is not None else get_config_path()
    path = path.expanduser()

    if not path.exists():
        logger.info("Config file not found at %s — using defaults.", path)
        return ContextPulseConfig()

    logger.info("Loading config from %s", path)
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Malformed TOML in {path}: {exc}") from exc

    return ContextPulseConfig.model_validate(data)


def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist.

    Returns:
        The directory :class:`~pathlib.Path`.
    """
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Ensured config directory exists: %s", config_dir)
    return config_dir


def get_db_path(config: ContextPulseConfig) -> str:
    """Resolve *db_path* from *config*, expanding ``~``.

    Returns:
        Absolute path string.
    """
    return str(Path(config.collector.db_path).expanduser().resolve())


def write_default_config(path: Path | None = None) -> Path:
    """Write a default ``config.toml`` with comments to *path*.

    If *path* is ``None``, writes to :func:`get_config_path`.

    Returns:
        The :class:`~pathlib.Path` that was written.
    """
    target = path if path is not None else get_config_path()
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_DEFAULT_TOML_TEMPLATE, encoding="utf-8")
    logger.info("Wrote default config to %s", target)
    return target
