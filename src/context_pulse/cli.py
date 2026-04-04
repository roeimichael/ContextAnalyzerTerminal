"""CLI entry point for context-pulse, built with Typer."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from context_pulse.config import (
    get_config_dir,
    get_config_path,
    load_config,
    write_default_config,
)

logger = logging.getLogger("context_pulse.cli")

console = Console()

app = typer.Typer(
    name="context-pulse",
    help="Per-tool-call context window analyzer for Claude Code",
)


def _resolve_port(port: int | None) -> int:
    """Return *port* if given, otherwise read default from config."""
    if port is not None:
        return port
    try:
        return load_config().collector.port
    except Exception:
        return 7821


def _collector_base_url(port: int | None) -> str:
    """Build the collector base URL from port (or config default)."""
    try:
        cfg = load_config()
        host = cfg.collector.host
        p = port if port is not None else cfg.collector.port
    except Exception:
        host = "127.0.0.1"
        p = port if port is not None else 7821
    return f"http://{host}:{p}"


# ---------------------------------------------------------------------------
# Hook definitions for settings.json
# ---------------------------------------------------------------------------

_HOOK_SCRIPTS = [
    "_hook_config.py",
    "post_tool_use.py",
    "subagent_stop.py",
    "stop.py",
    "user_prompt_submit.py",
    "session_start.py",
    "statusline.py",
]

_HOOK_EVENT_MAP: dict[str, str] = {
    "post_tool_use.py": "PostToolUse",
    "subagent_stop.py": "SubagentStop",
    "stop.py": "Stop",
    "user_prompt_submit.py": "UserPromptSubmit",
    "session_start.py": "SessionStart",
}

_CONTEXT_PULSE_MARKER = "context-pulse"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_hooks_source_dir() -> Path:
    """Locate the hooks/ directory shipped alongside the package.

    We look relative to this file first (development layout), then try
    importlib.resources for installed packages.
    """
    # Development layout: src/context_pulse/cli.py -> ../../hooks/
    dev_hooks = Path(__file__).resolve().parent.parent.parent / "hooks"
    if dev_hooks.is_dir():
        return dev_hooks

    # Installed layout: try importlib.resources
    try:
        import importlib.resources as ir

        # Python 3.12+ files() API
        ref = ir.files("context_pulse").joinpath("hooks")
        if hasattr(ref, "_path"):
            p = Path(str(ref._path))  # type: ignore[attr-defined]
            if p.is_dir():
                return p
        # Fallback: resolve traversable
        p = Path(str(ref))
        if p.is_dir():
            return p
    except Exception:
        pass

    raise FileNotFoundError(
        "Cannot locate hooks/ directory. "
        "Ensure you are running from the project root or that the package is properly installed."
    )


def _copy_hook_scripts(hooks_dir: Path) -> list[str]:
    """Copy hook scripts from the package to *hooks_dir*.

    Returns:
        List of filenames that were copied.
    """
    hooks_dir.mkdir(parents=True, exist_ok=True)
    source_dir = _find_hooks_source_dir()
    copied: list[str] = []
    for script_name in _HOOK_SCRIPTS:
        src = source_dir / script_name
        if not src.exists():
            logger.warning("Hook script not found: %s", src)
            continue
        dst = hooks_dir / script_name
        shutil.copy2(src, dst)
        copied.append(script_name)
        logger.debug("Copied %s -> %s", src, dst)
    return copied


def _build_hooks_config(
    hooks_dir: Path, use_http: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Build the hooks portion of settings.json for context-pulse."""
    hooks_config: dict[str, list[dict[str, Any]]] = {}
    for script_name, event_name in _HOOK_EVENT_MAP.items():
        script_path = hooks_dir / script_name
        # Use forward slashes in paths (Claude Code convention)
        path_str = str(script_path).replace("\\", "/")

        if use_http:
            cfg = load_config()
            hook_entry = {
                "type": "http",
                "url": f"http://{cfg.collector.host}:{cfg.collector.port}/hook/event",
                "timeout": 5,
            }
        else:
            hook_entry = {
                "type": "command",
                "command": f"uv run {path_str}",
                "timeout": 5,
            }

        hooks_config[event_name] = [
            {
                "matcher": "",
                "hooks": [hook_entry],
            }
        ]
    return hooks_config


def _build_statusline_config(hooks_dir: Path) -> dict[str, str]:
    """Build the statusLine portion of settings.json."""
    statusline_path = hooks_dir / "statusline.py"
    path_str = str(statusline_path).replace("\\", "/")
    return {
        "type": "command",
        "command": f"uv run {path_str}",
    }


def _is_context_pulse_hook(hook_entry: dict[str, Any]) -> bool:
    """Check if a hook entry belongs to context-pulse."""
    command: str = hook_entry.get("command", "")
    url: str = hook_entry.get("url", "")
    return _CONTEXT_PULSE_MARKER in command or _CONTEXT_PULSE_MARKER in url


def _merge_settings(
    existing: dict[str, Any],
    hooks_dir: Path,
    use_http: bool = False,
) -> dict[str, Any]:
    """Merge context-pulse hooks into existing settings.json content.

    Preserves existing non-context-pulse hooks for each event type.
    """
    settings: dict[str, Any] = dict(existing)

    # -- Merge hooks --------------------------------------------------------
    new_hooks_config = _build_hooks_config(hooks_dir, use_http=use_http)
    current_hooks: dict[str, Any] = settings.get("hooks", {})

    for event_name, new_entries in new_hooks_config.items():
        existing_entries: list[dict[str, Any]] = current_hooks.get(event_name, [])

        # Remove any existing context-pulse entries for this event
        cleaned: list[dict[str, Any]] = []
        for entry in existing_entries:
            inner_hooks: list[dict[str, Any]] = entry.get("hooks", [])
            non_cp = [h for h in inner_hooks if not _is_context_pulse_hook(h)]
            if non_cp:
                cleaned.append({**entry, "hooks": non_cp})
        # Append our new entries
        cleaned.extend(new_entries)
        current_hooks[event_name] = cleaned

    settings["hooks"] = current_hooks

    # -- Merge statusLine ---------------------------------------------------
    new_statusline: dict[str, str] = _build_statusline_config(hooks_dir)
    existing_sl: dict[str, Any] = settings.get("statusLine", {})

    # Only overwrite if it's empty or already belongs to context-pulse.
    # Respect existing non-context-pulse statusLines.
    sl_cmd: str = existing_sl.get("command", "")
    if not existing_sl or _CONTEXT_PULSE_MARKER in sl_cmd:
        settings["statusLine"] = new_statusline
    else:
        logger.warning(
            "Existing statusLine found that does not belong to context-pulse. "
            "Keeping existing statusLine. Use --force-statusline to override."
        )

    return settings


def _format_timestamp(ts_ms: int) -> str:
    """Format a millisecond epoch timestamp for display."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone()
    return dt.strftime("%H:%M:%S")


def _format_token_delta(delta: int | None) -> str:
    """Format a token delta with sign and comma separators."""
    if delta is None:
        return ""
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:,}"


def _truncate_session_id(session_id: str, length: int = 8) -> str:
    """Truncate a session ID for display."""
    if len(session_id) <= length:
        return session_id
    return session_id[:length] + "..."


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host"),
    port: int | None = typer.Option(None, help="Bind port"),
    config_path: Path | None = typer.Option(
        None, "--config", help="Config file path"
    ),
    log_level: str = typer.Option("info", help="Log level"),
) -> None:
    """Start the context-pulse collector server."""
    try:
        import uvicorn

        from context_pulse.collector.server import create_app
    except ImportError as exc:
        console.print(
            f"[red]Missing dependency:[/red] {exc}. "
            "Install with: [bold]pip install context-pulse[/bold]"
        )
        raise typer.Exit(1) from None

    try:
        cfg = load_config(config_path)
    except ValueError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        raise typer.Exit(1) from None

    actual_host = host if host is not None else cfg.collector.host
    actual_port = port if port is not None else cfg.collector.port

    # Ensure the database directory exists
    db_path = Path(cfg.collector.db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        uvicorn.run(
            create_app(),
            host=actual_host,
            port=actual_port,
            log_level=log_level,
        )
    except OSError as exc:
        exc_lower = str(exc).lower()
        if "address already in use" in exc_lower or "error while attempting to bind" in exc_lower:
            console.print(
                f"[red]Port {actual_port} is already in use.[/red] "
                "Is another context-pulse instance running? "
                f"Use [bold]--port[/bold] to specify a different port."
            )
        else:
            console.print(f"[red]Server error:[/red] {exc}")
        raise typer.Exit(1) from None


@app.command()
def status(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
    limit: int = typer.Option(10, help="Number of recent events to show"),
) -> None:
    """Show active sessions and recent tasks from the running collector."""
    import httpx

    url = _collector_base_url(port)

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{url}/api/status")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Is it running? Start with: [bold]context-pulse serve[/bold]"
        )
        raise typer.Exit(1) from None
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]HTTP error from collector:[/red] {exc.response.status_code}")
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]Error communicating with collector:[/red] {exc}")
        raise typer.Exit(1) from None

    # -- Active Sessions Panel ----------------------------------------------
    sessions = data.get("active_sessions", [])
    if sessions:
        sessions_table = Table(show_header=True, header_style="bold cyan", expand=True)
        sessions_table.add_column("Session", style="bold")
        sessions_table.add_column("Events", justify="right")
        sessions_table.add_column("Tokens", justify="right")
        sessions_table.add_column("Ctx %", justify="right")
        sessions_table.add_column("Model")

        for s in sessions:
            sid = _truncate_session_id(s.get("session_id", "unknown"))
            event_count = str(s.get("event_count", 0))
            total_tokens = (
                f"{s['total_tokens_used']:,}" if s.get("total_tokens_used") is not None else "--"
            )
            used_pct = (
                f"{s['used_percentage']}%" if s.get("used_percentage") is not None else "--"
            )
            model = s.get("model_id") or "--"
            sessions_table.add_row(sid, event_count, total_tokens, used_pct, model)

        console.print(Panel(sessions_table, title="Active Sessions", border_style="green"))
    else:
        console.print(Panel("[dim]No active sessions[/dim]", title="Active Sessions"))

    # -- Recent Tasks Panel -------------------------------------------------
    tasks = data.get("recent_tasks", [])
    if tasks:
        # Apply limit
        tasks = tasks[:limit]

        tasks_table = Table(show_header=True, header_style="bold cyan", expand=True)
        tasks_table.add_column("Time")
        tasks_table.add_column("Session")
        tasks_table.add_column("Type")
        tasks_table.add_column("Token Delta", justify="right")
        tasks_table.add_column("Compaction", justify="center")

        for t in tasks:
            time_str = _format_timestamp(t.get("timestamp_ms", 0))
            sid = _truncate_session_id(t.get("session_id", "unknown"))
            task_type = t.get("task_type", "")
            delta = _format_token_delta(t.get("token_delta"))
            is_compaction = "yes" if t.get("is_compaction") else ""

            # Color the delta
            if t.get("token_delta") is not None:
                if t["token_delta"] < 0:
                    delta = f"[red]{delta}[/red]"
                elif t["token_delta"] > 0:
                    delta = f"[green]{delta}[/green]"

            tasks_table.add_row(time_str, sid, task_type, delta, is_compaction)

        console.print(Panel(tasks_table, title="Recent Tasks", border_style="blue"))
    else:
        console.print(Panel("[dim]No recent tasks[/dim]", title="Recent Tasks"))


@app.command()
def health(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
) -> None:
    """Check collector health status."""
    import httpx

    url = _collector_base_url(port)

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{url}/api/health")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Is it running? Start with: [bold]context-pulse serve[/bold]"
        )
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]Error communicating with collector:[/red] {exc}")
        raise typer.Exit(1) from None

    status_str = data.get("status", "unknown")
    uptime = data.get("uptime_seconds", 0.0)
    event_count = data.get("event_count", 0)
    snapshot_count = data.get("snapshot_count", 0)
    db_path = data.get("db_path", "unknown")

    # Format uptime nicely
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        uptime_str = f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        uptime_str = f"{minutes}m {seconds}s"
    else:
        uptime_str = f"{seconds}s"

    health_table = Table(show_header=False, expand=True)
    health_table.add_column("Key", style="bold")
    health_table.add_column("Value")

    color = "green" if status_str == "ok" else "red"
    health_table.add_row("Status", f"[{color}]{status_str}[/{color}]")
    health_table.add_row("Uptime", uptime_str)
    health_table.add_row("Events", f"{event_count:,}")
    health_table.add_row("Snapshots", f"{snapshot_count:,}")
    health_table.add_row("Database", db_path)

    console.print(Panel(health_table, title="Collector Health", border_style="green"))


def _default_claude_settings() -> Path:
    """Return the Claude settings path, respecting ``CLAUDE_SETTINGS_PATH`` env var."""
    env = os.environ.get("CLAUDE_SETTINGS_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude" / "settings.json"


@app.command()
def install(
    claude_settings: Path = typer.Option(
        None,
        "--claude-settings",
        help="Path to Claude Code settings.json "
             "(default: ~/.claude/settings.json or CLAUDE_SETTINGS_PATH)",
    ),
    hooks_dir: Path = typer.Option(
        get_config_dir() / "hooks",
        "--hooks-dir",
        help="Where to copy hook scripts",
    ),
    use_http: bool = typer.Option(
        False,
        "--use-http",
        help="Use HTTP hooks instead of command hooks",
    ),
) -> None:
    """Install hooks and statusline into Claude Code settings."""
    if claude_settings is None:
        claude_settings = _default_claude_settings()
    try:
        _run_install(claude_settings, hooks_dir, use_http)
    except Exception as exc:
        console.print(f"[red]Installation failed:[/red] {exc}")
        logger.exception("Installation failed")
        raise typer.Exit(1) from None


# Alias: `context-pulse init` does the same as `context-pulse install`
app.command(name="init", hidden=True)(install)


def _run_install(
    claude_settings: Path,
    hooks_dir: Path,
    use_http: bool,
) -> None:
    """Core install logic, separated for testability."""
    # 1. Create directories
    hooks_dir = hooks_dir.expanduser()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"  Created hooks directory: [bold]{hooks_dir}[/bold]")

    # 2. Copy hook scripts
    copied = _copy_hook_scripts(hooks_dir)
    if not copied:
        console.print("[yellow]Warning: No hook scripts were found to copy.[/yellow]")
    else:
        console.print(f"  Copied {len(copied)} hook script(s): {', '.join(copied)}")

    # 3. Write default config.toml if it doesn't exist
    config_path = get_config_path().expanduser()
    if not config_path.exists():
        write_default_config(config_path)
        console.print(f"  Wrote default config: [bold]{config_path}[/bold]")
    else:
        console.print(f"  Config already exists: [bold]{config_path}[/bold] (skipped)")

    # 4. Read existing settings.json (or start from empty)
    claude_settings = claude_settings.expanduser()
    existing: dict[str, Any] = {}
    if claude_settings.exists():
        try:
            raw: object = json.loads(claude_settings.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = cast(dict[str, Any], raw)
        except (json.JSONDecodeError, OSError) as exc:
            console.print(
                f"[yellow]Warning: Could not parse existing settings.json: {exc}[/yellow]\n"
                "  Starting with empty settings."
            )

    # 5. Merge hooks and statusLine
    merged = _merge_settings(existing, hooks_dir, use_http=use_http)

    # 6. Write back settings.json
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    console.print(f"  Updated settings: [bold]{claude_settings}[/bold]")

    # 7. Verify collector is reachable
    try:
        import httpx

        with httpx.Client(timeout=3.0) as client:
            cfg = load_config()
            resp = client.get(f"http://{cfg.collector.host}:{cfg.collector.port}/api/health")
            resp.raise_for_status()
        console.print("  Collector is [green]reachable[/green].")
    except Exception:
        console.print(
            "  [yellow]Collector is not running.[/yellow] "
            "Start it with: [bold]context-pulse serve[/bold]"
        )

    # 8. Summary
    mode = "HTTP" if use_http else "command"
    console.print()
    console.print(
        Panel(
            f"[green]Installation complete![/green]\n\n"
            f"Hook mode: [bold]{mode}[/bold]\n"
            f"Hooks dir: {hooks_dir}\n"
            f"Config:    {config_path}\n"
            f"Settings:  {claude_settings}\n\n"
            "Next steps:\n"
            "  1. Start the collector:  [bold]context-pulse serve[/bold]\n"
            "  2. Check health:         [bold]context-pulse health[/bold]\n"
            "  3. View status:          [bold]context-pulse status[/bold]",
            title="context-pulse",
            border_style="green",
        )
    )

    # 9. Check and offer RTK integration
    from context_pulse.rtk_integration import (
        install_rtk_hooks,
        is_rtk_hooks_installed,
        is_rtk_installed,
    )

    if is_rtk_installed():
        if not is_rtk_hooks_installed():
            console.print("\n[cyan]RTK detected but hooks not installed.[/cyan]")
            console.print("Installing RTK hooks for maximum token savings...")
            if install_rtk_hooks():
                console.print("[green]RTK hooks installed successfully![/green]")
            else:
                console.print(
                    "[yellow]RTK hook installation failed.[/yellow] "
                    "Run manually: rtk init -g --auto-patch"
                )
        else:
            console.print("\n[green]RTK hooks already active.[/green] Token compression enabled.")
    else:
        console.print(
            "\n[dim]Tip: Install RTK for 60-90% token savings: "
            "pip install rtk-py && rtk init -g --auto-patch[/dim]"
        )


@app.command()
def anomalies(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
    limit: int = typer.Option(10, help="Number of anomalies to show"),
    session_id: str | None = typer.Option(None, "--session", help="Filter by session"),
) -> None:
    """Show recent anomalies with root cause analysis."""
    import httpx

    url = _collector_base_url(port)
    params: dict[str, str | int] = {"limit": limit}
    if session_id is not None:
        params["session_id"] = session_id

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{url}/api/anomalies", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Is it running? Start with: [bold]context-pulse serve[/bold]"
        )
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None

    anomaly_list: list[dict[str, Any]] = data.get("anomalies", [])
    total_count: int = data.get("total_count", 0)

    if not anomaly_list:
        console.print(Panel("[dim]No anomalies detected yet[/dim]", title="Anomalies"))
        return

    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Time")
    table.add_column("Session")
    table.add_column("Tool")
    table.add_column("Tokens", justify="right")
    table.add_column("Z-Score", justify="right")
    table.add_column("Severity", justify="center")
    table.add_column("Cause")

    for a in anomaly_list:
        time_str = _format_timestamp(a.get("timestamp_ms", 0))
        sid = _truncate_session_id(a.get("session_id", "unknown"))
        task_type: str = a.get("task_type", "")
        tokens = f"{a.get('token_cost', 0):,}"
        z = f"{a.get('z_score', 0):.1f}"
        severity: str = a.get("severity") or "--"
        cause: str = a.get("cause") or "[dim]pending...[/dim]"

        sev_color = {"low": "yellow", "medium": "orange3", "high": "red"}.get(
            severity.lower(), "dim"
        )
        severity_display = f"[{sev_color}]{severity}[/{sev_color}]"

        table.add_row(time_str, sid, task_type, tokens, z, severity_display, cause)

    header = f"Anomalies (showing {len(anomaly_list)} of {total_count})"
    console.print(Panel(table, title=header, border_style="red"))

    # Show full details for each anomaly that has a suggestion
    for a in anomaly_list:
        suggestion = a.get("suggestion")
        cause_full = a.get("cause") or ""
        if suggestion or cause_full:
            time_str = _format_timestamp(a.get("timestamp_ms", 0))
            task_type = a.get("task_type", "")
            tokens = f"{a.get('token_cost', 0):,}"
            detail_parts: list[str] = []
            if cause_full:
                detail_parts.append(f"[bold]Cause:[/bold] {cause_full}")
            if suggestion:
                detail_parts.append(f"[bold]Suggestion:[/bold] {suggestion}")
            console.print(
                Panel(
                    "\n".join(detail_parts),
                    title=f"{time_str} — {task_type} ({tokens} tokens)",
                    border_style="yellow",
                )
            )


@app.command()
def dashboard(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
    refresh: float = typer.Option(2.0, help="Refresh interval in seconds"),
) -> None:
    """Launch the live TUI dashboard (Ctrl+C to exit)."""
    from context_pulse.dashboard.tui import run_dashboard

    console.print(
        f"[bold]Starting context-pulse dashboard[/bold] "
        f"(collector: 127.0.0.1:{port}, refresh: {refresh}s)"
    )
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")
    run_dashboard(port=port, refresh_rate=refresh)


@app.command(name="rtk-status")
def rtk_status(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
) -> None:
    """Show RTK (Rust Token Killer) integration status and savings."""
    from context_pulse.rtk_integration import (
        get_rtk_savings_summary,
        get_rtk_version,
        is_rtk_hooks_installed,
        is_rtk_installed,
        recommend_rtk_install,
    )

    installed = is_rtk_installed()
    version = get_rtk_version()
    hooks = is_rtk_hooks_installed()

    # Status panel
    status_items: list[str] = []
    status_items.append(f"RTK installed: {'[green]Yes[/green]' if installed else '[red]No[/red]'}")
    if version:
        status_items.append(f"Version: [bold]{version}[/bold]")
    hooks_str = "[green]Yes[/green]" if hooks else "[yellow]No[/yellow]"
    status_items.append(f"Hooks active: {hooks_str}")

    if installed:
        savings = get_rtk_savings_summary(since_hours=24)
        if savings:
            saved = savings.get("tokens_saved", 0)
            pct = savings.get("savings_percentage", 0.0)
            cmds = savings.get("total_commands", 0)
            status_items.append(f"Commands (24h): {cmds}")
            status_items.append(f"Tokens saved (24h): [green]{saved:,}[/green] ({pct:.0f}%)")

            top = savings.get("top_commands", [])
            if top:
                status_items.append("\nTop savings by command:")
                for cmd, tokens in top[:5]:
                    status_items.append(f"  {cmd}: {tokens:,} tokens saved")

    console.print(Panel("\n".join(status_items), title="RTK Integration", border_style="cyan"))

    if not installed:
        console.print()
        console.print(
            Panel(recommend_rtk_install(), title="Recommendation", border_style="yellow")
        )


@app.command(name="context-cost")
def context_cost(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
    session: str | None = typer.Option(None, "--session", help="Session ID"),
) -> None:
    """Show context cost breakdown: fresh session vs. current session.

    Highlights how much of each API call is wasted on conversation
    history, and recommends starting fresh when overhead is high.
    """
    import httpx

    from context_pulse.engine.context_breakdown import (
        compute_breakdown,
        format_breakdown_table,
    )

    url = _collector_base_url(port)

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{url}/api/status")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Start with: [bold]context-pulse serve[/bold]"
        )
        raise typer.Exit(1) from None

    sessions_list: list[dict[str, Any]] = data.get("active_sessions", [])
    if not sessions_list:
        console.print("[dim]No active sessions found.[/dim]")
        return

    # Pick session
    if session:
        target = next(
            (s for s in sessions_list if s["session_id"].startswith(session)),
            None,
        )
        if not target:
            console.print(f"[red]Session {session} not found.[/red]")
            return
    else:
        # Use the most active session (most events)
        target = max(sessions_list, key=lambda s: s.get("event_count", 0))

    sid = target["session_id"]
    tokens_used = target.get("total_tokens_used")
    used_pct = target.get("used_percentage")

    if tokens_used is None or used_pct is None:
        console.print(
            f"[yellow]Session {sid[:12]} has no token data yet.[/yellow]"
        )
        return

    # Get snapshot data for full breakdown
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{url}/api/sessions/{sid}/snapshots", params={"limit": 1}
            )
            resp.raise_for_status()
            snaps = resp.json()
    except Exception:
        snaps = []

    if snaps:
        snap = cast(dict[str, Any], snaps[0])
        cw_size = int(snap.get("context_window_size", 0))
        breakdown = compute_breakdown(
            total_input_tokens=int(snap.get("total_input_tokens", tokens_used)),
            total_output_tokens=int(snap.get("total_output_tokens", 0)),
            cache_creation_input_tokens=int(
                snap.get("cache_creation_input_tokens", 0)
            ),
            cache_read_input_tokens=int(snap.get("cache_read_input_tokens", 0)),
            context_window_size=cw_size,
            used_percentage=used_pct,
        )
    else:
        breakdown = compute_breakdown(
            total_input_tokens=tokens_used,
            total_output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            context_window_size=0,
            used_percentage=used_pct,
        )

    table_str = format_breakdown_table(breakdown)
    sev = breakdown.get("severity", "low")
    border = {
        "low": "green", "medium": "yellow", "high": "red", "critical": "red",
    }.get(sev, "white")

    console.print(
        Panel(
            table_str,
            title=f"Context Cost — {sid[:12]}",
            border_style=border,
        )
    )


@app.command()
def clear(
    port: int | None = typer.Option(None, help="Collector port (default: from config)"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Clear all stored data and start fresh.

    Wipes the SQLite database so the dashboard shows a clean slate.
    The collector must be stopped first.
    """
    import httpx

    # Check if collector is running
    try:
        with httpx.Client(timeout=2.0) as client:
            client.get(f"{_collector_base_url(port)}/api/health")
        console.print(
            "[red]Collector is still running.[/red] "
            "Stop it first (Ctrl+C in its terminal), then run clear."
        )
        raise typer.Exit(1) from None
    except (httpx.ConnectError, httpx.ConnectTimeout):
        pass  # Good — collector is stopped

    from context_pulse.config import get_db_path, load_config

    cfg = load_config()
    db_path = Path(get_db_path(cfg))

    if not db_path.exists():
        console.print("[dim]No database found. Nothing to clear.[/dim]")
        return

    size_kb = db_path.stat().st_size // 1024

    if not confirm:
        console.print(
            f"This will delete [bold]{db_path}[/bold] ({size_kb} KB)\n"
            "All events, snapshots, tasks, anomalies, and baselines will be lost."
        )
        response = input("Continue? [y/N] ")
        if response.lower() != "y":
            console.print("[dim]Cancelled.[/dim]")
            return

    db_path.unlink()
    console.print(
        f"[green]Cleared.[/green] Deleted {db_path} ({size_kb} KB)\n"
        "Start the collector again: [bold]context-pulse serve[/bold]"
    )


@app.command()
def prune(
    days: int = typer.Option(None, help="Override retention_days from config"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted"),
) -> None:
    """Prune data older than the configured retention period.

    By default uses [retention] retention_days from config.toml.
    """
    import asyncio

    from context_pulse.config import get_db_path
    from context_pulse.db.maintenance import get_table_counts, prune_old_data
    from context_pulse.db.schema import open_db

    cfg = load_config()
    retention = days if days is not None else cfg.retention.retention_days

    if retention <= 0:
        console.print("[dim]Retention is set to 0 (keep forever). Nothing to prune.[/dim]")
        return

    db_path = get_db_path(cfg)
    if not Path(db_path).exists():
        console.print("[dim]No database found.[/dim]")
        return

    async def _run() -> None:
        db = await open_db(db_path)
        try:
            if dry_run:
                counts = await get_table_counts(db)
                console.print(
                    f"[bold]Dry run[/bold] — would prune data older than {retention} days:"
                )
                for table, count in counts.items():
                    console.print(f"  {table}: {count} total rows")
            else:
                deleted = await prune_old_data(db, retention)
                if deleted:
                    total = sum(deleted.values())
                    console.print(
                        f"[green]Pruned {total} rows[/green] older than {retention} days:"
                    )
                    for table, count in deleted.items():
                        console.print(f"  {table}: {count} rows deleted")
                else:
                    console.print(f"[dim]No data older than {retention} days.[/dim]")
        finally:
            await db.close()

    asyncio.run(_run())
