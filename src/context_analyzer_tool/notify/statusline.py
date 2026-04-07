"""Statusline formatter with anomaly badges for context-analyzer-tool (Phase 3).

Produces compact single-line strings suitable for rendering in the Claude
Code statusline hook.  An optional anomaly badge can replace the rate-limit
section to keep the line short when an alert is active.
"""

from __future__ import annotations


def _format_tokens_short(n: int) -> str:
    """Format tokens in compact form: >=1000 as ``'8.4k'``, else plain."""
    if n >= 1000:
        k = n / 1000
        # Use one decimal place; drop trailing ".0" for whole numbers
        formatted = f"{k:.1f}"
        if formatted.endswith(".0"):
            formatted = formatted[:-2]
        return f"{formatted}k"
    return str(n)


def _progress_bar(pct: int, width: int = 10) -> str:
    """Build a Unicode progress bar of *width* characters.

    Uses ``\u2588`` (full block) for filled segments and ``\u2591`` (light
    shade) for empty segments.
    """
    filled = round(pct / 100 * width)
    filled = max(0, min(filled, width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def format_anomaly_badge(
    task_type: str,
    token_delta: int,
    z_score: float,
) -> str:
    """Build a short anomaly badge for the statusline.

    Parameters
    ----------
    task_type:
        The tool/task that triggered the anomaly (e.g. ``"Bash"``).
    token_delta:
        The observed token cost.
    z_score:
        Standard deviations above the baseline mean.

    Returns
    -------
    str
        A compact badge like ``"\u26a0 Bash 8.4k (4.2\u03c3)"``.
    """
    tokens_str = _format_tokens_short(token_delta)
    z_str = f"{z_score:.1f}"
    return f"\u26a0 {task_type} {tokens_str} ({z_str}\u03c3)"


def format_statusline_with_anomaly(
    model_name: str,
    used_pct: int,
    total_cost: float,
    five_hour_pct: float,
    seven_day_pct: float,
    anomaly_badge: str | None = None,
) -> str:
    """Build the full statusline string.

    Parameters
    ----------
    model_name:
        Display name of the model (e.g. ``"Opus"``).
    used_pct:
        Context-window usage percentage (0-100).
    total_cost:
        Cumulative session cost in USD.
    five_hour_pct:
        5-hour rate-limit bucket usage percentage.
    seven_day_pct:
        7-day rate-limit bucket usage percentage.
    anomaly_badge:
        Optional anomaly badge from :func:`format_anomaly_badge`. When
        present it replaces the rate-limit section.

    Returns
    -------
    str
        A single-line statusline string.

    Examples
    --------
    >>> format_statusline_with_anomaly("Opus", 42, 0.01, 24.0, 41.0)
    'Opus | ctx 42% ████░░░░░░ | $0.01 | 5h: 24% | 7d: 41%'

    >>> badge = format_anomaly_badge("Bash", 8400, 4.2)
    >>> format_statusline_with_anomaly("Opus", 71, 0.03, 24.0, 41.0, badge)
    'Opus | ctx 71% ███████░░░ | ⚠ Bash 8.4k (4.2σ) | $0.03'
    """
    bar = _progress_bar(used_pct)
    cost_str = f"${total_cost:.2f}"

    if anomaly_badge is not None:
        return f"{model_name} | ctx {used_pct}% {bar} | {anomaly_badge} | {cost_str}"

    five_str = f"{five_hour_pct:.0f}"
    seven_str = f"{seven_day_pct:.0f}"
    return (
        f"{model_name} | ctx {used_pct}% {bar} | {cost_str} "
        f"| 5h: {five_str}% | 7d: {seven_str}%"
    )
