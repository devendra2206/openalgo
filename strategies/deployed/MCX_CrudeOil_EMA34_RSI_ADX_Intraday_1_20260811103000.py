"""
===============================================================================
MCX CRUDEOIL EMA34 + RSI + ADX Intraday Option Buyer (CE/PE)
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Derived from: `MCX_CrudeOil_EMA9_RSI_Intraday_1_20260723150000.py` (this
              project's most-trusted/most-copied-from reference, per
              `AUTHORING_CHECKLIST.md`) -- same platform-integration
              skeleton (non-blocking run_cycle, retry/cancel/manual error
              recovery, state persistence, dynamic PriceStream, background
              order-fill polling), new signal logic and a BUY-side
              (long-option, bounded-risk) order/PnL convention adapted from
              `CRUDEOIL_VWAP_EMA20_Positional_1_20260805170700.py` (this
              project's existing long-option-buyer example). User-directed
              build, 2026-08-11.

*** THIS STRATEGY BUYS OPTIONS -- BOUNDED RISK (LIMITED TO PREMIUM PAID) ***
Unlike the EMA9/RSI reference (a naked/unhedged SELLER, undefined risk),
every leg here is a long CE or PE purchase -- max loss per trade is the
premium paid, no margin/undefined-risk exposure. No native stop-loss beyond
the technical EMA/RSI/ADX reversal exits below; a losing trade can still
decay to a near-total loss of premium before an exit condition fires.

Key structural difference vs. the NIFTY/SENSEX strategies in this family
------------------------------------------------------------------------
NIFTY/SENSEX have a quotable spot INDEX (NSE_INDEX/BSE_INDEX) to compute the
EMA/RSI signal off. CRUDEOIL has no such index -- MCX's only index-only
feeds are sectoral indices (MCXBULLDEX, MCXCRUDEX, etc., exchange code
MCX_INDEX), not a plain CRUDEOIL spot/index. So this strategy's signal reads
the CURRENT MONTH FUTURES CONTRACT's own candles instead -- resolved
dynamically via `client.expiry(..., instrumenttype="futures")`, NOT
hardcoded, since the exact tradable base symbol naming (broker-master-data-
dependent) can vary. Resolved ONCE PER DAY (see `_reset_day_if_needed`)
since the front-month contract only changes on monthly rollover, not
intraday -- unlike NIFTY/SENSEX's underlying symbol, which never changes.

Both the underlying futures leg AND the options exchange for this
instrument are plain `MCX` (unlike NFO/BFO, which are split from
NSE_INDEX/BSE_INDEX) -- MCX has no separate "underlying quote exchange" vs.
"options exchange" distinction, confirmed against
`docs/prompt/symbol-format.md`'s MCX Futures/Options sections and
`docs/api/options-services/*.md`'s exchange field (`NFO, BFO, CDS, MCX,
CRYPTO`).

Signal Rules
------------------------------------------------------------------------
Computed off the front-month futures contract's own 45-minute candles:
  - EMA(High, 34) and EMA(Low, 34), RSI(14) on Close, ADX(14), and a
    session-anchored VWAP (see _session_vwap -- same hand-rolled pattern
    as CRUDEOIL_VWAP_EMA20_Positional_1, no openalgo.ta helper exists) --
    all computed over the last-closed 45m bars (the newest fetched bar is
    unconditionally dropped as still-forming, same defensive pattern used
    everywhere else in this project).
  - Evaluated purely off the last CLOSED candle's own values -- no live-LTP
    breakout-confirmation layer (that pattern in the 3m EMA9/RSI reference
    exists for its fast timeframe; a 45m candle doesn't need it, so entry/
    exit can only actually change once per 45-minute bar).

  CE entry : close_prev1 > ema_high34_prev1   (price closed above its own
                                                34-EMA of Highs -- trend up)
             AND rsi_prev1 > 53                (momentum confirms)
             AND adx_prev1 > 25                (trend strength gate --
                                                shared by both sides, no
                                                directional +DI/-DI check)
             AND close_prev1 > vwap_prev1      (price also above session
                                                VWAP -- value/location
                                                confirmation, independent
                                                of ADX's trend-strength
                                                measure; added 2026-08-11
                                                after live entries fired on
                                                already-extended moves)
             AND ltp > vwap_prev1              (live price also above
                                                session VWAP at the moment
                                                of entry, not just as of
                                                the last closed candle --
                                                added 2026-08-11)
  PE entry : close_prev1 < ema_low34_prev1    (mirror, below 34-EMA of Lows)
             AND rsi_prev1 < 47
             AND adx_prev1 > 25
             AND close_prev1 < vwap_prev1      (mirror)
             AND ltp < vwap_prev1              (mirror)

  CE exit  : close_prev1 < ema_low34_prev1  OR  rsi_prev1 < 47
  PE exit  : close_prev1 > ema_high34_prev1 OR  rsi_prev1 > 53

  CE and PE are TWO INDEPENDENT legs (not a single flipping position, like
  VWAP_NoHA/Batman) -- each entered/exited purely off its own condition;
  both are the same (long, bounded) risk class so there's no priority-
  ordering concern between them.

  Trailing stop-loss (added 2026-08-11, flat-trail shape as of the second
  pass same day), software-monitored -- checked every 10s run_cycle tick
  using LIVE premium, independent of the 45m candle gating above (a long
  option's premium can move fast within a candle). OR'd with the EMA/RSI/
  ADX exit_condition, not a replacement -- either one closes the leg. % is
  against the OPTION'S OWN premium (current_ltp/entry_px - 1), scales
  with entry price, and RATCHETS on the highest profit % ever reached
  since entry (never loosens back down on a pullback). Flat, constant-gap
  shape: below sl_trail_pct (10%) profit ever reached, SL stays at
  sl_initial_pct (-25%); once profit crosses each 10% step, SL locks
  exactly one step behind:

    Profit reached   Effective SL     Example (entry 259.4, qty 100)
    (none yet)        -25%             194.55 (-Rs.6,485 worst case)
    +10%               0% (breakeven)  259.40 (Rs.0)
    +20%              +10%             285.34 (+Rs.2,594)
    +30%              +20%             311.28 (+Rs.5,188)
    +40%              +30%             337.22 (+Rs.7,782)
    +50%              +40%             363.16 (+Rs.10,376)
    ...continues in the same constant-10-point-gap pattern uncapped
    (+150% locks +140%, etc.)

  See compute_trailing_sl_price() / Config.sl_trail_pct/sl_initial_pct.
  No broker-side resting SL/SL-M order is placed -- a breach triggers the
  exact same _exit_leg()/LIMIT-crossing/error-recovery path as the EMA/
  RSI exit, just with reason="stop_loss". A stop-loss exit also blocks a
  fresh entry on THAT SAME leg (CE stop-out only blocks CE, PE
  unaffected) until the wall-clock candle actually advances, so a
  stopped-out trade can't immediately re-enter against the identical
  stale-candle reading that just stopped it out -- see
  LegState.last_sl_exit_candle_boundary.

Strike selection, expiry, and product
------------------------------------------------------------------------
  - Strike = ATM+1 (one step further OTM than ATM), restricted to ROUND-100
    strikes only (`strike % 100 == 0`) -- see `pick_atm_plus1_round100_leg`.
    Resolved FRESH on every entry, same as the EMA9/RSI reference.
  - Options expiry: nearest upcoming expiry via `client.expiry(...,
    instrumenttype="options")`, rolling to the NEXT expiry once 2 or fewer
    TRADING days remain before it (not calendar days, not the EMA9/RSI
    reference's same-day-only roll) -- see `resolve_current_month_expiry`
    below. Uses `utils/trading_calendar.py`'s `is_trading_day` to count
    trading days; that calendar is not MCX-specific and can differ from
    MCX's actual holiday calendar on a handful of days per year (accepted
    limitation -- this is a risk-buffer feature against near-expiry theta
    decay, not a hard safety cutoff).
  - **Product = NRML, not MIS** (deliberate choice, unlike the EMA9/RSI
    reference) -- an MIS exit placed at/after the broker's own square-off
    cutoff gets rejected outright, racing against this strategy's own exit
    logic (same rationale already applied to the NIFTY OI strategy,
    `Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py`). NRML sidesteps
    that broker-side race; the strategy still behaves intraday in practice
    via `universal_exit_time` below, just without a broker-enforced
    square-off backstop if the strategy's own exit fails for some other
    reason (error_state) -- such a position would carry overnight until a
    human resolves it via Retry/Cancel/Manually Completed.
  - Quantity: 1 lot per leg (lot size fetched dynamically from the option
    chain response, never hardcoded).
  - Trading window: MCX's official session is 09:00-23:55
    (`database/market_calendar_db.py`'s MCX offsets). Entry window
    09:20-22:30; universal exit at 23:15, leaving a buffer for square-off
    order execution before the 23:55 hard close. All config values --
    adjust freely.
  - Max 3 entries per leg per day.

Live price feed: WebSocket, DYNAMIC subscription (like VWAP_NoHA/Batman)
------------------------------------------------------------------------
Unlike NIFTY/SENSEX (whose underlying symbol never changes), this
strategy's underlying is a FUTURES CONTRACT that rolls monthly, and its
option leg symbols aren't known until the strike is resolved at entry --
`PriceStream` here uses the same DYNAMIC `add_instruments()` design as the
EMA9/RSI reference: the front-month futures symbol is (re-)subscribed once
per day in `_reset_day_if_needed`, and each option leg's symbol is added
the moment it's resolved at entry. A same-day restart resuming mid-session
re-subscribes to the already-known futures symbol and any already-open
option legs at startup.

Order placement robustness, trade log, PnL reporting
------------------------------------------------------------------------
Same platform-integration machinery as the EMA9/RSI reference (see that
script's module docstring for the full writeup) -- rejected/cancelled
order handling clears stale order ids instead of looping forever,
`poll_fill()` re-prices a stale unfilled order to current LTP before
giving up, a background thread writes closed trades to
`trades_{STRATEGY_ID}.csv`, and `report_pnl_tick()` pushes a live PnL
snapshot to the OpenAlgo Python Strategy Host. The BUY/SELL action words,
PnL sign, and displayed-quantity sign are all flipped from the reference
throughout (entry=BUY, exit=SELL, pnl=exit_px-entry_px, quantity displayed
positive) -- this is a long option BUYER, not a naked seller. Also carries
the two-sided-quote liquidity guard (bid>0 AND ask>0 required before
trusting a quote's `ltp`) into `pick_atm_plus1_round100_leg`'s strike
selection itself, not just the SL/exit-price reads `fetch_symbol_ltp`
already guards -- `optionchain()`'s raw `ltp` field has no such guarantee
built in (confirmed live, 2026-08-10, on the NIFTY OI strategy: an
illiquid strike's `ltp` came back matching the underlying's own spot
level, with bid=0/ask=0).

`place()` sends **LIMIT crossing the spread, never MARKET** -- confirmed
live 2026-08-11, this broker's API rejects true MARKET orders outright
("ALGO_CHK: MKT Order type not allowed for API order"). The broker-layer
MPP conversion (`broker/shoonya/mapping/transform_data.py`) is meant to
convert MARKET->LIMIT automatically but falls back to sending the raw
(rejected) MKT type through whenever its own quote fetch lacks a genuine
two-sided market -- rather than depend on that always succeeding, this
strategy computes its own crossing price directly (same pattern as the
reprice logic) and never sends MARKET at all.

Base symbol: CRUDEOIL (standard), not CRUDEOILM (Mini) -- confirmed, not
assumed
------------------------------------------------------------------------
MCX lists TWO separate crude oil contract families: the standard 100-barrel
`CRUDEOIL` and the 10-barrel Mini `CRUDEOILM`. Per the OpenAlgo symbol-format
reference, the Mini variant is FUTURES-ONLY -- it has no listed options
segment; only the standard `CRUDEOIL` contract has one. Since this strategy
trades OPTIONS, `INSTRUMENTS[0].name = "CRUDEOIL"` is the only correct
base -- and the EMA/RSI/ADX signal source (the futures contract, see above)
must read the SAME standard `CRUDEOIL` futures, not the Mini.

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.ema(data, period)` -> ndarray. `ta.rsi(data, period=14)` -> ndarray.
  * `ta.adx(high, low, close, period=14)` -> (+DI, -DI, ADX) ndarrays --
    only the ADX (3rd) array is used here, no directional DI check.
  * `client.history(..., interval='45m')` does NOT work -- confirmed live
    2026-08-11, the broker rejects it outright (HTTP 400: "Must be one of:
    1s, 5s, 10s, 15s, 30s, 45s, 1m, 2m, 3m, 5m, 10m, 15m, 20m, 30m, 1h, 2h,
    3h, 4h, D, W, M, Q, Y" -- no 45m). Fetches at `fetch_interval` (15m)
    instead and resamples to `candle_interval_minutes` (45m) locally via
    pandas in `_resample_to_candle_interval` -- see that function and
    `compute_instrument_signal`.

Author
------
<Project Owner>
===============================================================================
"""

import copy
import csv
import json
import logging
import math
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import http.client
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

# Python's default thread stack reservation is 8MB. This process runs
# several threads at once (fill-watchers, the Force Exit background check,
# PnL push, the trade-log writer, plus PriceStream's own watchdog/WS
# threads) -- none of which do anything beyond simple polling loops and
# REST calls, nowhere near deep recursion. At the default size that adds up
# to tens of MB of virtual address space reserved purely for stacks, out of
# the STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap (blueprints/python_strategy.py's
# set_resource_limits(), 1024MB by default) every strategy subprocess runs
# under -- confirmed in production as the actual ceiling behind
# "RuntimeError: can't start new thread" (2026-07-28, on the Combined
# script specifically, but the same risk applies here). Must be called
# before any thread is created; affects every threading.Thread from here
# on, including ones spawned internally by ThreadPoolExecutor.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, notify_whatsapp_error, filter_known_fields
except ImportError:
    # Shared helper (strategies/scripts/_strategy_platform_client.py) not
    # present alongside this script -- e.g. it was copied out standalone.
    # Degrade gracefully: the live "trade just closed" SSE push and WhatsApp
    # failure alerts simply won't fire, but nothing else about the strategy
    # is affected.
    def notify_trade_closed(env, log_warning=None):
        pass

    def notify_whatsapp_error(env, message, log_warning=None):
        pass

    # Same behavior as the shared module's version (see its own docstring) --
    # trivial and dependency-free enough to duplicate exactly rather than
    # degrade, unlike notify_trade_closed above which genuinely needs
    # network/env access it can't have as a standalone fallback.
    def filter_known_fields(cls, raw):
        known = set(vars(cls()).keys())
        return {k: v for k, v in raw.items() if k in known}

try:
    from utils.trading_calendar import is_trading_day
except ImportError:
    # Shared holiday-aware calendar (utils/trading_calendar.py) not
    # importable from this isolated subprocess (no deployed strategy in
    # this project currently relies on cross-module imports beyond the
    # openalgo SDK and _strategy_platform_client above -- untested sys.path
    # for this specific module). Degrade to a weekday-only check (Mon-Fri =
    # trading day) -- no holiday awareness, but the caller
    # (resolve_current_month_expiry) only uses this to count a small
    # buffer of days before a broker-confirmed real expiry date, so being
    # off by a holiday or two in that count is a low-consequence gap, not a
    # correctness-critical one.
    def is_trading_day(day) -> bool:
        return day.weekday() < 5

load_dotenv()

print("OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
@dataclass
class InstrumentConfig:
    name: str                    # "CRUDEOIL" -- change here if your broker lists it differently
    underlying_exchange: str     # "MCX" -- used as the optionchain() underlying-exchange param
    options_exchange: str        # "MCX" -- same exchange; MCX has no NSE_INDEX-style split


INSTRUMENTS = [
    InstrumentConfig(name="CRUDEOIL", underlying_exchange="MCX", options_exchange="MCX"),
]


@dataclass
class Config:
    strategy_name: str = "MCX CRUDEOIL EMA34 + RSI + ADX Intraday Option Buyer"
    version: str = "1.0.0"

    # 2026-08-11: confirmed live the broker rejects "45m" outright (HTTP 400
    # "Must be one of: ...15m, 20m, 30m, 1h..." -- no 45m). Fetches at
    # fetch_interval (15m, a supported interval that divides 45 evenly) and
    # resamples to candle_interval_minutes-minute bars locally in
    # compute_instrument_signal -- see that function for the resample.
    fetch_interval: str = "15m"
    candle_interval_minutes: int = 45  # the actual signal timeframe -- also drives get_signal's _current_candle_boundary()
    history_lookback_days: int = 10   # calendar days of 45m history to fetch (EMA34/RSI14/ADX14 warmup)
    ema_period: int = 34
    rsi_period: int = 14
    adx_period: int = 14
    adx_threshold: float = 25.0       # shared trend-strength gate for both CE and PE entries, no directional DI check
    ce_rsi_entry_threshold: float = 53.0
    pe_rsi_entry_threshold: float = 47.0
    ce_rsi_exit_threshold: float = 47.0
    pe_rsi_exit_threshold: float = 53.0

    # 2026-08-11: trailing stop-loss, checked every run_cycle tick (10s) --
    # OR'd with the EMA/RSI/ADX exit_condition above, not a replacement.
    # % is against the OPTION'S OWN premium (current_ltp/entry_px - 1), not
    # the underlying futures price. Ratchets on the highest profit % ever
    # reached since entry -- see compute_trailing_sl_price().
    #
    # 2026-08-11 (second pass, same day): replaced the original 7-tier
    # table (uneven gaps -- 30pts at the +50% tier, 40pts at +100%) with a
    # flat, constant-gap trail, and tightened the initial floor -50% ->
    # -25% (the -50% initial risked ~Rs.12,970 on a single lot at a 259.4
    # entry, judged too large). Below sl_trail_pct (10%) profit ever
    # reached, SL stays at sl_initial_pct. Once profit crosses each
    # sl_trail_pct step (10%, 20%, 30%, ...), SL locks exactly one step
    # behind -- +10% locks breakeven, +20% locks +10%, +30% locks +20%,
    # ..., +150% locks +140%, continuing the same pattern uncapped beyond
    # that. See compute_trailing_sl_price().
    sl_initial_pct: float = -0.25
    sl_trail_pct: float = 0.10   # step size AND trailing gap AND activation threshold

    strike_round: int = 100           # strike selection restricted to strike % strike_round == 0 only
    # Options expiry rolls to the NEXT expiry once this many TRADING days
    # (not calendar days) remain -- see resolve_current_month_expiry.
    expiry_roll_trading_days_before: int = 2

    lot_multiplier: int = 1
    max_trades_per_leg_per_day: int = 3

    product: str = "NRML"             # not MIS -- see module docstring for why
    # 2026-08-11: MARKET is rejected outright by this broker for API orders
    # ("ALGO_CHK: MKT Order type not allowed for API order", confirmed
    # live) -- place() now always sends LIMIT crossing the spread instead
    # (see place()'s own docstring), so this field is no longer read
    # anywhere; kept only as a documented record of why MARKET isn't used.
    price_type: str = "LIMIT"

    entry_start: time = time(9, 20)
    entry_end: time = time(22, 30)
    universal_exit_time: time = time(23, 15)
    market_open: time = time(9, 0)
    market_close: time = time(23, 55)   # MCX official session, per database/market_calendar_db.py

    scheduler_interval: int = 10
    indicator_refresh_interval: int = 15     # throttle for the 3m EMA/RSI history fetch
    pnl_tick_interval: float = 0.8             # seconds between PnL pushes -- runs on its OWN scheduler
                                               # job (see report_pnl_tick), decoupled from
                                               # scheduler_interval, since it's cache-only/read-only and
                                               # doesn't share the blocking-call risk that interval guards

    # report_pnl_tick()'s WS price cache can stay stale for an EXTENDED
    # period during a genuine broker-side outage (confirmed in production,
    # 2026-07-30: both the WS feed AND REST quotes() failed for a specific
    # NIFTY option contract for 2+ hours, while the broker's OWN historical
    # data endpoint kept working fine -- a real, if unusual, broker-side
    # partial outage). Previously such a leg just vanished from the pushed
    # PnL payload for the ENTIRE outage. Now falls back to a REST quotes()
    # call, throttled to at most once per this interval per leg -- frequent
    # enough to recover visibility within a reasonable window, rare enough
    # that a 1-second job doing this doesn't spam the broker for the whole
    # outage's duration.
    pnl_rest_fallback_interval_sec: float = 900.0   # 15 minutes

    # WebSocket LTP cache: a tick older than this is treated as stale and
    # falls back to a one-off REST client.quotes() call for that symbol.
    ws_stale_seconds: float = 20.0
    ws_watchdog_interval: float = 15.0       # how often the reconnect watchdog checks staleness
    # Consecutive stale watchdog cycles (same symbol, in a row) before giving
    # up on the cheap per-symbol resubscribe and escalating to a full
    # reconnect -- confirmed in production that per-symbol resubscribe alone
    # can retry 30+ times with zero recovery while the connection stays
    # reported connected/authenticated, so something in the connection's own
    # state needs a clean reset, not another poke at the same symbol.
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    # 5s per wait-cycle (1 initial + 59 reprices) = 60 x 5s = 300s (5 min)
    # total before giving up and raising OrderNeedsAttention -- each reprice
    # crosses the spread with a fresh bid/ask (see _reprice_and_wait_once),
    # not just the last-traded price.
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59   # times poll_fill() re-prices a stale unfilled order before giving up

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    # push_leg_error() only pushed once, on the transition into error_state --
    # a single lost POST (server busy, transient network blip) left the UI's
    # error badge silently blank for hours even though state.json correctly
    # tracked the error the whole time (confirmed in production, 2026-07-28:
    # three legs sat in exit_failed for 1-4 hours with no UI error shown).
    # Re-pushing at this interval for every leg still in error_state means a
    # single lost push self-heals within a minute instead of indefinitely.
    error_repush_interval_sec: float = 60.0

    # Minimum gap between WhatsApp alerts fired from run_cycle's own outer
    # except-clause -- without this, a persistently-recurring bug would fire
    # one WhatsApp message per scheduler tick and flood the phone.
    cycle_failure_notify_interval_sec: float = 300.0

    state_file: str = "strategy_state.json"
    log_level: int = logging.INFO

    test_mode: bool = os.getenv("STRATEGY_TEST_MODE", "0") == "1"


config = Config()
IST = pytz.timezone("Asia/Kolkata")

LEG_KEYS = [f"{inst.name}_{opt}" for inst in INSTRUMENTS for opt in ("PE", "CE")]


###############################################################################
# LOGGER
###############################################################################
class Log:
    logger = logging.getLogger("OpenAlgoStrategy")
    logger.setLevel(config.log_level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    @staticmethod
    def info(message):
        Log.logger.info(message)

    @staticmethod
    def warning(message):
        Log.logger.warning(message)

    @staticmethod
    def error(message):
        Log.logger.error(message)

    @staticmethod
    def exception(message):
        """Use inside an `except` block instead of manually building a
        traceback string (import traceback / traceback.format_exc()) -- this
        captures the current exception's traceback via the standard logging
        exc_info mechanism instead of jamming it into the message text."""
        Log.logger.exception(message)


###############################################################################
# MODELS
###############################################################################
@dataclass
class LegPosition:
    symbol: str = ""
    quantity: int = 0
    entry_time: str = ""
    entry_px: float = 0.0          # LTP-based estimate -- feeds the trade log's PnL columns AND
                                    # (2026-08-11) the trailing stop-loss's profit% calculation.
    entry_order_id: str = ""
    entry_filled: bool = False
    exit_order_id: str = ""
    exit_filled: bool = False
    execution_id: int = 0          # which process run OPENED this leg -- captured at entry so a
                                    # mid-position restart still tags the eventual close correctly
    # Order error recovery (see docs/prd/python-strategies-order-error-recovery.md) --
    # set when poll_fill() exhausts its automatic retries. Kept on the position (not
    # a separate structure) so it survives a strategy restart via state.json, same as
    # entry_order_id etc. Cleared once the user resolves it via Retry/Cancel/Manual.
    error_state: str = ""           # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""            # "" | "terminal" (order already dead) | "resting" (still live, unfilled)
    error_order_id: str = ""        # the order id Retry/Cancel act on when error_kind == "resting"
    error_message: str = ""         # last exception text, for display
    error_since: str = ""           # ISO timestamp this error (or its latest re-entry) began
    manual_exit_px: Optional[float] = None  # set only by a "manual" resolution on an exit
    # 2026-08-11: trailing stop-loss ratchet -- highest (current_ltp/entry_px - 1)
    # ever observed since entry. Only ever increases; see compute_trailing_sl_price().
    highest_profit_pct: float = 0.0


@dataclass
class LegState:
    trade_count: int = 0
    position: LegPosition = field(default_factory=LegPosition)
    # 2026-08-11: set to _current_candle_boundary()'s WALL-CLOCK forming-
    # candle boundary at the moment a stop-loss exit fired -- blocks a fresh
    # entry on THIS SAME leg (CE stop-out only blocks CE re-entry, PE is
    # unaffected) until the candle actually advances, so a stopped-out trade
    # can't immediately whipsaw back in against the same stale-candle EMA/
    # RSI/ADX reading that just got it stopped out. Deliberately NOT
    # InstrumentSignal.candle_key (last-CLOSED candle from the cached
    # signal) -- that cache refreshes asynchronously in the background
    # (get_signal/_refresh_signal_and_chain_bg) and can lag the true
    # wall-clock boundary by up to one refresh cycle. Confirmed live
    # 2026-08-11: an SL fired one cycle before the cache had caught up to a
    # boundary that had already passed, stamping the OLD candle_key; the very
    # next cycle's cache refresh landed on the NEW candle_key, made the
    # comparison mismatch, and let a re-entry through 60s after the SL, same
    # physical 45m window. _current_candle_boundary() is pure wall-clock
    # (datetime.now() only, no cache/network dependency) so it can't lag.
    # Reset daily in _reset_day_if_needed alongside trade_count.
    last_sl_exit_candle_boundary: str = ""


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    last_updated: str = ""
    today_realized_pnl: float = 0.0  # sum of closed legs' pnl_rupees today -- pushed via report_pnl_to_platform
    futures_symbol: str = ""         # current-month futures contract, resolved once/day -- see _reset_day_if_needed
    last_execution_id: int = 0       # incremented once per process start (see main()) -- the Trades UI's
                                      # execution dropdown groups trades_{id}.csv rows by this number


###############################################################################
# ENVIRONMENT
###############################################################################
class Environment:
    def __init__(self):
        self.api_key = os.getenv("OPENALGO_API_KEY")
        self.host = (
            os.getenv("HOST_SERVER")
            or os.getenv("OPENALGO_HOST")
            or "http://127.0.0.1:5000"
        )
        self.version = "v1"
        # Was 120.0 -- far too long for a call made synchronously inside
        # run_cycle() (indicator/chain refresh, LTP fallback). A single broker
        # hiccup could block the whole scheduler for up to 2 minutes PER call,
        # and several such calls stacking in one cycle caused a real ~11-minute
        # stall in production (2026-07-24). Order placement/poll_fill already
        # have their own retry/reprice loops on top of this, so a short
        # per-call ceiling just makes a stuck call fail fast instead of
        # hanging -- it doesn't change normal-case behavior at all.
        self.timeout = 10.0
        # A single-symbol quotes() call (the WS-stale LTP fallback used
        # directly in run_cycle's main-thread loop) should never legitimately
        # need anywhere near 10s -- a healthy broker answers it in well under
        # a second. Giving it its own short ceiling (via a second lightweight
        # client, see Broker.connect_ltp_client) bounds run_cycle's own
        # worst-case wall-clock time much tighter than sharing the 10s
        # history()/optionchain() timeout would.
        self.ltp_timeout = 3.0
        self.ws_url = os.getenv("WEBSOCKET_URL")
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "mcx_crudeoil_ema34_rsi_adx_intraday"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    """Shared by StrategyEngine and PriceStream's reconnect watchdog -- MCX
    goes quiet outside its own session, so staleness checks must not fire
    (and force pointless reconnects) outside 09:00-23:55."""
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return config.market_open <= now <= config.market_close


def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle -- e.g.
    for interval_minutes=3 at 16:16:42 IST, returns 16:15:00. Used by
    get_signal() to detect "a new candle has just closed" precisely,
    instead of relying solely on a rolling indicator_refresh_interval timer
    that has no awareness of where the actual 3-minute candle boundaries
    fall -- that rolling-only approach fetches ~12 times per 3m candle
    (180s / 15s), ~11 of them wasted (the candle hasn't closed yet), and
    the one useful fetch can land anywhere up to indicator_refresh_interval
    late relative to the true close, depending on timing phase. Comparing
    against this boundary lets get_signal() fetch on the very first cycle
    after a new bucket begins, cutting both the wasted-fetch count and the
    worst-case detection latency down to ~scheduler_interval (10s) + one
    broker round-trip."""
    now = datetime.now(IST)
    total_minutes = now.hour * 60 + now.minute
    bucket_start_minutes = (total_minutes // interval_minutes) * interval_minutes
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=bucket_start_minutes)


def _candle_key_boundary(candle_key: str) -> Optional[datetime]:
    """Parses a signal's candle_key (str(bars.index[-1]), e.g.
    '2026-07-29 16:15:00+05:30') back into a datetime for comparison
    against _current_candle_boundary()'s output -- lets get_signal() know
    whether the CACHED signal already reflects the current candle, or is
    still one (or more) candle(s) behind. Returns None if parsing ever
    fails (unexpected format) -- callers must treat that as "don't know,"
    never as "have fresh data," so a parse failure can't silently suppress
    a needed refresh."""
    try:
        return datetime.fromisoformat(candle_key)
    except (ValueError, TypeError):
        return None


###############################################################################
# BROKER
###############################################################################
class Broker:
    def __init__(self, env: Environment):
        self.env = env
        self.client: Optional[api] = None

    def connect(self):
        self.env.validate()
        self.client = api(
            api_key=self.env.api_key,
            host=self.env.host,
            version=self.env.version,
            timeout=self.env.timeout,
            ws_url=self.env.ws_url,
            # PriceStream._watchdog_loop already owns full reconnect +
            # resubscribe on this same client. Leaving the SDK's own
            # auto_reconnect thread enabled races it: both call
            # _do_connect() on self.ws independently, which can tear
            # down/replace the socket concurrently and immediately trigger
            # another spurious close -- observed in production as a
            # ~45-50s repeating "connection down" cycle that never
            # settles. The watchdog is the single owner of reconnect.
            auto_reconnect=False,
        )
        Log.info("Connected to OpenAlgo")
        return self.client

    def connect_ltp_client(self):
        """A second, independent client used ONLY for the WS-stale LTP
        fallback (quotes()) -- deliberately NOT sharing self.client's
        self.timeout attribute, since that's a plain mutable instance
        attribute read at call-time; mutating it around one call while a
        background thread is mid-flight on the same client would race. A
        second lightweight httpx-backed client is cheap (no auth handshake,
        just a pooled HTTP client) and avoids that entirely."""
        return api(
            api_key=self.env.api_key,
            host=self.env.host,
            version=self.env.version,
            timeout=self.env.ltp_timeout,
            ws_url=self.env.ws_url,
        )

    @property
    def connected(self):
        return self.client is not None


###############################################################################
# LIVE PRICE STREAM (WebSocket, DYNAMIC subscription -- see module docstring's
# "Live price feed" section for why this differs from Pivot+Supertrend/
# EMA34_RSI's fixed-list PriceStream)
###############################################################################
class PriceStream:
    """Subscribes to LTP mode for a DYNAMICALLY growing set of symbols
    (the front-month futures contract, resolved once/day, plus each option
    leg's symbol as it's resolved at entry) over OpenAlgo's shared
    WebSocket proxy. Keeps an in-memory, thread-safe
    {(symbol, exchange): (ltp, tick_time)} cache updated by the push
    callback. A background watchdog thread detects a stale/silent feed
    during market hours and reconnects (full reconnect if the connection
    itself is down, or a per-symbol resubscribe -- leaving every other
    symbol's feed undisturbed -- if only some symbol(s) are stale while the
    connection is otherwise healthy). If a symbol stays stale across
    several consecutive cycles despite that per-symbol resubscribe,
    escalates to a full reconnect instead (see _watchdog_loop) -- confirmed
    in production that the per-symbol path alone can retry 30+ times with
    zero recovery while the connection itself stays reported
    connected/authenticated the whole time, so something in that
    connection's own state needs a clean reset, not another poke at the
    same symbol."""

    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple] = {}
        self._instruments: dict[tuple, dict] = {}
        self._stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        # Defaults to the static module-level check (market_open/market_close
        # bounds only) -- main() upgrades this to the MCX-holiday/special-
        # session-aware StrategyEngine._within_market_hours once the engine
        # exists, via set_market_hours_check(). Without this upgrade, the
        # watchdog would keep treating a full MCX holiday (or an early-close
        # special session) as "should be live," repeatedly attempting
        # reconnects/resubscribes against a feed that legitimately has no
        # ticks coming for the rest of the static window.
        self._market_hours_fn = _within_market_hours
        # Consecutive watchdog cycles a given (symbol, exchange) has been
        # found stale IN A ROW -- see _watchdog_loop's escalation policy.
        self._stale_streak: dict[tuple, int] = {}
        # Per-symbol backoff for the per-symbol resubscribe retry path --
        # independent of any OTHER symbol's own backoff, so one chronically
        # stale symbol's growing wait never slows down retries for a
        # different symbol that just started going stale.
        self._symbol_backoff_step: dict[tuple, int] = {}
        self._symbol_next_retry_at: dict[tuple, datetime] = {}

    def _on_tick(self, msg):
        try:
            symbol = msg["symbol"]
            exchange = msg.get("exchange", "")
            ltp = float(msg["data"]["ltp"])
        except (KeyError, TypeError, ValueError) as exc:
            Log.warning(f"[PriceStream] malformed tick ignored: {exc} ({msg})")
            return
        with self._lock:
            self._cache[(symbol, exchange)] = (ltp, datetime.now(IST))

    def get_ltp(self, symbol: str, exchange: str, max_age: float) -> Optional[float]:
        with self._lock:
            entry = self._cache.get((symbol, exchange))
        if entry is None:
            return None
        ltp, ts = entry
        if (datetime.now(IST) - ts).total_seconds() > max_age:
            return None
        return ltp

    def set_market_hours_check(self, fn):
        """Called once from main() after the StrategyEngine exists, to swap
        the watchdog's market-hours check from the static module-level
        function to the engine's MCX-holiday/special-session-aware version
        (StrategyEngine._within_market_hours). The watchdog thread reads
        self._market_hours_fn fresh on every loop iteration, so this is safe
        to call after start() -- the very next check picks it up."""
        self._market_hours_fn = fn

    def add_instruments(self, instruments: list):
        new_ones = []
        with self._lock:
            for inst in instruments:
                key = (inst["symbol"], inst["exchange"])
                if key not in self._instruments:
                    self._instruments[key] = inst
                    new_ones.append(inst)
        if not new_ones:
            return
        try:
            self.client.subscribe_ltp(new_ones, on_data_received=self._on_tick)
            Log.info(f"[PriceStream] subscribed: {new_ones}")
        except Exception as exc:
            Log.warning(f"[PriceStream] subscribe failed for {new_ones}: {exc}")

    def remove_instruments(self, instruments: list):
        """Called once a leg's option symbol is fully closed -- without this,
        every distinct strike traded in a day stays subscribed and watched by
        _watchdog_loop for the rest of this long-lived process's life, an
        unbounded (across days) WS-subscription accumulation."""
        removed = []
        with self._lock:
            for inst in instruments:
                key = (inst["symbol"], inst["exchange"])
                if key in self._instruments:
                    del self._instruments[key]
                    self._cache.pop(key, None)
                    self._stale_streak.pop(key, None)
                    self._symbol_backoff_step.pop(key, None)
                    self._symbol_next_retry_at.pop(key, None)
                    removed.append(inst)
        if not removed:
            return
        try:
            self.client.unsubscribe_ltp(removed)
            Log.info(f"[PriceStream] unsubscribed: {removed}")
        except Exception as exc:
            Log.warning(f"[PriceStream] unsubscribe failed for {removed}: {exc}")

    def _connect(self):
        self.client.connect()
        with self._lock:
            all_instruments = list(self._instruments.values())
        if all_instruments:
            self.client.subscribe_ltp(all_instruments, on_data_received=self._on_tick)
        Log.info(f"[PriceStream] connected"
                 + (f" and (re)subscribed: {all_instruments}" if all_instruments else " (no symbols known yet)"))

    def _teardown(self):
        try:
            with self._lock:
                all_instruments = list(self._instruments.values())
            if all_instruments:
                self.client.unsubscribe_ltp(all_instruments)
        except Exception as exc:
            Log.warning(f"[PriceStream] unsubscribe_ltp failed during teardown: {exc}")
        try:
            self.client.disconnect()
        except Exception as exc:
            Log.warning(f"[PriceStream] disconnect failed during teardown: {exc}")

    def _confirm_genuinely_broken_via_rest(self, stale_instruments: list) -> bool:
        """Before paying the cost of a disruptive full reconnect (which
        briefly drops EVERY tracked symbol's stream, not just the stuck
        ones), confirm via a REST quotes() call that at least one stale
        symbol's price has genuinely moved since its last cached tick. If
        it has, the WS feed really is failing to deliver and a reconnect
        is warranted. If REST also shows the SAME frozen price for every
        stale symbol, they simply aren't trading right now (thin
        liquidity, confirmed in production 2026-07-29 for a single MCX
        option leg) -- reconnecting cannot fix that, so the caller should
        skip it and let per-symbol backoff keep retrying instead.

        A REST call that itself fails/errors counts as "can't confirm
        either way" and is skipped for that symbol (not treated as proof
        of brokenness) -- this only needs ONE symbol to show real
        movement to return True."""
        for inst in stale_instruments:
            key = (inst["symbol"], inst["exchange"])
            cached = self._cache.get(key)
            try:
                resp = self.client.quotes(symbol=inst["symbol"], exchange=inst["exchange"])
            except Exception as exc:
                Log.warning(f"[PriceStream] REST confirm-check failed for "
                            f"{inst['symbol']}.{inst['exchange']}: {exc}")
                continue
            data = resp.get("data", resp) if isinstance(resp, dict) else resp
            rest_ltp = data.get("ltp") if isinstance(data, dict) else None
            if rest_ltp is None:
                continue
            if cached is None or abs(float(rest_ltp) - cached[0]) > 1e-9:
                return True
        return False

    def _watchdog_loop(self):
        """Two-tier recovery, escalating only when warranted:

        1. Connection-down (transport itself dead) -> full reconnect
           immediately.
        2. Connection alive, but some symbols' ticks are stale -> per-symbol
           resubscribe, each on its OWN independent backoff
           (_symbol_backoff_step/_symbol_next_retry_at). Only escalates to a
           full reconnect if a MAJORITY of tracked symbols are
           simultaneously stuck past ws_stale_reconnect_after consecutive
           cycles, AND _confirm_genuinely_broken_via_rest() shows real
           price movement the WS feed isn't delivering -- not just thin
           liquidity. Confirmed in production, 2026-07-29: a single
           thinly-traded MCX option leg stayed stale for an extended
           stretch while the futures contract on the SAME connection
           ticked fine the whole time -- the OLD "any one symbol
           escalates" rule would force repeated full reconnects that could
           never fix a liquidity problem, disrupting the healthy futures
           stream for nothing.
        """
        backoffs = (1, 2, 5, 10, 30)
        failures = 0
        try:
            self._connect()
        except Exception as exc:
            Log.warning(f"[PriceStream] initial connect failed: {exc}")

        while not self._stop.is_set():
            self._stop.wait(config.ws_watchdog_interval)
            if self._stop.is_set():
                break
            if not self._market_hours_fn():
                continue

            if not (getattr(self.client, "connected", False)
                    and getattr(self.client, "authenticated", False)):
                failures += 1
                wait = backoffs[min(failures - 1, len(backoffs) - 1)]
                Log.warning(
                    f"[PriceStream] connection down (attempt {failures}) -- "
                    f"reconnecting fully, then waiting {wait}s."
                )
                self._teardown()
                try:
                    self._connect()
                except Exception as exc:
                    Log.warning(f"[PriceStream] reconnect failed: {exc}")
                self._stop.wait(wait)
                continue

            now = datetime.now(IST)
            stale_instruments = []
            with self._lock:
                for key, inst in self._instruments.items():
                    entry = self._cache.get(key)
                    if entry is None or (now - entry[1]).total_seconds() > config.ws_stale_seconds:
                        stale_instruments.append(inst)

            with self._lock:
                all_keys = set(self._instruments.keys())
            stale_keys = {(i["symbol"], i["exchange"]) for i in stale_instruments}
            for key in all_keys:
                if key in stale_keys:
                    self._stale_streak[key] = self._stale_streak.get(key, 0) + 1
                else:
                    self._stale_streak[key] = 0
                    self._symbol_backoff_step.pop(key, None)
                    self._symbol_next_retry_at.pop(key, None)

            if not stale_instruments:
                failures = 0
                continue

            names = ", ".join(f"{i['symbol']}.{i['exchange']}" for i in stale_instruments)
            symbols_at_limit = {
                k for k in stale_keys
                if self._stale_streak[k] >= config.ws_stale_reconnect_after
            }

            if len(symbols_at_limit) > len(all_keys) / 2:
                if self._confirm_genuinely_broken_via_rest(stale_instruments):
                    failures += 1
                    wait = backoffs[min(failures - 1, len(backoffs) - 1)]
                    Log.warning(
                        f"[PriceStream] {names} stale for {config.ws_stale_reconnect_after}+ "
                        f"consecutive cycles on {len(symbols_at_limit)}/{len(all_keys)} tracked "
                        f"symbols (a majority), REST-confirmed as genuinely broken -- "
                        f"escalating to a full reconnect."
                    )
                    self._teardown()
                    try:
                        self._connect()
                    except Exception as exc:
                        Log.warning(f"[PriceStream] full reconnect (escalation) failed: {exc}")
                    for key in all_keys:
                        self._stale_streak[key] = 0
                        self._symbol_backoff_step.pop(key, None)
                        self._symbol_next_retry_at.pop(key, None)
                    self._stop.wait(wait)
                    continue
                Log.warning(
                    f"[PriceStream] {names} stale on {len(symbols_at_limit)}/{len(all_keys)} "
                    f"tracked symbols, but REST shows no price movement -- likely thin "
                    f"liquidity, not a broken feed. Skipping full reconnect; continuing "
                    f"per-symbol retries."
                )

            due_for_retry = [
                inst for inst in stale_instruments
                if now >= self._symbol_next_retry_at.get(
                    (inst["symbol"], inst["exchange"]), now
                )
            ]
            if not due_for_retry:
                continue

            due_names = ", ".join(f"{i['symbol']}.{i['exchange']}" for i in due_for_retry)
            Log.warning(f"[PriceStream] stale/missing ticks for: {due_names} -- "
                        f"resubscribing just this/these symbol(s).")
            # 2026-07-30: dropped the unsubscribe_ltp() call that used to run
            # before this subscribe. Fyers' HSM protocol has no real
            # per-symbol unsubscribe -- unsubscribe_symbols() only clears
            # OpenAlgo's own tracking, it never tells Fyers to actually stop
            # the token. So every 15-30s retry cycle was telling Fyers
            # "give me this token" for a token Fyers already considered
            # active, right after wiping our own bookkeeping for it --
            # confirmed in production that this redundant unsub/resub
            # churn, repeated for minutes, never once self-recovered the
            # feed, while a single clean subscribe (no preceding
            # unsubscribe) via manual /websocket/test consistently did,
            # every time it was tried. A subscribe to an already-subscribed
            # token is a safe, idempotent re-affirmation on its own.
            try:
                self.client.subscribe_ltp(due_for_retry, on_data_received=self._on_tick)
            except Exception as exc:
                Log.warning(f"[PriceStream] resubscribe (stale symbols) failed: {exc}")

            for inst in due_for_retry:
                key = (inst["symbol"], inst["exchange"])
                step = self._symbol_backoff_step.get(key, 0)
                wait = backoffs[min(step, len(backoffs) - 1)]
                self._symbol_backoff_step[key] = step + 1
                self._symbol_next_retry_at[key] = now + timedelta(seconds=wait)

    def start(self):
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="price-stream-watchdog", daemon=True
        )
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        self._teardown()


###############################################################################
# STATE STORE
###############################################################################
class StateStore:
    def __init__(self, env: Environment):
        base_name = Path(config.state_file).stem
        self.path = Path(__file__).resolve().parent / f"{base_name}_{env.strategy_tag}.json"
        self.state = StrategyState()

    def load(self):
        if not self.path.exists():
            self.save()
            return self.state
        with self.path.open("r") as fp:
            data = json.load(fp)
        self.state = StrategyState()
        self.state.current_day = data.get("current_day", "")
        self.state.last_updated = data.get("last_updated", "")
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.futures_symbol = data.get("futures_symbol", "")
        self.state.last_execution_id = data.get("last_execution_id", 0)
        legs_data = data.get("legs", {})
        for key in LEG_KEYS:
            leg_raw = legs_data.get(key, {})
            leg = LegState()
            leg.trade_count = leg_raw.get("trade_count", 0)
            leg.last_sl_exit_candle_boundary = leg_raw.get("last_sl_exit_candle_boundary", "")
            pos_raw = leg_raw.get("position", {})
            leg.position = LegPosition(**{**asdict(LegPosition()), **filter_known_fields(LegPosition, pos_raw)})
            self.state.legs[key] = leg
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "futures_symbol": self.state.futures_symbol,
            "last_execution_id": self.state.last_execution_id,
            "legs": {
                key: {
                    "trade_count": leg.trade_count,
                    "last_sl_exit_candle_boundary": leg.last_sl_exit_candle_boundary,
                    "position": asdict(leg.position),
                }
                for key, leg in self.state.legs.items()
            },
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=4)


###############################################################################
# HELPERS
###############################################################################
def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def _trading_days_between(start: "datetime.date", end: "datetime.date") -> int:
    """Count of trading days from `start` up to and including `end` --
    e.g. start==end returns 1 if that day is itself a trading day, 0
    otherwise. Used to measure "how many trading days remain until
    expiry" without assuming calendar days == trading days (weekends,
    and where available, holidays)."""
    count = 0
    d = start
    while d <= end:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def resolve_current_month_expiry(client, inst: InstrumentConfig) -> str:
    """Nearest upcoming OPTIONS expiry (DDMMMYY) for CRUDEOIL -- EXCEPT once
    config.expiry_roll_trading_days_before (2) or fewer TRADING days remain
    before it, when it rolls to the NEXT expiry instead (avoids holding a
    long option through its heaviest theta-decay days -- more consequential
    for a premium BUYER than the EMA9/RSI reference's naked seller, which
    only rolls on the exact expiry day itself). Trading-day count uses
    `is_trading_day` (see its own import/fallback above)."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve options expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    for i, raw in enumerate(dates_raw):
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            trading_days_left = _trading_days_between(today, d)
            if trading_days_left <= config.expiry_roll_trading_days_before:
                if i + 1 < len(dates_raw):
                    return _compact_expiry(dates_raw[i + 1])
                # Broker's expiry list ends within the roll buffer with no
                # later date to roll to -- silently falling through to the
                # soon-to-expire contract is exactly what this function
                # exists to avoid. Raise loudly instead of trading it.
                raise RuntimeError(
                    f"{inst.name}: nearest options expiry ({d}) is within "
                    f"{config.expiry_roll_trading_days_before} trading day(s) and the "
                    f"broker returned no later expiry date to roll to -- refusing to "
                    f"silently trade a contract this close to expiry."
                )
            return _compact_expiry(raw)
    return _compact_expiry(dates_raw[-1])


def resolve_current_month_futures(client, inst: InstrumentConfig) -> str:
    """Resolve the nearest-expiry FUTURES symbol (`[Base][DDMMMYY]FUT`) for
    the underlying -- CRUDEOIL has no quotable spot/index (unlike
    NIFTY/SENSEX, which have NSE_INDEX/BSE_INDEX), so this strategy's
    EMA/RSI signal reads the near-month FUTURES contract's own price series
    directly. Resolved ONCE PER DAY (see StrategyEngine._reset_day_if_needed)
    since the front-month contract only changes on monthly rollover, not
    intraday."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="futures")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve futures expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    for raw in dates_raw:
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            return f"{inst.name}{_compact_expiry(raw)}FUT"
    return f"{inst.name}{_compact_expiry(dates_raw[-1])}FUT"


def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
    """`require_two_sided=True` additionally requires bid>0 AND ask>0 before
    trusting the quote -- defends against a quote that looks like it belongs
    to a DIFFERENT instrument than requested (confirmed in production
    2026-08-10 on this same broker: an option's LTP came back matching its
    underlying's spot level, with bid=0/ask=0). Pass True only for
    TRADABLE-instrument reads -- an INDEX symbol legitimately has no bid/ask
    (no order book), so leave this False (default) for underlying-spot
    lookups. See docs/CUSTOMIZATIONS.md."""
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() failed for {symbol}: {exc}")
        return None
    if _is_error_response(resp) and resp.get("status") != "success":
        Log.warning(f"quotes() error response for {symbol}: {resp}")
        return None
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(data, dict):
        return None
    ltp = data.get("ltp")
    if ltp is None:
        return None
    ltp = float(ltp)
    if require_two_sided:
        try:
            bid = float(data.get("bid") or 0)
            ask = float(data.get("ask") or 0)
        except (TypeError, ValueError) as exc:
            Log.warning(f"fetch_symbol_ltp: malformed bid/ask for {symbol}.{exchange} "
                        f"(bid={data.get('bid')!r}, ask={data.get('ask')!r}): {exc} -- treating as untrustworthy")
            return None
        if not (ltp > 0 and bid > 0 and ask > 0):
            Log.warning(
                f"fetch_symbol_ltp: quote for {symbol}.{exchange} lacks a two-sided "
                f"market (ltp={ltp}, bid={bid}, ask={ask}) -- treating as untrustworthy"
            )
            return None
    return ltp


def fetch_symbol_bid_ask(client, symbol: str, exchange: str) -> tuple[Optional[float], Optional[float]]:
    """Used only by the reprice loop (_reprice_and_wait_once) -- crossing the
    spread with a fresh bid/ask is a genuinely different, more aggressive
    price than the last-traded price fetch_symbol_ltp returns, and is what
    actually gets a resting order filled rather than just re-quoting the
    same stale level."""
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() (bid/ask) failed for {symbol}: {exc}")
        return None, None
    if _is_error_response(resp) and resp.get("status") != "success":
        Log.warning(f"quotes() (bid/ask) error response for {symbol}: {resp}")
        return None, None
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(data, dict):
        return None, None
    bid = data.get("bid")
    ask = data.get("ask")
    return (float(bid) if bid is not None else None,
            float(ask) if ask is not None else None)


@dataclass
class InstrumentSignal:
    close_prev1: float
    ema_high_prev1: float
    ema_low_prev1: float
    rsi_prev1: float
    adx_prev1: float
    vwap_prev1: float
    ltp: float
    candle_key: str


# Log the [inst] candle=... line only when the candle actually advances, not
# on every indicator_refresh_interval refetch (same fix applied to the other
# 3 strategies in this project).
_last_logged_candle: dict[str, str] = {}


def _session_vwap(bars) -> np.ndarray:
    """Session-anchored VWAP over `bars` (a DataFrame with a tz-aware
    DatetimeIndex and open/high/low/close/volume columns), resetting at each
    calendar day's first bar. No existing session-VWAP helper was found in
    `openalgo.ta` (same finding as CRUDEOIL_VWAP_EMA20_Positional_1, which
    this is copied from), so computed directly:
    cumsum(typical_price * volume) / cumsum(volume), with both cumulative
    sums reset to zero at the first bar of every new IST calendar day."""
    typical = (bars["high"].to_numpy(dtype=float) + bars["low"].to_numpy(dtype=float)
               + bars["close"].to_numpy(dtype=float)) / 3.0
    volume = bars["volume"].to_numpy(dtype=float) if "volume" in bars.columns else np.ones(len(bars))
    # Zero/missing volume bars would make VWAP undefined (0/0) -- fall back
    # to the typical price itself for that bar's weight, same as treating a
    # single share/contract as having traded, rather than propagating NaN
    # through the whole session's cumulative sums.
    volume = np.where(volume <= 0, 1.0, volume)

    session_dates = np.array([ts.tz_convert(IST).date() if ts.tzinfo else ts.date()
                               for ts in bars.index])

    vwap = np.zeros(len(bars))
    cum_pv = 0.0
    cum_v = 0.0
    current_session = None
    for i in range(len(bars)):
        if session_dates[i] != current_session:
            current_session = session_dates[i]
            cum_pv = 0.0
            cum_v = 0.0
        cum_pv += typical[i] * volume[i]
        cum_v += volume[i]
        vwap[i] = cum_pv / cum_v if cum_v > 0 else typical[i]
    return vwap


def _resample_to_candle_interval(bars, interval_minutes: int):
    """Aggregate fetch_interval (15m, broker-supported) bars up to
    candle_interval_minutes (45m, NOT broker-supported -- confirmed live
    2026-08-11, HTTP 400 'Must be one of: ...15m, 20m, 30m, 1h...') bars.
    origin='start_day' anchors bins to midnight, matching
    _current_candle_boundary()'s own midnight-based bucketing exactly --
    MCX's 09:00 session open lands exactly on a midnight-aligned 45-minute
    boundary (540 minutes = 12*45), so this never misaligns against the
    candle-boundary detection get_signal relies on."""
    resampled = bars.resample(f"{interval_minutes}min", origin="start_day").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum" if "volume" in bars.columns else "first",
    })
    return resampled.dropna(subset=["open", "high", "low", "close"])


def compute_instrument_signal(client, inst: InstrumentConfig, futures_symbol: str,
                               ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch fetch_interval (15m) history for the CURRENT MONTH FUTURES
    CONTRACT (not the bare underlying name -- CRUDEOIL has no quotable
    spot/index, see module docstring), resample locally to
    candle_interval_minutes (45m -- see _resample_to_candle_interval, the
    broker itself rejects 45m directly), then compute EMA(High,34),
    EMA(Low,34), RSI(14), ADX(14) off the last genuinely CLOSED resampled
    bars, plus live LTP. Returns None (with a logged reason) if anything is
    unavailable."""
    end = datetime.now(IST).date()

    raw_bars = client.history(
        symbol=futures_symbol, exchange=inst.options_exchange,
        interval=config.fetch_interval,
        start_date=(end - timedelta(days=config.history_lookback_days)).isoformat(),
        end_date=end.isoformat(),
    )
    if _is_error_response(raw_bars):
        Log.warning(f"[{inst.name}] {config.fetch_interval} history error response for {futures_symbol}: {raw_bars}")
        return None
    if raw_bars is None or raw_bars.empty:
        Log.warning(f"[{inst.name}] empty {config.fetch_interval} history for {futures_symbol}.")
        return None

    bars = _resample_to_candle_interval(raw_bars, config.candle_interval_minutes)
    if bars.empty:
        Log.warning(f"[{inst.name}] resample to {config.candle_interval_minutes}m produced no bars "
                    f"from {len(raw_bars)} {config.fetch_interval} bars.")
        return None
    # Drop the still-forming last candle -- the broker's last bar keeps
    # updating well past its nominal close, same defensive pattern used
    # everywhere else in this codebase. Applies post-resample: a 45m bucket
    # built from a still-incomplete run of 15m bars (e.g. only 1-2 of the 3
    # expected) is exactly as "still forming" as a raw still-forming bar
    # would be.
    if len(bars) >= 2:
        bars = bars.iloc[:-1]
    warmup_needed = max(config.ema_period, config.adx_period) + 2
    if len(bars) < warmup_needed:
        Log.warning(
            f"[{inst.name}] only {len(bars)} {config.candle_interval_minutes}m bars after dropping "
            f"the still-forming one (need >= {warmup_needed} for a stable "
            f"EMA{config.ema_period}/ADX{config.adx_period}) -- no signal."
        )
        return None

    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)

    ema_high = np.asarray(ta.ema(high, config.ema_period))
    ema_low = np.asarray(ta.ema(low, config.ema_period))
    rsi = np.asarray(ta.rsi(close, config.rsi_period))
    _plus_di, _minus_di, adx = ta.adx(high, low, close, config.adx_period)
    adx = np.asarray(adx)
    vwap = _session_vwap(bars)

    candle_key = str(bars.index[-1])

    if ltp is None:
        ltp = fetch_symbol_ltp(client, futures_symbol, inst.options_exchange)
    if ltp is None:
        ltp = float(close[-1])  # fallback: closed-candle close is a reasonable proxy

    signal = InstrumentSignal(
        close_prev1=float(close[-1]),
        ema_high_prev1=float(ema_high[-1]), ema_low_prev1=float(ema_low[-1]),
        rsi_prev1=float(rsi[-1]), adx_prev1=float(adx[-1]), vwap_prev1=float(vwap[-1]),
        ltp=ltp, candle_key=candle_key,
    )

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] futures={futures_symbol} candle={candle_key} close={signal.close_prev1:.2f} "
            f"ema_high{config.ema_period}={signal.ema_high_prev1:.2f} ema_low{config.ema_period}={signal.ema_low_prev1:.2f} "
            f"rsi={signal.rsi_prev1:.2f} adx={signal.adx_prev1:.2f} vwap={signal.vwap_prev1:.2f} ltp={ltp:.2f}"
        )
    return signal


def fetch_chain(client, inst: InstrumentConfig, expiry: str, underlying_symbol: str):
    """`underlying_symbol` must be an actually QUOTABLE instrument -- unlike
    NIFTY/SENSEX (real quotable indices), CRUDEOIL has no bare quotable
    symbol on MCX, confirmed live: optionchain(underlying="CRUDEOIL", ...)
    fails with "Symbol 'CRUDEOIL' not found for exchange 'MCX'" because the
    endpoint needs to fetch the underlying's LTP to anchor ATM selection.
    The current-month FUTURES contract (e.g. "CRUDEOIL19AUG26FUT") IS
    quotable, so that's what's passed here -- NOT inst.name."""
    resp = client.optionchain(
        # strike_count=6 -- pick_atm_plus1_round100_leg needs several REAL
        # listed strikes past ATM to guarantee at least 2 round-100 strikes
        # are present after filtering (CRUDEOIL's native listing spacing is
        # narrower than 100, same situation as NIFTY's 50pt listing under a
        # 100pt trading convention -- see today's NIFTY OI strategy work).
        # Still far smaller than a full-chain fetch, keeping the same
        # "don't fan out to more broker quote calls than needed" intent as
        # the EMA9/RSI reference's strike_count=1 comment above.
        underlying=underlying_symbol, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=6,
    )
    if resp.get("status") != "success":
        raise RuntimeError(f"optionchain failed for {underlying_symbol}: {resp}")
    return resp


def _legs_with_strike(chain: dict, option_type: str) -> list:
    key = option_type.lower()
    legs = []
    for row in chain["chain"]:
        leg = row.get(key)
        if leg:
            merged = dict(leg)
            merged["strike"] = row["strike"]
            legs.append(merged)
    return legs


def pick_atm_plus1_round100_leg(chain: dict, option_type: str, spot: float) -> dict:
    """Strike = ATM+1: restrict to ROUND-100 strikes only (strike % 100 ==
    0), find the nearest (ATM) among those, then return the next one
    further OTM in option_type's direction -- one step further OTM than
    ATM, never the ATM strike itself.

    Liquidity guard (the "LTP issue fix", same pattern as today's NIFTY OI
    strategy's select_monthly_delta_strike): a leg is only eligible if it
    has a genuine two-sided market (bid>0 AND ask>0) -- optionchain()'s raw
    `ltp` field has no guarantee it reflects a real trade for an illiquid
    strike; confirmed live that an illiquid strike can carry a stale/
    fallback `ltp` matching the underlying's own price instead of a real
    premium, with bid=0/ask=0. A round-100 strike that fails this guard is
    treated as absent, same as if it weren't listed at all."""
    legs = _legs_with_strike(chain, option_type)
    liquid_round100 = [
        leg for leg in legs
        if leg["strike"] % config.strike_round == 0
        and leg.get("bid") and leg.get("ask") and leg.get("ltp")
    ]
    if not liquid_round100:
        raise RuntimeError(
            f"No liquid round-{config.strike_round} {option_type} legs found in chain (spot={spot})"
        )
    liquid_round100.sort(key=lambda l: l["strike"])
    atm_leg = min(liquid_round100, key=lambda l: abs(l["strike"] - spot))
    atm_index = liquid_round100.index(atm_leg)
    direction = 1 if option_type == "CE" else -1
    next_index = atm_index + direction
    if not (0 <= next_index < len(liquid_round100)):
        raise RuntimeError(
            f"ATM round-{config.strike_round} {option_type} leg (strike={atm_leg['strike']}) has no "
            f"further-OTM round-{config.strike_round} neighbor in the fetched chain -- widen strike_count."
        )
    otm1_leg = liquid_round100[next_index]
    # Sanity check: the OTM+1 leg must actually be further OTM than ATM in
    # option_type's direction (CE: higher strike; PE: lower strike) -- a
    # malformed/duplicate chain row could otherwise silently pick a strike
    # that isn't genuinely further OTM.
    if (option_type == "CE" and otm1_leg["strike"] <= atm_leg["strike"]) or \
       (option_type == "PE" and otm1_leg["strike"] >= atm_leg["strike"]):
        raise RuntimeError(
            f"ATM+1 {option_type} leg resolution produced a non-OTM strike "
            f"(atm={atm_leg['strike']}, atm+1={otm1_leg['strike']}) -- refusing to trade it."
        )
    return otm1_leg


def compute_trailing_sl_price(entry_px: float, highest_profit_pct: float) -> float:
    """Effective stop-loss price for a long option leg -- flat, constant-
    gap trail (2026-08-11, second pass). Below config.sl_trail_pct (10%)
    profit ever reached, SL stays at config.sl_initial_pct. Once profit
    crosses each sl_trail_pct step, SL locks exactly one step behind that
    step: +10% locks breakeven (0%), +20% locks +10%, +30% locks +20%,
    ..., +150% locks +140%, continuing uncapped beyond that -- a constant
    10-point trailing gap for the rest of the trade. Based on the highest
    profit % EVER reached (the RATCHET, via highest_profit_pct), never the
    current one -- a pullback after touching a higher step does not
    loosen the SL back down.

    round(..., 6) before flooring guards against float noise from the
    repeated (ltp/entry_px)-1 division elsewhere landing just under a
    clean step boundary (e.g. 0.19999999997 instead of 0.20)."""
    step = config.sl_trail_pct
    if highest_profit_pct < step:
        return entry_px * (1 + config.sl_initial_pct)
    steps_crossed = math.floor(round(highest_profit_pct / step, 6))
    locked_pct = (steps_crossed - 1) * step
    return entry_px * (1 + locked_pct)


class OrderNeedsAttention(Exception):
    """poll_fill() exhausted its automatic reprice attempts but the order is
    still resting, UNFILLED, at the broker -- not cancelled. Distinguishes
    "nothing left to act on" (order_id is dead -- a genuine broker rejection
    or cancellation, still raised as a plain RuntimeError immediately,
    unchanged) from "still live, needs a human decision" (this). See
    docs/prd/python-strategies-order-error-recovery.md."""
    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


def _reprice_and_wait_once(client, order_id: str, strategy: str, symbol: str, exchange: str,
                            action: str, quantity: int) -> Optional[dict]:
    """One reprice-to-a-fresh-crossing-price (modifyorder(), keeping the same
    order id/queue position) + one fill_poll_timeout-bounded wait. Reprices
    to the current ASK for a BUY and the current BID for a SELL -- crossing
    the spread against a fresh quote each attempt, rather than re-quoting the
    last-traded price, since that's what actually gets a resting order filled
    instead of just re-posting at the same stale level. Returns the fill data
    if it completed, None if still unfilled (order left resting either way --
    this never cancels). Shared by poll_fill()'s own reprice loop AND by
    Entry-Cancel's one-last-chance flow (_watch_entry_cancel), so this "give
    it a fair, aggressive price, then wait" behavior only exists in one
    place."""
    import time as _time

    bid, ask = fetch_symbol_bid_ask(client, symbol, exchange)
    fresh_price = ask if action == "BUY" else bid
    if fresh_price is None:
        Log.warning(f"Order {order_id}: no fresh bid/ask available to re-price -- skipping this attempt.")
        return None
    try:
        client.modifyorder(
            order_id=order_id, strategy=strategy, symbol=symbol, action=action,
            exchange=exchange, price_type="LIMIT", product=config.product,
            quantity=str(quantity), price=str(fresh_price),
            disclosed_quantity="0", trigger_price="0",
        )
        Log.warning(f"Order {order_id}: re-priced to {fresh_price} (crossing to "
                    f"{'ask' if action == 'BUY' else 'bid'}).")
    except Exception as exc:
        Log.warning(f"Order {order_id}: modify (reprice) failed: {exc}.")
        return None

    deadline_ts = datetime.now(IST).timestamp() + config.fill_poll_timeout
    while datetime.now(IST).timestamp() < deadline_ts:
        resp = client.orderstatus(order_id=order_id, strategy=strategy)
        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        if status in {"complete", "rejected", "cancelled", "canceled"}:
            if status != "complete":
                raise RuntimeError(f"Order {order_id} ended in status '{status}': {data}")
            return data
        _time.sleep(config.fill_poll_interval)
    return None  # still resting, unfilled


def poll_fill(client, orderid: str, strategy: str, symbol: str, exchange: str,
              action: str, quantity: int) -> dict:
    """Polls order status until a terminal state or config.fill_poll_timeout
    (15s). On timeout, actively RE-PRICES the same order (via
    _reprice_and_wait_once, keeping its order id/queue position) crossing the
    spread with a fresh bid/ask each time -- up to config.reprice_max_attempts
    (19) times, for a combined ceiling of ~5 minutes (fill_poll_timeout x
    (1 + reprice_max_attempts)). If it's STILL resting after all of those,
    raises OrderNeedsAttention WITHOUT cancelling it -- entering error mode is
    now a user decision (Retry/Cancel/Manually Completed), not an automatic
    cancel. A genuine broker rejection/cancellation (order never became
    fillable at all) still raises RuntimeError immediately, unchanged.
    See docs/prd/python-strategies-order-error-recovery.md."""
    import time as _time

    def _poll_until(deadline_ts) -> Optional[dict]:
        while datetime.now(IST).timestamp() < deadline_ts:
            resp = client.orderstatus(order_id=orderid, strategy=strategy)
            data = resp.get("data", {})
            status = str(data.get("order_status", "")).lower()
            if status in {"complete", "rejected", "cancelled", "canceled"}:
                if status != "complete":
                    raise RuntimeError(f"Order {orderid} ended in status '{status}': {data}")
                return data
            _time.sleep(config.fill_poll_interval)
        return None  # timed out, not yet terminal

    result = _poll_until(datetime.now(IST).timestamp() + config.fill_poll_timeout)
    if result is not None:
        return result

    for reprice_attempt in range(1, config.reprice_max_attempts + 1):
        result = _reprice_and_wait_once(client, orderid, strategy, symbol, exchange, action, quantity)
        if result is not None:
            return result
        Log.warning(f"Order {orderid}: still unfilled after reprice attempt "
                    f"{reprice_attempt}/{config.reprice_max_attempts}.")

    raise OrderNeedsAttention(
        orderid,
        f"Order {orderid} still unfilled after {config.reprice_max_attempts} reprice "
        f"attempt(s) -- resting at broker, needs manual action.",
    )


def place(client, strategy: str, symbol: str, exchange: str, action: str, quantity: int) -> str:
    """Places an order. Only retries a CLEAN rejection response (nothing was
    placed, safe to retry) up to config.place_order_max_attempts times --
    deliberately does NOT retry a raised exception (ambiguous outcome, could
    duplicate a real order; see the other 3 strategies' identical place()
    for the full rationale).

    2026-08-11: sends LIMIT crossing the spread (ask for BUY, bid for SELL),
    NOT MARKET -- confirmed live, Shoonya's API rejects true MARKET orders
    outright ("Rejected : ALGO_CHK: MKT Order type not allowed for API
    order"). Shoonya's own broker-layer MPP (broker/shoonya/mapping/
    transform_data.py) is SUPPOSED to convert MARKET->LIMIT automatically,
    but falls back to sending the raw (rejected) MKT type through whenever
    its own quote fetch comes back without a genuine two-sided market
    (bid<=0/ask<=0), has no auth token, or hits any exception -- the exact
    same class of illiquid/bad-quote gap this session has been fixing all
    day elsewhere (NIFTY OI's select_monthly_delta_strike, this file's own
    pick_atm_plus1_round100_leg). Rather than depend on that broker-layer
    fallback behaving correctly for every order, this strategy now computes
    its own crossing price directly (same fetch_symbol_bid_ask + cross-the-
    spread pattern already used throughout this file's reprice logic) and
    never sends MARKET at all. If no fresh bid/ask is available, raises
    immediately rather than falling back to MARKET -- a fallback to the one
    order type this broker rejects outright would just trade one failure
    mode for a guaranteed one."""
    import time as _time

    last_exc: Optional[Exception] = None
    for attempt in range(1, config.place_order_max_attempts + 1):
        bid, ask = fetch_symbol_bid_ask(client, symbol, exchange)
        price = ask if action == "BUY" else bid
        if price is None or price <= 0:
            last_exc = RuntimeError(
                f"place(): no fresh bid/ask available for {symbol} {action} -- "
                f"cannot safely place a LIMIT order (MARKET is rejected by this broker)."
            )
            Log.warning(f"placeorder attempt {attempt}/{config.place_order_max_attempts} "
                        f"skipped for {symbol} {action}: {last_exc}")
            if attempt < config.place_order_max_attempts:
                _time.sleep(config.place_order_retry_delay)
            continue
        try:
            resp = client.placeorder(
                strategy=strategy, symbol=symbol, exchange=exchange, action=action,
                product=config.product, price_type="LIMIT",
                quantity=str(quantity), price=str(price), trigger_price="0", disclosed_quantity="0",
            )
        except Exception as exc:
            Log.warning(f"placeorder raised for {symbol} {action} (not retried -- "
                        f"outcome ambiguous, could duplicate a real order): {exc}")
            raise
        orderid = resp.get("orderid") if resp.get("status") == "success" else None
        if orderid:
            return orderid
        # 2026-08-11: confirmed live -- a genuine broker-side rejection
        # ("ALGO_CHK: MKT Order type not allowed for API order", logged at
        # ERROR by broker.shoonya.api.order_api) can still come back through
        # client.placeorder() with a response that satisfies
        # resp.get("status")=="success" but carries no real orderid (None/
        # missing). Trusting "status"=="success" alone treated that as a
        # successful placement, so pos.entry_order_id ended up literally the
        # string "None" and the strategy proceeded straight into poll_fill's
        # reprice loop against a nonexistent order -- an order rejection was
        # never captured as a failure, and never routed through
        # _enter_error_mode at all. A missing orderid is now ALWAYS treated
        # as a rejection (retryable, same as a status!="success" response) --
        # nothing was actually placed at the broker either way (confirmed:
        # the broker's own log shows a clean rejection, nothing resting), so
        # retrying via a fresh placeorder() call is exactly as safe as any
        # other clean-rejection retry this function already does.
        last_exc = RuntimeError(f"placeorder failed for {symbol} {action}: {resp}")
        Log.warning(f"placeorder attempt {attempt}/{config.place_order_max_attempts} "
                    f"rejected for {symbol} {action} at price={price}: {resp}")
        if attempt < config.place_order_max_attempts:
            _time.sleep(config.place_order_retry_delay)
    raise last_exc


###############################################################################
# TRADE LOG (background thread -- see append_trade_log)
###############################################################################
_trade_log_queue: "queue.Queue" = queue.Queue()
_trade_log_thread: Optional[threading.Thread] = None
_trade_log_thread_lock = threading.Lock()

_TRADE_LOG_HEADER = ["leg", "symbol", "quantity", "entry_time", "entry_px",
                     "exit_time", "exit_px", "pnl_points", "pnl_rupees",
                     "exit_reason", "execution_id"]


def _migrate_trade_log_if_needed(strategy_tag: str):
    """One-time migration: a trades_{strategy_tag}.csv written before
    execution_id tracking existed has a header missing that column.
    Rewrite the file ONCE -- add the column, backfill every existing row
    with execution_id="legacy" -- so old and new rows share one consistent
    schema going forward. Safe to call on every startup: a no-op once the
    header already matches (checked BEFORE the trade-log writer thread
    starts, so there's no concurrent-write race with this rewrite)."""
    log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
    if not log_path.exists():
        return
    with log_path.open("r", newline="") as fp:
        rows = list(csv.reader(fp))
    if not rows or "execution_id" in rows[0]:
        return  # empty file, or already migrated
    Log.info(f"Migrating {log_path.name}: adding execution_id column (existing rows -> \"legacy\").")
    migrated = [_TRADE_LOG_HEADER] + [row + ["legacy"] for row in rows[1:]]
    with log_path.open("w", newline="") as fp:
        csv.writer(fp).writerows(migrated)


def _trade_log_writer_loop():
    while True:
        item = _trade_log_queue.get()
        try:
            if item is None:
                break
            (strategy_tag, leg_key, symbol, quantity,
             entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id) = item
            log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
            is_new = not log_path.exists()
            pnl_points = exit_px - entry_px  # long option: buy low, sell high = profit
            pnl_rupees = pnl_points * quantity
            # Display quantity signed positive -- every leg in this strategy
            # is a long option buyer.
            display_quantity = quantity
            with log_path.open("a", newline="") as fp:
                writer = csv.writer(fp)
                if is_new:
                    writer.writerow(_TRADE_LOG_HEADER)
                writer.writerow([leg_key, symbol, display_quantity, entry_time, round(entry_px, 2),
                                  exit_time, round(exit_px, 2), round(pnl_points, 2),
                                  round(pnl_rupees, 2), exit_reason, execution_id])
        except Exception as exc:
            Log.warning(f"Trade log writer failed: {exc}")
        finally:
            _trade_log_queue.task_done()


def _ensure_trade_log_thread():
    global _trade_log_thread
    with _trade_log_thread_lock:
        if _trade_log_thread is None or not _trade_log_thread.is_alive():
            _trade_log_thread = threading.Thread(
                target=_trade_log_writer_loop, name="trade-log-writer", daemon=True
            )
            _trade_log_thread.start()


def append_trade_log(strategy_tag: str, leg_key: str, symbol: str, quantity: int,
                      entry_time: str, entry_px: float, exit_time: str, exit_px: float,
                      exit_reason: str, execution_id: int):
    _ensure_trade_log_thread()
    _trade_log_queue.put((strategy_tag, leg_key, symbol, quantity,
                          entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id))




def _post_json_local(env: "Environment", path: str, payload: bytes, timeout: float = 3.0):
    """POST to the dedicated strategy_reporting subprocess over plain TCP
    loopback on STRATEGY_REPORTING_PORT (default 8766). Retries once at
    3x the timeout before giving up -- covers the post-restart burst
    where every running strategy reconnects at once and the dev-grade
    werkzeug server can't accept new connections fast enough within the
    first, short timeout. 2026-08-07: dropped the env.host (public
    domain) fallback that used to run here -- confirmed in production
    that env.host is proxied through Cloudflare, which silently 403s
    this urllib-originated traffic (bot/WAF protection) before it ever
    reaches nginx or this app, masking the real error (a loopback
    timeout) behind a misleading "Forbidden" that looked like an
    ownership bug. 2026-08-05: moved off the main app's openalgo.sock/
    FLASK_PORT -- these reporting calls used to share the single
    gunicorn+eventlet worker with every other route in the app, so an
    unrelated slow endpoint elsewhere (confirmed in production:
    /traffic/api/stats) could block the worker long enough that these
    timed out even though nothing about the strategy was wrong. The
    dedicated subprocess is immune to that by construction (a separate
    process, plain OS threads, not eventlet). Raises if both attempts
    fail; caller logs and swallows."""
    headers = {"Content-Type": "application/json"}
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    last_exc: Optional[Exception] = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            req = urllib.request.Request(f"{base}{path}", data=payload, method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=attempt_timeout).close()
            return
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_post_json_local: attempt (timeout={attempt_timeout}s) failed: {exc}")
    raise last_exc


def report_pnl_to_platform(env: "Environment", realized_pnl: float, open_positions: list):
    """Push a PnL snapshot to the OpenAlgo Python Strategy Host so the UI's
    PNL button can show live PnL without the platform having to poll/parse
    this process's logs. Fire-and-forget: stdlib-only (no new dependency),
    short timeout, any failure is logged and swallowed -- must never block
    or crash the main scheduler loop over a reporting hiccup."""
    unrealized_pnl = sum(p.get("pnl", 0.0) for p in open_positions)
    payload = json.dumps({
        "apikey": env.api_key,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": open_positions,
    }).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/pnl"
    try:
        _post_json_local(env, path, payload)
    except Exception as exc:
        Log.warning(f"report_pnl_to_platform failed: {exc}")


def _get_json_local(env: "Environment", path: str, timeout: float = 3.0) -> dict:
    """GET counterpart to _post_json_local -- same loopback-with-retry
    behavior (2026-08-07: see that function's docstring for why the
    env.host/Cloudflare fallback was dropped). Raises if both attempts
    fail; caller treats it as "no action pending" rather than crashing
    the scheduler loop over a reporting hiccup."""
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    last_exc: Optional[Exception] = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=attempt_timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_get_json_local: attempt (timeout={attempt_timeout}s) failed: {exc}")
    raise last_exc


def push_leg_error(env: "Environment", leg_key: str, pos: "LegPosition",
                    action: str = "", clear: bool = False):
    """Push (or clear) this leg's error-mode state to the platform so the UI's
    error badge/page reflects it live, without polling this process's logs.
    Fire-and-forget, same style as report_pnl_to_platform -- must never block
    or crash the scheduler loop over a reporting hiccup. `clear=True` is used
    once a Retry/Cancel/Manual action has actually resolved the leg (pos's
    error_state is already "" by then), so the platform drops the alert.
    See docs/prd/python-strategies-order-error-recovery.md."""
    payload = json.dumps({
        "apikey": env.api_key,
        "leg_key": leg_key,
        "error_state": pos.error_state,
        "error_kind": pos.error_kind,
        "error_message": pos.error_message,
        "error_since": pos.error_since,
        "symbol": pos.symbol,
        "quantity": pos.quantity,
        "action": action,
        "clear": clear,
    }).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/errors"
    try:
        _post_json_local(env, path, payload)
    except Exception as exc:
        Log.warning(f"push_leg_error failed for {leg_key}: {exc}")


def check_pending_action(env: "Environment", leg_key: str) -> Optional[dict]:
    """Pull whether the user has issued a Retry/Cancel/Manual action for this
    leg from the platform. Only ever called for a leg currently in error mode
    (near-zero legs, near-zero overhead on the normal per-cycle path). Returns
    None on any failure (transport error, or genuinely nothing pending) --
    degrades to "nothing pending yet, try again next cycle" rather than
    raising, matching report_pnl_to_platform's best-effort style."""
    # apikey MUST be in the query string -- api_check_pending_action requires
    # it and returns 401 without it (this was missing entirely until now,
    # which meant every call silently 401'd and this function always
    # reported "nothing pending", regardless of what the user submitted).
    path = f"/python/api/strategy/{env.strategy_tag}/pending_action?leg_key={leg_key}&apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_pending_action failed for {leg_key}: {exc}")
        return None
    return data if data.get("action") else None


def ack_pending_action(env: "Environment", leg_key: str):
    """Tell the platform this leg's pending action has been consumed, so a
    stale action can't be re-applied on a later, unrelated error for the same
    leg. Fire-and-forget, same style as report_pnl_to_platform."""
    payload = json.dumps({"apikey": env.api_key, "leg_key": leg_key}).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/pending_action/ack"
    try:
        _post_json_local(env, path, payload)
    except Exception as exc:
        Log.warning(f"ack_pending_action failed for {leg_key}: {exc}")


def check_force_exit(env: "Environment") -> bool:
    """Pull whether the user has clicked the Force Exit button for this
    strategy. Called once per cycle (near-zero overhead) -- returns False on
    any transport failure, matching check_pending_action's best-effort
    style (degrades to "not requested" rather than raising)."""
    path = f"/python/api/strategy/{env.strategy_tag}/force_exit?apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_force_exit failed: {exc}")
        return False
    return bool(data.get("requested"))


def ack_force_exit_complete(env: "Environment"):
    """Tell the platform every open leg has been force-closed and confirmed
    flat -- this is what actually stops the strategy process (see
    api_complete_force_exit). Fire-and-forget, same style as
    report_pnl_to_platform."""
    payload = json.dumps({"apikey": env.api_key}).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/force_exit/complete"
    try:
        _post_json_local(env, path, payload)
    except Exception as exc:
        Log.warning(f"ack_force_exit_complete failed: {exc}")


###############################################################################
# STRATEGY ENGINE
###############################################################################
class StrategyEngine:
    def __init__(self, client, store: StateStore, env: Environment, price_stream: "PriceStream",
                 execution_id: int = 0, ltp_client=None):
        self.client = client
        # Short-timeout client used for the WS-stale LTP fallback calls made
        # directly in run_cycle -- see Broker.connect_ltp_client(). Falls
        # back to the main client if not supplied (e.g. a test harness
        # constructing StrategyEngine directly) so this stays optional.
        self.ltp_client = ltp_client if ltp_client is not None else client
        self.store = store
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id  # this process run's number -- see main()
        self._signal_cache: dict[str, InstrumentSignal] = {}
        self._last_indicator_refresh: dict[str, datetime] = {}
        self._ws_fallback_logged: dict[str, bool] = {}
        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds (a stale/stuck order re-pricing against a moving market) -- doing that
        # INSIDE run_cycle would stall every other leg's entry/exit check for the same
        # duration. Order confirmation now happens on a per-leg background task instead
        # (see _watch_entry_fill/_watch_exit_fill), submitted to one shared,
        # module-instance-lifetime executor rather than a raw thread per order (FD
        # hygiene: threads/executors are shared singletons, never spun up per-call).
        # _state_lock serializes every self.store.save() call (main thread and
        # background tasks alike) -- StateStore.save() writes the whole state to one
        # shared JSON file, so two concurrent writers could corrupt it even though each
        # leg's own fields are only ever touched by one thread at a time (guarded by
        # _pending_fills below). _pending_fills tracks which leg_keys already have an
        # active watcher so run_cycle doesn't submit a duplicate one next cycle.
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._last_error_push: dict[str, datetime] = {}
        # Throttles the outer run_cycle except-clause's WhatsApp alert --
        # separate from _last_error_push above, which only governs the UI
        # error-badge re-push. In-memory/per-instance, not persisted.
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        # check_pending_action is also a synchronous local HTTP call made
        # inline from run_cycle's per-instrument loop -- same blocking-bug
        # class as check_force_exit/notify_trade_closed above. Result is
        # consumed by the caller, so (unlike push_leg_error/
        # notify_trade_closed, which are pure fire-and-forget) it needs a
        # cache rather than plain dispatch-and-forget.
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        # Guards _reset_day_if_needed's background dispatch -- the daily
        # futures-contract resolution (client.expiry()) used to run inline on
        # the main scheduler thread, and on failure retried EVERY cycle with
        # no backoff, blocking the whole thread for up to env.timeout on each
        # attempt until it succeeded.
        self._day_reset_pending: bool = False
        # MCX-specific holiday/special-session window for TODAY, populated
        # via client.timings() (backed by database/market_calendar_db.py's
        # holiday + special-session data) by _refresh_mcx_session_window_bg,
        # called every cycle from run_cycle -- deliberately NOT tied to the
        # once-a-day _reset_day_if_needed dispatch, so a transient timings()
        # failure (network blip, broker error) just retries next cycle
        # instead of being permanently mistaken for a confirmed holiday.
        # _mcx_session_checked_day is only ever set once the check
        # DEFINITIVELY resolved today (a real window OR a confirmed empty
        # session) -- never on a fetch failure/exception, so
        # _within_market_hours can tell "not resolved yet, fall back to
        # static bounds" apart from "confirmed holiday, refuse to trade"
        # apart from "confirmed session, use that window."
        self._mcx_session_window: Optional[tuple] = None
        self._mcx_session_checked_day: str = ""
        self._mcx_session_check_pending: bool = False
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(LEG_KEYS), thread_name_prefix="fillwatch"
        )
        # Separate, single-worker pool purely for the Force Exit check
        # (check_force_exit, a quick local HTTP call). If all _fill_executor
        # workers are simultaneously busy watching fills (each can block up
        # to fill_poll_timeout * (1 + reprice_max_attempts) seconds), a Force
        # Exit check submitted to that SAME pool would just queue silently
        # behind them for minutes with no log line and no escalation, exactly
        # when a human is trying to intervene fastest.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bgcheck")
        # Dedicated single worker for report_pnl_tick's platform push --
        # separate from _fill_executor so a fill-watcher blocked for minutes
        # (reprice loop) can never make the live PnL display go stale too;
        # PnL pushes are small/fast and only need to run one at a time.
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
        # Dedicated pool for periodic background work that must never queue
        # behind a stuck order-fill watcher (which can block up to
        # fill_poll_timeout * (1 + reprice_max_attempts), ~5 minutes): the
        # indicator/chain refresh (_refresh_signal_and_chain_bg) and the
        # daily futures-contract resolution (_reset_day_if_needed). Both
        # used to share _fill_executor with the fill-watchers themselves --
        # if both legs (this strategy's entire _fill_executor capacity,
        # len(LEG_KEYS)=2) were simultaneously stuck repricing, exit-signal
        # refresh and day-rollover could queue behind them for the same
        # ~5 minutes, exactly while positions were already in trouble.
        self._refresh_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="refresh")
        # Options expiry only rolls monthly, not intraday -- resolving it fresh via
        # client.expiry() on every single entry (as before) added a full REST
        # round-trip to the entry critical path for no reason. Cached per instrument,
        # cleared in _reset_day_if_needed alongside the other daily state.
        self._expiry_cache: dict[str, str] = {}
        # Background ATM-chain cache: {inst.name: chain_dict}, refreshed on the same
        # cadence as the indicator signal (see get_signal below) -- NOT fetched at
        # the moment a signal fires. This decouples the optionchain() REST
        # round-trip from the entry critical path entirely, so _enter_leg only
        # has to place the order once a signal condition is true (see its own
        # comment for why this matters for reaction speed).
        self._chain_cache: dict[str, dict] = {}
        # Which candle_key self._chain_cache[inst.name] was actually
        # successfully populated for -- lets _refresh_signal_and_chain_bg
        # tell "chain already fetched for THIS candle, skip re-fetching" apart
        # from "chain fetch failed and was never populated for this candle,
        # keep retrying" (see that method's own comment for why this
        # distinction matters).
        self._chain_cache_candle_key: dict[str, str] = {}
        # Guards get_signal so it submits at most one in-flight
        # indicator/chain refresh per instrument at a time -- without this, a
        # refresh that takes longer than indicator_refresh_interval to
        # complete would get re-submitted every subsequent cycle, piling up
        # duplicate background REST calls against the same instrument.
        self._signal_refresh_pending: set[str] = set()

    def _save_state(self):
        """Every self.store.save() call in this engine goes through here --
        StateStore.save() writes the entire state to one shared JSON file, so
        concurrent writers (run_cycle vs. a background fill-watch task) could
        otherwise corrupt it or drop an update."""
        with self._state_lock:
            self.store.save()

    def _refresh_chain_cache(self, inst: InstrumentConfig, futures_symbol: str) -> bool:
        """Returns True if self._chain_cache[inst.name] was actually updated
        (fetch succeeded), False if it failed and the prior (possibly stale
        or missing) entry was left untouched -- callers use this to decide
        whether it's safe to mark the chain as "done" for the current
        candle, or whether it must keep being retried."""
        try:
            expiry = self._expiry_cache.get(inst.name)
            if expiry is None:
                expiry = resolve_current_month_expiry(self.client, inst)
                self._expiry_cache[inst.name] = expiry
            self._chain_cache[inst.name] = fetch_chain(self.client, inst, expiry, futures_symbol)
            return True
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background chain refresh failed (will retry live at "
                        f"entry if needed): {exc}")
            return False

    def _refresh_signal_and_chain_bg(self, inst: InstrumentConfig, futures_symbol: str,
                                       ltp: Optional[float], refresh_chain: bool):
        """Runs on _refresh_executor (kept separate from _fill_executor so a
        stuck fill-watcher can never delay this) -- this is what makes
        get_signal's periodic refresh genuinely non-blocking.
        compute_instrument_signal() and
        fetch_chain() (via _refresh_chain_cache) both make real REST calls
        (client.history()/client.optionchain()); previously these were called
        inline from get_signal(), which is itself called inline from
        run_cycle(), so a slow/stuck broker response stalled the whole
        scheduler cycle for every leg. Now run_cycle only ever reads
        self._signal_cache / self._chain_cache -- never waits on the network
        call itself.

        Chain refresh is skipped only when self._chain_cache was already
        SUCCESSFULLY populated for the "reference" candle_key -- this
        cycle's fresh signal if the indicator fetch succeeded, else the
        PREVIOUSLY cached signal (see reference_signal below). Two distinct
        live bugs motivated this, both confirmed 2026-08-11:
          1. Right after a 45m boundary, the broker's history() data for the
             just-closed bar can lag several cycles behind wall-clock, so
             get_signal's due_for_refresh retries compute_instrument_signal
             every ~10s until it finally advances. Naively re-fetching the
             chain on every one of those retries produced 13-15 redundant
             optionchain() calls per candle boundary. Fixed by tracking
             self._chain_cache_candle_key (what candle the cache actually
             reflects) instead of just "did the candle_key change."
          2. That fix alone still forced a chain re-fetch on every bare
             history() failure (fresh is None) even when the chain was
             already correctly cached for the still-current candle, since it
             only compared against fresh.candle_key -- a transient indicator
             blip unrelated to candle boundaries would otherwise unnecessarily
             burn an optionchain() call every such cycle. Fixed by falling
             back to the PREVIOUS cached signal's candle_key as the
             reference when this cycle's fetch failed, so only a genuine
             "chain not yet fetched for the current candle" gap triggers a
             retry -- never a same-candle indicator hiccup alone."""
        previous = self._signal_cache.get(inst.name)
        fresh = None
        try:
            fresh = compute_instrument_signal(self.client, inst, futures_symbol, ltp=ltp)
            if fresh is not None:
                self._signal_cache[inst.name] = fresh
                self._last_indicator_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background indicator refresh failed (will retry "
                        f"next cycle): {exc}")

        if refresh_chain:
            # The candle to check the chain cache against: prefer this
            # cycle's fresh signal, but fall back to the PREVIOUS cached
            # signal when this cycle's indicator fetch itself failed
            # (fresh is None) -- a bare history() blip, unrelated to candle
            # boundaries, must not force a chain re-fetch when the chain is
            # still correctly cached for the last known-good candle. Only
            # when neither is available (very first call, and it failed) is
            # there no reference to compare against.
            reference_signal = fresh if fresh is not None else previous
            reference_candle_key = reference_signal.candle_key if reference_signal is not None else None
            chain_already_current = (
                reference_candle_key is not None
                and self._chain_cache_candle_key.get(inst.name) == reference_candle_key
            )
            if not chain_already_current:
                if self._refresh_chain_cache(inst, futures_symbol) and reference_candle_key is not None:
                    self._chain_cache_candle_key[inst.name] = reference_candle_key

        self._signal_refresh_pending.discard(inst.name)

    def get_signal(self, inst: InstrumentConfig, futures_symbol: str,
                    ltp: Optional[float] = None, refresh_chain: bool = False) -> Optional[InstrumentSignal]:
        now = datetime.now(IST)
        last = self._last_indicator_refresh.get(inst.name)
        current_boundary = _current_candle_boundary(config.candle_interval_minutes)
        # compute_instrument_signal always drops the still-forming last bar
        # (see its own docstring/comment), so a fresh signal's candle_key is
        # always the LAST CLOSED candle -- one full interval BEFORE
        # current_boundary (the start of the candle currently in progress),
        # never equal to or after it. Comparing cached_boundary directly
        # against current_boundary can therefore never be satisfied and was
        # forcing a background refresh (client.history() + client.
        # optionchain() when refresh_chain=True) on EVERY single run_cycle
        # tick instead of once per candle -- confirmed live 2026-08-11 via
        # journalctl showing continuous ~10s-interval optionchain() calls
        # for minutes on end. Compare against the last-CLOSED candle's own
        # boundary instead.
        last_closed_boundary = current_boundary - timedelta(minutes=config.candle_interval_minutes)
        cached_for_boundary = self._signal_cache.get(inst.name)
        cached_boundary = (
            _candle_key_boundary(cached_for_boundary.candle_key)
            if cached_for_boundary is not None else None
        )
        have_current_candle = cached_boundary is not None and cached_boundary >= last_closed_boundary
        due_for_refresh = last is None or not have_current_candle
        if due_for_refresh and inst.name not in self._signal_refresh_pending:
            # Only worth the background broker traffic when a new leg could actually
            # still be entered this cycle (within the entry window, at least one side
            # still flat) -- refreshing all day regardless (incl. after both legs are
            # already filled, or outside the entry window entirely) would just add
            # constant option-quote load on the broker for no reachable benefit.
            self._signal_refresh_pending.add(inst.name)
            self._refresh_executor.submit(
                self._refresh_signal_and_chain_bg, inst, futures_symbol, ltp, refresh_chain
            )

        cached = self._signal_cache.get(inst.name)
        if cached is None:
            return None

        if ltp is None:
            ltp = fetch_symbol_ltp(self.ltp_client, futures_symbol, inst.options_exchange)
        if ltp is not None:
            cached.ltp = ltp
        return cached

    # ---- state helpers -----------------------------------------------------
    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.store.state.current_day == today_key:
            return
        if self._day_reset_pending:
            return  # already dispatched in the background -- wait for it
        self._day_reset_pending = True

        def _run():
            try:
                futures_symbol = resolve_current_month_futures(self.client, INSTRUMENTS[0])
            except Exception as exc:
                Log.warning(f"Could not resolve current-month futures contract yet ({exc}); "
                            f"retrying next cycle.")
                self._day_reset_pending = False
                return  # don't mark the day as reset -- retry next cycle

            old_futures_symbol = self.store.state.futures_symbol
            try:
                Log.info(f"New day detected ({today_key}); resetting daily trade counters. "
                          f"Futures contract: {futures_symbol}")
                self.store.state.current_day = today_key
                self.store.state.today_realized_pnl = 0.0
                self.store.state.futures_symbol = futures_symbol
                self._expiry_cache.clear()
                self._chain_cache.clear()
                for leg in self.store.state.legs.values():
                    leg.trade_count = 0
                    leg.last_sl_exit_candle_boundary = ""
                self._save_state()
                self.price_stream.add_instruments(
                    [{"symbol": futures_symbol, "exchange": INSTRUMENTS[0].options_exchange}]
                )
                # Unsubscribe the previous month's futures contract now that
                # the new one is live -- without this, every monthly
                # rollover permanently leaves one more expired futures
                # symbol subscribed on the shared WS feed for the rest of
                # this long-running process's life (PriceStream tracks and
                # watchdog-checks it forever, same unbounded-accumulation
                # risk remove_instruments already guards against for option
                # legs, just uncovered for the futures symbol until now).
                if old_futures_symbol and old_futures_symbol != futures_symbol:
                    self.price_stream.remove_instruments(
                        [{"symbol": old_futures_symbol, "exchange": INSTRUMENTS[0].options_exchange}]
                    )
            except Exception as exc:
                # Anything in this block failing partway (state already
                # mutated in memory, e.g. _save_state()'s disk I/O itself
                # raising) must not leave current_day silently marking today
                # as "already reset" -- this function's own top-level guard
                # (`if self.store.state.current_day == today_key: return`)
                # would then permanently skip the reset for the rest of the
                # day with zero log output. Reset current_day so the next
                # cycle's call genuinely retries, and log loudly -- every
                # other background task in this file already has this same
                # safety net (see _watch_entry_fill/_watch_exit_fill's own
                # comments), this one previously didn't.
                Log.exception(f"Daily reset failed partway through ({exc}); will retry next cycle.")
                self.store.state.current_day = ""
            finally:
                self._day_reset_pending = False

        # Dispatched, not run inline: this used to block the main scheduler
        # thread for a real client.expiry() round-trip on the first cycle of
        # each day, and on failure retried EVERY subsequent cycle with no
        # backoff. Deferring to the background is safe here -- run_cycle just
        # keeps using yesterday's (still-valid intraday) futures_symbol/state
        # for the one extra cycle until this completes, exactly the same
        # graceful-degradation behavior the old code already had on failure,
        # just applied uniformly instead of only after an error. Runs on
        # _refresh_executor, not _fill_executor -- see that pool's own
        # comment in __init__ for why.
        self._refresh_executor.submit(_run)

    def _within_entry_window(self) -> bool:
        if config.test_mode:
            return True
        now = datetime.now(IST).time()
        return config.entry_start <= now <= config.entry_end

    def _refresh_mcx_session_window_bg(self):
        """Dispatches client.timings() to the background executor -- a quick
        local-then-broker HTTP round-trip, but still not something to run
        inline on the main scheduler thread every cycle. Guarded so a slow
        call isn't resubmitted on top of itself. Only ever sets
        _mcx_session_checked_day on a DEFINITIVE result (a real session
        window, or a confirmed empty list = holiday); a failed/errored call
        leaves it unset so this simply retries next cycle instead of a
        transient broker hiccup being mistaken for a confirmed holiday."""
        today_key = datetime.now(IST).date().isoformat()
        if self._mcx_session_checked_day == today_key:
            return  # already definitively resolved today
        if self._mcx_session_check_pending:
            return
        self._mcx_session_check_pending = True

        def _run():
            try:
                timings_resp = self.client.timings(date=today_key)
                if timings_resp.get("status") != "success":
                    Log.warning(f"client.timings() returned non-success ({timings_resp}); "
                                f"will retry next cycle.")
                    return
                mcx_row = next(
                    (row for row in timings_resp.get("data", []) if row.get("exchange") == "MCX"),
                    None,
                )
                if mcx_row:
                    start_t = datetime.fromtimestamp(mcx_row["start_time"] / 1000, IST).time()
                    end_t = datetime.fromtimestamp(mcx_row["end_time"] / 1000, IST).time()
                    self._mcx_session_window = (start_t, end_t)
                    Log.info(f"MCX session today: {start_t}-{end_t}")
                else:
                    self._mcx_session_window = None
                    Log.warning("MCX has no trading session today (holiday) -- "
                                "entries/signal computation will be skipped all day.")
                self._mcx_session_checked_day = today_key  # only set on a DEFINITIVE result
            except Exception as exc:
                Log.warning(f"Could not fetch today's MCX session window ({exc}); "
                            f"will retry next cycle.")
            finally:
                self._mcx_session_check_pending = False

        self._bg_executor.submit(_run)

    def _within_market_hours(self) -> bool:
        if config.test_mode:
            return True
        today_key = datetime.now(IST).date().isoformat()
        if self._mcx_session_checked_day == today_key:
            if self._mcx_session_window is None:
                # Checked today's calendar and MCX has no session at all --
                # a full MCX holiday. Refuse to trade regardless of the
                # static config.market_open/market_close bounds.
                return False
            start_t, end_t = self._mcx_session_window
            now = datetime.now(IST).time()
            return start_t <= now <= end_t
        # Not resolved definitively yet today (process just started, the
        # background fetch hasn't completed, or it's still retrying after a
        # failure) -- fall back to the static bounds rather than blocking on
        # a synchronous call here.
        return _within_market_hours()

    def _past_universal_exit(self) -> bool:
        if config.test_mode:
            return False
        return datetime.now(IST).time() >= config.universal_exit_time

    def report_pnl_tick(self):
        """Runs on its OWN scheduler job at config.pnl_tick_interval (0.8s),
        completely decoupled from run_cycle's scheduler_interval (10s). PnL
        is purely observational -- it never feeds a trading decision -- so
        it can refresh far more often than the main cycle without any of
        the blocking-call risk scheduler_interval exists to protect
        against. Reads ONLY the WebSocket price cache (price_stream.get_ltp)
        -- deliberately NEVER a REST fallback here, since a 1-second job
        doing a REST quotes() call per open leg would spam the broker all
        day. If a leg's feed is momentarily stale, its PnL just holds at
        its last-known value for this tick rather than making a network
        call from this job."""
        try:
            open_positions = []
            for leg_key in LEG_KEYS:
                pos = self.store.state.legs[leg_key].position
                if not pos.symbol or not pos.entry_filled:
                    continue
                inst_name = leg_key.split("_")[0]
                inst = next(i for i in INSTRUMENTS if i.name == inst_name)
                current_px = self.price_stream.get_ltp(
                    pos.symbol, inst.options_exchange, max_age=config.ws_stale_seconds
                )
                if current_px is None:
                    continue
                pnl = (current_px - pos.entry_px) * pos.quantity  # long leg
                open_positions.append({
                    "leg_key": leg_key, "symbol": pos.symbol, "direction": "LONG",
                    # Display quantity signed positive -- every leg here is a
                    # long option buyer -- so the Trades/PnL UI reads
                    # correctly without a separate direction column.
                    "quantity": pos.quantity, "entry_price": pos.entry_px,
                    "current_price": current_px, "pnl": pnl,
                    "entry_time": pos.entry_time, "execution_id": pos.execution_id,
                })
            try:
                self._pnl_executor.submit(
                    report_pnl_to_platform, self.env, self.store.state.today_realized_pnl,
                    open_positions,
                )
            except Exception as exc:
                # .submit() itself can raise (transient thread-creation
                # hiccup, same class as notify_trade_closed's dispatch) --
                # never let it crash this 0.8s job.
                Log.warning(f"Failed to dispatch report_pnl_to_platform: {exc}")
        except Exception as exc:
            Log.exception(f"report_pnl_tick failed: {exc}")

    # ---- entry / exit (independent long leg, resumable) ---------------------
    def _enter_leg(self, leg_key: str, inst: InstrumentConfig, option_type: str, spot: float,
                    futures_symbol: str, condition_desc: str = ""):
        leg = self.store.state.legs[leg_key]
        strategy_tag = self.env.strategy_tag
        pos = leg.position

        if not pos.symbol:
            # Reads the background-refreshed chain cache (see _refresh_chain_cache,
            # piggybacked on get_signal's indicator_refresh_interval cadence) --
            # this is what keeps the signal-to-order path down to just the order
            # placement call itself. If the cache isn't populated yet (rare --
            # right after a restart, before the first background refresh
            # completes), skip entry THIS cycle rather than falling back to a
            # synchronous chain fetch here, which would block every other
            # leg/instrument's check on the main scheduler thread for a real
            # broker round-trip. get_signal's own due_for_refresh path already
            # has (or will trigger) a background refresh, so the cache fills in
            # within indicator_refresh_interval and this leg enters on a later
            # cycle instead -- a small, bounded delay rather than a stall.
            chain = self._chain_cache.get(inst.name)
            if chain is None:
                Log.warning(f"[{leg_key}] Entry signal fired but the option chain "
                            f"cache isn't populated yet -- skipping this cycle, "
                            f"will retry once the background refresh completes.")
                return
            try:
                atm1_leg = pick_atm_plus1_round100_leg(chain, option_type, spot)
            except RuntimeError as exc:
                Log.warning(f"[{leg_key}] ATM+1 round-{config.strike_round} strike resolution "
                            f"failed against the cached chain ({exc}) -- skipping this cycle, "
                            f"forcing a chain refresh.")
                self._chain_cache.pop(inst.name, None)
                return
            # If the cache lagged (a slow/failed background refresh) or spot
            # gapped since it was fetched, the TRUE current ATM could have
            # drifted entirely outside the cached strikes, and
            # pick_atm_plus1_round100_leg would silently return a strike
            # that isn't actually ATM+1 with no warning. Bail out (same
            # "not populated yet" skip-and-retry path used above) if the
            # resolved strike is implausibly far from spot.
            if abs(atm1_leg["strike"] - spot) > 2 * config.strike_round:
                Log.warning(f"[{leg_key}] Cached option chain looks stale -- resolved strike "
                            f"{atm1_leg['strike']} is implausibly far from spot {spot}. "
                            f"Skipping this cycle and forcing a chain refresh.")
                self._chain_cache.pop(inst.name, None)
                return
            quantity = config.lot_multiplier * atm1_leg["lotsize"]

            Log.info(f"[{leg_key}] Entry: strike={atm1_leg['strike']} (ATM+1) symbol={atm1_leg['symbol']}@{atm1_leg['ltp']} "
                     f"qty={quantity}" + (f" | condition: {condition_desc}" if condition_desc else ""))

            pos = LegPosition(
                symbol=atm1_leg["symbol"],
                quantity=quantity,
                entry_time=datetime.now(IST).isoformat(),
                entry_px=float(atm1_leg["ltp"]),
                execution_id=self.execution_id,
            )
            leg.position = pos
            self._save_state()
            self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])

        if pos.entry_filled or leg_key in self._pending_fills:
            return  # already filled, or a background watcher is already tracking this order

        if not pos.entry_order_id:
            # place() can raise (either a RuntimeError after exhausting its own
            # retries on a persistent clean rejection, or an immediate ambiguous
            # exception it deliberately never retries) -- uncaught, that would
            # escape to run_cycle's outer handler with NO error_state ever set,
            # silently retrying every cycle forever with zero UI visibility.
            # Both cases route to "terminal" (nothing resting to reprice/watch),
            # matching how every other genuinely-ambiguous case in this file is
            # already resolved via Retry/Cancel/Manual.
            try:
                pos.entry_order_id = place(self.client, strategy_tag, pos.symbol,
                                            inst.options_exchange, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for entry: {exc}")
                self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                return
            self._save_state()

        # The actual fill confirmation (poll_fill, up to fill_poll_timeout *
        # (1 + reprice_max_attempts) seconds) happens off this thread -- see
        # module-level note on _state_lock/_pending_fills/_fill_executor.
        # place() above is the only REST call left on the signal-to-order path.
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_entry_fill, leg_key, inst, pos.entry_order_id, pos.symbol, pos.quantity
        )

    def _watch_entry_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                           symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                      "BUY", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.entry_order_id == order_id:  # guard vs. a superseded/stale order
                pos.entry_filled = True
                leg.trade_count += 1
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled: {symbol}")
        except OrderNeedsAttention as exc:
            self._enter_error_mode(leg_key, "entry_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
        except Exception as exc:
            # Anything unexpected (malformed broker response, unhandled SDK
            # exception, etc.) -- without this, the executor would silently
            # drop it (nobody calls .result() on this future), leaving the
            # leg stuck with no log line and no error-state entry. Treated as
            # "resting" (not "terminal") since we don't actually know the
            # order's true fate -- safer to let the user Retry/Cancel it
            # explicitly than to assume it's dead.
            Log.exception(f"[{leg_key}] Unexpected error while watching entry fill: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _enter_error_mode(self, leg_key: str, error_state: str, error_kind: str,
                          error_order_id: str, message: str):
        """See docs/prd/python-strategies-order-error-recovery.md. Called
        from both watchers' exception handlers -- and, by construction, will
        be called AGAIN on the same leg if a subsequent Retry/Cancel-driven
        attempt also fails, since it's the normal failure path, not a
        one-shot special case. error_since is overwritten every time so the
        UI shows how long the CURRENT attempt has been stuck."""
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        self._save_state()
        Log.error(f"[{leg_key}] {error_state} ({error_kind}): {message}")
        action = "BUY" if error_state == "entry_failed" else "SELL"
        push_leg_error(self.env, leg_key, pos, action=action)
        self._last_error_push[leg_key] = datetime.now(IST)
        # WhatsApp self-alert on every genuine error-state transition (order
        # rejection, fill timeout, or any other place()/poll_fill failure
        # that routes here) -- fires once per transition, not on the
        # periodic _repush_active_errors re-push. Dispatched via
        # _pnl_executor (non-blocking, same pool this file already uses for
        # push_leg_error) and notify_whatsapp_error() itself never raises --
        # a WhatsApp/network hiccup can never break this method or the
        # calling run_cycle.
        try:
            self._pnl_executor.submit(
                notify_whatsapp_error, self.env,
                f"[{config.strategy_name}] {leg_key} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state (above) -- if that one POST is lost (server busy,
        transient network blip), the UI's error badge silently never
        appears, even though state.json correctly tracks the error the
        whole time (confirmed in production, 2026-07-28: three legs sat in
        exit_failed for 1-4 hours with no UI error shown). Re-pushes at
        most once per config.error_repush_interval_sec for every leg still
        in error_state, so a single lost push self-heals within a minute
        instead of leaving the UI blind indefinitely. Dispatched via
        _pnl_executor (not _fill_executor) -- same reasoning as
        report_pnl_tick: must never queue behind a fill-watcher stuck for
        minutes in a reprice loop."""
        now = datetime.now(IST)
        for leg_key in LEG_KEYS:
            pos = self.store.state.legs[leg_key].position
            if not pos.error_state:
                self._last_error_push.pop(leg_key, None)
                continue
            last = self._last_error_push.get(leg_key)
            if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
                continue
            self._last_error_push[leg_key] = now
            action = "BUY" if pos.error_state == "entry_failed" else "SELL"
            try:
                self._pnl_executor.submit(push_leg_error, self.env, leg_key, pos, action=action)
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to dispatch periodic error re-push: {exc}")

    def _push_leg_error_bg(self, leg_key: str, pos: "LegPosition", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _pnl_executor -- same
        blocking-bug class as _repush_active_errors above (which already
        dispatches this way), for the "clear"/on-resolution pushes from
        _resolve_leg_error, which run synchronously on run_cycle's own
        thread (unlike _enter_error_mode's push, already called from a
        watcher's own background thread). `pos` is snapshotted with a
        shallow copy on THIS (calling) thread before handing off --
        executor.submit() evaluates its arguments immediately, only the
        function call itself is deferred -- since pos is a live, mutable
        LegPosition this same cycle may reset moments later."""
        snapshot = copy.copy(pos)
        try:
            self._pnl_executor.submit(push_leg_error, self.env, leg_key, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"[{leg_key}] Failed to dispatch push_leg_error: {exc}")

    def _refresh_pending_action_bg(self, leg_key: str):
        """Dispatches check_pending_action to _bg_executor instead of
        blocking run_cycle() -- mirrors _refresh_force_exit_check_bg, but
        keyed per leg_key (multiple legs can be in error state at once) and
        caches the result for _pop_pending_action() to consume, since
        (unlike push_leg_error/notify_trade_closed) the caller needs the
        return value, not just fire-and-forget. Guarded per leg_key so a
        slow check that outlives one cycle isn't resubmitted on top of
        itself."""
        if leg_key in self._pending_action_inflight:
            return
        self._pending_action_inflight.add(leg_key)

        def _run():
            try:
                result = check_pending_action(self.env, leg_key)
                if result is not None:
                    self._pending_action_cache[leg_key] = result
            except Exception as exc:
                Log.warning(f"check_pending_action background refresh failed for {leg_key}: {exc}")
            finally:
                self._pending_action_inflight.discard(leg_key)

        self._bg_executor.submit(_run)

    def _pop_pending_action(self, leg_key: str) -> Optional[dict]:
        """The last background-fetched pending action for this leg, if any
        -- consumed once (popped), so a fetched action is never applied
        twice."""
        return self._pending_action_cache.pop(leg_key, None)

    def _check_stop_loss(self, leg_key: str, pos: LegPosition, inst: InstrumentConfig) -> tuple[bool, Optional[float]]:
        """Trailing stop-loss check -- see compute_trailing_sl_price() and
        Config.sl_trail_pct/sl_initial_pct for the spec. Checked every run_cycle tick
        (10s), independent of the 45m candle gating that governs the EMA/
        RSI/ADX signal -- a long option's premium can move fast within a
        candle, so this can't wait for the next candle close the way the
        EMA/RSI exit does.

        WS-cache-first, REST-fallback price read -- same two-tier pattern
        used everywhere else a live price is needed in this file (e.g.
        _finalize_exit's exit_px read), including the two-sided-market
        liquidity guard on the REST fallback. Returns (False, None) if no
        trustworthy price is available this cycle -- never acts on a
        missing/stale quote, same philosophy as the rest of this file.

        Updates pos.highest_profit_pct (the ratchet) and persists it via
        _save_state() whenever it advances, so a restart mid-trade resumes
        with the correct trail level rather than resetting to 0. Logs a
        line ONLY when the effective SL price actually moves to a new tier
        (not on every tiny highest_profit_pct tick that doesn't cross a
        tier boundary) -- keeps this visible without spamming a line every
        10s during a normal favorable move."""
        ltp = self.price_stream.get_ltp(pos.symbol, inst.options_exchange, max_age=config.ws_stale_seconds)
        if ltp is None:
            ltp = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange, require_two_sided=True)
        if ltp is None or pos.entry_px <= 0:
            return False, None

        current_profit_pct = (ltp / pos.entry_px) - 1
        sl_price = compute_trailing_sl_price(pos.entry_px, pos.highest_profit_pct)
        if current_profit_pct > pos.highest_profit_pct:
            pos.highest_profit_pct = current_profit_pct
            new_sl_price = compute_trailing_sl_price(pos.entry_px, pos.highest_profit_pct)
            if new_sl_price != sl_price:
                Log.info(f"[{leg_key}] Trailing SL advanced: profit={pos.highest_profit_pct:.2%} "
                          f"-> sl_price={new_sl_price:.2f} (was {sl_price:.2f}), ltp={ltp:.2f}")
            sl_price = new_sl_price
            self._save_state()

        return ltp <= sl_price, sl_price

    def _exit_leg(self, leg_key: str, inst: InstrumentConfig, reason: str = "unknown"):
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        if pos.exit_filled:
            self._finalize_exit(leg_key, inst, reason)
            return

        if leg_key in self._pending_fills:
            return  # exit order already in flight -- background watcher will resolve it

        if not pos.exit_order_id:
            # See _enter_leg's matching comment -- an uncaught place() failure
            # here leaves a long option open indefinitely with no
            # error_state/UI alert at all.
            try:
                pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                           inst.options_exchange, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for exit: {exc}")
                self._enter_error_mode(leg_key, "exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        # Fill confirmation happens off this thread -- see _enter_leg's note on
        # _state_lock/_pending_fills/_fill_executor. place() above is the only
        # REST call left on the exit-signal-to-order path.
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_exit_fill, leg_key, inst, pos.exit_order_id, pos.symbol, pos.quantity
        )

    def _watch_exit_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                          symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                      "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.exit_order_id == order_id:  # guard vs. a superseded/stale order
                pos.exit_filled = True
                self._save_state()
        except OrderNeedsAttention as exc:
            self._enter_error_mode(leg_key, "exit_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(leg_key, "exit_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{leg_key}] Unexpected error while watching exit fill: {exc}")
            self._enter_error_mode(leg_key, "exit_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _finalize_exit(self, leg_key: str, inst: InstrumentConfig, reason: str):
        """Runs on the main thread's next run_cycle tick once _watch_exit_fill has
        set pos.exit_filled -- the trade-log write and today_realized_pnl update
        don't need to be on the background thread's own critical path, and
        keeping them here means the watcher thread's body stays minimal."""
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        Log.info(f"[{leg_key}] Position closed: {pos.symbol}")

        # A "Manually Completed" exit resolution sets manual_exit_px -- the
        # user's real fill price is authoritative there, not a fresh quote
        # (see docs/prd/python-strategies-order-error-recovery.md). Otherwise
        # WS-cache-first, REST fallback -- same pattern used everywhere else
        # price is read, instead of an unconditional REST round-trip here.
        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(pos.symbol, inst.options_exchange,
                                                 max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange, require_two_sided=True)
        if exit_px is not None:
            # Long option buyer: exit_px - entry_px (bought low/at entry,
            # sold at exit) -- opposite sign from a naked seller.
            self.store.state.today_realized_pnl += (exit_px - pos.entry_px) * pos.quantity
            self._save_state()
            try:
                append_trade_log(
                    strategy_tag, leg_key, pos.symbol, pos.quantity,
                    pos.entry_time, pos.entry_px,
                    datetime.now(IST).isoformat(), exit_px, reason,
                    pos.execution_id,
                )
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to append trade log: {exc}")
            # Dispatched to the background executor, NOT called inline --
            # _finalize_exit runs on the MAIN scheduler thread (see _exit_leg's
            # synchronous call into it), and notify_trade_closed makes a real
            # blocking network call (_post_json_local, 3s default timeout).
            # Calling it inline here would stall every OTHER leg's entry/exit
            # check for up to 3s on a slow/stuck local Flask response --
            # exactly the class of bug this codebase already fixed elsewhere
            # (see the "~11-minute production stall" note in this script's
            # module docstring) for every other REST call on this thread.
            try:
                self._fill_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)
            except Exception as exc:
                # .submit() itself can raise (e.g. RuntimeError: can't start
                # new thread, a transient OS-level thread-creation hiccup --
                # confirmed NOT a leak, just occasional resource contention)
                # before it ever returns a Future. Uncaught, this crashed
                # _finalize_exit and aborted the whole leg-exit cycle in
                # production. This push is fire-and-forget/best-effort (see
                # notify_trade_closed's own docstring) -- losing one live SSE
                # nudge is harmless; crashing leg finalization over it is not.
                Log.warning(f"[{leg_key}] Failed to dispatch notify_trade_closed: {exc}")
        else:
            # Both the WS cache and the REST fallback failed at this exact
            # moment -- the exit already filled at the broker (that's the
            # only way this function is reached), so leaving pos.exit_filled
            # set and NOT clearing the position means _exit_leg's own
            # `if pos.exit_filled: self._finalize_exit(...)` guard retries
            # this same price resolution again next cycle, instead of
            # silently and permanently losing this trade's PnL/log row.
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self.price_stream.remove_instruments(
            [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
        )
        leg.position = LegPosition()
        self._save_state()

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    # See docs/prd/python-strategies-order-error-recovery.md for the full
    # design rationale behind every branch here.
    def _resolve_leg_error(self, leg_key: str, inst: InstrumentConfig, action: dict):
        if leg_key in self._pending_fills:
            # A Retry/Cancel resolution (or a resumed fill watcher) is already
            # in flight for this leg -- e.g. the user submitted a second
            # action (Cancel-again, or switching to Retry) before the first
            # one's background watcher finished (up to fill_poll_timeout,
            # ~60s). error_state isn't cleared until that watcher completes,
            # so without this guard a second call here would dispatch a
            # SECOND concurrent watcher/place() against the same order --
            # two threads racing modifyorder/cancelorder, or worse, a
            # duplicate order from a second terminal-retry place(). The new
            # action is simply left un-acked and gets picked up on a later
            # cycle once this leg's _pending_fills entry clears.
            return
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            # The actual broker calls (reprice via modifyorder, or a fresh
            # place() for a terminal rejection) run on _fill_executor, not
            # inline -- a user clicking Retry must not block run_cycle's
            # per-cycle signal/exit checks for every other leg for a full
            # broker round-trip. Guard + ack happen here, synchronously and
            # cheaply, so run_cycle's error-check dispatch can't re-fire this
            # same action again on the next cycle while resolution is still
            # in flight.
            self._pending_fills.add(leg_key)
            ack_pending_action(self.env, leg_key)
            self._fill_executor.submit(
                self._do_retry_resolution, leg_key, inst, was_exit, kind
            )
            return

        if action["action"] == "cancel":
            if was_exit:
                # Ignore the failed attempt entirely -- no broker-side action,
                # no further re-pricing, regardless of error_kind. Position
                # stays open; a fresh exit_condition cycle places a brand-new
                # order normally later.
                pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(leg_key, pos, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            if kind == "terminal":
                # Nothing resting -- no broker call needed, straight to flat.
                self.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
                )
                leg.position = LegPosition()
                self._save_state()
                self._push_leg_error_bg(leg_key, leg.position, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            # kind == "resting": one last honest re-price + bounded wait, then
            # an explicit cancelorder() if it still didn't fill -- never
            # silently abandoned (an untracked entry order filling later would
            # create a position from nothing). Can take up to one
            # fill_poll_timeout window, so runs on _fill_executor, not inline.
            # Ack'd here, immediately, NOT deferred until _watch_entry_cancel
            # finishes -- otherwise error_state stays set and a later cycle's
            # error-check could re-read this same still-"pending" action from
            # the platform and dispatch a SECOND concurrent _watch_entry_cancel
            # against the same order_id (two threads racing modifyorder/
            # cancelorder on one order). Mirrors how the "retry" branch above
            # already acks before dispatch.
            ack_pending_action(self.env, leg_key)
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(
                self._watch_entry_cancel, leg_key, inst, pos.error_order_id,
                pos.symbol, pos.quantity
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if was_exit:
                pos.exit_filled = True
                pos.manual_exit_px = fill_price
                # _exit_leg's normal `if pos.exit_filled: self._finalize_exit(...)`
                # path runs next cycle and uses manual_exit_px (see that method).
            else:
                pos.entry_filled = True
                pos.entry_px = fill_price
                leg.trade_count += 1
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            self._push_leg_error_bg(leg_key, pos, clear=True)
            ack_pending_action(self.env, leg_key)

    def _do_retry_resolution(self, leg_key: str, inst: InstrumentConfig, was_exit: bool, kind: str):
        """The actual broker calls behind a Retry action (reprice via
        modifyorder, or a fresh place() for a terminal rejection) -- moved
        off the main scheduler thread by _resolve_leg_error, which has
        already added leg_key to _pending_fills and ack'd the action before
        submitting this. Discards leg_key from _pending_fills itself UNLESS
        it hands off to _watch_entry_fill, which owns that guard from then
        on (mirrors _watch_entry_fill/_watch_exit_fill/_watch_entry_cancel's
        own finally-discard pattern)."""
        try:
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if was_exit:
                # _exit_leg IS reliably called again on a later cycle regardless
                # of exit_condition's value (see the exit_already_committed
                # gate in run_cycle) -- and its body unconditionally resumes
                # watching whatever exit_order_id is currently set. So the exit
                # side only needs the reprice (if resting) + clearing the error
                # fields; the normal flow does the rest.
                if kind == "resting":
                    # Cross the spread (ask for BUY, bid for SELL) rather
                    # than re-quote the last-traded price -- matches
                    # _reprice_and_wait_once's approach, which is what
                    # actually gets a resting order filled on a thin book.
                    # Exit side here is SELL (closing a long) -> cross to bid.
                    bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, inst.options_exchange)
                    if bid is not None:
                        try:
                            self.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                symbol=pos.symbol, action="SELL",
                                exchange=inst.options_exchange, price_type="LIMIT",
                                product=config.product, quantity=str(pos.quantity),
                                price=str(bid), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[{leg_key}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                    # exit_order_id already equals error_order_id -- _exit_leg's
                    # own "if not pos.exit_order_id" guard stays false and it
                    # resumes watching this same order on its next call.
                else:  # kind == "terminal" -- nothing resting, must place fresh
                    pos.exit_order_id = ""  # _exit_leg places a brand-new close order next cycle
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(leg_key, pos, clear=True)
                self._pending_fills.discard(leg_key)
                return

            # Entry side: unlike exit, run_cycle only ever calls _enter_leg
            # when pos.symbol is EMPTY (has_position is false) -- and an
            # entry attempt already in error mode has pos.symbol set from the
            # moment the attempt began. run_cycle would therefore never call
            # _enter_leg again to resume this leg; Retry has to directly
            # (re)submit the watcher itself instead of relying on a normal-flow
            # path that structurally cannot fire for an in-progress entry.
            if kind == "resting":
                # Cross the spread (ask for BUY, bid for SELL) rather than
                # re-quote the last-traded price -- matches
                # _reprice_and_wait_once's approach, which is what actually
                # gets a resting order filled on a thin book.
                # Entry side here is BUY (opening a long) -> cross to ask.
                bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, inst.options_exchange)
                if ask is not None:
                    try:
                        self.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                            symbol=pos.symbol, action="BUY",
                            exchange=inst.options_exchange, price_type="LIMIT",
                            product=config.product, quantity=str(pos.quantity),
                            price=str(ask), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{leg_key}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:  # kind == "terminal" -- nothing resting, place a genuinely new order
                # If THIS retry attempt's own place() fails, re-enter error
                # mode with a fresh message/timestamp (see _enter_error_mode's
                # own docstring -- it's explicitly designed to be called again
                # on a repeated failure) instead of falling through to the
                # outer except, which only logs and would leave the UI
                # showing the stale pre-retry error text.
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                             inst.options_exchange, "BUY", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[{leg_key}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                    self._pending_fills.discard(leg_key)
                    return
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, leg_key, pos, clear=True)
            # _watch_entry_fill owns _pending_fills for this leg_key from here.
            self._fill_executor.submit(
                self._watch_entry_fill, leg_key, inst, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{leg_key}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(leg_key)

    def _watch_entry_cancel(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                             symbol: str, quantity: int):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned. Deliberately self-terminating (unlike the
        other watchers): ends in exactly one of two definitive outcomes, never
        back in error mode -- UNLESS something genuinely unexpected happens
        (see the outer except below), in which case it re-enters error mode
        rather than leaving the leg stuck. _pending_fills is ALWAYS released
        in the finally -- with _resolve_leg_error's re-entry guard now in
        place, a leg whose guard never clears would be permanently
        unresolvable via Retry/Cancel/Manual without a process restart."""
        strategy_tag = self.env.strategy_tag
        try:
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             symbol, inst.options_exchange, "BUY", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.error_order_id != order_id:
                return  # superseded by a newer action/order in the meantime -- do nothing
            if result is not None:
                pos.entry_filled = True
                leg.trade_count += 1
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.client.cancelorder(order_id=order_id, strategy=strategy_tag)
                except Exception as exc:
                    Log.warning(f"[{leg_key}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify "
                                f"manually at the broker that nothing is resting.")
                self.price_stream.remove_instruments(
                    [{"symbol": symbol, "exchange": inst.options_exchange}]
                )
                leg.position = LegPosition()
                self._save_state()
            # NOT ack_pending_action() here -- _resolve_leg_error's cancel/resting
            # branch already acked THIS action before dispatching us. Acking again
            # here would risk discarding a genuinely NEW action a user submitted
            # for this same leg while we were running (up to fill_poll_timeout),
            # since the platform's ack has no action-identity/token check -- it
            # would silently swallow that new action instead of leaving it for
            # the next cycle to pick up once _pending_fills clears in the finally.
            push_leg_error(self.env, leg_key, leg.position, clear=True)
        except Exception as exc:
            # _reprice_and_wait_once can raise (a RuntimeError if the order
            # landed rejected/cancelled during the wait, or an unguarded
            # client.orderstatus() failure) -- uncaught, that would leave this
            # leg's _pending_fills entry stuck forever, silently blocking all
            # future Retry/Cancel/Manual actions via _resolve_leg_error's
            # re-entry guard. Re-enter error mode instead so it stays
            # actionable; "resting" since we genuinely don't know the order's
            # final fate.
            Log.exception(f"[{leg_key}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    # ---- main cycle -----------------------------------------------------
    def _refresh_force_exit_check_bg(self):
        """Dispatches check_force_exit (a synchronous local HTTP call) to the
        background executor every cycle instead of running it inline -- a
        human just clicked the Force Exit button and expects it to be picked
        up quickly, so this is intentionally NOT throttled to a slow interval
        (an earlier interval-throttle traded away exactly that responsiveness
        for defense against a rare slow-local-HTTP edge case). Guarded so a
        slow check that outlives one scheduler_interval isn't resubmitted on
        top of itself; in the normal/fast case (a local unix-socket call,
        typically well under a second) this means a fresh check completes
        every single cycle, giving essentially real-time detection while
        still never blocking run_cycle even in the rare slow case."""
        if self._force_exit_check_pending:
            return
        self._force_exit_check_pending = True

        def _run():
            try:
                self._force_exit_pending = check_force_exit(self.env)
            except Exception as exc:
                Log.warning(f"check_force_exit background refresh failed: {exc}")
            finally:
                self._force_exit_check_pending = False

        self._bg_executor.submit(_run)

    def _handle_force_exit(self) -> bool:
        """Force-closes every leg currently holding a position, regardless of
        the strategy's own signal/exit logic -- called from run_cycle while a
        Force Exit is pending (see check_force_exit). Reuses _exit_leg
        exactly like the existing universal_exit force-close path, so it's
        idempotent/resumable across cycles (a leg with an exit already in
        flight is left alone; the background fill-watcher finishes it).
        Returns True only once every leg is fully flat, which is what lets
        run_cycle report completion back to the platform. A leg already in
        error mode is intentionally left untouched -- Force Exit doesn't
        override an unresolved Retry/Cancel/Manual decision; the user must
        resolve that first."""
        all_flat = True
        for leg_key in LEG_KEYS:
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.error_state:
                Log.warning(f"[{leg_key}] Force Exit waiting on an unresolved error "
                            f"({pos.error_state}/{pos.error_kind}) -- resolve it via "
                            f"Retry/Cancel/Manually Completed first.")
                all_flat = False
                continue
            if pos.symbol:
                all_flat = False
                inst_name = leg_key.split("_")[0]
                inst = next(i for i in INSTRUMENTS if i.name == inst_name)
                self._exit_leg(leg_key, inst, reason="force_exit")
        return all_flat

    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()
            self._refresh_mcx_session_window_bg()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                # _handle_force_exit leaves any leg already in error_state
                # untouched (Force Exit doesn't override an unresolved
                # Retry/Cancel/Manual decision) -- but this branch returns
                # right after, and while force_exit_pending stays True this
                # is the ONLY branch that runs. Without checking here too, a
                # user's Retry/Cancel/Manual click on that errored leg would
                # never be consumed, permanently deadlocking both the leg and
                # Force Exit until a manual restart. Resolve it first, same
                # as the per-leg loop below, so a just-resolved leg can be
                # force-closed in this same cycle by _handle_force_exit right
                # after.
                for leg_key in LEG_KEYS:
                    leg = self.store.state.legs[leg_key]
                    if leg.position.error_state:
                        inst_name = leg_key.split("_")[0]
                        inst = next(i for i in INSTRUMENTS if i.name == inst_name)
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- all positions flat. Stopping.")
                    ack_force_exit_complete(self.env)
                    self._force_exit_pending = False
                return

            if not self._within_market_hours():
                return

            futures_symbol = self.store.state.futures_symbol
            if not futures_symbol:
                return  # not yet resolved this cycle -- _reset_day_if_needed will retry

            past_universal_exit = self._past_universal_exit()

            if past_universal_exit:
                for leg_key in LEG_KEYS:
                    leg = self.store.state.legs[leg_key]
                    inst_name = leg_key.split("_")[0]
                    inst = next(i for i in INSTRUMENTS if i.name == inst_name)
                    if leg.position.error_state:
                        # Frozen awaiting a Retry/Cancel/Manual decision -- do NOT
                        # auto-force an exit through an errored leg. Still checks
                        # for a pending action every cycle even after hours (this
                        # branch used to `return` right after this loop, which
                        # meant the ONLY check_pending_action call site -- in the
                        # normal per-instrument loop below -- never ran again for
                        # the rest of the day once past_universal_exit went true,
                        # permanently stranding an errored leg with no way for a
                        # Retry/Cancel/Manual click to ever be consumed). Once
                        # resolved, the leg naturally falls through to the
                        # force-close check below on the NEXT cycle (still past
                        # universal exit time), so it still gets closed before
                        # the day ends.
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        else:
                            Log.error(f"[{leg_key}] Universal exit time reached but this leg is "
                                      f"still in error mode ({leg.position.error_state}) -- "
                                      f"resolve it via Retry/Cancel/Manually Completed NOW.")
                        continue
                    if leg.position.symbol:
                        Log.warning(f"[{leg_key}] Universal exit time reached; force-closing.")
                        self._exit_leg(leg_key, inst, reason="universal_exit")
                return

            within_entry = self._within_entry_window()

            for inst in INSTRUMENTS:
                # Live futures LTP from the WebSocket cache (pushed, not
                # polled) -- falls back to a single one-off REST quotes()
                # call for just this contract if the feed is stale/missing.
                inst_ltp = self.price_stream.get_ltp(
                    futures_symbol, inst.options_exchange, max_age=config.ws_stale_seconds
                )
                if inst_ltp is None:
                    if not self._ws_fallback_logged.get(inst.name):
                        Log.warning(f"[{inst.name}] WS LTP stale/missing -- falling back to REST quotes().")
                        self._ws_fallback_logged[inst.name] = True
                    inst_ltp = fetch_symbol_ltp(self.ltp_client, futures_symbol, inst.options_exchange)
                else:
                    self._ws_fallback_logged[inst.name] = False
                # A new leg can only ever be entered within the entry window, and
                # only on whichever side (PE/CE) is still flat -- gates the
                # background chain refresh so it doesn't run all day regardless.
                still_enterable = within_entry and any(
                    not self.store.state.legs[f"{inst.name}_{ot}"].position.symbol
                    for ot in ("PE", "CE")
                )
                signal = self.get_signal(inst, futures_symbol, ltp=inst_ltp,
                                          refresh_chain=still_enterable)
                if signal is None:
                    continue

                for option_type in ("PE", "CE"):
                    leg_key = f"{inst.name}_{option_type}"
                    leg = self.store.state.legs[leg_key]

                    if leg.position.error_state:
                        # Frozen awaiting a Retry/Cancel/Manual decision (see
                        # docs/prd/python-strategies-order-error-recovery.md) --
                        # this is what makes the pause per-leg, not per-strategy:
                        # every OTHER leg_key/instrument in these two loops is
                        # untouched and keeps evaluating its own signal normally.
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        continue

                    has_position = bool(leg.position.symbol)

                    if option_type == "CE":
                        entry_condition = (
                            signal.close_prev1 > signal.ema_high_prev1
                            and signal.rsi_prev1 > config.ce_rsi_entry_threshold
                            and signal.adx_prev1 > config.adx_threshold
                            and signal.close_prev1 > signal.vwap_prev1
                            and signal.ltp > signal.vwap_prev1
                        )
                        exit_condition = (
                            signal.close_prev1 < signal.ema_low_prev1
                            or signal.rsi_prev1 < config.ce_rsi_exit_threshold
                        )
                    else:  # PE
                        entry_condition = (
                            signal.close_prev1 < signal.ema_low_prev1
                            and signal.rsi_prev1 < config.pe_rsi_entry_threshold
                            and signal.adx_prev1 > config.adx_threshold
                            and signal.close_prev1 < signal.vwap_prev1
                            and signal.ltp < signal.vwap_prev1
                        )
                        exit_condition = (
                            signal.close_prev1 > signal.ema_high_prev1
                            or signal.rsi_prev1 > config.pe_rsi_exit_threshold
                        )

                    if has_position:
                        # Once an exit order has been placed (or its fill confirmed by
                        # the background watcher), it must be driven through to
                        # _finalize_exit regardless of what exit_condition does on a
                        # LATER cycle (e.g. RSI recovers before the async fill lands)
                        # -- otherwise a filled-but-not-yet-finalized position would
                        # never get its trade-log row written or its leg cleared,
                        # permanently blocking re-entry.
                        exit_already_committed = bool(leg.position.exit_order_id) or leg.position.exit_filled
                        # Trailing stop-loss -- checked every cycle using LIVE price,
                        # independent of the 45m candle gating that governs
                        # exit_condition above (see _check_stop_loss's own docstring).
                        sl_hit, sl_price = self._check_stop_loss(leg_key, leg.position, inst)
                        if exit_condition or sl_hit or exit_already_committed:
                            if not exit_already_committed:
                                if sl_hit and not exit_condition:
                                    Log.info(f"[{leg_key}] Stop-loss hit -> closing. "
                                              f"ltp<=sl_price={sl_price:.2f} (highest_profit_pct="
                                              f"{leg.position.highest_profit_pct:.2%})")
                                elif exit_condition:
                                    Log.info(f"[{leg_key}] Exit condition met -> closing.")
                            reason = "stop_loss" if (sl_hit and not exit_condition) else "ema_rsi_adx_reversal"
                            if sl_hit and reason == "stop_loss":
                                # Blocks a fresh entry on THIS SAME leg until the
                                # WALL-CLOCK candle advances -- see LegState.
                                # last_sl_exit_candle_boundary's own comment for
                                # why this is _current_candle_boundary(), not
                                # signal.candle_key (the latter is a background-
                                # refreshed cache that can lag the true boundary
                                # and let a re-entry slip through, confirmed live
                                # 2026-08-11).
                                leg.last_sl_exit_candle_boundary = str(
                                    _current_candle_boundary(config.candle_interval_minutes)
                                )
                            self._exit_leg(leg_key, inst, reason=reason)
                        continue

                    if not within_entry:
                        continue
                    if leg.trade_count >= config.max_trades_per_leg_per_day:
                        continue
                    if leg.last_sl_exit_candle_boundary and leg.last_sl_exit_candle_boundary == str(
                        _current_candle_boundary(config.candle_interval_minutes)
                    ):
                        continue
                    if not entry_condition:
                        continue

                    if option_type == "CE":
                        condition_desc = (
                            f"close_prev1={signal.close_prev1:.2f} > ema_high_prev1={signal.ema_high_prev1:.2f}, "
                            f"rsi_prev1={signal.rsi_prev1:.2f} > ce_rsi_entry_threshold={config.ce_rsi_entry_threshold}, "
                            f"adx_prev1={signal.adx_prev1:.2f} > adx_threshold={config.adx_threshold}, "
                            f"close_prev1={signal.close_prev1:.2f} > vwap_prev1={signal.vwap_prev1:.2f}, "
                            f"ltp={signal.ltp:.2f} > vwap_prev1={signal.vwap_prev1:.2f}"
                        )
                    else:
                        condition_desc = (
                            f"close_prev1={signal.close_prev1:.2f} < ema_low_prev1={signal.ema_low_prev1:.2f}, "
                            f"rsi_prev1={signal.rsi_prev1:.2f} < pe_rsi_entry_threshold={config.pe_rsi_entry_threshold}, "
                            f"adx_prev1={signal.adx_prev1:.2f} > adx_threshold={config.adx_threshold}, "
                            f"close_prev1={signal.close_prev1:.2f} < vwap_prev1={signal.vwap_prev1:.2f}, "
                            f"ltp={signal.ltp:.2f} < vwap_prev1={signal.vwap_prev1:.2f}"
                        )
                    self._enter_leg(leg_key, inst, option_type, spot=signal.ltp, futures_symbol=futures_symbol,
                                     condition_desc=condition_desc)

        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            # WhatsApp self-alert for a genuinely unexpected crash not
            # already routed through _enter_error_mode's own alert.
            # Throttled since an outer catch-all could otherwise fire every
            # scheduler tick if the same bug keeps recurring.
            now = datetime.now(IST)
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                try:
                    self._pnl_executor.submit(
                        notify_whatsapp_error, self.env,
                        f"[{config.strategy_name}] Cycle failed: {exc}",
                        log_warning=Log.warning,
                    )
                except Exception as dispatch_exc:
                    Log.warning(f"Failed to dispatch WhatsApp crash notification: {dispatch_exc}")


###############################################################################
# STARTUP
###############################################################################
def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version              : {config.version}")
    print(f"Instrument           : {INSTRUMENTS[0].name} ({INSTRUMENTS[0].options_exchange})")
    print(f"EMA period / RSI / ADX : {config.ema_period} / {config.rsi_period} / {config.adx_period} (thresh {config.adx_threshold})")
    print(f"Interval             : {config.candle_interval_minutes}m (resampled from {config.fetch_interval} fetch)")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print(f"Product              : {config.product}")
    print("Order pricing        : LIMIT crossing spread (MARKET rejected by broker)")
    print(f"Stop-loss            : initial {config.sl_initial_pct:.0%}, then flat "
          f"{config.sl_trail_pct:.0%} trailing gap per step (see module docstring)")
    print(f"Strike selection     : ATM+1, round-{config.strike_round} only")
    print(f"Expiry roll buffer   : {config.expiry_roll_trading_days_before} trading day(s)")
    print("LONG OPTION BUYING -- BOUNDED RISK, PREMIUM CAN DECAY TO NEAR-ZERO")
    print("NO NATIVE PER-TRADE STOP-LOSS -- exit is EMA/RSI reversal only")
    if config.test_mode:
        print("TEST MODE ENABLED -- market-hours/entry-window checks are BYPASSED")
    print("=" * 70)


def main():
    print_banner()

    env = Environment()
    _migrate_trade_log_if_needed(env.strategy_tag)
    state_store = StateStore(env)
    state_store.load()

    # New execution number for this process run -- see LegPosition.execution_id
    # and the Trades UI's execution dropdown.
    state_store.state.last_execution_id += 1
    execution_id = state_store.state.last_execution_id
    state_store.save()

    broker = Broker(env)
    client = broker.connect()
    ltp_client = broker.connect_ltp_client()

    price_stream = PriceStream(client)
    price_stream.start()

    # Mid-day restart: if today's futures contract was already resolved and
    # any option legs already opened before this process started, subscribe
    # to those symbols immediately instead of waiting for the next
    # _reset_day_if_needed()/entry (same resumability guarantee as
    # VWAP_NoHA/Batman).
    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        if state_store.state.futures_symbol:
            already_known.append({
                "symbol": state_store.state.futures_symbol,
                "exchange": INSTRUMENTS[0].options_exchange,
            })
        for leg in state_store.state.legs.values():
            if leg.position.symbol:
                already_known.append({
                    "symbol": leg.position.symbol,
                    "exchange": INSTRUMENTS[0].options_exchange,
                })
    if already_known:
        price_stream.add_instruments(already_known)

    print()
    print("=" * 70)
    print("HEALTH CHECK")
    print("=" * 70)
    print(f"OpenAlgo Connected : {broker.connected}")
    print(f"State File         : OK ({state_store.path})")
    print(f"Execution ID       : {execution_id}")
    print(f"Price Stream       : starting ({len(already_known)} symbol(s) already known today)")
    print("=" * 70)

    engine = StrategyEngine(client, state_store, env, price_stream, execution_id=execution_id,
                             ltp_client=ltp_client)
    # Upgrade PriceStream's watchdog from the static market-hours check to
    # the MCX-holiday/special-session-aware one now that the engine exists
    # (see PriceStream.set_market_hours_check's own docstring) -- safe even
    # though price_stream.start() already ran above; the watchdog re-reads
    # this on every loop iteration.
    price_stream.set_market_hours_check(engine._within_market_hours)

    # Restart while a leg was in error mode: STRATEGY_ERRORS on the platform
    # side is in-memory only, so re-push any already-erroring leg once here --
    # otherwise the UI badge would silently vanish across a restart even
    # though the leg is still frozen awaiting a decision. See
    # docs/prd/python-strategies-order-error-recovery.md.
    for leg_key, leg in state_store.state.legs.items():
        if leg.position.error_state:
            action = "BUY" if leg.position.error_state == "entry_failed" else "SELL"
            push_leg_error(env, leg_key, leg.position, action=action)
            Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                      f"({leg.position.error_state}/{leg.position.error_kind}) -- "
                      f"needs Retry/Cancel/Manually Completed.")
            continue

        # Restart between order-placement and fill-confirmation: run_cycle
        # only calls _enter_leg for a leg with NO position yet, and _exit_leg
        # for one WITH a position -- neither path re-arms the fill watcher
        # for a leg whose entry/exit order was placed but never confirmed
        # before this process died. Without this, has_position stays True
        # forever (blocking re-entry) while entry_filled never becomes True,
        # and a later exit_condition could fire a closing order against an
        # entry that was never confirmed filled at the broker.
        pos = leg.position
        inst_name = leg_key.split("_")[0]
        inst = next((i for i in INSTRUMENTS if i.name == inst_name), None)
        if inst is None:
            continue
        if pos.entry_order_id and not pos.entry_filled:
            Log.warning(f"[{leg_key}] Resuming entry-fill watch for an order placed "
                        f"before a restart (order_id={pos.entry_order_id}).")
            engine._pending_fills.add(leg_key)
            engine._fill_executor.submit(
                engine._watch_entry_fill, leg_key, inst, pos.entry_order_id,
                pos.symbol, pos.quantity
            )
        elif pos.exit_order_id and not pos.exit_filled:
            Log.warning(f"[{leg_key}] Resuming exit-fill watch for an order placed "
                        f"before a restart (order_id={pos.exit_order_id}).")
            engine._pending_fills.add(leg_key)
            engine._fill_executor.submit(
                engine._watch_exit_fill, leg_key, inst, pos.exit_order_id,
                pos.symbol, pos.quantity
            )

    Log.info("Strategy Initialization Complete. Starting scheduler...")

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        engine.run_cycle,
        trigger=IntervalTrigger(seconds=config.scheduler_interval),
        id="strategy_cycle",
        max_instances=1,
        coalesce=True,
    )
    # Separate, faster job purely for PnL -- see report_pnl_tick's own
    # docstring for why this is safe to run far more often than the main
    # strategy cycle (cache-only, never a REST call).
    scheduler.add_job(
        engine.report_pnl_tick,
        trigger=IntervalTrigger(seconds=config.pnl_tick_interval),
        id="pnl_tick",
        max_instances=1,
        coalesce=True,
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        Log.info("Shutting down scheduler.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        engine._pnl_executor.shutdown(wait=False)
        engine._refresh_executor.shutdown(wait=False)
    except Exception:
        # scheduler.start() shouldn't normally raise anything else (job
        # exceptions are caught/logged by APScheduler itself), but if it
        # ever does, run the exact same cleanup instead of leaking the
        # WebSocket connection and both thread pools silently.
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        engine._pnl_executor.shutdown(wait=False)
        engine._refresh_executor.shutdown(wait=False)
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
