# database/strategy_reporting_db.py
"""
Shared state for the Python-strategy reporting subprocess (``strategy_reporting/``).

Until 2026-08-05 this state (last-pushed PnL snapshot, legs currently in
error mode, pending Retry/Cancel/Manual actions, Force Exit requests) lived
purely as in-process dicts inside ``blueprints/python_strategy.py`` --
`STRATEGY_PNL`, `STRATEGY_ERRORS`, `STRATEGY_ACTIONS`, `STRATEGY_FORCE_EXIT`.
That meant every strategy subprocess's routine reporting call (PnL push,
error push, pending-action poll, force-exit poll) had to be served by the
same single gunicorn+eventlet worker that also serves every other route in
the app -- so an unrelated slow endpoint elsewhere (confirmed:
`/traffic/api/stats`'s ~57 sequential SQLite queries) could block the worker
long enough that strategy reporting calls timed out, even though nothing
about the strategy itself was wrong.

Moving this state here lets a genuinely separate process
(`strategy_reporting/server.py`) serve those calls with zero dependency on
the main app's own responsiveness, while the main process's browser-facing
routes (`api_get_pnl`, `api_get_leg_errors`, `api_post_leg_action`,
`api_request_force_exit`) read/write the exact same tables -- DB is the
single source of truth both processes agree on, no in-process cache to keep
in sync. See CLAUDE.md's "ZeroMQ bus" section for how the two processes tell
each other "something changed" (the live SSE push to the browser still needs
that -- writing to these tables alone doesn't notify anyone).

Mirrors database/scalping_db.py: SQLite via the shared NullPool engine
factory (one connection per operation, closed immediately -- FD hygiene for
a long-running Gunicorn/eventlet process that never restarts), scoped_session
registered in utils/db_sessions.py for the same reason.
"""

import logging

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.sql import func

from database.engine_factory import create_db_engine

logger = logging.getLogger(__name__)

# Canonical engine factory enforces the project-wide pooling policy
# (SQLite -> NullPool with check_same_thread=False) for FD hygiene. No
# database_url override -- these tables live in the same openalgo.db as
# every other DATABASE_URL-backed module (auth, strategy configs, etc.),
# matching scalping_db.py's own choice not to spin up a 7th DB file for a
# small amount of state.
engine = create_db_engine()

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class StrategyPnl(Base):
    """Last-pushed PnL snapshot for a running strategy. One row per
    strategy_id, overwritten on every push (see report_pnl_to_platform in
    every deployed strategy script)."""

    __tablename__ = "strategy_pnl"

    strategy_id = Column(String(64), primary_key=True)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    # JSON-encoded list of open-position dicts -- same shape the frontend's
    # OpenPosition type expects, stored as text since this is SQLite.
    open_positions = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class StrategyLegError(Base):
    """A leg currently sitting in error mode (entry_failed/exit_failed),
    awaiting a Retry/Cancel/Manually-Completed decision. Row is deleted once
    resolved -- see docs/prd/python-strategies-order-error-recovery.md."""

    __tablename__ = "strategy_leg_errors"
    __table_args__ = (UniqueConstraint("strategy_id", "leg_key", name="uq_strategy_leg_error"),)

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    leg_key = Column(String(64), nullable=False)
    error_state = Column(String(20), nullable=False, default="")
    error_kind = Column(String(20), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    error_since = Column(String(40), nullable=False, default="")
    symbol = Column(String(60), nullable=False, default="")
    quantity = Column(Integer, nullable=False, default=0)
    action = Column(String(10), nullable=False, default="")  # BUY/SELL the leg was attempting


class StrategyPendingAction(Base):
    """A Retry/Cancel/Manually-Completed decision the user submitted for a
    leg currently in error mode, awaiting the strategy subprocess's next
    poll. Row is deleted once the subprocess acks having consumed it."""

    __tablename__ = "strategy_pending_actions"
    __table_args__ = (UniqueConstraint("strategy_id", "leg_key", name="uq_strategy_pending_action"),)

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(String(64), nullable=False, index=True)
    leg_key = Column(String(64), nullable=False)
    action = Column(String(10), nullable=False)  # retry | cancel | manual
    fill_price = Column(Float, nullable=True)  # set only for a "manual" resolution


class StrategyForceExit(Base):
    """One row per strategy with a Force Exit request outstanding. Row is
    deleted once the strategy subprocess confirms every leg closed (the main
    process is what actually stops the OS process at that point -- see
    strategy_reporting/server.py's force_exit_complete handling)."""

    __tablename__ = "strategy_force_exit"

    strategy_id = Column(String(64), primary_key=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    status = Column(String(10), nullable=False, default="pending")  # pending | complete


def init_db():
    """Create the strategy-reporting tables if they don't exist. No
    migration needed for existing installs -- this state was purely
    in-process dicts before, wiped on every gunicorn restart, so there is
    nothing to carry over; tables just start empty."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Strategy Reporting DB", logger)


def get_pnl_snapshot(strategy_id: str) -> dict:
    """Shared by strategy_reporting/server.py's GET .../pnl route and
    strategy_reporting/broadcast_bridge.py (which re-reads the fresh row
    after a PUSH to build the SSE payload) -- one place for the JSON shape
    both need, importable from either process without pulling in the whole
    Flask app module."""
    import json

    row = db_session.get(StrategyPnl, strategy_id)
    if row is None:
        return {
            "realized_pnl": 0.0, "unrealized_pnl": 0.0, "total_pnl": 0.0,
            "open_positions": [], "updated_at": None,
        }
    open_positions = json.loads(row.open_positions or "[]")
    return {
        "realized_pnl": row.realized_pnl,
        "unrealized_pnl": row.unrealized_pnl,
        "total_pnl": row.realized_pnl + row.unrealized_pnl,
        "open_positions": open_positions,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
