"""CRUD operations for the tasks table."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def insert_task(
    db: aiosqlite.Connection,
    session_id: str,
    event_id: int,
    task_type: str,
    timestamp_ms: int,
    snapshot_before_id: int | None = None,
    token_delta: int | None = None,
    is_compaction: bool = False,
    estimated_tokens: int | None = None,
) -> int:
    """Insert a new task row and return its id.

    Parameters
    ----------
    db:
        An open aiosqlite connection with ``row_factory`` set.
    session_id:
        The session this task belongs to.
    event_id:
        Foreign key into the events table.
    task_type:
        Free-form label such as ``"code_edit"`` or ``"chat_reply"``.
    timestamp_ms:
        Unix-epoch milliseconds when the task started.
    snapshot_before_id:
        Optional FK into token_snapshots (state before).
    token_delta:
        Optional token-count change.  May be filled later via
        :func:`update_task_delta`.
    is_compaction:
        Whether this task represents a compaction event.  Stored as
        ``0`` / ``1`` in SQLite.

    Returns
    -------
    int
        The ``id`` (ROWID) of the newly inserted row.
    """
    cursor = await db.execute(
        """
        INSERT INTO tasks (
            session_id,
            event_id,
            task_type,
            timestamp_ms,
            snapshot_before_id,
            token_delta,
            is_compaction,
            estimated_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            event_id,
            task_type,
            timestamp_ms,
            snapshot_before_id,
            token_delta,
            int(is_compaction),
            estimated_tokens,
        ),
    )
    await db.commit()
    row_id: int | None = cursor.lastrowid
    if row_id is None:  # pragma: no cover – should never happen for INSERT
        msg = "INSERT into tasks did not return a lastrowid"
        raise RuntimeError(msg)
    logger.debug(
        "Inserted task %d (type=%s, session=%s)", row_id, task_type, session_id
    )
    return row_id


async def update_task_delta(
    db: aiosqlite.Connection,
    task_id: int,
    token_delta: int | None,
    snapshot_after_id: int,
    is_compaction: bool = False,
) -> None:
    """Update the delta-related columns on an existing task.

    Typically called once the *after* snapshot has been captured so the
    delta can be computed.

    Parameters
    ----------
    db:
        An open aiosqlite connection.
    task_id:
        Primary key of the task to update.
    token_delta:
        Computed token-count change (may be ``None`` if unknown).
    snapshot_after_id:
        FK into token_snapshots (state after).
    is_compaction:
        Whether this task represents a compaction event.
    """
    await db.execute(
        """
        UPDATE tasks
           SET token_delta      = ?,
               snapshot_after_id = ?,
               is_compaction    = ?
         WHERE id = ?
        """,
        (token_delta, snapshot_after_id, int(is_compaction), task_id),
    )
    await db.commit()
    logger.debug(
        "Updated task %d: delta=%s, snapshot_after=%d, compaction=%s",
        task_id,
        token_delta,
        snapshot_after_id,
        is_compaction,
    )


async def get_recent_tasks(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: str | None = None,
    task_type: str | None = None,
    exclude_compaction: bool = False,
) -> list[dict[str, Any]]:
    """Return the most recent tasks, newest first.

    Parameters
    ----------
    db:
        An open aiosqlite connection with ``row_factory = aiosqlite.Row``.
    limit:
        Maximum number of rows to return.
    session_id:
        If provided, filter to this session only.
    task_type:
        If provided, filter to this task type only.
    exclude_compaction:
        If ``True``, exclude rows where ``is_compaction = 1``.

    Returns
    -------
    list[dict[str, Any]]
        Each row converted to a plain dict.
    """
    clauses: list[str] = []
    params: list[str | int] = []

    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)

    if task_type is not None:
        clauses.append("task_type = ?")
        params.append(task_type)

    if exclude_compaction:
        clauses.append("is_compaction = 0")

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT *
          FROM tasks
         {where}
         ORDER BY timestamp_ms DESC
         LIMIT ?
    """
    params.append(limit)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_tasks_by_type(
    db: aiosqlite.Connection,
    task_type: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent tasks of a given type that have a computed delta.

    Used for baseline computation -- only rows where ``token_delta`` is
    not ``NULL`` are meaningful for statistics.

    Parameters
    ----------
    db:
        An open aiosqlite connection with ``row_factory = aiosqlite.Row``.
    task_type:
        The task type to filter on.
    limit:
        Maximum number of rows to return.

    Returns
    -------
    list[dict[str, Any]]
        Each row converted to a plain dict.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT *
          FROM tasks
         WHERE task_type = ?
           AND token_delta IS NOT NULL
         ORDER BY timestamp_ms DESC
         LIMIT ?
        """,
        (task_type, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_null_delta_tasks(
    db: aiosqlite.Connection,
    older_than_ms: int,
) -> list[dict[str, Any]]:
    """Return tasks that still have no delta and are older than the cutoff.

    Used by a cleanup routine to find orphaned / pending tasks whose
    after-snapshot was never captured.

    Parameters
    ----------
    db:
        An open aiosqlite connection with ``row_factory = aiosqlite.Row``.
    older_than_ms:
        Unix-epoch milliseconds; only tasks with ``timestamp_ms`` strictly
        less than this value are returned.

    Returns
    -------
    list[dict[str, Any]]
        Each row converted to a plain dict.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT *
          FROM tasks
         WHERE token_delta IS NULL
           AND timestamp_ms < ?
        """,
        (older_than_ms,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
