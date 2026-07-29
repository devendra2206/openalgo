"""
===============================================================================
Nifty & Sensex EMA34 + RSI Intraday Option Seller
===============================================================================
Version     : 1.2.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Ported from : Tradetron export "Nifty_SENSEX_EMA34_RSI_PE_CE_Short.json"
              -- see Nifty_Sensex_EMA34_RSI_Intraday_ANALYSIS.md for the full
              decoded-logic writeup this script was built from.

Description
-----------
Pure INTRADAY option-selling strategy across two underlyings (NIFTY/NFO and
SENSEX/BFO), four independent legs (NIFTY PE, NIFTY CE, SENSEX PE, SENSEX
CE), same family as the two intraday strategies built earlier this project
(`Nifty_Sensex_Pivot_Supertrend_Intraday_1`, `Nifty_Sensex_VWAP_Intraday_1`).

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
Faithful, literal port -- the source Tradetron strategy has no hedge leg.

Key structural note vs. the other two strategies in this family
------------------------------------------------------------------------
The signal here is computed off the **underlying's own** price (via
`tt_get_instrument('NFO,NIFTY 50,,,,,')` / `'BFO,SENSEX,,,,,'` in the
source -- no strike/expiry/option-type specified, i.e. NIFTY/SENSEX futures
in Tradetron's engine), same architectural shape as Pivot+Supertrend (NOT
the option's-own-premium approach used by the VWAP strategy). The source
references the FUTURES instrument specifically; this port uses the spot
index (NSE_INDEX/BSE_INDEX) instead, matching the convention already
established for the other two strategies in this family, and because no
historical futures data exists to backtest against either way -- flagged
here as an explicit simplification, not silently assumed.

ATM strike is resolved **fresh on every entry** (like Pivot+Supertrend),
not locked once per day (unlike the VWAP strategy).

Signal Rules (per instrument, shared between its PE and CE leg)
------------------------------------------------------------------
Computed off the underlying's own 3-minute candles (a standard OpenAlgo
interval -- no synthetic bucketing needed, unlike the VWAP strategy's "2m"):
  - EMA(High, 34) and EMA(Low, 34), and RSI(14) on Close -- all computed
    over the last-closed 3m bars (the newest fetched bar is unconditionally
    dropped as still-forming, same defensive pattern used everywhere else).
  - close_prev1/prev2, high_prev2, low_prev2 = the last two CLOSED 3m
    candles' own OHLC.
  - ltp = live LTP (client.quotes()) -- used only as a live
    breakout-confirmation filter alongside the closed-candle signal.

  PE entry : close_prev2 > ema_high34_prev2   (2 candles ago, price closed
                                                above its own 34-EMA of
                                                Highs -- an overextension)
             AND ltp > close_prev1             (still pushing higher live)
             AND close_prev1 > high_prev2       (last candle broke above
                                                the high of the one before)
             AND rsi_prev1 > 53                 (momentum confirms)
  CE entry : close_prev2 < ema_low34_prev2     (mirror, below 34-EMA of Lows)
             AND ltp < close_prev1
             AND close_prev1 < low_prev2
             AND rsi_prev1 < 47

  This is a CONTRARIAN/fade-the-spike entry (sell PE into a premium-side
  breakout in the underlying that looks overextended), the opposite
  philosophy from the VWAP strategy's follow-the-decay entries -- both are
  legitimate, different approaches; this is a faithful port of the source,
  not a redesign.

  PE exit  : close_prev1 < ema_low34_prev1  OR  rsi_prev1 < 47
  CE exit  : close_prev1 > ema_high34_prev1 OR  rsi_prev1 > 53

  Exit is evaluated purely off CLOSED-candle values (no live LTP term in
  the source's exit condition) -- unlike the other two strategies, this
  one's exit can only actually change once per 3-minute bar, even though
  the scheduler still polls every cycle.

  ** No native per-trade stop-loss or price-based exit exists in the
  source.** The only risk containment is the technical EMA/RSI reversal
  above and the added universal exit below (which bounds a bad trade to
  "at most until end of day", not to a fixed rupee/point amount). Flagged
  explicitly -- unlike Pivot+Supertrend (stop-loss added via a backtest
  sweep) and VWAP (native point-based stop), this strategy ships with
  neither. Worth reconsidering after seeing backtest drawdown numbers.

"Universal Exit" was ADDED, not present in source
------------------------------------------------------------------------
The source Tradetron export has no "Universal Exit" condition block for
this strategy (unlike Pivot+Supertrend/VWAP, which both have one) -- it
likely relies on a platform-level auto square-off setting not visible in
the JSON export. `config.universal_exit_time = 15:15` was added here as an
explicit safety net (same time used by the other two strategies), flagged
as a deviation from the literal source, not a silent assumption.

Live price feed: WebSocket, not REST polling (v1.2.0)
------------------------------------------------------------------------
Same rationale and same fixed-instrument-list `PriceStream` design as
Pivot+Supertrend (see that script's module docstring for the full
writeup): Shoonya's `multiquotes()` in this OpenAlgo installation fans out
to one individual `GetQuotes` HTTP call per symbol, concurrently, rather
than being a true broker-side batch endpoint -- with 4 strategy instances
sharing one Shoonya session on unjittered polling intervals, their bursts
can phase-align and spike past Shoonya's real ~10 quotes/sec ceiling.
This strategy needs only the 2 underlyings' LTP (no option-level LTP for
entry or exit, matching Pivot+Supertrend's architecture, NOT VWAP's
option-premium-based one), so `PriceStream` subscribes to NIFTY/SENSEX
once at startup -- a fixed list, exactly like Pivot+Supertrend, not the
dynamically-growing one VWAP needs. `run_cycle()` reads underlying LTP
from the WS cache with a one-off REST `client.quotes()` fallback per
instrument if the cache entry is stale/missing. The background watchdog
checks each instrument individually and resubscribes only a stale one
without disturbing a healthy one's feed; the SDK's own built-in
`auto_reconnect` handles a genuine connection drop on its own.

Order placement robustness (v1.2.0)
------------------------------------------------------------------------
Same fix as Pivot+Supertrend, same underlying bug: a rejected/cancelled
entry or exit order used to leave its order id persisted forever, so
every subsequent cycle re-polled the same dead order and failed
identically -- worst case for `_exit_leg`, where a rejected close order
left the position open at the broker with no further close attempt ever
made. Fixed identically here: `_enter_leg` clears the whole leg position
on a rejected entry (fresh strike/expiry + new order next cycle, doesn't
consume a trade_count slot); `_exit_leg` clears only `exit_order_id` on a
rejected exit (position is still open) so the next cycle places a
brand-new close order. A repeated failure is surfaced via the error-mode/
Retry-Cancel-Manual system (see `_enter_error_mode`), not a local counter.
`place()` retries up to 3
attempts (1.5s apart) before raising. A `TimeoutError` from `poll_fill`
now means it already tried RE-PRICING the stale order (via
`modifyorder()`, to the current LTP) up to `config.reprice_max_attempts`
times, then cancelled it -- so it's treated the same as a rejection
(clear and retry fresh). See `poll_fill`'s own docstring for the full
mechanism.

Persistent trade log (state.json intentionally left minimal)
------------------------------------------------------------------
Same pattern as the other two strategies: `state.json` only tracks each
leg's CURRENTLY open position. A single module-level background thread +
queue (`append_trade_log()`) writes one row per closed leg to
`trades_{STRATEGY_ID}.csv`, so a disk write never sits on the scheduler's
critical path. `entry_px`/`exit_px` are captured purely for the trade log's
PnL columns (the exit DECISION itself never looks at price), same
LTP-estimate basis as the other two strategies.

Design notes carried over from the other two strategies in this family
------------------------------------------------------------------------
  - Resumable, leg-by-leg entry/exit via persisted order IDs.
  - State file unique per strategy_tag (STRATEGY_ID), anchored to this
    script's own directory.
  - `client.history()`/`quotes()` can return an error dict instead of a
    DataFrame on a bad broker session -- handled explicitly.
  - The broker's last returned 3m bar is dropped unconditionally before use.
  - The `[inst] candle=...` signal log line is only emitted when the candle
    key actually advances, not on every indicator refresh (the noisy-log
    fix applied to Pivot+Supertrend earlier this project, built in here
    from the start).

Shared rules across all 4 legs
--------------------------------
  - Runs the full session, 09:15 - 15:30. Entry window 09:19 - 15:00
    (matching the source Tradetron config).
  - Universal exit: >= 15:15 (ADDED -- see note above).
  - Max 3 entries per leg per day.
  - Strike selection: ATM, Current Week expiry, resolved FRESH on every
    entry (no daily lock, unlike the VWAP strategy) -- EXCEPT on the
    underlying's own expiry day itself, when `resolve_current_week_expiry()`
    rolls to NEXT week's expiry instead (see that function's docstring;
    backtested improvement, smaller worst-trade/max-drawdown at a modest
    cost to total return).
  - Quantity: 1 lot per leg. Product: NRML (literal port fidelity, same
    choice as the other two strategies).

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.ema(data, period)` -> ndarray.
  * `ta.rsi(data, period=14)` -> ndarray.
  * `client.history(..., interval='3m')` is a standard, documented interval
    (confirmed in docs/api/market-data/intervals.md's sample response) --
    no synthetic resampling needed here, unlike the VWAP strategy's "2m".
  * `client.quotes(symbol=, exchange=)` -> live LTP.

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
from pathlib import Path
from typing import Optional

import numpy as np
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

# Python's default thread stack reservation is 8MB. This process runs
# several threads at once (fill-watchers, signal-refresh, PnL push, the
# trade-log writer, plus PriceStream's own watchdog/WS threads) -- none of
# which do anything beyond simple polling loops and REST calls, nowhere
# near deep recursion. At the default size that adds up to tens of MB of
# virtual address space reserved purely for stacks, out of the
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap (blueprints/python_strategy.py's
# set_resource_limits(), 1024MB by default) every strategy subprocess runs
# under -- confirmed in production as the actual ceiling behind
# "RuntimeError: can't start new thread" (2026-07-28, on the Combined
# script specifically, but the same risk applies here). Must be called
# before any thread is created; affects every threading.Thread from here
# on, including ones spawned internally by ThreadPoolExecutor.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, filter_known_fields
except ImportError:
    # Shared helper (strategies/scripts/_strategy_platform_client.py) not
    # present alongside this script -- e.g. it was copied out standalone.
    # Degrade gracefully: the live "trade just closed" SSE push simply won't
    # fire, but nothing else about the strategy is affected.
    def notify_trade_closed(env, log_warning=None):
        pass

    # Same behavior as the shared module's version (see its own docstring) --
    # trivial and dependency-free enough to duplicate exactly rather than
    # degrade, unlike notify_trade_closed above which genuinely needs
    # network/env access it can't have as a standalone fallback.
    def filter_known_fields(cls, raw):
        known = set(vars(cls()).keys())
        return {k: v for k, v in raw.items() if k in known}

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
    strategy_name: str = "Nifty & Sensex EMA34 + RSI Intraday Seller"
    version: str = "1.1.0"

    intraday_interval: str = "3m"     # standard, documented OpenAlgo interval
    history_lookback_days: int = 10   # calendar days of 3m history to fetch (EMA34/RSI14 warmup;
                                       # not session-reset, continuous like the source)
    ema_period: int = 34
    rsi_period: int = 14
    pe_rsi_entry_threshold: float = 53.0
    ce_rsi_entry_threshold: float = 47.0
    pe_rsi_exit_threshold: float = 47.0
    ce_rsi_exit_threshold: float = 53.0

    lot_multiplier: int = 1
    max_trades_per_leg_per_day: int = 3

    product: str = "NRML"
    price_type: str = "MARKET"

    entry_start: time = time(9, 19)
    entry_end: time = time(15, 0)
    universal_exit_time: time = time(15, 15)   # ADDED -- not in source, see module docstring
    market_close: time = time(15, 30)

    scheduler_interval: int = 10
    indicator_refresh_interval: int = 15     # throttle for the 3m EMA/RSI history fetch
    pnl_tick_interval: float = 0.8             # seconds between PnL pushes -- runs on its OWN scheduler
                                               # job (see report_pnl_tick), decoupled from
                                               # scheduler_interval, since it's cache-only/read-only and
                                               # doesn't share the blocking-call risk that interval guards

    # WebSocket LTP cache: a tick older than this is treated as stale and
    # falls back to a one-off REST client.quotes() call for that instrument.
    ws_stale_seconds: float = 20.0
    # NIFTY.NSE_INDEX/SENSEX.BSE_INDEX only get a new tick when their
    # underlying constituents actually trade and the index recalculates --
    # in the first ~45 minutes after 09:15 this is naturally burstier than
    # the rest of the day, with legitimate gaps wider than 20s between
    # recalculations. At the normal threshold, the watchdog was
    # misdiagnosing that normal opening-minutes irregularity as a dead
    # connection and forcing repeated resubscribes/reconnects -- confirmed
    # in production (2026-07-29) to reach all the way to a real
    # Unsubscribe/resubscribe cycle at the broker adapter, which itself
    # takes a moment to recover, compounding the next check's gap. A wider
    # threshold during this specific window only affects how fast the
    # WATCHDOG reacts to a genuinely dead connection (up to
    # ws_stale_seconds_open instead of ws_stale_seconds before it notices
    # and reconnects) -- it does not affect get_ltp()'s REST-fallback
    # behavior, which stays at the tighter ws_stale_seconds everywhere.
    ws_stale_seconds_open: float = 60.0
    ws_post_open_grace_until: time = time(10, 0)
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
    entry_px: float = 0.0          # LTP-based estimate, captured purely for the trade log's PnL
                                    # columns -- the exit decision itself never looks at price.
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
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "nifty_sensex_ema34_rsi_intraday"
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


def _current_ws_stale_threshold() -> float:
    """Widened staleness threshold for PriceStream's watchdog during the
    post-open grace window (see config.ws_stale_seconds_open's docstring)
    -- only affects how fast the watchdog reacts to a stale symbol, not
    get_ltp()'s REST-fallback threshold, which stays at ws_stale_seconds."""
    now = datetime.now(IST).time()
    if time(9, 15) <= now < config.ws_post_open_grace_until:
        return config.ws_stale_seconds_open
    return config.ws_stale_seconds


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
# LIVE PRICE STREAM (WebSocket, replaces per-cycle REST quotes for the
# underlyings -- see the module docstring's "Live price feed" section)
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
    during market hours and reconnects -- fully if the connection itself
    is down, or per-symbol (leaving a healthy symbol's feed undisturbed)
    if only some symbol(s) are stale while the connection is otherwise
    healthy. If a symbol stays stale across several consecutive cycles
    despite that per-symbol resubscribe, escalates to a full reconnect
    instead (see _watchdog_loop) -- confirmed in production that the
    per-symbol path alone can retry 30+ times with zero recovery while the
    connection itself stays reported connected/authenticated the whole
    time, so something in that connection's own state needs a clean reset,
    not another poke at the same symbol."""

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

            now = datetime.now(IST)
            stale_threshold = _current_ws_stale_threshold()
            with self._lock:
                tracked = list(self._instruments.items())
            stale_instruments = []
            for key, inst in tracked:
                entry = self._cache.get(key)
                if entry is None or (now - entry[1]).total_seconds() > stale_threshold:
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
    close_prev1: float
    close_prev2: float
    high_prev2: float
    low_prev2: float
    ema_high34_prev1: float
    ema_high34_prev2: float
    ema_low34_prev1: float
    ema_low34_prev2: float
    rsi_prev1: float
    ltp: float
    candle_key: str


# Log the [inst] candle=... line only when the candle actually advances, not
# on every indicator_refresh_interval refetch (same fix applied to
# Pivot+Supertrend -- see that script's changelog).
_last_logged_candle: dict[str, str] = {}


def compute_instrument_signal(client, inst: InstrumentConfig, ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch 3m history for one instrument's underlying, compute EMA(High,34),
    EMA(Low,34), RSI(14) off the last genuinely CLOSED bars, plus live LTP.
    Returns None (with a logged reason) if anything is unavailable."""
    end = datetime.now(IST).date()

    bars = client.history(
        symbol=inst.name, exchange=inst.underlying_exchange,
        interval=config.intraday_interval,
        start_date=(end - timedelta(days=config.history_lookback_days)).isoformat(),
        end_date=end.isoformat(),
    )
    if _is_error_response(bars):
        Log.warning(f"[{inst.name}] {config.intraday_interval} history error response: {bars}")
        return None
    if bars is None or bars.empty:
        Log.warning(f"[{inst.name}] empty {config.intraday_interval} history.")
        return None
    # Drop the still-forming last candle -- the broker's last bar keeps
    # updating well past its nominal close, same defensive pattern used
    # everywhere else in this codebase.
    if len(bars) >= 2:
        bars = bars.iloc[:-1]
    if len(bars) < config.ema_period + 2:
        Log.warning(
            f"[{inst.name}] only {len(bars)} {config.intraday_interval} bars after dropping "
            f"the still-forming one (need >= {config.ema_period + 2} for a stable EMA{config.ema_period}) "
            f"-- no signal."
        )
        return None

    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)

    ema_high = np.asarray(ta.ema(high, config.ema_period))
    ema_low = np.asarray(ta.ema(low, config.ema_period))
    rsi = np.asarray(ta.rsi(close, config.rsi_period))

    candle_key = str(bars.index[-1])

    if ltp is None:
        ltp = fetch_ltp(client, inst)
    if ltp is None:
        ltp = float(close[-1])  # fallback: closed-candle close is a reasonable proxy

    signal = InstrumentSignal(
        close_prev1=float(close[-1]), close_prev2=float(close[-2]),
        high_prev2=float(high[-2]), low_prev2=float(low[-2]),
        ema_high34_prev1=float(ema_high[-1]), ema_high34_prev2=float(ema_high[-2]),
        ema_low34_prev1=float(ema_low[-1]), ema_low34_prev2=float(ema_low[-2]),
        rsi_prev1=float(rsi[-1]), ltp=ltp, candle_key=candle_key,
    )

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] candle={candle_key} close={signal.close_prev1:.2f} "
            f"ema_high34={signal.ema_high34_prev1:.2f} ema_low34={signal.ema_low34_prev1:.2f} "
            f"rsi={signal.rsi_prev1:.2f} ltp={ltp:.2f} | "
            f"prev2: close={signal.close_prev2:.2f} high={signal.high_prev2:.2f} low={signal.low_prev2:.2f} "
            f"ema_high34={signal.ema_high34_prev2:.2f} ema_low34={signal.ema_low34_prev2:.2f}"
        )
    return signal


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
        self._last_error_push: dict[str, datetime] = {}
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(LEG_KEYS), thread_name_prefix="fillwatch"
        )
        # Separate, smaller pool purely for the periodic indicator/chain
        # refresh (get_signal -> _refresh_signal_and_chain_bg). If all
        # _fill_executor workers are simultaneously busy watching fills (each
        # can block up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds), a refresh submitted to that SAME pool would just queue
        # silently behind them for minutes, serving stale cached signals with
        # no log line and no escalation. One worker per instrument is enough
        # since _signal_refresh_pending already prevents duplicate submits.
        self._signal_executor = ThreadPoolExecutor(
            max_workers=len(INSTRUMENTS), thread_name_prefix="sigrefresh"
        )
        # Dedicated single worker for report_pnl_tick's platform push --
        # separate from _fill_executor so a fill-watcher blocked for minutes
        # (reprice loop) can never make the live PnL display go stale too;
        # PnL pushes are small/fast and only need to run one at a time.
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
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

    def _refresh_signal_and_chain_bg(self, inst: InstrumentConfig, ltp: Optional[float],
                                       refresh_chain: bool):
        """Runs on _fill_executor -- this is what makes get_signal's periodic
        refresh genuinely non-blocking. compute_instrument_signal() and
        fetch_chain() (via _refresh_chain_cache) both make real REST calls
        (client.history()/client.optionchain()); previously these were called
        inline from get_signal(), which is itself called inline from
        run_cycle(), so a slow/stuck broker response stalled the whole
        scheduler cycle for every leg. Now run_cycle only ever reads
        self._signal_cache / self._chain_cache -- never waits on the network
        call itself."""
        try:
            fresh = compute_instrument_signal(self.client, inst, ltp=ltp)
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
        now = datetime.now(IST)
        last = self._last_indicator_refresh.get(inst.name)
        due_for_refresh = last is None or (now - last).total_seconds() >= config.indicator_refresh_interval
        if due_for_refresh and inst.name not in self._signal_refresh_pending:
            # Only worth the background broker traffic when a new leg could actually
            # still be entered this cycle (within the entry window, at least one side
            # still flat) -- refreshing all day regardless (incl. after both legs are
            # already filled, or outside the entry window entirely) would just add
            # constant option-quote load on the broker for no reachable benefit.
            self._signal_refresh_pending.add(inst.name)
            self._signal_executor.submit(
                self._refresh_signal_and_chain_bg, inst, ltp, refresh_chain
            )

        cached = self._signal_cache.get(inst.name)
        if cached is None:
            return None

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
                    pos.symbol, inst.options_exchange, max_age=_current_ws_stale_threshold()
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
        self._last_error_push[leg_key] = datetime.now(IST)

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
            action = "SELL" if pos.error_state == "entry_failed" else "BUY"
            try:
                self._pnl_executor.submit(push_leg_error, self.env, leg_key, pos, action=action)
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to dispatch periodic error re-push: {exc}")

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
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange)
        if exit_px is not None:
            # Short leg: sell high (entry) -> buy back low (exit) = profit.
            # Kept in sync with today_realized_pnl (pushed to the platform
            # via report_pnl_to_platform) so it updates immediately, without
            # waiting on the background CSV writer thread.
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

        # Submitted to _signal_executor, NOT _fill_executor -- fill-watchers
        # can legitimately block that pool for minutes during a reprice loop,
        # which would otherwise silently stall Force Exit detection at
        # exactly the moment a human is trying to intervene fastest.
        self._signal_executor.submit(_run)

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
                # call for just this instrument if the feed is stale/missing.
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
                        entry_condition = (
                            signal.close_prev2 > signal.ema_high34_prev2
                            and signal.ltp > signal.close_prev1
                            and signal.close_prev1 > signal.high_prev2
                            and signal.rsi_prev1 > config.pe_rsi_entry_threshold
                        )
                        exit_condition = (
                            signal.close_prev1 < signal.ema_low34_prev1
                            or signal.rsi_prev1 < config.pe_rsi_exit_threshold
                        )
                    else:
                        entry_condition = (
                            signal.close_prev2 < signal.ema_low34_prev2
                            and signal.ltp < signal.close_prev1
                            and signal.close_prev1 < signal.low_prev2
                            and signal.rsi_prev1 < config.ce_rsi_entry_threshold
                        )
                        exit_condition = (
                            signal.close_prev1 > signal.ema_high34_prev1
                            or signal.rsi_prev1 > config.ce_rsi_exit_threshold
                        )

                    if has_position:
                        # Once an exit order has been placed (or its fill confirmed by
                        # the background watcher), it must be driven through to
                        # _finalize_exit regardless of what exit_condition does on a
                        # LATER cycle (e.g. RSI recovers back above threshold before
                        # the async fill lands) -- otherwise a filled-but-not-yet-
                        # finalized position would never get its trade-log row
                        # written or its leg cleared, permanently blocking re-entry.
                        exit_already_committed = bool(leg.position.exit_order_id) or leg.position.exit_filled
                        if exit_condition or exit_already_committed:
                            if exit_condition and not exit_already_committed:
                                Log.info(f"[{leg_key}] Exit condition met -> closing.")
                            self._exit_leg(leg_key, inst, reason="ema_rsi_reversal")
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


###############################################################################
# STARTUP
###############################################################################
def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version              : {config.version}")
    print(f"Instruments          : {', '.join(i.name for i in INSTRUMENTS)}")
    print(f"EMA period / RSI     : {config.ema_period} / {config.rsi_period}")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time} (ADDED, not in source)")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print("⚠️  NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK ⚠️")
    print("⚠️  NO NATIVE PER-TRADE STOP-LOSS -- exit is EMA/RSI reversal only ⚠️")
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
        if pos.entry_order_id and not pos.entry_filled:
            Log.warning(f"[{leg_key}] Resuming entry-fill watch for an order placed "
                        f"before a restart (order_id={pos.entry_order_id}).")
            engine._pending_fills.add(leg_key)
            inst_name = leg_key.split("_")[0]
            inst = next((i for i in INSTRUMENTS if i.name == inst_name), None)
            if inst is not None:
                engine._fill_executor.submit(
                    engine._watch_entry_fill, leg_key, inst, pos.entry_order_id,
                    pos.symbol, pos.quantity
                )
        elif pos.exit_order_id and not pos.exit_filled:
            Log.warning(f"[{leg_key}] Resuming exit-fill watch for an order placed "
                        f"before a restart (order_id={pos.exit_order_id}).")
            engine._pending_fills.add(leg_key)
            inst_name = leg_key.split("_")[0]
            inst = next((i for i in INSTRUMENTS if i.name == inst_name), None)
            if inst is not None:
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
        engine._signal_executor.shutdown(wait=False)
        engine._pnl_executor.shutdown(wait=False)
    except Exception:
        # scheduler.start() shouldn't normally raise anything else (job
        # exceptions are caught/logged by APScheduler itself), but if it
        # ever does, run the exact same cleanup instead of leaking the
        # WebSocket connection and both thread pools silently.
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._signal_executor.shutdown(wait=False)
        engine._pnl_executor.shutdown(wait=False)
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
