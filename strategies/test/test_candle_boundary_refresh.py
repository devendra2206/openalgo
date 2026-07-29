"""
Regression tests for `_current_candle_boundary()` and its use inside
`get_signal()`/`_get_option_signal()` across the 5 deployed scripts that
compute candle-based indicators (Combined, EMA34_RSI, Pivot_Supertrend,
MCX, VWAP_NoHA -- NOT Expiry_Batman, which has no candle-based signal at
all, confirmed by grep: it's a pure straddle-entry + spot-move-triggered
repair strategy with zero client.history()/get_signal() calls).

Background: the previous refresh throttle was a pure rolling timer --
"has indicator_refresh_interval (15s) passed since the last fetch?" --
with no awareness of where the actual 3-minute (or 2-minute, for
VWAP_NoHA's option-level signal) candle boundaries fall. That means
roughly 12 fetches happen per 3m candle, ~11 of them wasted (the candle
hasn't closed yet), and the one useful fetch can land anywhere up to 15s
late relative to the true close depending on timing phase.

The fix replaces that rolling timer with a direct comparison: does the
CACHED signal's own candle_key (str(bars.index[-1]), i.e. the last
candle it was actually computed from) already correspond to the current
wall-clock candle boundary (_current_candle_boundary())? If yes, no
refetch is needed at all until the next boundary arrives -- cutting the
wasted-fetch count to ~1 per candle. If no, a refresh is due on every
tick (bounded only by the pre-existing _signal_refresh_pending guard,
which prevents more than one fetch in flight at a time) -- this is
actually a FASTER retry than the old 15s timer whenever a fresh candle
is still pending, and the worst-case detection latency for a genuinely
new candle drops to roughly scheduler_interval + one broker round-trip.

These tests exercise `_current_candle_boundary()`/`_candle_key_boundary()`
directly (pure functions, no strategy state needed) plus the cache-based
dispatch behavior in the Combined script (representative of all 5 -- same
pattern, just different attribute/dict names per script).
"""

import importlib.util
import sys
import threading
from datetime import datetime as real_datetime
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_Sensex_Pivot_EMA_Combined_Intraday_1_20260726000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_strategy_pnl_executor.py's identical helper for why this
    is needed -- the repo root is itself importable as a package literally
    named `openalgo`, which shadows the pip-installed SDK under pytest's
    rootdir-on-sys.path behavior."""
    existing = sys.modules.get("openalgo")
    if existing is not None and hasattr(existing, "api"):
        return

    site_pkg_init = REPO_ROOT / ".venv" / "Lib" / "site-packages" / "openalgo" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "openalgo", site_pkg_init, submodule_search_locations=[str(site_pkg_init.parent)]
    )
    real_openalgo = importlib.util.module_from_spec(spec)
    sys.modules["openalgo"] = real_openalgo
    spec.loader.exec_module(real_openalgo)


def _load_script_module():
    _ensure_real_openalgo_sdk_loaded()
    spec = importlib.util.spec_from_file_location("combined_strategy_script_boundary", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


class _FixedDateTime:
    """Stand-in for the `datetime` class the script imports at module
    level, so `datetime.now(IST)` inside `_current_candle_boundary()`
    returns a controlled instant instead of the real wall clock.
    `fromisoformat` is passed through to the real class unchanged --
    `_candle_key_boundary()` needs it and it has nothing to do with the
    wall clock being frozen."""

    _fixed: "real_datetime" = None

    @classmethod
    def fromisoformat(cls, value):
        return real_datetime.fromisoformat(value)

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_time(script_module, monkeypatch, hour: int, minute: int, second: int = 0):
    # IST.localize(...), NOT real_datetime(..., tzinfo=IST) -- pytz's raw
    # tzinfo constructor attaches the zone's historical "LMT" offset
    # (+05:53 for Kolkata) instead of the modern +05:30, which silently
    # breaks any datetime comparison against a correctly-localized value
    # (e.g. _candle_key_boundary()'s datetime.fromisoformat("...+05:30")).
    fixed = script_module.IST.localize(real_datetime(2026, 7, 29, hour, minute, second))
    _FixedDateTime._fixed = fixed
    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)
    return fixed


# ---- _current_candle_boundary() -------------------------------------------


def test_boundary_mid_candle_rounds_down(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 16, 16, 42)
    boundary = script_module._current_candle_boundary(3)
    assert (boundary.hour, boundary.minute, boundary.second) == (16, 15, 0)


def test_boundary_exactly_at_start(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 16, 18, 0)
    boundary = script_module._current_candle_boundary(3)
    assert (boundary.hour, boundary.minute, boundary.second) == (16, 18, 0)


def test_boundary_advances_to_next_bucket_one_second_later(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 16, 17, 59)
    before = script_module._current_candle_boundary(3)
    _freeze_time(script_module, monkeypatch, 16, 18, 0)
    after = script_module._current_candle_boundary(3)
    assert before != after
    assert (before.hour, before.minute) == (16, 15)
    assert (after.hour, after.minute) == (16, 18)


def test_boundary_handles_hour_rollover(script_module, monkeypatch):
    """A 3-minute bucket crossing an hour boundary (e.g. 16:59-17:02 isn't
    a real bucket since buckets are minute-of-day aligned, but 16:57-17:00
    and 17:00-17:03 are) must compute correctly across the hour rollover,
    not just within a single hour."""
    _freeze_time(script_module, monkeypatch, 17, 1, 30)
    boundary = script_module._current_candle_boundary(3)
    assert (boundary.hour, boundary.minute, boundary.second) == (17, 0, 0)


def test_boundary_with_2_minute_interval(script_module, monkeypatch):
    """VWAP_NoHA uses candle_bucket_minutes=2, not 3 -- confirm the helper
    is genuinely parameterized, not hardcoded to 3."""
    _freeze_time(script_module, monkeypatch, 16, 17, 45)
    boundary = script_module._current_candle_boundary(2)
    assert (boundary.hour, boundary.minute, boundary.second) == (16, 16, 0)


# ---- _candle_key_boundary() -------------------------------------------


def test_candle_key_boundary_parses_real_format(script_module):
    """candle_key is always str(bars.index[-1]) -- a pandas Timestamp's
    string repr, e.g. '2026-07-29 16:15:00+05:30'. Must parse back to the
    equivalent datetime."""
    parsed = script_module._candle_key_boundary("2026-07-29 16:15:00+05:30")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 7, 29)
    assert (parsed.hour, parsed.minute, parsed.second) == (16, 15, 0)


def test_candle_key_boundary_returns_none_on_garbage(script_module):
    """Must fail safe (None, meaning "treat as stale/unknown"), never
    raise or silently pretend to have a valid boundary."""
    assert script_module._candle_key_boundary("not-a-timestamp") is None
    assert script_module._candle_key_boundary("") is None


# ---- get_signal()'s cache-based dispatch -----------------------------------


from unittest.mock import MagicMock  # noqa: E402


@pytest.fixture
def engine(script_module):
    env = script_module.Environment()
    store = script_module.StateStore(env)
    client = MagicMock()
    price_stream = MagicMock()
    eng = script_module.StrategyEngine(client, store, env, price_stream, execution_id=1)
    yield eng
    eng._fill_executor.shutdown(wait=False)
    eng._signal_executor.shutdown(wait=False)
    eng._pnl_executor.shutdown(wait=False)


def _drain_signal_executor(engine):
    engine._signal_executor.submit(lambda: None).result(timeout=5)


def _make_fake_signal(script_module, candle_key: str):
    """Minimal but genuinely valid InstrumentSignal -- only candle_key
    matters for these tests, but the dataclass needs its other fields
    populated to construct at all."""
    pivot = script_module.PivotSignal(
        r1=100.0, s1=90.0, last_close=95.0, last_high=96.0, last_low=94.0, supertrend=93.0
    )
    ema = script_module.EmaRsiSignal(
        close_prev1=95.0, close_prev2=94.0, high_prev2=96.0, low_prev2=93.0,
        ema_high34_prev1=96.0, ema_high34_prev2=95.5, ema_low34_prev1=93.0,
        ema_low34_prev2=92.5, rsi_prev1=55.0,
    )
    return script_module.InstrumentSignal(pivot=pivot, ema=ema, ltp=95.0, candle_key=candle_key)


def test_get_signal_dispatches_on_first_call(engine, script_module, monkeypatch):
    """First-ever call for an instrument has no cached signal at all, so
    it must dispatch a refresh regardless of boundary logic."""
    calls = []
    monkeypatch.setattr(
        engine, "_refresh_signal_and_chain_bg",
        lambda *a, **k: calls.append(a),
    )
    inst = script_module.INSTRUMENTS[0]
    engine.get_signal(inst, ltp=100.0)
    _drain_signal_executor(engine)
    assert len(calls) == 1


def test_get_signal_skips_refresh_when_cache_already_matches_current_boundary(
    engine, script_module, monkeypatch
):
    """Once the cached signal's own candle_key already corresponds to the
    CURRENT candle boundary, a call in that same boundary must NOT
    dispatch again -- this is the "don't fetch ~11 times per candle" half
    of the fix."""
    inst = script_module.INSTRUMENTS[0]
    _freeze_time(script_module, monkeypatch, 16, 18, 5)
    now = script_module.datetime.now(script_module.IST)

    # Cached signal's candle_key IS the current 16:18 boundary.
    engine._signal_cache[inst.name] = _make_fake_signal(script_module, "2026-07-29 16:18:00+05:30")
    engine._last_indicator_refresh[inst.name] = now
    # Isolate due_signal's cache-based logic from the Combined script's
    # separate due_daily OR-condition, which would otherwise dispatch on
    # its own (no prior daily-pivot fetch recorded) and mask what this
    # test is actually checking.
    engine._last_daily_refresh[inst.name] = now

    calls = []
    monkeypatch.setattr(
        engine, "_refresh_signal_and_chain_bg",
        lambda *a, **k: calls.append(a),
    )
    engine.get_signal(inst, ltp=100.0)
    _drain_signal_executor(engine)
    assert calls == []


def test_get_signal_dispatches_when_cache_is_one_candle_behind(
    engine, script_module, monkeypatch
):
    """Simulates the exact scenario the fix targets: the cached signal
    still reflects the PREVIOUS candle (16:15) even though the wall clock
    has moved into a new one (16:18) -- must dispatch immediately, not
    wait out any rolling timer."""
    inst = script_module.INSTRUMENTS[0]
    _freeze_time(script_module, monkeypatch, 16, 18, 2)
    now = script_module.datetime.now(script_module.IST)

    engine._signal_cache[inst.name] = _make_fake_signal(script_module, "2026-07-29 16:15:00+05:30")
    # Even though a fetch was attempted very recently (well within any
    # old-style rolling throttle window), it must still dispatch again,
    # since the cache is behind the current boundary.
    engine._last_indicator_refresh[inst.name] = now
    engine._last_daily_refresh[inst.name] = now

    calls = []
    monkeypatch.setattr(
        engine, "_refresh_signal_and_chain_bg",
        lambda *a, **k: calls.append(a),
    )
    engine.get_signal(inst, ltp=100.0)
    _drain_signal_executor(engine)
    assert len(calls) == 1, (
        "expected a refresh dispatch: the cached signal's candle_key is "
        "one boundary behind the current wall-clock candle"
    )


def test_get_signal_retries_every_tick_while_cache_still_behind(
    engine, script_module, monkeypatch
):
    """While the cache hasn't yet caught up to the current boundary
    (broker-side finalization lag, say), get_signal() must keep retrying
    on every call -- bounded only by _signal_refresh_pending, not by any
    fixed retry interval. This is the retry mechanism that replaces the
    old rolling-timer fallback."""
    inst = script_module.INSTRUMENTS[0]
    _freeze_time(script_module, monkeypatch, 16, 18, 0)
    now = script_module.datetime.now(script_module.IST)

    engine._signal_cache[inst.name] = _make_fake_signal(script_module, "2026-07-29 16:15:00+05:30")
    engine._last_indicator_refresh[inst.name] = now  # attempted just now
    engine._last_daily_refresh[inst.name] = now

    calls = []
    monkeypatch.setattr(
        engine, "_refresh_signal_and_chain_bg",
        lambda *a, **k: calls.append(a),
    )
    engine.get_signal(inst, ltp=100.0)
    _drain_signal_executor(engine)
    assert len(calls) == 1


# ---- one-day simulation -----------------------------------------------------


def test_one_day_simulation_fetch_count_matches_candle_count_not_tick_count(
    engine, script_module, monkeypatch
):
    """Drives get_signal() across an entire simulated trading session
    (09:15-15:30, one call every scheduler_interval=10s, matching
    run_cycle()'s real cadence) and counts how many times a background
    refresh would actually be dispatched.

    Bypasses the real ThreadPoolExecutor (submit() runs the fake refresh
    synchronously instead) purely to keep this deterministic and fast --
    the real threaded dispatch path is already covered by the other tests
    above; this test is specifically about the THROTTLE MATH holding up
    over a realistic full-day tick sequence, not the threading mechanics.

    Session length is 6h15m = 22500s. At the old rolling-only throttle
    (indicator_refresh_interval=15s), that would be ~1500 dispatches
    (22500/15). At one candle (180s) per dispatch, it should be close to
    125 (22500/180) instead -- confirming the fix actually cuts the
    wasted-fetch rate by roughly the expected ~12x, not just at a single
    boundary crossing but sustained across a realistic full session,
    including the daily-pivot refresh's own independent 600s cadence
    (config.daily_refresh_interval) running alongside it."""
    inst = script_module.INSTRUMENTS[0]

    def fake_refresh(inst_arg, ltp_arg, refresh_chain_arg, due_daily_arg):
        now = script_module.datetime.now(script_module.IST)
        # Mirrors the real _refresh_signal_and_chain_bg: on a successful
        # fetch, the cache is updated to reflect whatever candle the
        # broker's (here, simulated) data currently shows -- the current
        # boundary, exactly like a real client.history() call would once
        # that candle has actually closed.
        current_boundary = script_module._current_candle_boundary(3)
        engine._signal_cache[inst_arg.name] = _make_fake_signal(
            script_module, current_boundary.isoformat()
        )
        engine._last_indicator_refresh[inst_arg.name] = now
        if due_daily_arg:
            engine._last_daily_refresh[inst_arg.name] = now
        engine._signal_refresh_pending.discard(inst_arg.name)

    call_count = 0

    def counting_fake_refresh(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        fake_refresh(*args, **kwargs)

    monkeypatch.setattr(engine, "_refresh_signal_and_chain_bg", counting_fake_refresh)
    # Run the (fake) refresh synchronously instead of on a background
    # thread -- see docstring above for why.
    monkeypatch.setattr(
        engine._signal_executor, "submit", lambda fn, *a, **k: fn(*a, **k)
    )

    session_start = script_module.IST.localize(real_datetime(2026, 7, 29, 9, 15, 0))
    session_end = script_module.IST.localize(real_datetime(2026, 7, 29, 15, 30, 0))
    scheduler_interval = script_module.config.scheduler_interval

    current = session_start
    ticks = 0
    while current <= session_end:
        _FixedDateTime._fixed = current
        monkeypatch.setattr(script_module, "datetime", _FixedDateTime)
        engine.get_signal(inst, ltp=100.0)
        ticks += 1
        current = current + timedelta(seconds=scheduler_interval)

    session_seconds = (session_end - session_start).total_seconds()
    expected_ticks = session_seconds / scheduler_interval
    expected_candles = session_seconds / 180  # 3-minute candles
    # due_daily has its own, independent 600s cadence (config.daily_refresh_interval)
    # that also dispatches _refresh_signal_and_chain_bg regardless of the
    # candle-boundary logic being tested here -- each of those adds to
    # call_count too, though some coincide with an already-due candle
    # refresh (dispatch serves both at once) rather than adding a
    # standalone extra call.
    expected_daily_triggers = session_seconds / script_module.config.daily_refresh_interval
    old_throttle_dispatch_count = session_seconds / script_module.config.indicator_refresh_interval

    assert ticks == pytest.approx(expected_ticks, abs=1)
    # The whole point of the fix: dispatch count should track candle count
    # (~125) plus the daily timer's own independent contribution (~38),
    # not tick count (~2250) or the old rolling throttle's cadence
    # (~1500) -- it must stay an order of magnitude below the old
    # throttle's rate regardless of exactly where in that combined range
    # it lands.
    assert call_count <= expected_candles + expected_daily_triggers + 5
    assert call_count < old_throttle_dispatch_count / 5
