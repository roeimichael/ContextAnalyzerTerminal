"""Shared config reader for context-pulse hook scripts.

Uses only stdlib (tomllib, pathlib, os) — no external dependencies.
Hook scripts import from this module to get the collector URL, timeout,
and other settings without hardcoding values.
"""

import os
import tomllib
from pathlib import Path

_CONFIG_CACHE: dict | None = None


def _load_config() -> dict:
    """Load and cache config.toml. Returns empty dict on failure."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    config_dir = os.environ.get(
        "CONTEXT_PULSE_CONFIG_DIR", str(Path.home() / ".context-pulse")
    )
    config_path = Path(config_dir) / "config.toml"
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                _CONFIG_CACHE = tomllib.load(f)
                return _CONFIG_CACHE
        except Exception:
            pass
    _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_collector_url(endpoint: str = "/hook/event") -> str:
    """Return the full collector URL for *endpoint*."""
    data = _load_config()
    collector = data.get("collector", {})
    host = collector.get("host", "127.0.0.1")
    port = collector.get("port", 7821)
    return f"http://{host}:{port}{endpoint}"


def get_timeout() -> float:
    """Return the hook HTTP timeout in seconds."""
    data = _load_config()
    return float(data.get("hooks", {}).get("timeout_seconds", 2.0))


def get_chars_per_token() -> int:
    """Return the characters-per-token estimate for hook-side token counting."""
    data = _load_config()
    return int(data.get("hooks", {}).get("chars_per_token_estimate", 4))


def get_large_output_threshold() -> int:
    """Return the token threshold for large tool output warnings."""
    data = _load_config()
    return int(data.get("hooks", {}).get("large_output_threshold", 5000))
