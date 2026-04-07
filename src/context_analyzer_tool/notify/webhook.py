"""Webhook notification module for context-analyzer-tool (Phase 3).

Sends anomaly alerts to external services (Slack, Discord, custom
webhooks) via HTTP POST.  All public functions are exception-safe and
will never raise; errors are logged as warnings.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

logger = logging.getLogger("context_analyzer_tool.notify.webhook")

# ---------------------------------------------------------------------------
# Payload formatters
# ---------------------------------------------------------------------------


def format_slack_payload(
    task_type: str,
    token_delta: int,
    z_score: float,
    session_id: str,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
) -> dict[str, Any]:
    """Build a Slack Block Kit payload for an anomaly alert.

    Returns a dict ready to be serialised as JSON and POSTed to a Slack
    Incoming Webhook URL.

    Parameters
    ----------
    task_type:
        The tool/task type that triggered the anomaly (e.g. "Bash").
    token_delta:
        The token cost that triggered the anomaly.
    z_score:
        The computed z-score for the anomaly.
    session_id:
        The session where the anomaly occurred.
    cause:
        Root-cause explanation from the classifier (may be ``None``).
    severity:
        Severity level from the classifier (may be ``None``).
    suggestion:
        Actionable suggestion from the classifier (may be ``None``).

    Returns
    -------
    dict[str, Any]
        Slack-compatible JSON payload.
    """
    tokens_fmt = f"{token_delta:,}"
    z_fmt = f"{z_score:.1f}"
    severity_display = severity or "unknown"

    # Fallback text (shown in notifications / non-Block Kit clients)
    fallback_text = (
        f"\u26a0 context-analyzer-tool: {task_type} used {tokens_fmt} tokens "
        f"({z_fmt}\u03c3)"
    )

    # Build section fields
    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Tool:* {task_type}"},
        {"type": "mrkdwn", "text": f"*Tokens:* {tokens_fmt}"},
        {"type": "mrkdwn", "text": f"*Z-Score:* {z_fmt}"},
        {"type": "mrkdwn", "text": f"*Severity:* {severity_display}"},
    ]

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "\u26a0 Token Spike Detected",
            },
        },
        {
            "type": "section",
            "fields": fields,
        },
    ]

    # Optional cause / suggestion block
    detail_parts: list[str] = []
    if cause is not None:
        detail_parts.append(f"*Cause:* {cause}")
    if suggestion is not None:
        detail_parts.append(f"*Suggestion:* {suggestion}")

    if detail_parts:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(detail_parts),
                },
            }
        )

    # Context block with session id
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Session: `{session_id}`",
                },
            ],
        }
    )

    return {
        "text": fallback_text,
        "blocks": blocks,
    }


def format_generic_payload(
    task_type: str,
    token_delta: int,
    z_score: float,
    session_id: str,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
) -> dict[str, Any]:
    """Build a generic JSON payload for Discord or custom webhooks.

    Returns a flat dict suitable for any service that accepts a JSON POST.

    Parameters
    ----------
    task_type:
        The tool/task type that triggered the anomaly.
    token_delta:
        The token cost that triggered the anomaly.
    z_score:
        The computed z-score for the anomaly.
    session_id:
        The session where the anomaly occurred.
    cause:
        Root-cause explanation from the classifier (may be ``None``).
    severity:
        Severity level from the classifier (may be ``None``).
    suggestion:
        Actionable suggestion from the classifier (may be ``None``).

    Returns
    -------
    dict[str, Any]
        Simple JSON payload.
    """
    return {
        "event": "token_anomaly",
        "task_type": task_type,
        "token_delta": token_delta,
        "z_score": z_score,
        "session_id": session_id,
        "cause": cause,
        "severity": severity,
        "suggestion": suggestion,
        "timestamp_iso": datetime.datetime.now(datetime.UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# HTTP sender
# ---------------------------------------------------------------------------


async def send_webhook(
    url: str,
    payload: dict[str, Any],
    timeout: float = 5.0,
) -> bool:
    """POST *payload* as JSON to *url*.

    Returns ``True`` on a 2xx response, ``False`` on any error.
    Never raises.

    Parameters
    ----------
    url:
        The webhook endpoint URL.
    payload:
        The JSON-serialisable dict to send.
    timeout:
        HTTP timeout in seconds.

    Returns
    -------
    bool
        ``True`` if the server responded with a 2xx status code.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
        if response.is_success:
            logger.debug(
                "Webhook delivered to %s (status %d)",
                url,
                response.status_code,
            )
            return True
        logger.warning(
            "Webhook to %s returned status %d: %s",
            url,
            response.status_code,
            response.text[:200],
        )
        return False
    except httpx.TimeoutException:
        logger.warning("Webhook to %s timed out after %.1fs", url, timeout)
        return False
    except httpx.HTTPError as exc:
        logger.warning("Webhook HTTP error for %s: %s", url, exc)
        return False
    except Exception:
        logger.exception("Unexpected error sending webhook to %s", url)
        return False


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


async def notify_webhook(
    url: str,
    task_type: str,
    token_delta: int,
    z_score: float,
    session_id: str,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
) -> bool:
    """Format and send a webhook notification.

    Auto-detects whether the URL is a Slack webhook (by checking if
    ``"slack"`` appears in the URL) and uses the appropriate payload
    format.

    Parameters
    ----------
    url:
        The webhook endpoint URL.
    task_type:
        The tool/task type that triggered the anomaly.
    token_delta:
        The token cost that triggered the anomaly.
    z_score:
        The computed z-score for the anomaly.
    session_id:
        The session where the anomaly occurred.
    cause:
        Root-cause explanation from the classifier (may be ``None``).
    severity:
        Severity level from the classifier (may be ``None``).
    suggestion:
        Actionable suggestion from the classifier (may be ``None``).

    Returns
    -------
    bool
        ``True`` if the webhook was delivered successfully.
    """
    try:
        if "slack" in url.lower():
            payload = format_slack_payload(
                task_type=task_type,
                token_delta=token_delta,
                z_score=z_score,
                session_id=session_id,
                cause=cause,
                severity=severity,
                suggestion=suggestion,
            )
        else:
            payload = format_generic_payload(
                task_type=task_type,
                token_delta=token_delta,
                z_score=z_score,
                session_id=session_id,
                cause=cause,
                severity=severity,
                suggestion=suggestion,
            )
        return await send_webhook(url, payload)
    except Exception:
        logger.exception("Unexpected error in notify_webhook for %s", url)
        return False
