"""
===============================================================================
CRUDEOIL 1H VWAP/EMA20 Crossover -- Positional Long Options
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Derived from: `MCX_CrudeOil_EMA9_RSI_Intraday_1_20260723150000.py` -- same
              instrument/broker/exchange plumbing and the same proven
              order-placement/fill-watching/error-recovery/PnL-reporting
              infrastructure, reused near-verbatim. See
              `PLAN_CRUDEOIL_VWAP_EMA_Positional.md` (this directory) for the
              full design writeup this script was built from -- delete that
              plan doc once this is confirmed working, it is a planning
              artifact, not documentation to keep in sync.

*** THIS STRATEGY BUYS OPTIONS -- RISK IS CAPPED AT THE PREMIUM PAID ***
Unlike every other strategy in this project (naked option SELLING, undefined
risk), this one only ever BUYS a single CALL or PUT -- the worst case for any
one trade is losing the premium paid, never more. No hedge leg, no per-trade
stop-loss beyond the signal's own reversal condition.

*** THIS STRATEGY IS POSITIONAL (NRML) -- NO DAILY SQUARE-OFF ***
Deliberately different from every other strategy in this project (all MIS,
all force-closed by a universal_exit_time). A position opened today may
still be open next week -- the ONLY things that close it are (a) the
opposite VWAP/EMA20 crossover signal, or (b) the expiry-roll mechanism
below. There is no time-of-day force-close branch in run_cycle() at all.

Signal Rules
------------------------------------------------------------------------
Computed off the front-month FUTURES contract's own 1-HOUR candles (CRUDEOIL
has no quotable spot/index on MCX -- same reasoning as the EMA9/RSI
reference script, see its own docstring):
  - Session VWAP (resets at MCX session open, 09:00 IST) on the 1H bars.
  - EMA(20) on Close -- a continuously-rolling indicator, UNAFFECTED by the
    daily VWAP reset (computed across the full multi-day history window,
    same as every other EMA in this project).
  - RSI(14) on Close.
  - Crossover: compare (vwap_prev2 vs ema20_prev2) against (vwap_prev1 vs
    ema20_prev1) -- a sign flip between the two is a crossover on the most
    recently closed candle.

  Two filters, BOTH required, on every crossover candle:
    1. Slope: (vwap_prev1 - vwap_prev2) / 2, expressed as a percentage of
       futures_price -- must exceed vwap_slope_pct_threshold (0.10%/candle).
       Confirms VWAP is moving fast relative to EMA20, not just barely
       ticking across it.
    2. Gap: abs(vwap_prev1 - ema20_prev1) / futures_price at the crossover
       candle -- must exceed vwap_gap_pct_threshold (0.05%). Filters out a
       crossover that's already collapsing back toward EMA20 the moment it
       happens.

  CALL entry : VWAP crosses ABOVE EMA20 (this candle)
               AND slope filter passes AND gap filter passes
               AND rsi_prev1 > 50
  PUT entry  : VWAP crosses BELOW EMA20 (this candle)
               AND slope filter passes AND gap filter passes
               AND rsi_prev1 < 50

  CALL exit  : VWAP crosses back BELOW EMA20 (no slope/gap filter on exit --
               those exist to avoid entering on a weak signal, not to delay
               getting out of a position that's already open)
  PUT exit   : VWAP crosses back ABOVE EMA20

  Reversal: a CALL-exit signal is, by construction, the SAME event as a PUT-
  entry signal (VWAP crossing below EMA20) and vice versa. This strategy
  submits the exit immediately, then submits the opposite entry the MOMENT
  the exit fill is CONFIRMED (via _watch_exit_fill's completion, not gated
  behind waiting for the next candle) -- see StrategyState.pending_reversal_to
  and _finalize_exit's handling of it. This sequencing (exit-confirms-before-
  entry) is deliberate: submitting both orders in the same cycle without
  waiting for the first fill could leave TWO positions open simultaneously
  if the entry filled before the exit did, violating "max one position."

  ** No native per-trade stop-loss.** Risk is bounded by construction (long
  premium, never more than paid) but there is no independent price-based
  cut -- the only exit is the opposite crossover, or the expiry roll below.

Strike selection, expiry roll, and product
------------------------------------------------------------------------
  - Strike: nearest LISTED strike that is >= min_otm_points (100) points
    out-of-the-money from the futures price -- NOT the classic nearest-ATM
    selection the reference script uses (see pick_otm_leg below, a new
    function; the reference's pick_atm_leg is untouched, used by other
    scripts).
  - Options expiry: CRUDEOIL options are MONTHLY only (no weekly list to
    filter out, confirmed against the reference script's own
    resolve_current_month_expiry). Resolved once per day, same pattern as
    the reference's resolve_current_month_futures.
  - Expiry roll: expiry_roll_days_before (4) trading days before the
    CURRENTLY-HELD position's expiry, close it (reason="expiry_roll") and,
    once the exit fill confirms, re-enter the SAME option_type on the NEXT
    monthly expiry (simply the next entry in the broker's own sorted expiry
    list -- no hand-rolled month arithmetic) at a freshly-resolved
    nearest->=100pt-OTM strike. If no position is open when the threshold is
    crossed, there's nothing to roll -- fresh entries near the roll window
    resolve against whichever expiry is current at that moment (the
    resolved expiry itself already accounts for "is this expiry about to
    roll" via the same day-count check, so a fresh entry doesn't open into
    an expiry it would immediately need to roll again).
  - **Product = NRML** (positional, overnight carry) -- see module docstring
    above. Deliberate, not a placeholder.
  - Quantity: fixed at quantity_lots (2) lots x the option chain's own
    lotsize (never hardcoded in absolute contracts, same reasoning as the
    reference script -- MCX CRUDEOIL/CRUDEOILM lot sizes are broker-master-
    data-dependent).
  - Entry evaluation window: the whole MCX session, 09:00-23:30 (positional,
    no reason to restrict to a narrow intraday window the way an MIS
    strategy does).
  - Max trades/day: UNLIMITED by design (position flips only as often as a
    genuine crossover occurs -- there is no max_trades_per_leg_per_day cap
    in this script at all, unlike every MIS strategy in this project).

Confirmed non-blocking (2026-08-05, read end-to-end against the reference
script before writing this one -- see PLAN_CRUDEOIL_VWAP_EMA_Positional.md's
matching section for the full writeup): get_signal() only ever reads an
in-memory cache and dispatches any refresh to _fill_executor in the
background; run_cycle() never itself waits on client.history(). The one
inline synchronous call is a single best-effort fetch_symbol_ltp() REST
fallback, only when the WebSocket price cache is stale -- identical to the
reference script. scheduler_interval is 60 seconds -- comfortably fine given
the candle-boundary caching means most cycles do zero network work at all
regardless of the underlying signal being on a 1-hour timeframe.

Live price feed: WebSocket, DYNAMIC subscription (identical design to the
reference script -- see its own module docstring's "Live price feed"
section for the full watchdog/reconnect writeup).

Order placement robustness, trade log, PnL reporting
------------------------------------------------------------------------
Identical machinery to the reference script (place/poll_fill/reprice,
background trade-log writer, strategy_reporting push helpers) -- EXCEPT
every BUY/SELL direction and every PnL sign convention is flipped for long
options instead of naked short selling (see inline comments at each site --
this was the single most important thing to get right when adapting the
reference script, confirmed by re-reading its trade-log writer and PnL
functions line by line: they hard-code the SHORT convention throughout).

Base symbol: CRUDEOIL (standard), not CRUDEOILM (Mini) -- same confirmed
reasoning as the reference script (Mini is futures-only, no options
segment).

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.ema(data, period)` -> ndarray. `ta.rsi(data, period=14)` -> ndarray.
  * `client.history(..., interval='1h')` is a standard, documented interval.
  * No existing in-repo helper for session-anchored VWAP was found (checked
    `openalgo.ta` before hand-rolling one) -- computed manually here as
    cumsum(typical_price * volume) / cumsum(volume), reset at each session's
    first bar.

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

# Same reasoning as the reference script -- several long-lived threads
# (fill-watchers, Force Exit background check, PnL push, trade-log writer,
# PriceStream's own watchdog/WS threads), none doing anything beyond simple
# polling loops and REST calls. Must be called before any thread is created.
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

print("Combined OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
@dataclass
class InstrumentConfig:
    name: str                    # "CRUDEOIL" -- change here if your broker lists it differently
    underlying_exchange: str     # "MCX"
    options_exchange: str        # "MCX" -- MCX has no NSE_INDEX-style underlying/options split


INSTRUMENTS = [
    InstrumentConfig(name="CRUDEOIL", underlying_exchange="MCX", options_exchange="MCX"),
]


@dataclass
class Config:
    strategy_name: str = "CRUDEOIL 1H VWAP/EMA20 Crossover -- Positional Long Options"
    version: str = "1.0.0"

    intraday_interval: str = "1h"
    history_lookback_days: int = 60   # calendar days of 1h history -- EMA20 warmup + multi-day continuity
    ema_period: int = 20
    rsi_period: int = 14
    rsi_ce_threshold: float = 50.0    # CALL entry needs RSI > this
    rsi_pe_threshold: float = 50.0    # PUT entry needs RSI < this

    vwap_slope_candles: int = 2       # candles the VWAP slope is measured over
    vwap_slope_pct_threshold: float = 0.10   # % of futures price per candle
    vwap_gap_pct_threshold: float = 0.05     # % of futures price, at the crossover candle

    min_otm_points: float = 100.0     # strike must be at least this many points OTM

    quantity_lots: int = 2
    # No max_trades_per_leg_per_day cap -- deliberate, see module docstring's
    # "Max trades/day: UNLIMITED by design" section. Position flips only as
    # often as a genuine crossover occurs.

    product: str = "NRML"             # positional, overnight carry -- see module docstring
    price_type: str = "MARKET"

    # Whole MCX session -- positional, no reason to restrict entries to a
    # narrow intraday window the way an MIS strategy does.
    entry_start: time = time(9, 0)
    entry_end: time = time(23, 30)
    market_open: time = time(9, 0)
    market_close: time = time(23, 55)   # MCX official session, per database/market_calendar_db.py
    # Deliberately NO universal_exit_time / MIS square-off concept in this
    # config at all -- see module docstring. NRML + no daily force-close is
    # an intentional, explicit deviation from every other strategy in this
    # project (all of which pair MIS/NRML with SOME time-based backstop).

    expiry_roll_days_before: int = 4   # trading days before expiry -> close + roll to next month

    scheduler_interval: int = 60
    indicator_refresh_interval: int = 15
    pnl_tick_interval: float = 0.8

    pnl_rest_fallback_interval_sec: float = 900.0

    ws_stale_seconds: float = 20.0
    ws_watchdog_interval: float = 15.0
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

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


# Single conceptual leg -- unlike the reference script's LEG_KEYS-keyed dict
# of two SIMULTANEOUS legs (PE + CE), this strategy holds at most ONE
# position at a time, whose identity (CE/PE) flips on a reversal signal. One
# fixed leg_key is used throughout (matches the platform's push_leg_error/
# check_pending_action APIs, which are keyed by leg_key string) regardless
# of which option_type is currently held, so the reporting layer never has
# to deal with a leg_key that changes identity mid-flight.
LEG_KEY = f"{INSTRUMENTS[0].name}_POSITION"


@dataclass
class StrategyState:
    current_day: str = ""
    position: LegPosition = field(default_factory=LegPosition)
    current_option_type: str = ""    # "CE" | "PE" | "" (flat)
    current_expiry: str = ""         # DDMMMYY of the expiry the open position (if any) is on
    trade_count: int = 0             # total trades today, purely informational (no cap enforced)
    # Set the moment a reversal-triggered exit is submitted (the exit is the
    # SAME event as the opposite entry's signal) -- consumed by
    # _finalize_exit once the exit fill actually confirms, which is what
    # submits the new entry. See module docstring's "Reversal" section for
    # why this can't just submit both orders in the same cycle.
    pending_reversal_to: str = ""    # "CE" | "PE" | ""
    last_updated: str = ""
    today_realized_pnl: float = 0.0
    futures_symbol: str = ""
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
            or "crudeoil_vwap_ema20_positional"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return config.market_open <= now <= config.market_close


def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle. Same
    mechanism as the reference script, called with interval_minutes=60 here
    (1H candles) instead of 3."""
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
# LIVE PRICE STREAM (WebSocket, DYNAMIC subscription -- identical design to
# the reference script; see its module docstring for the full watchdog/
# reconnect writeup)
###############################################################################
class PriceStream:
    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple] = {}
        self._instruments: dict[tuple, dict] = {}
        self._stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stale_streak: dict[tuple, int] = {}
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
        self.state.current_option_type = data.get("current_option_type", "")
        self.state.current_expiry = data.get("current_expiry", "")
        self.state.trade_count = data.get("trade_count", 0)
        self.state.pending_reversal_to = data.get("pending_reversal_to", "")
        self.state.last_updated = data.get("last_updated", "")
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.futures_symbol = data.get("futures_symbol", "")
        self.state.last_execution_id = data.get("last_execution_id", 0)
        pos_raw = data.get("position", {})
        self.state.position = LegPosition(
            **{**asdict(LegPosition()), **filter_known_fields(LegPosition, pos_raw)}
        )
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "current_option_type": self.state.current_option_type,
            "current_expiry": self.state.current_expiry,
            "trade_count": self.state.trade_count,
            "pending_reversal_to": self.state.pending_reversal_to,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "futures_symbol": self.state.futures_symbol,
            "last_execution_id": self.state.last_execution_id,
            "position": asdict(self.state.position),
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=4)


###############################################################################
# HELPERS
###############################################################################
def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def _fetch_options_expiry_list(client, inst: InstrumentConfig) -> list[str]:
    """Raw sorted expiry date strings ("DD-Mon-YY") from the broker, as-is.
    Shared by resolve_current_month_expiry (current) and
    resolve_next_month_expiry (roll target) so both read the SAME list in
    one call site each, rather than two separately-written client.expiry()
    calls that could drift."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve options expiry for {inst.name}: {resp}")
    return resp["data"]


def resolve_current_month_expiry(client, inst: InstrumentConfig, roll_buffer_days: int = 0) -> str:
    """Nearest upcoming OPTIONS expiry (DDMMMYY), rolling to the NEXT expiry
    if today is within roll_buffer_days trading-day-equivalent* of the
    resolved expiry (or on/past it) -- same gamma/theta-cliff avoidance as
    the reference script's identical function, generalized here to accept a
    buffer instead of only "today == expiry day" (this strategy's own
    4-trading-day roll window needs a fresh ENTRY inside that window to
    resolve directly against next month, not open into an expiry it would
    immediately have to roll again).

    * Uses calendar-day count, not a true trading-day count (see
    PLAN_CRUDEOIL_VWAP_EMA_Positional.md's expiry-roll section -- no
    existing MCX holiday-calendar helper was found in this repo to build a
    true trading-day count from; calendar-day is an accepted approximation,
    noted explicitly rather than silently assumed exact)."""
    dates_raw = _fetch_options_expiry_list(client, inst)
    today = datetime.now(IST).date()
    for i, raw in enumerate(dates_raw):
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today and (d - today).days > roll_buffer_days:
            return _compact_expiry(raw)
        if d >= today:
            # Within the roll buffer (or today IS the expiry) -- roll to next.
            if i + 1 < len(dates_raw):
                return _compact_expiry(dates_raw[i + 1])
            raise RuntimeError(
                f"{inst.name}: nearest options expiry ({raw}) is within the "
                f"{roll_buffer_days}-day roll buffer and the broker returned no "
                f"later expiry to roll to -- refusing to silently trade an "
                f"about-to-expire contract."
            )
    return _compact_expiry(dates_raw[-1])


def resolve_next_month_expiry(client, inst: InstrumentConfig, current_expiry_compact: str) -> str:
    """The expiry immediately after current_expiry_compact in the broker's
    own sorted list -- simply the next list entry, since CRUDEOIL options
    are monthly-only (confirmed against the reference script's own
    resolve_current_month_expiry docstring: no weekly list to filter). Used
    by the roll mechanism; NOT the same as resolve_current_month_expiry's
    own roll-forward branch (that one decides CURRENT vs next based on
    today's date; this one is given an explicit current expiry and returns
    whatever comes after it)."""
    dates_raw = _fetch_options_expiry_list(client, inst)
    compacted = [_compact_expiry(d) for d in dates_raw]
    try:
        idx = compacted.index(current_expiry_compact)
    except ValueError:
        raise RuntimeError(
            f"{inst.name}: current expiry {current_expiry_compact!r} not found in the "
            f"broker's own expiry list {compacted} -- cannot resolve a roll target."
        )
    if idx + 1 >= len(compacted):
        raise RuntimeError(
            f"{inst.name}: no expiry listed after {current_expiry_compact!r} to roll to."
        )
    return compacted[idx + 1]


def resolve_current_month_futures(client, inst: InstrumentConfig) -> str:
    """Resolve the nearest-expiry FUTURES symbol -- identical to the
    reference script's function of the same name. Resolved once per day
    (see StrategyEngine._reset_day_if_needed)."""
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
    return isinstance(obj, dict) and obj.get("status") == "error"


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
        Log.warning(f"quotes() failed for {symbol}.{exchange}: {exc}")
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
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() (bid/ask) failed for {symbol}.{exchange}: {exc}")
        return None, None
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(data, dict):
        return None, None
    bid = data.get("bid")
    ask = data.get("ask")
    return (float(bid) if bid is not None else None,
            float(ask) if ask is not None else None)


###############################################################################
# SIGNAL -- VWAP/EMA20/RSI14 crossover with slope + gap filters
###############################################################################
@dataclass
class InstrumentSignal:
    close_prev1: float
    vwap_prev1: float
    vwap_prev2: float
    ema20_prev1: float
    ema20_prev2: float
    rsi_prev1: float
    ltp: float
    candle_key: str
    session_bar_count: int   # how many bars into TODAY's session this candle is -- warm-up gate


_last_logged_candle: dict[str, str] = {}


def _session_vwap(bars) -> tuple[np.ndarray, np.ndarray]:
    """Session-anchored VWAP over `bars` (a DataFrame with a tz-aware
    DatetimeIndex and open/high/low/close/volume columns), resetting at each
    calendar day's first bar. No existing session-VWAP helper was found in
    `openalgo.ta` (checked before hand-rolling this -- see module
    docstring's Notes section), so computed directly:
    cumsum(typical_price * volume) / cumsum(volume), with both cumulative
    sums reset to zero at the first bar of every new IST calendar day.
    Returns (vwap_array, session_bar_count_array) -- the second is how many
    bars into that day's session each bar is (1-indexed), used by the
    caller to gate entries until VWAP has enough of today's data to be
    meaningful."""
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
    session_bar_count = np.zeros(len(bars), dtype=int)
    cum_pv = 0.0
    cum_v = 0.0
    current_session = None
    bar_idx_in_session = 0
    for i in range(len(bars)):
        if session_dates[i] != current_session:
            current_session = session_dates[i]
            cum_pv = 0.0
            cum_v = 0.0
            bar_idx_in_session = 0
        cum_pv += typical[i] * volume[i]
        cum_v += volume[i]
        bar_idx_in_session += 1
        vwap[i] = cum_pv / cum_v if cum_v > 0 else typical[i]
        session_bar_count[i] = bar_idx_in_session
    return vwap, session_bar_count


def compute_instrument_signal(client, inst: InstrumentConfig, futures_symbol: str,
                               ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetch 1H history for the current-month FUTURES contract, compute
    session VWAP, EMA(20), RSI(14) off the last genuinely CLOSED bars.
    Returns None (with a logged reason) if anything is unavailable -- same
    defensive shape as the reference script's compute_instrument_signal."""
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
    if len(bars) >= 2:
        bars = bars.iloc[:-1]   # drop the still-forming last candle
    if len(bars) < config.ema_period + 2:
        Log.warning(
            f"[{inst.name}] only {len(bars)} {config.intraday_interval} bars after dropping "
            f"the still-forming one (need >= {config.ema_period + 2} for a stable "
            f"EMA{config.ema_period}) -- no signal."
        )
        return None

    close = bars["close"].to_numpy(dtype=float)
    ema20 = np.asarray(ta.ema(close, config.ema_period))
    rsi = np.asarray(ta.rsi(close, config.rsi_period))
    vwap, session_bar_count = _session_vwap(bars)

    candle_key = str(bars.index[-1])

    if ltp is None:
        ltp = fetch_symbol_ltp(client, futures_symbol, inst.options_exchange)
    if ltp is None:
        ltp = float(close[-1])

    signal = InstrumentSignal(
        close_prev1=float(close[-1]),
        vwap_prev1=float(vwap[-1]), vwap_prev2=float(vwap[-2]),
        ema20_prev1=float(ema20[-1]), ema20_prev2=float(ema20[-2]),
        rsi_prev1=float(rsi[-1]), ltp=ltp, candle_key=candle_key,
        session_bar_count=int(session_bar_count[-1]),
    )

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        Log.info(
            f"[{inst.name}] futures={futures_symbol} candle={candle_key} close={signal.close_prev1:.2f} "
            f"vwap={signal.vwap_prev1:.2f} ema{config.ema_period}={signal.ema20_prev1:.2f} "
            f"rsi={signal.rsi_prev1:.2f} ltp={ltp:.2f} session_bar={signal.session_bar_count} | "
            f"prev2: vwap={signal.vwap_prev2:.2f} ema{config.ema_period}={signal.ema20_prev2:.2f}"
        )
    return signal


def fetch_chain(client, inst: InstrumentConfig, expiry: str, underlying_symbol: str, strike_count: int = 10):
    """Same reasoning as the reference script's fetch_chain -- underlying_symbol
    must be the quotable FUTURES contract, not the bare CRUDEOIL name (no
    quotable spot/index on MCX). strike_count is wider than the reference
    script's (1) since this strategy needs to scan multiple OTM strikes to
    find the nearest one >= min_otm_points away, not just the single ATM
    strike."""
    resp = client.optionchain(
        underlying=underlying_symbol, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=strike_count,
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


def pick_otm_leg(chain: dict, option_type: str, spot: float, min_points: float) -> dict:
    """Nearest LISTED strike that is AT LEAST min_points OTM from spot --
    NOT the reference script's pick_atm_leg (nearest-to-spot). "OTM" means
    strike > spot for a CALL, strike < spot for a PUT. Among strikes that
    qualify (>= min_points away, on the correct side), picks the one
    closest to spot (i.e. the least-far-OTM strike that still clears the
    minimum), matching "100-point OTM" read as a floor, not an exact target
    (CRUDEOIL strike intervals may not land on exactly spot+100)."""
    legs = _legs_with_strike(chain, option_type)
    if not legs:
        raise RuntimeError(f"No {option_type} legs found in chain (spot={spot})")
    if option_type == "CE":
        candidates = [leg for leg in legs if leg["strike"] - spot >= min_points]
    else:
        candidates = [leg for leg in legs if spot - leg["strike"] >= min_points]
    if not candidates:
        raise RuntimeError(
            f"No {option_type} strike found >= {min_points} points OTM from spot={spot} "
            f"in the fetched chain (widen strike_count if this recurs)."
        )
    return min(candidates, key=lambda l: abs(l["strike"] - spot))


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


def _migrate_trade_log_if_needed(strategy_tag: str):
    log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
    if not log_path.exists():
        return
    with log_path.open("r", newline="") as fp:
        rows = list(csv.reader(fp))
    if not rows or "execution_id" in rows[0]:
        return
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
            # LONG option convention (buy low, sell high = profit) --
            # DELIBERATELY the opposite sign from the reference script's
            # short-seller convention (entry_px - exit_px). Confirmed by
            # reading the reference script's writer function line by line
            # before writing this one -- flipping this is the single most
            # important adaptation in this whole file to get right.
            pnl_points = exit_px - entry_px
            pnl_rupees = pnl_points * quantity
            # Display quantity POSITIVE -- this strategy only ever holds
            # long positions, unlike the reference script's short sellers
            # (which display negative).
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


###############################################################################
# PLATFORM REPORTING (strategy_reporting subprocess -- see
# STRATEGY_REPORTING_PORT usage below, not FLASK_PORT/openalgo.sock)
###############################################################################
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
    ownership bug. Raises if both attempts fail; caller logs and
    swallows."""
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
    """GET counterpart to _post_json_local -- same loopback-with-retry
    behavior (see that function's docstring for why the env.host/
    Cloudflare fallback was dropped). Raises if both attempts fail;
    caller treats it as "no action pending" rather than crashing the
    scheduler loop over a reporting hiccup."""
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
        self._ws_fallback_logged: dict[str, bool] = {}
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._last_error_push: dict[str, datetime] = {}
        # Throttles the outer run_cycle except-clause's WhatsApp alert --
        # separate from _last_error_push above, which only governs the UI
        # error-badge re-push. In-memory/per-instance, not persisted.
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        self._day_reset_pending: bool = False
        self._mcx_session_window: Optional[tuple] = None
        self._mcx_session_checked_day: str = ""
        self._mcx_session_check_pending: bool = False
        # Single-leg strategy -- one fill-watcher slot is enough (vs the
        # reference script's len(LEG_KEYS)=2), but sized 2 anyway so an
        # exit-watch and the immediately-following reversal entry-watch
        # (see _finalize_exit's pending_reversal_to handling) never queue
        # behind each other on a single-worker pool.
        self._fill_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fillwatch")
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bgcheck")
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
        self._expiry_cache: dict[str, str] = {}
        self._chain_cache: dict[str, dict] = {}
        self._signal_refresh_pending: set[str] = set()
        # Expiry-roll check is cheap (pure date comparison against
        # store.state.current_expiry) but gates whether the roll's own
        # background resolve (next-month expiry + chain refetch) is already
        # in flight, same guarded-dispatch pattern as every other background
        # check in this engine.
        self._expiry_roll_check_pending: bool = False
        self._expiry_roll_checked_day: str = ""

    def _save_state(self):
        with self._state_lock:
            self.store.save()

    def _refresh_chain_cache(self, inst: InstrumentConfig, futures_symbol: str, expiry: str):
        try:
            self._chain_cache[inst.name] = fetch_chain(self.client, inst, expiry, futures_symbol)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background chain refresh failed (will retry live at "
                        f"entry if needed): {exc}")

    def _refresh_signal_and_chain_bg(self, inst: InstrumentConfig, futures_symbol: str,
                                       ltp: Optional[float], refresh_chain: bool):
        try:
            fresh = compute_instrument_signal(self.client, inst, futures_symbol, ltp=ltp)
            if fresh is not None:
                self._signal_cache[inst.name] = fresh
                self._last_indicator_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background indicator refresh failed (will retry "
                        f"next cycle): {exc}")
        if refresh_chain:
            expiry = self._expiry_cache.get(inst.name)
            if expiry is not None:
                self._refresh_chain_cache(inst, futures_symbol, expiry)
        self._signal_refresh_pending.discard(inst.name)

    def get_signal(self, inst: InstrumentConfig, futures_symbol: str,
                    ltp: Optional[float] = None, refresh_chain: bool = False) -> Optional[InstrumentSignal]:
        """Same caching shape as the reference script's get_signal -- ONLY
        difference is the candle boundary is 60 minutes (1H bars) instead
        of 3. Never itself waits on client.history(); dispatches to
        _fill_executor when the cached candle is behind the current
        60-minute boundary."""
        last = self._last_indicator_refresh.get(inst.name)
        current_boundary = _current_candle_boundary(60)
        cached_for_boundary = self._signal_cache.get(inst.name)
        cached_boundary = (
            _candle_key_boundary(cached_for_boundary.candle_key)
            if cached_for_boundary is not None else None
        )
        have_current_candle = cached_boundary is not None and cached_boundary >= current_boundary
        due_for_refresh = last is None or not have_current_candle
        if due_for_refresh and inst.name not in self._signal_refresh_pending:
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
        if self.store.state.current_day == today_key:
            return
        if self._day_reset_pending:
            return
        self._day_reset_pending = True

        def _run():
            try:
                futures_symbol = resolve_current_month_futures(self.client, INSTRUMENTS[0])
            except Exception as exc:
                Log.warning(f"Could not resolve current-month futures contract yet ({exc}); "
                            f"retrying next cycle.")
                self._day_reset_pending = False
                return
            Log.info(f"New day detected ({today_key}); resetting daily trade counter. "
                      f"Futures contract: {futures_symbol}")
            self.store.state.current_day = today_key
            # today_realized_pnl and trade_count are informational only for
            # a positional strategy (no daily square-off, positions persist
            # across days) -- still reset daily, same as the reference
            # script, purely so the PnL tile shows "today's" realized PnL,
            # not a lifetime total. The OPEN position itself is untouched.
            self.store.state.today_realized_pnl = 0.0
            self.store.state.trade_count = 0
            self.store.state.futures_symbol = futures_symbol
            self._expiry_cache.clear()
            self._chain_cache.clear()
            self._save_state()
            self.price_stream.add_instruments(
                [{"symbol": futures_symbol, "exchange": INSTRUMENTS[0].options_exchange}]
            )
            self._day_reset_pending = False

        self._fill_executor.submit(_run)

    def _within_entry_window(self) -> bool:
        if config.test_mode:
            return True
        now = datetime.now(IST).time()
        return config.entry_start <= now <= config.entry_end

    def _refresh_mcx_session_window_bg(self):
        today_key = datetime.now(IST).date().isoformat()
        if self._mcx_session_checked_day == today_key:
            return
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
                self._mcx_session_checked_day = today_key
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
                return False
            start_t, end_t = self._mcx_session_window
            now = datetime.now(IST).time()
            return start_t <= now <= end_t
        return _within_market_hours()

    def _refresh_expiry_roll_check_bg(self):
        """Once per day: is the CURRENTLY-HELD position's expiry within
        expiry_roll_days_before of today? If so and a position is open,
        submit the roll (close current, then -- once that exit confirms --
        open the equivalent on next month's expiry, via the SAME
        pending_reversal_to-style mechanism _finalize_exit already uses for
        signal-driven reversals, just carrying the CURRENT option_type
        forward instead of flipping it). Cheap date-only check; the actual
        network calls (next-month expiry resolution, fresh chain fetch) only
        happen if the roll is genuinely due AND a position is open."""
        if not self.store.state.position.symbol or not self.store.state.current_expiry:
            return  # nothing open, nothing to roll
        today_key = datetime.now(IST).date().isoformat()
        if self._expiry_roll_checked_day == today_key:
            return
        if self._expiry_roll_check_pending:
            return
        self._expiry_roll_check_pending = True

        def _run():
            try:
                expiry_date = datetime.strptime(self.store.state.current_expiry, "%d%b%y").date()
                today = datetime.now(IST).date()
                days_to_expiry = (expiry_date - today).days
                if days_to_expiry <= config.expiry_roll_days_before:
                    Log.warning(
                        f"[{LEG_KEY}] Held position's expiry {self.store.state.current_expiry} "
                        f"is {days_to_expiry} day(s) away (<= {config.expiry_roll_days_before} "
                        f"roll threshold) -- closing to roll to next month."
                    )
                    # Carry the SAME option_type forward on roll (not a
                    # reversal) -- reuse pending_reversal_to as the vehicle
                    # since _finalize_exit already knows how to consume it;
                    # the option_type value is simply unchanged from
                    # current_option_type instead of flipped.
                    self.store.state.pending_reversal_to = self.store.state.current_option_type
                    self._save_state()
                    self._exit_leg(reason="expiry_roll")
                self._expiry_roll_checked_day = today_key
            except Exception as exc:
                Log.warning(f"Expiry roll check failed ({exc}); will retry next cycle.")
            finally:
                self._expiry_roll_check_pending = False

        self._bg_executor.submit(_run)

    def report_pnl_tick(self):
        try:
            pos = self.store.state.position
            open_positions = []
            if pos.symbol and pos.entry_filled:
                current_px = self.price_stream.get_ltp(
                    pos.symbol, INSTRUMENTS[0].options_exchange, max_age=config.ws_stale_seconds
                )
                if current_px is not None:
                    # LONG convention -- profit when price rises, opposite
                    # sign from the reference script's short-seller pnl.
                    pnl = (current_px - pos.entry_px) * pos.quantity
                    open_positions.append({
                        "leg_key": LEG_KEY, "symbol": pos.symbol, "direction": "LONG",
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
                Log.warning(f"Failed to dispatch report_pnl_to_platform: {exc}")
        except Exception as exc:
            Log.exception(f"report_pnl_tick failed: {exc}")

    # ---- entry / exit (single long-options leg, resumable) ------------------
    def _enter_leg(self, inst: InstrumentConfig, option_type: str, spot: float,
                    futures_symbol: str, expiry: str, condition_desc: str = ""):
        pos = self.store.state.position

        if not pos.symbol:
            chain = self._chain_cache.get(inst.name)
            if chain is None:
                Log.warning(f"[{LEG_KEY}] Entry signal fired but the option chain "
                            f"cache isn't populated yet -- skipping this cycle, "
                            f"will retry once the background refresh completes.")
                return
            try:
                otm_leg = pick_otm_leg(chain, option_type, spot, config.min_otm_points)
            except RuntimeError as exc:
                Log.warning(f"[{LEG_KEY}] {exc} -- skipping this cycle, will retry "
                            f"once the background chain refresh completes.")
                return
            quantity = config.quantity_lots * otm_leg["lotsize"]

            Log.info(f"[{LEG_KEY}] Entry: type={option_type} strike={otm_leg['strike']} "
                     f"symbol={otm_leg['symbol']}@{otm_leg['ltp']} qty={quantity} expiry={expiry}"
                     + (f" | condition: {condition_desc}" if condition_desc else ""))

            pos = LegPosition(
                symbol=otm_leg["symbol"],
                quantity=quantity,
                entry_time=datetime.now(IST).isoformat(),
                entry_px=float(otm_leg["ltp"]),
                execution_id=self.execution_id,
            )
            self.store.state.position = pos
            self.store.state.current_option_type = option_type
            self.store.state.current_expiry = expiry
            self._save_state()
            self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])

        if pos.entry_filled or LEG_KEY in self._pending_fills:
            return

        if not pos.entry_order_id:
            try:
                # BUY to enter -- long options, opposite of the reference
                # script's SELL-to-enter naked-selling convention.
                pos.entry_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                            inst.options_exchange, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for entry: {exc}")
                self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(LEG_KEY)
        self._fill_executor.submit(
            self._watch_entry_fill, inst, pos.entry_order_id, pos.symbol, pos.quantity
        )

    def _watch_entry_fill(self, inst: InstrumentConfig, order_id: str, symbol: str, quantity: int):
        try:
            poll_fill(self.client, order_id, self.env.strategy_tag, symbol, inst.options_exchange,
                      "BUY", quantity)
            pos = self.store.state.position
            if pos.entry_order_id == order_id:
                pos.entry_filled = True
                self.store.state.trade_count += 1
                self._save_state()
                Log.info(f"[{LEG_KEY}] Entry filled: {symbol}")
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
        # BUY for a stuck entry, SELL for a stuck exit -- opposite of the
        # reference script's SELL/BUY (naked selling).
        action = "BUY" if error_state == "entry_failed" else "SELL"
        push_leg_error(self.env, LEG_KEY, pos, action=action)
        self._last_error_push[LEG_KEY] = datetime.now(IST)
        # WhatsApp self-alert on every genuine error-state transition (order
        # rejection, fill timeout, or any other place()/poll_fill failure
        # that routes here) -- fires once per transition, not on the
        # periodic _repush_active_errors re-push. Dispatched via
        # _pnl_executor (non-blocking, same pool this file already uses for
        # push_leg_error) and notify_telegram_error() itself never raises --
        # a WhatsApp/network hiccup can never break this method or the
        # calling run_cycle.
        try:
            self._pnl_executor.submit(
                notify_telegram_error, self.env,
                f"[{config.strategy_name}] {LEG_KEY} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        pos = self.store.state.position
        now = datetime.now(IST)
        if not pos.error_state:
            self._last_error_push.pop(LEG_KEY, None)
            return
        last = self._last_error_push.get(LEG_KEY)
        if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
            return
        self._last_error_push[LEG_KEY] = now
        action = "BUY" if pos.error_state == "entry_failed" else "SELL"
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

        self._bg_executor.submit(_run)

    def _pop_pending_action(self) -> Optional[dict]:
        return self._pending_action_cache.pop(LEG_KEY, None)

    def _exit_leg(self, reason: str = "unknown"):
        inst = INSTRUMENTS[0]
        pos = self.store.state.position

        if pos.exit_filled:
            self._finalize_exit(inst, reason)
            return

        if LEG_KEY in self._pending_fills:
            return

        if not pos.exit_order_id:
            try:
                # SELL to exit a long option -- opposite of the reference
                # script's BUY-to-exit (which closes a short).
                pos.exit_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                           inst.options_exchange, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for exit: {exc}")
                self._enter_error_mode("exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fills.add(LEG_KEY)
        self._fill_executor.submit(
            self._watch_exit_fill, inst, pos.exit_order_id, pos.symbol, pos.quantity
        )

    def _watch_exit_fill(self, inst: InstrumentConfig, order_id: str, symbol: str, quantity: int):
        try:
            poll_fill(self.client, order_id, self.env.strategy_tag, symbol, inst.options_exchange,
                      "SELL", quantity)
            pos = self.store.state.position
            if pos.exit_order_id == order_id:
                pos.exit_filled = True
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

    def _finalize_exit(self, inst: InstrumentConfig, reason: str):
        pos = self.store.state.position

        Log.info(f"[{LEG_KEY}] Position closed: {pos.symbol} reason={reason}")

        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(pos.symbol, inst.options_exchange,
                                                 max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange, require_two_sided=True)
        if exit_px is not None:
            # LONG convention -- profit when exit > entry, opposite sign
            # from the reference script's short-seller convention.
            self.store.state.today_realized_pnl += (exit_px - pos.entry_px) * pos.quantity
            entry_time_snapshot = pos.entry_time
            entry_px_snapshot = pos.entry_px
            quantity_snapshot = pos.quantity
            symbol_snapshot = pos.symbol
            self._save_state()
            try:
                append_trade_log(
                    self.env.strategy_tag, LEG_KEY, symbol_snapshot, quantity_snapshot,
                    entry_time_snapshot, entry_px_snapshot,
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
            [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
        )
        self.store.state.position = LegPosition()
        self.store.state.current_option_type = ""
        self.store.state.current_expiry = ""
        self._save_state()

        # Reversal/roll: if this exit was flagged with a pending target
        # option_type, submit the new entry NOW that the exit is genuinely
        # confirmed filled -- not gated behind waiting for the next candle
        # boundary (see module docstring's "Reversal" section for why this
        # sequencing matters: submitting both orders in the same cycle
        # without waiting for the first fill could leave two positions open
        # simultaneously). Covers BOTH a signal-driven reversal (target
        # option_type differs from what was just closed) and an
        # expiry-roll continuation (target option_type is the SAME as what
        # was just closed, just resolving against next month's expiry).
        pending_to = self.store.state.pending_reversal_to
        if pending_to:
            self.store.state.pending_reversal_to = ""
            self._save_state()
            self._submit_reversal_entry(inst, pending_to, reason)

    def _submit_reversal_entry(self, inst: InstrumentConfig, option_type: str, prior_reason: str):
        """Resolves a fresh spot/expiry/chain and submits the opposite (or,
        for a roll, same-direction-on-next-month) entry immediately after a
        confirmed exit. Runs synchronously on whichever thread called it
        (either _finalize_exit on the main cycle thread for a normal
        candle-driven exit, or a background executor thread if the exit
        confirmed asynchronously) -- the network calls here (LTP fetch,
        expiry resolution, chain fetch) are the same ones _enter_leg would
        make on a later cycle anyway; doing them here just avoids the
        extra cycle of latency the checklist's own "don't gate a
        fill-driven transition behind a candle boundary" principle warns
        against."""
        try:
            futures_symbol = self.store.state.futures_symbol
            if not futures_symbol:
                Log.warning(f"[{LEG_KEY}] Reversal/roll to {option_type} deferred -- "
                            f"futures_symbol not yet resolved.")
                return
            spot = self.price_stream.get_ltp(futures_symbol, inst.options_exchange,
                                              max_age=config.ws_stale_seconds)
            if spot is None:
                spot = fetch_symbol_ltp(self.ltp_client, futures_symbol, inst.options_exchange)
            if spot is None:
                Log.warning(f"[{LEG_KEY}] Reversal/roll to {option_type} deferred -- "
                            f"no spot price available this cycle, will retry next cycle "
                            f"via the normal entry-signal path.")
                return

            is_roll = prior_reason == "expiry_roll"
            if is_roll:
                # Roll target: whatever comes after the JUST-closed expiry
                # in the broker's own list -- not a fresh
                # resolve_current_month_expiry() call, which could still
                # return the same (about-to-roll) expiry if called too
                # early relative to its own roll_buffer_days.
                old_expiry = self._expiry_cache.get(inst.name) or self.store.state.current_expiry
                expiry = resolve_next_month_expiry(self.client, inst, old_expiry)
            else:
                expiry = resolve_current_month_expiry(
                    self.client, inst, roll_buffer_days=config.expiry_roll_days_before
                )
            self._expiry_cache[inst.name] = expiry
            chain = fetch_chain(self.client, inst, expiry, futures_symbol)
            self._chain_cache[inst.name] = chain

            condition_desc = "expiry_roll continuation" if is_roll else "reversal (opposite crossover)"
            self._enter_leg(inst, option_type, spot, futures_symbol, expiry, condition_desc=condition_desc)
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Reversal/roll entry to {option_type} failed: {exc}")

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    def _resolve_leg_error(self, inst: InstrumentConfig, action: dict):
        if LEG_KEY in self._pending_fills:
            return
        pos = self.store.state.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fills.add(LEG_KEY)
            ack_pending_action(self.env, LEG_KEY)
            self._fill_executor.submit(
                self._do_retry_resolution, inst, was_exit, kind
            )
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
                    [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
                )
                self.store.state.position = LegPosition()
                self.store.state.current_option_type = ""
                self.store.state.current_expiry = ""
                self._save_state()
                self._push_leg_error_bg(self.store.state.position, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            ack_pending_action(self.env, LEG_KEY)
            self._pending_fills.add(LEG_KEY)
            self._fill_executor.submit(
                self._watch_entry_cancel, inst, pos.error_order_id,
                pos.symbol, pos.quantity
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if was_exit:
                pos.exit_filled = True
                pos.manual_exit_px = fill_price
                # Manually Completed finalizes IMMEDIATELY, not on a later
                # cycle -- checklist item, same as the reference script.
                self._exit_leg(reason="manual_completion")
            else:
                pos.entry_filled = True
                pos.entry_px = fill_price
                self.store.state.trade_count += 1
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            self._push_leg_error_bg(pos, clear=True)
            ack_pending_action(self.env, LEG_KEY)

    def _do_retry_resolution(self, inst: InstrumentConfig, was_exit: bool, kind: str):
        try:
            pos = self.store.state.position
            if was_exit:
                if kind == "resting":
                    # SELL to exit -- cross to the bid, opposite of the
                    # reference script's BUY-to-exit (ask).
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
                            Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(pos, clear=True)
                self._pending_fills.discard(LEG_KEY)
                return

            if kind == "resting":
                # BUY to enter -- cross to the ask, opposite of the
                # reference script's SELL-to-enter (bid).
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
                        Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                             inst.options_exchange, "BUY", pos.quantity)
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
                self._watch_entry_fill, inst, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(LEG_KEY)

    def _watch_entry_cancel(self, inst: InstrumentConfig, order_id: str, symbol: str, quantity: int):
        try:
            # BUY (crossing to ask) -- opposite of the reference script's
            # SELL (crossing to bid) for this same one-last-chance flow.
            result = _reprice_and_wait_once(self.client, order_id, self.env.strategy_tag,
                                             symbol, inst.options_exchange, "BUY", quantity)
            pos = self.store.state.position
            if pos.error_order_id != order_id:
                return
            if result is not None:
                pos.entry_filled = True
                self.store.state.trade_count += 1
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                Log.info(f"[{LEG_KEY}] Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.client.cancelorder(order_id=order_id, strategy=self.env.strategy_tag)
                except Exception as exc:
                    Log.warning(f"[{LEG_KEY}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify "
                                f"manually at the broker that nothing is resting.")
                self.price_stream.remove_instruments(
                    [{"symbol": symbol, "exchange": inst.options_exchange}]
                )
                self.store.state.position = LegPosition()
                self.store.state.current_option_type = ""
                self.store.state.current_expiry = ""
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
            # Force Exit is an emergency override -- clear any pending
            # reversal/roll so a just-force-closed position doesn't
            # immediately re-enter the moment this exit confirms.
            self.store.state.pending_reversal_to = ""
            self._save_state()
            self._exit_leg(reason="force_exit")
            return False
        return True

    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()
            self._refresh_mcx_session_window_bg()
            self._refresh_expiry_roll_check_bg()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                pos = self.store.state.position
                if pos.error_state:
                    self._refresh_pending_action_bg()
                    pending = self._pop_pending_action()
                    if pending is not None:
                        self._resolve_leg_error(INSTRUMENTS[0], pending)
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- flat. Stopping.")
                    ack_force_exit_complete(self.env)
                    self._force_exit_pending = False
                return

            if not self._within_market_hours():
                return

            futures_symbol = self.store.state.futures_symbol
            if not futures_symbol:
                return

            # NOTE: deliberately NO past-universal-exit branch here -- this
            # strategy is positional (NRML, no daily square-off). See module
            # docstring. The only forced closes are Force Exit (above) and
            # the expiry roll (_refresh_expiry_roll_check_bg, dispatched
            # above every cycle).

            inst = INSTRUMENTS[0]
            pos = self.store.state.position

            if pos.error_state:
                # Frozen awaiting a Retry/Cancel/Manual decision -- checked
                # every cycle regardless of anything else below.
                self._refresh_pending_action_bg()
                pending = self._pop_pending_action()
                if pending is not None:
                    self._resolve_leg_error(inst, pending)
                return

            within_entry = self._within_entry_window()
            has_position = bool(pos.symbol)

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

            still_enterable = within_entry and not has_position
            signal = self.get_signal(inst, futures_symbol, ltp=inst_ltp, refresh_chain=still_enterable)
            if signal is None:
                return

            crossed_above = signal.vwap_prev2 <= signal.ema20_prev2 and signal.vwap_prev1 > signal.ema20_prev1
            crossed_below = signal.vwap_prev2 >= signal.ema20_prev2 and signal.vwap_prev1 < signal.ema20_prev1

            if has_position:
                exit_already_committed = bool(pos.exit_order_id) or pos.exit_filled
                option_type = self.store.state.current_option_type
                exit_condition = (
                    (option_type == "CE" and crossed_below)
                    or (option_type == "PE" and crossed_above)
                )
                if exit_condition or exit_already_committed:
                    if exit_condition and not exit_already_committed:
                        # The exit signal for a CALL (VWAP crosses below
                        # EMA20) is, by construction, the SAME event as a
                        # PUT entry signal -- and vice versa. Flag the
                        # reversal so _finalize_exit submits the opposite
                        # entry the moment this exit's fill confirms,
                        # rather than waiting for a later cycle.
                        reversal_to = "PE" if option_type == "CE" else "CE"
                        Log.info(f"[{LEG_KEY}] Exit condition met (reversal to {reversal_to}) -> closing.")
                        self.store.state.pending_reversal_to = reversal_to
                        self._save_state()
                    self._exit_leg(reason="vwap_ema_reversal")
                return

            if not within_entry:
                return

            slope_pct = ((signal.vwap_prev1 - signal.vwap_prev2) / config.vwap_slope_candles
                         / signal.close_prev1 * 100.0)
            gap_pct = abs(signal.vwap_prev1 - signal.ema20_prev1) / signal.close_prev1 * 100.0
            filters_pass = (
                abs(slope_pct) >= config.vwap_slope_pct_threshold
                and gap_pct >= config.vwap_gap_pct_threshold
            )

            entry_option_type = None
            if crossed_above and filters_pass and signal.rsi_prev1 > config.rsi_ce_threshold:
                entry_option_type = "CE"
            elif crossed_below and filters_pass and signal.rsi_prev1 < config.rsi_pe_threshold:
                entry_option_type = "PE"

            if entry_option_type is None:
                return

            expiry = self._expiry_cache.get(inst.name)
            if expiry is None:
                # Not yet resolved by the background chain refresh -- skip
                # this cycle rather than resolving inline (would block the
                # scheduler thread on a real client.expiry() round-trip).
                return

            condition_desc = (
                f"crossed_{'above' if entry_option_type == 'CE' else 'below'}=True, "
                f"slope_pct={slope_pct:.3f} (threshold {config.vwap_slope_pct_threshold}), "
                f"gap_pct={gap_pct:.3f} (threshold {config.vwap_gap_pct_threshold}), "
                f"rsi_prev1={signal.rsi_prev1:.2f}"
            )
            self._enter_leg(inst, entry_option_type, spot=signal.ltp, futures_symbol=futures_symbol,
                             expiry=expiry, condition_desc=condition_desc)

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
    print(f"Instrument           : {INSTRUMENTS[0].name} ({INSTRUMENTS[0].options_exchange})")
    print(f"Timeframe            : {config.intraday_interval}")
    print(f"VWAP / EMA / RSI     : session VWAP / EMA{config.ema_period} / RSI{config.rsi_period}")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Product              : {config.product} (positional -- no daily square-off)")
    print(f"Quantity             : {config.quantity_lots} lot(s)")
    print(f"Expiry roll          : {config.expiry_roll_days_before} trading day(s) before expiry")
    print(f"Max trades/day       : unlimited (flips only on genuine crossovers)")
    print("LONG OPTIONS -- risk capped at premium paid, no hedge leg")
    print("NO NATIVE PER-TRADE STOP-LOSS -- exit is VWAP/EMA crossover or expiry roll only")
    if config.test_mode:
        print("TEST MODE ENABLED -- market-hours/entry-window checks are BYPASSED")
    print("=" * 70)


def main():
    print_banner()

    env = Environment()
    _migrate_trade_log_if_needed(env.strategy_tag)
    state_store = StateStore(env)
    state_store.load()

    state_store.state.last_execution_id += 1
    execution_id = state_store.state.last_execution_id
    state_store.save()

    broker = Broker(env)
    client = broker.connect()
    ltp_client = broker.connect_ltp_client()

    price_stream = PriceStream(client)
    price_stream.start()

    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key and state_store.state.futures_symbol:
        already_known.append({
            "symbol": state_store.state.futures_symbol,
            "exchange": INSTRUMENTS[0].options_exchange,
        })
    if state_store.state.position.symbol:
        already_known.append({
            "symbol": state_store.state.position.symbol,
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

    pos = state_store.state.position
    if pos.error_state:
        action = "BUY" if pos.error_state == "entry_failed" else "SELL"
        push_leg_error(env, LEG_KEY, pos, action=action)
        Log.error(f"[{LEG_KEY}] Resuming with an unresolved error from before restart "
                  f"({pos.error_state}/{pos.error_kind}) -- needs Retry/Cancel/Manually Completed.")
    elif pos.symbol:
        inst = INSTRUMENTS[0]
        if pos.entry_order_id and not pos.entry_filled:
            Log.warning(f"[{LEG_KEY}] Resuming entry-fill watch for an order placed "
                        f"before a restart (order_id={pos.entry_order_id}).")
            engine._pending_fills.add(LEG_KEY)
            engine._fill_executor.submit(
                engine._watch_entry_fill, inst, pos.entry_order_id, pos.symbol, pos.quantity
            )
        elif pos.exit_order_id and not pos.exit_filled:
            Log.warning(f"[{LEG_KEY}] Resuming exit-fill watch for an order placed "
                        f"before a restart (order_id={pos.exit_order_id}).")
            engine._pending_fills.add(LEG_KEY)
            engine._fill_executor.submit(
                engine._watch_exit_fill, inst, pos.exit_order_id, pos.symbol, pos.quantity
            )
        else:
            Log.info(f"[{LEG_KEY}] Resuming an already-open position from before restart: "
                      f"{pos.symbol}@{pos.entry_px} ({state_store.state.current_option_type}) -- "
                      f"monitoring, not re-entering.")

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
