"""Tests for the in-memory delta correlation engine."""

from __future__ import annotations

import time
from collections.abc import Callable

import aiosqlite

from context_pulse.collector.delta_engine import (
    SessionState,
    cleanup_stale_sessions,
    on_session_stop,
    on_snapshot,
    on_tool_use,
    restore_sessions_from_db,
)
from context_pulse.collector.models import HookEventRequest, StatuslineSnapshotRequest
from context_pulse.db import events as db_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _insert_event_for(
    db: aiosqlite.Connection,
    event: HookEventRequest,
) -> int:
    """Insert the raw event row and return its id."""
    import json

    return await db_events.insert_event(
        db,
        session_id=event.session_id,
        event_type=event.event_type,
        timestamp_ms=event.timestamp_ms,
        payload_json=json.dumps(event.payload),
        tool_name=event.tool_name,
        tool_input_summary=event.tool_input_summary,
        agent_id=event.agent_id,
        cwd=event.cwd,
    )


async def _insert_snapshot_for(
    db: aiosqlite.Connection,
    snap: StatuslineSnapshotRequest,
) -> int:
    """Insert the snapshot row and return its id."""
    return await db_events.insert_snapshot(
        db,
        session_id=snap.session_id,
        timestamp_ms=snap.timestamp_ms,
        total_input_tokens=snap.total_input_tokens,
        total_output_tokens=snap.total_output_tokens,
        cache_creation_input_tokens=snap.cache_creation_input_tokens,
        cache_read_input_tokens=snap.cache_read_input_tokens,
        context_window_size=snap.context_window_size,
        used_percentage=snap.used_percentage,
        total_cost_usd=snap.total_cost_usd,
        model_id=snap.model_id,
    )


# ---------------------------------------------------------------------------
# Core delta tests
# ---------------------------------------------------------------------------


async def test_first_snapshot_no_delta(
    db_connection: aiosqlite.Connection,
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """The very first snapshot for a session should return an empty list."""
    sessions: dict[str, SessionState] = {}
    snap = sample_snapshot(
        session_id="sess-A", total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap_id = await _insert_snapshot_for(db_connection, snap)

    results = await on_snapshot(sessions, db_connection, snap, snap_id)

    assert results == []
    assert sessions["sess-A"].has_initial_snapshot is True
    assert sessions["sess-A"].last_snapshot_total_input == 10_000


async def test_single_tool_call_delta(
    db_connection: aiosqlite.Connection,
    sample_hook_event: Callable[..., HookEventRequest],
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """One tool call followed by a snapshot should produce the correct delta."""
    sessions: dict[str, SessionState] = {}
    sid = "sess-B"

    # First snapshot: baseline.
    snap1 = sample_snapshot(
        session_id=sid, total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap1_id = await _insert_snapshot_for(db_connection, snap1)
    await on_snapshot(sessions, db_connection, snap1, snap1_id)

    # Tool call.
    event = sample_hook_event(session_id=sid, tool_name="Edit")
    event_id = await _insert_event_for(db_connection, event)
    task_id = await on_tool_use(sessions, db_connection, event, event_id)
    assert task_id > 0

    # Second snapshot: delta should be 5000.
    snap2 = sample_snapshot(
        session_id=sid, total_output_tokens=15_000, cache_creation_input_tokens=0
    )
    snap2_id = await _insert_snapshot_for(db_connection, snap2)
    results = await on_snapshot(sessions, db_connection, snap2, snap2_id)

    assert len(results) == 1
    returned_task_id, delta, is_compaction = results[0]
    assert returned_task_id == task_id
    assert delta == 5_000
    assert is_compaction is False


async def test_multiple_pending_tools_last_gets_delta(
    db_connection: aiosqlite.Connection,
    sample_hook_event: Callable[..., HookEventRequest],
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """With multiple pending tool calls, the last gets the full delta; others get 0."""
    sessions: dict[str, SessionState] = {}
    sid = "sess-C"

    # Baseline snapshot.
    snap1 = sample_snapshot(
        session_id=sid, total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap1_id = await _insert_snapshot_for(db_connection, snap1)
    await on_snapshot(sessions, db_connection, snap1, snap1_id)

    # Three tool calls before the next snapshot.
    task_ids = []
    for tool in ("Read", "Edit", "Bash"):
        ev = sample_hook_event(session_id=sid, tool_name=tool)
        eid = await _insert_event_for(db_connection, ev)
        tid = await on_tool_use(sessions, db_connection, ev, eid)
        task_ids.append(tid)

    # Snapshot with delta of 9000.
    snap2 = sample_snapshot(
        session_id=sid, total_output_tokens=19_000, cache_creation_input_tokens=0
    )
    snap2_id = await _insert_snapshot_for(db_connection, snap2)
    results = await on_snapshot(sessions, db_connection, snap2, snap2_id)

    assert len(results) == 3

    # First two pending tools get delta=0.
    assert results[0] == (task_ids[0], 0, False)
    assert results[1] == (task_ids[1], 0, False)

    # Last pending tool gets the full delta.
    assert results[2] == (task_ids[2], 9_000, False)


async def test_negative_delta_compaction(
    db_connection: aiosqlite.Connection,
    sample_hook_event: Callable[..., HookEventRequest],
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """A negative delta should be flagged as compaction."""
    sessions: dict[str, SessionState] = {}
    sid = "sess-D"

    # Baseline at 50000 tokens.
    snap1 = sample_snapshot(
        session_id=sid, total_output_tokens=50_000, cache_creation_input_tokens=0
    )
    snap1_id = await _insert_snapshot_for(db_connection, snap1)
    await on_snapshot(sessions, db_connection, snap1, snap1_id)

    # Tool call.
    ev = sample_hook_event(session_id=sid, tool_name="Bash")
    eid = await _insert_event_for(db_connection, ev)
    await on_tool_use(sessions, db_connection, ev, eid)

    # Snapshot with LOWER tokens (compaction happened).
    snap2 = sample_snapshot(
        session_id=sid, total_output_tokens=20_000, cache_creation_input_tokens=0
    )
    snap2_id = await _insert_snapshot_for(db_connection, snap2)
    results = await on_snapshot(sessions, db_connection, snap2, snap2_id)

    assert len(results) == 1
    _, delta, is_compaction = results[0]
    assert delta == -30_000
    assert is_compaction is True


async def test_zero_delta(
    db_connection: aiosqlite.Connection,
    sample_hook_event: Callable[..., HookEventRequest],
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """A zero delta is valid and should be stored as 0 (not None)."""
    sessions: dict[str, SessionState] = {}
    sid = "sess-E"

    # Baseline.
    snap1 = sample_snapshot(
        session_id=sid, total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap1_id = await _insert_snapshot_for(db_connection, snap1)
    await on_snapshot(sessions, db_connection, snap1, snap1_id)

    # Tool call.
    ev = sample_hook_event(session_id=sid, tool_name="Read")
    eid = await _insert_event_for(db_connection, ev)
    await on_tool_use(sessions, db_connection, ev, eid)

    # Snapshot with same token count.
    snap2 = sample_snapshot(
        session_id=sid, total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap2_id = await _insert_snapshot_for(db_connection, snap2)
    results = await on_snapshot(sessions, db_connection, snap2, snap2_id)

    assert len(results) == 1
    _, delta, is_compaction = results[0]
    assert delta == 0
    assert is_compaction is False


async def test_snapshot_no_pending_tools(
    db_connection: aiosqlite.Connection,
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """A snapshot with no pending tool calls should produce no task updates."""
    sessions: dict[str, SessionState] = {}
    sid = "sess-F"

    # Baseline.
    snap1 = sample_snapshot(
        session_id=sid, total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap1_id = await _insert_snapshot_for(db_connection, snap1)
    await on_snapshot(sessions, db_connection, snap1, snap1_id)

    # Second snapshot with NO tool calls in between.
    snap2 = sample_snapshot(
        session_id=sid, total_output_tokens=12_000, cache_creation_input_tokens=0
    )
    snap2_id = await _insert_snapshot_for(db_connection, snap2)
    results = await on_snapshot(sessions, db_connection, snap2, snap2_id)

    assert results == []
    # Session state should still be updated.
    assert sessions[sid].last_snapshot_total_input == 12_000


# ---------------------------------------------------------------------------
# Session isolation tests
# ---------------------------------------------------------------------------


async def test_session_isolation(
    db_connection: aiosqlite.Connection,
    sample_hook_event: Callable[..., HookEventRequest],
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """Two sessions should not interfere with each other's delta computation."""
    sessions: dict[str, SessionState] = {}

    # Session A baseline.
    snap_a1 = sample_snapshot(
        session_id="A", total_output_tokens=10_000, cache_creation_input_tokens=0
    )
    snap_a1_id = await _insert_snapshot_for(db_connection, snap_a1)
    await on_snapshot(sessions, db_connection, snap_a1, snap_a1_id)

    # Session B baseline.
    snap_b1 = sample_snapshot(
        session_id="B", total_output_tokens=20_000, cache_creation_input_tokens=0
    )
    snap_b1_id = await _insert_snapshot_for(db_connection, snap_b1)
    await on_snapshot(sessions, db_connection, snap_b1, snap_b1_id)

    # Tool call in session A only.
    ev_a = sample_hook_event(session_id="A", tool_name="Edit")
    ev_a_id = await _insert_event_for(db_connection, ev_a)
    task_a_id = await on_tool_use(sessions, db_connection, ev_a, ev_a_id)

    # Snapshot for session A.
    snap_a2 = sample_snapshot(
        session_id="A", total_output_tokens=15_000, cache_creation_input_tokens=0
    )
    snap_a2_id = await _insert_snapshot_for(db_connection, snap_a2)
    results_a = await on_snapshot(sessions, db_connection, snap_a2, snap_a2_id)

    # Snapshot for session B (no tool calls).
    snap_b2 = sample_snapshot(
        session_id="B", total_output_tokens=25_000, cache_creation_input_tokens=0
    )
    snap_b2_id = await _insert_snapshot_for(db_connection, snap_b2)
    results_b = await on_snapshot(sessions, db_connection, snap_b2, snap_b2_id)

    # Session A should have a delta.
    assert len(results_a) == 1
    assert results_a[0][0] == task_a_id
    assert results_a[0][1] == 5_000

    # Session B should have NO pending tool results.
    assert results_b == []

    # Both sessions track independently.
    assert sessions["A"].last_snapshot_total_input == 15_000
    assert sessions["B"].last_snapshot_total_input == 25_000


# ---------------------------------------------------------------------------
# Stale session cleanup tests
# ---------------------------------------------------------------------------


async def test_stale_session_cleanup() -> None:
    """Sessions idle longer than max_idle_ms should be evicted."""
    sessions: dict[str, SessionState] = {}

    # Active session: last activity is now.
    sessions["active"] = SessionState(
        session_id="active",
        last_activity_ms=_now_ms(),
    )

    # Stale session: last activity is 2 hours ago.
    sessions["stale"] = SessionState(
        session_id="stale",
        last_activity_ms=_now_ms() - 7_200_000,
    )

    # Stopped session: marked with last_activity_ms=0.
    sessions["stopped"] = SessionState(
        session_id="stopped",
        last_activity_ms=0,
    )

    evicted = await cleanup_stale_sessions(sessions, max_idle_ms=3_600_000)

    assert "stale" in evicted
    assert "stopped" in evicted
    assert "active" not in evicted

    assert "active" in sessions
    assert "stale" not in sessions
    assert "stopped" not in sessions


async def test_on_session_stop() -> None:
    """on_session_stop should mark the session's last_activity_ms to 0."""
    sessions: dict[str, SessionState] = {}
    sessions["s1"] = SessionState(
        session_id="s1",
        last_activity_ms=_now_ms(),
    )

    await on_session_stop(sessions, "s1")

    assert sessions["s1"].last_activity_ms == 0


async def test_on_session_stop_unknown_session() -> None:
    """on_session_stop for an unknown session should not raise."""
    sessions: dict[str, SessionState] = {}

    # Should not raise.
    await on_session_stop(sessions, "nonexistent")

    assert "nonexistent" not in sessions


# ---------------------------------------------------------------------------
# Session restore from DB tests
# ---------------------------------------------------------------------------


async def test_session_restore_from_db(
    db_connection: aiosqlite.Connection,
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """restore_sessions_from_db should reconstruct SessionState from recent DB data."""
    now_ms = _now_ms()
    sid = "restore-test"

    # Insert a recent event so the session is considered active.
    await db_events.insert_event(
        db_connection,
        session_id=sid,
        event_type="PostToolUse",
        timestamp_ms=now_ms,
        payload_json="{}",
    )

    # Insert a snapshot.
    snap = sample_snapshot(
        session_id=sid,
        timestamp_ms=now_ms,
        total_output_tokens=42_000,
        cache_creation_input_tokens=0,
    )
    await _insert_snapshot_for(db_connection, snap)

    sessions: dict[str, SessionState] = {}
    restored_count = await restore_sessions_from_db(sessions, db_connection, lookback_ms=60_000)

    assert restored_count == 1
    assert sid in sessions
    assert sessions[sid].has_initial_snapshot is True
    assert sessions[sid].last_snapshot_total_input == 42_000


async def test_session_restore_skips_sessions_without_snapshots(
    db_connection: aiosqlite.Connection,
) -> None:
    """Sessions with events but no snapshots should not be restored."""
    now_ms = _now_ms()

    await db_events.insert_event(
        db_connection,
        session_id="no-snap-sess",
        event_type="PostToolUse",
        timestamp_ms=now_ms,
        payload_json="{}",
    )

    sessions: dict[str, SessionState] = {}
    restored_count = await restore_sessions_from_db(sessions, db_connection, lookback_ms=60_000)

    assert restored_count == 0
    assert "no-snap-sess" not in sessions


async def test_session_restore_does_not_overwrite_existing(
    db_connection: aiosqlite.Connection,
    sample_snapshot: Callable[..., StatuslineSnapshotRequest],
) -> None:
    """restore_sessions_from_db should not overwrite an already-tracked session."""
    now_ms = _now_ms()
    sid = "already-tracked"

    # Insert event + snapshot so the session would be eligible for restore.
    await db_events.insert_event(
        db_connection,
        session_id=sid,
        event_type="PostToolUse",
        timestamp_ms=now_ms,
        payload_json="{}",
    )
    snap = sample_snapshot(
        session_id=sid,
        timestamp_ms=now_ms,
        total_output_tokens=99_000,
        cache_creation_input_tokens=0,
    )
    await _insert_snapshot_for(db_connection, snap)

    # Pre-populate sessions dict.
    existing = SessionState(
        session_id=sid,
        last_snapshot_total_input=1_000,
        has_initial_snapshot=True,
    )
    sessions: dict[str, SessionState] = {sid: existing}

    restored_count = await restore_sessions_from_db(sessions, db_connection, lookback_ms=60_000)

    # Should not overwrite.
    assert restored_count == 0
    assert sessions[sid].last_snapshot_total_input == 1_000
