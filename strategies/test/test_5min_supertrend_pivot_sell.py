"""
Unit tests for
strategies/deployed/Nifty_5Min_SupertrendEMA_PivotSell_1_20260829000000.py --
the NIFTY 5-min Supertrend/EMA9/Pivot naked option-selling strategy ported
from data23_to_26/backtest_nifty_5min_supertrend_pivot_optionsell.py.

No live broker connection is used anywhere in this file -- every
client/price_stream dependency is a stub or MagicMock, and network-side
calls (append_trade_log, notify_trade_closed, push_leg_error,
check_force_exit, check_pending_action, ack_pending_action,
ack_force_exit_complete) are monkeypatched out, so nothing here touches
disk or the loopback strategy_reporting port. Entry/exit condition and
strike-selection tests use fixture values chosen to hit each named
Setup A/B/C branch individually (mirroring the backtest's own verified
rules) -- see the module docstring of the deployed script itself for the
exact rule text each test exercises.
"""

import importlib.util
import sys
from datetime import time as dtime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_5Min_SupertrendEMA_PivotSell_1_20260829000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_candle_boundary_refresh.py / test_leaps_options_sell.py's
    identical helper: the repo root is itself importable as a package
    literally named `openalgo`, which can shadow the pip-installed SDK
    under pytest's rootdir-on-sys.path behavior."""
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
    spec = importlib.util.spec_from_file_location("nifty_5min_st_ema_pivot_sell_script", SCRIPT_PATH)
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


def _make_signal(script_module, **overrides):
    """Baseline InstrumentSignal with every mandatory/setup condition FALSE
    by default -- each test overrides only the fields its own condition
    needs, so an assertion failure clearly identifies which rule broke."""
    base = dict(
        r1=100.0, s1=80.0, r2=110.0, s2=70.0,
        last_open=100.0, last_close=100.0, last_high=101.0, last_low=99.0,
        last_supertrend=95.0, last_ema9=100.0,
        prev_open=100.0, prev_close=100.0, prev_supertrend=95.0,
        ltp=100.0, candle_key="2026-08-29 10:15:00+05:30",
    )
    base.update(overrides)
    return script_module.InstrumentSignal(**base)


# ---------------------------------------------------------------------------
# Entry: mandatory conditions gate every setup
# ---------------------------------------------------------------------------
class TestMandatoryConditions:
    def test_pe_mandatory_false_blocks_entry_even_with_setup_a_true(self, script_module, engine):
        # Setup A true (last_close > r1 and last_low < r1) but mandatory
        # condition 2 (last_close > last_supertrend) is false.
        signal = _make_signal(
            script_module,
            last_close=105.0, last_open=104.0, r1=100.0, last_low=99.0,
            last_supertrend=110.0,  # last_close < last_supertrend -- mandatory fails
            ltp=108.0, last_high=106.0,
        )
        assert engine._entry_condition("PE", signal, running_low=None, running_high=None) is None

    def test_ce_mandatory_false_blocks_entry_even_with_setup_a_true(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_close=75.0, last_open=76.0, s1=80.0, last_high=81.0,
            last_supertrend=70.0,  # last_close > last_supertrend -- mandatory fails
            ltp=72.0, last_low=74.0,
        )
        assert engine._entry_condition("CE", signal, running_low=None, running_high=None) is None


# ---------------------------------------------------------------------------
# Entry: Setup A/B/C, each side
# ---------------------------------------------------------------------------
class TestSetupA:
    def test_pe_setup_a_pivot_rejection(self, script_module, engine):
        # mandatory: Close>Open, Close>ST, ltp>min(Close+5,High+2)
        # setup A: Close>R1 and Low<R1
        signal = _make_signal(
            script_module,
            last_open=98.0, last_close=102.0, last_supertrend=95.0,
            r1=100.0, last_low=99.0, last_high=103.0,
            ltp=107.0,  # > min(102+5=107, 103+2=105) -> > 105
        )
        assert engine._entry_condition("PE", signal, running_low=None, running_high=None) == "A"

    def test_ce_setup_a_pivot_rejection(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=102.0, last_close=98.0, last_supertrend=105.0,
            s1=100.0, last_high=101.0, last_low=97.0,
            ltp=92.0,  # < max(98-5=93, 97-2=95) -> < 93... use 92
        )
        assert engine._entry_condition("CE", signal, running_low=None, running_high=None) == "A"


class TestSetupB:
    def test_pe_setup_b_two_candle_continuation(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=98.0, last_close=102.0, last_supertrend=95.0,
            r1=100.0, last_low=101.0, last_high=103.0,  # Low NOT < r1 -- Setup A false
            last_ema9=99.0,   # last_close(102) > ema9 -- needed for setup B
            prev_close=97.0, prev_open=99.0, prev_supertrend=90.0,  # prev_close>prev_ST, prev_close<prev_open
            ltp=108.0,
        )
        assert engine._entry_condition("PE", signal, running_low=None, running_high=None) == "B"

    def test_ce_setup_b_two_candle_continuation(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=102.0, last_close=98.0, last_supertrend=105.0,
            s1=100.0, last_high=99.0, last_low=97.0,  # High NOT > s1 -- Setup A false
            last_ema9=101.0,  # last_close(98) < ema9
            prev_close=103.0, prev_open=101.0, prev_supertrend=110.0,  # prev_close<prev_ST, prev_close>prev_open
            ltp=92.0,
        )
        assert engine._entry_condition("CE", signal, running_low=None, running_high=None) == "B"


class TestSetupC:
    def test_pe_setup_c_s2_spike_calm_prior_candle(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=98.0, last_close=102.0, last_supertrend=95.0,
            r1=100.0, last_low=101.0, last_high=103.0,  # Setup A false (Low not < r1)
            last_ema9=105.0,  # last_close(102) < ema9 -- Setup B's own ema9 leg would fail too
            prev_close=97.0, prev_open=99.0, prev_supertrend=90.0,  # would satisfy B except ema9 check below
            s2=70.0,
            ltp=108.0,
        )
        # Setup C: running_low < s2 AND last_low > last_ema9 -- force B false by
        # keeping last_close < last_ema9 (already set above), then supply a
        # running_low under s2 and last_low above ema9.
        signal.last_low = 106.0  # > last_ema9 (105)
        assert engine._entry_condition(
            "PE", signal, running_low=65.0, running_high=None
        ) == "C"

    def test_pe_setup_c_false_when_running_low_missing(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=98.0, last_close=102.0, last_supertrend=95.0,
            r1=100.0, last_low=106.0, last_high=103.0,
            last_ema9=105.0,
            prev_close=100.0, prev_open=100.0, prev_supertrend=100.0,  # setup B mandatory pieces false
            s2=70.0,
            ltp=108.0,
        )
        assert engine._entry_condition(
            "PE", signal, running_low=None, running_high=None
        ) is None

    def test_ce_setup_c_r2_spike_calm_prior_candle(self, script_module, engine):
        signal = _make_signal(
            script_module,
            last_open=102.0, last_close=98.0, last_supertrend=105.0,
            s1=100.0, last_high=99.0, last_low=97.0,  # Setup A false
            last_ema9=95.0,  # last_close(98) > ema9 -- Setup B's ema9 leg would fail
            prev_close=103.0, prev_open=101.0, prev_supertrend=110.0,
            r2=110.0,
            ltp=92.0,
        )
        signal.last_high = 94.0  # < last_ema9 (95)
        assert engine._entry_condition(
            "CE", signal, running_low=None, running_high=130.0
        ) == "C"


# ---------------------------------------------------------------------------
# Exit conditions (spot-only technical conditions; premium stop is a
# separate run_cycle-level check, tested via _premium_stop_hit directly)
# ---------------------------------------------------------------------------
class TestTechnicalExit:
    def test_pe_exit_on_supertrend_flip(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=105.0, prev_supertrend=100.0,   # prev: close > ST (bullish)
            last_close=95.0, last_supertrend=100.0,    # last: close < ST (flipped)
        )
        assert engine._technical_exit_condition("PE", signal) is True

    def test_pe_exit_on_preemptive_live_breach(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,     # no flip yet
            last_close=105.0, last_supertrend=100.0,    # still bullish per closed candle
            ltp=89.0,   # < last_supertrend(100) - 10 = 90
        )
        assert engine._technical_exit_condition("PE", signal) is True

    def test_pe_exit_on_ema9_and_pivot_loss(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,
            last_close=90.0, last_supertrend=80.0,   # no flip (still > ST)
            ltp=95.0,                                 # no pre-emptive breach
            last_ema9=95.0, r1=95.0,                 # last_close(90) < both
        )
        assert engine._technical_exit_condition("PE", signal) is True

    def test_pe_no_exit_when_nothing_fires(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,
            last_close=105.0, last_supertrend=95.0,
            ltp=110.0, last_ema9=95.0, r1=90.0,
        )
        assert engine._technical_exit_condition("PE", signal) is False

    def test_ce_exit_on_supertrend_flip(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=95.0, prev_supertrend=100.0,   # prev: close < ST (bearish)
            last_close=105.0, last_supertrend=100.0,  # last: close > ST (flipped)
        )
        assert engine._technical_exit_condition("CE", signal) is True


class TestPeSetupCExemptFromEma9PivotLoss:
    """A PE position opened via Setup C skips the ema9_pivot_loss leg of
    the exit condition -- confirmed against 3 years of backtest data (see
    _technical_exit_condition's own docstring). Every other PE setup, and
    CE under every setup including its own Setup C mirror, still use it."""

    def test_pe_setup_c_does_not_exit_on_ema9_pivot_loss(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,   # no flip
            last_close=90.0, last_supertrend=80.0,    # no flip (still > ST)
            ltp=95.0,                                  # no pre-emptive breach
            last_ema9=95.0, r1=95.0,                  # would trigger ema9_pivot_loss for A/B
        )
        assert engine._technical_exit_condition("PE", signal, entry_setup="C") is False

    def test_pe_setup_a_still_exits_on_ema9_pivot_loss(self, script_module, engine):
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,
            last_close=90.0, last_supertrend=80.0,
            ltp=95.0,
            last_ema9=95.0, r1=95.0,
        )
        assert engine._technical_exit_condition("PE", signal, entry_setup="A") is True

    def test_pe_setup_c_still_exits_on_other_conditions(self, script_module, engine):
        # Setup C exemption only removes ema9_pivot_loss -- st_flip and the
        # pre-emptive breach still apply to it.
        flip_signal = _make_signal(
            script_module,
            prev_close=105.0, prev_supertrend=100.0,
            last_close=95.0, last_supertrend=100.0,
        )
        assert engine._technical_exit_condition("PE", flip_signal, entry_setup="C") is True

        preempt_signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,
            last_close=105.0, last_supertrend=100.0,
            ltp=89.0,
        )
        assert engine._technical_exit_condition("PE", preempt_signal, entry_setup="C") is True

    def test_ce_setup_c_still_exits_on_ema9_pivot_loss(self, script_module, engine):
        # The exemption is PE-only -- CE's own Setup C mirror is unaffected.
        signal = _make_signal(
            script_module,
            prev_close=95.0, prev_supertrend=100.0,   # no flip
            last_close=105.0, last_supertrend=110.0,  # no flip (still < ST)
            ltp=100.0,                                 # no pre-emptive breach
            last_ema9=100.0, s1=100.0,                # last_close(105) > both
        )
        assert engine._technical_exit_condition("CE", signal, entry_setup="C") is True

    def test_default_entry_setup_behaves_like_non_c(self, script_module, engine):
        # entry_setup defaults to "" (e.g. a leg resumed from state.json
        # written before this field existed) -- must NOT be silently
        # treated as Setup C's exemption.
        signal = _make_signal(
            script_module,
            prev_close=100.0, prev_supertrend=90.0,
            last_close=90.0, last_supertrend=80.0,
            ltp=95.0,
            last_ema9=95.0, r1=95.0,
        )
        assert engine._technical_exit_condition("PE", signal) is True


class TestPremiumStop:
    def test_stop_hit_above_100_uses_1_2x(self, script_module, engine):
        pos = script_module.LegPosition(entry_px=150.0)
        assert engine._premium_stop_hit(pos, current_premium=180.0) is True
        assert engine._premium_stop_hit(pos, current_premium=179.0) is False

    def test_stop_hit_at_or_below_100_uses_1_5x(self, script_module, engine):
        pos = script_module.LegPosition(entry_px=100.0)
        assert engine._premium_stop_hit(pos, current_premium=150.0) is True
        assert engine._premium_stop_hit(pos, current_premium=149.0) is False


# ---------------------------------------------------------------------------
# select_capped_strike: ATM band + fallback walk (ported from the backtest's
# own select_capped_strike, same semantics)
# ---------------------------------------------------------------------------
def _make_chain(rows):
    """rows: list of (strike, pe_ltp, ce_ltp, lotsize)."""
    chain_rows = []
    for strike, pe_ltp, ce_ltp, lotsize in rows:
        row = {"strike": strike}
        if pe_ltp is not None:
            row["pe"] = {"symbol": f"NIFTY{strike}PE", "ltp": pe_ltp, "lotsize": lotsize}
        if ce_ltp is not None:
            row["ce"] = {"symbol": f"NIFTY{strike}CE", "ltp": ce_ltp, "lotsize": lotsize}
        chain_rows.append(row)
    return {"status": "success", "chain": chain_rows}


class TestSelectCappedStrike:
    def test_atm_within_band_selected_directly(self, script_module):
        chain = _make_chain([
            (24900, 60.0, 40.0, 65), (25000, 50.0, 45.0, 65), (25100, 30.0, 55.0, 65),
        ])
        leg, premium, used_fallback = script_module.select_capped_strike(chain, "PE", spot=25000)
        assert leg["strike"] == 25000
        assert premium == 50.0
        assert used_fallback is False

    def test_atm_too_rich_walks_further_otm_for_pe(self, script_module):
        # PE walks toward DECREASING strikes when ATM premium >= 120. 24900's
        # premium (150) still exceeds 120, so the walk must continue past it
        # to 24800 (90, in-band) -- exercises a multi-step walk, not just one.
        chain = _make_chain([
            (24800, 90.0, 20.0, 65), (24900, 150.0, 30.0, 65), (25000, 200.0, 45.0, 65),
        ])
        leg, premium, used_fallback = script_module.select_capped_strike(chain, "PE", spot=25000)
        assert leg["strike"] == 24800
        assert premium == 90.0
        assert used_fallback is True

    def test_atm_too_rich_walks_further_otm_for_ce(self, script_module):
        # CE walks toward INCREASING strikes when ATM premium >= 120. 25100's
        # premium (150) still exceeds 120, so the walk must continue past it
        # to 25200 (90, in-band).
        chain = _make_chain([
            (25000, 45.0, 200.0, 65), (25100, 30.0, 150.0, 65), (25200, 20.0, 90.0, 65),
        ])
        leg, premium, used_fallback = script_module.select_capped_strike(chain, "CE", spot=25000)
        assert leg["strike"] == 25200
        assert premium == 90.0
        assert used_fallback is True

    def test_atm_too_cheap_no_fallback_returns_none(self, script_module):
        chain = _make_chain([(25000, 15.0, 45.0, 65)])
        assert script_module.select_capped_strike(chain, "PE", spot=25000) is None

    def test_no_candidate_in_band_returns_none(self, script_module):
        chain = _make_chain([
            (24900, 200.0, 20.0, 65), (25000, 250.0, 45.0, 65),
        ])
        assert script_module.select_capped_strike(chain, "PE", spot=25000) is None


# ---------------------------------------------------------------------------
# _enter_leg's chain-staleness guard -- added after code review flagged it
# was dropped relative to the sibling script's equivalent check.
# ---------------------------------------------------------------------------
class TestEnterLegChainStalenessGuard:
    def test_stale_chain_skips_entry_and_evicts_cache(self, script_module, engine):
        inst = script_module.INSTRUMENTS[0]
        chain = _make_chain([
            (24900, 60.0, 40.0, 65), (25000, 50.0, 45.0, 65), (25100, 30.0, 55.0, 65),
        ])
        engine._chain_cache[inst.name] = chain

        # Spot has moved 300 points away from the nearest cached strike
        # (25100), while the strike step is only 100 -- the cache must be
        # treated as stale and NOT used to select an entry strike.
        engine._enter_leg("NIFTY_PE", inst, "PE", spot=25400.0)

        assert engine.store.state.legs["NIFTY_PE"].position.symbol == ""
        assert inst.name not in engine._chain_cache

    def test_fresh_chain_still_enters_normally(self, script_module, engine, monkeypatch):
        inst = script_module.INSTRUMENTS[0]
        chain = _make_chain([
            (24900, 60.0, 40.0, 65), (25000, 50.0, 45.0, 65), (25100, 30.0, 55.0, 65),
        ])
        engine._chain_cache[inst.name] = chain
        monkeypatch.setattr(script_module, "place", lambda *a, **k: "ORD1")
        # Don't actually dispatch the async fill-watcher against a MagicMock
        # client -- out of scope for this test (order placement/fill flow
        # is covered elsewhere) and would otherwise race the fixture's own
        # executor teardown.
        monkeypatch.setattr(engine._fill_executor, "submit", lambda *a, **k: None)

        engine._enter_leg("NIFTY_PE", inst, "PE", spot=25000.0)

        assert engine.store.state.legs["NIFTY_PE"].position.symbol == "NIFTY25000PE"
        assert inst.name in engine._chain_cache


# ---------------------------------------------------------------------------
# Daily trade-count cap + reset
# ---------------------------------------------------------------------------
class TestDailyCapAndReset:
    def test_reset_day_clears_trade_count_and_caches(self, script_module, engine):
        engine.store.state.current_day = "2020-01-01"  # force a "new day" on next check
        engine.store.state.legs["NIFTY_PE"].trade_count = 3
        engine.store.state.legs["NIFTY_CE"].trade_count = 2
        engine._expiry_cache["NIFTY"] = "29AUG26"
        engine._chain_cache["NIFTY"] = {"status": "success", "chain": []}
        engine._running_extreme["NIFTY"] = (None, 1.0, 2.0)

        engine._reset_day_if_needed()

        assert engine.store.state.legs["NIFTY_PE"].trade_count == 0
        assert engine.store.state.legs["NIFTY_CE"].trade_count == 0
        assert engine._expiry_cache == {}
        assert engine._chain_cache == {}
        assert engine._running_extreme == {}

    def test_max_trades_per_leg_per_day_default(self, script_module):
        assert script_module.config.max_trades_per_leg_per_day == 3


# ---------------------------------------------------------------------------
# Config: confirmed design decisions (product/window/premium band/strike count)
# ---------------------------------------------------------------------------
class TestConfirmedConfig:
    def test_product_is_nrml_no_broker_backstop(self, script_module):
        assert script_module.config.product == "NRML"

    def test_entry_window_matches_backtest(self, script_module):
        assert script_module.config.entry_start == dtime(9, 20)
        assert script_module.config.entry_end == dtime(14, 45)

    def test_universal_exit_time(self, script_module):
        assert script_module.config.universal_exit_time == dtime(15, 15)

    def test_premium_band(self, script_module):
        assert script_module.config.premium_filter_low == 20.0
        assert script_module.config.premium_filter_high == 120.0

    def test_strike_count(self, script_module):
        assert script_module.config.strike_count == 10

    def test_supertrend_and_ema_params(self, script_module):
        assert script_module.config.supertrend_period == 6
        assert script_module.config.supertrend_multiplier == 3.0
        assert script_module.config.ema_period == 9


# ---------------------------------------------------------------------------
# Running Low[0]/High[0] bucket tracker
# ---------------------------------------------------------------------------
class TestRunningExtreme:
    def test_tracks_min_max_within_same_bucket(self, script_module, engine, monkeypatch):
        fixed_boundary = script_module.datetime(2026, 8, 29, 10, 15, tzinfo=script_module.IST)
        monkeypatch.setattr(script_module, "_current_candle_boundary", lambda mins: fixed_boundary)

        engine._update_running_extreme("NIFTY", 100.0)
        engine._update_running_extreme("NIFTY", 95.0)
        engine._update_running_extreme("NIFTY", 103.0)

        lo, hi = engine._get_running_extreme("NIFTY")
        assert lo == 95.0
        assert hi == 103.0

    def test_new_bucket_resets_extreme(self, script_module, engine, monkeypatch):
        b1 = script_module.datetime(2026, 8, 29, 10, 15, tzinfo=script_module.IST)
        b2 = script_module.datetime(2026, 8, 29, 10, 20, tzinfo=script_module.IST)

        monkeypatch.setattr(script_module, "_current_candle_boundary", lambda mins: b1)
        engine._update_running_extreme("NIFTY", 100.0)
        engine._update_running_extreme("NIFTY", 90.0)

        monkeypatch.setattr(script_module, "_current_candle_boundary", lambda mins: b2)
        engine._update_running_extreme("NIFTY", 105.0)

        lo, hi = engine._get_running_extreme("NIFTY")
        assert lo == 105.0
        assert hi == 105.0

    def test_none_ltp_is_ignored(self, script_module, engine):
        engine._running_extreme.clear()
        engine._update_running_extreme("NIFTY", None)
        assert engine._get_running_extreme("NIFTY") == (None, None)


# ---------------------------------------------------------------------------
# _force_close_stale_day_legs: cross-day safety net (NRML has no broker
# backstop) -- added after code review flagged that _past_universal_exit()
# alone can't catch a leg still open across a day boundary.
# ---------------------------------------------------------------------------
class TestForceCloseStaleDayLegs:
    def test_closes_a_leg_entered_on_a_prior_day(self, script_module, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_exit_leg", lambda leg_key, inst, reason: calls.append((leg_key, reason)))

        engine.store.state.legs["NIFTY_PE"].position = script_module.LegPosition(
            symbol="NIFTY24900PE", quantity=65, entry_time="2020-01-01T10:00:00+05:30",
        )
        engine._force_close_stale_day_legs()

        assert calls == [("NIFTY_PE", "stale_day_force_close")]

    def test_leaves_a_leg_entered_today_alone(self, script_module, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_exit_leg", lambda leg_key, inst, reason: calls.append((leg_key, reason)))

        today_iso = script_module.datetime.now(script_module.IST).isoformat()
        engine.store.state.legs["NIFTY_PE"].position = script_module.LegPosition(
            symbol="NIFTY24900PE", quantity=65, entry_time=today_iso,
        )
        engine._force_close_stale_day_legs()

        assert calls == []

    def test_does_not_force_through_an_unresolved_error(self, script_module, engine, monkeypatch):
        calls = []
        monkeypatch.setattr(engine, "_exit_leg", lambda leg_key, inst, reason: calls.append((leg_key, reason)))

        engine.store.state.legs["NIFTY_CE"].position = script_module.LegPosition(
            symbol="NIFTY25100CE", quantity=65, entry_time="2020-01-01T10:00:00+05:30",
            error_state="exit_failed", error_kind="terminal",
        )
        engine._force_close_stale_day_legs()

        assert calls == []

    def test_a_raised_exception_does_not_stop_the_other_leg(self, script_module, engine, monkeypatch):
        calls = []

        def _flaky_exit(leg_key, inst, reason):
            if leg_key == "NIFTY_PE":
                raise RuntimeError("boom")
            calls.append((leg_key, reason))

        monkeypatch.setattr(engine, "_exit_leg", _flaky_exit)

        for leg_key in ("NIFTY_PE", "NIFTY_CE"):
            engine.store.state.legs[leg_key].position = script_module.LegPosition(
                symbol=f"{leg_key}_SYM", quantity=65, entry_time="2020-01-01T10:00:00+05:30",
            )
        engine._force_close_stale_day_legs()

        assert calls == [("NIFTY_CE", "stale_day_force_close")]


# ---------------------------------------------------------------------------
# fetch_daily_pivot: exact classic floor-pivot formula parity with the
# validated backtest (PP=(H+L+C)/3, R1=2PP-L, S1=2PP-H, R2=PP+(H-L), S2=PP-(H-L))
# ---------------------------------------------------------------------------
class TestDailyPivotFormula:
    def test_formula_matches_backtest_exactly(self, script_module):
        import pandas as pd

        # Daily history includes TODAY's still-forming bar as the last row,
        # which fetch_daily_pivot() drops -- so the target previous-day
        # H/L/C must be the SECOND-to-last row, not the last.
        h, l, c = 25100.0, 24900.0, 25000.0
        df = pd.DataFrame(
            {"high": [24800.0, h, 25200.0], "low": [24700.0, l, 25050.0], "close": [24750.0, c, 25150.0]},
            index=pd.to_datetime(["2026-08-27", "2026-08-28", "2026-08-29"]),
        )
        client = MagicMock()
        client.history.return_value = df
        inst = script_module.INSTRUMENTS[0]

        result = script_module.fetch_daily_pivot(client, inst)
        assert result is not None
        r1, s1, r2, s2 = result

        pp = (h + l + c) / 3.0
        expected_r1 = 2 * pp - l
        expected_s1 = 2 * pp - h
        expected_r2 = pp + (h - l)
        expected_s2 = pp - (h - l)
        assert r1 == pytest.approx(expected_r1)
        assert s1 == pytest.approx(expected_s1)
        assert r2 == pytest.approx(expected_r2)
        assert s2 == pytest.approx(expected_s2)
