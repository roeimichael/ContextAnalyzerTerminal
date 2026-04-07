from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import cast

import aiosqlite

from context_analyzer_tool.db import baselines as db_baselines

logger = logging.getLogger("context_analyzer_tool.engine.baseline")


class RollingWelford:
    """Incremental mean/variance over a fixed-size rolling window.

    Uses Welford's online algorithm. When the window is full, the oldest
    value is removed before the new value is added.

    Parameters
    ----------
    window_size:
        Maximum number of samples in the rolling window.
    """

    def __init__(self, window_size: int = 20) -> None:
        self._window_size: int = window_size
        self._window: deque[float] = deque(maxlen=window_size)
        self._n: int = 0
        self._mean: float = 0.0
        self._m2: float = 0.0

    @property
    def sample_count(self) -> int:
        """Number of samples currently in the window."""
        return self._n

    @property
    def mean(self) -> float:
        """Current mean of the window."""
        return self._mean

    @property
    def variance(self) -> float:
        """Sample variance (m2 / (n-1)). Returns 0.0 if n < 2."""
        if self._n < 2:
            return 0.0
        return self._m2 / (self._n - 1)

    @property
    def stddev(self) -> float:
        """Sample standard deviation (sqrt of variance)."""
        return self.variance ** 0.5

    def update(self, value: float) -> None:
        """Add a new value to the rolling window.

        If the window is full, the oldest value is removed first
        (using the reverse Welford step), then the new value is added.
        """
        if self._n >= self._window_size:
            # Remove the oldest value
            oldest = self._window[0]  # will be auto-popped by deque
            self._remove(oldest)

        self._add(value)
        self._window.append(value)

    def _add(self, x: float) -> None:
        """Welford add step."""
        self._n += 1
        delta = x - self._mean
        self._mean += delta / self._n
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def _remove(self, x: float) -> None:
        """Welford remove step (reverse). Clamps M2 to max(0, M2)."""
        if self._n <= 1:
            self._n = 0
            self._mean = 0.0
            self._m2 = 0.0
            return
        old_mean = self._mean
        self._n -= 1
        delta = x - old_mean
        self._mean = (old_mean * (self._n + 1) - x) / self._n
        delta2 = x - self._mean
        self._m2 -= delta * delta2
        self._m2 = max(0.0, self._m2)  # clamp to prevent negative from float drift

    def to_dict(self) -> dict[str, object]:
        """Serialize state for DB persistence."""
        return {
            "n": self._n,
            "mean": self._mean,
            "m2": self._m2,
            "window": list(self._window),
        }

    def get_m2(self) -> float:
        """Return raw M2 accumulator (for persistence)."""
        return self._m2

    def get_window_list(self) -> list[float]:
        """Return a copy of the window as a plain list (for persistence)."""
        return list(self._window)

    @classmethod
    def from_dict(
        cls, data: dict[str, object], window_size: int = 20
    ) -> RollingWelford:
        """Restore state from DB persistence."""
        w = cls(window_size=window_size)
        raw_n = data.get("n", 0)
        raw_mean = data.get("mean", 0.0)
        raw_m2 = data.get("m2", 0.0)
        w._n = int(raw_n) if isinstance(raw_n, (int, float, str)) else 0  # noqa: SLF001
        w._mean = float(raw_mean) if isinstance(raw_mean, (int, float, str)) else 0.0  # noqa: SLF001
        w._m2 = float(raw_m2) if isinstance(raw_m2, (int, float, str)) else 0.0  # noqa: SLF001
        raw_window: object = data.get("window", [])
        if isinstance(raw_window, list):
            window_vals = cast(list[object], raw_window)
            converted: list[float] = []
            for v in window_vals:
                if isinstance(v, (int, float, str)):
                    converted.append(float(v))
            w._window = deque(  # noqa: SLF001
                converted,
                maxlen=window_size,
            )
        return w


class BaselineManager:
    """Manages per-task_type baselines with DB persistence.

    Holds an in-memory cache of RollingWelford instances keyed by task_type.
    Loads from DB on first access, persists after updates.

    Parameters
    ----------
    db:
        An open aiosqlite connection.
    window_size:
        Rolling window size (from config.anomaly.baseline_window).
    update_interval:
        Number of new samples before persisting to DB. Default 5.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        window_size: int = 20,
        update_interval: int = 5,
    ) -> None:
        self._db: aiosqlite.Connection = db
        self._window_size: int = window_size
        self._update_interval: int = update_interval
        self._baselines: dict[str, RollingWelford] = {}
        self._update_counters: dict[str, int] = {}

    async def get_baseline(self, task_type: str) -> RollingWelford:
        """Return the RollingWelford for *task_type*, loading from DB if needed."""
        if task_type not in self._baselines:
            row = await db_baselines.get_baseline(self._db, task_type)
            if row is not None:
                data: dict[str, object] = {
                    "n": row["sample_count"],
                    "mean": row["mean"],
                    "m2": row["m2"],
                    "window": json.loads(row["window_json"]),
                }
                self._baselines[task_type] = RollingWelford.from_dict(
                    data, window_size=self._window_size
                )
            else:
                self._baselines[task_type] = RollingWelford(
                    window_size=self._window_size
                )
            self._update_counters[task_type] = 0
        return self._baselines[task_type]

    async def record_delta(self, task_type: str, delta: float) -> RollingWelford:
        """Add a delta to the baseline for *task_type*.

        Increments the update counter. When the counter reaches
        ``update_interval``, persists to DB and resets the counter.

        Returns the updated RollingWelford instance.
        """
        baseline = await self.get_baseline(task_type)
        baseline.update(delta)

        counter = self._update_counters.get(task_type, 0) + 1
        self._update_counters[task_type] = counter

        if counter >= self._update_interval:
            await self._persist(task_type, baseline)
            self._update_counters[task_type] = 0

        return baseline

    async def _persist(
        self, task_type: str, baseline: RollingWelford
    ) -> None:
        """Write the current baseline state to the DB."""
        await db_baselines.upsert_baseline(
            self._db,
            task_type=task_type,
            mean=baseline.mean,
            stddev=baseline.stddev,
            sample_count=baseline.sample_count,
            m2=baseline.get_m2(),
            window_json=json.dumps(baseline.get_window_list()),
            updated_at=int(time.time() * 1000),
        )
        logger.debug(
            "Persisted baseline for %s: mean=%.1f stddev=%.1f n=%d",
            task_type,
            baseline.mean,
            baseline.stddev,
            baseline.sample_count,
        )

    async def flush_all(self) -> None:
        """Persist all dirty baselines to DB. Call on shutdown."""
        for task_type, baseline in self._baselines.items():
            if self._update_counters.get(task_type, 0) > 0:
                await self._persist(task_type, baseline)
                self._update_counters[task_type] = 0
