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
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytz
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, session, stream_with_context

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
MAIN_APP_BASE = f"http://127.0.0.1:{FLASK_PORT}"

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
# shape/purpose than this purely-internal loopback relay.
_relay_client = httpx.Client(timeout=10.0)


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

    row = db_session.get(StrategyPnl, strategy_id)
    if row is None:
        row = StrategyPnl(strategy_id=strategy_id)
        db_session.add(row)
    row.realized_pnl = realized_pnl
    row.unrealized_pnl = unrealized_pnl
    row.open_positions = json.dumps(open_positions)
    db_session.commit()

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

    existing = (
        db_session.query(StrategyLegError)
        .filter_by(strategy_id=strategy_id, leg_key=leg_key)
        .first()
    )
    if data.get("clear"):
        if existing:
            db_session.delete(existing)
            db_session.commit()
    else:
        if existing is None:
            existing = StrategyLegError(strategy_id=strategy_id, leg_key=leg_key)
            db_session.add(existing)
        existing.error_state = data.get("error_state", "")
        existing.error_kind = data.get("error_kind", "")
        existing.error_message = data.get("error_message", "")
        existing.error_since = data.get("error_since", "")
        existing.symbol = data.get("symbol", "")
        existing.quantity = data.get("quantity", 0)
        existing.action = data.get("action", "")
        db_session.commit()

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

    db_session.query(StrategyPendingAction).filter_by(
        strategy_id=strategy_id, leg_key=leg_key
    ).delete()
    db_session.commit()
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

    row = (
        db_session.query(StrategyPendingAction)
        .filter_by(strategy_id=strategy_id, leg_key=leg_key)
        .first()
    )
    if row is None:
        row = StrategyPendingAction(strategy_id=strategy_id, leg_key=leg_key)
        db_session.add(row)
    row.action = action
    row.fill_price = fill_price
    db_session.commit()

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
    row = db_session.get(StrategyForceExit, strategy_id)
    if row is None:
        row = StrategyForceExit(strategy_id=strategy_id)
        db_session.add(row)
    row.status = "pending"
    db_session.commit()
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

    db_session.query(StrategyForceExit).filter_by(strategy_id=strategy_id).delete()
    db_session.commit()

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
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


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
            with _relay_client.stream(
                request.method, target_url, headers=headers, content=request.get_data(),
                timeout=None,
            ) as upstream:
                for chunk in upstream.iter_raw():
                    yield chunk

        return Response(stream_with_context(_stream()), mimetype="text/event-stream")

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
