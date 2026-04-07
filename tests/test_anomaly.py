"""Tests for the Z-score anomaly detector (Phase 2)."""

from __future__ import annotations

import time

import aiosqlite

from context_analyzer_tool.config import AnomalyConfig, ClassifierConfig
from context_analyzer_tool.engine.anomaly import detect_anomaly
from context_analyzer_tool.engine.baseline import BaselineManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _anomaly_config(**overrides: object) -> AnomalyConfig:
    """Return an AnomalyConfig with sensible test defaults."""
    defaults: dict[str, object] = {
        "z_score_threshold": 2.0,
        "min_sample_count": 5,
        "cooldown_seconds": 60,
        "task_types_ignored": [],
        "baseline_window": 20,
    }
    defaults.update(overrides)
    return AnomalyConfig(**defaults)  # type: ignore[arg-type]


def _classifier_config_disabled() -> ClassifierConfig:
    """Classifier always disabled for anomaly-detector tests."""
    return ClassifierConfig(enabled=False)


async def _populate_baseline(
    mgr: BaselineManager,
    task_type: str,
    values: list[float],
) -> None:
    """Feed a list of values into the baseline manager."""
    for v in values:
        await mgr.record_delta(task_type, v)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_learning_mode(db_connection: aiosqlite.Connection) -> None:
    """Fewer than min_sample_count samples returns None (learning mode)."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    # Feed only 3 samples (below the min of 5)
    await _populate_baseline(mgr, "Bash", [100.0, 200.0, 300.0])

    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=1,
        session_id="sess-1",
        task_type="Bash",
        token_delta=10000,
        tool_input_summary="big read",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    # The 10000 delta feeds as the 4th sample — still < min_sample_count=5
    assert result is None


async def test_normal_delta(db_connection: aiosqlite.Connection) -> None:
    """A delta with z-score below threshold returns None."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    # Build a baseline with reasonable spread
    await _populate_baseline(mgr, "Read", [500.0, 600.0, 550.0, 580.0, 520.0, 560.0])

    # Delta of 600 is within normal range of ~550 mean
    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=10,
        session_id="sess-1",
        task_type="Read",
        token_delta=600,
        tool_input_summary="read file",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is None


async def test_spike_detected(db_connection: aiosqlite.Connection) -> None:
    """A very high delta triggers an anomaly result."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    # Build a tight baseline around ~500
    await _populate_baseline(mgr, "Bash", [500.0, 510.0, 490.0, 505.0, 495.0, 500.0])

    # Delta of 50000 is a massive spike
    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=20,
        session_id="sess-2",
        task_type="Bash",
        token_delta=50000,
        tool_input_summary="huge output",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is not None
    assert result.task_type == "Bash"
    assert result.token_delta == 50000
    assert result.z_score > 2.0


async def test_negative_delta_skipped(db_connection: aiosqlite.Connection) -> None:
    """Negative delta (compaction) returns None."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=30,
        session_id="sess-3",
        task_type="Bash",
        token_delta=-500,
        tool_input_summary="compaction",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is None


async def test_zero_delta_skipped(db_connection: aiosqlite.Connection) -> None:
    """Zero delta returns None (but is still fed to baseline)."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=31,
        session_id="sess-3",
        task_type="Edit",
        token_delta=0,
        tool_input_summary="no-op",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is None

    # Verify the zero was fed into the baseline
    baseline = await mgr.get_baseline("Edit")
    assert baseline.sample_count == 1


async def test_ignored_task_type(db_connection: aiosqlite.Connection) -> None:
    """A task_type in the ignored list returns None."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(task_types_ignored=["TodoRead", "Glob"])

    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=40,
        session_id="sess-4",
        task_type="TodoRead",
        token_delta=99999,
        tool_input_summary="ignored",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is None


async def test_cooldown_prevents_duplicate(
    db_connection: aiosqlite.Connection,
) -> None:
    """A second anomaly within cooldown_seconds returns None."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5, cooldown_seconds=60)

    # Build a tight baseline
    await _populate_baseline(mgr, "Bash", [500.0, 510.0, 490.0, 505.0, 495.0, 500.0])

    now_ms = int(time.time() * 1000)

    # First spike — should be detected
    result1 = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=50,
        session_id="sess-5",
        task_type="Bash",
        token_delta=50000,
        tool_input_summary="spike 1",
        timestamp_ms=now_ms,
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result1 is not None

    # Second spike within cooldown — should be suppressed
    result2 = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=51,
        session_id="sess-5",
        task_type="Bash",
        token_delta=50000,
        tool_input_summary="spike 2",
        timestamp_ms=now_ms + 5000,  # 5s later, within 60s cooldown
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result2 is None


async def test_min_stddev_floor(db_connection: aiosqlite.Connection) -> None:
    """All-same baselines still compute z-score correctly via MIN_STDDEV floor."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    cfg = _anomaly_config(min_sample_count=5)

    # All values identical — stddev will be 0, but MIN_STDDEV=100 is used
    await _populate_baseline(mgr, "Edit", [1000.0] * 6)

    # Delta of 1300 with mean=1000, effective_stddev=100 => z = 3.0 > threshold
    result = await detect_anomaly(
        baseline_manager=mgr,
        db=db_connection,
        task_id=60,
        session_id="sess-6",
        task_type="Edit",
        token_delta=1300,
        tool_input_summary="bigger edit",
        timestamp_ms=int(time.time() * 1000),
        anomaly_config=cfg,
        classifier_config=_classifier_config_disabled(),
    )
    assert result is not None
    # z = (1300 - ~1000) / 100 = ~3.0
    assert result.z_score > 2.0
