"""
===============================================================================
NIFTY LEAPS Options Sell (RSI-driven, quarterly, hedge-protected)
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Validated by: data23_to_26/backtest_leaps_options_sell.py (31 trades,
              2023-01-30 to 2024-09-06, net +Rs 53,004 without the daily gap
              hedge, fully audited against raw data with zero discrepancies).

*** THIS STRATEGY SELLS A NAKED NIFTY OPTION, PARTIALLY HEDGED BY A SEPARATE,
    FURTHER-OTM, MONTHLY-ROLLING HEDGE LEG -- NOT A FULLY DEFINED-RISK SPREAD. ***

Distinct in shape from every other deployed strategy in this project: it
holds a single directional leg at a time (never both CE and PE), on a
genuinely long-dated QUARTERLY contract (Mar/Jun/Sep/Dec), protected by a
separately-rolling MONTHLY hedge -- no other script here trades anything
longer than the current/next month.

Signal
------------------------------------------------------------------------
RSI(14) on 1-hour NIFTY spot candles, evaluated at every hourly candle
close, every trading day -- continuous, not gated to a monthly cycle.
  - RSI > 52 -> sell OTM PE (bullish bias).
  - RSI < 32 -> sell OTM CE (bearish bias).
  - RSI in [32, 52] -> no new trade.
At most ONE directional position open at a time -- a true single-slot state
machine (LeapsPosition), not a per-leg dict.

Exit is a FULL opposite-threshold RSI CROSSING (needs the previous closed
bar's RSI, not just leaving the 32-52 zone): a sold PE closes only when RSI
crosses below 32; that same crossing is itself a fresh CE signal, so the new
leg opens the moment the old leg's close is CONFIRMED by the fill-watcher --
not gated to the next scheduler tick / hourly boundary (same-bar reversal).

No premium-based stop-loss, no profit target -- the monthly hedge is the
only defined-risk mechanism.

Main (sold) leg
------------------------------------------------------------------------
OTM strikes at 500-point increments from ATM (round(spot/500)*500 as base),
scanned up to config.max_otm_steps (6, i.e. 3000pt OTM -- a DELIBERATE
reduction from the validated backtest's 10 steps/5000pt, to keep every
optionchain() scan band comfortably under the 10s client timeout every
deployed strategy in this repo uses; see select_main_strike()'s own
docstring for the measured-latency reasoning). Premium target Rs 350, band
300-400 -- picks whichever scanned strike has premium closest to 350 within
the band; if none fall in the band, closest-to-350 among all scanned.
Expiry is a genuinely long-dated QUARTERLY contract, resolved via
resolve_leaps_expiry() (ported near-verbatim from the validated backtest)
-- feeds ONLY new entries; an
open position's own main leg never rolls mid-trade.

Monthly hedge
------------------------------------------------------------------------
Same side as the sold leg, further OTM, nearest LISTED strike ~2% away from
the ORIGINAL SOLD STRIKE -- fixed at entry, every recurring roll re-targets
that same 2% band around the ORIGINAL sold strike, never the market's
current level. Selected by strike-DISTANCE (abs(strike - target_strike)),
not premium. Lives on its own, SHORTER monthly contract, always a different
expiry than the main leg's quarterly one -- select_hedge_strike() always
fetches its own fresh optionchain() call rather than reusing the main leg's
scan (documented explicitly below so this is never "optimized" into an
incorrect reuse).

Hedge's own expiry: day-of-month > 15 (16th onward) -> next month's own last
weekly expiry; day <= 15 -> current month's own last weekly expiry. This is
a ONE-DAY-SHIFTED boundary from the validated backtest (which used day < 15
/ day >= 15) -- confirmed deliberately with the user for this live version
(day 15 itself still counts as "current month"). See
resolve_hedge_monthly_expiry()'s own docstring -- do not "fix" this back to
the backtest's boundary.

Hedge rollover: recurring, independent of the main position's lifetime. On
the 18th calendar day of every month (or the nearest PRIOR actual trading
day if the 18th is a holiday/weekend), while a position remains open: close
the current hedge, open a fresh one via the day>15 rule re-applied at the
roll date (always resolves next month, since 18 > 15). Checked once per day
(cheap date comparison at the top of every run_cycle()), guarded by
pos.last_hedge_roll_date so it never double-fires the same day.

Expiry-day safety close: if the currently open position's own quarterly
contract expires today and RSI hasn't triggered a reversal, force-close
(short leg then hedge leg) at config.expiry_day_close_time -- mirrors how
Nifty_OI_Positional_MonthlySell_1 handles its own contract's expiry day. No
new-position search follows that same day; the next entry waits for the
next RSI signal.

Explicitly OUT OF SCOPE (confirmed with user)
------------------------------------------------------------------------
No news/event no-trade filter. No premium-based stop-loss. No daily
overnight-gap hedge (the backtest's optional Rs5 daily hedge was
near-break-even and adds real operational complexity -- dropped entirely
for this live deployment).

Position sizing / product
------------------------------------------------------------------------
1 lot (quantity=65). Product NRML (mandatory -- multi-day/week/quarter
hold; there is no broker-side forced square-off backstop for NRML -- the
strategy's own RSI-reversal / hedge-roll / expiry-day-close logic is the
ONLY thing that ever closes a position; a failure to close is a genuine
overnight-carry risk).

Data sources, order placement, exception handling, PnL reporting -- same
conventions as every other deployed script in this project (see
strategies/deployed/AUTHORING_CHECKLIST.md): place() retries ONLY a clean
broker rejection; poll_fill()/_reprice_and_wait_once() cross the spread with
a fresh bid/ask; every fill (short leg AND hedge leg, independently) is
confirmed via a backgrounded fill-watcher; report_pnl_to_platform() pushes
one combined realized+unrealized snapshot per tick as its own APScheduler
job; _repush_active_errors() runs unconditionally every run_cycle.

*** MANDATORY PRE-DEPLOYMENT LIVE DRY-RUN GATE (partially de-risked, LIQUIDITY
    still not yet verified) ***
No existing deployed strategy has ever traded a quarterly-dated NIFTY
contract. The LISTING half of this risk is now backed by documentation, not
just inferred from the backtest: docs/api/symbol-services/expiry.md's own
sample client.expiry() response for NIFTY options already lists real
quarterly-dated expiries stretching years out (26-MAR-26, 25-JUN-26,
31-DEC-26, 24-JUN-27, ... to 25-JUN-30), confirming resolve_leaps_expiry()
CAN find a genuinely long-dated contract to target. What remains unverified
is LIQUIDITY: whether that far-dated contract's strikes actually carry a
genuinely two-sided, non-stale market near the Rs300-400 premium band --
the docs' optionchain() sample only demonstrates a near-month contract
(30DEC25) with real bid/ask, not a multi-quarter-out one. Before this script
is ever scheduled to trade real capital: confirm live
client.optionchain(underlying="NIFTY", exchange="NSE_INDEX",
expiry_date=<the quarterly contract resolve_leaps_expiry() currently
targets>, with_quotes=True) shows genuinely two-sided, non-stale bid/ask/ltp
around 500-pt OTM strikes near that band. If it doesn't, this is a blocking
design question (e.g. sourcing the quarterly contract differently), not
something to silently work around in code. NOT performed by this commit --
no live broker connection was available while writing this script.

Author
------
<Project Owner>
===============================================================================
"""

import calendar
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
from datetime import datetime, time, timedelta, date
from pathlib import Path
from typing import Optional

import numpy as np
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

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

try:
    from utils.trading_calendar import is_trading_day
except ImportError:
    # Same documented fallback as MCX_CrudeOil_HMRSI_SMA50_Intraday: no
    # deployed strategy in this project relies on cross-module imports
    # beyond the openalgo SDK and _strategy_platform_client -- untested
    # sys.path for this specific module when run as an isolated subprocess.
    # Degrades to a weekday-only check (no holiday awareness) -- only used
    # to walk the 18th-of-month hedge roll date back off a holiday/weekend,
    # so being off by a holiday is a low-consequence gap, not correctness-
    # critical (worst case: the roll fires one weekday early or late).
    def is_trading_day(day) -> bool:
        return day.weekday() < 5

load_dotenv()

print("OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
UNDERLYING_SYMBOL = "NIFTY"
UNDERLYING_SPOT_EXCHANGE = "NSE_INDEX"
OPTIONS_EXCHANGE = "NFO"

# Single pseudo-leg key -- this strategy holds at most ONE directional
# position at a time (never CE and PE together), so there is no per-leg
# dict the way the multi-leg donor scripts have. Kept as a named constant
# (rather than a bare string scattered through push_leg_error/
# check_pending_action call sites) purely for readability/grep-ability.
LEG_KEY = "LEAPS"


@dataclass
class Config:
    strategy_name: str = "NIFTY LEAPS Options Sell"
    version: str = "1.0.0"

    intraday_interval: str = "1h"          # confirmed valid live (Shoonya timeframe_map) -- see
                                            # CRUDEOIL_VWAP_EMA20_Positional_1, which already uses "1h"
    rsi_period: int = 14
    rsi_bull_threshold: float = 52.0       # RSI > 52 -> sell OTM PE
    rsi_bear_threshold: float = 32.0       # RSI < 32 -> sell OTM CE
    history_lookback_days: int = 40        # RSI(14) warm-up without reset artifacts

    main_strike_round: int = 500
    premium_target: float = 350.0
    premium_band_low: float = 300.0
    premium_band_high: float = 400.0
    max_otm_steps: int = 6                  # 6 x 500pt = 3000pt OTM max reach -- deliberately
                                            # NOT 10 (5000pt): with_quotes=True's real per-strike
                                            # broker quote fan-out was measured live at 32.53s for
                                            # a ~120-strike_count request (Nifty_TrendFollow_
                                            # DualTF27_9_2's own finding), and every deployed
                                            # strategy in this repo uses a 10s client timeout
                                            # (Environment.timeout) -- a 60-strike_count band
                                            # needed for 5000pt reach would likely always exceed
                                            # that budget. 3000pt keeps every band's worst-case
                                            # comfortably under 10s instead.
    main_strike_scan_bands: tuple = (10, 20, 30)   # expanding optionchain() strike_count --
                                            # try small (cheap, with_quotes=True fans out a
                                            # real broker quote call per strike) before
                                            # widening to 30 (covers 6 steps x 500pt = 3000pt
                                            # at the documented 100pt NIFTY grid, ~8s worst
                                            # case at the measured ~0.133s/row rate) only if no
                                            # in-band candidate is found
    main_strike_retry_window_sec: float = 600.0    # if RSI qualifies but no strike is found,
                                            # keep re-scanning (fresh spot + optionchain each
                                            # time) for up to 10 minutes before giving up
                                            # until the next hourly candle
    main_strike_retry_interval_sec: float = 60.0   # spacing between retry scans

    monthly_expiry_plausibility_days: int = 8   # a genuine NSE monthly (last-Thursday-type)
                                            # expiry always falls within the final ~7-8
                                            # calendar days of its month; a "month_ends"
                                            # candidate earlier than that signals an
                                            # incomplete/stale expiry() list (a later same-
                                            # month weekly not yet listed), not a real monthly

    hedge_pct: float = 2.0                 # ~2% from ORIGINAL sold strike, fixed at entry
    hedge_strike_round: int = 100          # hedge strike must be a round-100 multiple
    hedge_roll_dom: int = 18               # recurring roll day-of-month (holiday-shifted back)
    hedge_month_roll_dom: int = 15         # day>15 -> next month's own last weekly expiry; day<=15 stays current month

    quarter_months: tuple = (3, 6, 9, 12)

    quantity: int = 65                     # 1 lot
    product: str = "NRML"                  # mandatory -- multi-day/week/quarter hold, no broker-side square-off
    price_type: str = "MARKET"

    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    expiry_day_close_time: time = time(15, 15)   # force-close if main leg expires today

    scheduler_interval: int = 15           # tight tick purely to catch the hourly candle
                                            # boundary promptly (e.g. fire within ~15s of
                                            # 10:15) -- the heavy work (history fetch, RSI,
                                            # strike scan) only actually runs when
                                            # _new_candle_closed() is true, so a short tick
                                            # costs nothing extra the rest of the hour
    pnl_tick_interval: float = 0.8

    # PriceStream config -- not part of the plan's own literal Config
    # snippet, but PriceStream is copied byte-for-byte from the primary
    # donor (Nifty_OI_Positional_MonthlySell_1) per the approved plan, and
    # that class reads these fields directly. Judgment call: added with the
    # donor's own defaults, since PriceStream cannot function without them.
    ws_stale_seconds: float = 20.0
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
class LeapsPosition:
    """A single directional position -- at most one ever open at a time (a
    true single-slot state machine, not a per-leg dict like the OI
    Positional donor's ShortHedgePosition). `side` ("" / "CE" / "PE") is the
    flat/open sentinel, matching the donor's own `short_symbol`-as-sentinel
    convention but explicit about direction too, since select_hedge_strike
    and the RSI cross checks both need to know which side is open."""
    side: str = ""                      # "" / "CE" / "PE" -- flat when empty

    short_symbol: str = ""
    short_strike: float = 0.0
    quantity: int = 0
    entry_time: str = ""
    entry_px: float = 0.0
    expiry_date: str = ""               # the QUARTERLY contract -- never rolls mid-trade
    entry_order_id: str = ""
    entry_filled: bool = False
    short_exit_order_id: str = ""
    short_exit_filled: bool = False
    short_exit_fill_px: Optional[float] = None

    hedge_symbol: str = ""
    hedge_strike: float = 0.0           # re-targeted every roll, always vs short_strike
    hedge_expiry: str = ""              # own (shorter, monthly) contract -- independent lifecycle
    hedge_entry_px: float = 0.0
    hedge_entry_order_id: str = ""
    hedge_entry_filled: bool = False
    hedge_exit_order_id: str = ""
    hedge_exit_filled: bool = False
    hedge_exit_fill_px: Optional[float] = None
    last_hedge_roll_date: str = ""      # guards the 18th-of-month roll to at most once/day

    pending_exit_reason: str = ""
    pending_reentry_side: str = ""      # set on same-bar-reversal close; consumed once flat confirmed

    execution_id: int = 0

    error_state: str = ""
    error_kind: str = ""
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None


@dataclass
class StrategyState:
    current_day: str = ""
    position: LeapsPosition = field(default_factory=LeapsPosition)
    trade_count: int = 0
    last_rsi_prev: Optional[float] = None   # the previously-processed closed candle's RSI -- cross-detection
    last_candle_key: str = ""
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
            or "nifty_leaps_options_sell"
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
# from the primary donor (Nifty_OI_Positional_MonthlySell_1), itself carried
# unchanged from MCX_CrudeOil_EMA9_RSI_Intraday's already-hardened
# PriceStream. Needed for (a) NIFTY spot LTP (strike selection) and (b) the
# open position's own symbols (short + hedge) for PnL display.
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
        -- for main() to call BEFORE start(). See the primary donor's
        identical docstring for the startup race this avoids."""
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
        self.state.trade_count = data.get("trade_count", 0)
        self.state.last_rsi_prev = data.get("last_rsi_prev")
        self.state.last_candle_key = data.get("last_candle_key", "")
        self.state.last_updated = data.get("last_updated", "")
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.last_execution_id = data.get("last_execution_id", 0)
        pos_raw = data.get("position", {})
        self.state.position = LeapsPosition(
            **{**asdict(LeapsPosition()), **filter_known_fields(LeapsPosition, pos_raw)}
        )
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "trade_count": self.state.trade_count,
            "last_rsi_prev": self.state.last_rsi_prev,
            "last_candle_key": self.state.last_candle_key,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_execution_id": self.state.last_execution_id,
            "position": asdict(self.state.position),
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=2)


###############################################################################
# BROKER DATA HELPERS
###############################################################################
def _is_error_response(obj) -> bool:
    """quotes() always returns a dict; client.history() returns a pandas
    DataFrame on SUCCESS and an error dict on FAILURE."""
    return isinstance(obj, dict)


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
    """`require_two_sided=True` additionally requires bid>0 AND ask>0 --
    pass True only for TRADABLE-instrument reads; an INDEX symbol
    legitimately has no bid/ask, so leave this False for underlying-spot
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


def _is_plausible_current_month_expiry(d: date) -> bool:
    """A genuine NSE monthly (last-Thursday-type) index-options expiry
    always falls within the final config.monthly_expiry_plausibility_days
    calendar days of its own month. Only meaningful for resolving the
    CURRENT calendar month's contract: a future month's `month_ends` entry
    is always trustworthy as-is (the broker only ever lists a single date
    for a month that far out -- there is no ambiguity to guard against,
    and an early-in-month date there is a legitimate holiday-shifted
    monthly, not a sign of incomplete data). The CURRENT month is
    different: it may still have several weeklies listed with more yet to
    come, so `month_ends[current_key]` (max-so-far) could quietly be an
    ordinary mid-month weekly mistaken for the monthly if the broker's
    list hasn't caught up yet. Callers MUST raise/refuse rather than
    silently trade a date that fails this, never just log and continue."""
    days_in_month = calendar.monthrange(d.year, d.month)[1]
    return d.day >= days_in_month - config.monthly_expiry_plausibility_days


def _legs_with_strike(chain: dict, option_type: str) -> list:
    """Flattens optionchain()'s per-strike-row shape into a flat list of leg
    dicts, each carrying its own `strike` -- established pattern, copied
    from the primary donor."""
    key = option_type.lower()
    legs = []
    for row in chain["chain"]:
        leg = row.get(key)
        if leg:
            merged = dict(leg)
            merged["strike"] = row["strike"]
            legs.append(merged)
    return legs


###############################################################################
# EXPIRY RESOLUTION -- main (quarterly) leg + hedge (monthly) leg
###############################################################################
def resolve_leaps_expiry(client, today: date) -> Optional[tuple]:
    """Resolves the QUARTERLY (Mar/Jun/Sep/Dec) contract this strategy's
    main leg should target for a NEW entry today. Ported near-verbatim from
    the validated backtest (data23_to_26/backtest_leaps_options_sell.py:
    124-163), swapping the backtest's precomputed expiry list for a live
    client.expiry() query.

    Rolls to the next quarterly (Mar->Jun->Sep->Dec->Mar) on day 20 of the
    month immediately before the currently-targeted quarterly's own expiry
    month:
      Jan, Feb 1-20 -> Mar; Feb 21-end, Mar, Apr 1-20 -> Jun;
      Apr 21-end, May, Jun, Jul 1-20 -> Sep;
      Jul 21-end, Aug, Sep, Oct 1-20 -> Dec;
      Oct 21-end, Nov, Dec -> Mar (next year).

    Returns (compact_expiry, expiry_date) or None if client.expiry()
    doesn't return a quarterly-dated contract that far out -- callers MUST
    treat None as a hard failure (no entry attempted), never silently skip
    it. This is exactly the risk the mandatory pre-deployment live dry-run
    gate (see module docstring) exists to catch before any real order is
    placed. Feeds ONLY the entry path -- never called against an
    already-open position's own expiry_date."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve LEAPS quarterly expiry: {resp}")
    all_dates = sorted(datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"])
    dates = [d for d in all_dates if d >= today]
    if not dates:
        return None
    month_ends: dict[tuple, date] = {}
    for d in dates:
        key = (d.year, d.month)
        if key not in month_ends or d > month_ends[key]:
            month_ends[key] = d

    def month_before(year, month):
        return (year, 12) if month == 1 else (year, month - 1)

    y, m = today.year, today.month
    for _ in range(12):
        if m not in config.quarter_months:
            nq = next((q for q in config.quarter_months if q > m), None)
            y2, m2 = (y, nq) if nq else (y + 1, config.quarter_months[0])
        else:
            y2, m2 = y, m
        by, bm = month_before(y2, m2)
        threshold = date(by, bm, config.hedge_roll_dom + 2)   # day 20 -- literal port
        if today <= threshold and (y2, m2) in month_ends:
            chosen = month_ends[(y2, m2)]
            return _compact_expiry(chosen.strftime("%d-%b-%y")), chosen
        idx = config.quarter_months.index(m2)
        if idx + 1 < len(config.quarter_months):
            y, m = y2, config.quarter_months[idx + 1]
        else:
            y, m = y2 + 1, config.quarter_months[0]
    return None


def resolve_hedge_monthly_expiry(client, ref_day: date) -> tuple[str, str]:
    """Resolves the hedge leg's own (shorter, monthly) contract, used both
    at entry AND at every recurring 18th-of-month roll (18 > 15 always, so
    every recurring roll naturally lands on next month's contract).

    *** DELIBERATE ONE-DAY BOUNDARY SHIFT FROM THE VALIDATED BACKTEST ***
    The backtest (data23_to_26/backtest_leaps_options_sell.py:166-183) used
    `ref_day.day < config.hedge_month_roll_dom` (day>=15 already rolls to
    next month). This LIVE version instead uses `ref_day.day <=
    config.hedge_month_roll_dom` for current-month (day 15 itself still
    counts as current month; only day 16 onward -- i.e. day>15 -- rolls to
    next month). Confirmed deliberately with the user for this live
    deployment -- do NOT "correct" this back to the backtest's boundary.
    Also do not conflate this with Nifty_OI_Positional_MonthlySell_1's own
    `<=15`/`>15` split for resolve_monthly_expiry() -- a different function,
    different purpose, which happens to use the same boundary as this
    corrected version. That's a coincidence worth noting, not a reason to
    merge the two functions."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve hedge monthly expiry: {resp}")
    dates = sorted(datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"])
    dates = [d for d in dates if d >= ref_day]
    if not dates:
        raise RuntimeError("No upcoming expiries at all for the hedge leg.")
    month_ends: dict[tuple, date] = {}
    for d in dates:
        key = (d.year, d.month)
        if key not in month_ends or d > month_ends[key]:
            month_ends[key] = d
    current_key = (ref_day.year, ref_day.month)
    if ref_day.day <= config.hedge_month_roll_dom and current_key in month_ends:
        chosen = month_ends[current_key]
        if not _is_plausible_current_month_expiry(chosen):
            raise RuntimeError(
                f"Current-month hedge expiry candidate {chosen} is too early in "
                f"{chosen.year}-{chosen.month:02d} to plausibly be the real monthly "
                f"contract -- the broker's expiry() list is likely incomplete for "
                f"this month (a later weekly not yet listed). Refusing to use it."
            )
    else:
        future_keys = sorted(k for k in month_ends if k > current_key)
        if not future_keys:
            raise RuntimeError("No next-month expiry available to roll the hedge to.")
        chosen = month_ends[future_keys[0]]
    raw = chosen.strftime("%d-%b-%y")
    return _compact_expiry(raw), raw


def hedge_roll_date_for_month(year: int, month: int) -> date:
    """The 18th of (year, month), or the nearest PRIOR actual trading day
    if the 18th is a holiday/weekend. Deliberately NOT
    trading_calendar.prev_trading_day() -- that returns the trading day
    STRICTLY BEFORE a given date, which is wrong when the 18th itself is a
    trading day (must return the 18th itself then)."""
    cursor = date(year, month, config.hedge_roll_dom)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


###############################################################################
# STRIKE SELECTION
###############################################################################
def select_main_strike(client, spot: float, side: str, expiry_compact: str) -> Optional[dict]:
    """Ranks a fixed config.max_otm_steps-step OTM scan by premium-closeness
    to config.premium_target -- NOT a delta/OI selection like the OI
    Positional donor. Returns {"strike", "premium", "in_band", "diff"} for
    the winning strike, or None if nothing scanned has a usable premium.

    Expanding-band optionchain() scan (mirrors the OI Positional donor's
    own band_step_count/max_bands pattern for select_qualifying_monthly_strike):
    with_quotes=True is confirmed to trigger a genuine per-strike broker
    quote fan-out, not a cheap flag -- Nifty_TrendFollow_DualTF27_9_2's own
    fetch_chain_strikes() docstring measured this live at 32.53s for 244
    rows (strike_count~120) vs 5.80s at strike_count=20, tracing the exact
    same openalgo SDK path this script calls. A single large-strike_count
    call every attempt would pay that full cost even when the premium
    target sits close to ATM (the common case). Instead, try
    config.main_strike_scan_bands in increasing size, stopping as soon as
    an IN-BAND candidate is found -- premium falls off roughly monotonically
    moving away from ATM, so a genuine in-band hit at a small band is very
    unlikely to be beaten by something further out worth paying for a wider
    (slower) call. Only if no band ever produces an in-band candidate does
    the final (largest) band's own scan get used for the closest-to-target
    fallback -- never silently settling for a narrower band's fallback
    when a wider, unqueried one might have held a better (or any) candidate.

    strike_count sizing: per docs/api/options-services/optionchain.md,
    strike_count means strikes ABOVE and BELOW ATM (not total rows), and
    its own documented sample response for NIFTY lists strikes 100 points
    apart near ATM (26100/26200/26300), not 50. config.max_otm_steps is
    deliberately capped at 6 (3000pt OTM, not the wider 5000pt a 10-step
    scan would need) so the final band (30) stays comfortably under the
    ~10s client timeout every deployed strategy in this repo uses
    (Environment.timeout) -- at the measured ~0.133s/row rate, a
    60-strike_count band (needed for 5000pt reach) would likely exceed
    that budget most of the time, while 30 (needed for 3000pt reach) has
    real margin (~8s worst case)."""
    atm = round(spot / config.main_strike_round) * config.main_strike_round
    direction = 1 if side == "CE" else -1
    side_key = side.lower()

    scanned: list = []
    for scan_count in config.main_strike_scan_bands:
        resp = client.optionchain(underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                                   expiry_date=expiry_compact, strike_count=scan_count,
                                   with_quotes=True)
        if resp.get("status") != "success" or not resp.get("chain"):
            raise RuntimeError(f"optionchain() failed for expiry {expiry_compact}: {resp}")

        premium_by_strike: dict[float, float] = {}
        for row in resp["chain"]:
            leg = row.get(side_key)
            if not leg or not leg.get("ltp") or not leg.get("bid") or not leg.get("ask"):
                continue   # require a genuine two-sided market
            premium_by_strike[float(row["strike"])] = float(leg["ltp"])

        scanned = []
        for step in range(1, config.max_otm_steps + 1):
            strike = atm + direction * step * config.main_strike_round
            px = premium_by_strike.get(strike)
            if px is None or px <= 0:
                continue
            in_band = config.premium_band_low <= px <= config.premium_band_high
            scanned.append({"strike": strike, "premium": px, "in_band": in_band,
                             "diff": abs(px - config.premium_target)})
        if any(c["in_band"] for c in scanned):
            break   # found a genuine in-band candidate -- no need for a wider (costlier) scan

    if not scanned:
        return None
    pool = [c for c in scanned if c["in_band"]] or scanned
    return min(pool, key=lambda c: c["diff"])


def select_hedge_strike(client, expiry_compact: str, side: str, sold_strike: float) -> Optional[dict]:
    """Strike-DISTANCE ranking (abs(strike - target_strike)), NOT premium --
    unlike select_main_strike above. target_strike is ~2% away from
    `sold_strike`, always applied against the ORIGINAL sold strike (fixed at
    entry -- callers must pass pos.short_strike unchanged on every
    recurring roll, never the market's current level). Candidates are
    restricted to round config.hedge_strike_round (100-pt) multiples --
    NIFTY lists 50-pt strikes near spot, but the hedge must sit on the same
    100-pt grid as the main leg's own 500-pt strikes, not an arbitrary
    50-pt one.

    *** Important divergence from the OI Positional donor's own
    select_hedge_strike(): there, the sold leg and hedge share the SAME
    monthly expiry, so reusing one optionchain() response from the short-
    strike scan is a real optimization. HERE the main leg is quarterly and
    the hedge is monthly -- always DIFFERENT contracts, so this function
    always fetches its own fresh optionchain() call. Do not "optimize" this
    into reusing select_main_strike()'s chain response -- that would be
    querying the wrong expiry entirely. ***"""
    direction = 1 if side == "CE" else -1
    target_strike = sold_strike * (1 + direction * config.hedge_pct / 100.0)

    resp = client.optionchain(underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                               expiry_date=expiry_compact, strike_count=30, with_quotes=True)
    if resp.get("status") != "success" or not resp.get("chain"):
        Log.warning(f"[HEDGE_{side}] optionchain() failed for expiry {expiry_compact}: {resp}")
        return None
    legs = _legs_with_strike(resp, side)
    # Require a genuine two-sided market -- same guard as select_main_strike().
    # Without this, a stale/zero ltp on an illiquid far-OTM strike could be
    # bought at MARKET price with no real quote at all.
    legs = [l for l in legs if l.get("ltp") and l.get("bid") and l.get("ask")]
    if side == "CE":
        candidates = [l for l in legs if l["strike"] > sold_strike]
    else:
        candidates = [l for l in legs if l["strike"] < sold_strike]
    # Hedge strike must be a round-100 multiple -- NIFTY lists 50-pt strikes
    # near spot but this leg is meant to sit on the same 100-pt grid as the
    # main leg's own 500-pt-multiple strikes, not an arbitrary 50-pt one.
    candidates = [l for l in candidates if l["strike"] % config.hedge_strike_round == 0]
    if not candidates:
        Log.warning(f"[HEDGE_{side}] no candidate strictly beyond sold strike {sold_strike} "
                    f"on a {config.hedge_strike_round}-pt strike with a usable premium -- "
                    f"no hedge available.")
        return None
    return min(candidates, key=lambda l: abs(l["strike"] - target_strike))


###############################################################################
# RSI SIGNAL -- 1-hour NIFTY spot candles
###############################################################################
def _current_hourly_candle_boundary() -> datetime:
    """Start-of-bucket timestamp for the current wall-clock 1-hour candle,
    anchored to the SESSION OPEN (09:15 IST), not midnight. See
    AUTHORING_CHECKLIST.md's "candle-boundary caching" section: a 60-minute
    bucket does NOT evenly divide 555 minutes-from-midnight (unlike a native
    5m/15m interval), so midnight-anchoring would silently disagree with the
    true 09:15/10:15/11:15... candle boundaries this strategy's 1h bars
    actually close on. Purely a cheap gate for WHEN to call history() --
    the RSI computation itself still drops the still-forming last bar off
    whatever the broker actually returns, regardless of this function."""
    now = datetime.now(IST)
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < session_start:
        return session_start
    elapsed = now - session_start
    interval = timedelta(hours=1)
    buckets = int(elapsed // interval)
    return session_start + buckets * interval


def _get_rsi_signal(client) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Fetch client.history(interval="1h") for NIFTY spot, unconditionally
    drop the still-forming last bar, compute RSI(14) over the closed bars.
    Returns (cur_rsi, prev_rsi, candle_key) -- prev_rsi is the PREVIOUS
    closed bar's own RSI (needed for a genuine cross-detection, not just
    "RSI happens to sit past the threshold" -- see _crossed_below/above
    below), read directly off the same freshly-fetched array rather than
    trusting a persisted value across a restart. One call per new candle --
    never re-fetched mid-hour (gated by _new_candle_closed() in the
    caller)."""
    end = datetime.now(IST).date()
    start = end - timedelta(days=config.history_lookback_days)
    try:
        bars = client.history(symbol=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                               interval=config.intraday_interval,
                               start_date=start.isoformat(), end_date=end.isoformat())
    except Exception as exc:
        Log.warning(f"history() failed for RSI signal: {exc}")
        return None, None, None
    if _is_error_response(bars):
        Log.warning(f"history() error response for RSI signal: {bars}")
        return None, None, None
    if bars is None or bars.empty:
        Log.warning("history() returned no data for RSI signal.")
        return None, None, None
    if len(bars) >= 2:
        bars = bars.iloc[:-1]
    if len(bars) < config.rsi_period + 2:
        Log.warning(f"Only {len(bars)} closed {config.intraday_interval} bars after dropping the "
                    f"still-forming one (need >= {config.rsi_period + 2} for a stable RSI) -- no signal.")
        return None, None, None

    close = bars["close"].to_numpy(dtype=float)
    rsi = np.asarray(ta.rsi(close, config.rsi_period))
    candle_key = str(bars.index[-1])
    cur_rsi = float(rsi[-1])
    prev_rsi = float(rsi[-2]) if len(rsi) >= 2 else None
    Log.info(f"[RSI] candle={candle_key} close={close[-1]:.2f} rsi={cur_rsi:.2f} "
             f"(prev={prev_rsi if prev_rsi is None else round(prev_rsi, 2)})")
    return cur_rsi, prev_rsi, candle_key


def _crossed_below(prev_rsi: Optional[float], cur_rsi: float, threshold: float) -> bool:
    return prev_rsi is not None and prev_rsi >= threshold and cur_rsi < threshold


def _crossed_above(prev_rsi: Optional[float], cur_rsi: float, threshold: float) -> bool:
    return prev_rsi is not None and prev_rsi <= threshold and cur_rsi > threshold


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
    the timeout. See AUTHORING_CHECKLIST.md section 3 for why this must
    never hit FLASK_PORT/openalgo.sock."""
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


def push_leg_error(env: "Environment", leg_key: str, pos: "LeapsPosition",
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

        self._leaps_expiry_cache: Optional[tuple] = None    # (today_iso, compact, expiry_date)
        self._last_candle_boundary: Optional[datetime] = None

        # poll_fill() can block for a long time in a reprice loop -- order
        # confirmation always runs on a background executor, never inline
        # in run_cycle(). Single-slot state machine (at most one position at
        # a time, guarded by _pending_fills), so 2 workers is generous
        # headroom, not a hard requirement.
        self._fill_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fill-watch")
        # Separate, single-worker pool for check_force_exit/push_leg_error/
        # notify_trade_closed/check_pending_action's HTTP calls -- kept off
        # _fill_executor so these can never queue silently behind a fill
        # watcher stuck for minutes in a reprice loop.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")
        # client.history() is a real network call, not a cheap check --
        # dispatched here rather than run inline on the scheduler thread so
        # a slow/hanging history() call can never delay the next
        # force-exit / hedge-roll / expiry-day-close check.
        self._rsi_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rsi")
        self._rsi_fetch_pending: bool = False
        self._state_lock = threading.Lock()
        # Contains "position" while an entry/exit/hedge-roll order chain is
        # in flight on _fill_executor -- run_cycle checks this before
        # dispatching anything new for the leg.
        self._pending_fills: set = set()
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        self._last_error_push_at: Optional[datetime] = None

    def save_state(self):
        with self._state_lock:
            self.state_store.save()

    # -------------------------------------------------------------------
    # Data helpers
    # -------------------------------------------------------------------
    def get_spot_ltp(self) -> Optional[float]:
        ltp = self.price_stream.get_ltp(UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        return fetch_symbol_ltp(self.client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE)

    def get_leaps_expiry(self) -> tuple[str, date]:
        today = datetime.now(IST).date()
        today_iso = today.isoformat()
        if self._leaps_expiry_cache and self._leaps_expiry_cache[0] == today_iso:
            return self._leaps_expiry_cache[1], self._leaps_expiry_cache[2]
        result = resolve_leaps_expiry(self.client, today)
        if result is None:
            raise RuntimeError(
                "resolve_leaps_expiry() returned None -- no quarterly-dated contract resolvable "
                "that far out from the broker's expiry calendar. Treating as a hard failure; "
                "no entry will be attempted this cycle."
            )
        compact, chosen_date = result
        self._leaps_expiry_cache = (today_iso, compact, chosen_date)
        Log.info(f"LEAPS quarterly expiry resolved: {chosen_date} ({compact})")
        return compact, chosen_date

    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily state.")
            self.state.current_day = today_key
            self.state.today_realized_pnl = 0.0
            self._leaps_expiry_cache = None
            self.save_state()

    def _new_candle_closed(self) -> bool:
        boundary = _current_hourly_candle_boundary()
        if self._last_candle_boundary is None:
            self._last_candle_boundary = boundary
            # Restart recovery: a persisted last_candle_key means this
            # process has run before and may have missed evaluating
            # whatever candle closed most recently during the downtime --
            # force an immediate RSI re-check rather than silently waiting
            # up to an hour for the next boundary change. Harmless if
            # nothing actually changed since the restart (the freshly
            # fetched RSI would be identical to what was last processed,
            # so no fresh cross is detected and no action is taken).
            return bool(self.state.last_candle_key)
        if self._last_candle_boundary != boundary:
            self._last_candle_boundary = boundary
            return True
        return False

    # -------------------------------------------------------------------
    # Platform integration (backgrounded)
    # -------------------------------------------------------------------
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

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state -- re-pushes at most once per
        config.error_repush_interval_sec while the position remains in
        error_state, so a single lost push self-heals within a minute.
        Called unconditionally at the top of run_cycle(), every cycle."""
        pos = self.state.position
        if not pos.error_state:
            self._last_error_push_at = None
            return
        now = datetime.now(IST)
        if (self._last_error_push_at is not None
                and (now - self._last_error_push_at).total_seconds() < config.error_repush_interval_sec):
            return
        self._last_error_push_at = now
        is_hedge = pos.error_state in ("hedge_entry_failed", "hedge_exit_failed")
        is_exit = pos.error_state in ("exit_failed", "hedge_exit_failed")
        buy_action = "BUY" if is_hedge else "SELL"
        sell_action = "SELL" if is_hedge else "BUY"
        action = sell_action if is_exit else buy_action
        self._push_leg_error_bg(LEG_KEY, pos, action=action)

    def _push_leg_error_bg(self, leg_key: str, pos: "LeapsPosition", action: str = "", clear: bool = False):
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
            Log.warning(f"Failed to dispatch Telegram error notification: {exc}")

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

    def _resolve_pending_error_if_any(self):
        pos = self.state.position
        if not pos.error_state:
            return
        self._refresh_pending_action_bg(LEG_KEY)
        pending = self._pop_pending_action(LEG_KEY)
        if pending is not None:
            self._resolve_leg_error(pending)

    # -------------------------------------------------------------------
    # Entry
    # -------------------------------------------------------------------
    def _enter_position(self, side: str):
        # Check-then-set must be atomic: _finalize_exit() (fill-watcher
        # thread) can call this for a same-bar reversal at the same moment
        # a concurrent run_cycle() tick (scheduler thread) also reaches
        # here -- without a lock both could pass the checks and each
        # submit an independent worker, placing two real orders.
        with self._state_lock:
            if "position" in self._pending_fills:
                return
            if self.state.position.side:
                return  # defensive no-op -- callers already check flat
            self._pending_fills.add("position")
        self._fill_executor.submit(self._enter_position_worker, side)

    def _enter_position_worker(self, side: str):
        handed_off = False
        try:
            with self._state_lock:
                if self.state.position.side:
                    Log.warning("Entry: position already open by the time this worker ran -- "
                                "aborting to avoid a duplicate order.")
                    return
            try:
                expiry_compact, expiry_date = self.get_leaps_expiry()
            except RuntimeError as exc:
                Log.exception(f"Entry: LEAPS quarterly expiry resolution failed: {exc}")
                self._notify_telegram_error_bg(f"Entry blocked (main expiry): {exc}")
                return

            # RSI condition is already met (that's why we're here) -- if no
            # qualifying strike is found on the first scan, keep re-scanning
            # (fresh spot + optionchain() each time) for up to
            # main_strike_retry_window_sec before giving up until the next
            # hourly candle, rather than abandoning the signal on one miss.
            import time as _time
            deadline = _time.monotonic() + config.main_strike_retry_window_sec
            attempt = 0
            main = None
            had_exception = False
            while True:
                if self._force_exit_pending:
                    Log.warning("Entry: Force Exit requested during the strike-selection retry "
                                "window -- aborting this entry attempt, never placing an order.")
                    return
                attempt += 1
                spot = self.get_spot_ltp()
                if spot is not None:
                    try:
                        main = select_main_strike(self.client, spot, side, expiry_compact)
                    except Exception as exc:
                        # Broad catch, not just RuntimeError -- a real HTTP
                        # timeout (env.timeout=10s) raises a different
                        # exception type that a narrower catch would miss
                        # entirely, silently swallowed by the fire-and-
                        # forget executor submission. Treated as a failed
                        # ATTEMPT (retried within the window), not an
                        # immediate abort -- a single transient blip
                        # shouldn't kill the whole entry when we're already
                        # retrying anyway.
                        had_exception = True
                        Log.warning(f"Entry: main-strike lookup failed on attempt {attempt} "
                                    f"(will retry within the window): {exc}")
                        main = None
                if main is not None:
                    break
                if _time.monotonic() >= deadline:
                    reason = "no strike with a usable premium found" if spot is not None else "spot LTP unavailable"
                    Log.info(f"Entry: {reason} after {attempt} attempt(s) over "
                             f"{config.main_strike_retry_window_sec:.0f}s -- giving up until the next hourly candle.")
                    if had_exception:
                        self._notify_telegram_error_bg(
                            f"Entry ({side}) abandoned after {attempt} attempts over "
                            f"{config.main_strike_retry_window_sec:.0f}s -- repeated main-strike "
                            f"lookup failures (see logs)."
                        )
                    return
                Log.info(f"Entry: attempt {attempt} found no {side} strike (or no spot) -- "
                         f"retrying in {config.main_strike_retry_interval_sec:.0f}s "
                         f"(up to {config.main_strike_retry_window_sec:.0f}s total).")
                _time.sleep(config.main_strike_retry_interval_sec)
            symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(main['strike'])}{side}"

            today = datetime.now(IST).date()
            try:
                hedge_expiry_compact, hedge_expiry_raw = resolve_hedge_monthly_expiry(self.client, today)
            except RuntimeError as exc:
                Log.exception(f"Entry: hedge monthly expiry resolution failed: {exc}")
                self._notify_telegram_error_bg(f"Entry blocked (hedge expiry): {exc}")
                return

            hedge = select_hedge_strike(self.client, hedge_expiry_compact, side, main["strike"])
            if hedge is None:
                Log.warning(f"Entry: qualifying main strike {main['strike']} found but no hedge "
                            f"candidate available -- refusing to sell naked, skipping entry.")
                return
            hedge_symbol = f"{UNDERLYING_SYMBOL}{hedge_expiry_compact}{int(hedge['strike'])}{side}"

            if self._force_exit_pending:
                Log.warning("Entry: Force Exit requested just before order placement -- "
                             "aborting this entry attempt, never placing an order.")
                return

            pos = LeapsPosition(
                side=side, short_symbol=symbol, short_strike=main["strike"], quantity=config.quantity,
                entry_time=datetime.now(IST).isoformat(), entry_px=0.0,
                expiry_date=expiry_date.isoformat(),
                hedge_symbol=hedge_symbol, hedge_strike=hedge["strike"],
                hedge_expiry=datetime.strptime(hedge_expiry_raw, "%d-%b-%y").date().isoformat(),
                execution_id=self.execution_id,
            )
            self.state.position = pos
            self.save_state()

            try:
                order_id = place(self.client, self.env.strategy_tag, symbol, OPTIONS_EXCHANGE, "SELL", config.quantity)
            except Exception as exc:
                Log.exception(f"Entry: short entry placeorder failed for {symbol}: {exc}")
                self._enter_error_mode("", "entry_failed", "terminal", "", str(exc))
                return

            self.state.trade_count += 1
            pos.entry_order_id = order_id
            self.save_state()

            handed_off = True
            self._watch_entry_fill(order_id, symbol, hedge_symbol)
        finally:
            if not handed_off:
                self._pending_fills.discard("position")

    def _watch_entry_fill(self, order_id, symbol, hedge_symbol):
        """Runs on _fill_executor. Confirms the SHORT leg's fill, then --
        still on this same background thread -- places and confirms the
        HEDGE leg's fill immediately after. _pending_fills is held for the
        WHOLE chain (short + hedge), released in the finally below."""
        try:
            try:
                fill = poll_fill(self.client, order_id, self.env.strategy_tag, symbol,
                                  OPTIONS_EXCHANGE, "SELL", config.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(order_id, "entry_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(order_id, "entry_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"Entry: short entry fill-poll failed for {symbol}: {exc}")
                self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
                return

            pos = self.state.position
            if pos.entry_order_id != order_id:
                return  # guard vs. a superseded/stale order
            entry_px = float(fill.get("average_price") or fill.get("price") or 0.0)
            pos.entry_px = entry_px
            pos.entry_filled = True
            self.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
            self.save_state()
            Log.info(f"Short entry filled: {symbol}@{entry_px} qty={config.quantity} side={pos.side}")

            try:
                hedge_order_id = place(self.client, self.env.strategy_tag, hedge_symbol,
                                        OPTIONS_EXCHANGE, "BUY", config.quantity)
            except Exception as exc:
                Log.exception(f"Entry: hedge entry placeorder failed for {hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_entry_failed", "terminal", "", str(exc))
                return

            pos.hedge_entry_order_id = hedge_order_id
            self.save_state()

            try:
                hedge_fill = poll_fill(self.client, hedge_order_id, self.env.strategy_tag,
                                        hedge_symbol, OPTIONS_EXCHANGE, "BUY", config.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"Entry: hedge entry fill-poll failed for {hedge_symbol}: {exc}")
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "resting", hedge_order_id, str(exc))
                return

            pos = self.state.position
            if pos.hedge_entry_order_id != hedge_order_id:
                return
            hedge_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
            pos.hedge_entry_px = hedge_px
            pos.hedge_entry_filled = True
            self.price_stream.add_instruments([{"symbol": hedge_symbol, "exchange": OPTIONS_EXCHANGE}])
            self.save_state()
            Log.info(f"Hedge entry filled: {hedge_symbol}@{hedge_px} qty={config.quantity} -- position fully open.")
        finally:
            self._pending_fills.discard("position")

    # -------------------------------------------------------------------
    # Exit -- short leg first (unbounded/naked risk), then hedge leg
    # (bounded risk), per AUTHORING_CHECKLIST.md's close-ordering rule.
    # -------------------------------------------------------------------
    def _exit_position(self, reason: str):
        # Check-then-set must be atomic -- _check_hedge_roll_due() (scheduler
        # thread) and an RSI-triggered exit (_rsi_executor thread) can both
        # evaluate "position" not in _pending_fills at the same moment and
        # each submit a competing job against the same live position.
        with self._state_lock:
            pos = self.state.position
            if not pos.side or pos.error_state:
                return
            if "position" in self._pending_fills:
                return
            if pos.short_exit_order_id and not pos.short_exit_filled:
                return  # already in flight
            self._pending_fills.add("position")
        self._fill_executor.submit(self._exit_position_worker, reason)

    def _exit_position_worker(self, reason: str):
        pos = self.state.position
        pos.pending_exit_reason = reason
        self.save_state()

        if pos.short_exit_filled:
            # The short leg is already confirmed closed -- e.g. resuming
            # after a Cancel reset only the hedge leg. Never re-place an
            # already-filled short exit; go straight to the hedge leg.
            self._place_and_watch_hedge_exit(pos)
            return

        try:
            order_id = place(self.client, self.env.strategy_tag, pos.short_symbol,
                              OPTIONS_EXCHANGE, "BUY", pos.quantity)
        except Exception as exc:
            Log.exception(f"Exit: short exit placeorder failed for {pos.short_symbol}: {exc}")
            self._enter_error_mode("", "exit_failed", "terminal", "", str(exc))
            self._pending_fills.discard("position")
            return

        pos.short_exit_order_id = order_id
        pos.short_exit_filled = False
        self.save_state()
        self._watch_short_exit_fill(order_id, pos.short_symbol, pos.quantity)

    def _watch_short_exit_fill(self, order_id, symbol, quantity):
        """Confirms the SHORT leg's exit fill, then hands off to
        _place_and_watch_hedge_exit() for the hedge leg. On full success,
        _finalize_exit() is called DIRECTLY from that same background
        thread (not waiting for the next run_cycle tick) -- this is what
        makes the same-bar RSI reversal re-entry immediate rather than
        delayed up to an hour (plan requirement)."""
        pos = self.state.position
        try:
            fill = poll_fill(self.client, order_id, self.env.strategy_tag, symbol,
                              OPTIONS_EXCHANGE, "BUY", quantity)
        except OrderNeedsAttention as exc:
            self._enter_error_mode(order_id, "exit_failed", "resting", exc.order_id, str(exc))
            self._pending_fills.discard("position")
            return
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(order_id, "exit_failed", "terminal", "", str(exc))
            self._pending_fills.discard("position")
            return
        except Exception as exc:
            Log.exception(f"Exit: short exit fill-poll failed for {symbol}: {exc}")
            self._enter_error_mode(order_id, "exit_failed", "resting", order_id, str(exc))
            self._pending_fills.discard("position")
            return

        if pos.short_exit_order_id != order_id:
            self._pending_fills.discard("position")
            return
        pos.short_exit_fill_px = float(fill.get("average_price") or fill.get("price") or 0.0)
        pos.short_exit_filled = True
        self.save_state()
        Log.info(f"Exit: short leg filled {symbol}@{pos.short_exit_fill_px} reason={pos.pending_exit_reason}")

        self._place_and_watch_hedge_exit(pos)

    def _place_and_watch_hedge_exit(self, pos: "LeapsPosition"):
        """Places and confirms the HEDGE leg's exit (the short leg is
        already confirmed filled by the time this is called, whether from
        _watch_short_exit_fill's normal flow or from a Cancel-triggered
        resume where only the hedge leg needed re-attempting). Releases
        _pending_fills in the finally below regardless of outcome."""
        handed_off = False
        try:
            if not pos.hedge_symbol:
                # A prior hedge roll failure can leave the position open but
                # deliberately unhedged (see _do_hedge_roll) -- there is
                # nothing to exit on this leg, so finalize directly rather
                # than placing an order against an empty symbol.
                Log.warning("Exit: position has no hedge leg (previously left unhedged) -- "
                            "finalizing on the short leg alone.")
                pos.hedge_exit_fill_px = pos.hedge_entry_px
                pos.hedge_exit_filled = True
                self.save_state()
                handed_off = True
                self._finalize_exit(pos, pos.pending_exit_reason)
                return

            try:
                hedge_order_id = place(self.client, self.env.strategy_tag, pos.hedge_symbol,
                                        OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"Exit: hedge exit placeorder failed for {pos.hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_exit_failed", "terminal", "", str(exc))
                return

            pos.hedge_exit_order_id = hedge_order_id
            pos.hedge_exit_filled = False
            self.save_state()

            handed_off = True
            self._watch_hedge_exit_fill(hedge_order_id, pos.hedge_symbol, pos.quantity)
        finally:
            if not handed_off:
                self._pending_fills.discard("position")

    def _watch_hedge_exit_fill(self, order_id, symbol, quantity):
        """Confirms the HEDGE leg's exit fill. The order has already been
        placed by the caller (either a fresh placeorder from
        _place_and_watch_hedge_exit, or an existing resting order being
        resumed by a Retry) -- this only polls/confirms it. Releases
        _pending_fills in the finally below regardless of outcome."""
        pos = self.state.position
        handed_off = False
        try:
            try:
                hedge_fill = poll_fill(self.client, order_id, self.env.strategy_tag, symbol,
                                        OPTIONS_EXCHANGE, "SELL", quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(order_id, "hedge_exit_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(order_id, "hedge_exit_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"Exit: hedge exit fill-poll failed for {symbol}: {exc}")
                self._enter_error_mode(order_id, "hedge_exit_failed", "resting", order_id, str(exc))
                return

            if pos.hedge_exit_order_id != order_id:
                return
            pos.hedge_exit_fill_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
            pos.hedge_exit_filled = True
            self.save_state()

            handed_off = True
            self._finalize_exit(pos, pos.pending_exit_reason)
        finally:
            if not handed_off:
                self._pending_fills.discard("position")

    def _finalize_exit(self, pos: "LeapsPosition", reason: str):
        """Computes realized PnL, logs both legs, clears the position, and
        -- if a same-bar reversal set pending_reentry_side -- immediately
        opens the new side using this same cycle's already-determined
        direction (no re-fetch/re-check of the RSI cross condition; only
        the entry's own strike/expiry selection reads fresh live data,
        which it must do regardless of how it was triggered)."""
        short_exit_px = pos.short_exit_fill_px if pos.short_exit_fill_px is not None else pos.entry_px
        hedge_exit_px = pos.hedge_exit_fill_px if pos.hedge_exit_fill_px is not None else pos.hedge_entry_px

        short_pnl = (pos.entry_px - short_exit_px) * pos.quantity
        hedge_pnl = (hedge_exit_px - pos.hedge_entry_px) * pos.quantity
        total_pnl = short_pnl + hedge_pnl
        self.state.today_realized_pnl += total_pnl

        now_iso = datetime.now(IST).isoformat()
        append_trade_log(self.env.strategy_tag, LEG_KEY, pos.short_symbol, pos.quantity,
                          pos.entry_time, pos.entry_px, now_iso, short_exit_px, reason,
                          pos.execution_id, is_short=True)
        append_trade_log(self.env.strategy_tag, f"{LEG_KEY}_HEDGE", pos.hedge_symbol, pos.quantity,
                          pos.entry_time, pos.hedge_entry_px, now_iso, hedge_exit_px, reason,
                          pos.execution_id, is_short=False)
        Log.info(f"Position closed: side={pos.side} short={pos.short_symbol} hedge={pos.hedge_symbol} "
                 f"reason={reason} short_pnl={short_pnl:.2f} hedge_pnl={hedge_pnl:.2f} total_pnl={total_pnl:.2f}")

        self.price_stream.remove_instruments([
            {"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE},
            {"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE},
        ])
        self._notify_trade_closed_bg()

        reentry_side = pos.pending_reentry_side
        self.state.position = LeapsPosition()
        self.save_state()

        # Release the exit chain's hold on _pending_fills NOW -- before
        # potentially dispatching the same-bar re-entry below, since
        # _enter_position() re-acquires this same guard. Must happen from
        # THIS fill-watcher thread the moment the old side is confirmed
        # flat, not wait for the next scheduler tick / hourly boundary
        # (plan's same-bar-reversal requirement).
        self._pending_fills.discard("position")

        if reentry_side and not self._force_exit_pending:
            Log.info(f"Same-bar reversal: immediately entering {reentry_side} using this cycle's signal.")
            self._enter_position(reentry_side)
        elif reentry_side:
            Log.warning(f"Same-bar reversal to {reentry_side} suppressed -- a Force Exit is pending "
                        f"and must not be defeated by a fresh entry.")

    # -------------------------------------------------------------------
    # RSI signal fetch -- backgrounded (client.history() is a real network
    # call, never run inline on the scheduler thread)
    # -------------------------------------------------------------------
    def _rsi_signal_worker(self):
        try:
            cur_rsi, prev_rsi, candle_key = _get_rsi_signal(self.client)
            if cur_rsi is not None:
                self.state.last_rsi_prev = cur_rsi
                self.state.last_candle_key = candle_key or ""
                self.save_state()
                self._process_new_candle(cur_rsi, prev_rsi)
        except Exception as exc:
            Log.exception(f"RSI signal fetch/processing failed: {exc}")
        finally:
            self._rsi_fetch_pending = False

    # -------------------------------------------------------------------
    # RSI signal -> single-slot state machine control flow
    # -------------------------------------------------------------------
    def _process_new_candle(self, cur_rsi: float, prev_rsi: Optional[float]):
        pos = self.state.position
        side = pos.side

        exit_condition = False
        if side == "PE":
            exit_condition = _crossed_below(prev_rsi, cur_rsi, config.rsi_bear_threshold)
        elif side == "CE":
            exit_condition = _crossed_above(prev_rsi, cur_rsi, config.rsi_bull_threshold)

        if side and exit_condition:
            new_side = "CE" if side == "PE" else "PE"
            pos.pending_reentry_side = new_side
            self.save_state()
            self._exit_position(reason="rsi_reversal")
            return   # new-side entry happens from _finalize_exit, once flat

        if not side:
            if cur_rsi > config.rsi_bull_threshold:
                self._enter_position("PE")
            elif cur_rsi < config.rsi_bear_threshold:
                self._enter_position("CE")

    # -------------------------------------------------------------------
    # Monthly hedge rollover (18th of month, holiday-shifted)
    # -------------------------------------------------------------------
    def _check_hedge_roll_due(self):
        pos = self.state.position
        if not pos.side or pos.error_state:
            return
        today = datetime.now(IST).date()
        roll_date = hedge_roll_date_for_month(today.year, today.month)
        if today != roll_date:
            return
        if pos.last_hedge_roll_date == today.isoformat():
            return
        # Check-then-set must be atomic -- an RSI-triggered _exit_position()
        # (on _rsi_executor's thread) could otherwise pass its own
        # _pending_fills check at the same moment as this one, and both
        # submit competing jobs against the same live position.
        with self._state_lock:
            if "position" in self._pending_fills:
                return
            self._pending_fills.add("position")
        self._fill_executor.submit(self._do_hedge_roll)

    def _do_hedge_roll(self):
        """Closes the current hedge, then opens a fresh one for the next
        month (rule 8's day>15 threshold, re-applied at the roll date --
        18 > 15 always, so this always resolves NEXT month's contract),
        re-targeted ~2% from the ORIGINAL sold strike (pos.short_strike,
        never today's spot). Runs entirely on _fill_executor; releases
        _pending_fills in the finally below regardless of outcome."""
        pos = self.state.position
        today = datetime.now(IST).date()
        try:
            if not pos.side:
                return
            if not pos.hedge_symbol:
                # Already unhedged (a prior roll failure) -- mark today's
                # roll as handled regardless, or _check_hedge_roll_due()
                # would resubmit this to _fill_executor on every ~15s tick
                # for the rest of the day.
                Log.warning("Hedge roll due but position is already unhedged -- skipping this roll.")
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                return

            try:
                order_id = place(self.client, self.env.strategy_tag, pos.hedge_symbol,
                                  OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"Hedge roll: exit placeorder failed for {pos.hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_exit_failed", "terminal", "", str(exc))
                return
            try:
                fill = poll_fill(self.client, order_id, self.env.strategy_tag, pos.hedge_symbol,
                                  OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(order_id, "hedge_exit_failed", "resting", exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(order_id, "hedge_exit_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"Hedge roll: exit fill-poll failed for {pos.hedge_symbol}: {exc}")
                self._enter_error_mode(order_id, "hedge_exit_failed", "resting", order_id, str(exc))
                return

            old_hedge_symbol = pos.hedge_symbol
            old_hedge_entry_px = pos.hedge_entry_px
            exit_px = float(fill.get("average_price") or fill.get("price") or 0.0)
            roll_pnl = (exit_px - old_hedge_entry_px) * pos.quantity
            self.state.today_realized_pnl += roll_pnl
            now_iso = datetime.now(IST).isoformat()
            append_trade_log(self.env.strategy_tag, f"{LEG_KEY}_HEDGE", old_hedge_symbol, pos.quantity,
                              pos.entry_time, old_hedge_entry_px, now_iso, exit_px, "hedge_roll",
                              pos.execution_id, is_short=False)
            self.price_stream.remove_instruments([{"symbol": old_hedge_symbol, "exchange": OPTIONS_EXCHANGE}])
            Log.info(f"Hedge roll: closed old hedge {old_hedge_symbol}@{exit_px} pnl={roll_pnl:.2f}")

            try:
                expiry_compact, expiry_raw = resolve_hedge_monthly_expiry(self.client, today)
            except RuntimeError as exc:
                Log.exception(f"Hedge roll: could not resolve next hedge expiry: {exc}")
                pos.hedge_symbol = ""
                pos.hedge_strike = 0.0
                pos.hedge_expiry = ""
                pos.hedge_entry_order_id = ""
                pos.hedge_entry_filled = False
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                self._notify_telegram_error_bg(
                    f"Hedge roll: closed old hedge but failed to resolve next expiry: {exc} -- "
                    f"position now UNHEDGED, needs manual attention."
                )
                return

            new_hedge = select_hedge_strike(self.client, expiry_compact, pos.side, pos.short_strike)
            if new_hedge is None:
                Log.warning("Hedge roll: no hedge candidate found for the rolled expiry -- "
                            "position left UNHEDGED.")
                pos.hedge_symbol = ""
                pos.hedge_strike = 0.0
                pos.hedge_expiry = ""
                pos.hedge_entry_order_id = ""
                pos.hedge_entry_filled = False
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                self._notify_telegram_error_bg(
                    "Hedge roll: no hedge candidate available -- position now UNHEDGED, "
                    "needs manual attention."
                )
                return

            new_hedge_symbol = f"{UNDERLYING_SYMBOL}{expiry_compact}{int(new_hedge['strike'])}{pos.side}"
            try:
                new_order_id = place(self.client, self.env.strategy_tag, new_hedge_symbol,
                                      OPTIONS_EXCHANGE, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"Hedge roll: new hedge placeorder failed for {new_hedge_symbol}: {exc}")
                pos.hedge_symbol = ""
                pos.hedge_strike = 0.0
                pos.hedge_expiry = ""
                pos.hedge_entry_order_id = ""
                pos.hedge_entry_filled = False
                pos.last_hedge_roll_date = today.isoformat()
                self._enter_error_mode("", "hedge_entry_failed", "terminal", "", str(exc))
                return

            pos.hedge_symbol = new_hedge_symbol
            pos.hedge_strike = new_hedge["strike"]
            pos.hedge_expiry = datetime.strptime(expiry_raw, "%d-%b-%y").date().isoformat()
            pos.hedge_entry_order_id = new_order_id
            pos.hedge_entry_filled = False
            self.save_state()

            try:
                new_fill = poll_fill(self.client, new_order_id, self.env.strategy_tag, new_hedge_symbol,
                                      OPTIONS_EXCHANGE, "BUY", pos.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(new_order_id, "hedge_entry_failed", "resting", exc.order_id, str(exc))
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(new_order_id, "hedge_entry_failed", "terminal", "", str(exc))
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                return
            except Exception as exc:
                Log.exception(f"Hedge roll: new hedge fill-poll failed for {new_hedge_symbol}: {exc}")
                self._enter_error_mode(new_order_id, "hedge_entry_failed", "resting", new_order_id, str(exc))
                pos.last_hedge_roll_date = today.isoformat()
                self.save_state()
                return

            pos.hedge_entry_px = float(new_fill.get("average_price") or new_fill.get("price") or 0.0)
            pos.hedge_entry_filled = True
            pos.last_hedge_roll_date = today.isoformat()
            self.price_stream.add_instruments([{"symbol": new_hedge_symbol, "exchange": OPTIONS_EXCHANGE}])
            self.save_state()
            Log.info(f"Hedge roll complete: new hedge {new_hedge_symbol}@{pos.hedge_entry_px}")
        finally:
            self._pending_fills.discard("position")

    # -------------------------------------------------------------------
    # Error recovery -- Retry/Cancel/Manually-Completed, per
    # docs/prd/python-strategies-order-error-recovery.md
    # -------------------------------------------------------------------
    def _enter_error_mode(self, order_id, error_state, error_kind, error_order_id, message):
        pos = self.state.position
        known_ids = {pos.entry_order_id, pos.hedge_entry_order_id,
                     pos.short_exit_order_id, pos.hedge_exit_order_id}
        if order_id and order_id not in known_ids:
            return  # superseded/stale order -- don't overwrite a newer attempt's state
        pos.error_state = error_state
        pos.error_kind = error_kind
        pos.error_order_id = error_order_id
        pos.error_message = message
        pos.error_since = datetime.now(IST).isoformat()
        action = {"entry_failed": "SELL", "hedge_entry_failed": "BUY",
                  "exit_failed": "BUY", "hedge_exit_failed": "SELL"}.get(error_state, "")
        self._push_leg_error_bg(LEG_KEY, pos, action=action)
        self.save_state()
        Log.error(f"{error_state} ({error_kind}): {message}")
        self._notify_telegram_error_bg(f"{error_state} ({error_kind}): {message}")

    def _resolve_leg_error(self, action: dict):
        """Applies a Retry/Cancel/Manually-Completed decision pulled from
        the platform. `error_state` tells us which of the four independent
        order lifecycles (short entry, hedge entry, short exit, hedge exit)
        is stuck -- resolved generically via each state's own action
        direction and symbol/quantity."""
        with self._state_lock:
            if "position" in self._pending_fills:
                return
        pos = self.state.position
        error_state = pos.error_state
        is_exit = error_state in ("exit_failed", "hedge_exit_failed")
        is_hedge = error_state in ("hedge_entry_failed", "hedge_exit_failed")
        symbol = pos.hedge_symbol if is_hedge else pos.short_symbol
        buy_action = "BUY" if is_hedge else "SELL"    # hedge entry=BUY, short entry=SELL
        sell_action = "SELL" if is_hedge else "BUY"   # hedge exit=SELL, short exit=BUY
        order_action = sell_action if is_exit else buy_action
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fills.add("position")
            ack_pending_action(self.env, LEG_KEY)
            self._fill_executor.submit(
                self._do_retry_resolution, error_state, kind, symbol, order_action, pos.quantity
            )
            return

        if action["action"] == "cancel":
            if is_exit:
                if kind == "terminal":
                    # Order was rejected outright -- nothing live at the
                    # broker to cancel. Reset this leg to "not yet
                    # attempted" so the next exit trigger resumes cleanly
                    # (never re-placing an already-filled leg, per
                    # _exit_position_worker's/_finalize_exit's guards).
                    if is_hedge:
                        pos.hedge_exit_order_id = ""
                        pos.hedge_exit_filled = False
                    else:
                        pos.short_exit_order_id = ""
                        pos.short_exit_filled = False
                    pos.error_state = ""
                    pos.error_kind = ""
                    pos.error_order_id = ""
                    self.save_state()
                    self._push_leg_error_bg(LEG_KEY, pos, clear=True)
                    ack_pending_action(self.env, LEG_KEY)
                    return
                # kind == "resting" -- the order may still fill at any
                # moment; give it one last chance before actually
                # cancelling at the broker (same pattern as the entry
                # side's _watch_cancel_final_chance).
                ack_pending_action(self.env, LEG_KEY)
                self._pending_fills.add("position")
                self._fill_executor.submit(
                    self._watch_exit_cancel_final_chance, pos.error_order_id, symbol,
                    pos.quantity, order_action, error_state
                )
                return
            if kind == "terminal":
                if error_state == "entry_failed":
                    # The short leg never entered -- the whole attempted
                    # position is abandoned (no hedge was ever placed yet).
                    self.price_stream.remove_instruments(
                        [{"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE}]
                    )
                    self.state.position = LeapsPosition()
                else:
                    # Hedge entry never filled -- the short leg is already
                    # live; leave it open (don't discard the whole position)
                    # but clear ALL hedge fields, not just the order id, so
                    # the position accurately shows as unhedged rather than
                    # "looks hedged" with nothing actually live at the
                    # broker (a later exit/roll must never place a SELL
                    # against a leg that was never bought).
                    pos.hedge_symbol = ""
                    pos.hedge_strike = 0.0
                    pos.hedge_expiry = ""
                    pos.hedge_entry_order_id = ""
                    pos.hedge_entry_filled = False
                    pos.error_state = ""
                    pos.error_kind = ""
                    pos.error_order_id = ""
                    self._notify_telegram_error_bg(
                        "Hedge entry cancelled -- position is now UNHEDGED, needs manual attention."
                    )
                self.save_state()
                self._push_leg_error_bg(LEG_KEY, self.state.position, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            ack_pending_action(self.env, LEG_KEY)
            self._pending_fills.add("position")
            self._fill_executor.submit(
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
                self.save_state()
                self._push_leg_error_bg(LEG_KEY, pos, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                if pos.short_exit_filled and pos.hedge_exit_filled:
                    self._finalize_exit(pos, pos.pending_exit_reason)
                return
            if is_hedge:
                pos.hedge_entry_filled = True
                pos.hedge_entry_px = fill_price
                self.price_stream.add_instruments(
                    [{"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            else:
                pos.entry_filled = True
                pos.entry_px = fill_price
                self.state.trade_count += 1
                self.price_stream.add_instruments(
                    [{"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE}]
                )
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.save_state()
            self._push_leg_error_bg(LEG_KEY, pos, clear=True)
            ack_pending_action(self.env, LEG_KEY)

    def _do_retry_resolution(self, error_state, kind, symbol, order_action, quantity):
        try:
            pos = self.state.position
            if error_state in ("exit_failed", "hedge_exit_failed"):
                is_hedge_leg = error_state == "hedge_exit_failed"
                if kind == "resting":
                    bid, ask = fetch_symbol_bid_ask(self.client, symbol, OPTIONS_EXCHANGE)
                    fresh_price = ask if order_action == "BUY" else bid
                    if fresh_price is not None:
                        try:
                            self.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                symbol=symbol, action=order_action, exchange=OPTIONS_EXCHANGE,
                                price_type="LIMIT", product=config.product, quantity=str(quantity),
                                price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"Retry's reprice failed ({exc}) -- resuming the watcher on the order as-is.")
                    resume_order_id = pos.error_order_id
                else:
                    # kind == "terminal": the previous placeorder was
                    # rejected outright -- nothing resting to resume, place
                    # a fresh order for this leg (mirrors the entry side's
                    # terminal-retry path below).
                    try:
                        resume_order_id = place(self.client, self.env.strategy_tag, symbol,
                                                 OPTIONS_EXCHANGE, order_action, quantity)
                    except Exception as exc:
                        Log.exception(f"Retry's fresh place() failed again: {exc}")
                        self._enter_error_mode(pos.error_order_id, error_state, "terminal", "", str(exc))
                        self._pending_fills.discard("position")
                        return
                    if is_hedge_leg:
                        pos.hedge_exit_order_id = resume_order_id
                        pos.hedge_exit_filled = False
                    else:
                        pos.short_exit_order_id = resume_order_id
                        pos.short_exit_filled = False
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.save_state()
                self._push_leg_error_bg(LEG_KEY, pos, clear=True)
                # Resubmit the appropriate watcher -- "position" stays in
                # _pending_fills (never discarded here) until that watcher's
                # own finally releases it, matching the entry-side pattern
                # below.
                if is_hedge_leg:
                    self._fill_executor.submit(self._watch_hedge_exit_fill, resume_order_id, symbol, quantity)
                else:
                    self._fill_executor.submit(self._watch_short_exit_fill, resume_order_id, symbol, quantity)
                return

            # Entry side -- run_cycle only calls _enter_position() when flat,
            # so Retry directly resumes the appropriate watcher.
            if kind == "resting":
                bid, ask = fetch_symbol_bid_ask(self.client, symbol, OPTIONS_EXCHANGE)
                fresh_price = ask if order_action == "BUY" else bid
                if fresh_price is not None:
                    try:
                        self.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                            symbol=symbol, action=order_action, exchange=OPTIONS_EXCHANGE,
                            price_type="LIMIT", product=config.product, quantity=str(quantity),
                            price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"Retry's reprice failed ({exc}) -- resuming the watcher on the order as-is.")
                resume_order_id = pos.error_order_id
            else:
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, symbol,
                                             OPTIONS_EXCHANGE, order_action, quantity)
                except Exception as exc:
                    Log.exception(f"Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode(pos.entry_order_id, error_state, "terminal", "", str(exc))
                    self._pending_fills.discard("position")
                    return
                if error_state == "hedge_entry_failed":
                    pos.hedge_entry_order_id = resume_order_id
                else:
                    pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.save_state()
            self._push_leg_error_bg(LEG_KEY, pos, clear=True)
            if error_state == "hedge_entry_failed":
                def _watch_hedge_only():
                    try:
                        hedge_fill = poll_fill(self.client, resume_order_id, self.env.strategy_tag,
                                                symbol, OPTIONS_EXCHANGE, "BUY", quantity)
                        p = self.state.position
                        if p.hedge_entry_order_id != resume_order_id:
                            return
                        p.hedge_entry_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
                        p.hedge_entry_filled = True
                        self.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                        self.save_state()
                    except OrderNeedsAttention as exc:
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "resting", exc.order_id, str(exc))
                    except (RuntimeError, TimeoutError) as exc:
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "terminal", "", str(exc))
                    except Exception as exc:
                        Log.exception(f"Retry-resumed hedge fill-poll failed: {exc}")
                        self._enter_error_mode(resume_order_id, "hedge_entry_failed", "resting", resume_order_id, str(exc))
                    finally:
                        self._pending_fills.discard("position")
                self._fill_executor.submit(_watch_hedge_only)
            else:
                self._fill_executor.submit(self._watch_entry_fill, resume_order_id, symbol, pos.hedge_symbol)
        except Exception as exc:
            Log.exception(f"Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard("position")

    def _watch_cancel_final_chance(self, order_id, symbol, quantity, order_action, error_state):
        """Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned."""
        try:
            result = _reprice_and_wait_once(self.client, order_id, self.env.strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, order_action, quantity)
            pos = self.state.position
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
                    self.state.trade_count += 1
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                self.save_state()
                Log.info(f"Entry filled during Cancel's final chance: {symbol}")
            else:
                try:
                    self.client.cancelorder(order_id=order_id, strategy=self.env.strategy_tag)
                except Exception as exc:
                    Log.warning(f"cancelorder failed while abandoning entry ({exc}) -- clearing local "
                                f"position anyway; verify manually at the broker.")
                self.price_stream.remove_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
                if is_hedge:
                    # Never bought -- clear ALL hedge fields, not just the
                    # order id, so the position accurately shows as
                    # unhedged rather than "looks hedged" with nothing
                    # actually live at the broker.
                    pos.hedge_symbol = ""
                    pos.hedge_strike = 0.0
                    pos.hedge_expiry = ""
                    pos.hedge_entry_order_id = ""
                    pos.hedge_entry_filled = False
                    self._notify_telegram_error_bg(
                        "Hedge entry cancelled during Cancel's final chance -- position is now "
                        "UNHEDGED, needs manual attention."
                    )
                else:
                    self.state.position = LeapsPosition()
                self.save_state()
            self._push_leg_error_bg(LEG_KEY, self.state.position, clear=True)
        except Exception as exc:
            Log.exception(f"Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(order_id, error_state, "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard("position")

    def _watch_exit_cancel_final_chance(self, order_id, symbol, quantity, order_action, error_state):
        """Cancel's one-last-chance flow for a still-resting EXIT order
        (short or hedge leg) -- mirrors the entry side's
        _watch_cancel_final_chance. If the order fills anyway during this
        last look, it's a normal fill: resume the exit chain (which is
        idempotent -- _exit_position_worker/_finalize_exit never re-place
        or re-finalize an already-filled leg) rather than finalizing
        blindly. Otherwise cancel it at the broker and reset ONLY this leg
        to a clean not-yet-attempted state, so the next exit trigger
        resumes just this leg."""
        handed_off = False
        try:
            result = _reprice_and_wait_once(self.client, order_id, self.env.strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, order_action, quantity)
            pos = self.state.position
            if pos.error_order_id != order_id:
                return
            is_hedge = error_state == "hedge_exit_failed"
            if result is not None:
                fill_px = float(result.get("average_price") or result.get("price") or 0.0)
                if is_hedge:
                    pos.hedge_exit_filled = True
                    pos.hedge_exit_fill_px = fill_px
                else:
                    pos.short_exit_filled = True
                    pos.short_exit_fill_px = fill_px
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.save_state()
                Log.info(f"Exit filled during Cancel's final chance: {symbol}")
                self._push_leg_error_bg(LEG_KEY, pos, clear=True)
                if pos.short_exit_filled and pos.hedge_exit_filled:
                    handed_off = True
                    self._finalize_exit(pos, pos.pending_exit_reason)
                    return
                # The other leg still needs its own exit -- resume via the
                # normal (idempotent) exit chain rather than re-implementing
                # it here.
                handed_off = True
                self._exit_position_worker(pos.pending_exit_reason)
                return
            try:
                self.client.cancelorder(order_id=order_id, strategy=self.env.strategy_tag)
            except Exception as exc:
                Log.warning(f"cancelorder failed while abandoning exit leg ({exc}) -- "
                            f"clearing local state anyway; verify manually at the broker.")
            if is_hedge:
                pos.hedge_exit_order_id = ""
                pos.hedge_exit_filled = False
            else:
                pos.short_exit_order_id = ""
                pos.short_exit_filled = False
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self.save_state()
            self._push_leg_error_bg(LEG_KEY, self.state.position, clear=True)
            Log.warning(f"Exit leg {symbol} cancelled -- reset for a fresh retry via the next exit trigger.")
        except Exception as exc:
            Log.exception(f"Unexpected error during exit Cancel's final chance: {exc}")
            self._enter_error_mode(order_id, error_state, "resting", order_id, str(exc))
        finally:
            if not handed_off:
                self._pending_fills.discard("position")

    # -------------------------------------------------------------------
    # run_cycle
    # -------------------------------------------------------------------
    def run_cycle(self):
        try:
            self._repush_active_errors()
            self._refresh_force_exit_check_bg()

            if self._force_exit_pending:
                # Resolve any pending Retry/Cancel/Manual decision on the
                # position BEFORE returning -- this branch is the ONLY code
                # path that runs while Force Exit is pending, so it must not
                # deadlock a leg stuck in error_state (AUTHORING_CHECKLIST.md
                # section 1).
                self._resolve_pending_error_if_any()
                if self._force_exit_all():
                    Log.warning("Force Exit complete -- position flat.")
                    self._force_exit_pending = False
                return

            self._reset_day_if_needed()
            if not _within_market_hours():
                return

            pos = self.state.position
            today_iso = datetime.now(IST).date().isoformat()
            expires_today = bool(pos.side and pos.expiry_date == today_iso)
            if expires_today and datetime.now(IST).time() >= config.expiry_day_close_time:
                # Terminal for today regardless -- but still resolve a
                # pending Retry/Cancel/Manual decision every cycle, never
                # just skip/log it (AUTHORING_CHECKLIST.md section 1) --
                # otherwise an errored position on its own expiry day could
                # freeze forever with no way to ever get resolved.
                self._resolve_pending_error_if_any()
                if pos.side and not pos.error_state and "position" not in self._pending_fills:
                    Log.warning("Main leg's own quarterly contract expires today and RSI hasn't "
                                "reversed -- force-closing (expiry-day safety close).")
                    self._exit_position(reason="expiry_day_close")
                return  # terminal for the day -- no new-position search follows

            if pos.error_state:
                self._resolve_pending_error_if_any()
                return  # frozen -- don't evaluate RSI/hedge-roll while unresolved

            self._check_hedge_roll_due()

            if ("position" not in self._pending_fills and not self._rsi_fetch_pending
                    and self._new_candle_closed()):
                self._rsi_fetch_pending = True
                self._rsi_executor.submit(self._rsi_signal_worker)
        except Exception as exc:
            Log.exception(f"run_cycle failed: {exc}")
            now = datetime.now(IST)
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                self._notify_telegram_error_bg(f"Cycle failed: {exc}")

    # -------------------------------------------------------------------
    # Startup reconciliation
    # -------------------------------------------------------------------
    def reconcile_pending_orders(self):
        """Startup-only crash recovery: finds any of the FOUR independent
        order lifecycles (short entry, hedge entry, short exit, hedge exit)
        whose order was placed but never confirmed filled before the
        previous process instance stopped. Called once from main(), before
        the scheduler starts."""
        pos = self.state.position
        if pos.error_state:
            return
        if pos.entry_order_id and not pos.entry_filled:
            self._reconcile_one(pos, pos.entry_order_id, "entry_failed", pos.short_symbol, "SELL")
        elif pos.hedge_entry_order_id and not pos.hedge_entry_filled:
            self._reconcile_one(pos, pos.hedge_entry_order_id, "hedge_entry_failed", pos.hedge_symbol, "BUY")
        elif pos.short_exit_order_id and not pos.short_exit_filled:
            self._reconcile_one(pos, pos.short_exit_order_id, "exit_failed", pos.short_symbol, "BUY")
        elif pos.hedge_exit_order_id and not pos.hedge_exit_filled:
            self._reconcile_one(pos, pos.hedge_exit_order_id, "hedge_exit_failed", pos.hedge_symbol, "SELL")
        elif pos.short_symbol and not pos.entry_order_id and not pos.entry_filled:
            # The narrow crash window: _enter_position_worker persists the
            # position as "attempting entry" (short_symbol set,
            # entry_order_id empty) BEFORE calling place(). Never guess --
            # flag for a human to verify against the broker directly.
            pos.error_state = "entry_failed"
            pos.error_kind = "terminal"
            pos.error_order_id = ""
            pos.error_message = (
                "Restart interrupted this position between recording the attempt and the "
                "broker's placeorder() response -- unknown whether an order/position actually "
                "exists at the broker. Verify manually before choosing Retry (risks a duplicate "
                "if one exists) -- prefer Cancel if nothing was placed, or Manually Completed "
                "with the real fill price if it was."
            )
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, LEG_KEY, pos, action="SELL")
            self.save_state()
            Log.error("reconcile: ambiguous pre-placeorder crash window -- flagged for manual verification.")

    def _reconcile_one(self, pos, order_id, error_state, symbol, action):
        try:
            resp = self.client.orderstatus(order_id=order_id, strategy=self.env.strategy_tag)
        except Exception as exc:
            Log.exception(f"reconcile: orderstatus() failed for {order_id} -- flagging for manual review.")
            pos.error_state = error_state
            pos.error_kind = "resting"
            pos.error_order_id = order_id
            pos.error_message = f"reconcile after restart: orderstatus() failed: {exc}"
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, LEG_KEY, pos, action=action)
            self.save_state()
            return

        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        Log.info(f"reconcile: {error_state} order {order_id} status='{status}' (resuming after restart)")

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
            Log.info(f"reconcile: {error_state} order {order_id} was actually filled @ {fill_px} -- resuming.")
            if pos.short_exit_filled and pos.hedge_exit_filled:
                self._finalize_exit(pos, pos.pending_exit_reason)
            else:
                self.save_state()
            return

        if status in {"rejected", "cancelled", "canceled"}:
            if error_state == "entry_failed":
                self.state.trade_count = max(0, self.state.trade_count - 1)
                self.state.position = LeapsPosition()
                Log.info(f"reconcile: short entry {order_id} was genuinely rejected/cancelled -- "
                         f"clearing the position, safe to re-evaluate fresh.")
            elif error_state == "hedge_entry_failed":
                # Never bought -- clear ALL hedge fields, not just the order
                # id, so the position accurately shows as unhedged rather
                # than "looks hedged" (hedge_symbol still set) with nothing
                # actually live at the broker. A later exit/hedge-roll must
                # see an empty hedge_symbol, or it would place a real SELL
                # order to "close" a leg that was never opened.
                pos.hedge_symbol = ""
                pos.hedge_strike = 0.0
                pos.hedge_expiry = ""
                pos.hedge_entry_order_id = ""
                pos.hedge_entry_filled = False
                Log.warning(f"reconcile: hedge entry {order_id} was genuinely rejected/cancelled -- "
                            f"short leg remains open and UNHEDGED, needs manual attention "
                            f"(no automatic hedge-entry retry exists).")
                self._notify_telegram_error_bg(
                    f"Hedge entry {order_id} rejected/cancelled during restart reconciliation -- "
                    f"position is now UNHEDGED, needs manual attention."
                )
            elif error_state == "exit_failed":
                pos.short_exit_order_id = ""
                pos.short_exit_filled = False
                Log.info(f"reconcile: short exit {order_id} was genuinely rejected/cancelled -- "
                         f"position remains open, will re-evaluate exit conditions normally.")
            else:
                pos.hedge_exit_order_id = ""
                pos.hedge_exit_filled = False
                Log.info(f"reconcile: hedge exit {order_id} was genuinely rejected/cancelled -- "
                         f"short leg already closed, will retry the hedge exit next cycle.")
            self.save_state()
            return

        # Still resting/pending -- flag for a human decision.
        pos.error_state = error_state
        pos.error_kind = "resting"
        pos.error_order_id = order_id
        pos.error_message = f"reconcile after restart: order still '{status}', needs a decision."
        pos.error_since = datetime.now(IST).isoformat()
        push_leg_error(self.env, LEG_KEY, pos, action=action)
        Log.error(f"reconcile: {error_state} order {order_id} still '{status}' after restart -- "
                  f"needs Retry/Cancel/Manually Completed.")
        self.save_state()

    # -------------------------------------------------------------------
    # PnL / Force Exit
    # -------------------------------------------------------------------
    def _open_positions_for_pnl(self) -> list:
        pos = self.state.position
        if not pos.short_symbol or not pos.entry_filled:
            return []
        short_ltp = self.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
        if short_ltp is None:
            short_ltp = pos.entry_px  # last-known fallback -- never fabricate movement
        short_pnl = (pos.entry_px - short_ltp) * pos.quantity

        hedge_pnl = 0.0
        if pos.hedge_symbol and pos.hedge_entry_filled:
            hedge_ltp = self.price_stream.get_ltp(pos.hedge_symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
            if hedge_ltp is None:
                hedge_ltp = pos.hedge_entry_px
            hedge_pnl = (hedge_ltp - pos.hedge_entry_px) * pos.quantity

        return [{
            "leg_key": LEG_KEY, "symbol": pos.short_symbol, "direction": "SHORT",
            "quantity": -pos.quantity, "entry_price": pos.entry_px, "current_price": short_ltp,
            "pnl": short_pnl + hedge_pnl, "entry_time": pos.entry_time, "execution_id": pos.execution_id,
        }]

    def report_pnl_tick(self):
        try:
            report_pnl_to_platform(self.env, self.state.today_realized_pnl, self._open_positions_for_pnl())
        except Exception:
            Log.exception("report_pnl_tick failed")

    def _force_exit_all(self):
        """Force-closes the open position regardless of the strategy's own
        RSI/hedge-roll logic -- idempotent/resumable across cycles. Left
        untouched if already in error_state (Force Exit doesn't override an
        unresolved Retry/Cancel/Manual decision)."""
        pos = self.state.position
        if pos.error_state:
            return False
        if not pos.side:
            if "position" in self._pending_fills:
                # An entry attempt is still in flight (e.g. mid-way through
                # the strike-selection retry window) -- pos.side only gets
                # set once an order is actually placed, so treating an
                # empty side as "already flat" here would falsely ack Force
                # Exit as complete while a fresh SELL could still land
                # moments later. _enter_position_worker's own
                # _force_exit_pending check will abort that attempt; just
                # wait for _pending_fills to clear before declaring done.
                return False
            ack_force_exit_complete(self.env)
            return True
        if (pos.short_exit_order_id and pos.short_exit_filled
                and pos.hedge_exit_order_id and pos.hedge_exit_filled):
            self._finalize_exit(pos, pos.pending_exit_reason)
            ack_force_exit_complete(self.env)
            return True
        if "position" not in self._pending_fills and not pos.short_exit_order_id:
            pos.pending_reentry_side = ""
            self._exit_position(reason="force_exit")
        return False


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
    print(f"RSI period           : {config.rsi_period}")
    print(f"RSI bull/bear thresh : > {config.rsi_bull_threshold} sell PE, < {config.rsi_bear_threshold} sell CE")
    print(f"Main strike round    : {config.main_strike_round} pts, premium target Rs {config.premium_target} "
          f"(band {config.premium_band_low}-{config.premium_band_high})")
    print(f"Hedge target         : ~{config.hedge_pct}% from ORIGINAL sold strike, rolls monthly on day "
          f"{config.hedge_roll_dom} (holiday-shifted back)")
    print(f"Quantity             : {config.quantity}")
    print(f"Product              : {config.product}")
    print("SINGLE-SLOT RSI-driven QUARTERLY option seller, protected by a separately-rolling MONTHLY hedge.")
    print("NO news/event filter. NO premium-based stop-loss. NO daily overnight-gap hedge.")
    if config.test_mode:
        print("TEST MODE ENABLED -- market-hours checks are BYPASSED")
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

    already_known = [{"symbol": UNDERLYING_SYMBOL, "exchange": UNDERLYING_SPOT_EXCHANGE}]
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        pos = state_store.state.position
        if pos.short_symbol:
            already_known.append({"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE})
        if pos.hedge_symbol:
            already_known.append({"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE})
    # Seed BEFORE start() -- see PriceStream.seed_instruments' own docstring
    # for why this avoids a race between start()'s background _connect()
    # and a separate add_instruments() call from this (the main) thread.
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
    # touches state.
    engine.reconcile_pending_orders()

    pos = state_store.state.position
    if pos.error_state:
        is_hedge = pos.error_state in ("hedge_entry_failed", "hedge_exit_failed")
        is_exit = pos.error_state in ("exit_failed", "hedge_exit_failed")
        action = ("SELL" if is_exit else "BUY") if is_hedge else ("BUY" if is_exit else "SELL")
        push_leg_error(env, LEG_KEY, pos, action=action)
        Log.error(f"Resuming with an unresolved error from before restart "
                  f"({pos.error_state}/{pos.error_kind}) -- needs Retry/Cancel/Manually Completed.")
    elif pos.side:
        Log.info(f"Resuming an already-open position from before restart: side={pos.side} "
                 f"short={pos.short_symbol}@{pos.entry_px} hedge={pos.hedge_symbol}@{pos.hedge_entry_px} "
                 f"-- monitoring, not re-entering.")

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
        engine._rsi_executor.shutdown(wait=False)
    except Exception:
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        engine._rsi_executor.shutdown(wait=False)
        raise


if __name__ == "__main__":
    main()
