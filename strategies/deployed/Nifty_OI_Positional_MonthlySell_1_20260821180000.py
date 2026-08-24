"""
===============================================================================
NIFTY OI-Based Positional Monthly Option Selling
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11

Sister strategy to Nifty_OI_WeeklyBuy_MonthlySell_1 (same OI-signal
philosophy, monthly instrument, proven live) -- but this one is POSITIONAL
(multi-day held), sells a single monthly CE and/or PE selected PURELY by
target delta (0.27, scanned within a 0.22-0.32 band, no premium filter at
all), each protected by its own monthly hedge (premium-target selected,
~20% of the sold strike's premium, further OTM in the same direction), with
a tiered stop-loss (38.2% flat, permanently ratcheting to breakeven once
premium ever touches 50% of entry) monitored continuously via live
WebSocket LTP. One leg per side (MONTHLY_CE / MONTHLY_PE); no weekly buy
leg at all.

Strike qualification (both required)
------------------------------------------------------------------------
  1. Delta closest to 0.27 among round-100 strikes (0.22-0.32 is only a
     "stop widening the scan" heuristic, never a hard filter).
  2. That exact strike's own OI signal classifies "accumulation" (short
     covering or long unwinding) via classify_oi_premium(premium_change,
     oi_change), computed against the day's Reference Snapshot (same
     _ensure_reference()/gap-threshold mechanism as
     Nifty_OI_WeeklyBuy_MonthlySell, but reading 15-minute candles here,
     not 5-minute).

No premium constraint anywhere in strike selection -- whatever premium the
winning strike carries is simply read off afterward for entry price, SL
thresholds, and hedge sizing.

Entry
------------------------------------------------------------------------
9:30 AM entry fires ONLY on the first trading day of a new monthly expiry
cycle (the day after the prior contract's own expiry, derived purely from
the expiry calendar). CE qualifies -> sell CE + buy its hedge. PE
qualifies -> sell PE + buy its hedge. Both can qualify independently.

Stop-loss -- tiered, permanent ratchet, continuous
------------------------------------------------------------------------
  Tier 1 (default): exit once current_premium >= entry_premium * 1.382.
  Tier 2 (once triggered, permanent): the moment premium has ever touched
    <= 50% of entry premium, the SL locks to the entry premium itself
    (breakeven) for the rest of the trade -- never reverts even if premium
    later climbs back above 50%.
Checked on every WS tick (not gated to a candle boundary); on breach,
closes the short leg first, then the hedge leg, immediately.

Hedging
------------------------------------------------------------------------
Monthly hedge only. Target premium ~= 20% of the sold strike's CURRENT
premium, further OTM in the same direction as the sold leg, selected by
closest observed premium to that target (never carried forward as a fixed
strike -- re-selected fresh every time the short leg is (re-)selected).
Modeled on Nifty_Sensex_Expiry_Batman's pick_leg_by_target_ltp pattern.

Monthly rollover / expiry-day safety
------------------------------------------------------------------------
resolve_monthly_expiry() (day > 15 -> next month's contract) feeds ONLY
new-entry strike search -- it never force-closes an already-open position.
Separately, if an open position's OWN contract expires today, it is
force-closed (short then hedge) unconditionally at the 3:15 daily
decision -- terminal for the day, no new-position search follows.

3:15 PM daily decision (every day that is NOT the 9:30 entry day)
------------------------------------------------------------------------
  - Resolve the active expiry (informational for whatever fires below).
  - Position open and its contract expires today -> force-close, stop.
  - Position open (not expiring today) -> fetch a FRESH OI verdict right
    now (never reuse anything cached earlier): still "accumulation" ->
    hold; no longer -> close short, then hedge, then search for a fresh
    qualifying strike and re-enter if one qualifies.
  - No position open (closed earlier today by a WS-driven SL hit) ->
    search for a new qualifying strike, re-enter if one qualifies.

Data sources, WebSocket reliability, order placement, exception handling,
PnL reporting -- identical conventions to every other deployed script in
this project (see strategies/deployed/AUTHORING_CHECKLIST.md): place()
retries ONLY a clean broker rejection; poll_fill()/_reprice_and_wait_once()
cross the spread with a fresh bid/ask; every entry/exit fill (short leg
AND hedge leg, independently) is confirmed via a backgrounded
poll_fill()/_watch_*_fill; report_pnl_to_platform() pushes one combined
realized+unrealized snapshot per tick as its own APScheduler job;
_repush_active_errors() runs unconditionally every run_cycle.

Product: NRML (mandatory -- positional, multi-day hold; see
config.product's docstring in Nifty_OI_WeeklyBuy_MonthlySell for the
overnight-carry trade-off, unchanged here). Underlying: NIFTY only.
Quantity: 65 (1 lot) per leg (short + hedge each carry the same quantity).

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

import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api
from opengreeks import black76

# See MCX_CrudeOil_EMA9_RSI_Intraday's identical comment -- several
# always-on threads (fill-watchers, trade-log writer, PriceStream's own
# watchdog/WS threads) at the default 8MB stack size add up against the
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap every strategy subprocess runs
# under. Must be called before any thread is created.
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
UNDERLYING_SYMBOL = "NIFTY"
UNDERLYING_SPOT_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"

OPTION_TYPES = ("CE", "PE")
# Every leg-slot this strategy can hold, independently, at once -- no
# weekly leg at all in this positional strategy.
LEG_KEYS = ["MONTHLY_CE", "MONTHLY_PE"]


@dataclass
class Config:
    strategy_name: str = "NIFTY OI Positional Monthly Option Selling"
    version: str = "1.0.0"

    intraday_interval: str = "15m"      # OI/premium candles -- 15-minute here, not the 5m sibling script uses
    history_lookback_days: int = 5      # enough to reliably find the last closed 15m bars

    gap_threshold_pct: float = 0.5      # Reference Engine gap threshold, reused verbatim
    reference_check_time: time = time(9, 30)
    reference_wait_time: time = time(9, 35)   # large-gap case waits for this candle to close

    # On a gap day, the reference (09:30 baseline) and the very first
    # entry-evaluation "current" read (fired once reference.computed flips
    # true, ~09:35) would otherwise both resolve to the SAME 09:15-09:30
    # 15-minute candle -- 15m candles don't close again until 09:45, so
    # "current" and "reference" collapse onto the identical row and
    # premium_change/oi_change are always exactly zero (forced "flat" for
    # every candidate, and since entry evaluation locks once per day, that
    # day's entry is permanently lost). Confirmed via backtest 2024-01-29.
    # Fix: capture the reference AND that one bootstrap "current" read at
    # 5-minute granularity (matching Nifty_OI_WeeklyBuy_MonthlySell's own
    # native interval) so 09:30 and 09:35 are genuinely different candles.
    # Everything else (15:15 daily decision, later days) stays on the
    # regular 15m interval -- already far enough from 09:30 to never collide.
    reference_gap_bootstrap_interval: str = "5m"

    # Strike qualification -- delta-closeness only, NO premium filter anywhere.
    monthly_delta_target: float = 0.27
    monthly_delta_low: float = 0.22     # widen-scan-stop heuristic only, never a hard filter
    monthly_delta_high: float = 0.32
    monthly_strike_round: int = 100
    monthly_expiry_roll_day_of_month: int = 15   # today.day > 15 resolves to next month's contract

    # Tiered, permanently-ratcheting stop-loss (checked continuously via WS LTP).
    sl_tier1_pct: float = 38.2           # exit once current_premium >= entry_premium * 1.382
    sl_tier2_trigger_pct: float = 50.0   # permanent ratchet trigger (premium <= 50% of entry, ever)

    # Hedge -- premium-target selected, same side as the short leg, further OTM.
    hedge_premium_pct_of_short: float = 20.0
    hedge_strike_scan_count: int = 30    # mirrors Batman's repair_chain_strike_count

    daily_decision_time: time = time(15, 15)      # fresh OI re-check / rollover-search, non-entry days
    entry_evaluation_time: time = time(9, 30)     # fires only on a cycle's own start day

    quantity: int = 65                  # NIFTY's current lot size == 1 lot
    capital_tag: float = 250_000.0      # reporting/return-on-capital display only

    # NRML mandatory -- positional, multi-day hold. No broker-side forced
    # square-off backstop; the strategy's own SL/daily-decision/expiry-day
    # closes are the ONLY things that ever close a position.
    product: str = "NRML"
    price_type: str = "MARKET"

    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    # Kept for parity with the sibling script's Config shape -- this
    # strategy's own exits (SL / daily-decision / expiry-day close) all
    # fire well before this; there is no separate "universal exit time"
    # sweep in this script (see module docstring's decision tree).
    monthly_universal_exit_time: time = time(15, 20)

    scheduler_interval: int = 10
    pnl_tick_interval: float = 0.8

    # Bounded retry for the 9:30/15:15 OI candle read: the broker can lag a
    # couple of minutes publishing the just-closed 15m candle (confirmed on
    # MCX/Shoonya, commit 9618fe65a). Since this strategy only evaluates
    # once a day at each of those times, a single stale read has a full
    # day's consequence -- retry a few times before falling back to
    # whatever's available so the day's decision is never skipped outright.
    oi_freshness_max_retries: int = 6
    oi_freshness_retry_delay_sec: float = 15.0

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
# OI + PREMIUM INTERPRETATION -- copied verbatim from Nifty_OI_WeeklyBuy_MonthlySell
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
class ShortHedgePosition:
    """One MONTHLY_CE/MONTHLY_PE leg is TWO independent order/fill
    lifecycles -- the sold (short) strike and its own hedge -- tracked
    under one key. `short_symbol` (not a separate `is_open` flag) is the
    "this leg holds a position" marker, matching LegPosition's own
    convention of `symbol` as the open/flat sentinel."""
    short_symbol: str = ""
    short_strike: float = 0.0
    quantity: int = 0
    entry_time: str = ""
    entry_px: float = 0.0               # original sell premium -- also the tier-2 SL level
    entry_order_id: str = ""
    entry_filled: bool = False
    short_exit_order_id: str = ""
    short_exit_filled: bool = False
    short_exit_fill_px: Optional[float] = None

    hedge_symbol: str = ""
    hedge_strike: float = 0.0
    hedge_entry_px: float = 0.0
    hedge_entry_order_id: str = ""
    hedge_entry_filled: bool = False
    hedge_exit_order_id: str = ""
    hedge_exit_filled: bool = False
    hedge_exit_fill_px: Optional[float] = None

    reference_oi: float = 0.0
    reference_premium: float = 0.0
    entry_trigger_verdict: str = ""     # "accumulation" at entry, always
    delta_at_entry: float = 0.0
    sl_tier2_locked: bool = False       # permanent ratchet flag -- never unset once True
    expiry_date: str = ""               # ISO date of the contract this position was actually entered on

    # Carries the exit reason across the async fill-confirmation gap (same
    # rationale as LegPosition.pending_exit_reason in the sibling script) --
    # and whether a fresh re-entry search should follow once BOTH legs
    # (short + hedge) are confirmed closed (set True only for the daily-
    # decision's "no longer accumulation" close; SL-breach and expiry-day
    # closes leave it False -- no immediate same-day re-search).
    pending_exit_reason: str = ""
    pending_reentry_search: bool = False

    execution_id: int = 0

    # Order error recovery -- same shape/semantics as LegPosition
    # (docs/prd/python-strategies-order-error-recovery.md). error_state
    # values used here: "entry_failed" (short leg), "hedge_entry_failed",
    # "exit_failed" (short leg), "hedge_exit_failed" -- distinguishes which
    # of the two independent order lifecycles needs attention.
    error_state: str = ""
    error_kind: str = ""
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None


@dataclass
class LegState:
    trade_count: int = 0
    position: ShortHedgePosition = field(default_factory=ShortHedgePosition)
    # ISO date string ("" if not yet resolved today) -- guards the 9:30
    # entry-evaluation dispatch to at most once per calendar day, resolved
    # regardless of outcome (found nothing, errored, a genuine entry).
    last_entry_eval_date: str = ""
    # ISO date string -- guards the 15:15 daily-decision dispatch to at
    # most once per calendar day, same resolved-regardless-of-outcome rule.
    last_daily_decision_date: str = ""


@dataclass
class ReferenceSnapshot:
    """Computed once per day at 09:30, fixed for the rest of the session --
    reused unchanged from Nifty_OI_WeeklyBuy_MonthlySell. `mode` is
    "prev_close" or "today_0935"; `reference_time_iso` is the exact candle
    timestamp OI/premium lookups must use for every leg's Reference values
    for the rest of the day."""
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
            or "nifty_oi_positional_monthly_sell"
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
            # its docstring below for why the SDK's own auto_reconnect must
            # stay off.
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
# from MCX_CrudeOil_EMA9_RSI_Intraday's already-hardened PriceStream (via
# Nifty_OI_WeeklyBuy_MonthlySell, which already carries it unchanged). Needed
# for (a) NIFTY spot LTP (strike selection) and (b) each open leg's own
# symbols (short + hedge), for the continuous tiered-SL check. OI is never in
# the WS feed -- it's REST-only via history(), a fully separate code path on
# a separate cadence.
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
            leg.last_entry_eval_date = leg_raw.get("last_entry_eval_date", "")
            leg.last_daily_decision_date = leg_raw.get("last_daily_decision_date", "")
            pos_raw = leg_raw.get("position", {})
            leg.position = ShortHedgePosition(
                **{**asdict(ShortHedgePosition()), **filter_known_fields(ShortHedgePosition, pos_raw)}
            )
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
                k: {
                    "trade_count": v.trade_count,
                    "last_entry_eval_date": v.last_entry_eval_date,
                    "last_daily_decision_date": v.last_daily_decision_date,
                    "position": asdict(v.position),
                }
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
    FAILURE. Matches Nifty_OI_WeeklyBuy_MonthlySell/MCX_CrudeOil's proven
    `_is_error_response`, used for both call sites here."""
    return isinstance(obj, dict)


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
    """`require_two_sided=True` additionally requires bid>0 AND ask>0 before
    trusting the quote -- defends against a quote that looks like it belongs
    to a DIFFERENT instrument than requested. Pass True only for TRADABLE-
    instrument reads -- an INDEX symbol legitimately has no bid/ask, so leave
    this False (default) for underlying-spot lookups. See docs/CUSTOMIZATIONS.md."""
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


def fetch_option_chain_strikes(client, expiry_compact: str, strike_count: int = 20) -> list[float]:
    """Real listed strikes around current spot, via optionchain() -- kept
    per the approved plan (carried over from Nifty_OI_WeeklyBuy_MonthlySell,
    where it backs select_weekly_otm1_strike, dropped entirely in this
    script). Not currently called by select_hedge_strike() (which reads
    strikes directly off its own optionchain() response via
    _legs_with_strike instead) -- retained for structural parity / any
    future caller needing just the flat strike list without the full
    with_quotes fan-out."""
    resp = client.optionchain(
        underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
        expiry_date=expiry_compact, strike_count=strike_count,
        with_quotes=False,
    )
    if resp.get("status") != "success" or not resp.get("chain"):
        raise RuntimeError(f"optionchain() failed for expiry {expiry_compact}: {resp}")
    chain = resp["chain"]
    strikes = sorted({float(row["strike"]) for row in chain if "strike" in row})
    if not strikes:
        raise RuntimeError(f"optionchain() returned no strikes for expiry {expiry_compact}")
    return strikes


def resolve_monthly_expiry(client) -> tuple[str, str]:
    """Calendar day-of-month > monthly_expiry_roll_day_of_month (15) ->
    roll to next month's expiry, regardless of how many days remain to the
    current month's own expiry; on/before day 15, use the current month's
    own expiry. Copied unchanged from Nifty_OI_WeeklyBuy_MonthlySell (only
    Config.monthly_expiry_roll_day_of_month's VALUE differs -- 15 here vs.
    20 there -- the comparison itself, `today.day <= roll_day`, is
    identical). This result is used ONLY to decide which expiry a NEW entry
    searches against -- it never closes an already-open position (see
    module docstring's rollover section)."""
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

    if today.day <= config.monthly_expiry_roll_day_of_month and current_month_end is not None:
        chosen = current_month_end
    else:
        # Past the roll day-of-month (or the current month's own expiry has
        # already passed) -- next month's month-end.
        future_keys = sorted(k for k in month_ends if k > current_month_key)
        if not future_keys:
            raise RuntimeError("No next-month expiry available to roll to.")
        chosen = month_ends[future_keys[0]]

    raw = chosen.strftime("%d-%b-%y")
    return _compact_expiry(raw), raw


def resolve_cycle_start_date(client) -> Optional["datetime.date"]:
    """The first trading day this strategy can enter the CURRENT monthly
    contract -- the day after the PRIOR contract's own expiry (spec item 3).
    Derived purely from the expiry calendar returned by client.expiry(),
    independent of any position history, so it self-corrects across
    restarts with no state to persist. Returns None if the calendar has no
    PAST month-end expiry to anchor from (e.g. the very first month this
    strategy is ever deployed in) -- callers must treat that as "not a
    cycle-start day", never guess a date."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve options expiry calendar: {resp}")
    today = datetime.now(IST).date()
    all_dates = sorted(datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"])

    month_ends: dict[tuple, "datetime.date"] = {}
    for d in all_dates:
        key = (d.year, d.month)
        if key not in month_ends or d > month_ends[key]:
            month_ends[key] = d

    past_month_ends = sorted(v for v in month_ends.values() if v < today)
    if not past_month_ends:
        return None
    prev_expiry = past_month_ends[-1]
    return _next_trading_day(prev_expiry)


def _next_trading_day(d) -> "datetime.date":
    """Next calendar day, skipping weekends -- does NOT consult the exchange
    holiday calendar, same documented simplification as
    Nifty_OI_WeeklyBuy_MonthlySell's resolve_previous_trading_day (fails
    safe: a holiday-adjacent misclassification here means the entry check
    simply never matches `today`, not a wrong trade)."""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d += timedelta(days=1)
    return d


def resolve_previous_trading_day(today) -> "datetime.date":
    """Previous calendar day, skipping weekends -- copied unchanged from
    Nifty_OI_WeeklyBuy_MonthlySell (Reference Engine helper)."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _years_to_expiry(expiry_raw: str) -> float:
    """Time to expiry in years, for the Black-76 model -- replicates
    services/option_greeks_service.py's calculate_time_to_expiry() exactly
    (same NFO 15:30 expiry time, same IST 'now', same 0.0001-year floor).
    Copied unchanged from Nifty_OI_WeeklyBuy_MonthlySell."""
    expiry_date = datetime.strptime(expiry_raw, "%d-%b-%y")
    expiry_dt = IST.localize(
        expiry_date.replace(hour=15, minute=30, second=0, microsecond=0)
    )
    now = datetime.now(IST)
    years = (expiry_dt - now).total_seconds() / (60 * 60 * 24) / 365.0
    return max(years, 0.0001)


def _legs_with_strike(chain: dict, option_type: str) -> list:
    """Flattens optionchain()'s per-strike-row shape into a flat list of leg
    dicts, each carrying its own `strike` -- copied from
    Nifty_Sensex_Expiry_Batman's `_legs_with_strike` (the established
    pattern for reading a batched optionchain() response by side)."""
    key = option_type.lower()
    legs = []
    for row in chain["chain"]:
        leg = row.get(key)
        if leg:
            merged = dict(leg)
            merged["strike"] = row["strike"]
            legs.append(merged)
    return legs


def select_qualifying_monthly_strike(client, spot: float, expiry_raw: str, option_type: str,
                                      reference: "ReferenceSnapshot",
                                      require_fresh_by: Optional[datetime] = None,
                                      current_interval: Optional[str] = None) -> Optional[tuple]:
    """Extends Nifty_OI_WeeklyBuy_MonthlySell's select_monthly_delta_strike --
    PURE delta-closeness ranking to config.monthly_delta_target (0.27), no
    premium filter anywhere. Keeps that function's expanding-band-scan
    skeleton (band_step_count=8, max_bands=2, ONE batched
    optionchain(with_quotes=True) call per band, local opengreeks.black76
    IV+delta computed from that single response) but:

      1. Ranks ALL scanned round-100-strike candidates by delta-closeness
         to monthly_delta_target (not just tracking a single "best so far").
      2. Walks the ranked list, checking each candidate's OWN OI
         classification (via fetch_candle_oi_premium + classify_oi_premium,
         using `reference` for that candidate's own premium/OI deltas --
         the one necessarily per-candidate, non-batched step in this chain,
         since OI history isn't part of the optionchain() response) and
         returns the first candidate whose verdict is "accumulation".
      3. Returns None if nothing across both bands passes the accumulation
         filter -- never falls back to "closest delta regardless of OI"
         the way the sibling script's delta-only selector does.

    NOTE (judgment call, plan gap): the plan's own signature for this
    function omits a `reference` parameter, but OI classification is
    structurally impossible without one (classify_oi_premium needs
    premium_change/oi_change relative to the day's Reference Snapshot) --
    added here, matching the established pattern used everywhere else in
    this codebase for computing a verdict.

    Returns (strike, delta, premium, verdict, chain_response) on success --
    the raw optionchain() response is returned alongside the winning strike
    so select_hedge_strike() can reuse it without a second network call.

    `current_interval` overrides the "current" OI/premium candle's interval
    (passed straight to fetch_candle_oi_premium) -- set ONLY by the entry
    evaluation's gap-day bootstrap (config.reference_gap_bootstrap_interval),
    so the very first 09:30 entry attempt on a gap day compares a genuine
    09:35 candle against the 09:30 reference instead of colliding on the
    same 15-minute bucket. None everywhere else (falls back to the normal
    config.intraday_interval)."""
    atm_100 = round(spot / 100.0) * 100.0
    direction = 1 if option_type == "CE" else -1
    side_key = "ce" if option_type == "CE" else "pe"
    band_step_count = 8
    max_bands = 2

    years_to_expiry = _years_to_expiry(expiry_raw)
    flag = "c" if option_type == "CE" else "p"

    scanned: list[dict] = []   # each: {"strike", "delta", "premium", "diff"}
    last_chain_response: Optional[dict] = None

    for band in range(1, max_bands + 1):
        strike_count = band_step_count * band * 2
        resp = client.optionchain(
            underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
            expiry_date=_compact_expiry(expiry_raw), strike_count=strike_count, with_quotes=True,
        )
        if resp.get("status") != "success" or not resp.get("chain"):
            raise RuntimeError(f"optionchain() failed for expiry {expiry_raw}: {resp}")
        last_chain_response = resp

        forward = float(resp.get("underlying_ltp") or spot)
        premium_by_strike: dict[float, float] = {}
        for row in resp["chain"]:
            leg = row.get(side_key)
            if not leg or not leg.get("ltp"):
                continue
            if not leg.get("bid") or not leg.get("ask"):
                continue  # no genuine two-sided market -- see sibling script's identical guard
            premium_by_strike[float(row["strike"])] = float(leg["ltp"])

        def _delta_at(strike: float) -> Optional[float]:
            premium = premium_by_strike.get(strike)
            if premium is None:
                return None
            try:
                iv = black76.implied_volatility(premium, forward, strike, 0.0, years_to_expiry, flag)
                return float(black76.delta(flag, forward, strike, years_to_expiry, 0.0, iv))
            except Exception as exc:
                Log.warning(f"Local Greeks computation failed for strike {strike}: {exc}")
                return None

        band_start = band_step_count * (band - 1) + 1
        band_end = band_step_count * band
        for step in range(band_start, band_end + 1):
            strike = atm_100 + direction * step * 100.0
            delta = _delta_at(strike)
            if delta is None:
                continue
            premium = premium_by_strike[strike]
            diff = abs(abs(delta) - config.monthly_delta_target)
            scanned.append({"strike": strike, "delta": delta, "premium": premium, "diff": diff})

        best_diff_so_far = min((c["diff"] for c in scanned), default=float("inf"))
        if best_diff_so_far != float("inf"):
            best_so_far = min(scanned, key=lambda c: c["diff"])
            if config.monthly_delta_low <= abs(best_so_far["delta"]) <= config.monthly_delta_high:
                break
        if band < max_bands:
            Log.info(f"[MONTHLY_{option_type}] no round-100 strike within delta range in "
                      f"+/-{band_step_count * band * 100}pts, extending to "
                      f"+/-{band_step_count * (band + 1) * 100}pts")

    if not scanned:
        raise RuntimeError(
            f"No usable {option_type} premium found near spot {spot}, expiry {expiry_raw} "
            f"(scanned up to +/-{band_step_count * max_bands * 100}pts, all illiquid or unpriceable)"
        )

    ranked = sorted(scanned, key=lambda c: c["diff"])
    for candidate in ranked:
        strike = candidate["strike"]
        symbol = f"{UNDERLYING_SYMBOL}{_compact_expiry(expiry_raw)}{int(strike)}{option_type}"
        current = fetch_candle_oi_premium(client, symbol, OPTIONS_EXCHANGE, require_fresh_by=require_fresh_by,
                                           interval=current_interval)
        ref_reading = fetch_reference_oi_premium(client, symbol, OPTIONS_EXCHANGE, reference)
        if current is None or ref_reading is None:
            Log.warning(f"[MONTHLY_{option_type}] OI/premium unavailable for candidate strike "
                        f"{strike} -- skipping to next-ranked candidate.")
            continue
        premium_change = current["premium"] - ref_reading["premium"]
        oi_change = current["oi"] - ref_reading["oi"]
        verdict = classify_oi_premium(premium_change, oi_change)
        Log.info(f"[MONTHLY_{option_type}] candidate strike={strike} delta={candidate['delta']:.4f} "
                  f"(diff={candidate['diff']:.4f}) premium={candidate['premium']:.2f} verdict={verdict}")
        if verdict == "accumulation":
            return strike, candidate["delta"], candidate["premium"], verdict, last_chain_response

    Log.info(f"[MONTHLY_{option_type}] no candidate (of {len(ranked)} scanned) passed the "
              f"OI-accumulation filter -- no qualifying strike this cycle.")
    return None


def select_hedge_strike(client, expiry_compact: str, option_type: str, sold_strike: float,
                         sold_premium: float, chain_response: Optional[dict] = None) -> Optional[dict]:
    """PREMIUM-based selection (unlike the short leg, the hedge IS chosen by
    premium target, per spec) -- modeled on Nifty_Sensex_Expiry_Batman's
    pick_leg_by_target_ltp. Reuses `chain_response` (from the just-completed
    short-strike scan, same option_type) when it already covers the target
    strike range; only calls optionchain() itself otherwise. Rejects any
    candidate on the WRONG side of sold_strike even if its premium is
    closer to target (further-OTM CE protects a sold CE; further-OTM PE
    protects a sold PE)."""
    target_premium = sold_premium * (config.hedge_premium_pct_of_short / 100.0)

    def _candidates_from(resp) -> list:
        legs = _legs_with_strike(resp, option_type)
        if option_type == "CE":
            return [l for l in legs if l["strike"] > sold_strike and l.get("ltp")]
        return [l for l in legs if l["strike"] < sold_strike and l.get("ltp")]

    # Reuse chain_response as-is when given -- it's the SAME expiry+side the
    # short-strike scan just fetched, and per the plan is only re-fetched if
    # the hedge target strike range falls outside what it already covers
    # (rare, given hedge_strike_scan_count=30 is wider than the delta-scan's
    # own bands). An empty _candidates_from(chain_response) here means
    # "genuinely no strike on the correct side of sold_strike in that
    # response" (already the widest band scanned), not "doesn't cover the
    # range" -- re-fetching would only repeat the exact same emptiness, so
    # only fall back to a fresh optionchain() call when no chain_response
    # was passed in at all.
    if chain_response is not None:
        candidates = _candidates_from(chain_response)
    else:
        resp = client.optionchain(
            underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
            expiry_date=expiry_compact, strike_count=config.hedge_strike_scan_count,
            with_quotes=True,   # ONE batched call, same efficiency pattern as the short-leg scan
        )
        if resp.get("status") != "success" or not resp.get("chain"):
            Log.warning(f"[HEDGE_{option_type}] optionchain() failed for expiry {expiry_compact}: {resp}")
            return None
        candidates = _candidates_from(resp)

    if not candidates:
        Log.warning(f"[HEDGE_{option_type}] no {option_type} candidate strictly beyond sold "
                    f"strike {sold_strike} with a usable premium -- no hedge available.")
        return None

    return min(candidates, key=lambda l: abs(l["ltp"] - target_premium))


def fetch_candle_oi_premium(client, symbol: str, exchange: str,
                             at_or_before: Optional[datetime] = None,
                             require_fresh_by: Optional[datetime] = None,
                             interval: Optional[str] = None) -> Optional[dict]:
    """Fetch OI + premium (close) for `symbol` from the last CLOSED
    config.intraday_interval (15-minute) candle at or before `at_or_before`
    (or simply the last closed candle if None). Returns None on any failure
    -- caller must skip this cycle's evaluation for this leg/side, never
    treat None as zero. Copied from Nifty_OI_WeeklyBuy_MonthlySell, with the
    candle_interval literal changed from 5 to 15 minutes to match this
    script's own 15m interval.

    `require_fresh_by` (only meaningful when `at_or_before` is None -- i.e.
    "give me the freshest closed candle right now"): raises
    CandleNotYetFresh if the latest closed candle available doesn't yet
    reach that boundary -- the broker hasn't published the just-closed
    candle yet (confirmed real lag on MCX/Shoonya, commit 9618fe65a).
    Callers retry a bounded number of times on this before falling back to
    whatever's available.

    `interval` overrides config.intraday_interval for this one call -- used
    ONLY for the gap-day reference bootstrap (config.reference_gap_bootstrap_interval,
    "5m"), which needs a finer candle than the regular 15m interval to keep
    09:30 and 09:35 as genuinely distinct candles. Leave None everywhere else."""
    active_interval = interval or config.intraday_interval
    end = datetime.now(IST).date()
    start = end - timedelta(days=config.history_lookback_days)
    try:
        bars = client.history(
            symbol=symbol, exchange=exchange, interval=active_interval,
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

    # Drop the last row if it hasn't actually closed yet -- see
    # AUTHORING_CHECKLIST.md's "candle-boundary caching" section: comparing
    # the fetched candle's own implied end-time against wall-clock `now`
    # directly (rather than maintaining a separate boundary function) is
    # this project's preferred pattern for a new script, immune by
    # construction to the three bugs documented there.
    candle_interval = timedelta(minutes=int(active_interval.rstrip("m")))
    if bars.index[-1] + candle_interval > datetime.now(IST):
        bars = bars.iloc[:-1]
        if bars.empty:
            Log.warning(f"Only a still-forming candle available for {symbol}.{exchange} -- "
                        f"no closed candle yet.")
            return None

    if (at_or_before is None and require_fresh_by is not None
            and bars.index[-1] + candle_interval < require_fresh_by):
        raise CandleNotYetFresh(
            f"{symbol}.{exchange}: latest closed candle ends "
            f"{bars.index[-1] + candle_interval} -- broker hasn't published a candle "
            f"reaching {require_fresh_by} yet"
        )

    last = bars.iloc[-1]
    return {
        "premium": float(last["close"]),
        "open": float(last.get("open", last["close"])),
        "high": float(last.get("high", last["close"])),
        "low": float(last.get("low", last["close"])),
        "oi": float(last.get("oi", 0)),
        "timestamp": str(bars.index[-1]),
    }


def fetch_reference_oi_premium(client, symbol: str, exchange: str,
                                reference: "ReferenceSnapshot") -> Optional[dict]:
    """OI + premium at the day's fixed Reference Time -- either the previous
    trading day's last closed candle (mode == "prev_close") or today's
    09:30 candle close (mode == "today_0935", read via the 5-minute
    bootstrap interval -- see config.reference_gap_bootstrap_interval's own
    comment for why: the regular 15m interval would collapse 09:30 and the
    first 09:35 "current" read onto the identical candle)."""
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
        return fetch_candle_oi_premium(client, symbol, exchange, at_or_before=at,
                                        interval=config.reference_gap_bootstrap_interval)

    Log.warning(f"fetch_reference_oi_premium called with unset reference mode for {symbol}.{exchange}")
    return None


###############################################################################
# TIERED STOP-LOSS (pure function + ratchet update)
###############################################################################
def current_sl_price(entry_px: float, sl_tier2_locked: bool) -> float:
    """Tier 2 (once ever triggered, permanent): SL locks to entry_px itself
    (breakeven). Tier 1 (default): flat sl_tier1_pct% above entry_px."""
    if sl_tier2_locked:
        return entry_px
    return entry_px * (1 + config.sl_tier1_pct / 100.0)


###############################################################################
# ORDER PLACEMENT / FILL HANDLING -- identical conventions to every other
# deployed script in this project (docs/prd/python-strategies-order-error-recovery.md)
###############################################################################
class OrderNeedsAttention(Exception):
    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


class CandleNotYetFresh(Exception):
    """Raised by fetch_candle_oi_premium when require_fresh_by was given and
    the broker hasn't yet published a candle reaching that boundary -- the
    latest closed candle available is genuinely older than the caller asked
    for. Distinct from returning None (which means no usable data at all);
    this signals "retry shortly, don't treat as a permanent failure"."""
    pass


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
            # Short leg (the sold strike): sell high, buy back low = profit.
            # Hedge leg (bought, long): buy low, sell high = profit.
            pnl_points = (exit_px - entry_px) if not is_short else (entry_px - exit_px)
            pnl_rupees = pnl_points * quantity
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


def _post_json_local(env: "Environment", path: str, payload: bytes, timeout: float = 3.0):
    """POST to the dedicated strategy_reporting subprocess over plain TCP
    loopback on STRATEGY_REPORTING_PORT (default 8766). Retries once at 3x
    the timeout -- covers the post-restart burst where every running
    strategy reconnects at once. See AUTHORING_CHECKLIST.md section 3 for
    why this must never hit FLASK_PORT/openalgo.sock."""
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


def _get_json_local(env: "Environment", path: str, timeout: float = 3.0) -> dict:
    """GET counterpart to _post_json_local -- same loopback-with-retry
    behavior. Raises if both attempts fail; caller treats it as "no action
    pending" rather than crashing the scheduler loop."""
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


def push_leg_error(env: "Environment", leg_key: str, pos: "ShortHedgePosition",
                    action: str = "", clear: bool = False):
    payload = json.dumps({
        "apikey": env.api_key,
        "leg_key": leg_key,
        "error_state": pos.error_state,
        "error_kind": pos.error_kind,
        "error_message": pos.error_message,
        "error_since": pos.error_since,
        "symbol": pos.short_symbol,
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
    leg from the platform. Returns None on any failure (transport error, or
    genuinely nothing pending) -- degrades to "nothing pending yet, try
    again next cycle" rather than raising."""
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


def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle -- e.g.
    for interval_minutes=15 at 10:07:42 IST, returns 10:00:00. Used only to
    gate the (low-value) OI-verdict LOGGING inside _manage_open_position to
    once per candle -- the tiered SL check itself is NOT gated by this (see
    module docstring: continuous, every WS tick)."""
    now = datetime.now(IST)
    total_minutes = now.hour * 60 + now.minute
    bucket_start_minutes = (total_minutes // interval_minutes) * interval_minutes
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=bucket_start_minutes)


###############################################################################
# PER-SIDE ENGINE -- ONE generic class, instantiated twice (CE/PE). Both
# sides are structurally identical, so this is the "parameterize, don't
# duplicate" way of guaranteeing CE and PE can never accidentally diverge.
###############################################################################
class MonthlyShortEngine:
    """Sells the delta-qualified monthly `option_type` strike, protected by
    its own premium-target hedge. Two independent order/fill lifecycles
    (short + hedge) under one ShortHedgePosition, tiered SL monitored
    continuously via WS LTP, daily-decision-driven rollover/exit."""

    def __init__(self, option_type: str, engine: "StrategyEngine"):
        self.option_type = option_type
        self.leg_key = f"MONTHLY_{option_type}"
        self.engine = engine
        # Own-candle-boundary marker for the (low-value) OI-verdict logging
        # inside _manage_open_position only -- NOT read by anyone else.
        # In-memory only, self-corrects within one scheduler tick after a
        # restart, no need to survive one.
        self._last_logged_boundary: Optional[str] = None

    @property
    def leg(self) -> LegState:
        return self.engine.state.legs[self.leg_key]

    # -------------------------------------------------------------------
    # Dispatch
    # -------------------------------------------------------------------
    def evaluate(self):
        if self.leg_key in self.engine._pending_fills:
            return  # a background fill-watcher is already resolving this leg
        pos = self.leg.position
        if pos.error_state:
            # Backgrounded (see _refresh_pending_action_bg) so a slow/
            # contended loopback call never blocks run_cycle().
            self.engine._refresh_pending_action_bg(self.leg_key)
            pending = self.engine._pop_pending_action(self.leg_key)
            if pending is not None:
                self._resolve_leg_error(pending)
            return

        if pos.short_symbol:
            if not (pos.entry_filled and pos.hedge_entry_filled):
                return  # still entering (short and/or hedge fill unconfirmed) -- watcher owns it
            if (pos.short_exit_order_id and pos.short_exit_filled
                    and pos.hedge_exit_order_id and pos.hedge_exit_filled):
                # Both legs' exits confirmed -- finalize NOW, every scheduler
                # tick, never gated behind a candle boundary (AUTHORING_
                # CHECKLIST.md section 1).
                self._finalize_exit(pos, pos.pending_exit_reason)
                return
            if pos.short_exit_order_id or pos.hedge_exit_order_id:
                return  # an exit is already in flight (one leg confirmed, other pending) -- watcher owns it
            # Continuous tiered-SL check -- every tick, not gated to a
            # candle boundary (spec item 5).
            self._manage_open_position_bg()
            # 15:15 daily decision -- only on days that are NOT this cycle's
            # own 9:30 entry day (spec item 10).
            self._maybe_daily_decision_bg()
        else:
            # Flat -- either the 15:15 daily decision (re-entry search) or
            # the 9:30 entry evaluation can apply, on mutually exclusive days.
            self._maybe_daily_decision_bg()
            self._maybe_evaluate_entry_bg()

    def _dispatch_bg(self, fn, *args):
        """Shared background-dispatch wrapper -- runs `fn` on
        _fill_executor, guarded by _eval_pending so a slow call from one
        tick is never resubmitted on top of itself. Mirrors
        Nifty_OI_WeeklyBuy_MonthlySell's _dispatch_eval_bg."""
        if self.leg_key in self.engine._eval_pending:
            return
        self.engine._eval_pending.add(self.leg_key)

        def _run():
            try:
                fn(*args)
            except Exception as exc:
                Log.exception(f"[{self.leg_key}] Background task failed: {exc}")
            finally:
                self.engine._eval_pending.discard(self.leg_key)

        self.engine._fill_executor.submit(_run)

    def _manage_open_position_bg(self):
        self._dispatch_bg(self._manage_open_position)

    def _maybe_evaluate_entry_bg(self):
        """Fires only on this cycle's own start day (the day after the
        prior contract's own expiry), at/after entry_evaluation_time
        (9:30), at most once per calendar day regardless of outcome."""
        if datetime.now(IST).time() < config.entry_evaluation_time:
            return
        today = datetime.now(IST).date()
        cycle_start = self.engine.get_cycle_start_date()
        if cycle_start is None or today != cycle_start:
            return
        today_key = today.isoformat()
        if self.leg.last_entry_eval_date == today_key:
            return  # already resolved today's entry decision
        if self.leg_key in self.engine._eval_pending:
            return
        self.engine._eval_pending.add(self.leg_key)

        def _run():
            try:
                self._evaluate_entry()
            except Exception as exc:
                Log.exception(f"[{self.leg_key}] Background entry evaluation failed: {exc}")
            finally:
                self.leg.last_entry_eval_date = today_key
                self.engine.save_state()
                self.engine._eval_pending.discard(self.leg_key)

        self.engine._fill_executor.submit(_run)

    def _maybe_daily_decision_bg(self):
        """Fires once per calendar day at/after daily_decision_time
        (15:15), on every day that is NOT this cycle's own 9:30 entry day
        (spec item 10 -- the two are mutually exclusive per day)."""
        if datetime.now(IST).time() < config.daily_decision_time:
            return
        today = datetime.now(IST).date()
        cycle_start = self.engine.get_cycle_start_date()
        if cycle_start is not None and today == cycle_start:
            return  # entry day handles itself via _maybe_evaluate_entry_bg
        today_key = today.isoformat()
        if self.leg.last_daily_decision_date == today_key:
            return
        if self.leg_key in self.engine._eval_pending:
            return
        self.engine._eval_pending.add(self.leg_key)

        def _run():
            try:
                self._daily_decision()
            except Exception as exc:
                Log.exception(f"[{self.leg_key}] Background daily decision failed: {exc}")
            finally:
                self.leg.last_daily_decision_date = today_key
                self.engine.save_state()
                self.engine._eval_pending.discard(self.leg_key)

        self.engine._fill_executor.submit(_run)

    # -------------------------------------------------------------------
    # Entry
    # -------------------------------------------------------------------
    def _select_qualifying_strike_with_retry(self, spot: float, expiry_raw: str,
                                              trigger_time: time,
                                              current_interval: Optional[str] = None) -> Optional[tuple]:
        """Bounded retry around select_qualifying_monthly_strike for the
        candle-freshness gate: the broker can lag publishing the just-closed
        15m candle (confirmed real lag on MCX/Shoonya, commit 9618fe65a).
        Since this strategy evaluates only once at `trigger_time` per day, a
        single stale read has a full day's consequence -- retry
        config.oi_freshness_max_retries times, config.oi_freshness_retry_delay_sec
        apart, before falling back (final attempt disables the freshness
        gate) rather than skipping the day's decision outright. Runs on the
        background fill-executor thread, so blocking sleeps here are safe.
        A genuine RuntimeError (no usable strike at all) is NOT caught --
        it propagates to the caller unchanged.

        `current_interval` is forwarded to select_qualifying_monthly_strike
        unchanged -- set by _evaluate_entry on a gap day (see
        config.reference_gap_bootstrap_interval), None everywhere else."""
        import time as _time
        trigger_boundary = datetime.combine(datetime.now(IST).date(), trigger_time, tzinfo=IST)
        max_attempts = config.oi_freshness_max_retries
        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            fresh_gate = None if is_last else trigger_boundary
            try:
                return select_qualifying_monthly_strike(
                    self.engine.client, spot, expiry_raw, self.option_type,
                    self.engine.state.reference, require_fresh_by=fresh_gate,
                    current_interval=current_interval,
                )
            except CandleNotYetFresh as exc:
                Log.info(f"[MONTHLY_{self.option_type}] candle not yet published by broker "
                          f"(attempt {attempt}/{max_attempts}) -- retrying in "
                          f"{config.oi_freshness_retry_delay_sec:.0f}s: {exc}")
                _time.sleep(config.oi_freshness_retry_delay_sec)
        return None  # unreachable in practice -- the last attempt's fresh_gate is None

    def _evaluate_entry(self):
        spot = self.engine.get_spot_ltp()
        if spot is None:
            Log.warning(f"[MONTHLY_{self.option_type}] spot LTP unavailable this cycle.")
            return
        try:
            expiry_compact, expiry_raw = self.engine.get_monthly_expiry()
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] expiry resolution failed: {exc}")
            return

        # On a gap day, the regular 15m interval would collapse the 09:30
        # reference and this first entry-evaluation "current" read onto the
        # same candle (see config.reference_gap_bootstrap_interval) --
        # bootstrap this one call at 5m so they're genuinely distinct.
        bootstrap_interval = (config.reference_gap_bootstrap_interval
                               if self.engine.state.reference.mode == "today_0935" else None)
        try:
            result = self._select_qualifying_strike_with_retry(
                spot, expiry_raw, config.entry_evaluation_time, current_interval=bootstrap_interval)
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] strike selection failed: {exc}")
            return
        if result is None:
            Log.info(f"[MONTHLY_{self.option_type}] no qualifying strike today -- staying flat.")
            return
        strike, delta, premium, verdict, chain_response = result

        hedge = select_hedge_strike(
            self.engine.client, expiry_compact, self.option_type, strike, premium, chain_response
        )
        if hedge is None:
            Log.warning(f"[MONTHLY_{self.option_type}] qualifying short strike {strike} found but no "
                        f"hedge candidate available -- refusing to sell naked, skipping entry.")
            return

        symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(strike)}{self.option_type}"
        hedge_symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(hedge['strike'])}{self.option_type}"
        expiry_date_iso = datetime.strptime(expiry_raw, "%d-%b-%y").date().isoformat()
        self._enter(symbol, strike, delta, verdict, hedge_symbol, hedge, expiry_date_iso)

    def _enter(self, symbol, strike, delta, verdict, hedge_symbol, hedge, expiry_date_iso):
        leg = self.leg
        if leg.position.short_symbol:
            return  # defensive no-op -- evaluate() already branches on this

        # Persist "attempting entry" BEFORE calling place() -- crash-safety/
        # error-routing rationale identical to every other deployed script.
        leg.position = ShortHedgePosition(
            short_symbol=symbol, short_strike=strike, quantity=config.quantity,
            entry_time=datetime.now(IST).isoformat(), entry_px=0.0,
            entry_order_id="", entry_filled=False,
            hedge_symbol=hedge_symbol, hedge_strike=hedge["strike"],
            execution_id=self.engine.execution_id,
            entry_trigger_verdict=verdict, delta_at_entry=delta,
            expiry_date=expiry_date_iso,
        )
        self.engine.save_state()

        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "SELL", config.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] short entry placeorder failed for {symbol}: {exc}")
            self._enter_error_mode("", "entry_failed", "terminal", "", str(exc))
            return

        leg.trade_count += 1
        leg.position.entry_order_id = order_id
        self.engine.save_state()

        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(self._watch_entry_fill, order_id, symbol, hedge_symbol)

    def _watch_entry_fill(self, order_id, symbol, hedge_symbol):
        """Runs on _fill_executor. Confirms the SHORT leg's fill, then --
        still on this same background thread -- places and confirms the
        HEDGE leg's fill immediately after. _pending_fills is held for the
        WHOLE chain (short + hedge), released only in the outer `finally`,
        so evaluate() never touches this leg mid-chain."""
        try:
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
                Log.exception(f"[MONTHLY_{self.option_type}] short entry fill-poll failed for {symbol}: {exc}")
                self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
                return

            pos = self.leg.position
            if pos.entry_order_id != order_id:
                return  # guard vs. a superseded/stale order
            entry_px = float(fill.get("average_price") or fill.get("price") or 0.0)
            pos.entry_px = entry_px
            pos.entry_filled = True
            self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
            self.engine.save_state()
            Log.info(f"[MONTHLY_{self.option_type}] Short entry filled: {symbol}@{entry_px} "
                     f"qty={config.quantity} verdict={pos.entry_trigger_verdict} delta={pos.delta_at_entry:.3f}")

            try:
                hedge_order_id = place(self.engine.client, self.engine.env.strategy_tag, hedge_symbol,
                                        OPTIONS_EXCHANGE, "BUY", config.quantity)
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge entry placeorder failed for "
                              f"{hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_entry_failed", "terminal", "", str(exc))
                return

            pos.hedge_entry_order_id = hedge_order_id
            self.engine.save_state()

            try:
                hedge_fill = poll_fill(self.engine.client, hedge_order_id, self.engine.env.strategy_tag,
                                        hedge_symbol, OPTIONS_EXCHANGE, "BUY", config.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge entry fill-poll failed for "
                              f"{hedge_symbol}: {exc}")
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "resting", hedge_order_id, str(exc))
                return

            pos = self.leg.position
            if pos.hedge_entry_order_id != hedge_order_id:
                return
            hedge_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
            pos.hedge_entry_px = hedge_px
            pos.hedge_entry_filled = True
            self.engine.price_stream.add_instruments([{"symbol": hedge_symbol, "exchange": OPTIONS_EXCHANGE}])
            self.engine.save_state()
            Log.info(f"[MONTHLY_{self.option_type}] Hedge entry filled: {hedge_symbol}@{hedge_px} "
                     f"qty={config.quantity} -- position fully open.")
        finally:
            self.engine._pending_fills.discard(self.leg_key)

    # -------------------------------------------------------------------
    # Continuous tiered-SL management
    # -------------------------------------------------------------------
    def _manage_open_position(self):
        pos = self.leg.position

        ltp = self.engine.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
        if ltp is None:
            ltp = fetch_symbol_ltp(self.engine.client, pos.short_symbol, OPTIONS_EXCHANGE, require_two_sided=True)
        if ltp is None:
            # Never act on a missing/stale LTP -- skip this cycle, re-check
            # next tick (spec's "Operational conventions").
            return

        if not pos.sl_tier2_locked and ltp <= pos.entry_px * (config.sl_tier2_trigger_pct / 100.0):
            pos.sl_tier2_locked = True
            self.engine.save_state()
            Log.info(f"[MONTHLY_{self.option_type}] SL tier-2 ratchet LOCKED: premium {ltp:.2f} -- "
                     f"SL now {pos.entry_px:.2f} (breakeven, permanent).")

        sl_price = current_sl_price(pos.entry_px, pos.sl_tier2_locked)
        if ltp >= sl_price:
            Log.info(f"[MONTHLY_{self.option_type}] Stop-loss hit: premium={ltp:.2f} >= sl_price={sl_price:.2f} "
                     f"(tier2_locked={pos.sl_tier2_locked})")
            self._exit(pos, "stop_loss")
            return

        # OI-verdict LOGGING only, once per closed candle -- no action taken
        # on a signal change here (that's the 15:15 daily decision's job).
        boundary = self.engine._last_candle_boundary
        boundary_key = boundary.isoformat() if boundary is not None else None
        if boundary_key is not None and boundary_key != self._last_logged_boundary:
            current = fetch_candle_oi_premium(self.engine.client, pos.short_symbol, OPTIONS_EXCHANGE)
            if current is not None:
                premium_change = current["premium"] - pos.reference_premium
                oi_change = current["oi"] - pos.reference_oi
                verdict = classify_oi_premium(premium_change, oi_change)
                Log.info(f"[MONTHLY_{self.option_type}] (open) candle={current['timestamp']} "
                         f"symbol={pos.short_symbol} premium={current['premium']:.2f} "
                         f"premium_chg={premium_change:+.2f} oi_chg={oi_change:+.0f} verdict={verdict} "
                         f"sl_price={sl_price:.2f} tier2_locked={pos.sl_tier2_locked}")
                self._last_logged_boundary = boundary_key

    # -------------------------------------------------------------------
    # 15:15 daily decision
    # -------------------------------------------------------------------
    def _daily_decision(self):
        try:
            self.engine.get_monthly_expiry()  # informational -- feeds whichever entry search fires below
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] daily-decision expiry resolution failed: {exc}")

        pos = self.leg.position
        today_key = datetime.now(IST).date().isoformat()

        if pos.short_symbol:
            if pos.expiry_date and pos.expiry_date == today_key:
                Log.info(f"[MONTHLY_{self.option_type}] open position's own contract expires today -- "
                         f"force-closing (expiry-day safety close, terminal for today).")
                pos.pending_reentry_search = False
                self._exit(pos, "expiry_day_close")
                return  # terminal -- no new-position search follows today

            verdict = self._reclassify_with_retry(pos, config.daily_decision_time)
            if verdict is None:
                Log.warning(f"[MONTHLY_{self.option_type}] fresh OI re-check unavailable this cycle -- "
                            f"holding, will retry the daily decision tomorrow.")
                return
            if verdict == "accumulation":
                Log.info(f"[MONTHLY_{self.option_type}] daily decision: still accumulation -- holding.")
                return
            Log.info(f"[MONTHLY_{self.option_type}] daily decision: no longer accumulation "
                     f"(verdict={verdict}) -- closing short then hedge, will search for a fresh "
                     f"qualifying strike once closed.")
            pos.pending_reentry_search = True
            self._exit(pos, "opposite_signal")
            return

        # Flat -- closed earlier today by a WS-driven SL hit, or simply
        # never entered -- search for a fresh qualifying strike.
        self._search_and_enter_fresh()

    def _fresh_oi_reclassify(self, pos, require_fresh_by: Optional[datetime] = None) -> Optional[str]:
        """Always re-fetched, NEVER cached -- fetch_candle_oi_premium's own
        "drop if still forming" guard is the candle-boundary check here,
        per AUTHORING_CHECKLIST.md's preferred pattern. May raise
        CandleNotYetFresh (propagated, not caught here) when require_fresh_by
        is given -- caller (_reclassify_with_retry) owns the retry loop."""
        current = fetch_candle_oi_premium(self.engine.client, pos.short_symbol, OPTIONS_EXCHANGE,
                                           require_fresh_by=require_fresh_by)
        ref_reading = fetch_reference_oi_premium(
            self.engine.client, pos.short_symbol, OPTIONS_EXCHANGE, self.engine.state.reference
        )
        if current is None or ref_reading is None:
            return None
        premium_change = current["premium"] - ref_reading["premium"]
        oi_change = current["oi"] - ref_reading["oi"]
        return classify_oi_premium(premium_change, oi_change)

    def _reclassify_with_retry(self, pos, trigger_time: time) -> Optional[str]:
        """Bounded retry mirroring _select_qualifying_strike_with_retry, for
        the daily-decision's fresh re-check of an already-open position's
        own OI verdict."""
        import time as _time
        trigger_boundary = datetime.combine(datetime.now(IST).date(), trigger_time, tzinfo=IST)
        max_attempts = config.oi_freshness_max_retries
        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            fresh_gate = None if is_last else trigger_boundary
            try:
                return self._fresh_oi_reclassify(pos, require_fresh_by=fresh_gate)
            except CandleNotYetFresh as exc:
                Log.info(f"[MONTHLY_{self.option_type}] daily-decision candle not yet published by "
                          f"broker (attempt {attempt}/{max_attempts}) -- retrying in "
                          f"{config.oi_freshness_retry_delay_sec:.0f}s: {exc}")
                _time.sleep(config.oi_freshness_retry_delay_sec)
        return None  # unreachable in practice -- the last attempt's fresh_gate is None

    def _search_and_enter_fresh(self):
        spot = self.engine.get_spot_ltp()
        if spot is None:
            Log.warning(f"[MONTHLY_{self.option_type}] spot LTP unavailable for daily-decision "
                        f"re-entry search.")
            return
        try:
            expiry_compact, expiry_raw = self.engine.get_monthly_expiry()
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] expiry resolution failed: {exc}")
            return
        try:
            result = self._select_qualifying_strike_with_retry(spot, expiry_raw, config.daily_decision_time)
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] daily-decision strike selection failed: {exc}")
            return
        if result is None:
            Log.info(f"[MONTHLY_{self.option_type}] daily decision: no qualifying strike -- staying flat.")
            return
        strike, delta, premium, verdict, chain_response = result
        hedge = select_hedge_strike(
            self.engine.client, expiry_compact, self.option_type, strike, premium, chain_response
        )
        if hedge is None:
            Log.warning(f"[MONTHLY_{self.option_type}] daily decision: qualifying strike {strike} found "
                        f"but no hedge candidate available -- refusing to sell naked, skipping re-entry.")
            return
        symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(strike)}{self.option_type}"
        hedge_symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(hedge['strike'])}{self.option_type}"
        expiry_date_iso = datetime.strptime(expiry_raw, "%d-%b-%y").date().isoformat()
        self._enter(symbol, strike, delta, verdict, hedge_symbol, hedge, expiry_date_iso)

    # -------------------------------------------------------------------
    # Exit -- short leg first, then hedge leg (AUTHORING_CHECKLIST.md:
    # close the unbounded/naked-risk leg before the bounded-risk leg)
    # -------------------------------------------------------------------
    def _exit(self, pos, reason):
        if pos.short_exit_order_id and not pos.short_exit_filled:
            return  # already in flight
        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.short_symbol,
                              OPTIONS_EXCHANGE, "BUY", pos.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] short exit placeorder failed for "
                          f"{pos.short_symbol}: {exc}")
            self._enter_error_mode("", "exit_failed", "terminal", "", str(exc))
            return

        pos.short_exit_order_id = order_id
        pos.short_exit_filled = False
        pos.pending_exit_reason = reason
        self.engine.save_state()

        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(self._watch_short_exit_fill, order_id, pos.short_symbol, pos.quantity)

    def _watch_short_exit_fill(self, order_id, symbol, quantity):
        """Confirms the SHORT leg's exit fill, then -- still on this same
        background thread -- places and confirms the HEDGE leg's exit
        immediately after (short-before-hedge, per AUTHORING_CHECKLIST.md).
        _pending_fills held for the whole chain."""
        try:
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
                Log.exception(f"[MONTHLY_{self.option_type}] short exit fill-poll failed for {symbol}: {exc}")
                self._enter_error_mode(order_id, "exit_failed", "resting", order_id, str(exc))
                return

            pos = self.leg.position
            if pos.short_exit_order_id != order_id:
                return
            pos.short_exit_fill_px = float(fill.get("average_price") or fill.get("price") or 0.0)
            pos.short_exit_filled = True
            self.engine.save_state()

            try:
                hedge_order_id = place(self.engine.client, self.engine.env.strategy_tag, pos.hedge_symbol,
                                        OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge exit placeorder failed for "
                              f"{pos.hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_exit_failed", "terminal", "", str(exc))
                return

            pos.hedge_exit_order_id = hedge_order_id
            pos.hedge_exit_filled = False
            self.engine.save_state()

            try:
                hedge_fill = poll_fill(self.engine.client, hedge_order_id, self.engine.env.strategy_tag,
                                        pos.hedge_symbol, OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge exit fill-poll failed for "
                              f"{pos.hedge_symbol}: {exc}")
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "resting", hedge_order_id, str(exc))
                return

            pos = self.leg.position
            if pos.hedge_exit_order_id != hedge_order_id:
                return
            pos.hedge_exit_fill_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
            pos.hedge_exit_filled = True
            self.engine.save_state()
        finally:
            self.engine._pending_fills.discard(self.leg_key)

    def _finalize_exit(self, pos, reason):
        leg = self.leg
        # NOTE: manual_exit_px is a single field shared across both
        # independent exit lifecycles (short + hedge) -- _resolve_leg_error's
        # "manual" branch already writes the confirmed fill directly into
        # short_exit_fill_px/hedge_exit_fill_px (whichever leg was actually
        # manually completed), so those two fields alone are the correct,
        # leg-specific source of truth here. manual_exit_px itself is never
        # read here (it would misapply one leg's override to the other).
        short_exit_px = pos.short_exit_fill_px if pos.short_exit_fill_px is not None else pos.entry_px
        hedge_exit_px = pos.hedge_exit_fill_px if pos.hedge_exit_fill_px is not None else pos.hedge_entry_px

        # Short: sold high (entry_px), bought back (short_exit_px) -- profit
        # when short_exit_px < entry_px. Hedge: bought (hedge_entry_px),
        # sold (hedge_exit_px) -- profit when hedge_exit_px > hedge_entry_px.
        short_pnl_rupees = (pos.entry_px - short_exit_px) * pos.quantity
        hedge_pnl_rupees = (hedge_exit_px - pos.hedge_entry_px) * pos.quantity
        total_pnl_rupees = short_pnl_rupees + hedge_pnl_rupees
        self.engine.state.today_realized_pnl += total_pnl_rupees

        now_iso = datetime.now(IST).isoformat()
        append_trade_log(self.engine.env.strategy_tag, self.leg_key, pos.short_symbol, pos.quantity,
                          pos.entry_time, pos.entry_px, now_iso, short_exit_px, reason,
                          pos.execution_id, is_short=True)
        append_trade_log(self.engine.env.strategy_tag, f"{self.leg_key}_HEDGE", pos.hedge_symbol, pos.quantity,
                          pos.entry_time, pos.hedge_entry_px, now_iso, hedge_exit_px, reason,
                          pos.execution_id, is_short=False)
        Log.info(f"[MONTHLY_{self.option_type}] Position closed: short={pos.short_symbol} "
                 f"hedge={pos.hedge_symbol} reason={reason} short_pnl={short_pnl_rupees:.2f} "
                 f"hedge_pnl={hedge_pnl_rupees:.2f} total_pnl={total_pnl_rupees:.2f}")

        self.engine.price_stream.remove_instruments([
            {"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE},
            {"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE},
        ])
        self.engine._notify_trade_closed_bg()
        reentry_search = pos.pending_reentry_search
        leg.position = ShortHedgePosition()
        self.engine.save_state()

        if reentry_search:
            # Spec item 10's "close short, then hedge, then search for a
            # new qualifying strike and re-enter if one qualifies" --
            # backgrounded like every other entry-eval call (see
            # _maybe_evaluate_entry_bg's identical rationale).
            self._dispatch_bg(self._search_and_enter_fresh)

    # -------------------------------------------------------------------
    # Error recovery -- Retry/Cancel/Manually-Completed, per
    # docs/prd/python-strategies-order-error-recovery.md
    # -------------------------------------------------------------------
    def _enter_error_mode(self, order_id, error_state, error_kind, error_order_id, message):
        pos = self.leg.position
        known_ids = {pos.entry_order_id, pos.hedge_entry_order_id,
                     pos.short_exit_order_id, pos.hedge_exit_order_id}
        if order_id and order_id not in known_ids:
            return  # superseded/stale order -- don't overwrite a newer attempt's state
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        action = "SELL" if error_state == "entry_failed" else (
            "BUY" if error_state == "hedge_entry_failed" else (
            "BUY" if error_state == "exit_failed" else "SELL"))
        self.engine._push_leg_error_bg(self.leg_key, pos, action=action)
        self.engine.save_state()
        Log.error(f"[MONTHLY_{self.option_type}] {error_state} ({error_kind}): {message}")
        self.engine._notify_telegram_error_bg(
            f"MONTHLY_{self.option_type} {error_state} ({error_kind}): {message}"
        )

    def _resolve_leg_error(self, action: dict):
        """Applies a Retry/Cancel/Manually-Completed decision pulled from
        the platform. `error_state` tells us which of the four independent
        order lifecycles (short entry, hedge entry, short exit, hedge exit)
        is stuck -- resolved generically via each state's own action
        direction and symbol/quantity, rather than four near-duplicate
        branches."""
        if self.leg_key in self.engine._pending_fills:
            return
        pos = self.leg.position
        error_state = pos.error_state
        is_exit = error_state in ("exit_failed", "hedge_exit_failed")
        is_hedge = error_state in ("hedge_entry_failed", "hedge_exit_failed")
        symbol = pos.hedge_symbol if is_hedge else pos.short_symbol
        buy_action = "BUY" if is_hedge else "SELL"    # hedge entry=BUY, short entry=SELL
        sell_action = "SELL" if is_hedge else "BUY"   # hedge exit=SELL, short exit=BUY
        order_action = sell_action if is_exit else buy_action
        kind = pos.error_kind

        if action["action"] == "retry":
            self.engine._pending_fills.add(self.leg_key)
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._fill_executor.submit(
                self._do_retry_resolution, error_state, kind, symbol, order_action, pos.quantity
            )
            return

        if action["action"] == "cancel":
            if is_exit:
                if is_hedge:
                    pos.hedge_exit_order_id = ""
                else:
                    pos.short_exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                self.engine._push_leg_error_bg(self.leg_key, pos, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            if kind == "terminal":
                if error_state == "entry_failed":
                    # The short leg never entered -- the whole attempted
                    # position is abandoned (no hedge was ever placed yet).
                    self.engine.price_stream.remove_instruments(
                        [{"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE}]
                    )
                    self.leg.position = ShortHedgePosition()
                else:
                    # Hedge entry failed terminally -- the short leg is
                    # already live and naked; leave it open (don't discard
                    # the whole position) and just clear this error so the
                    # daily-decision/SL logic keeps managing the short leg.
                    # A human should re-hedge manually via the broker if
                    # desired -- Cancel here means "give up trying to place
                    # this specific hedge order automatically."
                    pos.hedge_entry_order_id = ""
                    pos.error_state = ""
                    pos.error_kind = ""
                    pos.error_order_id = ""
                self.engine.save_state()
                self.engine._push_leg_error_bg(self.leg_key, self.leg.position, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                return
            ack_pending_action(self.engine.env, self.leg_key)
            self.engine._pending_fills.add(self.leg_key)
            self.engine._fill_executor.submit(
                self._watch_cancel_final_chance, pos.error_order_id, symbol, pos.quantity, order_action, error_state
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if is_exit:
                if is_hedge:
                    pos.hedge_exit_filled = True
                    pos.hedge_exit_fill_px = fill_price
                else:
                    pos.short_exit_filled = True
                    pos.short_exit_fill_px = fill_price
                pos.manual_exit_px = fill_price
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                self.engine._push_leg_error_bg(self.leg_key, pos, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                if pos.short_exit_filled and pos.hedge_exit_filled:
                    self._finalize_exit(pos, pos.pending_exit_reason)
                return
            if is_hedge:
                pos.hedge_entry_filled = True
                pos.hedge_entry_px = fill_price
                self.engine.price_stream.add_instruments(
                    [{"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            else:
                pos.entry_filled = True
                pos.entry_px = fill_price
                self.leg.trade_count += 1
                self.engine.price_stream.add_instruments(
                    [{"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            self.engine._push_leg_error_bg(self.leg_key, pos, clear=True)
            ack_pending_action(self.engine.env, self.leg_key)

    def _do_retry_resolution(self, error_state, kind, symbol, order_action, quantity):
        try:
            pos = self.leg.position
            if error_state in ("exit_failed", "hedge_exit_failed"):
                if kind == "resting":
                    bid, ask = fetch_symbol_bid_ask(self.engine.client, symbol, OPTIONS_EXCHANGE)
                    fresh_price = ask if order_action == "BUY" else bid
                    if fresh_price is not None:
                        try:
                            self.engine.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                                symbol=symbol, action=order_action, exchange=OPTIONS_EXCHANGE,
                                price_type="LIMIT", product=config.product, quantity=str(quantity),
                                price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[MONTHLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:
                    if error_state == "hedge_exit_failed":
                        pos.hedge_exit_order_id = ""
                    else:
                        pos.short_exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                self.engine._push_leg_error_bg(self.leg_key, pos, clear=True)
                self.engine._pending_fills.discard(self.leg_key)
                return

            # Entry side -- unlike exit, evaluate() only calls
            # _evaluate_entry() when the leg is flat, so Retry directly
            # resubmits the appropriate watcher rather than relying on the
            # normal flow.
            if kind == "resting":
                bid, ask = fetch_symbol_bid_ask(self.engine.client, symbol, OPTIONS_EXCHANGE)
                fresh_price = ask if order_action == "BUY" else bid
                if fresh_price is not None:
                    try:
                        self.engine.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.engine.env.strategy_tag,
                            symbol=symbol, action=order_action, exchange=OPTIONS_EXCHANGE,
                            price_type="LIMIT", product=config.product, quantity=str(quantity),
                            price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[MONTHLY_{self.option_type}] Retry's reprice failed ({exc}) -- "
                                    f"resuming the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:
                try:
                    resume_order_id = place(self.engine.client, self.engine.env.strategy_tag, symbol,
                                             OPTIONS_EXCHANGE, order_action, quantity)
                except Exception as exc:
                    Log.exception(f"[MONTHLY_{self.option_type}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode(pos.entry_order_id, error_state, "terminal", "", str(exc))
                    self.engine._pending_fills.discard(self.leg_key)
                    return
                if error_state == "hedge_entry_failed":
                    pos.hedge_entry_order_id = resume_order_id
                else:
                    pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.engine.save_state()
            self.engine._push_leg_error_bg(self.leg_key, pos, clear=True)
            if error_state == "hedge_entry_failed":
                # Resume the hedge-fill leg only -- the short leg is
                # already confirmed filled.
                def _watch_hedge_only():
                    try:
                        hedge_fill = poll_fill(self.engine.client, resume_order_id, self.engine.env.strategy_tag,
                                                symbol, OPTIONS_EXCHANGE, "BUY", quantity)
                        p = self.leg.position
                        if p.hedge_entry_order_id != resume_order_id:
                            return
                        p.hedge_entry_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
                        p.hedge_entry_filled = True
                        self.engine.price_stream.add_instruments(
                            [{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}]
                        )
                        self.engine.save_state()
                    except OrderNeedsAttention as exc:
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "resting", exc.order_id, str(exc))
                    except (RuntimeError, TimeoutError) as exc:
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "terminal", "", str(exc))
                    except Exception as exc:
                        Log.exception(f"[MONTHLY_{self.option_type}] Retry-resumed hedge fill-poll failed: {exc}")
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "resting", resume_order_id, str(exc))
                    finally:
                        self.engine._pending_fills.discard(self.leg_key)
                self.engine._fill_executor.submit(_watch_hedge_only)
            else:
                self.engine._fill_executor.submit(self._watch_entry_fill, resume_order_id, symbol, pos.hedge_symbol)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] Retry resolution failed unexpectedly: {exc}")
            self.engine._pending_fills.discard(self.leg_key)

    def _watch_cancel_final_chance(self, order_id, symbol, quantity, order_action, error_state):
        """Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned."""
        try:
            result = _reprice_and_wait_once(self.engine.client, order_id, self.engine.env.strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, order_action, quantity)
            pos = self.leg.position
            if pos.error_order_id != order_id:
                return
            is_hedge = error_state == "hedge_entry_failed"
            if result is not None:
                fill_px = float(result.get("average_price") or result.get("price") or 0.0)
                if is_hedge:
                    pos.hedge_entry_filled = True
                    pos.hedge_entry_px = fill_px
                else:
                    pos.entry_filled = True
                    pos.entry_px = fill_px
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
                if is_hedge:
                    pos.hedge_entry_order_id = ""
                else:
                    self.leg.position = ShortHedgePosition()
                self.engine.save_state()
            self.engine._push_leg_error_bg(self.leg_key, self.leg.position, clear=True)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(order_id, error_state, "resting", order_id, str(exc))
        finally:
            self.engine._pending_fills.discard(self.leg_key)


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

        self.monthly = {ot: MonthlyShortEngine(ot, self) for ot in OPTION_TYPES}

        self._monthly_expiry_cache: Optional[tuple] = None
        self._cycle_start_date_cache: Optional[tuple] = None
        self._last_candle_boundary: Optional[datetime] = None

        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds -- doing that INSIDE run_cycle would stall every other
        # leg's check for the same duration. Order confirmation runs on a
        # per-leg background task instead, submitted to _fill_executor,
        # sized to the leg count so both legs can have a watcher in flight
        # at once. Mirrors the proven pattern in every other deployed script.
        self._fill_executor = ThreadPoolExecutor(max_workers=len(LEG_KEYS), thread_name_prefix="fill-watch")
        # Separate, single-worker pool for check_force_exit/push_leg_error/
        # notify_trade_closed/check_pending_action's HTTP calls -- kept off
        # _fill_executor so these can never queue silently behind a fill
        # watcher stuck for minutes in a reprice loop.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")
        # _state_lock serializes every state_store.save() call (main thread
        # and background watchers alike) -- StateStore.save() writes the
        # WHOLE state to one shared JSON file, so two concurrent writers
        # could corrupt it. _pending_fills tracks which leg_keys already
        # have an active watcher chain so evaluate() never submits a
        # duplicate one.
        self._state_lock = threading.Lock()
        self._pending_fills: set = set()
        # Guards MonthlyShortEngine's _manage_open_position_bg /
        # _maybe_evaluate_entry_bg / _maybe_daily_decision_bg dispatches --
        # a leg is never doing more than one of these at once.
        self._eval_pending: set = set()
        # check_force_exit is a synchronous local HTTP call -- run inline on
        # the main scheduler thread it would be the same class of blocking
        # bug as poll_fill. Dispatched every cycle via
        # _refresh_force_exit_check_bg instead; _force_exit_pending is the
        # cheap boolean run_cycle actually reads.
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        # Throttles the outer run_cycle except-clause's WhatsApp alert.
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        # See _repush_active_errors below.
        self._last_error_push: dict[str, datetime] = {}

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

    def get_monthly_expiry(self) -> tuple[str, str]:
        today = datetime.now(IST).date().isoformat()
        if self._monthly_expiry_cache and self._monthly_expiry_cache[0] == today:
            return self._monthly_expiry_cache[1], self._monthly_expiry_cache[2]
        compact, raw = resolve_monthly_expiry(self.client)
        self._monthly_expiry_cache = (today, compact, raw)
        Log.info(f"Monthly expiry resolved: {raw} ({compact})")
        return compact, raw

    def get_cycle_start_date(self) -> Optional["datetime.date"]:
        today = datetime.now(IST).date().isoformat()
        if self._cycle_start_date_cache and self._cycle_start_date_cache[0] == today:
            return self._cycle_start_date_cache[1]
        try:
            cycle_start = resolve_cycle_start_date(self.client)
        except RuntimeError as exc:
            Log.warning(f"resolve_cycle_start_date failed: {exc}")
            return None
        self._cycle_start_date_cache = (today, cycle_start)
        Log.info(f"Cycle start date resolved: {cycle_start}")
        return cycle_start

    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily state.")
            self.state.current_day = today_key
            self.state.today_realized_pnl = 0.0
            self.state.reference = ReferenceSnapshot()
            # trade_count is a cumulative counter (never reset daily) --
            # nothing to reset here per-leg, unlike the sibling script's
            # candle-boundary markers (this script's per-day markers,
            # last_entry_eval_date/last_daily_decision_date, are compared
            # against the CURRENT date directly, so a new day naturally
            # makes them stale without needing an explicit reset).
            self._monthly_expiry_cache = None
            self._cycle_start_date_cache = None
            self.save_state()

    def _ensure_reference(self):
        """Reused unchanged from Nifty_OI_WeeklyBuy_MonthlySell -- computed
        once, fixed for the rest of the day."""
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
        # naive tzinfo= argument -- see Nifty_OI_WeeklyBuy_MonthlySell's
        # identical comment for the LMT-offset corruption this avoids.
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
        # Anchored at 09:30 (not 09:35) -- fetch_reference_oi_premium reads
        # this via the 5-minute bootstrap interval, so 09:30 and the first
        # 09:35 "current" read land on genuinely different candles. We still
        # wait until 09:35 real time (above) before finalizing, so that
        # later 5m candle actually exists by the time anything reads it.
        ref.reference_time_iso = datetime.now(IST).replace(
            hour=9, minute=30, second=0, microsecond=0).isoformat()
        ref.computed = True
        Log.info(f"[Reference] gap={gap_pct:+.3f}% (> {config.gap_threshold_pct}%) -> "
                 f"Reference Time = today 09:30 (5m bootstrap)")

    def _new_candle_closed(self) -> bool:
        boundary = _current_candle_boundary(15)
        if self._last_candle_boundary is None:
            self._last_candle_boundary = boundary
            return False
        if self._last_candle_boundary != boundary:
            self._last_candle_boundary = boundary
            return True
        return False

    def _refresh_force_exit_check_bg(self):
        """Dispatches check_force_exit to _bg_executor every cycle instead
        of running it inline -- not throttled, per AUTHORING_CHECKLIST.md."""
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

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state -- re-pushes at most once per
        config.error_repush_interval_sec for every leg still in
        error_state, so a single lost push self-heals within a minute.
        Called unconditionally at the top of run_cycle(), every cycle."""
        now = datetime.now(IST)
        for leg_key in LEG_KEYS:
            pos = self.state.legs[leg_key].position
            if not pos.error_state:
                self._last_error_push.pop(leg_key, None)
                continue
            last = self._last_error_push.get(leg_key)
            if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
                continue
            self._last_error_push[leg_key] = now
            is_hedge = pos.error_state in ("hedge_entry_failed", "hedge_exit_failed")
            is_exit = pos.error_state in ("exit_failed", "hedge_exit_failed")
            buy_action = "BUY" if is_hedge else "SELL"
            sell_action = "SELL" if is_hedge else "BUY"
            action = sell_action if is_exit else buy_action
            self._push_leg_error_bg(leg_key, pos, action=action)

    def _push_leg_error_bg(self, leg_key: str, pos: "ShortHedgePosition", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _bg_executor -- `pos` is a
        live, mutable object the same cycle may reset moments later, so it
        is snapshotted with a shallow copy on THIS (calling) thread first."""
        snapshot = copy.copy(pos)
        self._bg_executor.submit(push_leg_error, self.env, leg_key, snapshot, action=action, clear=clear)

    def _notify_trade_closed_bg(self):
        self._bg_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)

    def _notify_telegram_error_bg(self, message: str):
        try:
            self._bg_executor.submit(
                notify_telegram_error, self.env, f"[{config.strategy_name}] {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

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

        self._bg_executor.submit(_run)

    def _pop_pending_action(self, leg_key: str) -> Optional[dict]:
        return self._pending_action_cache.pop(leg_key, None)

    def run_cycle(self):
        try:
            self._repush_active_errors()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                # Resolve any pending Retry/Cancel/Manual decision on an
                # errored leg BEFORE returning -- this branch is the ONLY
                # code path that runs while Force Exit is pending, so it
                # must not deadlock a leg stuck in error_state (see
                # AUTHORING_CHECKLIST.md section 1).
                for ot in OPTION_TYPES:
                    side = self.monthly[ot]
                    if side.leg.position.error_state:
                        self._refresh_pending_action_bg(side.leg_key)
                        pending = self._pop_pending_action(side.leg_key)
                        if pending is not None:
                            side._resolve_leg_error(pending)
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

            new_candle = self._new_candle_closed()

            for ot in OPTION_TYPES:
                self.monthly[ot].evaluate()

            if new_candle:
                self.save_state()
        except Exception as exc:
            Log.exception(f"run_cycle failed: {exc}")
            now = datetime.now(IST)
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                self._notify_telegram_error_bg(f"Cycle failed: {exc}")

    def reconcile_pending_orders(self):
        """Startup-only crash recovery: finds any of the FOUR independent
        order lifecycles (short entry, hedge entry, short exit, hedge exit)
        whose order was placed but never confirmed filled before the
        previous process instance stopped. Called once from main(), before
        the scheduler starts."""
        for leg_key, leg in self.state.legs.items():
            pos = leg.position
            if pos.error_state:
                continue
            if pos.entry_order_id and not pos.entry_filled:
                self._reconcile_one(leg_key, leg, pos, pos.entry_order_id, "entry_failed", pos.short_symbol, "SELL")
            elif pos.hedge_entry_order_id and not pos.hedge_entry_filled:
                self._reconcile_one(leg_key, leg, pos, pos.hedge_entry_order_id, "hedge_entry_failed",
                                     pos.hedge_symbol, "BUY")
            elif pos.short_exit_order_id and not pos.short_exit_filled:
                self._reconcile_one(leg_key, leg, pos, pos.short_exit_order_id, "exit_failed", pos.short_symbol, "BUY")
            elif pos.hedge_exit_order_id and not pos.hedge_exit_filled:
                self._reconcile_one(leg_key, leg, pos, pos.hedge_exit_order_id, "hedge_exit_failed",
                                     pos.hedge_symbol, "SELL")
            elif pos.short_symbol and not pos.entry_order_id and not pos.entry_filled:
                # The narrow crash window: _enter() persists the leg as
                # "attempting entry" (short_symbol set, entry_order_id
                # empty) BEFORE calling place(). Never guess -- flag for a
                # human to verify against the broker directly.
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
                push_leg_error(self.env, leg_key, pos, action="SELL")
                self.save_state()
                Log.error(f"[{leg_key}] reconcile: ambiguous pre-placeorder crash window -- "
                          f"flagged for manual verification against the broker.")

    def _reconcile_one(self, leg_key, leg, pos, order_id, error_state, symbol, action):
        try:
            resp = self.client.orderstatus(order_id=order_id, strategy=self.env.strategy_tag)
        except Exception as exc:
            Log.exception(f"[{leg_key}] reconcile: orderstatus() failed for {order_id} -- "
                          f"flagging for manual review rather than guessing.")
            pos.error_state = error_state
            pos.error_kind = "resting"
            pos.error_order_id = order_id
            pos.error_message = f"reconcile after restart: orderstatus() failed: {exc}"
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, leg_key, pos, action=action)
            self.save_state()
            return

        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        Log.info(f"[{leg_key}] reconcile: {error_state} order {order_id} status='{status}' "
                 f"(resuming after restart)")

        if status == "complete":
            fill_px = float(data.get("average_price") or data.get("price") or 0.0)
            if error_state == "entry_failed":
                pos.entry_px = fill_px
                pos.entry_filled = True
                self.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
            elif error_state == "hedge_entry_failed":
                pos.hedge_entry_px = fill_px
                pos.hedge_entry_filled = True
                self.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
            elif error_state == "exit_failed":
                pos.short_exit_fill_px = fill_px
                pos.short_exit_filled = True
            else:  # hedge_exit_failed
                pos.hedge_exit_fill_px = fill_px
                pos.hedge_exit_filled = True
            Log.info(f"[{leg_key}] reconcile: {error_state} order {order_id} was actually filled @ "
                     f"{fill_px} -- resuming.")
            if pos.short_exit_filled and pos.hedge_exit_filled:
                self.monthly[leg_key.split("_")[1]]._finalize_exit(pos, pos.pending_exit_reason)
            else:
                self.save_state()
            return

        if status in {"rejected", "cancelled", "canceled"}:
            if error_state == "entry_failed":
                leg.trade_count = max(0, leg.trade_count - 1)
                leg.position = ShortHedgePosition()
                Log.info(f"[{leg_key}] reconcile: short entry {order_id} was genuinely rejected/cancelled -- "
                         f"clearing the leg, safe to re-evaluate fresh.")
            elif error_state == "hedge_entry_failed":
                pos.hedge_entry_order_id = ""
                Log.info(f"[{leg_key}] reconcile: hedge entry {order_id} was genuinely rejected/cancelled -- "
                         f"short leg remains open and naked, will retry the hedge next cycle.")
            elif error_state == "exit_failed":
                pos.short_exit_order_id = ""
                pos.short_exit_filled = False
                Log.info(f"[{leg_key}] reconcile: short exit {order_id} was genuinely rejected/cancelled -- "
                         f"leg remains open, will re-evaluate exit conditions normally.")
            else:
                pos.hedge_exit_order_id = ""
                pos.hedge_exit_filled = False
                Log.info(f"[{leg_key}] reconcile: hedge exit {order_id} was genuinely rejected/cancelled -- "
                         f"short leg already closed, will retry the hedge exit next cycle.")
            self.save_state()
            return

        # Still resting/pending -- flag for a human decision.
        pos.error_state = error_state
        pos.error_kind = "resting"
        pos.error_order_id = order_id
        pos.error_message = f"reconcile after restart: order still '{status}', needs a decision."
        pos.error_since = datetime.now(IST).isoformat()
        push_leg_error(self.env, leg_key, pos, action=action)
        Log.error(f"[{leg_key}] reconcile: {error_state} order {order_id} still '{status}' after restart -- "
                  f"needs Retry/Cancel/Manually Completed.")
        self.save_state()

    def _open_positions_for_pnl(self) -> list:
        """Reports the short leg and the hedge leg as TWO SEPARATE entries
        -- each with its own symbol/direction/entry_price/pnl -- not one
        merged row with the hedge's PnL silently folded into the short
        leg's number. A merged single row means the platform's View
        Trade/PnL UI never sees the hedge/BUY leg's own symbol at all --
        confirmed live 2026-08-24 on Nifty_LEAPS_OptionsSell_1 (this
        script's own descendant), which had the identical shape: the
        hedge leg simply never appeared there, even though it was
        genuinely filled."""
        open_positions = []
        for leg_key, leg in self.state.legs.items():
            pos = leg.position
            if not pos.short_symbol or not pos.entry_filled:
                continue
            short_ltp = self.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
            if short_ltp is None:
                short_ltp = pos.entry_px  # last-known fallback -- never fabricate movement
            short_pnl = (pos.entry_px - short_ltp) * pos.quantity

            open_positions.append({
                "leg_key": leg_key, "symbol": pos.short_symbol, "direction": "SHORT",
                "quantity": -pos.quantity, "entry_price": pos.entry_px, "current_price": short_ltp,
                "pnl": short_pnl, "entry_time": pos.entry_time, "execution_id": pos.execution_id,
            })

            if pos.hedge_symbol and pos.hedge_entry_filled:
                hedge_ltp = self.price_stream.get_ltp(pos.hedge_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
                if hedge_ltp is None:
                    hedge_ltp = pos.hedge_entry_px
                hedge_pnl = (hedge_ltp - pos.hedge_entry_px) * pos.quantity
                open_positions.append({
                    "leg_key": f"{leg_key}_HEDGE", "symbol": pos.hedge_symbol, "direction": "LONG",
                    "quantity": pos.quantity, "entry_price": pos.hedge_entry_px, "current_price": hedge_ltp,
                    "pnl": hedge_pnl, "entry_time": pos.entry_time, "execution_id": pos.execution_id,
                })
        return open_positions

    def report_pnl_tick(self):
        # Scheduled as its own APScheduler job ("report_pnl_tick") with
        # max_instances=1 -- a slow/timed-out push queues behind itself,
        # never behind strategy_cycle.
        try:
            report_pnl_to_platform(self.env, self.state.today_realized_pnl, self._open_positions_for_pnl())
        except Exception:
            Log.exception("report_pnl_tick failed")

    def _force_exit_all(self):
        """Force-closes every leg currently holding a position, regardless
        of the strategy's own signal/exit logic -- idempotent/resumable
        across cycles. A leg already in error_state is left untouched.
        Short (naked, undefined risk) leg is always closed before its own
        hedge (bounded risk) leg -- see MonthlyShortEngine._exit's own
        ordering, reused here unchanged."""
        all_flat = True
        for ot in OPTION_TYPES:
            side = self.monthly[ot]
            pos = side.leg.position
            if pos.error_state:
                all_flat = False
            elif pos.short_symbol:
                if (pos.short_exit_order_id and pos.short_exit_filled
                        and pos.hedge_exit_order_id and pos.hedge_exit_filled):
                    side._finalize_exit(pos, pos.pending_exit_reason)
                else:
                    all_flat = False
                    if side.leg_key not in self._pending_fills and not pos.short_exit_order_id:
                        pos.pending_reentry_search = False
                        side._exit(pos, "force_exit")
        self.save_state()
        if all_flat:
            ack_force_exit_complete(self.env)
        return all_flat


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
    print(f"Monthly delta target : {config.monthly_delta_target} "
          f"(stop-widening band: {config.monthly_delta_low}-{config.monthly_delta_high})")
    print(f"SL tier 1            : premium >= entry * {1 + config.sl_tier1_pct / 100.0:.3f}")
    print(f"SL tier 2 (ratchet)  : locks to breakeven once premium <= entry * "
          f"{config.sl_tier2_trigger_pct / 100.0:.2f}")
    print(f"Hedge target premium : {config.hedge_premium_pct_of_short}% of sold strike's premium")
    print(f"Entry evaluation time: {config.entry_evaluation_time.strftime('%H:%M')} (cycle-start day only)")
    print(f"Daily decision time  : {config.daily_decision_time.strftime('%H:%M')} (non-entry days)")
    print(f"Quantity per leg     : {config.quantity}")
    print(f"Product              : {config.product}")
    print("MONTHLY SELL -- delta-qualified strike + own-side OI accumulation, hedged, tiered SL.")
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
            if leg.position.short_symbol:
                already_known.append({"symbol": leg.position.short_symbol, "exchange": OPTIONS_EXCHANGE})
            if leg.position.hedge_symbol:
                already_known.append({"symbol": leg.position.hedge_symbol, "exchange": OPTIONS_EXCHANGE})
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
    # order-placed-but-not-yet-confirmed window before anything else
    # touches state. An already-fully-filled leg needs no reconciliation
    # (it resumes normally once the scheduler starts).
    engine.reconcile_pending_orders()

    for leg_key, leg in state_store.state.legs.items():
        if leg.position.error_state:
            is_hedge = leg.position.error_state in ("hedge_entry_failed", "hedge_exit_failed")
            is_exit = leg.position.error_state in ("exit_failed", "hedge_exit_failed")
            action = ("SELL" if is_exit else "BUY") if is_hedge else ("BUY" if is_exit else "SELL")
            push_leg_error(env, leg_key, leg.position, action=action)
            Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                      f"({leg.position.error_state}/{leg.position.error_kind}) -- "
                      f"needs Retry/Cancel/Manually Completed.")
        elif leg.position.short_symbol:
            Log.info(f"[{leg_key}] Resuming an already-open position from before restart: "
                      f"short={leg.position.short_symbol}@{leg.position.entry_px} "
                      f"hedge={leg.position.hedge_symbol}@{leg.position.hedge_entry_px} -- "
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
        id="report_pnl_tick",
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
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        raise


if __name__ == "__main__":
    main()
