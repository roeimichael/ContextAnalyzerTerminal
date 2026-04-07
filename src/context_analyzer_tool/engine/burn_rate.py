"""Burn rate projection: estimate turns remaining before context fills."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Minimum snapshots needed for a meaningful regression
_MIN_SNAPSHOTS = 5


def compute_burn_rate(
    snapshots: list[dict[str, Any]],
    context_window_size: int,
) -> dict[str, Any] | None:
    """Compute linear regression over used_percentage vs. snapshot index.

    Parameters
    ----------
    snapshots:
        List of snapshot dicts ordered by timestamp ascending, each with
        at least ``used_percentage`` and ``timestamp_ms``.
    context_window_size:
        Total context window size in tokens.

    Returns
    -------
    dict or None:
        ``{"pct_per_turn": float, "turns_remaining": int, "fills_at_turn": int}``
        or ``None`` if insufficient data or non-positive slope.
    """
    if len(snapshots) < _MIN_SNAPSHOTS:
        return None

    n = len(snapshots)
    xs = list(range(n))
    ys = [float(s.get("used_percentage", 0)) for s in snapshots]

    # Simple linear regression: y = a + b*x
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    denominator = sum((x - x_mean) ** 2 for x in xs)

    if denominator == 0:
        return None

    slope = numerator / denominator  # pct per turn

    if slope <= 0:
        # Context is not growing (or shrinking due to compaction)
        return None

    current_pct = ys[-1]
    remaining_pct = 100.0 - current_pct

    if remaining_pct <= 0:
        return {
            "pct_per_turn": round(slope, 2),
            "turns_remaining": 0,
            "fills_at_turn": n,
        }

    turns_remaining = int(remaining_pct / slope)

    return {
        "pct_per_turn": round(slope, 2),
        "turns_remaining": max(0, turns_remaining),
        "fills_at_turn": n + turns_remaining,
    }
