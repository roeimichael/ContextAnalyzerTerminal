"""In-session alert formatter for context-analyzer-tool (Phase 3).

Produces human-readable alert strings intended for injection into Claude
Code's ``additionalContext`` field via the PostToolUse hook.  When Claude
sees these alerts it can adjust its own behaviour to reduce token waste.
"""

from __future__ import annotations


def _format_tokens(n: int) -> str:
    """Format a token count with thousands separators."""
    return f"{n:,}"


def format_session_alert(
    task_type: str,
    token_delta: int,
    z_score: float,
    baseline_mean: float,
    cause: str | None,
    suggestion: str | None,
) -> str:
    """Build a formatted in-session alert string.

    Parameters
    ----------
    task_type:
        The tool/task that triggered the anomaly (e.g. ``"Bash"``).
    token_delta:
        The observed token cost for the tool use.
    z_score:
        How many standard deviations above the baseline mean.
    baseline_mean:
        The rolling baseline mean for this task type.
    cause:
        Optional human-readable root-cause description.
    suggestion:
        Optional actionable suggestion for the user/agent.

    Returns
    -------
    str
        A multi-line alert string ready for ``additionalContext``.

    Examples
    --------
    >>> format_session_alert("Bash", 8400, 4.2, 2000.0, None, None)
    '[CAT] ⚠ Last Bash command cost 8,400 tokens (4.2σ above your baseline of 2,000).'
    """
    tokens_str = _format_tokens(token_delta)
    mean_str = _format_tokens(int(baseline_mean))
    z_str = f"{z_score:.1f}"

    headline = (
        f"[CAT] \u26a0 Last {task_type} command cost {tokens_str} tokens "
        f"({z_str}\u03c3 above your baseline of {mean_str})."
    )

    lines: list[str] = [headline]

    if cause is not None:
        lines.append(f"Cause: {cause}")

    if suggestion is not None:
        lines.append(f"Consider: {suggestion}")

    return "\n".join(lines)
