"""Tests for the burn rate projection engine."""

from context_pulse.engine.burn_rate import compute_burn_rate


def test_linear_growth_projection() -> None:
    """10 snapshots going from 10% to 55% should project ~9 turns remaining."""
    snapshots = [{"used_percentage": 10 + i * 5} for i in range(10)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is not None
    assert result["pct_per_turn"] == 5.0
    # At 55%, remaining = 45%, at 5%/turn = 9 turns
    assert result["turns_remaining"] == 9


def test_insufficient_data_returns_none() -> None:
    """Only 3 snapshots should return None (need at least 5)."""
    snapshots = [{"used_percentage": 10 + i * 5} for i in range(3)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is None


def test_flat_usage_returns_none() -> None:
    """All snapshots at same usage -- slope is 0, returns None."""
    snapshots = [{"used_percentage": 30} for _ in range(10)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is None


def test_decreasing_usage_returns_none() -> None:
    """Usage going down (post-compaction) -- negative slope, returns None."""
    snapshots = [{"used_percentage": 80 - i * 5} for i in range(10)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is None


def test_near_capacity() -> None:
    """Snapshots at 90-99% should show very few turns remaining."""
    snapshots = [{"used_percentage": 90 + i} for i in range(10)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is not None
    assert result["turns_remaining"] <= 2


def test_exact_full_capacity() -> None:
    """Already at 100% should return turns_remaining=0."""
    snapshots = [{"used_percentage": 50 + i * 6} for i in range(10)]
    # Last snapshot at 50 + 54 = 104, clamped logic doesn't matter,
    # remaining_pct would be negative
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is not None
    # slope is positive, remaining is <=0
    assert result["turns_remaining"] == 0


def test_slow_growth() -> None:
    """Slow growth (1% per turn) from 10% should show ~90 turns."""
    snapshots = [{"used_percentage": 10 + i} for i in range(10)]
    result = compute_burn_rate(snapshots, context_window_size=200_000)
    assert result is not None
    assert result["pct_per_turn"] == 1.0
    # At 19%, remaining = 81%, at 1%/turn = 81 turns
    assert result["turns_remaining"] == 81
