"""Database maintenance: pruning old data based on retention policy."""

from __future__ import annotations

import logging
import time

import aiosqlite

logger = logging.getLogger("context_analyzer_tool.db.maintenance")


async def prune_old_data(
    db: aiosqlite.Connection,
    retention_days: int,
) -> dict[str, int]:
    """Delete rows older than *retention_days* from all tables.

    Skipped when *retention_days* is ``0`` (keep forever).
    Deletes in dependency order to respect foreign keys.

    Returns a dict mapping table name to number of rows deleted.
    """
    if retention_days <= 0:
        return {}

    cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
    deleted: dict[str, int] = {}

    # Order matters: children before parents
    tables = [
        ("tasks", "timestamp_ms"),
        ("anomalies", "timestamp_ms"),
        ("token_snapshots", "timestamp_ms"),
        ("events", "timestamp_ms"),
        ("pending_messages", "created_at"),
        ("classifier_cache", "created_at"),
    ]

    for table, ts_col in tables:
        try:
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?",  # noqa: S608
                (cutoff_ms,),
            )
            count = cursor.rowcount
            if count > 0:
                deleted[table] = count
        except Exception:
            logger.debug("Prune skipped for %s (table may not exist)", table)

    if deleted:
        await db.commit()
        total = sum(deleted.values())
        logger.info(
            "Pruned %d rows older than %d days: %s",
            total,
            retention_days,
            deleted,
        )

    return deleted


async def get_table_counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Return row counts for all data tables (for dry-run / diagnostics)."""
    counts: dict[str, int] = {}
    for table in ("events", "token_snapshots", "tasks", "anomalies",
                  "pending_messages", "classifier_cache", "baselines"):
        try:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            row = await cursor.fetchone()
            counts[table] = int(row[0]) if row else 0
        except Exception:
            pass
    return counts
