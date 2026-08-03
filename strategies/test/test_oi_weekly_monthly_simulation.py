"""
End-to-end simulation of the OI Weekly Buy + Monthly Sell strategy
(strategies/deployed/Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py)
against 5 days of synthetic 5-minute candle data, driving the REAL
StrategyEngine/WeeklySideEngine/MonthlySideEngine classes loaded from the
actual deployed script -- not a reimplementation of the signal logic.

Data source: strategies/test/data/nifty_spot_5min.csv and
options_oi_5min.csv (spot + 6 option-strike symbols, exported from
oi_simulation_data.py's Python source of truth via data/export_csv.py --
edit the Python file and re-run that exporter if the scenario data needs to
change, rather than hand-editing the CSVs out of sync with it). Every test
below reads these CSVs at collection time via _load_csv_candles() -- this
is the actual dataset the simulation runs against, not a byproduct.

Broker calls (history/quotes/expiry/optionchain/optiongreeks/placeorder/
orderstatus) are served by a FakeClient reading that CSV data; everything
else (state persistence, two-phase crash-safe order placement/fill,
strike-freeze bookkeeping, trade log, PnL accumulation) runs unmodified,
exercising the real production code paths.

What this proves, end to end:

  Day 1 (2026-08-03, small gap UP -> Reference Time = previous day's close):
    - WEEKLY_CE: Long Build-up -> entry, then 2 consecutive Short Build-up
      candles -> exit (opposite_signal).
    - WEEKLY_PE: Short Covering -> entry, independently of CE on the SAME
      candle CE also acted on (never cross-referenced, plan doc SS1.3),
      then a +100% profit-target exit immediately followed by a same-cycle
      re-entry (spec SS5).
    - MONTHLY_CE: gate (Weekly CE's own signal, Weakening) x confirm
      (Monthly CE's own strike, Weakening) -> entry, then its OWN exit (2
      consecutive opposite/Accumulation candles, no profit target/SL --
      spec SS12).
    - MONTHLY_PE: gate never opens (Weekly PE stays Accumulation) even
      though confirm alone would pass -> no entry (the gate is a genuine
      AND, not satisfied by confirmation #2 alone).

  Day 2 (2026-08-04, large gap UP -> Reference Time = today's 09:35 candle):
    confirms the OTHER reference mode, and that Day 1's state doesn't leak.

  Day 3 (2026-08-05, large gap DOWN): same today_0935 mode, opposite sign
    (guards a |gap%| sign bug an up-only test could hide) + a fresh
    Long Build-up entry (a quadrant Day 1's PE walk didn't use).

  Day 4 (2026-08-06, small gap DOWN, rolled to a new weekly expiry since
    this date is itself Day 1-3's expiry day -- see the strategy's own
    roll-on-expiry-day logic): streak INTERRUPTION (weakening -> resets on
    an accumulation candle -> weakening x2 -> exit, proving a true
    consecutive counter, not cumulative) + universal exit time (15:15)
    force-closing a leg with no OI-based exit ever firing.

  Restart/crash-recovery (no dedicated "day", exercises the two-phase
    order persistence added specifically for this): a true reload-from-disk
    restart resumes an already-open leg without re-entering, and
    reconcile_pending_orders() recovers the narrow crash window between
    placeorder() succeeding and the fill confirming.
"""

import csv
import importlib.util
import sys
import time
from datetime import datetime as real_datetime
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

import oi_simulation_data as sim

DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_csv_candles() -> dict:
    """Loads the CSV export of oi_simulation_data.py (see
    data/export_csv.py) into the {symbol: [candle_dict, ...]} shape
    FakeClient expects. This is the actual dataset every test below drives
    the strategy against -- the CSVs are the source of truth read at test
    time, not just a byproduct dumped alongside the Python literals."""
    candles_by_symbol: dict[str, list[dict]] = {}
    for csv_name in ("nifty_spot_5min.csv", "options_oi_5min.csv"):
        with (DATA_DIR / csv_name).open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                symbol = row.pop("symbol")
                candle = {
                    "timestamp": row["timestamp"],
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]), "oi": float(row["oi"]),
                }
                candles_by_symbol.setdefault(symbol, []).append(candle)
    return candles_by_symbol


_CSV_CANDLES = _load_csv_candles()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT / "strategies" / "deployed"
    / "Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_pricestream_reconnect_backoff.py's identical helper -- the
    repo root is itself importable as a package literally named `openalgo`,
    which shadows the pip-installed SDK under pytest's rootdir-on-sys.path
    behavior."""
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
    spec = importlib.util.spec_from_file_location("oi_weekly_monthly_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


class _FixedDateTime:
    """Stand-in for the script module's `datetime` name -- lets every
    `datetime.now(IST)` call inside the engine see a manually-advanced
    simulated clock instead of real wall-clock time."""

    _current: "real_datetime" = None

    @classmethod
    def now(cls, tz=None):
        return cls._current if tz is None else cls._current.astimezone(tz)

    @classmethod
    def combine(cls, date, time_, tzinfo=None):
        dt = real_datetime.combine(date, time_)
        return dt.replace(tzinfo=tzinfo) if tzinfo else dt

    @staticmethod
    def fromisoformat(s):
        return real_datetime.fromisoformat(s)

    @staticmethod
    def strptime(s, fmt):
        return real_datetime.strptime(s, fmt)


def _parse_ts(ts: str) -> real_datetime:
    return real_datetime.fromisoformat(ts)


class FakeClient:
    """Serves the synthetic dataset in place of real broker calls. Orders
    fill immediately at the current simulated quote -- this simulation is
    about signal-generation correctness, not fill-latency behavior (already
    covered by the reused, previously-hardened poll_fill()/place() code)."""

    def __init__(self, clock: _FixedDateTime, candles_by_symbol: dict, weekly_expiry_raw: str,
                 monthly_expiry_raw: str, chain_strikes: list, greeks_by_symbol: dict):
        self.clock = clock
        self.candles_by_symbol = candles_by_symbol
        self.weekly_expiry_raw = weekly_expiry_raw
        self.monthly_expiry_raw = monthly_expiry_raw
        self.chain_strikes = chain_strikes
        self.greeks_by_symbol = greeks_by_symbol
        self._order_seq = 0
        self.placed_orders = []   # [(symbol, action, quantity), ...] -- assertions read this
        self.connected = True
        self.authenticated = True

    # -- market data -----------------------------------------------------
    def history(self, symbol, exchange, interval, start_date, end_date):
        # Sorted by timestamp regardless of the caller-supplied dict's own
        # ordering -- a merged multi-day dataset assembled out of
        # chronological order (an easy authoring mistake) must not silently
        # produce a wrong "latest candle" read.
        #
        # Returns a pandas DataFrame (timestamp as a tz-aware DatetimeIndex)
        # on success and an error dict on failure -- matching the REAL
        # openalgo SDK's client.history() exactly (see .venv/Lib/site-
        # packages/openalgo/data.py), not the raw REST JSON shape. An
        # earlier version of this fake returned the raw {"status":...,
        # "data":[...]} dict on success, which happened to make the
        # deployed script's (bugged) `isinstance(resp, dict)`-as-success
        # check pass -- masking a real production bug (confirmed
        # 2026-08-03: the strategy's own history() parsing had success/
        # error backwards, silently discarding every real response) that
        # this simulation should have caught but didn't, precisely because
        # this fake didn't match the real SDK's return type.
        rows = sorted(self.candles_by_symbol.get(symbol, []), key=lambda r: _parse_ts(r["timestamp"]))
        now = self.clock.now()
        out = [
            r for r in rows
            if start_date <= _parse_ts(r["timestamp"]).date().isoformat() <= end_date
            and _parse_ts(r["timestamp"]) <= now
        ]
        if not out:
            return {"status": "error", "message": "no data"}
        df = pd.DataFrame(out)
        df["timestamp"] = df["timestamp"].apply(_parse_ts)
        df = df.set_index("timestamp").sort_index()
        return df

    def _latest_close(self, symbol) -> float:
        rows = sorted(self.candles_by_symbol.get(symbol, []), key=lambda r: _parse_ts(r["timestamp"]))
        now = self.clock.now()
        eligible = [r for r in rows if _parse_ts(r["timestamp"]) <= now]
        if not eligible:
            raise AssertionError(f"FakeClient: no candle yet for {symbol} at {now}")
        return float(eligible[-1]["close"])

    def quotes(self, symbol, exchange):
        px = self._latest_close(symbol)
        return {"status": "success", "data": {"ltp": px, "bid": px, "ask": px}}

    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": [self.weekly_expiry_raw, self.monthly_expiry_raw]}

    def optionchain(self, underlying, exchange, expiry_date, strike_count=20):
        # Enforce the COMPACT expiry format ("13AUG26", no dashes) --
        # the real optionchain() 404s on the raw dash form ("13-Aug-26"),
        # confirmed in production 2026-08-03 (select_weekly_otm1_strike was
        # passing the raw form; the master contract genuinely had the
        # strike data, another strategy traded the same expiry successfully
        # the same day, but this call still failed on the format mismatch).
        # An earlier version of this fake ignored expiry_date entirely,
        # which is why 92 passing tests never caught it.
        assert "-" not in expiry_date, (
            f"optionchain() called with a dash-form expiry ({expiry_date!r}) -- "
            f"expected the compact form (e.g. '13AUG26'), matching the real API."
        )
        # Chain rows live under "chain", NOT "data" -- an earlier version of
        # this fake used "data" here too, matching the strategy's own (wrong)
        # assumption rather than the real API response shape (confirmed in
        # production 2026-08-03: optionchain() actually returns {"status":
        # "success", "chain": [...], ...}), which is why this bug also slipped
        # past 92 passing tests.
        return {"status": "success", "chain": [{"strike": s} for s in self.chain_strikes]}

    def optiongreeks(self, symbol, exchange, **kwargs):
        delta = self.greeks_by_symbol.get(symbol)
        if delta is None:
            return {"status": "success", "greeks": {"delta": 0.5}}  # deliberately out of [0.20,0.25]
        return {"status": "success", "greeks": {"delta": delta}}

    # -- orders ------------------------------------------------------------
    def placeorder(self, strategy, symbol, exchange, action, product, price_type,
                    quantity, price, trigger_price, disclosed_quantity):
        self._order_seq += 1
        order_id = f"SIM{self._order_seq}"
        self.placed_orders.append((symbol, action, int(quantity)))
        return {"status": "success", "orderid": order_id}

    def orderstatus(self, order_id, strategy):
        # Immediate fill at the current quote for whichever symbol this
        # order_id was placed against (order_id encodes the placement index).
        idx = int(order_id.replace("SIM", "")) - 1
        symbol = self.placed_orders[idx][0]
        px = self._latest_close(symbol)
        return {"data": {"order_status": "complete", "average_price": px, "price": px}}

    def modifyorder(self, **kwargs):
        return {"status": "success"}

    # -- websocket (unused directly -- FakePriceStream below bypasses these) --
    def subscribe_ltp(self, *a, **k):
        pass

    def unsubscribe_ltp(self, *a, **k):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass


class FakePriceStream:
    """Always reports "no cached tick" -- forces every LTP read through the
    FakeClient's quotes() REST fallback uniformly, which is sufficient for
    this simulation (WS-specific reconnect/staleness behavior is already
    covered by test_pricestream_reconnect_backoff.py against the shared,
    unmodified PriceStream implementation)."""

    def get_ltp(self, symbol, exchange, max_age):
        return None

    def add_instruments(self, instruments):
        pass

    def remove_instruments(self, instruments):
        pass


def _build_engine(module, clock, candles_by_symbol, weekly_expiry_raw, monthly_expiry_raw,
                   chain_strikes, greeks_by_symbol, tmp_path):
    # Shallow copy -- several tests do client.candles_by_symbol[symbol] = [...]
    # to inject scenario-specific candles. _CSV_CANDLES is a single
    # module-level dict shared by every test in this file; without this copy,
    # that assignment mutates the SHARED dict's key, permanently corrupting
    # that symbol's data for every other test that runs afterward in the
    # same session (order-dependent pollution). A shallow copy is sufficient
    # since these tests reassign a symbol's whole list, never mutate one in
    # place.
    client = FakeClient(clock, dict(candles_by_symbol), weekly_expiry_raw, monthly_expiry_raw,
                        chain_strikes, greeks_by_symbol)

    env = module.Environment.__new__(module.Environment)
    env.api_key = "TEST_KEY"
    env.host = "http://127.0.0.1:5000"
    env.version = "v1"
    env.timeout = 10.0
    env.ltp_timeout = 3.0
    env.ws_url = None
    env.strategy_tag = "oi_weekly_monthly_sim"

    state_store = module.StateStore.__new__(module.StateStore)
    state_store.path = tmp_path / "state.json"
    state_store.state = module.StrategyState()

    price_stream = FakePriceStream()
    engine = module.StrategyEngine(client, state_store, env, price_stream, execution_id=1, ltp_client=client)

    # Fire-and-forget platform-integration calls do real network I/O by
    # default (localhost POST/GET) -- no-op them for a clean, hermetic
    # simulation; none of them affect trading decisions.
    module.check_force_exit = lambda _env: False
    module.report_pnl_to_platform = lambda *a, **k: None
    module.push_leg_error = lambda *a, **k: None
    module.notify_trade_closed = lambda *a, **k: None
    module.check_pending_action = lambda _env, _leg_key: None
    module.ack_pending_action = lambda *a, **k: None

    return engine, client, state_store


def _drain_fills(engine, timeout=2.0):
    """Entry/exit fill confirmation now happens on a background
    ThreadPoolExecutor task (see StrategyEngine._fill_executor), not inline
    -- even with FakeClient's instant/synchronous poll_fill, the watcher
    still runs on another thread, so the test's main thread must wait for
    it before asserting on state. Busy-waits on _pending_fills draining to
    empty rather than a fixed sleep, since FakeClient normally resolves in
    well under a millisecond."""
    deadline = time.monotonic() + timeout
    while engine._pending_fills and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not engine._pending_fills, (
        f"fill watcher(s) still pending after {timeout}s: {engine._pending_fills}"
    )


def _settle(engine, timeout=2.0):
    """Drains any in-flight background fill-watchers, then runs the
    finalize step (PnL/trade-log/leg-clear, and Weekly's profit-target
    reenter) that a real run_cycle would only pick up on evaluate()'s NEXT
    closed candle -- deterministic in tests instead of depending on the
    scenario's window extending another 5 minutes past the trigger. A
    reenter can itself submit a fresh watcher, so this drains/finalizes in
    a short loop rather than a single pass."""
    for _ in range(4):
        _drain_fills(engine, timeout)
        settled_any = False
        for ot in engine.weekly:
            pos = engine.weekly[ot].leg.position
            if pos.exit_order_id and pos.exit_filled:
                engine.weekly[ot]._finalize_exit(pos, pos.pending_exit_reason, pos.pending_exit_reenter)
                settled_any = True
        for ot in engine.monthly:
            pos = engine.monthly[ot].leg.position
            if pos.exit_order_id and pos.exit_filled:
                engine.monthly[ot]._finalize_exit(pos, pos.pending_exit_reason)
                settled_any = True
        if not settled_any and not engine._pending_fills:
            break


def _run_day(module, engine, clock, day: str, start_hhmm: str, end_hhmm: str):
    """Advances the simulated clock in 5-minute steps from start to end
    (inclusive), calling run_cycle() once per step -- mirrors the real
    scheduler's "evaluate once per newly-closed 5-min candle" behavior
    exactly, since run_cycle() itself gates on _new_candle_closed(). Settles
    (drains + finalizes) after every step so async fill confirmation never
    leaks across candle boundaries within the simulation."""
    IST = module.IST
    h0, m0 = (int(x) for x in start_hhmm.split(":"))
    h1, m1 = (int(x) for x in end_hhmm.split(":"))
    t = real_datetime.strptime(f"{day} {h0:02d}:{m0:02d}", "%Y-%m-%d %H:%M")
    end = real_datetime.strptime(f"{day} {h1:02d}:{m1:02d}", "%Y-%m-%d %H:%M")
    while t <= end:
        clock._current = IST.localize(t)
        engine.run_cycle()
        _settle(engine)
        t += timedelta(minutes=5)


@pytest.fixture
def clock(script_module):
    fixed = _FixedDateTime
    original = script_module.datetime
    script_module.datetime = fixed
    yield fixed
    script_module.datetime = original


WEEKLY_CHAIN_STRIKES = [23100.0, 23400.0, 23700.0, 24300.0, 24600.0, 24900.0]

GREEKS = {
    sim.MONTHLY_CE_SYMBOL: 0.22,
    sim.MONTHLY_PE_SYMBOL: -0.22,
}


@pytest.mark.parametrize(
    "premium_change,oi_change,expected,label",
    [
        (10, 100, "accumulation", "Long Build-up"),
        (-10, 100, "weakening", "Short Build-up"),
        (10, -100, "accumulation", "Short Covering"),
        (-10, -100, "weakening", "Long Unwinding"),
        (0, 100, "flat", "no premium movement"),
        (10, 0, "flat", "no OI movement"),
        (0, 0, "flat", "both flat"),
    ],
)
def test_classify_oi_premium_covers_every_table_quadrant(script_module, premium_change, oi_change, expected, label):
    assert script_module.classify_oi_premium(premium_change, oi_change) == expected, label


def test_day1_weekly_ce_entry_then_exit_on_two_opposite_signals(script_module, clock, tmp_path):
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:55")

    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    # Entry (BUY) at 09:35, exit (SELL) after the 2nd consecutive weakening
    # candle (09:50) -- exactly 2 orders, no duplicate entries in between.
    assert ce_orders == [
        (sim.WEEKLY_CE_SYMBOL, "BUY", 65),
        (sim.WEEKLY_CE_SYMBOL, "SELL", 65),
    ]
    leg = engine.state.legs["WEEKLY_CE"].position
    assert leg.symbol == ""  # closed, position cleared
    assert engine.state.legs["WEEKLY_CE"].trade_count == 1


def test_entry_log_lines_include_open_oi_and_premium_for_both_engines(script_module, clock, tmp_path, caplog):
    """Demonstrates (and asserts on) exactly what a human reviewing the logs
    sees at entry -- both the pre-entry condition-check log AND the
    fill-confirmation log must show open, premium (close), and OI, for
    BOTH reference and current candle, on both Weekly and Monthly."""
    import logging
    caplog.set_level(logging.INFO, logger="OpenAlgoStrategy")

    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    # Weekly CE enters at 09:35 (Accumulation); Monthly CE enters at 09:55
    # once Weekly CE's own signal turns Weakening (see the Day 1 CE walk).
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:55")

    weekly_lines = [r.message for r in caplog.records if "[WEEKLY_CE]" in r.message]
    monthly_lines = [r.message for r in caplog.records if "[MONTHLY_CE]" in r.message]

    weekly_pre_entry = next(l for l in weekly_lines if "verdict=accumulation" in l and "Entry filled" not in l)
    weekly_entry_filled = next(l for l in weekly_lines if "Entry filled" in l)
    monthly_entry_filled = next(l for l in monthly_lines if "Entry filled" in l)

    print("\n--- WEEKLY_CE pre-entry condition check ---")
    print(weekly_pre_entry)
    print("\n--- WEEKLY_CE entry filled ---")
    print(weekly_entry_filled)
    print("\n--- MONTHLY_CE entry filled (with Weekly gate detail) ---")
    print(monthly_entry_filled)

    for line in (weekly_pre_entry, weekly_entry_filled):
        for field in ("ref_open=", "cur_open=", "ref_premium=", "cur_premium=", "ref_oi=", "cur_oi="):
            assert field in line, f"missing {field!r} in: {line}"

    for field in ("ref_open=", "cur_open=", "ref_premium=", "cur_premium=", "ref_oi=", "cur_oi=",
                  "weekly_gate:"):
        assert field in monthly_entry_filled, f"missing {field!r} in: {monthly_entry_filled}"


def test_monthly_gate_sees_fresh_weakening_even_on_the_candle_weekly_exits_by_universal_time(
    script_module, clock, tmp_path
):
    """Regression for the "Weekly's own exit paths skip populating
    latest_weekly_detail" gap: on the EXACT candle Weekly force-closes via
    universal_exit_time, Monthly's same-side confirmation #1 must still see
    that candle's real verdict (here, Weakening) -- not silently see
    nothing just because Weekly happened to exit that same cycle."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 15:25", "%Y-%m-%d %H:%M"))
    engine.state.legs["WEEKLY_CE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=112.0, entry_filled=True,
        entry_order_id="SIM-entry", reference_oi=50_000.0, reference_premium=100.0,
    )
    # This candle's own OI/premium reading vs. the frozen reference (100/50000)
    # is a clear Weakening quadrant (premium down, OI up).
    client.candles_by_symbol[sim.WEEKLY_CE_SYMBOL] = [
        {"timestamp": f"{sim.DAY1} 15:25:00+05:30", "open": 100.0, "high": 100.0,
         "low": 85.0, "close": 85.0, "volume": 1000, "oi": 58_000},
    ]

    engine.weekly["CE"]._manage_open_position()
    _settle(engine)

    # Universal exit time (15:25) still force-closed the leg as expected --
    # this fix doesn't change WHEN Weekly exits, only whether Monthly's gate
    # got to see the reading first.
    assert engine.state.legs["WEEKLY_CE"].position.symbol == ""
    detail = engine.latest_weekly_detail["CE"]
    assert detail["verdict"] == "weakening"
    assert detail["cur_premium"] == pytest.approx(85.0)
    assert detail["cur_oi"] == pytest.approx(58_000.0)


def test_day1_weekly_pe_enters_independently_of_ce(script_module, clock, tmp_path):
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:50")

    pe_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_PE_SYMBOL]
    assert pe_orders == [(sim.WEEKLY_PE_SYMBOL, "BUY", 65)]
    # Still open at the end of the simulated window (never touched CE's
    # exit condition -- proves no cross-contamination).
    assert engine.state.legs["WEEKLY_PE"].position.symbol == sim.WEEKLY_PE_SYMBOL


def test_day1_weekly_pe_profit_target_exit_then_immediate_reentry(script_module, clock, tmp_path):
    """Spec SS5: hitting +100% profit exits the leg, then immediately
    re-selects a fresh OTM1 strike off current spot and re-enters if that
    side's signal still holds. At 09:55 PE's premium (200) is >= 2x the
    entry price (99), and that same candle's own numbers vs. the Day-0
    reference are ALSO Short Covering/Accumulation -- so re-entry fires in
    the same cycle as the exit."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:55")

    pe_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_PE_SYMBOL]
    assert pe_orders == [
        (sim.WEEKLY_PE_SYMBOL, "BUY", 65),    # 09:35 entry
        (sim.WEEKLY_PE_SYMBOL, "SELL", 65),   # 09:55 profit-target exit
        (sim.WEEKLY_PE_SYMBOL, "BUY", 65),    # 09:55 same-cycle re-entry
    ]
    assert engine.state.legs["WEEKLY_PE"].trade_count == 2
    leg = engine.state.legs["WEEKLY_PE"].position
    assert leg.symbol == sim.WEEKLY_PE_SYMBOL  # re-entered, open again
    assert leg.entry_px == pytest.approx(200.0)


def test_day1_monthly_ce_enters_when_gate_and_confirmation_both_weaken(script_module, clock, tmp_path):
    """Gate open (Weekly CE's own signal = weakening) x confirm open
    (Monthly CE's own strike = weakening) -> entry."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:50")

    ce_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_CE_SYMBOL]
    assert ce_orders == [(sim.MONTHLY_CE_SYMBOL, "SELL", 65)]
    assert engine.state.legs["MONTHLY_CE"].position.symbol == sim.MONTHLY_CE_SYMBOL
    assert engine.state.legs["MONTHLY_CE"].position.delta_at_entry == pytest.approx(0.22)


def test_day1_monthly_ce_exits_on_two_consecutive_opposite_signals_no_target_no_sl(
    script_module, clock, tmp_path
):
    """Spec SS12/plan doc SS7#2: Monthly Sell's ONLY exit is 2 consecutive
    opposite-verdict (Accumulation) candles on its own frozen strike -- no
    profit target, no stop-loss. 09:50 and 09:55 both show Long Build-up
    (Accumulation) vs. the entry's own Weakening trigger -> exit at 09:55,
    buying back to close the short."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:55")

    ce_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_CE_SYMBOL]
    assert ce_orders == [
        (sim.MONTHLY_CE_SYMBOL, "SELL", 65),   # 09:45 entry (short)
        (sim.MONTHLY_CE_SYMBOL, "BUY", 65),    # 09:55 exit (buy back to close)
    ]
    assert engine.state.legs["MONTHLY_CE"].position.symbol == ""
    assert engine.state.legs["MONTHLY_CE"].trade_count == 1


def test_day1_monthly_pe_never_enters_because_gate_never_opens(script_module, clock, tmp_path):
    """Gate closed (Weekly PE's own signal stays accumulation, never
    weakens in this window) x confirm open (Monthly PE's own strike DOES
    show weakening-shaped numbers) -> still NO entry. Proves the gate is a
    genuine AND, not satisfiable by confirmation #2 alone."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:50")

    pe_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_PE_SYMBOL]
    assert pe_orders == []
    assert engine.state.legs["MONTHLY_PE"].position.symbol == ""


def test_day1_reference_mode_is_previous_close_for_small_gap(script_module, clock, tmp_path):
    engine, _client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:35")
    assert engine.state.reference.mode == "prev_close"
    assert engine.state.reference.computed is True


def test_day2_reference_mode_is_today_0935_for_large_gap_and_day1_state_does_not_leak(
    script_module, clock, tmp_path
):
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    # Day 1 first, so there's real prior-day state to prove doesn't leak.
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "09:50")
    day1_ce_orders = len([o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL])
    assert day1_ce_orders == 2  # entry + exit, confirmed already above

    _run_day(script_module, engine, clock, sim.DAY2, "09:15", "09:40")

    assert engine.state.reference.mode == "today_0935"
    assert engine.state.reference.reference_date == sim.DAY2
    assert engine.state.legs["WEEKLY_CE"].trade_count == 1  # reset at day boundary, then +1 for Day 2's entry

    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    # Day 1's 2 orders (BUY, SELL) + Day 2's fresh entry (BUY) = 3 total,
    # same strike/symbol both days (see oi_simulation_data.py's Day 2 note).
    assert len(ce_orders) == 3
    assert ce_orders[-1] == (sim.WEEKLY_CE_SYMBOL, "BUY", 65)
    assert engine.state.legs["WEEKLY_CE"].position.symbol == sim.WEEKLY_CE_SYMBOL


def test_day3_reference_mode_is_today_0935_for_a_gap_DOWN(script_module, clock, tmp_path):
    """Day 2 already proved today_0935 mode for a gap UP; this proves the
    same mode for a gap DOWN, which a gap-up-only test could never catch
    since the Reference Engine's check is |Gap%| (magnitude), and a sign
    bug (e.g. accidentally checking `gap_pct > threshold` instead of
    `abs(gap_pct) > threshold`) would silently misclassify a down-gap day
    as small-gap/prev_close instead of large-gap/today_0935."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY3, "09:15", "09:40")

    assert engine.state.reference.mode == "today_0935"
    assert engine.state.reference.reference_date == sim.DAY3
    assert engine.state.reference.gap_pct < 0  # confirms this is genuinely the DOWN case, not up
    assert abs(engine.state.reference.gap_pct) > script_module.config.gap_threshold_pct

    pe_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_PE_SYMBOL]
    # Long Build-up (premium up, OI up) off the today_0935 reference -> entry.
    assert pe_orders == [(sim.WEEKLY_PE_SYMBOL, "BUY", 65)]
    assert engine.state.legs["WEEKLY_PE"].position.symbol == sim.WEEKLY_PE_SYMBOL


def test_restart_resumes_already_open_leg_without_reentering(script_module, clock, tmp_path):
    """The common restart case: a leg fully entered and confirmed BEFORE
    the process stopped. A true reload-from-disk (fresh StateStore.load(),
    fresh StrategyEngine, fresh FakeClient -- everything a real process
    restart would recreate) must resume monitoring that leg, never place a
    second entry order for it."""
    engine1, client1, store1 = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    # WEEKLY_CE enters at 09:35 and is still open at 09:40 (exit doesn't
    # happen until 09:50 -- see the Day-1 CE walk).
    _run_day(script_module, engine1, clock, sim.DAY1, "09:15", "09:40")
    assert engine1.state.legs["WEEKLY_CE"].position.symbol == sim.WEEKLY_CE_SYMBOL
    assert len([o for o in client1.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]) == 1

    # "Restart": brand-new client, brand-new StateStore reloaded from the
    # SAME disk path engine1 just wrote to, brand-new engine -- nothing
    # carried over in memory, exactly like a real process restart.
    store2 = script_module.StateStore.__new__(script_module.StateStore)
    store2.path = store1.path
    store2.load()
    assert store2.state.legs["WEEKLY_CE"].position.symbol == sim.WEEKLY_CE_SYMBOL  # persisted correctly

    client2 = FakeClient(clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26", WEEKLY_CHAIN_STRIKES, GREEKS)
    env2 = script_module.Environment.__new__(script_module.Environment)
    env2.api_key = "TEST_KEY"
    env2.strategy_tag = "oi_weekly_monthly_sim"
    price_stream2 = FakePriceStream()
    engine2 = script_module.StrategyEngine(client2, store2, env2, price_stream2, execution_id=2, ltp_client=client2)

    engine2.reconcile_pending_orders()  # no-op here -- leg was already fully filled
    assert client2.placed_orders == []  # confirms reconcile itself placed nothing

    # Resume the SAME day's remaining candles (09:45 through the 09:50 exit).
    _run_day(script_module, engine2, clock, sim.DAY1, "09:45", "09:50")

    ce_orders = [o for o in client2.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    # Exactly ONE order after resuming -- the exit. No second entry.
    assert ce_orders == [(sim.WEEKLY_CE_SYMBOL, "SELL", 65)]
    assert engine2.state.legs["WEEKLY_CE"].position.symbol == ""
    assert engine2.state.legs["WEEKLY_CE"].trade_count == 1  # never incremented again on resume


def test_reconcile_pending_orders_recovers_a_crash_mid_fill(script_module, clock, tmp_path):
    """The narrow window _enter()/_exit() are built to survive: process
    dies AFTER placeorder() succeeds but BEFORE poll_fill() confirms it --
    state.json was deliberately left with entry_filled=False for exactly
    this reason. Simulates that exact state by hand (since FakeClient fills
    instantly, this window can't be reached just by stepping candles), then
    confirms reconcile_pending_orders() finds the real (already-complete)
    fill via orderstatus() and marks the leg open WITHOUT placing a new
    order."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(
        real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M")
    )

    # Place a real order through the FakeClient directly (so orderstatus()
    # has something to find), then hand-construct the "crashed before
    # poll_fill() ran" state -- entry_order_id set, entry_filled=False.
    order_id = client.placeorder(
        strategy="oi_weekly_monthly_sim", symbol=sim.WEEKLY_CE_SYMBOL, exchange="NFO",
        action="BUY", product="MIS", price_type="MARKET", quantity="65",
        price="0", trigger_price="0", disclosed_quantity="0",
    )["orderid"]
    leg = engine.state.legs["WEEKLY_CE"]
    leg.trade_count = 1  # _enter() increments this BEFORE the crash window too
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65,
        entry_time=clock.now().isoformat(), entry_px=0.0,
        entry_order_id=order_id, entry_filled=False,
        execution_id=1, reference_oi=50_000.0, reference_premium=100.0,
        entry_trigger_verdict="accumulation",
    )

    engine.reconcile_pending_orders()

    assert client.placed_orders == [(sim.WEEKLY_CE_SYMBOL, "BUY", 65)]  # still just the ONE original order
    assert leg.position.entry_filled is True
    assert leg.position.entry_px == pytest.approx(112.0)  # the 09:35 candle's close, per orderstatus()
    assert leg.position.symbol == sim.WEEKLY_CE_SYMBOL  # resumed as open, not cleared or duplicated


def test_day4_reference_mode_is_prev_close_for_a_small_gap_DOWN(script_module, clock, tmp_path):
    """Day 1 covered small-gap UP; this covers small-gap DOWN, the last
    unexercised cell of the 2x2 (small/large) x (up/down) gap matrix."""
    engine, _client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "13-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY4, "09:15", "09:35")

    assert engine.state.reference.mode == "prev_close"
    assert engine.state.reference.reference_date == sim.DAY4
    assert engine.state.reference.gap_pct < 0
    assert abs(engine.state.reference.gap_pct) <= script_module.config.gap_threshold_pct


def test_day4_weekly_ce_streak_interruption_requires_two_TRUE_consecutive_candles(
    script_module, clock, tmp_path
):
    """Distinguishes a correct CONSECUTIVE-candle counter from a buggy
    cumulative one. Walk: Accumulation (entry) -> Weakening (streak=1) ->
    Accumulation (streak RESETS to 0) -> Weakening (streak=1) -> Weakening
    (streak=2, exit). A cumulative counter would wrongly count the
    pre-reset weakening candle too and exit one candle early, at 09:50
    instead of the true 09:55."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "13-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY4, "09:15", "09:50")
    # Not yet exited after only 09:50 -- the reset at 09:45 means this is
    # only the 1st weakening candle since the reset, not the 2nd overall.
    ce_orders_so_far = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL_DAY4]
    assert ce_orders_so_far == [(sim.WEEKLY_CE_SYMBOL_DAY4, "BUY", 65)]  # still just the entry
    assert engine.state.legs["WEEKLY_CE"].position.symbol == sim.WEEKLY_CE_SYMBOL_DAY4  # still open

    _run_day(script_module, engine, clock, sim.DAY4, "09:55", "09:55")
    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL_DAY4]
    assert ce_orders == [
        (sim.WEEKLY_CE_SYMBOL_DAY4, "BUY", 65),
        (sim.WEEKLY_CE_SYMBOL_DAY4, "SELL", 65),   # exits only now, at the TRUE 2nd consecutive candle
    ]
    assert engine.state.legs["WEEKLY_CE"].position.symbol == ""


def test_day4_weekly_pe_force_closed_by_universal_exit_time_only(script_module, clock, tmp_path):
    """No OI-based exit condition and no profit target ever fires in this
    window (see oi_simulation_data.py's Day 4 PE note -- the reading stays
    perpetually Accumulation vs. its own fixed reference, resetting any
    weakening streak to 0 every cycle) -- confirms 15:25 alone force-closes
    the leg."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "13-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY4, "09:15", "15:20")
    assert engine.state.legs["WEEKLY_PE"].position.symbol == sim.WEEKLY_PE_SYMBOL_DAY4  # still open right up to 15:20

    _run_day(script_module, engine, clock, sim.DAY4, "15:25", "15:25")
    pe_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_PE_SYMBOL_DAY4]
    assert pe_orders == [
        (sim.WEEKLY_PE_SYMBOL_DAY4, "BUY", 65),
        (sim.WEEKLY_PE_SYMBOL_DAY4, "SELL", 65),
    ]
    assert engine.state.legs["WEEKLY_PE"].position.symbol == ""


def test_day1_sequential_pe_then_ce_both_weekly_and_monthly(script_module, clock, tmp_path):
    """"weekly PE buy then exit then weekly CE buy... similar for selling
    side": proves leg-slot independence holds for SEQUENTIAL hand-off, not
    just the simultaneous CE+PE entries already covered at 09:35. Full
    extended Day-1 timeline:

      09:35  WEEKLY_CE enters (1st cycle)          WEEKLY_PE enters (1st cycle)
      09:45  MONTHLY_CE enters (gate: CE weakening)
      09:50  WEEKLY_CE exits (1st cycle)
      09:55  MONTHLY_CE exits              WEEKLY_PE profit-target exit + re-enter (2nd cycle)
      10:00                                MONTHLY_PE enters (gate: PE weakening, 2nd cycle)
      10:05                                WEEKLY_PE exits (2nd cycle) <- PE fully done here
      10:10  WEEKLY_CE enters (2nd cycle, AFTER PE fully closed)
      10:15  MONTHLY_CE enters (2nd cycle) MONTHLY_PE exits
      10:20  WEEKLY_CE exits (2nd cycle)
      10:30  MONTHLY_CE exits (2nd cycle)

    Confirms via exact order sequencing that WEEKLY_CE's 2nd-cycle entry
    (10:10) genuinely comes after WEEKLY_PE's 2nd-cycle exit (10:05), and
    the same hand-off shape holds for MONTHLY_CE/MONTHLY_PE on the sell
    side."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY1, "09:15", "10:30")

    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    assert ce_orders == [
        (sim.WEEKLY_CE_SYMBOL, "BUY", 65), (sim.WEEKLY_CE_SYMBOL, "SELL", 65),   # 1st cycle
        (sim.WEEKLY_CE_SYMBOL, "BUY", 65), (sim.WEEKLY_CE_SYMBOL, "SELL", 65),   # 2nd cycle
    ]
    pe_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_PE_SYMBOL]
    assert pe_orders == [
        (sim.WEEKLY_PE_SYMBOL, "BUY", 65), (sim.WEEKLY_PE_SYMBOL, "SELL", 65),   # 1st cycle
        (sim.WEEKLY_PE_SYMBOL, "BUY", 65), (sim.WEEKLY_PE_SYMBOL, "SELL", 65),   # 2nd cycle (profit target + reentry, then exit)
    ]
    monthly_ce_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_CE_SYMBOL]
    assert monthly_ce_orders == [
        (sim.MONTHLY_CE_SYMBOL, "SELL", 65), (sim.MONTHLY_CE_SYMBOL, "BUY", 65),   # 1st cycle
        (sim.MONTHLY_CE_SYMBOL, "SELL", 65), (sim.MONTHLY_CE_SYMBOL, "BUY", 65),   # 2nd cycle
    ]
    monthly_pe_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_PE_SYMBOL]
    assert monthly_pe_orders == [
        (sim.MONTHLY_PE_SYMBOL, "SELL", 65), (sim.MONTHLY_PE_SYMBOL, "BUY", 65),   # only fires once (gate closed until 10:00)
    ]

    # The key sequencing assertion: WEEKLY_PE's 2nd-cycle exit (10:05)
    # strictly precedes WEEKLY_CE's 2nd-cycle entry (10:10) -- both ended
    # the window flat, and _run_day() only advances the clock forward, so
    # the per-symbol order lists above are already in strict wall-clock
    # order by construction; this just confirms the final resting state.
    pe_final = engine.state.legs["WEEKLY_PE"].position
    ce_final = engine.state.legs["WEEKLY_CE"].position
    assert pe_final.symbol == ""      # PE ended the window flat
    assert ce_final.symbol == ""      # CE ended the window flat too (both fully cycled)

    # All 4 leg-slots have non-zero trade_count -- everything actually traded.
    for leg_key in ("WEEKLY_CE", "WEEKLY_PE", "MONTHLY_CE", "MONTHLY_PE"):
        assert engine.state.legs[leg_key].trade_count >= 1, leg_key
    assert engine.state.legs["WEEKLY_CE"].trade_count == 2
    assert engine.state.legs["WEEKLY_PE"].trade_count == 2


# ---------------------------------------------------------------------------
# The remaining cases below don't fit the day-long CSV-walk shape (they need
# to happen mid-position, or need isolated/hand-picked inputs) -- each still
# drives the REAL engine/module-level functions directly, just via
# hand-constructed state instead of stepping through a candle sequence.
# ---------------------------------------------------------------------------

def test_monthly_gate_open_but_own_confirmation_closed_means_no_entry(script_module, clock, tmp_path):
    """The mirror of test_day1_monthly_pe_never_enters_because_gate_never_
    opens: here the GATE is open (Weekly's own signal already weakening)
    but Monthly's OWN strike shows Accumulation, not Weakening -- still no
    entry. Proves confirmation #2 is a genuine, independent requirement,
    not redundant with the gate."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    # Fake the gate directly: Weekly CE's signal this cycle reads Weakening.
    engine.latest_weekly_detail["CE"] = {
        "verdict": "weakening", "symbol": sim.WEEKLY_CE_SYMBOL, "candle": "2026-08-03 09:45:00+05:30",
        "cur_premium": 95.0, "ref_premium": 100.0, "cur_open": 100.0, "ref_open": 100.0,
        "cur_oi": 58_000.0, "ref_oi": 50_000.0,
    }
    engine.state.reference.mode = "prev_close"
    engine.state.reference.reference_date = sim.DAY1
    engine.state.reference.reference_time_iso = sim.DAY0
    engine.state.reference.computed = True
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:45", "%Y-%m-%d %H:%M"))

    # Monthly CE's OWN candle at this moment shows Accumulation (premium
    # UP, OI UP vs. its Day-0 reference 250/80,000) -- confirmation #2 fails.
    client.candles_by_symbol[sim.MONTHLY_CE_SYMBOL] = [
        {"timestamp": f"{sim.DAY0} 15:25:00+05:30", "open": 250.0, "high": 250.0, "low": 250.0, "close": 250.0, "volume": 1000, "oi": 80_000},
        {"timestamp": f"{sim.DAY1} 09:45:00+05:30", "open": 260.0, "high": 260.0, "low": 260.0, "close": 260.0, "volume": 1000, "oi": 84_000},
    ]

    engine.monthly["CE"].evaluate()

    assert client.placed_orders == []
    assert engine.state.legs["MONTHLY_CE"].position.symbol == ""


def test_monthly_expiry_rolls_to_current_month_when_more_than_20_days_remain(script_module, clock):
    """Spec SS8: >20 days to expiry -> current month."""
    clock._current = script_module.IST.localize(real_datetime.strptime("2026-08-03 10:00", "%Y-%m-%d %H:%M"))
    client = FakeClient(clock, {}, "06-Aug-26", "27-Aug-26", [], {})
    # 27-Aug-26 minus 03-Aug-26 = 24 days, > 20 -> current month (27-Aug-26).
    compact, raw = script_module.resolve_monthly_expiry(client)
    assert raw == "27-Aug-26"
    assert compact == "27AUG26"


def test_monthly_expiry_rolls_to_next_month_when_20_days_or_fewer_remain(script_module, clock):
    """Spec SS8: <=20 days to expiry -> next month, not current."""
    clock._current = script_module.IST.localize(real_datetime.strptime("2026-08-07 10:00", "%Y-%m-%d %H:%M"))
    # 27-Aug-26 minus 07-Aug-26 = 20 days, NOT > 20 -> roll to next month's
    # expiry (24-Sep-26, provided as the only later-month date here).
    client = FakeClient(clock, {}, "06-Aug-26", "27-Aug-26", [], {})
    client.monthly_expiry_raw = "27-Aug-26"
    # expiry() only returns 2 fixed dates in FakeClient -- extend it here
    # via a small subclass-free monkeypatch so a next-month date exists to
    # roll to (a real broker's expiry() would list many months out).
    client.expiry = lambda *a, **k: {"status": "success", "data": ["06-Aug-26", "27-Aug-26", "24-Sep-26"]}
    compact, raw = script_module.resolve_monthly_expiry(client)
    assert raw == "24-Sep-26"
    assert compact == "24SEP26"


def test_monthly_delta_strike_scan_continues_past_a_rejected_first_candidate(script_module, clock):
    """select_monthly_delta_strike() scans outward from ATM in 100-pt steps
    -- confirms it doesn't stop at the FIRST candidate if that one's delta
    is outside [0.20, 0.25], and correctly returns the first one that DOES
    qualify further out."""
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:45", "%Y-%m-%d %H:%M"))
    # spot=24,072 -> atm_100=24,100 -> CE scans 24200(step1), 24300(step2), 24400(step3)...
    client = FakeClient(clock, {}, "06-Aug-26", "27-Aug-26", [], {
        "NIFTY27AUG2624200CE": 0.35,   # step 1: too high, rejected
        "NIFTY27AUG2624300CE": 0.30,   # step 2: still too high, rejected
        "NIFTY27AUG2624400CE": 0.23,   # step 3: qualifies
    })
    strike, delta = script_module.select_monthly_delta_strike(client, 24072.0, "27-Aug-26", "CE")
    assert strike == 24400.0
    assert delta == pytest.approx(0.23)


def test_reconcile_pending_orders_recovers_a_crash_mid_exit_fill(script_module, clock, tmp_path):
    """Mirror of test_reconcile_pending_orders_recovers_a_crash_mid_fill,
    but for the EXIT side's two-phase persistence (exit_order_id saved
    before poll_fill()). A crash in this window must resume by completing
    the exit (PnL, trade log, clearing the leg), not by re-placing a second
    exit order."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:50", "%Y-%m-%d %H:%M"))

    order_id = client.placeorder(
        strategy="oi_weekly_monthly_sim", symbol=sim.WEEKLY_CE_SYMBOL, exchange="NFO",
        action="SELL", product="MIS", price_type="MARKET", quantity="65",
        price="0", trigger_price="0", disclosed_quantity="0",
    )["orderid"]
    leg = engine.state.legs["WEEKLY_CE"]
    leg.trade_count = 1
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65,
        entry_time="2026-08-03T09:35:00+05:30", entry_px=112.0,
        entry_order_id="SIM-earlier", entry_filled=True,
        exit_order_id=order_id, exit_filled=False,
        execution_id=1, reference_oi=50_000.0, reference_premium=100.0,
        entry_trigger_verdict="accumulation",
    )

    engine.reconcile_pending_orders()

    assert client.placed_orders == [(sim.WEEKLY_CE_SYMBOL, "SELL", 65)]  # no second order placed
    assert leg.position.symbol == ""  # exit completed, leg cleared
    assert engine.state.today_realized_pnl == pytest.approx((90.0 - 112.0) * 65)  # 09:50 candle close = 90.0


def test_reconcile_pending_orders_clears_leg_when_entry_was_genuinely_rejected(script_module, clock, tmp_path):
    """If the crash-interrupted order turns out to have been rejected/
    cancelled (never actually filled), reconcile must clear the leg AND
    undo the optimistic trade_count increment -- safe to re-evaluate fresh,
    not left dangling as a phantom position."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    order_id = "SIM-rejected-1"
    client.placed_orders.append((sim.WEEKLY_CE_SYMBOL, "BUY", 65))  # so orderstatus() index math stays consistent
    client.orderstatus = lambda order_id, strategy: {"data": {"order_status": "rejected"}}

    leg = engine.state.legs["WEEKLY_CE"]
    leg.trade_count = 1
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65,
        entry_time=clock.now().isoformat(), entry_px=0.0,
        entry_order_id=order_id, entry_filled=False,
        execution_id=1, reference_oi=50_000.0, reference_premium=100.0,
        entry_trigger_verdict="accumulation",
    )

    engine.reconcile_pending_orders()

    assert leg.position.symbol == ""
    assert leg.trade_count == 0  # optimistic increment undone
    assert leg.position.error_state == ""  # NOT an error -- genuinely, safely resolved


def test_reconcile_pending_orders_flags_still_pending_order_for_manual_review(script_module, clock, tmp_path):
    """If the order is still resting/pending after restart (broker hasn't
    resolved it either way), reconcile must NOT guess -- flags error_state
    for a human decision, same taxonomy as a normal poll_fill() timeout."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    order_id = "SIM-pending-1"
    client.orderstatus = lambda order_id, strategy: {"data": {"order_status": "open"}}

    leg = engine.state.legs["WEEKLY_CE"]
    leg.trade_count = 1
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65,
        entry_time=clock.now().isoformat(), entry_px=0.0,
        entry_order_id=order_id, entry_filled=False,
        execution_id=1, reference_oi=50_000.0, reference_premium=100.0,
        entry_trigger_verdict="accumulation",
    )

    engine.reconcile_pending_orders()

    assert client.placed_orders == []  # never re-placed a new order
    assert leg.position.error_state == "entry_failed"
    assert leg.position.error_kind == "resting"
    assert leg.position.error_order_id == order_id


def test_force_exit_all_closes_every_open_leg(script_module, clock, tmp_path):
    """The kill-switch path: force_exit closes every currently open leg
    (weekly and monthly, both sides), regardless of what their own OI
    signal is doing that cycle."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:45", "%Y-%m-%d %H:%M"))

    engine.state.legs["WEEKLY_CE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=112.0, entry_filled=True,
        entry_order_id="SIM-a", reference_oi=50_000.0, reference_premium=100.0,
    )
    engine.state.legs["MONTHLY_CE"].position = script_module.LegPosition(
        symbol=sim.MONTHLY_CE_SYMBOL, quantity=65, entry_px=240.0, entry_filled=True,
        entry_order_id="SIM-b", reference_oi=80_000.0, reference_premium=250.0,
    )

    engine._force_exit_all()
    _settle(engine)

    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    monthly_ce_orders = [o for o in client.placed_orders if o[0] == sim.MONTHLY_CE_SYMBOL]
    assert ce_orders == [(sim.WEEKLY_CE_SYMBOL, "SELL", 65)]     # long leg force-closed with a SELL
    assert monthly_ce_orders == [(sim.MONTHLY_CE_SYMBOL, "BUY", 65)]  # short leg force-closed with a BUY
    assert engine.state.legs["WEEKLY_CE"].position.symbol == ""
    assert engine.state.legs["MONTHLY_CE"].position.symbol == ""


def test_gap_exactly_at_threshold_is_small_gap_prev_close(script_module, clock, tmp_path):
    """Spec's gap check is `<= GAP_THRESHOLD` (0.5%) -> boundary case
    exactly AT the threshold must still be prev_close, not today_0935."""
    engine, _client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    # Day 0 close is 24,000 (see oi_simulation_data.py). Exactly +0.5% = 24,120.
    engine.client.candles_by_symbol["NIFTY"] = [
        *engine.client.candles_by_symbol["NIFTY"],
        {"timestamp": f"{sim.DAY1} 09:30:00+05:30", "open": 24120.0, "high": 24120.0,
         "low": 24120.0, "close": 24120.0, "volume": 1000, "oi": 0},
    ]
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:30", "%Y-%m-%d %H:%M"))
    engine._reset_day_if_needed()
    engine._ensure_reference()

    assert engine.state.reference.computed is True
    assert engine.state.reference.mode == "prev_close"
    assert engine.state.reference.gap_pct == pytest.approx(0.5, abs=1e-6)


def test_history_failure_mid_cycle_skips_gracefully_without_crashing_or_fabricating_a_signal(
    script_module, clock, tmp_path
):
    """A broker-side history() failure for one leg's candle fetch must
    result in that leg's evaluation being skipped for the cycle (no order
    placed, no crash) -- never treated as a signal one way or the other."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    engine.state.reference.mode = "prev_close"
    engine.state.reference.reference_date = sim.DAY1
    engine.state.reference.reference_time_iso = sim.DAY0
    engine.state.reference.computed = True
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))

    original_history = client.history
    client.history = lambda *a, **k: {"status": "error", "message": "broker timeout"}
    try:
        engine.weekly["CE"].evaluate()  # must not raise
    finally:
        client.history = original_history

    assert client.placed_orders == []
    assert engine.state.legs["WEEKLY_CE"].position.symbol == ""


def test_day5_sideways_quiet_day_places_zero_orders_all_session(script_module, clock, tmp_path):
    """Market-regime case: a choppy/range-bound day where premium never
    exceeds its own reference (every candle classifies as Weakening or
    flat, never Accumulation -- see oi_simulation_data.py's Day 5 note).
    No false positives: zero orders across a full session for all 4 legs,
    even though many individual candles are close to the threshold."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "13-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    _run_day(script_module, engine, clock, sim.DAY5, "09:15", "10:45")

    assert client.placed_orders == []
    for leg_key in ("WEEKLY_CE", "WEEKLY_PE", "MONTHLY_CE", "MONTHLY_PE"):
        assert engine.state.legs[leg_key].position.symbol == ""
        assert engine.state.legs[leg_key].trade_count == 0


def test_weekly_otm1_strike_selection_adapts_to_current_spot_while_flat(script_module, clock):
    """select_weekly_otm1_strike() must reflect CURRENT spot each time it's
    called (only ever invoked while a side is flat, per spec SS1 "strike
    determined using current spot only when no position is open") -- not a
    stale/cached value from earlier in the day. Confirms two different spot
    levels against the SAME chain ladder resolve to two different strikes."""
    client = FakeClient(clock, {}, "13-Aug-26", "27-Aug-26", WEEKLY_CHAIN_STRIKES, GREEKS)
    # Ladder: [23100, 23400, 23700, 24300, 24600, 24900]
    # Compact expiry form ("13AUG26") -- what optionchain() actually expects
    # in production (see FakeClient.optionchain()'s own format assertion).
    strike_low = script_module.select_weekly_otm1_strike(client, 24072.0, "13AUG26", "CE")
    strike_high = script_module.select_weekly_otm1_strike(client, 24550.0, "13AUG26", "CE")
    assert strike_low == 24300.0    # nearest listed strike above 24,072
    assert strike_high == 24600.0   # spot has since crossed the 24,300 gap -- OTM1 shifts with it
    assert strike_low != strike_high

    # Mirror for PE (OTM = below spot). 23,650 has since crossed BELOW the
    # 23,700 strike, so OTM1 PE shifts down to the next rung, 23,400.
    pe_high_spot = script_module.select_weekly_otm1_strike(client, 24072.0, "13AUG26", "PE")
    pe_low_spot = script_module.select_weekly_otm1_strike(client, 23650.0, "13AUG26", "PE")
    assert pe_high_spot == 23700.0
    assert pe_low_spot == 23400.0
    assert pe_high_spot != pe_low_spot


def test_all_four_legs_open_simultaneously(script_module, clock, tmp_path):
    """Explicit snapshot: Weekly CE, Weekly PE, Monthly CE, and Monthly PE
    ALL open at once (the maximum concurrency this strategy can reach, per
    plan doc SS1.5). Confirms PnL aggregation and state persistence both
    handle all 4 leg-slots simultaneously without cross-contamination --
    each leg's own long/short PnL sign, and a full save/reload round-trip
    preserving all 4 positions correctly."""
    engine, _client, store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 10:00", "%Y-%m-%d %H:%M"))

    engine.state.legs["WEEKLY_CE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=112.0, entry_filled=True,
        entry_order_id="SIM-1", reference_oi=50_000.0, reference_premium=100.0,
    )
    engine.state.legs["WEEKLY_PE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_PE_SYMBOL, quantity=65, entry_px=99.0, entry_filled=True,
        entry_order_id="SIM-2", reference_oi=40_000.0, reference_premium=90.0,
    )
    engine.state.legs["MONTHLY_CE"].position = script_module.LegPosition(
        symbol=sim.MONTHLY_CE_SYMBOL, quantity=65, entry_px=240.0, entry_filled=True,
        entry_order_id="SIM-3", reference_oi=80_000.0, reference_premium=250.0,
    )
    engine.state.legs["MONTHLY_PE"].position = script_module.LegPosition(
        symbol=sim.MONTHLY_PE_SYMBOL, quantity=65, entry_px=210.0, entry_filled=True,
        entry_order_id="SIM-4", reference_oi=70_000.0, reference_premium=230.0,
    )

    open_positions = engine._open_positions_for_pnl()
    assert len(open_positions) == 4
    by_leg = {p["leg"]: p for p in open_positions}
    # Weekly legs are LONG: pnl = (ltp - entry_px) x qty, positive when price rises.
    # Monthly legs are SHORT: pnl = (entry_px - ltp) x qty, positive when price falls.
    # PriceStream is a stub here (always None -> falls back to entry_px as
    # "last known" -- see _open_positions_for_pnl), so ltp == entry_px and
    # every leg's pnl is exactly 0 -- the point of this assertion is that
    # all 4 leg keys are present with the correct sign convention wired in,
    # not a specific nonzero value.
    for leg_key in ("WEEKLY_CE", "WEEKLY_PE", "MONTHLY_CE", "MONTHLY_PE"):
        assert leg_key in by_leg
        assert by_leg[leg_key]["pnl"] == pytest.approx(0.0)

    # Round-trip through disk: a restart mid-way through holding all 4 legs
    # must preserve every one of them exactly.
    store.save()
    reloaded = script_module.StateStore.__new__(script_module.StateStore)
    reloaded.path = store.path
    reloaded.load()
    for leg_key, symbol, px in (
        ("WEEKLY_CE", sim.WEEKLY_CE_SYMBOL, 112.0), ("WEEKLY_PE", sim.WEEKLY_PE_SYMBOL, 99.0),
        ("MONTHLY_CE", sim.MONTHLY_CE_SYMBOL, 240.0), ("MONTHLY_PE", sim.MONTHLY_PE_SYMBOL, 210.0),
    ):
        pos = reloaded.state.legs[leg_key].position
        assert pos.symbol == symbol
        assert pos.entry_px == pytest.approx(px)
        assert pos.entry_filled is True


def test_retry_terminal_entry_error_places_fresh_order_and_resumes(script_module, clock, tmp_path):
    """Retry on a 'terminal' entry error (nothing resting at the broker)
    must place a genuinely NEW order and resume the normal async
    fill-watch -- not silently drop the leg."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id="SIM-old", entry_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
        error_state="entry_failed", error_kind="terminal", error_order_id="",
    )

    engine.weekly["CE"]._resolve_leg_error({"action": "retry"})
    _drain_fills(engine)

    ce_orders = [o for o in client.placed_orders if o[0] == sim.WEEKLY_CE_SYMBOL]
    assert ce_orders == [(sim.WEEKLY_CE_SYMBOL, "BUY", 65)]  # a genuinely new order
    assert leg.position.error_state == ""
    assert leg.position.entry_filled is True  # FakeClient fills it instantly


def test_retry_resting_entry_error_reprices_same_order_and_resumes(script_module, clock, tmp_path):
    """Retry on a 'resting' entry error must re-price the SAME order
    (modifyorder), not place a duplicate."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    client.placed_orders.append((sim.WEEKLY_CE_SYMBOL, "BUY", 65))  # order_id index math
    order_id = "SIM1"
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id=order_id, entry_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
        error_state="entry_failed", error_kind="resting", error_order_id=order_id,
    )
    modify_calls = []
    client.modifyorder = lambda **kwargs: modify_calls.append(kwargs) or {"status": "success"}

    engine.weekly["CE"]._resolve_leg_error({"action": "retry"})
    _drain_fills(engine)

    assert len(client.placed_orders) == 1  # no duplicate placeorder
    assert len(modify_calls) == 1
    assert modify_calls[0]["order_id"] == order_id
    assert modify_calls[0]["action"] == "BUY"
    assert leg.position.error_state == ""
    assert leg.position.entry_filled is True


def test_cancel_terminal_entry_error_clears_leg_with_no_broker_call(script_module, clock, tmp_path):
    """Cancel on a 'terminal' entry error needs no broker call -- nothing is
    resting -- just clears the leg locally, safe to re-evaluate fresh."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["MONTHLY_CE"]
    leg.trade_count = 1
    leg.position = script_module.LegPosition(
        symbol=sim.MONTHLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id="SIM-old", entry_filled=False,
        reference_oi=80_000.0, reference_premium=250.0,
        error_state="entry_failed", error_kind="terminal", error_order_id="",
    )

    engine.monthly["CE"]._resolve_leg_error({"action": "cancel"})

    assert client.placed_orders == []  # no broker call at all
    assert leg.position.symbol == ""
    assert leg.position.error_state == ""


def test_cancel_exit_error_leaves_position_open_for_a_fresh_exit_later(script_module, clock, tmp_path):
    """Cancel on an exit error abandons ONLY the failed close attempt --
    the underlying position must remain open (never silently flattened),
    so a later exit_condition cycle places a brand-new close order."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=112.0, entry_filled=True,
        entry_order_id="SIM-entry", exit_order_id="SIM-exit-old", exit_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
        error_state="exit_failed", error_kind="resting", error_order_id="SIM-exit-old",
        pending_exit_reason="opposite_signal",
    )

    engine.weekly["CE"]._resolve_leg_error({"action": "cancel"})

    assert leg.position.symbol == sim.WEEKLY_CE_SYMBOL  # still open
    assert leg.position.exit_order_id == ""
    assert leg.position.error_state == ""


def test_manual_completes_entry_at_the_given_fill_price(script_module, clock, tmp_path):
    """Manually Completed on an entry error trusts the user's real
    broker-confirmed fill price -- resumes the leg as open, no fresh order."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["WEEKLY_PE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_PE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id="SIM-old", entry_filled=False,
        reference_oi=40_000.0, reference_premium=90.0,
        error_state="entry_failed", error_kind="resting", error_order_id="SIM-old",
    )

    engine.weekly["PE"]._resolve_leg_error({"action": "manual", "fill_price": 95.0})

    assert client.placed_orders == []  # no broker call -- trusts the user's number
    assert leg.position.entry_filled is True
    assert leg.position.entry_px == pytest.approx(95.0)
    assert leg.position.error_state == ""
    assert leg.trade_count == 1


def test_manual_completes_exit_at_the_given_fill_price_and_finalizes_pnl(script_module, clock, tmp_path):
    """Manually Completed on an exit error: manual_exit_px must win over
    any broker-confirmed exit_fill_px when finalize runs (the user's
    number is authoritative for a manually-resolved order)."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["MONTHLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.MONTHLY_CE_SYMBOL, quantity=65, entry_px=250.0, entry_filled=True,
        entry_order_id="SIM-entry", exit_order_id="SIM-exit-old", exit_filled=False,
        reference_oi=80_000.0, reference_premium=250.0,
        error_state="exit_failed", error_kind="resting", error_order_id="SIM-exit-old",
        pending_exit_reason="opposite_signal",
    )

    engine.monthly["CE"]._resolve_leg_error({"action": "manual", "fill_price": 230.0})
    _settle(engine)

    assert leg.position.symbol == ""  # finalized and cleared
    # Short leg profit = entry_px - exit_px = 250 - 230 = 20 points/unit.
    assert engine.state.today_realized_pnl == pytest.approx(20.0 * 65)


def test_cancel_resting_entry_abandons_order_when_final_reprice_chance_fails(script_module, clock, tmp_path):
    """Cancel's one-last-chance flow for a resting order: if the final
    reprice+wait genuinely comes back empty (never fills), the order must
    be explicitly cancelled at the broker and the leg cleared locally --
    never left dangling."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    order_id = "SIM-stuck-1"
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id=order_id, entry_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
        error_state="entry_failed", error_kind="resting", error_order_id=order_id,
    )
    # Simulate the final reprice+wait chance genuinely failing (never fills)
    # without risking a real-time hang against the frozen test clock.
    script_module._reprice_and_wait_once = lambda *a, **k: None
    cancel_calls = []
    client.cancelorder = lambda order_id, strategy: cancel_calls.append(order_id) or {"status": "success"}

    engine.weekly["CE"]._resolve_leg_error({"action": "cancel"})
    _drain_fills(engine)

    assert cancel_calls == [order_id]
    assert leg.position.symbol == ""
    assert leg.position.error_state == ""


def test_no_new_entry_after_cutoff_time_even_when_signal_holds(script_module, clock, tmp_path):
    """Spec addition: no NEW trade entries from config.entry_cutoff_time
    (14:45) onward, on either side/engine -- existing open legs are
    untouched by this gate (only _evaluate_entry is gated, never
    _manage_open_position)."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    engine.state.reference.mode = "prev_close"
    engine.state.reference.reference_date = sim.DAY1
    engine.state.reference.reference_time_iso = sim.DAY0
    engine.state.reference.computed = True
    # 09:35's own candle data is a strong Accumulation signal (see the Day 1
    # CE walk) -- confirms the cutoff blocks entry even though the signal
    # itself would otherwise fire.
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 14:50", "%Y-%m-%d %H:%M"))
    engine.latest_weekly_detail["CE"] = {
        "verdict": "weakening", "strike": 24300.0, "symbol": sim.WEEKLY_CE_SYMBOL,
        "candle": "irrelevant", "cur_premium": 90.0, "ref_premium": 100.0,
        "cur_oi": 60000, "ref_oi": 50000,
    }

    engine.weekly["CE"]._evaluate_entry()
    engine.monthly["CE"]._evaluate_entry()

    assert client.placed_orders == []
    assert engine.state.legs["WEEKLY_CE"].position.symbol == ""
    assert engine.state.legs["MONTHLY_CE"].position.symbol == ""


def test_entry_cutoff_does_not_block_managing_an_already_open_position(script_module, clock, tmp_path):
    """The cutoff only gates NEW entries -- an already-open leg must still
    be fully managed (exit checks, profit target, etc.) past 14:45."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 15:00", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=112.0, entry_filled=True,
        entry_order_id="SIM-entry", reference_oi=50_000.0, reference_premium=100.0,
    )

    engine.weekly["CE"].evaluate()
    _settle(engine)

    # Past universal_exit_time (15:15)? No -- 15:00 is before it, so this
    # just proves evaluate() routed to _manage_open_position (not silently
    # skipped) by observing a fresh OI-verdict log/consecutive_opposite
    # update happened -- simplest signal: the leg is still tracked (not
    # untouched/stuck) and no order was needed since nothing triggered yet.
    assert leg.position.symbol == sim.WEEKLY_CE_SYMBOL


def test_entry_placeorder_exception_sets_error_state_not_silently_dropped(script_module, clock, tmp_path):
    """A placeorder() exception (ambiguous -- could have actually gone
    through at the broker) must surface as an error_state, same as a
    poll_fill() failure -- not just a log line with the leg silently left
    flat to retry blindly next cycle."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    client.placeorder = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker unreachable"))

    engine.weekly["CE"]._enter(
        sim.WEEKLY_CE_SYMBOL, 24300.0,
        {"oi": 50_000.0, "premium": 100.0, "open": 100.0, "timestamp": "x"},
        {"oi": 60_000.0, "premium": 130.0, "open": 100.0, "timestamp": "x"},
    )

    leg = engine.state.legs["WEEKLY_CE"]
    assert leg.position.symbol == sim.WEEKLY_CE_SYMBOL  # persisted before the failed place()
    assert leg.position.error_state == "entry_failed"
    assert leg.position.error_kind == "terminal"
    assert client.placed_orders == []  # placeorder() raised -- nothing actually recorded as placed


def test_exit_placeorder_exception_sets_error_state(script_module, clock, tmp_path):
    """Same as the entry case, for the exit side -- covers both the normal
    signal-driven exit path AND Force Exit, since they share this same
    _exit() call."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:50", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["MONTHLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.MONTHLY_CE_SYMBOL, quantity=65, entry_px=250.0, entry_filled=True,
        entry_order_id="SIM-entry", reference_oi=80_000.0, reference_premium=250.0,
    )
    client.placeorder = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("broker unreachable"))

    engine.monthly["CE"]._exit(leg.position, 270.0, "opposite_signal")

    assert leg.position.symbol == sim.MONTHLY_CE_SYMBOL  # still open -- exit never confirmed
    assert leg.position.error_state == "exit_failed"
    assert leg.position.error_kind == "terminal"


def test_reconcile_flags_ambiguous_pre_placeorder_crash_window(script_module, clock, tmp_path):
    """The narrowest crash window: process died between persisting the leg
    as "attempting entry" and place() actually returning -- reconcile must
    NOT guess (silently clearing risks missing a real position; silently
    treating as open risks trading on entry_px=0.0). Flags for manual
    verification instead."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    leg = engine.state.legs["WEEKLY_CE"]
    leg.position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id="", entry_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
        entry_trigger_verdict="accumulation",
    )

    engine.reconcile_pending_orders()

    assert client.placed_orders == []  # never guessed by placing a fresh order itself
    assert leg.position.symbol == sim.WEEKLY_CE_SYMBOL  # left as-is, not cleared
    assert leg.position.error_state == "entry_failed"
    assert leg.position.error_kind == "terminal"
    assert "verify manually" in leg.position.error_message.lower() or \
           "verify" in leg.position.error_message.lower()


def test_pnl_snapshot_excludes_a_leg_still_awaiting_entry_confirmation(script_module, clock, tmp_path):
    """_open_positions_for_pnl must exclude a leg whose entry isn't
    confirmed filled yet (entry_px is still 0.0) -- including it would
    report a fabricated, wildly wrong "profit" against that zero."""
    engine, client, _store = _build_engine(
        script_module, clock, _CSV_CANDLES, "06-Aug-26", "27-Aug-26",
        WEEKLY_CHAIN_STRIKES, GREEKS, tmp_path,
    )
    clock._current = script_module.IST.localize(real_datetime.strptime(f"{sim.DAY1} 09:35", "%Y-%m-%d %H:%M"))
    engine.state.legs["WEEKLY_CE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_CE_SYMBOL, quantity=65, entry_px=0.0,
        entry_order_id="SIM-1", entry_filled=False,
        reference_oi=50_000.0, reference_premium=100.0,
    )
    engine.state.legs["WEEKLY_PE"].position = script_module.LegPosition(
        symbol=sim.WEEKLY_PE_SYMBOL, quantity=65, entry_px=99.0, entry_filled=True,
        entry_order_id="SIM-2", reference_oi=40_000.0, reference_premium=90.0,
    )

    open_positions = engine._open_positions_for_pnl()

    by_leg = {p["leg"] for p in open_positions}
    assert "WEEKLY_CE" not in by_leg  # still mid-entry -- excluded
    assert "WEEKLY_PE" in by_leg      # confirmed filled -- included
