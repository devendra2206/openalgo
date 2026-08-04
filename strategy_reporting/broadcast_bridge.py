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

2026-08-05 fix -- do NOT call broadcast_* from a genuine OS thread under
eventlet. `SSE_SUBSCRIBERS`' queue.Queue objects and SSE_LOCK in
blueprints/python_strategy.py are the eventlet-monkey-patched
threading/queue primitives (the whole process is patched under
gunicorn+eventlet) -- mutating/signaling them from a real, unpatched OS
thread is the exact "greenlet.error: Cannot switch to a different thread"
class of bug already documented and fixed in
services/websocket_client.py's `_run_coroutine_and_wait` (production
incident, 2026-07-29). It doesn't always hard-crash here -- observed
in production as "PnL/trade price updating, just slowly": a foreign
thread's queue.put() can leave the notification the SSE generator's
q.get() is waiting on unheard by eventlet's hub, so the update only
surfaces once something else happens to give the hub a reason to check
(worst case, the 30s heartbeat cycle in api_strategy_events' q.get(timeout=30)).

Fix: under eventlet, the receive loop runs as a genuine eventlet green
thread (eventlet.spawn), not an OS thread -- the only non-green part is
each individual blocking socket.recv() call, wrapped in
eventlet.tpool.execute() (eventlet's own documented, hub-safe mechanism
for exactly this: run one blocking call on a background native thread,
then safely hand the result back to the calling greenlet). Everything
downstream of that -- _handle_event(), and therefore every call into
broadcast_* -- runs on the calling GREENLET, in the same eventlet-green
world as the SSE generator it's signaling, so it's safe by construction.
Outside eventlet (this dev machine, or any environment where eventlet
isn't monkey-patching the process), there is no greenlet/native-thread
split to worry about -- plain threading.Thread + plain queue.Queue are
mutually safe, so the original real-OS-thread approach is kept for that
case.
"""

import json
import os
import sys
import threading as _stdlib_threading

from utils.logging import get_logger

logger = get_logger(__name__)

_EVENTLET_ACTIVE = "eventlet" in sys.modules

if _EVENTLET_ACTIVE:
    import eventlet
    import eventlet.tpool

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
    finally:
        # get_pnl_snapshot() (and anything else touched above) uses a
        # scoped_session -- this runs outside a Flask request, so
        # app.py's own teardown_appcontext never fires for it (see
        # utils/db_sessions.py's own docstring: background work must
        # remove its scoped sessions itself). Without this, a stale
        # cached row on this same thread/greenlet could be served again
        # on the NEXT event, same staleness class fixed in
        # strategy_reporting/server.py's teardown_appcontext.
        from utils.db_sessions import remove_all_scoped_sessions

        remove_all_scoped_sessions()


def _open_socket():
    import zmq

    port = os.getenv("ZMQ_REPORTING_PORT", "5565")
    host = os.getenv("ZMQ_HOST", "127.0.0.1")
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.bind(f"tcp://{host}:{port}")
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    logger.info(f"strategy_reporting broadcast bridge listening on {host}:{port}")
    return socket


def _bridge_loop_native_thread():
    """No-eventlet path: a plain OS thread with a plain blocking recv() --
    safe as-is, since there's no greenlet/native-thread primitive split to
    worry about outside eventlet."""
    socket = _open_socket()
    while True:
        try:
            raw = socket.recv()
            event = json.loads(raw)
            _handle_event(event)
        except Exception:
            logger.exception("broadcast_bridge: error in receive loop")


def _bridge_loop_eventlet_green():
    """Eventlet path: the loop itself is a green thread (eventlet.spawn),
    so _handle_event()'s broadcast_* calls -- which touch
    SSE_SUBSCRIBERS'/SSE_LOCK's eventlet-patched primitives -- run on a
    genuine greenlet, safe by construction. Only the individual blocking
    recv() call needs to leave the green world, via eventlet.tpool.execute
    (eventlet's own hub-safe bridge for exactly this: one blocking call on
    a background native thread, result handed back to the calling
    greenlet). See this module's docstring for the production incident
    this fixes."""
    socket = _open_socket()
    while True:
        try:
            raw = eventlet.tpool.execute(socket.recv)
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

    if _EVENTLET_ACTIVE:
        eventlet.spawn(_bridge_loop_eventlet_green)
    else:
        _bridge_thread = _stdlib_threading.Thread(
            target=_bridge_loop_native_thread, daemon=True, name="strategy-reporting-bridge"
        )
        _bridge_thread.start()
