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

from utils.logging import get_logger

logger = get_logger(__name__)

_strategy_reporting_subprocess = None
_strategy_reporting_started = False


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


def _terminate_strategy_reporting_subprocess():
    global _strategy_reporting_subprocess
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
    global _strategy_reporting_started
    if _strategy_reporting_started:
        logger.debug("strategy_reporting already started, skipping")
        return
    _strategy_reporting_started = True
    _spawn_strategy_reporting_subprocess()
