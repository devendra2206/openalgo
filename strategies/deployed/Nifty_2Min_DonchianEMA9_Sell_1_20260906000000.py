"""
===============================================================================
NIFTY 2-Min Donchian(10)/EMA(9) Intraday Option Seller
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Ported from : data23_to_26/backtest_nifty_2min_donchian_ema9_sell.py -- full
              2021-2026 backtest (corrected, 2026-09-08, shared cap=6/day,
              armed state resets at day boundary): 3,191 trades, net
              +Rs9,92,508.75, max drawdown -Rs44,677.25, 56.8% win rate.
              Every entry/exit condition below is a literal, unmodified port
              of that validated backtest's own logic -- nothing here is new
              or re-derived.

              The backtest needed THREE real bug fixes during verification
              before it was trustworthy:
              1. Overlapping trades from a missing position-block.
              2. A stale-price entry from a missing same-day freshness guard.
              3. (2026-09-06) NO entry cutoff at all let a confirmation fire
                 up to 15:31 -- AFTER the 15:15 universal exit time -- which
                 then computed an exit_ts EARLIER than its own entry_ts (a
                 chronologically impossible negative holding period).
                 Affected 427/3505 trades (12.2%) before the fix. The LIVE
                 script below was NEVER affected by bug #3: run_cycle's own
                 past_universal_exit branch returns before the entry-signal
                 path is ever reached once past 15:15, so no new entry could
                 structurally fire there -- but it had no explicit cutoff
                 BEFORE 15:15 either, so entry_end below is now pinned to
                 match the corrected backtest exactly (14:45), not left at
                 an unconfirmed guess.
              All three fixes are baked into the rules below.

Description
-----------
Pure INTRADAY naked option-SELLING strategy on NIFTY. Unlike this project's
other option-selling scripts, this one has only ONE position slot total (not
one per side) -- at most one of {CE, PE} can be open at any time, and no new
signal is even evaluated while a position is open. Max 6 trades/day, SHARED
across CE and PE combined (confirmed 2026-09-06 -- of shared-3/day,
per-side-3-each, and shared-6/day, this variant had the highest net-of-cost
PnL AND a shallower max drawdown than the per-side alternative).

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
No hedge leg -- protected only by the premium-based SL/target below plus the
unconditional 15:15 force-close. Product is MIS (intraday only): this
strategy's OWN 15:15 square-off AND the broker's MIS auto-square-off are both
active backstops (unlike this project's NRML LEAPS-style scripts, which have
no broker-side backstop at all).

Signal Rules (verbatim from the validated backtest)
------------------------------------------------------------------
Indicators, computed on a continuous 2-min NIFTY spot series (never reset per
day):
  - Donchian(10) channel and EMA(9) on 2-min Close.
  - IMPORTANT (confirmed live/backtest fidelity requirement): the Donchian
    channel used to evaluate a TRIGGER on candle i is built from the PRIOR 10
    CLOSED candles, EXCLUDING candle i itself. `ta.donchian()`'s own
    `highest()`/`lowest()` are INCLUSIVE rolling windows (today's own bar
    counts in its own 10-bar max/min) -- calling it on the full inclusive
    series would fire the trigger differently than backtested. This script
    always computes `ta.donchian(bars_excluding_latest["high"], ...)` (i.e.
    on the series with the just-closed candle dropped) and uses ITS last
    value as "today's" channel -- see `compute_donchian_signal`.

CALL SELL (top reversal):
  Trigger      : a closed 2-min candle's HIGH >= Donchian Upper(10) (as above).
  Confirmation : a LATER closed candle's CLOSE < EMA(9) -> SELL ATM CE at
                 that close.

PUT SELL (bottom reversal): mirror -- Trigger: LOW <= Donchian Lower(10).
  Confirmation: a later candle's CLOSE > EMA(9) -> SELL ATM PE.

Trigger validity / state machine (verbatim from the backtest):
  - Once armed, the strategy waits up to 20 closed 2-min candles for the
    EMA9 confirmation candle. No confirmation within that window disarms
    the setup back to idle.
  - A fresh SAME-side trigger while already armed for that side REFRESHES
    the 20-candle window (does not just no-op).
  - An OPPOSITE-side trigger while armed for one side CANCELS the pending
    side and arms the new one instead.
  - The FIRST candle of the trading day is never a valid TRIGGER (the
    indicators themselves are still computed continuously, no reset).
  - An ARMED (unconfirmed) setup does NOT carry across a day boundary
    (confirmed 2026-09-08): if a candle's date differs from the previous
    candle's, any pending armed_side is cleared before that candle's own
    trigger/confirm logic runs. Indicators (Donchian/EMA9) stay CONTINUOUS
    across days -- only the ARMED state resets. This applies even inside a
    multi-candle catch-up replay after a restart/outage, not just once at
    the top of a new day -- see compute_donchian_signal's own fix note.
    Backtest impact of this change: 3,318 -> 3,191 trades, but HIGHER win
    rate (56.5% -> 56.8%), HIGHER total PnL (Rs9,84,497 -> Rs9,92,509), and
    a SHALLOWER max drawdown (-Rs48,857 -> -Rs44,677) -- a clean win on
    every metric, not a tradeoff.
  - No arming/confirmation is evaluated AT ALL while a position is open --
    this alone satisfies "no overlapping trades" / "opposite-side only
    after close" (this is also the fix for the overlapping-trade bug the
    backtest itself had before verification).

Strike selection: ATM = nearest listed strike to live spot. If ATM premium <
Rs100: step ITM (CE: lower strikes; PE: higher strikes) to the nearest listed
strike whose premium > Rs100. If nothing clears the floor, fall back to the
ATM strike itself (never skip the entry outright) -- matches the corrected
backtest's own fallback exactly. See `select_itm_floor_strike()`.

Exit (continuous, either true -> close):
  SL     : current premium >= entry_premium * 1.30
  Target : current premium <= entry_premium * 0.70
  Both   : unconditional force-close at 15:15 regardless of P&L.

Expiry: nearest weekly >= today, rolled to the NEXT weekly if today itself is
that expiry ("on expiry move to next day", confirmed) -- this is the
repo-standard `resolve_current_week_expiry()`, reused verbatim from
Nifty_5Min_SupertrendEMA_PivotSell_1_20260829000000.py.

Shared rules
--------------------------------
  - Entry window: 09:17:00 - 14:45:00 IST (09:17 skips the literal first
    2-min candle 09:15-09:17 as a valid TRIGGER candle, matching "never use
    the first candle of the day"; 14:45 is the confirmed entry cutoff --
    see bug #3 above -- no NEW entry confirmation is accepted at/after this
    time, though an already-armed setup can still expire/cancel normally).
  - Universal exit: >= 15:15 -- force-close the open position unconditionally.
  - Max 6 trades/day, SHARED across CE and PE (confirmed 2026-09-06 -- a
    single counter, not one per side; beat both shared-3/day and
    per-side-3-each on net-of-cost PnL and drawdown in the sweep).
  - Quantity: 1 lot (config.lot_multiplier). Product: MIS.

Live-specific machinery (ported from this project's other live scripts, not
from the backtest -- the backtest has no notion of order placement, WS feeds,
or process restarts; identical to Nifty_5Min_SupertrendEMA_PivotSell's own
machinery, nothing here is strategy-specific)
------------------------------------------------------------------------
  - Live price feed: WebSocket (`PriceStream`) for NIFTY spot, background
    watchdog reconnect/resubscribe.
  - Candle/indicator state: re-fetched via `client.history(interval="1m")`
    (NOT "2m" -- confirmed live 2026-09-07 that "2m" is not a broker-supported
    interval; 1m bars are bucketed into 2-min bars locally via
    `resample_to_bars()`, anchored to 09:15) on each candle-boundary-triggered
    refresh (NOT a tick-driven in-memory bucket). EFFICIENT-API-CALLS
    requirement (explicit ask): this fetch only happens when
    `_current_candle_boundary(2)` has genuinely advanced past the cached
    signal's own candle_key (itself anchored to 09:15, matching
    resample_to_bars' anchor -- see that function's fix note) -- never on a
    plain timer, never redundantly within the same still-open candle.
  - `client.optionchain()` is called ONLY from inside `_enter_position`'s
    strike resolution -- never speculatively, never per-cycle. One call
    (`strike_count=15`) covers ATM lookup AND the up-to-10-step ITM
    fallback walk from the SAME response -- no second broker call.
  - LTP for the exit-side premium check follows the same
    WS-first/REST-fallback/two-sided-quote-sanity-check chain as every
    other script in this project: `price_stream.get_ltp()` ->
    `fetch_symbol_ltp(..., require_two_sided=True)`. No LTP fetch happens
    at all while flat with nothing armed pending an option trade --
    exit-condition checks only run for an OPEN position.
  - Order placement robustness, resumable entry/exit, order error recovery
    (Retry/Cancel/Manually Completed), the async CSV trade log, PnL
    reporting, and the platform error/force-exit HTTP surface are all
    copied verbatim from Nifty_5Min_SupertrendEMA_PivotSell_1_20260829000000.py.

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.donchian(high, low, period)` returns `(upper, middle, lower)`, backed
    by INCLUSIVE rolling `highest()`/`lowest()` -- see the fidelity note
    above; this script always calls it on the series with the latest
    (still-being-evaluated) candle dropped.
  * `ta.ema(data, period)` returns the EMA array, same convention as every
    other script in this project.
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
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

# See Nifty_5Min_SupertrendEMA_PivotSell_1_20260829000000.py's own comment
# for the full rationale -- must be called before any thread is created.
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


INSTRUMENT = InstrumentConfig(name="NIFTY", underlying_exchange="NSE_INDEX", options_exchange="NFO")

# Single strategy-wide position slot -- see module docstring. Kept as a
# one-element "leg key" list purely so the error-push/pending-action/state
# JSON shapes match every other script in this project (and its consumers
# on the reporting side) without inventing a new shape for a single leg.
LEG_KEY = "NIFTY"


@dataclass
class Config:
    strategy_name: str = "NIFTY 2-Min Donchian(10)/EMA(9) Intraday Seller"
    version: str = "1.0.0"

    intraday_interval: str = "2m"      # logical bucket size -- for LOGGING only, see candle_interval_fetch
    candle_interval_fetch: str = "1m"  # "2m" is NOT a supported broker interval (confirmed 2026-09-07) --
                                        # fetch 1m and bucket into 2-min bars locally, see resample_to_bars()
    history_lookback_days: int = 3    # 2m bars: a few days easily covers Donchian(10)/EMA(9) warmup + 20-candle window
    bar_minutes: int = 2
    donchian_period: int = 10
    ema_period: int = 9
    confirmation_max_candles: int = 20

    lot_multiplier: int = 1           # number of lots
    max_trades_per_day: int = 6       # SHARED across CE+PE (confirmed 2026-09-06 -- see module docstring)

    min_premium: float = 100.0
    max_itm_steps: int = 10           # bounded to fit inside one optionchain() response
    strike_count: int = 15            # optionchain() scan width for ATM + ITM-fallback walk

    sl_mult: float = 1.30
    target_mult: float = 0.70

    product: str = "MIS"              # intraday only -- own 15:15 close + broker MIS auto-square-off both active
    price_type: str = "MARKET"

    entry_start: time = time(9, 17)   # skips the literal first 2-min candle (09:15-09:17) as a trigger
    entry_end: time = time(14, 45)    # confirmed 2026-09-06 -- matches the corrected backtest's ENTRY_END_TIME
    universal_exit_time: time = time(15, 15)   # force-close unconditionally at/after this
    market_close: time = time(15, 30)

    # Indicator/candle state is entirely candle-boundary-driven (not a plain
    # timer) -- see get_signal(). scheduler_interval is the LTP/exit-check
    # cadence; indicator_refresh_interval paces RETRIES against the SAME
    # closed-candle boundary (ported from MCX_CrudeOil_EMA34_RSI_ADX /
    # Nifty_5Min_SupertrendEMA_PivotSell's identical mechanism) so a
    # client.history() outage doesn't refetch every single scheduler tick.
    scheduler_interval: int = 10
    indicator_refresh_interval: int = 15
    pnl_tick_interval: float = 0.8

    ws_stale_seconds: float = 20.0
    ws_stale_seconds_open: float = 60.0
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
    option_type: str = ""            # "" | "CE" | "PE" -- which side is currently open


@dataclass
class StrategyState:
    current_day: str = ""
    trade_count: int = 0   # SHARED across CE+PE (confirmed 2026-09-06)
    position: LegPosition = field(default_factory=LegPosition)
    # Armed-state machine, persisted across restarts so a mid-window arm
    # survives a process bounce.
    armed_side: str = ""              # "" | "CE" | "PE"
    armed_countdown: int = 0
    armed_since_candle_key: str = ""
    last_processed_candle_key: str = ""
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
            or "nifty_2min_donchian_ema9_sell"
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
    """Start-of-bucket timestamp for the current wall-clock candle.

    2026-09-07 fix: MUST anchor to the 09:15 session open, matching
    resample_to_bars()'s own anchor (`session_start = idx[0].normalize() +
    Timedelta(hours=9, minutes=15)`) -- NOT midnight. 09:15 is minute 555 of
    the day (odd), so every real 2-min bucket start (555, 557, 559, ...) is
    an ODD minute; a midnight-anchored `(total_minutes // interval) *
    interval` is always EVEN for interval=2, so it could never equal a real
    bucket's start. This is the exact bug already found and fixed live in
    Nifty_Sensex_VWAP_NoHA_Intraday_1 (2026-08-13): due_signal evaluated
    True on essentially every scheduler tick instead of once per real
    2-minute candle, since cached_boundary (from a real, odd-minute
    candle_key) could never satisfy `cached_boundary >= current_boundary`
    against a midnight-anchored value. Confirmed present here too before
    this fix -- 5-min buckets (the donor this was copied from) don't hit
    it, since 555 is evenly divisible by 5, but 2-min buckets do."""
    now = datetime.now(IST)
    total_minutes = now.hour * 60 + now.minute
    session_open_minutes = 9 * 60 + 15  # 555 -- must match resample_to_bars' 09:15 anchor
    minutes_since_open = total_minutes - session_open_minutes
    bucket_offset = (minutes_since_open // interval_minutes) * interval_minutes
    bucket_start_minutes = session_open_minutes + bucket_offset
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=bucket_start_minutes)


def _candle_key_boundary(candle_key: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(candle_key)
    except (ValueError, TypeError):
        return None


def _strip_tz(dt: datetime) -> datetime:
    """Normalizes to a naive datetime regardless of whether `dt` came in
    tz-aware or tz-naive -- see compute_donchian_signal's own fix note
    (2026-09-08): client.history() returns tz-aware (+05:30) timestamps in
    production, which unit-test fixtures (built from naive pd.to_datetime())
    never exercised, so an asymmetric aware-vs-naive comparison only
    surfaced live."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


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
    """Same mechanism as Nifty_5Min_SupertrendEMA_PivotSell's PriceStream --
    see that script's own docstring for the full rationale behind the
    watchdog/reconnect/per-symbol-resubscribe design; nothing here is
    strategy-specific."""

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
        self.state.trade_count = data.get("trade_count", 0)
        self.state.armed_side = data.get("armed_side", "")
        self.state.armed_countdown = data.get("armed_countdown", 0)
        self.state.armed_since_candle_key = data.get("armed_since_candle_key", "")
        self.state.last_processed_candle_key = data.get("last_processed_candle_key", "")
        self.state.last_updated = data.get("last_updated", "")
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.last_execution_id = data.get("last_execution_id", 0)
        pos_raw = data.get("position", {})
        self.state.position = LegPosition(**{**asdict(LegPosition()), **filter_known_fields(LegPosition, pos_raw)})
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "trade_count": self.state.trade_count,
            "armed_side": self.state.armed_side,
            "armed_countdown": self.state.armed_countdown,
            "armed_since_candle_key": self.state.armed_since_candle_key,
            "last_processed_candle_key": self.state.last_processed_candle_key,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_execution_id": self.state.last_execution_id,
            "position": asdict(self.state.position),
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
    own expiry day itself, when it rolls to NEXT week's expiry instead
    ("on expiry move to next day", confirmed). Repo-standard live function,
    reused verbatim from Nifty_5Min_SupertrendEMA_PivotSell."""
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
    trusting the quote. Pass True for TRADABLE-instrument reads (option
    legs); leave False for the underlying INDEX (legitimately no bid/ask).
    Verbatim from every other script in this project -- see
    docs/CUSTOMIZATIONS.md."""
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
    """WS-first, REST-with-sanity-check-fallback LTP resolution for the
    option leg's own live price -- verbatim from every other script in this
    project. `max_age` defaults to the FIXED config.ws_stale_seconds (20s),
    not the widened post-open-grace threshold, since this feeds a trading
    decision (the SL/target check) or a booked realized-PnL price."""
    if max_age is None:
        max_age = config.ws_stale_seconds
    px = price_stream.get_ltp(symbol, exchange, max_age=max_age)
    if px is not None:
        return px
    return fetch_symbol_ltp(ltp_client, symbol, exchange, require_two_sided=True)


@dataclass
class DonchianSignal:
    donchian_upper: float     # from the PRIOR 10 closed candles, excluding the latest
    donchian_lower: float
    last_close: float
    last_ema9: float
    candle_key: str           # ISO timestamp of the latest CLOSED candle this signal reflects
    ltp: float
    pending_entry_side: str = ""   # "" | "CE" | "PE" -- set when confirmation just fired on the LATEST candle


_last_logged_candle: Optional[str] = None


def compute_donchian_signal(intraday: pd.DataFrame, state: StrategyState, ltp: Optional[float]) -> Optional[DonchianSignal]:
    """Advances the arm/confirm/countdown state machine over every NEWLY
    CLOSED candle since `state.last_processed_candle_key` (in chronological
    order -- not just the single latest bar), so a refresh cadence that
    lags behind actual candle closes (broker delay, a process restart)
    never silently skips an intermediate arm/confirm/expire/cancel event.
    An entry is only ever DISPATCHED (pending_entry_side set) if the
    confirming candle is the LATEST one in this batch -- older candles in a
    catch-up batch still advance the state machine (so armed_side/countdown
    stay accurate) but never fire a stale, already-passed entry.

    Mutates `state` in place (armed_side/armed_countdown/
    armed_since_candle_key/last_processed_candle_key) -- caller is
    responsible for persisting it."""
    global _last_logged_candle

    if intraday is None or intraday.empty:
        return None
    # Drop the still-forming last candle -- never trade on an unsettled bar.
    if len(intraday) >= 2:
        intraday = intraday.iloc[:-1]
    period = config.donchian_period
    ema_p = config.ema_period
    if len(intraday) < period + ema_p + 2:
        Log.warning(
            f"[NIFTY] only {len(intraday)} {config.intraday_interval} bars after dropping "
            f"the still-forming one -- need >= {period + ema_p + 2} for warmup, no signal."
        )
        return None

    closes = intraday["close"].to_numpy()
    highs = intraday["high"].to_numpy()
    lows = intraday["low"].to_numpy()
    ema_arr = np.asarray(ta.ema(intraday["close"], ema_p))

    # Donchian channel EXCLUDING the candle it's being compared against --
    # see module docstring's fidelity note. donch_upper[i]/donch_lower[i]
    # below are built from bars up to i-1 (via the shifted-by-one series),
    # so ta.donchian()'s own INCLUSIVE rolling window never counts candle i
    # in its own trigger check. ONE call covers both upper and lower.
    shifted_high = np.concatenate(([np.nan], highs[:-1]))
    shifted_low = np.concatenate(([np.nan], lows[:-1]))
    donch_upper_arr, _mid, donch_lower_arr = ta.donchian(shifted_high, shifted_low, period)
    donch_upper_arr = np.asarray(donch_upper_arr)
    donch_lower_arr = np.asarray(donch_lower_arr)

    # 2026-09-09 fix: confirmed live -- the "never a trigger" exemption
    # must protect the SESSION-OPEN candle specifically (its own timestamp
    # == 09:15, matching resample_to_bars'/_current_candle_boundary's own
    # anchor), NOT "whichever row happens to be first for that calendar
    # date". A stray pre-open broker tick (confirmed live: real 1-min
    # prints at 09:13/09:14, before NSE's 09:15 open -- practically absent
    # from the historical backtest dataset, 1 row in ~1.5M) becomes its own
    # earlier same-day row, so a date-groupby "first row" check silently
    # hands the exemption to THAT spurious bar instead of the real 09:15
    # candle -- exactly what happened: a PE trigger fired on the actual
    # 09:15 opening candle and a live trade was placed from it, the one
    # candle this rule exists specifically to exclude.
    is_first_bar_of_day = np.array([ts.time() == time(9, 15) for ts in intraday.index])

    last_processed_key = state.last_processed_candle_key
    last_processed_boundary = _candle_key_boundary(last_processed_key) if last_processed_key else None

    n = len(intraday)
    # 2026-09-08 fix: confirmed live -- `TypeError: can't compare
    # offset-naive and offset-aware datetimes`. intraday.index[i] came back
    # from client.history() as tz-AWARE (+05:30), while ONLY the left side
    # was being stripped to naive via .replace(tzinfo=None) -- an
    # asymmetric strip that never surfaced in unit tests (whose fixtures
    # build naive timestamps on both sides). Normalize BOTH sides to naive
    # before comparing so this holds regardless of which convention
    # client.history() happens to use.
    new_bar_idxs = [
        i for i in range(n)
        if last_processed_boundary is None
        or _strip_tz(intraday.index[i].to_pydatetime()) > _strip_tz(last_processed_boundary)
    ]
    if not new_bar_idxs:
        # No genuinely new closed candle since last time -- return the
        # cached view (still fresh LTP) without touching the state machine.
        candle_key = str(intraday.index[-1])
        return DonchianSignal(
            donchian_upper=float(donch_upper_arr[-1]) if not np.isnan(donch_upper_arr[-1]) else float("nan"),
            donchian_lower=float(donch_lower_arr[-1]) if not np.isnan(donch_lower_arr[-1]) else float("nan"),
            last_close=float(closes[-1]), last_ema9=float(ema_arr[-1]),
            candle_key=candle_key, ltp=ltp if ltp is not None else float(closes[-1]),
            pending_entry_side="",
        )

    pending_entry_side = ""
    for i in new_bar_idxs:
        if i > 0 and intraday.index[i].date() != intraday.index[i - 1].date():
            # 2026-09-08 confirmed with user: an armed-but-unconfirmed setup
            # must NOT carry across a day boundary -- indicators (Donchian/
            # EMA9) stay CONTINUOUS (unchanged), only the ARMED state
            # resets. This check must live HERE, inside the per-candle
            # replay loop, not just in the engine's once-a-cycle
            # _reset_day_if_needed() -- a multi-day catch-up batch (e.g.
            # after an outage) replays PRIOR days' candles through this
            # same loop, and without this check their trigger/confirm
            # events would re-arm state.armed_side from stale, previous-day
            # price action, silently undoing the fresh reset that already
            # ran before this replay started (confirmed live 2026-09-08:
            # exactly this sequence left the strategy armed=CE at today's
            # open from a trigger on 2026-09-07's candles).
            if state.armed_side:
                Log.info(f"[NIFTY] Day boundary crossed ({intraday.index[i-1].date()} -> "
                         f"{intraday.index[i].date()}) -- clearing carried-over armed state "
                         f"({state.armed_side}).")
            state.armed_side = ""
            state.armed_countdown = 0
            state.armed_since_candle_key = ""

        up, low_band, ema_val = donch_upper_arr[i], donch_lower_arr[i], ema_arr[i]
        close_px, high_px, low_px = float(closes[i]), float(highs[i]), float(lows[i])
        is_latest = (i == n - 1)

        # --- confirmation check (only meaningful if already armed BEFORE
        # this bar) ---
        confirmed_side = ""
        if state.armed_side == "CE" and close_px < ema_val:
            confirmed_side = "CE"
        elif state.armed_side == "PE" and close_px > ema_val:
            confirmed_side = "PE"

        if confirmed_side:
            candle_time = intraday.index[i].time()
            if candle_time >= config.entry_end:
                # 2026-09-06 fix: without this, a confirmation could fire
                # AFTER the universal exit time -- see module docstring's
                # bug #3 (427/3505 backtest trades affected before the fix).
                Log.info(f"[NIFTY] {confirmed_side} confirmation at/after entry cutoff "
                         f"({config.entry_end}) -- skipping, not dispatching an entry.")
            elif state.trade_count >= config.max_trades_per_day:
                Log.info(f"[NIFTY] {confirmed_side} confirmation but the SHARED daily trade cap "
                         f"({config.max_trades_per_day}) is already reached -- skipping.")
            elif is_latest:
                pending_entry_side = confirmed_side
            else:
                Log.warning(
                    f"[NIFTY] {confirmed_side} confirmation matched on a CATCH-UP candle "
                    f"({intraday.index[i]}), not the latest -- state advanced but no stale "
                    f"entry dispatched (will need a fresh trigger/confirmation to trade)."
                )
            state.armed_side = ""
            state.armed_countdown = 0
            state.armed_since_candle_key = ""
            continue

        # --- countdown / disarm ---
        if state.armed_side:
            state.armed_countdown -= 1
            if state.armed_countdown <= 0:
                Log.info(f"[NIFTY] {state.armed_side} confirmation window expired "
                         f"({config.confirmation_max_candles} candles) -- disarming.")
                state.armed_side = ""
                state.armed_countdown = 0
                state.armed_since_candle_key = ""

        # --- trigger check (skip the first candle of the day) ---
        if not is_first_bar_of_day[i] and not np.isnan(up) and not np.isnan(low_band):
            ce_trigger = high_px >= up
            pe_trigger = low_px <= low_band
            candle_ts = str(intraday.index[i])
            if ce_trigger:
                if state.armed_side == "PE":
                    Log.info(f"[NIFTY] CE trigger @ {candle_ts} cancels pending PE armed-state -- switching to CE.")
                elif state.armed_side == "CE":
                    Log.info(f"[NIFTY] CE trigger @ {candle_ts} again while armed -- refreshing "
                              f"{config.confirmation_max_candles}-candle wait.")
                state.armed_side = "CE"
                state.armed_countdown = config.confirmation_max_candles
                state.armed_since_candle_key = candle_ts
            elif pe_trigger:
                if state.armed_side == "CE":
                    Log.info(f"[NIFTY] PE trigger @ {candle_ts} cancels pending CE armed-state -- switching to PE.")
                elif state.armed_side == "PE":
                    Log.info(f"[NIFTY] PE trigger @ {candle_ts} again while armed -- refreshing "
                              f"{config.confirmation_max_candles}-candle wait.")
                state.armed_side = "PE"
                state.armed_countdown = config.confirmation_max_candles
                state.armed_since_candle_key = candle_ts

    candle_key = str(intraday.index[-1])
    state.last_processed_candle_key = candle_key

    if _last_logged_candle != candle_key:
        _last_logged_candle = candle_key
        Log.info(
            f"[NIFTY] candle={candle_key} C={closes[-1]:.2f} DCU={donch_upper_arr[-1]:.2f} "
            f"DCL={donch_lower_arr[-1]:.2f} EMA9={ema_arr[-1]:.2f} armed={state.armed_side or 'none'} "
            f"countdown={state.armed_countdown}"
        )

    return DonchianSignal(
        donchian_upper=float(donch_upper_arr[-1]),
        donchian_lower=float(donch_lower_arr[-1]),
        last_close=float(closes[-1]), last_ema9=float(ema_arr[-1]),
        candle_key=candle_key, ltp=ltp if ltp is not None else float(closes[-1]),
        pending_entry_side=pending_entry_side,
    )


def resample_to_bars(df: pd.DataFrame, bucket_minutes: int) -> pd.DataFrame:
    """Buckets 1m bars into `bucket_minutes`-minute bars anchored at the
    09:15 session start (matching _current_candle_boundary's own anchor).

    2026-09-07 fix: "2m" is NOT a standard OpenAlgo interval -- confirmed
    live, client.history(interval="2m") returns HTTP 500 ("Unsupported
    interval '2m'. Supported intervals are: 1m, 3m, 5m, 10m, 15m, 30m, 1h,
    2h, 4h, D"). Every history fetch was failing before this fix, so the
    strategy could never get a signal at all. The fix fetches the
    broker-supported "1m" interval and buckets it into 2-min bars locally
    instead -- mirrors Nifty_Sensex_VWAP_NoHA_Intraday_1's own
    resample_to_bars() (that script hit this exact same "2m unsupported"
    problem first and already solved it this way).

    Does NOT drop the still-forming last bucket -- compute_donchian_signal
    already does that itself; dropping it here too would silently discard
    one extra real candle every refresh."""
    if df is None or df.empty:
        return df
    idx = pd.to_datetime(df.index)
    session_start = idx[0].normalize() + pd.Timedelta(hours=9, minutes=15)
    cols = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df.set_index(idx).resample(
        f"{bucket_minutes}min", label="left", closed="left", origin=session_start
    ).agg(cols).dropna()


def fetch_chain(client, inst: InstrumentConfig, expiry: str):
    """strike_count=15 (confirmed) -- comfortably under Shoonya's 20-symbol
    batching boundary (avoids the 1s inter-batch rate-limit delay) while
    giving enough room for the up-to-10-step ITM fallback walk.
    with_quotes=True is required -- select_itm_floor_strike() reads each
    leg's own premium."""
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


def select_itm_floor_strike(chain: dict, option_type: str, spot: float) -> Optional[tuple]:
    """Direct live port of the corrected backtest's own
    `_select_strike_with_premium_floor()`: ATM (nearest listed strike to
    spot); if its premium < config.min_premium, step ITM (CE: lower
    strikes; PE: higher strikes) to the nearest listed strike whose premium
    > config.min_premium (bounded to config.max_itm_steps). If nothing
    clears the floor, fall back to the ATM strike itself -- never skip the
    entry outright, matching the backtest's own fallback exactly. Walks
    within the SAME cached chain response -- no second broker call needed.
    Returns (leg_dict, premium, stepped_itm) or None if the ATM leg itself
    has no readable premium at all."""
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
    if atm_premium > config.min_premium:
        return atm_leg, atm_premium, False

    step = -1 if option_type == "CE" else 1  # CE: step down (ITM); PE: step up (ITM)
    idx = atm_idx + step
    steps_taken = 0
    while 0 <= idx < len(legs) and steps_taken < config.max_itm_steps:
        leg = legs[idx]
        premium = _premium(leg)
        if premium is not None and premium > config.min_premium:
            return leg, premium, True
        idx += step
        steps_taken += 1

    # nothing cleared the floor -- fall back to the ATM strike itself
    return atm_leg, atm_premium, False


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
        self._signal_cache: Optional[DonchianSignal] = None
        self._last_indicator_refresh: Optional[datetime] = None
        self._ws_fallback_logged = False
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._last_error_push: Optional[datetime] = None
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._fill_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fillwatch")
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bgcheck")
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        self._expiry_cache: Optional[str] = None
        self._chain_cache: Optional[dict] = None
        # Which candle_key self._chain_cache was actually successfully
        # populated for -- ported from Nifty_5Min_SupertrendEMA_PivotSell's
        # identical fix (confirmed live there 2026-08-31): without this,
        # get_signal's due_signal retries re-fetch the chain on every
        # retry too, not just the indicator.
        self._chain_cache_candle_key: Optional[str] = None
        # Paces due_signal retries against the SAME last_closed_boundary.
        self._indicator_cooldown_boundary: Optional[datetime] = None
        self._signal_refresh_pending: bool = False

    def _save_state(self):
        with self._state_lock:
            self.store.save()

    def _refresh_chain_cache(self) -> bool:
        """Returns True if self._chain_cache was actually updated (fetch
        succeeded), False otherwise."""
        try:
            expiry = self._expiry_cache
            if expiry is None:
                expiry = resolve_current_week_expiry(self.client, INSTRUMENT)
                self._expiry_cache = expiry
            self._chain_cache = fetch_chain(self.client, INSTRUMENT, expiry)
            return True
        except Exception as exc:
            Log.warning(f"[NIFTY] Background chain refresh failed (will retry live at "
                        f"entry if needed): {exc}")
            return False

    def _refresh_signal_chain_bg(self, ltp: Optional[float], refresh_chain: bool):
        """Runs on _fill_executor -- makes get_signal's periodic refresh
        genuinely non-blocking. Chain refresh is skipped when
        self._chain_cache was already SUCCESSFULLY populated for the
        "reference" candle_key -- same fix as Nifty_5Min_SupertrendEMA_PivotSell's
        _refresh_signal_chain_bg (efficient-API-calls requirement: avoids
        refetching optionchain() on every due_signal retry, not just once
        per genuinely new candle)."""
        previous = self._signal_cache
        fresh = None
        try:
            end = datetime.now(IST).date()
            raw = self.client.history(
                symbol=INSTRUMENT.name, exchange=INSTRUMENT.underlying_exchange,
                interval=config.candle_interval_fetch,
                start_date=(end - timedelta(days=config.history_lookback_days)).isoformat(),
                end_date=end.isoformat(),
            )
            if _is_error_response(raw):
                Log.warning(f"[NIFTY] {config.candle_interval_fetch} history error response: {raw}")
            elif raw is None or raw.empty:
                Log.warning(f"[NIFTY] empty {config.candle_interval_fetch} history.")
            else:
                intraday = resample_to_bars(raw, config.bar_minutes)
                fresh = compute_donchian_signal(intraday, self.store.state, ltp)
                if fresh is not None:
                    self._signal_cache = fresh
                    self._last_indicator_refresh = datetime.now(IST)
                    self._save_state()  # persist armed_side/countdown/last_processed_candle_key
        except Exception as exc:
            Log.warning(f"[NIFTY] Background indicator refresh failed (will retry "
                        f"next cycle): {exc}")

        if refresh_chain:
            reference_signal = fresh if fresh is not None else previous
            reference_candle_key = reference_signal.candle_key if reference_signal is not None else None
            chain_already_current = (
                reference_candle_key is not None
                and self._chain_cache_candle_key == reference_candle_key
            )
            if not chain_already_current:
                if self._refresh_chain_cache() and reference_candle_key is not None:
                    self._chain_cache_candle_key = reference_candle_key
        self._signal_refresh_pending = False

    def get_signal(self, ltp: Optional[float] = None, refresh_chain: bool = False) -> Optional[DonchianSignal]:
        """Cached, throttled indicator fetch -- EFFICIENT-API-CALLS
        requirement: client.history() is refetched only when a genuinely
        new closed candle boundary has arrived, never on a plain timer,
        never redundantly within the same still-open candle."""
        now = datetime.now(IST)

        last = self._last_indicator_refresh
        current_boundary = _current_candle_boundary(config.bar_minutes)
        cached_boundary = (
            _candle_key_boundary(self._signal_cache.candle_key)
            if self._signal_cache is not None else None
        )
        # candle_key is the START of the last CLOSED bar, so it always lags
        # current_boundary (start of the currently-forming bar) by exactly
        # one bar_minutes interval.
        last_closed_boundary = current_boundary - timedelta(minutes=config.bar_minutes)
        have_current_candle = cached_boundary is not None and cached_boundary >= last_closed_boundary
        due_signal = last is None or not have_current_candle

        cooldown_boundary = self._indicator_cooldown_boundary
        cooldown_elapsed = (
            last is None
            or cooldown_boundary != last_closed_boundary
            or (now - last).total_seconds() >= config.indicator_refresh_interval
        )

        if due_signal and cooldown_elapsed and not self._signal_refresh_pending:
            self._signal_refresh_pending = True
            self._indicator_cooldown_boundary = last_closed_boundary
            self._fill_executor.submit(self._refresh_signal_chain_bg, ltp, refresh_chain)

        cached = self._signal_cache
        if cached is None:
            return None
        if ltp is not None:
            cached.ltp = ltp
        return cached

    # ---- state helpers -----------------------------------------------------
    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.store.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily trade counter and armed state.")
            self.store.state.current_day = today_key
            self.store.state.today_realized_pnl = 0.0
            self.store.state.trade_count = 0
            self.store.state.armed_side = ""
            self.store.state.armed_countdown = 0
            self.store.state.armed_since_candle_key = ""
            self._expiry_cache = None
            self._chain_cache = None
            self._save_state()

    def _increment_trade_count(self, option_type: str = ""):
        """SHARED counter increment (confirmed 2026-09-06) -- only on a
        CONFIRMED entry fill, never on mere placement, so a rejected/retried
        entry doesn't consume a cap slot (same rule as every donor script).
        `option_type` is accepted (unused) purely so every call site's
        shape stays identical regardless of which cap design is active."""
        self.store.state.trade_count += 1

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
            pos = self.store.state.position
            if pos.symbol and pos.entry_filled:
                current_px = self.price_stream.get_ltp(
                    pos.symbol, INSTRUMENT.options_exchange, max_age=_current_ws_stale_threshold()
                )
                if current_px is not None:
                    pnl = (pos.entry_px - current_px) * pos.quantity  # short leg
                    open_positions.append({
                        "leg_key": LEG_KEY, "symbol": pos.symbol, "direction": "SHORT",
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

    def _sl_or_target_hit(self, pos: "LegPosition", current_premium: float) -> Optional[str]:
        if current_premium >= pos.entry_px * config.sl_mult:
            return "sl_hit"
        if current_premium <= pos.entry_px * config.target_mult:
            return "target_hit"
        return None

    # ---- entry / exit (single naked position, resumable) -------------------
    def _enter_position(self, option_type: str, spot: float):
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag

        if not pos.symbol:
            chain = self._chain_cache
            if chain is None:
                Log.warning(f"[{LEG_KEY}] Entry signal fired but the option chain "
                            f"cache isn't populated yet -- skipping this cycle, "
                            f"will retry once the background refresh completes.")
                return
            # Staleness guard, ported from Nifty_5Min_SupertrendEMA_PivotSell's
            # _enter_leg: the chain cache is only refreshed on
            # indicator_refresh_interval's cadence -- if spot has moved more
            # than one listed strike since the cache was last populated,
            # the "ATM" anchor select_itm_floor_strike() would walk from is
            # already stale.
            strikes = sorted({l["strike"] for l in _legs_with_strike(chain, option_type)})
            if len(strikes) >= 2:
                strike_step = min(b - a for a, b in zip(strikes, strikes[1:]))
                nearest = min(strikes, key=lambda s: abs(s - spot))
                if abs(nearest - spot) > strike_step:
                    Log.warning(f"[{LEG_KEY}] Cached option chain looks stale -- nearest strike "
                                f"{nearest} is more than one strike step ({strike_step}) from "
                                f"spot {spot}. Skipping this cycle and forcing a chain refresh.")
                    self._chain_cache = None
                    return
            sel = select_itm_floor_strike(chain, option_type, spot)
            if sel is None:
                Log.info(f"[{LEG_KEY}] Entry signal fired but the ATM {option_type} leg has "
                         f"no readable premium (spot={spot}) -- skipping this cycle.")
                return
            leg_row, premium, stepped_itm = sel
            quantity = config.lot_multiplier * leg_row["lotsize"]

            tag = f" (stepped ITM, ATM premium <= Rs{config.min_premium:.0f})" if stepped_itm else ""
            Log.info(f"[{LEG_KEY}] Entry: {option_type} strike={leg_row['strike']} "
                      f"symbol={leg_row['symbol']}@{premium} qty={quantity}{tag}")

            pos = LegPosition(
                symbol=leg_row["symbol"],
                quantity=quantity,
                entry_time=datetime.now(IST).isoformat(),
                entry_px=float(premium),
                execution_id=self.execution_id,
                option_type=option_type,
            )
            self.store.state.position = pos
            self._save_state()
            self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": INSTRUMENT.options_exchange}])

        if pos.entry_filled or LEG_KEY in self._pending_fills:
            return

        if not pos.entry_order_id:
            try:
                pos.entry_order_id = place(self.client, strategy_tag, pos.symbol,
                                            INSTRUMENT.options_exchange, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for entry: {exc}")
                self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(LEG_KEY)
        self._fill_executor.submit(
            self._watch_entry_fill, pos.entry_order_id, pos.symbol, pos.quantity
        )

    def _watch_entry_fill(self, order_id: str, symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill = poll_fill(self.client, order_id, strategy_tag, symbol, INSTRUMENT.options_exchange,
                              "SELL", quantity)
            pos = self.store.state.position
            if pos.entry_order_id == order_id:
                pos.entry_filled = True
                fill_price = fill.get("average_price") or fill.get("price")
                if fill_price:
                    pos.entry_px = float(fill_price)
                self._increment_trade_count(pos.option_type)
                self._save_state()
                Log.info(f"[{LEG_KEY}] Entry filled: {symbol}@{pos.entry_px}")
        except OrderNeedsAttention as exc:
            self._enter_error_mode("entry_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode("entry_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Unexpected error while watching entry fill: {exc}")
            self._enter_error_mode("entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(LEG_KEY)

    def _enter_error_mode(self, error_state: str, error_kind: str,
                          error_order_id: str, message: str):
        pos = self.store.state.position
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        self._save_state()
        Log.error(f"[{LEG_KEY}] {error_state} ({error_kind}): {message}")
        action = "SELL" if error_state == "entry_failed" else "BUY"
        push_leg_error(self.env, LEG_KEY, pos, action=action)
        self._last_error_push = datetime.now(IST)
        try:
            self._pnl_executor.submit(
                notify_telegram_error, self.env,
                f"[{config.strategy_name}] {LEG_KEY} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        now = datetime.now(IST)
        pos = self.store.state.position
        if not pos.error_state:
            self._last_error_push = None
            return
        last = self._last_error_push
        if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
            return
        self._last_error_push = now
        action = "SELL" if pos.error_state == "entry_failed" else "BUY"
        try:
            self._pnl_executor.submit(push_leg_error, self.env, LEG_KEY, pos, action=action)
        except Exception as exc:
            Log.warning(f"[{LEG_KEY}] Failed to dispatch periodic error re-push: {exc}")

    def _push_leg_error_bg(self, pos: "LegPosition", action: str = "", clear: bool = False):
        snapshot = copy.copy(pos)
        try:
            self._pnl_executor.submit(push_leg_error, self.env, LEG_KEY, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"[{LEG_KEY}] Failed to dispatch push_leg_error: {exc}")

    def _refresh_pending_action_bg(self):
        if LEG_KEY in self._pending_action_inflight:
            return
        self._pending_action_inflight.add(LEG_KEY)

        def _run():
            try:
                result = check_pending_action(self.env, LEG_KEY)
                if result is not None:
                    self._pending_action_cache[LEG_KEY] = result
            except Exception as exc:
                Log.warning(f"check_pending_action background refresh failed for {LEG_KEY}: {exc}")
            finally:
                self._pending_action_inflight.discard(LEG_KEY)

        self._pnl_executor.submit(_run)

    def _pop_pending_action(self) -> Optional[dict]:
        return self._pending_action_cache.pop(LEG_KEY, None)

    def _exit_position(self, reason: str = "unknown"):
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag

        if pos.exit_filled:
            self._finalize_exit(reason)
            return

        if LEG_KEY in self._pending_fills:
            return

        if not pos.exit_order_id:
            try:
                pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                           INSTRUMENT.options_exchange, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for exit: {exc}")
                self._enter_error_mode("exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(LEG_KEY)
        self._fill_executor.submit(
            self._watch_exit_fill, pos.exit_order_id, pos.symbol, pos.quantity
        )

    def _watch_exit_fill(self, order_id: str, symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill = poll_fill(self.client, order_id, strategy_tag, symbol, INSTRUMENT.options_exchange,
                              "BUY", quantity)
            pos = self.store.state.position
            if pos.exit_order_id == order_id:
                pos.exit_filled = True
                fill_price = fill.get("average_price") or fill.get("price")
                if fill_price:
                    pos.exit_fill_px = float(fill_price)
                self._save_state()
        except OrderNeedsAttention as exc:
            self._enter_error_mode("exit_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode("exit_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Unexpected error while watching exit fill: {exc}")
            self._enter_error_mode("exit_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(LEG_KEY)

    def _finalize_exit(self, reason: str):
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag

        Log.info(f"[{LEG_KEY}] Position closed: {pos.symbol}")

        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = pos.exit_fill_px
        if exit_px is None:
            exit_px = resolve_exit_ltp(self.price_stream, self.ltp_client, pos.symbol, INSTRUMENT.options_exchange)
        if exit_px is not None:
            self.store.state.today_realized_pnl += (pos.entry_px - exit_px) * pos.quantity
            self._save_state()
            try:
                append_trade_log(
                    strategy_tag, LEG_KEY, pos.symbol, pos.quantity,
                    pos.entry_time, pos.entry_px,
                    datetime.now(IST).isoformat(), exit_px, reason,
                    pos.execution_id,
                )
            except Exception as exc:
                Log.warning(f"[{LEG_KEY}] Failed to append trade log: {exc}")
            try:
                self._fill_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)
            except Exception as exc:
                Log.warning(f"[{LEG_KEY}] Failed to dispatch notify_trade_closed: {exc}")
        else:
            Log.warning(f"[{LEG_KEY}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self.price_stream.remove_instruments(
            [{"symbol": pos.symbol, "exchange": INSTRUMENT.options_exchange}]
        )
        self.store.state.position = LegPosition()
        self._save_state()

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    def _resolve_leg_error(self, action: dict):
        if LEG_KEY in self._pending_fills:
            return
        pos = self.store.state.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fills.add(LEG_KEY)
            ack_pending_action(self.env, LEG_KEY)
            self._fill_executor.submit(self._do_retry_resolution, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(pos, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            if kind == "terminal":
                self.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": INSTRUMENT.options_exchange}]
                )
                self.store.state.position = LegPosition()
                self._save_state()
                self._push_leg_error_bg(self.store.state.position, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            ack_pending_action(self.env, LEG_KEY)
            self._pending_fills.add(LEG_KEY)
            self._fill_executor.submit(
                self._watch_entry_cancel, pos.error_order_id, pos.symbol, pos.quantity
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
                self._increment_trade_count(pos.option_type)
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            self._push_leg_error_bg(pos, clear=True)
            ack_pending_action(self.env, LEG_KEY)

    def _do_retry_resolution(self, was_exit: bool, kind: str):
        try:
            pos = self.store.state.position
            if was_exit:
                if kind == "resting":
                    bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, INSTRUMENT.options_exchange)
                    if ask is not None:
                        try:
                            self.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                symbol=pos.symbol, action="BUY",
                                exchange=INSTRUMENT.options_exchange, price_type="LIMIT",
                                product=config.product, quantity=str(pos.quantity),
                                price=str(ask), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                push_leg_error(self.env, LEG_KEY, pos, clear=True)
                self._pending_fills.discard(LEG_KEY)
                return

            if kind == "resting":
                bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, INSTRUMENT.options_exchange)
                if bid is not None:
                    try:
                        self.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                            symbol=pos.symbol, action="SELL",
                            exchange=INSTRUMENT.options_exchange, price_type="LIMIT",
                            product=config.product, quantity=str(pos.quantity),
                            price=str(bid), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                             INSTRUMENT.options_exchange, "SELL", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[{LEG_KEY}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                    self._pending_fills.discard(LEG_KEY)
                    return
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, LEG_KEY, pos, clear=True)
            self._fill_executor.submit(
                self._watch_entry_fill, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(LEG_KEY)

    def _watch_entry_cancel(self, order_id: str, symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             symbol, INSTRUMENT.options_exchange, "SELL", quantity)
            pos = self.store.state.position
            if pos.error_order_id != order_id:
                return
            if result is not None:
                pos.entry_filled = True
                self._increment_trade_count(pos.option_type)
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                Log.info(f"[{LEG_KEY}] Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.client.cancelorder(order_id=order_id, strategy=strategy_tag)
                except Exception as exc:
                    Log.warning(f"[{LEG_KEY}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify "
                                f"manually at the broker that nothing is resting.")
                self.price_stream.remove_instruments(
                    [{"symbol": symbol, "exchange": INSTRUMENT.options_exchange}]
                )
                self.store.state.position = LegPosition()
                self._save_state()
            push_leg_error(self.env, LEG_KEY, self.store.state.position, clear=True)
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode("entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(LEG_KEY)

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
        pos = self.store.state.position
        if pos.error_state:
            Log.warning(f"[{LEG_KEY}] Force Exit waiting on an unresolved error "
                        f"({pos.error_state}/{pos.error_kind}) -- resolve it via "
                        f"Retry/Cancel/Manually Completed first.")
            return False
        if pos.symbol:
            try:
                self._exit_position(reason="force_exit")
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] Force Exit close attempt raised: {exc}")
            return False
        return True

    def _force_close_stale_day_position(self):
        """Safety net for a genuinely rare but real gap: _past_universal_exit()
        only trips from universal_exit_time to midnight of the CURRENT day,
        so a process that stays down across a day boundary while the
        position was still open (or the day rolls over while errored) would
        otherwise never force-close it via that check alone. Runs on every
        cycle, before the force-exit-pending and market-hours gates."""
        today = datetime.now(IST).date()
        pos = self.store.state.position
        if not pos.symbol or not pos.entry_time:
            return
        try:
            entry_date = datetime.fromisoformat(pos.entry_time).date()
        except ValueError:
            return
        if entry_date >= today:
            return
        if pos.error_state:
            Log.error(f"[{LEG_KEY}] Position from a prior day ({entry_date}) is still in "
                      f"error mode ({pos.error_state}) -- resolve it via Retry/Cancel/"
                      f"Manually Completed NOW.")
            return
        Log.warning(f"[{LEG_KEY}] Position from a prior day ({entry_date}) is still open -- "
                    f"force-closing immediately.")
        try:
            self._exit_position(reason="stale_day_force_close")
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Stale-day force-close attempt raised: {exc}")

    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()
            self._force_close_stale_day_position()

            past_universal_exit = self._past_universal_exit()
            pos = self.store.state.position

            if past_universal_exit:
                if pos.error_state:
                    self._refresh_pending_action_bg()
                    pending = self._pop_pending_action()
                    if pending is not None:
                        self._resolve_leg_error(pending)
                    else:
                        Log.error(f"[{LEG_KEY}] Universal exit time reached but this position is "
                                  f"still in error mode ({pos.error_state}) -- resolve it via "
                                  f"Retry/Cancel/Manually Completed NOW.")
                elif pos.symbol:
                    Log.warning(f"[{LEG_KEY}] Universal exit time reached; force-closing.")
                    try:
                        self._exit_position(reason="eod_force_close")
                    except Exception as exc:
                        Log.exception(f"[{LEG_KEY}] Universal-exit close attempt raised: {exc}")
                return

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                if pos.error_state:
                    self._refresh_pending_action_bg()
                    pending = self._pop_pending_action()
                    if pending is not None:
                        self._resolve_leg_error(pending)
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- position flat. Stopping.")
                    ack_force_exit_complete(self.env)
                    self._force_exit_pending = False
                return

            if not self._within_market_hours():
                return

            if pos.error_state:
                self._refresh_pending_action_bg()
                pending = self._pop_pending_action()
                if pending is not None:
                    self._resolve_leg_error(pending)
                return

            if LEG_KEY in self._pending_fills:
                return

            within_entry = self._within_entry_window()

            inst_ltp = self.price_stream.get_ltp(
                INSTRUMENT.name, INSTRUMENT.underlying_exchange, max_age=config.ws_stale_seconds
            )
            if inst_ltp is None:
                if not self._ws_fallback_logged:
                    Log.warning(f"[NIFTY] WS LTP stale/missing -- falling back to REST quotes().")
                    self._ws_fallback_logged = True
                inst_ltp = fetch_ltp(self.ltp_client, INSTRUMENT)
            else:
                self._ws_fallback_logged = False

            if not pos.symbol:
                # FLAT: signal drives entry. Chain is only refreshed when we
                # could actually still enter this cycle (SHARED trade cap +
                # entry window not exhausted) -- efficient-API-calls
                # requirement. The entry-cutoff TIME is additionally enforced
                # inside compute_donchian_signal itself (bug #3 fix).
                still_enterable = within_entry and self.store.state.trade_count < config.max_trades_per_day
                signal = self.get_signal(ltp=inst_ltp, refresh_chain=still_enterable)
                if signal is None or not still_enterable:
                    return
                if signal.pending_entry_side:
                    self._enter_position(signal.pending_entry_side, spot=signal.ltp)
                return

            # OPEN: only the SL/target check matters now -- no signal/chain
            # fetch needed at all while a position is open (the arm/confirm
            # state machine intentionally does not run while in a position,
            # matching "no overlapping trades").
            if pos.exit_order_id or pos.exit_filled:
                self._exit_position(reason="unknown")
                return

            current_premium = resolve_exit_ltp(
                self.price_stream, self.ltp_client, pos.symbol, INSTRUMENT.options_exchange
            )
            if current_premium is None:
                return
            hit = self._sl_or_target_hit(pos, current_premium)
            if hit:
                Log.info(f"[{LEG_KEY}] Exit condition met ({hit}) -> closing.")
                self._exit_position(reason=hit)

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
    print(f"Instrument           : {INSTRUMENT.name}")
    print(f"Donchian             : ({config.donchian_period})")
    print(f"EMA                  : {config.ema_period}")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/day       : {config.max_trades_per_day} (SHARED, single position slot)")
    print(f"Product              : {config.product} (own 15:15 close + broker MIS auto-square-off)")
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

    ws_instruments = [{"exchange": INSTRUMENT.underlying_exchange, "symbol": INSTRUMENT.name}]
    price_stream = PriceStream(client, ws_instruments)
    price_stream.start()

    # Mid-day restart: an option position already open before this process
    # started needs its symbol subscribed immediately -- same resumability
    # guarantee as every other script in this project. Gated on
    # current_day == today -- this is a pure intraday strategy with NO
    # legitimate overnight position, so a position still recorded open from
    # a PRIOR day is stale by definition; run_cycle's own
    # _force_close_stale_day_position() closes it on the first cycle
    # regardless (via REST fallback, since it was never WS-subscribed here).
    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key and state_store.state.position.symbol:
        already_known.append({
            "symbol": state_store.state.position.symbol,
            "exchange": INSTRUMENT.options_exchange,
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

    pos = state_store.state.position
    if pos.error_state:
        action = "SELL" if pos.error_state == "entry_failed" else "BUY"
        push_leg_error(env, LEG_KEY, pos, action=action)
        Log.error(f"[{LEG_KEY}] Resuming with an unresolved error from before restart "
                  f"({pos.error_state}/{pos.error_kind}) -- needs Retry/Cancel/Manually Completed.")
    elif pos.entry_order_id and not pos.entry_filled:
        Log.warning(f"[{LEG_KEY}] Resuming entry-fill watch for an order placed "
                    f"before a restart (order_id={pos.entry_order_id}).")
        engine._pending_fills.add(LEG_KEY)
        engine._fill_executor.submit(
            engine._watch_entry_fill, pos.entry_order_id, pos.symbol, pos.quantity
        )
    elif pos.exit_order_id and not pos.exit_filled:
        Log.warning(f"[{LEG_KEY}] Resuming exit-fill watch for an order placed "
                    f"before a restart (order_id={pos.exit_order_id}).")
        engine._pending_fills.add(LEG_KEY)
        engine._fill_executor.submit(
            engine._watch_exit_fill, pos.exit_order_id, pos.symbol, pos.quantity
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
