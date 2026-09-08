"""
Unit tests for
strategies/deployed/Nifty_2Min_DonchianEMA9_Sell_1_20260906000000.py -- the
NIFTY 2-min Donchian(10)/EMA(9) naked option-SELLING strategy ported from
data23_to_26/backtest_nifty_2min_donchian_ema9_sell.py.

No live broker connection is used anywhere in this file -- every
client/price_stream dependency is a stub or MagicMock, and network-side
calls are monkeypatched out. Focuses on the logic that is genuinely NEW in
this script (not copied verbatim from an already-tested donor):
  - The Donchian-channel fidelity requirement (excludes the current/trigger
    candle, unlike ta.donchian()'s own inclusive rolling window).
  - The arm/confirm/countdown/cancel state machine, including the
    catch-up-batch handling that only dispatches an entry on the LATEST
    closed candle.
  - The ITM-floor strike-selection fallback (mirrors the corrected
    backtest's own fix).
  - The SL/target premium check.
  - The single shared daily trade cap and single-position-slot semantics
    (the two bugs the backtest itself needed fixing before it was
    trustworthy -- this is their live-code regression coverage).
"""

import importlib.util
import sys
from datetime import date, datetime as real_datetime
from datetime import time as dtime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_2Min_DonchianEMA9_Sell_1_20260906000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_5min_supertrend_pivot_sell.py's identical helper: the repo
    root is itself importable as a package literally named `openalgo`,
    which can shadow the pip-installed SDK under pytest's
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
    spec = importlib.util.spec_from_file_location("nifty_2min_donchian_ema9_sell_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


@pytest.fixture
def engine(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "notify_trade_closed", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "notify_telegram_error", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "push_leg_error", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "check_pending_action", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "ack_pending_action", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "check_force_exit", lambda *a, **k: False)
    monkeypatch.setattr(script_module, "ack_force_exit_complete", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "append_trade_log", lambda *a, **k: None)

    env = script_module.Environment()
    store = script_module.StateStore(env)
    monkeypatch.setattr(store, "save", lambda: None)
    client = MagicMock()
    price_stream = MagicMock()
    price_stream.get_ltp.return_value = None
    eng = script_module.StrategyEngine(client, store, env, price_stream, execution_id=1, ltp_client=MagicMock())
    yield eng
    eng._fill_executor.shutdown(wait=False)
    eng._bg_executor.shutdown(wait=False)
    eng._pnl_executor.shutdown(wait=False)


def _make_bars(rows: list) -> pd.DataFrame:
    """rows: list of (timestamp_str, open, high, low, close). Builds the
    lowercase-column DataFrame shape client.history() returns."""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
        },
        index=idx,
    )


def _ts_at(day: str, start_hm: tuple, offset_idx: int) -> str:
    h, m = start_hm
    total_min = h * 60 + m + 2 * offset_idx
    hh, mm = divmod(total_min, 60)
    return f"{day} {hh:02d}:{mm:02d}:00"


def _filler_bars(day: str, start_hm: tuple, n: int, start_idx: int = 0,
                  close: float = 99.5) -> list:
    """Strictly monotonically-NARROWING candles (high decreasing, low
    increasing by a tiny amount each bar) -- guarantees every bar's own
    High/Low is always safely inside the rolling max/min of the PRECEDING
    bars, so these never spuriously self-trigger the Donchian band
    regardless of how many are chained together (a genuinely flat/repeated
    range would tie against its own rolling window and false-trigger via
    the >=/<= comparison). `close` defaults to 99.5 (matching the OHLC
    range) but can be raised (e.g. 100.0) so a caller can keep a bar
    unambiguously ABOVE ema9 while still armed for CE, without introducing
    a fresh false trigger of its own."""
    rows = []
    for i in range(n):
        idx = start_idx + i
        high = 100.0 - 0.001 * idx
        low = 99.0 + 0.001 * idx
        rows.append((_ts_at(day, start_hm, idx), 99.5, high, low, close))
    return rows


# Enough filler bars to clear compute_donchian_signal's own warmup floor
# (donchian_period + ema_period + 2 = 10 + 9 + 2 = 21 CLOSED bars) with
# room to spare before any test's own special candles are appended.
WARMUP_N = 20


# ---------------------------------------------------------------------------
# Donchian channel fidelity: must exclude the current/trigger candle
# ---------------------------------------------------------------------------
class TestDonchianFidelity:
    def test_channel_excludes_the_latest_closed_candle(self, script_module):
        """A candle whose own High is the new all-time high of the WHOLE
        series (including itself) must NOT count as a Donchian-upper
        trigger purely because of its own extreme -- the channel it's
        compared against must be built from the PRIOR 10 candles only."""
        day = "2026-09-01"
        rows = _filler_bars(day, (9, 15), WARMUP_N)
        # One candle with an extreme high -- if the channel included that
        # candle itself, donchian_upper would jump to include it and the
        # trigger math would be internally circular.
        rows.append((_ts_at(day, (9, 15), WARMUP_N), 99.5, 150.0, 99.5, 99.5))
        # Still-forming bar, dropped.
        rows.append((_ts_at(day, (9, 15), WARMUP_N + 1), 99.5, 100.0, 99.0, 99.5))
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        sig = script_module.compute_donchian_signal(bars, state, ltp=99.5)

        assert sig is not None
        # The channel for the extreme-high candle must be built from the
        # 10 preceding filler candles only -- their own highs top out just
        # under 100, NOT anywhere near 150.
        assert sig.donchian_upper < 100.0

    def test_first_bar_of_day_is_never_a_trigger(self, script_module):
        """A day's first candle touching the Donchian band must not arm
        anything, even though the indicator value itself is still computed
        continuously (no reset)."""
        prior_day = _filler_bars("2026-08-31", (9, 15), WARMUP_N)
        # New day's FIRST candle spikes -- must not arm despite High>=channel.
        new_day_first = [("2026-09-01 09:15:00", 99.5, 150.0, 99.5, 149.0)]
        # Still-forming bar, dropped.
        still_forming = [("2026-09-01 09:17:00", 149.0, 150.0, 149.0, 149.0)]
        bars = _make_bars(prior_day + new_day_first + still_forming)

        state = script_module.StrategyState()
        sig = script_module.compute_donchian_signal(bars, state, ltp=149.0)

        assert sig is not None
        assert state.armed_side == "", "first candle of the day must never arm a trigger"


# ---------------------------------------------------------------------------
# Arm / confirm / countdown / cancel state machine
# ---------------------------------------------------------------------------
class TestStateMachine:
    DAY = "2026-09-01"

    def test_trigger_then_confirmation_enters(self, script_module):
        """A trigger candle (High>=upper) followed by a LATER candle whose
        Close < EMA9 fires a CE entry on that later candle -- and only
        because it IS the latest closed candle in the batch."""
        rows = _filler_bars(self.DAY, (9, 15), WARMUP_N)
        n = WARMUP_N
        # Trigger candle: high spikes above the established channel.
        rows.append((_ts_at(self.DAY, (9, 15), n), 99.5, 150.0, 99.5, 99.5))
        # One neutral candle in between (still armed, not yet confirmed).
        # Close=100.0 is clearly ABOVE ema9 (~99.5) -- avoids a
        # floating-point tie against ema9's recursive EWM computation that
        # would otherwise register a spurious CE confirmation here.
        rows.append((_ts_at(self.DAY, (9, 15), n + 1), 99.5, 100.0, 99.3, 100.0))
        # Confirmation candle: closes below EMA9 (EMA9 is still ~99.5 after
        # a long flat run, so a close well below it, e.g. 50, confirms).
        rows.append((_ts_at(self.DAY, (9, 15), n + 2), 99.5, 100.0, 49.0, 50.0))
        # Still-forming bar, dropped.
        rows.append((_ts_at(self.DAY, (9, 15), n + 3), 50.0, 51.0, 49.0, 50.0))
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        sig = script_module.compute_donchian_signal(bars, state, ltp=50.0)

        assert sig is not None
        assert sig.pending_entry_side == "CE"
        assert state.armed_side == "", "confirmed entry must clear the armed state"

    def test_same_side_retrigger_refreshes_countdown(self, script_module):
        rows = _filler_bars(self.DAY, (9, 15), WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(self.DAY, (9, 15), n), 99.5, 150.0, 99.5, 99.5))  # CE trigger, countdown=20
        # 15 neutral candles pass (countdown would reach 5 without a
        # refresh). Monotonically-narrowing (via _filler_bars) so none of
        # these self-tie their own rolling channel over such a long run;
        # close=100.0 keeps them clearly ABOVE ema9 too, avoiding a
        # floating-point tie that would otherwise register a spurious CE
        # confirmation while still armed.
        rows += _filler_bars(self.DAY, (9, 15), 15, start_idx=n + 1, close=100.0)
        # A SECOND CE trigger candle -- must refresh the window back to 20.
        # Close also kept at 100.0 (well above ema9) for the same reason --
        # only its HIGH (150) is meant to matter here, not its close.
        rows.append((_ts_at(self.DAY, (9, 15), n + 16), 99.5, 150.0, 99.5, 100.0))
        rows.append((_ts_at(self.DAY, (9, 15), n + 17), 100.0, 101.0, 99.3, 100.0))  # still-forming, dropped
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        script_module.compute_donchian_signal(bars, state, ltp=99.5)

        assert state.armed_side == "CE"
        assert state.armed_countdown == script_module.config.confirmation_max_candles

    def test_opposite_side_trigger_cancels_and_switches(self, script_module):
        rows = _filler_bars(self.DAY, (9, 15), WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(self.DAY, (9, 15), n), 99.5, 150.0, 99.5, 99.5))       # CE trigger
        # Close=99.9, clearly ABOVE ema9 (~99.5) so this does NOT also
        # register as a (spurious, float-precision-driven) CE confirmation
        # -- only the Low breach (PE trigger) should fire here.
        rows.append((_ts_at(self.DAY, (9, 15), n + 1), 99.5, 100.0, 50.0, 99.9))   # PE trigger (Low breach)
        rows.append((_ts_at(self.DAY, (9, 15), n + 2), 99.9, 100.0, 99.0, 99.9))   # still-forming, dropped
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        script_module.compute_donchian_signal(bars, state, ltp=99.5)

        assert state.armed_side == "PE", "opposite-side trigger must cancel CE and arm PE instead"
        assert state.armed_countdown == script_module.config.confirmation_max_candles

    def test_countdown_expires_and_disarms(self, script_module):
        rows = _filler_bars(self.DAY, (9, 15), WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(self.DAY, (9, 15), n), 99.5, 150.0, 99.5, 99.5))  # CE trigger, countdown=20
        # 21 neutral candles with NO confirmation -- window must expire
        # (the last one acts as the still-forming bar and is dropped, so
        # only 20 of these are actually processed as closed candles).
        # Monotonically-narrowing (via _filler_bars) so a run this long
        # never self-ties its own rolling channel; close=100.0 keeps them
        # clearly ABOVE ema9, avoiding a floating-point tie that would
        # otherwise register a spurious CE confirmation.
        rows += _filler_bars(self.DAY, (9, 15), 21, start_idx=n + 1, close=100.0)
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        script_module.compute_donchian_signal(bars, state, ltp=99.5)

        assert state.armed_side == "", "20-candle confirmation window must expire without confirmation"

    def test_confirmation_on_a_catchup_candle_does_not_dispatch_a_stale_entry(self, script_module):
        """If a whole batch of candles (a restart/gap) is processed at
        once and the confirmation candle is NOT the latest one in that
        batch, the state machine still advances (disarms) but must NOT
        set pending_entry_side -- entering on stale historical data would
        be wrong for a live strategy."""
        rows = _filler_bars(self.DAY, (9, 15), WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(self.DAY, (9, 15), n), 99.5, 150.0, 99.5, 99.5))        # CE trigger
        rows.append((_ts_at(self.DAY, (9, 15), n + 1), 99.5, 100.0, 49.0, 50.0))    # confirms CE -- but NOT latest
        # Low kept comfortably ABOVE 49 (the confirmation candle's own low,
        # still inside the rolling window) so this candle doesn't ALSO
        # register a fresh, genuine PE trigger of its own.
        rows.append((_ts_at(self.DAY, (9, 15), n + 2), 50.0, 60.0, 55.0, 57.0))     # a later closed candle exists
        rows.append((_ts_at(self.DAY, (9, 15), n + 3), 57.0, 58.0, 56.0, 57.0))     # still-forming, dropped
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        sig = script_module.compute_donchian_signal(bars, state, ltp=55.0)

        assert sig is not None
        assert sig.pending_entry_side == "", "a stale (non-latest) confirmation must not dispatch an entry"
        assert state.armed_side == "", "the state machine must still have advanced past the stale confirmation"


# ---------------------------------------------------------------------------
# ITM-floor strike selection (mirrors the corrected backtest's own fix)
# ---------------------------------------------------------------------------
def _make_chain(strikes_premiums: dict, option_type: str, lotsize: int = 75) -> dict:
    key = option_type.lower()
    return {
        "chain": [
            {"strike": strike, key: {"strike": strike, "ltp": premium, "lotsize": lotsize,
                                      "symbol": f"NIFTY{strike:.0f}{option_type}"}}
            for strike, premium in strikes_premiums.items()
        ]
    }


class TestStrikeSelection:
    def test_atm_premium_above_floor_used_directly(self, script_module):
        chain = _make_chain({24900: 80, 25000: 150, 25100: 60}, "CE")
        result = script_module.select_itm_floor_strike(chain, "CE", spot=25000)
        leg, premium, stepped_itm = result
        assert leg["strike"] == 25000
        assert premium == 150
        assert stepped_itm is False

    def test_ce_steps_itm_lower_strikes_when_atm_too_cheap(self, script_module):
        # ATM (25000) premium too low; CE steps to LOWER strikes (ITM).
        chain = _make_chain({24800: 220, 24900: 130, 25000: 40, 25100: 20}, "CE")
        result = script_module.select_itm_floor_strike(chain, "CE", spot=25000)
        leg, premium, stepped_itm = result
        assert leg["strike"] == 24900  # nearest ITM strike clearing the Rs100 floor
        assert premium == 130
        assert stepped_itm is True

    def test_pe_steps_itm_higher_strikes_when_atm_too_cheap(self, script_module):
        # ATM (25000) premium too low; PE steps to HIGHER strikes (ITM).
        chain = _make_chain({24900: 20, 25000: 40, 25100: 130, 25200: 220}, "PE")
        result = script_module.select_itm_floor_strike(chain, "PE", spot=25000)
        leg, premium, stepped_itm = result
        assert leg["strike"] == 25100
        assert premium == 130
        assert stepped_itm is True

    def test_falls_back_to_atm_when_nothing_clears_the_floor(self, script_module):
        chain = _make_chain({24900: 10, 25000: 20, 25100: 5}, "CE")
        result = script_module.select_itm_floor_strike(chain, "CE", spot=25000)
        leg, premium, stepped_itm = result
        assert leg["strike"] == 25000  # falls back to ATM itself, never skips entirely
        assert premium == 20
        assert stepped_itm is False

    def test_itm_walk_is_bounded_by_max_itm_steps(self, script_module):
        # Every candidate within range is too cheap -- must not walk past
        # config.max_itm_steps and must not raise.
        strikes = {25000 - 50 * i: 1.0 for i in range(0, 15)}
        chain = _make_chain(strikes, "CE")
        result = script_module.select_itm_floor_strike(chain, "CE", spot=25000)
        leg, premium, stepped_itm = result
        assert leg["strike"] == 25000
        assert stepped_itm is False


# ---------------------------------------------------------------------------
# SL / target premium check
# ---------------------------------------------------------------------------
class TestSlTarget:
    def test_sl_hit(self, script_module, engine):
        pos = script_module.LegPosition(entry_px=100.0)
        assert engine._sl_or_target_hit(pos, current_premium=130.0) == "sl_hit"
        assert engine._sl_or_target_hit(pos, current_premium=129.9) is None

    def test_target_hit(self, script_module, engine):
        pos = script_module.LegPosition(entry_px=100.0)
        assert engine._sl_or_target_hit(pos, current_premium=70.0) == "target_hit"
        assert engine._sl_or_target_hit(pos, current_premium=70.1) is None

    def test_neither_hit_in_the_middle(self, script_module, engine):
        pos = script_module.LegPosition(entry_px=100.0)
        assert engine._sl_or_target_hit(pos, current_premium=100.0) is None


# ---------------------------------------------------------------------------
# Shared daily cap (confirmed 2026-09-06, cap=6) / single-position-slot
# semantics (regression coverage for the bugs the backtest itself had before
# it was fixed, including the 2026-09-06 entry-cutoff-time bug)
# ---------------------------------------------------------------------------
class TestSharedDailyCapAndSingleSlot:
    def test_trade_count_is_a_single_shared_counter(self, script_module):
        """StrategyState carries ONE trade_count, not one per side --
        confirms the shared-cap design (max 6/day combined CE+PE, since
        only one position can ever be open at a time). Of shared-3/day,
        per-side-3-each, and shared-6/day, this variant had the best
        net-of-cost PnL AND a shallower drawdown than per-side (2026-09-06)."""
        state = script_module.StrategyState()
        assert hasattr(state, "trade_count")
        assert not hasattr(state, "legs"), (
            "this strategy must use a single shared position/counter, not "
            "the per-leg LEG_KEYS dict shape some other scripts in this "
            "project use"
        )

    def test_reset_day_clears_armed_state_and_trade_count(self, script_module, engine):
        engine.store.state.current_day = "2026-08-31"
        engine.store.state.trade_count = 5
        engine.store.state.armed_side = "CE"
        engine.store.state.armed_countdown = 5

        real_today = real_datetime.now(script_module.IST).date().isoformat()
        engine.store.state.current_day = "2026-08-31"  # force a stale day
        engine._reset_day_if_needed()

        assert engine.store.state.trade_count == 0
        assert engine.store.state.armed_side == ""
        assert engine.store.state.armed_countdown == 0
        assert engine.store.state.current_day == real_today

    def test_increment_trade_count_is_shared(self, script_module, engine):
        engine._increment_trade_count("CE")
        engine._increment_trade_count("CE")
        engine._increment_trade_count("PE")
        assert engine.store.state.trade_count == 3

    def test_confirmation_blocked_when_shared_cap_is_reached(self, script_module):
        """A confirmation on EITHER side must be skipped once the SHARED
        trade_count reaches max_trades_per_day (6) -- confirms the cap is
        a combined pool, not tracked independently per side."""
        day = "2026-09-01"
        rows = _filler_bars(day, (9, 15), WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(day, (9, 15), n), 99.5, 150.0, 99.5, 99.5))  # CE trigger
        rows.append((_ts_at(day, (9, 15), n + 1), 99.5, 100.0, 49.0, 50.0))  # CE confirms
        rows.append((_ts_at(day, (9, 15), n + 2), 50.0, 51.0, 49.0, 50.0))  # still-forming, dropped
        bars = _make_bars(rows)

        state = script_module.StrategyState()
        state.trade_count = script_module.config.max_trades_per_day  # already at cap
        sig = script_module.compute_donchian_signal(bars, state, ltp=50.0)

        assert sig is not None
        assert sig.pending_entry_side == "", "confirmation must be skipped once the shared daily cap is reached"

    def test_confirmation_blocked_at_or_after_entry_cutoff(self, script_module):
        """2026-09-06 fix: a confirmation at/after config.entry_end must
        never dispatch an entry -- this is what let 427/3505 backtest
        trades fire past the 15:15 universal exit time before the fix."""
        day = "2026-09-01"
        entry_end = script_module.config.entry_end
        # Build the trigger+confirmation pair landing exactly AT entry_end.
        cutoff_minutes = entry_end.hour * 60 + entry_end.minute
        start_minutes = cutoff_minutes - 2 * (WARMUP_N + 1)  # so the confirmation candle (index n+1) lands exactly at entry_end
        start_hm = (start_minutes // 60, start_minutes % 60)
        rows = _filler_bars(day, start_hm, WARMUP_N)
        n = WARMUP_N
        rows.append((_ts_at(day, start_hm, n), 99.5, 150.0, 99.5, 99.5))       # CE trigger
        rows.append((_ts_at(day, start_hm, n + 1), 99.5, 100.0, 49.0, 50.0))   # CE confirms AT entry_end
        rows.append((_ts_at(day, start_hm, n + 2), 50.0, 51.0, 49.0, 50.0))    # still-forming, dropped
        bars = _make_bars(rows)
        confirm_ts = pd.to_datetime(_ts_at(day, start_hm, n + 1))
        assert confirm_ts.time() >= entry_end, "test setup must land the confirmation at/after entry_end"

        state = script_module.StrategyState()
        sig = script_module.compute_donchian_signal(bars, state, ltp=50.0)

        assert sig is not None
        assert sig.pending_entry_side == "", "a confirmation at/after entry_end must never dispatch an entry"


# ---------------------------------------------------------------------------
# 2026-09-07 fixes: "2m" is not a broker-supported interval (confirmed live --
# client.history(interval="2m") returned HTTP 500), and the candle-boundary
# helper was anchored to midnight instead of the 09:15 session open (a bug
# already found and fixed live in Nifty_Sensex_VWAP_NoHA_Intraday_1 for the
# exact same reason: 09:15 = minute 555, an ODD number, so a midnight-anchored
# `(total_minutes // 2) * 2` can never land on a real 2-min bucket's start).
# ---------------------------------------------------------------------------
class TestOneMinuteFetchAndBoundaryAnchor:
    def test_resample_to_bars_buckets_1m_into_2min_anchored_at_0915(self, script_module):
        # 6 one-minute bars starting at 09:15 -> should collapse into 3
        # two-minute buckets: [09:15-09:17), [09:17-09:19), [09:19-09:21).
        rows = [
            ("2026-09-01 09:15:00", 100.0, 101.0, 99.0, 100.5),
            ("2026-09-01 09:16:00", 100.5, 102.0, 100.0, 101.0),
            ("2026-09-01 09:17:00", 101.0, 103.0, 100.5, 102.0),
            ("2026-09-01 09:18:00", 102.0, 104.0, 101.5, 103.0),
            ("2026-09-01 09:19:00", 103.0, 105.0, 102.5, 104.0),
            ("2026-09-01 09:20:00", 104.0, 106.0, 103.5, 105.0),
        ]
        df = _make_bars(rows)
        bars = script_module.resample_to_bars(df, 2)

        assert list(bars.index.strftime("%H:%M")) == ["09:15", "09:17", "09:19"]
        # First bucket (09:15-09:17): open=first(100.0), high=max(102.0), low=min(99.0), close=last(101.0)
        first = bars.iloc[0]
        assert first["open"] == 100.0
        assert first["high"] == 102.0
        assert first["low"] == 99.0
        assert first["close"] == 101.0

    def test_resample_to_bars_does_not_drop_the_last_bucket(self, script_module):
        """resample_to_bars() itself must NOT drop the still-forming last
        bucket -- compute_donchian_signal already does that (single source
        of truth); dropping it in both places would silently discard one
        extra real candle every refresh."""
        rows = [
            ("2026-09-01 09:15:00", 100.0, 101.0, 99.0, 100.5),
            ("2026-09-01 09:16:00", 100.5, 102.0, 100.0, 101.0),
        ]
        df = _make_bars(rows)
        bars = script_module.resample_to_bars(df, 2)
        assert len(bars) == 1  # the single (still-forming) 09:15 bucket, NOT dropped here

    def test_candle_boundary_anchors_to_0915_not_midnight(self, script_module, monkeypatch):
        """A midnight-anchored boundary can only ever land on EVEN minutes
        for a 2-min bucket; 09:15 is an ODD minute (555), so every genuine
        bucket start (09:15, 09:17, 09:19, ...) is odd too -- confirming
        the fixed helper returns odd-minute boundaries proves it's anchored
        to 09:15, not midnight."""
        import datetime as real_datetime_module

        class _FrozenDatetime(real_datetime_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime_module.datetime(2026, 9, 7, 9, 18, 42, tzinfo=tz)

        monkeypatch.setattr(script_module, "datetime", _FrozenDatetime)
        boundary = script_module._current_candle_boundary(2)
        assert boundary.hour == 9
        assert boundary.minute == 17, "09:18:42 falls in the [09:17,09:19) bucket, which starts at :17"

    def test_candle_boundary_matches_resample_to_bars_bucket_starts(self, script_module):
        """The boundary helper and resample_to_bars() must agree on where
        buckets start -- generate a day of 1m bars, resample them, and
        confirm every resulting bucket's own start timestamp is achievable
        by _current_candle_boundary (i.e. lands on an odd minute >= 555)."""
        rows = [(_ts_at("2026-09-01", (9, 15), i), 100.0, 101.0, 99.0, 100.0) for i in range(0, 20, 1)]
        df = _make_bars(rows)
        bars = script_module.resample_to_bars(df, 2)
        for ts in bars.index:
            total_minutes = ts.hour * 60 + ts.minute
            assert (total_minutes - 555) % 2 == 0, f"bucket start {ts} is not 09:15-anchored on an even offset"

    def test_compute_donchian_signal_handles_tz_aware_broker_timestamps(self, script_module):
        """2026-09-08 fix: confirmed live -- `TypeError: can't compare
        offset-naive and offset-aware datetimes`. client.history() returns
        tz-AWARE (+05:30) timestamps in production; this test builds bars
        with that same tz-aware index (unlike every other test in this file,
        which uses naive fixtures and so never exercised the mismatch) and
        confirms a second call -- simulating the next scheduler cycle,
        with state.last_processed_candle_key already set from the first
        call -- does not raise."""
        import pytz
        ist = pytz.timezone("Asia/Kolkata")

        def _aware_bars(rows):
            idx = pd.to_datetime([r[0] for r in rows]).tz_localize(ist)
            return pd.DataFrame(
                {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                 "low": [r[3] for r in rows], "close": [r[4] for r in rows]},
                index=idx,
            )

        rows = _filler_bars("2026-09-01", (9, 15), WARMUP_N + 3)
        bars = _aware_bars(rows)

        state = script_module.StrategyState()
        sig1 = script_module.compute_donchian_signal(bars, state, ltp=99.5)
        assert sig1 is not None
        assert state.last_processed_candle_key  # confirms candle_key round-trips through the aware index

        # Second call, one more closed candle appended -- this is exactly
        # where the bug fired live: comparing the new tz-aware bar against
        # the now-set (tz-aware, parsed back from candle_key) last_processed_boundary.
        rows2 = rows + [(_ts_at("2026-09-01", (9, 15), WARMUP_N + 3), 99.5, 100.0, 99.3, 99.5)]
        bars2 = _aware_bars(rows2)
        sig2 = script_module.compute_donchian_signal(bars2, state, ltp=99.5)
        assert sig2 is not None  # must not raise TypeError
