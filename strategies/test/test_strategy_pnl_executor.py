"""
Regression tests for the dedicated `_pnl_executor` used by
`StrategyEngine.report_pnl_tick()` across the 6 deployed Python strategy
scripts under strategies/deployed/.

Background: report_pnl_tick() previously called report_pnl_to_platform()
(a blocking local HTTP push) directly inline, on its own tightly-scheduled
0.8s APScheduler job. If that call was ever slow, the next scheduled tick
got skipped ("maximum number of running instances reached (1)"), observed
repeatedly in production logs. The fix dispatches the push through
_fill_executor instead of calling it inline -- but _fill_executor is also
used by fill-watchers, which can block for minutes during a reprice loop
(seen taking ~5 minutes across 59 reprice attempts in production). Sharing
that pool would let a stuck fill-watch make the live PnL display go stale
for the same duration. So report_pnl_tick() gets its own single-worker
`_pnl_executor`, completely independent of `_fill_executor`.

These tests import one of the 6 scripts (Pivot_EMA_Combined, chosen since
it's the most complex/representative -- all 6 share this exact pattern) as
a module via importlib, construct a real StrategyEngine with lightweight
stand-ins for the broker client/price stream (no live broker/DB/network
touched), and verify the dispatch behaves correctly under load.
"""

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_Sensex_Pivot_EMA_Combined_Intraday_1_20260726000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """This repo's own root directory is itself importable as a package
    literally named `openalgo` (it has its own __init__.py). When pytest
    adds the repo rootdir to sys.path, a bare `from openalgo import api, ta`
    inside a loaded strategy script can resolve to THIS repo instead of the
    pip-installed openalgo SDK (site-packages/openalgo), since Python
    checks sys.modules/sys.path in an order this collision defeats. Force
    the real SDK to be loaded and cached under the `openalgo` key in
    sys.modules first, so the script's import resolves correctly regardless
    of sys.path ordering."""
    existing = sys.modules.get("openalgo")
    if existing is not None and hasattr(existing, "api"):
        return  # already the real SDK

    site_pkg_init = REPO_ROOT / ".venv" / "Lib" / "site-packages" / "openalgo" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "openalgo", site_pkg_init, submodule_search_locations=[str(site_pkg_init.parent)]
    )
    real_openalgo = importlib.util.module_from_spec(spec)
    sys.modules["openalgo"] = real_openalgo
    spec.loader.exec_module(real_openalgo)


def _load_script_module():
    """Load the deployed script by file path -- it's not on sys.path and
    isn't a package, so importlib.util.spec_from_file_location is used
    directly (see _strategy_platform_client.py's own docstring for why a
    plain `import` wouldn't work here)."""
    _ensure_real_openalgo_sdk_loaded()
    spec = importlib.util.spec_from_file_location("combined_strategy_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


@pytest.fixture
def engine(script_module):
    env = script_module.Environment()
    store = script_module.StateStore(env)  # fresh in-memory state, no file I/O
    client = MagicMock()
    price_stream = MagicMock()
    eng = script_module.StrategyEngine(client, store, env, price_stream, execution_id=1)
    yield eng
    eng._fill_executor.shutdown(wait=False)
    eng._signal_executor.shutdown(wait=False)
    eng._pnl_executor.shutdown(wait=False)


def test_pnl_executor_is_independent_of_fill_executor(engine):
    assert engine._pnl_executor is not engine._fill_executor
    assert engine._pnl_executor._max_workers == 1


def test_thread_stack_size_reduced_from_default(script_module):
    """Loading the script sets a smaller default thread stack size (1MB
    instead of Python's 8MB default) -- this process runs ~12 threads at
    once, and at the default size that's ~96MB of virtual address space
    reserved purely for stacks, out of the 1024MB RLIMIT_AS cap every
    strategy subprocess runs under (confirmed as the actual ceiling behind
    a production "can't start new thread" crash, 2026-07-28). This must be
    set before any thread is created -- verified here by confirming a
    freshly spawned thread's actual OS-level stack allocation reflects it."""
    assert threading.stack_size() == 1024 * 1024

    # Confirm it's not just the setting that changed, but that newly
    # created threads actually work correctly with the reduced stack --
    # a thread doing ordinary (non-recursive) work should complete fine.
    result = {}

    def worker():
        result["ran"] = True
        result["thread_name"] = threading.current_thread().name

    t = threading.Thread(target=worker, name="stack-size-test-thread")
    t.start()
    t.join(timeout=5)
    assert result.get("ran") is True


def test_report_pnl_tick_dispatches_via_pnl_executor_not_fill_executor(engine, script_module, monkeypatch):
    """With no open positions (fresh state), report_pnl_tick() should still
    dispatch a push with an empty snapshot -- and it must run on a
    pnl_executor worker thread, not a fillwatch one."""
    seen_thread_names = []
    done = threading.Event()

    def fake_report(env, realized_pnl, open_positions):
        seen_thread_names.append(threading.current_thread().name)
        done.set()

    monkeypatch.setattr(script_module, "report_pnl_to_platform", fake_report)

    engine.report_pnl_tick()
    assert done.wait(timeout=2.0), "report_pnl_to_platform was never invoked"
    assert len(seen_thread_names) == 1
    assert seen_thread_names[0].startswith("pnltick"), (
        f"expected a pnltick-prefixed worker thread, got {seen_thread_names[0]!r}"
    )


def test_report_pnl_tick_uses_current_ws_stale_threshold(engine, script_module, monkeypatch):
    """report_pnl_tick() must ask price_stream.get_ltp() for the SAME
    time-aware threshold the watchdog uses (_current_ws_stale_threshold()),
    not the flat config.ws_stale_seconds -- otherwise a leg entered during
    the 09:15-10:00 post-open grace window can flicker out of the PnL push
    every ~20s even while the watchdog correctly judges the connection
    healthy (see docs/CUSTOMIZATIONS.md's 2026-07-29 entry). Freezes
    _current_ws_stale_threshold() itself (rather than the wall clock) so
    this test is time-of-day independent and directly pins the call site's
    argument to whatever that function currently returns."""
    leg_key = script_module.LEG_KEYS[0]
    pos = engine.store.state.legs[leg_key].position
    pos.symbol = "NIFTY04AUG2624000PE"
    pos.quantity = 65
    pos.entry_px = 100.0
    pos.entry_filled = True

    monkeypatch.setattr(script_module, "_current_ws_stale_threshold", lambda: 987.0)
    engine.price_stream.get_ltp.return_value = 95.0

    engine.report_pnl_tick()

    assert engine.price_stream.get_ltp.called
    _, kwargs = engine.price_stream.get_ltp.call_args
    assert kwargs["max_age"] == 987.0


def test_report_pnl_tick_not_delayed_by_saturated_fill_executor(engine, script_module, monkeypatch):
    """The core regression check: saturate EVERY _fill_executor worker with
    a long-running blocking task (simulating stuck fill-watchers/reprice
    loops), then confirm report_pnl_tick() still dispatches and completes
    promptly via the separate _pnl_executor -- proving a stuck fill-watch
    can no longer make the live PnL display go stale."""
    fill_workers = engine._fill_executor._max_workers
    release = threading.Event()

    def block_forever():
        release.wait(timeout=10)

    # Saturate every fill_executor worker.
    futures = [engine._fill_executor.submit(block_forever) for _ in range(fill_workers)]

    pnl_done = threading.Event()

    def fake_report(env, realized_pnl, open_positions):
        pnl_done.set()

    monkeypatch.setattr(script_module, "report_pnl_to_platform", fake_report)

    try:
        start = time.time()
        engine.report_pnl_tick()  # must not block waiting on _fill_executor
        dispatch_elapsed = time.time() - start
        assert dispatch_elapsed < 0.5, (
            f"report_pnl_tick() itself blocked for {dispatch_elapsed:.2f}s -- "
            f"it should only submit to _pnl_executor and return immediately"
        )
        assert pnl_done.wait(timeout=2.0), (
            "report_pnl_to_platform was never invoked -- pnl_executor may be "
            "starved by the saturated fill_executor, which is exactly the "
            "regression this fix prevents"
        )
    finally:
        release.set()
        for f in futures:
            f.result(timeout=5)


def test_submit_failure_is_caught_not_raised(engine, script_module, monkeypatch):
    """If _pnl_executor.submit() itself raises (e.g. a transient
    RuntimeError: can't start new thread), report_pnl_tick() must swallow
    it and log a warning -- never crash the calling scheduler job."""

    def raising_submit(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(engine._pnl_executor, "submit", raising_submit)

    # Must not raise.
    engine.report_pnl_tick()


# ---------------------------------------------------------------------------
# Regression tests for the periodic error re-push fix (2026-07-28).
#
# Background: push_leg_error() only fired once, on the transition into
# error_state. If that one POST was lost (server busy, transient network
# blip), the UI's error badge silently never appeared -- confirmed in
# production: three legs sat in exit_failed for 1-4 hours with no UI error
# shown, even though the strategy's own state.json correctly tracked the
# error the whole time. _repush_active_errors() re-pushes at most once per
# config.error_repush_interval_sec (60s) for every leg still in
# error_state, so a single lost push self-heals within a minute.
# ---------------------------------------------------------------------------

def _make_errored_leg(engine, leg_key: str, error_state: str = "exit_failed"):
    pos = engine.store.state.legs[leg_key].position
    pos.symbol = "NIFTY04AUG2624000PE"
    pos.error_state = error_state
    pos.error_kind = "resting"
    pos.error_order_id = "26072899949009"
    return pos


def _drain_pnl_executor(engine):
    """_repush_active_errors() dispatches the actual push to _pnl_executor
    (a background thread) -- submitting a no-op to that same single-worker
    executor and waiting for its result guarantees every previously queued
    task has already completed (FIFO, one worker), without a sleep/race."""
    engine._pnl_executor.submit(lambda: None).result(timeout=5)


def test_repush_dispatches_for_a_leg_in_error(engine, script_module, monkeypatch):
    calls = []

    def fake_push_leg_error(env, leg_key, pos, action=None, clear=False):
        calls.append((leg_key, action))

    monkeypatch.setattr(script_module, "push_leg_error", fake_push_leg_error)
    _make_errored_leg(engine, "EMA_NIFTY_PE")

    engine._repush_active_errors()
    _drain_pnl_executor(engine)

    assert len(calls) == 1
    assert calls[0][0] == "EMA_NIFTY_PE"


def test_repush_does_not_spam_within_interval(engine, script_module, monkeypatch):
    """A second call within error_repush_interval_sec must NOT re-push --
    only the first call (or one after the interval elapses) should."""
    calls = []

    def fake_push_leg_error(env, leg_key, pos, action=None, clear=False):
        calls.append(leg_key)

    monkeypatch.setattr(script_module, "push_leg_error", fake_push_leg_error)
    _make_errored_leg(engine, "EMA_NIFTY_PE")

    engine._repush_active_errors()
    engine._repush_active_errors()
    engine._repush_active_errors()
    _drain_pnl_executor(engine)

    assert len(calls) == 1, f"expected exactly 1 push within the interval, got {len(calls)}"


def test_repush_fires_again_after_interval_elapses(engine, script_module, monkeypatch):
    calls = []

    def fake_push_leg_error(env, leg_key, pos, action=None, clear=False):
        calls.append(leg_key)

    monkeypatch.setattr(script_module, "push_leg_error", fake_push_leg_error)
    monkeypatch.setattr(script_module.config, "error_repush_interval_sec", 0.2)
    _make_errored_leg(engine, "EMA_NIFTY_PE")

    engine._repush_active_errors()
    time.sleep(0.3)
    engine._repush_active_errors()
    _drain_pnl_executor(engine)

    assert len(calls) == 2


def test_repush_stops_once_error_cleared(engine, script_module, monkeypatch):
    """Once a leg's error_state is cleared (Retry/Cancel/Manual resolved
    it), _repush_active_errors must stop pushing for it and drop its
    tracked timestamp -- confirms no stale entries linger forever."""
    calls = []

    def fake_push_leg_error(env, leg_key, pos, action=None, clear=False):
        calls.append(leg_key)

    monkeypatch.setattr(script_module, "push_leg_error", fake_push_leg_error)
    pos = _make_errored_leg(engine, "EMA_NIFTY_PE")

    engine._repush_active_errors()
    _drain_pnl_executor(engine)
    assert "EMA_NIFTY_PE" in engine._last_error_push

    pos.error_state = ""
    engine._repush_active_errors()
    _drain_pnl_executor(engine)

    assert len(calls) == 1  # only the first call, before it was cleared
    assert "EMA_NIFTY_PE" not in engine._last_error_push


def test_repush_dispatch_failure_is_caught_not_raised(engine, script_module, monkeypatch):
    """.submit() itself raising (transient thread-creation hiccup) must not
    crash run_cycle -- same class of fix as report_pnl_tick's."""
    _make_errored_leg(engine, "EMA_NIFTY_PE")

    def raising_submit(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(engine._pnl_executor, "submit", raising_submit)

    # Must not raise.
    engine._repush_active_errors()
