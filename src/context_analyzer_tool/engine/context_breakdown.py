"""Context cost breakdown analysis.

Compares what a fresh session costs vs. the current session, highlighting
how much of each API call is consumed by conversation history overhead.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("context_analyzer_tool.engine.context_breakdown")

# Fixed baseline costs for a fresh Claude Code session (in tokens).
# These are constant regardless of conversation length.
FIXED_CONTEXT: dict[str, int] = {
    "System prompt": 4_000,
    "Global CLAUDE.md": 1_600,
    "Project CLAUDE.md": 2_500,
    "MEMORY.md index": 400,
    "Tool definitions": 3_500,
    "Git status snapshot": 600,
    "System reminders": 800,
    "Skill list": 300,
}

FRESH_SESSION_COST: int = sum(FIXED_CONTEXT.values())  # ~13,700


def compute_breakdown(
    total_input_tokens: int,
    total_output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    context_window_size: int,
    used_percentage: float,
) -> dict[str, Any]:
    """Compute the context cost breakdown for the current session.

    Returns a dict with:
    - fixed_components: dict mapping component name to token count
    - fresh_session_cost: total fixed cost for a new session
    - conversation_history: estimated tokens consumed by history
    - current_message_cost: total tokens sent per message now
    - overhead_ratio: how many times more expensive current is vs. fresh
    - savings_if_reset: tokens saved per message by starting fresh
    - recommendation: human-readable recommendation
    """
    # The total context currently sent to the API per message
    # = input_tokens (which includes system prompt + history + tools)
    current_message_cost = total_input_tokens

    # Conversation history = total context - fixed components
    conversation_history = max(0, current_message_cost - FRESH_SESSION_COST)

    # Overhead ratio
    overhead_ratio = (
        current_message_cost / FRESH_SESSION_COST
        if FRESH_SESSION_COST > 0
        else 1.0
    )

    # Savings per message if they start fresh
    savings_per_message = conversation_history

    # Build recommendation
    if overhead_ratio < 2.0:
        recommendation = "Context is healthy. No action needed."
        severity = "low"
    elif overhead_ratio < 5.0:
        recommendation = (
            f"Each message costs {overhead_ratio:.0f}x more than a fresh session. "
            "Consider starting a new session when convenient."
        )
        severity = "medium"
    elif overhead_ratio < 10.0:
        recommendation = (
            f"Each message costs {overhead_ratio:.0f}x more than a fresh session. "
            f"You'd save ~{savings_per_message:,} tokens per message by starting fresh. "
            "Strongly recommend a new session."
        )
        severity = "high"
    else:
        recommendation = (
            f"CRITICAL: Each message costs {overhead_ratio:.0f}x more than a fresh session. "
            f"That's ~{savings_per_message:,} wasted tokens per message. "
            "Start a new session immediately."
        )
        severity = "critical"

    return {
        "fixed_components": FIXED_CONTEXT,
        "fresh_session_cost": FRESH_SESSION_COST,
        "conversation_history": conversation_history,
        "current_message_cost": current_message_cost,
        "overhead_ratio": round(overhead_ratio, 1),
        "savings_per_message": savings_per_message,
        "used_percentage": used_percentage,
        "context_window_size": context_window_size,
        "total_output_tokens": total_output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "recommendation": recommendation,
        "severity": severity,
    }


def format_breakdown_table(breakdown: dict[str, Any]) -> str:
    """Format the breakdown as a readable text table.

    Produces the exact table format the user requested:
    ┌──────────────────────┬─────────────┬───────────────┐
    │      Component       │  New chat   │   This chat   │
    ...
    """
    fixed = breakdown["fixed_components"]
    history = breakdown["conversation_history"]
    fresh_total = breakdown["fresh_session_cost"]
    current_total = breakdown["current_message_cost"]
    ratio = breakdown["overhead_ratio"]

    lines = [
        "+------------------------+-------------+---------------+",
        "|      Component         |  New chat   |   This chat   |",
        "+------------------------+-------------+---------------+",
    ]

    for name, tokens in fixed.items():
        lines.append(
            f"| {name:<22s} | {tokens:>9,} | {tokens:>11,}   |"
        )

    # Conversation history row — the key difference
    lines.append(
        "+------------------------+-------------+---------------+"
    )
    lines.append(
        f"| {'Conversation history':<22s} | {'0':>9s} | {history:>11,}   |"
    )
    lines.append(
        "+------------------------+-------------+---------------+"
    )
    lines.append(
        f"| {'TOTAL per message':<22s} | {fresh_total:>9,} | {current_total:>11,}   |"
    )
    lines.append(
        "+------------------------+-------------+---------------+"
    )
    lines.append("")
    lines.append(
        f"Overhead: {ratio}x  |  "
        f"History is {history:,} tokens "
        f"({history * 100 // max(current_total, 1)}% of each message)"
    )
    lines.append(f"Recommendation: {breakdown['recommendation']}")

    return "\n".join(lines)
