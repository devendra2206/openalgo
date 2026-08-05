"""
strategy_reporting/server.py

A dedicated subprocess for the `/python` strategy page's reporting/action
traffic -- PnL, leg errors, pending Retry/Cancel/Manual actions, Force Exit,
and trade/execution history -- plus a thin relay for every other `/python/*`
route.

Why this exists: production runs `gunicorn --worker-class eventlet -w 1` --
a single worker serving every request. Confirmed root cause of a real
incident (2026-08-04): `/traffic/api/stats` ran ~57 sequential SQLite
queries per call, and since eventlet only yields at monkey-patched socket
I/O (not SQLite/file I/O), that endpoint blocked the single worker for its
full multi-second duration -- during which strategy subprocesses' own
loopback reporting calls (`report_pnl_to_platform`, `check_pending_action`,
`check_force_exit`, etc.) timed out, even on a strategy script that already
had the best available client-side isolation. This process removes that
class of bug structurally: it is a genuinely separate OS process, running
plain OS threads (not eventlet), so nothing that ever blocks the main
gunicorn worker can affect it.

`blueprints/python_strategy.py` (the main app's `/python` blueprint) is
DELIBERATELY left completely unmodified -- see docs/CUSTOMIZATIONS.md and
strategy_reporting's own package docstring for why: it's the file most
likely to collide with upstream's own active development (start/stop/
schedule/upload/CRUD, the BackgroundScheduler, RUNNING_STRATEGIES' Popen
handles), and none of that code was ever the thing timing out. This process
implements ONLY the reporting/action/history routes locally, and relays
every other `/python/*` request straight through to the unmodified main
process (see `_relay` below) -- so nginx can route the whole `/python`
prefix here with one rule while the actual lifecycle logic stays untouched.
"""

import csv
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytz
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, session, stream_with_context
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from sqlalchemy.exc import OperationalError

# Load .env before any module that reads env vars at import time (mirrors
# websocket_proxy/server.py's own standalone-subprocess bootstrap).
load_dotenv()

# Repo root is this file's grandparent (strategy_reporting/server.py ->
# strategy_reporting/ -> repo root) -- added to sys.path so `database.*`/
# `utils.*` imports resolve when this runs as `python -m strategy_reporting.server`
# from an arbitrary cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from database.auth_db import verify_api_key  # noqa: E402
from database.strategy_reporting_db import (  # noqa: E402
    StrategyForceExit,
    StrategyLegError,
    StrategyPendingAction,
    StrategyPnl,
    db_session,
    get_pnl_snapshot,
    init_db,
)
from utils.logging import get_logger  # noqa: E402
from utils.session import is_session_valid, revoke_user_tokens  # noqa: E402

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

STRATEGY_REPORTING_PORT = int(os.getenv("STRATEGY_REPORTING_PORT", "8766"))
FLASK_PORT = os.getenv("FLASK_PORT", "5000")

# install.sh's gunicorn systemd unit binds ONLY a Unix domain socket
# ("--bind unix:$OPENALGO_PATH/openalgo.sock", see install/install.sh) -- no
# TCP port is ever opened in that production layout, since nginx reaches the
# app over the socket directly (proxy_pass http://unix:$SOCKET_FILE). A relay
# that assumed a TCP main app (http://127.0.0.1:{FLASK_PORT}) got
# "Connection refused" on every single request there (2026-08-05 incident,
# found right after the nginx /python routing fix finally let /python/*
# requests reach this process at all). The dev server (`uv run app.py`) has
# no such socket file and really does listen on plain TCP, so fall back to
# that when the socket isn't present.
_MAIN_APP_SOCKET = _REPO_ROOT / "openalgo.sock"
_USE_UDS = _MAIN_APP_SOCKET.exists()
MAIN_APP_BASE = "http://openalgo-app" if _USE_UDS else f"http://127.0.0.1:{FLASK_PORT}"


def _make_relay_transport():
    """A FRESH httpx.HTTPTransport every call -- never share one instance
    across multiple httpx.Client()s. httpx.Client.close() unconditionally
    closes its transport's connection pool (see httpx's own Client.close()
    source), even for a transport that was passed in rather than created
    internally. The SSE relay below opens a short-lived Client per stream
    (`with httpx.Client(transport=...) as c:`); if it shared the pooled
    _relay_client's transport, EVERY time an SSE stream ended (browser tab
    closed, page navigated away, EventSource auto-reconnect) it would close
    the connection pool out from under the persistent _relay_client too,
    which then handed back a dead file descriptor on its next reused
    connection -- httpx.ReadError: [Errno 9] Bad file descriptor
    (2026-08-05 incident, hit /python/api/logs/... and /python/api/strategy/...
    with real 500s within seconds of an SSE client disconnecting)."""
    if _USE_UDS:
        return httpx.HTTPTransport(uds=str(_MAIN_APP_SOCKET))
    return None

# Same file the main process reads -- CONFIG_FILE in blueprints/python_strategy.py.
# Deliberately re-implemented here rather than imported from that module: this
# process must not import anything from blueprints/python_strategy.py (that
# module runs its own BackgroundScheduler and other import-time side effects
# in the MAIN process; importing it here would run those a second time).
CONFIG_FILE = _REPO_ROOT / "strategies" / "strategy_configs.json"
STRATEGIES_DIR = _REPO_ROOT / "strategies" / "scripts"

app = Flask(__name__)

_app_key = os.getenv("APP_KEY")
if not _app_key:
    raise RuntimeError(
        "CRITICAL: APP_KEY environment variable is not set. strategy_reporting "
        "needs the SAME APP_KEY as the main app to validate the same session cookie."
    )
app.secret_key = _app_key

_host_server = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
_use_https = _host_server.startswith("https://")
_session_cookie_name = os.getenv("SESSION_COOKIE_NAME", "session")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_use_https,
    SESSION_COOKIE_NAME=f"__Secure-{_session_cookie_name}" if _use_https else _session_cookie_name,
)

# Shared client for relaying to the main process -- separate from
# utils/httpx_client.py's singleton on purpose: that one is tuned (pooling,
# retry expectations) for external broker API calls, a different traffic
# shape/purpose than this purely-internal loopback relay. Uses the same
# transport (Unix socket or TCP) resolved above.
_relay_client = httpx.Client(timeout=10.0, transport=_make_relay_transport())


@app.teardown_appcontext
def _shutdown_db_session(exception=None):
    """Missing until 2026-08-05 -- db_session is a scoped_session, which
    SQLAlchemy keys per-thread by default (threading.get_ident()). This
    process serves requests via werkzeug's threaded WSGI server, which
    reuses OS threads across requests -- without removing the session
    after each request, a reused thread's session keeps its identity map
    from earlier requests, so a later GET on that same thread can return a
    stale, cached row instead of another thread's more recent commit.
    Observed in production as the PnL tile and the Trades page
    disagreeing about the exact same open position, reading the exact
    same query, because each happened to land on a different thread with
    a different cache state. Mirrors app.py's own
    shutdown_database_sessions teardown_appcontext for the main app."""
    db_session.remove()


def _write_with_retry(unit_of_work, max_attempts=5):
    """Run `unit_of_work()` -- a no-arg callable that does its own fresh
    read(s), mutation(s), and a single db_session.commit() -- retrying from
    scratch on a SQLite "database is locked" error.

    2026-08-05 incident: with 7 deployed strategies each pushing PnL every
    ~0.8-1s against the same openalgo.db, api_push_pnl's read-then-commit
    (db_session.get() followed later by db_session.commit(), same
    transaction/connection for its whole duration under NullPool) started
    hitting sqlite3.OperationalError: database is locked in production.
    database/__init__.py's busy_timeout=15000 pragma does NOT help here --
    per that module's own docstring, a WAL snapshot conflict (a stale read
    snapshot vs. a newer commit from a concurrent writer) returns
    immediately regardless of busy_timeout; only re-running the whole
    transaction against a FRESH snapshot fixes it, which is exactly why
    `unit_of_work` must re-do its own read, not just retry the commit.

    Every write route in this file follows this same get-then-commit shape
    (7 commit sites) and pushes/acks fire from independent strategy
    processes with no coordination between them, so all of them share this
    risk as the number of concurrently-running strategies grows -- this
    wraps all of them, not just the highest-frequency one."""
    for attempt in range(1, max_attempts + 1):
        try:
            unit_of_work()
            return
        except OperationalError as e:
            db_session.rollback()
            if "database is locked" not in str(e).lower() or attempt == max_attempts:
                raise
            # Small jittered backoff -- long enough to let the current
            # writer elsewhere finish its commit, short enough that even
            # max_attempts retries stay well under any caller's own timeout.
            time.sleep(0.05 * attempt + random.uniform(0, 0.05))


###############################################################################
# Auth helpers
###############################################################################

def _api_key_from_request() -> str | None:
    data = request.get_json(silent=True) or {}
    return data.get("apikey") or request.args.get("apikey")


def _require_api_key():
    """Returns (user_id, None) on success, or (None, (response, status)) on
    failure -- same two-tuple convention verify_strategy_ownership already
    uses elsewhere in this project, so route bodies read the same way."""
    apikey = _api_key_from_request()
    if not apikey:
        return None, (jsonify({"status": "error", "message": "apikey is required"}), 401)
    user_id = verify_api_key(apikey)
    if not user_id:
        return None, (jsonify({"status": "error", "message": "Invalid API key"}), 401)
    return user_id, None


def _load_strategy_config(strategy_id: str) -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    try:
        with CONFIG_FILE.open(encoding="utf-8") as f:
            configs = json.load(f)
    except Exception:
        logger.exception("Failed to read strategy_configs.json")
        return None
    return configs.get(strategy_id)


def _load_strategy_config_mtime() -> str:
    """DEBUG-TEMP (2026-08-05) -- see _verify_strategy_ownership's matching
    comment. Timing context only (writes are already known-atomic via
    os.replace(), so this isn't diagnosing a torn read -- it's checking
    whether a config write happened suspiciously close to a real 403,
    which would point at the restore-on-boot loop's timing rather than
    something in this process). Remove once root cause is confirmed/fixed."""
    try:
        return datetime.fromtimestamp(CONFIG_FILE.stat().st_mtime, tz=IST).isoformat()
    except OSError:
        return "unknown"


def _verify_strategy_ownership(strategy_id: str, user_id):
    """Mirrors blueprints/python_strategy.py's verify_strategy_ownership --
    re-implemented against the same on-disk strategy_configs.json rather
    than imported, per this module's own docstring on why it never imports
    from blueprints.python_strategy."""
    if not strategy_id or ".." in strategy_id or "/" in strategy_id or "\\" in strategy_id:
        return False, (jsonify({"status": "error", "message": "Invalid strategy ID"}), 400)
    config = _load_strategy_config(strategy_id)
    if config is None:
        return False, (jsonify({"status": "error", "message": "Strategy not found"}), 404)
    owner = config.get("user_id")
    if owner and owner != user_id:
        # DEBUG-TEMP (2026-08-05, transient 403-right-after-restart
        # investigation): every simpler explanation has been ruled out by
        # direct evidence -- strategy_reporting's own subprocess was already
        # up and listening 4s before a real occurrence's first 403 (rules
        # out a cold-start race); strategy_configs.json writes are already
        # atomic (os.replace(), rules out a torn read); user_id is never
        # written anywhere after a strategy's initial creation (rules out a
        # restart-triggered logic bug corrupting the owner field). This is
        # the one remaining unknown: what verify_api_key() actually
        # resolved `user_id` to, and what `owner` actually was, at the
        # exact moment of a real failure -- logged here instead of
        # continuing to guess. Remove once root cause is confirmed/fixed.
        logger.warning(
            f"[DEBUG-TEMP] ownership mismatch for strategy_id={strategy_id!r}: "
            f"config owner={owner!r} vs resolved user_id={user_id!r} "
            f"(config file mtime={_load_strategy_config_mtime()})"
        )
        return False, (jsonify({"status": "error", "message": "Unauthorized access to strategy"}), 403)
    return True, None


def _require_session(f):
    """Local equivalent of utils.session.check_session_validity -- same
    is_session_valid()/revoke_user_tokens()/session.clear() logic, but
    redirects to a hardcoded "/auth/login" path instead of
    `url_for("auth.login")`: that call resolves against THIS Flask app's own
    URL map, which (deliberately, see this module's docstring) has no
    `auth_bp` registered -- only the main process does. A hardcoded path
    still lands on the right page, since nginx's /python rule only covers
    /python/*; /auth/login is served by the main app directly, not relayed
    through here at all."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_session_valid():
            revoke_user_tokens()
            session.clear()
            is_ajax = (
                request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or request.headers.get("Accept", "").startswith("application/json")
                or request.content_type == "application/json"
                or request.is_json
            )
            if is_ajax:
                return jsonify({
                    "status": "error", "error": "session_expired",
                    "message": "Your session has expired. Please log in again.",
                }), 401
            from flask import redirect

            return redirect("/auth/login")
        return f(*args, **kwargs)

    return decorated


###############################################################################
# ZMQ publisher -- notifies the main process to broadcast an SSE update or
# (for force_exit/complete) to actually stop the strategy's OS process. See
# strategy_reporting/broadcast_bridge.py for the receiving side.
###############################################################################

_zmq_context = None
_zmq_socket = None


def _publish_bridge_event(event: dict):
    """Fire-and-forget -- a lost bridge message means the browser's SSE
    view goes stale until its next poll/reload, never a lost trade or a
    stuck strategy. Must never raise into the calling route."""
    global _zmq_context, _zmq_socket
    try:
        import zmq

        if _zmq_socket is None:
            port = os.getenv("ZMQ_REPORTING_PORT", "5565")
            _zmq_context = zmq.Context()
            _zmq_socket = _zmq_context.socket(zmq.PUB)
            _zmq_socket.setsockopt(zmq.LINGER, 1000)
            _zmq_socket.setsockopt(zmq.SNDHWM, 1000)
            _zmq_socket.connect(f"tcp://127.0.0.1:{port}")
        _zmq_socket.send_json(event)
    except Exception:
        logger.exception(f"Failed to publish bridge event: {event}")


###############################################################################
# PnL
###############################################################################

@app.route("/python/api/strategy/<strategy_id>/pnl", methods=["POST"])
def api_push_pnl(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config

    data = request.get_json(silent=True) or {}
    try:
        realized_pnl = float(data.get("realized_pnl", 0) or 0)
        unrealized_pnl = float(data.get("unrealized_pnl", 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "realized_pnl/unrealized_pnl must be numeric"}), 400
    open_positions = data.get("open_positions") or []
    open_positions_json = json.dumps(open_positions)

    def _do_write():
        # Single atomic UPSERT, no preceding SELECT -- avoids the
        # SQLITE_BUSY_SNAPSHOT class of "database is locked" entirely (not
        # just retries around it) by never holding a stale read snapshot
        # across a later write. See docs/CUSTOMIZATIONS.md's 2026-08-05
        # entry for the full explanation; this is the highest-frequency
        # writer of all 7 (every running strategy, every ~0.8-1s), so it's
        # the one most worth converting.
        stmt = sqlite_upsert(StrategyPnl).values(
            strategy_id=strategy_id,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_positions=open_positions_json,
        ).on_conflict_do_update(
            index_elements=["strategy_id"],
            set_={
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "open_positions": open_positions_json,
                # StrategyPnl.updated_at's onupdate=func.now() is an ORM-session
                # convenience -- it does NOT fire for a Core-level upsert (verified:
                # the compiled SQL has no updated_at in its SET clause at all
                # without this), so it must be set explicitly here or the column
                # would silently stop refreshing after each row's first insert.
                "updated_at": func.now(),
            },
        )
        db_session.execute(stmt)
        db_session.commit()

    _write_with_retry(_do_write)

    _publish_bridge_event({"type": "pnl_update", "strategy_id": strategy_id})
    return jsonify({"status": "success"})


@app.route("/python/api/strategy/<strategy_id>/pnl", methods=["GET"])
@_require_session
def api_get_pnl(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err
    return jsonify(get_pnl_snapshot(strategy_id))


###############################################################################
# Leg errors
###############################################################################

@app.route("/python/api/strategy/<strategy_id>/errors", methods=["POST"])
def api_push_leg_error(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config

    data = request.get_json(silent=True) or {}
    leg_key = data.get("leg_key")
    if not leg_key:
        return jsonify({"status": "error", "message": "leg_key is required"}), 400

    def _do_write():
        if data.get("clear"):
            # Bulk delete -- a no-op if the row doesn't exist, so no
            # existence check needed first (see api_push_pnl's comment on
            # why avoiding a preceding SELECT matters under SQLite WAL).
            db_session.query(StrategyLegError).filter_by(
                strategy_id=strategy_id, leg_key=leg_key
            ).delete()
            db_session.commit()
        else:
            values = {
                "error_state": data.get("error_state", ""),
                "error_kind": data.get("error_kind", ""),
                "error_message": data.get("error_message", ""),
                "error_since": data.get("error_since", ""),
                "symbol": data.get("symbol", ""),
                "quantity": data.get("quantity", 0),
                "action": data.get("action", ""),
            }
            stmt = sqlite_upsert(StrategyLegError).values(
                strategy_id=strategy_id, leg_key=leg_key, **values,
            ).on_conflict_do_update(
                index_elements=["strategy_id", "leg_key"],
                set_=values,
            )
            db_session.execute(stmt)
            db_session.commit()

    _write_with_retry(_do_write)

    _publish_bridge_event({
        "type": "error_update", "strategy_id": strategy_id, "leg_key": leg_key,
        "cleared": bool(data.get("clear")),
    })
    return jsonify({"status": "success"})


@app.route("/python/api/strategy/<strategy_id>/errors", methods=["GET"])
@_require_session
def api_get_leg_errors(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err
    rows = db_session.query(StrategyLegError).filter_by(strategy_id=strategy_id).all()
    legs = [
        {
            "leg_key": r.leg_key, "error_state": r.error_state, "error_kind": r.error_kind,
            "error_message": r.error_message, "error_since": r.error_since,
            "symbol": r.symbol, "quantity": r.quantity, "action": r.action,
        }
        for r in rows
    ]
    return jsonify({"errors": legs})


###############################################################################
# Trade closed (no state -- pure SSE nudge, see blueprints/python_strategy.py's
# original api_push_trade_closed docstring for why)
###############################################################################

@app.route("/python/api/strategy/<strategy_id>/trade_closed", methods=["POST"])
def api_push_trade_closed(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config
    _publish_bridge_event({"type": "trade_update", "strategy_id": strategy_id})
    return jsonify({"status": "success"})


###############################################################################
# Pending Retry/Cancel/Manual actions
###############################################################################

@app.route("/python/api/strategy/<strategy_id>/pending_action", methods=["GET"])
def api_check_pending_action(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    leg_key = request.args.get("leg_key")
    if not leg_key:
        return jsonify({"status": "error", "message": "leg_key is required"}), 400
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config

    row = (
        db_session.query(StrategyPendingAction)
        .filter_by(strategy_id=strategy_id, leg_key=leg_key)
        .first()
    )
    if not row:
        return jsonify({"action": None})
    return jsonify({"action": row.action, "fill_price": row.fill_price})


@app.route("/python/api/strategy/<strategy_id>/pending_action/ack", methods=["POST"])
def api_ack_pending_action(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    leg_key = data.get("leg_key")
    if not leg_key:
        return jsonify({"status": "error", "message": "leg_key is required"}), 400
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config

    def _do_write():
        db_session.query(StrategyPendingAction).filter_by(
            strategy_id=strategy_id, leg_key=leg_key
        ).delete()
        db_session.commit()

    _write_with_retry(_do_write)
    return jsonify({"status": "success"})


@app.route("/python/api/strategy/<strategy_id>/action", methods=["POST"])
@_require_session
def api_post_leg_action(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err

    data = request.get_json(silent=True) or {}
    leg_key = data.get("leg_key")
    action = data.get("action")
    if not leg_key or action not in ("retry", "cancel", "manual"):
        return jsonify({"status": "error", "message": "leg_key and a valid action are required"}), 400

    fill_price = None
    if action == "manual":
        try:
            fill_price = float(data.get("fill_price"))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "fill_price must be a positive number for a manual resolution"}), 400
        if fill_price <= 0:
            return jsonify({"status": "error", "message": "fill_price must be a positive number for a manual resolution"}), 400

    def _do_write():
        stmt = sqlite_upsert(StrategyPendingAction).values(
            strategy_id=strategy_id, leg_key=leg_key, action=action, fill_price=fill_price,
        ).on_conflict_do_update(
            index_elements=["strategy_id", "leg_key"],
            set_={"action": action, "fill_price": fill_price},
        )
        db_session.execute(stmt)
        db_session.commit()

    _write_with_retry(_do_write)

    return jsonify({"status": "success"})


###############################################################################
# Force exit
###############################################################################

@app.route("/python/api/strategy/<strategy_id>/force_exit", methods=["POST"])
@_require_session
def api_request_force_exit(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err
    # NOTE: the old main-process route also checked `strategy_id in
    # RUNNING_STRATEGIES` before accepting this -- that in-memory registry
    # lives only in the main process (real Popen handles, not shareable) and
    # deliberately isn't reachable here. Writing this flag for a strategy
    # that isn't actually running is a harmless no-op: nothing ever polls
    # GET .../force_exit for a strategy that isn't running, so the row just
    # sits unconsumed. The UI itself only shows the Force Exit button for a
    # strategy it already knows is running, so this path isn't reachable in
    # normal use either way.
    def _do_write():
        # requested_at intentionally NOT in set_ -- matches the prior ORM
        # code's behavior of only ever touching `status` on an existing
        # row, leaving the original request timestamp untouched by a
        # re-request while one is already pending.
        stmt = sqlite_upsert(StrategyForceExit).values(
            strategy_id=strategy_id, status="pending",
        ).on_conflict_do_update(
            index_elements=["strategy_id"],
            set_={"status": "pending"},
        )
        db_session.execute(stmt)
        db_session.commit()

    _write_with_retry(_do_write)
    return jsonify({"status": "success", "message": "Force exit requested"})


@app.route("/python/api/strategy/<strategy_id>/force_exit", methods=["GET"])
def api_check_force_exit(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config
    row = db_session.get(StrategyForceExit, strategy_id)
    return jsonify({"requested": bool(row and row.status == "pending")})


@app.route("/python/api/strategy/<strategy_id>/force_exit/complete", methods=["POST"])
def api_complete_force_exit(strategy_id):
    user_id, err = _require_api_key()
    if err:
        return err
    ok, err_or_config = _verify_strategy_ownership(strategy_id, user_id)
    if not ok:
        return err_or_config

    def _do_write():
        db_session.query(StrategyForceExit).filter_by(strategy_id=strategy_id).delete()
        db_session.commit()

    _write_with_retry(_do_write)

    # The actual OS-process stop only the MAIN process can do (it alone
    # holds the Popen handle) -- see strategy_reporting/broadcast_bridge.py.
    _publish_bridge_event({"type": "force_exit_complete", "strategy_id": strategy_id})
    return jsonify({"status": "success"})


###############################################################################
# Trade / execution history (reads trades_{id}.csv, same file the strategy
# subprocess itself writes -- unchanged from blueprints/python_strategy.py)
###############################################################################

def _read_trade_log_rows(strategy_id: str) -> list:
    csv_path = STRATEGIES_DIR / f"trades_{strategy_id}.csv"
    if not csv_path.exists():
        return []
    with csv_path.open("r", newline="", encoding="utf-8", errors="replace") as fp:
        return list(csv.DictReader(fp))


def _entry_time_ist_date(entry_time: str) -> str | None:
    if not entry_time:
        return None
    try:
        dt = datetime.fromisoformat(entry_time)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.astimezone(IST).date().isoformat()


def _entry_time_matches_date(entry_time: str, target_date) -> bool:
    if not entry_time:
        return False
    try:
        dt = datetime.fromisoformat(entry_time)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    return dt.astimezone(IST).date() == target_date


def _entry_time_is_today_ist(entry_time: str) -> bool:
    return _entry_time_matches_date(entry_time, datetime.now(IST).date())


TODAY_EXECUTION_FILTER = "__today__"


@app.route("/python/api/strategy/<strategy_id>/executions")
@_require_session
def api_get_executions(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err
    try:
        rows = _read_trade_log_rows(strategy_id)
    except Exception as e:
        logger.exception(f"Error reading trade log for {strategy_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    groups: dict[str, dict] = {}
    for row in rows:
        exec_id = row.get("execution_id") or "legacy"
        group = groups.setdefault(exec_id, {"execution_id": exec_id, "start_time": None,
                                             "trade_count": 0, "total_pnl": 0.0})
        group["trade_count"] += 1
        entry_time = row.get("entry_time") or None
        if entry_time and (group["start_time"] is None or entry_time < group["start_time"]):
            group["start_time"] = entry_time
        try:
            group["total_pnl"] += float(row.get("pnl_rupees", 0) or 0)
        except (TypeError, ValueError):
            pass

    snapshot = get_pnl_snapshot(strategy_id)
    for pos in snapshot.get("open_positions", []):
        exec_id = str(pos.get("execution_id")) if pos.get("execution_id") else "legacy"
        group = groups.setdefault(exec_id, {"execution_id": exec_id, "start_time": None,
                                             "trade_count": 0, "total_pnl": 0.0})
        group["trade_count"] += 1
        entry_time = pos.get("entry_time") or None
        if entry_time and (group["start_time"] is None or entry_time < group["start_time"]):
            group["start_time"] = entry_time
        group["total_pnl"] += float(pos.get("pnl", 0) or 0)

    for group in groups.values():
        group["total_pnl"] = round(group["total_pnl"], 2)

    def _sort_key(group):
        try:
            return (0, -int(group["execution_id"]))
        except (TypeError, ValueError):
            return (1, 0)

    executions = sorted(groups.values(), key=_sort_key)
    return jsonify({"executions": executions})


@app.route("/python/api/strategy/<strategy_id>/trade-dates")
@_require_session
def api_get_trade_dates(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err
    try:
        rows = _read_trade_log_rows(strategy_id)
    except Exception as e:
        logger.exception(f"Error reading trade log for {strategy_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    counts: dict[str, int] = {}
    for row in rows:
        d = _entry_time_ist_date(row.get("entry_time", ""))
        if d:
            counts[d] = counts.get(d, 0) + 1

    snapshot = get_pnl_snapshot(strategy_id)
    for pos in snapshot.get("open_positions", []):
        d = _entry_time_ist_date(pos.get("entry_time", ""))
        if d:
            counts[d] = counts.get(d, 0) + 1

    dates = sorted(
        ({"date": d, "trade_count": c} for d, c in counts.items()),
        key=lambda item: item["date"],
        reverse=True,
    )
    return jsonify({"dates": dates})


@app.route("/python/api/strategy/<strategy_id>/trades")
@_require_session
def api_get_trades(strategy_id):
    ok, err = _verify_strategy_ownership(strategy_id, session.get("user"))
    if not ok:
        return err

    execution_id = request.args.get("execution_id")
    today_only = execution_id == TODAY_EXECUTION_FILTER

    trade_date = None
    date_param = request.args.get("date")
    if date_param:
        try:
            trade_date = datetime.strptime(date_param, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"status": "error", "message": f"Invalid date {date_param!r}, expected YYYY-MM-DD"}), 400

    try:
        rows = _read_trade_log_rows(strategy_id)
    except Exception as e:
        logger.exception(f"Error reading trade log for {strategy_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    trades = []
    total_pnl = 0.0
    for row in rows:
        if trade_date is not None:
            if not _entry_time_matches_date(row.get("entry_time", ""), trade_date):
                continue
        elif today_only:
            if not _entry_time_is_today_ist(row.get("entry_time", "")):
                continue
        elif execution_id is not None and (row.get("execution_id") or "legacy") != execution_id:
            continue
        row = dict(row, status="CLOSED")
        trades.append(row)
        try:
            total_pnl += float(row.get("pnl_rupees", 0) or 0)
        except (TypeError, ValueError):
            pass

    snapshot = get_pnl_snapshot(strategy_id)
    for pos in snapshot.get("open_positions", []):
        pos_exec_id = str(pos.get("execution_id")) if pos.get("execution_id") else "legacy"
        if trade_date is not None:
            if not _entry_time_matches_date(pos.get("entry_time", ""), trade_date):
                continue
        elif today_only:
            if not _entry_time_is_today_ist(pos.get("entry_time", "")):
                continue
        elif execution_id is not None and pos_exec_id != execution_id:
            continue
        pnl = float(pos.get("pnl", 0) or 0)
        trades.append({
            "leg": pos.get("leg_key", ""),
            "symbol": pos.get("symbol", ""),
            "quantity": pos.get("quantity", ""),
            "direction": pos.get("direction", ""),
            "entry_time": pos.get("entry_time", ""),
            "entry_px": pos.get("entry_price", ""),
            "exit_time": "",
            "exit_px": pos.get("current_price", ""),
            "pnl_points": "",
            "pnl_rupees": round(pnl, 2),
            "exit_reason": "",
            "execution_id": pos_exec_id,
            "status": "OPEN",
        })
        total_pnl += pnl

    return jsonify({"trades": trades, "total_pnl": round(total_pnl, 2)})


###############################################################################
# Relay -- everything else under /python/* goes to the unmodified main
# process. This is what lets nginx route the whole /python prefix here with
# one rule while blueprints/python_strategy.py's lifecycle code stays
# completely untouched in the main process.
###############################################################################

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

# "host" was in the strip set above until 2026-08-05 -- it is NOT actually a
# hop-by-hop header (RFC 7230's list is Connection/Keep-Alive/Proxy-*/TE/
# Trailers/Transfer-Encoding/Upgrade only). Stripping it meant the relay let
# httpx derive its own Host header from MAIN_APP_BASE ("openalgo-app", the
# dummy placeholder used for the UDS transport -- see MAIN_APP_BASE above)
# instead of forwarding the browser's real Host (algodev.co.in, correctly
# preserved by nginx's own proxy_set_header Host $host on the way in).
# Flask-WTF's CSRF protection compares the Referer header's origin against
# request.host -- with Host silently rewritten to "openalgo-app" but Referer
# still "https://algodev.co.in/...", every CSRF-protected relayed POST (e.g.
# /stop/<id>, /start/<id>) failed with "400 Bad Request: The referrer does
# not match the host." confirmed via httpx.Client().build_request(): an
# explicitly-set Host header IS honored over the URL's own host, so simply
# not stripping it here is sufficient -- no other change needed.


@app.route(
    "/<path:_unused>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
def _relay(_unused):
    """Forwards any request this process doesn't handle locally to the main
    app unchanged, and relays the response back verbatim. /python/api/events
    (SSE) is streamed chunk-by-chunk rather than buffered, since it's a
    long-lived connection."""
    target_url = f"{MAIN_APP_BASE}{request.full_path if request.query_string else request.path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}

    is_sse = request.path.rstrip("/").endswith("/api/events")

    if is_sse:
        def _stream():
            # A dedicated, one-off connection for this stream -- NOT the
            # pooled _relay_client used for ordinary buffered requests below.
            # A long-lived SSE stream sharing a connection pool with regular
            # request/response traffic risks head-of-line blocking / delayed
            # flushing on that pool; isolating it removes that as a variable.
            # Must be a real httpx.Client (not the httpx.stream() module-level
            # convenience function) because only Client's constructor accepts
            # `transport=` -- needed to reach the main app over its Unix
            # socket in production. A FRESH transport from
            # _make_relay_transport() every call -- never the pooled
            # _relay_client's own transport instance, see that function's
            # docstring for why sharing one broke buffered relay requests.
            # HTTP/1.1 by default (h2 requires the optional `h2` package AND
            # explicit http2=True; neither applies to this call).
            with httpx.Client(transport=_make_relay_transport(), timeout=None) as _stream_client:
                with _stream_client.stream(
                    request.method, target_url, headers=headers, content=request.get_data(),
                ) as upstream:
                    for chunk in upstream.iter_raw():
                        yield chunk

        response = Response(stream_with_context(_stream()), mimetype="text/event-stream")
        # Belt-and-suspenders against buffering at every layer between the
        # main app's SSE generator and the browser: nginx's own
        # proxy_buffering is already off for the /python location block,
        # but X-Accel-Buffering covers it explicitly too in case that
        # config ever drifts. Deliberately NOT setting direct_passthrough --
        # that flag is for wrapping a real file-like object (send_file's use
        # case), not a generator; setting it here broke werkzeug's dev
        # server outright ("applications must write bytes"), caught by a
        # synthetic latency test before this shipped.
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Cache-Control"] = "no-cache"
        return response

    upstream = _relay_client.request(
        request.method, target_url, headers=headers, content=request.get_data(),
    )
    response_headers = [
        (k, v) for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    ]
    return Response(upstream.content, status=upstream.status_code, headers=response_headers)


def main():
    init_db()
    host = os.getenv("STRATEGY_REPORTING_HOST", "127.0.0.1")
    logger.info(f"strategy_reporting server starting on {host}:{STRATEGY_REPORTING_PORT}")

    # TCP loopback only -- no Unix socket. werkzeug's run_simple has no clean
    # way to bind a pre-created AF_UNIX socket, and TCP-on-127.0.0.1 is
    # already sufficient for this process's traffic: the strategy scripts'
    # own loopback calls and nginx's reverse-proxy hop are both local,
    # same-host connections either way.
    from werkzeug.serving import run_simple

    run_simple(hostname=host, port=STRATEGY_REPORTING_PORT, application=app, threaded=True)


if __name__ == "__main__":
    main()
