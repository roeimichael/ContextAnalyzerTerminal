# CAT - Context Analyzer Terminal

[![CI](https://github.com/roeimichael/ContextAnalyzerTerminal/actions/workflows/ci.yml/badge.svg)](https://github.com/roeimichael/ContextAnalyzerTerminal/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**Per-tool-call context window analyzer for Claude Code.**

CAT tells you *which* tool call ate your context, *why* it was expensive, and *how* to avoid it next time. It hooks into Claude Code sessions, tracks token deltas per tool call, detects anomalies using rolling baselines, and explains spikes with an LLM classifier.

## The Problem

Claude Code users — especially those running multi-agent workflows — have no way to understand why their context window spiked. Existing tools tell you *how much* context you've used. None of them tell you *which task caused it*.

- A single subagent silently consumes 30-40% of the session
- Multi-agent setups make attribution impossible
- No historical baseline to detect "this task cost 5x more than usual"
- No root-cause analysis for *why* a tool call was expensive

## What CAT Does

```
Claude Code sessions (1..N)
    |
    |-- Hooks: PostToolUse, SubagentStop, Stop, UserPromptSubmit, SessionStart
    |-- Statusline script: real-time token snapshots
    |
    v
[Collector] -- FastAPI server (localhost:7821)
    |
    |-- SQLite DB: events, tasks, baselines, anomalies
    |-- Delta Engine: correlates snapshots with tool calls
    |-- Anomaly Engine: Z-score detection over rolling baselines
    |-- Classifier: Haiku explains *why* in plain language
    |-- Notifier: statusline badge + system alert + webhook
    |
    v
[Dashboard] -- Rich TUI with live session tracking
```

### Key Features

- **Per-tool-call token tracking** -- not just session-level totals
- **Rolling baselines** via Welford's online algorithm (O(1) memory per task type)
- **Anomaly detection** with configurable Z-score thresholds
- **LLM root-cause classification** -- "This Read call on a 15MB log file cost 12x the baseline because the entire file was loaded into context"
- **Multi-channel notifications** -- statusline badges, system alerts (macOS/Linux), Slack/Discord webhooks
- **Multi-session tracking** -- works across concurrent Claude Code sessions
- **Rich TUI dashboard** -- live view of sessions, costs, and anomalies

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Claude Code

### Install

```bash
# Clone
git clone https://github.com/roeimichael/ContextAnalyzerTerminal.git
cd ContextAnalyzerTerminal

# Install
uv sync

# Optional: install the LLM classifier (uses Haiku, ~$0.0001/event)
uv sync --extra classifier
```

### Setup

```bash
# Initialize default config
uv run context-pulse config init

# Install hooks into Claude Code
uv run context-pulse install
```

This copies hook scripts to `~/.context-pulse/hooks/` and registers them in Claude Code's `settings.json`.

### Run

```bash
# Start the collector (keep this running)
uv run context-pulse serve

# Open the dashboard in another terminal
uv run context-pulse dashboard
```

That's it. CAT will start tracking token costs as you use Claude Code.

## CLI Commands

| Command | Description |
|---------|-------------|
| `context-pulse serve` | Start the collector server |
| `context-pulse dashboard` | Open the TUI dashboard |
| `context-pulse status` | View active sessions |
| `context-pulse anomalies` | List recent anomalies |
| `context-pulse baseline` | Show rolling baseline stats |
| `context-pulse health` | Collector health check |
| `context-pulse install` | Install hooks into Claude Code |
| `context-pulse config init` | Write default config |
| `context-pulse config show` | Display loaded configuration |
| `context-pulse prune` | Clean up old data |

## Configuration

CAT uses a TOML config file at `~/.context-pulse/config.toml`. Every setting can be overridden with environment variables using the `CONTEXT_PULSE_` prefix.

```toml
[collector]
host = "127.0.0.1"
port = 7821

[anomaly]
z_score_threshold = 2.0    # Standard deviations above mean to flag
min_samples = 5            # Minimum data points before detection activates
cooldown_seconds = 60      # Debounce duplicate alerts

[classifier]
enabled = true
model = "claude-haiku-4-5-20251001"

[notifications]
statusline = true          # Inject warning badges into Claude Code statusline
system = false             # OS-level notifications (macOS/Linux)
webhook_url = ""           # Slack/Discord/custom webhook URL
```

Environment variable overrides:
```bash
CONTEXT_PULSE_COLLECTOR_PORT=8080
CONTEXT_PULSE_ANOMALY_Z_SCORE_THRESHOLD=3.0
CONTEXT_PULSE_CLASSIFIER_ENABLED=false
```

## How It Works

### Token Delta Computation

Claude Code hooks don't provide token counts directly. CAT correlates two data streams:

1. **Hook events** (PostToolUse, etc.) -- carry tool metadata but no token data
2. **Statusline snapshots** -- the only source of real-time token counts

The delta engine matches snapshots to tool calls by session ID and timestamps, computing per-call token costs.

### Anomaly Detection

CAT maintains a rolling baseline (mean + standard deviation) per task type using Welford's online algorithm. When a tool call's token cost exceeds the baseline by more than the configured Z-score threshold, it's flagged as an anomaly.

### LLM Classification

Flagged anomalies are sent to Haiku (configurable) for root-cause analysis. The classifier receives the tool name, input summary, token delta, and baseline -- and returns a plain-language explanation with severity and a suggestion for avoiding the cost.

## Development

```bash
# Install with dev dependencies
uv sync --all-extras

# Run tests (101 tests)
uv run pytest tests/ -v

# Lint
uv run ruff check src tests

# Type check (strict mode)
uv run pyright
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

## Tech Stack

- **FastAPI** + **Uvicorn** -- async HTTP collector
- **SQLite** + **aiosqlite** -- persistent storage with WAL mode
- **Pydantic** -- data validation and configuration
- **Typer** + **Rich** -- CLI and TUI dashboard
- **Anthropic SDK** -- optional LLM classifier

## License

[MIT](LICENSE)
