"""
Regression tests for the 2026-08-13 (third pass) trailing SL redesign in
MCX_CrudeOil_EMA34_RSI_ADX_Intraday_1 (`compute_trailing_sl_price`) and the
same-day `max_trades_per_leg_per_day` bump.

Trailing SL is now three tiers, driven entirely by Config defaults:
  - below sl_guard_activation_pct (10%): SL at sl_initial_pct (-25%)
  - 10%-25% (sl_trail_pct): single flat guard at sl_guard_locked_pct (-10%)
  - 25% and up: locks one sl_trail_pct (25%) step behind the highest step
    crossed, until the LOCKED value would reach
    sl_trail_tighten_threshold_pct (100%), then narrows to
    sl_trail_step_late (10%) for the rest of the trade.

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
    for profit in (0.0, 0.05, 0.099):
        sl = script_module.compute_trailing_sl_price(ENTRY_PX, profit)
        assert sl == pytest.approx(ENTRY_PX * 0.75), f"FAIL at profit={profit}"
    print("Below 10% profit -> initial -25% floor PASS")


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
