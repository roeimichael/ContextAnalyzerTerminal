# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

"""
context-pulse hook: UserPromptSubmit
Reads hook payload from stdin, POSTs to collector, exits 0.
"""

import sys
import json
import time
import httpx

from _hook_config import get_collector_url, get_timeout

COLLECTOR_URL = get_collector_url("/hook/event")
TIMEOUT_SECONDS = get_timeout()


def build_envelope(payload: dict) -> dict:
    prompt = payload.get("prompt", "")
    if len(prompt) > 200:
        prompt = prompt[:197] + "..."
    return {
        "event_type": "UserPromptSubmit",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
        "prompt_preview": prompt,
        "cwd": payload.get("cwd"),
    }


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        envelope = build_envelope(payload)

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            client.post(COLLECTOR_URL, json=envelope)
    except Exception:
        # NEVER fail. Claude Code must not see a non-zero exit.
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
