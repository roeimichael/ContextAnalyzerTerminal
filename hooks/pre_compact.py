# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Hook: PreCompact -- fires before Claude Code compacts the context."""

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
            "event_type": "PreCompact",
            "session_id": payload.get("session_id", "unknown"),
            "timestamp_ms": int(time.time() * 1000),
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
