"""Tests for the Haiku anomaly classifier (Phase 2)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite

from context_analyzer_tool.collector.models import ClassifierResponse
from context_analyzer_tool.config import ClassifierConfig
from context_analyzer_tool.engine.classifier import (
    _compute_cache_key,
    _parse_classifier_output,
    classify_anomaly,
)

# ---------------------------------------------------------------------------
# _parse_classifier_output tests
# ---------------------------------------------------------------------------


def test_parse_valid_json() -> None:
    """Valid JSON is parsed into a ClassifierResponse."""
    raw = json.dumps({
        "cause": "Large file read",
        "severity": "high",
        "suggestion": "Use partial reads.",
    })
    result = _parse_classifier_output(raw)
    assert isinstance(result, ClassifierResponse)
    assert result.cause == "Large file read"
    assert result.severity == "high"
    assert result.suggestion == "Use partial reads."


def test_parse_json_with_markdown_fences() -> None:
    """Markdown code fences around JSON are stripped before parsing."""
    raw = (
        "```json\n"
        '{"cause": "Recursive glob", "severity": "medium", "suggestion": "Narrow the glob."}\n'
        "```"
    )
    result = _parse_classifier_output(raw)
    assert result.cause == "Recursive glob"
    assert result.severity == "medium"
    assert result.suggestion == "Narrow the glob."


def test_parse_invalid_json_fallback() -> None:
    """Malformed JSON returns a fallback ClassifierResponse."""
    raw = "this is not json at all {{{["
    result = _parse_classifier_output(raw)
    assert isinstance(result, ClassifierResponse)
    assert result.severity == "medium"
    assert "unparseable" in result.cause.lower() or "spike" in result.cause.lower()


# ---------------------------------------------------------------------------
# _compute_cache_key tests
# ---------------------------------------------------------------------------


def test_cache_key_computation() -> None:
    """Cache key rounds token_delta to the nearest 1000."""
    # 1499 rounds to 1000
    assert _compute_cache_key("Bash", 1499) == "Bash:1000"
    # 1500 rounds to 2000
    assert _compute_cache_key("Bash", 1500) == "Bash:2000"
    # Exact multiple
    assert _compute_cache_key("Read", 3000) == "Read:3000"
    # Small value rounds to 0
    assert _compute_cache_key("Edit", 499) == "Edit:0"


# ---------------------------------------------------------------------------
# classify_anomaly async tests
# ---------------------------------------------------------------------------


async def test_classify_anomaly_cache_hit(
    db_connection: aiosqlite.Connection,
) -> None:
    """When a cached response exists, classify_anomaly returns it without calling the API."""
    cfg = ClassifierConfig(enabled=True, cache_results=True)

    # Pre-populate the cache table
    cache_key = _compute_cache_key("Bash", 5000)  # "Bash:5000"
    cached_data = json.dumps({
        "cause": "Large output from build",
        "severity": "high",
        "suggestion": "Limit build output.",
    })
    await db_connection.execute(
        "INSERT INTO classifier_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        (cache_key, cached_data, 1000000),
    )
    await db_connection.commit()

    # classify_anomaly should return the cached entry — no API call needed
    result = await classify_anomaly(
        db=db_connection,
        tool_name="Bash",
        tool_input_summary="npm run build",
        token_cost=5000,
        baseline_mean=1000.0,
        baseline_stddev=200.0,
        z_score=20.0,
        classifier_config=cfg,
    )

    assert result is not None
    assert result.cause == "Large output from build"
    assert result.severity == "high"
    assert result.suggestion == "Limit build output."


async def test_classify_anomaly_disabled(
    db_connection: aiosqlite.Connection,
) -> None:
    """When classifier_config.enabled is False, classify_anomaly returns None."""
    cfg = ClassifierConfig(enabled=False)

    result = await classify_anomaly(
        db=db_connection,
        tool_name="Bash",
        tool_input_summary="ls -la",
        token_cost=5000,
        baseline_mean=1000.0,
        baseline_stddev=200.0,
        z_score=20.0,
        classifier_config=cfg,
    )
    assert result is None


async def test_classify_anomaly_api_call_mocked(
    db_connection: aiosqlite.Connection,
) -> None:
    """When there is no cache hit, the Anthropic API is called (mocked)."""
    import sys

    cfg = ClassifierConfig(enabled=True, cache_results=False)

    # Build a mock response object that mimics the Anthropic SDK structure
    mock_text_block = MagicMock()
    mock_text_block.text = json.dumps({
        "cause": "Massive file read",
        "severity": "high",
        "suggestion": "Read only needed sections.",
    })
    mock_message = MagicMock()
    mock_message.content = [mock_text_block]

    mock_client_instance = MagicMock()
    mock_client_instance.messages = MagicMock()
    mock_client_instance.messages.create = AsyncMock(return_value=mock_message)

    # Build a mock anthropic module
    mock_anthropic_module = MagicMock()
    mock_anthropic_module.AsyncAnthropic.return_value = mock_client_instance
    mock_anthropic_module.RateLimitError = type("RateLimitError", (Exception,), {})
    mock_anthropic_module.APIConnectionError = type("APIConnectionError", (Exception,), {})

    # The classifier does a lazy `import anthropic` inside the function body,
    # so we inject our mock into sys.modules to intercept it.
    original = sys.modules.get("anthropic")
    sys.modules["anthropic"] = mock_anthropic_module
    try:
        result = await classify_anomaly(
            db=db_connection,
            tool_name="Read",
            tool_input_summary="cat /var/log/syslog",
            token_cost=8000,
            baseline_mean=2000.0,
            baseline_stddev=500.0,
            z_score=12.0,
            classifier_config=cfg,
        )
    finally:
        # Restore original state to avoid leaking the mock into other tests
        if original is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = original

    assert result is not None
    assert result.cause == "Massive file read"
    assert result.severity == "high"
    assert result.suggestion == "Read only needed sections."
    mock_client_instance.messages.create.assert_awaited_once()
