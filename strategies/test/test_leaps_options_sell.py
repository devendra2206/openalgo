"""
Unit tests for strategies/deployed/Nifty_LEAPS_OptionsSell_1_20260823190000.py
-- the RSI-driven, quarterly NIFTY option-selling strategy with a
separately-rolling monthly hedge (see that script's own module docstring
and the approved implementation plan for the full rule set).

Loaded via importlib.util.spec_from_file_location (the script's filename
starts with a letter but pytest's own rootdir-on-sys.path collision with
the pip-installed `openalgo` SDK still applies -- see
test_candle_boundary_refresh.py's identical helper for why
_ensure_real_openalgo_sdk_loaded() is needed before exec_module()).

No live broker connection is used anywhere in this file -- every
client/price_stream dependency is a stub or MagicMock, and network-side
calls (append_trade_log, notify_trade_closed, push_leg_error,
check_force_exit, check_pending_action, ack_pending_action,
ack_force_exit_complete) are monkeypatched out in any test that would
otherwise reach them, so nothing here touches disk or the loopback
strategy_reporting port.
"""

import importlib.util
import sys
from datetime import date, datetime as real_datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_LEAPS_OptionsSell_1_20260823190000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_candle_boundary_refresh.py / test_strategy_pnl_executor.py's
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
    spec = importlib.util.spec_from_file_location("leaps_options_sell_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


@pytest.fixture
def engine(script_module, monkeypatch):
    # Never touch STRATEGY_REPORTING_PORT/network from a background task
    # spawned incidentally during a test.
    monkeypatch.setattr(script_module, "notify_trade_closed", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "notify_telegram_error", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "push_leg_error", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "check_pending_action", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "ack_pending_action", lambda *a, **k: None)
    monkeypatch.setattr(script_module, "check_force_exit", lambda *a, **k: False)
    monkeypatch.setattr(script_module, "ack_force_exit_complete", lambda *a, **k: None)
    # append_trade_log spawns a real background writer thread that writes
    # trades_<tag>.csv to strategies/deployed/ -- keep tests hermetic.
    monkeypatch.setattr(script_module, "append_trade_log", lambda *a, **k: None)

    env = script_module.Environment()
    store = script_module.StateStore(env)  # fresh in-memory state
    # engine.save_state() is exercised by several tests below (e.g. via
    # _process_new_candle/_finalize_exit/_check_hedge_roll_due) -- keep this
    # fixture hermetic by never actually touching disk, regardless of which
    # code path calls save_state().
    monkeypatch.setattr(store, "save", lambda: None)
    client = MagicMock()
    price_stream = MagicMock()
    price_stream.get_ltp.return_value = None
    eng = script_module.StrategyEngine(client, store, env, price_stream, execution_id=1, ltp_client=MagicMock())
    yield eng
    eng._fill_executor.shutdown(wait=False)
    eng._bg_executor.shutdown(wait=False)
    eng._rsi_executor.shutdown(wait=False)


def _drain(executor):
    executor.submit(lambda: None).result(timeout=5)


# =============================================================================
# resolve_leaps_expiry() -- 21 hold-period test cases
# =============================================================================
class _FakeExpiryClient:
    """Stub for client.expiry() -- returns a fixed calendar of quarterly
    (Mar/Jun/Sep/Dec) month-end expiry dates spanning several years, so any
    test date used below has enough forward coverage for
    resolve_leaps_expiry()'s 12-iteration walk."""

    def __init__(self, quarter_days=None):
        self._dates = []
        days = quarter_days or {3: 28, 6: 26, 9: 25, 12: 26}
        for year in range(2022, 2028):
            for month, day in days.items():
                self._dates.append(date(year, month, day))

    def expiry(self, symbol, exchange, instrumenttype):
        return {
            "status": "success",
            "data": [d.strftime("%d-%b-%y") for d in self._dates],
        }


def _expected_leaps_month(today: date) -> int:
    """Independent re-derivation of the documented hold-period table, used
    only to compute the expected answer for each test case below (NOT a
    copy of resolve_leaps_expiry()'s own algorithm):
      hold March:      Nov 21 (prev year) - Feb 20
      hold June:        Feb 21 - May 20
      hold September:   May 21 - Aug 20
      hold December:    Aug 21 - Nov 20
    """
    y, m, d = today.year, today.month, today.day
    if (m == 11 and d >= 21) or m == 12 or m == 1 or (m == 2 and d <= 20):
        return 3
    if (m == 2 and d >= 21) or m == 3 or m == 4 or (m == 5 and d <= 20):
        return 6
    if (m == 5 and d >= 21) or m == 6 or m == 7 or (m == 8 and d <= 20):
        return 9
    if (m == 8 and d >= 21) or m == 9 or m == 10 or (m == 11 and d <= 20):
        return 12
    raise AssertionError(f"unreachable date {today}")


def _expected_leaps_year(today: date, expected_month: int) -> int:
    """The March contract targeted by a Nov21-Dec31 date is NEXT year's
    March; every other targeted month is the current year's."""
    if expected_month == 3 and today.month in (11, 12):
        return today.year + 1
    return today.year


LEAPS_EXPIRY_TEST_DATES = [
    # -- December-2023 window: Aug 21, 2023 - Nov 20, 2023 --
    date(2023, 8, 20),   # day before window opens (still holding Sept -- boundary "before")
    date(2023, 8, 21),   # window opens (boundary "on"/"after" for the Sept->Dec transition)
    date(2023, 10, 1),   # mid-window
    date(2023, 11, 20),  # last day of window (boundary "on")
    # -- March-2024 window: Nov 21, 2023 - Feb 20, 2024 (crosses the year boundary) --
    date(2023, 11, 21),  # window opens (boundary "after")
    date(2023, 12, 25),  # mid-window
    date(2024, 1, 15),   # mid-window, new calendar year
    date(2024, 2, 20),   # last day of window (boundary "on")
    # -- June-2024 window: Feb 21, 2024 - May 20, 2024 --
    date(2024, 2, 21),   # window opens (boundary "after")
    date(2024, 3, 10),   # mid-window
    date(2024, 4, 30),   # mid-window
    date(2024, 5, 20),   # last day of window (boundary "on")
    # -- September-2024 window: May 21, 2024 - Aug 20, 2024 --
    date(2024, 5, 21),   # window opens (boundary "after")
    date(2024, 6, 15),   # mid-window
    date(2024, 7, 4),    # mid-window
    date(2024, 8, 20),   # last day of window (boundary "on")
    # -- December-2024 window: Aug 21, 2024 - Nov 20, 2024 --
    date(2024, 8, 21),   # window opens (boundary "after")
    date(2024, 9, 30),   # mid-window
    date(2024, 11, 20),  # last day of window (boundary "on")
    # -- March-2025 window: Nov 21, 2024 onward --
    date(2024, 11, 21),  # window opens (boundary "after")
    date(2025, 1, 1),    # mid-window, new calendar year again (21st overall case)
]


@pytest.mark.parametrize("today", LEAPS_EXPIRY_TEST_DATES, ids=lambda d: d.isoformat())
def test_resolve_leaps_expiry_hold_period_table(script_module, today):
    assert len(LEAPS_EXPIRY_TEST_DATES) == 21
    expected_month = _expected_leaps_month(today)
    expected_year = _expected_leaps_year(today, expected_month)
    client = _FakeExpiryClient()

    result = script_module.resolve_leaps_expiry(client, today)

    assert result is not None, f"resolve_leaps_expiry returned None for {today}"
    compact, chosen_date = result
    assert (chosen_date.year, chosen_date.month) == (expected_year, expected_month), (
        f"for today={today}, expected quarter {expected_year}-{expected_month:02d}, "
        f"got {chosen_date.year}-{chosen_date.month:02d}"
    )
    assert compact == chosen_date.strftime("%d%b%y").upper()


# =============================================================================
# resolve_hedge_monthly_expiry() -- day<=15/day>15 boundary + monthly
# plausibility check
# =============================================================================
class _FakeWeeklyExpiryClient:
    """Stub for client.expiry() -- a realistic mixed calendar: several
    weeklies still remaining in the reference month, then exactly ONE
    listed date per month further out (matching the documented live
    sample and the live dry-run check: near months carry weekly
    granularity, everything beyond that is monthly-only)."""

    def __init__(self, dates):
        self._dates = dates

    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": [d.strftime("%d-%b-%y") for d in self._dates]}


def test_resolve_hedge_monthly_expiry_day_15_stays_current_month(script_module):
    # January 2024: weeklies on 4,11,18,25 (last = 25th, a plausible monthly
    # -- 31 days in Jan, day 25 >= 31-8=23). February's single monthly: 29th.
    dates = [date(2024, 1, 4), date(2024, 1, 11), date(2024, 1, 18), date(2024, 1, 25),
             date(2024, 2, 29)]
    client = _FakeWeeklyExpiryClient(dates)

    compact, raw = script_module.resolve_hedge_monthly_expiry(client, date(2024, 1, 15))

    assert raw == "25-Jan-24"
    assert compact == "25JAN24"


def test_resolve_hedge_monthly_expiry_day_16_rolls_to_next_month(script_module):
    dates = [date(2024, 1, 4), date(2024, 1, 11), date(2024, 1, 18), date(2024, 1, 25),
             date(2024, 2, 29)]
    client = _FakeWeeklyExpiryClient(dates)

    compact, raw = script_module.resolve_hedge_monthly_expiry(client, date(2024, 1, 16))

    assert compact == "29FEB24"


def test_resolve_hedge_monthly_expiry_rejects_implausibly_early_current_month_candidate(script_module):
    """If the broker's expiry() list for the CURRENT month only goes up to
    an early-month weekly (a later one not yet listed), month_ends would
    otherwise mistake that early weekly for the monthly -- this must raise,
    never silently trade the wrong contract."""
    # January 2024: only a 4th and 11th listed -- the list is clearly
    # incomplete (a real monthly can't be day 11 in a 31-day month, day
    # 11 < 31-8=23), so this must be refused rather than silently used.
    dates = [date(2024, 1, 4), date(2024, 1, 11), date(2024, 2, 29)]
    client = _FakeWeeklyExpiryClient(dates)

    with pytest.raises(RuntimeError, match="too early"):
        script_module.resolve_hedge_monthly_expiry(client, date(2024, 1, 5))


def test_resolve_hedge_monthly_expiry_future_month_never_needs_plausibility_check(script_module):
    """A future month's month_ends entry is trusted as-is even if it falls
    early in that month (a legitimate holiday-shifted monthly) -- the
    plausibility check only ever applies to the CURRENT-month branch."""
    dates = [date(2024, 1, 25), date(2024, 2, 6)]  # Feb's only listing is day 6 -- early, but future-month
    client = _FakeWeeklyExpiryClient(dates)

    compact, raw = script_module.resolve_hedge_monthly_expiry(client, date(2024, 1, 16))

    assert compact == "06FEB24"  # accepted without complaint -- future month, single listing, trusted


# =============================================================================
# select_main_strike()
# =============================================================================
def _make_optionchain_resp(spot: float, side_key: str, premiums_by_strike: dict, round_step: int = 500):
    atm = round(spot / round_step) * round_step
    all_strikes = set(premiums_by_strike.keys()) | {atm}
    chain = []
    for strike in sorted(all_strikes):
        row = {"strike": strike}
        px = premiums_by_strike.get(strike)
        if px is not None:
            row[side_key] = {"ltp": px, "bid": px - 0.5, "ask": px + 0.5}
        chain.append(row)
    return {"status": "success", "chain": chain}


def test_select_main_strike_in_band_wins_over_out_of_band_strike_proximity(script_module):
    """Band membership is a hard filter, not a soft weight: an in-band
    candidate wins even when a candidate closer to ATM (fewer OTM steps)
    has a premium far outside the 300-400 band."""
    spot = 20000.0
    atm = 20000.0
    # step 1 (closest to ATM): premium 200 -- well outside the band.
    # step 5: premium 360 -- inside the band, closest-to-350.
    premiums = {atm + 1 * 500: 200.0, atm + 5 * 500: 360.0}
    client = MagicMock()
    client.optionchain.return_value = _make_optionchain_resp(spot, "ce", premiums)

    result = script_module.select_main_strike(client, spot, "CE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm + 5 * 500
    assert result["in_band"] is True


def test_select_main_strike_falls_back_to_closest_overall_when_none_in_band(script_module):
    spot = 20000.0
    atm = 20000.0
    # PE scans OTM in the DOWN direction (atm - step*500). Neither candidate
    # is in [300, 400] -- 450 (diff 100) beats 200 (diff 150).
    premiums = {atm - 1 * 500: 200.0, atm - 2 * 500: 450.0}
    client = MagicMock()
    client.optionchain.return_value = _make_optionchain_resp(spot, "pe", premiums)

    result = script_module.select_main_strike(client, spot, "PE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm - 2 * 500
    assert result["in_band"] is False


def test_select_main_strike_is_bounded_to_max_otm_steps(script_module):
    """A strike beyond config.max_otm_steps must never be selected, even
    if its premium is a perfect match -- the scan loop itself never
    reaches that far step."""
    spot = 20000.0
    atm = 20000.0
    premiums = {
        atm + 1 * 500: 320.0,     # in-band, within the scanned range
        atm + 11 * 500: 350.0,    # perfect match, but far beyond max_otm_steps
    }
    client = MagicMock()
    client.optionchain.return_value = _make_optionchain_resp(spot, "ce", premiums)

    result = script_module.select_main_strike(client, spot, "CE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm + 1 * 500


def test_select_main_strike_max_otm_steps_is_6_not_10(script_module):
    """Deliberate reduction from the backtest's 10 steps (5000pt) to 6
    steps (3000pt), to keep every optionchain() scan band comfortably
    under the platform's 10s client timeout -- step 6 is reachable, step 7
    is not, even with a perfect premium match."""
    assert script_module.config.max_otm_steps == 6
    spot = 20000.0
    atm = 20000.0
    premiums = {
        atm + 6 * 500: 355.0,   # in-band, exactly at the new limit
        atm + 7 * 500: 350.0,   # perfect match, one step beyond the new limit
    }
    client = MagicMock()
    client.optionchain.return_value = _make_optionchain_resp(spot, "ce", premiums)

    result = script_module.select_main_strike(client, spot, "CE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm + 6 * 500  # step 7 is never reached


def test_select_main_strike_stops_at_the_first_band_with_an_in_band_hit(script_module):
    """with_quotes=True fans out a real per-strike broker quote call, so
    finding an in-band candidate in the smallest (cheapest) band must skip
    the wider, costlier optionchain() calls entirely -- never call more
    than once when the first band already qualifies."""
    spot = 20000.0
    atm = 20000.0
    premiums = {atm + 1 * 500: 320.0}   # in-band, within even the smallest scan band
    client = MagicMock()
    client.optionchain.return_value = _make_optionchain_resp(spot, "ce", premiums)

    result = script_module.select_main_strike(client, spot, "CE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm + 500
    assert client.optionchain.call_count == 1  # never widened past the first band


def test_select_main_strike_widens_to_a_later_band_when_earlier_ones_have_no_in_band_hit(script_module):
    """If a smaller band's response has no in-band candidate, the scan
    must widen to the next band rather than settling for that smaller
    band's own out-of-band fallback -- a wider (unqueried) band might hold
    a real in-band hit further from ATM."""
    spot = 20000.0
    atm = 20000.0
    # First band's response: only an out-of-band candidate (too rich).
    first_band_resp = _make_optionchain_resp(spot, "ce", {atm + 1 * 500: 500.0})
    # A later, wider band's response: the SAME out-of-band candidate plus a
    # genuine in-band one further out that only a wider strike_count would see.
    wider_band_resp = _make_optionchain_resp(spot, "ce", {atm + 1 * 500: 500.0, atm + 6 * 500: 340.0})
    client = MagicMock()
    client.optionchain.side_effect = [first_band_resp, wider_band_resp, wider_band_resp]

    result = script_module.select_main_strike(client, spot, "CE", "28MAR24")

    assert result is not None
    assert result["strike"] == atm + 6 * 500  # the in-band candidate found only after widening
    assert client.optionchain.call_count == 2  # stopped as soon as the in-band hit appeared


# =============================================================================
# select_hedge_strike()
# =============================================================================
def test_select_hedge_strike_ranks_by_strike_distance_not_premium(script_module):
    sold_strike = 20000.0
    # target = 20000 * 1.02 = 20400
    # Candidate A: strike 20400 (distance 0 from target, round-100), expensive premium.
    # Candidate B: strike 20600 (distance 200 from target, round-100), cheap premium.
    chain = {"status": "success", "chain": [
        {"strike": 20400.0, "ce": {"ltp": 500.0, "bid": 499.0, "ask": 501.0}},
        {"strike": 20600.0, "ce": {"ltp": 5.0, "bid": 4.5, "ask": 5.5}},
    ]}
    client = MagicMock()
    client.optionchain.return_value = chain

    result = script_module.select_hedge_strike(client, "28MAR24", "CE", sold_strike)

    assert result is not None
    assert result["strike"] == 20400.0  # closer to target 20400 by strike distance, despite the higher premium


def test_select_hedge_strike_rejects_non_round_100_strikes(script_module):
    """A strike not on the 100-pt grid must never be chosen, even when it
    is numerically closer to the 2% target than any round-100 candidate."""
    sold_strike = 20000.0
    # target = 20000 * 1.02 = 20400
    chain = {"status": "success", "chain": [
        {"strike": 20350.0, "ce": {"ltp": 5.0, "bid": 4.5, "ask": 5.5}},   # distance 50 -- closer, but NOT round-100
        {"strike": 20500.0, "ce": {"ltp": 5.0, "bid": 4.5, "ask": 5.5}},   # distance 100 -- farther, but round-100
    ]}
    client = MagicMock()
    client.optionchain.return_value = chain

    result = script_module.select_hedge_strike(client, "28MAR24", "CE", sold_strike)

    assert result is not None
    assert result["strike"] == 20500.0  # the only round-100 candidate, despite being farther numerically


def test_select_hedge_strike_rejects_wrong_side_even_if_numerically_closer(script_module):
    """For a sold CE, a hedge candidate must be strictly ABOVE sold_strike.
    A candidate below sold_strike is rejected even when its raw distance to
    the 2% target is smaller than any valid candidate's."""
    sold_strike = 20500.0
    # target = 20500 * 1.02 = 20910
    chain = {"status": "success", "chain": [
        {"strike": 20490.0, "ce": {"ltp": 10.0, "bid": 9.5, "ask": 10.5}},   # WRONG side -- distance to target = 420
        {"strike": 21500.0, "ce": {"ltp": 3.0, "bid": 2.5, "ask": 3.5}},     # correct side -- distance = 590 (farther)
    ]}
    client = MagicMock()
    client.optionchain.return_value = chain

    result = script_module.select_hedge_strike(client, "28MAR24", "CE", sold_strike)

    assert result is not None
    assert result["strike"] == 21500.0  # the only valid (correct-side) candidate, despite being farther numerically


def test_select_hedge_strike_rejects_stale_no_bid_ask_candidate(script_module):
    """A strike with a nonzero ltp but no genuine two-sided market (missing
    bid/ask) must never be selected -- same liquidity guard as
    select_main_strike(), so the hedge is never bought against a stale
    quote at MARKET price."""
    sold_strike = 20000.0
    chain = {"status": "success", "chain": [
        {"strike": 20400.0, "ce": {"ltp": 5.0, "bid": 0, "ask": 0}},        # stale -- no real market
        {"strike": 21000.0, "ce": {"ltp": 4.0, "bid": 3.5, "ask": 4.5}},    # genuinely liquid, farther out
    ]}
    client = MagicMock()
    client.optionchain.return_value = chain

    result = script_module.select_hedge_strike(client, "28MAR24", "CE", sold_strike)

    assert result is not None
    assert result["strike"] == 21000.0  # the only genuinely liquid candidate


def test_select_hedge_strike_returns_none_when_no_valid_side_candidate(script_module):
    chain = {"status": "success", "chain": [{"strike": 19000.0, "ce": {"ltp": 10.0}}]}
    client = MagicMock()
    client.optionchain.return_value = chain

    result = script_module.select_hedge_strike(client, "28MAR24", "CE", 20000.0)

    assert result is None


# =============================================================================
# RSI cross-detection helpers
# =============================================================================
def test_crossed_below_requires_a_genuine_cross_not_just_sitting_past_threshold(script_module):
    crossed_below = script_module._crossed_below
    assert crossed_below(35.0, 25.0, 32.0) is True    # genuine cross
    assert crossed_below(20.0, 15.0, 32.0) is False   # already below on both bars -- no fresh cross
    assert crossed_below(None, 15.0, 32.0) is False   # no previous bar -- can't confirm a cross


def test_crossed_above_requires_a_genuine_cross_not_just_sitting_past_threshold(script_module):
    crossed_above = script_module._crossed_above
    assert crossed_above(45.0, 60.0, 52.0) is True
    assert crossed_above(70.0, 65.0, 52.0) is False
    assert crossed_above(None, 65.0, 52.0) is False


# =============================================================================
# Same-bar reversal control flow
# =============================================================================
def test_process_new_candle_exit_condition_sets_pending_reentry_side(script_module, engine, monkeypatch):
    """A PE position whose RSI crosses below the bear threshold must be
    flagged to re-enter as CE, and _exit_position must fire immediately --
    not wait for a later cycle."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY28MAR2420000PE"

    exit_calls = []
    monkeypatch.setattr(engine, "_exit_position", lambda reason: exit_calls.append(reason))

    engine._process_new_candle(cur_rsi=25.0, prev_rsi=35.0)

    assert pos.pending_reentry_side == "CE"
    assert exit_calls == ["rsi_reversal"]


def test_process_new_candle_no_exit_when_rsi_merely_sits_past_threshold(script_module, engine, monkeypatch):
    """RSI already below 32 on the PREVIOUS bar too (no genuine cross this
    bar) must NOT re-trigger an exit."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY28MAR2420000PE"

    exit_calls = []
    monkeypatch.setattr(engine, "_exit_position", lambda reason: exit_calls.append(reason))

    engine._process_new_candle(cur_rsi=20.0, prev_rsi=18.0)

    assert pos.pending_reentry_side == ""
    assert exit_calls == []


def test_process_new_candle_enters_fresh_when_flat(script_module, engine, monkeypatch):
    enter_calls = []
    monkeypatch.setattr(engine, "_enter_position", lambda side: enter_calls.append(side))

    engine._process_new_candle(cur_rsi=60.0, prev_rsi=45.0)  # RSI > 52 -> sell PE

    assert enter_calls == ["PE"]


def test_finalize_exit_immediately_reenters_on_same_bar_reversal(script_module, engine, monkeypatch):
    """Once both legs' exits are confirmed, _finalize_exit must (a) reset
    the position to flat, (b) release _pending_fills BEFORE re-entering
    (else _enter_position's own guard would silently no-op), and (c) call
    _enter_position with the side captured at the RSI-cross moment --
    using this cycle's already-determined direction, not re-fetching."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY28MAR2420000PE"
    pos.entry_px = 350.0
    pos.short_exit_fill_px = 300.0
    pos.hedge_symbol = "NIFTY25SEP2420800PE"
    pos.hedge_entry_px = 50.0
    pos.hedge_exit_fill_px = 40.0
    pos.pending_reentry_side = "CE"
    engine._pending_fills.add("position")

    seen_pending_fills_at_call = []
    enter_calls = []

    def _fake_enter_position(side):
        seen_pending_fills_at_call.append("position" in engine._pending_fills)
        enter_calls.append(side)

    monkeypatch.setattr(engine, "_enter_position", _fake_enter_position)

    engine._finalize_exit(pos, "rsi_reversal")

    assert engine.state.position.side == ""  # flat / fresh LeapsPosition
    assert enter_calls == ["CE"]
    assert seen_pending_fills_at_call == [False]  # guard was released BEFORE re-entry was dispatched


def test_finalize_exit_does_not_reenter_without_pending_reentry_side(script_module, engine, monkeypatch):
    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.entry_px = 350.0
    pos.short_exit_fill_px = 400.0
    pos.hedge_symbol = "NIFTY25SEP2420800CE"
    pos.hedge_entry_px = 50.0
    pos.hedge_exit_fill_px = 60.0
    pos.pending_reentry_side = ""
    engine._pending_fills.add("position")

    enter_calls = []
    monkeypatch.setattr(engine, "_enter_position", lambda side: enter_calls.append(side))

    engine._finalize_exit(pos, "expiry_day_close")

    assert enter_calls == []
    assert "position" not in engine._pending_fills


# =============================================================================
# Hedge-roll holiday-shift logic
# =============================================================================
def test_hedge_roll_date_for_month_walks_back_off_a_holiday_weekend(script_module, monkeypatch):
    """If the 18th falls on a day trading_calendar reports closed, the roll
    date walks back to the nearest PRIOR actual trading day -- never
    forward, and never trading_calendar.prev_trading_day() (which would be
    wrong when the 18th itself is a trading day)."""
    closed_days = {date(2026, 4, 18), date(2026, 4, 17)}  # simulate a holiday, then a weekend before it

    def fake_is_trading_day(day):
        return day not in closed_days

    monkeypatch.setattr(script_module, "is_trading_day", fake_is_trading_day)

    result = script_module.hedge_roll_date_for_month(2026, 4)

    assert result == date(2026, 4, 16)


def test_hedge_roll_date_for_month_returns_the_18th_itself_when_trading(script_module, monkeypatch):
    monkeypatch.setattr(script_module, "is_trading_day", lambda day: True)

    result = script_module.hedge_roll_date_for_month(2026, 3)

    assert result == date(2026, 3, 18)


def test_check_hedge_roll_due_dispatches_once_and_guards_same_day(script_module, engine, monkeypatch):
    fixed_today = date(2026, 3, 18)

    class _FixedDate(date):
        @classmethod
        def today(cls):
            return fixed_today

    monkeypatch.setattr(script_module, "is_trading_day", lambda day: True)

    class _FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return script_module.IST.localize(real_datetime(2026, 3, 18, 11, 0, 0))

    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)

    pos = engine.state.position
    pos.side = "CE"
    pos.hedge_symbol = "NIFTY25APR2420800CE"
    pos.last_hedge_roll_date = ""

    submitted = []
    monkeypatch.setattr(engine._fill_executor, "submit", lambda fn, *a, **k: submitted.append(fn))

    engine._check_hedge_roll_due()
    assert len(submitted) == 1
    assert "position" in engine._pending_fills

    # Simulate the roll having completed and the guard being set, same day.
    engine._pending_fills.discard("position")
    pos.last_hedge_roll_date = fixed_today.isoformat()

    engine._check_hedge_roll_due()
    assert len(submitted) == 1  # not dispatched again the same day


def test_check_hedge_roll_due_skips_when_not_roll_date(script_module, engine, monkeypatch):
    class _FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return script_module.IST.localize(real_datetime(2026, 3, 10, 11, 0, 0))

    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(script_module, "is_trading_day", lambda day: True)

    pos = engine.state.position
    pos.side = "CE"
    pos.hedge_symbol = "NIFTY25APR2420800CE"

    submitted = []
    monkeypatch.setattr(engine._fill_executor, "submit", lambda fn, *a, **k: submitted.append(fn))

    engine._check_hedge_roll_due()

    assert submitted == []


# =============================================================================
# Expiry-day safety close -- terminal, no new-position search that day
# =============================================================================
def _freeze_run_cycle_clock(script_module, monkeypatch, hour, minute):
    class _FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return script_module.IST.localize(real_datetime(2026, 3, 28, hour, minute, 0))

    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)


def test_run_cycle_force_closes_on_main_leg_own_expiry_day(script_module, engine, monkeypatch):
    _freeze_run_cycle_clock(script_module, monkeypatch, 15, 20)  # past expiry_day_close_time (15:15)
    monkeypatch.setattr(script_module, "_within_market_hours", lambda: True)

    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.expiry_date = date(2026, 3, 28).isoformat()  # expires TODAY

    exit_calls = []
    monkeypatch.setattr(engine, "_exit_position", lambda reason: exit_calls.append(reason))
    hedge_roll_calls = []
    monkeypatch.setattr(engine, "_check_hedge_roll_due", lambda: hedge_roll_calls.append(True))

    engine.run_cycle()

    assert exit_calls == ["expiry_day_close"]
    # Terminal for the day -- no hedge-roll check, no RSI/new-position search.
    assert hedge_roll_calls == []


def test_run_cycle_does_not_force_close_when_expiry_is_not_today(script_module, engine, monkeypatch):
    _freeze_run_cycle_clock(script_module, monkeypatch, 15, 20)
    monkeypatch.setattr(script_module, "_within_market_hours", lambda: True)

    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY26JUN2420000CE"
    pos.expiry_date = date(2026, 6, 26).isoformat()  # NOT today

    exit_calls = []
    monkeypatch.setattr(engine, "_exit_position", lambda reason: exit_calls.append(reason))
    monkeypatch.setattr(engine, "_check_hedge_roll_due", lambda: None)
    monkeypatch.setattr(engine, "_new_candle_closed", lambda: False)  # keep this test focused

    engine.run_cycle()

    assert exit_calls == []


def test_run_cycle_expiry_day_still_resolves_pending_error_before_returning(script_module, engine, monkeypatch):
    """AUTHORING_CHECKLIST.md section 1: even the terminal expiry-day branch
    must resolve a pending Retry/Cancel/Manual decision every cycle, never
    just skip/log it -- otherwise an errored position on its own expiry day
    could freeze forever."""
    _freeze_run_cycle_clock(script_module, monkeypatch, 15, 20)
    monkeypatch.setattr(script_module, "_within_market_hours", lambda: True)

    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.expiry_date = date(2026, 3, 28).isoformat()
    pos.error_state = "exit_failed"

    resolve_calls = []
    monkeypatch.setattr(engine, "_resolve_pending_error_if_any", lambda: resolve_calls.append(True))
    exit_calls = []
    monkeypatch.setattr(engine, "_exit_position", lambda reason: exit_calls.append(reason))

    engine.run_cycle()

    assert resolve_calls == [True]
    # Still in error_state (nothing actually resolved it in this test) --
    # must NOT attempt a force-close through an errored leg.
    assert exit_calls == []


# =============================================================================
# Code-review fixes: force-exit vs same-bar reversal, unhedged-position
# handling, duplicate-order guards, hedge-roll bookkeeping
# =============================================================================
def test_finalize_exit_suppresses_reentry_when_force_exit_pending(script_module, engine, monkeypatch):
    """A pending Force Exit must not be silently defeated by a same-bar
    RSI-reversal re-entry that was already queued before the operator's
    request landed."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY28MAR2420000PE"
    pos.entry_px = 350.0
    pos.short_exit_fill_px = 300.0
    pos.hedge_symbol = "NIFTY25SEP2420800PE"
    pos.hedge_entry_px = 50.0
    pos.hedge_exit_fill_px = 40.0
    pos.pending_reentry_side = "CE"
    engine._pending_fills.add("position")
    engine._force_exit_pending = True

    enter_calls = []
    monkeypatch.setattr(engine, "_enter_position", lambda side: enter_calls.append(side))

    engine._finalize_exit(pos, "rsi_reversal")

    assert enter_calls == []
    assert engine.state.position.side == ""  # still flattened -- only the re-entry is suppressed


def test_watch_short_exit_fill_finalizes_directly_when_unhedged(script_module, engine, monkeypatch):
    """A position left unhedged by a prior failed hedge roll must finalize
    on the short leg alone -- never call place() against an empty hedge
    symbol."""
    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.entry_px = 300.0
    pos.short_exit_order_id = "ORD1"
    pos.hedge_symbol = ""
    pos.hedge_entry_px = 50.0
    pos.pending_exit_reason = "rsi_reversal"
    engine._pending_fills.add("position")

    monkeypatch.setattr(script_module, "poll_fill",
                         lambda *a, **k: {"average_price": 250.0})
    place_calls = []
    monkeypatch.setattr(script_module, "place", lambda *a, **k: place_calls.append(a))
    finalize_calls = []
    monkeypatch.setattr(engine, "_finalize_exit", lambda p, reason: finalize_calls.append(reason))

    engine._watch_short_exit_fill("ORD1", pos.short_symbol, pos.quantity)

    assert place_calls == []  # never placed a hedge-exit order for an empty symbol
    assert finalize_calls == ["rsi_reversal"]
    assert pos.short_exit_filled is True
    assert pos.hedge_exit_filled is True
    assert pos.hedge_exit_fill_px == pos.hedge_entry_px  # zero-PnL phantom leg, no double counting


def test_enter_position_worker_aborts_if_position_already_open(script_module, engine, monkeypatch):
    """A defensive re-check inside the worker itself: even if _enter_position's
    own guard were somehow bypassed, the worker must never place a
    duplicate order against an already-open position."""
    pos = engine.state.position
    pos.side = "PE"  # already open

    spot_calls = []
    monkeypatch.setattr(engine, "get_spot_ltp", lambda: spot_calls.append(True))

    engine._enter_position_worker("CE")

    assert spot_calls == []  # aborted before even fetching spot -- no work attempted


def test_enter_position_worker_catches_select_main_strike_failure(script_module, engine, monkeypatch):
    """select_main_strike() raising ANY exception (not just RuntimeError --
    a real HTTP timeout raises a different type) must be caught and
    retried within the window -- never silently swallowed by the fire-and-
    forget executor submission, and never crash the worker. After the
    window is exhausted with nothing but failures, exactly one Telegram
    alert is sent (not one per attempt)."""
    monkeypatch.setattr(script_module.config, "main_strike_retry_window_sec", 0.1)
    monkeypatch.setattr(script_module.config, "main_strike_retry_interval_sec", 0.03)
    engine.state.position.side = ""
    monkeypatch.setattr(engine, "get_spot_ltp", lambda: 20000.0)
    monkeypatch.setattr(engine, "get_leaps_expiry", lambda: ("28MAR26", date(2026, 3, 28)))

    calls = []

    def _raise(*a, **k):
        calls.append(1)
        raise TimeoutError("optionchain() timed out")  # deliberately NOT a RuntimeError
    monkeypatch.setattr(script_module, "select_main_strike", _raise)

    notify_calls = []
    monkeypatch.setattr(engine, "_notify_telegram_error_bg", lambda msg: notify_calls.append(msg))

    engine._pending_fills.add("position")  # simulate what _enter_position would have set
    engine._enter_position_worker("PE")

    assert len(calls) > 1  # retried within the window, not aborted on the first failure
    assert len(notify_calls) == 1  # exactly one alert, not one per failed attempt
    assert "abandoned" in notify_calls[0].lower()
    assert "position" not in engine._pending_fills  # released despite the failure


def test_enter_position_worker_retries_strike_selection_until_success(script_module, engine, monkeypatch):
    """If RSI qualifies but the first scan(s) find no strike, keep
    re-scanning (fresh spot + optionchain() each time) rather than giving
    up after a single miss -- bounded by main_strike_retry_window_sec."""
    monkeypatch.setattr(script_module.config, "main_strike_retry_window_sec", 0.3)
    monkeypatch.setattr(script_module.config, "main_strike_retry_interval_sec", 0.05)
    monkeypatch.setattr(engine, "get_leaps_expiry", lambda: ("28MAR26", date(2026, 3, 28)))
    monkeypatch.setattr(engine, "get_spot_ltp", lambda: 20000.0)

    calls = []

    def _fake_select(client, spot, side, expiry_compact):
        calls.append(1)
        if len(calls) < 3:
            return None
        return {"strike": 20500.0, "premium": 350.0, "in_band": True, "diff": 0.0}

    monkeypatch.setattr(script_module, "select_main_strike", _fake_select)
    monkeypatch.setattr(script_module, "resolve_hedge_monthly_expiry",
                         lambda client, today: ("25SEP25", "25-Sep-25"))
    monkeypatch.setattr(script_module, "select_hedge_strike",
                         lambda *a, **k: {"strike": 20900.0, "premium": 10.0})
    monkeypatch.setattr(script_module, "place", lambda *a, **k: "ORDX")
    monkeypatch.setattr(engine, "_watch_entry_fill", lambda *a, **k: None)

    engine._enter_position_worker("CE")

    assert len(calls) == 3  # failed twice, succeeded on the 3rd
    assert engine.state.position.side == "CE"
    assert engine.state.position.short_strike == 20500.0


def test_enter_position_worker_gives_up_after_retry_window_expires(script_module, engine, monkeypatch):
    """If nothing qualifies within the retry window, give up cleanly --
    never hang waiting past the configured deadline, and release
    _pending_fills so the next hourly candle can try again."""
    monkeypatch.setattr(script_module.config, "main_strike_retry_window_sec", 0.1)
    monkeypatch.setattr(script_module.config, "main_strike_retry_interval_sec", 0.03)
    monkeypatch.setattr(engine, "get_leaps_expiry", lambda: ("28MAR26", date(2026, 3, 28)))
    monkeypatch.setattr(engine, "get_spot_ltp", lambda: 20000.0)
    monkeypatch.setattr(script_module, "select_main_strike", lambda *a, **k: None)

    engine._pending_fills.add("position")  # simulate what _enter_position would have set
    engine._enter_position_worker("CE")

    assert engine.state.position.side == ""  # never entered
    assert "position" not in engine._pending_fills  # released after giving up


def test_do_hedge_roll_already_unhedged_sets_last_roll_date(script_module, engine, monkeypatch):
    """If a position is already unhedged (a prior roll failure), the daily
    roll-due check must not keep resubmitting this to the executor on
    every tick for the rest of the day."""
    pos = engine.state.position
    pos.side = "PE"
    pos.hedge_symbol = ""  # already unhedged
    pos.last_hedge_roll_date = ""

    engine._do_hedge_roll()

    today_iso = real_datetime.now(script_module.IST).date().isoformat()
    assert pos.last_hedge_roll_date == today_iso


# =============================================================================
# Second full-review pass: reconciled/cancelled hedge-entry must clear ALL
# hedge fields, Force Exit vs in-flight entry, locked pending_fills checks,
# restart-recovery candle detection
# =============================================================================
def test_reconcile_hedge_entry_rejected_clears_all_hedge_fields(script_module, engine, monkeypatch):
    """A hedge-entry order confirmed rejected/cancelled during restart
    reconciliation must clear hedge_symbol/strike/expiry entirely -- not
    just the order id -- so the position accurately shows as unhedged
    rather than 'looks hedged' with nothing live at the broker."""
    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.hedge_symbol = "NIFTY25SEP2420800CE"
    pos.hedge_strike = 20800.0
    pos.hedge_expiry = "2025-09-25"
    pos.hedge_entry_order_id = "ORD_HEDGE"
    pos.hedge_entry_filled = False

    engine.client.orderstatus = MagicMock(
        return_value={"status": "success", "data": {"order_status": "rejected"}})
    notify_calls = []
    monkeypatch.setattr(engine, "_notify_telegram_error_bg", lambda msg: notify_calls.append(msg))

    engine._reconcile_one(pos, "ORD_HEDGE", "hedge_entry_failed", pos.hedge_symbol, "BUY")

    assert pos.hedge_symbol == ""
    assert pos.hedge_strike == 0.0
    assert pos.hedge_expiry == ""
    assert pos.hedge_entry_order_id == ""
    assert pos.hedge_entry_filled is False
    assert pos.side == "CE"  # short leg stays open -- not discarded
    assert len(notify_calls) == 1


def test_resolve_leg_error_cancel_terminal_hedge_entry_clears_all_hedge_fields(script_module, engine, monkeypatch):
    """Cancel on a terminally-rejected hedge-entry error must clear ALL
    hedge fields, not just the order id."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY28MAR2420000PE"
    pos.hedge_symbol = "NIFTY25SEP2420800PE"
    pos.hedge_strike = 20800.0
    pos.hedge_expiry = "2025-09-25"
    pos.hedge_entry_order_id = "ORD_HEDGE"
    pos.error_state = "hedge_entry_failed"
    pos.error_kind = "terminal"
    pos.error_order_id = "ORD_HEDGE"

    monkeypatch.setattr(script_module, "ack_pending_action", lambda *a, **k: None)
    notify_calls = []
    monkeypatch.setattr(engine, "_notify_telegram_error_bg", lambda msg: notify_calls.append(msg))

    engine._resolve_leg_error({"action": "cancel"})

    assert pos.hedge_symbol == ""
    assert pos.hedge_strike == 0.0
    assert pos.hedge_expiry == ""
    assert pos.hedge_entry_order_id == ""
    assert pos.hedge_entry_filled is False
    assert pos.error_state == ""
    assert len(notify_calls) == 1


def test_force_exit_all_does_not_ack_complete_while_entry_in_flight(script_module, engine):
    """An entry attempt still in flight (pos.side empty during the strike-
    selection retry window) must NOT be treated as 'already flat' --
    otherwise Force Exit is falsely acknowledged complete while a fresh
    SELL could still land moments later."""
    engine.state.position.side = ""
    engine._pending_fills.add("position")  # simulates _enter_position_worker running

    result = engine._force_exit_all()

    assert result is False  # not complete yet -- must keep polling


def test_force_exit_all_acks_complete_when_genuinely_flat(script_module, engine, monkeypatch):
    engine.state.position.side = ""
    ack_calls = []
    monkeypatch.setattr(script_module, "ack_force_exit_complete", lambda env: ack_calls.append(True))

    result = engine._force_exit_all()

    assert result is True
    assert ack_calls == [True]


def test_enter_position_worker_aborts_mid_retry_when_force_exit_requested(script_module, engine, monkeypatch):
    """Force Exit requested while the strike-selection retry loop is still
    running must abort the entry attempt before any order is placed."""
    monkeypatch.setattr(script_module.config, "main_strike_retry_window_sec", 5.0)
    monkeypatch.setattr(script_module.config, "main_strike_retry_interval_sec", 0.01)
    monkeypatch.setattr(engine, "get_leaps_expiry", lambda: ("28MAR26", date(2026, 3, 28)))
    monkeypatch.setattr(engine, "get_spot_ltp", lambda: 20000.0)
    monkeypatch.setattr(script_module, "select_main_strike", lambda *a, **k: None)  # never qualifies
    place_calls = []
    monkeypatch.setattr(script_module, "place", lambda *a, **k: place_calls.append(a))

    engine._force_exit_pending = True
    engine._pending_fills.add("position")
    engine._enter_position_worker("CE")

    assert place_calls == []  # aborted -- never placed a real order
    assert engine.state.position.side == ""


def test_new_candle_closed_forces_evaluation_on_restart_with_persisted_key(script_module, engine):
    """A persisted last_candle_key means this process ran before and may
    have missed evaluating whatever closed during the downtime -- the
    first post-restart check must force an immediate re-evaluation rather
    than silently waiting up to an hour for the next boundary change."""
    engine.state.last_candle_key = "2026-08-23 10:15:00"  # resumed from a prior run

    assert engine._new_candle_closed() is True


def test_new_candle_closed_does_not_force_evaluation_on_a_genuinely_fresh_install(script_module, engine):
    """No persisted candle key at all (first run ever) has nothing to
    recover -- behaves exactly as before, just starts tracking from now."""
    engine.state.last_candle_key = ""

    assert engine._new_candle_closed() is False


# =============================================================================
# _open_positions_for_pnl() -- the hedge/BUY leg must appear as its own
# reported position, not be silently folded into the short leg's PnL
# =============================================================================
def test_open_positions_for_pnl_reports_hedge_leg_separately(script_module, engine):
    """Confirmed live 2026-08-24: a merged single row meant the platform's
    View Trade/PnL UI never saw the hedge/BUY leg's own symbol at all,
    even though it was genuinely filled. Both legs must be reported as
    independent entries, each with its own correct pnl (not double-
    counted or combined)."""
    pos = engine.state.position
    pos.side = "PE"
    pos.short_symbol = "NIFTY29DEC2624000PE"
    pos.quantity = 65
    pos.entry_px = 306.15
    pos.entry_filled = True
    pos.entry_time = "2026-08-24T10:15:19+05:30"
    pos.hedge_symbol = "NIFTY29SEP2623500PE"
    pos.hedge_entry_px = 57.6
    pos.hedge_entry_filled = True
    engine.price_stream.get_ltp.side_effect = lambda symbol, exchange, max_age: {
        "NIFTY29DEC2624000PE": 300.0,   # short leg: premium fell -- profit for the seller
        "NIFTY29SEP2623500PE": 60.0,    # hedge leg: premium rose -- profit for the buyer
    }[symbol]

    positions = engine._open_positions_for_pnl()

    assert len(positions) == 2
    short_row = next(p for p in positions if p["leg_key"] == "LEAPS")
    hedge_row = next(p for p in positions if p["leg_key"] == "LEAPS_HEDGE")

    assert short_row["symbol"] == "NIFTY29DEC2624000PE"
    assert short_row["direction"] == "SHORT"
    assert short_row["quantity"] == -65
    assert short_row["pnl"] == pytest.approx((306.15 - 300.0) * 65)  # short-only, no hedge folded in

    assert hedge_row["symbol"] == "NIFTY29SEP2623500PE"
    assert hedge_row["direction"] == "LONG"
    assert hedge_row["quantity"] == 65
    assert hedge_row["pnl"] == pytest.approx((60.0 - 57.6) * 65)  # hedge-only


def test_open_positions_for_pnl_omits_hedge_row_when_unhedged(script_module, engine):
    """A position left unhedged by a prior failed hedge roll must report
    only the short leg -- no phantom hedge row for a leg that was never
    actually bought."""
    pos = engine.state.position
    pos.side = "CE"
    pos.short_symbol = "NIFTY28MAR2420000CE"
    pos.quantity = 65
    pos.entry_px = 350.0
    pos.entry_filled = True
    pos.entry_time = "2026-08-24T10:15:19+05:30"
    pos.hedge_symbol = ""  # unhedged
    engine.price_stream.get_ltp.return_value = 340.0

    positions = engine._open_positions_for_pnl()

    assert len(positions) == 1
    assert positions[0]["leg_key"] == "LEAPS"
