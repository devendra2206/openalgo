"""
Runs INSIDE the main gunicorn process. Binds a ZMQ SUB and reacts to events
published by the strategy_reporting subprocess (see its _publish_bridge_event)
by calling blueprints/python_strategy.py's EXISTING, UNCHANGED broadcast_*
functions (which drive the /python/api/events SSE stream the main process
still serves -- see strategy_reporting/server.py's relay for why the main
process still needs live SSE subscribers to push to) and, for a completed
Force Exit, stop_strategy_process (the only process that holds the actual
Popen handle in RUNNING_STRATEGIES).

Same "one binder, many connecting publishers" shape as the existing
market-data ZMQ bus (CLAUDE.md's "SUB binds, PUBs connect" invariant) --
a new, independent instance on its own fixed port, not a second binder on
the existing one.
"""

import json
import os
import sys
import threading as _stdlib_threading

from utils.logging import get_logger

logger = get_logger(__name__)

# Real OS thread even under eventlet -- this fork's established escape hatch
# (see services/websocket_client.py, sandbox/websocket_execution_engine.py,
# websocket_proxy/app_integration.py) for "needs a genuine blocking loop
# that eventlet's cooperative scheduler shouldn't have to babysit."
if "eventlet" in sys.modules:
    import eventlet

    _original_threading = eventlet.patcher.original("threading")
else:
    _original_threading = _stdlib_threading

_bridge_thread = None
_bridge_started = False


def _handle_event(event: dict):
    from blueprints.python_strategy import (
        broadcast_error_update,
        broadcast_pnl_update,
        broadcast_status_update,
        broadcast_trade_update,
        stop_strategy_process,
    )
    from database.strategy_reporting_db import get_pnl_snapshot

    event_type = event.get("type")
    strategy_id = event.get("strategy_id")
    if not strategy_id:
        return

    try:
        if event_type == "pnl_update":
            snapshot = get_pnl_snapshot(strategy_id)
            broadcast_pnl_update(strategy_id, snapshot)
        elif event_type == "error_update":
            broadcast_error_update(
                strategy_id, event.get("leg_key", ""), {"cleared": bool(event.get("cleared"))}
            )
        elif event_type == "trade_update":
            broadcast_trade_update(strategy_id)
        elif event_type == "force_exit_complete":
            # stop_strategy_process is the only thing that can actually
            # terminate the OS process -- it alone lives in RUNNING_STRATEGIES,
            # which only the main process holds (real Popen handles aren't
            # shareable across processes). Force Exit is an emergency/manual
            # override, so leaving the strategy running right after could
            # immediately re-enter on the very next signal -- same reasoning
            # blueprints/python_strategy.py's own api_complete_force_exit
            # docstring already documents.
            stop_strategy_process(strategy_id)
            broadcast_status_update(
                strategy_id, "stopped", "Force exit completed -- all positions closed"
            )
        else:
            logger.warning(f"broadcast_bridge: unknown event type {event_type!r}")
    except Exception:
        logger.exception(f"broadcast_bridge: failed to handle event {event}")


def _bridge_loop():
    import zmq

    port = os.getenv("ZMQ_REPORTING_PORT", "5565")
    host = os.getenv("ZMQ_HOST", "127.0.0.1")
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.bind(f"tcp://{host}:{port}")
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    logger.info(f"strategy_reporting broadcast bridge listening on {host}:{port}")

    while True:
        try:
            raw = socket.recv()
            event = json.loads(raw)
            _handle_event(event)
        except Exception:
            logger.exception("broadcast_bridge: error in receive loop")


def start_broadcast_bridge():
    """Call once from app.py, next to start_strategy_reporting_subprocess()."""
    global _bridge_thread, _bridge_started
    if _bridge_started:
        logger.debug("broadcast_bridge already started, skipping")
        return
    _bridge_started = True
    _bridge_thread = _original_threading.Thread(
        target=_bridge_loop, daemon=True, name="strategy-reporting-bridge"
    )
    _bridge_thread.start()
