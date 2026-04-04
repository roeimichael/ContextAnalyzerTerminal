"""Z-score anomaly detector for context-pulse (Phase 2).

Evaluates token deltas against a rolling baseline and flags statistical
outliers.  Classification of root cause is delegated to the classifier
module on a best-effort basis.
"""

from __future__ import annotations

import logging

import aiosqlite

from context_pulse.collector.models import AnomalyResult
from context_pulse.config import AnomalyConfig, ClassifierConfig
from context_pulse.db import anomalies as db_anomalies
from context_pulse.engine.baseline import BaselineManager
from context_pulse.engine.classifier import classify_anomaly

logger = logging.getLogger("context_pulse.engine.anomaly")

MIN_STDDEV: float = 100.0

# Tool-specific suggestions for the fallback classifier
_TOOL_SUGGESTIONS: dict[str, str] = {
    "Read": "Use offset/limit to read only the lines you need",
    "Bash": "Pipe output through head/tail or use rtk to reduce output",
    "Grep": "Narrow the search with a more specific path or glob filter",
    "Glob": "Use a more specific pattern to reduce matches",
    "Write": "Check if the file content is larger than necessary",
    "Edit": "Verify the edit scope — large old_string values add cost",
    "Agent": "Consider whether a lighter subagent_type would suffice",
    "WebFetch": "Fetch only the data you need; avoid large pages",
}


def _fallback_classify(
    task_type: str,
    token_cost: int,
    z_score: float,
    baseline_mean: float,
) -> tuple[str, str, str]:
    """Rule-based classification when the LLM classifier is unavailable.

    Returns (cause, severity, suggestion).
    """
    if token_cost > 10_000:
        severity = "high"
    elif token_cost > 3_000:
        severity = "medium"
    else:
        severity = "low"

    ratio = token_cost / max(baseline_mean, 1)
    cause = f"Large {task_type} output ({token_cost:,} tokens, {ratio:.1f}x baseline)"
    suggestion = _TOOL_SUGGESTIONS.get(
        task_type, f"Review whether this {task_type} call can be scoped down"
    )
    return cause, severity, suggestion
"""Minimum stddev floor to prevent division by zero on uniform data."""


def compute_z_score(value: float, mean: float, stddev: float) -> float:
    """Compute the Z-score for *value* given *mean* and *stddev*.

    Uses ``max(stddev, MIN_STDDEV)`` as the effective standard deviation
    to avoid division by zero when all samples are identical.

    Parameters
    ----------
    value:
        The observed value (e.g. token delta).
    mean:
        The baseline mean.
    stddev:
        The baseline standard deviation.

    Returns
    -------
    float
        The Z-score.
    """
    effective_stddev = max(stddev, MIN_STDDEV)
    return (value - mean) / effective_stddev


async def detect_anomaly(
    baseline_manager: BaselineManager,
    db: aiosqlite.Connection,
    task_id: int,
    session_id: str,
    task_type: str,
    token_delta: int,
    tool_input_summary: str | None,
    timestamp_ms: int,
    anomaly_config: AnomalyConfig,
    classifier_config: ClassifierConfig,
) -> AnomalyResult | None:
    """Evaluate a single task delta for anomaly status.

    Steps
    -----
    a) Skip if task_type is in anomaly_config.task_types_ignored.
    b) Skip if token_delta <= 0 (compaction or zero-cost).
       Zero deltas are still fed to the baseline as valid data points.
    c) Record delta in baseline_manager (updates the rolling window).
    d) Get the current baseline (mean, stddev, sample_count).
    e) If sample_count < anomaly_config.min_sample_count: return None
       (learning mode).
    f) Compute z_score via compute_z_score().
    g) If z_score < anomaly_config.z_score_threshold: return None (normal).
    h) Check cooldown via db_anomalies.check_cooldown().
    i) If in cooldown: return None.
    j) Insert anomaly into DB via db_anomalies.insert_anomaly().
    k) If classifier_config.enabled: call classify_anomaly() as best-effort
       (try/except, log errors).
    l) If classifier returned a result: update_anomaly_classification().
    m) Build and return AnomalyResult.

    Parameters
    ----------
    baseline_manager:
        The shared BaselineManager instance.
    db:
        An open aiosqlite connection.
    task_id:
        The id of the task row that produced this delta.
    session_id:
        The session this task belongs to.
    task_type:
        The tool/task type string (e.g. "Bash", "Read").
    token_delta:
        The computed token delta for this task.
    tool_input_summary:
        Truncated description of what the tool did (for classifier context).
    timestamp_ms:
        When the task occurred (unix ms).
    anomaly_config:
        The [anomaly] section of the config.
    classifier_config:
        The [classifier] section of the config.

    Returns
    -------
    AnomalyResult | None
        The anomaly result if an anomaly was detected, otherwise None.
    """
    try:
        return await _detect_anomaly_inner(
            baseline_manager=baseline_manager,
            db=db,
            task_id=task_id,
            session_id=session_id,
            task_type=task_type,
            token_delta=token_delta,
            tool_input_summary=tool_input_summary,
            timestamp_ms=timestamp_ms,
            anomaly_config=anomaly_config,
            classifier_config=classifier_config,
        )
    except Exception:
        logger.exception(
            "Unhandled error in detect_anomaly for task_id=%d session=%s",
            task_id,
            session_id,
        )
        return None


async def _detect_anomaly_inner(
    baseline_manager: BaselineManager,
    db: aiosqlite.Connection,
    task_id: int,
    session_id: str,
    task_type: str,
    token_delta: int,
    tool_input_summary: str | None,
    timestamp_ms: int,
    anomaly_config: AnomalyConfig,
    classifier_config: ClassifierConfig,
) -> AnomalyResult | None:
    """Core anomaly detection logic (may raise)."""

    # (a) Ignore excluded task types
    if task_type in anomaly_config.task_types_ignored:
        return None

    # (b) Skip non-positive deltas (compaction or cached/zero-cost)
    if token_delta <= 0:
        # Zero deltas are valid data points — feed to baseline
        if token_delta == 0:
            await baseline_manager.record_delta(task_type, float(token_delta))
        return None

    # (c) Record delta in baseline (updates the rolling window)
    baseline = await baseline_manager.record_delta(
        task_type, float(token_delta)
    )

    # (d) Get current baseline stats
    mean = baseline.mean
    stddev = baseline.stddev
    sample_count = baseline.sample_count

    # (e) Learning mode gate
    if sample_count < anomaly_config.min_sample_count:
        logger.debug(
            "Learning mode for %s: %d/%d samples",
            task_type,
            sample_count,
            anomaly_config.min_sample_count,
        )
        return None

    # (f) Compute z-score
    z_score = compute_z_score(float(token_delta), mean, stddev)
    effective_stddev = max(stddev, MIN_STDDEV)

    # (g) Threshold gate
    if z_score < anomaly_config.z_score_threshold:
        return None

    # (h) Check cooldown deduplication
    cooldown_since_ms = timestamp_ms - (anomaly_config.cooldown_seconds * 1000)
    is_in_cooldown = await db_anomalies.check_cooldown(
        db,
        session_id=session_id,
        task_type=task_type,
        since_ms=cooldown_since_ms,
    )

    # (i) If in cooldown, skip
    if is_in_cooldown:
        logger.debug(
            "Cooldown active for session=%s task_type=%s, skipping",
            session_id,
            task_type,
        )
        return None

    # (j) Insert anomaly row into DB
    anomaly_id = await db_anomalies.insert_anomaly(
        db,
        session_id=session_id,
        task_type=task_type,
        token_cost=token_delta,
        z_score=z_score,
        cause=None,
        severity=None,
        suggestion=None,
        timestamp_ms=timestamp_ms,
    )

    logger.info(
        "Anomaly detected: task_type=%s delta=%d z=%.2f mean=%.0f stddev=%.0f",
        task_type,
        token_delta,
        z_score,
        mean,
        effective_stddev,
    )

    # (k) Classifier — best-effort, never blocks or raises
    classification = None
    if classifier_config.enabled:
        try:
            classification = await classify_anomaly(
                db=db,
                tool_name=task_type,
                tool_input_summary=tool_input_summary,
                token_cost=token_delta,
                baseline_mean=mean,
                baseline_stddev=effective_stddev,
                z_score=z_score,
                classifier_config=classifier_config,
            )
        except Exception:
            logger.exception(
                "Classifier failed for anomaly_id=%d; continuing without classification",
                anomaly_id,
            )

    # (l) If classifier returned a result, enhance with RTK suggestion and persist
    if classification is not None:
        try:
            suggestion = classification.suggestion
            # Enhance Bash anomalies with RTK recommendation
            if task_type == "Bash":
                try:
                    from context_pulse.rtk_integration import (
                        enhance_suggestion_with_rtk,
                    )

                    suggestion = enhance_suggestion_with_rtk(task_type, suggestion)
                except Exception:
                    pass  # RTK module not available, skip enhancement
            await db_anomalies.update_anomaly_classification(
                db,
                anomaly_id=anomaly_id,
                cause=classification.cause,
                severity=classification.severity,
                suggestion=suggestion,
            )
        except Exception:
            logger.exception(
                "Failed to update classification for anomaly_id=%d",
                anomaly_id,
            )
    else:
        # Rule-based fallback when LLM classifier is unavailable
        fb_cause, fb_severity, fb_suggestion = _fallback_classify(
            task_type, token_delta, z_score, mean,
        )
        try:
            await db_anomalies.update_anomaly_classification(
                db,
                anomaly_id=anomaly_id,
                cause=fb_cause,
                severity=fb_severity,
                suggestion=fb_suggestion,
            )
        except Exception:
            logger.debug("Failed to write fallback classification", exc_info=True)

    # (m) Build and return result
    return AnomalyResult(
        task_id=task_id,
        session_id=session_id,
        task_type=task_type,
        token_delta=token_delta,
        z_score=z_score,
        baseline_mean=mean,
        baseline_stddev=effective_stddev,
        baseline_sample_count=sample_count,
        timestamp_ms=timestamp_ms,
    )
