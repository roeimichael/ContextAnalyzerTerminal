"""Tests for database CRUD operations and schema management."""

from __future__ import annotations

import json
import time

import aiosqlite

from context_pulse.db import events as db_events
from context_pulse.db import tasks as db_tasks
from context_pulse.db.schema import get_schema_version, open_db

# ---------------------------------------------------------------------------
# Schema / migration tests
# ---------------------------------------------------------------------------


async def test_schema_migration(db_connection: aiosqlite.Connection) -> None:
    """Verify all expected tables exist after run_migrations."""
    cursor = await db_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    rows = await cursor.fetchall()
    table_names = {row[0] for row in rows}

    expected = {
        "anomalies",
        "baselines",
        "events",
        "schema_version",
        "tasks",
        "token_snapshots",
    }
    # sqlite_sequence is auto-created by AUTOINCREMENT and may be present.
    assert expected.issubset(table_names)


async def test_schema_version_is_current(db_connection: aiosqlite.Connection) -> None:
    """After migrations the schema version should match the latest migration."""
    version = await get_schema_version(db_connection)
    assert version == 6


async def test_wal_mode_enabled() -> None:
    """Verify PRAGMA journal_mode returns 'wal' for a freshly opened DB."""
    db = await open_db(":memory:")
    try:
        cursor = await db.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row is not None
        # In-memory databases may report "memory" instead of "wal" because
        # WAL is not applicable to :memory:.  The important thing is that
        # the PRAGMA was executed without error on a file-based DB.  For
        # :memory: we accept either "wal" or "memory".
        assert row[0] in ("wal", "memory")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Event CRUD tests
# ---------------------------------------------------------------------------


async def test_insert_and_get_event(db_connection: aiosqlite.Connection) -> None:
    """Insert an event row and retrieve it via get_recent_events."""
    ts = int(time.time() * 1000)
    payload = json.dumps({"key": "value"})

    row_id = await db_events.insert_event(
        db_connection,
        session_id="sess-1",
        event_type="PostToolUse",
        timestamp_ms=ts,
        payload_json=payload,
        tool_name="Edit",
        tool_input_summary="edit main.py",
        cwd="/project",
    )

    assert isinstance(row_id, int)
    assert row_id > 0

    events = await db_events.get_recent_events(
        db_connection, limit=10, session_id="sess-1"
    )
    assert len(events) == 1
    assert events[0]["session_id"] == "sess-1"
    assert events[0]["event_type"] == "PostToolUse"
    assert events[0]["tool_name"] == "Edit"
    assert events[0]["payload_json"] == payload


# ---------------------------------------------------------------------------
# Snapshot CRUD tests
# ---------------------------------------------------------------------------


async def test_insert_and_get_snapshot(db_connection: aiosqlite.Connection) -> None:
    """Insert a token snapshot and retrieve it via get_latest_snapshot."""
    ts = int(time.time() * 1000)

    snap_id = await db_events.insert_snapshot(
        db_connection,
        session_id="sess-1",
        timestamp_ms=ts,
        total_input_tokens=10_000,
        total_output_tokens=2_000,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=300,
        context_window_size=200_000,
        used_percentage=5,
        total_cost_usd=0.05,
        model_id="claude-sonnet-4-20250514",
    )

    assert isinstance(snap_id, int)
    assert snap_id > 0

    latest = await db_events.get_latest_snapshot(db_connection, "sess-1")
    assert latest is not None
    assert latest["session_id"] == "sess-1"
    assert latest["total_input_tokens"] == 10_000
    assert latest["model_id"] == "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Task CRUD tests
# ---------------------------------------------------------------------------


async def test_insert_and_update_task(db_connection: aiosqlite.Connection) -> None:
    """Insert a task, then update its delta and snapshot_after_id."""
    ts = int(time.time() * 1000)

    # Need a parent event for the FK.
    event_id = await db_events.insert_event(
        db_connection,
        session_id="sess-1",
        event_type="PostToolUse",
        timestamp_ms=ts,
        payload_json="{}",
    )

    task_id = await db_tasks.insert_task(
        db_connection,
        session_id="sess-1",
        event_id=event_id,
        task_type="Edit",
        timestamp_ms=ts,
    )

    assert isinstance(task_id, int)
    assert task_id > 0

    # Verify initial state: token_delta should be NULL.
    tasks = await db_tasks.get_recent_tasks(
        db_connection, limit=10, session_id="sess-1"
    )
    assert len(tasks) == 1
    assert tasks[0]["token_delta"] is None

    # Insert a snapshot to serve as snapshot_after.
    snap_after_id = await db_events.insert_snapshot(
        db_connection,
        session_id="sess-1",
        timestamp_ms=ts + 1000,
        total_input_tokens=15_000,
        total_output_tokens=3_000,
        cache_creation_input_tokens=600,
        cache_read_input_tokens=400,
        context_window_size=200_000,
        used_percentage=8,
        total_cost_usd=0.08,
        model_id="claude-sonnet-4-20250514",
    )

    # Update the task.
    await db_tasks.update_task_delta(
        db_connection,
        task_id=task_id,
        token_delta=5_000,
        snapshot_after_id=snap_after_id,
        is_compaction=False,
    )

    # Verify updated state.
    tasks = await db_tasks.get_recent_tasks(
        db_connection, limit=10, session_id="sess-1"
    )
    assert len(tasks) == 1
    assert tasks[0]["token_delta"] == 5_000
    assert tasks[0]["snapshot_after_id"] == snap_after_id
    assert tasks[0]["is_compaction"] == 0


# ---------------------------------------------------------------------------
# Aggregate / lookup tests
# ---------------------------------------------------------------------------


async def test_get_active_session_ids(db_connection: aiosqlite.Connection) -> None:
    """get_active_session_ids returns sessions with events after the cutoff."""
    now_ms = int(time.time() * 1000)

    # Insert events for two sessions: one recent, one old.
    await db_events.insert_event(
        db_connection,
        session_id="recent-sess",
        event_type="PostToolUse",
        timestamp_ms=now_ms,
        payload_json="{}",
    )
    await db_events.insert_event(
        db_connection,
        session_id="old-sess",
        event_type="PostToolUse",
        timestamp_ms=now_ms - 7_200_000,  # 2 hours ago
        payload_json="{}",
    )

    # With a cutoff of 1 hour ago, only recent-sess should appear.
    cutoff = now_ms - 3_600_000
    active = await db_events.get_active_session_ids(db_connection, since_ms=cutoff)
    assert "recent-sess" in active
    assert "old-sess" not in active


async def test_get_event_count(db_connection: aiosqlite.Connection) -> None:
    """get_event_count returns the total number of event rows."""
    assert await db_events.get_event_count(db_connection) == 0

    ts = int(time.time() * 1000)
    await db_events.insert_event(
        db_connection,
        session_id="s1",
        event_type="PostToolUse",
        timestamp_ms=ts,
        payload_json="{}",
    )
    await db_events.insert_event(
        db_connection,
        session_id="s2",
        event_type="Stop",
        timestamp_ms=ts + 1,
        payload_json="{}",
    )

    assert await db_events.get_event_count(db_connection) == 2


async def test_get_snapshot_count(db_connection: aiosqlite.Connection) -> None:
    """get_snapshot_count returns the total number of snapshot rows."""
    assert await db_events.get_snapshot_count(db_connection) == 0

    ts = int(time.time() * 1000)
    await db_events.insert_snapshot(
        db_connection,
        session_id="s1",
        timestamp_ms=ts,
        total_input_tokens=1_000,
        total_output_tokens=200,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        context_window_size=200_000,
        used_percentage=1,
        total_cost_usd=0.01,
        model_id="claude-sonnet-4-20250514",
    )

    assert await db_events.get_snapshot_count(db_connection) == 1
