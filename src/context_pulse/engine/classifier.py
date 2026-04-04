"""Haiku classifier for anomaly root-cause analysis (Phase 2).

Calls an Anthropic model (default: claude-haiku-4-5) to classify *why* a
tool call consumed an unusual number of tokens.  Results are cached in
SQLite to avoid redundant API calls.

The ``anthropic`` package is imported **lazily** inside
:func:`classify_anomaly` so that the tool works without it installed when
the classifier is disabled in config.
"""

from __future__ import annotations

import json
import logging
import time

import aiosqlite

from context_pulse.collector.models import ClassifierResponse
from context_pulse.config import ClassifierConfig

logger = logging.getLogger("context_pulse.engine.classifier")

# ---------------------------------------------------------------------------
# System prompt — per project brief section 8
# ---------------------------------------------------------------------------

CLASSIFIER_SYSTEM_PROMPT: str = (
    "You are a terse analyst classifying why a Claude Code tool call "
    "consumed unusually many tokens.\n"
    "Respond ONLY with valid JSON: "
    '{"cause": str, "severity": "low|medium|high", "suggestion": str}\n'
    "cause: 1 sentence. suggestion: 1 actionable sentence. No other text."
)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_user_prompt(
    tool_name: str,
    tool_input_summary: str | None,
    token_cost: int,
    baseline_mean: float,
    baseline_stddev: float,
    z_score: float,
) -> str:
    """Build the user message for the classifier call."""
    summary = tool_input_summary or "(no input summary available)"
    return (
        f"Tool: {tool_name}\n"
        f"Input summary: {summary}\n"
        f"Token cost: {token_cost} "
        f"(baseline for this tool: {baseline_mean:.0f} +/- {baseline_stddev:.0f}, "
        f"z-score: {z_score:.1f})"
    )


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


def _compute_cache_key(tool_name: str, token_delta: int) -> str:
    """Compute cache key: tool_name + delta rounded to nearest 1000."""
    bucket = round(token_delta / 1000) * 1000
    return f"{tool_name}:{bucket}"


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------


async def _get_cached_response(
    db: aiosqlite.Connection,
    cache_key: str,
) -> ClassifierResponse | None:
    """Look up a cached classifier response.  Returns ``None`` on miss."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT response_json FROM classifier_cache WHERE cache_key = ?",
        (cache_key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["response_json"])
        return ClassifierResponse(**data)
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Corrupt cache entry for key=%s, ignoring", cache_key)
        return None


async def _set_cached_response(
    db: aiosqlite.Connection,
    cache_key: str,
    response: ClassifierResponse,
) -> None:
    """Write a classifier response to the cache."""
    response_json = json.dumps(
        {
            "cause": response.cause,
            "severity": response.severity,
            "suggestion": response.suggestion,
        }
    )
    await db.execute(
        "INSERT OR REPLACE INTO classifier_cache (cache_key, response_json, created_at) "
        "VALUES (?, ?, ?)",
        (cache_key, response_json, int(time.time() * 1000)),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def _parse_classifier_output(raw_text: str) -> ClassifierResponse:
    """Parse the raw Haiku response text into a :class:`ClassifierResponse`.

    Attempts JSON parse first.  On failure, returns a generic fallback.
    """
    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
        cause = str(data.get("cause", "Unknown cause"))
        severity = str(data.get("severity", "medium"))
        suggestion = str(data.get("suggestion", "Review the tool call."))

        # Validate severity
        if severity not in ("low", "medium", "high"):
            severity = "medium"

        return ClassifierResponse(
            cause=cause,
            severity=severity,
            suggestion=suggestion,
        )
    except (json.JSONDecodeError, AttributeError):
        logger.warning(
            "Failed to parse classifier JSON, using fallback. Raw: %s",
            raw_text[:200],
        )
        return ClassifierResponse(
            cause="Token spike detected (classifier response unparseable).",
            severity="medium",
            suggestion="Review the tool call input for unusually large data.",
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def classify_anomaly(
    db: aiosqlite.Connection,
    tool_name: str,
    tool_input_summary: str | None,
    token_cost: int,
    baseline_mean: float,
    baseline_stddev: float,
    z_score: float,
    classifier_config: ClassifierConfig,
) -> ClassifierResponse | None:
    """Call the Haiku classifier for an anomaly.

    1. Compute cache key from ``(tool_name, token_delta_bucket)``.
    2. If ``cache_results`` is enabled, check cache first.
    3. On cache miss, call the Anthropic SDK.
    4. Parse the response.
    5. Cache the result if caching is enabled.
    6. Return the :class:`ClassifierResponse`.

    Returns ``None`` if the classifier is disabled or the call fails
    entirely.

    Parameters
    ----------
    db:
        An open aiosqlite connection (for cache reads/writes).
    tool_name:
        The tool that caused the anomaly.
    tool_input_summary:
        Truncated description of what the tool did.
    token_cost:
        The token delta that triggered the anomaly.
    baseline_mean:
        Current baseline mean for this tool type.
    baseline_stddev:
        Current baseline stddev for this tool type.
    z_score:
        The computed z-score.
    classifier_config:
        The ``[classifier]`` section of the config.

    Returns
    -------
    ClassifierResponse | None
    """
    if not classifier_config.enabled:
        return None

    cache_key = _compute_cache_key(tool_name, token_cost)

    # ---- Check cache ----
    if classifier_config.cache_results:
        cached = await _get_cached_response(db, cache_key)
        if cached is not None:
            logger.debug("Classifier cache hit for key=%s", cache_key)
            return cached

    # ---- Build prompt ----
    user_prompt = _build_user_prompt(
        tool_name=tool_name,
        tool_input_summary=tool_input_summary,
        token_cost=token_cost,
        baseline_mean=baseline_mean,
        baseline_stddev=baseline_stddev,
        z_score=z_score,
    )

    # ---- Call Anthropic SDK (lazy import) ----
    try:
        import anthropic  # noqa: I001
    except ImportError:
        logger.error(
            "anthropic package not installed. "
            "Install with: pip install anthropic"
        )
        return None

    try:
        client = anthropic.AsyncAnthropic()  # uses ANTHROPIC_API_KEY env var
        message = await client.messages.create(
            model=classifier_config.model,
            max_tokens=classifier_config.max_tokens,
            temperature=0,
            system=CLASSIFIER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        # Extract text from response
        raw_text = ""
        for block in message.content:
            if hasattr(block, "text"):
                raw_text += str(getattr(block, "text", ""))
        if not raw_text:
            logger.warning("Classifier returned empty response")
            return None

    except anthropic.RateLimitError:
        logger.warning("Classifier rate-limited, skipping classification")
        return None
    except anthropic.APIConnectionError:
        logger.warning("Classifier network error, skipping classification")
        return None
    except Exception:
        logger.exception("Unexpected classifier error")
        return None

    # ---- Parse response ----
    result = _parse_classifier_output(str(raw_text))

    # ---- Cache result ----
    if classifier_config.cache_results:
        try:
            await _set_cached_response(db, cache_key, result)
            logger.debug("Cached classifier response for key=%s", cache_key)
        except Exception:
            logger.exception("Failed to cache classifier response")

    return result
