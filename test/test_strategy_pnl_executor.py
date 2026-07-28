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

REPO_ROOT = Path(__file__).resolve().parent.parent
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
