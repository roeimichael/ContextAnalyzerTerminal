"""Database schema creation and migration management."""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import aiosqlite

logger = logging.getLogger("context_pulse.db.schema")

# ---------------------------------------------------------------------------
# V1 DDL -- every CREATE TABLE / CREATE INDEX from phase-1 architecture §2.1
# ---------------------------------------------------------------------------

V1_SCHEMA: str = """\
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL  -- unix ms
);

-- Raw hook events (append-only log)
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    agent_id        TEXT,
    event_type      TEXT NOT NULL,
    tool_name       TEXT,
    tool_input_summary TEXT,
    cwd             TEXT,
    timestamp_ms    INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_session_type ON events(session_id, event_type);

-- Token snapshots from statusline (append-only, one per assistant message)
CREATE TABLE IF NOT EXISTS token_snapshots (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id                  TEXT NOT NULL,
    timestamp_ms                INTEGER NOT NULL,
    total_input_tokens          INTEGER NOT NULL,
    total_output_tokens         INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    cache_read_input_tokens     INTEGER NOT NULL,
    context_window_size         INTEGER NOT NULL,
    used_percentage             INTEGER NOT NULL,
    total_cost_usd              REAL NOT NULL,
    model_id                    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_session_id ON token_snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_session_ts ON token_snapshots(session_id, timestamp_ms);

-- Detected anomalies (Phase 2, but schema created in Phase 1)
CREATE TABLE IF NOT EXISTS anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    token_cost      INTEGER NOT NULL,
    z_score         REAL NOT NULL,
    cause           TEXT,
    severity        TEXT,
    suggestion      TEXT,
    notified        INTEGER NOT NULL DEFAULT 0,
    timestamp_ms    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_anomalies_session_id ON anomalies(session_id);
CREATE INDEX IF NOT EXISTS idx_anomalies_timestamp ON anomalies(timestamp_ms);

-- Derived task records: one per tool call, with computed token delta
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    event_id            INTEGER NOT NULL REFERENCES events(id),
    task_type           TEXT NOT NULL,
    token_delta         INTEGER,
    is_compaction       INTEGER NOT NULL DEFAULT 0,
    snapshot_before_id  INTEGER REFERENCES token_snapshots(id),
    snapshot_after_id   INTEGER REFERENCES token_snapshots(id),
    timestamp_ms        INTEGER NOT NULL,
    anomaly_id          INTEGER REFERENCES anomalies(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_type ON tasks(task_type);
CREATE INDEX IF NOT EXISTS idx_tasks_timestamp ON tasks(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_tasks_event_id ON tasks(event_id);

-- Rolling baselines per task_type (Phase 2, but schema created in Phase 1)
CREATE TABLE IF NOT EXISTS baselines (
    task_type       TEXT PRIMARY KEY,
    mean            REAL NOT NULL,
    stddev          REAL NOT NULL,
    sample_count    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str]] = [
    (1, "initial schema"),
    (2, "classifier cache table and baseline persistence columns"),
    (3, "pending messages queue for hook additionalContext injection"),
    (4, "estimated token counts on tasks"),
    (5, "composite index for anomaly cooldown lookups"),
]

# Internal mapping from version number to the coroutine that applies it.
_MIGRATION_FUNCS: dict[int, Callable[[aiosqlite.Connection], Awaitable[None]]] = {}


def _register(version: int) -> Callable[
    [Callable[[aiosqlite.Connection], Awaitable[None]]],
    Callable[[aiosqlite.Connection], Awaitable[None]],
]:
    """Decorator that registers an async migration function for *version*."""

    def _decorator(
        func: Callable[[aiosqlite.Connection], Awaitable[None]],
    ) -> Callable[[aiosqlite.Connection], Awaitable[None]]:
        _MIGRATION_FUNCS[version] = func
        return func

    return _decorator


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def open_db(db_path: str) -> aiosqlite.Connection:
    """Open SQLite connection with WAL mode, busy_timeout, synchronous, foreign_keys.

    Sets ``row_factory = aiosqlite.Row``.
    Returns the connection -- caller is responsible for closing it.
    """
    db = await aiosqlite.connect(db_path)
    # Pragmas must be executed individually (not via executescript) so that
    # aiosqlite can process them correctly.
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA synchronous = NORMAL")
    await db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = aiosqlite.Row
    logger.debug("Opened database at %s with WAL mode", db_path)
    return db


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Check ``schema_version`` table, apply any unapplied migrations.

    Each migration runs inside a transaction.  Raises :class:`RuntimeError` if
    a migration fails.
    """
    current = await get_schema_version(db)
    logger.info("Current schema version: %d", current)

    for version, description in MIGRATIONS:
        if version <= current:
            continue

        migration_func = _MIGRATION_FUNCS.get(version)
        if migration_func is None:
            raise RuntimeError(
                f"No migration function registered for version {version}"
            )

        logger.info(
            "Applying migration v%d: %s", version, description
        )
        try:
            await migration_func(db)
            # Record the applied version.
            applied_at = int(time.time() * 1000)
            await db.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
            await db.commit()
            logger.info("Migration v%d applied successfully", version)
        except Exception as exc:
            await db.rollback()
            msg = f"Migration v{version} ({description}) failed: {exc}"
            logger.error(msg)
            raise RuntimeError(msg) from exc


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """Return the current schema version, or ``0`` if the table doesn't exist."""
    try:
        async with db.execute(
            "SELECT MAX(version) FROM schema_version"
        ) as cursor:
            row = await cursor.fetchone()
            if row is None or row[0] is None:
                return 0
            version: int = int(row[0])
            return version
    except Exception:
        # Table doesn't exist yet.
        return 0


# ---------------------------------------------------------------------------
# Migration v1 -- initial schema
# ---------------------------------------------------------------------------


@_register(1)
async def _apply_v1(db: aiosqlite.Connection) -> None:  # pyright: ignore[reportUnusedFunction]
    """Apply version 1 schema: all CREATE TABLE and CREATE INDEX statements."""
    await db.executescript(V1_SCHEMA)


# ---------------------------------------------------------------------------
# Migration v2 -- classifier cache table and baseline persistence columns
# ---------------------------------------------------------------------------


@_register(2)
async def _apply_v2(db: aiosqlite.Connection) -> None:  # pyright: ignore[reportUnusedFunction]
    """Apply version 2 schema: classifier cache and baseline extras."""
    await db.execute(
        """\
        CREATE TABLE IF NOT EXISTS classifier_cache (
            cache_key    TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            created_at   INTEGER NOT NULL
        )"""
    )
    await db.execute(
        "ALTER TABLE baselines ADD COLUMN m2 REAL NOT NULL DEFAULT 0.0"
    )
    await db.execute(
        "ALTER TABLE baselines ADD COLUMN window_json TEXT NOT NULL DEFAULT '[]'"
    )


@_register(3)
async def _apply_v3(db: aiosqlite.Connection) -> None:  # pyright: ignore[reportUnusedFunction]
    """Add pending_messages table for hook additionalContext injection."""
    await db.execute(
        """\
        CREATE TABLE IF NOT EXISTS pending_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            message     TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            consumed    INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_session "
        "ON pending_messages(session_id, consumed)"
    )


@_register(4)
async def _apply_v4(db: aiosqlite.Connection) -> None:  # pyright: ignore[reportUnusedFunction]
    """Add estimated token count columns to tasks table."""
    await db.execute(
        "ALTER TABLE tasks ADD COLUMN estimated_tokens INTEGER"
    )


@_register(5)
async def _apply_v5(db: aiosqlite.Connection) -> None:  # pyright: ignore[reportUnusedFunction]
    """Add composite index for anomaly cooldown lookups."""
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_anomalies_cooldown "
        "ON anomalies(session_id, task_type, timestamp_ms)"
    )
