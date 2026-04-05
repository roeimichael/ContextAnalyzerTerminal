# Changelog

## 0.2.0 (2026-04-05)

### Smart Warnings & Cache Awareness

- **Context warnings suggest /compact and /clear** -- actionable advice at 60%, 70%, and 90% context thresholds instead of generic "start fresh"
- **Large tool output alerting** -- warns when a single tool response exceeds 5K tokens with advice to pipe through head/tail or use rtk
- **Overhead ratio in statusline** -- shows "cost: 3.2x fresh" when per-message cost exceeds 2x a fresh session
- **Cache miss detection** -- detects when the 5-minute prompt cache expires and warns about context rebuild cost
- **Cache efficiency % in dashboard** -- new column showing cache_read / total cache ratio per session
- **Burn rate projection** -- "fills in ~X turns" column in dashboard using linear regression over recent snapshots
- **Compaction tracking** -- PreCompact/PostCompact hooks track compaction events with tokens saved
- **Compaction API endpoint** -- `/api/compactions` for querying compaction history
- **Burn rate API endpoint** -- `/api/sessions/{id}/burn-rate` for programmatic access
- **Uninstall command** -- `context-pulse uninstall` cleanly removes hooks from Claude Code

### Improvements

- 113 tests (up from 101)
- Schema migration v6 for compaction_events table
- Configurable large output threshold via `[hooks] large_output_threshold`

## 0.1.0 (2026-04-03)

Initial release.

### Features

- **Per-tool-call token tracking** via Claude Code hooks (PostToolUse, SubagentStop, Stop, UserPromptSubmit, SessionStart, Statusline)
- **Token delta engine** that correlates statusline snapshots with tool-use events to compute per-call costs
- **Rolling baselines** using Welford's online algorithm (O(1) memory, 20-sample window per task type)
- **Anomaly detection** with configurable Z-score thresholds and cooldown periods
- **LLM root-cause classification** via Haiku — explains *why* a tool call was expensive
- **Multi-channel notifications**: system alerts (macOS/Linux), webhooks (Slack/Discord), statusline badges, in-session alerts
- **Rich TUI dashboard** with live session tracking, anomaly history, and baseline stats
- **Multi-session support** — tracks concurrent Claude Code sessions by session ID
- **SQLite persistence** with async I/O, schema migrations, and data retention policies
- **TOML configuration** with environment variable overrides
- **Full CLI** via Typer: `serve`, `dashboard`, `status`, `anomalies`, `baseline`, `health`, `install`, `prune`
- **101 tests** covering all core components
