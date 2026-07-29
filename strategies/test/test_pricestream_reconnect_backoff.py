"""
Regression tests for PriceStream._watchdog_loop()'s reconnect/backoff
redesign, applied to Combined, MCX, VWAP_NoHA, and Expiry_Batman
(EMA34_RSI/Pivot_Supertrend are standalone scripts not currently deployed
-- left untouched per explicit instruction).

Background: the OLD escalation rule was "ANY single tracked symbol stale
for ws_stale_reconnect_after (3) consecutive cycles -> full reconnect,"
shared across every symbol on the connection. Confirmed in production,
2026-07-29 (MCX): a single thinly-traded option leg stayed stale for an
extended stretch (genuine low liquidity, not a broken feed) while the
futures contract on the SAME connection ticked fine throughout. The old
rule eventually escalated anyway, tearing down and disrupting the
healthy futures stream for no possible benefit -- reconnecting cannot
make an illiquid contract start trading.

The fix, three parts:
1. Escalate to a full reconnect only when a MAJORITY of tracked symbols
   are simultaneously stuck past the streak limit -- a real signal the
   whole connection (not just one contract) is broken. A minority/single
   stale symbol never escalates on its own; it just keeps retrying via
   part 2 below.
2. Per-symbol backoff (_symbol_backoff_step/_symbol_next_retry_at):
   independent retry pacing per (symbol, exchange), so one chronically
   stale symbol's growing wait never slows down retries for a different
   symbol.
3. Even when a majority IS stuck, _confirm_genuinely_broken_via_rest()
   cross-checks via a REST quotes() call before actually reconnecting --
   if the REST price hasn't moved either, it's thin liquidity, not a
   broken feed, and the disruptive reconnect is skipped.

These tests exercise PriceStream directly (no StrategyEngine needed --
PriceStream is a self-contained class) using the Combined script,
representative of all 4 (same PriceStream implementation, just plumbed
through slightly different constructor signatures elsewhere in each
script -- unrelated to this logic).

Timing note: _watchdog_loop() calls datetime.now(IST) directly to
measure staleness. Since these tests run _watchdog_loop() synchronously
with _stop.wait() faked to return instantly (no real waiting), hundreds
or thousands of "cycles" would otherwise execute within milliseconds of
real wall-clock time -- nowhere near enough for any cached tick to
actually age past the staleness threshold. So the module's own datetime
is frozen and manually advanced by ws_watchdog_interval before each
cycle, mirroring the real cadence without any real delay.
"""

import importlib.util
import sys
import threading
from datetime import datetime as real_datetime
from datetime import timedelta
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
    spec = importlib.util.spec_from_file_location(
        "combined_strategy_script_reconnect", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


class _FixedDateTime:
    """Stand-in for the script's module-level `datetime` name -- lets
    _watchdog_loop()'s `datetime.now(IST)` calls see a manually-advanced
    clock instead of real wall-clock time. `fromisoformat` passes through
    to the real class unchanged (unused here, but kept for parity with
    the candle-boundary tests' identical helper in case any shared code
    path ever needs it)."""

    _current: "real_datetime" = None

    @classmethod
    def now(cls, tz=None):
        return cls._current

    @classmethod
    def fromisoformat(cls, value):
        return real_datetime.fromisoformat(value)


@pytest.fixture
def frozen_clock(script_module, monkeypatch):
    """Freezes script_module.datetime to a controllable instant, starting
    at 2026-07-29 11:00:00 IST -- deliberately OUTSIDE the 09:15-10:00
    post-open grace window (config.ws_post_open_grace_until) so these
    tests use the normal ws_stale_seconds (20s) threshold uniformly,
    without interacting with that separate widened-threshold feature
    (covered on its own in test_ws_stale_threshold.py). The two one-day
    simulation tests below deliberately override this back to 09:15:00
    to intentionally exercise that interaction too. Returns the
    _FixedDateTime class so tests can read/advance `._current` directly
    if needed."""
    start = script_module.IST.localize(real_datetime(2026, 7, 29, 11, 0, 0))
    _FixedDateTime._current = start
    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)
    return _FixedDateTime


def _quotes_response(ltp: float) -> dict:
    return {"status": "success", "data": {"ltp": ltp}}


def _make_price_stream(script_module, client, instruments):
    ps = script_module.PriceStream(client, instruments)
    ps.client = client
    return ps


def _run_watchdog_cycles(script_module, price_stream, n_cycles: int, before_cycle=None):
    """Runs PriceStream._watchdog_loop() exactly ONCE end-to-end (so its
    one-time initial connect happens exactly once, matching real
    behavior), with _stop.wait() patched to: (1) advance the frozen clock
    by ws_watchdog_interval seconds, (2) invoke before_cycle(i) -- letting
    the test inject fresh mock state (e.g. a simulated tick) for the
    upcoming cycle -- then (3) stop the loop after n_cycles iterations.
    No real threading, no real time delays -- fully synchronous and
    deterministic, but the SIMULATED clock genuinely advances each cycle
    so staleness math behaves like the real watchdog cadence."""
    state = {"count": 0}

    def fake_wait(timeout=None):
        state["count"] += 1
        if state["count"] > n_cycles:
            price_stream._stop.set()
        else:
            _FixedDateTime._current = _FixedDateTime._current + timedelta(
                seconds=script_module.config.ws_watchdog_interval
            )
            if before_cycle is not None:
                before_cycle(state["count"])
        return price_stream._stop.is_set()

    price_stream._stop.wait = fake_wait
    price_stream._watchdog_loop()


@pytest.fixture
def market_hours_always_open(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "_within_market_hours", lambda: True)


# ---- majority-vs-minority escalation ---------------------------------------


def test_single_chronically_stale_symbol_never_escalates(
    script_module, market_hours_always_open, frozen_clock
):
    """The exact production scenario: 2 tracked symbols, one ticks every
    cycle (healthy), the other never ticks at all (thin liquidity). Even
    after MANY cycles past the streak limit, escalation must never fire,
    since only 1 of 2 (not a majority) is stuck."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.quotes.return_value = _quotes_response(100.0)  # frozen -- no movement

    instruments = [
        {"symbol": "HEALTHY", "exchange": "NFO"},
        {"symbol": "THIN", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)
    ps._cache[("THIN", "NFO")] = (100.0, frozen_clock.now())

    def before_cycle(_i):
        # HEALTHY gets a fresh tick every single cycle; THIN never does.
        ps._cache[("HEALTHY", "NFO")] = (100.0, frozen_clock.now())

    _run_watchdog_cycles(script_module, ps, n_cycles=20, before_cycle=before_cycle)

    # Exactly one connect() call -- the initial one. No escalation-triggered
    # full reconnect ever happened despite THIN being stale the whole time.
    assert client.connect.call_count == 1
    assert client.disconnect.call_count == 0
    assert ps._stale_streak[("THIN", "NFO")] > 0


def test_majority_stale_with_real_price_movement_escalates(
    script_module, market_hours_always_open, frozen_clock
):
    """Both tracked symbols go stale simultaneously (a real connection-wide
    problem) AND REST confirms the price has genuinely moved since the
    last cached tick -- must escalate to a full reconnect."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    # REST shows a DIFFERENT (moved) price than what's cached -- confirms
    # the WS feed is genuinely failing to deliver, not just quiet.
    client.quotes.return_value = _quotes_response(999.0)

    instruments = [
        {"symbol": "SYM_A", "exchange": "NFO"},
        {"symbol": "SYM_B", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)
    ps._cache[("SYM_A", "NFO")] = (100.0, frozen_clock.now())
    ps._cache[("SYM_B", "NFO")] = (100.0, frozen_clock.now())

    _run_watchdog_cycles(script_module, ps, n_cycles=5)

    # Initial connect (1) + at least one escalation-triggered reconnect.
    assert client.connect.call_count >= 2
    assert client.disconnect.call_count >= 1


def test_majority_stale_but_rest_confirms_no_movement_does_not_escalate(
    script_module, market_hours_always_open, frozen_clock
):
    """Both symbols stale simultaneously, but REST ALSO shows the exact
    same frozen price for both -- not a broken feed, just no trades.
    Must NOT escalate, regardless of how many cycles pass."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.quotes.return_value = _quotes_response(100.0)  # matches cached -- no movement

    instruments = [
        {"symbol": "SYM_A", "exchange": "NFO"},
        {"symbol": "SYM_B", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)
    ps._cache[("SYM_A", "NFO")] = (100.0, frozen_clock.now())
    ps._cache[("SYM_B", "NFO")] = (100.0, frozen_clock.now())

    _run_watchdog_cycles(script_module, ps, n_cycles=10)

    assert client.connect.call_count == 1
    assert client.disconnect.call_count == 0


# ---- per-symbol backoff -----------------------------------------------------


def test_per_symbol_backoff_is_independent(
    script_module, market_hours_always_open, frozen_clock
):
    """5 symbols, 3 healthy + 2 chronically stale (2/5 is NOT a majority,
    so escalation never fires and backoff state survives): each stale
    symbol must accumulate its OWN backoff step independently, not share
    one counter."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.quotes.return_value = _quotes_response(100.0)

    instruments = [
        {"symbol": "HEALTHY_1", "exchange": "NFO"},
        {"symbol": "HEALTHY_2", "exchange": "NFO"},
        {"symbol": "HEALTHY_3", "exchange": "NFO"},
        {"symbol": "THIN_A", "exchange": "NFO"},
        {"symbol": "THIN_B", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)

    def before_cycle(_i):
        now = frozen_clock.now()
        ps._cache[("HEALTHY_1", "NFO")] = (100.0, now)
        ps._cache[("HEALTHY_2", "NFO")] = (100.0, now)
        ps._cache[("HEALTHY_3", "NFO")] = (100.0, now)

    _run_watchdog_cycles(script_module, ps, n_cycles=8, before_cycle=before_cycle)

    assert client.connect.call_count == 1, "2/5 stale must not escalate"
    assert ("THIN_A", "NFO") in ps._symbol_backoff_step
    assert ("THIN_B", "NFO") in ps._symbol_backoff_step
    assert ps._symbol_backoff_step[("THIN_A", "NFO")] > 0
    assert ps._symbol_backoff_step[("THIN_B", "NFO")] > 0


def test_backoff_resets_once_symbol_recovers(
    script_module, market_hours_always_open, frozen_clock
):
    """Once a stale symbol gets a fresh tick again, its backoff state must
    be cleared -- not left at whatever step it reached before recovering."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.quotes.return_value = _quotes_response(100.0)

    instruments = [{"symbol": "FLAPPY", "exchange": "NFO"}]
    ps = _make_price_stream(script_module, client, instruments)

    recovered_at_cycle = 5

    def before_cycle(i):
        if i >= recovered_at_cycle:
            ps._cache[("FLAPPY", "NFO")] = (100.0, frozen_clock.now())

    _run_watchdog_cycles(script_module, ps, n_cycles=8, before_cycle=before_cycle)

    assert ("FLAPPY", "NFO") not in ps._symbol_backoff_step
    assert ps._stale_streak[("FLAPPY", "NFO")] == 0


# ---- REST confirm helper directly -------------------------------------------


def test_confirm_genuinely_broken_true_when_price_moved(script_module):
    client = MagicMock()
    client.quotes.return_value = _quotes_response(150.0)
    ps = script_module.PriceStream(client, [])
    ps._cache[("SYM", "NFO")] = (100.0, real_datetime.now(script_module.IST))

    assert ps._confirm_genuinely_broken_via_rest([{"symbol": "SYM", "exchange": "NFO"}]) is True


def test_confirm_genuinely_broken_false_when_price_unchanged(script_module):
    client = MagicMock()
    client.quotes.return_value = _quotes_response(100.0)
    ps = script_module.PriceStream(client, [])
    ps._cache[("SYM", "NFO")] = (100.0, real_datetime.now(script_module.IST))

    assert ps._confirm_genuinely_broken_via_rest([{"symbol": "SYM", "exchange": "NFO"}]) is False


def test_confirm_genuinely_broken_skips_symbols_whose_rest_call_fails(script_module):
    """A REST call failing counts as 'can't confirm,' not as proof of
    brokenness -- must not crash and must not return True from a failure
    alone."""
    client = MagicMock()
    client.quotes.side_effect = Exception("timeout")
    ps = script_module.PriceStream(client, [])
    ps._cache[("SYM", "NFO")] = (100.0, real_datetime.now(script_module.IST))

    assert ps._confirm_genuinely_broken_via_rest([{"symbol": "SYM", "exchange": "NFO"}]) is False


# ---- one-day simulation -----------------------------------------------------


def test_one_day_simulation_thin_symbol_never_triggers_reconnect(
    script_module, market_hours_always_open, frozen_clock
):
    """Drives the watchdog across a full simulated session's worth of
    watchdog cycles (09:15-15:30 at ws_watchdog_interval=15s -> 1500
    cycles) with one symbol healthy (fresh tick every cycle) and one
    symbol permanently thin (never ticks, REST confirms frozen price the
    whole time). Confirms zero full reconnects across the ENTIRE
    simulated day -- the exact production scenario from 2026-07-29,
    proven not to recur under the new majority-based escalation rule.

    Deliberately starts the clock at the real session open (09:15:00,
    overriding frozen_clock's default 11:00:00 start) so this also
    exercises the interaction with the widened post-open staleness
    threshold (ws_stale_seconds_open, 09:15-10:00) from earlier the same
    day -- the thin symbol must still never escalate on its own even
    while that wider threshold is briefly in effect."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.quotes.return_value = _quotes_response(100.0)
    frozen_clock._current = script_module.IST.localize(real_datetime(2026, 7, 29, 9, 15, 0))

    instruments = [
        {"symbol": "NIFTY24AUGFUT", "exchange": "NFO"},
        {"symbol": "THIN_OPTION", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)
    ps._cache[("THIN_OPTION", "NFO")] = (100.0, frozen_clock.now())

    session_seconds = (15 * 3600 + 30 * 60) - (9 * 3600 + 15 * 60)  # 09:15-15:30
    n_cycles = session_seconds // script_module.config.ws_watchdog_interval  # ~1500

    def before_cycle(_i):
        ps._cache[("NIFTY24AUGFUT", "NFO")] = (100.0, frozen_clock.now())

    _run_watchdog_cycles(script_module, ps, n_cycles=n_cycles, before_cycle=before_cycle)

    assert client.connect.call_count == 1, (
        f"expected exactly 1 connect() call (the initial one) across the "
        f"whole simulated session, got {client.connect.call_count} -- a "
        f"chronically thin symbol must never trigger a full reconnect on "
        f"its own"
    )
    assert client.disconnect.call_count == 0
    # The thin symbol should still have had many per-symbol resubscribe
    # retries over the day, just none of them escalating.
    assert client.subscribe_ltp.call_count > 10
    assert ps._stale_streak[("THIN_OPTION", "NFO")] > 0
    assert ps._stale_streak[("NIFTY24AUGFUT", "NFO")] == 0


def test_one_day_simulation_genuine_outage_still_recovers(
    script_module, market_hours_always_open, frozen_clock
):
    """Sanity check on the flip side: if BOTH symbols genuinely go dark
    together partway through the day (a real connection-wide problem, not
    a liquidity quirk) and REST confirms real movement, the watchdog must
    still escalate to a full reconnect -- the new majority rule doesn't
    make the system blind to real outages, it just stops overreacting to
    isolated ones."""
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    # REST always shows a moved price relative to whatever's cached --
    # simulates a market that's genuinely still active while WS is stuck.
    client.quotes.return_value = _quotes_response(12345.0)

    instruments = [
        {"symbol": "SYM_A", "exchange": "NFO"},
        {"symbol": "SYM_B", "exchange": "NFO"},
    ]
    ps = _make_price_stream(script_module, client, instruments)

    outage_starts_at_cycle = 50

    def before_cycle(i):
        if i < outage_starts_at_cycle:
            now = frozen_clock.now()
            ps._cache[("SYM_A", "NFO")] = (100.0, now)
            ps._cache[("SYM_B", "NFO")] = (100.0, now)
        # From outage_starts_at_cycle onward, neither symbol ticks again.

    _run_watchdog_cycles(script_module, ps, n_cycles=100, before_cycle=before_cycle)

    assert client.connect.call_count >= 2, (
        "a genuine simultaneous outage across all tracked symbols must "
        "still escalate to a full reconnect"
    )
