"""
===============================================================================
NIFTY 5-Min Supertrend/EMA9/Pivot Point Intraday Option Seller
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Ported from : data23_to_26/backtest_nifty_5min_supertrend_pivot_optionsell.py
              (roll-expiry variant) -- full 3-year backtest (2023-01-05 to
              2026-03-24): 884 trades, net +Rs150,231, max drawdown -Rs13,494.
              Every entry/exit condition below is a literal, unmodified port
              of that validated backtest's own logic -- nothing here is new
              or re-derived.

Description
-----------
Pure INTRADAY naked option-selling strategy on NIFTY, two independent legs
(PE "bullish setup" sell, CE "bearish setup" sell). Each leg has its own
entry/exit condition and its own daily trade counter -- you can be short
NIFTY PE and NIFTY CE at the same time, they don't interact. At most ONE PE
and ONE CE position open at the same time (single slot per side); a fresh
entry on the SAME side is allowed the same tick a position on that side just
exited, if conditions/count/window all still pass (the backtest has no
same-bar-reversal suppression -- a deliberate difference from some of this
project's other scripts).

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
No hedge leg at all -- protected only by the technical exit conditions below
plus the unconditional 15:15 force-close. Product is NRML (confirmed) --
**no broker-side auto-square-off backstop at all**; the strategy's OWN
`universal_exit_time` force-close is the only thing that ever closes these
positions, so that branch of `run_cycle()` must fire every single day without
fail.

Signal Rules (verbatim from the validated backtest)
------------------------------------------------------------------
Indicators, computed on a continuous 5-min NIFTY spot series (never reset per
day -- Supertrend/EMA9 both depend on prior-bar state):
  - Supertrend(6, 3) and EMA(9) on 5-min Close.
  - Daily floor pivots (Pivot, R1, S1, R2, S2) computed ONCE per day from the
    PREVIOUS regular trading day's full-session High/Low/Close (classic
    formula: PP=(H+L+C)/3, R1=2PP-L, S1=2PP-H, R2=PP+(H-L), S2=PP-(H-L)),
    held constant all day.

Indexing convention (matches the backtest's own notation):
  - Close[-1]/Open[-1]/High[-1]/Low[-1]/ST[-1]/EMA9[-1] = the last CLOSED
    5-min candle (`last_*` fields on InstrumentSignal below).
  - Close[-2]/ST[-2] = the candle before that (`prev_*` fields).
  - Low[0]/High[0] = the CURRENTLY FORMING 5-min candle's running low/high,
    built from live LTP ticks sampled every scheduler cycle (10s) -- a
    finer-grained proxy for the backtest's own 1-min-close aggregation, not
    a fidelity loss.
  - NIFTY_LTP = the live spot LTP (WebSocket-cached, REST fallback).

PE entry (bullish setup) -- mandatory 1-3 AND mandatory 4 (premium band) AND
any of Setup A/B/C:
  mandatory: Close[-1] > Open[-1]
             AND Close[-1] > ST[-1]
             AND NIFTY_LTP > min(Close[-1]+5, High[-1]+2)
  Setup A  : Close[-1] > R1 AND Low[-1] < R1                  (pivot rejection)
  Setup B  : Close[-2] > ST[-2] AND Close[-1] > R1
             AND Open[-2] > Close[-2] AND Close[-1] > EMA9[-1]  (2-candle continuation)
  Setup C  : Low[0] < S2 AND Low[-1] > EMA9[-1]                (S2 spike, calm prior candle)

CE entry (bearish setup) -- mirror of the above (Open[-1]>Close[-1], Close[-1]<ST[-1],
  NIFTY_LTP < max(Close[-1]-5, Low[-1]-2); Setup A: Close[-1]<S1 AND High[-1]>S1;
  Setup B: Close[-2]<ST[-2] AND Close[-1]<S1 AND Close[-2]>Open[-2] AND Close[-1]<EMA9[-1];
  Setup C: High[0]>R2 AND High[-1]<EMA9[-1]).

Strike selection (mandatory condition 4, both sides): ATM = nearest listed
strike to live spot. If ATM premium is in (20, 120): sell it. If ATM premium
>= 120 (too rich): walk further OTM one listed strike at a time (PE:
decreasing strikes, CE: increasing) until one is found with 20 < premium <=
120 -- sell that instead. If ATM premium <= 20, or no fallback strike is
found in range, skip the entry this cycle. See `select_capped_strike()`.

Exit (continuous, any true -> close that leg immediately):
  PE: (Close[-2] > ST[-2] AND Close[-1] < ST[-1])                    [Supertrend flip]
      OR (Close[-1] > ST[-1] AND NIFTY_LTP < ST[-1] - 10)            [pre-emptive live breach]
      OR (Close[-1] < EMA9[-1] AND Close[-1] < R1                    [lost EMA9 + pivot --
          AND entry_setup != "C")                                    SKIPPED for a Setup C entry, see below]
      OR (current PE premium >= entry_px * 1.20 if entry_px > 100
          else entry_px * 1.50)                                     [premium stop]
  CE: mirror, WITHOUT the Setup C exception (every CE setup, including CE's
      own Setup C mirror, uses all three technical exit conditions).
  Both: unconditional force-close at 15:15 regardless of any of the above.

  PE-side Setup C exception (confirmed after live-testing this exact
  change against 3 years of backtest data, isolated to Setup C's own PE
  trades): a PE position opened via Setup C ((Low[0] < S2) AND (Low[-1] >
  EMA9[-1])) does NOT use the "lost EMA9 + pivot" exit -- it rides through
  to the Supertrend flip, the pre-emptive breach, the premium stop, or the
  unconditional 15:15 close instead. This let winners run to the close
  rather than getting cut early, and was the entire source of the gain:
  Setup C's PE trades went from +Rs87.75 (48 trades total, ema9_pivot_loss
  active) to +Rs14,400.75 with this exception (CE and every other setup
  left untouched). The same exception tested WORSE when applied to CE
  (-Rs1,729.00 -> -Rs2,684.50), so it is deliberately PE-only. See
  data23_to_26/backtest_nifty_5min_supertrend_pivot_optionsell.py's module
  docstring for the full analysis this was validated against.

Expiry: nearest weekly >= today, rolled to the NEXT weekly if today itself is
that expiry -- `resolve_current_week_expiry()`, this repo's existing
live-standard function (matches the backtest's roll-expiry variant, which
had ~27% shallower max drawdown than the same-week variant for near-identical
net PnL).

Shared rules
--------------------------------
  - Entry window: 09:20:00 - 14:45:00 IST (matches the backtest exactly --
    NOT the wider 09:20-15:00 some other scripts in this project use).
  - Universal exit: >= 15:15 -- force-close EVERY open leg unconditionally.
    Product is NRML (no broker backstop), so this branch must never be
    skipped, gated, or delayed behind a candle-boundary check.
  - Max 3 entries per leg per day (resets daily).
  - Quantity: 1 lot per leg (config.lot_multiplier).
  - Product: NRML (confirmed choice -- see module docstring above).

Live-specific machinery (ported from this project's other live scripts, not
from the backtest -- the backtest has no notion of order placement, WS
feeds, or process restarts)
------------------------------------------------------------------------
  - Live price feed: WebSocket (`PriceStream`), not REST polling, for NIFTY
    spot -- same rationale/mechanism as
    Nifty_Sensex_Pivot_Supertrend_Intraday_1 (shared broker-side WS
    connection over ZeroMQ, background watchdog reconnect/resubscribe).
  - Candle/indicator state: re-fetched via `client.history(interval="5m")`
    on each candle-boundary-triggered refresh (confirmed choice -- NOT a
    tick-driven in-memory bucket), same pattern as every other script in
    this project. Only the running Low[0]/High[0] of the CURRENTLY forming
    bucket is tracked live, from LTP ticks sampled every scheduler cycle
    (10s) -- see `_update_running_extreme`.
  - LTP for the exit-side premium check (an OPTION's own live price, needed
    as an active exit-condition input, not just for trade-log price
    resolution) follows the same WS-first/REST-fallback/two-sided-quote-
    sanity-check chain as `_finalize_exit`'s own price resolution:
    `price_stream.get_ltp()` -> `fetch_symbol_ltp(..., require_two_sided=True)`.
  - Order placement robustness, resumable leg-by-leg entry/exit, order error
    recovery (Retry/Cancel/Manually Completed), the async CSV trade log, PnL
    reporting, and the platform error/force-exit HTTP surface are all
    copied verbatim from Nifty_Sensex_Pivot_Supertrend_Intraday_1 -- see
    that script's own module docstring for the full history/rationale
    behind each of these mechanisms; nothing about them is strategy-specific.

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.supertrend(high, low, close, period=6, multiplier=3.0)` returns
    `(line, direction)` -- only `line` is used here.
  * Daily pivots are computed directly with the backtest's own formula
    (PP=(H+L+C)/3 etc.), NOT via `ta.pivot_points()` -- guarantees exact
    parity with the already-validated backtest rather than trusting a
    library function to use the identical convention.
  * `client.quotes(symbol=, exchange=)` -> live LTP (+ bid/ask).
  * `client.optionchain(...)` -> chain rows keyed by strike, each with
    nested `pe`/`ce` dicts (`ltp`, `lotsize`, `symbol`, ...).
  * `client.expiry(symbol=, exchange=, instrumenttype=)` returns dates in
    "DD-MMM-YY" format; OpenAlgo order/chain endpoints want "DDMMMYY".

Author
------
<Project Owner>
===============================================================================
"""

import copy
import csv
import json
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
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

# See Nifty_Sensex_Pivot_Supertrend_Intraday_1's own comment for the full
# rationale -- this process runs several polling/REST-only threads at once
# (fill-watchers, Force Exit background check, PnL push, trade-log writer,
# PriceStream's own watchdog/WS threads), none of which need deep recursion,
# and the default 8MB-per-thread stack reservation was the confirmed cause of
# "RuntimeError: can't start new thread" against this project's
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap. Must be called before any thread is
# created.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, notify_telegram_error, filter_known_fields
except ImportError:
    def notify_trade_closed(env, log_warning=None):
        pass

    def notify_telegram_error(env, message, log_warning=None):
        pass

    def filter_known_fields(cls, raw):
        known = set(vars(cls()).keys())
        return {k: v for k, v in raw.items() if k in known}

load_dotenv()

print("OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
@dataclass
class InstrumentConfig:
    name: str                    # "NIFTY"
    underlying_exchange: str     # NSE_INDEX
    options_exchange: str        # NFO


INSTRUMENTS = [
    InstrumentConfig(name="NIFTY", underlying_exchange="NSE_INDEX", options_exchange="NFO"),
]


@dataclass
class Config:
    strategy_name: str = "NIFTY 5-Min Supertrend/EMA9/Pivot Intraday Seller"
    version: str = "1.0.0"

    intraday_interval: str = "5m"     # standard, documented OpenAlgo interval
    history_lookback_days: int = 10   # calendar days of 5m history to fetch (Supertrend(6,3)/EMA9 warmup)
    daily_interval: str = "D"
    bar_minutes: int = 5              # matches the backtest's BAR_MIN -- also the running Low[0]/High[0] bucket width
    supertrend_period: int = 6
    supertrend_multiplier: float = 3.0
    ema_period: int = 9

    lot_multiplier: int = 1           # number of lots per leg
    max_trades_per_leg_per_day: int = 3

    premium_filter_low: float = 20.0
    premium_filter_high: float = 120.0
    strike_count: int = 10            # optionchain() scan width for ATM + fallback-strike walk

    product: str = "NRML"             # confirmed -- NO broker-side auto-square-off backstop, see module docstring
    price_type: str = "MARKET"

    entry_start: time = time(9, 20)
    entry_end: time = time(14, 45)             # matches the backtest exactly
    universal_exit_time: time = time(15, 15)   # force-close everything at/after this -- the ONLY backstop (NRML)
    market_close: time = time(15, 30)          # strategy actively runs the full session, 9:15-15:30

    # Entry/exit conditions compare a closed-candle indicator (Supertrend,
    # EMA9, daily pivots) against LIVE LTP -- the crossover moment itself can
    # happen at any second, not just on a 5m candle boundary. So the cycle
    # runs frequently (near-continuous) via `scheduler_interval`, but the
    # expensive client.history() calls (daily + 5m OHLC) are throttled
    # separately via `indicator_refresh_interval`.
    scheduler_interval: int = 10             # seconds between strategy cycles (LTP check cadence)
    indicator_refresh_interval: int = 15     # seconds between re-fetching the 5m Supertrend/EMA9 signal
    daily_refresh_interval: int = 600        # seconds between re-fetching the daily pivot (changes once/day)
    pnl_tick_interval: float = 0.8

    ws_stale_seconds: float = 20.0
    ws_stale_seconds_open: float = 60.0        # see Pivot_Supertrend's docstring -- widened during the noisy open
    ws_post_open_grace_until: time = time(10, 0)
    ws_watchdog_interval: float = 15.0
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    error_repush_interval_sec: float = 60.0
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
        Log.logger.exception(message)


###############################################################################
# MODELS
###############################################################################
@dataclass
class LegPosition:
    symbol: str = ""
    quantity: int = 0
    entry_time: str = ""
    entry_px: float = 0.0
    entry_order_id: str = ""
    entry_filled: bool = False
    exit_order_id: str = ""
    exit_filled: bool = False
    execution_id: int = 0
    error_state: str = ""           # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""            # "" | "terminal" | "resting"
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None
    exit_fill_px: Optional[float] = None
    entry_setup: str = ""           # "" | "A" | "B" | "C" -- which entry setup fired,
                                     # consulted by _technical_exit_condition's PE-side
                                     # Setup C exception (skips ema9_pivot_loss)


@dataclass
class LegState:
    trade_count: int = 0
    position: LegPosition = field(default_factory=LegPosition)


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    last_updated: str = ""
    today_realized_pnl: float = 0.0
    last_execution_id: int = 0


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
        self.timeout = 10.0
        self.ltp_timeout = 3.0
        self.ws_url = os.getenv("WEBSOCKET_URL")
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "nifty_5min_supertrend_ema_pivot_sell"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return time(9, 15) <= now <= config.market_close


def _current_ws_stale_threshold() -> float:
    now = datetime.now(IST).time()
    if time(9, 15) <= now < config.ws_post_open_grace_until:
        return config.ws_stale_seconds_open
    return config.ws_stale_seconds


def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle. See
    Nifty_Sensex_Pivot_Supertrend_Intraday_1's own docstring for the full
    rationale -- this lets get_signal() (and, here, the running Low[0]/
    High[0] tracker) detect a bucket rollover on the first cycle after it
    begins, instead of relying solely on a rolling refresh timer with no
    awareness of where the true candle boundaries fall."""
    now = datetime.now(IST)
    total_minutes = now.hour * 60 + now.minute
    bucket_start_minutes = (total_minutes // interval_minutes) * interval_minutes
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=bucket_start_minutes)


def _candle_key_boundary(candle_key: str) -> Optional[datetime]:
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
            auto_reconnect=False,
        )
        Log.info("Connected to OpenAlgo")
        return self.client

    def connect_ltp_client(self):
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
# LIVE PRICE STREAM (WebSocket)
###############################################################################
class PriceStream:
    """Same mechanism as Nifty_Sensex_Pivot_Supertrend_Intraday_1's
    PriceStream -- subscribes to LTP mode for a dynamically growing set of
    symbols (the NIFTY index at startup, plus each option leg's own symbol
    as it's resolved at entry). See that script's own docstring for the
    full rationale behind the watchdog/reconnect/per-symbol-resubscribe
    design; nothing here is strategy-specific."""

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

    def remove_instruments(self, instruments: list):
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
    """'10-JUL-25' -> '10JUL25'."""
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def resolve_current_week_expiry(client, inst: InstrumentConfig) -> str:
    """Nearest upcoming weekly expiry (DDMMMYY) -- EXCEPT on the underlying's
    own expiry day itself, when it rolls to NEXT week's expiry instead. This
    is the repo-standard live function (not strategy-specific) and matches
    the backtest's own roll-expiry variant."""
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
                raise RuntimeError(
                    f"{inst.name}: today ({today}) is the nearest expiry and the broker "
                    f"returned no later expiry date to roll to -- refusing to silently "
                    f"trade today's expiring contract."
                )
            return _compact_expiry(raw)
    return _compact_expiry(dates_raw[-1])


def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
    """`require_two_sided=True` additionally requires bid>0 AND ask>0 before
    trusting the quote -- defends against a quote that looks like it belongs
    to a DIFFERENT instrument than requested (confirmed in production on this
    same broker). Pass True for TRADABLE-instrument reads (option legs); an
    INDEX symbol legitimately has no bid/ask, so leave this False for spot
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


def fetch_ltp(client, inst: InstrumentConfig) -> Optional[float]:
    return fetch_symbol_ltp(client, inst.name, inst.underlying_exchange)


def fetch_symbol_bid_ask(client, symbol: str, exchange: str) -> tuple[Optional[float], Optional[float]]:
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


def resolve_exit_ltp(price_stream: "PriceStream", ltp_client, symbol: str, exchange: str,
                      max_age: float = None) -> Optional[float]:
    """WS-first, REST-with-sanity-check-fallback LTP resolution for an
    OPTION leg's own live price -- used both as the exit-side premium-stop
    condition's live input (run_cycle) and by _finalize_exit's trade-log
    price resolution. Never trusts a REST quote without require_two_sided,
    since this is exactly the class of quote-mismatch bug documented in
    fetch_symbol_ltp's own docstring.

    `max_age` defaults to the FIXED config.ws_stale_seconds (20s), not the
    widened post-open-grace threshold `_current_ws_stale_threshold()` uses
    for PriceStream's own watchdog -- both callers here feed a trading
    DECISION (an active exit condition, or the price booked into
    today_realized_pnl/the trade log), and a stale option premium accepted
    up to 60s old during the 9:15-10:00 grace window would be wrong for
    either. That widened threshold is intentionally only for the watchdog's
    OWN reconnect aggressiveness and for report_pnl_tick's purely
    observational display, never for a value that drives a decision or gets
    booked as realized PnL."""
    if max_age is None:
        max_age = config.ws_stale_seconds
    px = price_stream.get_ltp(symbol, exchange, max_age=max_age)
    if px is not None:
        return px
    return fetch_symbol_ltp(ltp_client, symbol, exchange, require_two_sided=True)


@dataclass
class InstrumentSignal:
    r1: float
    s1: float
    r2: float
    s2: float
    last_open: float
    last_close: float
    last_high: float
    last_low: float
    last_supertrend: float
    last_ema9: float
    prev_open: float
    prev_close: float
    prev_supertrend: float
    ltp: float
    candle_key: str


_last_logged_candle: dict[str, str] = {}


def fetch_daily_pivot(client, inst: InstrumentConfig) -> Optional[tuple]:
    """Fetch the previous COMPLETED day's daily OHLC and compute classic
    floor pivots (Pivot, R1, S1, R2, S2) using the EXACT formula the
    backtest itself uses (PP=(H+L+C)/3, R1=2PP-L, S1=2PP-H, R2=PP+(H-L),
    S2=PP-(H-L)) -- computed directly rather than via `ta.pivot_points()`,
    to guarantee exact parity with the already-validated backtest instead of
    trusting a library function to share the identical convention. Cached
    and refreshed on its own slow cadence (config.daily_refresh_interval),
    same as every other script in this project."""
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
    if len(daily) >= 2:
        daily = daily.iloc[:-1]  # drop today's still-forming daily bar
    prev_day = daily.iloc[-1]
    h, l, c = float(prev_day["high"]), float(prev_day["low"]), float(prev_day["close"])
    pp = (h + l + c) / 3.0
    r1 = 2 * pp - l
    s1 = 2 * pp - h
    r2 = pp + (h - l)
    s2 = pp - (h - l)
    return r1, s1, r2, s2


def compute_instrument_signal(client, inst: InstrumentConfig, r1: float, s1: float, r2: float, s2: float,
                               ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch 5m history and compute Supertrend(6,3)/EMA9 off the last two
    genuinely CLOSED bars, plus live LTP. Returns None (with a logged
    reason) if anything is unavailable -- callers must treat that as "no
    signal this cycle", not an error."""
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
    # Drop the still-forming last candle -- the broker's last bar keeps
    # updating well past its nominal close; never trade on an unsettled bar.
    if len(intraday) >= 2:
        intraday = intraday.iloc[:-1]
    if len(intraday) < config.supertrend_period + 2:
        Log.warning(
            f"[{inst.name}] only {len(intraday)} {config.intraday_interval} bars after dropping "
            f"the still-forming one (need >= {config.supertrend_period + 2} for Close[-2]) -- no signal."
        )
        return None

    st_line, _direction = ta.supertrend(
        intraday["high"], intraday["low"], intraday["close"],
        period=config.supertrend_period, multiplier=config.supertrend_multiplier,
    )
    ema_line = ta.ema(intraday["close"], config.ema_period)
    st_arr = np.asarray(st_line)
    ema_arr = np.asarray(ema_line)

    last_row = intraday.iloc[-1]
    prev_row = intraday.iloc[-2]
    last_open, last_close = float(last_row["open"]), float(last_row["close"])
    last_high, last_low = float(last_row["high"]), float(last_row["low"])
    prev_open, prev_close = float(prev_row["open"]), float(prev_row["close"])
    last_supertrend, prev_supertrend = float(st_arr[-1]), float(st_arr[-2])
    last_ema9 = float(ema_arr[-1])
    candle_key = str(intraday.index[-1])

    if ltp is None:
        ltp = fetch_ltp(client, inst)
    if ltp is None:
        ltp = last_close  # fallback: closed-candle close is a reasonable proxy if quotes() is down

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] candle={candle_key} O={last_open:.2f} C={last_close:.2f} "
            f"H={last_high:.2f} L={last_low:.2f} ST={last_supertrend:.2f} EMA9={last_ema9:.2f} "
            f"r1={r1:.2f} s1={s1:.2f} r2={r2:.2f} s2={s2:.2f} ltp={ltp:.2f}"
        )
    return InstrumentSignal(
        r1=r1, s1=s1, r2=r2, s2=s2,
        last_open=last_open, last_close=last_close, last_high=last_high, last_low=last_low,
        last_supertrend=last_supertrend, last_ema9=last_ema9,
        prev_open=prev_open, prev_close=prev_close, prev_supertrend=prev_supertrend,
        ltp=ltp, candle_key=candle_key,
    )


def fetch_chain(client, inst: InstrumentConfig, expiry: str):
    """strike_count=10 (confirmed) -- keeps the quote batch comfortably under
    Shoonya's 20-symbol batching boundary (avoids the 1s inter-batch
    rate-limit delay) while giving enough OTM room for the fallback-strike
    walk. with_quotes=True is required here -- select_capped_strike() reads
    each leg's own premium, unlike Pivot_Supertrend's ATM-only fetch_chain."""
    resp = client.optionchain(
        underlying=inst.name, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=config.strike_count, with_quotes=True,
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


def select_capped_strike(chain: dict, option_type: str, spot: float) -> Optional[tuple]:
    """Direct live port of the backtest's own select_capped_strike(): ATM
    (nearest listed strike to spot) if its premium is already in (20, 120).
    If the ATM premium is >= 120, walk further OTM one listed strike at a
    time (PE: decreasing strikes, CE: increasing) until one is found with
    20 < premium <= 120 -- return that instead. Returns (leg_dict, premium,
    used_fallback) or None if no candidate anywhere satisfies the band
    (including ATM premium <= 20, which has no fallback), matching the
    backtest's own semantics exactly. Walks within the SAME cached chain
    response -- no second broker call needed."""
    legs = sorted(_legs_with_strike(chain, option_type), key=lambda l: l["strike"])
    if not legs:
        return None
    atm_idx = min(range(len(legs)), key=lambda i: abs(legs[i]["strike"] - spot))

    def _premium(leg) -> Optional[float]:
        try:
            p = leg.get("ltp")
            return float(p) if p is not None else None
        except (TypeError, ValueError):
            return None

    atm_leg = legs[atm_idx]
    atm_premium = _premium(atm_leg)
    if atm_premium is None:
        return None
    if config.premium_filter_low < atm_premium < config.premium_filter_high:
        return atm_leg, atm_premium, False
    if atm_premium <= config.premium_filter_low:
        return None  # too cheap -- no fallback specified for this side of the band

    step = -1 if option_type == "PE" else 1
    idx = atm_idx + step
    while 0 <= idx < len(legs):
        leg = legs[idx]
        premium = _premium(leg)
        if premium is not None and config.premium_filter_low < premium <= config.premium_filter_high:
            return leg, premium, True
        idx += step
    return None


class OrderNeedsAttention(Exception):
    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


def _reprice_and_wait_once(client, order_id: str, strategy: str, symbol: str, exchange: str,
                            action: str, quantity: int) -> Optional[dict]:
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
    return None


def poll_fill(client, orderid: str, strategy: str, symbol: str, exchange: str,
              action: str, quantity: int) -> dict:
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
        return None

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
# TRADE LOG (background thread)
###############################################################################
_trade_log_queue: "queue.Queue" = queue.Queue()
_trade_log_thread: Optional[threading.Thread] = None
_trade_log_thread_lock = threading.Lock()

_TRADE_LOG_HEADER = ["leg", "symbol", "quantity", "entry_time", "entry_px",
                     "exit_time", "exit_px", "pnl_points", "pnl_rupees",
                     "exit_reason", "execution_id"]


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
            display_quantity = -quantity
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
    path = f"/python/api/strategy/{env.strategy_tag}/pending_action?leg_key={leg_key}&apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_pending_action failed for {leg_key}: {exc}")
        return None
    return data if data.get("action") else None


def ack_pending_action(env: "Environment", leg_key: str):
    payload = json.dumps({"apikey": env.api_key, "leg_key": leg_key}).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/pending_action/ack"
    try:
        _post_json_local(env, path, payload)
    except Exception as exc:
        Log.warning(f"ack_pending_action failed for {leg_key}: {exc}")


def check_force_exit(env: "Environment") -> bool:
    path = f"/python/api/strategy/{env.strategy_tag}/force_exit?apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_force_exit failed: {exc}")
        return False
    return bool(data.get("requested"))


def ack_force_exit_complete(env: "Environment"):
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
        self.ltp_client = ltp_client if ltp_client is not None else client
        self.store = store
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id
        self._signal_cache: dict[str, InstrumentSignal] = {}
        self._last_indicator_refresh: dict[str, datetime] = {}
        self._daily_pivot_cache: dict[str, tuple] = {}
        self._last_daily_refresh: dict[str, datetime] = {}
        self._ws_fallback_logged: dict[str, bool] = {}
        # Running Low[0]/High[0] of the currently-forming bar_minutes bucket,
        # per instrument -- (bucket_start, running_low, running_high),
        # updated from live spot LTP every run_cycle tick (see
        # _update_running_extreme). Separate from the cached 5m Supertrend/
        # EMA9 signal above -- this is a lightweight, always-live tracker,
        # not tied to indicator_refresh_interval.
        self._running_extreme: dict[str, tuple] = {}
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._last_error_push: dict[str, datetime] = {}
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(LEG_KEYS), thread_name_prefix="fillwatch"
        )
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bgcheck")
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        self._expiry_cache: dict[str, str] = {}
        self._chain_cache: dict[str, dict] = {}
        self._signal_refresh_pending: set[str] = set()

    def _save_state(self):
        with self._state_lock:
            self.store.save()

    def _update_running_extreme(self, inst_name: str, ltp: Optional[float]):
        """Tracks the currently-forming config.bar_minutes bucket's running
        low/high from live spot LTP, sampled every scheduler cycle (10s) --
        a finer-grained proxy for the backtest's own 1-min-close
        aggregation (Setup C needs this; it is NOT part of the cached 5m
        Supertrend/EMA9 signal, which only ever reflects CLOSED bars)."""
        if ltp is None:
            return
        bucket_start = _current_candle_boundary(config.bar_minutes)
        entry = self._running_extreme.get(inst_name)
        if entry is None or entry[0] != bucket_start:
            self._running_extreme[inst_name] = (bucket_start, ltp, ltp)
        else:
            _, lo, hi = entry
            self._running_extreme[inst_name] = (bucket_start, min(lo, ltp), max(hi, ltp))

    def _get_running_extreme(self, inst_name: str) -> tuple:
        entry = self._running_extreme.get(inst_name)
        return (entry[1], entry[2]) if entry is not None else (None, None)

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

    def _refresh_signal_chain_bg(self, inst: InstrumentConfig, ltp: Optional[float],
                                   refresh_chain: bool, refresh_daily: bool):
        """Runs on _fill_executor -- makes get_signal's periodic refresh
        genuinely non-blocking, same pattern as Pivot_Supertrend."""
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
        r1, s1, r2, s2 = daily

        try:
            fresh = compute_instrument_signal(self.client, inst, r1, s1, r2, s2, ltp=ltp)
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
        """Cached, throttled indicator fetch + fresh LTP every call -- same
        cadence split as Pivot_Supertrend: daily pivot on
        daily_refresh_interval, 5m Supertrend/EMA9 on candle-boundary-aware
        indicator_refresh_interval, LTP fresh on every call."""
        now = datetime.now(IST)

        last_daily = self._last_daily_refresh.get(inst.name)
        due_daily = (last_daily is None
                     or (now - last_daily).total_seconds() >= config.daily_refresh_interval)
        last = self._last_indicator_refresh.get(inst.name)
        current_boundary = _current_candle_boundary(config.bar_minutes)
        cached_for_boundary = self._signal_cache.get(inst.name)
        cached_boundary = (
            _candle_key_boundary(cached_for_boundary.candle_key)
            if cached_for_boundary is not None else None
        )
        # candle_key is the START of the last CLOSED bar (compute_instrument_signal
        # drops the still-forming last row), so it always lags current_boundary
        # (the start of the currently-forming bar) by exactly one bar_minutes
        # interval -- compare against that, not against current_boundary itself,
        # or this never matches and due_signal is permanently true.
        last_closed_boundary = current_boundary - timedelta(minutes=config.bar_minutes)
        have_current_candle = cached_boundary is not None and cached_boundary >= last_closed_boundary
        due_signal = last is None or not have_current_candle

        if (due_daily or due_signal) and inst.name not in self._signal_refresh_pending:
            self._signal_refresh_pending.add(inst.name)
            self._fill_executor.submit(
                self._refresh_signal_chain_bg, inst, ltp, refresh_chain, due_daily
            )

        daily = self._daily_pivot_cache.get(inst.name)
        if daily is None:
            return None
        r1, s1, r2, s2 = daily

        cached = self._signal_cache.get(inst.name)
        if cached is None:
            return None

        cached.r1, cached.s1, cached.r2, cached.s2 = r1, s1, r2, s2

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
            self._running_extreme.clear()
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
                    "quantity": -pos.quantity, "entry_price": pos.entry_px,
                    "current_price": current_px, "pnl": pnl,
                    "entry_time": pos.entry_time, "execution_id": pos.execution_id,
                })
            try:
                self._pnl_executor.submit(
                    report_pnl_to_platform, self.env, self.store.state.today_realized_pnl,
                    open_positions,
                )
            except Exception as exc:
                Log.warning(f"Failed to dispatch report_pnl_to_platform: {exc}")
        except Exception as exc:
            Log.exception(f"report_pnl_tick failed: {exc}")

    # ---- entry / exit condition evaluation ---------------------------------
    def _entry_condition(self, option_type: str, signal: InstrumentSignal,
                          running_low: Optional[float], running_high: Optional[float]) -> Optional[str]:
        """Literal port of the backtest's mandatory + Setup A/B/C conditions
        -- see this module's own docstring for the full rule text. Returns
        the matched setup tag ("A"/"B"/"C") rather than a plain bool -- the
        tag is persisted on LegPosition.entry_setup and consulted by
        _technical_exit_condition's PE-side Setup C exception below."""
        if option_type == "PE":
            mandatory = (
                signal.last_close > signal.last_open
                and signal.last_close > signal.last_supertrend
                and signal.ltp > min(signal.last_close + 5, signal.last_high + 2)
            )
            if not mandatory:
                return None
            setup_a = signal.last_close > signal.r1 and signal.last_low < signal.r1
            setup_b = (
                signal.prev_close > signal.prev_supertrend
                and signal.last_close > signal.r1
                and signal.prev_close < signal.prev_open
                and signal.last_close > signal.last_ema9
            )
            setup_c = running_low is not None and running_low < signal.s2 and signal.last_low > signal.last_ema9
            if setup_a:
                return "A"
            if setup_b:
                return "B"
            if setup_c:
                return "C"
            return None
        else:  # CE
            mandatory = (
                signal.last_close < signal.last_open
                and signal.last_close < signal.last_supertrend
                and signal.ltp < max(signal.last_close - 5, signal.last_low - 2)
            )
            if not mandatory:
                return None
            setup_a = signal.last_close < signal.s1 and signal.last_high > signal.s1
            setup_b = (
                signal.prev_close < signal.prev_supertrend
                and signal.last_close < signal.s1
                and signal.prev_close > signal.prev_open
                and signal.last_close < signal.last_ema9
            )
            setup_c = running_high is not None and running_high > signal.r2 and signal.last_high < signal.last_ema9
            if setup_a:
                return "A"
            if setup_b:
                return "B"
            if setup_c:
                return "C"
            return None

    def _technical_exit_condition(self, option_type: str, signal: InstrumentSignal,
                                   entry_setup: str = "") -> bool:
        """The three spot-only exit conditions (Supertrend flip, pre-emptive
        live breach, EMA9+pivot loss) -- the premium-based stop (conditions
        4/5) is checked separately in run_cycle since it needs the OPTION's
        own live premium, not the spot signal.

        PE-side Setup C exception (confirmed against 3 years of backtest
        data, isolated PnL swing: +Rs87.75 -> +Rs14,400.75 for Setup C's own
        PE trades, CE and every other setup left untouched): a PE position
        opened via Setup C does NOT use the ema9_pivot_loss leg of this
        condition -- letting it ride to st_flip/preemptive_breach/
        premium_stop/the 15:15 close instead of an early EMA9+pivot cut was
        the entire source of the gain. Every other PE setup (A/B), and CE
        under every setup including CE's own Setup C mirror, are unaffected
        -- the same change tested worse on CE and was deliberately not
        applied there. See data23_to_26/backtest_nifty_5min_supertrend_pivot_optionsell.py's
        module docstring ("PE-side Setup C exception") for the full
        analysis."""
        if option_type == "PE":
            ema9_pivot_loss = (
                entry_setup != "C"
                and signal.last_close < signal.last_ema9 and signal.last_close < signal.r1
            )
            return (
                (signal.prev_close > signal.prev_supertrend and signal.last_close < signal.last_supertrend)
                or (signal.last_close > signal.last_supertrend and signal.ltp < signal.last_supertrend - 10)
                or ema9_pivot_loss
            )
        else:  # CE
            return (
                (signal.prev_close < signal.prev_supertrend and signal.last_close > signal.last_supertrend)
                or (signal.last_close < signal.last_supertrend and signal.ltp > signal.last_supertrend + 10)
                or (signal.last_close > signal.last_ema9 and signal.last_close > signal.s1)
            )

    def _premium_stop_hit(self, pos: "LegPosition", current_premium: float) -> bool:
        threshold = pos.entry_px * 1.20 if pos.entry_px > 100 else pos.entry_px * 1.50
        return current_premium >= threshold

    # ---- entry / exit (single naked leg, resumable) -------------------------
    def _enter_leg(self, leg_key: str, inst: InstrumentConfig, option_type: str, spot: float,
                   entry_setup: str = ""):
        leg = self.store.state.legs[leg_key]
        strategy_tag = self.env.strategy_tag
        pos = leg.position

        if not pos.symbol:
            chain = self._chain_cache.get(inst.name)
            if chain is None:
                Log.warning(f"[{leg_key}] Entry signal fired but the option chain "
                            f"cache isn't populated yet -- skipping this cycle, "
                            f"will retry once the background refresh completes.")
                return
            # Staleness guard, ported from Nifty_Sensex_Pivot_Supertrend_Intraday_1's
            # _enter_leg: the chain cache is only refreshed on
            # indicator_refresh_interval's cadence (15s) -- if spot has moved
            # more than one listed strike since the cache was last populated,
            # the "ATM" anchor select_capped_strike() would walk from is
            # already stale. Force a fresh background refresh and skip this
            # cycle rather than silently selling a strike whose moneyness no
            # longer matches what the entry signal intended.
            strikes = sorted({l["strike"] for l in _legs_with_strike(chain, option_type)})
            if len(strikes) >= 2:
                strike_step = min(b - a for a, b in zip(strikes, strikes[1:]))
                nearest = min(strikes, key=lambda s: abs(s - spot))
                if abs(nearest - spot) > strike_step:
                    Log.warning(f"[{leg_key}] Cached option chain looks stale -- nearest strike "
                                f"{nearest} is more than one strike step ({strike_step}) from "
                                f"spot {spot}. Skipping this cycle and forcing a chain refresh.")
                    self._chain_cache.pop(inst.name, None)
                    return
            sel = select_capped_strike(chain, option_type, spot)
            if sel is None:
                Log.info(f"[{leg_key}] Entry signal fired but no strike in the "
                         f"premium band ({config.premium_filter_low}, {config.premium_filter_high}] "
                         f"was found (spot={spot}) -- skipping this cycle.")
                return
            leg_row, premium, used_fallback = sel
            quantity = config.lot_multiplier * leg_row["lotsize"]

            tag = " (fallback strike, ATM premium>=120)" if used_fallback else ""
            Log.info(f"[{leg_key}] Entry: strike={leg_row['strike']} symbol={leg_row['symbol']}"
                      f"@{premium} qty={quantity}{tag}")

            pos = LegPosition(
                symbol=leg_row["symbol"],
                quantity=quantity,
                entry_time=datetime.now(IST).isoformat(),
                entry_px=float(premium),
                execution_id=self.execution_id,
                entry_setup=entry_setup,
            )
            leg.position = pos
            self._save_state()
            self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])

        if pos.entry_filled or leg_key in self._pending_fills:
            return

        if not pos.entry_order_id:
            try:
                pos.entry_order_id = place(self.client, strategy_tag, pos.symbol,
                                            inst.options_exchange, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for entry: {exc}")
                self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_entry_fill, leg_key, inst, pos.entry_order_id, pos.symbol, pos.quantity
        )

    def _watch_entry_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                           symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill = poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                              "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.entry_order_id == order_id:
                pos.entry_filled = True
                fill_price = fill.get("average_price") or fill.get("price")
                if fill_price:
                    pos.entry_px = float(fill_price)
                leg.trade_count += 1
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled: {symbol}@{pos.entry_px}")
        except OrderNeedsAttention as exc:
            self._enter_error_mode(leg_key, "entry_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{leg_key}] Unexpected error while watching entry fill: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _enter_error_mode(self, leg_key: str, error_state: str, error_kind: str,
                          error_order_id: str, message: str):
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
        try:
            self._pnl_executor.submit(
                notify_telegram_error, self.env,
                f"[{config.strategy_name}] {leg_key} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
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

    def _push_leg_error_bg(self, leg_key: str, pos: "LegPosition", action: str = "", clear: bool = False):
        snapshot = copy.copy(pos)
        try:
            self._pnl_executor.submit(push_leg_error, self.env, leg_key, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"[{leg_key}] Failed to dispatch push_leg_error: {exc}")

    def _refresh_pending_action_bg(self, leg_key: str):
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

        self._pnl_executor.submit(_run)

    def _pop_pending_action(self, leg_key: str) -> Optional[dict]:
        return self._pending_action_cache.pop(leg_key, None)

    def _exit_leg(self, leg_key: str, inst: InstrumentConfig, reason: str = "unknown"):
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        if pos.exit_filled:
            self._finalize_exit(leg_key, inst, reason)
            return

        if leg_key in self._pending_fills:
            return

        if not pos.exit_order_id:
            try:
                pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                           inst.options_exchange, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for exit: {exc}")
                self._enter_error_mode(leg_key, "exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_exit_fill, leg_key, inst, pos.exit_order_id, pos.symbol, pos.quantity
        )

    def _watch_exit_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                          symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill = poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                              "BUY", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.exit_order_id == order_id:
                pos.exit_filled = True
                fill_price = fill.get("average_price") or fill.get("price")
                if fill_price:
                    pos.exit_fill_px = float(fill_price)
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
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        Log.info(f"[{leg_key}] Position closed: {pos.symbol}")

        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = pos.exit_fill_px
        if exit_px is None:
            exit_px = resolve_exit_ltp(self.price_stream, self.ltp_client, pos.symbol, inst.options_exchange)
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
            try:
                self._fill_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to dispatch notify_trade_closed: {exc}")
        else:
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self.price_stream.remove_instruments(
            [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
        )
        leg.position = LegPosition()
        self._save_state()

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    def _resolve_leg_error(self, leg_key: str, inst: InstrumentConfig, action: dict):
        if leg_key in self._pending_fills:
            return
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fills.add(leg_key)
            ack_pending_action(self.env, leg_key)
            self._fill_executor.submit(
                self._do_retry_resolution, leg_key, inst, was_exit, kind
            )
            return

        if action["action"] == "cancel":
            if was_exit:
                pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(leg_key, pos, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            if kind == "terminal":
                self.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
                )
                leg.position = LegPosition()
                self._save_state()
                self._push_leg_error_bg(leg_key, leg.position, clear=True)
                ack_pending_action(self.env, leg_key)
                return
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
        try:
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if was_exit:
                if kind == "resting":
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
                else:
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                push_leg_error(self.env, leg_key, pos, clear=True)
                self._pending_fills.discard(leg_key)
                return

            if kind == "resting":
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
            else:
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
            self._fill_executor.submit(
                self._watch_entry_fill, leg_key, inst, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{leg_key}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(leg_key)

    def _watch_entry_cancel(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                             symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             symbol, inst.options_exchange, "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.error_order_id != order_id:
                return
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
            push_leg_error(self.env, leg_key, leg.position, clear=True)
        except Exception as exc:
            Log.exception(f"[{leg_key}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    # ---- main cycle -----------------------------------------------------
    def _refresh_force_exit_check_bg(self):
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
                # Isolated per leg -- a raised exception closing NIFTY_PE must
                # never prevent NIFTY_CE from getting its own attempt this
                # same cycle (NRML has no broker-side backstop for either).
                try:
                    self._exit_leg(leg_key, inst, reason="force_exit")
                except Exception as exc:
                    Log.exception(f"[{leg_key}] Force Exit close attempt raised: {exc}")
        return all_flat

    def _force_close_stale_day_legs(self):
        """Safety net for a genuinely rare but real gap: _past_universal_exit()
        only trips from universal_exit_time to midnight of the CURRENT day,
        so a process that stays down across a day boundary while a leg was
        still open (or the day rolls over while errored) would otherwise
        never force-close it via that check alone. Any leg whose entry
        predates today is unconditionally overdue for a close regardless of
        the current wall-clock time -- NRML has no broker-side backstop to
        fall back on. Runs on every cycle, before the force-exit-pending and
        market-hours gates, so it fires the instant the process is up
        regardless of when that happens to be."""
        today = datetime.now(IST).date()
        for leg_key in LEG_KEYS:
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if not pos.symbol or not pos.entry_time:
                continue
            try:
                entry_date = datetime.fromisoformat(pos.entry_time).date()
            except ValueError:
                continue
            if entry_date >= today:
                continue
            if pos.error_state:
                Log.error(f"[{leg_key}] Leg from a prior day ({entry_date}) is still in "
                          f"error mode ({pos.error_state}) -- resolve it via Retry/Cancel/"
                          f"Manually Completed NOW.")
                continue
            inst_name = leg_key.split("_")[0]
            inst = next(i for i in INSTRUMENTS if i.name == inst_name)
            Log.warning(f"[{leg_key}] Leg from a prior day ({entry_date}) is still open -- "
                        f"force-closing immediately.")
            try:
                self._exit_leg(leg_key, inst, reason="stale_day_force_close")
            except Exception as exc:
                Log.exception(f"[{leg_key}] Stale-day force-close attempt raised: {exc}")

    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()

            # NRML has NO broker-side backstop -- this cross-day safety net
            # runs before every other gate below (including market hours),
            # so a leg left open from a prior day is force-closed the
            # instant the process is up, regardless of what time that is.
            self._force_close_stale_day_legs()

            past_universal_exit = self._past_universal_exit()

            # Universal exit: the ONLY thing that closes a same-day position
            # once past universal_exit_time -- must fire every day,
            # unconditionally, regardless of that leg's own technical exit
            # condition or the premium-stop check. Deliberately checked
            # BEFORE the market-hours gate below (past_universal_exit stays
            # True from universal_exit_time through midnight): a process
            # restarted after market_close (15:30) with a leg still open
            # must still reach this branch, not return early at the
            # market-hours check first.
            if past_universal_exit:
                for leg_key in LEG_KEYS:
                    leg = self.store.state.legs[leg_key]
                    inst_name = leg_key.split("_")[0]
                    inst = next(i for i in INSTRUMENTS if i.name == inst_name)
                    if leg.position.error_state:
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
                        # Isolated per leg -- one leg's exception must never
                        # starve every leg after it in this fixed-order loop;
                        # without this, a persistent bug on NIFTY_PE would
                        # leave NIFTY_CE open (no broker backstop) every
                        # cycle for the rest of the day.
                        try:
                            self._exit_leg(leg_key, inst, reason="eod_force_close")
                        except Exception as exc:
                            Log.exception(f"[{leg_key}] Universal-exit close attempt raised: {exc}")
                return

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
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

            within_entry = self._within_entry_window()

            for inst in INSTRUMENTS:
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

                self._update_running_extreme(inst.name, inst_ltp)
                running_low, running_high = self._get_running_extreme(inst.name)

                still_enterable = within_entry and any(
                    not self.store.state.legs[f"{inst.name}_{ot}"].position.symbol
                    for ot in ("PE", "CE")
                )
                # signal can legitimately be None (a client.history() outage
                # for the underlying) -- entry and the spot-based technical
                # exit conditions both need it and are skipped below when
                # it's missing, but the premium-based stop does NOT: it only
                # needs the OPTION leg's own live price, so a spot-data
                # outage must never leave an open naked short's premium stop
                # unmonitored.
                signal = self.get_signal(inst, ltp=inst_ltp, refresh_chain=still_enterable)

                # First pass: resolve which legs need a premium fetch at all
                # (open, no exit already committed/technically triggered),
                # then dispatch both option-leg fetches CONCURRENTLY via
                # _fill_executor rather than sequentially on this thread --
                # each fetch can take up to ltp_timeout (3s) on its REST
                # fallback, and running PE then CE one after another risked
                # ~6s of blocking on a 10s scheduler_interval when both legs'
                # WS ticks go stale in the same cycle.
                pending_premium_checks = {}
                exit_state = {}
                for option_type in ("PE", "CE"):
                    leg_key = f"{inst.name}_{option_type}"
                    leg = self.store.state.legs[leg_key]

                    if leg.position.error_state:
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        continue

                    if not leg.position.symbol:
                        if signal is None or not within_entry:
                            continue
                        if leg.trade_count >= config.max_trades_per_leg_per_day:
                            continue
                        matched_setup = self._entry_condition(option_type, signal, running_low, running_high)
                        if matched_setup is None:
                            continue
                        self._enter_leg(leg_key, inst, option_type, spot=signal.ltp, entry_setup=matched_setup)
                        continue

                    exit_already_committed = bool(leg.position.exit_order_id) or leg.position.exit_filled
                    exit_hit = exit_already_committed or (
                        signal is not None
                        and self._technical_exit_condition(option_type, signal, leg.position.entry_setup)
                    )
                    exit_state[leg_key] = [exit_hit, "technical_exit", exit_already_committed]
                    if not exit_hit:
                        pending_premium_checks[leg_key] = self._fill_executor.submit(
                            resolve_exit_ltp, self.price_stream, self.ltp_client,
                            leg.position.symbol, inst.options_exchange,
                        )

                for leg_key, future in pending_premium_checks.items():
                    try:
                        current_premium = future.result()
                    except Exception as exc:
                        Log.warning(f"[{leg_key}] Premium-stop fetch raised: {exc}")
                        continue
                    if current_premium is None:
                        continue
                    leg = self.store.state.legs[leg_key]
                    if self._premium_stop_hit(leg.position, current_premium):
                        exit_state[leg_key][0] = True
                        exit_state[leg_key][1] = "premium_stop"

                for leg_key, (exit_hit, exit_reason, exit_already_committed) in exit_state.items():
                    if not exit_hit:
                        continue
                    if not exit_already_committed:
                        Log.info(f"[{leg_key}] Exit condition met ({exit_reason}) -> closing.")
                    # inst here is still the enclosing `for inst in
                    # INSTRUMENTS` loop variable -- every leg_key in
                    # exit_state belongs to it (single instrument today, but
                    # keeps this correct if INSTRUMENTS ever grows).
                    self._exit_leg(leg_key, inst, reason=exit_reason)

        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            now = datetime.now(IST)
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                try:
                    self._pnl_executor.submit(
                        notify_telegram_error, self.env,
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
    print(f"Instruments          : {', '.join(i.name for i in INSTRUMENTS)}")
    print(f"Supertrend           : ({config.supertrend_period}, {config.supertrend_multiplier})")
    print(f"EMA                  : {config.ema_period}")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print(f"Product              : {config.product} (NO broker-side auto-square-off backstop)")
    print("NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK")
    if config.test_mode:
        print("TEST MODE ENABLED -- market-hours/entry-window checks are BYPASSED")
    print("=" * 70)


def main():
    print_banner()

    env = Environment()
    state_store = StateStore(env)
    state_store.load()

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
    # started needs its symbol subscribed immediately -- same resumability
    # guarantee as every other script in this project. Gated on
    # current_day == today (matching Nifty_Sensex_Pivot_Supertrend_Intraday_1)
    # -- this is a pure intraday strategy with NO legitimate overnight
    # position, so a leg still recorded open from a PRIOR day is stale by
    # definition; run_cycle's own _force_close_stale_day_legs() closes it on
    # the first cycle regardless (via REST fallback, since it was never
    # WS-subscribed here) rather than this startup path silently treating a
    # stale leftover the same as a genuine same-day resumption.
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

    for leg_key, leg in state_store.state.legs.items():
        if leg.position.error_state:
            action = "SELL" if leg.position.error_state == "entry_failed" else "BUY"
            push_leg_error(env, leg_key, leg.position, action=action)
            Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                      f"({leg.position.error_state}/{leg.position.error_kind}) -- "
                      f"needs Retry/Cancel/Manually Completed.")
            continue

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
    except Exception:
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        engine._pnl_executor.shutdown(wait=False)
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
