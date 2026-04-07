# Phase 1 Architecture Specification -- context-analyzer-tool

> Version: 1.0.0
> Date: 2026-03-28
> Scope: Foundation -- collector, hooks, statusline, SQLite, CLI, token delta engine
> Target: Implementing agents can code any component from this spec without ambiguity.

---

## Critical Correction from Project Brief

The project brief assumes hook payloads contain `context_window` / `current_usage` / token data. **They do not.** The actual data flow is:

- **Hooks** (PostToolUse, SubagentStop, Stop, UserPromptSubmit) carry **metadata only** -- tool names, inputs, session IDs. No token counts.
- **Statusline script** (a separate mechanism, NOT a hook) receives the **only** source of token/usage data on stdin, after every assistant message.
- Token deltas are computed by **correlating** statusline snapshots with tool-use events via `session_id` + timestamps.

This changes the entire data pipeline design compared to the brief.

---

## 1. Pydantic Models

All models use `pydantic.BaseModel`. Field names match the exact JSON keys from Claude Code.

### 1.1 Hook Payload Models (inbound from hook scripts)

```python
# src/context_analyzer_tool/collector/models.py

from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Any, Optional
import time


# --- PostToolUse Hook Payload ---

class PostToolUsePayload(BaseModel):
    """Payload received on stdin by the PostToolUse hook script."""
    session_id: str
    transcript_path: str
    cwd: str
    permission_mode: str
    hook_event_name: str  # always "PostToolUse"
    tool_name: str
    tool_input: dict[str, Any]
    tool_response: dict[str, Any]
    tool_use_id: str


# --- SubagentStop Hook Payload ---

class SubagentStopPayload(BaseModel):
    """Payload received on stdin by the SubagentStop hook script."""
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str  # always "SubagentStop"
    stop_hook_active: bool
    agent_id: str
    agent_type: str
    agent_transcript_path: str
    last_assistant_message: str


# --- Stop Hook Payload ---

class StopPayload(BaseModel):
    """Payload received on stdin by the Stop hook script."""
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str  # always "Stop"
    stop_hook_active: bool
    last_assistant_message: str


# --- UserPromptSubmit Hook Payload ---

class UserPromptSubmitPayload(BaseModel):
    """Payload received on stdin by the UserPromptSubmit hook script."""
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str  # always "UserPromptSubmit"
    prompt: str
```

### 1.2 Statusline Payload Model (inbound from statusline script)

```python
# Also in src/context_analyzer_tool/collector/models.py

class StatuslineCurrentUsage(BaseModel):
    """Token counts for the current API turn."""
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class StatuslineContextWindow(BaseModel):
    """Full context window state."""
    total_input_tokens: int
    total_output_tokens: int
    context_window_size: int
    used_percentage: int
    remaining_percentage: int
    current_usage: StatuslineCurrentUsage


class StatuslineCost(BaseModel):
    """Cumulative session cost metrics."""
    total_cost_usd: float
    total_duration_ms: int
    total_api_duration_ms: int
    total_lines_added: int
    total_lines_removed: int


class StatuslineRateLimitBucket(BaseModel):
    """A single rate-limit window."""
    used_percentage: float
    resets_at: int  # unix timestamp


class StatuslineRateLimits(BaseModel):
    """Rate limit info across windows."""
    five_hour: StatuslineRateLimitBucket
    seven_day: StatuslineRateLimitBucket


class StatuslineModelInfo(BaseModel):
    """Model identification."""
    id: str
    display_name: str


class StatuslinePayload(BaseModel):
    """Full payload received on stdin by the statusline script.
    This is the ONLY source of token/cost data."""
    session_id: str
    transcript_path: str
    model: StatuslineModelInfo
    cost: StatuslineCost
    context_window: StatuslineContextWindow
    rate_limits: StatuslineRateLimits
    version: str
```

### 1.3 Collector Inbound Models (POSTed from hook/statusline scripts to collector)

These are the HTTP request bodies that the hook and statusline scripts POST to the collector server. They wrap the raw payloads with a unified envelope.

```python
class HookEventRequest(BaseModel):
    """Unified envelope POSTed by hook scripts to collector.
    The hook script reads stdin, wraps it in this envelope, and POSTs it."""
    event_type: str  # "PostToolUse" | "SubagentStop" | "Stop" | "UserPromptSubmit"
    session_id: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    payload: dict[str, Any]  # the raw hook JSON, preserved for storage

    # Extracted fields for fast access (populated by hook script before POST)
    tool_name: Optional[str] = None       # only for PostToolUse
    tool_input_summary: Optional[str] = None  # truncated to 500 chars
    agent_id: Optional[str] = None        # only for SubagentStop
    agent_type: Optional[str] = None      # only for SubagentStop
    prompt_preview: Optional[str] = None  # only for UserPromptSubmit, first 200 chars
    cwd: Optional[str] = None

    @field_validator("tool_input_summary")
    @classmethod
    def truncate_tool_input(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 500:
            return v[:497] + "..."
        return v

    @field_validator("prompt_preview")
    @classmethod
    def truncate_prompt(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v) > 200:
            return v[:197] + "..."
        return v


class StatuslineSnapshotRequest(BaseModel):
    """POSTed by statusline script to collector."""
    session_id: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    total_input_tokens: int
    total_output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    context_window_size: int
    used_percentage: int
    remaining_percentage: int
    total_cost_usd: float
    total_duration_ms: int
    model_id: str
    model_display_name: str
    rate_limit_five_hour_pct: float
    rate_limit_seven_day_pct: float
    version: str
```

### 1.4 Internal DB Event Model

```python
class StoredEvent(BaseModel):
    """Represents a row in the events table."""
    id: Optional[int] = None
    session_id: str
    agent_id: Optional[str] = None
    event_type: str
    tool_name: Optional[str] = None
    tool_input_summary: Optional[str] = None
    cwd: Optional[str] = None
    timestamp_ms: int
    payload_json: str  # full original payload, stored as JSON string


class StoredSnapshot(BaseModel):
    """Represents a row in the token_snapshots table."""
    id: Optional[int] = None
    session_id: str
    timestamp_ms: int
    total_input_tokens: int
    total_output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    context_window_size: int
    used_percentage: int
    total_cost_usd: float
    model_id: str


class StoredTask(BaseModel):
    """Represents a row in the tasks table (derived: event + delta)."""
    id: Optional[int] = None
    session_id: str
    event_id: int  # FK to events.id
    task_type: str  # tool_name or synthetic type like "SubagentStop:{agent_type}"
    token_delta: Optional[int] = None  # null if no snapshot pair yet
    is_compaction: bool = False  # true if negative delta detected
    snapshot_before_id: Optional[int] = None  # FK to token_snapshots.id
    snapshot_after_id: Optional[int] = None   # FK to token_snapshots.id
    timestamp_ms: int
    anomaly_id: Optional[int] = None  # FK to anomalies.id, set later
```

### 1.5 API Response Models

```python
class EventResponse(BaseModel):
    """Single event in API responses."""
    id: int
    session_id: str
    agent_id: Optional[str]
    event_type: str
    tool_name: Optional[str]
    tool_input_summary: Optional[str]
    cwd: Optional[str]
    timestamp_ms: int


class TaskResponse(BaseModel):
    """Single task with token delta in API responses."""
    id: int
    session_id: str
    task_type: str
    token_delta: Optional[int]
    is_compaction: bool
    timestamp_ms: int
    anomaly_id: Optional[int]


class SnapshotResponse(BaseModel):
    """Single token snapshot in API responses."""
    id: int
    session_id: str
    timestamp_ms: int
    total_input_tokens: int
    total_output_tokens: int
    used_percentage: int
    model_id: str


class SessionSummary(BaseModel):
    """Summary of a session for the status command."""
    session_id: str
    event_count: int
    first_event_ms: int
    last_event_ms: int
    total_tokens_used: Optional[int]  # from latest snapshot
    used_percentage: Optional[int]
    model_id: Optional[str]


class StatusResponse(BaseModel):
    """Response for GET /status."""
    active_sessions: list[SessionSummary]
    recent_events: list[EventResponse]
    recent_tasks: list[TaskResponse]


class HealthResponse(BaseModel):
    """Response for GET /health."""
    status: str  # "ok"
    uptime_seconds: float
    db_path: str
    event_count: int
    snapshot_count: int
```

### 1.6 Configuration Model

```python
# src/context_analyzer_tool/config.py

from pydantic import BaseModel, Field
from pathlib import Path


class CollectorConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7821
    db_path: str = "~/.context-analyzer-tool/context_analyzer_tool.db"


class AnomalyConfig(BaseModel):
    z_score_threshold: float = 2.0
    min_sample_count: int = 5
    cooldown_seconds: int = 60
    task_types_ignored: list[str] = Field(default_factory=list)
    baseline_window: int = 20  # number of recent samples for rolling stats


class ClassifierConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 150
    cache_results: bool = True


class NotificationsConfig(BaseModel):
    statusline: bool = True
    system_notification: bool = True
    in_session_alert: bool = True
    webhook_url: str = ""


class DashboardConfig(BaseModel):
    default_mode: str = "tui"  # "tui" | "web"
    web_port: int = 7822


class CATConfig(BaseModel):
    """Root configuration model. Maps 1:1 to config.toml sections."""
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    anomaly: AnomalyConfig = Field(default_factory=AnomalyConfig)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
```

---

## 2. Database Schema

### 2.1 DDL (SQLite, WAL mode)

```sql
-- Executed on first DB open via schema.py

PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

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
```

### 2.2 Migration Strategy

- `schema_version` table tracks applied versions. On startup, `schema.py` reads current version, applies any unapplied migrations in order.
- Phase 1 is version 1. Each future phase that alters the schema increments version.
- Migrations are Python functions in an ordered list: `MIGRATIONS: list[tuple[int, str, Callable]]` where each entry is `(version, description, async_func)`.
- The async function receives an `aiosqlite.Connection` and executes DDL.
- All migrations run inside a transaction. If a migration fails, it rolls back and the app refuses to start with a clear error message.
- No down-migrations. If a migration is wrong, a new forward migration fixes it.

---

## 3. Token Delta Engine Design

### 3.1 Core Concept

Token data arrives via statusline snapshots. Tool metadata arrives via PostToolUse hooks. These are two separate streams that must be correlated.

**Timeline for a single tool call:**

```
t0: User sends prompt        -> UserPromptSubmit hook fires
t1: Claude calls a tool      -> (no hook yet)
t2: Tool executes, returns   -> PostToolUse hook fires, posts to collector
t3: Claude produces response -> Statusline script fires, posts snapshot to collector
```

The token delta for the tool call at t2 is: `snapshot(t3).total_input_tokens - snapshot(t_prev).total_input_tokens`

Where `t_prev` is the snapshot immediately before this tool call (from the previous assistant turn).

### 3.2 In-Memory State Per Session

```python
# src/context_analyzer_tool/collector/delta_engine.py

from dataclasses import dataclass, field
from collections import deque


@dataclass
class PendingToolCall:
    """A PostToolUse event waiting for a snapshot to compute its delta."""
    event_id: int
    task_type: str
    timestamp_ms: int


@dataclass
class SessionState:
    """In-memory state for one session's delta computation."""
    session_id: str
    last_snapshot_id: Optional[int] = None
    last_snapshot_total_input: Optional[int] = None
    last_snapshot_timestamp_ms: Optional[int] = None
    pending_tool_calls: deque[PendingToolCall] = field(default_factory=deque)
    # Track whether this is the very first snapshot (no delta possible)
    has_initial_snapshot: bool = False
```

The collector maintains a `dict[str, SessionState]` keyed by `session_id`, stored on `app.state.sessions`.

### 3.3 Correlation Algorithm

**On PostToolUse event received:**

1. Look up or create `SessionState` for `session_id`.
2. Insert into `events` table, get `event_id`.
3. Insert a `tasks` row with `token_delta=NULL`, `snapshot_before_id=session.last_snapshot_id`.
4. Append `PendingToolCall(event_id, task_type, timestamp_ms)` to `session.pending_tool_calls`.

**On statusline snapshot received:**

1. Look up or create `SessionState` for `session_id`.
2. Insert into `token_snapshots` table, get `snapshot_id`.
3. If `session.has_initial_snapshot` is False:
   - Set `session.last_snapshot_id = snapshot_id`, `last_snapshot_total_input = total_input_tokens`, `has_initial_snapshot = True`.
   - Discard all pending tool calls (no delta possible for first snapshot).
   - Return.
4. Compute `raw_delta = total_input_tokens - session.last_snapshot_total_input`.
5. If `raw_delta < 0`: this is a **context compaction** event (see 3.4).
6. If there are pending tool calls:
   - If exactly 1 pending: assign the full delta to it.
   - If multiple pending (rare, happens when Claude chains tool calls before responding): distribute delta equally (integer division, remainder to last), or assign full delta to the last one and 0 to others. **Decision: assign full delta to last pending tool call, 0 to earlier ones.** Rationale: the last tool call in a chain is the one whose response is freshest and most likely caused the token increase. Earlier calls' responses were already in context.
7. For each pending tool call, UPDATE the `tasks` row: set `token_delta`, `snapshot_after_id = snapshot_id`, `is_compaction` flag.
8. Clear `session.pending_tool_calls`.
9. Update `session.last_snapshot_id`, `last_snapshot_total_input`, `last_snapshot_timestamp_ms`.

### 3.4 Edge Cases

| Edge Case | Handling |
|---|---|
| **Session start (first snapshot)** | No previous snapshot exists. Set `has_initial_snapshot=True`, store snapshot, discard any pending tool calls. No delta computed. |
| **Negative delta (context compaction)** | Claude Code compacts context at ~83% capacity. A negative delta means tokens were removed, not added. Set `is_compaction=True` on all pending tasks. Store the negative delta as-is. **Do not feed compaction events to the anomaly detector** (Phase 2). Log a structured message: `"compaction detected, session={session_id}, delta={delta}"`. |
| **Zero delta** | Valid -- means a cached response or minimal-cost operation. Store as-is. Zero deltas are valid baseline samples. |
| **Snapshot with no pending tool calls** | The assistant responded without calling a tool (pure text response). Insert snapshot, update session state, but no task row to update. This is normal. |
| **Tool call with no subsequent snapshot** | Can happen if the session ends abruptly. The task row keeps `token_delta=NULL`. A background cleanup task (runs every 60s) can mark stale pending tool calls (>30s old with no snapshot) as `token_delta=NULL` and clear them from memory. |
| **Multiple snapshots between tool calls** | Each snapshot updates `session.last_snapshot_*`. Only the snapshot immediately following a tool call is relevant. Intermediate snapshots (from pure-text assistant turns) just update the baseline pointer. |
| **Interleaved sessions** | Each session has its own `SessionState`. No cross-contamination. The `dict[str, SessionState]` is keyed by session_id. |
| **Session resumption** | If a session_id reappears after a gap, its `SessionState` may have been evicted. Treat it as a new session (first snapshot has no delta). Eviction policy: remove sessions idle for >1 hour from the in-memory dict. |
| **Collector restart** | All in-memory state is lost. On restart, for each active session (events in last 30 min), load the last snapshot from DB to reconstruct `SessionState`. Pending tool calls from before crash are lost; their tasks keep `token_delta=NULL`. |
| **SubagentStop events** | These are recorded as events but do NOT get token deltas (subagents have their own transcript). Create a task row with `task_type="SubagentStop:{agent_type}"` and `token_delta=NULL`. Subagent token tracking requires parsing subagent transcripts (out of scope for Phase 1). |
| **UserPromptSubmit events** | Recorded as events only. No task row created. Used for context (what prompt preceded a spike). |
| **Stop events** | Recorded as events. Triggers session cleanup: mark session as inactive, evict from in-memory dict after final snapshot is processed. |

### 3.5 Data Flow Diagram

```
Hook Script (PostToolUse)         Statusline Script
        |                                |
        v                                v
  POST /hook/event              POST /hook/statusline
        |                                |
        v                                v
  Insert events row             Insert token_snapshots row
  Insert tasks row (delta=NULL)         |
  Append to pending_tool_calls          |
        |                                |
        +----------- Correlate ----------+
                         |
                         v
                  UPDATE tasks SET
                  token_delta = snapshot_delta,
                  snapshot_after_id = new_snapshot_id
```

---

## 4. Component Interfaces

### 4.1 `src/context_analyzer_tool/collector/server.py` -- FastAPI App

```python
"""FastAPI application factory and lifespan management."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator
import aiosqlite
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup:
      - Load config from ~/.context-analyzer-tool/config.toml
      - Open aiosqlite connection (WAL mode, busy_timeout=5000, synchronous=NORMAL)
      - Store db connection on app.state.db
      - Store config on app.state.config
      - Initialize app.state.sessions = {} (dict[str, SessionState])
      - Initialize app.state.start_time = time.time()
      - Run schema migrations
    Shutdown:
      - Close aiosqlite connection
    """
    ...

def create_app() -> FastAPI:
    """
    Returns FastAPI app with:
      - lifespan=lifespan
      - include_router(hook_router, prefix="/hook")
      - include_router(api_router, prefix="/api")
    """
    ...
```

External deps: `fastapi`, `aiosqlite`, `uvicorn`
Internal imports: `config.load_config`, `db.schema.run_migrations`, `collector.routes`

### 4.2 `src/context_analyzer_tool/collector/routes.py` -- Route Handlers

```python
"""All HTTP route handlers."""

from fastapi import APIRouter, Depends, Request
from aiosqlite import Connection

hook_router = APIRouter()
api_router = APIRouter()


async def get_db(request: Request) -> Connection:
    """Dependency: yields app.state.db."""
    return request.app.state.db


async def get_sessions(request: Request) -> dict[str, SessionState]:
    """Dependency: yields app.state.sessions."""
    return request.app.state.sessions


async def get_config(request: Request) -> CATConfig:
    """Dependency: yields app.state.config."""
    return request.app.state.config


@hook_router.post("/event", status_code=202)
async def receive_hook_event(
    event: HookEventRequest,
    db: Connection = Depends(get_db),
    sessions: dict = Depends(get_sessions),
) -> dict[str, str]:
    """
    Receives a hook event from any hook script.
    1. Validate event_type is one of the 4 known types.
    2. Call db.events.insert_event(db, event) -> event_id.
    3. If PostToolUse: call delta_engine.on_tool_use(sessions, db, event, event_id).
    4. If SubagentStop: call db.tasks.insert_task(db, task_with_null_delta).
    5. If Stop: call delta_engine.on_session_stop(sessions, event.session_id).
    6. Return {"status": "accepted"}.
    Errors: 422 for validation failure. Never 500 -- catch all exceptions,
    log them, return 202 anyway (hooks must not see errors).
    """
    ...


@hook_router.post("/statusline", status_code=202)
async def receive_statusline_snapshot(
    snapshot: StatuslineSnapshotRequest,
    db: Connection = Depends(get_db),
    sessions: dict = Depends(get_sessions),
) -> dict[str, str]:
    """
    Receives a statusline snapshot.
    1. Call db.events.insert_snapshot(db, snapshot) -> snapshot_id.
    2. Call delta_engine.on_snapshot(sessions, db, snapshot, snapshot_id).
    3. Return {"status": "accepted"}.
    """
    ...


@api_router.get("/status", response_model=StatusResponse)
async def get_status(
    db: Connection = Depends(get_db),
    sessions: dict = Depends(get_sessions),
) -> StatusResponse:
    """
    Returns active sessions, last 20 events, last 20 tasks.
    Used by `context-analyzer-tool status` CLI command.
    """
    ...


@api_router.get("/health", response_model=HealthResponse)
async def get_health(
    request: Request,
    db: Connection = Depends(get_db),
) -> HealthResponse:
    """
    Returns server health: uptime, db path, event/snapshot counts.
    Used by install script and CLI to verify collector is running.
    """
    ...


@api_router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def get_session_events(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Connection = Depends(get_db),
) -> list[EventResponse]:
    """Returns events for a specific session, ordered by timestamp desc."""
    ...


@api_router.get("/sessions/{session_id}/tasks", response_model=list[TaskResponse])
async def get_session_tasks(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    db: Connection = Depends(get_db),
) -> list[TaskResponse]:
    """Returns tasks (with deltas) for a specific session, ordered by timestamp desc."""
    ...


@api_router.get("/sessions/{session_id}/snapshots", response_model=list[SnapshotResponse])
async def get_session_snapshots(
    session_id: str,
    limit: int = 50,
    db: Connection = Depends(get_db),
) -> list[SnapshotResponse]:
    """Returns token snapshots for a specific session, ordered by timestamp desc."""
    ...
```

External deps: `fastapi`
Internal imports: `collector.models`, `collector.delta_engine`, `db.events`, `db.tasks`, `config`

### 4.3 `src/context_analyzer_tool/collector/delta_engine.py` -- Token Delta Engine

```python
"""In-memory correlation engine pairing tool calls with token snapshots."""

from dataclasses import dataclass, field
from collections import deque
from typing import Optional
import logging
import aiosqlite

logger = logging.getLogger("context_analyzer_tool.delta_engine")


@dataclass
class PendingToolCall:
    event_id: int
    task_id: int  # FK to tasks table, for UPDATE
    task_type: str
    timestamp_ms: int


@dataclass
class SessionState:
    session_id: str
    last_snapshot_id: Optional[int] = None
    last_snapshot_total_input: Optional[int] = None
    last_snapshot_timestamp_ms: Optional[int] = None
    pending_tool_calls: deque[PendingToolCall] = field(default_factory=deque)
    has_initial_snapshot: bool = False
    last_activity_ms: int = 0


async def on_tool_use(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    event: HookEventRequest,
    event_id: int,
) -> int:
    """
    Called when a PostToolUse event is received.
    1. Get or create SessionState.
    2. Insert task row with token_delta=NULL, snapshot_before_id=session.last_snapshot_id.
    3. Append PendingToolCall.
    4. Return task_id.
    """
    ...


async def on_snapshot(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    snapshot: StatuslineSnapshotRequest,
    snapshot_id: int,
) -> list[tuple[int, Optional[int], bool]]:
    """
    Called when a statusline snapshot is received.
    1. Get or create SessionState.
    2. If no initial snapshot: set it, clear pending, return [].
    3. Compute raw_delta.
    4. Determine if compaction (delta < 0).
    5. Assign delta to pending tool calls.
    6. UPDATE tasks rows in DB.
    7. Update session state.
    8. Return list of (task_id, token_delta, is_compaction) for downstream use.
    """
    ...


async def on_session_stop(
    sessions: dict[str, SessionState],
    session_id: str,
) -> None:
    """
    Called when a Stop event is received.
    Marks session for cleanup. Does not immediately evict
    (a final snapshot may still arrive).
    Sets a flag so the cleanup task can evict after 30s.
    """
    ...


async def cleanup_stale_sessions(
    sessions: dict[str, SessionState],
    max_idle_ms: int = 3_600_000,  # 1 hour
) -> list[str]:
    """
    Evict sessions idle for longer than max_idle_ms.
    Called periodically (every 60s) by a background task.
    Returns list of evicted session_ids.
    """
    ...


async def restore_sessions_from_db(
    sessions: dict[str, SessionState],
    db: aiosqlite.Connection,
    lookback_ms: int = 1_800_000,  # 30 minutes
) -> int:
    """
    On collector startup, reconstruct SessionState for recently active sessions.
    For each session with snapshots in the last lookback_ms:
      - Load the latest snapshot to set last_snapshot_* fields.
      - Set has_initial_snapshot=True.
      - Pending tool calls from before crash are lost.
    Returns number of restored sessions.
    """
    ...
```

External deps: `aiosqlite`
Internal imports: `collector.models`, `db.tasks`

### 4.4 `src/context_analyzer_tool/db/schema.py` -- Schema and Migrations

```python
"""Database schema creation and migration management."""

import aiosqlite
import logging

logger = logging.getLogger("context_analyzer_tool.db.schema")

MIGRATIONS: list[tuple[int, str]] = [
    (1, "initial schema"),
]


async def open_db(db_path: str) -> aiosqlite.Connection:
    """
    Open SQLite connection with WAL mode, busy_timeout=5000, synchronous=NORMAL.
    Sets row_factory = aiosqlite.Row.
    Returns the connection (caller is responsible for closing).
    """
    ...


async def run_migrations(db: aiosqlite.Connection) -> None:
    """
    Check schema_version table, apply any unapplied migrations.
    Each migration runs in a transaction.
    Raises RuntimeError if a migration fails.
    """
    ...


async def _apply_v1(db: aiosqlite.Connection) -> None:
    """Apply version 1 schema: all CREATE TABLE and CREATE INDEX statements."""
    ...


async def get_schema_version(db: aiosqlite.Connection) -> int:
    """Returns current schema version, or 0 if schema_version table doesn't exist."""
    ...
```

External deps: `aiosqlite`
Internal imports: none

### 4.5 `src/context_analyzer_tool/db/events.py` -- Event CRUD

```python
"""CRUD operations for the events and token_snapshots tables."""

import aiosqlite
import json
from typing import Optional


async def insert_event(
    db: aiosqlite.Connection,
    session_id: str,
    event_type: str,
    timestamp_ms: int,
    payload_json: str,
    tool_name: Optional[str] = None,
    tool_input_summary: Optional[str] = None,
    agent_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> int:
    """
    INSERT INTO events (...) VALUES (...).
    Returns the new row id (lastrowid).
    """
    ...


async def insert_snapshot(
    db: aiosqlite.Connection,
    session_id: str,
    timestamp_ms: int,
    total_input_tokens: int,
    total_output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    context_window_size: int,
    used_percentage: int,
    total_cost_usd: float,
    model_id: str,
) -> int:
    """
    INSERT INTO token_snapshots (...) VALUES (...).
    Returns the new row id.
    """
    ...


async def get_recent_events(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[dict]:
    """
    SELECT from events, ordered by timestamp_ms DESC.
    Optional filters by session_id and/or event_type.
    Returns list of dicts.
    """
    ...


async def get_latest_snapshot(
    db: aiosqlite.Connection,
    session_id: str,
) -> Optional[dict]:
    """
    SELECT latest token_snapshot for a given session.
    Returns dict or None.
    """
    ...


async def get_recent_snapshots(
    db: aiosqlite.Connection,
    session_id: str,
    limit: int = 50,
) -> list[dict]:
    """
    SELECT token_snapshots for session, ordered by timestamp_ms DESC.
    """
    ...


async def get_active_session_ids(
    db: aiosqlite.Connection,
    since_ms: int,
) -> list[str]:
    """
    SELECT DISTINCT session_id from events WHERE timestamp_ms > since_ms.
    Used for session restoration on startup.
    """
    ...


async def get_event_count(db: aiosqlite.Connection) -> int:
    """SELECT COUNT(*) FROM events."""
    ...


async def get_snapshot_count(db: aiosqlite.Connection) -> int:
    """SELECT COUNT(*) FROM token_snapshots."""
    ...
```

External deps: `aiosqlite`
Internal imports: none

### 4.6 `src/context_analyzer_tool/db/tasks.py` -- Task CRUD + Delta Operations

```python
"""CRUD operations for the tasks table."""

import aiosqlite
from typing import Optional


async def insert_task(
    db: aiosqlite.Connection,
    session_id: str,
    event_id: int,
    task_type: str,
    timestamp_ms: int,
    snapshot_before_id: Optional[int] = None,
    token_delta: Optional[int] = None,
    is_compaction: bool = False,
) -> int:
    """
    INSERT INTO tasks (...) VALUES (...).
    Returns new row id.
    """
    ...


async def update_task_delta(
    db: aiosqlite.Connection,
    task_id: int,
    token_delta: Optional[int],
    snapshot_after_id: int,
    is_compaction: bool = False,
) -> None:
    """
    UPDATE tasks SET token_delta=?, snapshot_after_id=?, is_compaction=?
    WHERE id=?.
    """
    ...


async def get_recent_tasks(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: Optional[str] = None,
    task_type: Optional[str] = None,
    exclude_compaction: bool = False,
) -> list[dict]:
    """
    SELECT from tasks, ordered by timestamp_ms DESC.
    Optional filters.
    If exclude_compaction=True, adds WHERE is_compaction=0.
    Returns list of dicts.
    """
    ...


async def get_tasks_by_type(
    db: aiosqlite.Connection,
    task_type: str,
    limit: int = 20,
) -> list[dict]:
    """
    SELECT from tasks WHERE task_type=? AND token_delta IS NOT NULL
    ORDER BY timestamp_ms DESC LIMIT ?.
    Used for baseline computation.
    """
    ...


async def get_null_delta_tasks(
    db: aiosqlite.Connection,
    older_than_ms: int,
) -> list[dict]:
    """
    SELECT tasks WHERE token_delta IS NULL AND timestamp_ms < older_than_ms.
    Used by cleanup routine to find orphaned pending tasks.
    """
    ...
```

External deps: `aiosqlite`
Internal imports: none

### 4.7 `src/context_analyzer_tool/config.py` -- Configuration

```python
"""TOML configuration loader."""

import tomllib
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger("context_analyzer_tool.config")

DEFAULT_CONFIG_DIR = Path.home() / ".context-analyzer-tool"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"


def load_config(config_path: Optional[Path] = None) -> CATConfig:
    """
    Load config from TOML file.
    1. If config_path is None, use DEFAULT_CONFIG_PATH.
    2. If file does not exist, return CATConfig() (all defaults).
    3. Parse TOML, validate with Pydantic.
    4. Expand ~ in db_path.
    Returns CATConfig.
    Raises ValueError if TOML is malformed.
    """
    ...


def ensure_config_dir() -> Path:
    """
    Create ~/.context-analyzer-tool/ if it doesn't exist.
    Returns the directory path.
    """
    ...


def get_db_path(config: CATConfig) -> str:
    """
    Resolve db_path from config, expanding ~.
    Returns absolute path string.
    """
    ...


def write_default_config(path: Optional[Path] = None) -> Path:
    """
    Write a default config.toml with comments to the given path.
    Returns the path written.
    """
    ...
```

External deps: `tomllib` (stdlib 3.11+), `pydantic`
Internal imports: `collector.models.CATConfig` (or defined here)

---

## 5. Hook Scripts Design

All hook scripts are standalone Python files using PEP 723 inline script metadata for `uv run` zero-install execution. They read JSON from stdin, POST to the collector, and exit 0 always.

### 5.1 Common Structure (all 4 hook scripts)

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

"""
context-analyzer-tool hook: {HookName}
Reads hook payload from stdin, POSTs to collector, exits 0.
"""

import sys
import json
import time
import httpx

COLLECTOR_URL = "http://127.0.0.1:7821/hook/event"
TIMEOUT_SECONDS = 2.0


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        # Build envelope (specific to each hook -- see below)
        envelope = build_envelope(payload)

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            client.post(COLLECTOR_URL, json=envelope)
    except Exception:
        # NEVER fail. Claude Code must not see a non-zero exit.
        pass

    sys.exit(0)
```

Note: `httpx` is used instead of `aiohttp` because these are short-lived synchronous scripts. Async would add startup overhead for no benefit. `httpx` is lighter than `requests` for this use case.

### 5.2 `hooks/post_tool_use.py`

```python
def build_envelope(payload: dict) -> dict:
    tool_input_str = json.dumps(payload.get("tool_input", {}))
    if len(tool_input_str) > 500:
        tool_input_str = tool_input_str[:497] + "..."
    return {
        "event_type": "PostToolUse",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
        "tool_name": payload.get("tool_name"),
        "tool_input_summary": tool_input_str,
        "cwd": payload.get("cwd"),
    }
```

This is the highest-volume hook. Fires after every tool call.

### 5.3 `hooks/subagent_stop.py`

```python
def build_envelope(payload: dict) -> dict:
    return {
        "event_type": "SubagentStop",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
        "agent_id": payload.get("agent_id"),
        "agent_type": payload.get("agent_type"),
    }
```

### 5.4 `hooks/stop.py`

```python
def build_envelope(payload: dict) -> dict:
    return {
        "event_type": "Stop",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
    }
```

### 5.5 `hooks/user_prompt_submit.py`

```python
def build_envelope(payload: dict) -> dict:
    prompt = payload.get("prompt", "")
    if len(prompt) > 200:
        prompt = prompt[:197] + "..."
    return {
        "event_type": "UserPromptSubmit",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
        "prompt_preview": prompt,
        "cwd": payload.get("cwd"),
    }
```

### 5.6 `hooks/statusline.py` -- The Statusline Script

This script serves a **dual purpose**: it both POSTs snapshot data to the collector AND outputs a statusline string to stdout for Claude Code to display.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

"""
context-analyzer-tool statusline script.
1. Reads statusline JSON from stdin (provided by Claude Code).
2. POSTs token snapshot to collector (fire-and-forget, 2s timeout).
3. Prints a formatted statusline string to stdout.
"""

import sys
import json
import time
import httpx

COLLECTOR_URL = "http://127.0.0.1:7821/hook/statusline"
TIMEOUT_SECONDS = 2.0


def post_snapshot(data: dict) -> None:
    """POST snapshot data to collector. Swallow all errors."""
    try:
        cw = data.get("context_window", {})
        cu = cw.get("current_usage", {})
        cost = data.get("cost", {})
        model = data.get("model", {})
        rl = data.get("rate_limits", {})

        snapshot = {
            "session_id": data["session_id"],
            "timestamp_ms": int(time.time() * 1000),
            "total_input_tokens": cw.get("total_input_tokens", 0),
            "total_output_tokens": cw.get("total_output_tokens", 0),
            "cache_creation_input_tokens": cu.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": cu.get("cache_read_input_tokens", 0),
            "context_window_size": cw.get("context_window_size", 0),
            "used_percentage": cw.get("used_percentage", 0),
            "total_cost_usd": cost.get("total_cost_usd", 0.0),
            "total_duration_ms": cost.get("total_duration_ms", 0),
            "model_id": model.get("id", "unknown"),
            "model_display_name": model.get("display_name", "Unknown"),
            "rate_limit_five_hour_pct": rl.get("five_hour", {}).get("used_percentage", 0.0),
            "rate_limit_seven_day_pct": rl.get("seven_day", {}).get("used_percentage", 0.0),
            "version": data.get("version", "unknown"),
        }

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            client.post(COLLECTOR_URL, json=snapshot)
    except Exception:
        pass


def format_statusline(data: dict) -> str:
    """
    Format the statusline string for Claude Code display.
    Normal: "modelName | ctx 42% ████░░ | $0.01 | 5h: 24% | 7d: 41%"
    """
    cw = data.get("context_window", {})
    cost = data.get("cost", {})
    model = data.get("model", {})
    rl = data.get("rate_limits", {})

    used_pct = cw.get("used_percentage", 0)
    model_name = model.get("display_name", "Claude")
    total_cost = cost.get("total_cost_usd", 0.0)
    five_hour_pct = rl.get("five_hour", {}).get("used_percentage", 0.0)
    seven_day_pct = rl.get("seven_day", {}).get("used_percentage", 0.0)

    # Build progress bar (10 chars)
    filled = round(used_pct / 10)
    bar = "\u2588" * filled + "\u2591" * (10 - filled)

    return (
        f"{model_name} | ctx {used_pct}% {bar} "
        f"| ${total_cost:.2f} "
        f"| 5h: {five_hour_pct:.0f}% "
        f"| 7d: {seven_day_pct:.0f}%"
    )


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)

        # POST to collector (best-effort)
        post_snapshot(data)

        # Output statusline to stdout
        print(format_statusline(data))
    except Exception:
        # On any error, output a safe default
        print("context-analyzer-tool | --")

    sys.exit(0)


if __name__ == "__main__":
    main()
```

### 5.7 Cross-Platform Notes

| Concern | Resolution |
|---|---|
| **Python path** | Hook commands use `uv run` which finds Python automatically. On Windows, `uv` is in PATH after install via `winget`/`scoop`/`pip`. |
| **stdin encoding** | Always UTF-8. No special handling needed on modern systems (Python 3.11+ defaults to UTF-8 mode). |
| **Path separators** | All paths from Claude Code use forward slashes regardless of OS. No conversion needed. |
| **httpx vs requests** | `httpx` is used because it supports HTTP/2, has better timeout handling, and is lighter for single-shot scripts. |
| **PEP 723 on Windows** | `uv run` supports inline script metadata on all platforms. The shebang line (`#!/usr/bin/env python3`) is ignored on Windows; `uv run` handles execution. |
| **Line endings** | Hook scripts should use LF. Committed with `.gitattributes: *.py text eol=lf`. |

### 5.8 Installation Configuration in settings.json

The `context-analyzer-tool install` command writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.context-analyzer-tool/hooks/post_tool_use.py",
            "timeout": 5
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.context-analyzer-tool/hooks/subagent_stop.py",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.context-analyzer-tool/hooks/stop.py",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.context-analyzer-tool/hooks/user_prompt_submit.py",
            "timeout": 5
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "uv run ~/.context-analyzer-tool/hooks/statusline.py"
  }
}
```

Note: The install command must **merge** with existing settings, not overwrite. It reads the existing file, adds/updates only the context-analyzer-tool entries, and writes back with proper JSON formatting.

---

## 6. CLI Commands

CLI is built with `typer`. Since Typer does not support async, all async operations are wrapped with `asyncio.run()`.

### 6.1 `context-analyzer-tool serve`

```
Usage: context-analyzer-tool serve [OPTIONS]

Start the collector server.

Options:
  --host TEXT     Bind host [default: 127.0.0.1]
  --port INTEGER  Bind port [default: 7821]
  --config PATH   Config file path [default: ~/.context-analyzer-tool/config.toml]
  --log-level TEXT  Log level [default: info]
```

**What it does:**
1. Load config (CLI args override config file values).
2. Ensure DB directory exists.
3. Call `uvicorn.run(create_app(), host=host, port=port, log_level=log_level)`.

**Output:** Logs to stderr. No stdout output (so it works in background).

**Implementation:**
```python
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(7821, help="Bind port"),
    config: Optional[Path] = typer.Option(None, help="Config file path"),
    log_level: str = typer.Option("info", help="Log level"),
) -> None:
    """Start the context-analyzer-tool collector server."""
    import uvicorn
    from context_analyzer_tool.collector.server import create_app

    cfg = load_config(config)
    actual_host = host or cfg.collector.host
    actual_port = port or cfg.collector.port

    uvicorn.run(
        create_app(),
        host=actual_host,
        port=actual_port,
        log_level=log_level,
    )
```

### 6.2 `context-analyzer-tool status`

```
Usage: context-analyzer-tool status [OPTIONS]

Show recent events and active sessions.

Options:
  --session TEXT   Filter by session ID (prefix match)
  --limit INTEGER  Number of events to show [default: 10]
  --json           Output as JSON instead of table
  --url TEXT       Collector URL [default: http://127.0.0.1:7821]
```

**What it does:**
1. GET `{url}/api/status` from the running collector.
2. If collector is unreachable, print error and suggest `context-analyzer-tool serve`.
3. Render a Rich table with columns: `Time | Session | Type | Tool | Delta | Compaction`.
4. Above the table, show active session summary: session_id (truncated to 8 chars), event count, latest ctx%, model.

**Output (normal):**
```
Active Sessions:
  abc12345  |  42 events  |  ctx 67%  |  Opus

Recent Events:
 Time       Session   Type          Tool    Delta   Compact
 14:23:05   abc1...   PostToolUse   Bash    +1,240
 14:22:58   abc1...   PostToolUse   Read    +340
 14:22:41   abc1...   PostToolUse   Grep    +890
 14:22:30   abc1...   PostToolUse   Bash    -3,200  yes
 14:22:12   abc1...   PostToolUse   Edit    +120
 ...
```

**Implementation:**
```python
@app.command()
def status(
    session: Optional[str] = typer.Option(None, help="Filter by session ID prefix"),
    limit: int = typer.Option(10, help="Number of events"),
    json_output: bool = typer.Option(False, "--json", help="JSON output"),
    url: str = typer.Option("http://127.0.0.1:7821", help="Collector URL"),
) -> None:
    """Show recent events and active sessions."""
    import asyncio
    asyncio.run(_status_async(session, limit, json_output, url))


async def _status_async(
    session: Optional[str], limit: int, json_output: bool, url: str
) -> None:
    import httpx
    from rich.console import Console
    from rich.table import Table

    console = Console()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{url}/api/status")
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Is it running? Start with: [bold]context-analyzer-tool serve[/bold]"
        )
        raise typer.Exit(1)

    if json_output:
        import json
        console.print(json.dumps(data, indent=2))
        return

    # Render active sessions
    # Render recent events table
    # (Rich table rendering code)
    ...
```

### 6.3 `context-analyzer-tool install`

```
Usage: context-analyzer-tool install [OPTIONS]

Install hooks and statusline into Claude Code settings.

Options:
  --claude-settings PATH   Path to settings.json [default: ~/.claude/settings.json]
  --hooks-dir PATH         Where to copy hook scripts [default: ~/.context-analyzer-tool/hooks/]
  --uninstall              Remove context-analyzer-tool hooks and statusline
  --check                  Verify installation without modifying anything
  --use-http               Use HTTP hooks instead of command hooks (posts directly to collector)
```

**What it does (install):**
1. Create `~/.context-analyzer-tool/` and `~/.context-analyzer-tool/hooks/` directories.
2. Copy hook scripts from the package to `~/.context-analyzer-tool/hooks/`.
3. Write default `config.toml` if it doesn't exist.
4. Read existing `~/.claude/settings.json` (or create if missing).
5. Merge hook entries and statusline entry (preserve existing non-context-analyzer-tool hooks).
6. Write back settings.json.
7. Verify collector is reachable (GET /health). If not, warn user.
8. Print summary of what was installed.

**What it does (--check):**
1. Check if hooks directory exists and contains expected scripts.
2. Check if settings.json has context-analyzer-tool hooks registered.
3. Check if collector is reachable.
4. Print pass/fail for each check.

**What it does (--uninstall):**
1. Remove context-analyzer-tool hook entries from settings.json.
2. Remove statusline entry from settings.json (only if it points to context-analyzer-tool).
3. Optionally remove ~/.context-analyzer-tool/ directory (prompt user).

**What it does (--use-http):**
Instead of `"type": "command"` hooks, install `"type": "http"` hooks that POST directly to the collector. This avoids spawning a Python process per hook but requires the collector to be running at hook time.

```json
{
  "type": "http",
  "url": "http://127.0.0.1:7821/hook/event",
  "timeout": 5
}
```

When using HTTP hooks, the statusline script is still a command (Claude Code does not support HTTP statuslines -- statusline must output to stdout).

**Implementation note:** The install command is synchronous (no async needed). It uses `pathlib` for path operations and `json` for settings.json manipulation.

---

## 7. Dependency Graph

### 7.1 Module Dependencies

```
config.py
  └── (no internal deps, only stdlib + pydantic)

db/schema.py
  └── (no internal deps, only aiosqlite)

db/events.py
  └── (no internal deps, only aiosqlite)

db/tasks.py
  └── (no internal deps, only aiosqlite)

collector/models.py
  └── (no internal deps, only pydantic)

collector/delta_engine.py
  ├── collector/models.py
  └── db/tasks.py

collector/routes.py
  ├── collector/models.py
  ├── collector/delta_engine.py
  ├── db/events.py
  └── db/tasks.py

collector/server.py
  ├── collector/routes.py
  ├── db/schema.py
  └── config.py

cli.py
  ├── collector/server.py
  ├── config.py
  └── (httpx for status command)

hooks/*.py
  └── (standalone, no internal deps, only httpx)
```

### 7.2 Parallelization Plan

These groups can be built in parallel:

**Group A (no internal deps):**
- `config.py`
- `collector/models.py`
- `db/schema.py`
- `db/events.py`
- `db/tasks.py`
- All hook scripts (`hooks/*.py`)

**Group B (depends on Group A):**
- `collector/delta_engine.py` (needs models, db/tasks)
- `collector/routes.py` (needs models, delta_engine, db/events, db/tasks)

**Group C (depends on Group B):**
- `collector/server.py` (needs routes, schema, config)
- `cli.py` (needs server, config)

**Group D (standalone, parallel with everything):**
- `tests/` (can be written alongside each module)

### 7.3 Visual Dependency Graph

```
                  ┌─────────────┐
                  │   cli.py    │
                  └──────┬──────┘
                         │
              ┌──────────┴──────────┐
              │                     │
     ┌────────▼────────┐    ┌──────▼──────┐
     │ collector/       │    │  config.py  │
     │   server.py      │    └─────────────┘
     └────────┬─────────┘
              │
     ┌────────▼─────────┐
     │ collector/        │
     │   routes.py       │
     └────────┬──────────┘
              │
    ┌─────────┼──────────────┐
    │         │              │
┌───▼───┐ ┌──▼──────────┐ ┌─▼────────────┐
│models │ │delta_engine  │ │ db/events.py │
│ .py   │ │    .py       │ │ db/tasks.py  │
└───────┘ └──────────────┘ │ db/schema.py │
                           └──────────────┘
```

---

## 8. Configuration Schema

### 8.1 Full TOML with Defaults and Comments

```toml
# ~/.context-analyzer-tool/config.toml
# context-analyzer-tool configuration

[collector]
# Host to bind the collector server
host = "127.0.0.1"
# Port for the collector HTTP server
port = 7821
# Path to SQLite database (~ is expanded)
db_path = "~/.context-analyzer-tool/context_analyzer_tool.db"

[anomaly]
# Z-score threshold for anomaly detection (Phase 2)
z_score_threshold = 2.0
# Minimum samples before anomaly detection activates
min_sample_count = 5
# Seconds before re-alerting for the same session
cooldown_seconds = 60
# Task types to exclude from anomaly detection
task_types_ignored = []
# Number of recent samples for rolling baseline
baseline_window = 20

[classifier]
# Enable LLM-based root cause classification (Phase 2)
enabled = true
# Model to use for classification
model = "claude-haiku-4-5-20251001"
# Max tokens for classifier response
max_tokens = 150
# Cache classifier results to avoid redundant calls
cache_results = true

[notifications]
# Show context-analyzer-tool data in Claude Code statusline
statusline = true
# Fire OS-level notifications on anomalies
system_notification = true
# Inject alerts into Claude Code via additionalContext
in_session_alert = true
# Webhook URL for Slack/Discord/custom (empty = disabled)
webhook_url = ""

[dashboard]
# Default dashboard mode: "tui" or "web"
default_mode = "tui"
# Port for web dashboard (Phase 5)
web_port = 7822
```

### 8.2 Pydantic Model

See Section 1.6 above. The model maps exactly: each TOML section is a nested BaseModel, each key is a field with the type and default shown in the TOML.

### 8.3 Config Loading Priority

1. Built-in defaults (Pydantic model defaults).
2. Config file (`~/.context-analyzer-tool/config.toml`).
3. Environment variables: `CAT_COLLECTOR_PORT=7822` overrides `collector.port`. Pattern: `CAT_{SECTION}_{KEY}` (uppercase).
4. CLI flags (highest priority, only for `serve` command).

Environment variable support is implemented with a custom `model_validator` on `CATConfig` that checks `os.environ` for matching keys after TOML loading.

---

## 9. File-by-File Breakdown

### 9.1 `src/context_analyzer_tool/__init__.py`

- **Contains:** Package version string `__version__ = "0.1.0"`.
- **Internal imports:** None.
- **External deps:** None.

### 9.2 `src/context_analyzer_tool/cli.py`

- **Contains:**
  - `app = typer.Typer()` -- the root CLI app.
  - `serve()` -- starts uvicorn with the FastAPI app.
  - `status()` -- fetches and displays recent events from the collector API.
  - `_status_async()` -- async implementation of status.
  - `install()` -- installs hooks into Claude Code settings.
  - `_merge_settings()` -- merges context-analyzer-tool hooks into existing settings.json.
  - `_copy_hook_scripts()` -- copies hook .py files to ~/.context-analyzer-tool/hooks/.
  - `_verify_installation()` -- checks that hooks are installed and collector is reachable.
- **Internal imports:** `config.load_config`, `config.ensure_config_dir`, `config.write_default_config`, `collector.server.create_app`.
- **External deps:** `typer`, `rich` (Console, Table, Panel), `httpx`, `uvicorn`, `asyncio`, `json`, `pathlib`, `shutil`.

### 9.3 `src/context_analyzer_tool/config.py`

- **Contains:**
  - All Pydantic config models (see Section 1.6): `CollectorConfig`, `AnomalyConfig`, `ClassifierConfig`, `NotificationsConfig`, `DashboardConfig`, `CATConfig`.
  - `load_config(config_path: Optional[Path] = None) -> CATConfig`
  - `ensure_config_dir() -> Path`
  - `get_db_path(config: CATConfig) -> str`
  - `write_default_config(path: Optional[Path] = None) -> Path`
- **Internal imports:** None.
- **External deps:** `pydantic` (BaseModel, Field), `tomllib` (stdlib), `pathlib`, `os`, `logging`.

### 9.4 `src/context_analyzer_tool/collector/__init__.py`

- **Contains:** Empty or re-exports.
- **Internal imports:** None.
- **External deps:** None.

### 9.5 `src/context_analyzer_tool/collector/models.py`

- **Contains:** All Pydantic models from Sections 1.1 through 1.5:
  - `PostToolUsePayload`, `SubagentStopPayload`, `StopPayload`, `UserPromptSubmitPayload`
  - `StatuslineCurrentUsage`, `StatuslineContextWindow`, `StatuslineCost`, `StatuslineRateLimitBucket`, `StatuslineRateLimits`, `StatuslineModelInfo`, `StatuslinePayload`
  - `HookEventRequest`, `StatuslineSnapshotRequest`
  - `StoredEvent`, `StoredSnapshot`, `StoredTask`
  - `EventResponse`, `TaskResponse`, `SnapshotResponse`, `SessionSummary`, `StatusResponse`, `HealthResponse`
- **Internal imports:** None.
- **External deps:** `pydantic` (BaseModel, Field, field_validator), `typing` (Any, Optional), `time`.

### 9.6 `src/context_analyzer_tool/collector/server.py`

- **Contains:**
  - `lifespan(app: FastAPI) -> AsyncGenerator[None, None]` -- async context manager.
  - `create_app() -> FastAPI` -- app factory.
- **Internal imports:** `config.load_config`, `config.get_db_path`, `db.schema.open_db`, `db.schema.run_migrations`, `collector.routes.hook_router`, `collector.routes.api_router`, `collector.delta_engine.restore_sessions_from_db`.
- **External deps:** `fastapi` (FastAPI), `contextlib` (asynccontextmanager), `typing` (AsyncGenerator), `time`, `logging`.

### 9.7 `src/context_analyzer_tool/collector/routes.py`

- **Contains:**
  - `hook_router = APIRouter()`, `api_router = APIRouter()`
  - Dependency functions: `get_db()`, `get_sessions()`, `get_config()`.
  - Route handlers: `receive_hook_event()`, `receive_statusline_snapshot()`, `get_status()`, `get_health()`, `get_session_events()`, `get_session_tasks()`, `get_session_snapshots()`.
- **Internal imports:** `collector.models` (all request/response models), `collector.delta_engine` (on_tool_use, on_snapshot, on_session_stop), `db.events` (insert_event, insert_snapshot, get_recent_events, get_event_count, get_snapshot_count, get_latest_snapshot, get_active_session_ids), `db.tasks` (insert_task, get_recent_tasks).
- **External deps:** `fastapi` (APIRouter, Depends, Request), `aiosqlite` (Connection type), `logging`, `json`, `time`.

### 9.8 `src/context_analyzer_tool/collector/delta_engine.py`

- **Contains:**
  - `PendingToolCall` dataclass.
  - `SessionState` dataclass.
  - `on_tool_use()`, `on_snapshot()`, `on_session_stop()`, `cleanup_stale_sessions()`, `restore_sessions_from_db()`.
- **Internal imports:** `db.tasks` (insert_task, update_task_delta), `db.events` (get_latest_snapshot, get_active_session_ids).
- **External deps:** `aiosqlite`, `dataclasses`, `collections` (deque), `typing`, `logging`, `time`.

### 9.9 `src/context_analyzer_tool/db/__init__.py`

- **Contains:** Empty or re-exports.
- **Internal imports:** None.
- **External deps:** None.

### 9.10 `src/context_analyzer_tool/db/schema.py`

- **Contains:**
  - `MIGRATIONS` list.
  - `open_db(db_path: str) -> aiosqlite.Connection`
  - `run_migrations(db: aiosqlite.Connection) -> None`
  - `_apply_v1(db: aiosqlite.Connection) -> None`
  - `get_schema_version(db: aiosqlite.Connection) -> int`
- **Internal imports:** None.
- **External deps:** `aiosqlite`, `logging`.

### 9.11 `src/context_analyzer_tool/db/events.py`

- **Contains:**
  - `insert_event()`, `insert_snapshot()`, `get_recent_events()`, `get_latest_snapshot()`, `get_recent_snapshots()`, `get_active_session_ids()`, `get_event_count()`, `get_snapshot_count()`.
- **Internal imports:** None.
- **External deps:** `aiosqlite`, `typing`, `json`.

### 9.12 `src/context_analyzer_tool/db/tasks.py`

- **Contains:**
  - `insert_task()`, `update_task_delta()`, `get_recent_tasks()`, `get_tasks_by_type()`, `get_null_delta_tasks()`.
- **Internal imports:** None.
- **External deps:** `aiosqlite`, `typing`.

### 9.13 `hooks/post_tool_use.py`

- **Contains:** `build_envelope()`, `main()`.
- **Internal imports:** None (standalone script).
- **External deps:** `httpx`, `json`, `sys`, `time`. Declared via PEP 723 inline metadata.

### 9.14 `hooks/subagent_stop.py`

- **Contains:** `build_envelope()`, `main()`.
- **Internal imports:** None.
- **External deps:** `httpx`, `json`, `sys`, `time`.

### 9.15 `hooks/stop.py`

- **Contains:** `build_envelope()`, `main()`.
- **Internal imports:** None.
- **External deps:** `httpx`, `json`, `sys`, `time`.

### 9.16 `hooks/user_prompt_submit.py`

- **Contains:** `build_envelope()`, `main()`.
- **Internal imports:** None.
- **External deps:** `httpx`, `json`, `sys`, `time`.

### 9.17 `hooks/statusline.py`

- **Contains:** `post_snapshot()`, `format_statusline()`, `main()`.
- **Internal imports:** None.
- **External deps:** `httpx`, `json`, `sys`, `time`.

### 9.18 `tests/test_delta_engine.py`

- **Contains:** Unit tests for the delta engine correlation logic.
  - `test_first_snapshot_no_delta()`
  - `test_single_tool_call_delta()`
  - `test_multiple_pending_tools_last_gets_delta()`
  - `test_negative_delta_compaction()`
  - `test_zero_delta()`
  - `test_snapshot_no_pending_tools()`
  - `test_session_isolation()`
  - `test_stale_session_cleanup()`
  - `test_session_restore_from_db()`
- **Internal imports:** `collector.delta_engine`, `collector.models`, `db.events`, `db.tasks`, `db.schema`.
- **External deps:** `pytest`, `pytest-asyncio`, `aiosqlite`.

### 9.19 `tests/test_routes.py`

- **Contains:** Integration tests for HTTP routes.
  - `test_post_hook_event()`
  - `test_post_statusline_snapshot()`
  - `test_get_status()`
  - `test_get_health()`
  - `test_invalid_event_type()`
  - `test_collector_resilience_on_error()`
- **Internal imports:** `collector.server.create_app`.
- **External deps:** `pytest`, `pytest-asyncio`, `httpx` (AsyncClient), `fastapi.testclient` or ASGI transport.

### 9.20 `tests/test_db.py`

- **Contains:** Unit tests for database CRUD operations.
  - `test_insert_and_get_event()`
  - `test_insert_and_get_snapshot()`
  - `test_insert_and_update_task()`
  - `test_schema_migration()`
  - `test_wal_mode_enabled()`
- **Internal imports:** `db.schema`, `db.events`, `db.tasks`.
- **External deps:** `pytest`, `pytest-asyncio`, `aiosqlite`.

### 9.21 `tests/test_config.py`

- **Contains:** Unit tests for config loading.
  - `test_default_config()`
  - `test_load_from_toml()`
  - `test_missing_config_uses_defaults()`
  - `test_env_var_override()`
  - `test_db_path_expansion()`
- **Internal imports:** `config`.
- **External deps:** `pytest`, `tmp_path` fixture.

### 9.22 `tests/conftest.py`

- **Contains:** Shared pytest fixtures.
  - `db_connection` -- in-memory aiosqlite connection with migrations applied.
  - `app_client` -- ASGI test client for the FastAPI app.
  - `sample_hook_event` -- factory for HookEventRequest.
  - `sample_snapshot` -- factory for StatuslineSnapshotRequest.
- **Internal imports:** `db.schema`, `collector.server`, `collector.models`.
- **External deps:** `pytest`, `pytest-asyncio`, `aiosqlite`, `httpx`.

### 9.23 `pyproject.toml`

- **Contains:** Project metadata, dependencies, scripts entry point.
- **Key sections:**

```toml
[project]
name = "context-analyzer-tool"
version = "0.1.0"
description = "Per-task token attribution and anomaly detection for Claude Code"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "aiosqlite>=0.21.0",
    "typer>=0.15.0",
    "rich>=13.9.0",
    "httpx>=0.28.0",
    "pydantic>=2.10.0",
]

[project.scripts]
context-analyzer-tool = "context_analyzer_tool.cli:app"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
    "ruff>=0.8.0",
    "pyright>=1.1.390",
]

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "ANN", "B", "A", "SIM", "TCH"]

[tool.pyright]
pythonVersion = "3.11"
typeCheckingMode = "strict"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

## 10. Edge Cases and Risk Registry

### 10.1 Data Collection Risks

| # | Edge Case | Severity | Handling |
|---|---|---|---|
| E1 | **Collector not running when hook fires** | Medium | Hook scripts catch `httpx.ConnectError`, log nothing, exit 0. Events are silently lost. Mitigation: `context-analyzer-tool install --check` warns if collector is down. Future: hook scripts can write to a local spool file (`~/.context-analyzer-tool/spool/`) and the collector drains it on startup. Phase 1 does NOT implement spooling. |
| E2 | **Statusline script fails to POST** | Medium | Same as E1. The statusline still outputs to stdout (Claude Code sees a status). Only the snapshot is lost. Token deltas for tool calls between the lost snapshot and the next one will be aggregated into a single larger delta. |
| E3 | **Hook script exceeds timeout (5s)** | Low | Claude Code kills the process. `httpx` timeout is 2s, so this should not happen unless `uv run` is slow on first invocation (dependency resolution). Mitigation: run `uv run hooks/post_tool_use.py < /dev/null` once during install to pre-cache deps. |
| E4 | **Multiple Claude Code instances race on settings.json** | Low | The install command uses a file lock (portalocker or fcntl) when writing settings.json. If locking is not available, it reads-modifies-writes with a retry on conflict. |
| E5 | **SQLite write contention from N sessions** | Low | WAL mode + busy_timeout=5000 handles this. If more than ~50 concurrent sessions write simultaneously, some may see delays. This is far beyond expected use (typically 1-5 concurrent sessions). |

### 10.2 Token Delta Risks

| # | Edge Case | Severity | Handling |
|---|---|---|---|
| E6 | **Context compaction produces negative delta** | High | Compaction happens at ~83% context usage. The entire conversation is summarized and tokens drop. Set `is_compaction=True`. Do NOT feed to anomaly detector. Log the compaction event. The task row stores the negative delta for display purposes. |
| E7 | **Collector restarts mid-session** | Medium | In-memory session state is lost. On startup, `restore_sessions_from_db()` loads last snapshot per active session. Pending tool calls from before crash are orphaned (keep `token_delta=NULL`). The next snapshot after restart creates a delta that includes all activity during downtime -- this is a known inaccuracy, acceptable for Phase 1. |
| E8 | **Session ID reuse** | Low | Claude Code session IDs are UUIDs, so reuse is effectively impossible. If it somehow happens, the session state is simply continued. No harm. |
| E9 | **Tool calls with no subsequent snapshot (session ends abruptly)** | Medium | Background cleanup (every 60s) finds tasks where `token_delta IS NULL AND timestamp_ms < now - 30000`. These remain NULL forever. The status display shows them as "pending" or "-". |
| E10 | **Interleaved tool calls (Claude chains 3 tools before responding)** | Medium | All 3 tool calls are pending. When the snapshot arrives, the full delta is assigned to the LAST tool call. Earlier calls get delta=0. This is a simplification -- the true per-tool cost is unknowable without per-turn token counts that Claude Code does not expose. Document this limitation. |
| E11 | **Statusline fires but no tool call happened (pure text response)** | Low | Snapshot is recorded, session state updated. No task row affected. This advances the baseline pointer so the next tool call gets an accurate delta. |

### 10.3 Platform and Environment Risks

| # | Edge Case | Severity | Handling |
|---|---|---|---|
| E12 | **Windows path handling** | Medium | All paths use `pathlib.Path` which handles OS-specific separators. Hook command paths in settings.json use forward slashes (Claude Code on Windows supports this). The `~` in `db_path` is expanded via `Path.expanduser()`. |
| E13 | **uv not installed** | High | The install command checks for `uv` in PATH. If missing, prints instructions: `pip install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`. Hooks will not work without `uv`. |
| E14 | **Python < 3.11** | Medium | `pyproject.toml` declares `requires-python = ">=3.11"`. `uv` enforces this. Hook scripts declare `requires-python = ">=3.11"` via PEP 723. |
| E15 | **Disk full (SQLite write fails)** | Low | All DB writes are wrapped in try/except. On write failure, the route returns 202 anyway (hook must not fail), logs an error. A persistent disk-full state is surfaced via `GET /health` which tries a test write. |
| E16 | **Port 7821 already in use** | Medium | `uvicorn.run()` raises `OSError`. The `serve` command catches this and prints a clear message: "Port 7821 is in use. Is another context-analyzer-tool instance running? Use --port to specify a different port." |

### 10.4 Data Integrity Risks

| # | Edge Case | Severity | Handling |
|---|---|---|---|
| E17 | **Malformed JSON from hook stdin** | Low | Hook scripts wrap `json.loads()` in try/except. On parse error, exit 0 silently. The collector also validates with Pydantic; malformed requests return 422 but hooks never see this (they fire-and-forget). |
| E18 | **Very large tool_response in PostToolUse** | Medium | The hook script does NOT include `tool_response` in the POST payload (only `tool_input_summary`, truncated to 500 chars). The full payload is stored in `payload_json` but the `tool_response` field is stripped or truncated to 1000 chars before storage. |
| E19 | **SQLite database corruption** | Low | WAL mode is resilient. If corruption is detected (sqlite3.DatabaseError on open), the `open_db` function logs the error, renames the corrupt file to `*.corrupt.{timestamp}`, and creates a fresh database. All historical data is lost but operation continues. |
| E20 | **Clock skew between hook and statusline timestamps** | Low | Both hook scripts and statusline script generate their own `timestamp_ms` using `time.time()`. Since they run on the same machine, skew is negligible (<1ms). If system clock jumps, correlation may be temporarily wrong. No mitigation needed. |
| E21 | **Concurrent writes to same task row** | Low | Only one code path writes to a given task row: `on_snapshot()` which runs sequentially per session (FastAPI processes one request at a time per session_id due to the in-memory dict). No row-level locking needed. |

### 10.5 Operational Risks

| # | Edge Case | Severity | Handling |
|---|---|---|---|
| E22 | **Database grows unbounded** | Medium | Phase 1 does not implement cleanup. Estimated growth: ~5MB/month of heavy use. A future `context-analyzer-tool gc` command will delete events older than a configurable retention period (default 30 days). For Phase 1, document that users can delete the DB file to reset. |
| E23 | **Memory leak from sessions dict** | Low | `cleanup_stale_sessions()` runs every 60s and evicts sessions idle for >1 hour. Each SessionState is ~200 bytes. Even 1000 sessions would be 200KB. Not a concern. |
| E24 | **First-run uv dependency resolution slow** | Medium | First `uv run` of a hook script downloads `httpx` and its deps. This can take 2-5s, potentially exceeding Claude Code's 5s hook timeout. Mitigation: `context-analyzer-tool install` runs each hook script once with empty stdin to pre-warm the `uv` cache. |
| E25 | **User has existing hooks in settings.json** | Medium | The install command merges, not overwrites. It reads the existing hooks arrays and appends context-analyzer-tool entries. If a context-analyzer-tool entry already exists (detected by checking if the command contains "context-analyzer-tool"), it is updated in place. |

---

## Appendix A: Project File Structure (Phase 1)

```
context-analyzer-tool/
├── pyproject.toml
├── CLAUDE.md
├── .gitattributes
├── src/
│   └── context_analyzer_tool/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── collector/
│       │   ├── __init__.py
│       │   ├── server.py
│       │   ├── routes.py
│       │   ├── models.py
│       │   └── delta_engine.py
│       └── db/
│           ├── __init__.py
│           ├── schema.py
│           ├── events.py
│           └── tasks.py
├── hooks/
│   ├── post_tool_use.py
│   ├── subagent_stop.py
│   ├── stop.py
│   ├── user_prompt_submit.py
│   └── statusline.py
├── tests/
│   ├── conftest.py
│   ├── test_delta_engine.py
│   ├── test_routes.py
│   ├── test_db.py
│   └── test_config.py
└── docs/
    └── phase1-architecture.md (this file)
```

Files NOT included in Phase 1 (deferred):
- `src/context_analyzer_tool/db/baselines.py` -- Phase 2
- `src/context_analyzer_tool/db/anomalies.py` -- Phase 2
- `src/context_analyzer_tool/engine/` -- Phase 2
- `src/context_analyzer_tool/notify/` -- Phase 3
- `src/context_analyzer_tool/dashboard/` -- Phase 4/5

---

## Appendix B: Sequence Diagram -- Full Request Flow

```
User types prompt in Claude Code
    │
    ├──> UserPromptSubmit hook fires
    │       └── POST /hook/event {event_type: "UserPromptSubmit", prompt_preview: "..."}
    │              └── Insert events row. No task row.
    │
Claude thinks, calls Bash tool
    │
    ├──> PostToolUse hook fires
    │       └── POST /hook/event {event_type: "PostToolUse", tool_name: "Bash", ...}
    │              ├── Insert events row -> event_id=42
    │              ├── Insert tasks row (delta=NULL, snapshot_before_id=prev) -> task_id=17
    │              └── Append PendingToolCall(event_id=42, task_id=17) to session
    │
Claude calls Read tool
    │
    ├──> PostToolUse hook fires
    │       └── POST /hook/event {event_type: "PostToolUse", tool_name: "Read", ...}
    │              ├── Insert events row -> event_id=43
    │              ├── Insert tasks row (delta=NULL) -> task_id=18
    │              └── Append PendingToolCall(event_id=43, task_id=18) to session
    │
Claude produces final text response
    │
    ├──> Statusline script fires (receives full token data on stdin)
    │       ├── POST /hook/statusline {total_input_tokens: 15234, ...}
    │       │      ├── Insert token_snapshots row -> snapshot_id=9
    │       │      ├── Compute delta = 15234 - 12800 (prev) = 2434
    │       │      ├── pending_tool_calls = [task_id=17 (Bash), task_id=18 (Read)]
    │       │      ├── UPDATE tasks SET token_delta=0 WHERE id=17     (earlier gets 0)
    │       │      ├── UPDATE tasks SET token_delta=2434 WHERE id=18  (last gets full delta)
    │       │      ├── Clear pending_tool_calls
    │       │      └── Update session.last_snapshot_* = snapshot_id=9, 15234
    │       │
    │       └── Print statusline to stdout:
    │           "Opus | ctx 8% █░░░░░░░░░ | $0.01 | 5h: 24% | 7d: 41%"
    │
User sees statusline in Claude Code footer
```

---

## Appendix C: HTTP Hook Mode

When installed with `--use-http`, PostToolUse hooks bypass the Python script entirely:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://127.0.0.1:7821/hook/raw/post-tool-use",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

This requires an additional route on the collector:

```python
@hook_router.post("/raw/post-tool-use", status_code=202)
async def receive_raw_post_tool_use(
    request: Request,
    db: Connection = Depends(get_db),
    sessions: dict = Depends(get_sessions),
) -> dict[str, str]:
    """
    Receives the raw PostToolUse payload directly from Claude Code
    (no hook script intermediary). Builds the HookEventRequest internally.
    """
    body = await request.json()
    # Build envelope from raw payload
    event = HookEventRequest(
        event_type="PostToolUse",
        session_id=body["session_id"],
        timestamp_ms=int(time.time() * 1000),
        payload=body,
        tool_name=body.get("tool_name"),
        tool_input_summary=json.dumps(body.get("tool_input", {}))[:500],
        cwd=body.get("cwd"),
    )
    # ... same logic as receive_hook_event
```

Similarly for the other 3 hook types: `/raw/subagent-stop`, `/raw/stop`, `/raw/user-prompt-submit`.

**Tradeoff:** HTTP hooks are faster (no Python process spawn) but require the collector to be running. Command hooks are resilient to collector downtime (they just fail silently). Default install uses command hooks.

The statusline always uses command mode because Claude Code statusline must output to stdout.

---

*End of Phase 1 Architecture Specification.*
