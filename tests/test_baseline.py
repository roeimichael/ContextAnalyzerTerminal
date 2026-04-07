"""Tests for the Welford algorithm and BaselineManager (Phase 2)."""

from __future__ import annotations

import math

import aiosqlite

from context_analyzer_tool.db import baselines as db_baselines
from context_analyzer_tool.engine.baseline import BaselineManager, RollingWelford

# ---------------------------------------------------------------------------
# RollingWelford unit tests
# ---------------------------------------------------------------------------


def test_empty_rolling_welford() -> None:
    """A fresh RollingWelford has mean=0, sample_count=0, variance=0."""
    w = RollingWelford(window_size=10)
    assert w.mean == 0.0
    assert w.sample_count == 0
    assert w.variance == 0.0


def test_known_sequence() -> None:
    """Feed [2, 4, 4, 4, 5, 5, 7, 9] and verify mean~5.0, stddev~2.0."""
    w = RollingWelford(window_size=20)
    for v in [2, 4, 4, 4, 5, 5, 7, 9]:
        w.update(float(v))

    assert w.sample_count == 8
    assert math.isclose(w.mean, 5.0, abs_tol=1e-9)
    # Sample stddev of [2,4,4,4,5,5,7,9] = sqrt(32/7) ≈ 2.138
    assert math.isclose(w.stddev, 2.138, abs_tol=0.01)


def test_rolling_window_eviction() -> None:
    """With window_size=3, after feeding 5 values only the last 3 matter."""
    w = RollingWelford(window_size=3)
    for v in [100.0, 200.0, 10.0, 20.0, 30.0]:
        w.update(v)

    # Only [10, 20, 30] should be in the window
    assert w.sample_count == 3
    assert math.isclose(w.mean, 20.0, abs_tol=1e-6)
    # stddev of [10, 20, 30] = sqrt(100) = 10.0 (sample stddev)
    assert math.isclose(w.stddev, 10.0, abs_tol=0.01)


def test_m2_clamping() -> None:
    """After a remove step, m2 should never go negative."""
    w = RollingWelford(window_size=3)
    # Feed identical values so m2 is essentially zero, then evict
    for v in [5.0, 5.0, 5.0]:
        w.update(v)
    # This triggers a remove of 5.0 then add of 5.0 — m2 must stay >= 0
    w.update(5.0)
    assert w.get_m2() >= 0.0
    assert w.variance >= 0.0


def test_serialization_roundtrip() -> None:
    """to_dict() then from_dict() preserves state exactly."""
    w = RollingWelford(window_size=10)
    for v in [3.0, 7.0, 11.0, 15.0, 19.0]:
        w.update(v)

    data = w.to_dict()
    restored = RollingWelford.from_dict(data, window_size=10)

    assert restored.sample_count == w.sample_count
    assert math.isclose(restored.mean, w.mean, abs_tol=1e-12)
    assert math.isclose(restored.get_m2(), w.get_m2(), abs_tol=1e-12)
    assert restored.get_window_list() == w.get_window_list()
    assert math.isclose(restored.variance, w.variance, abs_tol=1e-12)
    assert math.isclose(restored.stddev, w.stddev, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# BaselineManager async tests
# ---------------------------------------------------------------------------


async def test_baseline_manager_record_and_get(
    db_connection: aiosqlite.Connection,
) -> None:
    """Record 10 deltas and verify get_baseline returns correct stats."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
    values = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]

    for v in values:
        await mgr.record_delta("Bash", v)

    baseline = await mgr.get_baseline("Bash")
    assert baseline.sample_count == 10
    assert math.isclose(baseline.mean, 550.0, abs_tol=1e-6)


async def test_baseline_manager_persists_periodically(
    db_connection: aiosqlite.Connection,
) -> None:
    """With update_interval=3, DB should be written after every 3 samples."""
    mgr = BaselineManager(db=db_connection, window_size=20, update_interval=3)

    # After 2 samples — nothing in DB yet
    await mgr.record_delta("Read", 100.0)
    await mgr.record_delta("Read", 200.0)
    row = await db_baselines.get_baseline(db_connection, "Read")
    assert row is None, "DB should not be written before update_interval is reached"

    # Third sample triggers persist
    await mgr.record_delta("Read", 300.0)
    row = await db_baselines.get_baseline(db_connection, "Read")
    assert row is not None, "DB should be written after update_interval samples"
    assert row["sample_count"] == 3
    assert math.isclose(row["mean"], 200.0, abs_tol=1e-6)

    # After 3 more samples (6 total), another persist
    await mgr.record_delta("Read", 400.0)
    await mgr.record_delta("Read", 500.0)
    await mgr.record_delta("Read", 600.0)
    row = await db_baselines.get_baseline(db_connection, "Read")
    assert row is not None
    assert row["sample_count"] == 6
    assert math.isclose(row["mean"], 350.0, abs_tol=1e-6)
