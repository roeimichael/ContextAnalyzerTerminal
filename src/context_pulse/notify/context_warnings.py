"""Proactive context threshold warnings injected via additionalContext.

When context usage crosses configured thresholds, a one-time message is
queued for the PostToolUse hook to inject into Claude's context. This
alerts both Claude and the user that context is getting heavy.
"""

from __future__ import annotations

import logging

import aiosqlite

from context_pulse.db import messages as db_messages
from context_pulse.engine.context_breakdown import FRESH_SESSION_COST

logger = logging.getLogger("context_pulse.notify.context_warnings")

_FRESH_COST_K = FRESH_SESSION_COST // 1000  # e.g. 13

# Threshold messages — each fires once per session
_THRESHOLDS: list[tuple[float, str, str]] = [
    (
        60.0,
        "CONTEXT_WARNING_60",
        "[context-pulse] Context usage at {pct:.0f}%. "
        "Your conversation history is consuming significant context — "
        "each message now costs ~{cost_per_turn}K tokens "
        "(vs ~{fresh_k}K for a fresh session). "
        "Consider starting a fresh session to reduce per-turn cost. "
        "Current burn rate suggests ~{est_remaining} of useful context remaining.",
    ),
    (
        70.0,
        "CONTEXT_WARNING_70",
        "[context-pulse] Context at {pct:.0f}% — compaction may begin soon. "
        "Earlier conversation details will start being lost. "
        "Strongly recommend: save important context to memory, then start a fresh session. "
        "A fresh session costs ~{fresh_k}K/turn vs your current ~{cost_per_turn}K/turn.",
    ),
    (
        90.0,
        "CONTEXT_WARNING_90",
        "[context-pulse] CRITICAL: Context at {pct:.0f}%. "
        "Response quality is degrading. Please save any important findings "
        "to memory immediately, then start a new session. "
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
    for threshold_pct, dedup_key, template in _THRESHOLDS:
        if used_percentage < threshold_pct:
            continue

        # Check if this warning was already sent for this session
        already_sent = await db_messages.has_message_like(
            db, session_id, dedup_key,
        )
        if already_sent:
            continue

        # Format and queue the message
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
