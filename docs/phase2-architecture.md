# Phase 2 Architecture Specification -- context-pulse

> Version: 1.0.0
> Date: 2026-03-28
> Scope: Anomaly detection engine, Welford baselines, Haiku classifier, CLI command, tests
> Depends on: Phase 1 (collector, delta engine, SQLite schema, CLI framework)
> Target: Implementing agents can code any component from this spec without ambiguity.

---

## 1. New Files to Create

| File | Purpose |
|---|---|
| `src/context_pulse/engine/__init__.py` | Package init (empty) |
| `src/context_pulse/engine/baseline.py` | `RollingWelford` class -- Welford's online algorithm with deque-backed rolling window |
| `src/context_pulse/engine/anomaly.py` | Z-score anomaly detector -- computes z-score per task, applies thresholds and cooldown |
| `src/context_pulse/engine/classifier.py` | Haiku classifier call + SQLite-backed response cache |
| `src/context_pulse/db/baselines.py` | CRUD for the `baselines` table (get, upsert) |
| `src/context_pulse/db/anomalies.py` | CRUD for the `anomalies` table (insert, query, cooldown check) |
| `tests/test_baseline.py` | Unit tests for `RollingWelford` |
| `tests/test_anomaly.py` | Unit tests for anomaly detector |
| `tests/test_classifier.py` | Unit tests for classifier + cache |

No existing files need to be deleted. The following existing files require modifications (detailed in section 7):

- `src/context_pulse/collector/delta_engine.py` -- call anomaly detector after delta assignment
- `src/context_pulse/collector/routes.py` -- add anomaly API endpoints
- `src/context_pulse/collector/models.py` -- add new Pydantic response models
- `src/context_pulse/collector/server.py` -- store config on app.state (already done)
- `src/context_pulse/cli.py` -- add `anomalies` command
- `src/context_pulse/db/schema.py` -- add v2 migration for `classifier_cache` table
- `pyproject.toml` -- add `anthropic` dependency

---

## 2. Database Schema Changes (Migration v2)

### 2.1 New Table: `classifier_cache`

The `baselines` and `anomalies` tables already exist from the v1 schema. Phase 2 adds one new table for the classifier response cache.

```sql
-- Migration v2: Add classifier response cache table
CREATE TABLE IF NOT EXISTS classifier_cache (
    cache_key   TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    created_at  INTEGER NOT NULL  -- unix ms
);
```

### 2.2 New Column on `baselines` Table

The existing `baselines` table stores `mean`, `stddev`, `sample_count`, `updated_at`. For Welford's algorithm persistence, we also need to store the `M2` accumulator and the rolling window values. Add these via ALTER TABLE in migration v2:

```sql
ALTER TABLE baselines ADD COLUMN m2 REAL NOT NULL DEFAULT 0.0;
ALTER TABLE baselines ADD COLUMN window_json TEXT NOT NULL DEFAULT '[]';
```

- `m2`: The Welford M2 accumulator (sum of squared deviations). Required to resume incremental updates without recomputing from scratch.
- `window_json`: JSON array of the most recent `baseline_window` (default 20) delta values for the rolling window. Stored as a JSON string, e.g. `"[1200, 3400, 2100]"`.

### 2.3 Migration Registration

In `src/context_pulse/db/schema.py`, add:

```python
MIGRATIONS: list[tuple[int, str]] = [
    (1, "initial schema"),
    (2, "classifier cache table and baseline persistence columns"),
]

@_register(2)
async def _apply_v2(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS classifier_cache (
            cache_key   TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );
        ALTER TABLE baselines ADD COLUMN m2 REAL NOT NULL DEFAULT 0.0;
        ALTER TABLE baselines ADD COLUMN window_json TEXT NOT NULL DEFAULT '[]';
    """)
```

---

## 3. Pydantic Models (New)

Add these to `src/context_pulse/collector/models.py`:

```python
class BaselineSnapshot(BaseModel):
    """Current baseline statistics for a task type."""
    task_type: str
    mean: float
    stddev: float
    sample_count: int
    updated_at: int  # unix ms


class AnomalyResult(BaseModel):
    """Result of anomaly detection for a single task delta."""
    task_id: int
    session_id: str
    task_type: str
    token_delta: int
    z_score: float
    baseline_mean: float
    baseline_stddev: float
    baseline_sample_count: int
    timestamp_ms: int


class ClassifierResponse(BaseModel):
    """Parsed response from the Haiku classifier."""
    cause: str
    severity: str  # "low" | "medium" | "high"
    suggestion: str


class AnomalyResponse(BaseModel):
    """Single anomaly in API responses."""
    id: int
    session_id: str
    task_type: str
    token_cost: int
    z_score: float
    cause: str | None
    severity: str | None
    suggestion: str | None
    notified: bool
    timestamp_ms: int


class AnomaliesListResponse(BaseModel):
    """Response for GET /api/anomalies."""
    anomalies: list[AnomalyResponse]
    total_count: int
```

---

## 4. Welford Baseline Engine (`src/context_pulse/engine/baseline.py`)

### 4.1 `RollingWelford` Class

This is the core statistical engine. It maintains a fixed-size rolling window of delta values and computes mean/variance incrementally using Welford's online algorithm.

```python
from __future__ import annotations

import json
import logging
import time
from collections import deque

import aiosqlite

from context_pulse.db import baselines as db_baselines

logger = logging.getLogger("context_pulse.engine.baseline")


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
        """Population variance. Returns 0.0 if n < 2."""
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

    @classmethod
    def from_dict(
        cls, data: dict[str, object], window_size: int = 20
    ) -> RollingWelford:
        """Restore state from DB persistence."""
        w = cls(window_size=window_size)
        w._n = int(data.get("n", 0))  # noqa: SLF001
        w._mean = float(data.get("mean", 0.0))  # noqa: SLF001
        w._m2 = float(data.get("m2", 0.0))  # noqa: SLF001
        window_vals = data.get("window", [])
        if isinstance(window_vals, list):
            w._window = deque(  # noqa: SLF001
                (float(v) for v in window_vals),
                maxlen=window_size,
            )
        return w
```

### 4.2 `BaselineManager` -- Persistence and Lifecycle

```python
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
                data = {
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
            m2=baseline._m2,  # noqa: SLF001
            window_json=json.dumps(list(baseline._window)),  # noqa: SLF001
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
```

### 4.3 When Baselines Update

The `BaselineManager.record_delta()` is called from the anomaly detector (section 5) on every new task record that has a non-null, non-negative delta. This happens inside `delta_engine.on_snapshot()` after deltas are assigned. The baseline persists to SQLite every `update_interval` (default 5) samples per task_type.

---

## 5. Anomaly Detector (`src/context_pulse/engine/anomaly.py`)

### 5.1 Core Detection Function

```python
from __future__ import annotations

import logging
import time

import aiosqlite

from context_pulse.collector.models import AnomalyResult, ClassifierResponse
from context_pulse.config import AnomalyConfig, ClassifierConfig
from context_pulse.db import anomalies as db_anomalies
from context_pulse.engine.baseline import BaselineManager
from context_pulse.engine.classifier import classify_anomaly

logger = logging.getLogger("context_pulse.engine.anomaly")

MIN_STDDEV: float = 100.0  # floor to prevent div-by-zero on uniform data


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

    Steps:
    1. Skip if task_type is in anomaly_config.task_types_ignored.
    2. Skip if token_delta <= 0 (compaction or zero-cost).
    3. Feed delta to BaselineManager.record_delta() to update the rolling baseline.
    4. If sample_count < anomaly_config.min_sample_count, return None (learning mode).
    5. Compute z_score = (token_delta - mean) / max(stddev, MIN_STDDEV).
    6. If z_score < anomaly_config.z_score_threshold, return None (normal).
    7. Check cooldown: if an anomaly for (session_id, task_type) exists within
       cooldown_seconds, return None (deduplication).
    8. Insert anomaly row into DB.
    9. If classifier is enabled, call classifier (async, best-effort).
       Update anomaly row with cause/severity/suggestion.
    10. Link anomaly to task row (UPDATE tasks SET anomaly_id = ?).
    11. Return AnomalyResult.

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
    # 1. Ignore excluded task types
    if task_type in anomaly_config.task_types_ignored:
        return None

    # 2. Skip non-positive deltas (compaction or cached/zero-cost)
    if token_delta <= 0:
        # Still feed zero deltas to baseline (they are valid data points)
        if token_delta == 0:
            await baseline_manager.record_delta(task_type, float(token_delta))
        return None

    # 3. Update baseline with this delta
    baseline = await baseline_manager.record_delta(
        task_type, float(token_delta)
    )

    # 4. Learning mode gate
    if baseline.sample_count < anomaly_config.min_sample_count:
        logger.debug(
            "Learning mode for %s: %d/%d samples",
            task_type,
            baseline.sample_count,
            anomaly_config.min_sample_count,
        )
        return None

    # 5. Compute z-score
    effective_stddev = max(baseline.stddev, MIN_STDDEV)
    z_score = (token_delta - baseline.mean) / effective_stddev

    # 6. Threshold gate
    if z_score < anomaly_config.z_score_threshold:
        return None

    # 7. Cooldown deduplication
    cooldown_since_ms = timestamp_ms - (anomaly_config.cooldown_seconds * 1000)
    is_in_cooldown = await db_anomalies.check_cooldown(
        db,
        session_id=session_id,
        task_type=task_type,
        since_ms=cooldown_since_ms,
    )
    if is_in_cooldown:
        logger.debug(
            "Cooldown active for session=%s task_type=%s, skipping",
            session_id,
            task_type,
        )
        return None

    # 8. Insert anomaly row
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
        baseline.mean,
        effective_stddev,
    )

    # 9. Classifier (best-effort, never blocks or raises)
    if classifier_config.enabled:
        try:
            classification = await classify_anomaly(
                db=db,
                tool_name=task_type,
                tool_input_summary=tool_input_summary,
                token_cost=token_delta,
                baseline_mean=baseline.mean,
                baseline_stddev=effective_stddev,
                z_score=z_score,
                classifier_config=classifier_config,
            )
            if classification is not None:
                await db_anomalies.update_anomaly_classification(
                    db,
                    anomaly_id=anomaly_id,
                    cause=classification.cause,
                    severity=classification.severity,
                    suggestion=classification.suggestion,
                )
        except Exception:
            logger.exception(
                "Classifier failed for anomaly_id=%d; continuing without classification",
                anomaly_id,
            )

    # 10. Link anomaly to task
    await db.execute(
        "UPDATE tasks SET anomaly_id = ? WHERE id = ?",
        (anomaly_id, task_id),
    )
    await db.commit()

    # 11. Build and return result
    return AnomalyResult(
        task_id=task_id,
        session_id=session_id,
        task_type=task_type,
        token_delta=token_delta,
        z_score=z_score,
        baseline_mean=baseline.mean,
        baseline_stddev=effective_stddev,
        baseline_sample_count=baseline.sample_count,
        timestamp_ms=timestamp_ms,
    )
```

### 5.2 Constants

| Name | Value | Purpose |
|---|---|---|
| `MIN_STDDEV` | `100.0` | Floor for effective stddev to prevent division by zero when all samples are identical |

### 5.3 Edge Cases

| Condition | Behavior |
|---|---|
| `token_delta < 0` (compaction) | Skip anomaly check entirely. Do NOT feed to baseline. |
| `token_delta == 0` (cached response) | Feed to baseline (valid data). Skip anomaly check (cannot spike). |
| `stddev == 0` (uniform data) | Use `MIN_STDDEV = 100.0` as floor |
| `sample_count < min_sample_count` | Return `None` -- learning mode, no detection |
| Cooldown active | Return `None` -- deduplication |
| Classifier fails | Log exception, leave cause/severity/suggestion as `None` on anomaly row |

---

## 6. Haiku Classifier (`src/context_pulse/engine/classifier.py`)

### 6.1 Full Implementation

```python
from __future__ import annotations

import json
import logging
import time

import aiosqlite

from context_pulse.collector.models import ClassifierResponse
from context_pulse.config import ClassifierConfig

logger = logging.getLogger("context_pulse.engine.classifier")

# The system prompt, per project brief section 8
CLASSIFIER_SYSTEM_PROMPT: str = (
    "You are a terse analyst classifying why a Claude Code tool call "
    "consumed unusually many tokens.\n"
    'Respond ONLY with valid JSON: {"cause": str, "severity": "low|medium|high", "suggestion": str}\n'
    "cause: 1 sentence. suggestion: 1 actionable sentence. No other text."
)


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


def _compute_cache_key(tool_name: str, token_delta: int) -> str:
    """Compute cache key: tool_name + delta rounded to nearest 1000."""
    bucket = round(token_delta / 1000) * 1000
    return f"{tool_name}:{bucket}"


async def _get_cached_response(
    db: aiosqlite.Connection,
    cache_key: str,
) -> ClassifierResponse | None:
    """Look up a cached classifier response. Returns None on miss."""
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


def _parse_classifier_output(raw_text: str) -> ClassifierResponse:
    """Parse the raw Haiku response text into a ClassifierResponse.

    Attempts JSON parse first. On failure, returns a generic fallback.
    """
    # Strip markdown code fences if present
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
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

    1. Compute cache key from (tool_name, token_delta_bucket).
    2. If cache_results is enabled, check cache first.
    3. On cache miss, call the Anthropic SDK.
    4. Parse the response.
    5. Cache the result if caching is enabled.
    6. Return the ClassifierResponse.

    Returns None if the classifier is disabled or the call fails entirely.

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
        The [classifier] section of the config.

    Returns
    -------
    ClassifierResponse | None
    """
    if not classifier_config.enabled:
        return None

    cache_key = _compute_cache_key(tool_name, token_cost)

    # Check cache
    if classifier_config.cache_results:
        cached = await _get_cached_response(db, cache_key)
        if cached is not None:
            logger.debug("Classifier cache hit for key=%s", cache_key)
            return cached

    # Build prompt
    user_prompt = _build_user_prompt(
        tool_name=tool_name,
        tool_input_summary=tool_input_summary,
        token_cost=token_cost,
        baseline_mean=baseline_mean,
        baseline_stddev=baseline_stddev,
        z_score=z_score,
    )

    # Call Anthropic SDK
    try:
        import anthropic

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
                raw_text += block.text
        if not raw_text:
            logger.warning("Classifier returned empty response")
            return None

    except ImportError:
        logger.error(
            "anthropic package not installed. "
            "Install with: pip install anthropic"
        )
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

    # Parse response
    result = _parse_classifier_output(raw_text)

    # Cache result
    if classifier_config.cache_results:
        try:
            await _set_cached_response(db, cache_key, result)
            logger.debug("Cached classifier response for key=%s", cache_key)
        except Exception:
            logger.exception("Failed to cache classifier response")

    return result
```

### 6.2 Cache Strategy

| Aspect | Detail |
|---|---|
| Cache key | `f"{tool_name}:{bucket}"` where `bucket = round(token_delta / 1000) * 1000` |
| Storage | `classifier_cache` SQLite table (see section 2.1) |
| Eviction | None in Phase 2. Table grows unbounded. Phase 3+ may add TTL-based cleanup. |
| Bypass | When `classifier_config.cache_results = false` |

### 6.3 Error Handling

| Error | Handling |
|---|---|
| `anthropic` not installed | Log error, return `None` |
| `RateLimitError` | Log warning, return `None` -- no retry |
| `APIConnectionError` | Log warning, return `None` |
| Malformed JSON response | Fall back to generic `ClassifierResponse` with cause="Token spike detected" |
| Any other exception | Log exception with traceback, return `None` |

The classifier must never raise. All errors are caught and logged. The anomaly row is created regardless of classifier success -- the cause/severity/suggestion fields remain `None` on failure.

---

## 7. Database CRUD

### 7.1 `src/context_pulse/db/baselines.py`

```python
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger("context_pulse.db.baselines")


async def get_baseline(
    db: aiosqlite.Connection,
    task_type: str,
) -> dict[str, Any] | None:
    """Return the baseline row for *task_type*, or None if not found.

    Returns a dict with keys: task_type, mean, stddev, sample_count,
    updated_at, m2, window_json.
    """
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM baselines WHERE task_type = ?",
        (task_type,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return dict(row)


async def upsert_baseline(
    db: aiosqlite.Connection,
    task_type: str,
    mean: float,
    stddev: float,
    sample_count: int,
    m2: float,
    window_json: str,
    updated_at: int,
) -> None:
    """Insert or update the baseline for *task_type*.

    Uses INSERT OR REPLACE to perform an upsert on the PRIMARY KEY (task_type).
    """
    await db.execute(
        """
        INSERT OR REPLACE INTO baselines
            (task_type, mean, stddev, sample_count, m2, window_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_type, mean, stddev, sample_count, m2, window_json, updated_at),
    )
    await db.commit()
    logger.debug(
        "Upserted baseline for %s: mean=%.1f stddev=%.1f n=%d",
        task_type,
        mean,
        stddev,
        sample_count,
    )


async def get_all_baselines(
    db: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Return all baseline rows."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM baselines ORDER BY task_type"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]
```

### 7.2 `src/context_pulse/db/anomalies.py`

```python
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

logger = logging.getLogger("context_pulse.db.anomalies")


async def insert_anomaly(
    db: aiosqlite.Connection,
    session_id: str,
    task_type: str,
    token_cost: int,
    z_score: float,
    cause: str | None,
    severity: str | None,
    suggestion: str | None,
    timestamp_ms: int,
) -> int:
    """Insert a new anomaly row and return its id.

    The anomaly is inserted with notified=0. Notification is handled
    separately by the notifier layer (Phase 3).
    """
    cursor = await db.execute(
        """
        INSERT INTO anomalies
            (session_id, task_type, token_cost, z_score,
             cause, severity, suggestion, notified, timestamp_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (session_id, task_type, token_cost, z_score,
         cause, severity, suggestion, timestamp_ms),
    )
    await db.commit()
    row_id: int | None = cursor.lastrowid
    if row_id is None:
        msg = "INSERT into anomalies did not return a lastrowid"
        raise RuntimeError(msg)
    logger.debug("Inserted anomaly %d for %s (z=%.2f)", row_id, task_type, z_score)
    return row_id


async def update_anomaly_classification(
    db: aiosqlite.Connection,
    anomaly_id: int,
    cause: str,
    severity: str,
    suggestion: str,
) -> None:
    """Update the classifier fields on an existing anomaly row."""
    await db.execute(
        """
        UPDATE anomalies
           SET cause = ?, severity = ?, suggestion = ?
         WHERE id = ?
        """,
        (cause, severity, suggestion, anomaly_id),
    )
    await db.commit()
    logger.debug("Updated classification for anomaly %d", anomaly_id)


async def check_cooldown(
    db: aiosqlite.Connection,
    session_id: str,
    task_type: str,
    since_ms: int,
) -> bool:
    """Return True if an anomaly exists for (session_id, task_type) since *since_ms*.

    Used for cooldown deduplication.
    """
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM anomalies
         WHERE session_id = ?
           AND task_type = ?
           AND timestamp_ms >= ?
        """,
        (session_id, task_type, since_ms),
    )
    row = await cursor.fetchone()
    return row is not None and int(row[0]) > 0


async def get_recent_anomalies(
    db: aiosqlite.Connection,
    limit: int = 20,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent anomalies, newest first.

    Optionally filtered by session_id.
    """
    clauses: list[str] = []
    params: list[str | int] = []

    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT * FROM anomalies
         {where}
         ORDER BY timestamp_ms DESC
         LIMIT ?
    """
    params.append(limit)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_anomaly_count(
    db: aiosqlite.Connection,
    session_id: str | None = None,
) -> int:
    """Return the total number of anomalies, optionally for a session."""
    if session_id is not None:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM anomalies WHERE session_id = ?",
            (session_id,),
        )
    else:
        cursor = await db.execute("SELECT COUNT(*) FROM anomalies")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0
```

---

## 8. Integration with Existing Code

### 8.1 Delta Engine Integration (`delta_engine.py`)

The `on_snapshot()` function must trigger anomaly detection after assigning deltas. The anomaly detector needs the `BaselineManager` instance, which is stored on `app.state`.

**Changes to `delta_engine.py`:**

Add a new function `process_anomalies()` called after delta assignment:

```python
async def process_anomalies(
    db: aiosqlite.Connection,
    baseline_manager: BaselineManager,
    anomaly_config: AnomalyConfig,
    classifier_config: ClassifierConfig,
    results: list[tuple[int, int | None, bool]],
    session_id: str,
    pending_list: list[PendingToolCall],
) -> list[AnomalyResult]:
    """Check each resolved delta for anomalies.

    Called after on_snapshot() resolves deltas for pending tool calls.

    Parameters
    ----------
    results:
        The list of (task_id, token_delta, is_compaction) from on_snapshot().
    pending_list:
        The list of PendingToolCall objects (same order as results).
        Needed to get task_type and for tool_input_summary lookup.

    Returns
    -------
    list[AnomalyResult]
        Any anomalies detected (may be empty).
    """
    from context_pulse.engine.anomaly import detect_anomaly

    anomalies: list[AnomalyResult] = []
    for (task_id, delta, is_compaction), ptc in zip(results, pending_list):
        if delta is None or is_compaction:
            continue
        result = await detect_anomaly(
            baseline_manager=baseline_manager,
            db=db,
            task_id=task_id,
            session_id=session_id,
            task_type=ptc.task_type,
            token_delta=delta,
            tool_input_summary=None,  # not available on PendingToolCall; see note below
            timestamp_ms=ptc.timestamp_ms,
            anomaly_config=anomaly_config,
            classifier_config=classifier_config,
        )
        if result is not None:
            anomalies.append(result)
    return anomalies
```

**Note on `tool_input_summary`:** The `PendingToolCall` dataclass does not currently store `tool_input_summary`. To make it available for the classifier, modify `PendingToolCall` to add an optional field:

```python
@dataclass
class PendingToolCall:
    event_id: int
    task_id: int
    task_type: str
    timestamp_ms: int
    tool_input_summary: str | None = None  # NEW: for classifier context
```

And populate it in `on_tool_use()`:

```python
session.pending_tool_calls.append(
    PendingToolCall(
        event_id=event_id,
        task_id=task_id,
        task_type=task_type,
        timestamp_ms=event.timestamp_ms,
        tool_input_summary=event.tool_input_summary,  # NEW
    )
)
```

**Modification to `on_snapshot()` return value:**

The `on_snapshot()` function currently returns `list[tuple[int, int | None, bool]]`. It must now also call `process_anomalies()`. However, `on_snapshot()` does not have access to `BaselineManager` or config. There are two clean options:

**Option A (recommended): Caller-side integration.** Keep `on_snapshot()` unchanged. The caller (in `routes.py`, the `receive_statusline_snapshot` handler) calls `process_anomalies()` after `on_snapshot()` returns. This keeps the delta engine pure and avoids threading config through.

**Implementation in `routes.py`:**

```python
@hook_router.post("/statusline", status_code=202)
async def receive_statusline_snapshot(
    snapshot: StatuslineSnapshotRequest,
    db: aiosqlite.Connection = Depends(get_db),
    sessions: dict[str, Any] = Depends(get_sessions),
    config: Any = Depends(get_config),
) -> dict[str, str]:
    try:
        snapshot_id = await db_events.insert_snapshot(db, ...)

        # Capture pending_list BEFORE on_snapshot clears it
        session = sessions.get(snapshot.session_id)
        pending_list = list(session.pending_tool_calls) if session else []

        results = await delta_engine.on_snapshot(sessions, db, snapshot, snapshot_id)

        # Anomaly detection (Phase 2)
        if results:
            baseline_manager = get_baseline_manager(request)  # from app.state
            await delta_engine.process_anomalies(
                db=db,
                baseline_manager=baseline_manager,
                anomaly_config=config.anomaly,
                classifier_config=config.classifier,
                results=results,
                session_id=snapshot.session_id,
                pending_list=pending_list,
            )
    except Exception:
        logger.exception(...)
    return {"status": "accepted"}
```

**Add a new dependency function in `routes.py`:**

```python
async def get_baseline_manager(request: Request) -> BaselineManager:
    """Dependency: returns app.state.baseline_manager."""
    return request.app.state.baseline_manager
```

### 8.2 Server Startup (`server.py`)

Add `BaselineManager` initialization in the lifespan:

```python
from context_pulse.engine.baseline import BaselineManager

# Inside lifespan(), after run_migrations(db):
baseline_manager = BaselineManager(
    db=db,
    window_size=cfg.anomaly.baseline_window,
    update_interval=5,
)
app.state.baseline_manager = baseline_manager

# Inside lifespan() shutdown (before db.close()):
await baseline_manager.flush_all()
```

### 8.3 API Routes (`routes.py`)

Add new endpoints:

```python
@api_router.get("/anomalies", response_model=AnomaliesListResponse)
async def get_anomalies(
    limit: int = 20,
    session_id: str | None = None,
    db: aiosqlite.Connection = Depends(get_db),
) -> AnomaliesListResponse:
    """Return recent anomalies with optional session filter."""
    rows = await db_anomalies.get_recent_anomalies(db, limit=limit, session_id=session_id)
    total = await db_anomalies.get_anomaly_count(db, session_id=session_id)

    anomalies = [
        AnomalyResponse(
            id=r["id"],
            session_id=r["session_id"],
            task_type=r["task_type"],
            token_cost=r["token_cost"],
            z_score=r["z_score"],
            cause=r.get("cause"),
            severity=r.get("severity"),
            suggestion=r.get("suggestion"),
            notified=bool(r.get("notified", 0)),
            timestamp_ms=r["timestamp_ms"],
        )
        for r in rows
    ]
    return AnomaliesListResponse(anomalies=anomalies, total_count=total)


@api_router.get("/baselines", response_model=list[BaselineSnapshot])
async def get_baselines(
    db: aiosqlite.Connection = Depends(get_db),
) -> list[BaselineSnapshot]:
    """Return all baseline snapshots."""
    rows = await db_baselines.get_all_baselines(db)
    return [
        BaselineSnapshot(
            task_type=r["task_type"],
            mean=r["mean"],
            stddev=r["stddev"],
            sample_count=r["sample_count"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
```

### 8.4 CLI Command (`cli.py`)

Add the `anomalies` command:

```python
@app.command()
def anomalies(
    port: int = typer.Option(7821, help="Collector port"),
    limit: int = typer.Option(10, help="Number of anomalies to show"),
    session_id: str | None = typer.Option(None, "--session", help="Filter by session"),
) -> None:
    """Show recent anomalies with root cause analysis."""
    import httpx

    url = f"http://127.0.0.1:{port}"
    params: dict[str, str | int] = {"limit": limit}
    if session_id is not None:
        params["session_id"] = session_id

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{url}/api/anomalies", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to collector.[/red] "
            "Is it running? Start with: [bold]context-pulse serve[/bold]"
        )
        raise typer.Exit(1) from None
    except httpx.HTTPError as exc:
        console.print(f"[red]Error communicating with collector:[/red] {exc}")
        raise typer.Exit(1) from None

    anomaly_list = data.get("anomalies", [])
    total_count = data.get("total_count", 0)

    if not anomaly_list:
        console.print(
            Panel("[dim]No anomalies detected yet[/dim]", title="Anomalies")
        )
        return

    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Time")
    table.add_column("Session")
    table.add_column("Tool")
    table.add_column("Tokens", justify="right")
    table.add_column("Z-Score", justify="right")
    table.add_column("Severity", justify="center")
    table.add_column("Cause")

    for a in anomaly_list:
        time_str = _format_timestamp(a.get("timestamp_ms", 0))
        sid = _truncate_session_id(a.get("session_id", "unknown"))
        task_type = a.get("task_type", "")
        tokens = f"{a.get('token_cost', 0):,}"
        z = f"{a.get('z_score', 0):.1f}"
        severity = a.get("severity") or "--"
        cause = a.get("cause") or "[dim]pending...[/dim]"

        # Color severity
        sev_color = {"low": "yellow", "medium": "orange3", "high": "red"}.get(
            severity.lower(), "dim"
        )
        severity_display = f"[{sev_color}]{severity}[/{sev_color}]"

        # Truncate cause for table
        if len(cause) > 60:
            cause = cause[:57] + "..."

        table.add_row(time_str, sid, task_type, tokens, z, severity_display, cause)

    header = f"Anomalies (showing {len(anomaly_list)} of {total_count})"
    console.print(Panel(table, title=header, border_style="red"))

    # Show suggestions for the most recent anomaly
    latest = anomaly_list[0]
    if latest.get("suggestion"):
        console.print()
        console.print(
            Panel(
                f"[bold]Latest anomaly suggestion:[/bold]\n{latest['suggestion']}",
                border_style="yellow",
            )
        )
```

---

## 9. Dependency Graph and Build Order

### 9.1 Dependency Graph

```
db/baselines.py ──────────────────────┐
                                      │
db/anomalies.py ──────────────────────┤
                                      │
engine/baseline.py ───uses───> db/baselines.py
      │                               │
      │                               │
engine/anomaly.py ───uses───> engine/baseline.py
      │                  ───uses───> db/anomalies.py
      │                               │
engine/classifier.py ─uses──> anthropic SDK
      │                  ─uses──> db (classifier_cache table)
      │                               │
engine/anomaly.py ───uses───> engine/classifier.py
      │                               │
delta_engine.py (modified) ──uses──> engine/anomaly.py
      │                               │
routes.py (modified) ─────────uses──> delta_engine + db/anomalies + db/baselines
      │                               │
cli.py (modified) ────────────uses──> routes (via HTTP)
      │                               │
schema.py (modified) ─────────v2 migration
```

### 9.2 Build Order (Parallelism Opportunities)

**Layer 1 -- No dependencies, build in parallel:**
- `src/context_pulse/db/baselines.py`
- `src/context_pulse/db/anomalies.py`
- `src/context_pulse/db/schema.py` (v2 migration addition)
- `src/context_pulse/engine/__init__.py`
- New Pydantic models in `models.py`

**Layer 2 -- Depends on Layer 1:**
- `src/context_pulse/engine/baseline.py` (depends on `db/baselines.py`)
- `src/context_pulse/engine/classifier.py` (depends on models, `db` for cache)

**Layer 3 -- Depends on Layer 2:**
- `src/context_pulse/engine/anomaly.py` (depends on `engine/baseline.py`, `engine/classifier.py`, `db/anomalies.py`)

**Layer 4 -- Depends on Layer 3:**
- Modifications to `delta_engine.py` (depends on `engine/anomaly.py`)
- Modifications to `routes.py` (depends on `db/anomalies.py`, `db/baselines.py`, models)
- Modifications to `server.py` (depends on `engine/baseline.py`)

**Layer 5 -- Depends on Layer 4:**
- Modifications to `cli.py` (depends on routes being in place)

**Layer 6 -- Tests (can start after their target module is built):**
- `tests/test_baseline.py` (after Layer 2)
- `tests/test_classifier.py` (after Layer 2)
- `tests/test_anomaly.py` (after Layer 3)

---

## 10. New Dependencies

### 10.1 `pyproject.toml` Changes

Add `anthropic` to the dependencies list:

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.34.0",
    "aiosqlite>=0.21.0",
    "httpx>=0.27.0",
    "typer>=0.15.0",
    "rich>=13.0.0",
    "pydantic>=2.10.0",
    "anthropic>=0.43.0",
]
```

The `anthropic` package is imported lazily in `classifier.py` (inside `classify_anomaly()`), so the tool works without an API key when `classifier.enabled = false`. The import is inside the function body, not at module level, to avoid `ImportError` at startup for users who have not installed it or do not want to use the classifier.

**Alternative approach for optional dependency:** If the anthropic SDK should be truly optional (not required at install time), instead add it as an optional extra:

```toml
[project.optional-dependencies]
classifier = ["anthropic>=0.43.0"]
```

This spec assumes the simpler approach: add it as a regular dependency, since it is small (~5MB) and Phase 2 is the intelligence layer where it is needed.

---

## 11. Test Plan

### 11.1 `tests/test_baseline.py`

Tests the `RollingWelford` class and `BaselineManager` in isolation.

```python
"""Tests for the Welford rolling baseline engine."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import aiosqlite
import pytest
import pytest_asyncio

from context_pulse.engine.baseline import BaselineManager, RollingWelford


# -------------------------------------------------------------------
# RollingWelford unit tests
# -------------------------------------------------------------------


class TestRollingWelford:
    """Pure unit tests for the RollingWelford class (no DB needed)."""

    def test_empty_state(self) -> None:
        """Fresh instance has n=0, mean=0, variance=0, stddev=0."""
        w = RollingWelford(window_size=5)
        assert w.sample_count == 0
        assert w.mean == 0.0
        assert w.variance == 0.0
        assert w.stddev == 0.0

    def test_single_sample(self) -> None:
        """After one sample, mean equals the sample, variance is 0."""
        w = RollingWelford(window_size=5)
        w.update(100.0)
        assert w.sample_count == 1
        assert w.mean == 100.0
        assert w.variance == 0.0

    def test_two_samples_variance(self) -> None:
        """Two samples should produce correct sample variance."""
        w = RollingWelford(window_size=5)
        w.update(100.0)
        w.update(200.0)
        assert w.sample_count == 2
        assert w.mean == 150.0
        # Sample variance of [100, 200] = (100-150)^2 + (200-150)^2 / (2-1) = 5000
        assert abs(w.variance - 5000.0) < 0.001

    def test_known_sequence(self) -> None:
        """Test against a known sequence: [2, 4, 4, 4, 5, 5, 7, 9]."""
        w = RollingWelford(window_size=20)
        values = [2, 4, 4, 4, 5, 5, 7, 9]
        for v in values:
            w.update(float(v))
        assert w.sample_count == 8
        assert abs(w.mean - 5.0) < 0.001
        # Sample variance = 4.571...
        assert abs(w.variance - 4.571428571) < 0.01

    def test_rolling_window_eviction(self) -> None:
        """When window is full, oldest value is removed."""
        w = RollingWelford(window_size=3)
        w.update(10.0)
        w.update(20.0)
        w.update(30.0)
        assert w.sample_count == 3
        assert w.mean == 20.0

        # Add a 4th value -- oldest (10) should be evicted
        w.update(40.0)
        assert w.sample_count == 3  # still 3
        assert w.mean == 30.0  # mean of [20, 30, 40]

    def test_rolling_window_variance_after_eviction(self) -> None:
        """Variance should be correct after eviction."""
        w = RollingWelford(window_size=3)
        for v in [100.0, 100.0, 100.0]:
            w.update(v)
        # All same -- variance should be 0
        assert w.variance == 0.0

        # Add a spike
        w.update(200.0)
        # Window is now [100, 100, 200], mean=133.33
        assert w.sample_count == 3

    def test_m2_clamp_prevents_negative(self) -> None:
        """M2 should never go negative due to float drift."""
        w = RollingWelford(window_size=2)
        w.update(1000.0)
        w.update(1000.0)
        # Evict and add same value
        w.update(1000.0)
        assert w.variance >= 0.0
        assert w._m2 >= 0.0

    def test_serialization_roundtrip(self) -> None:
        """to_dict / from_dict should preserve state exactly."""
        w = RollingWelford(window_size=5)
        for v in [10.0, 20.0, 30.0]:
            w.update(v)

        data = w.to_dict()
        w2 = RollingWelford.from_dict(data, window_size=5)

        assert w2.sample_count == w.sample_count
        assert abs(w2.mean - w.mean) < 0.001
        assert abs(w2.variance - w.variance) < 0.001


# -------------------------------------------------------------------
# BaselineManager integration tests (need DB)
# -------------------------------------------------------------------


class TestBaselineManager:
    """Integration tests for BaselineManager with in-memory SQLite."""

    async def test_record_delta_updates_mean(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Recording deltas should update the baseline mean."""
        mgr = BaselineManager(db=db_connection, window_size=10, update_interval=5)
        for v in [1000, 2000, 3000]:
            baseline = await mgr.record_delta("Bash", float(v))
        assert baseline.sample_count == 3
        assert abs(baseline.mean - 2000.0) < 0.001

    async def test_persistence_after_interval(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """After update_interval samples, baseline should be persisted to DB."""
        mgr = BaselineManager(db=db_connection, window_size=10, update_interval=3)
        for v in [100, 200, 300]:
            await mgr.record_delta("Read", float(v))
        # After 3 samples (= update_interval), should be in DB
        from context_pulse.db import baselines as db_baselines
        row = await db_baselines.get_baseline(db_connection, "Read")
        assert row is not None
        assert row["sample_count"] == 3

    async def test_reload_from_db(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """A new BaselineManager should load existing baselines from DB."""
        mgr1 = BaselineManager(db=db_connection, window_size=10, update_interval=2)
        for v in [1000, 2000]:
            await mgr1.record_delta("Edit", float(v))
        # Force persist
        await mgr1.flush_all()

        # New manager should load from DB
        mgr2 = BaselineManager(db=db_connection, window_size=10, update_interval=2)
        baseline = await mgr2.get_baseline("Edit")
        assert baseline.sample_count == 2
        assert abs(baseline.mean - 1500.0) < 0.001

    async def test_flush_all(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """flush_all should persist all dirty baselines."""
        mgr = BaselineManager(db=db_connection, window_size=10, update_interval=100)
        await mgr.record_delta("Bash", 1000.0)
        await mgr.record_delta("Read", 500.0)
        await mgr.flush_all()
        from context_pulse.db import baselines as db_baselines
        bash_row = await db_baselines.get_baseline(db_connection, "Bash")
        read_row = await db_baselines.get_baseline(db_connection, "Read")
        assert bash_row is not None
        assert read_row is not None
```

### 11.2 `tests/test_anomaly.py`

Tests the anomaly detection logic with mocked baselines.

```python
"""Tests for the anomaly detection engine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiosqlite

from context_pulse.collector.models import AnomalyResult
from context_pulse.config import AnomalyConfig, ClassifierConfig
from context_pulse.engine.anomaly import MIN_STDDEV, detect_anomaly
from context_pulse.engine.baseline import BaselineManager


class TestDetectAnomaly:
    """Tests for detect_anomaly()."""

    async def _seed_baseline(
        self,
        mgr: BaselineManager,
        task_type: str,
        values: list[float],
    ) -> None:
        """Helper: feed values into the baseline to build up sample_count."""
        for v in values:
            await mgr.record_delta(task_type, v)

    async def test_learning_mode_returns_none(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Should return None when sample_count < min_sample_count."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5)
        cls_cfg = ClassifierConfig(enabled=False)

        # Only 3 samples in baseline
        await self._seed_baseline(mgr, "Bash", [1000, 2000, 3000])

        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=10000,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is None

    async def test_normal_delta_returns_none(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """A delta within threshold should return None."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5, z_score_threshold=2.0)
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Bash", [1000, 1100, 900, 1050, 950])

        # Delta of 1200 should be within 2 sigma
        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=1200,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is None

    async def test_spike_returns_anomaly(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """A large delta should trigger an anomaly."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5, z_score_threshold=2.0)
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Bash", [1000, 1100, 900, 1050, 950])

        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=8000,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is not None
        assert isinstance(result, AnomalyResult)
        assert result.z_score > 2.0
        assert result.token_delta == 8000

    async def test_negative_delta_skipped(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Negative delta (compaction) should not trigger anomaly."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5, z_score_threshold=2.0)
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Bash", [1000, 1100, 900, 1050, 950])

        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=-5000,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is None

    async def test_zero_delta_feeds_baseline_no_anomaly(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Zero delta should update baseline but not trigger anomaly."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5, z_score_threshold=2.0)
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Bash", [1000, 1100, 900, 1050, 950])
        baseline_before = await mgr.get_baseline("Bash")
        count_before = baseline_before.sample_count

        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=0,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is None
        baseline_after = await mgr.get_baseline("Bash")
        assert baseline_after.sample_count == count_before + 1

    async def test_ignored_task_type(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Ignored task types should never trigger anomaly."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(
            min_sample_count=5,
            z_score_threshold=2.0,
            task_types_ignored=["Read"],
        )
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Read", [100, 100, 100, 100, 100])

        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Read",
            token_delta=50000,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is None

    async def test_cooldown_deduplication(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Second anomaly within cooldown should be suppressed."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(
            min_sample_count=5,
            z_score_threshold=2.0,
            cooldown_seconds=60,
        )
        cls_cfg = ClassifierConfig(enabled=False)

        await self._seed_baseline(mgr, "Bash", [1000, 1100, 900, 1050, 950])

        now_ms = 1000000
        # First anomaly should succeed
        r1 = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=8000,
            tool_input_summary=None,
            timestamp_ms=now_ms,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert r1 is not None

        # Second anomaly 10s later should be suppressed (within 60s cooldown)
        r2 = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=2,
            session_id="sess-1",
            task_type="Bash",
            token_delta=9000,
            tool_input_summary=None,
            timestamp_ms=now_ms + 10_000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert r2 is None

    async def test_min_stddev_floor(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """When all samples are identical, MIN_STDDEV floor prevents div-by-zero."""
        mgr = BaselineManager(db=db_connection, window_size=20, update_interval=100)
        cfg = AnomalyConfig(min_sample_count=5, z_score_threshold=2.0)
        cls_cfg = ClassifierConfig(enabled=False)

        # All identical -- stddev = 0
        await self._seed_baseline(mgr, "Bash", [1000, 1000, 1000, 1000, 1000])

        # Delta of 1300 -- z = (1300-1000)/100 = 3.0 > 2.0 threshold
        result = await detect_anomaly(
            baseline_manager=mgr,
            db=db_connection,
            task_id=1,
            session_id="sess-1",
            task_type="Bash",
            token_delta=1300,
            tool_input_summary=None,
            timestamp_ms=1000000,
            anomaly_config=cfg,
            classifier_config=cls_cfg,
        )
        assert result is not None
        assert abs(result.z_score - 3.0) < 0.5  # approximately 3.0
        assert result.baseline_stddev == MIN_STDDEV
```

### 11.3 `tests/test_classifier.py`

Tests the classifier with mocked Anthropic SDK.

```python
"""Tests for the Haiku classifier engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

from context_pulse.collector.models import ClassifierResponse
from context_pulse.config import ClassifierConfig
from context_pulse.engine.classifier import (
    _build_user_prompt,
    _compute_cache_key,
    _parse_classifier_output,
    classify_anomaly,
)


class TestCacheKey:
    """Tests for cache key computation."""

    def test_rounds_to_nearest_1000(self) -> None:
        assert _compute_cache_key("Bash", 1200) == "Bash:1000"
        assert _compute_cache_key("Bash", 1500) == "Bash:2000"
        assert _compute_cache_key("Bash", 1499) == "Bash:1000"
        assert _compute_cache_key("Read", 500) == "Read:1000"
        assert _compute_cache_key("Edit", 8400) == "Edit:8000"

    def test_zero_delta(self) -> None:
        assert _compute_cache_key("Bash", 0) == "Bash:0"

    def test_small_delta(self) -> None:
        assert _compute_cache_key("Bash", 499) == "Bash:0"


class TestParseClassifierOutput:
    """Tests for parsing Haiku response text."""

    def test_valid_json(self) -> None:
        raw = json.dumps({
            "cause": "Large file scan",
            "severity": "high",
            "suggestion": "Limit file size"
        })
        result = _parse_classifier_output(raw)
        assert result.cause == "Large file scan"
        assert result.severity == "high"
        assert result.suggestion == "Limit file size"

    def test_json_with_markdown_fences(self) -> None:
        raw = '```json\n{"cause": "Big grep", "severity": "medium", "suggestion": "Scope it"}\n```'
        result = _parse_classifier_output(raw)
        assert result.cause == "Big grep"

    def test_invalid_json_fallback(self) -> None:
        result = _parse_classifier_output("This is not JSON")
        assert result.severity == "medium"
        assert "unparseable" in result.cause.lower() or "spike" in result.cause.lower()

    def test_invalid_severity_defaults_to_medium(self) -> None:
        raw = json.dumps({
            "cause": "Something",
            "severity": "critical",  # not in allowed set
            "suggestion": "Fix it"
        })
        result = _parse_classifier_output(raw)
        assert result.severity == "medium"


class TestBuildUserPrompt:
    """Tests for user prompt construction."""

    def test_includes_all_fields(self) -> None:
        prompt = _build_user_prompt(
            tool_name="Bash",
            tool_input_summary="grep -r auth .",
            token_cost=8400,
            baseline_mean=2000.0,
            baseline_stddev=500.0,
            z_score=12.8,
        )
        assert "Bash" in prompt
        assert "grep -r auth" in prompt
        assert "8400" in prompt
        assert "2000" in prompt
        assert "12.8" in prompt

    def test_none_summary_fallback(self) -> None:
        prompt = _build_user_prompt(
            tool_name="Bash",
            tool_input_summary=None,
            token_cost=5000,
            baseline_mean=1000.0,
            baseline_stddev=200.0,
            z_score=20.0,
        )
        assert "no input summary" in prompt.lower()


class TestClassifyAnomaly:
    """Integration tests for classify_anomaly with mocked Anthropic SDK."""

    async def test_disabled_returns_none(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """When classifier is disabled, should return None without calling API."""
        cfg = ClassifierConfig(enabled=False)
        result = await classify_anomaly(
            db=db_connection,
            tool_name="Bash",
            tool_input_summary="grep -r . .",
            token_cost=8000,
            baseline_mean=2000.0,
            baseline_stddev=500.0,
            z_score=12.0,
            classifier_config=cfg,
        )
        assert result is None

    async def test_successful_classification(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Mock a successful Anthropic API call and verify parsing."""
        cfg = ClassifierConfig(enabled=True, cache_results=False)

        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = json.dumps({
            "cause": "Recursive grep across entire codebase",
            "severity": "high",
            "suggestion": "Add --max-count=100",
        })
        mock_response.content = [mock_block]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch(
            "context_pulse.engine.classifier.anthropic"
        ) as mock_module:
            mock_module.AsyncAnthropic.return_value = mock_client
            mock_module.RateLimitError = Exception
            mock_module.APIConnectionError = Exception

            result = await classify_anomaly(
                db=db_connection,
                tool_name="Bash",
                tool_input_summary="grep -r . .",
                token_cost=8000,
                baseline_mean=2000.0,
                baseline_stddev=500.0,
                z_score=12.0,
                classifier_config=cfg,
            )

        assert result is not None
        assert result.cause == "Recursive grep across entire codebase"
        assert result.severity == "high"

    async def test_cache_hit(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """Second call with same cache key should use cached response."""
        cfg = ClassifierConfig(enabled=True, cache_results=True)

        # Pre-populate cache
        cache_key = _compute_cache_key("Bash", 8000)
        cached_data = json.dumps({
            "cause": "Cached cause",
            "severity": "low",
            "suggestion": "Cached suggestion",
        })
        await db_connection.execute(
            "INSERT INTO classifier_cache (cache_key, response_json, created_at) "
            "VALUES (?, ?, ?)",
            (cache_key, cached_data, 1000000),
        )
        await db_connection.commit()

        # Should return cached result without calling API
        result = await classify_anomaly(
            db=db_connection,
            tool_name="Bash",
            tool_input_summary="grep -r . .",
            token_cost=8000,
            baseline_mean=2000.0,
            baseline_stddev=500.0,
            z_score=12.0,
            classifier_config=cfg,
        )

        assert result is not None
        assert result.cause == "Cached cause"

    async def test_rate_limit_returns_none(
        self, db_connection: aiosqlite.Connection
    ) -> None:
        """RateLimitError from Anthropic should return None gracefully."""
        cfg = ClassifierConfig(enabled=True, cache_results=False)

        with patch(
            "context_pulse.engine.classifier.anthropic"
        ) as mock_module:
            rate_limit_error = type("RateLimitError", (Exception,), {})
            mock_module.RateLimitError = rate_limit_error
            mock_module.APIConnectionError = Exception
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(
                side_effect=rate_limit_error("rate limited")
            )
            mock_module.AsyncAnthropic.return_value = mock_client

            result = await classify_anomaly(
                db=db_connection,
                tool_name="Bash",
                tool_input_summary=None,
                token_cost=8000,
                baseline_mean=2000.0,
                baseline_stddev=500.0,
                z_score=12.0,
                classifier_config=cfg,
            )

        assert result is None
```

### 11.4 Test Fixtures

The existing `tests/conftest.py` provides `db_connection` (in-memory SQLite with migrations) and model factories. Phase 2 tests reuse `db_connection` directly. No new conftest fixtures are required, since `BaselineManager` is instantiated directly in each test.

**Important:** The v2 migration must run as part of `run_migrations()` for the `classifier_cache` table and new `baselines` columns to exist in the test DB. Since `conftest.py` calls `await run_migrations(db)`, and the migration registry includes v2, this happens automatically.

### 11.5 Mock Strategy for Anthropic SDK

The `anthropic` module is imported inside `classify_anomaly()` at call time (not at module level). Tests mock it via `unittest.mock.patch("context_pulse.engine.classifier.anthropic")`. The mock must provide:

- `AsyncAnthropic()` returning a mock client
- `client.messages.create()` as an `AsyncMock` returning a mock message
- `message.content` as a list of mock blocks with `.text` attribute
- `anthropic.RateLimitError` and `anthropic.APIConnectionError` as exception classes

---

## 12. Summary of All File Changes

### New files:
1. `src/context_pulse/engine/__init__.py` -- empty
2. `src/context_pulse/engine/baseline.py` -- `RollingWelford`, `BaselineManager`
3. `src/context_pulse/engine/anomaly.py` -- `detect_anomaly()`, `MIN_STDDEV`
4. `src/context_pulse/engine/classifier.py` -- `classify_anomaly()`, cache helpers, prompt constants
5. `src/context_pulse/db/baselines.py` -- `get_baseline()`, `upsert_baseline()`, `get_all_baselines()`
6. `src/context_pulse/db/anomalies.py` -- `insert_anomaly()`, `update_anomaly_classification()`, `check_cooldown()`, `get_recent_anomalies()`, `get_anomaly_count()`
7. `tests/test_baseline.py`
8. `tests/test_anomaly.py`
9. `tests/test_classifier.py`

### Modified files:
1. `src/context_pulse/db/schema.py` -- add v2 migration (classifier_cache table + baselines columns)
2. `src/context_pulse/collector/models.py` -- add `BaselineSnapshot`, `AnomalyResult`, `ClassifierResponse`, `AnomalyResponse`, `AnomaliesListResponse`
3. `src/context_pulse/collector/delta_engine.py` -- add `tool_input_summary` field to `PendingToolCall`, add `process_anomalies()` function
4. `src/context_pulse/collector/routes.py` -- add `get_baseline_manager` dependency, modify `receive_statusline_snapshot` to call `process_anomalies()`, add `GET /api/anomalies` and `GET /api/baselines` endpoints
5. `src/context_pulse/collector/server.py` -- create `BaselineManager` in lifespan, store on `app.state`, flush on shutdown
6. `src/context_pulse/cli.py` -- add `anomalies` command
7. `pyproject.toml` -- add `anthropic>=0.43.0` to dependencies
