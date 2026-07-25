"""
===============================================================================
Nifty & Sensex Pivot Point + Supertrend Intraday Option Seller
===============================================================================
Version     : 1.8.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Ported from : Tradetron export "Nifty_sensex_Pivot_point_with_St_73 (2).json"
              -- see Nifty_Sensex_Pivot_Supertrend_Intraday_ANALYSIS.md for
              the full decoded-logic writeup this script was built from.

Description
-----------
Pure INTRADAY option-selling strategy across two underlyings (NIFTY/NFO and
SENSEX/BFO), four independent legs (NIFTY PE, NIFTY CE, SENSEX PE, SENSEX
CE). Each leg has its own entry/exit condition and its own daily trade
counter -- you can be short NIFTY PE and NIFTY CE at the same time, they
don't interact.

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
Unlike OpenAlgo_Nifty_Weekly_Trend_Seller (which always buys a far-OTM hedge
first for defined risk), this is a faithful, literal port of the source
Tradetron strategy, which has no hedge leg at all. This was an explicit,
confirmed choice (port for fidelity, not to add a hedge) -- do not treat this
as equivalent in risk to the hedged weekly strategy.

Signal Rules (per instrument, shared between its PE and CE leg)
------------------------------------------------------------------
Computed once per cycle per instrument:
  - r1, s1     = daily Pivot Points (standard) from the previous COMPLETED
                 trading day's H/L/C.
  - last_close, last_high, last_low = the last CLOSED 5m candle's own OHLC.
  - supertrend = Supertrend(7, 3) on 5m candles, last CLOSED bar.
  - ltp        = live LTP (client.quotes()) -- used only as a live
                 breakout-confirmation filter on top of the closed-candle
                 signal (see below), not for the Supertrend comparison
                 itself.

  PE entry : last_close > r1            (closed candle above pivot R1)
             AND supertrend < last_close (closed candle confirms bullish)
             AND ltp > last_high         (live price still above that
                                          candle's High -- breakout hasn't
                                          failed since the candle closed)
  CE entry : s1 > last_close            (closed candle below pivot S1)
             AND supertrend > last_close (closed candle confirms bearish)
             AND ltp < last_low          (live price still below that
                                          candle's Low)
  PE exit  : supertrend > ltp   (live LTP crosses back below Supertrend)
  CE exit  : ltp > supertrend   (live LTP crosses back above Supertrend)

  No per-trade stop-loss -- exit is the Supertrend(7,3) flip only. A Rs
  1,500 per-trade stop-loss was backtested and deployed in an earlier
  version (see ANALYSIS.md's threshold sweep) and outperformed no-SL on
  this specific backtest window, but was removed per explicit instruction
  -- do not re-add without a fresh confirmation.

Live price feed: WebSocket, not REST polling (v1.8.0)
------------------------------------------------------------------------
Underlying LTP (NIFTY/SENSEX) now comes from the shared OpenAlgo WebSocket
proxy (`PriceStream`, subscribe_ltp mode) instead of a per-cycle
`client.quotes()`/`multiquotes()` REST call. Rationale, found while
auditing this strategy for live Shoonya deployment alongside 3 other
concurrent strategy instances:

  - Shoonya's `client.multiquotes()` in this OpenAlgo installation is NOT
    a true broker-side batch endpoint -- it fans out to one individual
    `GetQuotes` HTTP call per symbol, fired concurrently via a
    ThreadPoolExecutor (see broker/shoonya/api/data.py). A "single batched
    call" for N symbols is actually N near-simultaneous REST hits to
    Shoonya. With 4 strategy instances sharing one Shoonya session and
    none of their scheduler intervals jittered, their per-cycle bursts can
    phase-align and spike past Shoonya's real ~10 quotes/sec ceiling in a
    single instant, even though the averaged per-minute volume stays
    comfortably under budget.
  - WebSocket streaming avoids this entirely: OpenAlgo pools ONE persistent
    broker-side WebSocket connection per broker+user session
    (`websocket_proxy/broker_factory.py:_POOLED_ADAPTERS`), fanned out over
    ZeroMQ to every local subscriber (all 4 strategy processes here share
    that single upstream Shoonya connection) -- no repeated REST polling,
    no rate-limit math to get right, ticks are pushed instead of pulled.
  - This strategy's signal logic reads the UNDERLYING's own price directly
    (pivots/Supertrend on the index, not the option), so only the 2
    underlyings (NIFTY, SENSEX) need a live subscription -- the option
    legs' own live LTP is not used in any entry/exit decision (that was
    only true when the now-removed per-trade Rs 1,500 stop-loss existed;
    see the removed `per_trade_stop_loss` history above). The old
    per-cycle option-leg LTP batch-fetch was therefore pure waste even
    before this change and has been removed; `fetch_symbol_ltp()` is still
    used, but only once per closed trade (in `_exit_leg`, for the trade
    log's exit-price estimate) -- not on the hot per-cycle path.
  - `PriceStream` subscribes once at startup via `client.subscribe_ltp()`
    and keeps an in-memory `{(symbol, exchange): (ltp, tick_time)}` cache
    updated by the push callback. `run_cycle()` reads from this cache
    (`max_age` staleness check, default 20s) and falls back to a single
    one-off `client.quotes()` REST call for just that instrument if the
    cache entry is missing or stale -- so a dead/reconnecting WebSocket
    degrades to the old REST behavior for that instrument rather than
    stalling the signal entirely. A background watchdog thread checks each
    subscribed instrument INDIVIDUALLY (not an aggregate "all stale" check)
    every `ws_watchdog_interval` (15s) during market hours: if the
    connection itself is down, it does a full reconnect+re-subscribe
    (bounded backoff); if the connection is alive but only SOME symbol(s)
    are stale/silent (e.g. a bad token mapping for just one instrument), it
    resubscribes only those -- `subscribe_ltp`/`unsubscribe_ltp` operate
    purely on the given instrument list and don't touch the connection or
    any other symbol's subscription (confirmed against the installed
    `openalgo` SDK's feed.py), so a healthy symbol's feed is never
    disrupted by fixing a different, broken one. The SDK itself also has
    its own built-in auto-reconnect (`auto_reconnect=True` by default,
    exponential backoff, automatic subscription replay) for a genuine
    connection drop -- this watchdog's per-symbol staleness check is what
    catches the case the SDK's own mechanism structurally cannot: a silent
    subscription-level failure where the connection stays healthy but one
    symbol just never ticks. Either way, this strategy always re-subscribes
    explicitly rather than assuming the server restores it ("some plugins
    auto-restore subscriptions on reconnect; never rely on this" -- per
    OpenAlgo's own WS reference docs).

Order placement robustness (v1.8.0)
------------------------------------------------------------------------
Found while reviewing this script for live deployment: if an entry or
exit order came back from the broker as `rejected`/`cancelled`,
`poll_fill()` raised, but the leg's persisted `entry_order_id`/
`exit_order_id` was NEVER cleared -- every subsequent cycle re-polled the
SAME dead order id, got the same terminal rejection, and raised again,
forever. For an entry this just meant the leg silently stopped trying to
open a new position. For an EXIT this was a real risk: a rejected close
order left the position open at the broker while the strategy endlessly
re-checked a dead order id instead of ever placing a fresh close order.
Fixed:
  - `_enter_leg`: a rejected/cancelled entry order clears the leg's entire
    position back to blank, so the NEXT cycle re-resolves strike/expiry
    and places a genuinely new order (does not consume a trade_count
    slot, matching the prior behavior before this fix).
  - `_exit_leg`: a rejected/cancelled exit order clears only
    `exit_order_id` (the leg is still open at the broker) so the next
    cycle places a brand-new close order instead of re-polling the dead
    one. A repeated failure is surfaced via the error-mode/
    Retry-Cancel-Manual system (see `_enter_error_mode`), not a local
    counter, since a stuck naked short position is the single worst
    failure mode for this strategy and needs to be loud, not silent.
  - `place()` now retries up to 3 attempts (1.5s apart) on either a raised
    exception (network blip) or a non-"success" response BEFORE giving up
    and raising -- absorbs transient errors without masking a genuine
    persistent rejection, which still surfaces (and is now handled
    correctly by the two fixes above instead of looping on a dead order
    id).
  - A `TimeoutError` from `poll_fill` now means it already tried
    RE-PRICING the stale order (via `modifyorder()`, to the current LTP)
    up to `config.reprice_max_attempts` times, then cancelled it after all
    of those still didn't fill -- so by the time it's raised, the order id
    is dead for good, and it IS treated the same as a rejection (clear and
    retry fresh next cycle). See `poll_fill`'s own docstring for the full
    reprice mechanism.

Persistent trade log (state.json intentionally left unchanged)
------------------------------------------------------------------
`state.json` only ever tracks each leg's CURRENTLY open position --
`_exit_leg()` resets it to a blank `LegPosition()` on close, so the record
of a completed trade would otherwise vanish entirely (only a `Log.info()`
line survives). `append_trade_log()` writes one row per closed leg to
`trades_{STRATEGY_ID}.csv` (same directory as the state file, same
STRATEGY_ID-based naming), BEFORE that reset -- leg, symbol, quantity,
entry/exit time+price, pnl_points, pnl_rupees, exit_reason. Same column
shape as the backtest's trade CSV for consistency. This is plain
open-append-close file I/O, not a new SQLite table/engine -- OpenAlgo's own
order/trade books are fetched live from the broker per-request and aren't
persisted with a queryable `strategy` column for live trading (confirmed by
reading services/place_order_service.py and restx_api/orderbook.py; only
Sandbox/Analyzer mode's database/sandbox_db.py has that structure, and only
for paper trades) -- so there was no existing OpenAlgo-native table to hook
into here. entry_px/exit_px are LTP-based estimates (same basis _enter_leg
already used for entry_px), not confirmed broker average-fill prices.

The actual CSV write happens on a dedicated background thread (a single
module-level singleton, started lazily, never spawned per call), not
inline in `_exit_leg()` -- `append_trade_log()` just enqueues the row and
returns immediately, so a disk write can never add latency to the main
scheduler loop's entry/exit condition checks. `state.json`'s own save()
calls remain synchronous as before (that's the resumability source of
truth and must stay that way); only this non-critical audit log is
offloaded.

Shared rules across all 4 legs
--------------------------------
  - Runs the full session, 09:15 - 15:30 (config.market_close = 15:30).
    Entry window is narrower, 09:20 - 15:00, matching the source Tradetron
    config.
  - Entry/exit conditions are checked near-CONTINUOUSLY -- every cycle
    (`scheduler_interval`, default 10s) re-fetches live LTP and re-evaluates
    the crossover, since a live-tick crossover can happen at any second, not
    just on a 5m candle boundary. The expensive part (daily pivots + 5m
    Supertrend, both from `client.history()`) is cached and only refreshed
    at most once per `indicator_refresh_interval` (default 15s) -- see
    `StrategyEngine.get_signal()`. This keeps the crossover check continuous
    without hammering `client.history()` every few seconds.
  - Universal exit : >= 15:15 -- force-close EVERY open leg unconditionally,
                     regardless of that leg's own exit condition. Pure
                     intraday: nothing is ever held overnight.
  - Max 3 entries per leg per day (resets daily, not weekly).
  - Strike selection: ATM (closest strike to spot), current week expiry,
    resolved fresh on every entry -- EXCEPT on the underlying's own expiry
    day itself, when `resolve_current_week_expiry()` rolls to NEXT week's
    expiry instead (see that function's docstring; backtested improvement,
    smaller worst-trade/max-drawdown at a modest cost to total return).
    Otherwise no expiry-lookahead/rollover machinery -- unlike the weekly
    strategy, there's nothing else to roll: today's expiry
    resolution is only ever used for a same-day entry).
  - Quantity: 1 lot per leg (config.lot_multiplier).
  - Product: NRML (this is a literal port of the source Tradetron config,
    which sets NRML on every leg even though the Universal Exit always
    squares off same-day. MIS is the more typical choice for an
    always-same-day strategy -- it changes margin treatment and adds
    broker-RMS auto-square-off as an extra safety net around 15:15-15:20.
    Left as NRML for fidelity to the source; change config.product to "MIS"
    if you'd rather have that safety net).

Design notes carried over from OpenAlgo_Nifty_Weekly_Trend_Seller_2/3
------------------------------------------------------------------------
  - Resumable, leg-by-leg entry/exit: each leg's order id and fill status is
    persisted to the state file *before* waiting on the next step, so a
    crash/restart mid-entry or mid-exit resumes correctly instead of losing
    track of a live order or re-issuing a duplicate.
  - State file unique per strategy_tag (STRATEGY_ID fallback), anchored to
    this script's own directory -- not the launcher's cwd.
  - `client.history()` can return an error dict instead of a DataFrame on a
    bad broker session -- handled explicitly, logs a warning and returns
    "no signal" instead of crashing the cycle.
  - The broker's last returned bar (daily AND 5m) is dropped unconditionally
    before use -- it is frequently still live/updating well past its
    nominal close, and using it as "the last completed candle" would mean
    trading on an unsettled bar (observed in production on the weekly
    strategy; same defensive pattern applied here from the start).

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.supertrend(high, low, close, period=7, multiplier=3.0)` returns
    `(line, direction)` -- only `line` is used here (matching the Tradetron
    condition, which compares the Supertrend *value*, not its direction flag).
  * `ta.pivot_points(high, low, close)` returns
    `(pivot, r1, s1, r2, s2, r3, s3)` -- only r1/s1 are used here.
  * `client.quotes(symbol=, exchange=)` -> live LTP.
  * `client.placeorder(...)` uses `price_type` per the official Python
    library docs/README.
  * `client.expiry(symbol=, exchange=, instrumenttype=)` returns dates in
    "DD-MMM-YY" format; OpenAlgo order/chain endpoints want "DDMMMYY"
    (no dashes).

Author
------
<Project Owner>
===============================================================================
"""

import csv
import json
import logging
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
from enum import Enum
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

load_dotenv()

print("🔁 OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
@dataclass
class InstrumentConfig:
    name: str                    # "NIFTY" / "SENSEX"
    underlying_exchange: str     # NSE_INDEX / BSE_INDEX
    options_exchange: str        # NFO / BFO


INSTRUMENTS = [
    InstrumentConfig(name="NIFTY", underlying_exchange="NSE_INDEX", options_exchange="NFO"),
    InstrumentConfig(name="SENSEX", underlying_exchange="BSE_INDEX", options_exchange="BFO"),
]


@dataclass
class Config:
    strategy_name: str = "Nifty & Sensex Pivot Point + Supertrend Intraday Seller"
    version: str = "1.6.0"

    intraday_interval: str = "5m"     # standard, documented OpenAlgo interval
    history_lookback_days: int = 10   # calendar days of 5m history to fetch (Supertrend(7,3) warmup) --
                                       # was today-only (start_date=end_date=today), which meant a
                                       # multi-bar warmup requirement (supertrend_period + 1) couldn't be
                                       # satisfied until enough of TODAY's session had elapsed (e.g.
                                       # 8 bars * 5m = 40 minutes after the 09:15 open) -- every single
                                       # morning, regardless of any prior day's data. EMA34_RSI/MCX
                                       # already use this multi-day lookback pattern; this just brings
                                       # Pivot_Supertrend in line with them. Signal-computation always
                                       # reads only the tail of the series (line[-1] etc.), so a wider
                                       # fetch window is strictly additive -- no behavior change once
                                       # enough same-day bars exist, just no more empty morning warmup.
    daily_interval: str = "D"
    supertrend_period: int = 7
    supertrend_multiplier: float = 3.0

    lot_multiplier: int = 1           # number of lots per leg
    max_trades_per_leg_per_day: int = 3

    product: str = "NRML"             # literal port of source config -- see module docstring
    price_type: str = "MARKET"

    entry_start: time = time(9, 20)
    entry_end: time = time(15, 0)
    universal_exit_time: time = time(15, 15)   # force-close everything at/after this
    market_close: time = time(15, 30)          # strategy actively runs the full session, 9:15-15:30

    # Entry/exit conditions compare a closed-candle indicator (Supertrend,
    # daily pivots) against LIVE LTP -- the crossover moment itself can
    # happen at any second, not just on a 5m candle boundary. So the cycle
    # runs frequently (near-continuous) via `scheduler_interval`, but the
    # expensive client.history() calls (daily + 5m OHLC) are throttled
    # separately via `indicator_refresh_interval` -- only LTP (a cheap
    # quotes() call) is fetched on every single cycle. This keeps the
    # crossover check continuous without hammering client.history().
    scheduler_interval: int = 10             # seconds between strategy cycles (LTP check cadence)
    indicator_refresh_interval: int = 15     # seconds between re-fetching the 5m Supertrend signal
    daily_refresh_interval: int = 600        # seconds between re-fetching the daily pivot (changes once/day)
    pnl_tick_interval: float = 0.8             # seconds between PnL pushes -- runs on its OWN scheduler
                                               # job (see report_pnl_tick), decoupled from
                                               # scheduler_interval, since it's cache-only/read-only and
                                               # doesn't share the blocking-call risk that interval guards

    # WebSocket LTP cache: a tick older than this is treated as stale and
    # falls back to a one-off REST client.quotes() call for that instrument.
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
    # 15s per wait-cycle (1 initial + 19 reprices) = 20 x 15s = 300s (5 min)
    # total before giving up and raising OrderNeedsAttention -- each reprice
    # crosses the spread with a fresh bid/ask (see _reprice_and_wait_once),
    # not just the last-traded price.
    fill_poll_timeout: float = 15.0
    reprice_max_attempts: int = 19   # times poll_fill() re-prices a stale unfilled order before giving up

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

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
    entry_px: float = 0.0          # option premium at entry -- trade-log basis
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


@dataclass
class LegState:
    trade_count: int = 0
    position: LegPosition = field(default_factory=LegPosition)


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    last_updated: str = ""
    today_realized_pnl: float = 0.0  # sum of closed legs' pnl_rupees today -- pushed via report_pnl_to_platform
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
        # Host sets STRATEGY_ID (unique per strategy config entry) but never
        # OPENALGO_STRATEGY_TAG -- fall back to STRATEGY_ID so two instances
        # under the host never collide on the same state file / order tag.
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "nifty_sensex_pivot_supertrend_intraday"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    """Shared by StrategyEngine and PriceStream's reconnect watchdog -- an
    index feed goes silent outside market hours by design, so staleness
    checks must not fire (and force pointless reconnects) overnight."""
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return time(9, 15) <= now <= config.market_close


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
# LIVE PRICE STREAM (WebSocket, replaces per-cycle REST quotes for the
# underlyings -- see the module docstring's "Live price feed" section for
# the full rationale)
###############################################################################
class PriceStream:
    """Subscribes to LTP mode for a DYNAMICALLY growing set of symbols --
    the two underlying indices at startup, plus each option leg's own
    symbol as it's resolved at entry (see add_instruments, called from
    _enter_leg) -- over OpenAlgo's shared WebSocket proxy. Without this, an
    open option leg's LTP was never actually WS-cached, so PnL/reporting
    code that reads only the cache (report_pnl_tick) could never see an
    open position for this strategy; it silently always fell back to
    "no cached price" for every option leg. Keeps an in-memory,
    thread-safe {(symbol, exchange): (ltp, tick_time)} cache updated by the
    push callback. A background watchdog thread detects a stale/silent feed
    during market hours and transparently reconnects + re-subscribes
    (bounded backoff) -- never assumes the server auto-restores
    subscriptions on reconnect, per OpenAlgo's own WS docs guidance. If a
    symbol stays stale across several consecutive cycles despite a
    per-symbol resubscribe, escalates to a full reconnect instead (see
    _watchdog_loop) -- confirmed in production that the per-symbol path
    alone can retry 30+ times with zero recovery while the connection
    itself stays reported connected/authenticated the whole time, so
    something in that connection's own state needs a clean reset, not
    another poke at the same symbol."""

    def __init__(self, client, instruments: list):
        self.client = client
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple] = {}
        self._instruments: dict[tuple, dict] = {
            (inst["symbol"], inst["exchange"]): inst for inst in instruments
        }
        self._subscribed = False
        self._stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        # Consecutive watchdog cycles a given (symbol, exchange) has been
        # found stale IN A ROW -- see _watchdog_loop's escalation policy.
        self._stale_streak: dict[tuple, int] = {}

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

    def add_instruments(self, instruments: list):
        """Called from _enter_leg once an option leg's symbol is known --
        adds it to the WS subscription so its LTP is actually cached
        (previously this strategy only ever WS-subscribed the two
        underlying indices, never the option legs themselves)."""
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
                    removed.append(inst)
        if not removed:
            return
        try:
            self.client.unsubscribe_ltp(removed)
            Log.info(f"[PriceStream] unsubscribed: {removed}")
        except Exception as exc:
            Log.warning(f"[PriceStream] unsubscribe failed for {removed}: {exc}")

    def _connect_and_subscribe(self):
        self.client.connect()
        with self._lock:
            all_instruments = list(self._instruments.values())
        if all_instruments:
            self.client.subscribe_ltp(all_instruments, on_data_received=self._on_tick)
        self._subscribed = True
        Log.info(f"[PriceStream] connected and subscribed: {all_instruments}")

    def _teardown(self):
        try:
            with self._lock:
                all_instruments = list(self._instruments.values())
            if self._subscribed and all_instruments:
                self.client.unsubscribe_ltp(all_instruments)
        except Exception as exc:
            Log.warning(f"[PriceStream] unsubscribe_ltp failed during teardown: {exc}")
        try:
            self.client.disconnect()
        except Exception as exc:
            Log.warning(f"[PriceStream] disconnect failed during teardown: {exc}")
        self._subscribed = False

    def _watchdog_loop(self):
        backoffs = (1, 2, 5, 10, 30)
        failures = 0
        try:
            self._connect_and_subscribe()
        except Exception as exc:
            Log.warning(f"[PriceStream] initial connect failed: {exc}")

        while not self._stop.is_set():
            self._stop.wait(config.ws_watchdog_interval)
            if self._stop.is_set():
                break
            if not _within_market_hours():
                continue

            # If the connection itself is down, a per-symbol resubscribe is
            # not possible (subscribe_ltp requires an authenticated session)
            # -- fall back to a full reconnect. This also covers the very
            # first connect attempt having failed at startup.
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
                    self._connect_and_subscribe()
                except Exception as exc:
                    Log.warning(f"[PriceStream] reconnect failed: {exc}")
                self._stop.wait(wait)
                continue

            # Connection is alive -- check EVERY subscribed instrument
            # individually against the cache (not an aggregate "all stale"
            # check: a single symbol that never got a tick, e.g. a bad token
            # mapping for just that instrument, must not hide behind another
            # symbol that's ticking fine) and resubscribe ONLY the stale
            # ones. unsubscribe_ltp/subscribe_ltp operate purely on the
            # given instrument list -- they don't touch the connection or
            # any other symbol's active subscription (confirmed against the
            # installed SDK's feed.py) -- so a healthy symbol's feed is
            # never disturbed by fixing a different, broken one.
            now = datetime.now(IST)
            with self._lock:
                tracked = list(self._instruments.items())
            stale_instruments = []
            for key, inst in tracked:
                entry = self._cache.get(key)
                if entry is None or (now - entry[1]).total_seconds() > config.ws_stale_seconds:
                    stale_instruments.append(inst)

            all_keys = {key for key, _ in tracked}
            stale_keys = {(i["symbol"], i["exchange"]) for i in stale_instruments}
            for key in all_keys:
                self._stale_streak[key] = self._stale_streak.get(key, 0) + 1 if key in stale_keys else 0

            if not stale_instruments:
                failures = 0
                continue

            failures += 1
            wait = backoffs[min(failures - 1, len(backoffs) - 1)]
            names = ", ".join(f"{i['symbol']}.{i['exchange']}" for i in stale_instruments)

            # A symbol stuck stale for several consecutive cycles despite
            # repeated per-symbol resubscribes means whatever's wrong isn't
            # fixable by resubscribing on the SAME connection -- escalate to
            # a full reconnect instead of retrying the same narrow fix
            # forever. This briefly disrupts every OTHER instrument on this
            # connection too (unavoidable -- the whole connection is being
            # torn down), but only for this one process; other strategy
            # processes' own connections are unaffected.
            if max(self._stale_streak[k] for k in stale_keys) >= config.ws_stale_reconnect_after:
                Log.warning(
                    f"[PriceStream] {names} stale for {config.ws_stale_reconnect_after}+ "
                    f"consecutive cycles despite per-symbol resubscribe -- escalating to a "
                    f"full reconnect."
                )
                self._teardown()
                try:
                    self._connect_and_subscribe()
                except Exception as exc:
                    Log.warning(f"[PriceStream] full reconnect (escalation) failed: {exc}")
                for key in all_keys:
                    self._stale_streak[key] = 0
                self._stop.wait(wait)
                continue

            Log.warning(
                f"[PriceStream] stale/missing ticks for: {names} "
                f"(attempt {failures}) -- resubscribing just this/these symbol(s), "
                f"then waiting {wait}s."
            )
            try:
                self.client.unsubscribe_ltp(stale_instruments)
            except Exception as exc:
                Log.warning(f"[PriceStream] unsubscribe (stale symbols) failed: {exc}")
            try:
                self.client.subscribe_ltp(stale_instruments, on_data_received=self._on_tick)
            except Exception as exc:
                Log.warning(f"[PriceStream] resubscribe (stale symbols) failed: {exc}")
            self._stop.wait(wait)

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
        self.state.last_execution_id = data.get("last_execution_id", 0)
        legs_data = data.get("legs", {})
        for key in LEG_KEYS:
            leg_raw = legs_data.get(key, {})
            leg = LegState()
            leg.trade_count = leg_raw.get("trade_count", 0)
            pos_raw = leg_raw.get("position", {})
            leg.position = LegPosition(**{**asdict(LegPosition()), **pos_raw})
            self.state.legs[key] = leg
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_execution_id": self.state.last_execution_id,
            "legs": {
                key: {
                    "trade_count": leg.trade_count,
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
    """'10-JUL-25' -> '10JUL25'."""
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def resolve_current_week_expiry(client, inst: InstrumentConfig) -> str:
    """Nearest upcoming weekly expiry (DDMMMYY) for one instrument -- EXCEPT
    on the underlying's own expiry day itself, when it rolls to NEXT week's
    expiry instead. A same-day-expiring contract has minimal time value
    left and an extreme gamma/theta cliff in its final hours; next week's
    contract needs more margin to hold but backtested consistently smaller
    worst-trade/max-drawdown across every strategy in this project for a
    modest cost to total return (see ANALYSIS.md)."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    for i, raw in enumerate(dates_raw):
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            if d == today:
                if i + 1 < len(dates_raw):
                    return _compact_expiry(dates_raw[i + 1])
                # Broker's expiry list ends exactly at today with no later
                # date to roll to -- silently falling through to today's
                # (already-expiring) contract is exactly what this whole
                # function exists to avoid. Raise loudly instead of trading
                # it, same as the "expiry lookup failed outright" case above.
                raise RuntimeError(
                    f"{inst.name}: today ({today}) is the nearest expiry and the broker "
                    f"returned no later expiry date to roll to -- refusing to silently "
                    f"trade today's expiring contract."
                )
            return _compact_expiry(raw)
    return _compact_expiry(dates_raw[-1])


def _is_error_response(obj) -> bool:
    """client.history()/quotes() can return an error dict instead of a
    DataFrame/expected shape on a bad broker session -- check explicitly
    rather than letting a downstream .empty/[...] call crash the cycle."""
    return isinstance(obj, dict)


def fetch_symbol_ltp(client, symbol: str, exchange: str) -> Optional[float]:
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() failed for {symbol}: {exc}")
        return None
    if _is_error_response(resp) and resp.get("status") != "success":
        Log.warning(f"quotes() error response for {symbol}: {resp}")
        return None
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    ltp = data.get("ltp") if isinstance(data, dict) else None
    return float(ltp) if ltp is not None else None


def fetch_ltp(client, inst: InstrumentConfig) -> Optional[float]:
    return fetch_symbol_ltp(client, inst.name, inst.underlying_exchange)


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
    r1: float
    s1: float
    last_close: float
    last_high: float
    last_low: float
    supertrend: float
    ltp: float
    candle_key: str


# Log the [inst] candle=... line only when the candle actually advances, not
# on every indicator_refresh_interval refetch -- the same closed 5m candle
# stays the "last completed" one for up to 5 minutes, so re-logging it every
# ~60s (up to 5x per candle per instrument) was pure noise with no new
# information.
_last_logged_candle: dict[str, str] = {}


def fetch_daily_pivot(client, inst: InstrumentConfig) -> Optional[tuple]:
    """Fetch the previous COMPLETED day's daily OHLC and compute pivot
    R1/S1. This only changes once per trading day, so it's cached and
    refreshed on its own slow cadence (`config.daily_refresh_interval`),
    separate from the 5m Supertrend signal below -- no reason to re-pull
    30 days of daily bars on the same fast cadence used for the intraday
    crossover check."""
    end = datetime.now(IST).date()
    daily = client.history(
        symbol=inst.name, exchange=inst.underlying_exchange,
        interval=config.daily_interval,
        start_date=(end - timedelta(days=30)).isoformat(), end_date=end.isoformat(),
    )
    if _is_error_response(daily):
        Log.warning(f"[{inst.name}] daily history error response: {daily}")
        return None
    if daily is None or daily.empty:
        Log.warning(f"[{inst.name}] empty daily history.")
        return None
    # Drop today's still-forming daily bar -- pivots must be computed off the
    # previous COMPLETED day's H/L/C, not today's in-progress one.
    if len(daily) >= 2:
        daily = daily.iloc[:-1]
    prev_day = daily.iloc[-1]
    pivot, r1, s1, r2, s2, r3, s3 = ta.pivot_points(
        np.array([prev_day["high"]]), np.array([prev_day["low"]]), np.array([prev_day["close"]])
    )
    return float(np.asarray(r1)[0]), float(np.asarray(s1)[0])


def compute_instrument_signal(client, inst: InstrumentConfig, r1: float, s1: float,
                               ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch 5m history for one instrument and compute Supertrend(7,3) off
    the last genuinely CLOSED bar, plus live LTP. `r1`/`s1` come from the
    separately-cached `fetch_daily_pivot()` (see StrategyEngine.get_signal)
    -- this function no longer re-fetches daily history itself. Returns
    None (with a logged reason) if anything is unavailable -- callers must
    treat that as "no signal this cycle", not an error.

    `ltp`: pass the WebSocket-cached LTP for this instrument (see
    PriceStream / run_cycle) to avoid an extra single-symbol quotes() call
    here -- only falls back to fetching it directly if not provided."""
    end = datetime.now(IST).date()

    intraday = client.history(
        symbol=inst.name, exchange=inst.underlying_exchange,
        interval=config.intraday_interval,
        start_date=(end - timedelta(days=config.history_lookback_days)).isoformat(),
        end_date=end.isoformat(),
    )
    if _is_error_response(intraday):
        Log.warning(f"[{inst.name}] {config.intraday_interval} history error response: {intraday}")
        return None
    if intraday is None or intraday.empty:
        Log.warning(f"[{inst.name}] empty {config.intraday_interval} history.")
        return None
    # Drop the still-forming last candle -- observed in production (weekly
    # strategy) that the broker's last bar keeps updating well past its
    # nominal close; never trade on an unsettled bar.
    if len(intraday) >= 2:
        intraday = intraday.iloc[:-1]
    if len(intraday) < config.supertrend_period + 1:
        Log.warning(
            f"[{inst.name}] only {len(intraday)} {config.intraday_interval} bars after dropping "
            f"the still-forming one (need >= {config.supertrend_period + 1}) -- no signal."
        )
        return None

    line, _direction = ta.supertrend(
        intraday["high"], intraday["low"], intraday["close"],
        period=config.supertrend_period, multiplier=config.supertrend_multiplier,
    )
    last_st = float(np.asarray(line)[-1])
    last_close = float(intraday["close"].iloc[-1])
    last_high = float(intraday["high"].iloc[-1])
    last_low = float(intraday["low"].iloc[-1])
    candle_key = str(intraday.index[-1])

    if ltp is None:
        ltp = fetch_ltp(client, inst)
    if ltp is None:
        ltp = last_close  # fallback: closed-candle close is a reasonable proxy if quotes() is down

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] candle={candle_key} close={last_close:.2f} high={last_high:.2f} "
            f"low={last_low:.2f} r1={r1:.2f} s1={s1:.2f} supertrend={last_st:.2f} ltp={ltp:.2f}"
        )
    return InstrumentSignal(r1=r1, s1=s1, last_close=last_close, last_high=last_high, last_low=last_low,
                             supertrend=last_st, ltp=ltp, candle_key=candle_key)


def fetch_chain(client, inst: InstrumentConfig, expiry: str):
    resp = client.optionchain(
        # strike_count=1 (ATM +/- 1) is enough -- pick_atm_leg only ever uses the
        # ATM strike itself. A wider chain just means more option quotes for the
        # backend to fan out to the broker (Shoonya's multiquotes isn't a true
        # batch call -- broker/shoonya/api/data.py fans out one GetQuotes per
        # symbol via a ThreadPoolExecutor), which was adding real seconds of
        # latency to the entry path for strikes that are never used.
        underlying=inst.name, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=1,
    )
    if resp.get("status") != "success":
        raise RuntimeError(f"optionchain failed for {inst.name}: {resp}")
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


def pick_atm_leg(chain: dict, option_type: str, spot: float) -> dict:
    legs = _legs_with_strike(chain, option_type)
    if not legs:
        raise RuntimeError(f"No {option_type} legs found in chain (spot={spot})")
    return min(legs, key=lambda l: abs(l["strike"] - spot))


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
    """Places an order. Only retries a CLEAN rejection response (the broker
    explicitly returned a non-"success" status -- nothing was placed, safe
    to retry) up to config.place_order_max_attempts times. Deliberately does
    NOT retry a raised exception (network timeout, connection reset, etc.)
    -- that case is ambiguous: the request may have already reached the
    broker and succeeded even though the client never got the response, so
    retrying could place a genuine DUPLICATE order. No idempotency key
    exists anywhere in this stack (confirmed against the openalgo SDK's
    placeorder() signature and services/place_order_service.py), so
    surfacing the exception immediately -- exactly once, no retry -- is the
    only safe choice. A genuine persistent rejection still raises after the
    last retry, and is handled by the caller (_enter_leg/_exit_leg clear the
    stale order id so the NEXT scheduled cycle tries again with a fresh
    order, rather than looping forever on a dead order id -- see the module
    docstring's "Order placement robustness" section)."""
    import time as _time

    last_exc: Optional[Exception] = None
    for attempt in range(1, config.place_order_max_attempts + 1):
        try:
            resp = client.placeorder(
                strategy=strategy, symbol=symbol, exchange=exchange, action=action,
                product=config.product, price_type=config.price_type,
                quantity=str(quantity), price="0", trigger_price="0", disclosed_quantity="0",
            )
        except Exception as exc:
            Log.warning(f"placeorder raised for {symbol} {action} (not retried -- "
                        f"outcome ambiguous, could duplicate a real order): {exc}")
            raise
        if resp.get("status") == "success":
            return resp["orderid"]
        last_exc = RuntimeError(f"placeorder failed for {symbol} {action}: {resp}")
        Log.warning(f"placeorder attempt {attempt}/{config.place_order_max_attempts} "
                    f"rejected for {symbol} {action}: {resp}")
        if attempt < config.place_order_max_attempts:
            _time.sleep(config.place_order_retry_delay)
    raise last_exc


###############################################################################
# TRADE LOG (background thread -- see append_trade_log)
###############################################################################
# A single shared background thread + queue (module-level singleton, never
# spawned per call -- per this project's FD-hygiene conventions) does the
# actual CSV file I/O. append_trade_log() just enqueues and returns
# immediately, so a disk write can never add latency to the main scheduler
# loop's entry/exit condition checks -- the thing that actually needs to
# stay fast. state.json (the resumability source of truth) is saved
# synchronously as before and is unaffected by any of this.
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
            if item is None:  # sentinel, not currently used but keeps the loop stoppable
                break
            (strategy_tag, leg_key, symbol, quantity,
             entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id) = item
            log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
            is_new = not log_path.exists()
            pnl_points = entry_px - exit_px  # short option: sell high, buy back low = profit
            pnl_rupees = pnl_points * quantity
            with log_path.open("a", newline="") as fp:
                writer = csv.writer(fp)
                if is_new:
                    writer.writerow(_TRADE_LOG_HEADER)
                writer.writerow([leg_key, symbol, quantity, entry_time, round(entry_px, 2),
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
    """Enqueue one row per CLOSED leg for the background writer thread to
    append to a durable CSV trade log, separate from state.json (which only
    tracks currently-open positions and gets overwritten back to blank once
    a leg closes -- see _exit_leg). One file per strategy instance
    (STRATEGY_ID-scoped, same as the state file). Same column shape as the
    backtest's trade CSV for consistency. entry_px/exit_px are LTP-based
    estimates (same basis _enter_leg already uses for entry_px), not
    confirmed broker fill prices -- there is no average-fill-price field
    parsed from orderstatus() today."""
    _ensure_trade_log_thread()
    _trade_log_queue.put((strategy_tag, leg_key, symbol, quantity,
                          entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id))


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal stdlib-only HTTP-over-Unix-domain-socket client. Needed
    because gunicorn can be bound via `--bind unix:/path/to/openalgo.sock`
    (common in multi-instance deployments, confirmed in production on this
    project) instead of a TCP port -- in that case there is NO TCP listener
    on 127.0.0.1 at all, so a plain urllib request gets "Connection
    refused" no matter what port is guessed."""

    def __init__(self, socket_path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _post_json_local(env: "Environment", path: str, payload: bytes, timeout: float = 3.0):
    """POST to the local OpenAlgo instance, trying in order: (1) a Unix
    domain socket at <repo_root>/openalgo.sock -- gunicorn's bind target in
    this project's multi-instance/socket-based deployments; (2) plain TCP
    loopback on FLASK_PORT -- default single-instance deployments; (3)
    env.host (HOST_SERVER/OPENALGO_HOST, i.e. the public domain) as a last
    resort only, since routing this internal call through a reverse
    proxy/WAF is what caused the original 403 in production. Raises if
    every transport fails; caller logs and swallows."""
    headers = {"Content-Type": "application/json"}

    socket_path = Path(__file__).resolve().parents[2] / "openalgo.sock"
    if socket_path.exists():
        conn = _UnixHTTPConnection(str(socket_path), timeout=timeout)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            conn.getresponse().read()
            return
        finally:
            conn.close()

    last_exc: Optional[Exception] = None
    for base in (f"http://127.0.0.1:{os.getenv('FLASK_PORT', '5000')}", env.host.rstrip("/")):
        try:
            req = urllib.request.Request(f"{base}{path}", data=payload, method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=timeout).close()
            return
        except Exception as exc:
            last_exc = exc
    raise last_exc


def report_pnl_to_platform(env: "Environment", realized_pnl: float, open_positions: list):
    """Push a PnL snapshot to the OpenAlgo Python Strategy Host so the UI's
    PNL button can show live PnL without the platform having to poll/parse
    this process's logs. Fire-and-forget: stdlib-only (no new dependency),
    short timeout, any failure is logged and swallowed -- must never block
    or crash the main scheduler loop over a reporting hiccup.

    `open_positions`: list of dicts, each {leg_key, symbol, direction
    ("SHORT"/"LONG"), quantity, entry_price, current_price, pnl} -- pnl per
    leg already signed correctly by the caller (short vs. long), so this
    function just sums them for unrealized_pnl rather than re-deriving sign."""
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
    """GET counterpart to _post_json_local -- same Unix-socket -> TCP loopback
    -> env.host fallback chain, for check_pending_action's pull. Raises if
    every transport fails; caller logs and treats it as "no action pending"
    rather than crashing the scheduler loop over a reporting hiccup."""
    socket_path = Path(__file__).resolve().parents[2] / "openalgo.sock"
    if socket_path.exists():
        conn = _UnixHTTPConnection(str(socket_path), timeout=timeout)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return json.loads(resp.read())
        finally:
            conn.close()

    last_exc: Optional[Exception] = None
    for base in (f"http://127.0.0.1:{os.getenv('FLASK_PORT', '5000')}", env.host.rstrip("/")):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last_exc = exc
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
        self._daily_pivot_cache: dict[str, tuple] = {}
        self._last_daily_refresh: dict[str, datetime] = {}
        self._ws_fallback_logged: dict[str, bool] = {}
        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds (a stale/stuck order re-pricing against a moving market) -- doing that
        # INSIDE run_cycle would stall every other instrument/leg's entry/exit check for
        # the same duration. Order confirmation now happens on a per-leg background task
        # instead (see _watch_entry_fill/_watch_exit_fill), submitted to one shared,
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
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
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
        # Weekly expiry only rolls at week boundaries, not intraday -- resolving it
        # fresh via client.expiry() on every single entry added a full REST
        # round-trip to the entry critical path for no reason. Cached per
        # instrument, cleared in _reset_day_if_needed alongside other daily state.
        self._expiry_cache: dict[str, str] = {}
        # Background ATM-chain cache: {inst.name: chain_dict}, refreshed on the same
        # cadence as the indicator signal (see get_signal below) -- NOT fetched at
        # the moment a signal fires. This decouples the optionchain() REST
        # round-trip from the entry critical path entirely, so _enter_leg only
        # has to place the order once a signal condition is true.
        self._chain_cache: dict[str, dict] = {}
        # Guards get_signal so it submits at most one in-flight
        # daily-pivot/indicator/chain refresh per instrument at a time --
        # without this, a refresh that takes longer than
        # indicator_refresh_interval to complete would get re-submitted every
        # subsequent cycle, piling up duplicate background REST calls against
        # the same instrument.
        self._signal_refresh_pending: set[str] = set()

    def _save_state(self):
        """Every self.store.save() call in this engine goes through here --
        StateStore.save() writes the entire state to one shared JSON file, so
        concurrent writers (run_cycle vs. a background fill-watch task) could
        otherwise corrupt it or drop an update."""
        with self._state_lock:
            self.store.save()

    def _refresh_chain_cache(self, inst: InstrumentConfig):
        try:
            expiry = self._expiry_cache.get(inst.name)
            if expiry is None:
                expiry = resolve_current_week_expiry(self.client, inst)
                self._expiry_cache[inst.name] = expiry
            self._chain_cache[inst.name] = fetch_chain(self.client, inst, expiry)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background chain refresh failed (will retry live at "
                        f"entry if needed): {exc}")

    def _refresh_pivot_signal_chain_bg(self, inst: InstrumentConfig, ltp: Optional[float],
                                         refresh_chain: bool, refresh_daily: bool):
        """Runs on _fill_executor -- this is what makes get_signal's periodic
        refresh genuinely non-blocking. fetch_daily_pivot(), compute_instrument_signal()
        and fetch_chain() (via _refresh_chain_cache) all make real REST calls
        (client.history()/client.optionchain()); previously these were called
        inline from get_signal(), which is itself called inline from
        run_cycle(), so a slow/stuck broker response stalled the whole
        scheduler cycle for every leg. Now run_cycle only ever reads
        self._daily_pivot_cache / self._signal_cache / self._chain_cache --
        never waits on the network call itself."""
        try:
            if refresh_daily:
                fresh_daily = fetch_daily_pivot(self.client, inst)
                if fresh_daily is not None:
                    self._daily_pivot_cache[inst.name] = fresh_daily
                    self._last_daily_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background daily-pivot refresh failed (will retry "
                        f"next cycle): {exc}")

        daily = self._daily_pivot_cache.get(inst.name)
        if daily is None:
            self._signal_refresh_pending.discard(inst.name)
            return
        r1, s1 = daily

        try:
            fresh = compute_instrument_signal(self.client, inst, r1, s1, ltp=ltp)
            if fresh is not None:
                self._signal_cache[inst.name] = fresh
                self._last_indicator_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background indicator refresh failed (will retry "
                        f"next cycle): {exc}")
        if refresh_chain:
            self._refresh_chain_cache(inst)
        self._signal_refresh_pending.discard(inst.name)

    def get_signal(self, inst: InstrumentConfig, ltp: Optional[float] = None,
                    refresh_chain: bool = False) -> Optional[InstrumentSignal]:
        """Cached, throttled indicator fetch + fresh LTP every call. The
        daily pivot (R1/S1) only changes once per trading day, so it's
        refreshed on its own slow cadence (`config.daily_refresh_interval`,
        default 600s) -- no reason to re-pull 30 days of daily bars on the
        same fast cadence as the 5m Supertrend signal. The 5m Supertrend
        signal changes every candle, so it's refreshed at most once per
        `config.indicator_refresh_interval` (default 15s), close enough to
        the underlying candle's own close that entry/exit detection isn't
        meaningfully delayed by this cache. LTP, on the other hand, is
        refreshed on EVERY call, since the crossover condition is checked
        against the live tick, not the closed candle -- this is what makes
        entry/exit checking "continuous" without hammering client.history().

        `ltp`: the WebSocket-cached value for this instrument (see
        PriceStream / run_cycle) -- avoids a separate quotes() call per
        instrument per cycle. Only falls back to a direct fetch_ltp() call
        if the WS cache didn't have a fresh tick."""
        now = datetime.now(IST)

        last_daily = self._last_daily_refresh.get(inst.name)
        due_daily = (last_daily is None
                     or (now - last_daily).total_seconds() >= config.daily_refresh_interval)
        last = self._last_indicator_refresh.get(inst.name)
        due_signal = (last is None
                      or (now - last).total_seconds() >= config.indicator_refresh_interval)

        if (due_daily or due_signal) and inst.name not in self._signal_refresh_pending:
            self._signal_refresh_pending.add(inst.name)
            self._fill_executor.submit(
                self._refresh_pivot_signal_chain_bg, inst, ltp, refresh_chain, due_daily
            )

        daily = self._daily_pivot_cache.get(inst.name)
        if daily is None:
            return None
        r1, s1 = daily

        cached = self._signal_cache.get(inst.name)
        if cached is None:
            return None

        # Daily pivot may have refreshed independently of the intraday
        # signal cache above -- keep the cached signal's r1/s1 current.
        cached.r1 = r1
        cached.s1 = s1

        if ltp is None:
            ltp = fetch_ltp(self.ltp_client, inst)
        if ltp is not None:
            cached.ltp = ltp
        return cached

    # ---- state helpers -----------------------------------------------------
    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.store.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily trade counters.")
            self.store.state.current_day = today_key
            self.store.state.today_realized_pnl = 0.0
            self._expiry_cache.clear()
            self._chain_cache.clear()
            for leg in self.store.state.legs.values():
                leg.trade_count = 0
            self._save_state()

    def _within_entry_window(self) -> bool:
        if config.test_mode:
            return True
        now = datetime.now(IST).time()
        return config.entry_start <= now <= config.entry_end

    def _within_market_hours(self) -> bool:
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
                pnl = (pos.entry_px - current_px) * pos.quantity  # short leg
                open_positions.append({
                    "leg_key": leg_key, "symbol": pos.symbol, "direction": "SHORT",
                    "quantity": pos.quantity, "entry_price": pos.entry_px,
                    "current_price": current_px, "pnl": pnl,
                    "entry_time": pos.entry_time, "execution_id": pos.execution_id,
                })
            report_pnl_to_platform(self.env, self.store.state.today_realized_pnl, open_positions)
        except Exception as exc:
            Log.exception(f"report_pnl_tick failed: {exc}")

    # ---- entry / exit (single naked leg, resumable) -------------------------
    def _enter_leg(self, leg_key: str, inst: InstrumentConfig, option_type: str, spot: float):
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
            atm_leg = pick_atm_leg(chain, option_type, spot)
            # pick_atm_leg only ever picks the nearest of the 3 cached
            # strikes (ATM+/-1) -- if the cache lagged (a slow/failed
            # background refresh) or spot gapped since it was fetched, the
            # TRUE current ATM could have drifted entirely outside that
            # window, and this would silently return a strike that isn't
            # actually ATM with no warning. Derive the chain's own strike
            # step from its cached rows and bail out (same "not populated
            # yet" skip-and-retry path used above) if the nearest cached
            # strike is more than one step away from spot, forcing a fresh
            # background refresh before trading this leg.
            strikes = sorted({leg["strike"] for leg in _legs_with_strike(chain, option_type)})
            if len(strikes) >= 2:
                strike_step = min(b - a for a, b in zip(strikes, strikes[1:]))
                if abs(atm_leg["strike"] - spot) > strike_step:
                    Log.warning(f"[{leg_key}] Cached option chain looks stale -- nearest strike "
                                f"{atm_leg['strike']} is more than one strike step ({strike_step}) "
                                f"from spot {spot}. Skipping this cycle and forcing a chain refresh.")
                    self._chain_cache.pop(inst.name, None)
                    return
            quantity = config.lot_multiplier * atm_leg["lotsize"]

            Log.info(f"[{leg_key}] Entry: strike={atm_leg['strike']} symbol={atm_leg['symbol']}@{atm_leg['ltp']} qty={quantity}")

            pos = LegPosition(
                symbol=atm_leg["symbol"],
                quantity=quantity,
                entry_time=datetime.now(IST).isoformat(),
                entry_px=float(atm_leg["ltp"]),
                execution_id=self.execution_id,
            )
            leg.position = pos
            self._save_state()
            # Start streaming this option leg's own live LTP over WebSocket
            # instead of only ever REST-polling it -- without this, the leg's
            # price was never actually WS-cached, so report_pnl_tick's
            # cache-only lookup could never see this position as open.
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
                                            inst.options_exchange, "SELL", pos.quantity)
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
                      "SELL", quantity)
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
        action = "SELL" if error_state == "entry_failed" else "BUY"
        push_leg_error(self.env, leg_key, pos, action=action)

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
            # here is the more dangerous direction: it would leave a naked
            # short open indefinitely with no error_state/UI alert at all.
            try:
                pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                           inst.options_exchange, "BUY", pos.quantity)
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
                      "BUY", quantity)
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

        # Trade log (separate from state.json, which resets leg.position to
        # blank right below and would otherwise lose this trade's record
        # entirely). exit_px is an LTP-based estimate, same basis entry_px
        # already uses -- best-effort only, doesn't block the exit if quotes
        # are briefly unavailable. A "Manually Completed" exit resolution
        # sets manual_exit_px -- the user's real fill price is authoritative
        # there, not a fresh quote (see
        # docs/prd/python-strategies-order-error-recovery.md). Otherwise
        # WS-cache-first, REST fallback -- same pattern used everywhere else
        # price is read, instead of an unconditional REST round-trip here.
        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(pos.symbol, inst.options_exchange,
                                                 max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange)
        if exit_px is not None:
            # Short leg: sell high (entry) -> buy back low (exit) = profit.
            # Same formula the async trade-log writer uses -- kept in sync
            # here so today_realized_pnl (pushed to the platform via
            # report_pnl_to_platform) reflects each closed leg immediately,
            # without waiting on the background CSV writer thread.
            self.store.state.today_realized_pnl += (pos.entry_px - exit_px) * pos.quantity
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
                push_leg_error(self.env, leg_key, pos, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            if kind == "terminal":
                # Nothing resting -- no broker call needed, straight to flat.
                self.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
                )
                leg.position = LegPosition()
                self._save_state()
                push_leg_error(self.env, leg_key, leg.position, clear=True)
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
            push_leg_error(self.env, leg_key, pos, clear=True)
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
                push_leg_error(self.env, leg_key, pos, clear=True)
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
                                             inst.options_exchange, "SELL", pos.quantity)
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
                                             symbol, inst.options_exchange, "SELL", quantity)
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
                        pending = check_pending_action(self.env, leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- all positions flat. Stopping.")
                    ack_force_exit_complete(self.env)
                    self._force_exit_pending = False
                return

            if not self._within_market_hours():
                return

            past_universal_exit = self._past_universal_exit()

            # Universal exit: force-close every open leg unconditionally,
            # regardless of that leg's own Supertrend-based exit condition.
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
                        pending = check_pending_action(self.env, leg_key)
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
                # Live underlying LTP from the WebSocket cache (pushed, not
                # polled) -- falls back to a single one-off REST quotes()
                # call for just this instrument if the feed is stale/missing
                # (e.g. still reconnecting), so a WS hiccup degrades
                # gracefully instead of stalling the signal. Only the
                # underlying's own LTP is needed live -- this strategy's
                # signal reads the underlying directly, not the option's own
                # premium (see module docstring).
                inst_ltp = self.price_stream.get_ltp(
                    inst.name, inst.underlying_exchange, max_age=config.ws_stale_seconds
                )
                if inst_ltp is None:
                    if not self._ws_fallback_logged.get(inst.name):
                        Log.warning(f"[{inst.name}] WS LTP stale/missing -- falling back to REST quotes().")
                        self._ws_fallback_logged[inst.name] = True
                    inst_ltp = fetch_ltp(self.ltp_client, inst)
                else:
                    self._ws_fallback_logged[inst.name] = False
                # A new leg can only ever be entered within the entry window, and
                # only on whichever side (PE/CE) is still flat -- gates the
                # background chain refresh so it doesn't run all day regardless.
                still_enterable = within_entry and any(
                    not self.store.state.legs[f"{inst.name}_{ot}"].position.symbol
                    for ot in ("PE", "CE")
                )
                signal = self.get_signal(inst, ltp=inst_ltp, refresh_chain=still_enterable)
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
                        pending = check_pending_action(self.env, leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        continue

                    has_position = bool(leg.position.symbol)

                    if option_type == "PE":
                        # Entry: closed 5m candle confirms bullish (pivot R1
                        # break + Supertrend below that candle's own close),
                        # AND live LTP confirms the breakout is still live by
                        # trading above that same candle's High.
                        entry_condition = (
                            signal.last_close > signal.r1
                            and signal.supertrend < signal.last_close
                            and signal.ltp > signal.last_high
                        )
                        exit_condition = signal.supertrend > signal.ltp
                    else:
                        # Entry: closed 5m candle confirms bearish (pivot S1
                        # break + Supertrend above that candle's own close),
                        # AND live LTP confirms the breakdown is still live by
                        # trading below that same candle's Low.
                        entry_condition = (
                            signal.s1 > signal.last_close
                            and signal.supertrend > signal.last_close
                            and signal.ltp < signal.last_low
                        )
                        exit_condition = signal.ltp > signal.supertrend

                    # No candle-key dedup here, by design: entry/exit conditions
                    # are checked EVERY cycle (near-continuous, per
                    # scheduler_interval) against the live LTP, not once per
                    # candle. Marking a candle as "seen" the instant it's
                    # observed -- even if the condition was false at that
                    # exact moment -- would permanently block re-checking it
                    # later in the same throttle window as LTP keeps moving,
                    # the same bug found and fixed on the weekly strategy.
                    # has_position gating below already prevents duplicate
                    # entries once a leg is open, so no separate dedup is
                    # needed for correctness.
                    if has_position:
                        # Once an exit order has been placed (or its fill confirmed by
                        # the background watcher), it must be driven through to
                        # _finalize_exit regardless of what exit_condition does on a
                        # LATER cycle (e.g. Supertrend flips back before the async
                        # fill lands) -- otherwise a filled-but-not-yet-finalized
                        # position would never get its trade-log row written or its
                        # leg cleared, permanently blocking re-entry.
                        exit_already_committed = bool(leg.position.exit_order_id) or leg.position.exit_filled
                        if exit_condition or exit_already_committed:
                            if exit_condition and not exit_already_committed:
                                Log.info(f"[{leg_key}] Exit condition met -> closing.")
                            self._exit_leg(leg_key, inst, reason="supertrend_flip")
                        continue

                    if not within_entry:
                        continue
                    if leg.trade_count >= config.max_trades_per_leg_per_day:
                        continue
                    if not entry_condition:
                        continue

                    self._enter_leg(leg_key, inst, option_type, spot=signal.ltp)

        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            # No global RECOVERY mode here (unlike the weekly strategy) --
            # each leg is independently resumable via its own persisted
            # order ids, so a failure in one instrument/leg this cycle
            # doesn't need to halt the others; the next scheduled cycle
            # simply retries whatever didn't complete.


###############################################################################
# STARTUP
###############################################################################
def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version              : {config.version}")
    print(f"Instruments          : {', '.join(i.name for i in INSTRUMENTS)}")
    print(f"Supertrend           : ({config.supertrend_period}, {config.supertrend_multiplier})")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print("⚠️  NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK ⚠️")
    if config.test_mode:
        print("⚠️  TEST MODE ENABLED -- market-hours/entry-window checks are BYPASSED")
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

    ws_instruments = [
        {"exchange": inst.underlying_exchange, "symbol": inst.name} for inst in INSTRUMENTS
    ]
    price_stream = PriceStream(client, ws_instruments)
    price_stream.start()

    # Mid-day restart: any option leg already open before this process
    # started needs its symbol subscribed immediately, not just on the next
    # _enter_leg (which only runs for a FRESH entry, never for a leg that's
    # already open) -- otherwise a resumed position's LTP would never be
    # WS-cached and report_pnl_tick would never see it (same resumability
    # guarantee as MCX/VWAP_NoHA/Batman).
    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        for leg_key, leg in state_store.state.legs.items():
            if leg.position.symbol:
                inst_name = leg_key.split("_")[0]
                inst = next((i for i in INSTRUMENTS if i.name == inst_name), None)
                if inst is not None:
                    already_known.append({
                        "symbol": leg.position.symbol,
                        "exchange": inst.options_exchange,
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
    print(f"Price Stream       : starting ({ws_instruments} + {len(already_known)} option leg(s) already known today)")
    print("=" * 70)

    engine = StrategyEngine(client, state_store, env, price_stream, execution_id=execution_id,
                             ltp_client=ltp_client)

    # Restart while a leg was in error mode: STRATEGY_ERRORS on the platform
    # side is in-memory only, so re-push any already-erroring leg once here --
    # otherwise the UI badge would silently vanish across a restart even
    # though the leg is still frozen awaiting a decision. See
    # docs/prd/python-strategies-order-error-recovery.md.
    for leg_key, leg in state_store.state.legs.items():
        if leg.position.error_state:
            action = "SELL" if leg.position.error_state == "entry_failed" else "BUY"
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
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
