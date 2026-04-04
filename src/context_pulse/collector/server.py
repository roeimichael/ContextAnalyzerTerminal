"""FastAPI application factory for the context-pulse collector."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from context_pulse.collector.delta_engine import SessionState, restore_sessions_from_db
from context_pulse.collector.routes import api_router, hook_router
from context_pulse.config import ensure_config_dir, get_db_path, load_config
from context_pulse.db.maintenance import prune_old_data
from context_pulse.db.schema import open_db, run_migrations
from context_pulse.engine.baseline import BaselineManager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage collector startup and shutdown lifecycle.

    Startup:
        1. Load configuration from TOML (with env overrides).
        2. Resolve and ensure the database parent directory exists.
        3. Open the SQLite database and run migrations.
        4. Restore in-memory session state from recent DB data.
        5. Store shared state on ``app.state``.

    Shutdown:
        Close the database connection.
    """
    # -- startup ---------------------------------------------------------
    ensure_config_dir()
    cfg = load_config()

    db_path = get_db_path(cfg)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    logger.info("Database path: %s", db_path)

    db = await open_db(db_path)
    await run_migrations(db)

    # Prune old data based on retention policy
    if cfg.retention.retention_days > 0:
        pruned = await prune_old_data(db, cfg.retention.retention_days)
        if pruned:
            logger.info("Startup pruning: %s", pruned)

    sessions: dict[str, SessionState] = {}
    restored = await restore_sessions_from_db(
        sessions, db, lookback_ms=cfg.server.session_restore_lookback_ms,
    )
    logger.info("Restored %d session(s) from database", restored)

    baseline_manager = BaselineManager(
        db=db,
        window_size=cfg.anomaly.baseline_window,
        update_interval=cfg.server.baseline_update_interval,
    )
    app.state.baseline_manager = baseline_manager

    app.state.db = db
    app.state.config = cfg
    app.state.sessions = sessions
    app.state.start_time = time.time()

    logger.info(
        "context-pulse collector started on %s:%d",
        cfg.collector.host,
        cfg.collector.port,
    )

    yield

    # -- shutdown --------------------------------------------------------
    logger.info("Shutting down context-pulse collector")
    await baseline_manager.flush_all()
    logger.info("Baselines flushed to database")
    await db.close()
    logger.info("Database connection closed")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        A fully configured :class:`FastAPI` instance with hook and API
        routers included.
    """
    app = FastAPI(lifespan=lifespan, title="context-pulse")
    app.include_router(hook_router, prefix="/hook")
    app.include_router(api_router, prefix="/api")
    return app
