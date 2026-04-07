"""Tests for configuration loading and environment variable overrides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from context_analyzer_tool.config import (
    CATConfig,
    get_db_path,
    load_config,
    write_default_config,
)

# ---------------------------------------------------------------------------
# Default config tests
# ---------------------------------------------------------------------------


def test_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """CATConfig() with no arguments should have correct defaults."""
    # Clear any CAT_* env vars that could override defaults.
    for key in list(os.environ):
        if key.startswith("CAT_"):
            monkeypatch.delenv(key)

    cfg = CATConfig()

    assert cfg.collector.host == "127.0.0.1"
    assert cfg.collector.port == 7821
    assert cfg.collector.db_path == "~/.context-analyzer-tool/context_analyzer_tool.db"

    assert cfg.anomaly.z_score_threshold == 2.0
    assert cfg.anomaly.min_sample_count == 5
    assert cfg.anomaly.cooldown_seconds == 60
    assert cfg.anomaly.task_types_ignored == []
    assert cfg.anomaly.baseline_window == 20

    assert cfg.classifier.enabled is True
    assert cfg.classifier.model == "claude-haiku-4-5-20251001"
    assert cfg.classifier.max_tokens == 150
    assert cfg.classifier.cache_results is True

    assert cfg.notifications.statusline is True
    assert cfg.notifications.system_notification is True
    assert cfg.notifications.in_session_alert is True
    assert cfg.notifications.webhook_url == ""

    assert cfg.dashboard.refresh_rate == 2.0

    assert cfg.hooks.timeout_seconds == 2.0
    assert cfg.hooks.chars_per_token_estimate == 4

    assert cfg.server.session_idle_cleanup_ms == 3_600_000
    assert cfg.server.baseline_update_interval == 5

    assert cfg.retention.retention_days == 30


# ---------------------------------------------------------------------------
# TOML loading tests
# ---------------------------------------------------------------------------


def test_load_from_toml(tmp_path: Path) -> None:
    """Write a TOML file, load it, and verify values are picked up."""
    toml_content = """\
[collector]
host = "0.0.0.0"
port = 9999
db_path = "/custom/path/data.db"

[anomaly]
z_score_threshold = 3.5
min_sample_count = 10

[dashboard]
refresh_rate = 1.0
"""
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(toml_content, encoding="utf-8")

    cfg = load_config(toml_file)

    assert cfg.collector.host == "0.0.0.0"
    assert cfg.collector.port == 9999
    assert cfg.collector.db_path == "/custom/path/data.db"
    assert cfg.anomaly.z_score_threshold == 3.5
    assert cfg.anomaly.min_sample_count == 10
    assert cfg.dashboard.refresh_rate == 1.0

    # Sections not in the TOML should still have defaults.
    assert cfg.classifier.enabled is True
    assert cfg.notifications.webhook_url == ""


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    """When the config file does not exist, load_config returns defaults."""
    non_existent = tmp_path / "does-not-exist.toml"
    cfg = load_config(non_existent)

    assert cfg.collector.port == 7821
    assert cfg.anomaly.z_score_threshold == 2.0
    assert cfg.dashboard.refresh_rate == 2.0


# ---------------------------------------------------------------------------
# Environment variable override tests
# ---------------------------------------------------------------------------


def test_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """CAT_COLLECTOR_PORT should override the default port."""
    monkeypatch.setenv("CAT_COLLECTOR_PORT", "1234")

    cfg = CATConfig()

    assert cfg.collector.port == 1234


def test_env_var_override_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boolean env vars should be coerced correctly."""
    monkeypatch.setenv("CAT_CLASSIFIER_ENABLED", "false")

    cfg = CATConfig()

    assert cfg.classifier.enabled is False


def test_env_var_override_float(monkeypatch: pytest.MonkeyPatch) -> None:
    """Float env vars should be coerced correctly."""
    monkeypatch.setenv("CAT_ANOMALY_Z_SCORE_THRESHOLD", "4.5")

    cfg = CATConfig()

    assert cfg.anomaly.z_score_threshold == 4.5


def test_env_var_override_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """List env vars should be split on commas."""
    monkeypatch.setenv("CAT_ANOMALY_TASK_TYPES_IGNORED", "chat,edit,search")

    cfg = CATConfig()

    assert cfg.anomaly.task_types_ignored == ["chat", "edit", "search"]


def test_env_var_override_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """String env vars should be set directly."""
    monkeypatch.setenv("CAT_COLLECTOR_HOST", "0.0.0.0")

    cfg = CATConfig()

    assert cfg.collector.host == "0.0.0.0"


# ---------------------------------------------------------------------------
# db_path expansion tests
# ---------------------------------------------------------------------------


def test_db_path_expansion() -> None:
    """The tilde in db_path should be expanded to the user's home directory."""
    cfg = CATConfig()
    resolved = get_db_path(cfg)

    # The resolved path should NOT contain a tilde.
    assert "~" not in resolved
    # It should be an absolute path.
    assert os.path.isabs(resolved)


def test_db_path_expansion_custom() -> None:
    """A custom db_path with ~ should also be expanded."""
    cfg = CATConfig()
    cfg.collector.db_path = "~/my-data/pulse.db"
    resolved = get_db_path(cfg)

    assert "~" not in resolved
    assert resolved.endswith("pulse.db")


# ---------------------------------------------------------------------------
# write_default_config tests
# ---------------------------------------------------------------------------


def test_write_default_config(tmp_path: Path) -> None:
    """write_default_config should produce a valid TOML that round-trips."""
    target = tmp_path / "config.toml"
    result_path = write_default_config(target)

    assert result_path == target
    assert target.exists()

    # Loading the written file should succeed and match defaults.
    cfg = load_config(target)
    assert cfg.collector.port == 7821
    assert cfg.anomaly.z_score_threshold == 2.0
