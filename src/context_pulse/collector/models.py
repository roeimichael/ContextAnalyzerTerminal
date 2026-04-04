from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PostToolUsePayload(BaseModel):
    session_id: str
    transcript_path: str
    cwd: str
    permission_mode: str
    hook_event_name: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_response: dict[str, Any]
    tool_use_id: str


class SubagentStopPayload(BaseModel):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    stop_hook_active: bool
    agent_id: str
    agent_type: str
    agent_transcript_path: str
    last_assistant_message: str


class StopPayload(BaseModel):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    stop_hook_active: bool
    last_assistant_message: str


class UserPromptSubmitPayload(BaseModel):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    prompt: str


class StatuslineCurrentUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class StatuslineContextWindow(BaseModel):
    total_input_tokens: int
    total_output_tokens: int
    context_window_size: int
    used_percentage: int
    remaining_percentage: int
    current_usage: StatuslineCurrentUsage


class StatuslineCost(BaseModel):
    total_cost_usd: float
    total_duration_ms: int
    total_api_duration_ms: int
    total_lines_added: int
    total_lines_removed: int


class StatuslineRateLimitBucket(BaseModel):
    used_percentage: float
    resets_at: int


class StatuslineRateLimits(BaseModel):
    five_hour: StatuslineRateLimitBucket
    seven_day: StatuslineRateLimitBucket


class StatuslineModelInfo(BaseModel):
    id: str
    display_name: str


class StatuslinePayload(BaseModel):
    session_id: str
    transcript_path: str
    model: StatuslineModelInfo
    cost: StatuslineCost
    context_window: StatuslineContextWindow
    rate_limits: StatuslineRateLimits
    version: str


class HookEventRequest(BaseModel):
    event_type: str
    session_id: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    payload: dict[str, Any]
    tool_name: str | None = None
    tool_input_summary: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    prompt_preview: str | None = None
    cwd: str | None = None
    estimated_tokens: int | None = None
    estimated_input_tokens: int | None = None
    estimated_response_tokens: int | None = None

    @field_validator("tool_input_summary")
    @classmethod
    def truncate_tool_input(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 500:
            return v[:497] + "..."
        return v

    @field_validator("prompt_preview")
    @classmethod
    def truncate_prompt(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 200:
            return v[:197] + "..."
        return v


class StatuslineSnapshotRequest(BaseModel):
    session_id: str
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    context_window_size: int = 0
    used_percentage: float = 0.0
    remaining_percentage: float = 0.0
    total_cost_usd: float = 0.0
    total_duration_ms: int = 0
    model_id: str = "unknown"
    model_display_name: str = "Unknown"
    rate_limit_five_hour_pct: float = 0.0
    rate_limit_seven_day_pct: float = 0.0
    version: str = "unknown"


class StoredEvent(BaseModel):
    id: int | None = None
    session_id: str
    agent_id: str | None = None
    event_type: str
    tool_name: str | None = None
    tool_input_summary: str | None = None
    cwd: str | None = None
    timestamp_ms: int
    payload_json: str


class StoredSnapshot(BaseModel):
    id: int | None = None
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
    id: int | None = None
    session_id: str
    event_id: int
    task_type: str
    token_delta: int | None = None
    is_compaction: bool = False
    snapshot_before_id: int | None = None
    snapshot_after_id: int | None = None
    timestamp_ms: int
    anomaly_id: int | None = None


class EventResponse(BaseModel):
    id: int
    session_id: str
    agent_id: str | None
    event_type: str
    tool_name: str | None
    tool_input_summary: str | None
    cwd: str | None
    timestamp_ms: int


class TaskResponse(BaseModel):
    id: int
    session_id: str
    task_type: str
    token_delta: int | None
    estimated_tokens: int | None
    is_compaction: bool
    timestamp_ms: int
    anomaly_id: int | None


class SnapshotResponse(BaseModel):
    id: int
    session_id: str
    timestamp_ms: int
    total_input_tokens: int
    total_output_tokens: int
    used_percentage: int
    model_id: str


class SessionSummary(BaseModel):
    session_id: str
    project_name: str | None = None
    event_count: int
    first_event_ms: int
    last_event_ms: int
    total_tokens_used: int | None
    used_percentage: int | None
    model_id: str | None


class StatusResponse(BaseModel):
    active_sessions: list[SessionSummary]
    recent_events: list[EventResponse]
    recent_tasks: list[TaskResponse]


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    db_path: str
    event_count: int
    snapshot_count: int


class BaselineSnapshot(BaseModel):
    task_type: str
    mean: float
    stddev: float
    sample_count: int
    updated_at: int


class AnomalyResult(BaseModel):
    task_id: int
    session_id: str
    task_type: str
    token_delta: int
    z_score: float
    baseline_mean: float
    baseline_stddev: float
    baseline_sample_count: int
    timestamp_ms: int


class ClassifierResponse(BaseModel):
    cause: str
    severity: str
    suggestion: str


class AnomalyResponse(BaseModel):
    id: int
    session_id: str
    task_type: str
    token_cost: int
    z_score: float
    cause: str | None
    severity: str | None
    suggestion: str | None
    notified: bool
    timestamp_ms: int


class AnomaliesListResponse(BaseModel):
    anomalies: list[AnomalyResponse]
    total_count: int
