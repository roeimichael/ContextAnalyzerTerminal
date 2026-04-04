# Changelog

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
