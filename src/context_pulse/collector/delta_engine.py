"""In-memory correlation engine pairing tool calls with token snapshots."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from context_pulse.collector.models import HookEventRequest, StatuslineSnapshotRequest
from context_pulse.db import events as db_events
from context_pulse.db import tasks as db_tasks

logger = logging.getLogger("context_pulse.delta_engine")


@dataclass
class PendingToolCall:
    """A PostToolUse event waiting for a snapshot to compute its delta."""

    event_id: int
    task_id: int
    task_type: str
    timestamp_ms: int
    tool_input_summary: str | None = None
    estimated_tokens: int | None = None


@dataclass
class SessionState:
    """In-memory state for one session's delta computation."""

    session_id: str
    last_snapshot_id: int | None = None
    last_snapshot_total_input: int | None = None
    last_snapshot_timestamp_ms: int | None = None
    pending_tool_calls: deque[PendingToolCall] = field(
        default_factory=lambda: deque[PendingToolCall]()
    )
    has_initial_snapshot: bool = False
    last_activity_ms: int = 0
    session_title: str | None = None
    transcript_path: str | None = None


def _now_ms() -> int:
    """Return the current time as Unix-epoch milliseconds."""
    return int(time.time() * 1000)


def _effective_context_tokens(snapshot: StatuslineSnapshotRequest) -> int:
    """Compute the effective context token count from a snapshot.

    Uses output_tokens + cache_creation_input_tokens as the best measure
    of per-turn context cost. The total_input_tokens field is a cumulative
    counter that barely changes between turns in long sessions.
    """
    return snapshot.total_output_tokens + snapshot.cache_creation_input_tokens


def _get_or_create_session(
    sessions: dict[str, SessionState],
    session_id: str,
) -> SessionState:
    """Look up an existing SessionState or create a fresh one."""
    session = sessions.get(session_id)
    if session is None:
        session = SessionState(session_id=session_id)
        sessions[session_id] = session
        logger.debug("Created new SessionState for session=%s", session_id)
    return session


async def on_tool_use(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    event: HookEventRequest,
    event_id: int,
) -> int:
    """Handle a PostToolUse event.

    1. Get or create ``SessionState`` for the session.
    2. Insert a task row with ``token_delta=NULL`` and
       ``snapshot_before_id`` pointing to the last known snapshot.
    3. Append a ``PendingToolCall`` so the next snapshot can fill the delta.
    4. Update ``last_activity_ms``.
    5. Return the new ``task_id``.
    """
    session = _get_or_create_session(sessions, event.session_id)

    task_type: str = event.tool_name if event.tool_name is not None else "Unknown"

    task_id = await db_tasks.insert_task(
        db,
        session_id=event.session_id,
        event_id=event_id,
        task_type=task_type,
        timestamp_ms=event.timestamp_ms,
        snapshot_before_id=session.last_snapshot_id,
        token_delta=None,
        is_compaction=False,
        estimated_tokens=event.estimated_tokens,
    )

    session.pending_tool_calls.append(
        PendingToolCall(
            event_id=event_id,
            task_id=task_id,
            task_type=task_type,
            timestamp_ms=event.timestamp_ms,
            tool_input_summary=event.tool_input_summary,
            estimated_tokens=event.estimated_tokens,
        )
    )

    session.last_activity_ms = _now_ms()

    logger.debug(
        "on_tool_use: session=%s task_id=%d task_type=%s pending=%d",
        event.session_id,
        task_id,
        task_type,
        len(session.pending_tool_calls),
    )

    return task_id


async def on_snapshot(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    snapshot: StatuslineSnapshotRequest,
    snapshot_id: int,
) -> list[tuple[int, int | None, bool]]:
    """Handle a statusline snapshot.

    1. Get or create ``SessionState``.
    2. If this is the first snapshot for the session, record it as the
       baseline, discard any pending tool calls (no delta is possible),
       and return an empty list.
    3. Compute ``raw_delta = total_input_tokens - last_snapshot_total_input``.
    4. Detect compaction (``raw_delta < 0``).
    5. Assign delta to pending tool calls: the **last** pending call
       receives the full delta; earlier ones receive ``0``.
    6. UPDATE each task row in the DB.
    7. Clear ``pending_tool_calls``.
    8. Update ``last_snapshot_*`` fields on the session.
    9. Return ``[(task_id, token_delta, is_compaction), ...]``.
    """
    session = _get_or_create_session(sessions, snapshot.session_id)

    # ---- First snapshot: establish baseline ----
    current_effective = _effective_context_tokens(snapshot)

    if not session.has_initial_snapshot:
        session.last_snapshot_id = snapshot_id
        session.last_snapshot_total_input = current_effective
        session.last_snapshot_timestamp_ms = snapshot.timestamp_ms
        session.has_initial_snapshot = True
        session.last_activity_ms = _now_ms()

        if session.pending_tool_calls:
            logger.info(
                "Initial snapshot for session=%s, discarding %d pending tool calls",
                snapshot.session_id,
                len(session.pending_tool_calls),
            )
            session.pending_tool_calls.clear()

        logger.debug(
            "Initial snapshot for session=%s: snapshot_id=%d effective=%d",
            snapshot.session_id,
            snapshot_id,
            current_effective,
        )
        return []

    # ---- Compute delta ----
    previous_total = session.last_snapshot_total_input
    if previous_total is None:
        session.last_snapshot_id = snapshot_id
        session.last_snapshot_total_input = current_effective
        session.last_snapshot_timestamp_ms = snapshot.timestamp_ms
        session.last_activity_ms = _now_ms()
        session.pending_tool_calls.clear()
        return []

    raw_delta: int = current_effective - previous_total
    is_compaction: bool = raw_delta < 0

    if is_compaction:
        logger.info(
            "Compaction detected, session=%s, delta=%d",
            snapshot.session_id,
            raw_delta,
        )

    # ---- Assign delta to pending tool calls ----
    results: list[tuple[int, int | None, bool]] = []

    if session.pending_tool_calls:
        pending_list = list(session.pending_tool_calls)
        num_pending = len(pending_list)

        for idx, ptc in enumerate(pending_list):
            if idx == num_pending - 1:
                # Last pending tool call gets the full delta
                delta: int | None = raw_delta
            else:
                # Earlier tool calls get 0
                delta = 0

            await db_tasks.update_task_delta(
                db,
                task_id=ptc.task_id,
                token_delta=delta,
                snapshot_after_id=snapshot_id,
                is_compaction=is_compaction,
            )

            results.append((ptc.task_id, delta, is_compaction))

            logger.debug(
                "Assigned delta=%s to task_id=%d (type=%s) compaction=%s",
                delta,
                ptc.task_id,
                ptc.task_type,
                is_compaction,
            )

        session.pending_tool_calls.clear()

    # ---- Update session state ----
    session.last_snapshot_id = snapshot_id
    session.last_snapshot_total_input = current_effective
    session.last_snapshot_timestamp_ms = snapshot.timestamp_ms
    session.last_activity_ms = _now_ms()

    logger.debug(
        "on_snapshot: session=%s snapshot_id=%d raw_delta=%d "
        "tasks_updated=%d compaction=%s",
        snapshot.session_id,
        snapshot_id,
        raw_delta,
        len(results),
        is_compaction,
    )

    return results


async def on_session_stop(
    sessions: dict[str, SessionState],
    session_id: str,
) -> None:
    """Handle a Stop event for a session.

    Marks the session for eventual cleanup by setting
    ``last_activity_ms = 0``.  This does **not** immediately evict the
    session -- a final snapshot may still arrive shortly after the stop
    event.  The periodic :func:`cleanup_stale_sessions` call will remove
    it once the idle threshold has elapsed.
    """
    session = sessions.get(session_id)
    if session is not None:
        session.last_activity_ms = 0
        logger.info(
            "Session stop received for session=%s; marked for cleanup",
            session_id,
        )
    else:
        logger.debug(
            "on_session_stop called for unknown session=%s; nothing to do",
            session_id,
        )


async def cleanup_stale_sessions(
    sessions: dict[str, SessionState],
    max_idle_ms: int = 3_600_000,
) -> list[str]:
    """Evict sessions that have been idle longer than *max_idle_ms*.

    A session is considered idle when ``now - last_activity_ms > max_idle_ms``.
    Sessions whose ``last_activity_ms`` is ``0`` (marked by
    :func:`on_session_stop`) are always evicted.

    This function is intended to be called periodically (e.g. every 60 s)
    by a background task.

    Returns the list of evicted session IDs.
    """
    now = _now_ms()
    evicted: list[str] = []

    # Build list first to avoid mutating dict during iteration
    for sid, session in list(sessions.items()):
        idle_time = now - session.last_activity_ms
        if session.last_activity_ms == 0 or idle_time > max_idle_ms:
            evicted.append(sid)

    for sid in evicted:
        del sessions[sid]

    if evicted:
        logger.info(
            "Evicted %d stale session(s): %s",
            len(evicted),
            ", ".join(evicted),
        )

    return evicted


async def restore_sessions_from_db(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    lookback_ms: int = 1_800_000,
) -> int:
    """Reconstruct ``SessionState`` for recently active sessions from the DB.

    Called on collector startup to recover from a restart.  For each
    session that has events within the last *lookback_ms* milliseconds:

    * Load the latest ``token_snapshot`` to populate
      ``last_snapshot_id``, ``last_snapshot_total_input``, and
      ``last_snapshot_timestamp_ms``.
    * Set ``has_initial_snapshot = True`` (since a snapshot exists in DB).
    * Pending tool calls from before the crash are **lost** -- their
      task rows keep ``token_delta = NULL``.

    Returns the number of sessions restored.
    """
    now = _now_ms()
    since_ms = now - lookback_ms

    active_session_ids = await db_events.get_active_session_ids(db, since_ms)

    restored = 0
    for sid in active_session_ids:
        if sid in sessions:
            # Already tracked (shouldn't happen on cold start, but be safe)
            logger.debug("Session %s already in memory; skipping restore", sid)
            continue

        latest_snap = await db_events.get_latest_snapshot(db, sid)
        if latest_snap is None:
            # Session has events but no snapshots yet -- nothing to restore
            logger.debug(
                "Session %s has events but no snapshots; skipping restore",
                sid,
            )
            continue

        snap_id = int(latest_snap["id"])
        snap_effective = (
            int(latest_snap["total_output_tokens"])
            + int(latest_snap.get("cache_creation_input_tokens", 0))
        )
        snap_ts = int(latest_snap["timestamp_ms"])

        # Extract transcript_path from the latest event payload
        transcript_path: str | None = None
        try:
            db.row_factory = aiosqlite.Row
            tp_cursor = await db.execute(
                "SELECT payload_json FROM events WHERE session_id = ? "
                "AND payload_json LIKE '%transcript_path%' LIMIT 1",
                (sid,),
            )
            tp_row = await tp_cursor.fetchone()
            if tp_row:
                tp_payload = json.loads(tp_row["payload_json"])
                transcript_path = tp_payload.get("transcript_path")
        except Exception:
            pass

        session = SessionState(
            session_id=sid,
            last_snapshot_id=snap_id,
            last_snapshot_total_input=snap_effective,
            last_snapshot_timestamp_ms=snap_ts,
            has_initial_snapshot=True,
            last_activity_ms=snap_ts,
            transcript_path=transcript_path,
        )
        sessions[sid] = session
        restored += 1

        logger.info(
            "Restored session=%s from DB: snapshot_id=%d effective=%d",
            sid,
            snap_id,
            snap_effective,
        )

    logger.info(
        "Session restore complete: %d session(s) restored from %d active",
        restored,
        len(active_session_ids),
    )

    return restored


async def process_anomalies(
    db: aiosqlite.Connection,
    baseline_manager: object,
    anomaly_config: object,
    classifier_config: object,
    notifications_config: object | None,
    results: list[tuple[int, int | None, bool]],
    session_id: str,
    pending_list: list[PendingToolCall],
) -> list[Any]:
    """Check each resolved delta for anomalies and dispatch notifications.

    Called by the statusline route handler after ``on_snapshot()`` resolves
    deltas for pending tool calls.  Imports the anomaly detector and
    notification dispatcher lazily to avoid circular imports.

    Returns a list of ``AnomalyResult`` objects (may be empty).
    """
    from context_pulse.collector.models import AnomalyResult
    from context_pulse.config import AnomalyConfig, ClassifierConfig, NotificationsConfig
    from context_pulse.engine.anomaly import detect_anomaly
    from context_pulse.engine.baseline import BaselineManager

    if not isinstance(baseline_manager, BaselineManager):
        return []
    if not isinstance(anomaly_config, AnomalyConfig):
        return []
    if not isinstance(classifier_config, ClassifierConfig):
        return []

    anomalies: list[AnomalyResult] = []
    for (task_id, _delta, is_compaction), ptc in zip(results, pending_list, strict=False):
        if is_compaction:
            continue
        # Use estimated_tokens (hook-provided, same metric as task timeline)
        # instead of raw_delta (snapshot correlation) for consistency
        token_cost = ptc.estimated_tokens
        if token_cost is None or token_cost <= 0:
            continue
        result = await detect_anomaly(
            baseline_manager=baseline_manager,
            db=db,
            task_id=task_id,
            session_id=session_id,
            task_type=ptc.task_type,
            token_delta=token_cost,
            tool_input_summary=ptc.tool_input_summary,
            timestamp_ms=ptc.timestamp_ms,
            anomaly_config=anomaly_config,
            classifier_config=classifier_config,
        )
        if result is not None:
            anomalies.append(result)

            # Dispatch notifications for this anomaly
            if isinstance(notifications_config, NotificationsConfig):
                try:
                    from context_pulse.notify.dispatcher import (
                        dispatch_anomaly_notifications,
                    )

                    # Fetch classifier output from DB if available
                    cause: str | None = None
                    severity: str | None = None
                    suggestion: str | None = None
                    from context_pulse.db import anomalies as db_anomalies_mod

                    rows = await db_anomalies_mod.get_recent_anomalies(
                        db, limit=1, session_id=session_id,
                    )
                    if rows:
                        cause = rows[0].get("cause")
                        severity = rows[0].get("severity")
                        suggestion = rows[0].get("suggestion")

                    await dispatch_anomaly_notifications(
                        config=notifications_config,
                        task_type=ptc.task_type,
                        token_delta=token_cost,
                        z_score=result.z_score,
                        session_id=session_id,
                        baseline_mean=result.baseline_mean,
                        cause=cause,
                        severity=severity,
                        suggestion=suggestion,
                    )
                except Exception:
                    logger.exception("Failed to dispatch notifications")

    return anomalies
