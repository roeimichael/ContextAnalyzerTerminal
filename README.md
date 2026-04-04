<p align="center">
  <img src="docs/CAT-socialpreview.jpg" alt="CAT - Context Analyzer Terminal" width="600">
</p>

<p align="center">
  <strong>Know exactly which tool call ate your context window.</strong>
</p>

<p align="center">
  <a href="https://github.com/roeimichael/ContextAnalyzerTerminal/actions/workflows/ci.yml"><img src="https://github.com/roeimichael/ContextAnalyzerTerminal/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
</p>

---

CAT hooks into your Claude Code sessions and tracks token cost **per tool call** -- not just per session. It builds rolling baselines, flags anomalies, and uses an LLM to explain *why* something was expensive.

<p align="center">
  <img src="docs/demo-dashboard.svg" alt="CAT Dashboard" width="100%">
</p>

## Install

```bash
git clone https://github.com/roeimichael/ContextAnalyzerTerminal.git
cd ContextAnalyzerTerminal
uv sync
```

## Setup & Run

```bash
# 1. Initialize config and install hooks into Claude Code
context-pulse config init
context-pulse install

# 2. Start the collector (keep running in a terminal)
context-pulse serve

# 3. Open the dashboard
context-pulse dashboard
```

That's it. Use Claude Code normally -- CAT tracks everything in the background.

## What You Get

| Feature | What it does |
|:--------|:-------------|
| **Per-tool-call tracking** | See exactly how many tokens each Read, Bash, Grep, etc. costs |
| **Rolling baselines** | Learns normal cost per task type using Welford's algorithm |
| **Anomaly detection** | Flags tool calls that exceed baseline by configurable Z-score |
| **Root-cause analysis** | Haiku classifier explains *why* in plain language |
| **Live dashboard** | Rich TUI with sessions, cost timeline, and anomaly feed |
| **Notifications** | Statusline badges, system alerts, Slack/Discord webhooks |
| **Multi-session** | Tracks concurrent Claude Code sessions independently |

<p align="center">
  <img src="docs/demo-cli.svg" alt="CAT CLI Output" width="100%">
</p>

## CLI Reference

```
context-pulse serve           # Start the collector server
context-pulse dashboard       # Open the TUI dashboard
context-pulse status          # View active sessions
context-pulse anomalies       # List recent anomalies
context-pulse baseline        # Show rolling baseline stats
context-pulse health          # Collector health check
context-pulse install         # Install hooks into Claude Code
context-pulse config init     # Write default config
context-pulse config show     # Display loaded configuration
context-pulse prune           # Clean up old data
```

## Configuration

Config lives at `~/.context-pulse/config.toml`. Every setting has an environment variable override with the `CONTEXT_PULSE_` prefix.

```toml
[collector]
host = "127.0.0.1"
port = 7821

[anomaly]
z_score_threshold = 2.0     # Std devs above mean to flag
min_samples = 5             # Data points before detection kicks in
cooldown_seconds = 60       # Debounce duplicate alerts

[classifier]
enabled = true              # Requires: uv sync --extra classifier
model = "claude-haiku-4-5-20251001"

[notifications]
statusline = true           # Badge in Claude Code statusline
system = false              # OS notifications (macOS/Linux)
webhook_url = ""            # Slack/Discord webhook
```

## How It Works

Claude Code hooks don't include token counts. CAT correlates two data streams:

1. **Hook events** (PostToolUse, SubagentStop, Stop, etc.) carry tool metadata
2. **Statusline snapshots** provide real-time token counts

The delta engine matches them by session ID + timestamps to compute per-call costs. Anomalies are detected via Z-score over a rolling 20-sample window per task type, then classified by Haiku.

```
Hooks + Statusline --> Collector --> Delta Engine --> Anomaly Detection --> Classifier --> Notifications
                                        |
                                    SQLite DB
                                        |
                                    Dashboard
```

## Development

```bash
uv sync --all-extras          # Install with dev + classifier deps
uv run pytest tests/ -v       # 101 tests
uv run ruff check src tests   # Lint
uv run pyright                # Type check (strict mode)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and areas where help is welcome.

## Tech Stack

**FastAPI** + **Uvicorn** (async collector) -- **SQLite** + **aiosqlite** (persistence) -- **Pydantic** (validation) -- **Typer** + **Rich** (CLI/TUI) -- **Anthropic SDK** (optional classifier)

## License

[MIT](LICENSE)
