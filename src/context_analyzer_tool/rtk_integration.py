"""RTK (Rust Token Killer) integration for context-analyzer-tool.

Detects RTK installation status, queries its SQLite database for token
savings analytics, and provides helpers for recommending or installing
RTK hooks into Claude Code.

RTK compresses command output before it enters the LLM context window,
saving 60-90% of tokens.  More info: https://github.com/rtk-ai/rtk
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

logger = logging.getLogger("context_analyzer_tool.rtk_integration")

# ---------------------------------------------------------------------------
# 1. RTK Detection & Status
# ---------------------------------------------------------------------------


def is_rtk_installed() -> bool:
    """Check if the ``rtk`` binary is available on PATH."""
    return shutil.which("rtk") is not None


def get_rtk_version() -> str | None:
    """Return the RTK version string, or ``None`` if RTK is not installed."""
    rtk_path = shutil.which("rtk")
    if rtk_path is None:
        return None
    try:
        result = subprocess.run(
            [rtk_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Failed to get RTK version: %s", exc)
    return None


def is_rtk_hooks_installed() -> bool:
    """Check whether RTK hooks are installed in Claude Code settings.

    Reads ``~/.claude/settings.json`` and looks for ``rtk-rewrite`` in the
    hooks configuration.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        raw = json.dumps(data)
        return "rtk-rewrite" in raw
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read Claude Code settings: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 2. RTK Savings Analytics
# ---------------------------------------------------------------------------

_RTK_DB_CANDIDATES: list[tuple[str, Path]] = [
    ("linux", Path.home() / ".local" / "share" / "rtk" / "history.db"),
    ("darwin", Path.home() / "Library" / "Application Support" / "rtk" / "history.db"),
]

if sys.platform == "win32":
    _appdata = os.environ.get("APPDATA", "")
    if _appdata:
        _RTK_DB_CANDIDATES.append(
            ("win32", Path(_appdata) / "rtk" / "history.db")
        )


def get_rtk_db_path() -> Path | None:
    """Locate RTK's SQLite database.

    Search order:

    1. ``RTK_DB_PATH`` environment variable (if set).
    2. Platform-specific default locations.

    Returns ``None`` if the database file does not exist.
    """
    env_override = os.environ.get("RTK_DB_PATH")
    if env_override:
        candidate = Path(env_override)
        if candidate.exists():
            return candidate
        logger.debug("RTK_DB_PATH set but file not found: %s", candidate)

    current_platform = sys.platform
    for plat, candidate in _RTK_DB_CANDIDATES:
        if plat == current_platform and candidate.exists():
            return candidate

    # Fallback: try all candidates regardless of platform
    for _plat, candidate in _RTK_DB_CANDIDATES:
        if candidate.exists():
            return candidate

    return None


def _parse_rows(
    rows: list[Any],
) -> dict[str, Any]:
    """Aggregate rows into a savings summary dict."""
    total_commands = len(rows)
    total_original_tokens = 0
    total_compressed_tokens = 0
    command_savings: dict[str, int] = {}

    for row in rows:
        command: str = str(row[0])
        original: int = int(row[1])
        compressed: int = int(row[2])
        saved = original - compressed

        total_original_tokens += original
        total_compressed_tokens += compressed
        command_savings[command] = command_savings.get(command, 0) + saved

    tokens_saved = total_original_tokens - total_compressed_tokens
    savings_percentage = (
        (tokens_saved / total_original_tokens * 100.0)
        if total_original_tokens > 0
        else 0.0
    )

    top_commands = sorted(
        command_savings.items(), key=lambda x: x[1], reverse=True
    )[:10]

    return {
        "total_commands": total_commands,
        "total_original_tokens": total_original_tokens,
        "total_compressed_tokens": total_compressed_tokens,
        "tokens_saved": tokens_saved,
        "savings_percentage": round(savings_percentage, 2),
        "top_commands": top_commands,
    }


def get_rtk_savings_summary(since_hours: int = 24) -> dict[str, Any] | None:
    """Query RTK's SQLite DB for token savings summary.

    The database is opened in **read-only** mode.

    Returns
    -------
    dict[str, Any] | None
        A dictionary with keys:

        - ``total_commands`` -- number of commands tracked
        - ``total_original_tokens`` -- total tokens before compression
        - ``total_compressed_tokens`` -- total tokens after compression
        - ``tokens_saved`` -- difference (original - compressed)
        - ``savings_percentage`` -- percentage of tokens saved
        - ``top_commands`` -- list of ``(command, tokens_saved)`` tuples

        Returns ``None`` if the RTK database is not found or on error.
    """
    db_path = get_rtk_db_path()
    if db_path is None:
        return None

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            offset = f"-{since_hours} hours"
            cursor = conn.execute(
                "SELECT command, original_tokens, filtered_tokens, timestamp "
                "FROM history "
                "WHERE timestamp >= datetime('now', ?)",
                (offset,),
            )
            rows: list[Any] = cursor.fetchall()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        logger.warning("Could not read RTK database at %s: %s", db_path, exc)
        return None

    return _parse_rows(rows)


async def get_rtk_savings_summary_async(
    since_hours: int = 24,
) -> dict[str, Any] | None:
    """Async version of :func:`get_rtk_savings_summary` using aiosqlite."""
    db_path = get_rtk_db_path()
    if db_path is None:
        return None

    uri = f"file:{db_path}?mode=ro"
    try:
        async with aiosqlite.connect(uri, uri=True) as conn:
            offset = f"-{since_hours} hours"
            cursor = await conn.execute(
                "SELECT command, original_tokens, filtered_tokens, timestamp "
                "FROM history "
                "WHERE timestamp >= datetime('now', ?)",
                (offset,),
            )
            rows: list[Any] = list(await cursor.fetchall())
    except Exception as exc:
        logger.warning("Could not read RTK database at %s: %s", db_path, exc)
        return None

    return _parse_rows(rows)


# ---------------------------------------------------------------------------
# 3. RTK Installation Helper
# ---------------------------------------------------------------------------


def install_rtk_hooks(auto_patch: bool = True) -> bool:
    """Run ``rtk init -g --auto-patch`` to install RTK hooks globally.

    Parameters
    ----------
    auto_patch:
        When ``True`` (the default), pass ``--auto-patch`` to let RTK
        automatically patch Claude Code settings.

    Returns
    -------
    bool
        ``True`` if the command completed successfully.
    """
    rtk_path = shutil.which("rtk")
    if rtk_path is None:
        logger.warning("Cannot install RTK hooks: rtk binary not found on PATH")
        return False

    cmd: list[str] = [rtk_path, "init", "-g"]
    if auto_patch:
        cmd.append("--auto-patch")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("RTK hooks installed successfully")
            return True
        logger.warning(
            "rtk init failed (exit %d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to run rtk init: %s", exc)
    return False


def recommend_rtk_install() -> str:
    """Return a user-friendly message recommending RTK installation."""
    return (
        "RTK (Rust Token Killer) can save 60-90% of tokens by compressing "
        "command output before it enters Claude's context window.\n"
        "Install: pip install rtk-py && rtk init -g --auto-patch\n"
        "Learn more: https://github.com/rtk-ai/rtk"
    )


# ---------------------------------------------------------------------------
# 4. Anomaly Suggestion Enhancement
# ---------------------------------------------------------------------------


def enhance_suggestion_with_rtk(
    tool_name: str,
    original_suggestion: str | None,
) -> str:
    """Enhance an anomaly suggestion with RTK-specific guidance.

    If the anomalous tool is a Bash command and RTK is not installed,
    recommends installing it.  If RTK *is* installed but hooks may not
    be active, suggests checking the hook configuration.

    Parameters
    ----------
    tool_name:
        The tool/task type that triggered the anomaly (e.g. ``"Bash"``).
    original_suggestion:
        The existing suggestion text from the classifier, or ``None``.

    Returns
    -------
    str
        The enhanced suggestion text.
    """
    base = original_suggestion or ""

    if tool_name != "Bash":
        return base

    if not is_rtk_installed():
        rtk_tip = (
            "\n\nTip: RTK (Rust Token Killer) can reduce Bash output tokens by "
            "60-90%. Install with: pip install rtk-py && rtk init -g --auto-patch "
            "(https://github.com/rtk-ai/rtk)"
        )
        return (base + rtk_tip).strip()

    if not is_rtk_hooks_installed():
        rtk_tip = (
            "\n\nTip: RTK is installed but hooks may not be active in Claude Code. "
            "Run: rtk init -g --auto-patch  to set up hooks."
        )
        return (base + rtk_tip).strip()

    # RTK is installed and hooks are active — the command may have produced
    # large output even after compression.
    return base


# ---------------------------------------------------------------------------
# 5. API Response Model
# ---------------------------------------------------------------------------


class RtkStatus(BaseModel):
    """RTK installation and savings status for API responses."""

    installed: bool
    version: str | None
    hooks_installed: bool
    db_path: str | None
    savings_24h: dict[str, Any] | None


def get_rtk_status() -> RtkStatus:
    """Build a complete :class:`RtkStatus` snapshot.

    Gathers RTK installation info, hook status, database path, and
    the last 24 hours of token savings in a single call.
    """
    installed = is_rtk_installed()
    version = get_rtk_version() if installed else None
    hooks_installed = is_rtk_hooks_installed()
    db_path = get_rtk_db_path()
    savings = get_rtk_savings_summary(since_hours=24) if db_path else None

    return RtkStatus(
        installed=installed,
        version=version,
        hooks_installed=hooks_installed,
        db_path=str(db_path) if db_path else None,
        savings_24h=savings,
    )


async def get_rtk_status_async() -> RtkStatus:
    """Async variant of :func:`get_rtk_status`."""
    installed = is_rtk_installed()
    version = get_rtk_version() if installed else None
    hooks_installed = is_rtk_hooks_installed()
    db_path = get_rtk_db_path()
    savings = (
        await get_rtk_savings_summary_async(since_hours=24) if db_path else None
    )

    return RtkStatus(
        installed=installed,
        version=version,
        hooks_installed=hooks_installed,
        db_path=str(db_path) if db_path else None,
        savings_24h=savings,
    )
