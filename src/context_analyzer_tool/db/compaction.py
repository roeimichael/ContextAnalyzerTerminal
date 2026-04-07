"""CRUD operations for the compaction_events table."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def insert_compaction_event(
    db: aiosqlite.Connection,
    session_id: str,
    trigger: str,
    timestamp_ms: int,
    pre_snapshot_id: int | None = None,
    post_snapshot_id: int | None = None,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
    tokens_saved: int | None = None,
    compact_summary: str | None = None,
) -> int:
    """Insert a compaction event and return its row id."""
    cursor = await db.execute(
        """INSERT INTO compaction_events
        (session_id, trigger, pre_snapshot_id, post_snapshot_id,
         tokens_before, tokens_after, tokens_saved, compact_summary, timestamp_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id, trigger, pre_snapshot_id, post_snapshot_id,
            tokens_before, tokens_after, tokens_saved, compact_summary,
            timestamp_ms,
        ),
    )
    await db.commit()
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    return row_id


async def get_recent_compactions(
    db: aiosqlite.Connection,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent compaction events, newest first."""
    if session_id:
        cursor = await db.execute(
            "SELECT * FROM compaction_events "
            "WHERE session_id = ? ORDER BY timestamp_ms DESC LIMIT ?",
            (session_id, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM compaction_events "
            "ORDER BY timestamp_ms DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
