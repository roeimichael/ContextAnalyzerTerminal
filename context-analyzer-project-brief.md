# context-pulse — Project Brief & Claude Code Execution Prompt

> Feed this file to Claude Code as your initial prompt. It contains the full execution plan, architecture, cost model, problem framing, and design constraints. Claude Code should use it to scaffold the project structure, generate a CLAUDE.md, and begin implementation in prioritized phases.

---

## 1. Problem Statement

Claude Code users — especially those running multi-agent workflows — have no way to understand *why* their context window spiked. Existing tools (ccusage, Claude-Code-Usage-Monitor, Claude HUD) tell you *how much* context you've used. None of them tell you *which task caused it*, *why it was expensive*, or *how to avoid it next time*.

### Pain points this tool addresses

- A single subagent task silently consumes 30–40% of the session without warning.
- Multi-agent setups (Dev + Ops agents, parallel worktrees) make attribution impossible — you can't tell which session or agent caused the drain.
- Users have no historical baseline to detect "this task cost 5× more than usual."
- No tool currently classifies root causes (large file scan, unconstrained web fetch, compounding conversation history, binary read, recursive grep) or notifies users proactively.

### What the tool does that nothing else does

1. Tracks token deltas **per tool call**, not just per session.
2. Builds a **rolling baseline** per task type across sessions.
3. Fires an **anomaly alert** when a single action exceeds its expected cost by a configurable threshold.
4. Calls a **cheap LLM classifier** (Haiku) to explain *why* in plain language.
5. Delivers **proactive notifications** (statusline + system notification + optional Slack/webhook).
6. Renders a **lightweight dashboard** showing per-task cost history and anomaly events.
7. Works across **multiple concurrent Claude Code sessions** by correlating session IDs from hook payloads.

---

## 2. Market Context (validated)

| Tool | What it does | Gap |
|---|---|---|
| ccusage | Parses JSONL → daily/session reports | No real-time, no per-task, no intelligence |
| Claude-Code-Usage-Monitor | Burn rate + TUI predictions | Session-level only, no attribution |
| Claude HUD | Live ctx% + active agents display | Display only, no analysis or alerts |
| claude-warden | Hook-based token guards, blocks waste | Prevention not explanation, no root cause |
| disler observability | Hook events → SQLite → Vue dashboard | Raw events, no anomaly layer |
| claude-code-otel | OTel + Grafana for teams | Enterprise infra, no individual intelligence |

**The gap**: no tool does per-task attribution + anomaly detection + root cause classification. A GitHub issue on the Claude Code repo explicitly requests this capability (issue #10388). The community has the pain, nobody has the solution.

---

## 3. Architecture

### Data flow

```
Claude Code sessions (1..N)
    │
    ├── Hook: PostToolUse      → { tool_name, tool_input, token_delta, session_id, timestamp }
    ├── Hook: UserPromptSubmit → { prompt_preview, session_id, timestamp }
    ├── Hook: SubagentStop     → { agent_id, usage.input_tokens, usage.output_tokens, duration_ms }
    ├── Hook: Stop             → { session_id, total_tokens }
    └── Statusline script      → current_usage.input_tokens (real-time delta)
              │
              ▼
    [Collector] — FastAPI local server (localhost:7821)
              │
              ├── SQLite DB (context_pulse.db)
              │     ├── events table       (raw hook events)
              │     ├── tasks table        (per-tool-call token deltas)
              │     ├── baselines table    (rolling mean + stddev per task_type)
              │     └── anomalies table    (detected anomalies + classifications)
              │
              ├── Anomaly Engine
              │     ├── Z-score over rolling 20-session window per task_type
              │     ├── Threshold: z > 2.0 → anomaly candidate
              │     └── On anomaly → call classifier
              │
              ├── Classifier (Haiku, single call, ~$0.0001/event)
              │     ├── Input: tool_name, tool_input_summary, token_delta, baseline
              │     └── Output: { cause: str, severity: low|medium|high, suggestion: str }
              │
              └── Notifier
                    ├── Statusline injection (⚠ badge + cause summary)
                    ├── System notification (macOS: osascript, Linux: notify-send)
                    └── Optional: webhook (Slack, Discord, custom)
              │
              ▼
    [Dashboard] — React + Vite (localhost:7822) or TUI (Rich)
          ├── Live session panel  (current context %, burn rate, active tasks)
          ├── Task cost timeline  (bar chart: token cost per tool call in session)
          ├── Anomaly feed        (list of anomalies with root cause and suggestion)
          ├── Baseline explorer   (per task_type: avg cost, p95, worst offender)
          └── Multi-session view  (all active sessions side by side)
```

### Component breakdown

**Collector (FastAPI, Python)**
- Single endpoint: `POST /event` receiving hook JSON
- Writes to SQLite with WAL mode for concurrent session writes
- Computes token delta from `current_usage` diff between consecutive PostToolUse events
- Background task: re-computes baseline after every 5 new events for a given task_type

**Anomaly Engine (Python, same process)**
- Runs as a background scheduler (APScheduler, every 5s) or triggered on write
- Per task_type: maintain (mean, stddev) from last 20 sessions using Welford's online algorithm
- Threshold configurable in `~/.context-pulse/config.toml`
- Deduplication: don't re-alert same anomaly within 60s

**Classifier (Haiku)**
- Called only on confirmed anomalies (z > threshold)
- System prompt: classify the root cause of a Claude Code token spike given tool metadata
- Max tokens: 150. Temperature: 0. Cost per call: ~$0.0001
- Response cached per (tool_name, token_delta_bucket) to avoid repeated Haiku calls for same pattern

**Hook scripts**
- Language: Python (using `uv run` for zero-install)
- Installed to `.claude/hooks/` in user's global Claude config (`~/.claude/`)
- Each script reads JSON from stdin, POSTs to collector, exits 0 (non-blocking)
- Async POST with 2s timeout so hooks never delay Claude

**Dashboard (two modes)**
- Mode A (TUI): Rich-based terminal dashboard, runs in a split pane alongside Claude Code
- Mode B (Web): React + Vite SPA, minimal dependencies, served locally
- Default: TUI. Web dashboard launched with `context-pulse --web`

**MCP server (Phase 2)**
- Expose collector data as MCP tools: `get_session_summary`, `get_anomalies`, `get_baseline_for_task`
- Lets Claude Code itself query its own context health in-session
- Distribution path: publish to MCP marketplace

---

## 4. Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Collector | FastAPI + uvicorn | Fast async, you know it well, minimal overhead |
| Database | SQLite (WAL mode) | Zero infra, handles concurrent writes from N sessions |
| Anomaly engine | Pure Python (math only) | No ML deps, Welford's algorithm is 10 lines |
| Classifier | Anthropic SDK → Haiku | Cheapest Claude model, single-turn, ~$0.0001/call |
| Hook scripts | Python via `uv run` | Zero install friction for users |
| TUI dashboard | Rich | Already in your stack, looks great |
| Web dashboard | React + Vite + Recharts | Simple, fast, no heavy framework |
| Config | TOML (`~/.context-pulse/config.toml`) | Human-readable, standard for CLI tools |
| Package manager | uv | Already your default |
| Linter/formatter | ruff | Already your default |
| Type checking | pyright | Already your default |
| MCP server | FastMCP 3.0 | Already your primary MCP stack |

---

## 5. Cost Model

### To run the tool (user cost)

| Component | Cost |
|---|---|
| Collector + anomaly engine | $0 (local Python process, ~30MB RAM) |
| Haiku classifier calls | ~$0.0001 per anomaly event |
| Estimated anomalies per heavy session | 3–10 |
| **Total LLM cost per day of heavy use** | **~$0.001 — effectively free** |
| Dashboard (TUI or web) | $0 (local) |
| SQLite storage | <5MB per month of sessions |

The entire intelligence layer costs less than a fraction of a cent per day. This is a key selling point — zero meaningful overhead.

### To build the tool (your time)

| Phase | Estimated effort |
|---|---|
| Phase 1: Collector + hooks + SQLite | 3–5 days |
| Phase 2: Anomaly engine + Haiku classifier | 2–3 days |
| Phase 3: TUI dashboard | 2–3 days |
| Phase 4: Notifier + statusline integration | 1–2 days |
| Phase 5: Bug fixes + hardening | 2–3 days |
| Phase 6: MCP server | 2–3 days |
| Phase 7: Packaging (uv tool, PyPI) | 1–2 days |
| **Total** | **~3 weeks part-time** |

---

## 6. Data Model (SQLite)

```sql
-- Raw hook events (append-only log)
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    agent_id    TEXT,                          -- null for main agent
    event_type  TEXT NOT NULL,                 -- PostToolUse, SubagentStop, etc.
    tool_name   TEXT,
    tool_input  TEXT,                          -- JSON, truncated to 500 chars
    token_delta INTEGER,                       -- tokens consumed by this event
    timestamp   INTEGER NOT NULL               -- unix ms
);

-- Aggregated per-task-call records (derived from events)
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    task_type   TEXT NOT NULL,                 -- e.g. "Bash", "Read", "WebFetch", "Task(Explore)"
    token_cost  INTEGER NOT NULL,
    duration_ms INTEGER,
    timestamp   INTEGER NOT NULL,
    anomaly_id  INTEGER REFERENCES anomalies(id)
);

-- Rolling baselines per task_type
CREATE TABLE baselines (
    task_type   TEXT PRIMARY KEY,
    mean        REAL NOT NULL,
    stddev      REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

-- Detected anomalies with classifier output
CREATE TABLE anomalies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    token_cost  INTEGER NOT NULL,
    z_score     REAL NOT NULL,
    cause       TEXT,                          -- Haiku output
    severity    TEXT,                          -- low | medium | high
    suggestion  TEXT,                          -- Haiku output
    notified    INTEGER DEFAULT 0,             -- bool
    timestamp   INTEGER NOT NULL
);
```

---

## 7. Hook Payload Mapping

### PostToolUse (primary data source)

```json
{
  "session_id": "abc123",
  "tool_name": "Bash",
  "tool_input": { "command": "grep -r 'auth' . --include='*.py'" },
  "tool_response": "...",
  "context_window": {
    "current_usage": {
      "input_tokens": 4218,
      "output_tokens": 312,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 1800
    },
    "used_percentage": 42.1
  }
}
```

**Token delta computation**: store `prev_input_tokens` per session, delta = `current_usage.input_tokens - prev`. This is the exact cost of the last tool call.

### SubagentStop (subagent attribution)

```json
{
  "session_id": "abc123",
  "agent_id": "subagent-xyz",
  "usage": {
    "input_tokens": 8400,
    "output_tokens": 920
  },
  "total_cost_usd": 0.0041,
  "duration_ms": 14200
}
```

Subagents get their own task record attributed by `agent_id`. This is the multi-agent attribution layer no other tool provides.

---

## 8. Classifier Prompt (Haiku)

```
System: You are a terse analyst classifying why a Claude Code tool call consumed unusually many tokens.
Respond ONLY with valid JSON: {"cause": str, "severity": "low|medium|high", "suggestion": str}
cause: 1 sentence. suggestion: 1 actionable sentence. No other text.

User: Tool: {tool_name}
Input summary: {tool_input_summary}
Token cost: {token_cost} (baseline for this tool: {baseline_mean:.0f} ± {baseline_stddev:.0f}, z-score: {z_score:.1f})
```

Example output:
```json
{
  "cause": "Recursive grep across entire codebase returned 12,000 lines without a line limit flag.",
  "severity": "high",
  "suggestion": "Add --max-count=100 or scope grep to a specific subdirectory."
}
```

---

## 9. Notification Design

### Statusline (always-on)

Normal state:
```
claude-sonnet-4-6 | ctx 42% ████░░ | ⚡ 3 tasks | reset 2h 14m
```

Anomaly state (persists for 60s):
```
claude-sonnet-4-6 | ctx 71% ██████░ | ⚠ Bash 8.4k (4.2σ) | reset 1h 52m
```

### System notification (on anomaly)

```
⚠ context-pulse — High token spike
Bash used 8,400 tokens (4.2× baseline).
Cause: Recursive grep returned 12k lines.
Fix: Add --max-count=100
```

### In-session alert (injected as additionalContext via hook)

```
[context-pulse] ⚠ Last Bash command cost 8,400 tokens (4.2σ above your baseline of 2,000).
Cause: Recursive grep without output limits.
Consider: scope to a subdirectory or add --max-count before continuing.
```

This last one uses the `additionalContext` field in hook output so Claude itself sees the alert and can adjust its next action.

---

## 10. Configuration (`~/.context-pulse/config.toml`)

```toml
[collector]
port = 7821
db_path = "~/.context-pulse/events.db"

[anomaly]
z_score_threshold = 2.0        # alert when cost > mean + 2σ
min_sample_count = 5           # don't alert until we have 5 baseline samples
cooldown_seconds = 60          # don't re-alert same session within 60s
task_types_ignored = []        # e.g. ["Read"] to suppress Read alerts

[classifier]
enabled = true
model = "claude-haiku-4-5-20251001"
max_tokens = 150
cache_results = true

[notifications]
statusline = true
system_notification = true     # osascript on macOS, notify-send on Linux
in_session_alert = true        # injects additionalContext into hook output
webhook_url = ""               # optional Slack/Discord/custom

[dashboard]
default_mode = "tui"           # tui | web
web_port = 7822
```

---

## 11. Execution Plan (Phased)

### Phase 1 — Foundation (week 1)
**Goal**: Data collection working end-to-end.

- [ ] Scaffold project with `uv init context-pulse`
- [ ] Set up `pyproject.toml` with FastAPI, uvicorn, aiohttp, aiosqlite, ruff, pyright
- [ ] Implement SQLite schema + migration system
- [ ] Implement `POST /event` collector endpoint with async write
- [ ] Write hook scripts for PostToolUse, SubagentStop, Stop, UserPromptSubmit
- [ ] Write install script: copies hooks to `~/.claude/hooks/`, registers in `~/.claude/settings.json`
- [ ] Implement token delta computation (stateful per session_id)
- [ ] Smoke test: start Claude Code, do 5 tool calls, verify events in DB
- [ ] Write `context-pulse status` CLI command — dump last 10 events as table

**Deliverable**: You can see per-tool token costs in your terminal within 1 week.

### Phase 2 — Intelligence (week 2)
**Goal**: Anomaly detection + classifier working.

- [ ] Implement Welford's online algorithm for baseline computation
- [ ] Implement background baseline updater (runs after every 5 new task records)
- [ ] Implement Z-score anomaly detector (runs on every new task record)
- [ ] Integrate Anthropic SDK → Haiku classifier call
- [ ] Implement classifier response cache (SQLite-backed)
- [ ] Write `context-pulse anomalies` CLI command — list recent anomalies with causes
- [ ] Unit tests for anomaly engine (mock events, verify correct z-score + alert firing)

**Deliverable**: You get a terminal alert within seconds of a context spike.

### Phase 3 — Notifications (week 2–3)
**Goal**: Proactive alerts that don't require you to watch a dashboard.

- [ ] Implement statusline script (`~/.context-pulse/statusline.sh`)
- [ ] Register statusline in Claude Code settings
- [ ] Implement system notification (macOS: `osascript`, Linux: `notify-send`)
- [ ] Implement `additionalContext` injection in hook output for in-session alerts
- [ ] Add cooldown logic (don't spam)
- [ ] Add webhook notifier (generic POST, Slack-compatible payload)

**Deliverable**: You forget to check and still get told.

### Phase 4 — TUI Dashboard (week 3)
**Goal**: A beautiful terminal view of your context health.

- [ ] Implement Rich-based TUI with panels:
  - Current session: ctx%, burn rate, active tool, session timer
  - Task cost timeline: bar chart of last 20 tool calls by token cost
  - Anomaly feed: scrollable list with cause + suggestion
  - Multi-session view: one row per active Claude Code session
- [ ] Launch with `context-pulse dashboard` (or `cpd`)
- [ ] Auto-refresh every 2s from SQLite

**Deliverable**: Something you'd actually keep open in a tmux pane.

### Phase 5 — Bug Fixes + Hardening
**Goal**: Fix all known bugs so the core tool is reliable.

#### Must-fix (runtime / data correctness)
- [ ] **Privacy violation**: `post_tool_use.py` sends full `tool_response` in payload, stored verbatim in `events.payload_json` — strip `tool_response` and truncate `tool_input` before sending
- [ ] **`used_percentage` precision loss**: `StatuslineSnapshotRequest.used_percentage` is `float` but DB column is `INTEGER` — change schema to `REAL`
- [ ] **First-batch anomaly skip**: `receive_statusline_snapshot` captures `pending_list` before `on_snapshot` creates the session, so first resolved batch never gets anomaly-checked
- [ ] **Deprecated `datetime.fromtimestamp`**: `tui.py` line 101 calls without `tz` arg — add `tz=UTC` (already correct in `cli.py`)

#### Should-fix (incorrect behavior / fragility)
- [ ] **StatusLine overwrite**: `_merge_settings` in `cli.py` overwrites non-context-pulse statusLine despite docstring saying it won't — respect existing user statusLines
- [ ] **Hardcoded 200K context window**: `context_cost` CLI fallback uses 200,000 — should handle 1M context models
- [ ] **`zip` strict=False hides mismatches**: `delta_engine.py` line 419 — use `strict=True` to catch bugs
- [ ] **Redundant `db.row_factory` mutations**: Remove per-function `db.row_factory = aiosqlite.Row` since `open_db` already sets it
- [ ] **LIKE pattern injection**: `messages.py` `has_message_like` doesn't escape `%`/`_` wildcards
- [ ] **Sync calls in async route**: `rtk_integration.py` uses sync `subprocess.run` and `sqlite3.connect`, called from async `get_rtk_status` route — wrap in `asyncio.to_thread`

#### Style / cleanup
- [ ] Split `cli.py` (875 lines) into submodules
- [ ] Move inline SQL from `routes.py` `get_status` into `db/` layer
- [ ] Remove redundant re-import of `load_config` in `clear` command

**Deliverable**: All known bugs fixed, tests green, tool is production-ready.

### Phase 6 — MCP Server
**Goal**: Let Claude itself query context health mid-session.

- [ ] Wrap collector + DB in FastMCP 3.0 server
- [ ] Tools: `get_session_summary`, `get_anomalies`, `get_task_baseline`, `get_recommendations`
- [ ] Register in Claude Code settings as MCP server
- [ ] Publish to MCP marketplace

### Phase 7 — Packaging + Distribution
- [ ] `uv tool install context-pulse` — zero-dependency install
- [ ] Publish to PyPI
- [ ] Write README with 60-second quick-start
- [ ] GitHub Actions: lint + test on push

---

## 12. Key Considerations and Risks

### Privacy
- Hook payloads contain tool inputs which may include code, file paths, or prompts.
- `tool_input` should be truncated to 500 chars before storing. Never store full file contents.
- All data stays local (SQLite). No telemetry, no cloud.
- Document this clearly — it's a competitive advantage over gateway-based tools.

### Performance
- Hook scripts must exit within 2s or Claude will timeout.
- Use `async` POST to collector. If collector is down, log to file and exit 0 — never block Claude.
- Classifier call is out-of-band (not in the hook path). Anomaly detection on write takes <1ms.
- SQLite WAL mode handles concurrent writes from N sessions without locking.

### Cold start
- Anomaly engine needs minimum 5 samples per task_type before it's useful.
- First-run mode: collect only, no alerts. Show "learning mode" in statusline.
- Optionally: ship a seed baseline (population averages from community data).

### Multi-session correlation
- Each Claude Code session has a unique `session_id` in hook payloads.
- Subagents have their own `session_id` — treat them as child sessions.
- Parent-child relationship inferred from `agent_id` + timing proximity.

### Baseline drift
- User's task costs naturally shift as they change projects or coding patterns.
- Use exponentially weighted moving average (EWMA) for baselines — older data decays.
- Config option: `baseline_window_sessions = 20` (how many sessions to include).

### Windows compatibility
- `notify-send` doesn't exist on Windows. Use `win10toast` or skip system notifications.
- PowerShell hook scripts as alternative to bash. Claude Code docs cover this.
- SQLite works everywhere. FastAPI works everywhere.

### LLM plan compatibility
- The tool works without the Haiku classifier if `classifier.enabled = false`.
- Token delta tracking, anomaly detection, and notifications all work without any LLM calls.
- Users on restricted plans can run in "no-LLM" mode and still get meaningful alerts.

### Future: multi-LLM support
- Architecture is model-agnostic at the data layer. Only the hook scripts are Claude Code-specific.
- Phase 2 of product vision: adapter layer for Gemini CLI, Aider, Amp (all emit similar events).
- Keep the hook → collector interface generic from day one.

---

## 13. Project Structure

```
context-pulse/
├── pyproject.toml
├── CLAUDE.md                          # instructions for Claude Code agents
├── README.md
├── src/
│   └── context_pulse/
│       ├── __init__.py
│       ├── cli.py                     # entry point (typer)
│       ├── collector/
│       │   ├── server.py              # FastAPI app
│       │   ├── routes.py              # POST /event, GET /session, GET /anomalies
│       │   └── models.py              # Pydantic models for hook payloads
│       ├── db/
│       │   ├── schema.py              # SQLite schema + migrations
│       │   ├── events.py              # event CRUD
│       │   ├── tasks.py               # task CRUD + delta computation
│       │   ├── baselines.py           # baseline read/write + Welford
│       │   └── anomalies.py           # anomaly CRUD
│       ├── engine/
│       │   ├── anomaly.py             # Z-score detector
│       │   ├── baseline.py            # Welford's online algorithm
│       │   └── classifier.py          # Haiku call + cache
│       ├── notify/
│       │   ├── statusline.py          # statusline string generator
│       │   ├── system.py              # osascript / notify-send
│       │   ├── webhook.py             # generic webhook POST
│       │   └── session_alert.py       # additionalContext injector
│       ├── dashboard/
│       │   ├── tui.py                 # Rich TUI
│       │   └── web/                   # React + Vite (built output served by FastAPI)
│       └── config.py                  # TOML config loader
├── hooks/                             # hook scripts (installed to ~/.claude/hooks/)
│   ├── post_tool_use.py
│   ├── subagent_stop.py
│   ├── stop.py
│   └── user_prompt_submit.py
├── scripts/
│   ├── install.sh                     # copies hooks, patches settings.json
│   └── uninstall.sh
└── tests/
    ├── test_anomaly.py
    ├── test_baseline.py
    ├── test_classifier.py
    └── test_collector.py
```

---

## 14. CLAUDE.md (for this project's agents)

```markdown
# context-pulse — Agent Instructions

## Stack
- Python 3.12+, uv, ruff (linter+formatter), pyright (strict)
- FastAPI + uvicorn for collector server
- aiosqlite for async SQLite access
- Anthropic SDK for Haiku classifier
- Rich for TUI
- Typer for CLI

## Rules
- All async. No sync DB calls in request handlers.
- Every function has a type annotation. Pyright strict mode must pass.
- Ruff must pass before any commit.
- No print() — use logging with structured fields.
- Hook scripts must exit 0 on any error (never block Claude Code).
- Token delta computation is stateful per session_id — use an in-memory dict in the collector, not DB round-trips.
- Classifier calls are always async and never in the hook path.
- Test all business logic (anomaly engine, baseline, classifier) with unit tests. Do not test FastAPI routes without a specific request.

## Key invariants
- A token delta of 0 is valid (cached response). Do not filter it.
- Z-score is undefined until sample_count >= min_sample_count. Return None, not 0.
- Haiku classifier output must be valid JSON. Wrap parse in try/except, fall back to generic cause string.
- SQLite WAL mode must be set on first open. Never change journal mode after.

## Start here
1. Read src/context_pulse/db/schema.py to understand the data model.
2. Read src/context_pulse/engine/anomaly.py to understand the detection logic.
3. Check CURRENT_PHASE in this file before starting any task.

## Current phase
PHASE 5 — Bug Fixes + Hardening
Goal: Fix all known bugs so the core tool (Phases 1-4) is reliable and production-ready.
```

---

## 15. Suggested First Prompt to Claude Code

After placing this file in your project root, run Claude Code and use this as your opening prompt:

```
Read context-analyzer-project-brief.md fully before starting.

Your task is Phase 1 of the execution plan:
1. Scaffold the project using `uv init context-pulse` with the directory structure in section 13.
2. Set up pyproject.toml with the dependencies listed in section 11 Phase 1.
3. Implement the SQLite schema from section 6.
4. Implement the FastAPI collector with POST /event from section 3.
5. Write the four hook scripts (post_tool_use.py, subagent_stop.py, stop.py, user_prompt_submit.py).
6. Write install.sh that copies hooks to ~/.claude/hooks/ and patches ~/.claude/settings.json.
7. Implement the `context-pulse status` CLI command using Typer + Rich.
8. Write unit tests for token delta computation.

Use the CLAUDE.md in section 14 as your operational constraints.
Do not start Phase 2 until Phase 1 passes: install.sh runs cleanly, Claude Code generates 5 tool calls, and `context-pulse status` shows them in the terminal.
```
