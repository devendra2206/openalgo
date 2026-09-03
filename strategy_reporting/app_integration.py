"""
Spawns strategy_reporting/server.py as an independent child *process* from
the main Flask app's startup -- mirrors websocket_proxy/app_integration.py's
_spawn_websocket_subprocess() exactly (same reasoning: a genuinely separate
process, not a thread, so nothing about the main gunicorn+eventlet worker's
own blocking can ever affect it).
"""

import atexit
import os
import subprocess
import sys
import threading
import time

from utils.logging import get_logger

logger = get_logger(__name__)

_strategy_reporting_subprocess = None
_strategy_reporting_started = False

# Watchdog: nginx routes the WHOLE /python prefix to this subprocess (see
# strategy_reporting/server.py's own docstring), so if it dies, every
# /python/* route 502s until something respawns it -- and before this,
# nothing did. Confirmed live (2026-09-02, 2026-09-03): the /python page
# broke while the rest of the app stayed fine, and recovering required a
# full box restart each time, which the person on call could not always do
# immediately (mobile-only monitoring). A background thread here polls
# liveness and respawns on death, with backoff so a genuine crash-loop
# doesn't spin the CPU or the log.
_watchdog_thread = None
_watchdog_running = False
_WATCHDOG_CHECK_INTERVAL_SEC = int(os.getenv("STRATEGY_REPORTING_WATCHDOG_INTERVAL_SEC", "10"))
_WATCHDOG_MIN_BACKOFF_SEC = 5
_WATCHDOG_MAX_BACKOFF_SEC = 120


def _spawn_strategy_reporting_subprocess():
    global _strategy_reporting_subprocess

    if _strategy_reporting_subprocess is not None and _strategy_reporting_subprocess.poll() is None:
        logger.debug("strategy_reporting subprocess already running, skipping spawn")
        return

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-u", "-m", "strategy_reporting.server"]
    logger.debug(f"Spawning strategy_reporting subprocess: {' '.join(cmd)} (cwd={project_root})")

    try:
        # Inherit stdout/stderr so the child's logging lands in the same
        # systemd journal as gunicorn (it already logs via utils.logging,
        # so file/json log handlers fire too). Do NOT set
        # start_new_session=True -- staying in the gunicorn cgroup means
        # systemd reaps the child if gunicorn dies hard.
        _strategy_reporting_subprocess = subprocess.Popen(
            cmd, cwd=project_root, stdout=None, stderr=None,
        )
        logger.info(f"strategy_reporting subprocess started with PID {_strategy_reporting_subprocess.pid}")
    except Exception:
        logger.exception("Failed to spawn strategy_reporting subprocess")
        _strategy_reporting_subprocess = None
        return

    atexit.register(_terminate_strategy_reporting_subprocess)


def _watchdog_loop():
    """Background daemon thread: polls strategy_reporting's liveness and
    respawns it on death. Backoff (capped) on repeated deaths so a genuine
    crash-loop doesn't spin the CPU or flood the log -- any check that finds
    it alive resets the backoff, since a respawn that stays up isn't a
    crash-loop. Uses real threading.Thread + time.sleep -- both are fine
    under eventlet (time.sleep cooperates with the hub; this thread only
    ever calls Popen()/poll(), which are non-blocking/instant), so this
    does not need utils/real_threading."""
    backoff = _WATCHDOG_MIN_BACKOFF_SEC
    while _watchdog_running:
        time.sleep(_WATCHDOG_CHECK_INTERVAL_SEC)
        if not _watchdog_running:
            break

        proc = _strategy_reporting_subprocess
        if proc is None:
            continue
        if proc.poll() is None:
            backoff = _WATCHDOG_MIN_BACKOFF_SEC
            continue

        logger.error(
            f"strategy_reporting subprocess (PID {proc.pid}) died (exit code "
            f"{proc.returncode}) -- every /python/* route 502s until this "
            f"respawns (nginx routes the whole /python prefix here). "
            f"Restarting in {backoff}s."
        )
        time.sleep(backoff)
        if not _watchdog_running:
            break
        _spawn_strategy_reporting_subprocess()
        backoff = min(backoff * 2, _WATCHDOG_MAX_BACKOFF_SEC)


def _terminate_strategy_reporting_subprocess():
    global _strategy_reporting_subprocess, _watchdog_running
    # Stop the watchdog FIRST -- an intentional shutdown/terminate must
    # never be mistaken for a crash and respawned out from under it.
    _watchdog_running = False
    if _strategy_reporting_subprocess is None:
        return
    if _strategy_reporting_subprocess.poll() is not None:
        _strategy_reporting_subprocess = None
        return
    try:
        logger.info(f"Terminating strategy_reporting subprocess PID {_strategy_reporting_subprocess.pid}")
        _strategy_reporting_subprocess.terminate()
        try:
            _strategy_reporting_subprocess.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("strategy_reporting subprocess did not exit on SIGTERM, sending SIGKILL")
            _strategy_reporting_subprocess.kill()
            _strategy_reporting_subprocess.wait(timeout=5)
    except Exception:
        logger.warning("Error terminating strategy_reporting subprocess", exc_info=True)
    finally:
        _strategy_reporting_subprocess = None


def start_strategy_reporting_subprocess():
    """Call once from app.py at startup, next to start_websocket_proxy(app)."""
    global _strategy_reporting_started, _watchdog_thread, _watchdog_running
    if _strategy_reporting_started:
        logger.debug("strategy_reporting already started, skipping")
        return
    _strategy_reporting_started = True
    _spawn_strategy_reporting_subprocess()

    _watchdog_running = True
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="strategy-reporting-watchdog", daemon=True
    )
    _watchdog_thread.start()
    logger.info(
        f"strategy_reporting watchdog started (checks every "
        f"{_WATCHDOG_CHECK_INTERVAL_SEC}s, respawns on death with backoff up to "
        f"{_WATCHDOG_MAX_BACKOFF_SEC}s)"
    )
