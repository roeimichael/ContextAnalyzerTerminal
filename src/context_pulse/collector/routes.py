"""All HTTP route handlers for the context-pulse collector."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, Request

from context_pulse.collector import delta_engine
from context_pulse.collector.models import (
    AnomaliesListResponse,
    AnomalyResponse,
    BaselineSnapshot,
    EventResponse,
    HealthResponse,
    HookEventRequest,
    SessionSummary,
    SnapshotResponse,
    StatuslineSnapshotRequest,
    StatusResponse,
    TaskResponse,
)
from context_pulse.db import anomalies as db_anomalies
from context_pulse.db import baselines as db_baselines
from context_pulse.db import events as db_events
from context_pulse.db import tasks as db_tasks

logger = logging.getLogger(__name__)

hook_router = APIRouter()
api_router = APIRouter()

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_db(request: Request) -> aiosqlite.Connection:
    """Dependency: returns app.state.db."""
    return request.app.state.db


async def get_sessions(request: Request) -> dict[str, Any]:
    """Dependency: returns app.state.sessions."""
    return request.app.state.sessions


async def get_config(request: Request) -> Any:
    """Dependency: returns app.state.config."""
    return request.app.state.config


# ---------------------------------------------------------------------------
# Session title resolution
# ---------------------------------------------------------------------------


def _resolve_session_title(transcript_path: str, session_id: str) -> str | None:
    """Read sessions-index.json next to *transcript_path* and return the title.

    Priority: customTitle > summary > first 60 chars of firstPrompt.
    Returns ``None`` if nothing found.
    """
    try:
        index_path = os.path.join(os.path.dirname(transcript_path), "sessions-index.json")
        if not os.path.isfile(index_path):
            return None
        with open(index_path, encoding="utf-8") as f:
            raw: list[Any] = json.load(f)
        entries: list[dict[str, str]] = [e for e in raw if isinstance(e, dict)]
        for entry in entries:
            if entry.get("sessionId") == session_id:
                title = entry.get("customTitle") or entry.get("summary") or None
                if title:
                    return title
                first_prompt = entry.get("firstPrompt", "")
                if first_prompt:
                    return first_prompt[:60].strip()
                return None
    except Exception:
        logger.debug("Failed to resolve session title", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Hook routes -- must NEVER return 500
# ---------------------------------------------------------------------------


@hook_router.post("/event", status_code=202)
async def receive_hook_event(
    event: HookEventRequest,
    db: aiosqlite.Connection = Depends(get_db),
    sessions: dict[str, Any] = Depends(get_sessions),
) -> dict[str, str]:
    """Receive a hook event from any hook script.

    Dispatches to the appropriate handler based on *event_type*.
    Always returns 202 -- errors are logged but never surfaced to hooks.
    """
    try:
        payload_json = json.dumps(event.payload)

        event_id = await db_events.insert_event(
            db,
            session_id=event.session_id,
            event_type=event.event_type,
            timestamp_ms=event.timestamp_ms,
            payload_json=payload_json,
            tool_name=event.tool_name,
            tool_input_summary=event.tool_input_summary,
            agent_id=event.agent_id,
            cwd=event.cwd,
        )

        if event.event_type == "PostToolUse":
            await delta_engine.on_tool_use(sessions, db, event, event_id)

        elif event.event_type == "SubagentStop":
            agent_type = event.agent_type or "unknown"
            task_type = f"SubagentStop:{agent_type}"
            await db_tasks.insert_task(
                db,
                session_id=event.session_id,
                event_id=event_id,
                task_type=task_type,
                timestamp_ms=event.timestamp_ms,
            )

        elif event.event_type == "Stop":
            await delta_engine.on_session_stop(sessions, event.session_id)

        elif event.event_type == "UserPromptSubmit":
            pass  # Event already inserted; no further processing needed.

        elif event.event_type == "SessionStart":
            logger.info("New session started: %s", event.session_id)

        else:
            logger.warning(
                "Unknown event_type=%r for session=%s", event.event_type, event.session_id
            )

        # Resolve session title lazily (once per session)
        session = sessions.get(event.session_id)
        if session is not None and session.session_title is None:
            transcript_path = event.payload.get("transcript_path")
            if transcript_path:
                session.transcript_path = transcript_path
                session.session_title = await asyncio.to_thread(
                    _resolve_session_title, transcript_path, event.session_id
                )

    except Exception:
        logger.exception(
            "Error processing hook event (type=%s, session=%s)",
            event.event_type,
            event.session_id,
        )

    return {"status": "accepted"}


@hook_router.post("/statusline", status_code=202)
async def receive_statusline_snapshot(
    snapshot: StatuslineSnapshotRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    sessions: dict[str, Any] = Depends(get_sessions),
    config: Any = Depends(get_config),
) -> dict[str, str]:
    """Receive a statusline snapshot.

    Inserts the snapshot into the DB, delegates to the delta engine,
    and triggers anomaly detection for any resolved deltas.
    Always returns 202.
    """
    try:
        snapshot_id = await db_events.insert_snapshot(
            db,
            session_id=snapshot.session_id,
            timestamp_ms=snapshot.timestamp_ms,
            total_input_tokens=snapshot.total_input_tokens,
            total_output_tokens=snapshot.total_output_tokens,
            cache_creation_input_tokens=snapshot.cache_creation_input_tokens,
            cache_read_input_tokens=snapshot.cache_read_input_tokens,
            context_window_size=snapshot.context_window_size,
            used_percentage=snapshot.used_percentage,
            total_cost_usd=snapshot.total_cost_usd,
            model_id=snapshot.model_id,
        )

        # Capture pending list BEFORE on_snapshot clears it
        session = sessions.get(snapshot.session_id)
        pending_list = list(session.pending_tool_calls) if session else []

        results = await delta_engine.on_snapshot(sessions, db, snapshot, snapshot_id)

        # Trigger anomaly detection for resolved deltas (Phase 2)
        if results and pending_list:
            baseline_manager = getattr(request.app.state, "baseline_manager", None)
            if baseline_manager is not None:
                await delta_engine.process_anomalies(
                    db=db,
                    baseline_manager=baseline_manager,
                    anomaly_config=config.anomaly,
                    classifier_config=config.classifier,
                    notifications_config=config.notifications,
                    results=results,
                    session_id=snapshot.session_id,
                    pending_list=pending_list,
                )

        # Check context thresholds and queue warnings
        try:
            from context_pulse.notify.context_warnings import check_context_thresholds

            await check_context_thresholds(
                db=db,
                session_id=snapshot.session_id,
                used_percentage=snapshot.used_percentage,
                context_window_size=snapshot.context_window_size,
            )
        except Exception:
            logger.debug("Context threshold check failed", exc_info=True)

    except Exception:
        logger.exception(
            "Error processing statusline snapshot (session=%s)",
            snapshot.session_id,
        )

    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@api_router.get("/status", response_model=StatusResponse)
async def get_status(
    db: aiosqlite.Connection = Depends(get_db),
    sessions: dict[str, Any] = Depends(get_sessions),
) -> StatusResponse:
    """Return active sessions, last 20 events, and last 20 tasks.

    Used by ``context-pulse status`` CLI command.
    """
    # Active sessions: those with events in the last 5 minutes.
    active_window_ms = 5 * 60 * 1000
    since_ms = int(time.time() * 1000) - active_window_ms
    active_ids = await db_events.get_active_session_ids(db, since_ms=since_ms)

    session_summaries: list[SessionSummary] = []
    for sid in active_ids:
        # Query event count and first/last timestamps for this session.
        cursor = await db.execute(
            "SELECT COUNT(*) AS cnt, MIN(timestamp_ms) AS first_ms, MAX(timestamp_ms) AS last_ms "
            "FROM events WHERE session_id = ?",
            (sid,),
        )
        row = await cursor.fetchone()
        event_count: int = row["cnt"] if row else 0  # type: ignore[index]
        first_event_ms: int = row["first_ms"] if row else 0  # type: ignore[index]
        last_event_ms: int = row["last_ms"] if row else 0  # type: ignore[index]

        # Get latest snapshot for context % and model info.
        latest_snap = await db_events.get_latest_snapshot(db, sid)
        total_tokens_used: int | None = None
        used_percentage: int | None = None
        model_id: str | None = None
        if latest_snap is not None:
            used_percentage = latest_snap.get("used_percentage")
            model_id = latest_snap.get("model_id")

        # Build project display name: {folder} — {session title}
        cwd_cursor = await db.execute(
            "SELECT cwd FROM events WHERE session_id = ? AND cwd IS NOT NULL "
            "ORDER BY timestamp_ms DESC LIMIT 1",
            (sid,),
        )
        cwd_row = await cwd_cursor.fetchone()
        folder_name: str | None = None
        if cwd_row and cwd_row[0]:
            cwd_path = str(cwd_row[0]).replace("\\", "/")
            parts = [p for p in cwd_path.split("/") if p]
            if parts:
                folder_name = parts[-1]

        # Get session title from in-memory state or first user prompt
        session = sessions.get(sid)
        session_title: str | None = None
        if session is not None:
            if session.session_title is None and session.transcript_path:
                session.session_title = await asyncio.to_thread(
                    _resolve_session_title, session.transcript_path, sid
                )
            session_title = session.session_title

        # Fall back to first user prompt for this session
        if session_title is None:
            prompt_cursor = await db.execute(
                "SELECT payload_json FROM events "
                "WHERE session_id = ? AND event_type = 'UserPromptSubmit' "
                "ORDER BY timestamp_ms ASC LIMIT 1",
                (sid,),
            )
            prompt_row = await prompt_cursor.fetchone()
            if prompt_row and prompt_row[0]:
                try:
                    prompt_payload = json.loads(prompt_row[0])
                    first_prompt = (prompt_payload.get("prompt") or "").strip()
                    # Take first line only, truncate to 20 chars
                    first_line = first_prompt.split("\n")[0][:20].strip()
                    if first_line:
                        if len(first_line) == 20:
                            first_line = first_line[:17] + "..."
                        session_title = first_line
                        if session is not None:
                            session.session_title = session_title
                except Exception:
                    pass

        if folder_name and session_title:
            project_name = f"{folder_name} — {session_title}"
        elif folder_name:
            project_name = folder_name
        else:
            project_name = session_title

        # Sum estimated_tokens from all tasks for this session.
        # This is consistent with what the task cost timeline displays.
        tok_cursor = await db.execute(
            "SELECT SUM(estimated_tokens) FROM tasks "
            "WHERE session_id = ? AND estimated_tokens IS NOT NULL",
            (sid,),
        )
        tok_row = await tok_cursor.fetchone()
        if tok_row and tok_row[0]:
            total_tokens_used = int(tok_row[0])

        session_summaries.append(
            SessionSummary(
                session_id=sid,
                project_name=project_name,
                event_count=event_count,
                first_event_ms=first_event_ms,
                last_event_ms=last_event_ms,
                total_tokens_used=total_tokens_used,
                used_percentage=used_percentage,
                model_id=model_id,
            )
        )

    # Recent events and tasks.
    recent_event_rows = await db_events.get_recent_events(db, limit=20)
    recent_events = [
        EventResponse(
            id=r["id"],
            session_id=r["session_id"],
            agent_id=r.get("agent_id"),
            event_type=r["event_type"],
            tool_name=r.get("tool_name"),
            tool_input_summary=r.get("tool_input_summary"),
            cwd=r.get("cwd"),
            timestamp_ms=r["timestamp_ms"],
        )
        for r in recent_event_rows
    ]

    recent_task_rows = await db_tasks.get_recent_tasks(db, limit=50)
    recent_tasks = [
        TaskResponse(
            id=r["id"],
            session_id=r["session_id"],
            task_type=r["task_type"],
            token_delta=r.get("token_delta"),
            estimated_tokens=r.get("estimated_tokens"),
            is_compaction=bool(r.get("is_compaction", False)),
            timestamp_ms=r["timestamp_ms"],
            anomaly_id=r.get("anomaly_id"),
        )
        for r in recent_task_rows
    ]

    return StatusResponse(
        active_sessions=session_summaries,
        recent_events=recent_events,
        recent_tasks=recent_tasks,
    )


@api_router.get("/health", response_model=HealthResponse)
async def get_health(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> HealthResponse:
    """Return server health: uptime, db path, event/snapshot counts.

    Used by install script and CLI to verify collector is running.
    """
    uptime_seconds = time.time() - request.app.state.start_time
    db_path = str(request.app.state.config.collector.db_path)
    event_count = await db_events.get_event_count(db)
    snapshot_count = await db_events.get_snapshot_count(db)

    return HealthResponse(
        status="ok",
        uptime_seconds=uptime_seconds,
        db_path=db_path,
        event_count=event_count,
        snapshot_count=snapshot_count,
    )


@api_router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def get_session_events(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
) -> list[EventResponse]:
    """Return events for a specific session, ordered by timestamp desc."""
    cursor = await db.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    )
    rows = await cursor.fetchall()

    return [
        EventResponse(
            id=r["id"],
            session_id=r["session_id"],
            agent_id=r["agent_id"],
            event_type=r["event_type"],
            tool_name=r["tool_name"],
            tool_input_summary=r["tool_input_summary"],
            cwd=r["cwd"],
            timestamp_ms=r["timestamp_ms"],
        )
        for r in rows
    ]


@api_router.get("/sessions/{session_id}/tasks", response_model=list[TaskResponse])
async def get_session_tasks(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TaskResponse]:
    """Return tasks (with deltas) for a specific session, ordered by timestamp desc."""
    cursor = await db.execute(
        "SELECT * FROM tasks WHERE session_id = ? ORDER BY timestamp_ms DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    )
    rows = await cursor.fetchall()

    return [
        TaskResponse(
            id=r["id"],
            session_id=r["session_id"],
            task_type=r["task_type"],
            token_delta=r["token_delta"],
            estimated_tokens=dict(r).get("estimated_tokens"),
            is_compaction=bool(r["is_compaction"]),
            timestamp_ms=r["timestamp_ms"],
            anomaly_id=r["anomaly_id"],
        )
        for r in rows
    ]


@api_router.get("/sessions/{session_id}/snapshots", response_model=list[SnapshotResponse])
async def get_session_snapshots(
    session_id: str,
    limit: int = 50,
    db: aiosqlite.Connection = Depends(get_db),
) -> list[SnapshotResponse]:
    """Return token snapshots for a specific session, ordered by timestamp desc."""
    snap_rows = await db_events.get_recent_snapshots(db, session_id=session_id, limit=limit)

    return [
        SnapshotResponse(
            id=r["id"],
            session_id=r["session_id"],
            timestamp_ms=r["timestamp_ms"],
            total_input_tokens=r["total_input_tokens"],
            total_output_tokens=r["total_output_tokens"],
            cache_creation_input_tokens=r.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=r.get("cache_read_input_tokens", 0),
            context_window_size=r.get("context_window_size", 0),
            used_percentage=r["used_percentage"],
            model_id=r["model_id"],
        )
        for r in snap_rows
    ]


@api_router.get("/anomalies", response_model=AnomaliesListResponse)
async def get_anomalies(
    limit: int = 20,
    session_id: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
) -> AnomaliesListResponse:
    """Return recent anomalies with optional session filter."""
    rows = await db_anomalies.get_recent_anomalies(db, limit=limit, session_id=session_id)
    total = await db_anomalies.get_anomaly_count(db)

    anomalies = [
        AnomalyResponse(
            id=r["id"],
            session_id=r["session_id"],
            task_type=r["task_type"],
            token_cost=r["token_cost"],
            z_score=r["z_score"],
            cause=r.get("cause"),
            severity=r.get("severity"),
            suggestion=r.get("suggestion"),
            notified=bool(r.get("notified", 0)),
            timestamp_ms=r["timestamp_ms"],
        )
        for r in rows
    ]
    return AnomaliesListResponse(anomalies=anomalies, total_count=total)


@api_router.get("/rtk-status")
async def get_rtk_status() -> dict[str, Any]:
    """Return RTK integration status and savings."""
    from context_pulse.rtk_integration import (
        get_rtk_savings_summary,
        get_rtk_version,
        is_rtk_hooks_installed,
        is_rtk_installed,
    )
    installed, version, hooks_installed, savings_24h = await asyncio.gather(
        asyncio.to_thread(is_rtk_installed),
        asyncio.to_thread(get_rtk_version),
        asyncio.to_thread(is_rtk_hooks_installed),
        asyncio.to_thread(get_rtk_savings_summary, 24),
    )
    return {
        "installed": installed,
        "version": version,
        "hooks_installed": hooks_installed,
        "savings_24h": savings_24h,
    }


@api_router.get("/baselines", response_model=list[BaselineSnapshot])
async def get_baselines(
    db: aiosqlite.Connection = Depends(get_db),
) -> list[BaselineSnapshot]:
    """Return all baseline snapshots."""
    rows = await db_baselines.get_all_baselines(db)
    return [
        BaselineSnapshot(
            task_type=r["task_type"],
            mean=r["mean"],
            stddev=r["stddev"],
            sample_count=r["sample_count"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


@api_router.get(
    "/sessions/{session_id}/latest-anomaly",
    response_model=AnomalyResponse | None,
)
async def get_latest_anomaly(
    session_id: str,
    max_age_ms: int = 60000,
    db: aiosqlite.Connection = Depends(get_db),
) -> AnomalyResponse | None:
    """Return the most recent anomaly for a session within max_age_ms.

    Used by hook scripts to check if there's a fresh anomaly to display
    in the statusline or inject as additionalContext.
    """
    since_ms = int(time.time() * 1000) - max_age_ms
    rows = await db_anomalies.get_recent_anomalies(db, limit=1, session_id=session_id)
    if not rows:
        return None
    r = rows[0]
    if r["timestamp_ms"] < since_ms:
        return None
    return AnomalyResponse(
        id=r["id"],
        session_id=r["session_id"],
        task_type=r["task_type"],
        token_cost=r["token_cost"],
        z_score=r["z_score"],
        cause=r.get("cause"),
        severity=r.get("severity"),
        suggestion=r.get("suggestion"),
        notified=bool(r.get("notified", 0)),
        timestamp_ms=r["timestamp_ms"],
    )


@api_router.post("/anomalies/{anomaly_id}/mark-notified", status_code=200)
async def mark_anomaly_notified(
    anomaly_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, str]:
    """Mark an anomaly as notified so it won't be re-injected."""
    await db.execute(
        "UPDATE anomalies SET notified = 1 WHERE id = ?", (anomaly_id,)
    )
    await db.commit()
    return {"status": "ok"}


@api_router.get("/sessions/{session_id}/pending-messages")
async def get_pending_messages(
    session_id: str,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Consume and return pending messages for a session.

    Messages are marked as consumed after being read, so each
    message is returned exactly once. Used by the PostToolUse hook
    to inject additionalContext.
    """
    try:
        from context_pulse.db import messages as db_messages

        msgs = await db_messages.consume_messages(db, session_id)
        return {"messages": msgs}
    except Exception:
        logger.debug("Failed to consume pending messages", exc_info=True)
        return {"messages": []}


@api_router.get("/sessions/{session_id}/context-breakdown")
async def get_context_breakdown(
    session_id: str,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Return a context cost breakdown for a session.

    Shows fixed costs vs. conversation history overhead.
    """
    from context_pulse.engine.context_breakdown import compute_breakdown

    snap = await db_events.get_latest_snapshot(db, session_id)
    if snap is None:
        return {"error": "No snapshot data for this session"}

    return compute_breakdown(
        total_input_tokens=snap.get("total_input_tokens", 0),
        total_output_tokens=snap.get("total_output_tokens", 0),
        cache_creation_input_tokens=snap.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=snap.get("cache_read_input_tokens", 0),
        context_window_size=snap.get("context_window_size", 0),
        used_percentage=snap.get("used_percentage", 0),
    )
