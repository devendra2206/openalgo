"""
===============================================================================
Nifty & Sensex Pivot+Supertrend / EMA34+RSI -- Combined Intraday Option Seller
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Combines    : Nifty_Sensex_Pivot_Supertrend_Intraday_1 (v1.10.0) and
              Nifty_Sensex_EMA34_RSI_Intraday_1 (v1.2.0) into ONE deployed
              process -- see each source script's own module docstring for
              the full per-strategy signal-logic writeup; this docstring
              covers only what changed by combining them.

Why this exists
------------------------------------------------------------------------
Both source strategies read the SAME underlyings (NIFTY/SENSEX) on the SAME
3-minute candle interval, on the SAME indicator_refresh_interval (15s), as
two entirely separate OpenAlgo Strategy Host processes. Running them
separately meant:
  - Two independent `client.history()` polls per underlying per cycle,
    unjittered and on the identical cadence -- a real risk of their bursts
    phase-aligning and spiking past the broker's real per-second rate
    ceiling (the exact class of problem flagged in both source scripts'
    "Live price feed" sections re: Shoonya's multiquotes() fan-out).
  - Two independent WebSocket subscriptions to the same 2 underlying
    symbols, each with its own watchdog/reconnect thread.
  - No natural decorrelation left between the two signal engines once they
    shared the same candle granularity -- a genuine risk of both engines
    firing the SAME direction on the SAME underlying at the SAME moment
    (e.g. both selling NIFTY PE simultaneously), doubling naked-option
    exposure on exactly the days volatility is already elevated, which
    neither strategy's own backtest could see in isolation.

This combined script is an INFRASTRUCTURE merge only -- NOT a signal
merge. Each engine's entry/exit conditions, thresholds, and independent
per-leg trade counters are copied verbatim from their respective source
scripts; there is no confluence requirement between them (a Pivot-engine
entry does not need EMA-engine agreement, and vice versa). What's shared:
  - ONE `client.history()` fetch per underlying per cycle -- both engines'
    indicators (Supertrend(7,3)+daily pivot, and EMA(34,High/Low)+RSI(14))
    are computed off the exact same fetched 3m bars dataframe.
  - ONE WebSocket subscription per underlying (PriceStream), shared by both
    engines' entry/exit LTP checks.
  - ONE strategy process / ONE strategy_tag -- registers as a single
    strategy in the Python Strategy Host UI, with a single combined PnL
    (report_pnl_to_platform sums realized+unrealized PnL across all 8 legs
    from both engines into one push).

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK ***
Both source strategies are literal, faithful ports with no hedge leg --
unchanged here.

8 independent legs, fully separated per engine
------------------------------------------------------------------------
LEG_KEYS = PIVOT_NIFTY_PE, PIVOT_NIFTY_CE, PIVOT_SENSEX_PE, PIVOT_SENSEX_CE,
           EMA_NIFTY_PE,   EMA_NIFTY_CE,   EMA_SENSEX_PE,   EMA_SENSEX_CE

Each leg has its own position, trade_count, state, and trade-log rows --
identical to running two separate 4-leg processes, just inside one. The
`PIVOT_`/`EMA_` prefix on every leg_key is what lets the single combined
Trades UI (reading one shared `trades_{strategy_tag}.csv`) segregate/filter
trades by engine without any new UI plumbing -- `leg_key` was already an
opaque string as far as blueprints/python_strategy.py is concerned, so this
naming convention is the ONLY change needed for that requirement.

Signal Rules -- copied verbatim from each source script
------------------------------------------------------------------------
PIVOT engine (per instrument, from Nifty_Sensex_Pivot_Supertrend_Intraday_1
v1.10.0 -- reverted to the original single-closed-candle logic, 3m candle):
  - r1, s1     = daily Pivot Points (standard) from the previous COMPLETED
                 trading day's H/L/C.
  - last_close, last_high, last_low = the last CLOSED 3m candle's own OHLC.
  - supertrend = Supertrend(7, 3) on 3m candles, last CLOSED bar.
  - ltp        = live LTP -- live breakout-confirmation filter only.

  PE entry : last_close > r1  AND supertrend < last_close  AND ltp > last_high
  CE entry : s1 > last_close  AND supertrend > last_close  AND ltp < last_low
  PE exit  : supertrend > ltp
  CE exit  : ltp > supertrend
  No per-trade stop-loss -- exit is the Supertrend(7,3) flip only.
  Entry window: 09:20 - 15:00.

EMA engine (per instrument, from Nifty_Sensex_EMA34_RSI_Intraday_1 v1.2.0,
unchanged, already 3m):
  - EMA(High, 34), EMA(Low, 34), RSI(14) on Close -- last CLOSED 3m bars.
  - close_prev1/prev2, high_prev2, low_prev2 = last two CLOSED 3m candles.
  - ltp = live LTP -- live breakout-confirmation filter only.

  PE entry : close_prev2 > ema_high34_prev2  AND ltp > close_prev1
             AND close_prev1 > high_prev2    AND rsi_prev1 > 53
  CE entry : close_prev2 < ema_low34_prev2   AND ltp < close_prev1
             AND close_prev1 < low_prev2     AND rsi_prev1 < 47
  PE exit  : close_prev1 < ema_low34_prev1  OR  rsi_prev1 < 47
  CE exit  : close_prev1 > ema_high34_prev1 OR  rsi_prev1 > 53
  No native per-trade stop-loss -- exit is the EMA/RSI reversal only.
  Entry window: 09:19 - 15:00 (kept distinct from PIVOT's 09:20 -- a
  literal-fidelity choice, not unified, since unifying them was never
  asked for and would be an unrequested behavior change to either source
  script).

Both engines share: universal exit >= 15:15 (force-close every open leg
regardless of its own exit condition), max 3 entries per leg per day, ATM
strike/current-week expiry resolved fresh on every entry (rolling to next
week's expiry on the underlying's own expiry day -- identical
`resolve_current_week_expiry()` logic, shared per-instrument, not
per-engine, since it's identical between the two sources), 1 lot per leg,
product=NRML, price_type=MARKET.

Everything else -- unchanged from both sources
------------------------------------------------------------------------
Order placement/reprice/poll_fill, the Retry/Cancel/Manually-Completed
error-recovery system, resumable leg-by-leg entry/exit via persisted order
IDs, the trade-log background writer, PnL reporting, Force Exit, and the
WebSocket PriceStream's stale-feed watchdog are all IDENTICAL to both
source scripts (they were already leg-key-generic, operating on
`LEG_KEYS`/`InstrumentConfig` without any engine-specific assumptions) --
copied here verbatim, just now operating over 8 legs instead of 4.

Fault isolation between the two engines
------------------------------------------------------------------------
`run_cycle()` wraps EACH instrument's per-leg loop in its own try/except
(not just one try/except around the whole cycle) -- an exception thrown
while evaluating one engine's signal or entry/exit condition for one
instrument does not stop the other engine's legs, or the other
instrument's legs, from being evaluated in the same cycle. This is new
relative to both source scripts (each only needed one top-level
try/except, since a single-engine script has nothing else running
alongside it to isolate from) -- the main new risk a merge introduces,
since a bug in one engine can no longer crash a fully separate OS process
without affecting the other.

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
import pandas as pd
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

load_dotenv()

print("Combined OpenAlgo Python Bot (Pivot+Supertrend / EMA34+RSI) is running.")

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

ENGINES = ("PIVOT", "EMA")


@dataclass
class Config:
    strategy_name: str = "Nifty & Sensex Pivot+Supertrend / EMA34+RSI Combined Intraday Seller"
    version: str = "1.0.0"

    intraday_interval: str = "3m"     # shared by both engines
    history_lookback_days: int = 10   # calendar days of 3m history to fetch -- covers both
                                       # Supertrend(7,3)'s and EMA(34)/RSI(14)'s warmup needs
    daily_interval: str = "D"

    # PIVOT engine
    supertrend_period: int = 7
    supertrend_multiplier: float = 3.0
    pivot_entry_start: time = time(9, 20)
    pivot_entry_end: time = time(15, 0)

    # EMA engine
    ema_period: int = 34
    rsi_period: int = 14
    pe_rsi_entry_threshold: float = 53.0
    ce_rsi_entry_threshold: float = 47.0
    pe_rsi_exit_threshold: float = 47.0
    ce_rsi_exit_threshold: float = 53.0
    ema_entry_start: time = time(9, 19)
    ema_entry_end: time = time(15, 0)

    # Shared across both engines
    lot_multiplier: int = 1           # number of lots per leg
    max_trades_per_leg_per_day: int = 3

    product: str = "NRML"             # literal port of both source configs
    price_type: str = "MARKET"

    universal_exit_time: time = time(15, 15)   # force-close everything at/after this
    market_close: time = time(15, 30)          # strategy actively runs the full session, 9:15-15:30

    # Entry/exit conditions compare a closed-candle indicator against LIVE
    # LTP -- the crossover moment itself can happen at any second, not just
    # on a 3m candle boundary. So the cycle runs frequently (near-continuous)
    # via `scheduler_interval`, but the expensive client.history() calls
    # (daily + 3m OHLC, ONE fetch per underlying serving BOTH engines) are
    # throttled separately via `indicator_refresh_interval` -- only LTP (a
    # cheap quotes() call) is fetched on every single cycle.
    scheduler_interval: int = 10             # seconds between strategy cycles (LTP check cadence)
    indicator_refresh_interval: int = 15     # seconds between re-fetching the shared 3m bars
    daily_refresh_interval: int = 600        # seconds between re-fetching the daily pivot (PIVOT engine only)
    pnl_tick_interval: float = 0.8             # seconds between PnL pushes -- runs on its OWN scheduler
                                               # job (see report_pnl_tick), decoupled from
                                               # scheduler_interval, since it's cache-only/read-only and
                                               # doesn't share the blocking-call risk that interval guards

    # WebSocket LTP cache: a tick older than this is treated as stale and
    # falls back to a one-off REST client.quotes() call for that instrument.
    ws_stale_seconds: float = 20.0
    ws_watchdog_interval: float = 15.0       # how often the reconnect watchdog checks staleness
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    # 5s per wait-cycle (1 initial + 59 reprices) = 60 x 5s = 300s (5 min)
    # total before giving up and raising OrderNeedsAttention -- matches both
    # source scripts' latest tuning.
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    state_file: str = "strategy_state.json"
    log_level: int = logging.INFO

    test_mode: bool = os.getenv("STRATEGY_TEST_MODE", "0") == "1"


config = Config()
IST = pytz.timezone("Asia/Kolkata")

# {engine: (entry_start, entry_end)} -- kept distinct per engine (literal
# fidelity to each source script), not unified.
ENGINE_ENTRY_WINDOW = {
    "PIVOT": (config.pivot_entry_start, config.pivot_entry_end),
    "EMA": (config.ema_entry_start, config.ema_entry_end),
}

LEG_KEYS = [
    f"{engine}_{inst.name}_{opt}"
    for engine in ENGINES
    for inst in INSTRUMENTS
    for opt in ("PE", "CE")
]

# Precomputed leg_key -> (engine, InstrumentConfig, option_type) so no call
# site ever has to string-split a leg_key to recover its instrument (the
# 3-part "ENGINE_INST_OPT" key can't be split the same naive way the
# original 2-part "INST_OPT" keys were in both source scripts).
LEG_META: dict[str, tuple[str, InstrumentConfig, str]] = {}
for _engine in ENGINES:
    for _inst in INSTRUMENTS:
        for _opt in ("PE", "CE"):
            LEG_META[f"{_engine}_{_inst.name}_{_opt}"] = (_engine, _inst, _opt)


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
    execution_id: int = 0          # which process run OPENED this leg
    # Order error recovery (see docs/prd/python-strategies-order-error-recovery.md)
    error_state: str = ""           # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""            # "" | "terminal" | "resting"
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None


@dataclass
class LegState:
    trade_count: int = 0
    position: LegPosition = field(default_factory=LegPosition)


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    last_updated: str = ""
    today_realized_pnl: float = 0.0  # sum of closed legs' pnl_rupees today, ACROSS BOTH ENGINES --
                                       # pushed as one combined number via report_pnl_to_platform
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
            or "nifty_sensex_pivot_ema_combined_intraday"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
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
        self.timeout attribute (a plain mutable instance attribute read at
        call-time; mutating it around one call while a background thread is
        mid-flight on the same client would race)."""
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
# LIVE PRICE STREAM (WebSocket) -- ONE subscription per underlying, shared by
# BOTH engines' entry/exit LTP checks (previously two separate subscriptions,
# one per process). See module docstring's "Why this exists" section.
###############################################################################
class PriceStream:
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
        """Called from _enter_leg once an option leg's symbol is known --
        both engines share this same PriceStream instance."""
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
        """Only removes a symbol once NO leg (from EITHER engine) still
        references it -- see _finalize_exit's call site, which checks that
        before calling this."""
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
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def resolve_current_week_expiry(client, inst: InstrumentConfig) -> str:
    """Nearest upcoming weekly expiry (DDMMMYY) for one instrument -- EXCEPT
    on the underlying's own expiry day itself, when it rolls to NEXT week's
    expiry instead. Shared per-instrument by BOTH engines (identical logic
    in both source scripts) -- not duplicated per-engine."""
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


###############################################################################
# SIGNAL COMPUTATION -- ONE shared 3m bars fetch per underlying feeds BOTH
# engines' indicators (this is the core of the infra merge -- see module
# docstring's "Why this exists" section).
###############################################################################
@dataclass
class PivotSignal:
    # r1/s1 are None until the daily-pivot fetch has succeeded at least once
    # for this instrument -- deliberately NOT required for the shared bars
    # fetch/EMA signal to exist (see compute_combined_signal's docstring).
    r1: Optional[float]
    s1: Optional[float]
    last_close: float
    last_high: float
    last_low: float
    supertrend: float


@dataclass
class EmaRsiSignal:
    close_prev1: float
    close_prev2: float
    high_prev2: float
    low_prev2: float
    ema_high34_prev1: float
    ema_high34_prev2: float
    ema_low34_prev1: float
    ema_low34_prev2: float
    rsi_prev1: float


@dataclass
class InstrumentSignal:
    """Combined per-instrument signal cache entry -- holds BOTH engines'
    indicators, computed off the same fetched bars, plus one shared LTP."""
    pivot: PivotSignal
    ema: EmaRsiSignal
    ltp: float
    candle_key: str


_last_logged_candle: dict[str, str] = {}


def fetch_daily_pivot(client, inst: InstrumentConfig) -> Optional[tuple]:
    """Fetch the previous COMPLETED day's daily OHLC and compute pivot
    R1/S1. PIVOT engine only. Cached on its own slow cadence
    (`config.daily_refresh_interval`) -- no reason to re-pull 30 days of
    daily bars on the same fast cadence used for the intraday signal."""
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
        daily = daily.iloc[:-1]
    prev_day = daily.iloc[-1]
    pivot, r1, s1, r2, s2, r3, s3 = ta.pivot_points(
        np.array([prev_day["high"]]), np.array([prev_day["low"]]), np.array([prev_day["close"]])
    )
    return float(np.asarray(r1)[0]), float(np.asarray(s1)[0])


def fetch_underlying_bars(client, inst: InstrumentConfig) -> Optional[pd.DataFrame]:
    """ONE `client.history()` fetch per underlying per cycle, serving BOTH
    engines. Drops the still-forming last candle (broker's last bar keeps
    updating well past its nominal close -- same defensive pattern used
    throughout this codebase). Requires enough bars for the LARGER of the
    two engines' warmup needs (EMA(34)'s +2 dominates over Supertrend(7)'s
    +1)."""
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
    if len(bars) >= 2:
        bars = bars.iloc[:-1]
    min_bars = max(config.supertrend_period + 1, config.ema_period + 2)
    if len(bars) < min_bars:
        Log.warning(
            f"[{inst.name}] only {len(bars)} {config.intraday_interval} bars after dropping "
            f"the still-forming one (need >= {min_bars} for a stable EMA{config.ema_period}) "
            f"-- no signal."
        )
        return None
    return bars


def compute_pivot_part(bars: pd.DataFrame, r1: Optional[float], s1: Optional[float]) -> PivotSignal:
    line, _direction = ta.supertrend(
        bars["high"], bars["low"], bars["close"],
        period=config.supertrend_period, multiplier=config.supertrend_multiplier,
    )
    return PivotSignal(
        r1=r1, s1=s1,
        last_close=float(bars["close"].iloc[-1]),
        last_high=float(bars["high"].iloc[-1]),
        last_low=float(bars["low"].iloc[-1]),
        supertrend=float(np.asarray(line)[-1]),
    )


def compute_ema_rsi_part(bars: pd.DataFrame) -> EmaRsiSignal:
    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)

    ema_high = np.asarray(ta.ema(high, config.ema_period))
    ema_low = np.asarray(ta.ema(low, config.ema_period))
    rsi = np.asarray(ta.rsi(close, config.rsi_period))

    return EmaRsiSignal(
        close_prev1=float(close[-1]), close_prev2=float(close[-2]),
        high_prev2=float(high[-2]), low_prev2=float(low[-2]),
        ema_high34_prev1=float(ema_high[-1]), ema_high34_prev2=float(ema_high[-2]),
        ema_low34_prev1=float(ema_low[-1]), ema_low34_prev2=float(ema_low[-2]),
        rsi_prev1=float(rsi[-1]),
    )


def compute_combined_signal(client, inst: InstrumentConfig, r1: Optional[float], s1: Optional[float],
                            ltp: Optional[float] = None) -> Optional[InstrumentSignal]:
    """Fetches 3m history ONCE, computes BOTH engines' indicators off it.
    Returns None (with a logged reason) only if the shared bars fetch itself
    fails -- that genuinely blocks both engines. r1/s1 may be None (daily
    pivot not yet available) WITHOUT blocking this -- the EMA engine has no
    dependency on daily pivot data at all, so it must not be held hostage by
    a slow/failed daily-pivot fetch; only the PIVOT engine's own entry check
    (in run_cycle) additionally requires r1/s1 to be non-None."""
    bars = fetch_underlying_bars(client, inst)
    if bars is None:
        return None

    pivot_sig = compute_pivot_part(bars, r1, s1)
    ema_sig = compute_ema_rsi_part(bars)
    candle_key = str(bars.index[-1])

    if ltp is None:
        ltp = fetch_ltp(client, inst)
    if ltp is None:
        ltp = pivot_sig.last_close  # fallback: closed-candle close is a reasonable proxy

    if _last_logged_candle.get(inst.name) != candle_key:
        _last_logged_candle[inst.name] = candle_key
        r1_str = f"{r1:.2f}" if r1 is not None else "pending"
        s1_str = f"{s1:.2f}" if s1 is not None else "pending"
        Log.info(
            f"[{inst.name}] candle={candle_key} close={pivot_sig.last_close:.2f} "
            f"high={pivot_sig.last_high:.2f} low={pivot_sig.last_low:.2f} "
            f"r1={r1_str} s1={s1_str} supertrend={pivot_sig.supertrend:.2f} | "
            f"ema_high34={ema_sig.ema_high34_prev1:.2f} ema_low34={ema_sig.ema_low34_prev1:.2f} "
            f"rsi={ema_sig.rsi_prev1:.2f} ltp={ltp:.2f}"
        )
    return InstrumentSignal(pivot=pivot_sig, ema=ema_sig, ltp=ltp, candle_key=candle_key)


def fetch_chain(client, inst: InstrumentConfig, expiry: str):
    resp = client.optionchain(
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
# TRADE LOG (background thread) -- ONE shared trades_{strategy_tag}.csv for
# BOTH engines. `leg` column (PIVOT_.../EMA_...) is what lets the Trades UI
# segregate by engine -- no new column, no UI change needed.
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
    def __init__(self, socket_path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _post_json_local(env: "Environment", path: str, payload: bytes, timeout: float = 3.0):
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
    """Pushes ONE combined PnL snapshot (both engines' realized+unrealized
    PnL summed together) to the Python Strategy Host -- this is what makes
    the UI show a single combined PnL for the whole strategy, with no
    per-engine breakdown needed at this layer (per-engine detail is still
    fully recoverable from the trade log's `leg` column)."""
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
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(LEG_KEYS), thread_name_prefix="fillwatch"
        )
        # One worker per instrument is enough for the shared signal/chain
        # refresh -- _signal_refresh_pending already prevents duplicate
        # submits. Separate from _fill_executor so a fill-watcher blocked
        # for minutes (reprice loop) can never silently starve indicator
        # refresh or Force Exit detection.
        self._signal_executor = ThreadPoolExecutor(
            max_workers=len(INSTRUMENTS), thread_name_prefix="sigrefresh"
        )
        self._expiry_cache: dict[str, str] = {}
        self._chain_cache: dict[str, dict] = {}
        self._signal_refresh_pending: set[str] = set()

    def _save_state(self):
        with self._state_lock:
            self.store.save()

    def _refresh_chain_cache(self, inst: InstrumentConfig):
        """Shared per-instrument, not per-engine -- both engines' ATM entry
        reads the exact same cached chain for a given instrument (identical
        resolve_current_week_expiry()/fetch_chain() logic in both sources)."""
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
                                       refresh_chain: bool, refresh_daily: bool):
        """Runs on _signal_executor. ONE client.history() call here feeds
        BOTH engines (see compute_combined_signal) -- this is the actual
        infra-sharing mechanism, not just a design description."""
        try:
            if refresh_daily:
                fresh_daily = fetch_daily_pivot(self.client, inst)
                if fresh_daily is not None:
                    self._daily_pivot_cache[inst.name] = fresh_daily
                    self._last_daily_refresh[inst.name] = datetime.now(IST)
        except Exception as exc:
            Log.warning(f"[{inst.name}] Background daily-pivot refresh failed (will retry "
                        f"next cycle): {exc}")

        # Deliberately NOT gated on daily pivot availability -- a slow/failed
        # daily-pivot fetch must only cost the PIVOT engine its entry signal
        # (handled in run_cycle by checking sig.r1/s1 for None), not take the
        # EMA engine's signal down with it. daily may legitimately be None
        # here (first cycle before the first daily fetch succeeds, or a
        # broker daily-history hiccup) -- compute_combined_signal handles
        # that via Optional r1/s1.
        daily = self._daily_pivot_cache.get(inst.name)
        r1, s1 = daily if daily is not None else (None, None)

        try:
            fresh = compute_combined_signal(self.client, inst, r1, s1, ltp=ltp)
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
        """ONE cached, throttled signal per instrument, shared by both
        engines. The daily pivot (R1/S1, PIVOT engine only) refreshes on its
        own slow cadence (config.daily_refresh_interval); the shared 3m bars
        (feeding BOTH engines) refresh at most once per
        config.indicator_refresh_interval. LTP is refreshed on every call.

        Returns None only if the shared signal itself was never computed
        (no bars fetched yet) -- NOT merely because the daily pivot hasn't
        arrived yet, since the EMA engine has no dependency on daily pivot
        data at all (see compute_combined_signal's docstring)."""
        now = datetime.now(IST)

        last_daily = self._last_daily_refresh.get(inst.name)
        due_daily = (last_daily is None
                     or (now - last_daily).total_seconds() >= config.daily_refresh_interval)
        last = self._last_indicator_refresh.get(inst.name)
        due_signal = (last is None
                      or (now - last).total_seconds() >= config.indicator_refresh_interval)

        if (due_daily or due_signal) and inst.name not in self._signal_refresh_pending:
            self._signal_refresh_pending.add(inst.name)
            self._signal_executor.submit(
                self._refresh_signal_and_chain_bg, inst, ltp, refresh_chain, due_daily
            )

        cached = self._signal_cache.get(inst.name)
        if cached is None:
            return None

        # Daily pivot may have refreshed independently of the shared signal
        # above (or may still be unavailable -- cached.pivot.r1/s1 then stay
        # whatever compute_combined_signal last set, possibly None).
        daily = self._daily_pivot_cache.get(inst.name)
        if daily is not None:
            cached.pivot.r1, cached.pivot.s1 = daily

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

    def _within_entry_window(self, engine: str) -> bool:
        if config.test_mode:
            return True
        now = datetime.now(IST).time()
        start, end = ENGINE_ENTRY_WINDOW[engine]
        return start <= now <= end

    def _within_market_hours(self) -> bool:
        return _within_market_hours()

    def _past_universal_exit(self) -> bool:
        if config.test_mode:
            return False
        return datetime.now(IST).time() >= config.universal_exit_time

    def report_pnl_tick(self):
        """Runs on its OWN scheduler job (0.8s), reading ONLY the WS price
        cache. Sums PnL across ALL 8 legs (both engines) into one combined
        push -- see report_pnl_to_platform's docstring."""
        try:
            open_positions = []
            for leg_key in LEG_KEYS:
                pos = self.store.state.legs[leg_key].position
                if not pos.symbol or not pos.entry_filled:
                    continue
                _, inst, _ = LEG_META[leg_key]
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
            chain = self._chain_cache.get(inst.name)
            if chain is None:
                Log.warning(f"[{leg_key}] Entry signal fired but the option chain "
                            f"cache isn't populated yet -- skipping this cycle, "
                            f"will retry once the background refresh completes.")
                return
            atm_leg = pick_atm_leg(chain, option_type, spot)
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
            poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                      "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.entry_order_id == order_id:
                pos.entry_filled = True
                leg.trade_count += 1
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled: {symbol}")
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
            poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                      "BUY", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.exit_order_id == order_id:
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
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        Log.info(f"[{leg_key}] Position closed: {pos.symbol}")

        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(pos.symbol, inst.options_exchange,
                                                 max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange)
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
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        # Only unsubscribe the option symbol from PriceStream once NO OTHER
        # leg (from either engine) is still holding the exact same option
        # symbol -- two engines on the same instrument can independently
        # pick the same ATM strike/type on the same day.
        symbol_still_in_use = any(
            other_leg.position.symbol == pos.symbol
            for other_key, other_leg in self.store.state.legs.items()
            if other_key != leg_key
        )
        if not symbol_still_in_use:
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
                push_leg_error(self.env, leg_key, pos, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            if kind == "terminal":
                # Only unsubscribe if no OTHER leg (either engine) still
                # holds this exact option symbol -- two engines can
                # independently pick the identical ATM strike/type on the
                # same day (see _finalize_exit's matching guard).
                symbol_still_in_use = any(
                    other_leg.position.symbol == pos.symbol
                    for other_key, other_leg in self.store.state.legs.items()
                    if other_key != leg_key
                )
                if not symbol_still_in_use:
                    self.price_stream.remove_instruments(
                        [{"symbol": pos.symbol, "exchange": inst.options_exchange}]
                    )
                leg.position = LegPosition()
                self._save_state()
                push_leg_error(self.env, leg_key, leg.position, clear=True)
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
            push_leg_error(self.env, leg_key, pos, clear=True)
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
                symbol_still_in_use = any(
                    other_leg.position.symbol == symbol
                    for other_key, other_leg in self.store.state.legs.items()
                    if other_key != leg_key
                )
                if not symbol_still_in_use:
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

        self._signal_executor.submit(_run)

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
                _, inst, _ = LEG_META[leg_key]
                self._exit_leg(leg_key, inst, reason="force_exit")
        return all_flat

    def run_cycle(self):
        try:
            self._reset_day_if_needed()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                for leg_key in LEG_KEYS:
                    leg = self.store.state.legs[leg_key]
                    if leg.position.error_state:
                        _, inst, _ = LEG_META[leg_key]
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
                    _, inst, _ = LEG_META[leg_key]
                    if leg.position.error_state:
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

            for inst in INSTRUMENTS:
                # Each instrument's block is independently try/except'd so a
                # failure evaluating one instrument's signal/legs (either
                # engine) cannot stop the OTHER instrument's legs, or the
                # other engine's legs on this SAME instrument, from being
                # evaluated this cycle. This is the fault-isolation this
                # merge needed that neither source script required on its
                # own (see module docstring).
                try:
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

                    # Any of the 4 legs (both engines) for this instrument
                    # still flat AND within ITS OWN engine's entry window --
                    # gates the background chain refresh.
                    still_enterable = any(
                        self._within_entry_window(engine)
                        and not self.store.state.legs[f"{engine}_{inst.name}_{ot}"].position.symbol
                        for engine in ENGINES for ot in ("PE", "CE")
                    )
                    signal = self.get_signal(inst, ltp=inst_ltp, refresh_chain=still_enterable)
                    if signal is None:
                        continue

                    for engine in ENGINES:
                        within_entry = self._within_entry_window(engine)
                        for option_type in ("PE", "CE"):
                            leg_key = f"{engine}_{inst.name}_{option_type}"
                            leg = self.store.state.legs[leg_key]

                            if leg.position.error_state:
                                pending = check_pending_action(self.env, leg_key)
                                if pending is not None:
                                    self._resolve_leg_error(leg_key, inst, pending)
                                continue

                            has_position = bool(leg.position.symbol)

                            if engine == "PIVOT":
                                sig = signal.pivot
                                # r1/s1 can be None (daily-pivot fetch hasn't
                                # succeeded yet, or is having a broker-side
                                # hiccup) WITHOUT that blocking the EMA
                                # engine's own signal -- see
                                # compute_combined_signal's docstring. The
                                # PIVOT engine simply can't enter until its
                                # own r1/s1 is available; exit doesn't need
                                # it at all, so it's unaffected.
                                if sig.r1 is None or sig.s1 is None:
                                    entry_condition = False
                                elif option_type == "PE":
                                    entry_condition = (
                                        sig.last_close > sig.r1
                                        and sig.supertrend < sig.last_close
                                        and signal.ltp > sig.last_high
                                    )
                                else:
                                    entry_condition = (
                                        sig.s1 > sig.last_close
                                        and sig.supertrend > sig.last_close
                                        and signal.ltp < sig.last_low
                                    )
                                exit_condition = (sig.supertrend > signal.ltp if option_type == "PE"
                                                   else signal.ltp > sig.supertrend)
                                exit_reason = "supertrend_flip"
                            else:  # engine == "EMA"
                                sig = signal.ema
                                if option_type == "PE":
                                    entry_condition = (
                                        sig.close_prev2 > sig.ema_high34_prev2
                                        and signal.ltp > sig.close_prev1
                                        and sig.close_prev1 > sig.high_prev2
                                        and sig.rsi_prev1 > config.pe_rsi_entry_threshold
                                    )
                                    exit_condition = (
                                        sig.close_prev1 < sig.ema_low34_prev1
                                        or sig.rsi_prev1 < config.pe_rsi_exit_threshold
                                    )
                                else:
                                    entry_condition = (
                                        sig.close_prev2 < sig.ema_low34_prev2
                                        and signal.ltp < sig.close_prev1
                                        and sig.close_prev1 < sig.low_prev2
                                        and sig.rsi_prev1 < config.ce_rsi_entry_threshold
                                    )
                                    exit_condition = (
                                        sig.close_prev1 > sig.ema_high34_prev1
                                        or sig.rsi_prev1 > config.ce_rsi_exit_threshold
                                    )
                                exit_reason = "ema_rsi_reversal"

                            if has_position:
                                exit_already_committed = bool(leg.position.exit_order_id) or leg.position.exit_filled
                                if exit_condition or exit_already_committed:
                                    if exit_condition and not exit_already_committed:
                                        Log.info(f"[{leg_key}] Exit condition met -> closing.")
                                    self._exit_leg(leg_key, inst, reason=exit_reason)
                                continue

                            if not within_entry:
                                continue
                            if leg.trade_count >= config.max_trades_per_leg_per_day:
                                continue
                            if not entry_condition:
                                continue

                            self._enter_leg(leg_key, inst, option_type, spot=signal.ltp)
                except Exception as exc:
                    Log.exception(f"[{inst.name}] Cycle failed for this instrument (other "
                                   f"instrument/engine unaffected): {exc}")

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
    print(f"Engines              : PIVOT (Supertrend {config.supertrend_period},"
          f"{config.supertrend_multiplier}) + EMA (EMA{config.ema_period}/RSI{config.rsi_period})")
    print(f"PIVOT entry window   : {config.pivot_entry_start} - {config.pivot_entry_end}")
    print(f"EMA entry window     : {config.ema_entry_start} - {config.ema_entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Max trades/leg/day   : {config.max_trades_per_leg_per_day}")
    print(f"Legs                 : {', '.join(LEG_KEYS)}")
    print("NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK")
    print("PIVOT engine: no native per-trade stop-loss -- exit is Supertrend flip only")
    print("EMA engine: no native per-trade stop-loss -- exit is EMA/RSI reversal only")
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

    ws_instruments = [
        {"exchange": inst.underlying_exchange, "symbol": inst.name} for inst in INSTRUMENTS
    ]
    price_stream = PriceStream(client, ws_instruments)
    price_stream.start()

    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        for leg_key, leg in state_store.state.legs.items():
            if leg.position.symbol:
                _, inst, _ = LEG_META[leg_key]
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
        if pos.entry_order_id and not pos.entry_filled:
            Log.warning(f"[{leg_key}] Resuming entry-fill watch for an order placed "
                        f"before a restart (order_id={pos.entry_order_id}).")
            engine._pending_fills.add(leg_key)
            _, inst, _ = LEG_META[leg_key]
            engine._fill_executor.submit(
                engine._watch_entry_fill, leg_key, inst, pos.entry_order_id,
                pos.symbol, pos.quantity
            )
        elif pos.exit_order_id and not pos.exit_filled:
            Log.warning(f"[{leg_key}] Resuming exit-fill watch for an order placed "
                        f"before a restart (order_id={pos.exit_order_id}).")
            engine._pending_fills.add(leg_key)
            _, inst, _ = LEG_META[leg_key]
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
        engine._signal_executor.shutdown(wait=False)
    except Exception:
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._signal_executor.shutdown(wait=False)
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
