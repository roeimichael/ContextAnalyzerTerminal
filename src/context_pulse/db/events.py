"""CRUD operations for the events and token_snapshots tables."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def insert_event(
    db: aiosqlite.Connection,
    session_id: str,
    event_type: str,
    timestamp_ms: int,
    payload_json: str,
    tool_name: str | None = None,
    tool_input_summary: str | None = None,
    agent_id: str | None = None,
    cwd: str | None = None,
) -> int:
    """Insert a row into the events table.

    Returns the new row id (lastrowid).
    """
    sql = (
        "INSERT INTO events "
        "(session_id, event_type, timestamp_ms, payload_json, "
        "tool_name, tool_input_summary, agent_id, cwd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    cursor = await db.execute(
        sql,
        (
            session_id,
            event_type,
            timestamp_ms,
            payload_json,
            tool_name,
            tool_input_summary,
            agent_id,
            cwd,
        ),
    )
    await db.commit()
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    logger.debug("Inserted event id=%d type=%s session=%s", row_id, event_type, session_id)
    return row_id


async def insert_snapshot(
    db: aiosqlite.Connection,
    session_id: str,
    timestamp_ms: int,
    total_input_tokens: int,
    total_output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    context_window_size: int,
    used_percentage: float,
    total_cost_usd: float,
    model_id: str,
) -> int:
    """Insert a row into the token_snapshots table.

    Returns the new row id (lastrowid).
    """
    sql = (
        "INSERT INTO token_snapshots "
        "(session_id, timestamp_ms, total_input_tokens, total_output_tokens, "
        "cache_creation_input_tokens, cache_read_input_tokens, "
        "context_window_size, used_percentage, total_cost_usd, model_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    cursor = await db.execute(
        sql,
        (
            session_id,
            timestamp_ms,
            total_input_tokens,
            total_output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            context_window_size,
            used_percentage,
            total_cost_usd,
            model_id,
        ),
    )
    await db.commit()
    row_id: int = cursor.lastrowid  # type: ignore[assignment]
    logger.debug("Inserted snapshot id=%d session=%s", row_id, session_id)
    return row_id


async def get_recent_events(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: str | None = None,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    """Select recent events ordered by timestamp_ms DESC.

    Optional filters by *session_id* and/or *event_type*.
    Returns a list of dicts.
    """
    clauses: list[str] = []
    params: list[str | int] = []

    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)

    where = ""
    if clauses:
        where = " WHERE " + " AND ".join(clauses)

    sql = f"SELECT * FROM events{where} ORDER BY timestamp_ms DESC LIMIT ?"
    params.append(limit)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_latest_snapshot(
    db: aiosqlite.Connection,
    session_id: str,
) -> dict[str, Any] | None:
    """Select the latest token_snapshot for a given session.

    Returns a dict or ``None`` if no snapshot exists.
    """
    sql = (
        "SELECT * FROM token_snapshots "
        "WHERE session_id = ? "
        "ORDER BY timestamp_ms DESC LIMIT 1"
    )
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, (session_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def get_recent_snapshots(
    db: aiosqlite.Connection,
    session_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Select token_snapshots for a session ordered by timestamp_ms DESC."""
    sql = (
        "SELECT * FROM token_snapshots "
        "WHERE session_id = ? "
        "ORDER BY timestamp_ms DESC LIMIT ?"
    )
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, (session_id, limit))
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_active_session_ids(
    db: aiosqlite.Connection,
    since_ms: int,
) -> list[str]:
    """Select distinct session IDs from events newer than *since_ms*.

    Used for session restoration on startup.
    """
    sql = "SELECT DISTINCT session_id FROM events WHERE timestamp_ms > ?"
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(sql, (since_ms,))
    rows = await cursor.fetchall()
    return [str(row["session_id"]) for row in rows]


async def get_event_count(db: aiosqlite.Connection) -> int:
    """Return the total number of rows in the events table."""
    cursor = await db.execute("SELECT COUNT(*) FROM events")
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


async def get_snapshot_count(db: aiosqlite.Connection) -> int:
    """Return the total number of rows in the token_snapshots table."""
    cursor = await db.execute("SELECT COUNT(*) FROM token_snapshots")
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])
