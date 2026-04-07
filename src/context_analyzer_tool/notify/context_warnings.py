"""Proactive context threshold warnings injected via additionalContext.

When context usage crosses configured thresholds, a one-time message is
queued for the PostToolUse hook to inject into Claude's context. This
alerts both Claude and the user that context is getting heavy.
"""

from __future__ import annotations

import logging

import aiosqlite

from context_analyzer_tool.db import messages as db_messages
from context_analyzer_tool.engine.context_breakdown import FRESH_SESSION_COST

logger = logging.getLogger("context_analyzer_tool.notify.context_warnings")

_FRESH_COST_K = FRESH_SESSION_COST // 1000  # e.g. 13

# Threshold messages — each fires once per session
_THRESHOLDS: list[tuple[float, str, str]] = [
    (
        60.0,
        "CONTEXT_WARNING_60",
        "[CAT] Context usage at {pct:.0f}%. "
        "Each message now costs ~{cost_per_turn}K tokens "
        "(vs ~{fresh_k}K for a fresh session). "
        "Run /compact to reclaim space, or start a fresh session. "
        "Remaining useful context: {est_remaining}.",
    ),
    (
        70.0,
        "CONTEXT_WARNING_70",
        "[CAT] Context at {pct:.0f}% \u2014 auto-compact approaching (~83%). "
        "Run /compact now to compact proactively (you control what's preserved). "
        "Save important context to memory first. "
        "A fresh session costs ~{fresh_k}K/turn vs your current ~{cost_per_turn}K/turn.",
    ),
    (
        90.0,
        "CONTEXT_WARNING_90",
        "[CAT] CRITICAL: Context at {pct:.0f}%. "
        "Run /clear to start fresh within this session, "
        "or save findings to memory and open a new session. "
        "Current cost: ~{cost_per_turn}K/turn. Fresh session: ~{fresh_k}K/turn.",
    ),
]


def _estimate_cost_per_turn(used_pct: float, context_window_size: int) -> int:
    """Estimate tokens per turn based on current usage."""
    used_tokens = int(context_window_size * used_pct / 100)
    return max(_FRESH_COST_K, used_tokens // 1000)


def _estimate_remaining(used_pct: float) -> str:
    """Estimate remaining useful context time."""
    remaining_pct = 100.0 - used_pct
    if remaining_pct <= 5:
        return "almost no"
    if remaining_pct <= 15:
        return "very little"
    if remaining_pct <= 30:
        return "limited"
    return "moderate"


async def check_context_thresholds(
    db: aiosqlite.Connection,
    session_id: str,
    used_percentage: float,
    context_window_size: int,
) -> None:
    """Check if context usage crossed any thresholds and queue warnings.

    Each threshold fires only once per session (deduplication via
    message pattern matching in the pending_messages table).
    """
    # Iterate from highest threshold down so we only fire the most relevant one
    for threshold_pct, dedup_key, template in reversed(_THRESHOLDS):
        if used_percentage < threshold_pct:
            continue

        # Check if this warning was already sent for this session
        already_sent = await db_messages.has_message_like(
            db, session_id, dedup_key,
        )
        if already_sent:
            break

        # Format and queue only the highest applicable threshold
        cost_per_turn = _estimate_cost_per_turn(used_percentage, context_window_size)
        est_remaining = _estimate_remaining(used_percentage)

        message = template.format(
            pct=used_percentage,
            cost_per_turn=cost_per_turn,
            est_remaining=est_remaining,
            fresh_k=_FRESH_COST_K,
        )
        # Prepend dedup key (invisible to user, used for has_message_like)
        tagged_message = f"<!-- {dedup_key} -->\n{message}"

        await db_messages.queue_message(db, session_id, tagged_message)
        logger.info(
            "Context warning queued: session=%s threshold=%.0f%% actual=%.0f%%",
            session_id,
            threshold_pct,
            used_percentage,
        )
        break
