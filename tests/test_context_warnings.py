"""Tests for context threshold warning messages.

Verifies that each threshold level (60%, 70%, 90%) produces the correct
actionable suggestions (/compact, /clear), and that deduplication and
below-threshold cases work correctly.
"""

from __future__ import annotations

import aiosqlite
import pytest

from context_analyzer_tool.db import messages as db_messages
from context_analyzer_tool.notify.context_warnings import check_context_thresholds

SESSION_ID = "test-session-warnings"
CONTEXT_WINDOW = 200_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_queued_messages(db: aiosqlite.Connection) -> list[str]:
    """Return all unconsumed messages for SESSION_ID."""
    return await db_messages.consume_messages(db, SESSION_ID)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContextWarnings:
    """Tests for context_analyzer_tool.notify.context_warnings."""

    @pytest.mark.asyncio
    async def test_60_percent_message_suggests_compact(
        self, db_connection: aiosqlite.Connection,
    ) -> None:
        """At 60% usage the warning should suggest /compact."""
        await check_context_thresholds(db_connection, SESSION_ID, 62.0, CONTEXT_WINDOW)

        messages = await _get_queued_messages(db_connection)
        assert len(messages) >= 1

        msg_60 = messages[0]
        assert "/compact" in msg_60
        assert "62%" in msg_60 or "62" in msg_60

    @pytest.mark.asyncio
    async def test_70_percent_message_suggests_compact_proactively(
        self, db_connection: aiosqlite.Connection,
    ) -> None:
        """At 70% usage only the highest threshold (70%) should fire."""
        await check_context_thresholds(db_connection, SESSION_ID, 72.0, CONTEXT_WINDOW)

        messages = await _get_queued_messages(db_connection)
        # Only the highest applicable threshold fires
        assert len(messages) == 1

        assert "CONTEXT_WARNING_70" in messages[0]
        assert "/compact" in messages[0]
        assert "auto-compact" in messages[0]

    @pytest.mark.asyncio
    async def test_90_percent_message_suggests_clear(
        self, db_connection: aiosqlite.Connection,
    ) -> None:
        """At 90% usage only the highest threshold (90%) should fire."""
        await check_context_thresholds(db_connection, SESSION_ID, 92.0, CONTEXT_WINDOW)

        messages = await _get_queued_messages(db_connection)
        # Only the highest applicable threshold fires
        assert len(messages) == 1

        assert "CONTEXT_WARNING_90" in messages[0]
        assert "/clear" in messages[0]

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_fire(
        self, db_connection: aiosqlite.Connection,
    ) -> None:
        """Firing the 60% threshold twice should only queue one message."""
        await check_context_thresholds(db_connection, SESSION_ID, 62.0, CONTEXT_WINDOW)
        await check_context_thresholds(db_connection, SESSION_ID, 65.0, CONTEXT_WINDOW)

        messages = await _get_queued_messages(db_connection)
        # Only one 60% warning, no duplicates
        msg_60 = [m for m in messages if "CONTEXT_WARNING_60" in m]
        assert len(msg_60) == 1

    @pytest.mark.asyncio
    async def test_below_threshold_queues_nothing(
        self, db_connection: aiosqlite.Connection,
    ) -> None:
        """At 50% usage no warnings should be queued."""
        await check_context_thresholds(db_connection, SESSION_ID, 50.0, CONTEXT_WINDOW)

        messages = await _get_queued_messages(db_connection)
        assert messages == []
