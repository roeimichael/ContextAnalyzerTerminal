"""OS-level desktop notifications for context-pulse (Phase 3).

Sends platform-specific system notifications when anomalies are detected.
Supports Windows (PowerShell balloon tips), macOS (``osascript``), and
Linux (``notify-send``).

All public functions are designed to never raise — errors are caught and
logged, returning ``False`` on failure.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

logger = logging.getLogger("context_pulse.notify.system")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_powershell(text: str) -> str:
    """Escape a string for safe embedding in a PowerShell single-quoted literal.

    PowerShell single-quoted strings only require doubling of single
    quotes.  We also strip control characters that could break the
    invocation.
    """
    # Remove control characters (keep newlines as spaces)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Double single quotes for PS single-quoted strings
    text = text.replace("'", "''")
    return text


def _escape_osascript(text: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    text = text.replace("\\", "\\\\")
    text = text.replace('"', '\\"')
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return text


def _sanitize_shell(text: str) -> str:
    """Strip characters that could cause issues in shell arguments."""
    # Remove control characters, keep printable content
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


# ---------------------------------------------------------------------------
# Core notification sender
# ---------------------------------------------------------------------------


async def send_system_notification(title: str, message: str) -> bool:
    """Send an OS-level desktop notification.

    Detects the current platform and dispatches to the appropriate
    notification mechanism:

    - **Windows**: PowerShell balloon notification via
      ``System.Windows.Forms.NotifyIcon``.
    - **macOS**: ``osascript -e 'display notification ...'``.
    - **Linux**: ``notify-send``.

    Parameters
    ----------
    title:
        The notification title/heading.
    message:
        The notification body text.

    Returns
    -------
    bool
        ``True`` if the notification was sent successfully, ``False`` on
        any error (including unsupported platform).
    """
    try:
        if sys.platform == "win32":
            return await _send_windows(title, message)
        elif sys.platform == "darwin":
            return await _send_macos(title, message)
        elif sys.platform.startswith("linux"):
            return await _send_linux(title, message)
        else:
            logger.warning(
                "Unsupported platform for system notifications: %s",
                sys.platform,
            )
            return False
    except Exception:
        logger.exception("Failed to send system notification")
        return False


# ---------------------------------------------------------------------------
# Platform implementations
# ---------------------------------------------------------------------------


async def _send_windows(title: str, message: str) -> bool:
    """Send a Windows balloon-tip notification via PowerShell.

    Uses ``System.Windows.Forms.NotifyIcon`` to display a balloon tip
    that auto-dismisses after 5 seconds.  The ``NotifyIcon`` is then
    disposed after a short sleep to allow the balloon to display.
    """
    escaped_title = _escape_powershell(title)
    escaped_message = _escape_powershell(message)

    script = (
        "[void][System.Reflection.Assembly]"
        "::LoadWithPartialName('System.Windows.Forms');"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(5000, '{escaped_title}', "
        f"'{escaped_message}', 'Warning');"
        "Start-Sleep -Seconds 6;"
        "$n.Dispose()"
    )

    try:
        process = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-Command",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            logger.warning(
                "PowerShell notification exited with code %d: %s",
                process.returncode,
                stderr_text[:200],
            )
            return False

        logger.debug("Windows balloon notification sent successfully")
        return True

    except TimeoutError:
        logger.warning("Windows notification timed out")
        return False
    except FileNotFoundError:
        logger.warning("PowerShell not found on PATH")
        return False
    except Exception:
        logger.exception("Windows notification failed")
        return False


async def _send_macos(title: str, message: str) -> bool:
    """Send a macOS notification via ``osascript``."""
    escaped_title = _escape_osascript(title)
    escaped_message = _escape_osascript(message)

    applescript = (
        f'display notification "{escaped_message}" '
        f'with title "{escaped_title}"'
    )

    try:
        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            applescript,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            logger.warning(
                "osascript notification exited with code %d: %s",
                process.returncode,
                stderr_text[:200],
            )
            return False

        logger.debug("macOS notification sent successfully")
        return True

    except TimeoutError:
        logger.warning("macOS notification timed out")
        return False
    except FileNotFoundError:
        logger.warning("osascript not found on PATH")
        return False
    except Exception:
        logger.exception("macOS notification failed")
        return False


async def _send_linux(title: str, message: str) -> bool:
    """Send a Linux notification via ``notify-send``."""
    clean_title = _sanitize_shell(title)
    clean_message = _sanitize_shell(message)

    try:
        process = await asyncio.create_subprocess_exec(
            "notify-send",
            clean_title,
            clean_message,
            "--urgency=normal",
            "--expire-time=5000",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            logger.warning(
                "notify-send exited with code %d: %s",
                process.returncode,
                stderr_text[:200],
            )
            return False

        logger.debug("Linux notification sent successfully")
        return True

    except TimeoutError:
        logger.warning("Linux notification timed out")
        return False
    except FileNotFoundError:
        logger.warning("notify-send not found; install libnotify-bin")
        return False
    except Exception:
        logger.exception("Linux notification failed")
        return False


# ---------------------------------------------------------------------------
# Anomaly formatting
# ---------------------------------------------------------------------------


def _severity_label(z_score: float) -> str:
    """Map a z-score to a human-readable severity label."""
    if z_score >= 4.0:
        return "High"
    elif z_score >= 3.0:
        return "Medium"
    else:
        return "Low"


def format_anomaly_notification(
    task_type: str,
    token_delta: int,
    z_score: float,
    baseline_mean: float,
    cause: str | None,
    suggestion: str | None,
) -> tuple[str, str]:
    """Format an anomaly into notification title and body.

    Parameters
    ----------
    task_type:
        The tool/task type string (e.g. ``"Bash"``, ``"Read"``).
    token_delta:
        The token delta that triggered the anomaly.
    z_score:
        The computed z-score.
    baseline_mean:
        The current baseline mean for this tool type.
    cause:
        Human-readable cause from the classifier, or ``None``.
    suggestion:
        Actionable suggestion from the classifier, or ``None``.

    Returns
    -------
    tuple[str, str]
        ``(title, message)`` ready for :func:`send_system_notification`.
    """
    severity = _severity_label(z_score)
    title = f"\u26a0 context-pulse \u2014 {severity} token spike"

    # Compute the multiplier relative to baseline
    if baseline_mean > 0:
        multiplier = token_delta / baseline_mean
        ratio_str = f"{multiplier:.1f}\u00d7 baseline"
    else:
        ratio_str = "above baseline"

    # Format token count with thousands separator
    delta_formatted = f"{token_delta:,}"

    lines: list[str] = [
        f"{task_type} used {delta_formatted} tokens ({ratio_str}).",
    ]

    if cause:
        lines.append(f"Cause: {cause}")
    if suggestion:
        lines.append(f"Fix: {suggestion}")

    message = "\n".join(lines)
    return title, message


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def notify_anomaly(
    task_type: str,
    token_delta: int,
    z_score: float,
    baseline_mean: float,
    cause: str | None,
    suggestion: str | None,
) -> bool:
    """Format and send an OS notification for an anomaly.

    Convenience wrapper that calls :func:`format_anomaly_notification`
    followed by :func:`send_system_notification`.

    Parameters
    ----------
    task_type:
        The tool/task type string (e.g. ``"Bash"``).
    token_delta:
        The token delta that triggered the anomaly.
    z_score:
        The computed z-score.
    baseline_mean:
        The current baseline mean for this tool type.
    cause:
        Human-readable cause from the classifier, or ``None``.
    suggestion:
        Actionable suggestion from the classifier, or ``None``.

    Returns
    -------
    bool
        ``True`` if the notification was sent successfully.
    """
    try:
        title, message = format_anomaly_notification(
            task_type=task_type,
            token_delta=token_delta,
            z_score=z_score,
            baseline_mean=baseline_mean,
            cause=cause,
            suggestion=suggestion,
        )
        return await send_system_notification(title, message)
    except Exception:
        logger.exception("Failed to notify anomaly")
        return False
