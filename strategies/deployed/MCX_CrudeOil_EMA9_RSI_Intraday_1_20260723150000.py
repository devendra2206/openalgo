"""
===============================================================================
MCX CRUDEOIL EMA9 + RSI Intraday Option Seller
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Derived from: `Nifty_Sensex_EMA34_RSI_Intraday_1_20260714000000.py` -- same
              family, same architecture, requested as a new instrument
              (MCX CRUDEOIL) with a faster EMA period (9 instead of 34) for
              crude's higher intraday volatility. NOT a Tradetron port --
              user-directed variant, built directly for this project.

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
Same risk profile as the other naked-seller strategies in this project
(Pivot+Supertrend, EMA34_RSI, VWAP_NoHA) -- no hedge leg.

Key structural difference vs. the NIFTY/SENSEX strategies in this family
------------------------------------------------------------------------
NIFTY/SENSEX have a quotable spot INDEX (NSE_INDEX/BSE_INDEX) to compute the
EMA/RSI signal off. CRUDEOIL has no such index -- MCX's only index-only
feeds are sectoral indices (MCXBULLDEX, MCXCRUDEX, etc., exchange code
MCX_INDEX), not a plain CRUDEOIL spot/index. So this strategy's signal reads
the CURRENT MONTH FUTURES CONTRACT's own 3-minute candles instead --
resolved dynamically via `client.expiry(..., instrumenttype="futures")`,
NOT hardcoded, since the exact tradable base symbol naming
(broker-master-data-dependent) can vary. Resolved ONCE PER DAY (see
`_reset_day_if_needed`) since the front-month contract only changes on
monthly rollover, not intraday -- unlike NIFTY/SENSEX's underlying symbol,
which never changes.

Both the underlying futures leg AND the options exchange for this
instrument are plain `MCX` (unlike NFO/BFO, which are split from
NSE_INDEX/BSE_INDEX) -- MCX has no separate "underlying quote exchange" vs.
"options exchange" distinction, confirmed against
`docs/prompt/symbol-format.md`'s MCX Futures/Options sections and
`docs/api/options-services/*.md`'s exchange field (`NFO, BFO, CDS, MCX,
CRYPTO`).

Signal Rules (identical shape to EMA34_RSI, EMA period 9 instead of 34)
------------------------------------------------------------------------
Computed off the front-month futures contract's own 3-minute candles:
  - EMA(High, 9) and EMA(Low, 9), and RSI(14) on Close -- all computed over
    the last-closed 3m bars (the newest fetched bar is unconditionally
    dropped as still-forming, same defensive pattern used everywhere else
    in this project).
  - close_prev1/prev2, high_prev2, low_prev2 = the last two CLOSED 3m
    candles' own OHLC.
  - ltp = live LTP of the futures contract -- used only as a live
    breakout-confirmation filter alongside the closed-candle signal.

  PE entry : close_prev2 > ema_high9_prev2   (2 candles ago, price closed
                                               above its own 9-EMA of
                                               Highs -- an overextension)
             AND ltp > close_prev1            (still pushing higher live)
             AND close_prev1 > high_prev2      (last candle broke above
                                               the high of the one before)
             AND rsi_prev1 > 53                (momentum confirms)
  CE entry : close_prev2 < ema_low9_prev2     (mirror, below 9-EMA of Lows)
             AND ltp < close_prev1
             AND close_prev1 < low_prev2
             AND rsi_prev1 < 47

  This is the same CONTRARIAN/fade-the-spike entry as EMA34_RSI (sell PE
  into a premium-side breakout that looks overextended) -- same philosophy,
  faster-reacting EMA suited to crude's higher intraday volatility than the
  NIFTY/SENSEX index.

  PE exit  : close_prev1 < ema_low9_prev1  OR  rsi_prev1 < 47
  CE exit  : close_prev1 > ema_high9_prev1 OR  rsi_prev1 > 53

  Exit is evaluated purely off CLOSED-candle values (no live LTP term),
  same as EMA34_RSI -- can only actually change once per 3-minute bar, even
  though the scheduler still polls every cycle.

  ** No native per-trade stop-loss or price-based exit.** Same disclosed
  risk profile as EMA34_RSI -- the only risk containment is the technical
  EMA/RSI reversal above and the universal exit below (bounds a bad trade
  to "at most until end of session", not a fixed rupee/point amount).
  Worth reconsidering after seeing live/backtest drawdown numbers on this
  new instrument.

Strike selection, expiry, and product (defaults chosen for this build --
please review before relying on them for live capital)
------------------------------------------------------------------------
  - ATM strike resolved FRESH on every entry (like EMA34_RSI/Pivot+Supertrend,
    not locked once/day like VWAP_NoHA).
  - Options expiry: nearest upcoming expiry via `client.expiry(...,
    instrumenttype="options")`, rolling to the NEXT expiry if today is
    itself the resolved expiry day (same gamma/theta-cliff avoidance as the
    other 3 strategies' `resolve_current_week_expiry` -- see
    `resolve_current_month_expiry` below).
  - **Product = MIS (intraday only, no overnight carry)** -- a deliberate,
    explicit choice for this build (unlike the NIFTY/SENSEX strategies,
    which use NRML): this is a brand-new instrument for this project with
    no backtest/live track record yet, deployed same-day per explicit
    instruction ("NSE closing, deploy MCX crudeoil now") -- MIS avoids
    carrying a naked commodity short overnight on a strategy that hasn't
    been observed live yet. Change to NRML only after you've reviewed live
    behavior and deliberately want overnight carry.
  - Quantity: 1 lot per leg (lot size fetched dynamically from the option
    chain response, never hardcoded -- MCX CRUDEOIL/CRUDEOILM lot sizes
    differ and are broker-master-data-dependent).
  - Trading window: MCX's official session is 09:00-23:55
    (`database/market_calendar_db.py`'s MCX offsets). Entry window
    09:20-22:30 (stops opening new naked shorts in the last ~1.5h before
    close); universal exit at 23:15, leaving a buffer for square-off order
    execution before the 23:55 hard close. All three are config values --
    adjust freely.
  - Max 3 entries per leg per day (same default as the other 3 strategies).

Live price feed: WebSocket, DYNAMIC subscription (like VWAP_NoHA/Batman,
NOT the fixed-list pattern used by Pivot+Supertrend/EMA34_RSI)
------------------------------------------------------------------------
Unlike NIFTY/SENSEX (whose underlying symbol never changes), this
strategy's underlying is a FUTURES CONTRACT that rolls monthly, and its
option leg symbols aren't known until ATM is resolved at entry -- so
`PriceStream` here uses the same DYNAMIC `add_instruments()` design as
VWAP_NoHA/Batman (see those scripts' module docstrings for the full
watchdog/reconnect writeup, identical here): the front-month futures
symbol is (re-)subscribed once per day in `_reset_day_if_needed`, and each
option leg's symbol is added the moment it's resolved at entry. A same-day
restart resuming mid-session also re-subscribes to the already-known
futures symbol and any already-open option legs at startup, same
resumability guarantee as VWAP_NoHA/Batman.

Order placement robustness, trade log, PnL reporting
------------------------------------------------------------------------
Identical to EMA34_RSI (see that script's module docstring for the full
writeup): rejected/cancelled order handling clears stale order ids instead
of looping forever, `poll_fill()` re-prices a stale unfilled order to
current LTP before giving up, a background thread writes closed trades to
`trades_{STRATEGY_ID}.csv`, and `report_pnl_tick()` pushes a live PnL
snapshot to the OpenAlgo Python Strategy Host on its own
`pnl_tick_interval`-second scheduler job (same mechanism now shared by all
5 strategies in this project).

Base symbol: CRUDEOIL (standard), not CRUDEOILM (Mini) -- confirmed, not
assumed
------------------------------------------------------------------------
MCX lists TWO separate crude oil contract families: the standard 100-barrel
`CRUDEOIL` and the 10-barrel Mini `CRUDEOILM`. Per the OpenAlgo symbol-format
reference (`.agents/skills/openalgo/references/symbol-format.md`), the Mini
variant is FUTURES-ONLY -- it has no listed options segment; only the
standard `CRUDEOIL` contract has one. Since this strategy trades OPTIONS,
`INSTRUMENTS[0].name = "CRUDEOIL"` is the only correct base -- and the
EMA9/RSI signal source (the futures contract, see above) must read the SAME
standard `CRUDEOIL` futures, not the Mini, since they are different
underlyings with different price/lot behavior and the signal must track
what's actually being traded. `client.expiry()`/`client.optionchain()` are
still called dynamically (no hardcoded expiry date), exactly like every
other strategy in this project does for NIFTY/SENSEX -- only the base name
itself was the open question, and it's resolved above.

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.ema(data, period)` -> ndarray. `ta.rsi(data, period=14)` -> ndarray.
  * `client.history(..., interval='3m')` is a standard, documented interval.

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

load_dotenv()

print("🔁 OpenAlgo Python Bot is running.")

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
    strategy_name: str = "MCX CRUDEOIL EMA9 + RSI Intraday Seller"
    version: str = "1.0.0"

    intraday_interval: str = "3m"     # standard, documented OpenAlgo interval
    history_lookback_days: int = 10   # calendar days of 3m history to fetch (EMA34/RSI14 warmup)
    ema_period: int = 34
    rsi_period: int = 14
    pe_rsi_entry_threshold: float = 53.0
    ce_rsi_entry_threshold: float = 47.0
    pe_rsi_exit_threshold: float = 47.0
    ce_rsi_exit_threshold: float = 53.0

    lot_multiplier: int = 1
    max_trades_per_leg_per_day: int = 3

    product: str = "MIS"              # intraday only, no overnight carry -- see module docstring
    price_type: str = "MARKET"

    entry_start: time = time(9, 20)
    entry_end: time = time(22, 30)
    universal_exit_time: time = time(23, 15)
    market_open: time = time(9, 0)
    market_close: time = time(23, 55)   # MCX official session, per database/market_calendar_db.py

    scheduler_interval: int = 10
    indicator_refresh_interval: int = 15     # throttle for the 3m EMA/RSI history fetch
    pnl_tick_interval: int = 1                # seconds between PnL pushes -- runs on its OWN scheduler
                                               # job (see report_pnl_tick), decoupled from
                                               # scheduler_interval, since it's cache-only/read-only and
                                               # doesn't share the blocking-call risk that interval guards

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
    fill_poll_timeout: float = 60.0
    reprice_max_attempts: int = 2    # times poll_fill() re-prices a stale unfilled order to LTP before giving up

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
    entry_px: float = 0.0          # LTP-based estimate, captured purely for the trade log's PnL
                                    # columns -- the exit decision itself never looks at price.
    entry_order_id: str = ""
    entry_filled: bool = False
    exit_order_id: str = ""
    exit_filled: bool = False
    exit_reject_count: int = 0     # consecutive rejected/cancelled exit orders for THIS open position
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
            or "mcx_crudeoil_ema9_rsi_intraday"
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
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass

    def _watchdog_loop(self):
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
                    self._connect()
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
        self.state.futures_symbol = data.get("futures_symbol", "")
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
            "futures_symbol": self.state.futures_symbol,
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


def resolve_current_month_expiry(client, inst: InstrumentConfig) -> str:
    """Nearest upcoming OPTIONS expiry (DDMMMYY) for CRUDEOIL -- EXCEPT on
    the resolved expiry day itself, when it rolls to the NEXT expiry
    instead (same gamma/theta-cliff avoidance as the other 3 strategies'
    resolve_current_week_expiry -- a same-day-expiring contract has minimal
    time value left in its final hours)."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve options expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    for i, raw in enumerate(dates_raw):
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            if d == today and i + 1 < len(dates_raw):
                return _compact_expiry(dates_raw[i + 1])
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


@dataclass
class InstrumentSignal:
    close_prev1: float
    close_prev2: float
    high_prev2: float
    low_prev2: float
    ema_high_prev1: float
    ema_high_prev2: float
    ema_low_prev1: float
    ema_low_prev2: float
    rsi_prev1: float
    ltp: float
    candle_key: str


# Log the [inst] candle=... line only when the candle actually advances, not
# on every indicator_refresh_interval refetch (same fix applied to the other
# 3 strategies in this project).
_last_logged_candle: dict[str, str] = {}


def compute_instrument_signal(client, inst: InstrumentConfig, futures_symbol: str,
                               ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch 3m history for the CURRENT MONTH FUTURES CONTRACT (not the bare
    underlying name -- CRUDEOIL has no quotable spot/index, see module
    docstring), compute EMA(High,9), EMA(Low,9), RSI(14) off the last
    genuinely CLOSED bars, plus live LTP. Returns None (with a logged
    reason) if anything is unavailable."""
    end = datetime.now(IST).date()

    bars = client.history(
        symbol=futures_symbol, exchange=inst.options_exchange,
        interval=config.intraday_interval,
        start_date=(end - timedelta(days=config.history_lookback_days)).isoformat(),
        end_date=end.isoformat(),
    )
    if _is_error_response(bars):
        Log.warning(f"[{inst.name}] {config.intraday_interval} history error response for {futures_symbol}: {bars}")
        return None
    if bars is None or bars.empty:
        Log.warning(f"[{inst.name}] empty {config.intraday_interval} history for {futures_symbol}.")
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
        ltp = fetch_symbol_ltp(client, futures_symbol, inst.options_exchange)
    if ltp is None:
        ltp = float(close[-1])  # fallback: closed-candle close is a reasonable proxy

    signal = InstrumentSignal(
        close_prev1=float(close[-1]), close_prev2=float(close[-2]),
        high_prev2=float(high[-2]), low_prev2=float(low[-2]),
        ema_high_prev1=float(ema_high[-1]), ema_high_prev2=float(ema_high[-2]),
        ema_low_prev1=float(ema_low[-1]), ema_low_prev2=float(ema_low[-2]),
        rsi_prev1=float(rsi[-1]), ltp=ltp, candle_key=candle_key,
    )

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] futures={futures_symbol} candle={candle_key} close={signal.close_prev1:.2f} "
            f"ema_high{config.ema_period}={signal.ema_high_prev1:.2f} ema_low{config.ema_period}={signal.ema_low_prev1:.2f} "
            f"rsi={signal.rsi_prev1:.2f} ltp={ltp:.2f} | "
            f"prev2: close={signal.close_prev2:.2f} high={signal.high_prev2:.2f} low={signal.low_prev2:.2f} "
            f"ema_high{config.ema_period}={signal.ema_high_prev2:.2f} ema_low{config.ema_period}={signal.ema_low_prev2:.2f}"
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
        # strike_count=1 (ATM +/- 1) is enough -- pick_atm_leg only ever uses the
        # ATM strike itself. A wider chain just means more option quotes for the
        # backend to fan out to the broker (Shoonya's multiquotes isn't a true
        # batch call -- broker/shoonya/api/data.py fans out one GetQuotes per
        # symbol via a ThreadPoolExecutor), which was adding real seconds of
        # latency to the entry path for strikes that are never used.
        underlying=underlying_symbol, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=1,
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
    """One reprice-to-current-LTP (modifyorder(), keeping the same order
    id/queue position) + one fill_poll_timeout-bounded wait. Returns the fill
    data if it completed, None if still unfilled (order left resting either
    way -- this never cancels). Shared by poll_fill()'s own reprice loop AND
    by Entry-Cancel's one-last-chance flow (_watch_entry_cancel), so this
    "give it a fair price, then wait" behavior only exists in one place."""
    import time as _time

    fresh_ltp = fetch_symbol_ltp(client, symbol, exchange)
    if fresh_ltp is None:
        Log.warning(f"Order {order_id}: no fresh quote available to re-price -- skipping this attempt.")
        return None
    try:
        client.modifyorder(
            order_id=order_id, strategy=strategy, symbol=symbol, action=action,
            exchange=exchange, price_type="LIMIT", product=config.product,
            quantity=str(quantity), price=str(fresh_ltp),
            disclosed_quantity="0", trigger_price="0",
        )
        Log.warning(f"Order {order_id}: re-priced to {fresh_ltp}.")
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
    """Polls order status until a terminal state or config.fill_poll_timeout.
    On timeout, actively RE-PRICES the same order (via _reprice_and_wait_once,
    keeping its order id/queue position) to the current LTP -- up to
    config.reprice_max_attempts times. If it's STILL resting after all of
    those, raises OrderNeedsAttention WITHOUT cancelling it -- entering error
    mode is now a user decision (Retry/Cancel/Manually Completed), not an
    automatic cancel. A genuine broker rejection/cancellation (order never
    became fillable at all) still raises RuntimeError immediately, unchanged.
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
    for the full rationale)."""
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
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(LEG_KEYS), thread_name_prefix="fillwatch"
        )
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

    def _refresh_chain_cache(self, inst: InstrumentConfig, futures_symbol: str):
        try:
            expiry = self._expiry_cache.get(inst.name)
            if expiry is None:
                expiry = resolve_current_month_expiry(self.client, inst)
                self._expiry_cache[inst.name] = expiry
            self._chain_cache[inst.name] = fetch_chain(self.client, inst, expiry, futures_symbol)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background chain refresh failed (will retry live at "
                        f"entry if needed): {exc}")

    def _refresh_signal_and_chain_bg(self, inst: InstrumentConfig, futures_symbol: str,
                                       ltp: Optional[float], refresh_chain: bool):
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
            fresh = compute_instrument_signal(self.client, inst, futures_symbol, ltp=ltp)
            if fresh is not None:
                self._signal_cache[inst.name] = fresh
                self._last_indicator_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background indicator refresh failed (will retry "
                        f"next cycle): {exc}")
        if refresh_chain:
            self._refresh_chain_cache(inst, futures_symbol)
        self._signal_refresh_pending.discard(inst.name)

    def get_signal(self, inst: InstrumentConfig, futures_symbol: str,
                    ltp: Optional[float] = None, refresh_chain: bool = False) -> Optional[InstrumentSignal]:
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
            self._fill_executor.submit(
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
        if self.store.state.current_day != today_key:
            try:
                futures_symbol = resolve_current_month_futures(self.client, INSTRUMENTS[0])
            except Exception as exc:
                Log.warning(f"Could not resolve current-month futures contract yet ({exc}); "
                            f"retrying next cycle.")
                return  # don't mark the day as reset -- retry next cycle
            Log.info(f"New day detected ({today_key}); resetting daily trade counters. "
                      f"Futures contract: {futures_symbol}")
            self.store.state.current_day = today_key
            self.store.state.today_realized_pnl = 0.0
            self.store.state.futures_symbol = futures_symbol
            self._expiry_cache.clear()
            self._chain_cache.clear()
            for leg in self.store.state.legs.values():
                leg.trade_count = 0
            self._save_state()
            self.price_stream.add_instruments(
                [{"symbol": futures_symbol, "exchange": INSTRUMENTS[0].options_exchange}]
            )

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
        """Runs on its OWN scheduler job at config.pnl_tick_interval (1s),
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
    def _enter_leg(self, leg_key: str, inst: InstrumentConfig, option_type: str, spot: float,
                    futures_symbol: str):
        leg = self.store.state.legs[leg_key]
        strategy_tag = self.env.strategy_tag
        pos = leg.position

        if not pos.symbol:
            # Reads the background-refreshed chain cache (see _refresh_chain_cache,
            # piggybacked on get_signal's indicator_refresh_interval cadence) instead
            # of fetching live here -- this is what keeps the signal-to-order path
            # down to just the order placement call itself. Live fetch is only a
            # fallback for the rare case a signal fires before the very first
            # background refresh has completed (e.g. right after a restart).
            chain = self._chain_cache.get(inst.name)
            if chain is None:
                expiry = self._expiry_cache.get(inst.name)
                if expiry is None:
                    expiry = resolve_current_month_expiry(self.client, inst)
                    self._expiry_cache[inst.name] = expiry
                chain = fetch_chain(self.client, inst, expiry, futures_symbol)
            atm_leg = pick_atm_leg(chain, option_type, spot)
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
            self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])

        if pos.entry_filled or leg_key in self._pending_fills:
            return  # already filled, or a background watcher is already tracking this order

        if not pos.entry_order_id:
            pos.entry_order_id = place(self.client, strategy_tag, pos.symbol,
                                        inst.options_exchange, "SELL", pos.quantity)
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
            pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                       inst.options_exchange, "BUY", pos.quantity)
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
        # (see docs/prd/python-strategies-order-error-recovery.md).
        exit_px = (pos.manual_exit_px if pos.manual_exit_px is not None
                   else fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange))
        if exit_px is not None:
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
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- skipping this row.")

        leg.position = LegPosition()
        self._save_state()

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    # See docs/prd/python-strategies-order-error-recovery.md for the full
    # design rationale behind every branch here.
    def _resolve_leg_error(self, leg_key: str, inst: InstrumentConfig, action: dict):
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            if was_exit:
                # _exit_leg IS reliably called again on a later cycle regardless
                # of exit_condition's value (see the exit_already_committed
                # gate in run_cycle) -- and its body unconditionally resumes
                # watching whatever exit_order_id is currently set. So the exit
                # side only needs the reprice (if resting) + clearing the error
                # fields; the normal flow does the rest.
                if kind == "resting":
                    fresh_ltp = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange)
                    if fresh_ltp is not None:
                        try:
                            self.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                symbol=pos.symbol, action="BUY",
                                exchange=inst.options_exchange, price_type="LIMIT",
                                product=config.product, quantity=str(pos.quantity),
                                price=str(fresh_ltp), disclosed_quantity="0", trigger_price="0",
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
                ack_pending_action(self.env, leg_key)
                return

            # Entry side: unlike exit, run_cycle only ever calls _enter_leg
            # when pos.symbol is EMPTY (has_position is false) -- and an
            # entry attempt already in error mode has pos.symbol set from the
            # moment the attempt began. run_cycle would therefore never call
            # _enter_leg again to resume this leg; Retry has to directly
            # (re)submit the watcher itself instead of relying on a normal-flow
            # path that structurally cannot fire for an in-progress entry.
            if kind == "resting":
                fresh_ltp = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange)
                if fresh_ltp is not None:
                    try:
                        self.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                            symbol=pos.symbol, action="SELL",
                            exchange=inst.options_exchange, price_type="LIMIT",
                            product=config.product, quantity=str(pos.quantity),
                            price=str(fresh_ltp), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{leg_key}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:  # kind == "terminal" -- nothing resting, place a genuinely new order
                resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                         inst.options_exchange, "SELL", pos.quantity)
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, leg_key, pos, clear=True)
            ack_pending_action(self.env, leg_key)
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(
                self._watch_entry_fill, leg_key, inst, resume_order_id, pos.symbol, pos.quantity
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
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(
                self._watch_entry_cancel, leg_key, inst, pos.error_order_id,
                pos.symbol, pos.quantity
            )
            return  # _watch_entry_cancel does its own save/push/ack once resolved

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

    def _watch_entry_cancel(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                             symbol: str, quantity: int):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned. Deliberately self-terminating (unlike the
        other watchers): ends in exactly one of two definitive outcomes, never
        back in error mode."""
        strategy_tag = self.env.strategy_tag
        result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                         symbol, inst.options_exchange, "SELL", quantity)
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        if pos.error_order_id != order_id:
            self._pending_fills.discard(leg_key)
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
            leg.position = LegPosition()
            self._save_state()
        self._pending_fills.discard(leg_key)
        push_leg_error(self.env, leg_key, leg.position, clear=True)
        ack_pending_action(self.env, leg_key)

    # ---- main cycle -----------------------------------------------------
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

            if check_force_exit(self.env):
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- all positions flat. Stopping.")
                    ack_force_exit_complete(self.env)
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
                        pending = check_pending_action(self.env, leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        continue

                    has_position = bool(leg.position.symbol)

                    if option_type == "PE":
                        entry_condition = (
                            signal.close_prev2 > signal.ema_high_prev2
                            and signal.ltp > signal.close_prev1
                            and signal.close_prev1 > signal.high_prev2
                            and signal.rsi_prev1 > config.pe_rsi_entry_threshold
                        )
                        exit_condition = (
                            signal.close_prev1 < signal.ema_low_prev1
                            or signal.rsi_prev1 < config.pe_rsi_exit_threshold
                        )
                    else:
                        entry_condition = (
                            signal.close_prev2 < signal.ema_low_prev2
                            and signal.ltp < signal.close_prev1
                            and signal.close_prev1 < signal.low_prev2
                            and signal.rsi_prev1 < config.ce_rsi_entry_threshold
                        )
                        exit_condition = (
                            signal.close_prev1 > signal.ema_high_prev1
                            or signal.rsi_prev1 > config.ce_rsi_exit_threshold
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

                    self._enter_leg(leg_key, inst, option_type, spot=signal.ltp, futures_symbol=futures_symbol)

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
    print(f"Instrument           : {INSTRUMENTS[0].name} ({INSTRUMENTS[0].options_exchange})")
    print(f"EMA period / RSI     : {config.ema_period} / {config.rsi_period}")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print(f"Product              : {config.product}")
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


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
