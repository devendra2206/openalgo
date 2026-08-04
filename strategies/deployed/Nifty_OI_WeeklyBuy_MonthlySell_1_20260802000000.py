"""
===============================================================================
NIFTY OI-Based Weekly Buy + Monthly Sell (Intraday)
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11

Design doc  : docs/plans/2026-08-02-oi-weekly-buy-monthly-sell-strategy-plan.md
              (the authoritative signal-logic reference -- this docstring is a
              summary, that file has the full derivation and every correction
              made during review).
Source spec : "NIFTY Intraday OI-Based Trading System - Functional
              Specification" (provided separately). Its cross-referenced
              CE/PE signal logic was corrected during review to a fully
              side-independent model -- see "Signal Logic" below. The plan
              doc's revision history documents each correction; this script
              implements only the final, corrected version.

Two independent engines, mirrored across CE and PE (4 possible concurrent
legs: WEEKLY_CE, WEEKLY_PE, MONTHLY_CE, MONTHLY_PE), sharing one Reference
Engine.

Reference Engine (once per day, at 09:30)
------------------------------------------------------------------------
Gap% = (09:30 NIFTY spot - previous close) / previous close x 100
  |Gap%| <= GAP_THRESHOLD (0.5%) -> Reference Time = previous day's close
  |Gap%| >  GAP_THRESHOLD        -> Reference Time = today's 09:35 candle
Fixed for the whole trading day once computed.

Signal Logic (own-side-only, CE and PE never cross-referenced -- CORRECTED)
------------------------------------------------------------------------
Standard OI-interpretation table, applied independently to each side's own
5-minute-candle premium (close) and Open Interest vs. that side's own value
at Reference Time:

    Premium   OI   Meaning          Verdict
    up        up   Long Build-up    Accumulation (bullish on this side)
    down      up   Short Build-up   Weakening    (bearish on this side)
    up        down Short Covering   Accumulation (bullish on this side)
    down      down Long Unwinding   Weakening    (bearish on this side)

  Weekly Buy (per side, independently):
    That side's own Accumulation -> Buy Weekly OTM1 <side>.
    Exit: 2 consecutive opposite-verdict candles on that leg's own frozen
    strike, OR 15:15, OR +100% profit (then immediately re-select a fresh
    OTM1 strike off current spot and re-enter if that side's signal still
    holds).

  Monthly Sell (per side, independently, gated same-side only):
    Requires BOTH, using only that side's own data at every step:
      1. Weekly's OWN <side> signal currently shows Weakening (never the
         opposite side, never Accumulation)
      2. Monthly's OWN selected <side> strike's own premium+OI also shows
         Weakening
    -> Sell Selected Monthly <side>.
    Exit: 2 consecutive opposite-verdict candles on that leg's own frozen
    strike -- ONLY exit condition, no profit target, no stop-loss, exactly
    as specified (confirmed decision, see plan doc SS7 item 2).

  A side's own Weekly signal therefore drives two different outcomes
  depending on which quadrant-group it lands in: Accumulation feeds that
  side's Weekly Buy; Weakening feeds that side's Monthly Sell gate -- never
  both at once for the same side. No re-entry on a side while that side's
  leg is already open (signal keeps evaluating/logging every candle for the
  2-consecutive-opposite exit tracking; a repeat same-direction signal is a
  logged no-op, not a duplicate order).

Strike Freeze
------------------------------------------------------------------------
Once any leg is open, its executed strike is locked: every later candle
re-evaluates the table on that EXACT strike only, never re-picks off
current spot/delta. A new strike for that side is only chosen after that
specific leg closes -- the other (up to 3) open legs are unaffected.

Data sources (each verified against this codebase's actual code, not just
docs -- see the plan doc SS2 for the verification trail)
------------------------------------------------------------------------
  - 5-min candle premium + OI: client.history(interval="5m") -- the public
    docs page doesn't mention the `oi` field, but services/history_service.py
    guarantees it's present in every response (0 if the broker doesn't
    supply it; real for NFO derivatives on Fyers).
  - Monthly delta-based strike (0.20-0.25): client.optiongreeks() -- a live,
    UNCACHED REST call, so only invoked when selecting a NEW monthly strike
    (leg flat), never on every candle.
  - Weekly/Monthly expiry dates: client.expiry(instrumenttype="options"),
    picked by date-distance logic (nearest = weekly; >20 days to expiry ->
    current month, else next month, per spec SS8).
  - Weekly OTM1 strike: client.optionchain() around current spot, picking
    the nearest REAL LISTED strike beyond ATM in the OTM direction (not an
    assumed fixed increment -- the spec only states a fixed 100-pt rounding
    for the Monthly strike, not Weekly OTM1).
  - Live LTP (spot, for strike selection, and each open leg, for the
    Weekly +100% profit target): WebSocket via PriceStream. OI is REST-only,
    never in the WS feed -- these are fully separate concerns on separate
    cadences by design (a slow/failed OI fetch never touches WS connection
    state, and a WS reconnect never blocks/skips an OI evaluation).

WebSocket reliability -- copied, not reimplemented
------------------------------------------------------------------------
PriceStream below is copied byte-for-byte from the already-hardened pattern
used by MCX_CrudeOil_EMA9_RSI_Intraday (itself sharing the same lineage as
Batman/VWAP_NoHA/Combined): per-symbol subscribe only, no unsubscribe before
a stale-retry resubscribe (Fyers' HSM protocol has no real per-symbol
unsubscribe -- a redundant unsub/resub cycle never once self-recovered a
stuck feed in production; a bare subscribe to an already-active token is a
safe, idempotent re-affirmation), majority-based REST-confirmed escalation
before a full reconnect, per-symbol independent backoff. This strategy's own
WS footprint is bounded at 5 symbols (spot + up to 4 legs) regardless of how
many legs are open.

Order placement, exception handling, PnL reporting
------------------------------------------------------------------------
Identical conventions to every other deployed script in this project:
place() retries ONLY a clean broker rejection, never an ambiguous exception
(duplicate-order risk); poll_fill()/_reprice_and_wait_once() cross the
spread with a fresh bid/ask rather than looping on a stale price;
report_pnl_to_platform() pushes one combined realized+unrealized snapshot
per tick; a background thread writes closed trades to
trades_{STRATEGY_ID}.csv; every entry log line prints the condition values
that fired it (Reference Time/OI/premium, current OI/premium, verdict).

Product: MIS (intraday). Underlying: NIFTY only. Quantity: 65 (1 lot,
NIFTY's current lot size) per leg. Capital tags: Rs 50,000 (Weekly Buy) /
Rs 2,50,000 (Monthly Sell), for reporting/return-on-capital display only --
does not gate order sizing.

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

import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api

# See MCX_CrudeOil_EMA9_RSI_Intraday's identical comment -- several
# always-on threads (fill-watchers, trade-log writer, PriceStream's own
# watchdog/WS threads) at the default 8MB stack size add up against the
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap every strategy subprocess runs
# under. Must be called before any thread is created.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, filter_known_fields
except ImportError:
    def notify_trade_closed(env, log_warning=None):
        pass

    def filter_known_fields(cls, raw):
        known = set(vars(cls()).keys())
        return {k: v for k, v in raw.items() if k in known}

load_dotenv()

print("OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
UNDERLYING_SYMBOL = "NIFTY"
UNDERLYING_SPOT_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"

OPTION_TYPES = ("CE", "PE")
# Every leg-slot this strategy can hold, independently, at once.
LEG_KEYS = ["WEEKLY_CE", "WEEKLY_PE", "MONTHLY_CE", "MONTHLY_PE"]


@dataclass
class Config:
    strategy_name: str = "NIFTY OI Weekly Buy + Monthly Sell Intraday"
    version: str = "1.0.0"

    intraday_interval: str = "5m"       # spec: "all calculations on 5-minute candle closes"
    history_lookback_days: int = 5      # enough to reliably find the last 2 closed 5m bars

    gap_threshold_pct: float = 0.5      # spec default
    reference_check_time: time = time(9, 30)
    reference_wait_time: time = time(9, 35)   # large-gap case waits for this candle to close

    weekly_profit_target_pct: float = 100.0   # Weekly Buy only
    monthly_delta_low: float = 0.20
    monthly_delta_high: float = 0.25
    monthly_strike_round: int = 100           # spec SS9: round Monthly strike to nearest 100
    monthly_expiry_roll_days: int = 20        # spec SS8: >20 days to expiry -> current month

    # Consecutive OPPOSITE-verdict candles (relative to the leg's own entry
    # trigger verdict) required to exit -- both engines, per spec.
    consecutive_opposite_exit: int = 2

    quantity: int = 65                  # NIFTY's current lot size (docs/prompt/LotSize.md) == 1 lot
    weekly_capital_tag: float = 50_000.0     # reporting/return-on-capital display only
    monthly_capital_tag: float = 250_000.0

    product: str = "MIS"
    price_type: str = "MARKET"

    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    weekly_universal_exit_time: time = time(15, 15)   # spec SS5.B
    monthly_universal_exit_time: time = time(15, 15)
    # No NEW entries (Weekly or Monthly, either side) from this time onward --
    # existing open legs are still managed/exited normally by their own rules.
    entry_cutoff_time: time = time(14, 45)

    scheduler_interval: int = 10
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
# OI + PREMIUM INTERPRETATION (the corrected core signal rule -- plan doc SS1.2)
###############################################################################
def classify_oi_premium(premium_change: float, oi_change: float) -> str:
    """Standard OI-interpretation table, applied to a SINGLE option's own
    premium and OI change vs. its own Reference value. Returns
    "accumulation" (bullish quadrants: Long Build-up / Short Covering),
    "weakening" (bearish quadrants: Short Build-up / Long Unwinding), or
    "flat" if either input is exactly zero (no clear direction -- treated
    as no-signal, never guessed either way)."""
    if premium_change == 0 or oi_change == 0:
        return "flat"
    if premium_change > 0 and oi_change > 0:
        return "accumulation"   # Long Build-up
    if premium_change < 0 and oi_change > 0:
        return "weakening"      # Short Build-up
    if premium_change > 0 and oi_change < 0:
        return "accumulation"   # Short Covering
    return "weakening"          # Long Unwinding (premium < 0 and oi < 0)


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

    # Set when an exit order is first dispatched (see WeeklySideEngine/
    # MonthlySideEngine._exit) -- carries the exit reason/reenter-flag across
    # the async gap to the background watcher's finalize step, so a restart
    # or a later cycle finalizing an already-filled exit uses the SAME reason
    # the original trigger decided on, not a value re-derived from scratch.
    pending_exit_reason: str = ""
    pending_exit_reenter: bool = False
    # The broker-confirmed exit fill price, set by _watch_exit_fill once the
    # background poll_fill() resolves -- kept separate from manual_exit_px
    # (an explicit user override via a future Retry/Cancel/Manual UI flow)
    # so the two carry distinct meanings.
    exit_fill_px: Optional[float] = None

    # Strike Freeze bookkeeping (plan doc SS1.5) -- captured at entry, used
    # for every subsequent candle's own-strike re-evaluation until exit.
    reference_oi: float = 0.0
    reference_premium: float = 0.0
    reference_open: float = 0.0         # candle open at the reference reading -- logging/audit only
    entry_trigger_verdict: str = ""     # "accumulation" (Weekly) or "weakening" (Monthly)
    consecutive_opposite: int = 0       # consecutive candles showing the OPPOSITE verdict
    delta_at_entry: float = 0.0         # Monthly only, informational

    # Order error recovery -- same shape/semantics as every other deployed
    # script in this project (docs/prd/python-strategies-order-error-recovery.md).
    error_state: str = ""
    error_kind: str = ""
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None


@dataclass
class LegState:
    trade_count: int = 0
    position: LegPosition = field(default_factory=LegPosition)


@dataclass
class ReferenceSnapshot:
    """Computed once per day at 09:30, fixed for the rest of the session --
    plan doc SS1.1. `mode` is "prev_close" or "today_0935"; `reference_time_iso`
    is the exact candle timestamp OI/premium lookups must use for every leg's
    Reference values for the rest of the day."""
    reference_date: str = ""
    gap_pct: float = 0.0
    mode: str = ""
    reference_time_iso: str = ""
    computed: bool = False


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    reference: ReferenceSnapshot = field(default_factory=ReferenceSnapshot)
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
            or "nifty_oi_weekly_buy_monthly_sell_intraday"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
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
            # PriceStream's own watchdog owns reconnect exclusively -- see
            # its docstring / MCX's identical rationale for why the SDK's
            # own auto_reconnect must stay off (racing reconnect owners
            # caused a repeating ~45-50s "connection down" cycle in
            # production that never settled).
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
# LIVE PRICE STREAM (WebSocket, dynamic subscription) -- copied byte-for-byte
# from MCX_CrudeOil_EMA9_RSI_Intraday's already-hardened PriceStream. Only
# needed for (a) NIFTY spot LTP (strike selection) and (b) each currently
# open leg's LTP (Weekly's +100% profit target). OI is never in the WS feed
# -- it's REST-only via history(), a fully separate code path on a separate
# cadence (see module docstring).
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

    def seed_instruments(self, instruments: list):
        """Populates the known-instrument set WITHOUT calling subscribe_ltp
        -- for main() to call BEFORE start(). start()'s background thread
        calls client.connect() and THEN subscribes to whatever's already in
        self._instruments in one batched call; calling the normal
        add_instruments() from the main thread right after start() (as
        before) could race that thread and fire its own separate
        subscribe_ltp() on a client that hasn't finished connect() yet.
        Seeding first means _connect()'s own initial subscribe is the only
        one that ever fires for the startup/restart-resume set."""
        with self._lock:
            for inst in instruments:
                key = (inst["symbol"], inst["exchange"])
                self._instruments[key] = inst

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
        Log.info("[PriceStream] connected"
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

            if all_keys and len(symbols_at_limit) > len(all_keys) / 2:
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
            # No unsubscribe_ltp() before this subscribe -- see module
            # docstring's "WebSocket reliability" section. A subscribe to an
            # already-subscribed token is a safe, idempotent re-affirmation.
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
        self.state.last_execution_id = data.get("last_execution_id", 0)
        ref_raw = data.get("reference", {})
        self.state.reference = ReferenceSnapshot(
            **{**asdict(ReferenceSnapshot()), **filter_known_fields(ReferenceSnapshot, ref_raw)}
        )
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
            "reference": asdict(self.state.reference),
            "legs": {
                k: {"trade_count": v.trade_count, "position": asdict(v.position)}
                for k, v in self.state.legs.items()
            },
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=2)


###############################################################################
# BROKER DATA HELPERS
###############################################################################
def _is_error_response(obj) -> bool:
    """quotes() always returns a dict; client.history() returns a pandas
    DataFrame on SUCCESS and an error dict (`{"status": "error", ...}`) on
    FAILURE -- the opposite of what this module originally assumed for
    history() (`isinstance(resp, dict)` as the success check), which
    silently discarded every real response as "no data" regardless of what
    the broker actually returned. Matches MCX_CrudeOil_EMA9_RSI_Intraday's
    proven `_is_error_response`, used for both call sites here."""
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


def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def resolve_weekly_expiry(client) -> tuple[str, str]:
    """Nearest upcoming NIFTY options expiry -- rolls to the NEXT expiry if
    today IS the resolved expiry day (same gamma/theta-cliff avoidance every
    other deployed script in this project uses). Returns
    (compact_ddmmmyy_for_symbol, raw_ddmmmyy_dash_for_history_lookups)."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve weekly options expiry: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    for i, raw in enumerate(dates_raw):
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            if d == today:
                if i + 1 < len(dates_raw):
                    return _compact_expiry(dates_raw[i + 1]), dates_raw[i + 1]
                raise RuntimeError(
                    "Today is the nearest weekly expiry and the broker returned no "
                    "later expiry date to roll to -- refusing to trade today's expiring contract."
                )
            return _compact_expiry(raw), raw
    raise RuntimeError(f"No upcoming weekly expiry found in: {dates_raw}")


def resolve_monthly_expiry(client) -> tuple[str, str]:
    """Spec SS8: current month's expiry if >20 days remain, else the next
    month's. `dates_raw` from client.expiry() is every live expiry
    (weekly + monthly mixed) sorted ascending -- the month-end dates are
    identifiable as the last expiry date falling within each calendar
    month."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve monthly options expiry: {resp}")
    today = datetime.now(IST).date()
    dates = sorted(datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"])
    dates = [d for d in dates if d >= today]
    if not dates:
        raise RuntimeError("No upcoming expiries at all.")

    # Group by (year, month), take the last (month-end) expiry in each group.
    month_ends: dict[tuple, "datetime.date"] = {}
    for d in dates:
        key = (d.year, d.month)
        if key not in month_ends or d > month_ends[key]:
            month_ends[key] = d
    current_month_key = (today.year, today.month)
    current_month_end = month_ends.get(current_month_key)

    if current_month_end is not None and (current_month_end - today).days > config.monthly_expiry_roll_days:
        chosen = current_month_end
    else:
        # Next month's month-end.
        future_keys = sorted(k for k in month_ends if k > current_month_key)
        if not future_keys:
            raise RuntimeError("No next-month expiry available to roll to.")
        chosen = month_ends[future_keys[0]]

    raw = chosen.strftime("%d-%b-%y")
    return _compact_expiry(raw), raw


def fetch_option_chain_strikes(client, expiry_compact: str, strike_count: int = 20) -> list[float]:
    """Real listed strikes around current spot, via optionchain() -- used so
    Weekly OTM1 selection picks an actual tradable strike rather than
    assuming a fixed 50/100-point increment (plan doc SS7 decision 8).

    optionchain()'s `expiry_date` expects the COMPACT form ("04AUG26", no
    dashes) -- confirmed against Nifty_Sensex_Expiry_Batman's working
    `fetch_chain()` call, which passes the same compact string this
    function's own `resolve_weekly_expiry()` already computes. Passing the
    raw dash form ("04-Aug-26") here instead was a real production bug
    (2026-08-03): the master contract genuinely had strike data for that
    expiry (confirmed -- another strategy traded it successfully the same
    day), but this call 404'd with "No strikes found" because of the
    format mismatch, not missing data."""
    resp = client.optionchain(
        underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
        expiry_date=expiry_compact, strike_count=strike_count,
    )
    # The chain rows live under the "chain" key, NOT "data" -- confirmed
    # against Nifty_Sensex_Expiry_Batman's working _legs_with_strike(), which
    # reads chain["chain"]. Checking "data" here (this function's own
    # earlier bug, 2026-08-03) meant a genuinely successful response
    # (status="success", real strikes under "chain") was always
    # misread as a failure, since "data" is never present in this response
    # shape at all.
    if resp.get("status") != "success" or not resp.get("chain"):
        raise RuntimeError(f"optionchain() failed for expiry {expiry_compact}: {resp}")
    chain = resp["chain"]
    strikes = sorted({float(row["strike"]) for row in chain if "strike" in row})
    if not strikes:
        raise RuntimeError(f"optionchain() returned no strikes for expiry {expiry_compact}")
    return strikes


def select_weekly_otm1_strike(client, spot: float, expiry_compact: str, option_type: str) -> float:
    """First REAL listed strike beyond ATM in the OTM direction for
    `option_type` -- CE is OTM above spot, PE is OTM below spot."""
    strikes = fetch_option_chain_strikes(client, expiry_compact)
    if option_type == "CE":
        candidates = [s for s in strikes if s > spot]
        if not candidates:
            raise RuntimeError(f"No listed CE strike above spot {spot} for expiry {expiry_compact}")
        return min(candidates)
    candidates = [s for s in strikes if s < spot]
    if not candidates:
        raise RuntimeError(f"No listed PE strike below spot {spot} for expiry {expiry_compact}")
    return max(candidates)


def select_monthly_delta_strike(client, spot: float, expiry_raw: str, option_type: str) -> tuple[float, float]:
    """Nearest 100-pt strike where |Delta| is within [monthly_delta_low,
    monthly_delta_high] (spec SS9), via optiongreeks() -- a live, uncached
    REST call, so only invoked at new-strike-selection time (leg flat),
    never on every candle (see module docstring). Scans outward from ATM in
    100-pt steps on the OTM side for `option_type`, since delta moves
    monotonically away from 0.50 as strikes go further OTM. Returns
    (strike, delta_at_selection)."""
    atm_100 = round(spot / config.monthly_strike_round) * config.monthly_strike_round
    direction = 1 if option_type == "CE" else -1
    # Bounded scan -- 20 steps of 100pts covers +/-2000 points from ATM,
    # comfortably beyond where a 0.20-0.25 delta strike would ever sit for
    # NIFTY's typical monthly IV/DTE combinations.
    for step in range(1, 21):
        strike = atm_100 + direction * step * config.monthly_strike_round
        symbol = f"{UNDERLYING_SYMBOL}{_compact_expiry(expiry_raw)}{int(strike)}{option_type}"
        try:
            resp = client.optiongreeks(symbol=symbol, exchange=OPTIONS_EXCHANGE)
        except Exception as exc:
            Log.warning(f"optiongreeks() failed for {symbol}: {exc}")
            continue
        if resp.get("status") != "success":
            Log.warning(f"optiongreeks() error for {symbol}: {resp}")
            continue
        delta = resp.get("greeks", {}).get("delta")
        if delta is None:
            continue
        if config.monthly_delta_low <= abs(float(delta)) <= config.monthly_delta_high:
            return strike, float(delta)
    raise RuntimeError(
        f"No {option_type} strike found within delta "
        f"[{config.monthly_delta_low}, {config.monthly_delta_high}] near spot {spot}, expiry {expiry_raw}"
    )


def fetch_candle_oi_premium(client, symbol: str, exchange: str,
                             at_or_before: Optional[datetime] = None) -> Optional[dict]:
    """Fetch OI + premium (close) for `symbol` from the last CLOSED 5-min
    candle at or before `at_or_before` (or simply the last closed candle if
    None -- the "current" reading case). Returns None on any failure --
    caller must skip this cycle's evaluation for this leg/side, never treat
    None as zero (a missing OI reading is not the same as OI being zero)."""
    end = datetime.now(IST).date()
    start = end - timedelta(days=config.history_lookback_days)
    try:
        bars = client.history(
            symbol=symbol, exchange=exchange, interval=config.intraday_interval,
            start_date=start.isoformat(), end_date=end.isoformat(),
        )
    except Exception as exc:
        Log.warning(f"history() failed for {symbol}.{exchange}: {exc}")
        return None
    if _is_error_response(bars):
        Log.warning(f"history() error response for {symbol}.{exchange}: {bars}")
        return None
    if bars is None or bars.empty:
        Log.warning(f"history() returned no data for {symbol}.{exchange}")
        return None

    if at_or_before is not None:
        bars = bars[bars.index <= at_or_before]
        if bars.empty:
            Log.warning(f"No {symbol}.{exchange} candle at/before {at_or_before}")
            return None

    # Drop the last row if it hasn't actually closed yet -- confirmed
    # against real production data that client.history()'s last row can be
    # the broker's LIVE, still-forming current candle (a candle read <2min
    # after its own start showed premium/OI materially different from its
    # own final values 5min later, once it had actually closed). This is
    # independent of at_or_before/_new_candle_closed()'s own boundary
    # tracking -- it protects against reading a forming candle no matter
    # *why* the read happened to land mid-candle (restart, a steady-state
    # boundary crossing, scheduler jitter, anything), by checking the
    # candle's own implied end-time against wall-clock now directly.
    candle_interval = timedelta(minutes=5)  # spec: 5-minute candles throughout (config.intraday_interval)
    if bars.index[-1] + candle_interval > datetime.now(IST):
        bars = bars.iloc[:-1]
        if bars.empty:
            Log.warning(f"Only a still-forming candle available for {symbol}.{exchange} -- "
                        f"no closed candle yet.")
            return None

    last = bars.iloc[-1]
    return {
        "premium": float(last["close"]),
        "open": float(last.get("open", last["close"])),
        "high": float(last.get("high", last["close"])),
        "low": float(last.get("low", last["close"])),
        "oi": float(last.get("oi", 0)),
        "timestamp": str(bars.index[-1]),
    }


###############################################################################
# ORDER PLACEMENT / FILL HANDLING -- identical conventions to every other
# deployed script in this project (docs/prd/python-strategies-order-error-recovery.md)
###############################################################################
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
    """Places an order. Only retries a CLEAN rejection response (nothing was
    placed, safe to retry) up to config.place_order_max_attempts times --
    deliberately does NOT retry a raised exception (ambiguous outcome, could
    duplicate a real order)."""
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
            (strategy_tag, leg_key, symbol, quantity, entry_time, entry_px,
             exit_time, exit_px, exit_reason, execution_id, is_short) = item
            log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
            is_new = not log_path.exists()
            # Weekly Buy is LONG (buy low, sell high = profit); Monthly Sell
            # is SHORT (sell high, buy back low = profit) -- pnl sign flips
            # accordingly per leg type.
            pnl_points = (exit_px - entry_px) if not is_short else (entry_px - exit_px)
            pnl_rupees = pnl_points * quantity
            # Display quantity signed (-ve for a short/Monthly leg) so the
            # Trades UI reads correctly without a separate direction column --
            # pnl above already used the unsigned quantity and is unaffected.
            display_quantity = -quantity if is_short else quantity
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
                      exit_reason: str, execution_id: int, is_short: bool):
    _ensure_trade_log_thread()
    _trade_log_queue.put((strategy_tag, leg_key, symbol, quantity,
                          entry_time, entry_px, exit_time, exit_px, exit_reason,
                          execution_id, is_short))


###############################################################################
# PLATFORM INTEGRATION (PnL push, error push, force exit) -- identical
# transport/conventions to every other deployed script in this project
###############################################################################
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
    """Pull whether the user has issued a Retry/Cancel/Manual action for this
    leg from the platform. Only ever called for a leg currently in error mode
    (near-zero legs, near-zero overhead on the normal per-cycle path). Returns
    None on any failure (transport error, or genuinely nothing pending) --
    degrades to "nothing pending yet, try again next cycle" rather than
    raising, matching report_pnl_to_platform's best-effort style."""
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
# REFERENCE ENGINE HELPERS (plan doc SS1.1)
###############################################################################
def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle -- e.g.
    for interval_minutes=5 at 10:07:42 IST, returns 10:05:00. Used by
    run_cycle() to evaluate signals exactly once per closed 5-min candle,
    not on every 10s scheduler tick."""
    now = datetime.now(IST)
    total_minutes = now.hour * 60 + now.minute
    bucket_start_minutes = (total_minutes // interval_minutes) * interval_minutes
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=bucket_start_minutes)


def resolve_previous_trading_day(today) -> "datetime.date":
    """Previous calendar day, skipping weekends. Does NOT consult the
    exchange holiday calendar (database/market_calendar_db.py) -- a known
    simplification: on the trading day immediately after an NSE holiday,
    this would resolve to the holiday date itself rather than the true
    previous trading day. fetch_reference_oi_premium's history() call for
    that date would then simply return no data (market was shut), which is
    already handled as "reference unavailable, retry next cycle" -- so this
    fails safe (skips the day, never fabricates a wrong reference) rather
    than fails silently wrong. Worth wiring to the real market calendar in
    a follow-up if holiday-adjacent gap misclassification is observed live."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def fetch_reference_oi_premium(client, symbol: str, exchange: str,
                                reference: "ReferenceSnapshot") -> Optional[dict]:
    """OI + premium at the day's fixed Reference Time -- either the previous
    trading day's last closed candle (mode == "prev_close") or today's
    09:35 candle close (mode == "today_0935"). Returns None on any failure
    -- caller must skip this cycle's evaluation, never treat None as zero."""
    if reference.mode == "prev_close":
        ref_date = datetime.fromisoformat(reference.reference_time_iso).date()
        try:
            bars = client.history(
                symbol=symbol, exchange=exchange, interval=config.intraday_interval,
                start_date=ref_date.isoformat(), end_date=ref_date.isoformat(),
            )
        except Exception as exc:
            Log.warning(f"history() (reference/prev_close) failed for {symbol}.{exchange}: {exc}")
            return None
        if _is_error_response(bars):
            Log.warning(f"history() (reference/prev_close) error response for {symbol}.{exchange}: {bars}")
            return None
        if bars is None or bars.empty:
            Log.warning(f"No {symbol}.{exchange} candle found on reference date {ref_date}")
            return None
        last = bars.iloc[-1]
        return {
            "premium": float(last["close"]),
            "open": float(last.get("open", last["close"])),
            "high": float(last.get("high", last["close"])),
            "low": float(last.get("low", last["close"])),
            "oi": float(last.get("oi", 0)),
            "timestamp": str(bars.index[-1]),
        }

    if reference.mode == "today_0935":
        at = datetime.fromisoformat(reference.reference_time_iso)
        return fetch_candle_oi_premium(client, symbol, exchange, at_or_before=at)

    Log.warning(f"fetch_reference_oi_premium called with unset reference mode for {symbol}.{exchange}")
    return None


###############################################################################
# PER-SIDE ENGINES -- one instance per option_type ("CE"/"PE"), fully
# independent of its sibling at every layer (plan doc SS1). Weekly and
# Monthly are separate classes because their entry trigger, exit rule, and
# order direction all differ; CE and PE are the SAME class instantiated
# twice because their logic is identical, mirrored -- this is the
# "parameterize, don't duplicate" way of guaranteeing CE and PE can never
# accidentally diverge or cross-reference each other.
###############################################################################
class WeeklySideEngine:
    """Buys OTM1 <option_type> on that side's own Accumulation (plan doc
    SS1.3). Never reads the other side's OI/premium/signal state."""

    def __init__(self, option_type: str, engine: "StrategyEngine"):
        self.option_type = option_type
        self.leg_key = f"WEEKLY_{option_type}"
        self.engine = engine

    @property
    def leg(self) -> LegState:
        return self.engine.state.legs[self.leg_key]

    def current_verdict(self) -> Optional[str]:
        """This side's verdict as computed THIS cycle (cached on the engine
        so MonthlySideEngine's same-side gate can read it without a second
        fetch). None if not yet computed this cycle -- callers must treat
        that as "gate not open", never guess."""
        detail = self.engine.latest_weekly_detail.get(self.option_type)
        return detail["verdict"] if detail else None

    def current_detail(self) -> Optional[dict]:
        """The full reading this side's verdict was computed from this
        cycle (candle timestamp, reference/current premium+OI) -- used so
        MonthlySideEngine's entry log can print the Weekly gate's own
        condition values, not just the word "weakening"."""
        return self.engine.latest_weekly_detail.get(self.option_type)

    def evaluate(self):
        if self.leg_key in self.engine._pending_fills:
            return  # a background fill-watcher is already resolving this leg
        pos = self.leg.position
        if pos.error_state:
            # Frozen awaiting a Retry/Cancel/Manual decision -- still checks
            # for a pending action every cycle so a user's click is picked up
            # promptly instead of only on the next restart.
            pending = check_pending_action(self.engine.env, self.leg_key)
            if pending is not None:
                self._resolve_leg_error(pending)
            return
        if pos.symbol:
            if not pos.entry_filled:
                return  # entry order placed but not yet confirmed -- watcher owns it
            if pos.exit_order_id and pos.exit_filled:
                self._finalize_exit(pos, pos.pending_exit_reason, pos.pending_exit_reenter)
                return
            self._manage_open_position()
        else:
            self._evaluate_entry()

    def _evaluate_entry(self):
        if datetime.now(IST).time() >= config.entry_cutoff_time:
            return  # no NEW entries (including a profit-target reenter) past cutoff
        spot = self.engine.get_spot_ltp()
        if spot is None:
            Log.warning(f"[WEEKLY_{self.option_type}] spot LTP unavailable this cycle.")
            return
        try:
            expiry_compact, expiry_raw = self.engine.get_weekly_expiry()
            strike = select_weekly_otm1_strike(self.engine.client, spot, expiry_compact, self.option_type)
        except RuntimeError as exc:
            Log.warning(f"[WEEKLY_{self.option_type}] strike selection failed: {exc}")
            return
        symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(strike)}{self.option_type}"

        current = fetch_candle_oi_premium(self.engine.client, symbol, OPTIONS_EXCHANGE)
        reference = fetch_reference_oi_premium(self.engine.client, symbol, OPTIONS_EXCHANGE, self.engine.state.reference)
        if current is None or reference is None:
            return
        premium_change = current["premium"] - reference["premium"]
        oi_change = current["oi"] - reference["oi"]
        verdict = classify_oi_premium(premium_change, oi_change)
        self.engine.latest_weekly_detail[self.option_type] = {
            "verdict": verdict, "strike": strike, "symbol": symbol,
            "candle": current["timestamp"],
            "cur_premium": current["premium"], "ref_premium": reference["premium"],
            "cur_open": current["open"], "ref_open": reference["open"],
            "cur_oi": current["oi"], "ref_oi": reference["oi"],
        }

        Log.info(
            f"[WEEKLY_{self.option_type}] candle={current['timestamp']} strike={strike} | "
            f"ref_open={reference['open']:.2f} cur_open={current['open']:.2f} | "
            f"ref_premium={reference['premium']:.2f} cur_premium={current['premium']:.2f} "
            f"premium_chg={premium_change:+.2f} | ref_oi={reference['oi']:.0f} "
            f"cur_oi={current['oi']:.0f} oi_chg={oi_change:+.0f} | verdict={verdict}"
        )

        if verdict != "accumulation":
            return

        self._enter(symbol, strike, reference, current)

    def _enter(self, symbol, strike, reference, current):
        leg = self.leg
        if leg.position.symbol:
            # Same-side re-entry guard (plan doc SS3/SS7#10): should be
            # unreachable given evaluate() already branches on
            # leg.position.symbol, kept as a defensive no-op.
            return

        # Persist the leg as "attempting entry" BEFORE calling place() --
        # matches MCX's _enter_leg. If place() itself raises (ambiguous:
        # broker error, network blip, even a lost-response after actually
        # submitting), _enter_error_mode needs a real leg.position with a
        # symbol to attach the error to -- and a restart in the narrow
        # window before place() returns finds pos.symbol set with no
        # order_id and no error_state, which reconcile_pending_orders()
        # flags for manual review rather than silently treating as open.
        leg.position = LegPosition(
            symbol=symbol, quantity=config.quantity,
            entry_time=datetime.now(IST).isoformat(), entry_px=0.0,
            entry_order_id="", entry_filled=False,
            execution_id=self.engine.execution_id,
            reference_oi=reference["oi"], reference_premium=reference["premium"],
            reference_open=reference["open"],
            entry_trigger_verdict="accumulation",
        )
        self.engine.save_state()

        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "BUY", config.quantity)
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] entry placeorder failed for {symbol}: {exc}")
            self._enter_error_mode("", "entry_failed", "terminal", "", str(exc))
            return

        # Persist entry_order_id (entry_filled=False) and SAVE TO DISK
        # immediately -- before waiting on the fill. If this process is
        # killed/restarted anywhere between here and the fill confirming,
        # main()'s startup reconciliation pass (see reconcile_pending_orders)
        # finds this order_id and checks its real status, instead of state
        # showing "flat" while a real broker-side order/position exists
        # untracked -- which would otherwise risk a duplicate entry next cycle.
        leg.trade_count += 1
        leg.position.entry_order_id = order_id
        self.engine.save_state()

        # The actual fill confirmation (poll_fill, up to fill_poll_timeout *
        # (1 + reprice_max_attempts) seconds) happens off this thread -- see
        # StrategyEngine.__init__'s note on _state_lock/_pending_fills/
        # _fill_executor. place() above is the only REST call left on the
        # signal-to-order path, so run_cycle stays fast for the other 3 legs.
        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(
            self._watch_entry_fill, order_id, symbol, strike, reference, current
        )

    def _watch_entry_fill(self, order_id, symbol, strike, reference, current):
        """Runs on _fill_executor, off the main scheduler thread. Must catch
        ALL exceptions internally -- this future's result is never awaited,
        so an uncaught exception here would vanish silently, leaving the leg
        stuck with entry_filled=False forever and no error_state/UI alert."""
        try:
            fill = poll_fill(self.engine.client, order_id, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "BUY", config.quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(order_id, "entry_failed", "resting", exc.order_id, str(exc))
            return
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(order_id, "entry_failed", "terminal", "", str(exc))
            return
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] entry fill-poll failed for {symbol}: {exc}")
            self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
            return
        finally:
            self.engine._pending_fills.discard(self.leg_key)

        pos = self.leg.position
        if pos.entry_order_id != order_id:
            return  # guard vs. a superseded/stale order (e.g. after a manual Cancel)
        entry_px = float(fill.get("average_price") or fill.get("price")
                          or (current["premium"] if current else pos.reference_premium))
        pos.entry_px = entry_px
        pos.entry_filled = True
        self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
        self.engine.save_state()
        if current is None or reference is None:
            # Retry-resumed fill -- no fresh candle snapshot survives the
            # Retry action (see _do_retry_resolution), so log a shorter form
            # instead of fabricating condition detail that was never re-read.
            Log.info(f"[WEEKLY_{self.option_type}] Entry filled (Retry-resumed): "
                     f"symbol={symbol}@{entry_px} qty={config.quantity} trade_count={self.leg.trade_count}")
            return
        ref = self.engine.state.reference
        Log.info(
            f"[WEEKLY_{self.option_type}] Entry filled: strike={strike} symbol={symbol}@{entry_px} "
            f"qty={config.quantity} | condition: reference_mode={ref.mode} "
            f"reference_time={ref.reference_time_iso} gap_pct={ref.gap_pct:+.3f}% "
            f"candle={current['timestamp']} ref_open={reference['open']:.2f} cur_open={current['open']:.2f} "
            f"ref_premium={reference['premium']:.2f} "
            f"cur_premium={current['premium']:.2f} premium_chg={current['premium']-reference['premium']:+.2f} "
            f"ref_oi={reference['oi']:.0f} cur_oi={current['oi']:.0f} "
            f"oi_chg={current['oi']-reference['oi']:+.0f} verdict=accumulation "
            f"trade_count={self.leg.trade_count}"
        )

    def _enter_error_mode(self, order_id, error_state, error_kind, error_order_id, message):
        pos = self.leg.position
        if pos.entry_order_id != order_id and pos.exit_order_id != order_id:
            return  # superseded/stale order -- don't overwrite a newer attempt's state
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        action = "BUY" if error_state == "entry_failed" else "SELL"
        push_leg_error(self.engine.env, self.leg_key, pos, action=action)
        self.engine.save_state()
        Log.error(f"[WEEKLY_{self.option_type}] {error_state} ({error_kind}): {message}")

    def _resolve_leg_error(self, action: dict):
        """Applies a Retry/Cancel/Manually-Completed decision pulled from the
        platform for this leg (see docs/prd/python-strategies-order-error-
        recovery.md). Mirrors MCX_CrudeOil_EMA9_RSI_Intraday's
        _resolve_leg_error exactly, adapted to Weekly's own action directions
        (entry=BUY, exit=SELL)."""
        if self.leg_key in self.engine._pending_fills:
            # A resolution (or a resumed watcher) is already in flight for
            # this leg -- leave the new action un-acked, picked up again once
            # _pending_fills clears, rather than racing a second concurrent
            # broker call against the same order.
            return
        pos = self.leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            # The actual broker calls run on _fill_executor, not inline --
            # a user clicking Retry must not block run_cycle's per-cycle
            # checks for every other leg for a full broker round-trip.
            self.engine._pending_fills.add(self.leg_key)
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._fill_executor.submit(self._do_retry_resolution, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                # Ignore the failed exit attempt entirely -- position stays
                # open; a fresh exit condition places a brand-new order later.
                pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            if kind == "terminal":
                self.engine.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}]
                )
                self.leg.position = LegPosition()
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, self.leg.position, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            # kind == "resting": one last honest re-price + bounded wait, then
            # an explicit cancelorder() if it still didn't fill -- never
            # silently abandoned. Ack'd immediately (not deferred until the
            # watcher finishes) so a later cycle's error-check can't re-read
            # this same still-"pending" action and dispatch a second watcher.
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._pending_fills.add(self.leg_key)
            self.engine._fill_executor.submit(
                self._watch_entry_cancel, pos.error_order_id, pos.symbol, pos.quantity
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if was_exit:
                pos.exit_filled = True
                pos.manual_exit_px = fill_price
                # evaluate()'s normal "exit_order_id and exit_filled ->
                # finalize" path runs next cycle and _finalize_exit prefers
                # manual_exit_px over the (unset) exit_fill_px.
            else:
                pos.entry_filled = True
                pos.entry_px = fill_price
                self.leg.trade_count += 1
                self.engine.price_stream.add_instruments(
                    [{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
            ack_pending_action(self.engine.env, self.leg_key)

    def _do_retry_resolution(self, was_exit: bool, kind: str):
        """The actual broker calls behind a Retry action (reprice via
        modifyorder, or a fresh place() for a terminal rejection) -- off the
        main thread, since _resolve_leg_error already added leg_key to
        _pending_fills and ack'd the action before submitting this."""
        try:
            pos = self.leg.position
            if was_exit:
                if kind == "resting":
                    # Weekly exit is a SELL (closing a long) -- cross the
                    # spread by hitting the bid, matching
                    # _reprice_and_wait_once's approach.
                    bid, _ask = fetch_symbol_bid_ask(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
                    if bid is not None:
                        try:
                            self.engine.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                                symbol=pos.symbol, action="SELL", exchange=OPTIONS_EXCHANGE,
                                price_type="LIMIT", product=config.product, quantity=str(pos.quantity),
                                price=str(bid), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[WEEKLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                    # pos.exit_order_id already equals error_order_id -- _exit()'s
                    # own logic resumes watching this same order.
                else:  # terminal -- nothing resting, a fresh exit order is placed next cycle
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
                self.engine._pending_fills.discard(self.leg_key)
                return

            # Entry side: unlike exit, evaluate() only ever calls
            # _evaluate_entry() when pos.symbol is empty -- an entry attempt
            # already in error mode has pos.symbol set, so evaluate() would
            # never resume it on its own. Retry directly resubmits the watcher.
            if kind == "resting":
                _bid, ask = fetch_symbol_bid_ask(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
                if ask is not None:
                    try:
                        self.engine.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                            symbol=pos.symbol, action="BUY", exchange=OPTIONS_EXCHANGE,
                            price_type="LIMIT", product=config.product, quantity=str(pos.quantity),
                            price=str(ask), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[WEEKLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                    f"resuming the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:  # terminal -- nothing resting, place a genuinely new order
                try:
                    resume_order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.symbol,
                                             OPTIONS_EXCHANGE, "BUY", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[WEEKLY_{self.option_type}] Retry's fresh place() failed again: {exc}")
                    pos.error_order_id = ""  # nothing to attribute this new failure to
                    self._enter_error_mode(pos.entry_order_id, "entry_failed", "terminal", "", str(exc))
                    self.engine._pending_fills.discard(self.leg_key)
                    return
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
            # _watch_entry_fill owns _pending_fills for this leg_key from here.
            # No strike/reference/current snapshot survives a restart-free
            # Retry, so the confirmation log falls back to a shorter form
            # (see _watch_entry_fill's own handling of strike=None).
            self.engine._fill_executor.submit(self._watch_entry_fill, resume_order_id, pos.symbol, None, None, None)
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] Retry resolution failed unexpectedly: {exc}")
            self.engine._pending_fills.discard(self.leg_key)

    def _watch_entry_cancel(self, order_id, symbol, quantity):
        """Cancel's one-last-chance flow for a still-`resting` entry order --
        never silently abandoned. Ends in exactly one of two definitive
        outcomes (filled anyway, or genuinely cancelled), unless something
        unexpected happens, in which case it re-enters error mode rather
        than leaving the leg stuck."""
        try:
            result = _reprice_and_wait_once(self.engine.client, order_id, self.engine.env.strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, "BUY", quantity)
            pos = self.leg.position
            if pos.error_order_id != order_id:
                return  # superseded by a newer action/order in the meantime
            if result is not None:
                pos.entry_filled = True
                pos.entry_px = float(result.get("average_price") or result.get("price") or pos.entry_px)
                self.leg.trade_count += 1
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                self.engine.save_state()
                Log.info(f"[WEEKLY_{self.option_type}] Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.engine.client.cancelorder(order_id=order_id, strategy=self.engine.env.strategy_tag)
                except Exception as exc:
                    Log.warning(f"[WEEKLY_{self.option_type}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify manually at the broker.")
                self.engine.price_stream.remove_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                self.leg.position = LegPosition()
                self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, self.leg.position, clear=True)
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
        finally:
            self.engine._pending_fills.discard(self.leg_key)

    def _manage_open_position(self):
        pos = self.leg.position

        ltp = self.engine.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
        if ltp is None:
            ltp = fetch_symbol_ltp(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)

        # Fetch this candle's own OI/premium reading and populate
        # latest_weekly_detail BEFORE any exit check below -- Monthly's
        # same-side gate reads this dict later THIS cycle regardless of
        # whether Weekly ends up exiting via profit target or universal
        # exit time. Populating it only in the "normal continuation" path
        # (as before) meant Monthly's confirmation #1 saw no reading at all
        # on the exact candle Weekly exited for one of those two reasons --
        # failing closed (safe, never a wrong trade) but silently missing a
        # legitimate Monthly entry opportunity on that one candle.
        current = fetch_candle_oi_premium(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
        verdict = None
        if current is not None:
            premium_change = current["premium"] - pos.reference_premium
            oi_change = current["oi"] - pos.reference_oi
            verdict = classify_oi_premium(premium_change, oi_change)
            self.engine.latest_weekly_detail[self.option_type] = {
                "verdict": verdict, "strike": None, "symbol": pos.symbol,
                "candle": current["timestamp"],
                "cur_premium": current["premium"], "ref_premium": pos.reference_premium,
                "cur_open": current["open"], "ref_open": pos.reference_open,
                "cur_oi": current["oi"], "ref_oi": pos.reference_oi,
            }

        if ltp is not None and pos.entry_px > 0:
            profit_pct = (ltp - pos.entry_px) / pos.entry_px * 100.0
            if profit_pct >= config.weekly_profit_target_pct:
                Log.info(f"[WEEKLY_{self.option_type}] profit target hit: {profit_pct:.1f}% "
                         f"(entry={pos.entry_px}, ltp={ltp})")
                self._exit(pos, ltp, "profit_target", reenter=True)
                return

        if datetime.now(IST).time() >= config.weekly_universal_exit_time:
            self._exit(pos, ltp if ltp is not None else pos.entry_px, "universal_exit_time", reenter=False)
            return

        if current is None:
            return

        if verdict == "weakening":
            pos.consecutive_opposite += 1
        elif verdict == "accumulation":
            pos.consecutive_opposite = 0
        # "flat": leave the streak unchanged -- ambiguous reading, not a signal.

        Log.info(
            f"[WEEKLY_{self.option_type}] (open) candle={current['timestamp']} symbol={pos.symbol} | "
            f"cur_open={current['open']:.2f} cur_premium={current['premium']:.2f} "
            f"premium_chg={current['premium']-pos.reference_premium:+.2f} "
            f"cur_oi={current['oi']:.0f} oi_chg={current['oi']-pos.reference_oi:+.0f} | verdict={verdict} "
            f"consecutive_opposite={pos.consecutive_opposite}/{config.consecutive_opposite_exit}"
        )

        if pos.consecutive_opposite >= config.consecutive_opposite_exit:
            self._exit(pos, ltp if ltp is not None else current["premium"], "opposite_signal", reenter=False)

    def _exit(self, pos, exit_px, reason, reenter: bool):
        if pos.exit_order_id and not pos.exit_filled:
            return  # already in flight -- defensive, evaluate()'s pending_fills guard covers this normally
        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.symbol,
                              OPTIONS_EXCHANGE, "SELL", pos.quantity)
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] exit placeorder failed for {pos.symbol}: {exc}")
            # Surfaced the same way as any other exit failure (Retry/Cancel/
            # Manual) -- whether this exit was triggered by the strategy's
            # own signal or by a UI-driven Force Exit, it's the same _exit()
            # call and therefore the same error-recovery path.
            self._enter_error_mode("", "exit_failed", "terminal", "", str(exc))
            return

        # Persist exit_order_id (exit_filled=False) immediately, same
        # crash-safety rationale as _enter() -- a restart between here and
        # the fill confirming must find this order_id via the startup
        # reconciliation pass, not silently forget an in-flight exit.
        # pending_exit_reason/reenter carry this decision across the async
        # gap so _finalize_exit (called once the watcher confirms the fill)
        # uses the same reason/reenter this trigger decided on.
        pos.exit_order_id = order_id
        pos.exit_filled = False
        pos.pending_exit_reason = reason
        pos.pending_exit_reenter = reenter
        self.engine.save_state()

        # Fill confirmation happens off this thread -- see _enter()/
        # _watch_entry_fill's matching note. place() above is the only REST
        # call left on the exit-signal-to-order path.
        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(
            self._watch_exit_fill, order_id, pos.symbol, pos.quantity, exit_px
        )

    def _watch_exit_fill(self, order_id, symbol, quantity, exit_px):
        try:
            fill = poll_fill(self.engine.client, order_id, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "SELL", quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(order_id, "exit_failed", "resting", exc.order_id, str(exc))
            return
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(order_id, "exit_failed", "terminal", "", str(exc))
            return
        except Exception as exc:
            Log.exception(f"[WEEKLY_{self.option_type}] exit fill-poll failed for {symbol}: {exc}")
            self._enter_error_mode(order_id, "exit_failed", "resting", order_id, str(exc))
            return
        finally:
            self.engine._pending_fills.discard(self.leg_key)

        pos = self.leg.position
        if pos.exit_order_id != order_id:
            return  # guard vs. a superseded/stale order
        pos.exit_fill_px = float(fill.get("average_price") or fill.get("price") or exit_px)
        pos.exit_filled = True
        self.engine.save_state()
        # Trade-log write / PnL update / re-entry happen on the main thread's
        # next evaluate() call (see _finalize_exit) -- kept off this watcher
        # thread so its body stays minimal, same split as MCX's design.

    def _finalize_exit(self, pos, reason, reenter):
        leg = self.leg
        # manual_exit_px (an explicit Manually-Completed override) takes
        # priority over the broker-confirmed exit_fill_px.
        if pos.manual_exit_px is not None:
            actual_exit_px = pos.manual_exit_px
        elif pos.exit_fill_px is not None:
            actual_exit_px = pos.exit_fill_px
        else:
            actual_exit_px = pos.entry_px
        pnl_points = actual_exit_px - pos.entry_px
        pnl_rupees = pnl_points * pos.quantity
        self.engine.state.today_realized_pnl += pnl_rupees
        append_trade_log(self.engine.env.strategy_tag, self.leg_key, pos.symbol, pos.quantity,
                          pos.entry_time, pos.entry_px, datetime.now(IST).isoformat(), actual_exit_px,
                          reason, pos.execution_id, is_short=False)
        Log.info(f"[WEEKLY_{self.option_type}] Position closed: {pos.symbol} reason={reason} "
                 f"pnl_rupees={pnl_rupees:.2f}")
        self.engine.price_stream.remove_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
        notify_trade_closed(self.engine.env, log_warning=Log.warning)
        leg.position = LegPosition()
        self.engine.save_state()

        if reenter:
            # Spec SS5: immediately re-select a fresh OTM1 strike off
            # current spot and re-enter if this side's signal still holds.
            self._evaluate_entry()


class MonthlySideEngine:
    """Sells the 0.20-0.25 delta Monthly <option_type> strike, gated by its
    SAME-SIDE Weekly sibling's own signal (never the opposite side) --
    plan doc SS1.4/SS7#12. Both confirmations (Weekly's own gate AND
    Monthly's own strike) must show Weakening; neither ever references the
    other option_type's data."""

    def __init__(self, option_type: str, engine: "StrategyEngine", weekly_sibling: WeeklySideEngine):
        self.option_type = option_type
        self.leg_key = f"MONTHLY_{option_type}"
        self.engine = engine
        self.weekly_sibling = weekly_sibling

    @property
    def leg(self) -> LegState:
        return self.engine.state.legs[self.leg_key]

    def evaluate(self):
        if self.leg_key in self.engine._pending_fills:
            return  # a background fill-watcher is already resolving this leg
        pos = self.leg.position
        if pos.error_state:
            pending = check_pending_action(self.engine.env, self.leg_key)
            if pending is not None:
                self._resolve_leg_error(pending)
            return
        if pos.symbol:
            if not pos.entry_filled:
                return  # entry order placed but not yet confirmed -- watcher owns it
            if pos.exit_order_id and pos.exit_filled:
                self._finalize_exit(pos, pos.pending_exit_reason)
                return
            self._manage_open_position()
        else:
            self._evaluate_entry()

    def _evaluate_entry(self):
        if datetime.now(IST).time() >= config.entry_cutoff_time:
            return  # no NEW entries past cutoff
        weekly_detail = self.weekly_sibling.current_detail()
        weekly_verdict = weekly_detail["verdict"] if weekly_detail else None
        if weekly_verdict != "weakening":
            return  # confirmation #1 not met -- same side only

        spot = self.engine.get_spot_ltp()
        if spot is None:
            Log.warning(f"[MONTHLY_{self.option_type}] spot LTP unavailable this cycle.")
            return
        try:
            expiry_compact, expiry_raw = self.engine.get_monthly_expiry()
            strike, delta = select_monthly_delta_strike(self.engine.client, spot, expiry_raw, self.option_type)
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] strike selection failed: {exc}")
            return
        symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(strike)}{self.option_type}"

        current = fetch_candle_oi_premium(self.engine.client, symbol, OPTIONS_EXCHANGE)
        reference = fetch_reference_oi_premium(self.engine.client, symbol, OPTIONS_EXCHANGE, self.engine.state.reference)
        if current is None or reference is None:
            return
        premium_change = current["premium"] - reference["premium"]
        oi_change = current["oi"] - reference["oi"]
        verdict = classify_oi_premium(premium_change, oi_change)

        Log.info(
            f"[MONTHLY_{self.option_type}] candle={current['timestamp']} strike={strike} delta={delta:.3f} | "
            f"weekly_gate: symbol={weekly_detail['symbol']} candle={weekly_detail['candle']} "
            f"ref_open={weekly_detail['ref_open']:.2f} cur_open={weekly_detail['cur_open']:.2f} "
            f"ref_premium={weekly_detail['ref_premium']:.2f} cur_premium={weekly_detail['cur_premium']:.2f} "
            f"ref_oi={weekly_detail['ref_oi']:.0f} cur_oi={weekly_detail['cur_oi']:.0f} "
            f"verdict={weekly_detail['verdict']}(confirmed) | "
            f"own: ref_open={reference['open']:.2f} cur_open={current['open']:.2f} "
            f"ref_premium={reference['premium']:.2f} "
            f"cur_premium={current['premium']:.2f} premium_chg={premium_change:+.2f} "
            f"ref_oi={reference['oi']:.0f} cur_oi={current['oi']:.0f} oi_chg={oi_change:+.0f} "
            f"verdict={verdict}"
        )

        if verdict != "weakening":
            return  # confirmation #2 not met

        self._enter(symbol, strike, delta, reference, current, weekly_detail)

    def _enter(self, symbol, strike, delta, reference, current, weekly_detail):
        leg = self.leg
        if leg.position.symbol:
            return

        # Persist "attempting entry" BEFORE calling place() -- same
        # crash-safety/error-routing rationale as WeeklySideEngine._enter().
        leg.position = LegPosition(
            symbol=symbol, quantity=config.quantity,
            entry_time=datetime.now(IST).isoformat(), entry_px=0.0,
            entry_order_id="", entry_filled=False,
            execution_id=self.engine.execution_id,
            reference_oi=reference["oi"], reference_premium=reference["premium"],
            reference_open=reference["open"],
            entry_trigger_verdict="weakening", delta_at_entry=delta,
        )
        self.engine.save_state()

        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "SELL", config.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] entry placeorder failed for {symbol}: {exc}")
            self._enter_error_mode("", "entry_failed", "terminal", "", str(exc))
            return

        leg.trade_count += 1
        leg.position.entry_order_id = order_id
        self.engine.save_state()

        # Fill confirmation happens off this thread -- see StrategyEngine.
        # __init__'s note on _state_lock/_pending_fills/_fill_executor.
        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(
            self._watch_entry_fill, order_id, symbol, strike, delta, reference, current, weekly_detail
        )

    def _watch_entry_fill(self, order_id, symbol, strike, delta, reference, current, weekly_detail):
        try:
            fill = poll_fill(self.engine.client, order_id, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "SELL", config.quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(order_id, "entry_failed", "resting", exc.order_id, str(exc))
            return
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(order_id, "entry_failed", "terminal", "", str(exc))
            return
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] entry fill-poll failed for {symbol}: {exc}")
            self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
            return
        finally:
            self.engine._pending_fills.discard(self.leg_key)

        pos = self.leg.position
        if pos.entry_order_id != order_id:
            return  # guard vs. a superseded/stale order
        entry_px = float(fill.get("average_price") or fill.get("price")
                          or (current["premium"] if current else pos.reference_premium))
        pos.entry_px = entry_px
        pos.entry_filled = True
        self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
        self.engine.save_state()
        if current is None or reference is None or weekly_detail is None:
            # Retry-resumed fill -- no fresh candle/gate snapshot survives
            # the Retry action (see _do_retry_resolution).
            Log.info(f"[MONTHLY_{self.option_type}] Entry filled (Retry-resumed): "
                     f"symbol={symbol}@{entry_px} qty={config.quantity} trade_count={self.leg.trade_count}")
            return
        ref = self.engine.state.reference
        Log.info(
            f"[MONTHLY_{self.option_type}] Entry filled: strike={strike} delta={delta:.3f} symbol={symbol}@{entry_px} "
            f"qty={config.quantity} | reference_mode={ref.mode} reference_time={ref.reference_time_iso} "
            f"gap_pct={ref.gap_pct:+.3f}% | weekly_gate: symbol={weekly_detail['symbol']} "
            f"candle={weekly_detail['candle']} ref_open={weekly_detail['ref_open']:.2f} "
            f"cur_open={weekly_detail['cur_open']:.2f} ref_premium={weekly_detail['ref_premium']:.2f} "
            f"cur_premium={weekly_detail['cur_premium']:.2f} ref_oi={weekly_detail['ref_oi']:.0f} "
            f"cur_oi={weekly_detail['cur_oi']:.0f} verdict=weakening(confirmed) | "
            f"own: candle={current['timestamp']} ref_open={reference['open']:.2f} cur_open={current['open']:.2f} "
            f"ref_premium={reference['premium']:.2f} "
            f"cur_premium={current['premium']:.2f} premium_chg={current['premium']-reference['premium']:+.2f} "
            f"ref_oi={reference['oi']:.0f} cur_oi={current['oi']:.0f} "
            f"oi_chg={current['oi']-reference['oi']:+.0f} verdict=weakening trade_count={self.leg.trade_count}"
        )

    def _enter_error_mode(self, order_id, error_state, error_kind, error_order_id, message):
        pos = self.leg.position
        if pos.entry_order_id != order_id and pos.exit_order_id != order_id:
            return  # superseded/stale order -- don't overwrite a newer attempt's state
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        action = "SELL" if error_state == "entry_failed" else "BUY"
        push_leg_error(self.engine.env, self.leg_key, pos, action=action)
        self.engine.save_state()
        Log.error(f"[MONTHLY_{self.option_type}] {error_state} ({error_kind}): {message}")

    def _resolve_leg_error(self, action: dict):
        """Applies a Retry/Cancel/Manually-Completed decision pulled from the
        platform for this leg. Mirrors WeeklySideEngine._resolve_leg_error,
        adapted to Monthly's own action directions (entry=SELL, exit=BUY)."""
        if self.leg_key in self.engine._pending_fills:
            return
        pos = self.leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self.engine._pending_fills.add(self.leg_key)
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._fill_executor.submit(self._do_retry_resolution, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            if kind == "terminal":
                self.engine.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}]
                )
                self.leg.position = LegPosition()
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, self.leg.position, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._pending_fills.add(self.leg_key)
            self.engine._fill_executor.submit(
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
                self.leg.trade_count += 1
                self.engine.price_stream.add_instruments(
                    [{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
            ack_pending_action(self.engine.env, self.leg_key)

    def _do_retry_resolution(self, was_exit: bool, kind: str):
        try:
            pos = self.leg.position
            if was_exit:
                if kind == "resting":
                    # Monthly exit is a BUY (buying back a short) -- cross
                    # the spread by hitting the ask.
                    _bid, ask = fetch_symbol_bid_ask(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
                    if ask is not None:
                        try:
                            self.engine.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                                symbol=pos.symbol, action="BUY", exchange=OPTIONS_EXCHANGE,
                                price_type="LIMIT", product=config.product, quantity=str(pos.quantity),
                                price=str(ask), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[MONTHLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
                self.engine._pending_fills.discard(self.leg_key)
                return

            # Entry side (SELL) -- unlike exit, evaluate() only calls
            # _evaluate_entry() when pos.symbol is empty, so Retry directly
            # resubmits the watcher rather than relying on the normal flow.
            if kind == "resting":
                bid, _ask = fetch_symbol_bid_ask(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
                if bid is not None:
                    try:
                        self.engine.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                            symbol=pos.symbol, action="SELL", exchange=OPTIONS_EXCHANGE,
                            price_type="LIMIT", product=config.product, quantity=str(pos.quantity),
                            price=str(bid), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[MONTHLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                    f"resuming the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:
                try:
                    resume_order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.symbol,
                                             OPTIONS_EXCHANGE, "SELL", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[MONTHLY_{self.option_type}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode(pos.entry_order_id, "entry_failed", "terminal", "", str(exc))
                    self.engine._pending_fills.discard(self.leg_key)
                    return
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, pos, clear=True)
            self.engine._fill_executor.submit(
                self._watch_entry_fill, resume_order_id, pos.symbol, None, None, None, None
            )
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] Retry resolution failed unexpectedly: {exc}")
            self.engine._pending_fills.discard(self.leg_key)

    def _watch_entry_cancel(self, order_id, symbol, quantity):
        """Cancel's one-last-chance flow for a still-`resting` entry order."""
        try:
            result = _reprice_and_wait_once(self.engine.client, order_id, self.engine.env.strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, "SELL", quantity)
            pos = self.leg.position
            if pos.error_order_id != order_id:
                return
            if result is not None:
                pos.entry_filled = True
                pos.entry_px = float(result.get("average_price") or result.get("price") or pos.entry_px)
                self.leg.trade_count += 1
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                self.engine.save_state()
                Log.info(f"[MONTHLY_{self.option_type}] Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.engine.client.cancelorder(order_id=order_id, strategy=self.engine.env.strategy_tag)
                except Exception as exc:
                    Log.warning(f"[MONTHLY_{self.option_type}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify manually at the broker.")
                self.engine.price_stream.remove_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                self.leg.position = LegPosition()
                self.engine.save_state()
            push_leg_error(self.engine.env, self.leg_key, self.leg.position, clear=True)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
        finally:
            self.engine._pending_fills.discard(self.leg_key)

    def _manage_open_position(self):
        pos = self.leg.position
        # No profit target, no stop-loss -- exactly as specified (plan doc
        # SS7#2). The two exits are 2 consecutive opposite-verdict candles on
        # this leg's own frozen strike, OR the universal exit time (MIS
        # intraday -- must not be left open into broker auto-square-off).
        if datetime.now(IST).time() >= config.monthly_universal_exit_time:
            ltp = self.engine.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
            if ltp is None:
                ltp = fetch_symbol_ltp(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
            self._exit(pos, ltp if ltp is not None else pos.entry_px, "universal_exit_time")
            return

        current = fetch_candle_oi_premium(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
        if current is None:
            return
        premium_change = current["premium"] - pos.reference_premium
        oi_change = current["oi"] - pos.reference_oi
        verdict = classify_oi_premium(premium_change, oi_change)

        if verdict == "accumulation":  # opposite of the entry trigger (weakening)
            pos.consecutive_opposite += 1
        elif verdict == "weakening":
            pos.consecutive_opposite = 0

        Log.info(
            f"[MONTHLY_{self.option_type}] (open) candle={current['timestamp']} symbol={pos.symbol} | "
            f"cur_open={current['open']:.2f} cur_premium={current['premium']:.2f} premium_chg={premium_change:+.2f} "
            f"cur_oi={current['oi']:.0f} oi_chg={oi_change:+.0f} | verdict={verdict} "
            f"consecutive_opposite={pos.consecutive_opposite}/{config.consecutive_opposite_exit}"
        )

        if pos.consecutive_opposite >= config.consecutive_opposite_exit:
            ltp = self.engine.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
            if ltp is None:
                ltp = fetch_symbol_ltp(self.engine.client, pos.symbol, OPTIONS_EXCHANGE)
            self._exit(pos, ltp if ltp is not None else current["premium"], "opposite_signal")

    def _exit(self, pos, exit_px, reason):
        if pos.exit_order_id and not pos.exit_filled:
            return  # already in flight -- defensive, evaluate()'s pending_fills guard covers this normally
        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.symbol,
                              OPTIONS_EXCHANGE, "BUY", pos.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] exit placeorder failed for {pos.symbol}: {exc}")
            self._enter_error_mode("", "exit_failed", "terminal", "", str(exc))
            return

        pos.exit_order_id = order_id
        pos.exit_filled = False
        pos.pending_exit_reason = reason
        self.engine.save_state()

        # Fill confirmation happens off this thread -- see _enter()/
        # _watch_entry_fill's matching note.
        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(
            self._watch_exit_fill, order_id, pos.symbol, pos.quantity, exit_px
        )

    def _watch_exit_fill(self, order_id, symbol, quantity, exit_px):
        try:
            fill = poll_fill(self.engine.client, order_id, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "BUY", quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(order_id, "exit_failed", "resting", exc.order_id, str(exc))
            return
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(order_id, "exit_failed", "terminal", "", str(exc))
            return
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] exit fill-poll failed for {symbol}: {exc}")
            self._enter_error_mode(order_id, "exit_failed", "resting", order_id, str(exc))
            return
        finally:
            self.engine._pending_fills.discard(self.leg_key)

        pos = self.leg.position
        if pos.exit_order_id != order_id:
            return  # guard vs. a superseded/stale order
        pos.exit_fill_px = float(fill.get("average_price") or fill.get("price") or exit_px)
        pos.exit_filled = True
        self.engine.save_state()

    def _finalize_exit(self, pos, reason):
        leg = self.leg
        if pos.manual_exit_px is not None:
            actual_exit_px = pos.manual_exit_px
        elif pos.exit_fill_px is not None:
            actual_exit_px = pos.exit_fill_px
        else:
            actual_exit_px = pos.entry_px
        # Short leg: sold high (entry_px), bought back (exit_px) -- profit
        # when exit_px < entry_px.
        pnl_points = pos.entry_px - actual_exit_px
        pnl_rupees = pnl_points * pos.quantity
        self.engine.state.today_realized_pnl += pnl_rupees
        append_trade_log(self.engine.env.strategy_tag, self.leg_key, pos.symbol, pos.quantity,
                          pos.entry_time, pos.entry_px, datetime.now(IST).isoformat(), actual_exit_px,
                          reason, pos.execution_id, is_short=True)
        Log.info(f"[MONTHLY_{self.option_type}] Position closed: {pos.symbol} reason={reason} "
                 f"pnl_rupees={pnl_rupees:.2f}")
        self.engine.price_stream.remove_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
        notify_trade_closed(self.engine.env, log_warning=Log.warning)
        leg.position = LegPosition()
        self.engine.save_state()


###############################################################################
# STRATEGY ENGINE (orchestrator)
###############################################################################
class StrategyEngine:
    def __init__(self, client, state_store: StateStore, env: Environment, price_stream: PriceStream,
                 execution_id: int, ltp_client):
        self.client = client
        self.state_store = state_store
        self.state = state_store.state
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id
        self.ltp_client = ltp_client

        self.weekly = {ot: WeeklySideEngine(ot, self) for ot in OPTION_TYPES}
        self.monthly = {ot: MonthlySideEngine(ot, self, self.weekly[ot]) for ot in OPTION_TYPES}
        self.latest_weekly_detail: dict[str, dict] = {}

        self._weekly_expiry_cache: Optional[tuple] = None
        self._monthly_expiry_cache: Optional[tuple] = None
        self._last_candle_boundary: Optional[datetime] = None

        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds -- doing that INSIDE run_cycle would stall every other leg's
        # entry/exit check for the same duration (all 4 legs are evaluated
        # sequentially on one scheduler thread). Order confirmation runs on a
        # per-leg background task instead (see WeeklySideEngine/MonthlySideEngine
        # _watch_entry_fill/_watch_exit_fill), submitted to _fill_executor, sized
        # to the leg count so all 4 legs can have a watcher in flight at once.
        # Mirrors the proven pattern in MCX_CrudeOil_EMA9_RSI_Intraday.
        self._fill_executor = ThreadPoolExecutor(max_workers=len(LEG_KEYS), thread_name_prefix="fill-watch")
        # Separate, single-worker pool for check_force_exit's HTTP poll -- kept
        # off _fill_executor so a Force Exit check can never queue silently
        # behind a fill watcher stuck for minutes in a reprice loop.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")
        # Dedicated single worker for report_pnl_to_platform's push, same
        # non-starvation reasoning -- PnL must never go stale because a fill
        # watcher is busy.
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnl")
        # _state_lock serializes every state_store.save() call (main thread and
        # background watchers alike) -- StateStore.save() writes the WHOLE state
        # to one shared JSON file, so two concurrent writers could corrupt it.
        # _pending_fills tracks which leg_keys already have an active watcher so
        # evaluate() never submits (or auto-manages) a duplicate one.
        self._state_lock = threading.Lock()
        self._pending_fills: set = set()
        # check_force_exit is a synchronous local HTTP call -- run inline on
        # the main scheduler thread it would be the same class of blocking
        # bug as poll_fill (see _bg_executor above). Dispatched every cycle
        # via _refresh_force_exit_check_bg instead; _force_exit_pending is
        # the cheap boolean run_cycle actually reads.
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False

    def save_state(self):
        """Every state_store.save() call in this engine goes through here --
        see _state_lock's docstring above for why concurrent writers need it."""
        with self._state_lock:
            self.state_store.save()

    def get_spot_ltp(self) -> Optional[float]:
        ltp = self.price_stream.get_ltp(UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        return fetch_symbol_ltp(self.client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE)

    def get_weekly_expiry(self) -> tuple[str, str]:
        today = datetime.now(IST).date().isoformat()
        if self._weekly_expiry_cache and self._weekly_expiry_cache[0] == today:
            return self._weekly_expiry_cache[1], self._weekly_expiry_cache[2]
        compact, raw = resolve_weekly_expiry(self.client)
        self._weekly_expiry_cache = (today, compact, raw)
        Log.info(f"Weekly expiry resolved: {raw} ({compact})")
        return compact, raw

    def get_monthly_expiry(self) -> tuple[str, str]:
        today = datetime.now(IST).date().isoformat()
        if self._monthly_expiry_cache and self._monthly_expiry_cache[0] == today:
            return self._monthly_expiry_cache[1], self._monthly_expiry_cache[2]
        compact, raw = resolve_monthly_expiry(self.client)
        self._monthly_expiry_cache = (today, compact, raw)
        Log.info(f"Monthly expiry resolved: {raw} ({compact})")
        return compact, raw

    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily state.")
            self.state.current_day = today_key
            self.state.today_realized_pnl = 0.0
            self.state.reference = ReferenceSnapshot()
            for leg in self.state.legs.values():
                leg.trade_count = 0
            self._weekly_expiry_cache = None
            self._monthly_expiry_cache = None
            self.save_state()

    def _ensure_reference(self):
        """Plan doc SS1.1 -- computed once, fixed for the rest of the day."""
        ref = self.state.reference
        today_key = datetime.now(IST).date().isoformat()
        if ref.reference_date == today_key and ref.computed:
            return

        now = datetime.now(IST).time()
        if now < config.reference_check_time:
            return  # too early, wait for 09:30

        spot_now = self.get_spot_ltp()
        if spot_now is None:
            Log.warning("[Reference] spot LTP unavailable, retrying next cycle.")
            return

        prev_day = resolve_previous_trading_day(datetime.now(IST).date())
        # pytz timezones must be applied via .localize(), never passed as a
        # naive tzinfo= argument -- datetime.combine(..., tzinfo=IST) would
        # silently stamp pytz's raw LMT offset (+5:53:20 for Asia/Kolkata,
        # a pre-standardization historical offset) instead of the correct
        # +5:30, corrupting every comparison against real IST timestamps.
        # Caught by strategies/test/test_oi_weekly_monthly_simulation.py.
        prev_close_at = IST.localize(datetime.combine(prev_day, time(15, 30)))
        prev_close_data = fetch_candle_oi_premium(
            self.client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE,
            at_or_before=prev_close_at,
        )
        if prev_close_data is None:
            Log.warning("[Reference] previous day's close unavailable, retrying next cycle.")
            return
        prev_close = prev_close_data["premium"]  # field name is generic; this is spot close here

        gap_pct = (spot_now - prev_close) / prev_close * 100.0

        if abs(gap_pct) <= config.gap_threshold_pct:
            ref.reference_date = today_key
            ref.gap_pct = gap_pct
            ref.mode = "prev_close"
            ref.reference_time_iso = prev_day.isoformat()
            ref.computed = True
            Log.info(f"[Reference] gap={gap_pct:+.3f}% (<= {config.gap_threshold_pct}%) -> "
                     f"Reference Time = previous day's close ({prev_day})")
            return

        if now < config.reference_wait_time:
            Log.info(f"[Reference] gap={gap_pct:+.3f}% (> {config.gap_threshold_pct}%) -- "
                     f"waiting for 09:35 candle to close.")
            return

        ref.reference_date = today_key
        ref.gap_pct = gap_pct
        ref.mode = "today_0935"
        ref.reference_time_iso = datetime.now(IST).replace(
            hour=9, minute=35, second=0, microsecond=0).isoformat()
        ref.computed = True
        Log.info(f"[Reference] gap={gap_pct:+.3f}% (> {config.gap_threshold_pct}%) -> "
                 f"Reference Time = today 09:35")

    def _new_candle_closed(self) -> bool:
        boundary = _current_candle_boundary(5)
        if self._last_candle_boundary is None:
            # First call after a fresh process start/restart -- the candle
            # active RIGHT NOW may have started only seconds ago and be
            # still forming at the broker (client.history()'s last row for
            # an in-progress candle is live/incomplete OHLC+OI, not a
            # genuinely closed reading). Treating "no boundary recorded
            # yet" the same as "boundary changed" made every restart
            # evaluate (and even enter) on that partial candle immediately.
            # Just record where we are and wait for the NEXT real crossing,
            # i.e. once the candle active at startup has actually finished.
            self._last_candle_boundary = boundary
            return False
        if self._last_candle_boundary != boundary:
            self._last_candle_boundary = boundary
            return True
        return False

    def _refresh_force_exit_check_bg(self):
        """Dispatches check_force_exit to _bg_executor every cycle instead of
        running it inline -- a human just clicked Force Exit and expects it
        picked up quickly, so this is intentionally not throttled to a slow
        interval. Guarded so a slow check that outlives one scheduler_interval
        isn't resubmitted on top of itself."""
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

    def run_cycle(self):
        try:
            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                if self._force_exit_all():
                    Log.warning("Force Exit complete -- all positions flat.")
                    self._force_exit_pending = False
                return

            self._reset_day_if_needed()
            if not _within_market_hours():
                return

            self._ensure_reference()
            if not self.state.reference.computed:
                return

            if not self._new_candle_closed():
                return  # only evaluate once per closed 5-min candle

            self.latest_weekly_detail = {}
            # Weekly BEFORE Monthly, same cycle -- Monthly's same-side gate
            # reads Weekly's freshly-computed verdict from this exact candle
            # (plan doc SS1.4).
            for ot in OPTION_TYPES:
                self.weekly[ot].evaluate()
            for ot in OPTION_TYPES:
                self.monthly[ot].evaluate()

            self.save_state()
        except Exception:
            Log.exception("run_cycle failed")

    def reconcile_pending_orders(self):
        """Startup-only crash recovery: finds any leg whose entry/exit order
        was placed but never confirmed filled before the previous process
        instance stopped (killed, redeployed, crashed) -- see _enter()/
        _exit()'s two-phase persistence (order_id saved to disk BEFORE
        waiting on the fill). Called once from main(), before the scheduler
        starts, so this synchronous orderstatus() round-trip (at most 4
        legs) costs nothing against the 5-min candle cadence.

        This is what makes a restart RESUME correctly instead of risking a
        duplicate entry: an already-fully-filled leg needs no reconciliation
        at all (its persisted position.symbol already routes evaluate() to
        _manage_open_position(), not _evaluate_entry()) -- this method only
        matters for the narrow crash window between placeorder() succeeding
        and the fill confirming, where state was deliberately left
        incomplete (entry_filled=False / exit_filled=False) on purpose."""
        for leg_key, leg in self.state.legs.items():
            pos = leg.position
            is_short = leg_key.startswith("MONTHLY_")

            if pos.entry_order_id and not pos.entry_filled and not pos.error_state:
                self._reconcile_one(leg_key, leg, pos, pos.entry_order_id, "entry", is_short)
            elif pos.exit_order_id and not pos.exit_filled and not pos.error_state:
                self._reconcile_one(leg_key, leg, pos, pos.exit_order_id, "exit", is_short)
            elif pos.symbol and not pos.entry_order_id and not pos.entry_filled and not pos.error_state:
                # The narrower crash window: _enter() now persists the leg
                # as "attempting entry" (symbol set, entry_order_id empty)
                # BEFORE calling place(), so place() can be retried on a
                # genuinely clean failure. If the process dies in the few
                # instructions between that persist and place() returning,
                # we genuinely don't know whether the broker ever saw the
                # order -- silently clearing it and letting evaluate() try
                # again risks a real DUPLICATE if place() actually succeeded
                # at the broker just before the crash. Never guess: flag for
                # a human to verify against the broker directly. Cancel (if
                # confirmed nothing was placed) or Manually Completed (if a
                # position genuinely exists) are the only safe resolutions --
                # Retry is available but the user must confirm no duplicate
                # exists first (the error message says so explicitly).
                pos.error_state = "entry_failed"
                pos.error_kind = "terminal"
                pos.error_order_id = ""
                pos.error_message = (
                    "Restart interrupted this leg between recording the attempt and the "
                    "broker's placeorder() response -- unknown whether an order/position "
                    "actually exists at the broker. Verify manually before choosing Retry "
                    "(risks a duplicate if one exists) -- prefer Cancel if nothing was "
                    "placed, or Manually Completed with the real fill price if it was."
                )
                pos.error_since = datetime.now(IST).isoformat()
                push_leg_error(self.env, leg_key, pos, action="BUY" if not is_short else "SELL")
                self.save_state()
                Log.error(f"[{leg_key}] reconcile: ambiguous pre-placeorder crash window -- "
                          f"flagged for manual verification against the broker.")

    def _reconcile_one(self, leg_key, leg, pos, order_id, phase, is_short):
        # Weekly entry = BUY, Weekly exit = SELL; Monthly entry = SELL
        # (short), Monthly exit = BUY (buy-to-cover) -- the informational
        # "action" pushed to the platform's error UI, not an order sent here.
        if phase == "entry":
            action = "SELL" if is_short else "BUY"
        else:
            action = "BUY" if is_short else "SELL"

        try:
            resp = self.client.orderstatus(order_id=order_id, strategy=self.env.strategy_tag)
        except Exception as exc:
            Log.exception(f"[{leg_key}] reconcile: orderstatus() failed for {order_id} -- "
                          f"flagging for manual review rather than guessing.")
            pos.error_state = "entry_failed" if phase == "entry" else "exit_failed"
            pos.error_kind = "resting"
            pos.error_order_id = order_id
            pos.error_message = f"reconcile after restart: orderstatus() failed: {exc}"
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, leg_key, pos, action=action)
            self.save_state()
            return

        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        Log.info(f"[{leg_key}] reconcile: {phase} order {order_id} status='{status}' (resuming after restart)")

        if status == "complete":
            fill_px = float(data.get("average_price") or data.get("price") or pos.entry_px or 0.0)
            if phase == "entry":
                pos.entry_px = fill_px
                pos.entry_filled = True
                self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
                Log.info(f"[{leg_key}] reconcile: entry {order_id} was actually filled @ {fill_px} -- "
                         f"resuming as an open position.")
            else:
                pnl_points = (fill_px - pos.entry_px) if not is_short else (pos.entry_px - fill_px)
                pnl_rupees = pnl_points * pos.quantity
                self.state.today_realized_pnl += pnl_rupees
                append_trade_log(self.env.strategy_tag, leg_key, pos.symbol, pos.quantity,
                                  pos.entry_time, pos.entry_px, datetime.now(IST).isoformat(), fill_px,
                                  "reconciled_after_restart", pos.execution_id, is_short=is_short)
                Log.info(f"[{leg_key}] reconcile: exit {order_id} was actually filled @ {fill_px} -- "
                         f"closing the leg (pnl_rupees={pnl_rupees:.2f}).")
                self.price_stream.remove_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
                leg.position = LegPosition()
            self.save_state()
            return

        if status in {"rejected", "cancelled", "canceled"}:
            if phase == "entry":
                # Never actually entered -- safe to clear. trade_count was
                # incremented optimistically in _enter(); undo that too.
                leg.trade_count = max(0, leg.trade_count - 1)
                leg.position = LegPosition()
                Log.info(f"[{leg_key}] reconcile: entry {order_id} was genuinely rejected/cancelled -- "
                         f"clearing the leg, safe to re-evaluate fresh.")
            else:
                # Exit never happened -- the leg is still legitimately open;
                # clear just the exit bookkeeping so normal continuation
                # evaluation picks it back up next cycle.
                pos.exit_order_id = ""
                pos.exit_filled = False
                Log.info(f"[{leg_key}] reconcile: exit {order_id} was genuinely rejected/cancelled -- "
                         f"leg remains open, will re-evaluate exit conditions normally.")
            self.save_state()
            return

        # Still resting/pending -- unknown how long it's been that way since
        # the restart; don't guess or block startup on a fresh poll_fill().
        # Flag for a human decision, same taxonomy as a normal poll_fill()
        # timeout (docs/prd/python-strategies-order-error-recovery.md).
        pos.error_state = "entry_failed" if phase == "entry" else "exit_failed"
        pos.error_kind = "resting"
        pos.error_order_id = order_id
        pos.error_message = f"reconcile after restart: order still '{status}', needs a decision."
        pos.error_since = datetime.now(IST).isoformat()
        push_leg_error(self.env, leg_key, pos, action=action)
        Log.error(f"[{leg_key}] reconcile: {phase} order {order_id} still '{status}' after restart -- "
                  f"needs Retry/Cancel/Manually Completed.")
        self.save_state()

    def _open_positions_for_pnl(self) -> list:
        open_positions = []
        for leg_key, leg in self.state.legs.items():
            pos = leg.position
            if not pos.symbol or not pos.entry_filled:
                # A leg mid-entry (order placed, fill not yet confirmed by
                # the async watcher) still has entry_px=0.0 -- including it
                # here would report a wildly wrong "profit" against that
                # zero, exactly the class of bug MCX's report_pnl_tick
                # already guards against.
                continue
            ltp = self.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
            if ltp is None:
                ltp = pos.entry_px  # last-known fallback -- never fabricate movement
            is_short = leg_key.startswith("MONTHLY_")
            pnl = (ltp - pos.entry_px) * pos.quantity if not is_short else (pos.entry_px - ltp) * pos.quantity
            open_positions.append({
                "leg_key": leg_key, "symbol": pos.symbol, "direction": "SHORT" if is_short else "LONG",
                # Display quantity signed (-ve for a short/Monthly leg) so it
                # reads correctly wherever this feeds a UI table -- the pnl
                # calc above already uses the unsigned pos.quantity with its
                # own is_short sign flip and is unaffected by this.
                "quantity": -pos.quantity if is_short else pos.quantity,
                "entry_price": pos.entry_px, "current_price": ltp, "pnl": pnl,
                "entry_time": pos.entry_time, "execution_id": pos.execution_id,
            })
        return open_positions

    def report_pnl_tick(self):
        try:
            report_pnl_to_platform(self.env, self.state.today_realized_pnl, self._open_positions_for_pnl())
        except Exception:
            Log.exception("report_pnl_tick failed")

    def _force_exit_all(self):
        """Force-closes every leg currently holding a position, regardless of
        the strategy's own signal/exit logic. Called every cycle while a
        Force Exit is pending (see check_force_exit) -- idempotent/resumable
        across cycles since _exit()'s own guard leaves a leg with an exit
        already in flight alone; the background watcher finishes it and a
        later cycle's evaluate()-style finalize check picks it up here. Only
        acks completion back to the platform once every leg is genuinely
        flat. A leg already in error_state is left untouched -- Force Exit
        doesn't override an unresolved Retry/Cancel/Manual decision."""
        all_flat = True
        for ot in OPTION_TYPES:
            weekly_pos = self.weekly[ot].leg.position
            if weekly_pos.error_state:
                all_flat = False
            elif weekly_pos.symbol:
                if weekly_pos.exit_order_id and weekly_pos.exit_filled:
                    self.weekly[ot]._finalize_exit(
                        weekly_pos, weekly_pos.pending_exit_reason, False
                    )
                else:
                    all_flat = False
                    if self.weekly[ot].leg_key not in self._pending_fills:
                        ltp = (self.price_stream.get_ltp(weekly_pos.symbol, OPTIONS_EXCHANGE,
                                                           config.ws_stale_seconds) or weekly_pos.entry_px)
                        self.weekly[ot]._exit(weekly_pos, ltp, "force_exit", reenter=False)

            monthly_pos = self.monthly[ot].leg.position
            if monthly_pos.error_state:
                all_flat = False
            elif monthly_pos.symbol:
                if monthly_pos.exit_order_id and monthly_pos.exit_filled:
                    self.monthly[ot]._finalize_exit(monthly_pos, monthly_pos.pending_exit_reason)
                else:
                    all_flat = False
                    if self.monthly[ot].leg_key not in self._pending_fills:
                        ltp = (self.price_stream.get_ltp(monthly_pos.symbol, OPTIONS_EXCHANGE,
                                                           config.ws_stale_seconds) or monthly_pos.entry_px)
                        self.monthly[ot]._exit(monthly_pos, ltp, "force_exit")
        self.save_state()
        if all_flat:
            ack_force_exit_complete(self.env)


###############################################################################
# MAIN
###############################################################################
def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version              : {config.version}")
    print(f"Underlying           : {UNDERLYING_SYMBOL}")
    print(f"Candle interval      : {config.intraday_interval}")
    print(f"Gap threshold        : {config.gap_threshold_pct}%")
    print(f"Weekly profit target : {config.weekly_profit_target_pct}%")
    print(f"Monthly delta range  : {config.monthly_delta_low}-{config.monthly_delta_high}")
    print(f"Quantity per leg     : {config.quantity}")
    print(f"Product              : {config.product}")
    print(f"Entry cutoff time    : {config.entry_cutoff_time.strftime('%H:%M')} (no new entries after)")
    print("WEEKLY BUY -- own-side Accumulation only. MONTHLY SELL -- own-side")
    print("Weakening gate AND own-side Weakening confirmation, no SL/target.")
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

    price_stream = PriceStream(client)

    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        already_known.append({"symbol": UNDERLYING_SYMBOL, "exchange": UNDERLYING_SPOT_EXCHANGE})
        for leg in state_store.state.legs.values():
            if leg.position.symbol:
                already_known.append({"symbol": leg.position.symbol, "exchange": OPTIONS_EXCHANGE})
    else:
        already_known.append({"symbol": UNDERLYING_SYMBOL, "exchange": UNDERLYING_SPOT_EXCHANGE})
    if already_known:
        # Seed BEFORE start() -- see seed_instruments' own docstring for why
        # this avoids a race between start()'s background _connect() and a
        # separate add_instruments() call from this (the main) thread.
        price_stream.seed_instruments(already_known)
    price_stream.start()

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

    # Crash-recovery reconciliation FIRST -- resolves the narrow
    # order-placed-but-not-yet-confirmed window (see reconcile_pending_orders'
    # docstring) before anything else touches state. An already-fully-filled
    # leg needs no reconciliation (it just resumes normally once the
    # scheduler starts, since evaluate() already routes a leg with a
    # persisted position.symbol straight to continuation/exit checks, never
    # back to entry) -- this only matters for orders interrupted mid-flight.
    engine.reconcile_pending_orders()

    for leg_key, leg in state_store.state.legs.items():
        if leg.position.error_state:
            action = "SELL" if leg.position.error_state == "entry_failed" else "BUY"
            push_leg_error(env, leg_key, leg.position, action=action)
            Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                      f"({leg.position.error_state}/{leg.position.error_kind}) -- "
                      f"needs Retry/Cancel/Manually Completed.")
        elif leg.position.symbol:
            Log.info(f"[{leg_key}] Resuming an already-open position from before restart: "
                      f"{leg.position.symbol}@{leg.position.entry_px} -- monitoring, not re-entering.")

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


if __name__ == "__main__":
    main()
