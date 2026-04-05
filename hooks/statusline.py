# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///

"""
context-pulse statusline script.
1. Reads statusline JSON from stdin (provided by Claude Code).
2. POSTs token snapshot to collector (fire-and-forget, 2s timeout).
3. Prints a formatted statusline string to stdout.
"""

import sys
import json
import time
import httpx

from _hook_config import get_collector_url, get_timeout

COLLECTOR_URL = get_collector_url("/hook/statusline")
TIMEOUT_SECONDS = get_timeout()


def post_snapshot(data: dict) -> None:
    """POST snapshot data to collector. Swallow all errors."""
    try:
        cw = data.get("context_window", {})
        cu = cw.get("current_usage", {})
        cost = data.get("cost", {})
        model = data.get("model", {})
        rl = data.get("rate_limits", {})

        snapshot = {
            "session_id": data["session_id"],
            "timestamp_ms": int(time.time() * 1000),
            "total_input_tokens": cw.get("total_input_tokens", 0),
            "total_output_tokens": cw.get("total_output_tokens", 0),
            "cache_creation_input_tokens": cu.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": cu.get("cache_read_input_tokens", 0),
            "context_window_size": cw.get("context_window_size", 0),
            "used_percentage": cw.get("used_percentage", 0),
            "remaining_percentage": cw.get("remaining_percentage", 0),
            "total_cost_usd": cost.get("total_cost_usd", 0.0),
            "total_duration_ms": cost.get("total_duration_ms", 0),
            "model_id": model.get("id", "unknown"),
            "model_display_name": model.get("display_name", "Unknown"),
            "rate_limit_five_hour_pct": rl.get("five_hour", {}).get("used_percentage", 0.0),
            "rate_limit_seven_day_pct": rl.get("seven_day", {}).get("used_percentage", 0.0),
            "version": data.get("version", "unknown"),
        }

        with httpx.Client(timeout=1.5) as client:
            client.post(COLLECTOR_URL, json=snapshot)
    except Exception:
        pass


def get_anomaly_badge(session_id: str) -> str:
    """Query collector for a recent anomaly and return a badge string."""
    try:
        url = get_collector_url(f"/api/sessions/{session_id}/latest-anomaly")
        with httpx.Client(timeout=0.5) as client:
            resp = client.get(url)
            if resp.status_code == 200 and resp.json():
                a = resp.json()
                task_type = a.get("task_type", "?")
                delta = a.get("token_cost", 0)
                z = a.get("z_score", 0.0)
                # Format tokens in k
                if delta >= 1000:
                    tokens_str = f"{delta / 1000:.1f}k"
                else:
                    tokens_str = str(delta)
                return f"\u26a0 {task_type} {tokens_str} ({z:.1f}\u03c3)"
    except Exception:
        pass
    return ""


def format_statusline(data: dict) -> str:
    """
    Format the statusline string for Claude Code display.
    Normal: "modelName | ctx 42% ████░░ | $0.01 | 5h: 24% | 7d: 41%"
    Anomaly: "modelName | ctx 71% ███████░░░ | ⚠ Bash 8.4k (4.2σ) | $0.03"
    """
    cw = data.get("context_window", {})
    cost = data.get("cost", {})
    model = data.get("model", {})
    rl = data.get("rate_limits", {})

    used_pct = cw.get("used_percentage", 0)
    model_name = model.get("display_name", "Claude")
    total_cost = cost.get("total_cost_usd", 0.0)
    five_hour_pct = rl.get("five_hour", {}).get("used_percentage", 0.0)
    seven_day_pct = rl.get("seven_day", {}).get("used_percentage", 0.0)

    # Build progress bar (10 chars)
    filled = round(used_pct / 10)
    bar = "\u2588" * filled + "\u2591" * (10 - filled)

    # Check for recent anomaly
    session_id = data.get("session_id", "")
    badge = get_anomaly_badge(session_id) if session_id else ""

    if badge:
        return (
            f"{model_name} | ctx {used_pct}% {bar} "
            f"| {badge} "
            f"| ${total_cost:.2f}"
        )
    return (
        f"{model_name} | ctx {used_pct}% {bar} "
        f"| ${total_cost:.2f} "
        f"| 5h: {five_hour_pct:.0f}% "
        f"| 7d: {seven_day_pct:.0f}%"
    )


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)

        # POST to collector (best-effort)
        post_snapshot(data)

        # Output statusline to stdout
        print(format_statusline(data))
    except Exception:
        # On any error, output a safe default
        print("context-pulse | --")

    sys.exit(0)


if __name__ == "__main__":
    main()
