"""
===============================================================================
NIFTY Weekly Intraday Gap Seller
===============================================================================
Version     : 1.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11

Source      : historical Excel trade log ("Intraday Weekly Option Sell
              Strategy_....xlsx", analyzed offline, not part of this repo).
              Rules below were confirmed directly with the project owner via
              Q&A and are final -- this docstring documents WHAT was agreed,
              not a re-derivation.

*** THIS STRATEGY SELLS A NAKED (UNHEDGED) NIFTY OPTION -- UNDEFINED RISK ***
*** PRODUCT IS NRML, NOT MIS -- SEE THE WARNING BELOW BEFORE TOUCHING       ***
*** config.universal_exit_time OR config.product.                          ***

Description
-----------
Pure INTRADAY single-instrument (NIFTY only -- no SENSEX) option-selling
strategy. Exactly ONE naked leg is ever open at a time: either that day's PE
or that day's CE, never both, decided once per day by the gap-direction rule
below. This is NOT a per-leg-pair structure like this project's other NIFTY/
SENSEX scripts (EMA34_RSI, Pivot+Supertrend) -- there is a single Position
slot, not a dict of independent legs, because only one side is even
considered on a given day.

Signal / entry rule (locked, confirmed with the project owner)
------------------------------------------------------------------------
Evaluated once per day, at config.entry_window_start (09:30 IST), with a
narrow tolerance window (09:30-09:35, config.entry_window_end) purely to
absorb scheduler_interval jitter -- this is NOT a retry-all-day window; if
nothing is entered by 09:35 the day is a no-trade day.

  Gap direction: compare NIFTY spot at the PREVIOUS trading day's 15:15 IST
  close against NIFTY spot at TODAY's 09:30 IST.

    today_0930 > prev_day_1515  -> gap UP   -> SELL PE
    today_0930 <= prev_day_1515 -> gap DOWN (or exactly flat) -> SELL CE

  Verbatim from the project owner: "strict comparison with 3:15 price ... if
  9:30 price > 3:15PM then PE sell else CE sell". This is a plain if/else --
  an exact tie deliberately falls to the CE branch (the literal "else"), NOT
  a no-trade day. No minimum-gap threshold: any difference at all counts.

  The previous day's 15:15 price is fetched via client.history() on "1m"
  bars (config.prev_day_price_interval), taking the candle at-or-nearest-
  before 15:15 IST. "1m" is a documented, standard OpenAlgo interval
  (docs/api/market-data/intervals.md's sample response lists it), but
  interval support is broker-dependent -- same defensive fallback pattern
  used elsewhere in this codebase (e.g. Nifty_OI_WeeklyBuy_MonthlySell's
  own history() calls): if "1m" comes back empty/error, this script falls
  back once to config.prev_day_price_interval_fallback ("5m") for that same
  lookup. Today's 09:30 price is a live LTP read (WebSocket cache via
  PriceStream, REST client.quotes() fallback if stale/missing) taken at the
  moment the entry window opens, not a candle close.

Strike selection -- TWO TIERS, tried in order (locked, confirmed with the
project owner across four follow-up refinements, 2026-08-09 -- originally a
four-tier ladder, collapsed to three, then to two -- see below)
------------------------------------------------------------------------
Tier 0 (current week, resolve_current_and_next_week_expiry()'s "current"
slot -- the nearest expiry that ISN'T today, same same-day-expiry
roll-forward every sibling script in this project uses, since a
same-day-expiring contract has an extreme gamma/theta cliff in its final
hours) -- select_strike_both_hard(): BOTH filters below are HARD -- both
must hold simultaneously for a strike to qualify, not a soft preference.

  1. Premium (LTP): config.premium_min <= ltp <= config.premium_max
     (30 <= ltp <= 42).
  2. Strike distance from spot: config.distance_min_pct <= distance%
     <= config.distance_max_pct (1.0% - 1.5% OTM) -- for a PE, the strike is
     BELOW spot by that percentage; for a CE, ABOVE spot.

  Among multiple simultaneous matches: closest premium to the midpoint of
  [premium_min, premium_max] (36) (ASSUMPTION -- not specified by the
  project owner, flagged for their review). No match at all -> tier 1.

Tier 1 (next week's expiry -- select_strike_premium_hard()): premium in
[premium_min, premium_max] is now the ONLY hard filter; distance is merely a
tie-break (closest to the [distance_min_pct, distance_max_pct] band among
premium-qualifying survivors), not a requirement. This REPLACED an earlier
"next week, both filters hard again" tier and a separate "next week,
distance-hard/premium-closest" tier -- collapsed into this single tier per
the project owner's final instruction ("current week hard match ... if not
move to next week with premium match"), validated by backtesting this exact
rule against the real historical xlsx trade log this strategy is based on
(C:\\Devendra\\OpenAlgo\\data23_to_26, 2026-08-09): the real trades honored
premium 94.8% of the time vs. distance only 35.1% of the time (mean distance
1.88%, median 1.69%, up to 5.73%), i.e. the actual historical execution
treated premium as the true hard constraint and let distance float loosely
-- the opposite of the earlier distance-hard design. This tier recovered
~76% of the real trades' total points in that backtest (idealized SL fill)
vs. ~55% for the earlier distance-hard version. No match at all -> routed
into the data/connectivity-failure retry mechanism below, NOT a separate
degraded-fallback tier.

*** TIER 2 (the earlier distance-closest-ignoring-premium degraded
fallback) WAS REMOVED 2026-08-09 *** per the project owner's explicit
instruction ("remove tier 2 as its not needed") -- it fired in only 2 of
151 backtested trades and wasn't judged worth the extra code path. If tier
1 also finds nothing (no strike on the correct side has premium in
[premium_min, premium_max] in next week's chain either), that is now
explicitly treated the SAME WAY as a genuine data/connectivity failure
(project owner's explicit choice when asked): it goes through the SAME
5-attempt/60s retry budget described below, not an immediate skip and not
an immediate degraded-strike trade. Only after all 5 retries are exhausted
does `state.today_no_trade` actually latch for the day. This is a real,
if rare, behavior change from the "no trade is not an option, always
place something" guarantee that existed while tier 2 was still present --
in the (should-be-very-rare) case where tier 1 keeps finding nothing on
every retry, today CAN end up a genuine no-trade day now, after ~5 minutes
of retrying. Widen config.strike_count or config.premium_min/max if this is
ever observed live.

Data/connectivity failure retries (5 attempts, 1 minute apart -- added
2026-08-09, project owner's exact words: "in case [of] no data or failure,
you should try for 5 times with 1 min interval")
------------------------------------------------------------------------
Scope: GENUINE data/connectivity failures during the entry sequence, PLUS
(since the tier-2 removal above) tier 1 finding no premium-qualifying
strike in next week's chain at all -- never a legitimate tier-0 "no match,
falling through to tier 1" outcome, which is normal tier progression, not a
failure. Covers: the prev-day 15:15 price fetch failing (both interval
attempts exhausted) or the current spot LTP being unavailable (gap
direction can't be computed at all without these); expiry resolution
failing; an option chain fetch erroring or returning something unusable at
tier 0 or tier 1; and tier 1 itself completing successfully but matching no
strike with premium in range on either side.

Mechanism (see _register_entry_failure, non-blocking -- no time.sleep
anywhere, per AUTHORING_CHECKLIST.md's rule that nothing may block the
scheduler thread): each such failure increments the persisted
`state.entry_failure_attempt_count` and sets
`state.entry_failure_next_retry_at` to now + 60s, then returns from that
cycle's entry attempt having placed nothing. `_attempt_entry()` re-checks
this timestamp on every later run_cycle tick and simply does nothing until
it's due -- force-exit/error-resolution/pending-action checks elsewhere in
run_cycle are completely unaffected, since they run before _attempt_entry
is ever reached. When due, the WHOLE entry sequence (tiers 0-3) is retried
from the top, not resumed mid-tier -- deliberately, to avoid a second
entry-point duplicating the tier logic (today_gap_computed being already
persisted means a retry doesn't re-fetch the prev-day price if that part
already succeeded). After 5 total failed attempts,
`state.today_no_trade` is FINALLY latched with an error-level log stating
all 5 attempts failed, and no further entry is attempted for the rest of
today's window. Both counters reset to 0/"" at the start of each new
trading day (same place today_no_trade itself resets) and are persisted
via StateStore exactly like every other state field, so a same-day restart
resumes the count instead of silently resetting to 0 or crashing on the
new fields.

Entry-window interaction: entry_window_end (09:35) still gates the FIRST
entry attempt only. Once a retry sequence has actually started
(entry_failure_attempt_count > 0) and hasn't been given up on yet, run_cycle
lets it keep retrying PAST entry_window_end until it either succeeds or
exhausts all 5 attempts (worst case ~5 minutes past the first failure) --
chosen over widening entry_window_end itself (e.g. to 09:40) so the
window's normal, no-failure-case behavior is completely unchanged; time is
only borrowed when a real failure sequence is actively in progress. See
config.entry_window_end's own comment and run_cycle's "retry_in_progress"
check.

Risk management (locked, confirmed with the project owner)
------------------------------------------------------------------------
  - Stop-loss: config.sl_pct = 0.50 (50%) of entry premium -- buy back the
    short the moment its LTP >= entry_px * (1 + sl_pct). This number comes
    directly from analyzing 49 historical stop-outs in the source Excel log:
    mean 50.9%, median 50.7%, range 42-58% of entry premium. 50% sits inside
    that observed range and matches the project owner's own choice.
  - NO profit target. A position that isn't stopped out simply runs to the
    universal intraday square-off below.
  - Universal exit (square-off): config.universal_exit_time = 15:15 IST,
    same 15-minute buffer before NSE's 15:30 close already used by every
    other NIFTY/SENSEX intraday script in this project (EMA34_RSI,
    Pivot+Supertrend). The project owner chose this explicitly, fully aware
    that NRML (see below) gives it no broker-side backstop -- this exit
    order succeeding is the ONLY thing that closes the position.
  - Product: NRML. *** The project owner's explicit choice, despite this
    assistant's own recommendation of MIS. Unlike MIS, NRML has NO
    broker-side forced square-off backstop at all -- if THIS strategy's own
    universal_exit_time exit order fails for any reason (network blip,
    rejection, the process being down at 15:15), nothing else closes an
    undefined-risk naked short before market close, and the position
    carries overnight as a genuine, unbounded-risk position. See
    AUTHORING_CHECKLIST.md's "product (MIS vs NRML) and universal_exit_time
    must be chosen together" item -- this script follows its NRML guidance
    (give universal_exit_time real margin before market_close) but the
    underlying trade-off documented there (no backstop at all) is what the
    project owner is knowingly accepting here, not something this script
    can mitigate in code. ***
  - NO re-entry after a stop-loss (or any exit) the same day -- max ONE
    entry per day, full stop. Enforced via state.trade_count (persisted, so
    a mid-day restart can't re-trigger a duplicate entry for a day that
    already traded) exactly like every sibling script's own trade_count
    guard, just capped at 1 instead of N.
  - Quantity: config.lot_multiplier = 1 lot, lot size fetched dynamically
    from the option chain response's own "lotsize" field -- never
    hardcoded, matching every other deployed script's convention.

Explicitly OUT of scope vs. this project's other NIFTY/SENSEX scripts
------------------------------------------------------------------------
  - No dynamic futures-contract resolution (NIFTY has a real quotable
    NSE_INDEX spot, used directly).
  - No SENSEX / multi-instrument legs -- NIFTY only, one leg at a time
    (this day's PE OR CE), not a LEG_KEYS dict of independently-tracked
    legs. A single module-level LEG_KEY constant stands in for the leg
    identifier the shared reporting endpoints (push_leg_error,
    check_pending_action, ...) expect.
  - No opposite-signal / technical exit -- exit is ONLY stop-loss or the
    universal exit time, never a signal reversal.

Order placement, fill confirmation, error recovery, PnL/trade-log reporting
------------------------------------------------------------------------
Identical conventions and identical machinery to every other deployed script
in this project (docs/prd/python-strategies-order-error-recovery.md):
place() retries only a clean broker rejection, never an ambiguous exception
(duplicate-order risk); poll_fill()/_reprice_and_wait_once() cross the
spread with a fresh bid/ask rather than looping on a stale price and hand
off to a background fill watcher rather than blocking run_cycle; a single
Position (not a dict of legs) carries the same error_state/error_kind/
error_order_id/error_message/error_since fields and the same Retry/Cancel/
Manually-Completed resolution machinery as every sibling script's
LegPosition; reconcile_pending_orders() runs once at startup, before the
scheduler starts, and queries the broker directly via orderstatus() rather
than guessing (mirrors Nifty_OI_WeeklyBuy_MonthlySell's reconcile, the most
recently hardened version of this pattern in the project -- see that
script's own docstring for the crash-window rationale); push_leg_error/
check_pending_action/check_force_exit all go through a single, dedicated
background executor (_bg_executor) rather than running inline on
run_cycle's own thread, and target STRATEGY_REPORTING_PORT (default 8766)
via the same _post_json_local/_get_json_local pair used everywhere else in
this project (no env.host/Cloudflare fallback -- see that function's own
docstring for why it was dropped, 2026-08-07).

WebSocket price feed
------------------------------------------------------------------------
PriceStream below is the same hardened design already used by
Nifty_OI_WeeklyBuy_MonthlySell (itself descended from
MCX_CrudeOil_EMA9_RSI_Intraday/Batman/VWAP_NoHA/Combined): per-symbol
subscribe only, no unsubscribe before a stale-retry resubscribe, a
majority-based REST-confirmation gate before ever escalating to a full
reconnect, and independent per-symbol backoff. This strategy's own WS
footprint is at most 2 symbols at a time (NIFTY spot + the one open option
leg, if any).

Non-blocking execution -- a deliberate simplification vs. the multi-leg
sibling scripts (documented as a judgement call, not an oversight)
------------------------------------------------------------------------
EMA34_RSI/MCX/OI background their per-candle SIGNAL refresh (a REST history()
call that repeats all day, across up to 4 legs) via a dedicated executor so
one slow broker round-trip never delays every OTHER leg's evaluation on the
same scheduler tick. This strategy has no such steady-state signal loop --
the gap decision and strike-selection scan each fire AT MOST ONCE PER DAY,
inside the narrow 09:30-09:35 entry window, and there is only ever one
instrument/leg to delay. Making that one-shot sequence (one history() call
+ up to two optionchain() calls, each bounded by Environment.timeout=10s)
happen inline inside run_cycle -- rather than adding a dedicated executor
and cache layer purely to shave a few seconds off a once-a-day, few-times-
total event -- was a deliberate choice to keep the script simpler without
reintroducing the actual bug class the executor pattern exists to prevent
(nothing else in run_cycle is EVER blocked behind this: force-exit checks,
pending-action resolution, and PnL reporting all still go through
_bg_executor/their own scheduler job, completely unaffected by however long
the gap/strike scan takes on a given cycle). Order placement and fill
confirmation themselves are NOT part of this inline path -- place() is fast
and stays synchronous like every sibling script, and poll_fill() still runs
on a background fill watcher (_fill_executor), never inline.

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

# Several always-on threads (fill-watcher, trade-log writer, PriceStream's
# own watchdog/WS threads) at the default 8MB stack size add up against the
# STRATEGY_MEMORY_LIMIT_MB RLIMIT_AS cap every strategy subprocess runs
# under (blueprints/python_strategy.py's set_resource_limits(), confirmed in
# production as the real ceiling behind "RuntimeError: can't start new
# thread" -- see MCX_CrudeOil_EMA9_RSI_Intraday's identical comment). Must
# be called before any thread is created.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, notify_whatsapp_error, filter_known_fields
except ImportError:
    # Shared helper (strategies/scripts/_strategy_platform_client.py) not
    # present alongside this script -- degrade gracefully: the live "trade
    # just closed" SSE push and WhatsApp failure alerts simply won't fire,
    # nothing else is affected.
    def notify_trade_closed(env, log_warning=None):
        pass

    def notify_whatsapp_error(env, message, log_warning=None):
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

# Single-leg strategy -- this stands in for the "leg_key" identifier every
# shared reporting endpoint (push_leg_error, check_pending_action, ...)
# expects, matching the multi-leg sibling scripts' own calling convention
# without needing an actual dict of legs.
LEG_KEY = "NIFTY_GAPSELLER"


@dataclass
class Config:
    strategy_name: str = "NIFTY Weekly Intraday Gap Seller"
    version: str = "1.0.0"

    # --- entry timing --------------------------------------------------
    entry_window_start: time = time(9, 30)
    entry_window_end: time = time(9, 35)   # tolerance for scheduler_interval jitter, NOT a retry-all-day window
    # NOTE: this end time gates the FIRST entry attempt only. A data-fetch
    # failure retry sequence (see _register_entry_failure, up to 5 attempts
    # 60s apart) is deliberately allowed to keep running PAST this time once
    # started -- see run_cycle's "retry_in_progress" check. Left unwidened
    # (rather than e.g. 09:40) so a normal, no-failure day's behavior is
    # completely unchanged.

    # --- gap-direction reference price -----------------------------------
    prev_day_price_target_time: time = time(15, 15)
    prev_day_price_interval: str = "1m"     # documented interval (docs/api/market-data/intervals.md)
    prev_day_price_interval_fallback: str = "5m"   # broker-dependent "1m" support -- defensive fallback

    # --- strike selection (both HARD filters, see module docstring) ------
    premium_min: float = 30.0
    premium_max: float = 42.0
    distance_min_pct: float = 1.0
    distance_max_pct: float = 1.5
    strike_count: int = 20   # optionchain() strikes above/below ATM -- wide enough to cover 1-1.5% OTM on NIFTY

    # --- risk management ---------------------------------------------------
    sl_pct: float = 0.50   # 50% of entry premium -- see module docstring's provenance note (49 historical stop-outs)
    lot_multiplier: int = 1

    # NRML -- project owner's explicit choice; see module docstring's
    # prominent warning before changing this or universal_exit_time.
    product: str = "NRML"
    price_type: str = "MARKET"

    universal_exit_time: time = time(15, 15)   # 15 min buffer before market_close -- the ONLY backstop under NRML
    market_close: time = time(15, 30)

    scheduler_interval: int = 10
    pnl_tick_interval: float = 0.8

    ws_stale_seconds: float = 20.0
    ws_watchdog_interval: float = 15.0
    ws_stale_reconnect_after: int = 3

    fill_poll_interval: float = 2.0
    # 5s per wait-cycle (1 initial + 59 reprices) = 300s (5 min) total before
    # giving up and raising OrderNeedsAttention -- same ceiling used by
    # every sibling script.
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    # push_leg_error() only fires once, on the transition into error_state --
    # a single lost POST would otherwise leave the UI's error badge silently
    # blank for hours (confirmed in production elsewhere in this project,
    # 2026-07-28). Re-pushed at this interval for as long as the leg stays
    # in error_state.
    error_repush_interval_sec: float = 60.0

    # Minimum gap between WhatsApp alerts fired from run_cycle's own outer
    # except-clause (genuinely unexpected crashes not already funneled
    # through _enter_error_mode's error-state machine). Without this, a
    # persistently-recurring bug would fire one WhatsApp message per
    # scheduler tick (every scheduler_interval seconds) and flood the phone.
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
        """Use inside an `except` block instead of manually building a
        traceback string -- captures the current exception's traceback via
        the standard logging exc_info mechanism instead of jamming it into
        the message text."""
        Log.logger.exception(message)


###############################################################################
# MODELS
###############################################################################
@dataclass
class Position:
    symbol: str = ""
    quantity: int = 0
    option_type: str = ""          # "PE" / "CE" -- informational (order side is always SELL to enter / BUY to exit)
    entry_time: str = ""
    entry_px: float = 0.0          # LTP-based estimate, captured purely for the trade log's PnL columns
    entry_order_id: str = ""
    entry_filled: bool = False
    exit_order_id: str = ""
    exit_filled: bool = False
    # Set the first time an exit is attempted -- carries the exit reason
    # across the async gap to the background watcher's finalize step (and
    # across a restart) so a later cycle finalizing an already-filled exit
    # uses the SAME reason the original trigger decided on, not one
    # re-derived from scratch. Mirrors Nifty_OI_WeeklyBuy_MonthlySell's
    # pending_exit_reason field.
    exit_reason: str = ""
    execution_id: int = 0   # which process run OPENED this position -- see LegPosition.execution_id elsewhere

    # Order error recovery (docs/prd/python-strategies-order-error-recovery.md)
    error_state: str = ""           # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""            # "" | "terminal" | "resting"
    error_order_id: str = ""
    error_message: str = ""
    error_since: str = ""
    manual_exit_px: Optional[float] = None
    # The broker-confirmed exit fill price, set by _watch_exit_fill once
    # poll_fill()'s background watcher resolves (average_price/price from
    # orderstatus()) -- kept separate from manual_exit_px (an explicit
    # human override via Retry/Cancel/Manually Completed) so the two carry
    # distinct meanings; _finalize_exit prefers manual_exit_px first (if a
    # human explicitly stated it), then this, then a live LTP re-fetch only
    # as a last resort if neither is available.
    exit_fill_px: Optional[float] = None


@dataclass
class StrategyState:
    current_day: str = ""
    position: Position = field(default_factory=Position)
    trade_count: int = 0            # 0 or 1 -- max ONE entry per day, whole stop
    today_realized_pnl: float = 0.0
    last_updated: str = ""
    last_execution_id: int = 0

    # Gap decision, computed once at the entry window's first cycle and
    # locked for the rest of the day (state.today_gap_computed persists it
    # across a restart mid-window).
    today_gap_computed: bool = False
    today_prev_close: float = 0.0
    today_entry_spot: float = 0.0
    today_gap_pct: float = 0.0
    today_option_type: str = ""     # "PE" / "CE", set once today_gap_computed is True

    # Data/connectivity failure retry budget for the entry sequence (prev-day
    # 15:15 price fetch, expiry resolution, option chain fetch at any tier) --
    # NOT for a legitimate "no strike matched" tier outcome, which is never a
    # failure. Up to 5 attempts, 60s apart, non-blocking (see
    # _register_entry_failure). Persisted so a same-day restart resumes the
    # count instead of silently resetting to 0 or crashing on the field.
    entry_failure_attempt_count: int = 0
    entry_failure_next_retry_at: str = ""   # ISO timestamp; "" = no retry pending

    # "No trade" is NOT a valid outcome of the strategy's own rules past
    # tier 0 (see module docstring's "Strike selection" section) -- this is
    # ONLY latched when next week's option chain itself returns zero
    # strikes on the required side at all, a genuine broker/data error, not
    # a routine "no signal met" day. Never set on a transient fetch
    # exception (that retries on a later cycle instead).
    today_no_trade: bool = False
    today_no_trade_reason: str = ""


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
            or "nifty_weekly_intraday_gapseller"
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
            # PriceStream's own watchdog owns reconnect entirely -- letting
            # the SDK's own auto_reconnect thread run too races it (both
            # would call _do_connect() independently), same rationale as
            # every sibling script.
            auto_reconnect=False,
        )
        Log.info("Connected to OpenAlgo")
        return self.client

    def connect_ltp_client(self):
        """A second, independent client used ONLY for the WS-stale LTP
        fallback (quotes()) -- deliberately not sharing self.client's
        mutable .timeout attribute across threads."""
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
# LIVE PRICE STREAM (WebSocket) -- copied from the most recently hardened
# version of this pattern in the project (Nifty_OI_WeeklyBuy_MonthlySell,
# itself descended from MCX_CrudeOil_EMA9_RSI_Intraday/Batman/VWAP_NoHA/
# Combined): per-symbol subscribe only, no unsubscribe before a stale-retry
# resubscribe, majority-based REST-confirmed escalation before a full
# reconnect, independent per-symbol backoff. This strategy's footprint is at
# most 2 symbols (NIFTY spot + the one open option leg, if any).
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
        connects and then subscribes to whatever's already known in one
        batched call; calling add_instruments() from the main thread right
        after start() instead could race that thread's own initial
        subscribe on a client that hasn't finished connecting yet."""
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
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.last_updated = data.get("last_updated", "")
        self.state.last_execution_id = data.get("last_execution_id", 0)
        self.state.today_gap_computed = data.get("today_gap_computed", False)
        self.state.today_prev_close = data.get("today_prev_close", 0.0)
        self.state.today_entry_spot = data.get("today_entry_spot", 0.0)
        self.state.today_gap_pct = data.get("today_gap_pct", 0.0)
        self.state.today_option_type = data.get("today_option_type", "")
        self.state.today_no_trade = data.get("today_no_trade", False)
        self.state.today_no_trade_reason = data.get("today_no_trade_reason", "")
        # .get(key, default) per field (same pattern as every other top-level
        # field above) gives the same forward/backward-compatibility
        # guarantee filter_known_fields gives Position below -- a state.json
        # written before this field existed just defaults it, and never
        # crashes on a stale/missing field.
        self.state.entry_failure_attempt_count = data.get("entry_failure_attempt_count", 0)
        self.state.entry_failure_next_retry_at = data.get("entry_failure_next_retry_at", "")
        pos_raw = data.get("position", {})
        self.state.position = Position(**{**asdict(Position()), **filter_known_fields(Position, pos_raw)})
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "trade_count": self.state.trade_count,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_updated": self.state.last_updated,
            "last_execution_id": self.state.last_execution_id,
            "today_gap_computed": self.state.today_gap_computed,
            "today_prev_close": self.state.today_prev_close,
            "today_entry_spot": self.state.today_entry_spot,
            "today_gap_pct": self.state.today_gap_pct,
            "today_option_type": self.state.today_option_type,
            "today_no_trade": self.state.today_no_trade,
            "today_no_trade_reason": self.state.today_no_trade_reason,
            "entry_failure_attempt_count": self.state.entry_failure_attempt_count,
            "entry_failure_next_retry_at": self.state.entry_failure_next_retry_at,
            "position": asdict(self.state.position),
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=4)


###############################################################################
# HELPERS
###############################################################################
def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


def resolve_previous_trading_day(today) -> "datetime.date":
    """Previous calendar day, skipping weekends. Does NOT consult the
    exchange holiday calendar (database/market_calendar_db.py) -- a known
    simplification, same one Nifty_OI_WeeklyBuy_MonthlySell accepts: on the
    trading day right after an NSE holiday this resolves to the holiday
    date itself, and the history() call for that date simply returns no
    data (market was shut) -- already handled as "unavailable, retry next
    cycle" by fetch_price_at_or_before, so this fails safe rather than
    fabricating a wrong reference. Worth wiring to the real market calendar
    in a follow-up if holiday-adjacent misclassification is observed live."""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d


def fetch_price_at_or_before(client, symbol: str, exchange: str, target_dt: datetime,
                              primary_interval: str, fallback_interval: str) -> Optional[float]:
    """Close of the last candle at-or-before `target_dt`, trying
    `primary_interval` first and falling back once to `fallback_interval` if
    the primary interval's response is empty/errored (broker-dependent
    interval support, same defensive pattern used throughout this project).
    Returns None if both attempts fail -- caller must retry on a later
    cycle, never fabricate a price."""
    for interval in (primary_interval, fallback_interval):
        try:
            bars = client.history(
                symbol=symbol, exchange=exchange, interval=interval,
                start_date=target_dt.date().isoformat(), end_date=target_dt.date().isoformat(),
            )
        except Exception as exc:
            Log.warning(f"history() failed for {symbol}.{exchange} interval={interval}: {exc}")
            continue
        if _is_error_response(bars):
            Log.warning(f"history() error response ({interval}) for {symbol}.{exchange}: {bars}")
            continue
        if bars is None or bars.empty:
            Log.warning(f"history() returned no {interval} data for {symbol}.{exchange} on {target_dt.date()}")
            continue
        sub = bars[bars.index <= target_dt]
        if sub.empty:
            Log.warning(f"No {interval} candle at/before {target_dt} for {symbol}.{exchange}")
            continue
        price = float(sub.iloc[-1]["close"])
        if interval != primary_interval:
            Log.warning(f"[{symbol}] Fell back to interval={interval} for the reference price "
                        f"lookup (primary interval={primary_interval} unavailable/empty).")
        return price
    return None


def fetch_symbol_ltp(client, symbol: str, exchange: str, require_two_sided: bool = False) -> Optional[float]:
    """`require_two_sided=True` additionally requires bid>0 AND ask>0 before
    trusting the quote -- defends against a quote that looks like it belongs
    to a DIFFERENT instrument than requested (confirmed in production
    2026-08-10: a NIFTY option's LTP came back matching NIFTY spot, with
    bid=0/ask=0, falsely triggering GapSeller's own stop-loss). Pass True
    only for TRADABLE-instrument reads (option/equity/future SL or exit
    checks) -- an INDEX symbol legitimately has no bid/ask (no order book),
    so leave this False (default) for underlying-spot lookups. See
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


def fetch_symbol_bid_ask(client, symbol: str, exchange: str) -> tuple[Optional[float], Optional[float]]:
    """Used only by the reprice loop -- crossing the spread with a fresh
    bid/ask is what actually gets a resting order filled, unlike re-quoting
    the same stale last-traded price."""
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


def resolve_current_and_next_week_expiry(client) -> tuple[tuple[str, str], tuple[str, str]]:
    """Returns ((current_week_compact, current_week_raw), (next_week_compact,
    next_week_raw)) -- the nearest upcoming NIFTY weekly expiry (rolling
    forward one slot if today itself is an expiry day, same gamma/theta-
    cliff avoidance every sibling script in this project uses) and the
    weekly expiry immediately after it. This is what lets strike selection
    try "current week" then fall back to "next week" per the module
    docstring's Resolution order -- the same-day roll composes with that
    fallback automatically, since "current week" here always already means
    "the nearest expiry that isn't today"."""
    resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve NIFTY options expiry: {resp}")
    today = datetime.now(IST).date()
    parsed = [(datetime.strptime(raw, "%d-%b-%y").date(), raw) for raw in resp["data"]]
    parsed = [p for p in parsed if p[0] >= today]
    if not parsed:
        raise RuntimeError("No upcoming NIFTY options expiry returned by the broker.")

    idx = 0
    if parsed[0][0] == today:
        idx = 1
    if idx >= len(parsed):
        raise RuntimeError(
            "Today is the nearest NIFTY expiry and the broker returned no later expiry date "
            "to roll to -- refusing to silently trade today's expiring contract."
        )
    current_date, current_raw = parsed[idx]
    next_idx = idx + 1
    if next_idx >= len(parsed):
        raise RuntimeError(
            f"No weekly expiry available beyond the current week's own ({current_raw}) to use "
            f"as the next-week fallback."
        )
    next_date, next_raw = parsed[next_idx]
    return (_compact_expiry(current_raw), current_raw), (_compact_expiry(next_raw), next_raw)


def fetch_option_chain(client, expiry_compact: str, strike_count: int) -> dict:
    resp = client.optionchain(
        underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
        expiry_date=expiry_compact, strike_count=strike_count,
    )
    if resp.get("status") != "success" or not resp.get("chain"):
        raise RuntimeError(f"optionchain() failed for expiry {expiry_compact}: {resp}")
    return resp


def _chain_candidates(client, expiry_compact: str, option_type: str, spot: float) -> list:
    """Every strike in `expiry_compact`'s chain on the `option_type` side,
    with its own distance_pct already computed -- no filtering applied here.
    Shared by both select_strike_current_week (tier 0) and
    select_strike_next_week (tiers 1/2, see their own docstrings)."""
    resp = fetch_option_chain(client, expiry_compact, config.strike_count)
    key = option_type.lower()
    candidates = []
    for row in resp.get("chain", []):
        leg = row.get(key)
        if not leg:
            continue
        strike = leg.get("strike", row.get("strike"))
        if strike is None:
            continue
        strike = float(strike)
        ltp = leg.get("ltp")
        lotsize = leg.get("lotsize")
        symbol = leg.get("symbol")
        if ltp is None or lotsize is None or not symbol:
            continue
        ltp = float(ltp)
        if option_type == "PE":
            distance_pct = (spot - strike) / spot * 100.0
        else:
            distance_pct = (strike - spot) / spot * 100.0
        candidates.append({
            "strike": strike, "symbol": symbol, "ltp": ltp,
            "lotsize": int(lotsize), "distance_pct": distance_pct,
        })
    return candidates


def select_strike_both_hard(client, expiry_compact: str, option_type: str, spot: float) -> Optional[dict]:
    """BOTH filters hard simultaneous AND (premium in [premium_min,
    premium_max] AND OTM distance in [distance_min_pct, distance_max_pct]).
    Generic over `expiry_compact` -- used for tier 0 (current week) AND tier
    1 (next week, same logic re-applied per the project owner's "apply both
    rules on tier 1 as well" instruction) so the filter logic itself is
    never duplicated between the two tiers, only the expiry passed in
    differs. Returns None (not an error) if no strike satisfies both -- the
    caller decides what tier to fall through to next."""
    candidates = _chain_candidates(client, expiry_compact, option_type, spot)
    matched = [
        c for c in candidates
        if config.premium_min <= c["ltp"] <= config.premium_max
        and config.distance_min_pct <= c["distance_pct"] <= config.distance_max_pct
    ]
    if not matched:
        return None
    # Among multiple simultaneous matches: closest premium to the midpoint
    # of [premium_min, premium_max] (assumption, see module docstring).
    midpoint = (config.premium_min + config.premium_max) / 2.0
    matched.sort(key=lambda c: abs(c["ltp"] - midpoint))
    return matched[0]


def select_strike_premium_hard(client, expiry_compact: str, option_type: str, spot: float) -> Optional[dict]:
    """Tier 1 (next week, FINAL tier -- the earlier tier-2 degraded fallback
    was removed 2026-08-09, see module docstring): premium in [premium_min,
    premium_max] is the HARD filter -- distance is NO LONGER required.
    Among premium-qualifying survivors, tie-break by whichever strike's
    distance_pct sits closest to the [distance_min_pct, distance_max_pct]
    band.

    Validated against the real historical xlsx trade log this strategy is
    based on (2026-08-09 backtest, C:\\Devendra\\OpenAlgo\\data23_to_26):
    premium was honored in 94.8% of the actual historical trades vs.
    distance in only 35.1% (mean distance 1.88%, median 1.69%, up to
    5.73%) -- the real execution treated premium as the true hard
    constraint and let distance float loosely, which is exactly what this
    tier now replicates. Backtesting this exact rule against the same xlsx
    window recovered ~76% of the real trades' total points (vs. ~55% for
    the earlier distance-hard tier design this replaced).

    Returns None (not an error) if no strike satisfies the premium filter
    at all -- the caller routes this through the same 5-attempt/60s
    data-failure retry budget as a genuine chain-fetch error (project
    owner's explicit instruction), not an immediate skip."""
    candidates = _chain_candidates(client, expiry_compact, option_type, spot)
    matched = [c for c in candidates if config.premium_min <= c["ltp"] <= config.premium_max]
    if not matched:
        return None

    def _band_gap(c):
        return max(0.0, config.distance_min_pct - c["distance_pct"], c["distance_pct"] - config.distance_max_pct)

    matched.sort(key=_band_gap)
    return matched[0]


class OrderNeedsAttention(Exception):
    """poll_fill() exhausted its automatic reprice attempts but the order is
    still resting, UNFILLED, at the broker -- not cancelled. See
    docs/prd/python-strategies-order-error-recovery.md."""
    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


def _reprice_and_wait_once(client, order_id: str, strategy: str, symbol: str, exchange: str,
                            action: str, quantity: int) -> Optional[dict]:
    """One reprice-to-a-fresh-crossing-price (modifyorder(), keeping the
    same order id/queue position) + one fill_poll_timeout-bounded wait.
    Reprices to the current ASK for a BUY and the current BID for a SELL.
    Returns the fill data if it completed, None if still unfilled (order
    left resting either way -- this never cancels)."""
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
    return None  # still resting, unfilled


def poll_fill(client, orderid: str, strategy: str, symbol: str, exchange: str,
              action: str, quantity: int) -> dict:
    """Polls order status until a terminal state or config.fill_poll_timeout.
    On timeout, actively RE-PRICES the same order up to
    config.reprice_max_attempts times, for a combined ceiling of ~5 minutes.
    If it's STILL resting after all of those, raises OrderNeedsAttention
    WITHOUT cancelling it. A genuine broker rejection/cancellation still
    raises RuntimeError immediately."""
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
    placed, safe to retry) up to config.place_order_max_attempts times.
    Deliberately does NOT retry a raised exception -- that outcome is
    ambiguous (the order may have already reached the broker), so retrying
    risks a genuine duplicate order. No idempotency key exists anywhere in
    this stack, so surfacing the exception immediately -- exactly once, no
    retry -- is the only safe choice."""
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
            display_quantity = -quantity   # always a short seller here
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
# PLATFORM REPORTING -- targets STRATEGY_REPORTING_PORT (default 8766), the
# dedicated strategy_reporting subprocess, NOT FLASK_PORT/openalgo.sock (see
# docs/CUSTOMIZATIONS.md's strategy_reporting/ section). No env.host/
# Cloudflare fallback -- dropped project-wide 2026-08-07 (see
# Nifty_Sensex_EMA34_RSI_Intraday's identical function docstring for why:
# env.host is proxied through Cloudflare, which silently 403s this
# urllib-originated traffic).
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


def push_leg_error(env: "Environment", leg_key: str, pos: "Position",
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

        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds -- doing that INSIDE run_cycle would stall force-exit/error-
        # resolution checks for the same duration. Order confirmation runs on
        # a background task instead (see _watch_entry_fill/_watch_exit_fill).
        # Only one leg is ever open at a time, so a single worker is enough.
        self._fill_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fill-watch")
        # Single dedicated pool for every other background REST call
        # (check_force_exit, push_leg_error, check_pending_action) --
        # mirrors Nifty_OI_WeeklyBuy_MonthlySell's consolidated _bg_executor
        # (2026-08-04: that script dropped a separate _pnl_executor that
        # duplicated this isolation without anything ever using it -- report_pnl_tick's
        # own scheduler job already isolates it, see that method below).
        # Kept separate from _fill_executor so a fill-watcher stuck for
        # minutes in a reprice loop can never delay a Force Exit check.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")

        # _state_lock serializes every store.save() call -- StateStore.save()
        # writes the whole state to one shared JSON file, so two concurrent
        # writers (main thread vs. a background fill-watch task) could
        # otherwise corrupt it.
        self._state_lock = threading.Lock()
        self._pending_fill: bool = False
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._pending_action_cache: Optional[dict] = None
        self._pending_action_inflight: bool = False
        self._last_error_push: Optional[datetime] = None
        # Throttles the outer run_cycle except-clause's WhatsApp alert (see
        # its own comment) -- separate from _last_error_push above, which
        # only governs the UI error-badge re-push. In-memory/per-instance,
        # not persisted to state.json: a restart is itself worth a fresh
        # alert if the same crash recurs immediately.
        self._last_cycle_failure_notify: Optional[datetime] = None

    def _save_state(self):
        with self._state_lock:
            self.store.save()

    # ---- market/window helpers -----------------------------------------------------
    def _within_market_hours(self) -> bool:
        return _within_market_hours()

    def _within_entry_window(self) -> bool:
        if config.test_mode:
            return True
        now = datetime.now(IST).time()
        return config.entry_window_start <= now <= config.entry_window_end

    def _past_universal_exit(self) -> bool:
        if config.test_mode:
            return False
        return datetime.now(IST).time() >= config.universal_exit_time

    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.store.state.current_day != today_key:
            Log.info(f"New day detected ({today_key}); resetting daily state.")
            self.store.state.current_day = today_key
            self.store.state.trade_count = 0
            self.store.state.today_realized_pnl = 0.0
            self.store.state.today_gap_computed = False
            self.store.state.today_prev_close = 0.0
            self.store.state.today_entry_spot = 0.0
            self.store.state.today_gap_pct = 0.0
            self.store.state.today_option_type = ""
            self.store.state.today_no_trade = False
            self.store.state.today_no_trade_reason = ""
            self.store.state.entry_failure_attempt_count = 0
            self.store.state.entry_failure_next_retry_at = ""
            self._save_state()

    def get_spot_ltp(self) -> Optional[float]:
        ltp = self.price_stream.get_ltp(UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        return fetch_symbol_ltp(self.ltp_client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE)

    # ---- gap decision + strike selection (see module docstring's "Non-blocking
    # execution" note for why this runs inline rather than via a background
    # executor -- at most once per day, bounded by Environment.timeout) --------
    def _compute_gap_decision(self) -> bool:
        """Returns True once state.today_gap_computed is set (either just now,
        or already set from an earlier cycle/restart today). False means
        "not ready yet, retry next cycle" -- never guesses."""
        state = self.store.state
        if state.today_gap_computed:
            return True

        prev_day = resolve_previous_trading_day(datetime.now(IST).date())
        # pytz timezones must be applied via .localize(), never a naive
        # tzinfo= kwarg to datetime.combine() -- that would silently stamp
        # pytz's raw LMT offset instead of the correct IST +5:30.
        target_dt = IST.localize(datetime.combine(prev_day, config.prev_day_price_target_time))
        prev_close = fetch_price_at_or_before(
            self.client, UNDERLYING_SYMBOL, UNDERLYING_SPOT_EXCHANGE, target_dt,
            config.prev_day_price_interval, config.prev_day_price_interval_fallback,
        )
        if prev_close is None:
            Log.warning("[GapDecision] previous trading day's 15:15 price unavailable -- retrying next cycle.")
            return False

        spot_now = self.get_spot_ltp()
        if spot_now is None:
            Log.warning("[GapDecision] current NIFTY spot LTP unavailable -- retrying next cycle.")
            return False

        # Verbatim rule (see module docstring): today_0930 > prev_1515 -> PE,
        # else (including an exact tie) -> CE.
        option_type = "PE" if spot_now > prev_close else "CE"
        gap_pct = (spot_now - prev_close) / prev_close * 100.0

        state.today_gap_computed = True
        state.today_prev_close = prev_close
        state.today_entry_spot = spot_now
        state.today_gap_pct = gap_pct
        state.today_option_type = option_type
        self._save_state()
        Log.info(f"[GapDecision] prev_day({prev_day}) 15:15={prev_close:.2f} today_0930={spot_now:.2f} "
                 f"gap={gap_pct:+.3f}% -> SELL {option_type}")
        return True

    def _register_entry_failure(self, reason: str):
        """Genuine data/connectivity failure during the entry sequence (prev-
        day 15:15 price fetch, expiry resolution, or an option-chain fetch
        erroring/returning something unusable at ANY tier) -- NOT a
        legitimate "no strike matched" tier outcome, which is never routed
        here. Project owner's exact words: "in case [of] no data or
        failure, you should try for 5 times with 1 min interval." Never
        blocks (no time.sleep) -- just records the attempt and a future
        retry timestamp; the next due run_cycle tick re-tries the WHOLE
        entry sequence from the top (see _attempt_entry's retry-gate at the
        top -- deliberately not resuming mid-tier, to avoid duplicating the
        tier 0-3 logic for two entry points). After 5 failed attempts, gives
        up for the day: latches today_no_trade so this doesn't retry forever."""
        state = self.store.state
        state.entry_failure_attempt_count += 1
        if state.entry_failure_attempt_count >= 5:
            state.today_no_trade = True
            state.today_no_trade_reason = (
                f"Entry aborted after {state.entry_failure_attempt_count} failed data-fetch "
                f"attempts (1 min apart): {reason}"
            )
            state.entry_failure_next_retry_at = ""
            self._save_state()
            Log.error(f"[Entry] All {state.entry_failure_attempt_count} data-fetch retry attempts "
                      f"failed -- giving up on today's entry. Last failure: {reason}")
            return
        next_retry = datetime.now(IST) + timedelta(seconds=60)
        state.entry_failure_next_retry_at = next_retry.isoformat()
        self._save_state()
        Log.warning(f"[Entry] data fetch failed (attempt {state.entry_failure_attempt_count}/5): "
                    f"{reason} -- retrying in 60s.")

    def _attempt_entry(self):
        state = self.store.state
        pos = state.position

        if state.trade_count >= 1:
            return

        if pos.symbol:
            # Symbol was already decided (a restart happened mid-flow) --
            # skip straight to placing/watching the order.
            self._enter_position()
            return

        if state.today_no_trade:
            return

        # Non-blocking retry gate: a data-fetch failure sequence is in
        # progress (see _register_entry_failure) and its 60s cooldown
        # hasn't elapsed yet -- do nothing this cycle. force-exit/
        # error-resolution/pending-action checks in run_cycle are entirely
        # unaffected by this (they run before this method is ever called).
        if state.entry_failure_next_retry_at:
            try:
                next_retry = datetime.fromisoformat(state.entry_failure_next_retry_at)
            except ValueError:
                next_retry = datetime.now(IST)  # malformed/stale value -- treat as due now, don't get stuck
            if datetime.now(IST) < next_retry:
                return

        if not self._compute_gap_decision():
            # None here can mean either "not ready yet this instant" (spot
            # LTP momentarily unavailable) or a genuine fetch failure (the
            # prev-day 15:15 price truly unavailable, both interval attempts
            # exhausted) -- _compute_gap_decision's own warning already
            # distinguishes the two in the log text; either way, without
            # this data entry cannot proceed, so it counts toward the same
            # 5-attempt budget rather than retrying unbounded forever.
            self._register_entry_failure("previous day's 15:15 price or current spot LTP unavailable")
            return

        option_type = state.today_option_type
        spot = state.today_entry_spot

        try:
            (cur_compact, cur_raw), (next_compact, next_raw) = resolve_current_and_next_week_expiry(self.client)
        except Exception as exc:
            self._register_entry_failure(f"expiry resolution failed: {exc}")
            return

        # Tier 0: current week, both filters hard-AND (see module docstring's
        # "Strike selection" section).
        try:
            chosen = select_strike_both_hard(self.client, cur_compact, option_type, spot)
        except Exception as exc:
            self._register_entry_failure(f"option chain fetch failed for current week expiry {cur_compact}: {exc}")
            return

        if chosen is not None:
            Log.info(f"[Entry] {option_type} strike matched in current week expiry {cur_compact}: "
                     f"strike={chosen['strike']} ltp={chosen['ltp']} distance={chosen['distance_pct']:.2f}%")
        else:
            Log.info(f"[Entry] No {option_type} strike in current week expiry {cur_compact} satisfies both "
                     f"hard filters (premium [{config.premium_min},{config.premium_max}], distance "
                     f"[{config.distance_min_pct},{config.distance_max_pct}]%) -- falling through to "
                     f"next week ({next_compact}).")

            # Tier 1: next week, premium-hard filter only (distance is now
            # just a tie-break, not required) -- see select_strike_premium_hard's
            # own docstring for the xlsx-validation rationale.
            try:
                chosen = select_strike_premium_hard(self.client, next_compact, option_type, spot)
            except Exception as exc:
                self._register_entry_failure(f"option chain fetch failed for next week expiry {next_compact}: {exc}")
                return

            if chosen is not None:
                Log.info(f"[Entry] {option_type} strike matched in next week expiry {next_compact} "
                         f"(tier 1: premium-hard, distance-closest): strike={chosen['strike']} "
                         f"ltp={chosen['ltp']} distance={chosen['distance_pct']:.2f}%")
            else:
                # No degraded fallback tier (removed 2026-08-09 per the
                # project owner -- tier 2's distance-closest-ignoring-premium
                # fallback fired only 2/151 times in backtest and wasn't
                # worth the extra tier). Instead: treat "next week's chain
                # has no strike with premium in range either" as routed
                # through the SAME 5-attempt/60s retry budget as a genuine
                # data/connectivity failure (project owner's explicit
                # instruction) -- NOT an immediate no-trade skip. Only after
                # all 5 attempts are exhausted does today_no_trade actually
                # latch, via _register_entry_failure's own escalation.
                self._register_entry_failure(
                    f"no {option_type} strike with premium in [{config.premium_min},{config.premium_max}] "
                    f"found in next week expiry {next_compact} either"
                )
                return

        # A strike was found -- clear any in-progress failure-retry bookkeeping
        # (harmless if it was already empty) so state.json doesn't carry a
        # stale attempt count/timestamp once entry actually succeeds.
        state.entry_failure_attempt_count = 0
        state.entry_failure_next_retry_at = ""

        quantity = config.lot_multiplier * chosen["lotsize"]
        pos.symbol = chosen["symbol"]
        pos.quantity = quantity
        pos.option_type = option_type
        pos.entry_time = datetime.now(IST).isoformat()
        pos.entry_px = float(chosen["ltp"])
        pos.execution_id = self.execution_id
        self._save_state()
        self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
        self._enter_position()

    # ---- entry / exit (single naked position, resumable) -------------------------
    def _enter_position(self):
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag

        if pos.entry_filled or self._pending_fill:
            return  # already filled, or a background watcher is already tracking this order

        if not pos.entry_order_id:
            try:
                pos.entry_order_id = place(self.client, strategy_tag, pos.symbol,
                                            OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for entry: {exc}")
                self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fill = True
        self._fill_executor.submit(self._watch_entry_fill, pos.entry_order_id, pos.symbol, pos.quantity)

    def _watch_entry_fill(self, order_id: str, symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, symbol, OPTIONS_EXCHANGE, "SELL", quantity)
            pos = self.store.state.position
            if pos.entry_order_id == order_id:  # guard vs. a superseded/stale order
                # Correct entry_px to the broker's ACTUAL fill price now that
                # the order has confirmed complete -- entry_px was only ever
                # a pre-trade chain LTP snapshot until this point (MARKET
                # orders slip). Falls back to that snapshot if the broker
                # response doesn't include either field, so this can only
                # improve accuracy, never break a working fill confirmation.
                # Mirrors reconcile_pending_orders' _reconcile_one, which
                # already does this correctly for the startup crash-recovery
                # path.
                fill_px = fill_data.get("average_price") or fill_data.get("price")
                if fill_px is not None:
                    pos.entry_px = float(fill_px)
                pos.entry_filled = True
                self.store.state.trade_count += 1
                self._save_state()
                Log.info(f"[{LEG_KEY}] Entry filled: {symbol} @ {pos.entry_px}")
        except OrderNeedsAttention as exc:
            self._enter_error_mode("entry_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode("entry_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Unexpected error while watching entry fill: {exc}")
            self._enter_error_mode("entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fill = False

    def _enter_error_mode(self, error_state: str, error_kind: str, error_order_id: str, message: str):
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
        # WhatsApp self-alert on every genuine error-state transition (order
        # rejection, fill timeout, or any other place()/poll_fill failure
        # that routes here) -- fires once per transition, not on the
        # periodic _repush_active_errors re-push above. Dispatched via
        # _bg_executor (non-blocking) and notify_whatsapp_error() itself
        # never raises -- a WhatsApp/network hiccup can never break this
        # method or the calling run_cycle.
        try:
            self._bg_executor.submit(
                notify_whatsapp_error, self.env,
                f"[{config.strategy_name}] {LEG_KEY} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state -- re-pushes at most once per config.error_repush_interval_sec
        so a single lost POST self-heals within a minute instead of leaving
        the UI blind indefinitely. Called unconditionally at the top of
        run_cycle(), every cycle."""
        pos = self.store.state.position
        if not pos.error_state:
            self._last_error_push = None
            return
        now = datetime.now(IST)
        if self._last_error_push is not None and (now - self._last_error_push).total_seconds() < config.error_repush_interval_sec:
            return
        self._last_error_push = now
        action = "SELL" if pos.error_state == "entry_failed" else "BUY"
        try:
            self._bg_executor.submit(push_leg_error, self.env, LEG_KEY, copy.copy(pos), action=action)
        except Exception as exc:
            Log.warning(f"Failed to dispatch periodic error re-push: {exc}")

    def _push_leg_error_bg(self, pos: "Position", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _bg_executor -- for the
        "clear"/on-resolution pushes from _resolve_position_error, which run
        synchronously on run_cycle's own thread. `pos` is snapshotted with a
        shallow copy on THIS (calling) thread first -- executor.submit()
        evaluates its arguments immediately, only the function call itself
        is deferred, and `pos` is a live, mutable object this same cycle may
        reset moments later."""
        snapshot = copy.copy(pos)
        try:
            self._bg_executor.submit(push_leg_error, self.env, LEG_KEY, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"Failed to dispatch push_leg_error: {exc}")

    def _refresh_pending_action_bg(self, leg_key: str):
        if self._pending_action_inflight:
            return
        self._pending_action_inflight = True

        def _run():
            try:
                result = check_pending_action(self.env, leg_key)
                if result is not None:
                    self._pending_action_cache = result
            except Exception as exc:
                Log.warning(f"check_pending_action background refresh failed for {leg_key}: {exc}")
            finally:
                self._pending_action_inflight = False

        self._bg_executor.submit(_run)

    def _pop_pending_action(self, leg_key: str) -> Optional[dict]:
        result = self._pending_action_cache
        self._pending_action_cache = None
        return result

    def _exit_position(self, reason: str):
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag
        if not pos.exit_reason:
            pos.exit_reason = reason

        if pos.exit_filled:
            self._finalize_exit(pos.exit_reason)
            return

        if self._pending_fill:
            return  # exit order already in flight -- background watcher will resolve it

        if not pos.exit_order_id:
            try:
                pos.exit_order_id = place(self.client, strategy_tag, pos.symbol,
                                           OPTIONS_EXCHANGE, "BUY", pos.quantity)
            except Exception as exc:
                Log.exception(f"[{LEG_KEY}] place() failed for exit: {exc}")
                self._enter_error_mode("exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        self._pending_fill = True
        self._fill_executor.submit(self._watch_exit_fill, pos.exit_order_id, pos.symbol, pos.quantity)

    def _watch_exit_fill(self, order_id: str, symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, symbol, OPTIONS_EXCHANGE, "BUY", quantity)
            pos = self.store.state.position
            if pos.exit_order_id == order_id:  # guard vs. a superseded/stale order
                # Capture the broker's ACTUAL exit fill price -- _finalize_exit
                # prefers this over a live LTP re-fetch made after the fact
                # (see its own docstring/precedence). Falls back to None
                # (LTP re-fetch still applies) if the broker response
                # doesn't include either field.
                fill_px = fill_data.get("average_price") or fill_data.get("price")
                if fill_px is not None:
                    pos.exit_fill_px = float(fill_px)
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
            self._pending_fill = False

    def _close_position(self, exit_px: float, reason: str):
        """Shared by _finalize_exit (normal path, LTP-derived exit price) and
        reconcile_pending_orders' exit-completed branch (broker-confirmed
        fill price) -- the trade-log write / today_realized_pnl update /
        WS-unsubscribe / position reset only need to happen once, however
        the exit price was determined."""
        pos = self.store.state.position
        strategy_tag = self.env.strategy_tag
        self.store.state.today_realized_pnl += (pos.entry_px - exit_px) * pos.quantity  # short: entry high, exit low = profit
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
        self.price_stream.remove_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
        self.store.state.position = Position()
        self._save_state()

    def _finalize_exit(self, reason: str):
        """Runs on the main thread's next run_cycle tick once _watch_exit_fill
        has set pos.exit_filled."""
        pos = self.store.state.position
        Log.info(f"[{LEG_KEY}] Position closed: {pos.symbol}")

        # Precedence: an explicit human override (manual_exit_px, set only via
        # a "Manually Completed" resolution) wins first; then the broker's
        # own confirmed fill price (exit_fill_px, captured by
        # _watch_exit_fill from poll_fill()'s return value); a live LTP
        # re-fetch is now only a last-resort fallback for the narrow case
        # where the broker response didn't include average_price/price at
        # all (e.g. a reconcile path that never went through the normal
        # watcher) -- this can only improve accuracy over the old
        # LTP-re-fetch-always approach, never break a working fill.
        exit_px = pos.manual_exit_px
        if exit_px is None:
            exit_px = pos.exit_fill_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, OPTIONS_EXCHANGE, require_two_sided=True)
        if exit_px is None:
            Log.warning(f"[{LEG_KEY}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self._close_position(exit_px, reason)

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    def _resolve_position_error(self, action: dict):
        if self._pending_fill:
            # A Retry/Cancel resolution (or a resumed fill watcher) is
            # already in flight -- leave the new action un-acked, picked up
            # on a later cycle once _pending_fill clears.
            return
        pos = self.store.state.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fill = True
            ack_pending_action(self.env, LEG_KEY)
            self._fill_executor.submit(self._do_retry_resolution, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                pos.exit_order_id = ""
                pos.exit_reason = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(pos, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            if kind == "terminal":
                self.price_stream.remove_instruments(
                    [{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}]
                )
                self.store.state.position = Position()
                self._save_state()
                self._push_leg_error_bg(self.store.state.position, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                return
            # kind == "resting": one last honest re-price + bounded wait,
            # then an explicit cancelorder() if it still didn't fill.
            ack_pending_action(self.env, LEG_KEY)
            self._pending_fill = True
            self._fill_executor.submit(
                self._watch_entry_cancel, pos.error_order_id, pos.symbol, pos.quantity
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if was_exit:
                pos.exit_filled = True
                pos.manual_exit_px = fill_price
                if not pos.exit_reason:
                    pos.exit_reason = "manual"
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(pos, clear=True)
                ack_pending_action(self.env, LEG_KEY)
                # "Manually Completed" finalizes immediately -- do not wait
                # for the next run_cycle pass to notice exit_filled.
                self._finalize_exit(pos.exit_reason)
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

    def _do_retry_resolution(self, was_exit: bool, kind: str):
        try:
            pos = self.store.state.position
            if was_exit:
                if kind == "resting":
                    bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, OPTIONS_EXCHANGE)
                    if ask is not None:
                        try:
                            self.client.modifyorder(
                                order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                                symbol=pos.symbol, action="BUY",
                                exchange=OPTIONS_EXCHANGE, price_type="LIMIT",
                                product=config.product, quantity=str(pos.quantity),
                                price=str(ask), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:  # kind == "terminal"
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                push_leg_error(self.env, LEG_KEY, pos, clear=True)
                self._pending_fill = False
                return

            # Entry side: run_cycle only ever calls _attempt_entry when
            # pos.symbol is empty -- an entry attempt already in error mode
            # has pos.symbol set, so Retry must resubmit the watcher itself.
            if kind == "resting":
                bid, ask = fetch_symbol_bid_ask(self.ltp_client, pos.symbol, OPTIONS_EXCHANGE)
                if bid is not None:
                    try:
                        self.client.modifyorder(
                            order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                            symbol=pos.symbol, action="SELL",
                            exchange=OPTIONS_EXCHANGE, price_type="LIMIT",
                            product=config.product, quantity=str(pos.quantity),
                            price=str(bid), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{LEG_KEY}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = pos.error_order_id
            else:  # kind == "terminal" -- nothing resting, place a genuinely new order
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                             OPTIONS_EXCHANGE, "SELL", pos.quantity)
                except Exception as exc:
                    Log.exception(f"[{LEG_KEY}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode("entry_failed", "terminal", "", str(exc))
                    self._pending_fill = False
                    return
                pos.entry_order_id = resume_order_id
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, LEG_KEY, pos, clear=True)
            # _watch_entry_fill owns _pending_fill from here.
            self._fill_executor.submit(
                self._watch_entry_fill, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fill = False

    def _watch_entry_cancel(self, order_id: str, symbol: str, quantity: int):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned."""
        strategy_tag = self.env.strategy_tag
        try:
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             symbol, OPTIONS_EXCHANGE, "SELL", quantity)
            pos = self.store.state.position
            if pos.error_order_id != order_id:
                return  # superseded by a newer action/order in the meantime -- do nothing
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
                    self.client.cancelorder(order_id=order_id, strategy=strategy_tag)
                except Exception as exc:
                    Log.warning(f"[{LEG_KEY}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify "
                                f"manually at the broker that nothing is resting.")
                self.price_stream.remove_instruments(
                    [{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}]
                )
                self.store.state.position = Position()
                self._save_state()
            push_leg_error(self.env, LEG_KEY, self.store.state.position, clear=True)
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode("entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fill = False

    # ---- startup reconciliation ---------------------------------------------
    def reconcile_pending_orders(self):
        """Startup-only crash recovery: finds an order that was placed but
        never confirmed filled before the previous process instance stopped
        (killed, redeployed, crashed). Called once from main(), before the
        scheduler starts. Mirrors Nifty_OI_WeeklyBuy_MonthlySell's
        reconcile_pending_orders -- the most recently hardened version of
        this pattern in the project -- adapted to this script's single
        Position instead of a dict of legs."""
        pos = self.store.state.position
        if pos.entry_order_id and not pos.entry_filled and not pos.error_state:
            self._reconcile_one(pos, pos.entry_order_id, "entry")
        elif pos.exit_order_id and not pos.exit_filled and not pos.error_state:
            self._reconcile_one(pos, pos.exit_order_id, "exit")
        elif pos.symbol and not pos.entry_order_id and not pos.entry_filled and not pos.error_state:
            # Narrowest crash window: symbol was chosen and persisted BEFORE
            # place() was called (see _attempt_entry), so place() itself can
            # be retried on a genuinely clean failure. If the process died in
            # the few instructions between that persist and place() returning,
            # we genuinely don't know whether the broker ever saw the order --
            # never guess. Flag for a human to verify against the broker.
            pos.error_state = "entry_failed"
            pos.error_kind = "terminal"
            pos.error_order_id = ""
            pos.error_message = (
                "Restart interrupted this position between recording the attempt and the "
                "broker's placeorder() response -- unknown whether an order/position actually "
                "exists at the broker. Verify manually before choosing Retry (risks a "
                "duplicate if one exists) -- prefer Cancel if nothing was placed, or "
                "Manually Completed with the real fill price if it was."
            )
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, LEG_KEY, pos, action="SELL")
            self._save_state()
            Log.error(f"[{LEG_KEY}] reconcile: ambiguous pre-placeorder crash window -- "
                      f"flagged for manual verification against the broker.")

    def _reconcile_one(self, pos: "Position", order_id: str, phase: str):
        action = "SELL" if phase == "entry" else "BUY"
        try:
            resp = self.client.orderstatus(order_id=order_id, strategy=self.env.strategy_tag)
        except Exception as exc:
            Log.exception(f"[{LEG_KEY}] reconcile: orderstatus() failed for {order_id} -- "
                          f"flagging for manual review rather than guessing.")
            pos.error_state = "entry_failed" if phase == "entry" else "exit_failed"
            pos.error_kind = "resting"
            pos.error_order_id = order_id
            pos.error_message = f"reconcile after restart: orderstatus() failed: {exc}"
            pos.error_since = datetime.now(IST).isoformat()
            push_leg_error(self.env, LEG_KEY, pos, action=action)
            self._save_state()
            return

        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        Log.info(f"[{LEG_KEY}] reconcile: {phase} order {order_id} status='{status}' (resuming after restart)")

        if status == "complete":
            fill_px = float(data.get("average_price") or data.get("price") or pos.entry_px or 0.0)
            if phase == "entry":
                pos.entry_px = fill_px
                pos.entry_filled = True
                self.store.state.trade_count += 1
                self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": OPTIONS_EXCHANGE}])
                self._save_state()
                Log.info(f"[{LEG_KEY}] reconcile: entry {order_id} was actually filled @ {fill_px} -- "
                         f"resuming as an open position.")
            else:
                Log.info(f"[{LEG_KEY}] reconcile: exit {order_id} was actually filled @ {fill_px} -- "
                         f"closing the position.")
                self._close_position(fill_px, pos.exit_reason or "reconciled_after_restart")
            return

        if status in {"rejected", "cancelled", "canceled"}:
            if phase == "entry":
                self.store.state.position = Position()
                Log.info(f"[{LEG_KEY}] reconcile: entry {order_id} was genuinely rejected/cancelled -- "
                         f"clearing the position, safe to re-evaluate fresh.")
            else:
                pos.exit_order_id = ""
                pos.exit_filled = False
                Log.info(f"[{LEG_KEY}] reconcile: exit {order_id} was genuinely rejected/cancelled -- "
                         f"position remains open, will re-evaluate the exit normally.")
            self._save_state()
            return

        # Still resting/pending -- unknown how long it's been that way since
        # the restart; flag for a human decision rather than guess.
        pos.error_state = "entry_failed" if phase == "entry" else "exit_failed"
        pos.error_kind = "resting"
        pos.error_order_id = order_id
        pos.error_message = f"reconcile after restart: order still '{status}', needs a decision."
        pos.error_since = datetime.now(IST).isoformat()
        push_leg_error(self.env, LEG_KEY, pos, action=action)
        Log.error(f"[{LEG_KEY}] reconcile: {phase} order {order_id} still '{status}' after restart -- "
                  f"needs Retry/Cancel/Manually Completed.")
        self._save_state()

    # ---- force exit -----------------------------------------------------------
    def _refresh_force_exit_check_bg(self):
        """Dispatched every cycle, not throttled -- a human just clicked
        Force Exit and expects it picked up quickly."""
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
        """Force-closes the position regardless of the strategy's own SL/
        universal-exit logic. Returns True once flat. A position already in
        error mode is left untouched -- Force Exit doesn't override an
        unresolved Retry/Cancel/Manual decision."""
        pos = self.store.state.position
        if pos.error_state:
            Log.warning(f"[{LEG_KEY}] Force Exit waiting on an unresolved error "
                        f"({pos.error_state}/{pos.error_kind}) -- resolve it via "
                        f"Retry/Cancel/Manually Completed first.")
            return False
        if pos.symbol:
            self._exit_position(reason="force_exit")
            return False
        return True

    # ---- PnL reporting ---------------------------------------------------------
    def _open_positions_for_pnl(self) -> list:
        pos = self.store.state.position
        if not pos.symbol or not pos.entry_filled:
            return []
        ltp = self.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, config.ws_stale_seconds)
        if ltp is None:
            ltp = pos.entry_px  # last-known fallback -- never fabricate movement
        pnl = (pos.entry_px - ltp) * pos.quantity  # short leg
        return [{
            "leg_key": LEG_KEY, "symbol": pos.symbol, "direction": "SHORT",
            "quantity": -pos.quantity, "entry_price": pos.entry_px,
            "current_price": ltp, "pnl": pnl,
            "entry_time": pos.entry_time, "execution_id": pos.execution_id,
        }]

    def report_pnl_tick(self):
        """Its own APScheduler job (see main()), max_instances=1 -- a slow
        push queues behind itself, never behind strategy_cycle. No separate
        executor needed: that scheduler-job isolation is enough (mirrors
        Nifty_OI_WeeklyBuy_MonthlySell, which dropped an unused dedicated
        pnl executor for this exact reason, 2026-08-04)."""
        try:
            report_pnl_to_platform(self.env, self.store.state.today_realized_pnl, self._open_positions_for_pnl())
        except Exception:
            Log.exception("report_pnl_tick failed")

    # ---- main cycle -----------------------------------------------------
    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                pos = self.store.state.position
                if pos.error_state:
                    self._refresh_pending_action_bg(LEG_KEY)
                    pending = self._pop_pending_action(LEG_KEY)
                    if pending is not None:
                        self._resolve_position_error(pending)
                if self._handle_force_exit():
                    Log.warning("Force Exit complete -- position flat. Stopping.")
                    ack_force_exit_complete(self.env)
                    self._force_exit_pending = False
                return

            if not self._within_market_hours():
                return

            past_universal_exit = self._past_universal_exit()
            pos = self.store.state.position

            if past_universal_exit:
                if pos.error_state:
                    # Frozen awaiting a Retry/Cancel/Manual decision -- still
                    # check for a pending action every cycle even after
                    # hours, same reasoning as every sibling script: this
                    # must never become unreachable for the rest of the day.
                    self._refresh_pending_action_bg(LEG_KEY)
                    pending = self._pop_pending_action(LEG_KEY)
                    if pending is not None:
                        self._resolve_position_error(pending)
                    else:
                        Log.error(f"[{LEG_KEY}] Universal exit time reached but still in error mode "
                                  f"({pos.error_state}) -- resolve it via Retry/Cancel/Manually "
                                  f"Completed NOW.")
                    return
                if pos.symbol:
                    Log.warning(f"[{LEG_KEY}] Universal exit time reached; force-closing.")
                    self._exit_position(reason="universal_exit")
                return

            if pos.error_state:
                self._refresh_pending_action_bg(LEG_KEY)
                pending = self._pop_pending_action(LEG_KEY)
                if pending is not None:
                    self._resolve_position_error(pending)
                return

            if pos.symbol:
                exit_already_committed = bool(pos.exit_order_id) or pos.exit_filled
                if exit_already_committed:
                    self._exit_position(reason=pos.exit_reason or "unknown")
                    return

                # Only real exit condition besides universal-exit-time: the
                # stop-loss. No opposite-signal/technical exit for this
                # strategy (see module docstring).
                option_ltp = self.price_stream.get_ltp(pos.symbol, OPTIONS_EXCHANGE, max_age=config.ws_stale_seconds)
                if option_ltp is None:
                    option_ltp = fetch_symbol_ltp(self.ltp_client, pos.symbol, OPTIONS_EXCHANGE, require_two_sided=True)
                if option_ltp is not None:
                    sl_trigger_px = pos.entry_px * (1 + config.sl_pct)
                    if option_ltp >= sl_trigger_px:
                        Log.info(f"[{LEG_KEY}] Stop-loss hit: ltp={option_ltp} >= trigger={sl_trigger_px:.2f} "
                                 f"(entry={pos.entry_px}, sl_pct={config.sl_pct}).")
                        self._exit_position(reason="stop_loss")
                return

            # No position yet today.
            if self.store.state.trade_count >= 1:
                return
            # entry_window_end (09:35) gates the FIRST entry attempt only.
            # Once a data-fetch failure retry sequence has actually started
            # (entry_failure_attempt_count > 0) and hasn't been given up on
            # yet (today_no_trade still False -- see _register_entry_failure),
            # keep retrying past the window until it succeeds or exhausts all
            # 5 attempts: the project owner's "try 5 times, 1 minute apart"
            # instruction takes priority for this failure path specifically.
            # This deliberately does NOT widen the window for the normal,
            # no-failure case -- a clean run still resolves well within
            # 09:30-09:35 exactly as before.
            retry_in_progress = (
                self.store.state.entry_failure_attempt_count > 0
                and not self.store.state.today_no_trade
            )
            if not self._within_entry_window() and not retry_in_progress:
                return
            self._attempt_entry()
        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            # WhatsApp self-alert for a genuinely unexpected crash not
            # already routed through _enter_error_mode's own alert.
            # Throttled (cycle_failure_notify_interval_sec) since an outer
            # catch-all could otherwise fire every scheduler tick if the
            # same bug keeps recurring -- notify_whatsapp_error() itself
            # never raises and this is dispatched via _bg_executor
            # (non-blocking), so a WhatsApp/network hiccup here can never
            # compound the original failure.
            now = datetime.now(IST)
            if (self._last_cycle_failure_notify is None
                    or (now - self._last_cycle_failure_notify).total_seconds()
                    >= config.cycle_failure_notify_interval_sec):
                self._last_cycle_failure_notify = now
                try:
                    self._bg_executor.submit(
                        notify_whatsapp_error, self.env,
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
    print(f"Instrument           : {UNDERLYING_SYMBOL}")
    print(f"Entry window         : {config.entry_window_start} - {config.entry_window_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Stop-loss            : {config.sl_pct * 100:.0f}% of entry premium")
    print(f"Premium filter       : [{config.premium_min}, {config.premium_max}]")
    print(f"Distance filter      : [{config.distance_min_pct}, {config.distance_max_pct}]% OTM")
    print(f"Max entries/day      : 1")
    print("WARNING: NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK")
    print("WARNING: PRODUCT IS NRML -- NO BROKER-SIDE FORCED SQUARE-OFF BACKSTOP.")
    print("         universal_exit_time's own exit order succeeding is the ONLY")
    print("         thing that closes this position before market close.")
    if config.test_mode:
        print("WARNING: TEST MODE ENABLED -- market-hours/entry-window checks are BYPASSED")
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
    if state_store.state.current_day == today_key and state_store.state.position.symbol:
        already_known.append({
            "symbol": state_store.state.position.symbol, "exchange": OPTIONS_EXCHANGE,
        })
    # Seed BEFORE start() -- see seed_instruments' own docstring for why this
    # avoids a race between start()'s background _connect() and a separate
    # add_instruments() call from this (the main) thread.
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

    # Crash-recovery reconciliation FIRST -- before anything else touches
    # state. Resolves the narrow order-placed-but-not-yet-confirmed window
    # (see reconcile_pending_orders' docstring); an already-fully-filled
    # position needs no reconciliation, it just resumes normally once the
    # scheduler starts.
    engine.reconcile_pending_orders()

    pos = state_store.state.position
    if pos.error_state:
        action = "SELL" if pos.error_state == "entry_failed" else "BUY"
        push_leg_error(env, LEG_KEY, pos, action=action)
        Log.error(f"[{LEG_KEY}] Resuming with an unresolved error from before restart "
                  f"({pos.error_state}/{pos.error_kind}) -- needs Retry/Cancel/Manually Completed.")
    elif pos.symbol:
        Log.info(f"[{LEG_KEY}] Resuming an already-open position from before restart: "
                 f"{pos.symbol}@{pos.entry_px} -- monitoring, not re-entering.")

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
