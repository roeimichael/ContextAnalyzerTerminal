"""CRUD operations for the anomalies table."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger("context_analyzer_tool.db.anomalies")


async def insert_anomaly(
    db: aiosqlite.Connection,
    session_id: str,
    task_type: str,
    token_cost: int,
    z_score: float,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
    timestamp_ms: int,
) -> int:
    """Insert a new anomaly row and return its id.

    The anomaly is inserted with notified=0. Notification is handled
    separately by the notifier layer (Phase 3).
    """
    cursor = await db.execute(
        """
        INSERT INTO anomalies
            (session_id, task_type, token_cost, z_score,
             cause, severity, suggestion, notified, timestamp_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (session_id, task_type, token_cost, z_score,
         cause, severity, suggestion, timestamp_ms),
    )
    await db.commit()
    row_id: int | None = cursor.lastrowid
    if row_id is None:
        msg = "INSERT into anomalies did not return a lastrowid"
        raise RuntimeError(msg)
    logger.debug("Inserted anomaly %d for %s (z=%.2f)", row_id, task_type, z_score)
    return row_id


async def update_anomaly_classification(
    db: aiosqlite.Connection,
    anomaly_id: int,
    cause: str,
    severity: str,
    suggestion: str,
) -> None:
    """Update the classifier fields on an existing anomaly row."""
    await db.execute(
        """
        UPDATE anomalies
           SET cause = ?, severity = ?, suggestion = ?
         WHERE id = ?
        """,
        (cause, severity, suggestion, anomaly_id),
    )
    await db.commit()
    logger.debug("Updated classification for anomaly %d", anomaly_id)


async def check_cooldown(
    db: aiosqlite.Connection,
    session_id: str,
    task_type: str,
    since_ms: int,
) -> bool:
    """Return True if an anomaly exists for (session_id, task_type) since *since_ms*.

    Used for cooldown deduplication.
    """
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM anomalies
         WHERE session_id = ?
           AND task_type = ?
           AND timestamp_ms >= ?
        """,
        (session_id, task_type, since_ms),
    )
    row = await cursor.fetchone()
    return row is not None and int(row[0]) > 0


async def get_recent_anomalies(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent anomalies, newest first.

    Optionally filtered by session_id.
    """
    clauses: list[str] = []
    params: list[str | int] = []

    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT * FROM anomalies
         {where}
         ORDER BY timestamp_ms DESC
         LIMIT ?
    """
    params.append(limit)

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_anomaly_count(
    db: aiosqlite.Connection,
    session_id: str | None = None,
) -> int:
    """Return the total number of anomalies, optionally for a session."""
    if session_id is not None:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE session_id = ?",
            (session_id,),
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM anomalies")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0
