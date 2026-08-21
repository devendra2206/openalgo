"""
===============================================================================
NIFTY Trend-Following -- Dual-Timeframe (27m/9m), Synthetic-Combo Execution
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Derived from: `Nifty_TrendFollow_DualTF45_15_1_20260817173543.py` (Instance B,
              Big=45m/Small=15m) -- identical logic, only the timeframe pair
              differs (see below), same as how that file's own Instance A
              (Big=1 Day/Small=1hr) was derived. All platform-integration
              scaffolding traces back to `MCX_CrudeOil_EMA9_RSI_Intraday_1_
              20260723150000.py` (see AUTHORING_CHECKLIST.md reference #1).
              The SIGNAL/EXECUTION model is entirely new, not copied from any
              existing script (see below).

*** DRY-RUN BY DEFAULT (Config.dry_run=True) -- NO REAL ORDERS SENT ***
This account already runs other strategies LIVE. OpenAlgo's Analyze Mode is
a single account-wide switch with no per-strategy scoping (confirmed: no
mode/analyze/sandbox field anywhere in blueprints/python_strategy.py or its
frontend) -- flipping it on to paper-test THIS strategy would also sandbox
every other live strategy simultaneously. Dry-run here is a SCRIPT-LEVEL
flag instead: every real broker-facing decision (data fetch, signal, entry/
exit/SL/handoff/rollover/hedge logic, state persistence) runs for real;
only `placeorder()`/`placesmartorder()` calls are replaced with a logged
"[DRY RUN] would place..." line plus an internally-tracked SIMULATED fill
(price = LTP at the decision moment), so every downstream check (SL,
handoff, re-entry, rollover) keeps operating against a real, live-tracked
position. Flip `dry_run` to False to go live -- no other code change
needed. This is NOT the same as the `test_mode` flag other scripts in this
project use (that one only bypasses market-hours/entry-window gating, it
does not stop real orders -- confirmed by reading
MCX_CrudeOil_EMA34_RSI_ADX_Intraday_1's usage). Do not confuse the two.

Origin: adapted from IBBM Advance Training notes (MACD/SuperTrend trend
following with a dual-timeframe handoff mechanism) -- full spec settled
interactively, summarized below.

Signal source: REAL NIFTY FUTURES price (NOT the synthetic combo's own
derived price) -- MACD(9,23,7) + SuperTrend(8,3.2), computed on TWO
timeframes per instance:
  - Big timeframe:   27 minutes
  - Small timeframe: 9 minutes (= Big / 3, the source spec's own ratio)
This is Instance C of three independently-deployed instances (see
Nifty_TrendFollow_DualTF1D_1hr_1_*.py for Instance A, Big=1 Day/Small=1hr, and
Nifty_TrendFollow_DualTF45_15_1_*.py for Instance B, Big=45m/Small=15m --
same logic, different timeframe pair, fully separate state/positions/PnL).

Entry-search priority (checked once per closed candle on whichever
timeframe is due):
  1. Only one open position at a time (long OR short OR flat -- never both).
  2. Search ALWAYS starts on the Big timeframe: enter if ST(8,3.2) and
     MACD(9,23,7) AGREE (both bullish -> long, both bearish -> short) on
     the last CLOSED Big-timeframe candle.
  3. If Big has no agreement (out of sync) -- including at day-start,
     before Big's own first candle of the day has closed -- fall through
     and check the Small timeframe for the same ST+MACD agreement instead.

Dual-timeframe monitoring handoff (once a trade is taken on the SMALL
timeframe because Big had no match at entry):
  - Monitored ACTIVELY on the Small timeframe (incl. its own SL reference)
    for as long as the Big-timeframe candle that was still forming at
    entry remains unformed.
  - The moment that Big-timeframe candle closes: recheck ST+MACD sync.
      * In sync (agrees with the trade's direction) -> hand off monitoring
        AND the SL candle-reference to the Big timeframe from here on.
      * Still out of sync -> keep monitoring on Small, recheck again at
        every subsequent Big-timeframe close.
  - A trade taken directly on the Big timeframe stays Big-controlled for
    its whole life -- no handoff ever needed for it.

Execution -- synthetic combo, NOT a real futures order:
  ATM strike = real futures price rounded to nearest 100.
  Long  -> BUY ATM Call + SELL ATM Put (same strike/expiry).
  Short -> SELL ATM Call + BUY ATM Put.
  A further-OTM HEDGE leg is placed alongside, same direction as the
  exposed short leg (protects the otherwise-unlimited-risk short side):
    Long position (short Put exposed)  -> BUY a further-OTM Put, ~2-3% OTM.
    Short position (short Call exposed) -> BUY a further-OTM Call, ~2-3% OTM.
  All 3 legs (2 core + 1 hedge) are placed together as one atomic group and
  closed together on any exit -- never partially. Hedge is pure insurance
  (psychology/margin), entered/exited/rolled in lockstep with the core
  position, never independently managed or timed.

Stop-loss (measured on the REAL FUTURES price, not the combo's own value):
  1% of entry futures price, OR the reference candle's high/low broken by
  the next candle (reference candle = the entry candle on whichever
  timeframe is currently controlling monitoring; see handoff above) ->
  close all 3 legs together. SL rules are unaffected by rollover -- only
  which month's futures price series they read changes.

Post-stop-loss sequencing:
  1. Re-entry check FIRST: if ST+MACD never actually changed since the SL,
     wait for the futures price to recover to the original entry level and
     re-enter the SAME direction on the SAME timeframe that was controlling
     at SL -- no candle-close required for this specific re-entry.
  2. Only if that condition isn't met (signal genuinely changed) -> run a
     fresh entry search from scratch (Big-first, fall to Small, per above).

Rollover (checked once at the start of each trading day):
  Contract "starts" the day after the previous month's own expiry; rolls to
  the next month once 21 CALENDAR days have elapsed since that start
  (typically lands ~20th-21st of the month in practice). If a position is
  open at the moment rollover triggers, it is FORCE-CLOSED FIRST (all 3
  legs, SHORT-before-LONG priority -- see Nifty_Sensex_Expiry_Batman_1's
  convention: exposed short leg closes first, then the covering long leg,
  then the hedge last), then a FRESH equivalent position is immediately
  reopened in the new month's contract, with ATM/hedge strikes re-derived
  from the NEW month's current futures price -- never carrying the old
  strikes/position forward. This rollover force-close-and-reopen behavior
  is genuinely new to this project; existing sibling scripts' rollover
  logic only ever affects which expiry NEW entries use, never touches an
  already-open position.

Order placement robustness, trade log, PnL reporting, error recovery
------------------------------------------------------------------------
Same platform-integration machinery as every other deployed script in this
project (see AUTHORING_CHECKLIST.md): per-leg async fill watchers,
OrderNeedsAttention/Retry-Cancel-Manual error recovery,
STRATEGY_REPORTING_PORT-targeted background reporting (PnL/errors/pending-
actions/Force-Exit, never inline on run_cycle's thread), WhatsApp self-alert
on failure, candle-boundary-aware (not rolling-timer) signal refresh --
tracked INDEPENDENTLY per timeframe here, since Big and Small each have
their own candle boundaries that must not suppress each other's refresh.

API-call efficiency: candle-boundary-aware history() refresh (not a rolling
timer, tracked independently per timeframe -- see above); WS-first LTP via
PriceStream, REST only as a stale-cache fallback; all fill-watching/
reporting/error-push dispatched via background executor pools, never
inline on run_cycle's thread. fetch_chain_strikes() calls optionchain()
once per entry (strike discovery only, no premium data consumed). NOTE:
the 3-leg pre-entry price reads in _open_position/_close_position are 3
sequential fetch_symbol_ltp() calls, not a batched multiquotes() call --
an earlier draft of this docstring claimed get_multiquotes() batching that
was never actually implemented; left as a known follow-up optimization,
not a correctness issue (3 sequential single-symbol quote calls per
entry/exit is what every other multi-leg script in this project already
does).

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `ta.macd(close, fast, slow, signal)` -> (macd_line, signal_line, hist).
  * `ta.supertrend(high, low, close, period, multiplier)` -> (supertrend,
    direction) or similar -- verify exact return shape against the
    installed indicators module before trusting the wiring below.
  * Confirmed live (2026-08-17) against `broker/shoonya/api/data.py`'s
    `timeframe_map`: Shoonya natively supports 1m/3m/5m/10m/15m/30m/1h/2h/
    4h/D only -- NO native 27m or 9m. Both 27 and 9 are exact multiples of
    the native 3m granularity (9=3x3, 27=9x3=3x3x3), same fix as CrudeOil's
    own 9m instance (3m fetch, locally aggregated). This instance fetches
    broker-native `config.big_fetch_interval=config.small_fetch_interval=
    "3m"` and locally resamples to 9m (Small) and 27m (Big) via
    `_resample_to_candle_interval` -- a genuine 3-bar and 9-bar aggregation
    respectively, not a 1:1 passthrough. Do not set big_fetch_interval="27m"
    or small_fetch_interval="9m" directly; either will raise "Unsupported
    interval" at the broker call. Instance A (Big=1 Day/Small=1hr) fetches
    "D"/"1h" directly instead, Instance B (Big=45m/Small=15m) fetches native
    15m and resamples only Big -- see compute_timeframe_signal's
    `_fetch_interval_matches_target` guard, which is what makes
    fetch_interval genuinely independent per timeframe.

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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from openalgo import api, ta

# Python's default 8MB/thread stack reservation adds up fast across this
# process's several concurrent threads (per-leg fill watchers x3, signal
# refresh x2 timeframes, PnL push, trade-log writer, PriceStream's own
# watchdog/WS threads) -- confirmed in production elsewhere in this project
# as the actual ceiling behind "RuntimeError: can't start new thread" under
# the STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap. Must run before any thread is
# created.
threading.stack_size(1024 * 1024)

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
    underlying_exchange: str     # "NSE_INDEX" -- real quotable spot index
    options_exchange: str        # "NFO"
    futures_exchange: str        # "NFO"


INSTRUMENTS = [
    InstrumentConfig(name="NIFTY", underlying_exchange="NSE_INDEX",
                      options_exchange="NFO", futures_exchange="NFO"),
]


@dataclass
class Config:
    strategy_name: str = "NIFTY Trend-Follow Dual-TF (27m/9m) Synthetic-Combo -- SL Frozen"
    version: str = "2.0.0"
    # v2, 2026-08-20: rollover reopen and post-SL re-entry now carry forward
    # the ORIGINAL entry_futures_px/sl_reference_high/sl_reference_low from
    # the very first entry of a logical trade, instead of computing a fresh
    # SL reference each time. Strikes still re-resolve off the CURRENT
    # futures price as before (unaffected) -- only the SL anchor/candle
    # reference is frozen. See _open_position's new sl_anchor_* params and
    # their two call sites (_handle_rollover, _check_sl_reentry). Built as a
    # separate file (not editing the deployed v1) specifically to A/B compare
    # via a 4-year backtest before deciding whether to port this into the
    # live script.
    instance_id: str = "27m_9m_v2_slfrozen"   # distinguishes this backtest variant's state/log files
                                                # from the deployed v1 instance -- never deploy both
                                                # under the same instance_id, they'd collide on state.json

    # --- Dual timeframe -------------------------------------------------
    big_timeframe_minutes: int = 27
    small_timeframe_minutes: int = 9   # Big / 3, per the source spec's own ratio
    # Broker-native interval to FETCH for each timeframe -- independently
    # chosen per timeframe since they don't always share one native
    # granularity (Instance A's Big=1 Day must fetch "D" directly; Small=1hr
    # fetches "1h" directly). Only resampled locally (_resample_to_candle_
    # interval) when the fetch interval isn't already the target
    # granularity -- see _fetch_interval_matches_target. For this instance
    # (Big=27m/Small=9m), Shoonya has no native 27m or 9m (confirmed
    # 2026-08-17 against broker/shoonya/api/data.py's timeframe_map), but
    # both are exact multiples of the native 3m granularity, so BOTH Big
    # and Small fetch native 3m and resample locally (3->9m, 3->27m) --
    # unlike Instance B, where only Big needed resampling.
    big_fetch_interval: str = "3m"
    small_fetch_interval: str = "3m"
    history_lookback_days: int = 10     # calendar days of history to fetch per timeframe -- must
                                         # comfortably clear the MACD/SuperTrend warmup floor for
                                         # the SMALLEST-granularity bar among big/small fetches

    # --- Signal indicators ------------------------------------------------
    macd_fast: int = 9
    macd_slow: int = 23
    macd_signal: int = 7
    supertrend_period: int = 8
    supertrend_multiplier: float = 3.2

    # --- Execution ----------------------------------------------------------
    atm_strike_round: int = 100
    hedge_otm_pct_low: float = 0.02     # 2%
    hedge_otm_pct_high: float = 0.03    # 3% -- midpoint used for hedge strike selection
    lot_multiplier: int = 1             # 1 lot per instance, per explicit instruction
    nifty_lot_size: int = 65            # NIFTY's current lot size (docs/prompt/LotSize.md) --
                                         # confirmed 2026-08-20 against Nifty_OI_WeeklyBuy_MonthlySell's
                                         # own quantity=65 that placeorder()'s quantity is real SHARE
                                         # count, not a lot multiplier -- there is no platform-side
                                         # auto lot-conversion (an earlier, wrong comment on
                                         # build_combo_legs claimed there was). qty must be
                                         # lot_multiplier * nifty_lot_size, never lot_multiplier alone.
    # strikes-each-side-of-ATM bound for optionchain() -- unbounded (the old
    # default) fans the broker out to a live quote per listed strike (this
    # SDK's optionchain() always returns quotes, no with_quotes toggle) and
    # measured 30+s in production 2026-08-20 (vs MCX_CrudeOil's strike_count=1,
    # which needs only the ATM strike). Hedge band tops out at 3% OTM, which
    # at 50pt NIFTY strike spacing is ~15 strikes out -- 20 leaves headroom.
    chain_strike_count: int = 20

    # --- Stop-loss ------------------------------------------------------
    sl_pct: float = 0.01                # 1% of entry futures price

    # --- Rollover ---------------------------------------------------------
    # Calendar day-OF-MONTH threshold (NOT a days-since-start duration --
    # confirmed explicitly 2026-08-19): once today.day >= this value, roll
    # to next month's contract, regardless of which day the current
    # contract itself expires or when it was first listed.
    rollover_days_after_start: int = 21
    # Rollover force-close-and-reopen never fires before this time each day
    # (user-confirmed 2026-08-19) -- 09:15-09:30 is often thin/volatile
    # right at market open, so wait a few minutes before force-closing and
    # reopening a position on rollover. Bypassed when config.test_mode is
    # True, same as every other timing gate in this file.
    rollover_earliest_time: time = time(9, 30)

    product: str = "NRML"               # can carry the synthetic combo overnight (unlike MIS)
    price_type: str = "LIMIT"

    entry_start: time = time(9, 20)
    entry_end: time = time(15, 0)       # no NEW entries after this time (unchanged, user-confirmed)
    # POSITIONAL strategy (user correction, 2026-08-17): no daily forced
    # square-off. universal_exit_time is intentionally UNUSED for closing
    # positions -- kept only so print_banner()/older tooling referencing it
    # doesn't break; a position stays open across days, closing only via
    # SL, rollover force-close-reopen, or a manual Force Exit. Matches
    # product=NRML, which was already configured for overnight carry.
    universal_exit_time: time = time(15, 15)
    # No candle/signal checking at all (signal refresh, entry search,
    # handoff recheck, post-SL re-entry recheck) once past this time each
    # day -- user correction, 2026-08-17. SL monitoring (both the 1% and
    # candle-break mechanisms) is UNAFFECTED and continues regardless.
    daily_candle_check_cutoff: time = time(15, 10)
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)

    scheduler_interval: int = 10
    pnl_tick_interval: float = 0.8
    pnl_rest_fallback_interval_sec: float = 900.0

    ws_stale_seconds: float = 20.0
    ws_stale_seconds_open: float = 60.0          # widened threshold, first ~45min after open
    ws_post_open_grace_until: time = time(10, 0)
    ws_watchdog_interval: float = 15.0
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    # A leg symbol just subscribed via add_instruments() has no WS tick yet,
    # so entry falls to the REST fetch_symbol_ltp() -- confirmed live
    # 2026-08-21: that REST read can itself return an untrustworthy quote
    # (require_two_sided rejects it), and immediately afterward there is
    # nothing left to fall back to except 0.0, recording a bogus entry
    # price. Bounded retry here (short, since it blocks _open_position's
    # per-leg loop) gives the broker a moment to have either a real WS tick
    # or a real two-sided REST quote ready, same philosophy as
    # place_order_max_attempts/place_order_retry_delay above but for the
    # pre-fill price read rather than the order placement call itself.
    ltp_retry_max_attempts: int = 3
    ltp_retry_delay: float = 1.0

    error_repush_interval_sec: float = 60.0
    signal_refresh_retry_cooldown_sec: float = 60.0  # min gap between retrying a FAILED
                                                       # compute_timeframe_signal for the same
                                                       # still-open candle boundary (independent
                                                       # per timeframe) -- a broker hiccup must not
                                                       # re-fire a fresh history() call every single
                                                       # scheduler tick until data lands.
    cycle_failure_notify_interval_sec: float = 300.0

    state_file: str = "strategy_state.json"
    log_level: int = logging.INFO

    # *** Dry-run: see module docstring's top banner. Default True. ***
    dry_run: bool = os.getenv("STRATEGY_DRY_RUN", "1") == "1"
    test_mode: bool = os.getenv("STRATEGY_TEST_MODE", "0") == "1"   # bypasses market-hours, entry-window, and
                                                                     # universal-exit-time gating ONLY -- does
                                                                     # NOT stop real orders (that's config.dry_run)


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
class OptionLeg:
    """One of the 3 legs making up a combined position: core_call, core_put,
    or hedge. Each is an independent broker order with its own fill
    lifecycle -- a leg can independently be pending/filled/errored while its
    siblings are in a different state, even though all 3 are always PLACED
    together as one group and CLOSED together on any exit."""
    role: str = ""            # "core_call" | "core_put" | "hedge"
    symbol: str = ""
    action: str = ""          # "BUY" | "SELL"
    quantity: int = 0
    entry_order_id: str = ""
    entry_filled: bool = False
    entry_px: float = 0.0
    exit_order_id: str = ""
    exit_filled: bool = False
    exit_px: float = 0.0
    # Order error recovery -- same shape as the reference scripts' LegPosition,
    # but per-LEG here (3 independent broker orders per combined position),
    # not per combined-position.
    error_state: str = ""     # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""      # "" | "terminal" | "resting"
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None


@dataclass
class CombinedPosition:
    """The whole 3-leg synthetic-combo position for this instance -- empty
    (direction="") when flat. Only ONE of these can be non-flat at a time
    (see module docstring's entry-search-priority section)."""
    direction: str = ""              # "" | "long" | "short"
    core_call: OptionLeg = field(default_factory=OptionLeg)
    core_put: OptionLeg = field(default_factory=OptionLeg)
    hedge: OptionLeg = field(default_factory=OptionLeg)
    entry_time: str = ""
    entry_futures_px: float = 0.0    # SL/re-entry reference -- see module docstring
    controlling_timeframe: str = ""  # "BIG" | "SMALL" -- which TF currently governs monitoring/SL
    entry_timeframe: str = ""        # which TF the entry itself fired on ("BIG"/"SMALL", audit only)
    big_candle_key_at_entry: str = ""  # candle_key of the Big-TF candle active when a SMALL-TF trade
                                        # was opened -- _check_handoff compares against this (NOT
                                        # entry_timeframe, which is a TF label, not a candle key) to
                                        # detect "that Big candle has now closed". Only meaningful
                                        # when controlling_timeframe == "SMALL".
    sl_reference_high: float = 0.0   # reference candle's high (entry candle, or handed-off Big candle)
    sl_reference_low: float = 0.0
    handoff_ts: str = ""             # wall-clock time the SMALL->BIG handoff fired, "" if never handed off
    expiry_compact: str = ""         # which month's contract this position's legs are struck in
    execution_id: int = 0
    is_dry_run: bool = False         # True if this position was opened while dry_run was active
    closing: bool = False            # True from the moment a close is initiated (SL/rollover/force-exit/
                                      # universal-exit) until every non-empty leg is confirmed exit_filled
                                      # (or parked in error_state for Retry/Cancel/Manual) -- direction
                                      # stays set the whole time so no NEW entry/handoff/SL logic runs
                                      # against a position that isn't actually flat yet.


@dataclass
class InstanceState:
    trade_count_today: int = 0
    position: CombinedPosition = field(default_factory=CombinedPosition)
    # Post-SL re-entry tracking (module docstring's "post-stop-loss
    # sequencing" section) -- set the moment an SL fires, cleared once
    # either the re-entry condition resolves or a fresh search takes over.
    awaiting_sl_reentry: bool = False
    sl_reentry_direction: str = ""     # "long" | "short" -- direction to re-enter if signal is unchanged
    sl_reentry_timeframe: str = ""     # which TF to re-enter on
    sl_reentry_futures_px: float = 0.0  # original entry price to wait for recovery to
    # v2 SL-frozen: the ORIGINAL candle-break reference (from the very first
    # entry of this logical trade), captured alongside sl_reentry_futures_px
    # the moment SL/signal-exit fires -- carried forward into a post-SL
    # re-entry unchanged, instead of v1's fresh recovery-candle reference.
    sl_reentry_sl_high: float = 0.0
    sl_reentry_sl_low: float = 0.0
    last_sl_exit_candle_boundary: str = ""  # same-candle whipsaw guard, see reference scripts


@dataclass
class StrategyState:
    current_day: str = ""
    instance: InstanceState = field(default_factory=InstanceState)
    last_updated: str = ""
    today_realized_pnl: float = 0.0
    futures_symbol: str = ""         # current-month futures contract, resolved once/day
    contract_start_date: str = ""    # date this month's contract became front-month -- rollover clock
    expiry_compact: str = ""         # current month's options expiry, compact form
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
            or f"nifty_trendfollow_dualtf_{config.instance_id}"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return config.market_open <= now <= config.market_close


def _current_ws_stale_threshold() -> float:
    """Widened staleness threshold during the first ~45min after open --
    NIFTY.NSE_INDEX only ticks when the index recalculates off constituents
    trading, naturally burstier right at 09:15 than the rest of the day.
    Same fix already applied to every NIFTY/SENSEX sibling in this project."""
    if config.test_mode:
        return config.ws_stale_seconds_open
    now = datetime.now(IST).time()
    if config.market_open <= now < config.ws_post_open_grace_until:
        return config.ws_stale_seconds_open
    return config.ws_stale_seconds


def _current_candle_boundary(interval_minutes: int) -> datetime:
    """Start-of-bucket timestamp for the current wall-clock candle, anchored
    to the 09:15 session open (555 minutes from midnight) -- NOT midnight,
    since a locally-resampled bucket size that doesn't evenly divide 555
    (e.g. this strategy's 45m does: 555/45=12.33 -- does NOT divide evenly
    either) would disagree with a 09:15-anchored resample() call on every
    single bucket, all day (AUTHORING_CHECKLIST.md section 5's first bug).
    Given NEITHER 45 nor 15 divides 555 evenly, this function and
    _resample_to_candle_interval below MUST use the exact same anchor
    consistently -- both anchor to 09:15 here, deliberately, rather than
    relying on divisibility."""
    now = datetime.now(IST)
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < session_start:
        session_start -= timedelta(days=1)  # pre-market: anchor to yesterday's session start
    elapsed_minutes = int((now - session_start).total_seconds() // 60)
    bucket_start_minutes = (elapsed_minutes // interval_minutes) * interval_minutes
    return session_start + timedelta(minutes=bucket_start_minutes)


def _candle_key_boundary(candle_key: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(candle_key)
    except (ValueError, TypeError):
        return None


def _resample_to_candle_interval(bars, interval_minutes: int):
    """Aggregate fetch_interval (3m, broker-native) bars up to
    candle_interval_minutes (27m or 9m). origin anchored to the 09:15
    session open (NOT midnight -- see _current_candle_boundary's own
    comment for why: neither 27 nor 9 evenly divides the 555-minutes-
    from-midnight session start for 27m specifically -- 555/27 isn't
    integral -- so this function and that one must share the identical
    anchor or they silently disagree on every bucket, all day).

    Anchored to bars.index[-1] (the LATEST/today's bar), not bars.index[0]
    -- confirmed live, 2026-08-20: bars here spans the full
    history_lookback_days=10 warmup window, so index[0] is ~10 calendar
    days in the past. pandas resample's origin is a single fixed instant
    propagated forward continuously, never re-snapping to 09:15 on later
    days; since 1440 (minutes/day) mod 27 == 9, a stale-day origin drifts
    the WHOLE day's 27m grid by a multiple of 9 minutes every subsequent
    day (confirmed live: BIG candle boundary landed on 09:06:00 instead of
    the correct 09:15-aligned 09:15/09:42/... grid -- SMALL's 9m interval
    was unaffected since 1440 mod 9 == 0, evenly divides a day). This is
    not just a mislabeled timestamp -- it means MACD/SuperTrend were being
    computed over the wrong 27-minute window of futures prices. Anchoring
    to the latest bar's own day keeps this in lockstep with
    _current_candle_boundary's per-day-fresh 09:15 anchor."""
    session_anchor = bars.index[-1].normalize() + pd.Timedelta(hours=9, minutes=15) \
        if len(bars) else None
    resampled = bars.resample(f"{interval_minutes}min", origin=session_anchor if session_anchor is not None else "start_day").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum" if "volume" in bars.columns else "first",
    })
    return resampled.dropna(subset=["open", "high", "low", "close"])


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
            # PriceStream._watchdog_loop owns full reconnect + resubscribe
            # on this same client -- the SDK's own auto_reconnect thread
            # would race it (both call _do_connect() on self.ws
            # independently), confirmed elsewhere in this project as a
            # repeating ~45-50s "connection down" cycle that never settles.
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
# LIVE PRICE STREAM (WebSocket, DYNAMIC subscription -- symbols aren't known
# until strikes are resolved at entry, same pattern as the CrudeOil/VWAP_NoHA/
# Batman siblings' dynamic PriceStream, see their module docstrings for the
# full watchdog/reconnect writeup, identical here)
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
                Log.warning(f"[PriceStream] connection down (attempt {failures}) -- "
                            f"reconnecting fully, then waiting {wait}s.")
                self._teardown()
                try:
                    self._connect()
                except Exception as exc:
                    Log.warning(f"[PriceStream] reconnect failed: {exc}")
                self._stop.wait(wait)
                continue

            now = datetime.now(IST)
            stale_threshold = _current_ws_stale_threshold()
            stale_instruments = []
            # Both reads under ONE lock acquisition -- previously two separate
            # with self._lock: blocks let remove_instruments() run in the gap
            # between them (e.g. a position closing mid-watchdog-pass), so
            # all_keys could miss a symbol that stale_instruments still
            # referenced from the earlier snapshot -- causing a spurious
            # resubscribe of a symbol the strategy no longer holds (found via
            # code review, 2026-08-19; harmless correctness-wise since
            # nothing reads get_ltp() for a symbol outside the current
            # position, but a real race nonetheless).
            with self._lock:
                all_keys = set(self._instruments.keys())
                for key, inst in self._instruments.items():
                    entry = self._cache.get(key)
                    if entry is None or (now - entry[1]).total_seconds() > stale_threshold:
                        stale_instruments.append(inst)
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
                    Log.warning(f"[PriceStream] {names} stale on a majority of tracked symbols, "
                                f"REST-confirmed genuinely broken -- escalating to full reconnect.")
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
                Log.warning(f"[PriceStream] {names} stale on a majority, but REST shows no "
                            f"price movement -- thin liquidity, not a broken feed. Skipping reconnect.")

            due_for_retry = [
                inst for inst in stale_instruments
                if now >= self._symbol_next_retry_at.get((inst["symbol"], inst["exchange"]), now)
            ]
            if not due_for_retry:
                continue

            due_names = ", ".join(f"{i['symbol']}.{i['exchange']}" for i in due_for_retry)
            Log.warning(f"[PriceStream] stale/missing ticks for: {due_names} -- resubscribing.")
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
        self.state.futures_symbol = data.get("futures_symbol", "")
        self.state.contract_start_date = data.get("contract_start_date", "")
        self.state.expiry_compact = data.get("expiry_compact", "")
        self.state.last_execution_id = data.get("last_execution_id", 0)

        inst_raw = data.get("instance", {})
        inst = InstanceState()
        inst.trade_count_today = inst_raw.get("trade_count_today", 0)
        inst.awaiting_sl_reentry = inst_raw.get("awaiting_sl_reentry", False)
        inst.sl_reentry_direction = inst_raw.get("sl_reentry_direction", "")
        inst.sl_reentry_timeframe = inst_raw.get("sl_reentry_timeframe", "")
        inst.sl_reentry_futures_px = inst_raw.get("sl_reentry_futures_px", 0.0)
        inst.sl_reentry_sl_high = inst_raw.get("sl_reentry_sl_high", 0.0)
        inst.sl_reentry_sl_low = inst_raw.get("sl_reentry_sl_low", 0.0)
        inst.last_sl_exit_candle_boundary = inst_raw.get("last_sl_exit_candle_boundary", "")

        pos_raw = inst_raw.get("position", {})
        pos = CombinedPosition()
        for simple_field in ("direction", "entry_time", "entry_futures_px", "controlling_timeframe",
                              "entry_timeframe", "big_candle_key_at_entry", "sl_reference_high",
                              "sl_reference_low", "handoff_ts", "expiry_compact", "execution_id",
                              "is_dry_run", "closing"):
            if simple_field in pos_raw:
                setattr(pos, simple_field, pos_raw[simple_field])
        for leg_field in ("core_call", "core_put", "hedge"):
            leg_raw = pos_raw.get(leg_field, {})
            setattr(pos, leg_field, OptionLeg(**{**asdict(OptionLeg()), **filter_known_fields(OptionLeg, leg_raw)}))
        inst.position = pos
        self.state.instance = inst
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        inst = self.state.instance
        payload = {
            "current_day": self.state.current_day,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "futures_symbol": self.state.futures_symbol,
            "contract_start_date": self.state.contract_start_date,
            "expiry_compact": self.state.expiry_compact,
            "last_execution_id": self.state.last_execution_id,
            "instance": {
                "trade_count_today": inst.trade_count_today,
                "awaiting_sl_reentry": inst.awaiting_sl_reentry,
                "sl_reentry_direction": inst.sl_reentry_direction,
                "sl_reentry_timeframe": inst.sl_reentry_timeframe,
                "sl_reentry_futures_px": inst.sl_reentry_futures_px,
                "sl_reentry_sl_high": inst.sl_reentry_sl_high,
                "sl_reentry_sl_low": inst.sl_reentry_sl_low,
                "last_sl_exit_candle_boundary": inst.last_sl_exit_candle_boundary,
                "position": asdict(inst.position),
            },
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=4)


###############################################################################
# HELPERS -- expiry / futures / rollover resolution
###############################################################################
def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def _last_tuesday_on_or_before(d: date) -> date:
    """The most recent Tuesday <= d (Python's date.weekday(): Monday=0,
    Tuesday=1) -- NSE's standardized weekly-expiry weekday for NIFTY."""
    return d - timedelta(days=(d.weekday() - 1) % 7)


def _estimate_contract_start(current_expiry_date: date) -> date:
    """Cold-start-only fallback for contract_start_date, used ONLY when
    there is no prior persisted state AND the broker's expiry() list has no
    earlier entry to derive it from (the normal case in practice -- see
    below). Calendar-exact instead of a flat day-count guess: the day
    after the PREVIOUS calendar month's own last Tuesday.

    Replaces an earlier `current_expiry_date - timedelta(days=30)` guess.
    That flat guess was confirmed live (2026-08-19, accelerated 2-day
    replay through the real StrategyEngine) to misfire: client.expiry()
    for NIFTY options returns ONLY unexpired dates (confirmed live:
    ['25-AUG-26','01-SEP-26','08-SEP-26',...], weekly-Tuesday near-term) --
    meaning `current_idx > 0` above can essentially never be true in
    practice (there is never an earlier, already-expired entry in the
    list to index back from), so this fallback is not a rare edge case,
    it is the path taken on EVERY cold start. The -30-day guess landed at
    23 days-since-start on a real August/September boundary -- 2 days
    past the 21-day rollover threshold -- triggering a spurious rollover
    to the following WEEK's expiry (01-SEP-26) on the very first cycle
    ever, before any position could exist. Harmless that one time only
    because no position was open yet to force-close-and-reopen on the
    wrong contract, and it happened to self-suppress on the next cycle by
    coincidence (the bad estimate landed in the future relative to that
    day) -- not a real fix, just lucky numbers. This calendar-exact
    version removes the misfire at its source."""
    prev_month_last_day = current_expiry_date.replace(day=1) - timedelta(days=1)
    return _last_tuesday_on_or_before(prev_month_last_day) + timedelta(days=1)


def _extract_monthly_expiries(expiry_dates: list) -> list:
    """Reduce a raw options-expiry list to one date per calendar month --
    the LAST listed expiry in each month. Confirmed live 2026-08-19:
    client.expiry(instrumenttype="options") for NIFTY returns weekly
    Tuesdays near-term mixed with monthly-only further out (e.g.
    ['25-AUG-26','01-SEP-26','08-SEP-26','15-SEP-26','22-SEP-26',
    '29-SEP-26','27-OCT-26',...]) -- NOT one-per-month as the original
    rollover logic assumed. Treating the raw list as one-per-month made
    'roll to next month' actually roll to next WEEK once 21 days
    genuinely elapsed (25-AUG-26 -> 01-SEP-26, a weekly expiry, instead
    of September's real monthly contract 29-SEP-26) -- caught before it
    could fire live, since NSE's Tuesday-weekly cadence means each
    month's own final Tuesday IS its monthly contract, so taking the max
    date per (year, month) correctly isolates monthly-only expiries
    regardless of how many weeklies precede it that month."""
    by_month: dict = {}
    for d in expiry_dates:
        key = (d.year, d.month)
        if key not in by_month or d > by_month[key]:
            by_month[key] = d
    return sorted(by_month.values())


def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


def resolve_current_month_context(client, inst: InstrumentConfig, prior_contract_start: str) -> dict:
    """Resolves the currently-ACTIVE month's options expiry + futures
    symbol, and the ROLLOVER state. Rollover rule (explicit original spec,
    reconfirmed 2026-08-19): once TODAY'S OWN CALENDAR DAY-OF-MONTH is >=
    config.rollover_days_after_start (21), trade the NEXT month's contract
    instead of the current one -- a fixed calendar-date rule, not a
    days-since-start or days-until-expiry duration. (A prior version of
    this function implemented a days-since-start duration instead -- a
    misreading of this same spec, not a different requirement -- see
    _estimate_contract_start's docstring for the full history.)

    Returns a dict: {expiry_compact, futures_symbol, contract_start_date,
    rolled_today: bool} -- contract_start_date is informational only
    (persisted/logged); rolled_today reflects today's day-of-month check
    directly (True for every cycle from day 21 through month-end, not just
    the first) -- the caller (StrategyEngine._handle_rollover) already
    de-dupes repeat cycles via its own `prior_expiry == ctx["expiry_compact"]`
    comparison, so this doesn't re-trigger force-close/reopen every cycle."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve options expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    dates_raw = resp["data"]
    # Reduced to ONE expiry per calendar month (see _extract_monthly_expiries's
    # docstring) -- the raw broker list mixes weekly + monthly, and every
    # index/roll computation below assumes a monthly-only series.
    expiry_dates = _extract_monthly_expiries(
        [datetime.strptime(raw, "%d-%b-%y").date() for raw in dates_raw]
    )

    # Find the nearest expiry that is still >= today -- this is the
    # "current" contract before applying the day-of-month rollover rule.
    current_idx = next((i for i, d in enumerate(expiry_dates) if d >= today), len(expiry_dates) - 1)

    # Rollover rule (explicit spec from this strategy's original design,
    # reconfirmed 2026-08-19): once TODAY'S OWN CALENDAR DAY-OF-MONTH is
    # >= config.rollover_days_after_start (21), trade the NEXT month's
    # contract instead of the current one. This is a fixed calendar-date
    # rule -- independent of which day the current contract itself
    # expires, when it was first listed, or any days-since-start/
    # days-until-expiry duration. (An earlier version of this function
    # computed a days-since-start duration off an estimated contract-start
    # date -- that was a misimplementation of this same "day 21" spec, not
    # a different requirement, and additionally had two bugs of its own:
    # the estimate was imprecise enough to misfire near the threshold, and
    # the roll target indexed a broker expiry list that mixes weekly and
    # monthly dates, landing on the next WEEKLY expiry instead of the next
    # MONTHLY one. Both are moot now that the decision is calendar-day-
    # based, but _extract_monthly_expiries above is still required so
    # current_idx/current_idx+1 index monthly-only contracts.)
    rolled_today = today.day >= config.rollover_days_after_start
    if rolled_today:
        if current_idx + 1 < len(expiry_dates):
            current_idx += 1
        else:
            raise RuntimeError(
                f"{inst.name}: today ({today.isoformat()}) is on/after day "
                f"{config.rollover_days_after_start} of the month -- past the rollover "
                f"threshold -- but the broker returned no later monthly expiry to roll to. "
                f"Refusing to silently keep trading the current contract."
            )
    current_expiry_date = expiry_dates[current_idx]
    # NOTE: current_idx indexes the monthly-reduced expiry_dates list above,
    # not the raw broker list (dates_raw) -- the two are no longer the same
    # length/order, so expiry_compact must be built from current_expiry_date
    # directly rather than re-indexing dates_raw.
    expiry_compact = current_expiry_date.strftime("%d%b%y").upper()
    # Informational only now (persisted/logged) -- the roll decision above
    # no longer depends on it. prior_contract_start is likewise unused in
    # this function's own logic; kept as a parameter only for call-site
    # compatibility (StrategyEngine passes self.state.contract_start_date).
    contract_start_date = _estimate_contract_start(current_expiry_date)

    fut_resp = client.expiry(symbol=inst.name, exchange=inst.futures_exchange, instrumenttype="futures")
    if fut_resp.get("status") != "success" or not fut_resp.get("data"):
        raise RuntimeError(f"Could not resolve futures expiry for {inst.name}: {fut_resp}")
    fut_dates_raw = fut_resp["data"]
    # Futures month is derived to MATCH the options month just resolved above
    # (same calendar month/year) -- never resolved independently, so the two
    # can never diverge (same principle as the CrudeOil sibling's
    # resolve_current_month_futures, confirmed in this project as the only
    # way to guarantee they never disagree).
    target_month_year = (current_expiry_date.month, current_expiry_date.year)
    futures_raw = next(
        (raw for raw in fut_dates_raw
         if (lambda d: (d.month, d.year))(datetime.strptime(raw, "%d-%b-%y").date()) == target_month_year),
        None,
    )
    if futures_raw is None:
        # Fallback: nearest futures expiry >= the options expiry just chosen.
        futures_raw = next(
            (raw for raw in fut_dates_raw if datetime.strptime(raw, "%d-%b-%y").date() >= current_expiry_date),
            fut_dates_raw[-1],
        )
    futures_symbol = f"{inst.name}{_compact_expiry(futures_raw)}FUT"

    return {
        "expiry_compact": expiry_compact,
        "futures_symbol": futures_symbol,
        "contract_start_date": contract_start_date.isoformat(),
        "rolled_today": rolled_today,
    }


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
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
            Log.warning(f"fetch_symbol_ltp: malformed bid/ask for {symbol}.{exchange}: {exc}")
            return None
        if not (ltp > 0 and bid > 0 and ask > 0):
            Log.warning(f"fetch_symbol_ltp: quote for {symbol}.{exchange} lacks a two-sided "
                        f"market (ltp={ltp}, bid={bid}, ask={ask}) -- treating as untrustworthy")
            return None
    return ltp


def fetch_symbol_ltp_with_retry(client, symbol: str, exchange: str,
                                 max_attempts: int = 1, retry_delay: float = 1.0) -> Optional[float]:
    """fetch_symbol_ltp(..., require_two_sided=True), retried up to
    max_attempts times with retry_delay between tries. Only for the ENTRY-
    FILL price read in _open_position -- a leg symbol just subscribed via
    add_instruments() has no WS tick yet, so it falls straight to this REST
    read; if that quote is ALSO untrustworthy (confirmed live 2026-08-21:
    the same wrong-symbol-level-quote broker bug require_two_sided guards
    against), there is nothing left to fall back to except a bogus 0.0
    entry price. A short bounded retry gives the broker a moment to have a
    genuine two-sided quote ready, same philosophy as
    place_order_max_attempts/place_order_retry_delay but for this pre-fill
    price read rather than the order placement call itself."""
    import time as _time
    for attempt in range(1, max_attempts + 1):
        ltp = fetch_symbol_ltp(client, symbol, exchange, require_two_sided=True)
        if ltp is not None:
            return ltp
        if attempt < max_attempts:
            Log.warning(f"fetch_symbol_ltp_with_retry: no trustworthy quote for {symbol}.{exchange} "
                        f"(attempt {attempt}/{max_attempts}) -- retrying in {retry_delay}s.")
            _time.sleep(retry_delay)
    return None


def fetch_symbol_bid_ask(client, symbol: str, exchange: str) -> tuple[Optional[float], Optional[float]]:
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() (bid/ask) failed for {symbol}: {exc}")
        return None, None
    if _is_error_response(resp) and resp.get("status") != "success":
        return None, None
    data = resp.get("data", resp) if isinstance(resp, dict) else resp
    if not isinstance(data, dict):
        return None, None
    bid = data.get("bid")
    ask = data.get("ask")
    return (float(bid) if bid is not None else None,
            float(ask) if ask is not None else None)


###############################################################################
# SIGNAL -- dual timeframe (Big/Small), MACD(9,23,7) + SuperTrend(8,3.2) on
# the REAL FUTURES price (see module docstring -- NOT the synthetic combo)
###############################################################################
@dataclass
class TimeframeSignal:
    close_prev1: float
    high_prev1: float
    low_prev1: float
    bullish: bool           # ST direction == -1.0 (uptrend) AND MACD line > signal line
    bearish: bool           # ST direction == 1.0 (downtrend) AND MACD line < signal line
    candle_key: str
    candle_start: datetime  # start-of-bucket time of this closed candle -- used to detect
                             # "the Big-timeframe candle active at entry has now closed"


_last_logged_candle: dict[str, str] = {}


def _fetch_interval_matches_target(fetch_interval: str, interval_minutes: int) -> bool:
    """True when fetch_interval is ALREADY the target granularity (no local
    resample needed). Handles 'D' (daily, interval_minutes>=1440 by
    convention) alongside the usual '15m'/'1h'-style broker interval
    strings -- unlike a bare `int(fetch_interval.rstrip("m"))`, which
    raises ValueError on 'D' (no trailing 'm' to strip). See Instance A
    (Big=1 Day), where fetch_interval='D' must never be routed through
    _resample_to_candle_interval -- that function's 09:15-anchored,
    intraday-only bucketing math is meaningless across multiple trading
    sessions."""
    if fetch_interval == "D":
        return interval_minutes >= 1440
    if fetch_interval.endswith("h"):
        return int(fetch_interval[:-1]) * 60 == interval_minutes
    if fetch_interval.endswith("m"):
        return int(fetch_interval[:-1]) == interval_minutes
    return False


def compute_timeframe_signal(client, inst: InstrumentConfig, futures_symbol: str,
                              interval_minutes: int, label: str, fetch_interval: str,
                              history_lookback_days: int) -> Optional[TimeframeSignal]:
    """Fetch `fetch_interval` (broker-native) history for the current-month
    futures contract, resample to interval_minutes ONLY if fetch_interval
    isn't already that granularity (see _fetch_interval_matches_target),
    compute MACD(9,23,7) + SuperTrend(8,3.2) off the last genuinely CLOSED
    bar. Returns None (logged) if anything is unavailable."""
    end = datetime.now(IST).date()
    raw_bars = client.history(
        symbol=futures_symbol, exchange=inst.futures_exchange,
        interval=fetch_interval,
        start_date=(end - timedelta(days=history_lookback_days)).isoformat(),
        end_date=end.isoformat(),
    )
    if _is_error_response(raw_bars):
        Log.warning(f"[{label}] {fetch_interval} history error response for {futures_symbol}: {raw_bars}")
        return None
    if raw_bars is None or raw_bars.empty:
        Log.warning(f"[{label}] empty {fetch_interval} history for {futures_symbol}.")
        return None

    bars = raw_bars if _fetch_interval_matches_target(fetch_interval, interval_minutes) \
        else _resample_to_candle_interval(raw_bars, interval_minutes)
    if bars.empty:
        Log.warning(f"[{label}] resample to {interval_minutes}m produced no bars.")
        return None
    # Drop the last row ONLY if it's genuinely still forming (its own
    # implied close time is still in the future) -- NOT unconditionally by
    # position. Confirmed live, 2026-08-20 via server logs: when a refresh
    # fetch lands right at a candle boundary before the broker has
    # published even the first native bar of the NEW bucket, the resampled
    # last row IS the fully-CLOSED previous bucket (no data exists yet to
    # form a later row) -- blindly dropping it by position discarded a
    # whole interval of real, usable data every time, self-correcting only
    # at the NEXT boundary (a silent, consistent one-full-interval-late
    # signal -- two history() calls exactly one interval apart, each
    # reaching data only up to the boundary instant). Mirrors the
    # implied-close-vs-wall-clock check already used correctly in the
    # accelerated replay harness's _slice_futures_bars.
    if len(bars):
        implied_close = bars.index[-1].to_pydatetime() + timedelta(minutes=interval_minutes)
        if implied_close > datetime.now(IST):
            bars = bars.iloc[:-1]

    warmup_needed = max(config.macd_slow + config.macd_signal, config.supertrend_period) + 3
    if len(bars) < warmup_needed:
        Log.warning(f"[{label}] only {len(bars)} {interval_minutes}m bars after dropping "
                    f"the still-forming one (need >= {warmup_needed}) -- no signal.")
        return None

    close = bars["close"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)

    macd_line, signal_line, _hist = ta.macd(close, config.macd_fast, config.macd_slow, config.macd_signal)
    macd_line = np.asarray(macd_line)
    signal_line = np.asarray(signal_line)
    _st, direction = ta.supertrend(high, low, close, config.supertrend_period, config.supertrend_multiplier)
    direction = np.asarray(direction)

    if np.isnan(macd_line[-1]) or np.isnan(signal_line[-1]) or np.isnan(direction[-1]):
        Log.warning(f"[{label}] MACD/SuperTrend still NaN after warmup floor -- no signal this cycle.")
        return None

    candle_key = str(bars.index[-1])
    candle_start = bars.index[-1].to_pydatetime()
    bullish = bool(direction[-1] == -1.0 and macd_line[-1] > signal_line[-1])
    bearish = bool(direction[-1] == 1.0 and macd_line[-1] < signal_line[-1])

    signal = TimeframeSignal(
        close_prev1=float(close[-1]), high_prev1=float(high[-1]), low_prev1=float(low[-1]),
        bullish=bullish, bearish=bearish, candle_key=candle_key, candle_start=candle_start,
    )

    log_key = f"{label}:{candle_key}"
    if _last_logged_candle.get(label) != log_key:
        _last_logged_candle[label] = log_key
        Log.info(f"[{label}] futures={futures_symbol} candle={candle_key} close={signal.close_prev1:.2f} "
                  f"ST_dir={'up' if direction[-1] == -1.0 else 'down'} MACD={macd_line[-1]:.2f} "
                  f"signal={signal_line[-1]:.2f} bullish={bullish} bearish={bearish}")
    return signal


###############################################################################
# STRIKE SELECTION -- synthetic ATM combo + hedge (module docstring's
# Execution/Hedging sections). Strikes are ALWAYS derived fresh from the
# current futures price -- never carried forward across a rollover.
###############################################################################
def _round_to_strike(price: float, round_to: int) -> int:
    return int(round(price / round_to) * round_to)


def resolve_atm_strike(futures_px: float) -> int:
    return _round_to_strike(futures_px, config.atm_strike_round)


def resolve_hedge_strike(futures_px: float, direction: str, chain_strikes: list) -> Optional[int]:
    """Hedge leg is further OTM, SAME direction as the exposed short leg
    (module docstring: Long -> exposed short leg is the sold Put -> hedge is
    a further-OTM Put; Short -> exposed short leg is the sold Call -> hedge
    is a further-OTM Call), 2-3% away from the current futures price.
    Picks the listed strike closest to the middle of that 2-3% band."""
    if not chain_strikes:
        return None
    low_band = futures_px * (1 - config.hedge_otm_pct_high if direction == "LONG" else 1 + config.hedge_otm_pct_low)
    high_band = futures_px * (1 - config.hedge_otm_pct_low if direction == "LONG" else 1 + config.hedge_otm_pct_high)
    band_mid = (low_band + high_band) / 2
    candidates = [s for s in chain_strikes if min(low_band, high_band) <= s <= max(low_band, high_band)]
    pool = candidates if candidates else chain_strikes
    return min(pool, key=lambda s: abs(s - band_mid))


def _correct_breached_sl_reference(direction: str, entry_futures_px: float,
                                    sl_high: float, sl_low: float) -> tuple[float, float]:
    """FRESH ENTRY ONLY (2026-08-20 fix): the reference candle (sig.high_prev1/
    low_prev1) is the last CLOSED candle of whichever timeframe drove the
    entry -- correctly timeframe-matched already (BIG entry -> BIG candle,
    SMALL entry -> SMALL candle; irrelevant which one, same correction
    either way). But entry_futures_px is the LIVE tick price at decision
    time, not that candle's own close -- live price can drift past the
    candle's own high/low in the time since it closed. Confirmed via a
    3.2-year backtest (2026-08-20): 105 of ~2196 fresh entries had the
    candle reference already on the WRONG side of the live entry price
    (e.g. a LONG's sl_reference_low sitting ABOVE its own entry price) --
    the trade is technically born already past its own candle-break stop.

    Fix: when breached, reflect the reference to the correct side of entry,
    preserving the SAME distance the candle originally implied (not an
    arbitrary new distance, not discarding the candle's own volatility
    read) -- gap = how far past its own stop entry already is;
    new_reference = entry_futures_px -/+ gap."""
    if direction == "LONG" and sl_low >= entry_futures_px:
        gap = sl_low - entry_futures_px
        sl_low = entry_futures_px - gap
    elif direction == "SHORT" and sl_high <= entry_futures_px:
        gap = entry_futures_px - sl_high
        sl_high = entry_futures_px + gap
    return sl_high, sl_low


def fetch_chain_strikes(client, inst: InstrumentConfig, expiry_compact: str) -> list:
    """Listed strikes only. Uses the REAL installed openalgo SDK's
    optionchain() signature -- confirmed 2026-08-17 via `help(api.
    optionchain)`: keyword-only `underlying=`/`exchange=`/`expiry_date=`/
    `strike_count=`, response shape `{"chain": [{"strike":..., "ce":{...},
    "pe":{...}}, ...]}`.

    strike_count=config.chain_strike_count + with_quotes=False -- confirmed
    2026-08-20 by tracing the full path (openalgo/options.py forwards
    with_quotes via **kwargs -> restx_api/option_chain.py's OptionChainSchema
    -> services/option_chain_service.py Step 8), unlike an earlier, WRONG
    conclusion this week that with_quotes didn't exist on the installed SDK.
    with_quotes=False skips the broker multiquote fan-out entirely (strikes/
    symbols/lotsize come straight from cache/DB, prices left at 0) -- safe
    here since only `strike` is ever read, never a premium. Unbounded +
    with_quotes=True (the old code) measured 32.53s live 2026-08-20 (244
    rows, fanning a quote call per strike) vs 5.80s at strike_count=20 alone;
    adding with_quotes=False removes the quote fan-out altogether."""
    resp = client.optionchain(underlying=inst.name, exchange=inst.options_exchange, expiry_date=expiry_compact,
                               strike_count=config.chain_strike_count, with_quotes=False)
    if _is_error_response(resp) and resp.get("status") != "success":
        Log.warning(f"optionchain() error for {inst.name} {expiry_compact}: {resp}")
        return []
    chain = resp.get("chain", []) if isinstance(resp, dict) else []
    strikes = set()
    for row in chain:
        if isinstance(row, dict) and "strike" in row:
            try:
                strikes.add(float(row["strike"]))
            except (TypeError, ValueError):
                continue
    return sorted(strikes)


def option_symbol(inst: InstrumentConfig, expiry_compact: str, strike: int, right: str) -> str:
    strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
    return f"{inst.name}{expiry_compact}{strike_str}{right}"


def build_combo_legs(inst: InstrumentConfig, expiry_compact: str, direction: str,
                      atm_strike: int, hedge_strike: Optional[int]) -> dict:
    """Returns {'core_call': OptionLeg, 'core_put': OptionLeg, 'hedge': OptionLeg}
    -- unfilled skeletons (role/symbol/action/quantity only). direction is
    'LONG' or 'SHORT'.
        LONG  -> Buy ATM Call + Sell ATM Put, hedge = further-OTM Put (BUY)
        SHORT -> Sell ATM Call + Buy ATM Put, hedge = further-OTM Call (BUY)
    Hedge is always a BUY (it caps risk on the sold leg).

    quantity = lot_multiplier * nifty_lot_size (real SHARE count) --
    corrected 2026-08-20. There is NO platform-side automatic lot
    conversion; placeorder()'s quantity is taken literally as share count
    (confirmed against Nifty_OI_WeeklyBuy_MonthlySell's own
    quantity=65 == 1 lot). The previous version sent quantity=
    config.lot_multiplier (1) directly -- a real 1-SHARE order, which NFO
    doesn't allow (options trade in whole lots only; a real order would
    have been rejected outright) -- and used that same wrong quantity for
    this strategy's own PnL math all along, under-reporting every PnL
    figure by a factor of nifty_lot_size (65) versus what a real position
    would show. Caught while manually verifying today's live dry-run PnL
    against a fresh broker quote fetch."""
    qty = config.lot_multiplier * config.nifty_lot_size
    call_symbol = option_symbol(inst, expiry_compact, atm_strike, "CE")
    put_symbol = option_symbol(inst, expiry_compact, atm_strike, "PE")
    if direction == "LONG":
        core_call = OptionLeg(role="core_call", symbol=call_symbol, action="BUY", quantity=qty)
        core_put = OptionLeg(role="core_put", symbol=put_symbol, action="SELL", quantity=qty)
        hedge_right = "PE"
    else:
        core_call = OptionLeg(role="core_call", symbol=call_symbol, action="SELL", quantity=qty)
        core_put = OptionLeg(role="core_put", symbol=put_symbol, action="BUY", quantity=qty)
        hedge_right = "CE"
    hedge = OptionLeg(role="hedge", quantity=qty, action="BUY")
    if hedge_strike is not None:
        hedge.symbol = option_symbol(inst, expiry_compact, hedge_strike, hedge_right)
    return {"core_call": core_call, "core_put": core_put, "hedge": hedge}


###############################################################################
# ORDER LIFECYCLE -- broker-agnostic, ported verbatim from
# MCX_CrudeOil_EMA9_RSI_Intraday_1 (AUTHORING_CHECKLIST.md reference #1).
# See that file's docstrings for the full rationale; unchanged here.
###############################################################################
class OrderNeedsAttention(Exception):
    """poll_fill() exhausted its automatic reprice attempts but the order is
    still resting, UNFILLED, at the broker -- not cancelled. Distinguishes
    "nothing left to act on" (order_id is dead) from "still live, needs a
    human decision" (this)."""
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


def place(client, strategy: str, symbol: str, exchange: str, action: str, quantity: int,
          dry_run_ltp: Optional[float] = None) -> tuple[str, bool]:
    """Places an order. Returns (orderid, is_dry_run_fill). In dry-run mode
    (config.dry_run=True) NO real order is sent -- logs a "[DRY RUN] would
    place..." line and returns a synthetic orderid + is_dry_run_fill=True so
    every downstream caller (poll_fill etc.) is skipped for this leg and the
    simulated fill price (dry_run_ltp, LTP at the decision moment) is used
    directly. Only retries a CLEAN rejection response (nothing was placed,
    safe to retry) up to config.place_order_max_attempts times -- does NOT
    retry a raised exception (ambiguous outcome, could duplicate a real
    order)."""
    import time as _time

    if config.dry_run:
        synthetic_id = f"DRYRUN-{datetime.now(IST).strftime('%Y%m%d%H%M%S%f')}"
        Log.info(f"[DRY RUN] would place {action} {quantity} x {symbol}.{exchange} "
                 f"(strategy={strategy}) @ simulated LTP={dry_run_ltp} -- order id {synthetic_id}")
        return synthetic_id, True

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
            return resp["orderid"], False
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

_TRADE_LOG_HEADER = ["execution_id", "direction", "leg", "symbol", "action", "quantity",
                     "entry_time", "entry_px", "exit_time", "exit_px", "pnl_points",
                     "pnl_rupees", "exit_reason", "is_dry_run",
                     "entry_timeframe", "controlling_timeframe_at_exit", "handoff_occurred", "handoff_ts",
                     "sl_pct_amount", "sl_pct_level", "sl_candle_reference_high", "sl_candle_reference_low"]


def _trade_log_writer_loop():
    while True:
        item = _trade_log_queue.get()
        try:
            if item is None:
                break
            (strategy_tag, execution_id, direction, role, symbol, action, quantity,
             entry_time, entry_px, exit_time, exit_px, exit_reason, is_dry_run,
             entry_timeframe, controlling_timeframe_at_exit, handoff_ts,
             sl_pct_level, sl_candle_reference_high, sl_candle_reference_low) = item
            log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
            is_new = not log_path.exists()
            # BUY leg profits when exit > entry; SELL leg profits when entry > exit.
            pnl_points = (exit_px - entry_px) if action == "BUY" else (entry_px - exit_px)
            pnl_rupees = pnl_points * quantity
            handoff_occurred = bool(handoff_ts)
            with log_path.open("a", newline="") as fp:
                writer = csv.writer(fp)
                if is_new:
                    writer.writerow(_TRADE_LOG_HEADER)
                writer.writerow([execution_id, direction, role, symbol, action, quantity,
                                  entry_time, round(entry_px, 2), exit_time, round(exit_px, 2),
                                  round(pnl_points, 2), round(pnl_rupees, 2), exit_reason, is_dry_run,
                                  entry_timeframe, controlling_timeframe_at_exit, handoff_occurred, handoff_ts,
                                  f"{config.sl_pct * 100:.0f}%", round(sl_pct_level, 2),
                                  round(sl_candle_reference_high, 2), round(sl_candle_reference_low, 2)])
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


def append_trade_log(strategy_tag: str, execution_id: int, direction: str, role: str, symbol: str,
                      action: str, quantity: int, entry_time: str, entry_px: float, exit_time: str,
                      exit_px: float, exit_reason: str, is_dry_run: bool,
                      entry_timeframe: str, controlling_timeframe_at_exit: str, handoff_ts: str,
                      sl_pct_level: float, sl_candle_reference_high: float, sl_candle_reference_low: float):
    _ensure_trade_log_thread()
    _trade_log_queue.put((strategy_tag, execution_id, direction, role, symbol, action, quantity,
                          entry_time, entry_px, exit_time, exit_px, exit_reason, is_dry_run,
                          entry_timeframe, controlling_timeframe_at_exit, handoff_ts,
                          sl_pct_level, sl_candle_reference_high, sl_candle_reference_low))


def _trade_log_extra_args(pos: "CombinedPosition") -> tuple:
    """The trade-level (not leg-level) columns appended to every
    append_trade_log() call -- entry vs controlling timeframe, handoff
    time, and both SL levels (1% and candle-break), all read straight off
    the position at close time. sl_pct_level is derived fresh here rather
    than stored, same formula _check_stop_loss uses."""
    sl_pct_level = (pos.entry_futures_px * (1 - config.sl_pct) if pos.direction == "LONG"
                    else pos.entry_futures_px * (1 + config.sl_pct))
    return (pos.entry_timeframe, pos.controlling_timeframe, pos.handoff_ts,
            sl_pct_level, pos.sl_reference_high, pos.sl_reference_low)


###############################################################################
# PLATFORM REPORTING -- STRATEGY_REPORTING_PORT loopback, never the main
# Flask port (see AUTHORING_CHECKLIST.md and docs/CUSTOMIZATIONS.md).
###############################################################################
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


def push_leg_error(env: "Environment", leg_key: str, leg: "OptionLeg", action: str = "", clear: bool = False):
    payload = json.dumps({
        "apikey": env.api_key,
        "leg_key": leg_key,
        "error_state": leg.error_state,
        "error_kind": leg.error_kind,
        "error_message": leg.error_message,
        "error_since": leg.error_since,
        "symbol": leg.symbol,
        "quantity": leg.quantity,
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
# STRATEGY ENGINE -- entry search (Big-first, fall to Small), dual-timeframe
# handoff, SL, post-SL re-entry, rollover force-close-and-reopen, 3-leg
# atomic order group. See module docstring for the full spec.
###############################################################################
_LEG_ROLES = ("core_call", "core_put", "hedge")


class StrategyEngine:
    def __init__(self, client, store: StateStore, env: Environment, price_stream: "PriceStream",
                 execution_id: int = 0, ltp_client=None):
        self.client = client
        self.ltp_client = ltp_client or client
        self.store = store
        self.env = env
        self.price_stream = price_stream
        self.state = store.load()
        self.execution_id = execution_id or self.state.last_execution_id
        self.inst = INSTRUMENTS[0]
        # Separate pools (AUTHORING_CHECKLIST.md): fill-watching/retry
        # resolution can block for minutes in a reprice loop and must never
        # starve reporting/error-push/force-exit/pending-action checks.
        self._fill_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nifty-tf-fill")
        self._bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nifty-tf-bg")
        self._lock = threading.RLock()
        self._big_last_boundary: Optional[str] = None
        self._small_last_boundary: Optional[str] = None
        # Day-boundary cache for _handle_rollover -- without this it re-calls
        # client.expiry() (options + futures, 2 real broker calls) on EVERY
        # run_cycle (every config.scheduler_interval=10s, unthrottled),
        # unlike _refresh_signals' candle-boundary-aware pattern. Set only on
        # a SUCCESSFUL resolution so a transient API failure retries on the
        # very next cycle rather than being silently deferred a full day.
        self._rollover_last_checked_day: Optional[str] = None
        # Reentrancy guard for backgrounded _open_position -- same shape as
        # Nifty_OI_WeeklyBuy_MonthlySell's _eval_pending/_dispatch_eval_bg
        # (that file's own comment documents the real production problem
        # this pattern fixes: _open_position's multi-round-trip optionchain()/
        # quotes()/placeorder() sequence running inline on run_cycle's own
        # thread can outlast scheduler_interval, and APScheduler
        # (max_instances=1) then silently SKIPS the next tick rather than
        # overlapping -- delaying SL/handoff/signal-exit monitoring of the
        # position that's mid-open by up to that overrun). Only one position
        # can ever exist at a time here, so a bool suffices (no per-leg-key
        # set needed like the multi-leg OI strategy).
        self._entry_pending: bool = False
        self._last_signal_big: Optional[TimeframeSignal] = None
        self._last_signal_small: Optional[TimeframeSignal] = None
        self._last_error_repush = 0.0
        # Candle-boundary refresh retry cooldown -- see Config.signal_refresh_retry_cooldown_sec.
        self._big_last_attempt_ts = 0.0
        self._small_last_attempt_ts = 0.0
        # Order error recovery (Retry/Cancel/Manually Completed), keyed by
        # leg role ("core_call"/"core_put"/"hedge") since only one combined
        # position can be open at a time.
        self._pending_fills: set = set()
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        # Force Exit -- refreshed in the background, never blocking run_cycle.
        self._force_exit_cache = False
        self._force_exit_inflight = False
        self._force_exit_in_progress = False

    def _save_state(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        self.state.last_execution_id = self.execution_id
        self.store.save()

    # -------------------------------------------------------------------
    # Day reset / rollover
    # -------------------------------------------------------------------
    def _reset_day_if_needed(self):
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if self.state.current_day != today:
            Log.info(f"New trading day {today} (was {self.state.current_day or 'none'}) -- "
                     f"resetting daily counters.")
            self.state.current_day = today
            self.state.instance.trade_count_today = 0
            self.state.today_realized_pnl = 0.0
            self._big_last_boundary = None
            self._small_last_boundary = None
            self._big_last_attempt_ts = 0.0
            self._small_last_attempt_ts = 0.0
            self._save_state()

    def _handle_rollover(self):
        """Resolve the current month's options expiry + futures symbol, and
        force-close-then-reopen any open position if this call causes the
        contract to actually roll (see resolve_current_month_context).
        Force-close BLOCKS (wait_for_fills=True) until every leg is
        genuinely confirmed closed before a fresh position is opened --
        never races an async fill-watcher against a fresh entry.

        Gated to run at most once per calendar day (day-boundary cache,
        _rollover_last_checked_day) -- run_cycle calls this unconditionally
        every cycle (every config.scheduler_interval=10s), and without this
        gate it would call client.expiry() (2 real broker calls: options +
        futures) every single cycle, unthrottled, all day -- unlike
        _refresh_signals' candle-boundary-aware pattern. Cache is set only
        on a SUCCESSFUL resolution, so a transient API failure retries on
        the very next cycle rather than being silently deferred a full day."""
        if self._entry_pending:
            # An entry is currently being opened on a background thread --
            # self.state.instance.position.direction is still empty until
            # that finishes, so we cannot yet tell whether there's about to
            # be a position on the OUTGOING contract that rollover needs to
            # force-close. Defer this cycle entirely (found via code review,
            # 2026-08-19: proceeding here on a stale "flat" reading would
            # skip force-closing a position the bg thread is about to write,
            # leaving it open on the old contract past rollover). Harmless
            # to defer -- rollover doesn't need split-second timing, only
            # to happen once the entry has actually resolved one way or the
            # other, which the very next cycle will see.
            return
        # The 09:30 gate below must NEVER block the cold-start resolution of
        # self.state.futures_symbol -- run_cycle's own guard
        # (`if not self.state.futures_symbol: self._handle_rollover(); if
        # not self.state.futures_symbol: return`) means the WHOLE strategy
        # does nothing at all, every cycle, until futures_symbol is set.
        # Confirmed live, 2026-08-20: gating the entire function blocked
        # everything from market open until 09:30 on a fresh deployment.
        # Safe to bypass on a cold start specifically -- there is no
        # existing position to protect from thin/volatile pricing when
        # futures_symbol has never been resolved yet (a position cannot
        # exist without it).
        cold_start = not self.state.futures_symbol
        if not cold_start and not config.test_mode and datetime.now(IST).time() < config.rollover_earliest_time:
            return  # 09:15-09:30 is often thin/volatile -- wait before force-closing/reopening
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        if self._rollover_last_checked_day == today_str:
            return
        try:
            ctx = resolve_current_month_context(self.client, self.inst, self.state.contract_start_date)
        except Exception as exc:
            Log.warning(f"resolve_current_month_context failed -- keeping last-known contract "
                        f"({self.state.futures_symbol or 'none'}): {exc}")
            return
        self._rollover_last_checked_day = today_str
        prior_expiry = self.state.expiry_compact
        prior_futures_symbol = self.state.futures_symbol
        self.state.expiry_compact = ctx["expiry_compact"]
        self.state.futures_symbol = ctx["futures_symbol"]
        self.state.contract_start_date = ctx["contract_start_date"]
        if ctx["futures_symbol"] != prior_futures_symbol:
            # WS subscription for the futures symbol -- covers both the
            # cold-start resolution (prior_futures_symbol == "") and a
            # genuine month-to-month rollover (unsubscribe the old symbol,
            # subscribe the new one). Without this, run_cycle's
            # price_stream.get_ltp() for the futures price would ALWAYS
            # return None (never-subscribed cache), silently falling
            # through to a REST fetch_symbol_ltp() call on every single
            # cycle forever -- the WS-first design was never actually wired
            # up for this symbol.
            if prior_futures_symbol:
                self.price_stream.remove_instruments(
                    [{"symbol": prior_futures_symbol, "exchange": self.inst.futures_exchange}])
            self.price_stream.add_instruments(
                [{"symbol": ctx["futures_symbol"], "exchange": self.inst.futures_exchange}])
        if not ctx["rolled_today"] or prior_expiry == ctx["expiry_compact"]:
            self._save_state()
            return
        Log.info(f"ROLLOVER: contract start moved to {ctx['contract_start_date']} -- "
                 f"now trading {ctx['expiry_compact']} / {ctx['futures_symbol']} "
                 f"(was {prior_expiry or 'none'}).")
        pos = self.state.instance.position
        if pos.direction and not pos.closing:
            Log.info(f"ROLLOVER: an open {pos.direction} position exists on the outgoing contract -- "
                     f"force-closing (all legs) before reopening on the new month.")
            direction = pos.direction
            # v2 SL-frozen: capture the ORIGINAL SL anchor/candle reference
            # before _close_position() wipes pos back to flat -- carried
            # forward into the reopened position unchanged, per explicit
            # instruction (2026-08-20): "rollover reopen -- old SL stays as
            # is." Strikes still re-resolve off the current futures price
            # below (unaffected).
            frozen_sl_anchor_px = pos.entry_futures_px
            frozen_sl_high = pos.sl_reference_high
            frozen_sl_low = pos.sl_reference_low
            fully_closed = self._close_position(reason="rollover", wait_for_fills=True)
            if not fully_closed:
                Log.warning("ROLLOVER: not every leg confirmed closed (one or more needs Retry/"
                            "Cancel/Manually Completed) -- skipping reopen this cycle; will retry "
                            "once the stuck leg(s) are resolved.")
                self._save_state()
                return
            futures_px = fetch_symbol_ltp(self.client, ctx["futures_symbol"], self.inst.futures_exchange)
            if futures_px is None:
                Log.warning("ROLLOVER: could not fetch new-month futures LTP -- skipping reopen "
                            "this cycle, will retry entry search normally on the next cycle.")
                self._save_state()
                return
            # v2 SL-frozen: reopen carries forward the ORIGINAL SL anchor
            # price and candle reference captured above, NOT a fresh
            # reference candle on the new month's futures (that was v1's
            # behavior -- see the sibling deployed file for the original
            # "fresh reference candle" version). Reopen always monitors on
            # BIG by default (matches the entry-search convention of always
            # starting on Big; there's no real "entry candle" for a
            # rollover-triggered reopen the way there is for a genuine
            # Big/Small entry search).
            # Strikes are still re-derived FRESH inside _open_position from
            # the current futures_px -- only the SL anchor/reference is frozen.
            # 2026-08-20: apply the same breach-reflect correction here too --
            # a carried reference from many rollovers/re-entries back can be
            # stale relative to the (also carried, unchanged) anchor price.
            # Confirmed via backtest: without this, ~5-15% of carried
            # references end up on the wrong side of their own anchor.
            frozen_sl_high, frozen_sl_low = _correct_breached_sl_reference(
                direction, frozen_sl_anchor_px, frozen_sl_high, frozen_sl_low)
            self._open_position(direction=direction, timeframe="BIG", futures_px=futures_px,
                                sl_high=frozen_sl_high, sl_low=frozen_sl_low, big_candle_key_at_entry="",
                                sl_anchor_futures_px=frozen_sl_anchor_px)
        self._save_state()

    # -------------------------------------------------------------------
    # Signal refresh -- independent per-timeframe candle-boundary tracking,
    # with a retry cooldown so a failed fetch doesn't hammer history() every
    # single scheduler tick until data lands.
    # -------------------------------------------------------------------
    def _refresh_signals(self) -> tuple[bool, bool]:
        """Returns (big_advanced, small_advanced) -- True for a timeframe
        iff its candle boundary actually moved to a NEW closed candle THIS
        call (not merely re-fetched/unchanged). Used by run_cycle to gate
        the candle-break SL check so it only fires on a genuine new close
        of the CONTROLLING timeframe, never on continuous intrabar LTP --
        see _check_stop_loss's own docstring (user correction, 2026-08-17:
        "for sl candle break, price should wait for candle close")."""
        now_ts = datetime.now(IST).timestamp()
        big_boundary = _current_candle_boundary(config.big_timeframe_minutes)
        small_boundary = _current_candle_boundary(config.small_timeframe_minutes)
        big_key = str(big_boundary)
        small_key = str(small_boundary)
        big_advanced = False
        small_advanced = False

        if (big_key != self._big_last_boundary
                and now_ts - self._big_last_attempt_ts >= config.signal_refresh_retry_cooldown_sec):
            self._big_last_attempt_ts = now_ts
            sig = compute_timeframe_signal(self.client, self.inst, self.state.futures_symbol,
                                            config.big_timeframe_minutes, "BIG",
                                            config.big_fetch_interval, config.history_lookback_days)
            if sig is not None:
                self._last_signal_big = sig
                self._big_last_boundary = big_key
                big_advanced = True
        if (small_key != self._small_last_boundary
                and now_ts - self._small_last_attempt_ts >= config.signal_refresh_retry_cooldown_sec):
            self._small_last_attempt_ts = now_ts
            sig = compute_timeframe_signal(self.client, self.inst, self.state.futures_symbol,
                                            config.small_timeframe_minutes, "SMALL",
                                            config.small_fetch_interval, config.history_lookback_days)
            if sig is not None:
                self._last_signal_small = sig
                self._small_last_boundary = small_key
                small_advanced = True
        return big_advanced, small_advanced

    # -------------------------------------------------------------------
    # Entry search -- Big-first, fall to Small (module docstring's
    # Entry-search-priority section)
    # -------------------------------------------------------------------
    def _search_entry(self) -> Optional[tuple[str, str, TimeframeSignal]]:
        """Returns (direction, timeframe_label, controlling_signal) or None.
        timeframe_label is 'BIG' or 'SMALL'."""
        big = self._last_signal_big
        if big is not None:
            if big.bullish:
                return "LONG", "BIG", big
            if big.bearish:
                return "SHORT", "BIG", big
        small = self._last_signal_small
        if small is not None:
            if small.bullish:
                return "LONG", "SMALL", small
            if small.bearish:
                return "SHORT", "SMALL", small
        return None

    # -------------------------------------------------------------------
    # Handoff -- once a trade is running on SMALL, recheck sync each time
    # the Big candle that was forming at entry closes.
    # -------------------------------------------------------------------
    def _check_handoff(self) -> bool:
        """Returns True iff monitoring was JUST handed off to BIG this call
        (a genuine SMALL->BIG transition, this cycle). run_cycle uses this
        to suppress the candle-break SL check on this SAME tick -- the SL
        reference was just freshly set FROM this same just-closed Big
        candle's own high/low, so comparing that candle against its own
        brand-new reference is a self-referential comparison that is
        ALWAYS true (X <= X), triggering an immediate spurious stop the
        instant every handoff occurs. Confirmed as a real bug via backtest,
        2026-08-17 -- the candle-break check must only ever apply to a
        candle that closes AFTER the reference was set, never the one that
        set it."""
        pos = self.state.instance.position
        if not pos.direction or pos.closing or pos.controlling_timeframe != "SMALL":
            return False
        big = self._last_signal_big
        if big is None or big.candle_key == pos.big_candle_key_at_entry:
            return False  # Big candle active at entry hasn't closed yet (or unavailable)
        in_sync = (pos.direction == "LONG" and big.bullish) or (pos.direction == "SHORT" and big.bearish)
        just_handed_off = False
        with self._lock:
            if in_sync:
                Log.info(f"HANDOFF: Big timeframe candle closed and agrees with the open {pos.direction} "
                         f"position -- handing monitoring + SL reference to BIG.")
                pos.controlling_timeframe = "BIG"
                # 2026-08-20: apply the same breach-reflect correction here
                # too -- the Big candle just closed covers a much wider
                # window than the position's own (unchanged) entry price,
                # so it can easily land on the wrong side of it. Confirmed
                # via backtest: 170/441 handed-off trades (38.5%) had an
                # invalid post-handoff reference before this fix.
                new_high, new_low = _correct_breached_sl_reference(
                    pos.direction, pos.entry_futures_px, big.high_prev1, big.low_prev1)
                pos.sl_reference_high = new_high
                pos.sl_reference_low = new_low
                pos.handoff_ts = datetime.now(IST).isoformat()
                just_handed_off = True
            else:
                Log.info("HANDOFF: Big timeframe candle closed but is still out of sync with the open "
                         f"{pos.direction} position -- continuing to monitor on SMALL.")
                # Record this Big candle as "already rechecked" so we don't
                # re-log every cycle until the NEXT Big candle closes. This
                # is a SEPARATE field from entry_timeframe (the TF label) --
                # conflating the two used to corrupt the handoff check.
                pos.big_candle_key_at_entry = big.candle_key
            self._save_state()
        return just_handed_off

    # -------------------------------------------------------------------
    # Stop-loss check -- TWO independent mechanisms, deliberately checked
    # on different cadences (user correction, 2026-08-17):
    #   - 1% of entry futures price: continuous, LTP-driven -- checked
    #     every run_cycle tick against live futures_px, same as before.
    #   - Reference candle high/low broken: ONLY evaluated when the
    #     CONTROLLING timeframe's own candle actually CLOSES (candle_
    #     just_closed=True, from _refresh_signals' return value) --
    #     compared against that closed candle's own high/low
    #     (TimeframeSignal.high_prev1/low_prev1), never continuous
    #     intrabar LTP. A tick where the controlling timeframe hasn't
    #     produced a new close this cycle skips the candle-break check
    #     entirely (stays pending until the next real close), even if
    #     live LTP is already trading through the reference level.
    # -------------------------------------------------------------------
    def _check_stop_loss(self, futures_px: float, candle_just_closed: bool) -> bool:
        pos = self.state.instance.position
        if not pos.direction or pos.closing:
            return False
        sl_pct_level = (pos.entry_futures_px * (1 - config.sl_pct) if pos.direction == "LONG"
                         else pos.entry_futures_px * (1 + config.sl_pct))
        pct_hit = (futures_px <= sl_pct_level) if pos.direction == "LONG" else (futures_px >= sl_pct_level)

        candle_hit = False
        candle_low_prev1 = None
        candle_high_prev1 = None
        if candle_just_closed:
            sig = self._last_signal_big if pos.controlling_timeframe == "BIG" else self._last_signal_small
            if sig is not None:
                candle_low_prev1 = sig.low_prev1
                candle_high_prev1 = sig.high_prev1
                candle_hit = ((sig.low_prev1 <= pos.sl_reference_low) if pos.direction == "LONG"
                              else (sig.high_prev1 >= pos.sl_reference_high))

        if pct_hit or candle_hit:
            reason = "sl_pct" if pct_hit else "sl_candle"
            # candle_hit compares the just-closed candle's own high/low
            # (candle_low_prev1/candle_high_prev1) against the frozen
            # reference -- NOT futures_px, which is only the live LTP used
            # for the separate 1% check. Without printing the candle's own
            # value here, a "sl_candle" line can show futures_px sitting
            # comfortably inside [ref_low, ref_high] and look like a false
            # trigger, when the candle actually spiked past the reference
            # intra-candle before closing back inside it (confirmed live,
            # 2026-08-21: sl_candle fired on SHORT with futures_px=24380.00
            # inside a [24354.00, 24386.00] channel -- correct, since that
            # candle's own high had reached >= 24386.00).
            candle_detail = (
                f" candle_low={candle_low_prev1:.2f} candle_high={candle_high_prev1:.2f}"
                if candle_hit else ""
            )
            Log.info(f"STOP LOSS ({reason}): {pos.direction} futures_px={futures_px:.2f} "
                     f"entry={pos.entry_futures_px:.2f} sl_pct_level={sl_pct_level:.2f} "
                     f"ref_low={pos.sl_reference_low:.2f} ref_high={pos.sl_reference_high:.2f}"
                     f"{candle_detail}")
            with self._lock:
                inst_state = self.state.instance
                inst_state.awaiting_sl_reentry = True
                inst_state.sl_reentry_direction = pos.direction
                inst_state.sl_reentry_timeframe = pos.controlling_timeframe
                inst_state.sl_reentry_futures_px = pos.entry_futures_px
                # v2 SL-frozen: capture the original candle reference too --
                # used by _check_sl_reentry to keep it frozen through the
                # re-entry, instead of v1's fresh recovery-candle reference.
                inst_state.sl_reentry_sl_high = pos.sl_reference_high
                inst_state.sl_reentry_sl_low = pos.sl_reference_low
                self._save_state()
            self._close_position(reason=reason)
            return True
        return False

    # -------------------------------------------------------------------
    # Signal-loss exit -- BOTH ST(8,3.2) and MACD(9,23,7) on the CONTROLLING
    # timeframe (Big or Small, whichever currently governs monitoring) must
    # stay in sync with the trade direction; if agreement breaks, close
    # immediately, independent of the price-based SL checks above. User
    # confirmed 2026-08-17: "we need to check both indicator should be in
    # sync, if not then exit -- applies to big timeframe as small timeframe
    # as applicable." Matches the original IBBM notes' "follow Big time
    # frame for trade exit" line, which the earlier build of this file had
    # never actually implemented as its own exit mechanism -- only price-
    # based SL existed until now. Checked on the SAME cadence as the
    # candle-break SL (only when the controlling timeframe's own candle
    # just closed, never continuous/intrabar) -- a signal reading is only
    # ever refreshed on a closed candle in the first place, so there is no
    # meaningful "continuous" version of this check.
    # -------------------------------------------------------------------
    def _check_signal_exit(self, candle_just_closed: bool) -> bool:
        pos = self.state.instance.position
        if not pos.direction or pos.closing or not candle_just_closed:
            return False
        sig = self._last_signal_big if pos.controlling_timeframe == "BIG" else self._last_signal_small
        if sig is None:
            return False
        in_sync = (pos.direction == "LONG" and sig.bullish) or (pos.direction == "SHORT" and sig.bearish)
        if in_sync:
            return False
        Log.info(f"SIGNAL EXIT: {pos.controlling_timeframe} ST+MACD no longer agree with the open "
                 f"{pos.direction} position -- closing (independent of price-based SL).")
        with self._lock:
            inst_state = self.state.instance
            inst_state.awaiting_sl_reentry = True
            inst_state.sl_reentry_direction = pos.direction
            inst_state.sl_reentry_timeframe = pos.controlling_timeframe
            inst_state.sl_reentry_futures_px = pos.entry_futures_px
            inst_state.sl_reentry_sl_high = pos.sl_reference_high
            inst_state.sl_reentry_sl_low = pos.sl_reference_low
            self._save_state()
        self._close_position(reason="signal_exit")
        return True

    # -------------------------------------------------------------------
    # Post-SL sequencing -- re-entry check FIRST, fresh search only if the
    # signal genuinely changed.
    # -------------------------------------------------------------------
    def _check_sl_reentry(self, futures_px: float) -> bool:
        inst_state = self.state.instance
        if not inst_state.awaiting_sl_reentry:
            return False
        direction = inst_state.sl_reentry_direction
        tf_label = inst_state.sl_reentry_timeframe
        sig = self._last_signal_big if tf_label == "BIG" else self._last_signal_small
        signal_unchanged = sig is not None and (
            (direction == "LONG" and sig.bullish) or (direction == "SHORT" and sig.bearish)
        )
        if not signal_unchanged:
            Log.info(f"POST-SL: signal on {tf_label} no longer agrees with {direction} -- "
                     f"dropping re-entry wait, falling through to a fresh entry search.")
            with self._lock:
                inst_state.awaiting_sl_reentry = False
                self._save_state()
            return False
        recovered = ((futures_px >= inst_state.sl_reentry_futures_px) if direction == "LONG"
                    else (futures_px <= inst_state.sl_reentry_futures_px))
        if not recovered:
            # Signal is UNCHANGED but price hasn't recovered yet -- this must
            # BLOCK run_cycle from falling through to a fresh entry search
            # this cycle (returning False here used to let it fall through,
            # which silently bypassed the "wait for recovery" requirement
            # entirely -- confirmed as a real bug via backtest, 2026-08-17:
            # it let a still-bullish Big signal re-enter LONG immediately at
            # a WORSE price than the original entry, no recovery at all).
            return True
        Log.info(f"POST-SL RE-ENTRY: signal unchanged and futures price recovered to entry level "
                 f"({futures_px:.2f} vs {inst_state.sl_reentry_futures_px:.2f}) -- re-entering "
                 f"{direction} on {tf_label}.")
        with self._lock:
            inst_state.awaiting_sl_reentry = False
            self._save_state()
        big_key_at_entry = (self._last_signal_big.candle_key
                            if (tf_label == "SMALL" and self._last_signal_big is not None) else "")
        # v2 SL-frozen: re-entry carries forward the ORIGINAL SL anchor price
        # and candle reference captured when the SL/signal-exit fired, NOT a
        # fresh reference off the recovery-moment candle (sig.high_prev1/
        # low_prev1 -- that was v1's behavior, see the sibling deployed
        # file). Strikes still re-resolve off the current futures_px inside
        # _open_position, unaffected.
        # 2026-08-20: apply the same breach-reflect correction here too --
        # a reference frozen when SL fired can be stale relative to the
        # (also frozen, unchanged) anchor price by the time recovery fires.
        reentry_sl_high, reentry_sl_low = _correct_breached_sl_reference(
            direction, inst_state.sl_reentry_futures_px, inst_state.sl_reentry_sl_high, inst_state.sl_reentry_sl_low)
        self._dispatch_open_position_bg(direction=direction, timeframe=tf_label, futures_px=futures_px,
                                        sl_high=reentry_sl_high,
                                        sl_low=reentry_sl_low,
                                        big_candle_key_at_entry=big_key_at_entry,
                                        sl_anchor_futures_px=inst_state.sl_reentry_futures_px)
        return True

    # -------------------------------------------------------------------
    # Position open / close -- 3-leg atomic group (core_call, core_put,
    # hedge). SHORT-before-LONG-before-hedge close priority per
    # Nifty_Sensex_Expiry_Batman_1's convention. A position stays non-empty
    # (pos.closing=True) from the moment a close is initiated until every
    # non-empty leg is confirmed exit_filled or parked in error_state --
    # never wiped from state before that, so a stuck leg still has a home
    # for Retry/Cancel/Manual to act on.
    # -------------------------------------------------------------------
    def _dispatch_open_position_bg(self, direction: str, timeframe: str, futures_px: float,
                                    sl_high: float, sl_low: float, big_candle_key_at_entry: str = "",
                                    sl_anchor_futures_px: Optional[float] = None):
        """Dispatches _open_position to _bg_executor instead of running it
        inline on run_cycle's own thread -- see _entry_pending's own comment
        (in __init__) for the specific production problem this fixes,
        matching Nifty_OI_WeeklyBuy_MonthlySell's _dispatch_eval_bg pattern.
        Reentrancy-guarded via _entry_pending; run_cycle's own flat-branch
        also checks _entry_pending before reaching either call site that
        leads here, so a duplicate dispatch should never actually happen --
        this check is a second, defensive layer, not the only one."""
        if self._entry_pending:
            Log.warning("_dispatch_open_position_bg called while an entry was already pending "
                        "-- skipping duplicate dispatch.")
            return
        self._entry_pending = True

        def _run():
            try:
                self._open_position(direction=direction, timeframe=timeframe, futures_px=futures_px,
                                    sl_high=sl_high, sl_low=sl_low,
                                    big_candle_key_at_entry=big_candle_key_at_entry,
                                    sl_anchor_futures_px=sl_anchor_futures_px)
            except Exception as exc:
                Log.exception(f"Backgrounded _open_position failed: {exc}")
            finally:
                self._entry_pending = False

        self._bg_executor.submit(_run)

    def _open_position(self, direction: str, timeframe: str, futures_px: float,
                        sl_high: float, sl_low: float, big_candle_key_at_entry: str = "",
                        sl_anchor_futures_px: Optional[float] = None):
        """v2 SL-frozen change: strikes ALWAYS resolve off the current
        futures_px (unaffected -- a rollover/re-entry still needs a real,
        current ATM/hedge strike). sl_anchor_futures_px, when given by the
        caller (rollover reopen / post-SL re-entry), is the ORIGINAL entry
        price from the very first entry of this logical trade -- used for
        entry_futures_px (hence the 1% SL threshold) INSTEAD of futures_px.
        A fresh entry passes sl_anchor_futures_px=None, so futures_px is
        used for both strikes AND the SL anchor, unchanged from v1."""
        atm_strike = resolve_atm_strike(futures_px)
        chain_strikes = fetch_chain_strikes(self.client, self.inst, self.state.expiry_compact)
        hedge_strike = resolve_hedge_strike(futures_px, direction, chain_strikes)
        legs = build_combo_legs(self.inst, self.state.expiry_compact, direction, atm_strike, hedge_strike)

        # WS subscription for every resolved leg -- without this, report_pnl_tick
        # (every config.pnl_tick_interval=0.8s while a position is open) and any
        # other LTP read against these symbols would ALWAYS miss the price_stream
        # cache and fall back to a REST quotes() call every single time, since
        # nothing else in this file ever subscribes option-leg symbols.
        self.price_stream.add_instruments([
            {"symbol": leg.symbol, "exchange": self.inst.options_exchange}
            for leg in legs.values() if leg.symbol
        ])

        self.execution_id += 1
        entry_time = datetime.now(IST).isoformat()
        for role in _LEG_ROLES:
            leg = legs[role]
            if not leg.symbol:
                Log.warning(f"OPEN {direction}: leg '{role}' has no resolved symbol (hedge strike "
                            f"unavailable?) -- skipping this leg only, other legs still placed.")
                continue
            # WS-first, REST fallback -- same pattern as everywhere else in this
            # file. In practice this specific read usually still falls to REST
            # (the leg was JUST subscribed above, so the WS feed hasn't had time
            # to push a tick yet) -- but a re-entry onto a symbol still
            # subscribed from moments ago can genuinely hit the WS cache, and
            # it's never wrong to check first.
            #
            # require_two_sided=True on the REST fallback: confirmed live
            # 2026-08-21, a fresh subscribe on this exact broker returned an
            # option's LTP matching the FUTURES/spot level (24236.6 for
            # NIFTY29SEP2624400PE, whose real premium was ~305) with no
            # two-sided market -- same failure mode Nifty_OI_WeeklyBuy_
            # MonthlySell's fetch_symbol_ltp docstring documents from
            # 2026-08-10 on this same broker. That entry corrupted this
            # leg's P&L by over 15 lakh rupees once closed. Rejecting a
            # quote with no real bid/ask catches it before it becomes a
            # fill price.
            ltp = self.price_stream.get_ltp(leg.symbol, self.inst.options_exchange,
                                            _current_ws_stale_threshold())
            if ltp is None:
                ltp = fetch_symbol_ltp_with_retry(self.client, leg.symbol, self.inst.options_exchange,
                                                  max_attempts=config.ltp_retry_max_attempts,
                                                  retry_delay=config.ltp_retry_delay)
            ltp = ltp or 0.0
            try:
                orderid, is_dry = place(self.client, self.env.strategy_tag, leg.symbol,
                                         self.inst.options_exchange, leg.action, leg.quantity,
                                         dry_run_ltp=ltp)
            except Exception as exc:
                Log.error(f"OPEN {direction}: place() failed for leg '{role}' ({leg.symbol}): {exc}")
                continue
            leg.entry_order_id = orderid
            if is_dry:
                leg.entry_filled = True
                leg.entry_px = ltp
            else:
                self._fill_executor.submit(self._watch_entry_fill, role, leg)

        sl_anchor_px = sl_anchor_futures_px if sl_anchor_futures_px is not None else futures_px
        pos = CombinedPosition(
            direction=direction, core_call=legs["core_call"], core_put=legs["core_put"], hedge=legs["hedge"],
            entry_time=entry_time, entry_futures_px=sl_anchor_px, controlling_timeframe=timeframe,
            entry_timeframe=timeframe, big_candle_key_at_entry=big_candle_key_at_entry,
            sl_reference_high=sl_high, sl_reference_low=sl_low,
            expiry_compact=self.state.expiry_compact, execution_id=self.execution_id, is_dry_run=config.dry_run,
        )
        with self._lock:
            self.state.instance.position = pos
            self.state.instance.trade_count_today += 1
            self._save_state()
        frozen_note = f" (SL frozen from original entry={sl_anchor_px:.2f})" if sl_anchor_futures_px is not None else ""
        Log.info(f"OPEN {direction} #{self.execution_id} on {timeframe}: futures_px={futures_px:.2f} "
                 f"ATM={atm_strike} hedge_strike={hedge_strike} expiry={self.state.expiry_compact} "
                 f"dry_run={config.dry_run}{frozen_note}")

    def _watch_entry_fill(self, role: str, leg: "OptionLeg"):
        self._pending_fills.add(role)
        try:
            fill = poll_fill(self.client, leg.entry_order_id, self.env.strategy_tag, leg.symbol,
                             self.inst.options_exchange, leg.action, leg.quantity)
            with self._lock:
                leg.entry_filled = True
                leg.entry_px = float(fill.get("average_price") or fill.get("price") or 0.0)
                self._save_state()
        except OrderNeedsAttention as exc:
            with self._lock:
                leg.error_state = "entry_failed"
                leg.error_kind = "resting"
                leg.error_order_id = exc.order_id
                leg.error_message = str(exc)
                leg.error_since = datetime.now(IST).isoformat()
                self._save_state()
            self._bg_executor.submit(push_leg_error, self.env, role, leg, leg.action)
        except Exception as exc:
            Log.error(f"_watch_entry_fill: leg '{role}' ({leg.symbol}) entry order "
                      f"{leg.entry_order_id} failed: {exc}")
            with self._lock:
                leg.error_state = "entry_failed"
                leg.error_kind = "terminal"
                leg.error_message = str(exc)
                leg.error_since = datetime.now(IST).isoformat()
                self._save_state()
            self._bg_executor.submit(push_leg_error, self.env, role, leg, leg.action)
        finally:
            self._pending_fills.discard(role)

    def _close_position(self, reason: str, wait_for_fills: bool = False) -> bool:
        """Initiates (or resumes trying to finalize) a close of the current
        combined position. Returns True once every non-empty leg is
        confirmed exit_filled (position is now fully flat and cleared from
        state), False if anything is still pending or stuck in error_state.
        wait_for_fills=True blocks synchronously on each live leg's fill
        confirmation instead of dispatching an async watcher -- used by
        rollover, which must never open a fresh position while the old
        one's legs might still be resting at the broker."""
        pos = self.state.instance.position
        if not pos.direction:
            return True
        if pos.closing:
            return self._try_finalize_close(reason)
        with self._lock:
            pos.closing = True
            self._save_state()

        close_order = [("core_call" if pos.direction == "SHORT" else "core_put"),
                       ("core_put" if pos.direction == "SHORT" else "core_call"),
                       "hedge"]
        exit_time = datetime.now(IST).isoformat()
        legs = {"core_call": pos.core_call, "core_put": pos.core_put, "hedge": pos.hedge}
        for role in close_order:
            leg = legs[role]
            if not leg.symbol or not leg.entry_filled or leg.exit_filled or leg.error_state:
                continue
            close_action = "SELL" if leg.action == "BUY" else "BUY"
            # WS-first -- unlike the entry-price read, this leg has been
            # subscribed since entry (some time ago), so the WS cache is
            # genuinely likely to be populated here.
            ltp = self.price_stream.get_ltp(leg.symbol, self.inst.options_exchange,
                                            _current_ws_stale_threshold())
            if ltp is None:
                ltp = fetch_symbol_ltp(self.client, leg.symbol, self.inst.options_exchange,
                                       require_two_sided=True)
            ltp = ltp or leg.entry_px
            try:
                orderid, is_dry = place(self.client, self.env.strategy_tag, leg.symbol,
                                         self.inst.options_exchange, close_action, leg.quantity,
                                         dry_run_ltp=ltp)
            except Exception as exc:
                Log.error(f"CLOSE {reason}: place() failed for leg '{role}' ({leg.symbol}): {exc}")
                with self._lock:
                    leg.error_state = "exit_failed"
                    leg.error_kind = "terminal"
                    leg.error_message = str(exc)
                    leg.error_since = datetime.now(IST).isoformat()
                    self._save_state()
                self._bg_executor.submit(push_leg_error, self.env, role, leg, close_action)
                continue
            leg.exit_order_id = orderid
            if is_dry:
                leg.exit_filled = True
                leg.exit_px = ltp
                with self._lock:
                    self._save_state()
                append_trade_log(self.env.strategy_tag, pos.execution_id, pos.direction, role,
                                 leg.symbol, leg.action, leg.quantity, pos.entry_time, leg.entry_px,
                                 exit_time, leg.exit_px, reason, True, *_trade_log_extra_args(pos))
            elif wait_for_fills:
                self._watch_exit_fill(role, leg, close_action, pos, reason, exit_time)
            else:
                self._fill_executor.submit(self._watch_exit_fill, role, leg, close_action, pos, reason, exit_time)

        Log.info(f"CLOSE {pos.direction} #{pos.execution_id} initiated reason={reason} dry_run={config.dry_run}")
        return self._try_finalize_close(reason)

    def _try_finalize_close(self, reason: str) -> bool:
        """Clears the position from state once every non-empty leg is
        exit_filled -- returns False (and leaves the position in place,
        still closing) if anything is still awaiting a fill or parked in
        error_state needing Retry/Cancel/Manual."""
        pos = self.state.instance.position
        if not pos.direction or not pos.closing:
            return not bool(pos.direction)
        for role in _LEG_ROLES:
            leg = getattr(pos, role)
            if not leg.symbol:
                continue
            if leg.error_state:
                return False
            if leg.entry_filled and not leg.exit_filled:
                return False
        # Unsubscribe every leg's WS feed now that the position is fully flat --
        # matches add_instruments() in _open_position; without this, closed
        # legs would sit in price_stream's subscription set forever (never
        # unbounded in practice since only ~3 symbols are tracked per trade,
        # but a stale/leaked subscription regardless).
        self.price_stream.remove_instruments([
            {"symbol": getattr(pos, role).symbol, "exchange": self.inst.options_exchange}
            for role in _LEG_ROLES if getattr(pos, role).symbol
        ])
        with self._lock:
            Log.info(f"CLOSE {pos.direction} #{pos.execution_id} fully finalized "
                     f"(reason={reason}) dry_run={config.dry_run}")
            self.state.instance.position = CombinedPosition()
            self._save_state()
        self._bg_executor.submit(self.report_pnl_tick)
        return True

    def _watch_exit_fill(self, role: str, leg: "OptionLeg", close_action: str,
                         pos: "CombinedPosition", reason: str, exit_time: str):
        self._pending_fills.add(role)
        try:
            fill = poll_fill(self.client, leg.exit_order_id, self.env.strategy_tag, leg.symbol,
                             self.inst.options_exchange, close_action, leg.quantity)
            exit_px = float(fill.get("average_price") or fill.get("price") or 0.0)
            with self._lock:
                leg.exit_filled = True
                leg.exit_px = exit_px
                pnl_points = (exit_px - leg.entry_px) if leg.action == "BUY" else (leg.entry_px - exit_px)
                self.state.today_realized_pnl += pnl_points * leg.quantity
                self._save_state()
            append_trade_log(self.env.strategy_tag, pos.execution_id, pos.direction, role,
                             leg.symbol, leg.action, leg.quantity, pos.entry_time, leg.entry_px,
                             exit_time, exit_px, reason, False, *_trade_log_extra_args(pos))
            self._try_finalize_close(reason)
        except OrderNeedsAttention as exc:
            with self._lock:
                leg.error_state = "exit_failed"
                leg.error_kind = "resting"
                leg.error_order_id = exc.order_id
                leg.error_message = str(exc)
                leg.error_since = datetime.now(IST).isoformat()
                self._save_state()
            self._bg_executor.submit(push_leg_error, self.env, role, leg, close_action)
        except Exception as exc:
            Log.error(f"_watch_exit_fill: leg '{role}' ({leg.symbol}) exit order "
                      f"{leg.exit_order_id} failed: {exc}")
            with self._lock:
                leg.error_state = "exit_failed"
                leg.error_kind = "terminal"
                leg.error_message = str(exc)
                leg.error_since = datetime.now(IST).isoformat()
                self._save_state()
            self._bg_executor.submit(push_leg_error, self.env, role, leg, close_action)
        finally:
            self._pending_fills.discard(role)

    # -------------------------------------------------------------------
    # PnL reporting -- own APScheduler job, fire-and-forget.
    # -------------------------------------------------------------------
    def report_pnl_tick(self):
        pos = self.state.instance.position
        open_positions = []
        if pos.direction:
            for role in _LEG_ROLES:
                leg = getattr(pos, role)
                if leg.symbol and leg.entry_filled and not leg.exit_filled:
                    # WS-first, REST only as fallback -- same pattern as the
                    # futures LTP read in run_cycle. Previously called
                    # fetch_symbol_ltp() (REST) unconditionally here, every
                    # config.pnl_tick_interval=0.8s per open leg -- exactly
                    # the "per-tick REST poll" the plan's API-efficiency
                    # section says never to do.
                    ltp = self.price_stream.get_ltp(leg.symbol, self.inst.options_exchange,
                                                    _current_ws_stale_threshold())
                    if ltp is None:
                        ltp = fetch_symbol_ltp(self.client, leg.symbol, self.inst.options_exchange,
                                               require_two_sided=True)
                    unreal = 0.0
                    if ltp is not None:
                        unreal = ((ltp - leg.entry_px) if leg.action == "BUY"
                                  else (leg.entry_px - ltp)) * leg.quantity
                    # Full field set matching the platform's expected shape
                    # (confirmed against MCX_CrudeOil_EMA34_RSI_ADX_Intraday's
                    # and Nifty_OI_WeeklyBuy_MonthlySell's report_pnl_tick) --
                    # this previously only sent symbol/role/pnl, leaving
                    # Qty/Entry/Direction/Entry Time/LTP-Exit blank
                    # ("undefined") on both the strategy card's "Today's
                    # Trades" panel and the Trades detail page (confirmed
                    # live via screenshots, 2026-08-20). "role" -> "leg_key"
                    # to match the field name the UI actually reads.
                    open_positions.append({
                        "leg_key": role, "symbol": leg.symbol,
                        "direction": "LONG" if leg.action == "BUY" else "SHORT",
                        "quantity": leg.quantity if leg.action == "BUY" else -leg.quantity,
                        "entry_price": leg.entry_px, "current_price": ltp, "pnl": unreal,
                        "entry_time": pos.entry_time, "execution_id": pos.execution_id,
                        # Live counterparts of the CSV trade log's dual-TF
                        # audit columns -- lets the Trades UI show these
                        # while a position is still OPEN, not only after it
                        # closes. controlling_timeframe/SL levels are
                        # intentionally NOT sent live: they can still change
                        # before close (handoff/ratchet), so only the CSV's
                        # frozen-at-close values are trustworthy for those.
                        "entry_timeframe": pos.entry_timeframe,
                        "handoff_occurred": "True" if pos.handoff_ts else "False",
                        "handoff_ts": pos.handoff_ts,
                    })
        try:
            report_pnl_to_platform(self.env, self.state.today_realized_pnl, open_positions)
        except Exception as exc:
            Log.warning(f"report_pnl_tick failed: {exc}")

    # -------------------------------------------------------------------
    # Force Exit -- checked in the background (never a blocking REST call
    # on run_cycle's own thread); resumes finalizing an in-progress
    # force-exit close across cycles until every leg is actually flat.
    # -------------------------------------------------------------------
    def _refresh_force_exit_check_bg(self):
        if self._force_exit_inflight:
            return
        self._force_exit_inflight = True

        def _run():
            try:
                self._force_exit_cache = check_force_exit(self.env)
            except Exception as exc:
                Log.warning(f"check_force_exit background refresh failed: {exc}")
            finally:
                self._force_exit_inflight = False

        self._bg_executor.submit(_run)

    def _handle_force_exit(self) -> bool:
        pos = self.state.instance.position
        if self._force_exit_in_progress:
            if pos.direction and self._try_finalize_close("force_exit"):
                self._force_exit_in_progress = False
                try:
                    ack_force_exit_complete(self.env)
                except Exception as exc:
                    Log.warning(f"ack_force_exit_complete failed: {exc}")
                return False
            return bool(pos.direction)

        self._refresh_force_exit_check_bg()
        if not self._force_exit_cache:
            return False
        if self._entry_pending:
            # An entry is currently being opened on a background thread --
            # pos.direction is still empty until that finishes, so we can't
            # yet tell whether there's about to be a position that needs
            # force-closing. Do NOT consume _force_exit_cache or ack here --
            # leaving it True means _refresh_force_exit_check_bg() re-arms
            # this same check on the very next cycle, once the entry has
            # actually resolved (found via code review, 2026-08-19: acking
            # "no position" here while an entry was moments from landing
            # would silently leave a real position open despite the
            # platform believing force-exit succeeded).
            Log.warning("FORCE EXIT requested while an entry is still being opened in the "
                        "background -- deferring until it resolves.")
            return True
        self._force_exit_cache = False  # consume -- avoid re-triggering before the ack lands
        if not pos.direction:
            try:
                ack_force_exit_complete(self.env)
            except Exception as exc:
                Log.warning(f"ack_force_exit_complete failed: {exc}")
            return False
        Log.info("FORCE EXIT requested via platform -- closing any open position.")
        self._force_exit_in_progress = True
        fully_closed = self._close_position(reason="force_exit")
        if fully_closed:
            self._force_exit_in_progress = False
            try:
                ack_force_exit_complete(self.env)
            except Exception as exc:
                Log.warning(f"ack_force_exit_complete failed: {exc}")
        return True

    # -------------------------------------------------------------------
    # Error re-push (periodic, so a still-open error badge doesn't vanish
    # from the platform UI across a restart)
    # -------------------------------------------------------------------
    def _repush_active_errors(self):
        now_ts = datetime.now(IST).timestamp()
        if now_ts - self._last_error_repush < config.error_repush_interval_sec:
            return
        self._last_error_repush = now_ts
        pos = self.state.instance.position
        if not pos.direction:
            return
        for role in _LEG_ROLES:
            leg = getattr(pos, role)
            if leg.error_state:
                self._bg_executor.submit(push_leg_error, self.env, role, leg, leg.action)

    def _push_leg_error_bg(self, role: str, leg: "OptionLeg", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _bg_executor. `leg` is
        snapshotted with a shallow copy on THIS (calling) thread before
        handing off -- executor.submit() evaluates its arguments
        immediately, only the function call itself is deferred, and `leg`
        is a live, mutable object this same cycle may reset moments later."""
        snapshot = copy.copy(leg)
        try:
            self._bg_executor.submit(push_leg_error, self.env, role, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"[{role}] Failed to dispatch push_leg_error: {exc}")

    # -------------------------------------------------------------------
    # Order error recovery (Retry / Cancel / Manually Completed)
    # -------------------------------------------------------------------
    def _process_leg_errors(self):
        """Checked every cycle: for any leg currently in error_state, pull
        whether the user has submitted a Retry/Cancel/Manual decision via
        the platform, and act on it. The pull itself is dispatched to
        _bg_executor (never a blocking REST call on this thread) and its
        result cached/consumed across cycles."""
        pos = self.state.instance.position
        if not pos.direction:
            return
        for role in _LEG_ROLES:
            leg = getattr(pos, role)
            if not leg.error_state:
                continue
            self._refresh_pending_action_bg(role)
            action = self._pop_pending_action(role)
            if action is not None:
                self._resolve_leg_error(role, action)

    def _refresh_pending_action_bg(self, role: str):
        if role in self._pending_action_inflight:
            return
        self._pending_action_inflight.add(role)

        def _run():
            try:
                result = check_pending_action(self.env, role)
                if result is not None:
                    self._pending_action_cache[role] = result
            except Exception as exc:
                Log.warning(f"check_pending_action background refresh failed for {role}: {exc}")
            finally:
                self._pending_action_inflight.discard(role)

        self._bg_executor.submit(_run)

    def _pop_pending_action(self, role: str) -> Optional[dict]:
        return self._pending_action_cache.pop(role, None)

    def _resolve_leg_error(self, role: str, action: dict):
        if role in self._pending_fills:
            # A resolution (or a resumed fill watcher) is already in flight
            # for this leg -- leave the new action un-acked, picked up again
            # once _pending_fills clears, rather than racing a second
            # concurrent watcher/place() against the same order.
            return
        pos = self.state.instance.position
        leg = getattr(pos, role, None)
        if leg is None or not leg.error_state:
            return
        was_exit = leg.error_state == "exit_failed"
        kind = leg.error_kind

        if action["action"] == "retry":
            self._pending_fills.add(role)
            ack_pending_action(self.env, role)
            self._fill_executor.submit(self._do_retry_resolution, role, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                # Ignore the failed close attempt -- no further broker-side
                # action. Leg stays open; a fresh close attempt fires
                # normally the next time _close_position runs for this
                # position (SL/rollover/force-exit/universal-exit).
                with self._lock:
                    leg.exit_order_id = ""
                    leg.error_state = ""
                    leg.error_kind = ""
                    leg.error_order_id = ""
                    leg.error_message = ""
                    self._save_state()
                self._push_leg_error_bg(role, leg, clear=True)
                ack_pending_action(self.env, role)
                return
            if kind == "terminal":
                # Nothing resting -- no broker call needed, this leg never
                # got a position; clear it back to empty.
                with self._lock:
                    setattr(pos, role, OptionLeg())
                    self._save_state()
                self._push_leg_error_bg(role, getattr(pos, role), clear=True)
                ack_pending_action(self.env, role)
                return
            # kind == "resting" entry -- one last honest re-price + bounded
            # wait, then an explicit cancel if it still didn't fill. Ack'd
            # here immediately (not deferred) so a later cycle's error-check
            # can't re-read this same action and dispatch a second
            # concurrent watcher against the same order_id.
            ack_pending_action(self.env, role)
            self._pending_fills.add(role)
            self._fill_executor.submit(self._watch_entry_cancel, role, leg.error_order_id,
                                       leg.symbol, leg.action, leg.quantity)
            return

        if action["action"] == "manual":
            fill_price = float(action["fill_price"])
            with self._lock:
                if was_exit:
                    leg.exit_filled = True
                    leg.exit_px = fill_price
                    pnl_points = (fill_price - leg.entry_px) if leg.action == "BUY" else (leg.entry_px - fill_price)
                    self.state.today_realized_pnl += pnl_points * leg.quantity
                    append_trade_log(self.env.strategy_tag, pos.execution_id, pos.direction, role,
                                     leg.symbol, leg.action, leg.quantity, pos.entry_time, leg.entry_px,
                                     datetime.now(IST).isoformat(), fill_price, "manual", pos.is_dry_run,
                                     *_trade_log_extra_args(pos))
                else:
                    leg.entry_filled = True
                    leg.entry_px = fill_price
                leg.error_state = ""
                leg.error_kind = ""
                leg.error_order_id = ""
                leg.error_message = ""
                self._save_state()
            self._push_leg_error_bg(role, leg, clear=True)
            ack_pending_action(self.env, role)
            if was_exit:
                self._try_finalize_close("manual")

    def _do_retry_resolution(self, role: str, was_exit: bool, kind: str):
        """The actual broker calls behind a Retry action (reprice via
        modifyorder, or a fresh place() for a terminal rejection) -- moved
        off run_cycle's thread by _resolve_leg_error, which already added
        role to _pending_fills and ack'd the action before submitting this.
        Hands off _pending_fills ownership to _watch_entry_fill/
        _watch_exit_fill once one of those is (re)dispatched; discards it
        itself on every other exit path (dry-run immediate fill, or a
        failure that re-enters error mode)."""
        try:
            pos = self.state.instance.position
            leg = getattr(pos, role)
            exchange = self.inst.options_exchange

            if was_exit:
                close_action = "SELL" if leg.action == "BUY" else "BUY"
                if kind == "resting":
                    bid, ask = fetch_symbol_bid_ask(self.ltp_client, leg.symbol, exchange)
                    fresh = ask if close_action == "BUY" else bid
                    if fresh is not None:
                        try:
                            self.client.modifyorder(
                                order_id=leg.error_order_id, strategy=self.env.strategy_tag,
                                symbol=leg.symbol, action=close_action, exchange=exchange,
                                price_type="LIMIT", product=config.product, quantity=str(leg.quantity),
                                price=str(fresh), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[{role}] Retry's reprice failed ({exc}) -- resuming watcher as-is.")
                    # exit_order_id already == error_order_id
                else:  # terminal -- nothing resting, a fresh close order is needed
                    leg.exit_order_id = ""
                with self._lock:
                    leg.error_state = ""
                    leg.error_kind = ""
                    leg.error_order_id = ""
                    leg.error_message = ""
                    self._save_state()
                self._push_leg_error_bg(role, leg, clear=True)
                if not leg.exit_order_id:
                    ltp = fetch_symbol_ltp(self.client, leg.symbol, exchange,
                                           require_two_sided=True) or leg.entry_px
                    try:
                        orderid, is_dry = place(self.client, self.env.strategy_tag, leg.symbol,
                                                 exchange, close_action, leg.quantity, dry_run_ltp=ltp)
                    except Exception as exc:
                        Log.exception(f"[{role}] Retry's fresh close place() failed: {exc}")
                        with self._lock:
                            leg.error_state = "exit_failed"
                            leg.error_kind = "terminal"
                            leg.error_message = str(exc)
                            leg.error_since = datetime.now(IST).isoformat()
                            self._save_state()
                        self._bg_executor.submit(push_leg_error, self.env, role, leg, close_action)
                        return
                    leg.exit_order_id = orderid
                    if is_dry:
                        with self._lock:
                            leg.exit_filled = True
                            leg.exit_px = ltp
                            self._save_state()
                        self._try_finalize_close("retry")
                        return
                self._fill_executor.submit(self._watch_exit_fill, role, leg, close_action, pos,
                                           "retry", datetime.now(IST).isoformat())
                return

            # entry side
            if kind == "resting":
                bid, ask = fetch_symbol_bid_ask(self.ltp_client, leg.symbol, exchange)
                fresh = ask if leg.action == "BUY" else bid
                if fresh is not None:
                    try:
                        self.client.modifyorder(
                            order_id=leg.error_order_id, strategy=self.env.strategy_tag,
                            symbol=leg.symbol, action=leg.action, exchange=exchange,
                            price_type="LIMIT", product=config.product, quantity=str(leg.quantity),
                            price=str(fresh), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{role}] Retry's reprice failed ({exc}) -- resuming watcher as-is.")
            else:  # terminal -- nothing resting, place a genuinely new order
                ltp = fetch_symbol_ltp(self.client, leg.symbol, exchange, require_two_sided=True) or 0.0
                try:
                    orderid, is_dry = place(self.client, self.env.strategy_tag, leg.symbol,
                                             exchange, leg.action, leg.quantity, dry_run_ltp=ltp)
                except Exception as exc:
                    Log.exception(f"[{role}] Retry's fresh entry place() failed: {exc}")
                    with self._lock:
                        leg.error_state = "entry_failed"
                        leg.error_kind = "terminal"
                        leg.error_message = str(exc)
                        leg.error_since = datetime.now(IST).isoformat()
                        self._save_state()
                    self._bg_executor.submit(push_leg_error, self.env, role, leg, leg.action)
                    return
                leg.entry_order_id = orderid
                if is_dry:
                    with self._lock:
                        leg.entry_filled = True
                        leg.entry_px = ltp
                        leg.error_state = ""
                        leg.error_kind = ""
                        leg.error_order_id = ""
                        self._save_state()
                    self._push_leg_error_bg(role, leg, clear=True)
                    return
            with self._lock:
                leg.error_state = ""
                leg.error_kind = ""
                leg.error_order_id = ""
                leg.error_message = ""
                self._save_state()
            self._push_leg_error_bg(role, leg, clear=True)
            self._fill_executor.submit(self._watch_entry_fill, role, leg)
        except Exception as exc:
            Log.exception(f"[{role}] Retry resolution failed unexpectedly: {exc}")
        finally:
            self._pending_fills.discard(role)

    def _watch_entry_cancel(self, role: str, order_id: str, symbol: str, action: str, quantity: int):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned: one final reprice + bounded wait, then an
        explicit cancelorder() if it still didn't fill."""
        try:
            result = _reprice_and_wait_once(self.client, order_id, self.env.strategy_tag,
                                            symbol, self.inst.options_exchange, action, quantity)
            pos = self.state.instance.position
            leg = getattr(pos, role, None)
            if leg is None or leg.entry_order_id != order_id:
                return  # superseded by a newer action/order in the meantime
            with self._lock:
                if result is not None:
                    leg.entry_filled = True
                    leg.entry_px = float(result.get("average_price") or result.get("price") or 0.0)
                else:
                    try:
                        self.client.cancelorder(order_id=order_id, strategy=self.env.strategy_tag)
                    except Exception as exc:
                        Log.warning(f"[{role}] Entry-Cancel: cancelorder() failed ({exc}) -- "
                                    f"clearing the leg anyway, order may already be dead at the broker.")
                    setattr(pos, role, OptionLeg())
                self._save_state()
            self._push_leg_error_bg(role, getattr(pos, role), clear=True)
        except Exception as exc:
            Log.exception(f"[{role}] Entry-Cancel flow failed unexpectedly: {exc}")
            pos = self.state.instance.position
            leg = getattr(pos, role, None)
            if leg is not None:
                with self._lock:
                    leg.error_state = "entry_failed"
                    leg.error_kind = "resting"
                    leg.error_message = str(exc)
                    leg.error_since = datetime.now(IST).isoformat()
                    self._save_state()
                self._bg_executor.submit(push_leg_error, self.env, role, leg, action)
        finally:
            self._pending_fills.discard(role)

    # -------------------------------------------------------------------
    # Main cycle
    # -------------------------------------------------------------------
    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._process_leg_errors()
            if self._handle_force_exit():
                return
            self._repush_active_errors()

            if not self.state.futures_symbol:
                self._handle_rollover()
                if not self.state.futures_symbol:
                    return

            if not _within_market_hours() and not config.test_mode:
                return

            self._handle_rollover()
            pos = self.state.instance.position

            if pos.closing:
                # Still resolving a close from a prior cycle (SL/rollover/
                # force-exit) -- keep trying to finalize it; nothing else
                # runs against this position until it's flat.
                self._try_finalize_close("pending_close")
                return

            # Signal refresh runs EVERY cycle, all day, with NO cutoff --
            # monitoring an already-open position (handoff/SL/signal-exit)
            # must never go blind for the last ~20 minutes of trading, and
            # Big's own last candle of the day closes at 15:15, just after
            # the entry cutoff below, so gating this too would silently
            # defer detecting it until the next morning. Corrected
            # 2026-08-18 (user correction) after this exact bug pushed a
            # Big-candle signal-loss exit from the same day it happened to
            # the next morning -- confirmed via a real trade in backtest.
            big_advanced, small_advanced = self._refresh_signals()

            # This cutoff ONLY blocks NEW entries (fresh search / post-SL
            # re-entry) below -- it must NEVER gate handoff/SL/signal-exit
            # monitoring of an already-open position (see above).
            entry_cutoff_active = config.test_mode or datetime.now(IST).time() < config.daily_candle_check_cutoff

            futures_px = self.price_stream.get_ltp(self.state.futures_symbol, self.inst.futures_exchange,
                                                    _current_ws_stale_threshold())
            if futures_px is None:
                futures_px = fetch_symbol_ltp(self.ltp_client, self.state.futures_symbol,
                                              self.inst.futures_exchange)
            if futures_px is None:
                Log.warning("run_cycle: no futures LTP available (WS stale, REST fallback also "
                            "failed) -- skipping this cycle's decisions.")
                return

            if pos.direction:
                just_handed_off = self._check_handoff()
                pos = self.state.instance.position  # handoff may have changed controlling_timeframe
                # Suppress the candle-break check on the SAME tick a handoff
                # just occurred -- the SL reference was just freshly set FROM
                # this exact candle, so it can't have already been "broken"
                # by itself (see _check_handoff's own docstring).
                candle_just_closed = not just_handed_off and (
                    (pos.controlling_timeframe == "BIG" and big_advanced)
                    or (pos.controlling_timeframe == "SMALL" and small_advanced)
                )
                if self._check_stop_loss(futures_px, candle_just_closed):
                    return
                if self._check_signal_exit(candle_just_closed):
                    return
                return

            if not entry_cutoff_active:
                return  # no re-entry recheck / fresh search past the daily cutoff

            if self._entry_pending:
                return  # a backgrounded _open_position from an earlier cycle hasn't finished yet

            if self._check_sl_reentry(futures_px):
                return

            if not config.test_mode and not (config.entry_start <= datetime.now(IST).time() <= config.entry_end):
                return
            found = self._search_entry()
            if found is None:
                return
            direction, tf_label, sig = found
            big_key_at_entry = (self._last_signal_big.candle_key
                                if (tf_label == "SMALL" and self._last_signal_big is not None) else "")
            # Fresh-entry-only breach correction -- see _correct_breached_sl_reference's
            # own docstring. Applies regardless of whether tf_label is BIG or SMALL.
            entry_sl_high, entry_sl_low = _correct_breached_sl_reference(
                direction, futures_px, sig.high_prev1, sig.low_prev1)
            self._dispatch_open_position_bg(direction=direction, timeframe=tf_label, futures_px=futures_px,
                                            sl_high=entry_sl_high, sl_low=entry_sl_low,
                                            big_candle_key_at_entry=big_key_at_entry)
        except Exception as exc:
            Log.exception(f"run_cycle failed: {exc}")
            self._bg_executor.submit(notify_telegram_error, self.env,
                                     f"[{config.strategy_name}] run_cycle exception: {exc}", Log.warning)


def print_banner():
    print("=" * 70)
    print(config.strategy_name)
    print("=" * 70)
    print(f"Version              : {config.version}")
    print(f"Instance             : {config.instance_id}")
    print(f"Instrument           : {INSTRUMENTS[0].name} ({INSTRUMENTS[0].options_exchange})")
    print(f"Timeframes           : Big={config.big_fetch_interval} ({config.big_timeframe_minutes}m) / "
          f"Small={config.small_fetch_interval} ({config.small_timeframe_minutes}m)")
    print(f"MACD / SuperTrend    : ({config.macd_fast},{config.macd_slow},{config.macd_signal}) / "
          f"({config.supertrend_period},{config.supertrend_multiplier})")
    print(f"Entry window         : {config.entry_start} - {config.entry_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Stop loss            : {config.sl_pct * 100:.1f}% or candle high/low break (on futures price)")
    print(f"Rollover             : {config.rollover_days_after_start} calendar days after contract start")
    print(f"Hedge OTM band       : {config.hedge_otm_pct_low * 100:.0f}%-{config.hedge_otm_pct_high * 100:.0f}%")
    print(f"Lots                 : {config.lot_multiplier}")
    print(f"Product              : {config.product}")
    if config.dry_run:
        print("*** DRY RUN MODE -- NO REAL ORDERS WILL BE PLACED (simulated fills only) ***")
    else:
        print("!!! LIVE MODE -- REAL ORDERS WILL BE PLACED !!!")
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

    price_stream = PriceStream(client)
    price_stream.start()

    # Mid-day restart: subscribe immediately to the futures symbol and any
    # already-open leg symbols instead of waiting for the next refresh.
    already_known = []
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        if state_store.state.futures_symbol:
            already_known.append({
                "symbol": state_store.state.futures_symbol,
                "exchange": INSTRUMENTS[0].futures_exchange,
            })
        pos = state_store.state.instance.position
        if pos.direction:
            for role in _LEG_ROLES:
                leg = getattr(pos, role)
                if leg.symbol:
                    already_known.append({"symbol": leg.symbol, "exchange": INSTRUMENTS[0].options_exchange})
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

    # Restart while a leg was in error mode, or between order-placement and
    # fill-confirmation: re-arm the fill watcher / re-push the error badge
    # so neither is silently lost across a process restart (see
    # AUTHORING_CHECKLIST.md's reconcile-pending-orders-at-startup item).
    pos = state_store.state.instance.position
    if pos.direction:
        for role in _LEG_ROLES:
            leg = getattr(pos, role)
            if not leg.symbol:
                continue
            if leg.error_state:
                push_leg_error(env, role, leg, action=leg.action)
                Log.error(f"[{role}] Resuming with an unresolved error from before restart "
                          f"({leg.error_state}/{leg.error_kind}) -- needs Retry/Cancel/Manually Completed.")
            elif leg.entry_order_id and not leg.entry_filled:
                Log.warning(f"[{role}] Resuming entry-fill watch for an order placed before a restart.")
                engine._fill_executor.submit(engine._watch_entry_fill, role, leg)
            elif leg.exit_order_id and not leg.exit_filled and leg.entry_filled:
                close_action = "SELL" if leg.action == "BUY" else "BUY"
                Log.warning(f"[{role}] Resuming exit-fill watch for an order placed before a restart.")
                engine._fill_executor.submit(engine._watch_exit_fill, role, leg, close_action, pos,
                                             "restart_resume", datetime.now(IST).isoformat())
        if pos.closing:
            Log.warning("Resuming a position that was mid-close before the restart -- "
                        "run_cycle will keep trying to finalize it.")

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
    except Exception:
        Log.exception("Scheduler stopped unexpectedly -- cleaning up before exit.")
        scheduler.shutdown(wait=False)
        price_stream.stop()
        engine._fill_executor.shutdown(wait=False)
        engine._bg_executor.shutdown(wait=False)
        raise


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    main()
