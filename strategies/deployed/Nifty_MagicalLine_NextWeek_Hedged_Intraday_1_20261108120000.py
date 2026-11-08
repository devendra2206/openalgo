"""
===============================================================================
NIFTY Intraday Magical Line -- Next-Week Expiry, Hedged Option Selling
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11

Live port of the rules backtested in
data23_to_26/backtest_nifty_magical_line.py (--expiry nextweek --hedge 1000,
hourly checks, 15% SL), confirmed by the author on 2026-11-08 as "same as
backtest", with three live choices made by the author the same day:
  * the line is read from the broker's 15-minute 09:15 candle at 09:31;
  * each hourly check reads the 09:30-based hourly candle at hh:31
    (one-minute buffer);
  * no hedge priced -> no trade (never sell naked).

Rules
------------------------------------------------------------------------
  Line      Close of the 09:15-09:30 15-minute candle, fixed for the day.
            Read at 09:31 and confirmed against its own 1-minute bars (09:15
            open, 09:29 close; never Shoonya's 09:14 pre-open bar). If they
            disagree it is re-read for 2 minutes, then the 1-minute values
            are used and an alert is sent.
  Entry     Candle close > open -> sell a PUT; close < open -> sell a CALL;
            close == open -> no 09:31 entry (the 10:30 check picks the side).
  Strike    Round-100 OTM strikes, |delta| closest to 0.25, HARD band
            0.20-0.30 (server delta from optionchain with_greeks). Nothing in
            the band -> no trade at that moment.
  Expiry    Next week: the 2nd upcoming NIFTY option expiry (on an expiry
            day that is next week's), never more than 14 days away, else no
            trade that day. Resolved like Nifty_OI_Positional's weekly hedge
            expiry: client.expiry(), dates >= today, cached once a day.
  Hedge     Same side and expiry, 1000 points further OTM. If that strike has
            no two-sided quote, 1050, 1100 ... 1300 are tried in turn. None ->
            no trade (author: never sell naked).
  Retry     No short strike in the band, or no hedge priced (author
            2026-11-08): wait 5 minutes and try ONCE more, same side and
            entry type (09:31 entry, check entries). Still nothing -> the next
            check decides. A check that runs first replaces the retry. A
            re-entry after an SL keeps re-trying each completed minute.
  Checks    Hourly candles 09:30-10:30 ... 13:30-14:30, read at 10:31, 11:31,
            12:31, 13:31, 14:31. The candle is built from that hour's
            1-minute candles, so its close is the hh:29 1-minute close
            (today-only reads, so the pre-open fold into the previous day's
            last candle never reaches it). The hh:29 bar is awaited up to
            90s with the SL still watched; if the broker never publishes it,
            the live feed's hh:29 minute close is used; failing both, the
            check is skipped and alerted -- never decided on a later price.
              PUT held and close < line  -> close it, sell a CALL.
              CALL held and close > line -> close it, sell a PUT.
              right side, or exactly on the line -> hold.
              flat (no strike earlier / doji) -> sell the side the line says.
  SL        Short leg only: LTP >= short fill x 1.15 -> close (short, then
            hedge). Checked every scheduler tick from the WebSocket LTP.
  Re-entry  After an SL nothing trades until the next check. At it:
              line says the other side -> sell that side now;
              line says the same side  -> wait until NIFTY reaches P, the
                stopped trade's NIFTY entry spot (PUT: spot >= P, CALL:
                spot <= P), judged on completed 1-minute closes, then sell
                it again with a fresh SL; its spot is the new P.
            Later checks still apply while waiting (a flip sells the other
            side).
  EOD       15:15: close everything. Nothing is carried overnight.

Order sequence (author 2026-11-08)
------------------------------------------------------------------------
  ENTRY  hedge BUY  -> (on fill) short SELL
  EXIT   short BUY  -> (on fill) hedge SELL
Buying the long leg first reduces the margin the short blocks, and a failure
part-way through leaves a long option rather than a naked short. Closing is
the reverse for the same reason.

Execution, duplicates, crash recovery, failure handling
------------------------------------------------------------------------
  * ONE position slot (leg key "MAGIC"). Every order chain (entry, exit,
    switch, check, line read) runs on a background executor behind a single
    busy guard, so two decisions can never become two orders.
  * Every order id is persisted the moment placeorder() returns, before the
    fill is awaited; every fill is confirmed with orderstatus(). place()
    retries only a clean broker rejection, never a raised exception (the
    outcome of that is unknown and a retry could duplicate the order).
  * A stale MARKET order is re-priced across the spread for ~5 minutes, then
    the leg goes to Retry / Cancel / Manually Completed on the platform with
    a Telegram alert. Nothing is ever decided by guessing.
  * Startup reconciliation asks the broker about every order that was placed
    but not confirmed before the previous process stopped. A crash inside
    placeorder() itself (nothing recorded) is flagged for a human, never
    retried blindly.
  * Every chain is resumable from the persisted state, so a resolved error
    or a restart continues exactly where it stopped.
  * The platform's Force Exit closes everything (short first), stops new
    trades for the day and reports completion once flat.
  * The 15:15 square-off runs every tick from 15:15 until flat.

Product: NRML. Shoonya rejects MIS orders at its 15:15 MIS cut-off
("MIS orders cannot be placed after square-off time"), which is exactly the
square-off time here; with NRML the strategy's own 15:15 close is the only
thing that closes the position, so an exit failure there is alerted.

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
import re
import threading
import time as time_mod
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from openalgo import api

# Several always-on threads at the default 8MB stack add up against the
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap every strategy subprocess runs under.
# Must be called before any thread is created.
threading.stack_size(1024 * 1024)

try:
    from _strategy_platform_client import (
        filter_known_fields,
        notify_telegram_error,
        notify_trade_closed,
    )
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
LEG_KEY = "MAGIC"
LEG_KEYS = [LEG_KEY]

# Where the state file and trade log live. A module global (not inlined) so a
# simulation harness can point it at a scratch folder.
BASE_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    strategy_name: str = "NIFTY Magical Line Next-Week Hedged Intraday"
    version: str = "1.0.0"

    lots: int = 1                       # author 2026-11-08: 1 lot to start
    default_lot_size: int = 65          # used only if the chain carries no lotsize

    # Strike selection (backtest DELTA_TARGET/LOW/HIGH, STRIKE_STEP, MAX_STEPS)
    delta_target: float = 0.25
    delta_low: float = 0.20
    delta_high: float = 0.30
    strike_round: int = 100
    band_step_count: int = 8            # 8 x 100 pts per band, 2 bands = +/-1600 pts
    max_bands: int = 2

    sl_pct: float = 15.0

    hedge_distance_pts: int = 1000
    hedge_extra_max_pts: int = 300
    hedge_extra_step_pts: int = 50

    next_week_max_days: int = 14

    # The line: the broker's 15-minute 09:15 candle, read at 09:31.
    line_candle_interval: str = "15m"
    line_candle_start: time = time(9, 15)
    line_read_time: time = time(9, 31)
    line_wait_sec: float = 120.0         # broker publishing lag allowance before the 1m fallback
    # The 09:31 entry is taken only up to this time. A process started later
    # (or a line that could not be read until then) leaves the side to the
    # 10:31 check, rather than entering an "09:30" trade at 10:05.
    initial_entry_deadline: time = time(9, 45)

    # Hourly checks: the candle ENDING at each of these times (09:30-based
    # hours), read check_buffer_min later.
    check_times: tuple = (time(10, 30), time(11, 30), time(12, 30), time(13, 30), time(14, 30))
    check_buffer_min: int = 1
    check_bar_wait_sec: float = 90.0
    bar_retry_gap_sec: float = 5.0

    square_off_time: time = time(15, 15)
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)

    # No short strike in the delta band, or no hedge priced (author
    # 2026-11-08): wait this long and try ONCE more, same side and entry type.
    # For the 09:31 entry and check entries; a re-entry after an SL already
    # re-tries every completed minute while NIFTY stays past P. A check that
    # runs first replaces a pending retry; none is sent at/after 15:15.
    not_found_retry_min: int = 5
    not_found_retry_types: tuple = ("INITIAL", "SWITCH", "CHECK")

    # Runaway guard only: the backtest never exceeded 6 entries in a day
    # (3078 trades, 2021-2026). Reaching this stops new entries and alerts.
    max_entries_per_day: int = 10

    # Muhurat / special sessions are skipped (author's standing choice).
    skip_special_sessions: bool = True

    product: str = "NRML"
    price_type: str = "MARKET"

    scheduler_interval: float = 2.0
    pnl_tick_interval: float = 1.0
    spot_rest_fallback_gap_sec: float = 5.0

    ws_stale_seconds: float = 20.0
    ws_stale_seconds_open: float = 60.0
    ws_post_open_grace_until: time = time(10, 0)
    ws_watchdog_interval: float = 15.0
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59      # ~5 minutes before a human is asked

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    error_repush_interval_sec: float = 60.0
    cycle_failure_notify_interval_sec: float = 300.0

    state_file: str = "strategy_state.json"
    log_level: int = logging.INFO


config = Config()
IST = pytz.timezone("Asia/Kolkata")


def _now() -> datetime:
    """Every clock read in this file goes through here, so a simulation
    harness can drive the strategy on a replayed day."""
    return datetime.now(IST)


def _sleep(seconds: float):
    time_mod.sleep(seconds)


def _at(t: time, d=None) -> datetime:
    """Today's (or d's) IST datetime at t -- IST.localize, never tzinfo=IST,
    which pytz resolves to LMT (+05:53)."""
    d = d or _now().date()
    return IST.localize(datetime.combine(d, t))


def _plus_min(t: time, minutes: int) -> time:
    return (datetime.combine(datetime(2000, 1, 1).date(), t) + timedelta(minutes=minutes)).time()


###############################################################################
# LOGGER
###############################################################################
class Log:
    logger = logging.getLogger("OpenAlgoStrategy")
    logger.setLevel(config.log_level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
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
class Position:
    """The one position slot: a short option and its hedge, two independent
    order lifecycles. `hedge_symbol` (the first leg placed) is the "this slot
    holds an attempt or a position" marker."""
    side: str = ""                      # "PE" (sold PUT) or "CE" (sold CALL)
    leg_type: str = ""                  # INITIAL / SWITCH / CHECK / REENTRY
    expiry: str = ""                    # ISO date of the contract
    quantity: int = 0

    short_symbol: str = ""
    short_strike: float = 0.0
    delta_at_entry: float = 0.0
    entry_spot: float = 0.0             # NIFTY at the decision -- the re-entry level P
    entry_time: str = ""
    entry_px: float = 0.0
    sl_px: float = 0.0
    entry_order_id: str = ""
    entry_filled: bool = False
    short_exit_order_id: str = ""
    short_exit_filled: bool = False
    short_exit_fill_px: float | None = None
    short_exit_time: str = ""

    hedge_symbol: str = ""
    hedge_strike: float = 0.0
    hedge_entry_time: str = ""
    hedge_entry_px: float = 0.0
    hedge_entry_order_id: str = ""
    hedge_entry_filled: bool = False
    hedge_exit_order_id: str = ""
    hedge_exit_filled: bool = False
    hedge_exit_fill_px: float | None = None
    hedge_exit_time: str = ""

    pending_exit_reason: str = ""       # set when a close starts; "" while open
    execution_id: int = 0

    # Order error recovery (docs/prd/python-strategies-order-error-recovery.md):
    # "hedge_entry_failed", "entry_failed" (short), "exit_failed" (short),
    # "hedge_exit_failed".
    error_state: str = ""
    error_kind: str = ""                # "terminal" | "resting"
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""


@dataclass
class StrategyState:
    current_day: str = ""
    line: float = 0.0
    candle_open: float = 0.0
    line_ready: bool = False
    line_source: str = ""
    initial_done: bool = False
    last_check_slot: str = ""           # "HH:MM" of the last check run today
    day_done: bool = False              # 15:15 reached or Force Exit -- no new trades today
    entries_today: int = 0
    # After an SL: the stopped side and its entry spot P. armed once a later
    # check confirmed the same side; then NIFTY reaching P re-enters.
    reentry_side: str = ""
    reentry_p: float = 0.0
    reentry_armed: bool = False
    reentry_sl_time: str = ""
    # One pending "not found" retry: side, entry type and due time (ISO).
    retry_side: str = ""
    retry_leg_type: str = ""
    retry_due: str = ""
    position: Position = field(default_factory=Position)
    today_realized_pnl: float = 0.0
    last_execution_id: int = 0
    last_updated: str = ""


def _day_fields() -> dict:
    """The fields reset at a new day (the position is NOT one of them: an
    open position is never silently forgotten)."""
    fresh = StrategyState()
    return {k: getattr(fresh, k) for k in (
        "line", "candle_open", "line_ready", "line_source", "initial_done",
        "last_check_slot", "day_done", "entries_today", "reentry_side",
        "reentry_p", "reentry_armed", "reentry_sl_time", "retry_side", "retry_leg_type",
        "retry_due", "today_realized_pnl")}


###############################################################################
# ENVIRONMENT / BROKER
###############################################################################
class Environment:
    def __init__(self):
        self.api_key = os.getenv("OPENALGO_API_KEY")
        self.host = os.getenv("HOST_SERVER") or os.getenv("OPENALGO_HOST") or "http://127.0.0.1:5000"
        self.version = "v1"
        self.timeout = 10.0
        self.ltp_timeout = 3.0
        # Option-chain calls only. Measured live on Shoonya 05-Nov-2026:
        # strike_count 16 6.3s, 30 10.0s (past the 10s above), 60 19.4s.
        self.chain_timeout = 60.0
        self.ws_url = os.getenv("WEBSOCKET_URL")
        self.strategy_tag = (os.getenv("OPENALGO_STRATEGY_TAG") or os.getenv("STRATEGY_ID")
                             or "nifty_magical_line_nextweek_hedged")

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


class Broker:
    def __init__(self, env: Environment):
        self.env = env
        self.client = None

    def connect(self):
        self.env.validate()
        # PriceStream's watchdog owns reconnect; the SDK's own auto_reconnect
        # races it (see the VWAP/OI scripts).
        self.client = api(api_key=self.env.api_key, host=self.env.host, version=self.env.version,
                          timeout=self.env.timeout, ws_url=self.env.ws_url, auto_reconnect=False)
        Log.info("Connected to OpenAlgo")
        return self.client

    def connect_ltp_client(self):
        return api(api_key=self.env.api_key, host=self.env.host, version=self.env.version,
                   timeout=self.env.ltp_timeout, ws_url=self.env.ws_url)

    def connect_chain_client(self):
        return api(api_key=self.env.api_key, host=self.env.host, version=self.env.version,
                   timeout=self.env.chain_timeout)


def _within_market_hours() -> bool:
    t = _now().time()
    return config.market_open <= t <= config.market_close


def _current_ws_stale_threshold() -> float:
    t = _now().time()
    if config.market_open <= t < config.ws_post_open_grace_until:
        return config.ws_stale_seconds_open
    return config.ws_stale_seconds


###############################################################################
# LIVE PRICE STREAM -- the hardened PriceStream used by every deployed
# script (VWAP_NoHA / OI_Positional), with seed_instruments for startup.
###############################################################################
class PriceStream:
    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._spot_minutes: dict = {}
        self._instruments: dict = {}
        self._stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._stale_streak: dict = {}
        self._symbol_backoff_step: dict = {}
        self._symbol_next_retry_at: dict = {}

    def _on_tick(self, msg):
        try:
            symbol = msg["symbol"]
            exchange = msg.get("exchange", "")
            ltp = float(msg["data"]["ltp"])
        except (KeyError, TypeError, ValueError) as exc:
            Log.warning(f"[PriceStream] malformed tick ignored: {exc} ({msg})")
            return
        now = _now()
        with self._lock:
            self._cache[(symbol, exchange)] = (ltp, now)
            if symbol == UNDERLYING_SYMBOL and exchange == UNDERLYING_SPOT_EXCHANGE:
                # Last NIFTY tick of each minute = that minute's close, at
                # tick precision (the scheduler only samples every 2s).
                self._spot_minutes[now.replace(second=0, microsecond=0)] = ltp
                if len(self._spot_minutes) > 600:
                    for k in sorted(self._spot_minutes)[:-400]:
                        del self._spot_minutes[k]

    def spot_minute_close(self, minute: datetime) -> float | None:
        """NIFTY close of the minute starting at `minute` from live ticks, only
        once that minute has ended."""
        if _now().replace(second=0, microsecond=0) <= minute:
            return None
        with self._lock:
            return self._spot_minutes.get(minute)

    def get_ltp(self, symbol: str, exchange: str, max_age: float) -> float | None:
        with self._lock:
            entry = self._cache.get((symbol, exchange))
        if entry is None:
            return None
        ltp, ts = entry
        if (_now() - ts).total_seconds() > max_age:
            return None
        return ltp

    def seed_instruments(self, instruments: list):
        """Known-instrument set without subscribing -- called before start(),
        whose own connect subscribes everything seeded in one call."""
        with self._lock:
            for inst in instruments:
                self._instruments[(inst["symbol"], inst["exchange"])] = inst

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
        to_remove = []
        with self._lock:
            for inst in instruments:
                key = (inst["symbol"], inst["exchange"])
                if key in self._instruments:
                    del self._instruments[key]
                    to_remove.append(inst)
                self._cache.pop(key, None)
                self._stale_streak.pop(key, None)
                self._symbol_backoff_step.pop(key, None)
                self._symbol_next_retry_at.pop(key, None)
        if not to_remove:
            return
        try:
            self.client.unsubscribe_ltp(to_remove)
            Log.info(f"[PriceStream] unsubscribed: {to_remove}")
        except Exception as exc:
            Log.warning(f"[PriceStream] unsubscribe_ltp failed for {to_remove}: {exc}")

    def _connect(self):
        self.client.connect()
        with self._lock:
            all_instruments = list(self._instruments.values())
        if all_instruments:
            self.client.subscribe_ltp(all_instruments, on_data_received=self._on_tick)
        Log.info("[PriceStream] connected" + (f" and (re)subscribed: {all_instruments}" if all_instruments else ""))

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
            with self._lock:
                cached = self._cache.get(key)
            try:
                resp = self.client.quotes(symbol=inst["symbol"], exchange=inst["exchange"])
            except Exception as exc:
                Log.warning(f"[PriceStream] REST confirm-check failed for {inst['symbol']}: {exc}")
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
            if not (getattr(self.client, "connected", False) and getattr(self.client, "authenticated", False)):
                failures += 1
                wait = backoffs[min(failures - 1, len(backoffs) - 1)]
                Log.warning(f"[PriceStream] connection down (attempt {failures}) -- reconnecting, then {wait}s.")
                self._teardown()
                try:
                    self._connect()
                except Exception as exc:
                    Log.warning(f"[PriceStream] reconnect failed: {exc}")
                self._stop.wait(wait)
                continue

            now = _now()
            stale_threshold = _current_ws_stale_threshold()
            with self._lock:
                tracked = list(self._instruments.items())
            stale_instruments = []
            for key, inst in tracked:
                with self._lock:
                    entry = self._cache.get(key)
                if entry is None or (now - entry[1]).total_seconds() > stale_threshold:
                    stale_instruments.append(inst)
            all_keys = {key for key, _ in tracked}
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
            at_limit = {k for k in stale_keys if self._stale_streak[k] >= config.ws_stale_reconnect_after}
            if len(at_limit) > len(all_keys) / 2:
                if self._confirm_genuinely_broken_via_rest(stale_instruments):
                    failures += 1
                    wait = backoffs[min(failures - 1, len(backoffs) - 1)]
                    Log.warning(f"[PriceStream] {names} stale on a majority of symbols and REST shows "
                                f"movement -- full reconnect.")
                    self._teardown()
                    try:
                        self._connect()
                    except Exception as exc:
                        Log.warning(f"[PriceStream] full reconnect failed: {exc}")
                    for key in all_keys:
                        self._stale_streak[key] = 0
                        self._symbol_backoff_step.pop(key, None)
                        self._symbol_next_retry_at.pop(key, None)
                    self._stop.wait(wait)
                    continue
            due = [i for i in stale_instruments
                   if now >= self._symbol_next_retry_at.get((i["symbol"], i["exchange"]), now)]
            if not due:
                continue
            Log.warning(f"[PriceStream] stale/missing ticks for: {', '.join(i['symbol'] for i in due)} "
                        f"-- resubscribing just these.")
            try:
                self.client.subscribe_ltp(due, on_data_received=self._on_tick)
            except Exception as exc:
                Log.warning(f"[PriceStream] resubscribe failed: {exc}")
            for inst in due:
                key = (inst["symbol"], inst["exchange"])
                step = self._symbol_backoff_step.get(key, 0)
                self._symbol_backoff_step[key] = step + 1
                self._symbol_next_retry_at[key] = now + timedelta(seconds=backoffs[min(step, len(backoffs) - 1)])

    def start(self):
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="price-stream-watchdog",
                                                 daemon=True)
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        self._teardown()


###############################################################################
# STATE STORE
###############################################################################
class StateStore:
    def __init__(self, env: Environment):
        self.path = BASE_DIR / f"{Path(config.state_file).stem}_{env.strategy_tag}.json"
        self.state = StrategyState()

    def load(self):
        if not self.path.exists():
            self.save()
            return self.state
        with self.path.open("r") as fp:
            data = json.load(fp)
        st = StrategyState()
        for k in asdict(StrategyState()).keys():
            if k == "position" or k not in data:
                continue
            setattr(st, k, data[k])
        pos_raw = data.get("position") or {}
        st.position = Position(**{**asdict(Position()), **filter_known_fields(Position, pos_raw)})
        self.state = st
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = _now().isoformat()
        payload = asdict(self.state)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w") as fp:
            json.dump(payload, fp, indent=2)
        os.replace(tmp, self.path)   # atomic: a crash mid-write never leaves half a file


###############################################################################
# BROKER DATA HELPERS
###############################################################################
def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


def fetch_symbol_quote(client, symbol: str, exchange: str) -> dict | None:
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() failed for {symbol}: {exc}")
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        Log.warning(f"quotes() error response for {symbol}: {resp}")
        return None
    data = resp.get("data", resp)
    return data if isinstance(data, dict) else None


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> float | None:
    """require_two_sided: bid>0 AND ask>0 as well -- a tradable instrument
    without a two-sided market is treated as unpriced (a Shoonya quote once
    came back carrying the underlying's spot as an option's ltp with bid=ask=0;
    see docs/CUSTOMIZATIONS.md). Never pass True for the index."""
    data = fetch_symbol_quote(client, symbol, exchange)
    if data is None or data.get("ltp") is None:
        return None
    try:
        ltp = float(data["ltp"])
        if require_two_sided:
            bid = float(data.get("bid") or 0)
            ask = float(data.get("ask") or 0)
            if not (ltp > 0 and bid > 0 and ask > 0):
                Log.warning(f"quote for {symbol} lacks a two-sided market (ltp={ltp}, bid={bid}, ask={ask})")
                return None
    except (TypeError, ValueError):
        return None
    return ltp


def fetch_symbol_bid_ask(client, symbol: str, exchange: str):
    data = fetch_symbol_quote(client, symbol, exchange)
    if data is None:
        return None, None
    bid, ask = data.get("bid"), data.get("ask")
    return (float(bid) if bid else None, float(ask) if ask else None)


def _compact_expiry_date(d) -> str:
    return d.strftime("%d%b%y").upper()


def option_symbol(expiry_date, strike: float, side: str) -> str:
    return f"{UNDERLYING_SYMBOL}{_compact_expiry_date(expiry_date)}{int(strike)}{side}"


_EXPIRY_CACHE: dict = {"day": None, "dates": None}
_EXPIRY_LOCK = threading.Lock()


def fetch_option_expiries(client) -> list:
    """NIFTY option expiry dates at or after today, oldest first -- the same
    resolution Nifty_OI_Positional uses for its weekly hedge (client.expiry,
    instrumenttype="options", cached once a day). Raises RuntimeError rather
    than guessing."""
    today = _now().date()
    with _EXPIRY_LOCK:
        if _EXPIRY_CACHE["day"] == today and _EXPIRY_CACHE["dates"]:
            return list(_EXPIRY_CACHE["dates"])
    try:
        resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    except Exception as exc:
        raise RuntimeError(f"NIFTY options expiry call failed: {exc}") from exc
    if not isinstance(resp, dict) or resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve NIFTY option expiries: {resp}")
    dates = sorted({datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"]})
    dates = [d for d in dates if d >= today]
    if not dates:
        raise RuntimeError("No upcoming NIFTY option expiry in the list.")
    with _EXPIRY_LOCK:
        _EXPIRY_CACHE["day"] = today
        _EXPIRY_CACHE["dates"] = list(dates)
    return list(dates)


def resolve_next_week_expiry(client):
    """The 2nd expiry on or after today (on an expiry day: next week's), only
    if it is at most next_week_max_days away -- the backtest's
    resolve_next_week_expiry. Returns (date, reason)."""
    today = _now().date()
    dates = fetch_option_expiries(client)
    if len(dates) < 2:
        return None, f"only {len(dates)} upcoming expiry listed"
    nxt = dates[1]
    if (nxt - today).days > config.next_week_max_days:
        return None, (f"2nd upcoming expiry {nxt} is {(nxt - today).days} days away "
                      f"(> {config.next_week_max_days}) -- not a next-week contract")
    return nxt, f"upcoming {dates[0]}, {dates[1]} -> next week {nxt}"


# ---- trading calendar (OpenAlgo API client.holidays, as OI_Positional) ----
_CALENDAR: dict = {"client": None, "years": {}, "retry_at": {}}
_CALENDAR_LOCK = threading.Lock()
_CALENDAR_RETRY_SEC = 300.0


def set_calendar_client(client):
    with _CALENDAR_LOCK:
        _CALENDAR["client"] = client


def _calendar_year(year: int) -> tuple:
    """(closed NFO dates, special-session dates) for year. Raises RuntimeError
    when unavailable. An EMPTY list is "not loaded", never "no holidays"."""
    with _CALENDAR_LOCK:
        if year in _CALENDAR["years"]:
            return _CALENDAR["years"][year]
        client = _CALENDAR["client"]
        retry_at = _CALENDAR["retry_at"].get(year)
    if client is None:
        raise RuntimeError("trading calendar not connected")
    if retry_at is not None and time_mod.monotonic() < retry_at:
        raise RuntimeError(f"holiday list for {year} unavailable (retrying shortly)")

    def _fail(why):
        with _CALENDAR_LOCK:
            _CALENDAR["retry_at"][year] = time_mod.monotonic() + _CALENDAR_RETRY_SEC
        raise RuntimeError(why)

    try:
        resp = client.holidays(year=year)
    except Exception as exc:
        _fail(f"holidays({year}) call failed: {exc}")
    if not isinstance(resp, dict) or resp.get("status") != "success":
        _fail(f"holidays({year}) answered {resp}")
    data = resp.get("data") or []
    if not data:
        _fail(f"the OpenAlgo API has no holiday list for {year}")
    closed, special = set(), set()
    for entry in data:
        try:
            day = datetime.strptime(str(entry.get("date"))[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        kind = str(entry.get("holiday_type") or "").upper()
        if kind == "SPECIAL_SESSION":
            special.add(day)
        elif kind == "TRADING_HOLIDAY" and OPTIONS_EXCHANGE in (entry.get("closed_exchanges") or []):
            closed.add(day)
    result = (frozenset(closed), frozenset(special))
    with _CALENDAR_LOCK:
        _CALENDAR["years"][year] = result
        _CALENDAR["retry_at"].pop(year, None)
    return result


def trading_day_status(d) -> tuple:
    """(trade_today: bool, why: str)."""
    try:
        closed, special = _calendar_year(d.year)
    except RuntimeError as exc:
        ok = d.weekday() < 5
        return ok, f"calendar unavailable ({exc}) -- weekday rule: {'trading' if ok else 'weekend'}"
    if d in special:
        return (not config.skip_special_sessions), "special session (Muhurat) -- skipped"
    if d.weekday() >= 5:
        return False, "weekend"
    if d in closed:
        return False, "NFO holiday"
    return True, "trading day"


###############################################################################
# ORDERS
###############################################################################
class OrderNeedsAttention(Exception):
    """poll_fill exhausted its re-prices; the order is still RESTING (not
    cancelled) and needs a human decision."""

    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


def _order_status(client, order_id: str, strategy: str) -> dict:
    resp = client.orderstatus(order_id=order_id, strategy=strategy)
    return resp.get("data", {}) if isinstance(resp, dict) else {}


def _wait_terminal(client, order_id: str, strategy: str, seconds: float) -> dict | None:
    """Polls until complete (returns data), rejected/cancelled (raises), or
    `seconds` of real time pass (returns None). Real monotonic time on
    purpose: a fill wait is about the broker, not the strategy clock."""
    deadline = time_mod.monotonic() + seconds
    while True:
        data = _order_status(client, order_id, strategy)
        status = str(data.get("order_status", "")).lower()
        if status == "complete":
            return data
        if status in {"rejected", "cancelled", "canceled"}:
            raise RuntimeError(f"Order {order_id} ended in status '{status}': {data}")
        if time_mod.monotonic() >= deadline:
            return None
        time_mod.sleep(config.fill_poll_interval)


def _reprice_and_wait_once(client, order_id, strategy, symbol, exchange, action, quantity) -> dict | None:
    """Modify the resting order to the far side of a fresh quote (ask for a
    BUY, bid for a SELL), then wait one fill_poll_timeout."""
    bid, ask = fetch_symbol_bid_ask(client, symbol, exchange)
    fresh = ask if action == "BUY" else bid
    if fresh is None:
        Log.warning(f"Order {order_id}: no fresh bid/ask to re-price -- skipping this attempt.")
        return _wait_terminal(client, order_id, strategy, config.fill_poll_timeout)
    try:
        client.modifyorder(order_id=order_id, strategy=strategy, symbol=symbol, action=action,
                           exchange=exchange, price_type="LIMIT", product=config.product,
                           quantity=str(quantity), price=str(fresh), disclosed_quantity="0",
                           trigger_price="0")
        Log.warning(f"Order {order_id}: re-priced to {fresh}.")
    except Exception as exc:
        Log.warning(f"Order {order_id}: modify (reprice) failed: {exc}.")
    return _wait_terminal(client, order_id, strategy, config.fill_poll_timeout)


def poll_fill(client, order_id, strategy, symbol, exchange, action, quantity) -> dict:
    result = _wait_terminal(client, order_id, strategy, config.fill_poll_timeout)
    if result is not None:
        return result
    for attempt in range(1, config.reprice_max_attempts + 1):
        result = _reprice_and_wait_once(client, order_id, strategy, symbol, exchange, action, quantity)
        if result is not None:
            return result
        Log.warning(f"Order {order_id}: still unfilled after reprice {attempt}/{config.reprice_max_attempts}.")
    raise OrderNeedsAttention(order_id, f"Order {order_id} still unfilled after {config.reprice_max_attempts} "
                                        f"re-prices -- resting at the broker, needs a decision.")


def place(client, strategy, symbol, exchange, action, quantity) -> str:
    """Retries ONLY a clean rejection (nothing was placed). A raised exception
    is re-raised at once: the order may have reached the broker, and there is
    no idempotency key anywhere in this stack, so a retry could duplicate it."""
    last_exc = None
    for attempt in range(1, config.place_order_max_attempts + 1):
        try:
            resp = client.placeorder(strategy=strategy, symbol=symbol, exchange=exchange, action=action,
                                     product=config.product, price_type=config.price_type,
                                     quantity=str(quantity), price="0", trigger_price="0",
                                     disclosed_quantity="0")
        except Exception as exc:
            Log.warning(f"placeorder raised for {symbol} {action} (not retried, outcome unknown): {exc}")
            raise
        if isinstance(resp, dict) and resp.get("status") == "success" and resp.get("orderid"):
            return str(resp["orderid"])
        last_exc = RuntimeError(f"placeorder rejected for {symbol} {action}: {resp}")
        Log.warning(f"placeorder attempt {attempt}/{config.place_order_max_attempts} rejected for "
                    f"{symbol} {action}: {resp}")
        if attempt < config.place_order_max_attempts:
            time_mod.sleep(config.place_order_retry_delay)
    raise last_exc


def _fill_px(fill: dict) -> float:
    return float(fill.get("average_price") or fill.get("price") or 0.0)


###############################################################################
# TRADE LOG (background writer thread)
###############################################################################
_trade_log_queue: "queue.Queue" = queue.Queue()
_trade_log_thread: threading.Thread | None = None
_trade_log_thread_lock = threading.Lock()
_TRADE_LOG_HEADER = ["leg", "symbol", "quantity", "entry_time", "entry_px", "exit_time", "exit_px",
                     "pnl_points", "pnl_rupees", "exit_reason", "execution_id"]


def _trade_log_writer_loop():
    while True:
        item = _trade_log_queue.get()
        try:
            if item is None:
                break
            (strategy_tag, leg, symbol, quantity, entry_time, entry_px, exit_time, exit_px,
             reason, execution_id, is_short) = item
            path = BASE_DIR / f"trades_{strategy_tag}.csv"
            is_new = not path.exists()
            pts = (entry_px - exit_px) if is_short else (exit_px - entry_px)
            with path.open("a", newline="") as fp:
                w = csv.writer(fp)
                if is_new:
                    w.writerow(_TRADE_LOG_HEADER)
                w.writerow([leg, symbol, -quantity if is_short else quantity, entry_time, round(entry_px, 2),
                            exit_time, round(exit_px, 2), round(pts, 2), round(pts * quantity, 2), reason,
                            execution_id])
        except Exception as exc:
            Log.warning(f"Trade log writer failed: {exc}")
        finally:
            _trade_log_queue.task_done()


def _ensure_trade_log_thread():
    global _trade_log_thread
    with _trade_log_thread_lock:
        if _trade_log_thread is None or not _trade_log_thread.is_alive():
            _trade_log_thread = threading.Thread(target=_trade_log_writer_loop, name="trade-log-writer",
                                                 daemon=True)
            _trade_log_thread.start()


def append_trade_log(*row):
    _ensure_trade_log_thread()
    _trade_log_queue.put(row)


###############################################################################
# PLATFORM INTEGRATION (strategy_reporting subprocess, loopback)
###############################################################################
_APIKEY_IN_URL = re.compile(r"(apikey=)[^&#\s]+", re.IGNORECASE)


def _redact_url(url: str) -> str:
    return _APIKEY_IN_URL.sub(r"\1***", url)


def _post_json_local(env, path: str, payload: bytes, timeout: float = 3.0):
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    last_exc = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            req = urllib.request.Request(f"{base}{path}", data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=attempt_timeout).close()
            return
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_post_json_local POST {_redact_url(base + path)} failed: {exc}")
    raise last_exc


def _get_json_local(env, path: str, timeout: float = 3.0) -> dict:
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    last_exc = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=attempt_timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_get_json_local GET {_redact_url(base + path)} failed: {exc}")
    raise last_exc


def report_pnl_to_platform(env, realized_pnl: float, open_positions: list):
    payload = json.dumps({"apikey": env.api_key, "realized_pnl": realized_pnl,
                          "unrealized_pnl": sum(p.get("pnl", 0.0) for p in open_positions),
                          "open_positions": open_positions}).encode("utf-8")
    try:
        _post_json_local(env, f"/python/api/strategy/{env.strategy_tag}/pnl", payload)
    except Exception as exc:
        Log.warning(f"report_pnl_to_platform failed: {exc}")


def push_leg_error(env, leg_key: str, pos: Position, action: str = "", clear: bool = False):
    is_hedge = pos.error_state in ("hedge_entry_failed", "hedge_exit_failed")
    payload = json.dumps({
        "apikey": env.api_key, "leg_key": leg_key, "error_state": pos.error_state,
        "error_kind": pos.error_kind, "error_message": pos.error_message, "error_since": pos.error_since,
        "symbol": pos.hedge_symbol if is_hedge else pos.short_symbol, "quantity": pos.quantity,
        "action": action, "clear": clear,
    }).encode("utf-8")
    try:
        _post_json_local(env, f"/python/api/strategy/{env.strategy_tag}/errors", payload)
    except Exception as exc:
        Log.warning(f"push_leg_error failed for {leg_key}: {exc}")


def check_pending_action(env, leg_key: str) -> dict | None:
    path = f"/python/api/strategy/{env.strategy_tag}/pending_action?leg_key={leg_key}&apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_pending_action failed for {leg_key}: {exc}")
        return None
    return data if data.get("action") else None


def ack_pending_action(env, leg_key: str):
    payload = json.dumps({"apikey": env.api_key, "leg_key": leg_key}).encode("utf-8")
    try:
        _post_json_local(env, f"/python/api/strategy/{env.strategy_tag}/pending_action/ack", payload)
    except Exception as exc:
        Log.warning(f"ack_pending_action failed for {leg_key}: {exc}")


def check_force_exit(env) -> bool:
    try:
        data = _get_json_local(env, f"/python/api/strategy/{env.strategy_tag}/force_exit?apikey={env.api_key}")
    except Exception as exc:
        Log.warning(f"check_force_exit failed: {exc}")
        return False
    return bool(data.get("requested"))


def ack_force_exit_complete(env):
    payload = json.dumps({"apikey": env.api_key}).encode("utf-8")
    try:
        _post_json_local(env, f"/python/api/strategy/{env.strategy_tag}/force_exit/complete", payload)
    except Exception as exc:
        Log.warning(f"ack_force_exit_complete failed: {exc}")


def _error_action(error_state: str) -> str:
    """The order direction the stuck lifecycle needs."""
    return {"hedge_entry_failed": "BUY", "entry_failed": "SELL",
            "exit_failed": "BUY", "hedge_exit_failed": "SELL"}.get(error_state, "")


def side_from_line(close: float, line: float) -> str | None:
    if close > line:
        return "PE"
    if close < line:
        return "CE"
    return None


SIDE_NAME = {"PE": "PUT", "CE": "CALL", None: "none"}


###############################################################################
# STRATEGY ENGINE
###############################################################################
class StrategyEngine:
    def __init__(self, client, store: StateStore, env: Environment, price_stream: PriceStream,
                 execution_id: int, ltp_client=None, chain_client=None):
        self.client = client
        self.ltp_client = ltp_client or client
        self.chain_client = chain_client or client
        self.store = store
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id

        self._state_lock = threading.Lock()
        # THE duplicate-order guard: holds LEG_KEY while any order chain or
        # decision for the slot runs. Checked and set only on the scheduler
        # thread; released in the background task's finally.
        self._busy: set = set()
        # One worker: the slot never runs two chains at once. A second
        # worker only serves the error-resolution sub-chains, which are
        # always submitted while _busy is held by their caller.
        self._fill_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="magic-orders")
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="magic-bg")

        self._force_exit_pending = False
        self._force_exit_check_pending = False
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        self._last_error_push: dict = {}
        self._last_cycle_failure_notify: datetime | None = None
        self._non_trading_logged = ""
        self._entry_cap_alerted = ""
        self._line_alerted = ""
        self._next_line_try: datetime | None = None

        # Completed 1-minute NIFTY closes sampled by the scheduler (WS or the
        # REST fallback): {minute_start: last ltp seen in that minute}. Used
        # when the stream has no tick-level close for the minute.
        self._spot_last: tuple | None = None          # (ts, ltp)
        self._spot_sampled: dict = {}
        self._spot_rest_last: datetime | None = None
        self._reentry_eval_minute: datetime | None = None
        self._sl_rest_last: datetime | None = None
        self._eod_error_logged: datetime | None = None

    # ------------------------------------------------------------------
    @property
    def state(self) -> StrategyState:
        return self.store.state

    @property
    def pos(self) -> Position:
        return self.store.state.position

    def save_state(self):
        with self._state_lock:
            self.store.save()

    def busy(self) -> bool:
        return LEG_KEY in self._busy

    def _dispatch(self, label: str, fn, *args) -> bool:
        """Run fn on the order executor behind the slot guard. Returns False
        (and does nothing) when the slot is already busy."""
        if LEG_KEY in self._busy:
            return False
        self._busy.add(LEG_KEY)

        def _run():
            try:
                fn(*args)
            except Exception as exc:
                Log.exception(f"[{label}] background task failed: {exc}")
                self._notify_telegram_error_bg(f"{label} failed unexpectedly: {exc}")
            finally:
                self._busy.discard(LEG_KEY)

        try:
            self._fill_executor.submit(_run)
        except Exception as exc:
            self._busy.discard(LEG_KEY)
            Log.warning(f"[{label}] could not be dispatched: {exc}")
            return False
        return True

    # ---- platform plumbing (all off the scheduler thread) -------------
    def _push_leg_error_bg(self, pos: Position, action: str = "", clear: bool = False):
        snapshot = copy.copy(pos)
        try:
            self._bg_executor.submit(push_leg_error, self.env, LEG_KEY, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"Failed to dispatch push_leg_error: {exc}")

    def _notify_telegram_error_bg(self, message: str):
        try:
            self._bg_executor.submit(notify_telegram_error, self.env, f"[{config.strategy_name}] {message}",
                                     log_warning=Log.warning)
        except Exception as exc:
            Log.warning(f"Failed to dispatch Telegram notification: {exc}")

    def _notify_trade_closed_bg(self):
        try:
            self._bg_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)
        except Exception as exc:
            Log.warning(f"Failed to dispatch notify_trade_closed: {exc}")

    def _refresh_force_exit_check_bg(self):
        if self._force_exit_check_pending:
            return
        self._force_exit_check_pending = True

        def _run():
            try:
                self._force_exit_pending = check_force_exit(self.env)
            finally:
                self._force_exit_check_pending = False

        try:
            self._bg_executor.submit(_run)
        except Exception:
            self._force_exit_check_pending = False

    def _refresh_pending_action_bg(self):
        if LEG_KEY in self._pending_action_inflight:
            return
        self._pending_action_inflight.add(LEG_KEY)

        def _run():
            try:
                result = check_pending_action(self.env, LEG_KEY)
                if result is not None:
                    self._pending_action_cache[LEG_KEY] = result
            finally:
                self._pending_action_inflight.discard(LEG_KEY)

        try:
            self._bg_executor.submit(_run)
        except Exception:
            self._pending_action_inflight.discard(LEG_KEY)

    def _pop_pending_action(self) -> dict | None:
        return self._pending_action_cache.pop(LEG_KEY, None)

    def _repush_active_errors(self):
        pos = self.pos
        if not pos.error_state:
            self._last_error_push.pop(LEG_KEY, None)
            return
        now = _now()
        last = self._last_error_push.get(LEG_KEY)
        if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
            return
        self._last_error_push[LEG_KEY] = now
        self._push_leg_error_bg(pos, action=_error_action(pos.error_state))

    # ---- prices ---------------------------------------------------------
    def spot_ltp(self) -> float | None:
        ltp = self.price_stream.get_ltp(UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        return fetch_symbol_ltp(self.ltp_client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE)

    def _sample_spot(self):
        """Feeds the completed-minute NIFTY close tracker. WS first; the REST
        fallback is throttled so a dead feed costs one quote per few seconds,
        not one per tick."""
        now = _now()
        ltp = self.price_stream.get_ltp(UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, config.ws_stale_seconds)
        if ltp is None:
            if (self._spot_rest_last is None
                    or (now - self._spot_rest_last).total_seconds() >= config.spot_rest_fallback_gap_sec):
                self._spot_rest_last = now
                ltp = fetch_symbol_ltp(self.ltp_client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE)
        if ltp is None:
            return
        minute = now.replace(second=0, microsecond=0)
        self._spot_last = (now, ltp)
        self._spot_sampled[minute] = ltp
        if len(self._spot_sampled) > 600:
            for k in sorted(self._spot_sampled)[:-400]:
                self._spot_sampled.pop(k, None)

    def _minute_close(self, minute: datetime) -> tuple:
        """(close, source) of the NIFTY minute starting at `minute`, only once
        it has ended: the last live tick of that minute, else the scheduler's
        last sample in it. (None, "") when neither saw that minute."""
        if _now().replace(second=0, microsecond=0) <= minute:
            return None, ""
        getter = getattr(self.price_stream, "spot_minute_close", None)
        px = getter(minute) if getter else None
        if px is not None:
            return px, "live ticks"
        px = self._spot_sampled.get(minute)
        if px is not None:
            return px, "sampled price"
        return None, ""

    def _short_ltp(self, pos: Position) -> float | None:
        ltp = self.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        now = _now()
        if self._sl_rest_last is not None and (now - self._sl_rest_last).total_seconds() < 3.0:
            return None
        self._sl_rest_last = now
        return fetch_symbol_ltp(self.ltp_client, pos.short_symbol, OPTIONS_EXCHANGE, require_two_sided=True)

    # ---- day ------------------------------------------------------------
    def _reset_day_if_needed(self):
        today = _now().date().isoformat()
        if self.state.current_day == today:
            return
        Log.info(f"New day {today}: resetting the line, checks, re-entry and day counters.")
        for k, v in _day_fields().items():
            setattr(self.state, k, v)
        self.state.current_day = today
        self._reentry_eval_minute = None
        self._spot_sampled = {}
        self._spot_last = None
        if self.pos.hedge_symbol or self.pos.short_symbol:
            msg = (f"a position from {self.pos.entry_time[:10] or 'an earlier day'} is still recorded "
                   f"({self.pos.short_symbol} / {self.pos.hedge_symbol}) -- it was not closed at 15:15. "
                   f"It is managed today (SL, checks, 15:15 close); verify it at the broker.")
            Log.error(msg)
            self._notify_telegram_error_bg(msg)
        self.save_state()

    # ---- the line ------------------------------------------------------
    def _history(self, interval: str):
        today = _now().date().isoformat()
        try:
            df = self.client.history(symbol=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                                     interval=interval, start_date=today, end_date=today)
        except Exception as exc:
            Log.warning(f"history({interval}) raised: {exc}")
            return None
        if _is_error_response(df) or df is None or len(df) == 0:
            Log.warning(f"history({interval}) unavailable: {df if _is_error_response(df) else 'empty'}")
            return None
        return df

    @staticmethod
    def _bar_at(df, hhmm: str):
        """Row whose candle starts at hh:mm today, or None."""
        today = _now().date()
        for ts, row in df.iterrows():
            t = ts.tz_convert(IST) if getattr(ts, "tzinfo", None) is not None else IST.localize(ts)
            if t.date() == today and t.strftime("%H:%M") == hhmm:
                return row
        return None

    def _line_from_1m(self):
        """(open, close) of the 09:15-09:30 candle built from today's 1-minute
        bars: the 09:15 bar's open and the 09:29 bar's close. Never the 09:14
        pre-open bar Shoonya dates today (docs/CUSTOMIZATIONS.md, 2026-11-05)."""
        df = self._history("1m")
        if df is None:
            return None
        o = self._bar_at(df, "09:15")
        c = self._bar_at(df, "09:29")
        if o is None or c is None:
            return None
        return float(o["open"]), float(c["close"])

    def _read_line(self):
        """The 09:15 15-minute candle (open, close), confirmed against the same
        candle built from its 1-minute bars. A 15m candle read while the broker
        is still folding in the last minute would carry the 09:28 close, so a
        disagreement is re-read until line_wait_sec; if it persists the 1m
        values are used (they ARE the candle) and an alert is sent. No 15m
        candle at all -> the 1m values. Runs on the executor."""
        start = _now()
        hhmm = config.line_candle_start.strftime("%H:%M")
        mismatch = unconfirmed = None
        while True:
            df = self._history(config.line_candle_interval)
            row = self._bar_at(df, hhmm) if df is not None else None
            one = self._line_from_1m()
            if row is not None:
                o15, c15 = float(row["open"]), float(row["close"])
                if one is None:
                    unconfirmed = (o15, c15)
                elif abs(o15 - one[0]) <= 0.05 and abs(c15 - one[1]) <= 0.05:
                    return o15, c15, "15m candle 09:15, matches 1m 09:15 open / 09:29 close"
                elif mismatch != (o15, c15, one):
                    mismatch = (o15, c15, one)
                    Log.warning(f"[LINE] 15m candle O={o15:.2f} C={c15:.2f} differs from its 1m bars "
                                f"O={one[0]:.2f} C={one[1]:.2f} -- re-reading")
            if (_now() - start).total_seconds() >= config.line_wait_sec:
                break
            _sleep(config.bar_retry_gap_sec)
        one = self._line_from_1m()
        if one is None:
            if unconfirmed is not None:
                Log.warning("[LINE] the 1-minute bars never arrived to confirm the 15m candle -- using it as read.")
                return unconfirmed[0], unconfirmed[1], "15m candle 09:15 (1m bars not available to confirm)"
            return None
        if mismatch is not None:
            msg = (f"the 09:15 15m candle (O={mismatch[0]:.2f} C={mismatch[1]:.2f}) never matched its own 1-minute "
                   f"bars (O={one[0]:.2f} C={one[1]:.2f}); the line uses the 1-minute values.")
            Log.error(f"[LINE] {msg}")
            self._notify_telegram_error_bg(msg)
            return one[0], one[1], "1m candles 09:15 open / 09:29 close (15m candle disagreed)"
        return one[0], one[1], "1m candles 09:15 open / 09:29 close (15m not published)"

    def _compute_line(self):
        got = self._read_line()
        if got is None:
            self._next_line_try = _now() + timedelta(seconds=30)
            today = _now().date().isoformat()
            if self._line_alerted != today:
                self._line_alerted = today
                msg = ("could not read the 09:15-09:30 candle from the broker -- no line yet, so no "
                       "entry or check can run. Retrying every 30s.")
                Log.error(msg)
                self._notify_telegram_error_bg(msg)
            return
        c_open, line, source = got
        st = self.state
        st.candle_open, st.line, st.line_source, st.line_ready = c_open, line, source, True
        colour = "bullish" if line > c_open else "bearish" if line < c_open else "doji"
        self.save_state()
        Log.info(f"[LINE] 09:15-09:30 candle open={c_open:.2f} close={line:.2f} ({colour}) from {source} "
                 f"-> Magical Line = {line:.2f}")

    # ---- entry chain ------------------------------------------------------
    def _select_strike(self, side: str, expiry, spot: float):
        """(strike, delta, premium, lot_size) or (None, why). Round-100 OTM
        strikes, |delta| closest to delta_target inside the HARD band, delta
        from the server's own greeks (optionchain with_greeks -- the forward
        it uses is the platform's synthetic forward, see OI_Positional)."""
        atm = round(spot / config.strike_round) * config.strike_round
        direction = 1 if side == "CE" else -1
        key = side.lower()
        scanned = []
        lot_size = None
        for band in range(1, config.max_bands + 1):
            count = config.band_step_count * band * 2   # strikes each side, 50-pt chain grid
            resp = self.chain_client.optionchain(underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                                                 expiry_date=_compact_expiry_date(expiry), strike_count=count,
                                                 with_quotes=True, with_greeks=True)
            if not isinstance(resp, dict) or resp.get("status") != "success" or not resp.get("chain"):
                return None, f"optionchain failed: {resp}"
            by_strike = {}
            for row in resp["chain"]:
                leg = row.get(key)
                if not leg:
                    continue
                by_strike[float(row["strike"])] = leg
                if lot_size is None and leg.get("lotsize"):
                    lot_size = int(leg["lotsize"])
            scanned = []
            for step in range(1, config.band_step_count * band + 1):
                strike = atm + direction * step * config.strike_round
                leg = by_strike.get(float(strike))
                if not leg or not leg.get("ltp") or not leg.get("bid") or not leg.get("ask"):
                    continue
                d = leg.get("delta")
                if d is None or float(d) == 0.0:
                    continue
                scanned.append((float(strike), abs(float(d)), float(leg["ltp"])))
            band_ok = [c for c in scanned if config.delta_low <= c[1] <= config.delta_high]
            if band_ok:
                best = min(band_ok, key=lambda c: abs(c[1] - config.delta_target))
                near = ", ".join(f"{s:.0f}:{d:.3f}" for s, d, _ in scanned[:8])
                Log.info(f"[STRIKE] {SIDE_NAME[side]} spot={spot:.2f} atm={atm:.0f} expiry={expiry} | "
                         f"scanned {near} -> {best[0]:.0f} delta={best[1]:.4f} ltp={best[2]:.2f}")
                return (best[0], best[1], best[2], lot_size or config.default_lot_size), ""
        near = ", ".join(f"{s:.0f}:{d:.3f}" for s, d, _ in scanned[:8]) or "none priced"
        return None, (f"no {SIDE_NAME[side]} strike with delta {config.delta_low}-{config.delta_high} "
                      f"(spot {spot:.2f}; scanned {near})")

    def _select_hedge(self, side: str, expiry, short_strike: float):
        """(strike, symbol, ltp) 1000 pts further OTM, else +50 ... +300; a
        two-sided quote is required. None when nothing in range is priced."""
        direction = 1 if side == "CE" else -1
        tried = []
        for extra in range(0, config.hedge_extra_max_pts + 1, config.hedge_extra_step_pts):
            strike = short_strike + direction * (config.hedge_distance_pts + extra)
            sym = option_symbol(expiry, strike, side)
            ltp = fetch_symbol_ltp(self.ltp_client, sym, OPTIONS_EXCHANGE, require_two_sided=True)
            tried.append(f"{strike:.0f}")
            if ltp is not None and ltp > 0:
                return strike, sym, ltp
        Log.warning(f"[HEDGE] no two-sided quote at {', '.join(tried)} {side}")
        return None

    def _clear_retry(self, why: str = ""):
        st = self.state
        if st.retry_side:
            if why:
                Log.info(f"[RETRY] pending {st.retry_leg_type} {SIDE_NAME[st.retry_side]} retry dropped: {why}")
            st.retry_side, st.retry_leg_type, st.retry_due = "", "", ""
            self.save_state()

    def _not_found(self, side: str, leg_type: str, is_retry: bool, what: str):
        """No short strike / no hedge: schedule the single retry, or say why not."""
        st = self.state
        if is_retry:
            Log.info(f"[RETRY] {leg_type} {SIDE_NAME[side]} retry also found {what} -- no further "
                     f"retry; the next check decides.")
            return
        if leg_type not in config.not_found_retry_types:
            return
        due = _now() + timedelta(minutes=config.not_found_retry_min)
        if due.time() >= config.square_off_time:
            Log.info(f"[RETRY] {leg_type}: a retry at {due:%H:%M} would be at/after "
                     f"{config.square_off_time:%H:%M} -- none.")
            return
        st.retry_side, st.retry_leg_type, st.retry_due = side, leg_type, due.isoformat()
        self.save_state()
        Log.info(f"[RETRY] {leg_type} {SIDE_NAME[side]}: {what} -- one retry at {due:%H:%M:%S}.")

    def _enter(self, side: str, leg_type: str, is_retry: bool = False) -> bool:
        """Build a new position: hedge BUY, then short SELL. True only when
        both legs are filled. Runs on the executor under the slot guard."""
        st = self.state
        if self.pos.hedge_symbol or self.pos.short_symbol:
            Log.warning(f"[{leg_type}] entry refused: the slot already holds {self.pos.short_symbol or self.pos.hedge_symbol}")
            return False
        if st.day_done or _now().time() >= config.square_off_time:
            Log.info(f"[{leg_type}] entry skipped: past {config.square_off_time:%H:%M} / day closed")
            return False
        if st.entries_today >= config.max_entries_per_day:
            today = _now().date().isoformat()
            if self._entry_cap_alerted != today:
                self._entry_cap_alerted = today
                msg = (f"{st.entries_today} entries today reached the safety limit "
                       f"{config.max_entries_per_day} -- no more entries today. Check the log.")
                Log.error(msg)
                self._notify_telegram_error_bg(msg)
            return False

        spot = self.spot_ltp()
        if spot is None:
            Log.warning(f"[{leg_type}] NIFTY price unavailable -- no entry this time.")
            return False
        try:
            expiry, why = resolve_next_week_expiry(self.client)
        except RuntimeError as exc:
            Log.warning(f"[{leg_type}] expiry list unavailable: {exc} -- no entry this time.")
            return False
        if expiry is None:
            Log.warning(f"[{leg_type}] no next-week expiry: {why} -- no entry.")
            return False
        try:
            picked, why = self._select_strike(side, expiry, spot)
        except Exception as exc:
            Log.warning(f"[{leg_type}] strike selection failed: {exc}")
            return False
        if picked is None:
            Log.info(f"[{leg_type}] {why} -- no entry.")
            self._not_found(side, leg_type, is_retry, "no short strike in the delta band")
            return False
        strike, delta, premium, lot_size = picked
        hedge = self._select_hedge(side, expiry, strike)
        if hedge is None:
            Log.warning(f"[{leg_type}] {SIDE_NAME[side]} {strike:.0f}: no hedge priced "
                        f"{config.hedge_distance_pts}-{config.hedge_distance_pts + config.hedge_extra_max_pts} "
                        f"pts out -- not selling naked, no entry.")
            self._not_found(side, leg_type, is_retry, "no hedge priced")
            return False
        h_strike, h_symbol, h_ltp = hedge
        qty = config.lots * lot_size
        short_symbol = option_symbol(expiry, strike, side)

        # Persist the attempt BEFORE the first order: a crash inside
        # placeorder() is then visible at restart (reconcile flags it).
        self.store.state.position = Position(
            side=side, leg_type=leg_type, expiry=expiry.isoformat(), quantity=qty,
            short_symbol=short_symbol, short_strike=strike, delta_at_entry=delta, entry_spot=spot,
            entry_time=_now().isoformat(), hedge_symbol=h_symbol, hedge_strike=h_strike,
            execution_id=self.execution_id)
        st.entries_today += 1
        self.save_state()
        Log.info(f"[{leg_type}] ENTER {SIDE_NAME[side]}: line={st.line:.2f} NIFTY={spot:.2f} | {why} | "
                 f"short {short_symbol} delta={delta:.4f} ltp={premium:.2f} | hedge {h_symbol} ltp={h_ltp:.2f} "
                 f"| qty={qty} ({config.lots} lot x {lot_size}) | entry #{st.entries_today} today")
        self.price_stream.add_instruments([{"symbol": h_symbol, "exchange": OPTIONS_EXCHANGE},
                                           {"symbol": short_symbol, "exchange": OPTIONS_EXCHANGE}])
        return self._continue_entry()

    def _continue_entry(self) -> bool:
        """Resumable entry chain from whatever the position already records:
        hedge order -> hedge fill -> short order -> short fill."""
        pos = self.pos
        tag = self.env.strategy_tag
        if not pos.hedge_entry_filled:
            if not pos.hedge_entry_order_id:
                try:
                    pos.hedge_entry_order_id = place(self.client, tag, pos.hedge_symbol, OPTIONS_EXCHANGE,
                                                     "BUY", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[ENTRY] hedge BUY placeorder failed for {pos.hedge_symbol}: {exc}")
                    self._enter_error_mode("hedge_entry_failed", "terminal", "", str(exc))
                    return False
                self.save_state()
                Log.info(f"[ENTRY] hedge BUY {pos.hedge_symbol} qty={pos.quantity} order={pos.hedge_entry_order_id}")
            fill = self._await_fill(pos.hedge_entry_order_id, pos.hedge_symbol, "BUY", "hedge_entry_failed")
            if fill is None:
                return False
            pos.hedge_entry_px = _fill_px(fill)
            pos.hedge_entry_filled = True
            pos.hedge_entry_time = _now().isoformat()
            self.save_state()
            Log.info(f"[ENTRY] hedge filled {pos.hedge_symbol} @ {pos.hedge_entry_px:.2f} -- selling the short now.")
        if not pos.entry_filled:
            if not pos.entry_order_id:
                try:
                    pos.entry_order_id = place(self.client, tag, pos.short_symbol, OPTIONS_EXCHANGE,
                                               "SELL", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[ENTRY] short SELL placeorder failed for {pos.short_symbol}: {exc}")
                    self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                    return False
                self.save_state()
                Log.info(f"[ENTRY] short SELL {pos.short_symbol} qty={pos.quantity} order={pos.entry_order_id}")
            fill = self._await_fill(pos.entry_order_id, pos.short_symbol, "SELL", "entry_failed")
            if fill is None:
                return False
            self._record_short_fill(_fill_px(fill))
        return True

    def _record_short_fill(self, px: float):
        pos = self.pos
        pos.entry_px = px
        pos.entry_filled = True
        pos.sl_px = round(px * (1 + config.sl_pct / 100.0), 2)
        st = self.state
        if st.retry_side:
            st.retry_side, st.retry_leg_type, st.retry_due = "", "", ""
        if pos.leg_type == "REENTRY" or st.reentry_side:
            # A filled position ends any wait for re-entry.
            st.reentry_side, st.reentry_p, st.reentry_armed, st.reentry_sl_time = "", 0.0, False, ""
        self.save_state()
        Log.info(f"[ENTRY] OPEN {pos.leg_type} {SIDE_NAME[pos.side]} short {pos.short_symbol} @ {px:.2f} "
                 f"SL={pos.sl_px:.2f} | hedge {pos.hedge_symbol} @ {pos.hedge_entry_px:.2f} | "
                 f"NIFTY at decision={pos.entry_spot:.2f} (re-entry level if stopped)")

    def _await_fill(self, order_id: str, symbol: str, action: str, error_state: str) -> dict | None:
        try:
            return poll_fill(self.client, order_id, self.env.strategy_tag, symbol, OPTIONS_EXCHANGE,
                             action, self.pos.quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(error_state, "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(error_state, "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"fill watch failed for {symbol}: {exc}")
            self._enter_error_mode(error_state, "resting", order_id, str(exc))
        return None

    # ---- exit chain -------------------------------------------------------
    def _exit(self, reason: str) -> bool:
        """Resumable close: short BUY, then hedge SELL, then finalize. True
        once the slot is flat."""
        pos = self.pos
        tag = self.env.strategy_tag
        if not pos.pending_exit_reason:
            pos.pending_exit_reason = reason
            self.save_state()
            Log.info(f"[EXIT] {reason}: closing {pos.short_symbol} then {pos.hedge_symbol}")
        reason = pos.pending_exit_reason
        if not pos.short_exit_filled:
            if not pos.entry_filled:
                if pos.entry_order_id:
                    Log.warning(f"[EXIT] short order {pos.entry_order_id} never confirmed filled -- nothing "
                                f"recorded to buy back; verify it at the broker.")
                pos.short_exit_filled = True
                pos.short_exit_fill_px = None
                self.save_state()
            else:
                if not pos.short_exit_order_id:
                    try:
                        pos.short_exit_order_id = place(self.client, tag, pos.short_symbol, OPTIONS_EXCHANGE,
                                                        "BUY", pos.quantity)
                    except Exception as exc:
                        Log.exception(f"[EXIT] short BUY placeorder failed for {pos.short_symbol}: {exc}")
                        self._enter_error_mode("exit_failed", "terminal", "", str(exc))
                        return False
                    self.save_state()
                fill = self._await_fill(pos.short_exit_order_id, pos.short_symbol, "BUY", "exit_failed")
                if fill is None:
                    return False
                pos.short_exit_fill_px = _fill_px(fill)
                pos.short_exit_filled = True
                pos.short_exit_time = _now().isoformat()
                self.save_state()
                Log.info(f"[EXIT] short bought back {pos.short_symbol} @ {pos.short_exit_fill_px:.2f}")
        if not pos.hedge_exit_filled:
            if not pos.hedge_entry_filled:
                pos.hedge_exit_filled = True
                pos.hedge_exit_fill_px = None
                self.save_state()
            else:
                if not pos.hedge_exit_order_id:
                    try:
                        pos.hedge_exit_order_id = place(self.client, tag, pos.hedge_symbol, OPTIONS_EXCHANGE,
                                                        "SELL", pos.quantity)
                    except Exception as exc:
                        Log.exception(f"[EXIT] hedge SELL placeorder failed for {pos.hedge_symbol}: {exc}")
                        self._enter_error_mode("hedge_exit_failed", "terminal", "", str(exc))
                        return False
                    self.save_state()
                fill = self._await_fill(pos.hedge_exit_order_id, pos.hedge_symbol, "SELL", "hedge_exit_failed")
                if fill is None:
                    return False
                pos.hedge_exit_fill_px = _fill_px(fill)
                pos.hedge_exit_filled = True
                pos.hedge_exit_time = _now().isoformat()
                self.save_state()
                Log.info(f"[EXIT] hedge sold {pos.hedge_symbol} @ {pos.hedge_exit_fill_px:.2f}")
        self._finalize(reason)
        return True

    def _finalize(self, reason: str):
        pos = self.pos
        st = self.state
        now_iso = _now().isoformat()
        short_pnl = hedge_pnl = 0.0
        if pos.entry_filled and pos.short_exit_fill_px is not None:
            short_pnl = (pos.entry_px - pos.short_exit_fill_px) * pos.quantity
            append_trade_log(self.env.strategy_tag, f"{LEG_KEY}_{pos.side}", pos.short_symbol, pos.quantity,
                             pos.entry_time, pos.entry_px, pos.short_exit_time or now_iso, pos.short_exit_fill_px,
                             f"{reason}|{pos.leg_type}", pos.execution_id, True)
        if pos.hedge_entry_filled and pos.hedge_exit_fill_px is not None:
            hedge_pnl = (pos.hedge_exit_fill_px - pos.hedge_entry_px) * pos.quantity
            append_trade_log(self.env.strategy_tag, f"{LEG_KEY}_{pos.side}_HEDGE", pos.hedge_symbol,
                             pos.quantity, pos.hedge_entry_time or pos.entry_time, pos.hedge_entry_px,
                             pos.hedge_exit_time or now_iso,
                             pos.hedge_exit_fill_px, f"{reason}|{pos.leg_type}", pos.execution_id, False)
        st.today_realized_pnl += short_pnl + hedge_pnl
        Log.info(f"[CLOSED] {reason} {pos.leg_type} {SIDE_NAME[pos.side]} short {pos.short_symbol} "
                 f"{pos.entry_px:.2f}->{pos.short_exit_fill_px} ({short_pnl:+.2f}) | hedge {pos.hedge_symbol} "
                 f"{pos.hedge_entry_px:.2f}->{pos.hedge_exit_fill_px} ({hedge_pnl:+.2f}) | trade "
                 f"{short_pnl + hedge_pnl:+.2f} | day realized {st.today_realized_pnl:+.2f}")
        if reason == "SL" and pos.entry_filled:
            st.reentry_side = pos.side
            st.reentry_p = pos.entry_spot
            st.reentry_armed = False
            st.reentry_sl_time = now_iso
            Log.info(f"[REENTRY] stopped {SIDE_NAME[pos.side]}: nothing trades until the next check; "
                     f"re-entry level P={pos.entry_spot:.2f}")
        self.price_stream.remove_instruments([{"symbol": s, "exchange": OPTIONS_EXCHANGE}
                                              for s in (pos.short_symbol, pos.hedge_symbol) if s])
        st.position = Position()
        self.save_state()
        self._notify_trade_closed_bg()

    # ---- decisions (all run on the executor) ---------------------------
    def _do_initial(self):
        st = self.state
        # Marked BEFORE the entry: a restart part-way through must resume the
        # recorded position, never attempt the 09:31 entry a second time.
        st.initial_done = True
        self.save_state()
        try:
            colour_side = side_from_line(st.line, st.candle_open)   # close above open -> PUT
            if colour_side is None:
                Log.info(f"[INITIAL] doji (open={st.candle_open:.2f} close={st.line:.2f}) -- no 09:31 "
                         f"entry; the 10:31 check picks the side.")
                return
            self._enter(colour_side, "INITIAL")
        finally:
            st.initial_done = True
            self.save_state()

    def _hourly_close(self, slot: time):
        """The 09:30-based hourly candle ending at `slot`, from that hour's
        1-minute candles: open of hh-1:30, high/low over the hour, close of
        the hh:29 bar. Waits up to check_bar_wait_sec for the hh:29 bar
        (watching the SL meanwhile); then the hh:29 minute close from the
        live feed. Never a later price: a check decided on anything but the
        candle close is skipped instead (None)."""
        start_t = _plus_min(slot, -60)
        last_t = _plus_min(slot, -1)
        began = _now()
        while True:
            df = self._history("1m")
            if df is not None:
                close_row = self._bar_at(df, last_t.strftime("%H:%M"))
                if close_row is not None:
                    lo, hi = _at(start_t), _at(slot)
                    idx = [ts.tz_convert(IST) if ts.tzinfo else IST.localize(ts) for ts in df.index]
                    hour = df[[lo <= t < hi for t in idx]]
                    o = float(hour["open"].iloc[0]) if len(hour) else float("nan")
                    h = float(hour["high"].max()) if len(hour) else float("nan")
                    lw = float(hour["low"].min()) if len(hour) else float("nan")
                    return float(close_row["close"]), (f"hourly {start_t:%H:%M}-{slot:%H:%M} from {len(hour)} "
                                                       f"1m bars O={o:.2f} H={h:.2f} L={lw:.2f}")
            if (_now() - began).total_seconds() >= config.check_bar_wait_sec:
                break
            self._sl_while_waiting()
            _sleep(config.bar_retry_gap_sec)
        px, how = self._minute_close(_at(last_t))
        if px is not None:
            return px, f"live-feed close of the {last_t:%H:%M} minute from {how} (1m bar not published)"
        return None, f"neither the {last_t:%H:%M} 1m bar nor the live feed's {last_t:%H:%M} close is available"

    def _sl_while_waiting(self):
        """The scheduler skips the SL while this slot is busy, so a task that
        waits on the broker (a late candle) checks it itself. Already on the
        executor under the slot guard, so the exit runs inline."""
        pos = self.pos
        if not (pos.entry_filled and pos.hedge_entry_filled) or pos.pending_exit_reason or pos.error_state:
            return
        ltp = self._short_ltp(pos)
        if ltp is not None and ltp >= pos.sl_px > 0:
            self._do_sl_exit(ltp)

    def _do_check(self, slot: time):
        st = self.state
        self._clear_retry(f"the {slot:%H:%M} check decides afresh")
        try:
            close, source = self._hourly_close(slot)
            if self.pos.error_state:
                Log.error(f"[CHECK {slot:%H:%M}] an SL exit during the wait for the candle needs attention "
                          f"({self.pos.error_state}) -- check not acted on.")
                return
            if close is None:
                Log.error(f"[CHECK {slot:%H:%M}] {source} -- check skipped; the position (if any) is kept "
                          f"with its SL.")
                self._notify_telegram_error_bg(f"{slot:%H:%M} check skipped: the broker did not publish the "
                                               f"hourly candle close and the live feed did not see it.")
                return
            want = side_from_line(close, st.line)
            rel = {"PE": "above", "CE": "below", None: "on"}[want]
            pos = self.pos
            holding = SIDE_NAME[pos.side] if pos.entry_filled else "flat"
            waiting = (f" | waiting re-entry {SIDE_NAME[st.reentry_side]} P={st.reentry_p:.2f}"
                       if st.reentry_side else "")
            Log.info(f"[CHECK {slot:%H:%M}] close={close:.2f} ({source}) {rel} line {st.line:.2f} -> "
                     f"wants {SIDE_NAME[want]} | holding {holding}{waiting}")
            if _now().time() >= config.square_off_time:
                return
            if pos.entry_filled and pos.hedge_entry_filled:
                if want is not None and want != pos.side:
                    Log.info(f"[CHECK {slot:%H:%M}] SWITCH {SIDE_NAME[pos.side]} -> {SIDE_NAME[want]}")
                    if self._exit("SWITCH"):
                        self._enter(want, "SWITCH")
                return
            if st.reentry_side:
                if want is not None and want != st.reentry_side:
                    Log.info(f"[CHECK {slot:%H:%M}] after the SL the line says the other side -> "
                             f"sell {SIDE_NAME[want]} now")
                    st.reentry_side, st.reentry_p, st.reentry_armed, st.reentry_sl_time = "", 0.0, False, ""
                    self.save_state()
                    self._enter(want, "SWITCH")
                elif want == st.reentry_side:
                    if not st.reentry_armed:
                        st.reentry_armed = True
                        self.save_state()
                        Log.info(f"[CHECK {slot:%H:%M}] same side still valid -> waiting for NIFTY to reach "
                                 f"P={st.reentry_p:.2f} ({'>=' if want == 'PE' else '<='})")
                return
            if want is not None:
                self._enter(want, "CHECK")
        finally:
            st.last_check_slot = slot.strftime("%H:%M")
            self.save_state()

    def _do_retry(self):
        st = self.state
        side, leg_type = st.retry_side, st.retry_leg_type
        st.retry_side, st.retry_leg_type, st.retry_due = "", "", ""
        self.save_state()                # once: cleared before the attempt, survives a restart
        if not side:
            return
        Log.info(f"[RETRY] {leg_type} {SIDE_NAME[side]}: trying once more")
        self._enter(side, leg_type, is_retry=True)

    def _do_reentry(self):
        st = self.state
        side = st.reentry_side
        if not side or not st.reentry_armed:
            return
        self._enter(side, "REENTRY")   # success clears the wait; failure keeps it for the next minute

    def _do_sl_exit(self, ltp: float):
        pos = self.pos
        Log.info(f"[SL] {pos.short_symbol} ltp={ltp:.2f} >= SL {pos.sl_px:.2f} (entry {pos.entry_px:.2f} "
                 f"x {1 + config.sl_pct / 100:.2f}) -> closing short then hedge")
        self._exit("SL")

    # ---- error recovery (Retry / Cancel / Manually Completed) ----------
    def _enter_error_mode(self, error_state: str, error_kind: str, error_order_id: str, message: str):
        pos = self.pos
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = _now().isoformat()
        self.save_state()
        Log.error(f"[{LEG_KEY}] {error_state} ({error_kind}): {message}")
        self._push_leg_error_bg(pos, action=_error_action(error_state))
        self._last_error_push[LEG_KEY] = _now()
        self._notify_telegram_error_bg(f"{error_state} ({error_kind}) on {pos.hedge_symbol if 'hedge' in error_state else pos.short_symbol}: {message}")

    def _clear_error(self):
        pos = self.pos
        pos.error_state = pos.error_kind = pos.error_order_id = pos.error_message = pos.error_since = ""
        self.save_state()
        self._push_leg_error_bg(pos, clear=True)

    def _resolve_leg_error(self, action: dict):
        """Called on the scheduler thread only when the slot is not busy.
        Every branch ends in the resumable chains, so a resolution continues
        the position exactly where it stopped."""
        pos = self.pos
        es, kind = pos.error_state, pos.error_kind
        is_exit = es in ("exit_failed", "hedge_exit_failed")
        is_hedge = es in ("hedge_entry_failed", "hedge_exit_failed")
        symbol = pos.hedge_symbol if is_hedge else pos.short_symbol
        order_action = _error_action(es)
        what = action.get("action")
        Log.warning(f"[{LEG_KEY}] resolving {es} ({kind}) with '{what}'")

        if what == "retry":
            ack_pending_action(self.env, LEG_KEY)
            self._dispatch("retry", self._retry, es, kind, symbol, order_action)
            return

        if what == "manual":
            ack_pending_action(self.env, LEG_KEY)
            px = float(action.get("fill_price") or 0.0)
            if es == "hedge_entry_failed":
                pos.hedge_entry_filled, pos.hedge_entry_px = True, px
                pos.hedge_entry_time = pos.hedge_entry_time or _now().isoformat()
            elif es == "entry_failed":
                self._record_short_fill(px)
            elif es == "exit_failed":
                pos.short_exit_filled, pos.short_exit_fill_px = True, px
                pos.short_exit_time = _now().isoformat()
            else:
                pos.hedge_exit_filled, pos.hedge_exit_fill_px = True, px
                pos.hedge_exit_time = _now().isoformat()
            self._clear_error()
            self._dispatch("manual-continue", self._continue_after_resolution)
            return

        if what == "cancel":
            ack_pending_action(self.env, LEG_KEY)
            if is_exit:
                # Drop this attempt only. The position stays recorded and the
                # strategy's own triggers (SL, check, 15:15) close it again;
                # a half-closed position resumes its exit on the next tick.
                if is_hedge:
                    pos.hedge_exit_order_id = ""
                else:
                    pos.short_exit_order_id = ""
                    pos.pending_exit_reason = ""
                self._clear_error()
                return
            if kind == "resting":
                self._dispatch("cancel-final-chance", self._cancel_final_chance, es, symbol, order_action)
                return
            self._abandon_entry("cancelled by the operator")
            self._clear_error()

    def _retry(self, es, kind, symbol, order_action):
        pos = self.pos
        if kind == "resting" and pos.error_order_id:
            bid, ask = fetch_symbol_bid_ask(self.ltp_client, symbol, OPTIONS_EXCHANGE)
            fresh = ask if order_action == "BUY" else bid
            if fresh is not None:
                try:
                    self.client.modifyorder(order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                            symbol=symbol, action=order_action, exchange=OPTIONS_EXCHANGE,
                                            price_type="LIMIT", product=config.product,
                                            quantity=str(pos.quantity), price=str(fresh),
                                            disclosed_quantity="0", trigger_price="0")
                except Exception as exc:
                    Log.warning(f"Retry's reprice failed ({exc}) -- watching the order as it is.")
        else:
            # terminal: the order is dead (or was never placed) -- clear its
            # id so the chain places a fresh one.
            if es == "hedge_entry_failed":
                pos.hedge_entry_order_id = ""
            elif es == "entry_failed":
                pos.entry_order_id = ""
            elif es == "exit_failed":
                pos.short_exit_order_id = ""
            else:
                pos.hedge_exit_order_id = ""
        self._clear_error()
        self._continue_after_resolution()

    def _continue_after_resolution(self):
        pos = self.pos
        if pos.pending_exit_reason:
            self._exit(pos.pending_exit_reason)
        elif pos.hedge_symbol and not (pos.hedge_entry_filled and pos.entry_filled):
            if _now().time() >= config.square_off_time or self.state.day_done:
                # Too late to complete a new position: close what is held.
                self._exit("EOD" if not self.state.day_done else "FORCE_EXIT")
            else:
                self._continue_entry()

    def _cancel_final_chance(self, es, symbol, order_action):
        """One last re-price of a resting ENTRY order, then cancelorder()."""
        pos = self.pos
        order_id = pos.error_order_id
        try:
            fill = _reprice_and_wait_once(self.client, order_id, self.env.strategy_tag, symbol,
                                          OPTIONS_EXCHANGE, order_action, pos.quantity)
        except Exception as exc:
            Log.warning(f"final-chance wait on {order_id} ended: {exc}")
            fill = None
        if fill is not None:
            Log.info(f"[{LEG_KEY}] {symbol} filled on the final chance @ {_fill_px(fill):.2f}")
            if es == "hedge_entry_failed":
                pos.hedge_entry_filled, pos.hedge_entry_px = True, _fill_px(fill)
                pos.hedge_entry_time = _now().isoformat()
            else:
                self._record_short_fill(_fill_px(fill))
            self._clear_error()
            self._continue_after_resolution()
            return
        try:
            self.client.cancelorder(order_id=order_id, strategy=self.env.strategy_tag)
        except Exception as exc:
            Log.warning(f"cancelorder {order_id} failed ({exc}) -- verify at the broker that nothing rests.")
        self._abandon_entry("cancelled by the operator after a final re-price")
        self._clear_error()

    def _abandon_entry(self, why: str):
        """Give up a half-built ENTRY. Places no order (OI_Positional author
        rule: failures are handled manually). Nothing filled -> flat. A filled
        leg -> the record is cleared as instructed, loudly, because the
        position itself may still exist at the broker."""
        pos = self.pos
        held = [s for s, f in ((pos.hedge_symbol, pos.hedge_entry_filled),
                               (pos.short_symbol, pos.entry_filled)) if s and f]
        if held:
            msg = (f"entry abandoned ({why}) while holding {', '.join(held)} -- the local record is cleared, "
                   f"the position is NOT closed by this. Close it at the broker if it is still open.")
            Log.warning(msg)
            self._notify_telegram_error_bg(msg)
        else:
            Log.info(f"[{LEG_KEY}] entry abandoned ({why}) -- nothing was filled, slot is flat.")
        self.price_stream.remove_instruments([{"symbol": s, "exchange": OPTIONS_EXCHANGE}
                                              for s in (pos.short_symbol, pos.hedge_symbol) if s])
        self.store.state.position = Position()
        self.save_state()

    # ---- crash recovery ---------------------------------------------------
    def reconcile_pending_orders(self):
        """Startup only, before the scheduler. For every order placed but not
        confirmed, ask the broker. Every window where an order may or may not
        exist with nothing recorded is flagged for a human, never retried."""
        pos = self.pos
        if pos.error_state or not (pos.hedge_symbol or pos.short_symbol):
            return
        steps = [
            ("hedge_entry_failed", pos.hedge_entry_order_id, pos.hedge_entry_filled),
            ("entry_failed", pos.entry_order_id, pos.entry_filled),
            ("exit_failed", pos.short_exit_order_id, pos.short_exit_filled),
            ("hedge_exit_failed", pos.hedge_exit_order_id, pos.hedge_exit_filled),
        ]
        for es, oid, filled in steps:
            if oid and not filled:
                self._reconcile_order(es, oid)
                return
        # Ambiguous windows: the next order of the chain may have been sent
        # inside placeorder() with no id recorded.
        ambiguous = None
        if not pos.hedge_entry_filled and not pos.hedge_entry_order_id and not pos.pending_exit_reason:
            ambiguous = ("hedge_entry_failed", "the hedge BUY")
        elif pos.hedge_entry_filled and not pos.entry_filled and not pos.entry_order_id and not pos.pending_exit_reason:
            ambiguous = ("entry_failed", "the short SELL")
        elif pos.pending_exit_reason and pos.entry_filled and not pos.short_exit_filled and not pos.short_exit_order_id:
            ambiguous = ("exit_failed", "the short BUY (close)")
        elif (pos.pending_exit_reason and pos.short_exit_filled and pos.hedge_entry_filled
              and not pos.hedge_exit_filled and not pos.hedge_exit_order_id):
            ambiguous = ("hedge_exit_failed", "the hedge SELL (close)")
        if ambiguous:
            es, what = ambiguous
            pos.error_state, pos.error_kind, pos.error_order_id = es, "terminal", ""
            pos.error_message = (f"The strategy restarted while sending {what}; no order id was recorded, so it is "
                                 f"unknown whether that order reached the broker. Check the order book: if it "
                                 f"filled, choose Manually Completed with its price; if nothing was placed, "
                                 f"choose Retry.")
            pos.error_since = _now().isoformat()
            self.save_state()
            push_leg_error(self.env, LEG_KEY, pos, action=_error_action(es))
            Log.error(f"[reconcile] {pos.error_message}")
            self._notify_telegram_error_bg(pos.error_message)

    def _reconcile_order(self, es: str, order_id: str):
        pos = self.pos
        try:
            data = _order_status(self.client, order_id, self.env.strategy_tag)
        except Exception as exc:
            data = None
            Log.exception(f"[reconcile] orderstatus({order_id}) failed: {exc}")
        status = str((data or {}).get("order_status", "")).lower()
        Log.info(f"[reconcile] {es} order {order_id} status='{status}'")
        if status == "complete":
            px = _fill_px(data)
            if es == "hedge_entry_failed":
                pos.hedge_entry_filled, pos.hedge_entry_px = True, px
                pos.hedge_entry_time = pos.hedge_entry_time or _now().isoformat()
            elif es == "entry_failed":
                self._record_short_fill(px)
            elif es == "exit_failed":
                pos.short_exit_filled, pos.short_exit_fill_px = True, px
                pos.short_exit_time = _now().isoformat()
            else:
                pos.hedge_exit_filled, pos.hedge_exit_fill_px = True, px
                pos.hedge_exit_time = _now().isoformat()
            self.save_state()
            Log.info(f"[reconcile] {order_id} had filled @ {px:.2f} -- the chain resumes on the first tick.")
            return
        if status in {"rejected", "cancelled", "canceled"}:
            if es == "hedge_entry_failed":
                pos.hedge_entry_order_id = ""
                if not pos.entry_filled:
                    self._abandon_entry(f"hedge order {order_id} was {status} before the restart")
                    return
            elif es == "entry_failed":
                pos.entry_order_id = ""
                self._enter_error_mode("entry_failed", "terminal", "",
                                       f"The short order {order_id} was {status} while the strategy was down; "
                                       f"the hedge {pos.hedge_symbol} is held. Retry sells the short again, "
                                       f"Cancel clears the record (close the hedge yourself).")
                return
            elif es == "exit_failed":
                pos.short_exit_order_id = ""
            else:
                pos.hedge_exit_order_id = ""
            self.save_state()
            Log.info(f"[reconcile] {order_id} was {status}; the chain places a fresh order when it resumes.")
            return
        pos.error_state, pos.error_kind, pos.error_order_id = es, "resting", order_id
        pos.error_message = (f"After a restart order {order_id} is '{status or 'unknown'}' -- Retry re-prices and "
                             f"keeps watching it, Cancel withdraws it, Manually Completed records a fill.")
        pos.error_since = _now().isoformat()
        self.save_state()
        push_leg_error(self.env, LEG_KEY, pos, action=_error_action(es))
        Log.error(f"[reconcile] {pos.error_message}")

    # ---- force exit -------------------------------------------------------
    def _handle_force_exit(self):
        st = self.state
        if not st.day_done:
            st.day_done = True
            st.reentry_side, st.reentry_p, st.reentry_armed = "", 0.0, False
            st.retry_side, st.retry_leg_type, st.retry_due = "", "", ""
            self.save_state()
            Log.warning("[FORCE EXIT] requested -- closing everything (short first), no new trades today.")
        if self.busy():
            return
        pos = self.pos
        if pos.error_state:
            self._refresh_pending_action_bg()
            action = self._pop_pending_action()
            if action is not None:
                self._resolve_leg_error(action)
            return
        if pos.hedge_symbol or pos.short_symbol:
            self._dispatch("force-exit", self._exit, "FORCE_EXIT")
            return
        Log.warning("[FORCE EXIT] complete -- flat.")
        ack_force_exit_complete(self.env)
        self._force_exit_pending = False

    # ---- main cycle -------------------------------------------------------
    def run_cycle(self):
        try:
            self._repush_active_errors()
            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                self._handle_force_exit()
                return

            self._reset_day_if_needed()
            if not _within_market_hours():
                return
            today = _now().date()
            ok, why = trading_day_status(today)
            if not ok:
                if self._non_trading_logged != today.isoformat():
                    self._non_trading_logged = today.isoformat()
                    Log.info(f"{today}: {why} -- not trading today.")
                return

            self._sample_spot()
            if self.busy():
                return

            st, pos = self.state, self.pos
            now = _now()
            t = now.time()

            if pos.error_state:
                self._refresh_pending_action_bg()
                action = self._pop_pending_action()
                if action is not None:
                    self._resolve_leg_error(action)
                elif t >= config.square_off_time and (
                        self._eod_error_logged is None or (now - self._eod_error_logged).total_seconds() >= 60):
                    self._eod_error_logged = now
                    Log.error(f"[{LEG_KEY}] 15:15 passed with {pos.error_state} unresolved -- resolve it NOW "
                              f"(Retry / Cancel / Manually Completed).")
                return

            holding = bool(pos.hedge_symbol or pos.short_symbol)
            if holding and (pos.pending_exit_reason
                            or not (pos.hedge_entry_filled and pos.entry_filled)):
                # A chain stopped part-way (resolved error, restart): resume it.
                self._dispatch("resume", self._continue_after_resolution)
                return

            if t >= config.square_off_time:
                if holding:
                    self._dispatch("eod", self._exit, "EOD")
                    return
                if not st.day_done:
                    st.day_done = True
                    st.reentry_side, st.reentry_p, st.reentry_armed = "", 0.0, False
                    st.retry_side, st.retry_leg_type, st.retry_due = "", "", ""
                    self.save_state()
                    Log.info(f"[EOD] {config.square_off_time:%H:%M}: flat, done for the day. "
                             f"Realized {st.today_realized_pnl:+.2f}")
                return
            if st.day_done:
                return

            # 1. Stop-loss, every tick.
            if holding:
                ltp = self._short_ltp(pos)
                if ltp is not None and ltp >= pos.sl_px > 0:
                    self._dispatch("sl", self._do_sl_exit, ltp)
                    return

            # 2. The line.
            if not st.line_ready:
                if t >= config.line_read_time and (self._next_line_try is None or now >= self._next_line_try):
                    self._dispatch("line", self._compute_line)
                return

            # 3. Hourly check: the latest elapsed slot not yet run.
            elapsed = [s for s in config.check_times if t >= _plus_min(s, config.check_buffer_min)]
            if elapsed:
                slot = elapsed[-1]
                if st.last_check_slot != slot.strftime("%H:%M"):
                    if not st.initial_done:
                        st.initial_done = True   # an 09:31 entry is never taken after a check
                        self.save_state()
                    self._dispatch(f"check {slot:%H:%M}", self._do_check, slot)
                    return

            # 4. The 09:31 entry.
            if not st.initial_done:
                if t <= config.initial_entry_deadline:
                    self._dispatch("initial", self._do_initial)
                else:
                    st.initial_done = True
                    self.save_state()
                    Log.info(f"[INITIAL] not taken: past {config.initial_entry_deadline:%H:%M} "
                             f"(started late or line read late) -- the 10:31 check decides.")
                return

            # 4b. The single "not found" retry.
            if not holding and st.retry_side and st.retry_due:
                try:
                    due = datetime.fromisoformat(st.retry_due)
                except ValueError:
                    due = now
                if now >= due:
                    self._dispatch("retry", self._do_retry)
                    return

            # 5. Re-entry after an SL, on completed 1-minute closes.
            if not holding and st.reentry_side and st.reentry_armed:
                minute = now.replace(second=0, microsecond=0)
                # Only the minute that just closed counts; if the feed missed
                # it, that minute is skipped rather than judged on a stale price.
                if self._reentry_eval_minute != minute:
                    m = minute - timedelta(minutes=1)
                    close, how = self._minute_close(m)
                    if close is not None:
                        self._reentry_eval_minute = minute
                        hit = close >= st.reentry_p if st.reentry_side == "PE" else close <= st.reentry_p
                        if hit:
                            Log.info(f"[REENTRY] NIFTY {m:%H:%M} close {close:.2f} ({how}) reached "
                                     f"P={st.reentry_p:.2f} -> sell {SIDE_NAME[st.reentry_side]} again")
                            self._dispatch("reentry", self._do_reentry)
        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            now = _now()
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                self._notify_telegram_error_bg(f"Cycle failed: {exc}")

    # ---- PnL ---------------------------------------------------------------
    def _open_positions_for_pnl(self) -> list:
        pos = self.pos
        out = []
        if pos.short_symbol and pos.entry_filled and not pos.short_exit_filled:
            ltp = self.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds) or pos.entry_px
            out.append({"leg_key": f"{LEG_KEY}_{pos.side}", "symbol": pos.short_symbol, "direction": "SHORT",
                        "quantity": -pos.quantity, "entry_price": pos.entry_px, "current_price": ltp,
                        "pnl": (pos.entry_px - ltp) * pos.quantity, "entry_time": pos.entry_time,
                        "execution_id": pos.execution_id})
        if pos.hedge_symbol and pos.hedge_entry_filled and not pos.hedge_exit_filled:
            ltp = (self.price_stream.get_ltp(pos.hedge_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
                   or pos.hedge_entry_px)
            out.append({"leg_key": f"{LEG_KEY}_{pos.side}_HEDGE", "symbol": pos.hedge_symbol, "direction": "LONG",
                        "quantity": pos.quantity, "entry_price": pos.hedge_entry_px, "current_price": ltp,
                        "pnl": (ltp - pos.hedge_entry_px) * pos.quantity,
                        "entry_time": pos.hedge_entry_time or pos.entry_time, "execution_id": pos.execution_id})
        return out

    def report_pnl_tick(self):
        try:
            report_pnl_to_platform(self.env, self.state.today_realized_pnl, self._open_positions_for_pnl())
        except Exception:
            Log.exception("report_pnl_tick failed")

    def shutdown(self):
        self._fill_executor.shutdown(wait=False)
        self._bg_executor.shutdown(wait=False)


###############################################################################
# MAIN
###############################################################################
def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version          : {config.version}")
    print(f"Line             : {config.line_candle_interval} candle {config.line_candle_start:%H:%M}, "
          f"read {config.line_read_time:%H:%M}")
    print(f"Initial entry    : {config.line_read_time:%H:%M} (not after {config.initial_entry_deadline:%H:%M})")
    print("Hourly checks    : " + " ".join(_plus_min(t, config.check_buffer_min).strftime("%H:%M")
                                           for t in config.check_times) + " (09:30-based hours)")
    print(f"Strike           : |delta| closest to {config.delta_target}, HARD {config.delta_low}-"
          f"{config.delta_high}, round {config.strike_round}")
    print(f"Expiry           : 2nd upcoming (next week), max {config.next_week_max_days} days")
    print(f"Hedge            : +{config.hedge_distance_pts} pts (up to +{config.hedge_distance_pts + config.hedge_extra_max_pts}), "
          f"none priced -> no trade")
    print(f"SL               : short LTP >= fill x {1 + config.sl_pct / 100:.2f}")
    print(f"Square-off       : {config.square_off_time:%H:%M}")
    print("Order sequence   : ENTRY hedge BUY -> short SELL | EXIT short BUY -> hedge SELL")
    print(f"Quantity         : {config.lots} lot(s), product {config.product}, {config.price_type}")
    print("=" * 70)


def main():
    print_banner()
    env = Environment()
    store = StateStore(env)
    store.load()
    store.state.last_execution_id += 1
    execution_id = store.state.last_execution_id
    store.save()

    broker = Broker(env)
    client = broker.connect()
    set_calendar_client(client)
    ltp_client = broker.connect_ltp_client()
    chain_client = broker.connect_chain_client()

    price_stream = PriceStream(client)
    known = [{"symbol": UNDERLYING_SYMBOL, "exchange": UNDERLYING_SPOT_EXCHANGE}]
    pos = store.state.position
    for sym in (pos.short_symbol, pos.hedge_symbol):
        if sym:
            known.append({"symbol": sym, "exchange": OPTIONS_EXCHANGE})
    price_stream.seed_instruments(known)
    price_stream.start()

    engine = StrategyEngine(client, store, env, price_stream, execution_id, ltp_client=ltp_client,
                            chain_client=chain_client)
    print(f"State file       : {store.path}")
    print(f"Execution ID     : {execution_id}")
    try:
        ok, why = trading_day_status(_now().date())
        print(f"Today            : {why}")
    except Exception as exc:
        print(f"Today            : calendar check failed ({exc})")

    engine.reconcile_pending_orders()
    pos = store.state.position
    if pos.error_state:
        push_leg_error(env, LEG_KEY, pos, action=_error_action(pos.error_state))
        Log.error(f"Resuming with an unresolved {pos.error_state} ({pos.error_kind}) -- needs Retry / Cancel / "
                  f"Manually Completed.")
    elif pos.hedge_symbol or pos.short_symbol:
        Log.info(f"Resuming the recorded position: short {pos.short_symbol}@{pos.entry_px} SL={pos.sl_px} "
                 f"hedge {pos.hedge_symbol}@{pos.hedge_entry_px}")
        if not _within_market_hours():
            msg = (f"started outside market hours with a position recorded ({pos.short_symbol} / "
                   f"{pos.hedge_symbol}) -- it will be managed from the next open; verify it at the broker.")
            Log.error(msg)
            notify_telegram_error(env, f"[{config.strategy_name}] {msg}", log_warning=Log.warning)

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(engine.run_cycle, trigger=IntervalTrigger(seconds=config.scheduler_interval),
                      id="strategy_cycle", max_instances=1, coalesce=True)
    scheduler.add_job(engine.report_pnl_tick, trigger=IntervalTrigger(seconds=config.pnl_tick_interval),
                      id="report_pnl_tick", max_instances=1, coalesce=True)
    Log.info("Strategy initialization complete. Starting scheduler...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        Log.info("Shutting down.")
    except Exception:
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        raise
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        price_stream.stop()
        engine.shutdown()


if __name__ == "__main__":
    main()
