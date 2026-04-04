"""Tests for the Rich TUI dashboard panel builders (Phase 3)."""

from __future__ import annotations

from io import StringIO
from typing import Any

from rich.console import Console
from rich.panel import Panel

from context_pulse.dashboard.tui import (
    DashboardClient,
    build_anomaly_panel,
    build_header,
    build_sessions_panel,
    build_tasks_panel,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(panel: Panel) -> str:
    """Render a Rich Panel to a plain string for assertion checks."""
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=True, no_color=True)
    console.print(panel)
    return buf.getvalue()


def _make_health(**overrides: object) -> dict[str, Any]:
    """Return a health-check payload with sensible defaults."""
    data: dict[str, Any] = {
        "uptime_seconds": 3661,
        "event_count": 42,
        "snapshot_count": 7,
    }
    data.update(overrides)
    return data


def _make_status(
    sessions: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a ``/api/status`` payload with optional session/task lists."""
    data: dict[str, Any] = {}
    if sessions is not None:
        data["active_sessions"] = sessions
    if tasks is not None:
        data["recent_tasks"] = tasks
    return data


def _make_session(**overrides: object) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "session_id": "abcdef1234567890",
        "event_count": 15,
        "total_tokens_used": 12_345,
        "used_percentage": 30,
        "model_id": "claude-sonnet-4-20250514",
    }
    defaults.update(overrides)
    return defaults


def _make_task(**overrides: object) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "timestamp_ms": 1_700_000_000_000,
        "task_type": "Edit",
        "token_delta": 500,
    }
    defaults.update(overrides)
    return defaults


def _make_anomaly(**overrides: object) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "timestamp_ms": 1_700_000_000_000,
        "task_type": "Bash",
        "token_cost": 9_500,
        "z_score": 3.2,
        "severity": "high",
        "cause": "Unexpectedly large output from recursive find",
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Header tests
# ---------------------------------------------------------------------------


class TestBuildHeader:
    def test_build_header_connected(self) -> None:
        """Valid health data produces a panel containing 'Connected'."""
        panel = build_header(_make_health())
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "Connected" in rendered

    def test_build_header_disconnected(self) -> None:
        """None health data produces a panel containing 'Disconnected'."""
        panel = build_header(None)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "Disconnected" in rendered


# ---------------------------------------------------------------------------
# Sessions panel tests
# ---------------------------------------------------------------------------


class TestBuildSessionsPanel:
    def test_build_sessions_panel_with_data(self) -> None:
        """Status with active sessions renders a table with session rows."""
        sessions = [
            _make_session(session_id="aaaa1111bbbb2222", event_count=10),
            _make_session(session_id="cccc3333dddd4444", event_count=5, used_percentage=80),
        ]
        status = _make_status(sessions=sessions)
        panel = build_sessions_panel(status)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        # Truncated session IDs (first 8 chars)
        assert "aaaa1111" in rendered
        assert "cccc3333" in rendered

    def test_build_sessions_panel_empty(self) -> None:
        """None status renders 'No active sessions' message."""
        panel = build_sessions_panel(None)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "No active sessions" in rendered


# ---------------------------------------------------------------------------
# Tasks panel tests
# ---------------------------------------------------------------------------


class TestBuildTasksPanel:
    def test_build_tasks_panel_with_data(self) -> None:
        """Status with recent tasks renders bar rows."""
        tasks = [
            _make_task(task_type="Edit", token_delta=500),
            _make_task(task_type="Bash", token_delta=1200),
            _make_task(task_type="Read", token_delta=300),
        ]
        status = _make_status(tasks=tasks)
        panel = build_tasks_panel(status)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "Edit" in rendered
        assert "Bash" in rendered
        assert "Read" in rendered
        assert "500" in rendered
        assert "1,200" in rendered

    def test_build_tasks_panel_empty(self) -> None:
        """None status renders 'No tasks recorded yet' message."""
        panel = build_tasks_panel(None)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "No tasks recorded yet" in rendered


# ---------------------------------------------------------------------------
# Anomaly panel tests
# ---------------------------------------------------------------------------


class TestBuildAnomalyPanel:
    def test_build_anomaly_panel_with_data(self) -> None:
        """Anomaly list with entries renders table rows with severity."""
        anomalies = [
            _make_anomaly(severity="high", cause="Large output"),
            _make_anomaly(severity="low", cause="Minor spike", z_score=2.1, token_cost=1_200),
        ]
        panel = build_anomaly_panel(anomalies)
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "high" in rendered
        assert "low" in rendered
        assert "Large output" in rendered
        assert "Minor spike" in rendered

    def test_build_anomaly_panel_empty(self) -> None:
        """Empty anomaly list renders 'No anomalies detected' message."""
        panel = build_anomaly_panel([])
        assert isinstance(panel, Panel)
        rendered = _render(panel)
        assert "No anomalies detected" in rendered


# ---------------------------------------------------------------------------
# DashboardClient error path
# ---------------------------------------------------------------------------


class TestDashboardClient:
    def test_dashboard_client_fetch_failure(self) -> None:
        """Client pointing at unreachable port returns None without raising."""
        client = DashboardClient(base_url="http://127.0.0.1:1")
        try:
            result = client.fetch_status()
            assert result is None
        finally:
            client.close()
