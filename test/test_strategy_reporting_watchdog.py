"""Tests for strategy_reporting/app_integration.py's watchdog (added
2026-09-03): nginx routes the WHOLE /python prefix through the
strategy_reporting subprocess (see strategy_reporting/server.py's own
docstring), so if it dies, every /python/* route 502s until something
respawns it. Before this fix, nothing did -- confirmed live on
2026-09-02 and 2026-09-03, both times requiring a full box restart to
recover. These tests exercise the watchdog loop directly (not through a
real subprocess) with a fake Popen-like object so they run fast and
deterministic.
"""

import importlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture()
def ai_module(monkeypatch):
    """Fresh import with a very short check interval so the watchdog loop
    ticks fast enough for tests to observe within a couple hundred ms,
    without needing to touch the real subprocess-spawning code path."""
    monkeypatch.setenv("STRATEGY_REPORTING_WATCHDOG_INTERVAL_SEC", "0")
    import strategy_reporting.app_integration as ai

    importlib.reload(ai)
    # 0s check interval would busy-loop; give the loop itself a tiny sleep
    # via the module constant instead, post-reload, so tests stay fast but
    # don't spin the CPU during the (short) time they run.
    ai._WATCHDOG_CHECK_INTERVAL_SEC = 0.05
    ai._WATCHDOG_MIN_BACKOFF_SEC = 0.05
    ai._WATCHDOG_MAX_BACKOFF_SEC = 0.2
    yield ai
    ai._watchdog_running = False
    if ai._watchdog_thread is not None:
        ai._watchdog_thread.join(timeout=2)
    ai._strategy_reporting_subprocess = None
    ai._strategy_reporting_started = False


class _FakeProc:
    """Minimal stand-in for subprocess.Popen -- poll() returns None while
    "alive", an int once "dead"."""

    def __init__(self, pid=12345):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def die(self, code=1):
        self.returncode = code

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _run_watchdog_briefly(ai_module, seconds=0.3):
    import threading

    ai_module._watchdog_running = True
    t = threading.Thread(target=ai_module._watchdog_loop, daemon=True)
    ai_module._watchdog_thread = t
    t.start()
    time.sleep(seconds)


def test_respawns_when_subprocess_dies(ai_module, monkeypatch):
    proc = _FakeProc()
    ai_module._strategy_reporting_subprocess = proc

    respawn_calls = []
    monkeypatch.setattr(
        ai_module,
        "_spawn_strategy_reporting_subprocess",
        lambda: respawn_calls.append(True),
    )

    proc.die(code=1)
    _run_watchdog_briefly(ai_module, seconds=0.6)
    ai_module._watchdog_running = False

    assert respawn_calls, "watchdog did not respawn a dead subprocess"


def test_does_not_respawn_while_subprocess_stays_alive(ai_module, monkeypatch):
    proc = _FakeProc()
    ai_module._strategy_reporting_subprocess = proc  # never dies in this test

    respawn_calls = []
    monkeypatch.setattr(
        ai_module,
        "_spawn_strategy_reporting_subprocess",
        lambda: respawn_calls.append(True),
    )

    _run_watchdog_briefly(ai_module, seconds=0.3)
    ai_module._watchdog_running = False

    assert respawn_calls == [], "watchdog respawned a subprocess that never died"


def test_stops_immediately_on_intentional_shutdown(ai_module, monkeypatch):
    """_terminate_strategy_reporting_subprocess must clear _watchdog_running
    FIRST -- an intentional shutdown must never be mistaken for a crash and
    trigger a respawn."""
    proc = _FakeProc()
    ai_module._strategy_reporting_subprocess = proc

    respawn_calls = []
    monkeypatch.setattr(
        ai_module,
        "_spawn_strategy_reporting_subprocess",
        lambda: respawn_calls.append(True),
    )

    import threading

    ai_module._watchdog_running = True
    t = threading.Thread(target=ai_module._watchdog_loop, daemon=True)
    ai_module._watchdog_thread = t
    t.start()

    # Simulate the process exiting as part of an intentional shutdown --
    # exactly what happens when _terminate_strategy_reporting_subprocess's
    # own terminate()/wait() sequence runs.
    proc.terminate()
    ai_module._terminate_strategy_reporting_subprocess()

    t.join(timeout=2)

    assert respawn_calls == [], (
        "watchdog respawned the subprocess during an intentional shutdown -- "
        "_watchdog_running must be cleared before terminate() runs"
    )
    assert ai_module._watchdog_running is False


def test_backoff_grows_and_is_capped(ai_module, monkeypatch):
    """Repeated deaths must back off (not hammer respawn every check
    interval), and the backoff must never exceed _WATCHDOG_MAX_BACKOFF_SEC."""
    proc = _FakeProc()
    ai_module._strategy_reporting_subprocess = proc

    respawn_times = []

    def _fake_spawn():
        respawn_times.append(time.monotonic())
        # New process starts, but immediately marked dead again so the
        # watchdog keeps backing off across several cycles within this
        # test's short window.
        proc.returncode = None
        proc.die(code=1)

    monkeypatch.setattr(ai_module, "_spawn_strategy_reporting_subprocess", _fake_spawn)

    proc.die(code=1)
    _run_watchdog_briefly(ai_module, seconds=0.6)
    ai_module._watchdog_running = False

    assert len(respawn_times) >= 2, "expected multiple respawn attempts across repeated deaths"
    gaps = [b - a for a, b in zip(respawn_times, respawn_times[1:], strict=False)]
    assert all(g <= ai_module._WATCHDOG_MAX_BACKOFF_SEC + 0.2 for g in gaps), (
        f"a backoff gap exceeded the configured cap: {gaps}"
    )
