# Changelog

## 0.3.1 (2026-04-08)

### Bug Fixes

- **Fix dashboard timestamps** -- `_ts_to_time` was dividing by 500 instead of 1000, producing wrong times in the task timeline and anomaly feed
- **Fix context warning spam** -- when the first snapshot arrives above 90%, only the highest applicable threshold fires (was firing 60%, 70%, and 90% simultaneously)
- **Fix unbounded pending_tool_calls** -- add `maxlen=100` to the pending deque to prevent memory leak when statusline snapshots stop arriving
- **Make migrations idempotent** -- ALTER TABLE migrations now catch "duplicate column" errors, preventing crashes on partial migration recovery

### Improvements

- **PR template** -- added `pyright` type check to the testing checklist
- **CONTRIBUTING.md** -- added "Good First Issues" section, architecture overview, and recommended reading order for new contributors
- **README.md** -- added Contributing section linking to good first issues
- **GitHub issues** -- created 12 curated good-first-issue and 6 intermediate issues for new contributors

## 0.3.0 (2026-04-07)

### Project Rename

- **Renamed from `context-pulse` to `context-analyzer-tool`** -- package, module, CLI entry point, and all internal references updated

### TUI Visual Overhaul

- **New color theme** -- vibrant `bright_*` color palette with centralized theme constants replacing scattered hardcoded styles
- **Distinct panel frames** -- each panel type has its own Rich box style: `HEAVY` header, `ROUNDED` sessions/anomalies, `DOUBLE` tasks
- **Alternating row highlights** -- zebra-striping on all tables for improved readability
- **Unicode panel icons** -- `⭐` header, `☰` sessions, `▒` tasks, `⚠` anomalies, `⚙` RTK
- **Header bar redesign** -- stats separated by `│` dividers with `⏱` `⚡` `▣` icons
- **Sleeping cat animation** -- 20-frame ASCII cat in the bottom-left "nap zone" with drifting z's, subtle ear twitches, and a brief wake-up frame
- **Double-bordered nap zone** -- outer `DOUBLE` frame with inner `ROUNDED` border confining the cat art
- **Faster refresh** -- default refresh rate increased from 2s to 1s for smoother animations

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
- **Uninstall command** -- `context-analyzer-tool uninstall` cleanly removes hooks from Claude Code

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
