"""Tests for the HTTP route handlers (hook and API routes)."""

from __future__ import annotations

import time

from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Hook route tests
# ---------------------------------------------------------------------------


async def test_post_hook_event(app_client: AsyncClient) -> None:
    """POST /hook/event with a PostToolUse event should return 202."""
    payload = {
        "event_type": "PostToolUse",
        "session_id": "sess-route-1",
        "timestamp_ms": int(time.time() * 1000),
        "payload": {
            "session_id": "sess-route-1",
            "transcript_path": "/tmp/t.json",
            "cwd": "/project",
            "permission_mode": "default",
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file": "main.py"},
            "tool_response": {"status": "ok"},
            "tool_use_id": "tu-1",
        },
        "tool_name": "Edit",
        "tool_input_summary": "Editing main.py",
        "cwd": "/project",
    }

    resp = await app_client.post("/hook/event", json=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"


async def test_post_hook_event_stop(app_client: AsyncClient) -> None:
    """POST /hook/event with a Stop event type should return 202."""
    payload = {
        "event_type": "Stop",
        "session_id": "sess-route-stop",
        "timestamp_ms": int(time.time() * 1000),
        "payload": {
            "session_id": "sess-route-stop",
            "transcript_path": "/tmp/t.json",
            "cwd": "/project",
            "hook_event_name": "Stop",
            "stop_hook_active": True,
            "last_assistant_message": "Done.",
        },
    }

    resp = await app_client.post("/hook/event", json=payload)

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


async def test_post_statusline_snapshot(app_client: AsyncClient) -> None:
    """POST /hook/statusline should accept a snapshot and return 202."""
    payload = {
        "session_id": "sess-route-2",
        "timestamp_ms": int(time.time() * 1000),
        "total_input_tokens": 10_000,
        "total_output_tokens": 2_000,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 300,
        "context_window_size": 200_000,
        "used_percentage": 5,
        "remaining_percentage": 95,
        "total_cost_usd": 0.05,
        "total_duration_ms": 5000,
        "model_id": "claude-sonnet-4-20250514",
        "model_display_name": "Claude Sonnet 4",
        "rate_limit_five_hour_pct": 10.0,
        "rate_limit_seven_day_pct": 2.0,
        "version": "1.0.0",
    }

    resp = await app_client.post("/hook/statusline", json=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


async def test_get_status(app_client: AsyncClient) -> None:
    """GET /api/status returns sessions, events, and tasks."""
    resp = await app_client.get("/api/status")

    assert resp.status_code == 200
    body = resp.json()
    assert "active_sessions" in body
    assert "recent_events" in body
    assert "recent_tasks" in body
    assert isinstance(body["active_sessions"], list)
    assert isinstance(body["recent_events"], list)
    assert isinstance(body["recent_tasks"], list)


async def test_get_health(app_client: AsyncClient) -> None:
    """GET /api/health should return status=ok with uptime and counts."""
    resp = await app_client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body
    assert body["uptime_seconds"] >= 0
    assert "db_path" in body
    assert "event_count" in body
    assert "snapshot_count" in body
    assert isinstance(body["event_count"], int)
    assert isinstance(body["snapshot_count"], int)


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


async def test_invalid_event_type(app_client: AsyncClient) -> None:
    """An unknown event_type should still return 202 (never 500)."""
    payload = {
        "event_type": "TotallyUnknownEvent",
        "session_id": "sess-unknown",
        "timestamp_ms": int(time.time() * 1000),
        "payload": {"something": "irrelevant"},
    }

    resp = await app_client.post("/hook/event", json=payload)

    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


async def test_post_event_and_check_status(app_client: AsyncClient) -> None:
    """After posting an event, /api/status should include it in recent_events."""
    ts = int(time.time() * 1000)
    payload = {
        "event_type": "UserPromptSubmit",
        "session_id": "sess-integrated",
        "timestamp_ms": ts,
        "payload": {
            "session_id": "sess-integrated",
            "transcript_path": "/tmp/t.json",
            "cwd": "/project",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Fix the bug",
        },
    }

    post_resp = await app_client.post("/hook/event", json=payload)
    assert post_resp.status_code == 202

    status_resp = await app_client.get("/api/status")
    assert status_resp.status_code == 200
    body = status_resp.json()

    # The event we just posted should appear in recent_events.
    event_types = [e["event_type"] for e in body["recent_events"]]
    assert "UserPromptSubmit" in event_types


async def test_post_snapshot_and_check_health(app_client: AsyncClient) -> None:
    """After posting a snapshot, /api/health snapshot_count should increase."""
    health_before = await app_client.get("/api/health")
    count_before = health_before.json()["snapshot_count"]

    snapshot_payload = {
        "session_id": "sess-health",
        "timestamp_ms": int(time.time() * 1000),
        "total_input_tokens": 5_000,
        "total_output_tokens": 1_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "context_window_size": 200_000,
        "used_percentage": 3,
        "remaining_percentage": 97,
        "total_cost_usd": 0.02,
        "total_duration_ms": 3000,
        "model_id": "claude-sonnet-4-20250514",
        "model_display_name": "Claude Sonnet 4",
        "rate_limit_five_hour_pct": 5.0,
        "rate_limit_seven_day_pct": 1.0,
        "version": "1.0.0",
    }
    post_resp = await app_client.post("/hook/statusline", json=snapshot_payload)
    assert post_resp.status_code == 202

    health_after = await app_client.get("/api/health")
    count_after = health_after.json()["snapshot_count"]

    assert count_after == count_before + 1


async def test_session_events_endpoint(app_client: AsyncClient) -> None:
    """GET /api/sessions/{session_id}/events returns events for a specific session."""
    ts = int(time.time() * 1000)
    # Post an event for a known session.
    await app_client.post("/hook/event", json={
        "event_type": "PostToolUse",
        "session_id": "sess-specific",
        "timestamp_ms": ts,
        "payload": {"key": "val"},
        "tool_name": "Bash",
    })

    resp = await app_client.get("/api/sessions/sess-specific/events")

    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert events[0]["session_id"] == "sess-specific"
    assert events[0]["event_type"] == "PostToolUse"
