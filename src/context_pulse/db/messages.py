"""CRUD for the pending_messages table (hook additionalContext injection)."""

from __future__ import annotations

import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)


async def queue_message(
    db: aiosqlite.Connection,
    session_id: str,
    message: str,
) -> int:
    """Insert a pending message for a session. Returns the row id."""
    cursor = await db.execute(
        "INSERT INTO pending_messages (session_id, message, created_at) VALUES (?, ?, ?)",
        (session_id, message, int(time.time() * 1000)),
    )
    await db.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        return 0
    logger.debug("Queued message for session=%s: %s", session_id, message[:80])
    return row_id


async def consume_messages(
    db: aiosqlite.Connection,
    session_id: str,
) -> list[str]:
    """Read and mark all unconsumed messages for a session.

    Returns the message strings. Marks them as consumed so they
    won't be returned again.
    """
    cursor = await db.execute(
        "SELECT id, message FROM pending_messages "
        "WHERE session_id = ? AND consumed = 0 ORDER BY created_at ASC",
        (session_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return []

    messages = [str(row["message"]) for row in rows]
    ids = [int(row["id"]) for row in rows]

    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"UPDATE pending_messages SET consumed = 1 WHERE id IN ({placeholders})",  # noqa: S608
        ids,
    )
    await db.commit()
    return messages


async def has_message_like(
    db: aiosqlite.Connection,
    session_id: str,
    pattern: str,
) -> bool:
    """Check if a message matching pattern exists (consumed or not) for dedup."""
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cursor = await db.execute(
        "SELECT COUNT(*) FROM pending_messages "
        "WHERE session_id = ? AND message LIKE ? ESCAPE '\\'",
        (session_id, f"%{escaped}%"),
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    return int(row[0]) > 0
