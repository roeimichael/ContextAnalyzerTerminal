"""Rich Live-based terminal dashboard for context-pulse.

Auto-refreshes every 2 seconds by polling the collector HTTP API.
Displays session overview, task cost timeline, and anomaly feed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger("context_pulse.dashboard.tui")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BAR_MAX_WIDTH = 30
_SESSION_ID_TRUNCATE = 8
_CAUSE_TRUNCATE = 40
_DEFAULT_PORT = None  # resolved from config at runtime

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class DashboardClient:
    """Synchronous HTTP client for fetching data from the collector API."""

    def __init__(self, base_url: str = "http://127.0.0.1:7821") -> None:
        self._base_url = base_url
        self._client = httpx.Client(timeout=3.0)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def fetch_status(self) -> dict[str, Any] | None:
        """Fetch ``/api/status``.  Returns ``None`` on error."""
        try:
            resp = self._client.get(f"{self._base_url}/api/status")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception:
            logger.debug("Failed to fetch /api/status", exc_info=True)
            return None

    def fetch_anomalies(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch ``/api/anomalies``."""
        try:
            resp = self._client.get(
                f"{self._base_url}/api/anomalies",
                params={"limit": limit},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data.get("anomalies", [])  # type: ignore[no-any-return]
        except Exception:
            logger.debug("Failed to fetch /api/anomalies", exc_info=True)
            return []

    def fetch_health(self) -> dict[str, Any] | None:
        """Fetch ``/api/health``."""
        try:
            resp = self._client.get(f"{self._base_url}/api/health")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception:
            logger.debug("Failed to fetch /api/health", exc_info=True)
            return None

    def fetch_rtk_status(self) -> dict[str, Any] | None:
        """Fetch ``/api/rtk-status``.  Returns ``None`` on error."""
        try:
            resp = self._client.get(f"{self._base_url}/api/rtk-status")
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception:
            logger.debug("Failed to fetch /api/rtk-status", exc_info=True)
            return None


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _ts_to_time(timestamp_ms: int) -> str:
    """Convert epoch-millisecond timestamp to local ``HH:MM:SS``."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0)
    return dt.strftime("%H:%M:%S")


def _format_uptime(seconds: float) -> str:
    """Format seconds into ``Xh Ym``."""
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    return f"{hours}h {minutes}m"


def _ctx_style(pct: int) -> str:
    """Return a Rich style string based on context-window usage percentage."""
    if pct < 50:
        return "green"
    if pct <= 75:
        return "yellow"
    return "red"


def _bar_color(value: int, max_value: int) -> str:
    """Return a bar colour based on relative magnitude."""
    if max_value <= 0:
        return "green"
    ratio = value / max_value
    if ratio < 0.33:
        return "green"
    if ratio < 0.66:
        return "yellow"
    return "red"


def _severity_style(severity: str | None) -> str:
    """Return a Rich style for anomaly severity."""
    if severity is None:
        return "dim"
    level = severity.lower()
    if level == "low":
        return "yellow"
    if level == "medium":
        return "orange3"
    return "red"


def _truncate(text: str, length: int) -> str:
    """Truncate *text* to *length* characters, adding ellipsis if needed."""
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def build_header(health_data: dict[str, Any] | None) -> Panel:
    """Build the top header panel with health information."""
    if health_data is None:
        header_text = Text.assemble(
            ("context-pulse dashboard", "bold cyan"),
            " | ",
            ("\u25cb Disconnected", "bold red"),
        )
        return Panel(header_text, style="red", height=3)

    uptime = _format_uptime(health_data.get("uptime_seconds", 0))
    event_count = health_data.get("event_count", 0)
    snapshot_count = health_data.get("snapshot_count", 0)

    header_text = Text.assemble(
        ("context-pulse dashboard", "bold cyan"),
        " | ",
        (f"Uptime: {uptime}", "dim"),
        " | ",
        (f"Events: {event_count}", "dim"),
        " | ",
        (f"Snapshots: {snapshot_count}", "dim"),
        " | ",
        ("\u25cf Connected", "bold green"),
    )
    return Panel(header_text, style="green", height=3)


def build_sessions_panel(status_data: dict[str, Any] | None) -> Panel:
    """Build the session overview panel with a table of active sessions."""
    table = Table(
        title="Session Overview",
        expand=True,
        title_style="bold white",
        border_style="cyan",
    )
    table.add_column("Project", style="bold", no_wrap=True, max_width=30)
    table.add_column("Events", justify="right", no_wrap=True, min_width=6)
    table.add_column("Tokens", justify="right", no_wrap=True, min_width=10)
    table.add_column("Ctx%", justify="right", no_wrap=True, min_width=5)
    table.add_column("Model", no_wrap=True)

    sessions: list[dict[str, Any]] = []
    if status_data is not None:
        sessions = status_data.get("active_sessions", [])

    if not sessions:
        return Panel(
            Text("No active sessions", style="dim"),
            title="Session Overview",
            border_style="cyan",
        )

    for sess in sessions:
        project = (
            sess.get("project_name")
            or str(sess.get("session_id", ""))[:_SESSION_ID_TRUNCATE]
        )
        event_count = sess.get("event_count", 0)
        total_tokens = sess.get("total_tokens_used")
        used_pct = sess.get("used_percentage")
        model_id = sess.get("model_id") or "unknown"

        tokens_str = f"{total_tokens:,}" if total_tokens is not None else "[dim]--[/dim]"
        pct_val = used_pct if used_pct is not None else 0
        pct_style = _ctx_style(pct_val)
        if used_pct is not None:
            pct_text = Text(f"{pct_val}%", style=pct_style)
        else:
            pct_text = Text("--", style="dim")

        table.add_row(
            project,
            str(event_count),
            tokens_str,
            pct_text,
            model_id,
        )

    return Panel(table, border_style="cyan")


def build_tasks_panel(status_data: dict[str, Any] | None) -> Panel:
    """Build the task cost timeline panel with horizontal bar chart."""
    tasks: list[dict[str, Any]] = []
    if status_data is not None:
        tasks = status_data.get("recent_tasks", [])

    # Build session_id -> project_name lookup from active sessions
    session_names: dict[str, str] = {}
    if status_data is not None:
        for sess in status_data.get("active_sessions", []):
            sid = sess.get("session_id", "")
            name = sess.get("project_name") or sid[:_SESSION_ID_TRUNCATE]
            session_names[sid] = name

    # Show tasks that have estimated_tokens (direct count) or token_delta
    tasks_with_cost = [
        t for t in tasks
        if t.get("estimated_tokens") is not None or t.get("token_delta") is not None
    ]
    tasks_with_cost = tasks_with_cost[-50:]

    if not tasks_with_cost:
        return Panel(
            Text("No tasks recorded yet", style="dim"),
            title="Task Cost Timeline",
            border_style="magenta",
        )

    def _get_cost(t: dict[str, Any]) -> int:
        return t.get("estimated_tokens") or abs(t.get("token_delta") or 0)

    max_cost = max(_get_cost(t) for t in tasks_with_cost)

    table = Table(
        title="Task Cost Timeline",
        expand=True,
        title_style="bold white",
        border_style="magenta",
        show_lines=False,
    )
    table.add_column("Time", no_wrap=True, style="dim")
    table.add_column("Project", no_wrap=True, style="cyan")
    table.add_column("Type", no_wrap=True)
    table.add_column("Cost", justify="left", no_wrap=True)
    table.add_column("Tokens", justify="right")

    for task in tasks_with_cost:
        timestamp_ms = task.get("timestamp_ms", 0)
        task_type = task.get("task_type", "unknown")
        session_id = task.get("session_id", "")
        cost = _get_cost(task)

        # Resolve project name; show folder part only to keep column compact
        full_name = session_names.get(session_id, session_id[:_SESSION_ID_TRUNCATE]) or ""
        # Extract just the folder portion (before the " — " title separator)
        project_label = full_name.split(" \u2014 ")[0] if " \u2014 " in full_name else full_name
        project_label = _truncate(project_label, 20)

        bar_width = max(1, int(cost / max_cost * _BAR_MAX_WIDTH)) if max_cost > 0 else 1

        color = _bar_color(cost, max_cost)
        bar = Text("\u2588" * bar_width, style=color)

        table.add_row(
            _ts_to_time(timestamp_ms),
            project_label,
            task_type,
            bar,
            f"{cost:,}",
        )

    return Panel(table, border_style="magenta")


def build_rtk_panel(client: DashboardClient) -> Panel:
    """Build a compact RTK savings panel."""
    rtk_data = client.fetch_rtk_status()

    if rtk_data is None or not rtk_data.get("installed"):
        rtk_text = Text.assemble(
            ("RTK: ", "bold"),
            ("\u25cb Not installed", "dim"),
        )
        return Panel(rtk_text, border_style="dim", height=3)

    parts: list[tuple[str, str]] = [("RTK: ", "bold"), ("\u25cf Active", "bold green")]

    savings = rtk_data.get("savings_24h")
    if savings:
        saved = savings.get("tokens_saved", 0)
        pct = savings.get("savings_percentage", 0.0)
        parts.append((" | ", "dim"))
        parts.append((f"Saved: {saved:,} tokens ({pct:.0f}%)", "green"))

    version: str = rtk_data.get("version") or ""
    if version:
        # Strip "rtk " prefix if present (e.g. "rtk 0.34.1" -> "0.34.1")
        ver_num = version.replace("rtk ", "").strip()
        parts.append((" | ", "dim"))
        parts.append((f"v{ver_num}", "dim"))

    rtk_text = Text.assemble(*parts)
    return Panel(rtk_text, border_style="cyan", height=3)


def build_anomaly_panel(anomalies: list[dict[str, Any]]) -> Panel:
    """Build the anomaly feed panel."""
    if not anomalies:
        return Panel(
            Text("No anomalies detected", style="green"),
            title="Anomaly Feed",
            border_style="green",
        )

    table = Table(
        title="Anomaly Feed",
        expand=True,
        title_style="bold white",
        border_style="red",
        show_lines=False,
    )
    table.add_column("Time", no_wrap=True, style="dim")
    table.add_column("Tool", no_wrap=True)
    table.add_column("Tokens", justify="right")
    table.add_column("Z-Score", justify="right")
    table.add_column("Severity", no_wrap=True)
    table.add_column("Cause")

    for anomaly in anomalies:
        timestamp_ms = anomaly.get("timestamp_ms", 0)
        task_type = anomaly.get("task_type", "unknown")
        token_cost = anomaly.get("token_cost", 0)
        z_score = anomaly.get("z_score", 0.0)
        severity = anomaly.get("severity")
        cause = anomaly.get("cause") or ""

        severity_display = str(severity or "unknown")
        severity_text = Text(severity_display, style=_severity_style(severity))
        cause_truncated = _truncate(cause, _CAUSE_TRUNCATE)

        table.add_row(
            _ts_to_time(timestamp_ms),
            task_type,
            f"{token_cost:,}",
            f"{z_score:.1f}",
            severity_text,
            cause_truncated,
        )

    table.add_row("", "", "", "", "", Text("Run: context-pulse anomalies", style="dim italic"))
    return Panel(table, border_style="red")


# ---------------------------------------------------------------------------
# Layout assembly
# ---------------------------------------------------------------------------


def _build_layout(client: DashboardClient) -> Layout:
    """Fetch all data and assemble the full dashboard layout."""
    health = client.fetch_health()
    status = client.fetch_status()
    anomalies = client.fetch_anomalies(limit=10)

    layout = Layout()
    layout.split_column(
        Layout(build_header(health), name="header", size=3),
        Layout(build_rtk_panel(client), name="rtk", size=3),
        Layout(name="body"),
    )
    # Body: left column (sessions + anomalies) | right column (task timeline)
    layout["body"].split_row(
        Layout(name="left"),
        Layout(build_tasks_panel(status), name="tasks"),
    )
    layout["left"].split_column(
        Layout(build_sessions_panel(status), name="sessions"),
        Layout(build_anomaly_panel(anomalies), name="anomalies"),
    )
    return layout


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_dashboard(port: int | None = _DEFAULT_PORT, refresh_rate: float = 2.0) -> None:
    """Run the live TUI dashboard.  Blocks until Ctrl+C.

    Parameters
    ----------
    port:
        Port where the context-pulse collector is listening.
    refresh_rate:
        Seconds between data refreshes.
    """
    console = Console()
    # Resolve port from config if not explicitly provided
    if port is None:
        try:
            from context_pulse.config import load_config
            cfg = load_config()
            port = cfg.collector.port
        except Exception:
            port = 7821
    client = DashboardClient(base_url=f"http://127.0.0.1:{port}")

    try:
        with Live(
            _build_layout(client),
            console=console,
            refresh_per_second=0.5,
            screen=True,
        ) as live:
            while True:
                time.sleep(refresh_rate)
                live.update(_build_layout(client))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        console.print("[dim]Dashboard stopped.[/dim]")
