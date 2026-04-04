"""Shared fixtures for context-pulse Phase 1 tests."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Callable

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from context_pulse.collector.models import HookEventRequest, StatuslineSnapshotRequest
from context_pulse.collector.routes import api_router, hook_router
from context_pulse.config import ContextPulseConfig
from context_pulse.db.schema import open_db, run_migrations

# ---------------------------------------------------------------------------
# Database fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """In-memory aiosqlite connection with all migrations applied."""
    db = await open_db(":memory:")
    await run_migrations(db)
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# Model factory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_hook_event() -> Callable[..., HookEventRequest]:
    """Factory that returns a new HookEventRequest with reasonable defaults.

    Any keyword argument overrides the corresponding default.
    """
    _counter = 0

    def _factory(**overrides: object) -> HookEventRequest:
        nonlocal _counter
        _counter += 1
        defaults = {
            "event_type": "PostToolUse",
            "session_id": f"sess-{_counter}",
            "timestamp_ms": int(time.time() * 1000),
            "payload": {
                "session_id": f"sess-{_counter}",
                "transcript_path": "/tmp/transcript.json",
                "cwd": "/home/user/project",
                "permission_mode": "default",
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file": "main.py"},
                "tool_response": {"status": "ok"},
                "tool_use_id": f"tool-{_counter}",
            },
            "tool_name": "Edit",
            "tool_input_summary": "Editing main.py",
            "cwd": "/home/user/project",
        }
        defaults.update(overrides)
        return HookEventRequest(**defaults)  # type: ignore[arg-type]

    return _factory


@pytest.fixture
def sample_snapshot() -> Callable[..., StatuslineSnapshotRequest]:
    """Factory that returns a new StatuslineSnapshotRequest with reasonable defaults.

    Any keyword argument overrides the corresponding default.
    """
    _counter = 0

    def _factory(**overrides: object) -> StatuslineSnapshotRequest:
        nonlocal _counter
        _counter += 1
        defaults = {
            "session_id": f"sess-{_counter}",
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
        defaults.update(overrides)
        return StatuslineSnapshotRequest(**defaults)  # type: ignore[arg-type]

    return _factory


# ---------------------------------------------------------------------------
# ASGI / HTTP client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_client(
    db_connection: aiosqlite.Connection,
) -> AsyncGenerator[AsyncClient, None]:
    """httpx AsyncClient wired to the FastAPI app via ASGI transport.

    Sets app.state directly because ASGITransport does not trigger
    FastAPI lifespan events.
    """
    app = FastAPI(title="context-pulse-test")
    app.include_router(hook_router, prefix="/hook")
    app.include_router(api_router, prefix="/api")
    app.state.db = db_connection
    app.state.config = ContextPulseConfig()
    app.state.sessions = {}
    app.state.start_time = time.time()
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
