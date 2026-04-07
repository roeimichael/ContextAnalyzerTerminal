"""Tests for Phase 3 notification modules.

Covers system notifications, webhook delivery, session alerts,
statusline formatting, and the dispatcher orchestrator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from context_analyzer_tool.config import NotificationsConfig
from context_analyzer_tool.notify.dispatcher import (
    build_additional_context,
    dispatch_anomaly_notifications,
)
from context_analyzer_tool.notify.session_alert import format_session_alert
from context_analyzer_tool.notify.statusline import (
    format_anomaly_badge,
    format_statusline_with_anomaly,
)
from context_analyzer_tool.notify.system import (
    format_anomaly_notification,
    send_system_notification,
)
from context_analyzer_tool.notify.webhook import (
    format_generic_payload,
    format_slack_payload,
    send_webhook,
)

# ---------------------------------------------------------------------------
# System notifications
# ---------------------------------------------------------------------------


class TestSystemNotifications:
    """Tests for context_analyzer_tool.notify.system."""

    def test_format_anomaly_notification(self) -> None:
        """Title contains severity; body contains task type, tokens, and ratio."""
        title, message = format_anomaly_notification(
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            baseline_mean=2000.0,
            cause="Large directory listing",
            suggestion="Use --max-depth to limit output",
        )

        # Title should contain severity label and the "context-analyzer-tool" prefix
        assert "context-analyzer-tool" in title
        assert "High" in title  # z >= 4.0 -> High

        # Message body checks
        assert "Bash" in message
        assert "8,400" in message
        assert "4.2\u00d7 baseline" in message
        assert "Cause: Large directory listing" in message
        assert "Fix: Use --max-depth to limit output" in message

    def test_format_anomaly_notification_medium_severity(self) -> None:
        """z_score between 3.0 and 4.0 yields Medium severity."""
        title, _ = format_anomaly_notification(
            task_type="Read",
            token_delta=5000,
            z_score=3.5,
            baseline_mean=1000.0,
            cause=None,
            suggestion=None,
        )
        assert "Medium" in title

    def test_format_anomaly_notification_zero_baseline(self) -> None:
        """When baseline_mean is 0, ratio string falls back gracefully."""
        _, message = format_anomaly_notification(
            task_type="Bash",
            token_delta=500,
            z_score=2.5,
            baseline_mean=0.0,
            cause=None,
            suggestion=None,
        )
        assert "above baseline" in message

    @pytest.mark.asyncio
    async def test_send_system_notification_mocked(self) -> None:
        """Mock subprocess creation; verify it is invoked without real I/O."""
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_process,
        ) as mock_exec:
            result = await send_system_notification("Test Title", "Test Body")

        assert result is True
        mock_exec.assert_called_once()


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class TestWebhook:
    """Tests for context_analyzer_tool.notify.webhook."""

    def test_format_slack_payload(self) -> None:
        """Slack Block Kit payload has correct structure and field values."""
        payload = format_slack_payload(
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            session_id="abc-123",
            cause="Large output",
            severity="High",
            suggestion="Limit output",
        )

        # Top-level keys
        assert "text" in payload
        assert "blocks" in payload

        # Fallback text includes task type and token count
        assert "Bash" in payload["text"]
        assert "8,400" in payload["text"]

        # Blocks structure: header, section with fields, detail section, context
        blocks = payload["blocks"]
        assert len(blocks) >= 3  # header + fields + context (+ optional detail)

        # First block is a header
        assert blocks[0]["type"] == "header"
        assert "Token Spike" in blocks[0]["text"]["text"]

        # Second block is a section with fields
        section = blocks[1]
        assert section["type"] == "section"
        field_texts = [f["text"] for f in section["fields"]]
        assert any("Bash" in t for t in field_texts)
        assert any("8,400" in t for t in field_texts)
        assert any("4.2" in t for t in field_texts)
        assert any("High" in t for t in field_texts)

        # Context block contains session id
        context_block = blocks[-1]
        assert context_block["type"] == "context"
        assert "abc-123" in context_block["elements"][0]["text"]

    def test_format_generic_payload(self) -> None:
        """Generic payload contains all expected top-level fields."""
        payload = format_generic_payload(
            task_type="Read",
            token_delta=3200,
            z_score=3.1,
            session_id="sess-456",
            cause="Big file",
            severity="Medium",
            suggestion="Read fewer lines",
        )

        assert payload["event"] == "token_anomaly"
        assert payload["task_type"] == "Read"
        assert payload["token_delta"] == 3200
        assert payload["z_score"] == 3.1
        assert payload["session_id"] == "sess-456"
        assert payload["cause"] == "Big file"
        assert payload["severity"] == "Medium"
        assert payload["suggestion"] == "Read fewer lines"
        assert "timestamp_iso" in payload

    @pytest.mark.asyncio
    async def test_send_webhook_success(self) -> None:
        """Mock httpx to return 200; verify send_webhook returns True."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        target = "context_analyzer_tool.notify.webhook.httpx.AsyncClient"
        with patch(target, return_value=mock_client):
            result = await send_webhook("https://example.com/hook", {"test": True})

        assert result is True
        mock_client.post.assert_called_once_with(
            "https://example.com/hook",
            json={"test": True},
        )

    @pytest.mark.asyncio
    async def test_send_webhook_failure(self) -> None:
        """Mock httpx to raise; verify send_webhook returns False."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        target = "context_analyzer_tool.notify.webhook.httpx.AsyncClient"
        with patch(target, return_value=mock_client):
            result = await send_webhook("https://example.com/hook", {"test": True})

        assert result is False


# ---------------------------------------------------------------------------
# Session alert
# ---------------------------------------------------------------------------


class TestSessionAlert:
    """Tests for context_analyzer_tool.notify.session_alert."""

    def test_format_session_alert_full(self) -> None:
        """Alert with cause and suggestion includes all three lines."""
        alert = format_session_alert(
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            baseline_mean=2000.0,
            cause="Large directory listing",
            suggestion="Use --max-depth to limit output",
        )

        lines = alert.split("\n")
        assert len(lines) == 3

        # Headline
        assert "[CAT]" in lines[0]
        assert "8,400" in lines[0]
        assert "4.2\u03c3" in lines[0]
        assert "2,000" in lines[0]
        assert "Bash" in lines[0]

        # Cause and suggestion
        assert lines[1] == "Cause: Large directory listing"
        assert lines[2] == "Consider: Use --max-depth to limit output"

    def test_format_session_alert_no_cause(self) -> None:
        """Alert without cause/suggestion is a single headline line."""
        alert = format_session_alert(
            task_type="Read",
            token_delta=5000,
            z_score=3.0,
            baseline_mean=1500.0,
            cause=None,
            suggestion=None,
        )

        assert "Cause:" not in alert
        assert "Consider:" not in alert
        assert "\n" not in alert
        assert "[CAT]" in alert
        assert "5,000" in alert


# ---------------------------------------------------------------------------
# Statusline
# ---------------------------------------------------------------------------


class TestStatusline:
    """Tests for context_analyzer_tool.notify.statusline."""

    def test_format_anomaly_badge(self) -> None:
        """Badge follows the pattern: warning sign, task, compact tokens, z-score."""
        badge = format_anomaly_badge("Bash", 8400, 4.2)
        assert badge == "\u26a0 Bash 8.4k (4.2\u03c3)"

    def test_format_anomaly_badge_small(self) -> None:
        """Tokens below 1000 are shown as raw numbers, not compact form."""
        badge = format_anomaly_badge("Read", 750, 2.5)
        assert badge == "\u26a0 Read 750 (2.5\u03c3)"

    def test_format_statusline_with_anomaly(self) -> None:
        """When anomaly_badge is present, it replaces rate-limit info."""
        badge = format_anomaly_badge("Bash", 8400, 4.2)
        line = format_statusline_with_anomaly(
            model_name="Opus",
            used_pct=71,
            total_cost=0.03,
            five_hour_pct=24.0,
            seven_day_pct=41.0,
            anomaly_badge=badge,
        )

        # Anomaly badge is present
        assert "\u26a0 Bash 8.4k (4.2\u03c3)" in line
        # Rate-limit fields are absent
        assert "5h:" not in line
        assert "7d:" not in line
        # Cost and context are still shown
        assert "$0.03" in line
        assert "ctx 71%" in line
        assert "Opus" in line

    def test_format_statusline_normal(self) -> None:
        """Without anomaly, rate-limit percentages are shown."""
        line = format_statusline_with_anomaly(
            model_name="Opus",
            used_pct=42,
            total_cost=0.01,
            five_hour_pct=24.0,
            seven_day_pct=41.0,
        )

        assert "5h: 24%" in line
        assert "7d: 41%" in line
        assert "$0.01" in line
        assert "ctx 42%" in line
        # No anomaly badge
        assert "\u26a0" not in line


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    """Tests for context_analyzer_tool.notify.dispatcher."""

    @pytest.mark.asyncio
    async def test_dispatch_all_disabled(self) -> None:
        """All notification options off returns an empty dict."""
        config = NotificationsConfig(
            system_notification=False,
            in_session_alert=False,
            webhook_url="",
        )

        results = await dispatch_anomaly_notifications(
            config=config,
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            session_id="s-1",
            baseline_mean=2000.0,
            cause=None,
            severity=None,
            suggestion=None,
        )

        assert results == {}

    @pytest.mark.asyncio
    async def test_dispatch_system_enabled(self) -> None:
        """When system_notification is on, the system notifier is called."""
        config = NotificationsConfig(
            system_notification=True,
            in_session_alert=False,
            webhook_url="",
        )

        with patch(
            "context_analyzer_tool.notify.system.notify_anomaly",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_notify:
            results = await dispatch_anomaly_notifications(
                config=config,
                task_type="Bash",
                token_delta=8400,
                z_score=4.2,
                session_id="s-1",
                baseline_mean=2000.0,
                cause="Large output",
                severity="High",
                suggestion="Limit depth",
            )

        assert results["system"] is True
        mock_notify.assert_called_once_with(
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            baseline_mean=2000.0,
            cause="Large output",
            suggestion="Limit depth",
        )

    @pytest.mark.asyncio
    async def test_dispatch_webhook_enabled(self) -> None:
        """When webhook_url is set, the webhook notifier is called."""
        config = NotificationsConfig(
            system_notification=False,
            in_session_alert=False,
            webhook_url="https://hooks.slack.com/services/XXX",
        )

        with patch(
            "context_analyzer_tool.notify.webhook.notify_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_wh:
            results = await dispatch_anomaly_notifications(
                config=config,
                task_type="Read",
                token_delta=5000,
                z_score=3.1,
                session_id="s-2",
                baseline_mean=1500.0,
                cause=None,
                severity=None,
                suggestion=None,
            )

        assert results["webhook"] is True
        mock_wh.assert_called_once_with(
            url="https://hooks.slack.com/services/XXX",
            task_type="Read",
            token_delta=5000,
            z_score=3.1,
            session_id="s-2",
            cause=None,
            severity=None,
            suggestion=None,
        )

    def test_build_additional_context_disabled(self) -> None:
        """When in_session_alert is off, returns None."""
        config = NotificationsConfig(in_session_alert=False)
        result = build_additional_context(
            config=config,
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            baseline_mean=2000.0,
            cause=None,
            suggestion=None,
        )
        assert result is None

    def test_build_additional_context_enabled(self) -> None:
        """When in_session_alert is on, returns formatted alert string."""
        config = NotificationsConfig(in_session_alert=True)
        result = build_additional_context(
            config=config,
            task_type="Bash",
            token_delta=8400,
            z_score=4.2,
            baseline_mean=2000.0,
            cause="Large output",
            suggestion="Use --max-depth",
        )
        assert result is not None
        assert "[CAT]" in result
        assert "8,400" in result
        assert "Cause: Large output" in result
