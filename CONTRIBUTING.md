# Contributing to CAT (Context Analyzer Tool)

Thanks for your interest in contributing! CAT is an open-source project and we welcome contributions of all kinds.

## New here? Start with a Good First Issue

We maintain a curated list of beginner-friendly issues — each one includes clear context, the files to look at, and a suggested approach:

**[Browse Good First Issues](https://github.com/roeimichael/ContextAnalyzerTerminal/labels/good%20first%20issue)**

These range from one-line bug fixes to small feature additions, and are a great way to get familiar with the codebase and dev workflow before tackling bigger tasks.
## Start Here

### Architecture Overview

Claude Code Hooks → FastAPI Collector → Delta Engine → Anomaly Detection → Notifications

### Recommended Reading Order

config.py → models.py → delta_engine.py → anomaly.py

### Architecture Docs
- docs/phase1-architecture.md  
- docs/phase2-architecture.md  

### Good First Issues

https://github.com/roeimichael/ContextAnalyzerTerminal/labels/good%20first%20issue

## Understanding the Architecture

CAT's data flows in a pipeline:

```
Claude Code Hooks → FastAPI Collector → Delta Engine → Anomaly Detection → Notifications
     (hooks/)        (collector/)       (collector/)      (engine/)         (notify/)
```

**Recommended reading order for new contributors:**
1. `src/context_analyzer_tool/config.py` — how configuration works
2. `src/context_analyzer_tool/collector/models.py` — data shapes (Pydantic models)
3. `src/context_analyzer_tool/collector/delta_engine.py` — core logic: correlating events with token snapshots
4. `src/context_analyzer_tool/engine/anomaly.py` — how anomalies are detected (Z-score statistics)

For deeper dives, see the architecture docs:
- [`docs/phase1-architecture.md`](docs/phase1-architecture.md) — Delta engine design and schema
- [`docs/phase2-architecture.md`](docs/phase2-architecture.md) — Anomaly detection, classifier, and notifications

## Getting Started

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager

### Development Setup

```bash
# Clone the repo
git clone https://github.com/roeimichael/ContextAnalyzerTerminal.git
cd ContextAnalyzerTerminal

# Install dependencies (including dev tools)
uv sync --all-extras

# Verify everything works
uv run pytest tests/ -v
uv run ruff check src tests
```

### Project Structure

```
src/context_analyzer_tool/
  cli.py              # Typer CLI entry point
  config.py           # TOML + env var configuration
  collector/           # FastAPI HTTP server + delta engine
  db/                  # SQLite async data layer
  engine/              # Baseline, anomaly detection, LLM classifier
  dashboard/           # Rich TUI dashboard
  notify/              # Multi-channel notifications
hooks/                 # Claude Code hook scripts
tests/                 # Pytest suite
```

## Development Workflow

### Running Tests

```bash
uv run pytest tests/ -v          # Full suite
uv run pytest tests/test_db.py   # Single module
```

### Linting & Type Checking

```bash
uv run ruff check src tests      # Linter
uv run ruff check --fix src      # Auto-fix
uv run pyright                   # Type checker (strict mode)
```

### Running Locally

```bash
# Start the collector server
uv run context-analyzer-tool serve

# In another terminal, view the dashboard
uv run context-analyzer-tool dashboard

# Check health
uv run context-analyzer-tool health
```

## Submitting Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes
4. Ensure tests pass and lint is clean
5. Commit with a clear message describing what and why
6. Open a Pull Request against `main`

### PR Guidelines

- Keep PRs focused — one feature or fix per PR
- Add tests for new functionality
- Update the README if you're adding user-facing features
- Run the full test suite before submitting

## Areas Where Help Is Welcome

- **New notification channels** (email, Telegram, etc.)
- **Dashboard improvements** (web UI, more visualizations)
- **Platform support** (Windows notifications, broader OS testing)
- **Documentation** (tutorials, examples, translations)
- **Performance** (database query optimization, memory profiling)

## Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting (config in `pyproject.toml`)
- [Pyright](https://github.com/microsoft/pyright) strict mode for type checking
- Line length: 99 characters
- All I/O code should be async

## Reporting Bugs

Open an issue with:
- What you expected vs what happened
- Steps to reproduce
- Your Python version and OS
- Relevant logs or error output

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
