"""Orchestrates all notification channels for anomaly alerts."""

from __future__ import annotations

import logging

from context_analyzer_tool.config import NotificationsConfig

logger = logging.getLogger("context_analyzer_tool.notify.dispatcher")


async def dispatch_anomaly_notifications(
    config: NotificationsConfig,
    task_type: str,
    token_delta: int,
    z_score: float,
    session_id: str,
    baseline_mean: float,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
) -> dict[str, bool]:
    """Send anomaly notifications through all enabled channels.

    Returns a dict mapping channel name to success status.
    Never raises — all errors are caught and logged per channel.
    """
    results: dict[str, bool] = {}

    # System notification (OS-level)
    if config.system_notification:
        try:
            from context_analyzer_tool.notify.system import notify_anomaly

            ok = await notify_anomaly(
                task_type=task_type,
                token_delta=token_delta,
                z_score=z_score,
                baseline_mean=baseline_mean,
                cause=cause,
                suggestion=suggestion,
            )
            results["system"] = ok
        except Exception:
            logger.exception("System notification failed")
            results["system"] = False

    # Webhook (Slack/Discord/custom)
    if config.webhook_url:
        try:
            from context_analyzer_tool.notify.webhook import notify_webhook

            ok = await notify_webhook(
                url=config.webhook_url,
                task_type=task_type,
                token_delta=token_delta,
                z_score=z_score,
                session_id=session_id,
                cause=cause,
                severity=severity,
                suggestion=suggestion,
            )
            results["webhook"] = ok
        except Exception:
            logger.exception("Webhook notification failed")
            results["webhook"] = False

    return results


def build_additional_context(
    config: NotificationsConfig,
    task_type: str,
    token_delta: int,
    z_score: float,
    baseline_mean: float,
    cause: str | None,
    suggestion: str | None,
) -> str | None:
    """Build the additionalContext string for in-session alerts.

    Returns None if in_session_alert is disabled or no anomaly info.
    """
    if not config.in_session_alert:
        return None

    try:
        from context_analyzer_tool.notify.session_alert import format_session_alert

        return format_session_alert(
            task_type=task_type,
            token_delta=token_delta,
            z_score=z_score,
            baseline_mean=baseline_mean,
            cause=cause,
            suggestion=suggestion,
        )
    except Exception:
        logger.exception("Failed to build session alert")
        return None
