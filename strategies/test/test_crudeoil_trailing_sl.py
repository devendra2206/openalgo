"""
Regression tests for the 2026-08-13 (third pass) trailing SL redesign in
MCX_CrudeOil_EMA34_RSI_ADX_Intraday_1 (`compute_trailing_sl_price`), the
same-day `max_trades_per_leg_per_day` bump, and the 2026-08-18 (fourth
pass) tier-1 redesign in `_check_stop_loss` itself.

Trailing SL is three tiers, driven entirely by Config defaults:
  - below sl_guard_activation_pct (10%): tier 1 -- see below, no longer a
    single flat premium floor
  - 10%-25% (sl_trail_pct): single flat guard at sl_guard_locked_pct (-10%)
  - 25% and up: locks one sl_trail_pct (25%) step behind the highest step
    crossed, until the LOCKED value would reach
    sl_trail_tighten_threshold_pct (100%), then narrows to
    sl_trail_step_late (10%) for the rest of the trade.

Tier 1 (2026-08-18): `_check_stop_loss` closes the leg on whichever of TWO
independent checks caps the loss to LESS -- a STRUCTURAL stop (futures
crossing the entry-trigger candle's own low (CE) / high (PE), captured at
entry into LegPosition.structural_stop_futures_px) or a flat sl_initial_pct
(15%) premium floor. `compute_trailing_sl_price` itself still returns
entry_px*(1+sl_initial_pct) for this tier (used as the fallback when
structural_stop_futures_px is unset, e.g. a leg resumed from an older
state.json) -- the OR-of-two-checks logic lives in `_check_stop_loss`,
not in that pure function, so it needs its own tests exercising
`_check_stop_loss` directly (a fake price_stream/store, no live
process/network/broker calls).

Every tier boundary must be strictly increasing (the SL never loosens on a
pullback, since it's driven by highest-ever profit, not current profit).

Imports the ACTUAL strategy module -- no live process, no network, no
broker calls; `compute_trailing_sl_price` is a pure function of
(entry_px, highest_profit_pct) plus module-level `config`.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "MCX_CrudeOil_EMA34_RSI_ADX_Intraday_1_20260811103000.py"
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
    spec = importlib.util.spec_from_file_location("crudeoil_ema34_adx_sl_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


ENTRY_PX = 259.4


def test_below_guard_activation_stays_at_initial_floor(script_module):
    # -15% as of 2026-08-18 (was -25%) -- compute_trailing_sl_price's
    # tier-1 return value is now only the FALLBACK used by
    # _check_stop_loss when structural_stop_futures_px is unset; see the
    # module docstring above and the _check_stop_loss-level tests below
    # for the actual OR-of-two-checks tier-1 behavior.
    for profit in (0.0, 0.05, 0.099):
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        assert sl == pytest.approx(ENTRY_PX * 0.85), f"FAIL at profit={profit}"
    print("Below 10% profit -> initial -15% floor PASS")


def test_guard_zone_locks_flat_minus_10_pct(script_module):
    for profit in (0.10, 0.15, 0.20, 0.2499):
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        assert sl == pytest.approx(ENTRY_PX * 0.90), f"FAIL at profit={profit}"
    print("10%-25% profit -> flat -10% guard PASS")


def test_exactly_25_pct_locks_breakeven(script_module):
    sl = script_module.compute_trailing_sl_price(ENTRY_PX, 0.25)
    assert sl == pytest.approx(ENTRY_PX * 1.00), "FAIL: expected breakeven at exactly 25% profit"
    print("Exactly 25% profit -> breakeven PASS")


def test_25_pct_step_region(script_module):
    cases = {
        0.30: 1.00,   # 25%-50% band -> locks 0%
        0.50: 1.25,   # 50%-75% band -> locks +25%
        0.74: 1.25,
        0.75: 1.50,   # 75%-100% band -> locks +50%
        0.99: 1.50,
    }
    for profit, expected_mult in cases.items():
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        assert sl == pytest.approx(ENTRY_PX * expected_mult), f"FAIL at profit={profit}"
    print("25% step region matches expected lock levels PASS")


def test_switch_to_10_pct_steps_at_100_pct_profit(script_module):
    # Just below the switch (highest_profit_pct < 100%): still 25%-step
    # formula, locks +50% (75%-100% band).
    sl_before = script_module.compute_trailing_sl_price(ENTRY_PX, 0.99)
    assert sl_before == pytest.approx(ENTRY_PX * 1.50)

    # At/after the switch (highest_profit_pct >= 100%): 10%-step formula.
    cases = {
        1.00: 1.90,   # 100%-110% band -> locks +90% (10-pt steps begin)
        1.05: 1.90,
        1.10: 2.00,   # 110%-120% band -> locks +100%
        1.20: 2.10,
        1.50: 2.40,
    }
    for profit, expected_mult in cases.items():
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        assert sl == pytest.approx(ENTRY_PX * expected_mult), f"FAIL at profit={profit}"

    # The switch itself must not lower the SL: value right at the switch
    # must be >= the value just before it.
    sl_at_switch = script_module.compute_trailing_sl_price(ENTRY_PX, 1.00)
    assert sl_at_switch >= sl_before, "FAIL: tier switch loosened the SL"
    print("Switch to 10%-step region at +100% profit is ratchet-safe PASS")


def test_ratchet_never_loosens_across_all_tiers(script_module):
    """highest_profit_pct is itself a ratchet variable upstream (never
    decreases once set) -- but as a direct regression guard on the
    function's own monotonicity, walk an increasing sequence spanning
    every tier and confirm the returned SL never decreases."""
    profits = [0.0, 0.05, 0.10, 0.18, 0.24, 0.25, 0.40, 0.50, 0.75, 0.99,
               1.00, 1.10, 1.30, 2.00]
    prev_sl = None
    for profit in profits:
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        if prev_sl is not None:
            assert sl >= prev_sl, f"FAIL: SL decreased at profit={profit} ({sl} < {prev_sl})"
        prev_sl = sl
    print("SL is non-decreasing across the full profit range PASS")


def test_max_trades_per_leg_per_day_is_5(script_module):
    assert script_module.config.max_trades_per_leg_per_day == 5, (
        "FAIL: expected max_trades_per_leg_per_day == 5 (raised from 3 on 2026-08-13)"
    )
    print("max_trades_per_leg_per_day == 5 PASS")


# --------------------------------------------------------------------------
# 2026-08-18 (fourth pass): _check_stop_loss's own tier-1 OR-of-two-checks
# logic (structural futures-level break OR a 15% premium floor, whichever
# fires first) -- not covered by the pure-function tests above, since the
# OR logic lives in _check_stop_loss itself, not compute_trailing_sl_price.
# --------------------------------------------------------------------------

class _FakePriceStream:
    """Minimal price_stream stand-in -- get_ltp(symbol, exchange, max_age)
    returns whatever was seeded for that symbol, or None (forcing
    _check_stop_loss's REST fallback, which is None'd out in these tests
    since ltp_client is never set -- see fetch_symbol_ltp's own signature,
    unused here by construction)."""
    def __init__(self, prices: dict):
        self.prices = prices

    def get_ltp(self, symbol, exchange, max_age):
        return self.prices.get(symbol)


class _FakeState:
    futures_symbol = "CRUDEOIL21SEP26FUT"


class _FakeStore:
    state = _FakeState()


FUTURES_SYMBOL = "CRUDEOIL21SEP26FUT"


@pytest.fixture
def engine(script_module):
    """A StrategyEngine with only the attributes _check_stop_loss actually
    reads populated -- __new__ bypasses __init__ (which needs a real
    client/env/etc.), matching this repo's established pattern for
    unit-testing a single method in isolation (see run_oi_backtest.py's
    build_engine for the same __new__ technique used at a larger scale)."""
    eng = script_module.StrategyEngine.__new__(script_module.StrategyEngine)
    eng.store = _FakeStore()
    eng.ltp_client = None  # never reached: price_stream always supplies a price in these tests
    return eng


def _pos(script_module, entry_px, structural_stop_futures_px, highest_profit_pct=0.0):
    return script_module.LegPosition(
        symbol="TESTOPT", entry_px=entry_px,
        structural_stop_futures_px=structural_stop_futures_px,
        highest_profit_pct=highest_profit_pct,
    )


def test_tier1_no_exit_when_neither_check_breached(script_module, engine):
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 95.0, FUTURES_SYMBOL: 7960.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7900.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is False
    print("Tier 1, neither structural nor 15% floor breached -> no exit PASS")


def test_tier1_structural_fires_before_15pct_floor_ce(script_module, engine):
    """CE: futures breaks the entry-trigger candle's own low while premium
    has only dropped 10% (< the 15% floor) -- structural must fire since
    it's the tighter (lesser-loss) constraint here."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 90.0, FUTURES_SYMBOL: 7895.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7900.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is True
    assert price == pytest.approx(7900.0), "FAIL: expected the structural level, not the premium floor"
    print("Tier 1 CE, structural fires ahead of the 15% floor PASS")


def test_tier1_15pct_floor_fires_before_structural_ce(script_module, engine):
    """CE: premium drops 16% (past the 15% floor) while futures is still
    nowhere near the entry-trigger candle's low -- the premium floor must
    fire since it's the tighter constraint here."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 84.0, FUTURES_SYMBOL: 7960.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7800.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is True
    assert price == pytest.approx(85.0), "FAIL: expected the 15% premium floor (entry_px * 0.85)"
    print("Tier 1 CE, 15% premium floor fires ahead of structural PASS")


def test_tier1_structural_fires_pe_mirror(script_module, engine):
    """PE mirror: futures breaks the entry-trigger candle's own HIGH (not
    low) -- direction must flip correctly for the PE side."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 90.0, FUTURES_SYMBOL: 8020.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=8013.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_PE", pos, inst, "PE")
    assert hit is True
    assert price == pytest.approx(8013.0)
    print("Tier 1 PE, structural (futures above trigger candle's high) fires PASS")


def test_tier1_pe_not_triggered_by_futures_still_below_high(script_module, engine):
    """PE: futures still BELOW the trigger candle's high (i.e. the setup
    hasn't failed) must not falsely trip the structural check, even though
    a naive CE-style '<=' comparison would wrongly fire here."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 95.0, FUTURES_SYMBOL: 7990.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=8013.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_PE", pos, inst, "PE")
    assert hit is False
    print("Tier 1 PE, futures below trigger high (not yet failed) -> no false trigger PASS")


def test_tier1_falls_back_to_flat_floor_when_structural_unset(script_module, engine):
    """A leg resumed from a state.json written before structural_stop_futures_px
    existed has it at the dataclass default (0.0) -- _check_stop_loss must
    fall back to the flat 15% premium floor only, never treat 0.0 as a
    real (and trivially-always-breached) futures level."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 84.0, FUTURES_SYMBOL: 7000.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=0.0)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is True
    assert price == pytest.approx(85.0)
    print("Tier 1, structural unset (0.0) -> flat 15% floor fallback only PASS")


def test_tier2_ignores_tier1_checks_once_profit_reaches_guard_activation(script_module, engine):
    """Once highest_profit_pct >= sl_guard_activation_pct (10%), tier 1's
    structural/15%-floor checks must NOT apply anymore, even if the
    futures price has since collapsed far past the structural level --
    the leg is on the premium-based guard/trail tiers from here on."""
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 111.0, FUTURES_SYMBOL: 7000.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7939.0, highest_profit_pct=0.12)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is False
    assert price == pytest.approx(90.0), "FAIL: expected the flat -10% guard level"
    print("Tier 2 (>=10% profit) ignores tier-1 structural/15% checks PASS")


def test_tier2_guard_still_fires_on_premium_alone(script_module, engine):
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({"TESTOPT": 89.0, FUTURES_SYMBOL: 8000.0})
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7939.0, highest_profit_pct=0.12)
    hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    assert hit is True
    assert price == pytest.approx(90.0)
    print("Tier 2 flat -10% guard still fires correctly on premium alone PASS")


def test_check_stop_loss_returns_false_when_no_ltp_available(script_module, engine):
    inst = script_module.INSTRUMENTS[0]
    engine.price_stream = _FakePriceStream({})  # no price for TESTOPT at all
    pos = _pos(script_module, entry_px=100.0, structural_stop_futures_px=7900.0)

    # fetch_symbol_ltp (REST fallback) will be called with ltp_client=None --
    # patch it to a no-op returning None so this test stays offline/fast
    # rather than exercising the real REST-fallback code path.
    original = script_module.fetch_symbol_ltp
    script_module.fetch_symbol_ltp = lambda *a, **k: None
    try:
        hit, price = engine._check_stop_loss("CRUDEOIL_CE", pos, inst, "CE")
    finally:
        script_module.fetch_symbol_ltp = original
    assert hit is False and price is None
    print("No LTP available -> (False, None), never acts on a missing quote PASS")
