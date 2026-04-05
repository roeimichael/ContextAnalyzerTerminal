from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger("context_pulse.db.baselines")


async def get_baseline(
    db: aiosqlite.Connection,
    task_type: str,
) -> dict[str, Any] | None:
    """Return the baseline row for *task_type*, or None if not found.

    Returns a dict with keys: task_type, mean, stddev, sample_count,
    updated_at, m2, window_json.
    """
    cursor = await db.execute(
        "SELECT * FROM baselines WHERE task_type = ?",
        (task_type,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def upsert_baseline(
    db: aiosqlite.Connection,
    task_type: str,
    mean: float,
    stddev: float,
    sample_count: int,
    m2: float,
    window_json: str,
    updated_at: int,
) -> None:
    """Insert or update the baseline for *task_type*.

    Uses INSERT OR REPLACE to perform an upsert on the PRIMARY KEY (task_type).
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO baselines
            (task_type, mean, stddev, sample_count, m2, window_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_type, mean, stddev, sample_count, m2, window_json, updated_at),
    )
    await db.commit()
    logger.debug(
        "Upserted baseline for %s: mean=%.1f stddev=%.1f n=%d",
        task_type,
        mean,
        stddev,
        sample_count,
    )


async def get_all_baselines(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all baseline rows."""
    cursor = await db.execute(
        "SELECT * FROM baselines ORDER BY task_type"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
