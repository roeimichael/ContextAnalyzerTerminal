# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

"""
context-pulse hook: PostToolUse
Reads hook payload from stdin, POSTs to collector, exits 0.
"""

import sys
import json
import time
import httpx

from _hook_config import get_collector_url, get_timeout, get_chars_per_token

COLLECTOR_URL = get_collector_url("/hook/event")
TIMEOUT_SECONDS = get_timeout()
_CHARS_PER_TOKEN = get_chars_per_token()


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using configurable chars-per-token ratio."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def build_envelope(payload: dict) -> dict:
    tool_input_str = json.dumps(payload.get("tool_input", {}))
    tool_response_str = json.dumps(payload.get("tool_response", {}))

    # Estimate tokens for input and response
    input_tokens = estimate_tokens(tool_input_str)
    response_tokens = estimate_tokens(tool_response_str)
    total_tokens = input_tokens + response_tokens

    if len(tool_input_str) > 500:
        tool_input_str = tool_input_str[:497] + "..."
    return {
        "event_type": "PostToolUse",
        "session_id": payload["session_id"],
        "timestamp_ms": int(time.time() * 1000),
        "payload": payload,
        "tool_name": payload.get("tool_name"),
        "tool_input_summary": tool_input_str,
        "cwd": payload.get("cwd"),
        "estimated_tokens": total_tokens,
        "estimated_input_tokens": input_tokens,
        "estimated_response_tokens": response_tokens,
    }


def get_session_alert(session_id: str) -> str:
    """Query collector for a recent un-notified anomaly and return an alert string."""
    try:
        url = get_collector_url(f"/api/sessions/{session_id}/latest-anomaly")
        with httpx.Client(timeout=1.0) as client:
            resp = client.get(url)
            if resp.status_code == 200 and resp.json():
                a = resp.json()
                # Skip if already notified (prevents repeated alerts)
                if a.get("notified"):
                    return ""
                anomaly_id = a.get("id")
                task_type = a.get("task_type", "?")
                delta = a.get("token_cost", 0)
                z = a.get("z_score", 0.0)
                cause = a.get("cause") or ""
                suggestion = a.get("suggestion") or ""
                mean = delta / max(z, 0.1)

                # Mark as notified so we don't repeat
                if anomaly_id:
                    try:
                        client.post(
                            get_collector_url(f"/api/anomalies/{anomaly_id}/mark-notified")
                        )
                    except Exception:
                        pass

                parts = [
                    f"[context-pulse] \u26a0 Last {task_type} command cost "
                    f"{delta:,} tokens ({z:.1f}\u03c3 above baseline of ~{mean:,.0f})."
                ]
                if cause:
                    parts.append(f"Cause: {cause}")
                if suggestion:
                    parts.append(f"Consider: {suggestion}")
                return "\n".join(parts)
    except Exception:
        pass
    return ""


def main() -> None:
    hook_output = None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)

        envelope = build_envelope(payload)

        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            client.post(COLLECTOR_URL, json=envelope)

        # Collect additionalContext from anomaly alerts + pending messages
        session_id = payload.get("session_id", "")
        context_parts = []

        if session_id:
            # Check anomaly alerts
            alert = get_session_alert(session_id)
            if alert:
                context_parts.append(alert)

            # Check pending messages (context threshold warnings, etc.)
            try:
                url = get_collector_url(f"/api/sessions/{session_id}/pending-messages")
                with httpx.Client(timeout=1.0) as c:
                    resp = c.get(url)
                    if resp.status_code == 200:
                        msgs = resp.json().get("messages", [])
                        for msg in msgs:
                            # Strip HTML comment dedup tags
                            clean = msg.split("-->")[-1].strip() if "-->" in msg else msg
                            context_parts.append(clean)
            except Exception:
                pass

        if context_parts:
            hook_output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n\n".join(context_parts),
                }
            }
    except Exception:
        pass

    # Output hook response if we have one
    if hook_output:
        print(json.dumps(hook_output))

    sys.exit(0)


if __name__ == "__main__":
    main()
