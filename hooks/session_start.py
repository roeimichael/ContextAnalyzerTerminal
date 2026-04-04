# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
# ]
# ///
"""Hook: SessionStart — fires when a new Claude Code session opens."""

import json
import sys
import time

import httpx

from _hook_config import get_collector_url, get_timeout

COLLECTOR_URL = get_collector_url("/hook/event")
TIMEOUT_SECONDS = get_timeout()


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        envelope = {
            "session_id": payload.get("session_id", "unknown"),
            "event_type": "SessionStart",
            "timestamp_ms": int(time.time() * 1000),
            "tool_name": None,
            "tool_input_summary": None,
            "payload": payload,
            "cwd": payload.get("cwd"),
        }

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            client.post(COLLECTOR_URL, json=envelope)
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
