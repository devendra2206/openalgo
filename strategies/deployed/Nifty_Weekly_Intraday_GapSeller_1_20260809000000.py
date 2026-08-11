"""
===============================================================================
NIFTY & SENSEX Weekly Intraday Gap Seller
===============================================================================
Version     : 2.0.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11

Source      : historical Excel trade log ("Intraday Weekly Option Sell
              Strategy_....xlsx", analyzed offline, not part of this repo).
              Rules below were confirmed directly with the project owner via
              Q&A and are final -- this docstring documents WHAT was agreed,
              not a re-derivation.

*** THIS STRATEGY SELLS NAKED (UNHEDGED) OPTIONS -- UNDEFINED RISK          ***
*** PRODUCT IS NRML, NOT MIS -- SEE THE WARNING BELOW BEFORE TOUCHING       ***
*** config.universal_exit_time OR config.product.                          ***

Description
-----------
Pure INTRADAY option-selling strategy, now covering TWO fully independent
instruments -- NIFTY and SENSEX -- run in the same process (converted
2026-08-10 from an original NIFTY-only single-`Position` design). For EACH
instrument, exactly ONE naked leg is ever open at a time: either that day's PE
or that day's CE for THAT instrument, never both, decided once per day by the
gap-direction rule below, evaluated entirely independently per instrument. On
a given day NIFTY may sell a PE while SENSEX sells a CE (or doesn't trade at
all) -- one instrument's outcome never influences the other's.

Structurally this now follows the same per-leg-dict pattern as this project's
other NIFTY/SENSEX scripts (EMA34_RSI, Pivot+Supertrend): `StrategyState.legs`
is a dict keyed by `LEG_KEYS` (`NIFTY_GAPSELLER`, `SENSEX_GAPSELLER`), each
value a `LegState` wrapping its own `Position`, `trade_count`, gap-decision
fields, and data-failure retry counters. Unlike those siblings, each
`LEG_KEYS` entry is ONE leg (not a PE/CE pair) -- only one side is ever even
considered per instrument per day, matching the original design's own
per-instrument decision, just no longer collapsed into a single global slot.

Signal / entry rule (locked, confirmed with the project owner) -- PER INSTRUMENT
------------------------------------------------------------------------
Evaluated once per day PER INSTRUMENT, at config.entry_window_start (09:30
IST), with a narrow tolerance window (09:30-09:35, config.entry_window_end)
purely to absorb scheduler_interval jitter -- this is NOT a retry-all-day
window; if nothing is entered by 09:35 for a given instrument, that
instrument's day is a no-trade day (the OTHER instrument is unaffected).

  Gap direction: compare THIS INSTRUMENT'S OWN spot at the PREVIOUS trading
  day's 15:15 IST close against THIS INSTRUMENT'S OWN spot at TODAY's 09:30
  IST. NIFTY's gap decision uses NIFTY.NSE_INDEX prices only; SENSEX's uses
  SENSEX.BSE_INDEX prices only -- never cross-referenced.

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
  used elsewhere in this codebase: if "1m" comes back empty/error, this
  script falls back once to config.prev_day_price_interval_fallback ("5m")
  for that same lookup. Today's 09:30 price is a live LTP read (WebSocket
  cache via PriceStream, REST client.quotes() fallback if stale/missing)
  taken at the moment the entry window opens, not a candle close.

Strike selection -- ONE rule, applied to TWO expiries in order (locked,
confirmed with the project owner, last revised 2026-08-10) -- PER
INSTRUMENT, using that instrument's OWN premium band
------------------------------------------------------------------------
NIFTY:  premium [30, 42],  distance >= 1.0% OTM (floor only, no ceiling).
SENSEX: premium [90, 120], distance >= 1.0% OTM (same floor as NIFTY --
        explicitly confirmed by the project owner, not a typo; only the
        premium band differs between the two instruments).

select_strike() -- the ONE filter, used identically for both tiers below
(collapsed from an earlier design with a DIFFERENT filter shape per tier,
see history note below):

  1. Premium (LTP) HARD filter: inst.premium_min <= ltp <= inst.premium_max.
  2. Strike distance from spot -- FLOOR ONLY, no upper cap:
     distance_pct >= inst.distance_min_pct (1.0% for both instruments) --
     for a PE, the strike is BELOW spot by that percentage; for a CE, ABOVE
     spot. Any distance beyond the floor qualifies as long as premium also
     matches.

  Among multiple simultaneous matches: closest premium to the midpoint of
  [premium_min, premium_max] (ASSUMPTION -- not specified by the project
  owner, flagged for their review).

Tier 0 (current week, resolve_current_and_next_week_expiry()'s "current"
slot -- the nearest expiry that ISN'T today, same same-day-expiry
roll-forward every sibling script in this project uses, since a
same-day-expiring contract has an extreme gamma/theta cliff in its final
hours): select_strike() against the current week's chain. No match ->
tier 1.

Tier 1 (next week's expiry): select_strike() re-applied UNCHANGED against
next week's chain -- project owner's exact words, 2026-08-10: "if not found
then move to next weekly option" (i.e. re-apply the identical rule, not a
looser one). No match at all -> routed into the data/connectivity-failure
retry mechanism below, NOT a separate degraded-fallback tier.

History note: this replaced an EARLIER two-tier design (2026-08-09) where
tier 0 required a HARD [1.0%, 1.5%] distance band (both filters hard-AND)
and tier 1 dropped distance to a mere tie-break with only premium hard --
validated at the time by backtesting against the real historical xlsx trade
log this strategy is based on (C:\\Devendra\\OpenAlgo\\data23_to_26,
NIFTY-only): the real trades honored premium 94.8% of the time vs. distance
only 35.1% of the time (mean distance 1.88%, median 1.69%, up to 5.73%).
The project owner then simplified this further (2026-08-10) to a single
premium-hard/distance-floor-only rule applied identically to both tiers --
the upper 1.5% distance ceiling is now gone entirely, for both instruments.

*** TIER 2 (an even earlier distance-closest-ignoring-premium degraded
fallback) WAS REMOVED 2026-08-09 *** per the project owner's explicit
instruction ("remove tier 2 as its not needed"). If tier 1 also finds
nothing (no strike on the correct side satisfies select_strike() in next
week's chain either), that is treated the SAME WAY as a genuine
data/connectivity failure (project owner's explicit choice when asked): it
goes through the SAME 5-attempt/60s retry budget described below, not an
immediate skip and not an immediate degraded-strike trade. Only after all 5
retries are exhausted does that instrument's own `leg.today_no_trade` latch
for the day -- the OTHER instrument's retry budget/no-trade state is
completely independent.

Data/connectivity failure retries (5 attempts, 1 minute apart, PER INSTRUMENT
-- project owner's exact words: "in case [of] no data or failure, you should
try for 5 times with 1 min interval")
------------------------------------------------------------------------
Scope: GENUINE data/connectivity failures during that instrument's entry
sequence, PLUS (since the tier-2 removal above) tier 1 finding no
premium-qualifying strike in next week's chain at all -- never a legitimate
tier-0 "no match, falling through to tier 1" outcome, which is normal tier
progression, not a failure. Covers: the prev-day 15:15 price fetch failing
(both interval attempts exhausted) or the current spot LTP being unavailable
(gap direction can't be computed at all without these); expiry resolution
failing; an option chain fetch erroring or returning something unusable at
tier 0 or tier 1; and tier 1 itself completing successfully but matching no
strike with premium in range on either side.

Mechanism (see _register_entry_failure, non-blocking -- no time.sleep
anywhere, per AUTHORING_CHECKLIST.md's rule that nothing may block the
scheduler thread): each such failure increments that leg's persisted
`leg.entry_failure_attempt_count` and sets
`leg.entry_failure_next_retry_at` to now + 60s, then returns from that
cycle's entry attempt for THAT instrument having placed nothing (the other
instrument's own evaluation this same cycle is unaffected).
`_attempt_entry(leg_key, inst)` re-checks this timestamp on every later
run_cycle tick and simply does nothing for that instrument until it's due --
force-exit/error-resolution/pending-action checks elsewhere in run_cycle, and
the OTHER instrument's own entry attempt, are completely unaffected. When
due, the WHOLE entry sequence (tiers 0-1) is retried from the top for that
instrument, not resumed mid-tier -- deliberately, to avoid a second
entry-point duplicating the tier logic (leg.today_gap_computed being already
persisted means a retry doesn't re-fetch the prev-day price if that part
already succeeded). After 5 total failed attempts,
`leg.today_no_trade` is FINALLY latched for that instrument with an
error-level log stating all 5 attempts failed, and no further entry is
attempted for that instrument for the rest of today's window. Both counters
reset to 0/"" per-leg at the start of each new trading day (same place
`today_no_trade` itself resets) and are persisted via StateStore exactly like
every other per-leg state field, so a same-day restart resumes each
instrument's count instead of silently resetting to 0 or crashing on the new
fields.

Entry-window interaction: entry_window_end (09:35) still gates the FIRST
entry attempt only, per instrument. Once a retry sequence has actually
started for an instrument (leg.entry_failure_attempt_count > 0) and hasn't
been given up on yet, run_cycle lets THAT instrument keep retrying PAST
entry_window_end until it either succeeds or exhausts all 5 attempts (worst
case ~5 minutes past the first failure) -- chosen over widening
entry_window_end itself so the window's normal, no-failure-case behavior is
completely unchanged; time is only borrowed when a real failure sequence is
actively in progress for that specific instrument. See config.entry_window_end's
own comment and run_cycle's per-instrument "retry_in_progress" check.

Risk management (locked, confirmed with the project owner) -- shared across
both instruments
------------------------------------------------------------------------
  - Stop-loss: config.sl_pct = 0.50 (50%) of entry premium -- buy back the
    short the moment its LTP >= entry_px * (1 + sl_pct). This number comes
    directly from analyzing 49 historical stop-outs in the source Excel log:
    mean 50.9%, median 50.7%, range 42-58% of entry premium. 50% sits inside
    that observed range and matches the project owner's own choice.
  - NO profit target. A position that isn't stopped out simply runs to the
    universal intraday square-off below.
  - Universal exit (square-off): config.universal_exit_time = 15:15 IST,
    same 15-minute buffer before market close already used by every other
    NIFTY/SENSEX intraday script in this project (EMA34_RSI, Pivot+Supertrend).
    The project owner chose this explicitly, fully aware that NRML (see below)
    gives it no broker-side backstop -- this exit order succeeding is the
    ONLY thing that closes a given instrument's position.
  - Product: NRML. *** The project owner's explicit choice, despite this
    assistant's own recommendation of MIS. Unlike MIS, NRML has NO
    broker-side forced square-off backstop at all -- if a leg's own
    universal_exit_time exit order fails for any reason (network blip,
    rejection, the process being down at 15:15), nothing else closes an
    undefined-risk naked short before market close, and that leg carries
    overnight as a genuine, unbounded-risk position. See
    AUTHORING_CHECKLIST.md's "product (MIS vs NRML) and universal_exit_time
    must be chosen together" item. ***
  - NO re-entry after a stop-loss (or any exit) the same day, PER INSTRUMENT
    -- max ONE entry per instrument per day, full stop. NIFTY trading today
    never blocks or unblocks SENSEX's own ability to trade today (and vice
    versa). Enforced via leg.trade_count (persisted per leg, so a mid-day
    restart can't re-trigger a duplicate entry for an instrument that already
    traded) exactly like every sibling script's own trade_count guard, just
    capped at 1 instead of N.
  - Quantity: config.lot_multiplier = 1 lot, lot size fetched dynamically
    from the option chain response's own "lotsize" field -- never
    hardcoded, matching every other deployed script's convention.

Explicitly OUT of scope vs. this project's other NIFTY/SENSEX scripts
------------------------------------------------------------------------
  - No dynamic futures-contract resolution (both NIFTY and SENSEX have real
    quotable index spots, used directly).
  - No opposite-signal / technical exit -- exit is ONLY stop-loss or the
    universal exit time, never a signal reversal.
  - Unlike EMA34_RSI/Pivot+Supertrend, each instrument here trades AT MOST
    ONE leg (that day's single PE-or-CE decision), not an independent PE
    leg and CE leg pair -- LEG_KEYS therefore has exactly one entry per
    instrument (`NIFTY_GAPSELLER`, `SENSEX_GAPSELLER`), not four.

Order placement, fill confirmation, error recovery, PnL/trade-log reporting
------------------------------------------------------------------------
Identical conventions and identical machinery to every other deployed script
in this project (docs/prd/python-strategies-order-error-recovery.md), now
applied per-leg (per instrument) instead of to a single global Position:
place() retries only a clean broker rejection, never an ambiguous exception
(duplicate-order risk); poll_fill()/_reprice_and_wait_once() cross the
spread with a fresh bid/ask rather than looping on a stale price and hand
off to a background fill watcher rather than blocking run_cycle; each leg's
Position carries the same error_state/error_kind/error_order_id/
error_message/error_since fields and the same Retry/Cancel/Manually-Completed
resolution machinery as every sibling script's LegPosition; reconcile_pending_orders()
runs once at startup, before the scheduler starts, per leg, and queries the
broker directly via orderstatus() rather than guessing; push_leg_error/
check_pending_action/check_force_exit all go through a shared, dedicated
background executor (_bg_executor) rather than running inline on run_cycle's
own thread, and target STRATEGY_REPORTING_PORT (default 8766) via the same
_post_json_local/_get_json_local pair used everywhere else in this project.

WebSocket price feed
------------------------------------------------------------------------
PriceStream below is the same hardened design already used by
Nifty_OI_WeeklyBuy_MonthlySell (itself descended from
MCX_CrudeOil_EMA9_RSI_Intraday/Batman/VWAP_NoHA/Combined): per-symbol
subscribe only, no unsubscribe before a stale-retry resubscribe, a
majority-based REST-confirmation gate before ever escalating to a full
reconnect, and independent per-symbol backoff. This strategy's own WS
footprint is now at most 4 symbols at a time (NIFTY spot + SENSEX spot + up
to one open option leg per instrument).

Non-blocking execution -- a deliberate simplification vs. the multi-leg
sibling scripts (documented as a judgement call, not an oversight)
------------------------------------------------------------------------
EMA34_RSI/MCX/OI background their per-candle SIGNAL refresh via a dedicated
executor so one slow broker round-trip never delays every OTHER leg's
evaluation on the same scheduler tick. This strategy has no such steady-state
signal loop -- the gap decision and strike-selection scan each fire AT MOST
ONCE PER DAY PER INSTRUMENT, inside the narrow 09:30-09:35 entry window.
run_cycle evaluates NIFTY then SENSEX sequentially inline each tick (one
history() call + up to two optionchain() calls per instrument, each bounded
by Environment.timeout=10s) -- a deliberate choice to keep the script simpler
without reintroducing the actual bug class the executor pattern exists to
prevent (nothing else in run_cycle is EVER blocked behind this: force-exit
checks, pending-action resolution, and PnL reporting all still go through
_bg_executor/their own scheduler job, completely unaffected by however long
the gap/strike scan takes on a given cycle). Worst case, both instruments'
entry sequences run inline back-to-back in the same 09:30-09:35 window tick
-- still bounded by 2x Environment.timeout, not unbounded, and this only
ever happens inside that narrow window. Order placement and fill confirmation
themselves are NOT part of this inline path -- place() is fast and stays
synchronous like every sibling script, and poll_fill() still runs on a
background fill watcher (_fill_executor), never inline.

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
@dataclass
class InstrumentConfig:
    name: str                    # "NIFTY" / "SENSEX"
    underlying_exchange: str     # NSE_INDEX / BSE_INDEX
    options_exchange: str        # NFO / BFO
    # Per-instrument premium band -- NIFTY and SENSEX have DIFFERENT premium
    # bands (unlike this project's other multi-instrument scripts, where
    # both instruments share one Config-level band), so this lives on
    # InstrumentConfig, not on the shared Config dataclass below.
    # distance_min_pct is a FLOOR ONLY (no upper cap -- removed project-wide
    # 2026-08-10 per the project owner: "if not found then move to next
    # weekly option" using the identical filter, not a looser one) and is
    # the same fixed value for both instruments, but still lives per-
    # instrument for symmetry/future-proofing.
    premium_min: float
    premium_max: float
    distance_min_pct: float


INSTRUMENTS = [
    InstrumentConfig(
        name="NIFTY", underlying_exchange="NSE_INDEX", options_exchange="NFO",
        premium_min=30.0, premium_max=42.0,
        distance_min_pct=1.0,
    ),
    InstrumentConfig(
        name="SENSEX", underlying_exchange="BSE_INDEX", options_exchange="BFO",
        premium_min=90.0, premium_max=120.0,
        distance_min_pct=1.0,   # same floor as NIFTY -- confirmed, not a typo
    ),
]

# One leg per instrument (this day's single PE-or-CE decision) -- NOT a
# PE/CE pair like this project's other multi-instrument scripts. Kept as
# "<NAME>_GAPSELLER" to match the original single-instrument script's own
# LEG_KEY constant ("NIFTY_GAPSELLER"), which every shared reporting
# endpoint (push_leg_error, check_pending_action, ...) already used for
# logging/tagging before this conversion.
LEG_KEYS = [f"{inst.name}_GAPSELLER" for inst in INSTRUMENTS]


def _leg_key_for(inst: "InstrumentConfig") -> str:
    return f"{inst.name}_GAPSELLER"


def _inst_for_leg_key(leg_key: str) -> "InstrumentConfig":
    inst_name = leg_key.split("_")[0]
    return next(i for i in INSTRUMENTS if i.name == inst_name)


@dataclass
class Config:
    strategy_name: str = "NIFTY & SENSEX Weekly Intraday Gap Seller"
    version: str = "2.0.0"

    # --- entry timing --------------------------------------------------
    entry_window_start: time = time(9, 30)
    entry_window_end: time = time(9, 35)   # tolerance for scheduler_interval jitter, NOT a retry-all-day window
    # NOTE: this end time gates the FIRST entry attempt only, per instrument.
    # A data-fetch failure retry sequence (see _register_entry_failure, up to
    # 5 attempts 60s apart) is deliberately allowed to keep running PAST this
    # time once started, for that instrument only -- see run_cycle's
    # per-instrument "retry_in_progress" check. Left unwidened (rather than
    # e.g. 09:40) so a normal, no-failure day's behavior is completely
    # unchanged.

    # --- gap-direction reference price -----------------------------------
    prev_day_price_target_time: time = time(15, 15)
    prev_day_price_interval: str = "1m"     # documented interval (docs/api/market-data/intervals.md)
    prev_day_price_interval_fallback: str = "5m"   # broker-dependent "1m" support -- defensive fallback

    # --- strike selection ------------------------------------------------
    # NOTE: premium_min/max and distance_min/max_pct are NOT here -- they
    # live per-instrument on InstrumentConfig above, since NIFTY and SENSEX
    # use different premium bands. strike_count is shared (both instruments'
    # bands sit comfortably within this many strikes either side of ATM).
    strike_count: int = 20   # optionchain() strikes above/below ATM

    # --- risk management ---------------------------------------------------
    sl_pct: float = 0.50   # 50% of entry premium -- see module docstring's provenance note (49 historical stop-outs)
    lot_multiplier: int = 1

    # NRML -- project owner's explicit choice; see module docstring's
    # prominent warning before changing this or universal_exit_time.
    product: str = "NRML"
    price_type: str = "MARKET"

    # 2026-08-11: moved 15:15 -> 15:05 -- SEBI's 15:15-15:20 transition/
    # reference-price window disallows fresh order entry (including
    # closes), so this must clear well before that window opens rather
    # than racing its start -- especially important here since this is the
    # ONLY backstop under NRML (no broker-side square-off net if this
    # order fails).
    universal_exit_time: time = time(15, 5)
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
    # 2026-07-28). Re-pushed at this interval for as long as a leg stays
    # in error_state.
    error_repush_interval_sec: float = 60.0

    # Minimum gap between WhatsApp alerts fired from run_cycle's own outer
    # except-clause (genuinely unexpected crashes not already funneled
    # through _enter_error_mode's error-state machine). Shared across both
    # instruments -- one crash alert covers the whole cycle, not per leg.
    # Without this, a persistently-recurring bug would fire one WhatsApp
    # message per scheduler tick (every scheduler_interval seconds) and
    # flood the phone.
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
class LegState:
    """One instrument's entire day-to-day state -- position, trade count,
    its own gap decision, and its own data-failure retry budget. Wraps a
    single `Position` (not a PE/CE pair) since only one side is ever
    considered per instrument per day. Keyed by LEG_KEYS on
    StrategyState.legs, replacing the original single global
    `StrategyState.position` field."""
    trade_count: int = 0            # 0 or 1 -- max ONE entry per instrument per day, whole stop
    position: Position = field(default_factory=Position)

    # Gap decision, computed once at the entry window's first cycle for THIS
    # instrument and locked for the rest of the day (today_gap_computed
    # persists it across a restart mid-window).
    today_gap_computed: bool = False
    today_prev_close: float = 0.0
    today_entry_spot: float = 0.0
    today_gap_pct: float = 0.0
    today_option_type: str = ""     # "PE" / "CE", set once today_gap_computed is True

    # Data/connectivity failure retry budget for THIS instrument's entry
    # sequence (prev-day 15:15 price fetch, expiry resolution, option chain
    # fetch at any tier) -- NOT for a legitimate "no strike matched" tier
    # outcome, which is never a failure. Up to 5 attempts, 60s apart,
    # non-blocking (see _register_entry_failure). Persisted so a same-day
    # restart resumes the count instead of silently resetting to 0 or
    # crashing on the field.
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


@dataclass
class StrategyState:
    current_day: str = ""
    legs: dict = field(default_factory=lambda: {k: LegState() for k in LEG_KEYS})
    today_realized_pnl: float = 0.0   # sum of BOTH instruments' closed-leg pnl_rupees today
    last_updated: str = ""
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
# most 4 symbols (NIFTY spot + SENSEX spot + up to one open option leg per
# instrument).
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
        self.state.today_realized_pnl = data.get("today_realized_pnl", 0.0)
        self.state.last_updated = data.get("last_updated", "")
        self.state.last_execution_id = data.get("last_execution_id", 0)
        if "legs" in data:
            legs_data = data.get("legs", {})
        else:
            # Pre-refactor (single-instrument, NIFTY-only) state file: these
            # fields lived at the top level, not nested under "legs". Without
            # this branch every leg would silently get a blank LegState() on
            # first load post-refactor -- losing a real open position's SL
            # tracking and allowing a duplicate same-day re-entry.
            Log.warning(f"[StateStore] Migrating pre-refactor (single-instrument) "
                        f"state file {self.path} -> NIFTY_GAPSELLER leg")
            legs_data = {"NIFTY_GAPSELLER": data}
        for key in LEG_KEYS:
            leg_raw = legs_data.get(key, {})
            leg = LegState()
            leg.trade_count = leg_raw.get("trade_count", 0)
            leg.today_gap_computed = leg_raw.get("today_gap_computed", False)
            leg.today_prev_close = leg_raw.get("today_prev_close", 0.0)
            leg.today_entry_spot = leg_raw.get("today_entry_spot", 0.0)
            leg.today_gap_pct = leg_raw.get("today_gap_pct", 0.0)
            leg.today_option_type = leg_raw.get("today_option_type", "")
            leg.today_no_trade = leg_raw.get("today_no_trade", False)
            leg.today_no_trade_reason = leg_raw.get("today_no_trade_reason", "")
            leg.entry_failure_attempt_count = leg_raw.get("entry_failure_attempt_count", 0)
            leg.entry_failure_next_retry_at = leg_raw.get("entry_failure_next_retry_at", "")
            pos_raw = leg_raw.get("position", {})
            leg.position = Position(**{**asdict(Position()), **filter_known_fields(Position, pos_raw)})
            self.state.legs[key] = leg
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_updated": self.state.last_updated,
            "last_execution_id": self.state.last_execution_id,
            "legs": {
                key: {
                    "trade_count": leg.trade_count,
                    "today_gap_computed": leg.today_gap_computed,
                    "today_prev_close": leg.today_prev_close,
                    "today_entry_spot": leg.today_entry_spot,
                    "today_gap_pct": leg.today_gap_pct,
                    "today_option_type": leg.today_option_type,
                    "today_no_trade": leg.today_no_trade,
                    "today_no_trade_reason": leg.today_no_trade_reason,
                    "entry_failure_attempt_count": leg.entry_failure_attempt_count,
                    "entry_failure_next_retry_at": leg.entry_failure_next_retry_at,
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


def resolve_current_and_next_week_expiry(client, inst: InstrumentConfig) -> tuple[tuple[str, str], tuple[str, str]]:
    """Returns ((current_week_compact, current_week_raw), (next_week_compact,
    next_week_raw)) -- the nearest upcoming weekly expiry for `inst` (rolling
    forward one slot if today itself is an expiry day, same gamma/theta-
    cliff avoidance every sibling script in this project uses) and the
    weekly expiry immediately after it. This is what lets strike selection
    try "current week" then fall back to "next week" per the module
    docstring's Resolution order -- the same-day roll composes with that
    fallback automatically, since "current week" here always already means
    "the nearest expiry that isn't today"."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve {inst.name} options expiry: {resp}")
    today = datetime.now(IST).date()
    parsed = [(datetime.strptime(raw, "%d-%b-%y").date(), raw) for raw in resp["data"]]
    parsed = [p for p in parsed if p[0] >= today]
    if not parsed:
        raise RuntimeError(f"No upcoming {inst.name} options expiry returned by the broker.")

    idx = 0
    if parsed[0][0] == today:
        idx = 1
    if idx >= len(parsed):
        raise RuntimeError(
            f"Today is the nearest {inst.name} expiry and the broker returned no later expiry date "
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


def fetch_option_chain(client, inst: InstrumentConfig, expiry_compact: str, strike_count: int) -> dict:
    resp = client.optionchain(
        underlying=inst.name, exchange=inst.underlying_exchange,
        expiry_date=expiry_compact, strike_count=strike_count,
    )
    if resp.get("status") != "success" or not resp.get("chain"):
        raise RuntimeError(f"optionchain() failed for {inst.name} expiry {expiry_compact}: {resp}")
    return resp


def _chain_candidates(client, inst: InstrumentConfig, expiry_compact: str, option_type: str, spot: float) -> list:
    """Every strike in `expiry_compact`'s chain on the `option_type` side,
    with its own distance_pct already computed -- no filtering applied here.
    Shared by select_strike(), called once per tier (current week, next
    week) with a different `expiry_compact`."""
    resp = fetch_option_chain(client, inst, expiry_compact, config.strike_count)
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


def select_strike(client, inst: InstrumentConfig, expiry_compact: str,
                   option_type: str, spot: float) -> Optional[dict]:
    """Single strike-selection rule, used IDENTICALLY for both tier 0
    (current week) and tier 1 (next week) -- only the expiry passed in
    differs (project owner, 2026-08-10: "if not found then move to next
    weekly option", i.e. re-apply the exact same rule, not a looser one).
    This REPLACED an earlier two-tier design with different filter shapes
    per tier (tier 0 both-hard within a [1.0,1.5]% distance band, tier 1
    premium-hard-only with distance as a tie-break) -- collapsed into one
    function since both tiers now use the same filter:

      1. Premium (LTP) HARD filter: inst.premium_min <= ltp <= inst.premium_max.
      2. Distance FLOOR ONLY (no upper cap): distance_pct >= inst.distance_min_pct.
         For a PE, the strike is BELOW spot by that percentage; for a CE,
         ABOVE spot. Any distance beyond the floor qualifies as long as
         premium also matches -- the earlier 1.5% ceiling is gone entirely.

    Among strikes satisfying both: closest premium to the midpoint of
    [premium_min, premium_max] (assumption, see module docstring) -- same
    tie-break convention used throughout this file. Returns None (not an
    error) if no strike satisfies both -- the caller decides what tier to
    fall through to next, or (if this was already tier 1) routes it through
    the same 5-attempt/60s data-failure retry budget as a genuine
    chain-fetch error (project owner's explicit instruction), not an
    immediate skip."""
    candidates = _chain_candidates(client, inst, expiry_compact, option_type, spot)
    matched = [
        c for c in candidates
        if inst.premium_min <= c["ltp"] <= inst.premium_max
        and c["distance_pct"] >= inst.distance_min_pct
    ]
    if not matched:
        return None
    midpoint = (inst.premium_min + inst.premium_max) / 2.0
    matched.sort(key=lambda c: abs(c["ltp"] - midpoint))
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
        # Sized to len(LEG_KEYS) (2 -- NIFTY + SENSEX) so one instrument's
        # fill watcher can never delay the other's, matching the multi-leg
        # sibling scripts' own fill-executor sizing.
        self._fill_executor = ThreadPoolExecutor(max_workers=len(LEG_KEYS), thread_name_prefix="fill-watch")
        # Single dedicated pool for every other background REST call
        # (check_force_exit, push_leg_error, check_pending_action, WhatsApp
        # alerts) -- shared across BOTH instruments, mirroring
        # Nifty_OI_WeeklyBuy_MonthlySell's consolidated _bg_executor. Kept
        # separate from _fill_executor so a fill-watcher stuck for minutes in
        # a reprice loop can never delay a Force Exit check.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")

        # _state_lock serializes every store.save() call -- StateStore.save()
        # writes the whole state to one shared JSON file, so concurrent
        # writers (main thread vs. background fill-watch tasks for either
        # instrument) could otherwise corrupt it.
        self._state_lock = threading.Lock()
        self._pending_fills: set = set()            # leg_keys with an in-flight fill watcher
        self._entry_pending: set = set()             # leg_keys with an in-flight background _attempt_entry
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        self._pending_action_cache: dict = {}        # leg_key -> action dict
        self._pending_action_inflight: set = set()    # leg_keys with an in-flight pending-action fetch
        self._last_error_push: dict = {}              # leg_key -> datetime
        # Throttles the outer run_cycle except-clause's WhatsApp alert (see
        # its own comment) -- separate from _last_error_push above, which
        # only governs the UI error-badge re-push. In-memory/per-instance,
        # not persisted to state.json: a restart is itself worth a fresh
        # alert if the same crash recurs immediately. Deliberately kept as a
        # SINGLE shared field, not per-instrument -- a genuinely unexpected
        # crash in run_cycle's own outer except is a whole-cycle failure
        # (both instruments' evaluation for that tick is lost together), so
        # one alert covering the whole cycle is more useful than two nearly-
        # simultaneous duplicate alerts.
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
            self.store.state.today_realized_pnl = 0.0
            for leg_key in LEG_KEYS:
                leg = self.store.state.legs[leg_key]
                leg.trade_count = 0
                leg.today_gap_computed = False
                leg.today_prev_close = 0.0
                leg.today_entry_spot = 0.0
                leg.today_gap_pct = 0.0
                leg.today_option_type = ""
                leg.today_no_trade = False
                leg.today_no_trade_reason = ""
                leg.entry_failure_attempt_count = 0
                leg.entry_failure_next_retry_at = ""
            self._save_state()

    def get_spot_ltp(self, inst: InstrumentConfig) -> Optional[float]:
        ltp = self.price_stream.get_ltp(inst.name, inst.underlying_exchange, config.ws_stale_seconds)
        if ltp is not None:
            return ltp
        return fetch_symbol_ltp(self.ltp_client, inst.name, inst.underlying_exchange)

    # ---- gap decision + strike selection (see module docstring's "Non-blocking
    # execution" note for why this runs inline rather than via a background
    # executor -- at most once per day per instrument, bounded by
    # Environment.timeout) --------------------------------------------------
    def _compute_gap_decision(self, leg_key: str, inst: InstrumentConfig) -> bool:
        """Returns True once leg.today_gap_computed is set for THIS
        instrument (either just now, or already set from an earlier
        cycle/restart today). False means "not ready yet, retry next cycle"
        -- never guesses. Entirely independent of the OTHER instrument's own
        gap decision -- each instrument's spot symbol is used exclusively."""
        leg = self.store.state.legs[leg_key]
        if leg.today_gap_computed:
            return True

        prev_day = resolve_previous_trading_day(datetime.now(IST).date())
        # pytz timezones must be applied via .localize(), never a naive
        # tzinfo= kwarg to datetime.combine() -- that would silently stamp
        # pytz's raw LMT offset instead of the correct IST +5:30.
        target_dt = IST.localize(datetime.combine(prev_day, config.prev_day_price_target_time))
        prev_close = fetch_price_at_or_before(
            self.client, inst.name, inst.underlying_exchange, target_dt,
            config.prev_day_price_interval, config.prev_day_price_interval_fallback,
        )
        if prev_close is None:
            Log.warning(f"[{leg_key}] previous trading day's 15:15 price unavailable -- retrying next cycle.")
            return False

        spot_now = self.get_spot_ltp(inst)
        if spot_now is None:
            Log.warning(f"[{leg_key}] current {inst.name} spot LTP unavailable -- retrying next cycle.")
            return False

        # Verbatim rule (see module docstring): today_0930 > prev_1515 -> PE,
        # else (including an exact tie) -> CE.
        option_type = "PE" if spot_now > prev_close else "CE"
        gap_pct = (spot_now - prev_close) / prev_close * 100.0

        leg.today_gap_computed = True
        leg.today_prev_close = prev_close
        leg.today_entry_spot = spot_now
        leg.today_gap_pct = gap_pct
        leg.today_option_type = option_type
        self._save_state()
        Log.info(f"[{leg_key}] prev_day({prev_day}) 15:15={prev_close:.2f} today_0930={spot_now:.2f} "
                 f"gap={gap_pct:+.3f}% -> SELL {option_type}")
        return True

    def _register_entry_failure(self, leg_key: str, reason: str):
        """Genuine data/connectivity failure during THIS instrument's entry
        sequence (prev-day 15:15 price fetch, expiry resolution, or an
        option-chain fetch erroring/returning something unusable at ANY
        tier) -- NOT a legitimate "no strike matched" tier outcome, which is
        never routed here. Project owner's exact words: "in case [of] no
        data or failure, you should try for 5 times with 1 min interval."
        Never blocks (no time.sleep) -- just records the attempt and a
        future retry timestamp on THIS leg; the OTHER instrument's own retry
        budget is completely untouched. The next due run_cycle tick
        re-tries the WHOLE entry sequence from the top for this instrument
        (see _attempt_entry's retry-gate at the top -- deliberately not
        resuming mid-tier, to avoid duplicating the tier 0/1 logic for two
        entry points). After 5 failed attempts, gives up for the day for
        THIS instrument: latches leg.today_no_trade so this doesn't retry
        forever."""
        leg = self.store.state.legs[leg_key]
        leg.entry_failure_attempt_count += 1
        if leg.entry_failure_attempt_count >= 5:
            leg.today_no_trade = True
            leg.today_no_trade_reason = (
                f"Entry aborted after {leg.entry_failure_attempt_count} failed data-fetch "
                f"attempts (1 min apart): {reason}"
            )
            leg.entry_failure_next_retry_at = ""
            self._save_state()
            Log.error(f"[{leg_key}] All {leg.entry_failure_attempt_count} data-fetch retry attempts "
                      f"failed -- giving up on today's entry. Last failure: {reason}")
            return
        next_retry = datetime.now(IST) + timedelta(seconds=60)
        leg.entry_failure_next_retry_at = next_retry.isoformat()
        self._save_state()
        Log.warning(f"[{leg_key}] data fetch failed (attempt {leg.entry_failure_attempt_count}/5): "
                    f"{reason} -- retrying in 60s.")

    def _attempt_entry_bg(self, leg_key: str, inst: InstrumentConfig):
        """Dispatch wrapper for _attempt_entry -- its gap-decision spot fetch,
        expiry resolution, and up to two option-chain fetches (tier 0 + tier
        1) are the same multi-round-trip shape that caused the documented
        2026-07-24 production stall in the VWAP_NoHA sibling's ATM lock when
        run inline in run_cycle (see that script's _lock_atm_if_needed_bg):
        worst case, one instrument's entry attempt blocks the other
        instrument's SL check for the rest of that cycle. Runs on
        _fill_executor (same pool _enter_leg's place()/fill-watch already
        use, so a leg never has two of its own background tasks racing) --
        only place() itself (inside _enter_leg, called from _attempt_entry
        once a strike is chosen) still legitimately blocks a thread; nothing
        upstream of it blocks run_cycle's own thread anymore. Guarded per leg
        so a slow fetch that outlives one scheduler_interval isn't
        resubmitted on top of itself."""
        if leg_key in self._entry_pending:
            return
        self._entry_pending.add(leg_key)

        def _run():
            try:
                self._attempt_entry(leg_key, inst)
            except Exception as exc:
                Log.exception(f"[{leg_key}] Entry attempt (background) failed: {exc}")
            finally:
                self._entry_pending.discard(leg_key)

        self._fill_executor.submit(_run)

    def _attempt_entry(self, leg_key: str, inst: InstrumentConfig):
        leg = self.store.state.legs[leg_key]
        pos = leg.position

        if leg.trade_count >= 1:
            return

        if pos.symbol:
            # Symbol was already decided (a restart happened mid-flow) --
            # skip straight to placing/watching the order.
            self._enter_leg(leg_key, inst)
            return

        if leg.today_no_trade:
            return

        # Non-blocking retry gate: a data-fetch failure sequence is in
        # progress for THIS instrument (see _register_entry_failure) and its
        # 60s cooldown hasn't elapsed yet -- do nothing this cycle for this
        # instrument. force-exit/error-resolution/pending-action checks in
        # run_cycle, and the OTHER instrument's own attempt, are entirely
        # unaffected by this (they run before/around this method).
        if leg.entry_failure_next_retry_at:
            try:
                next_retry = datetime.fromisoformat(leg.entry_failure_next_retry_at)
            except ValueError:
                next_retry = datetime.now(IST)  # malformed/stale value -- treat as due now, don't get stuck
            if datetime.now(IST) < next_retry:
                return

        if not self._compute_gap_decision(leg_key, inst):
            # None here can mean either "not ready yet this instant" (spot
            # LTP momentarily unavailable) or a genuine fetch failure (the
            # prev-day 15:15 price truly unavailable, both interval attempts
            # exhausted) -- _compute_gap_decision's own warning already
            # distinguishes the two in the log text; either way, without
            # this data entry cannot proceed, so it counts toward the same
            # 5-attempt budget rather than retrying unbounded forever.
            self._register_entry_failure(leg_key, "previous day's 15:15 price or current spot LTP unavailable")
            return

        option_type = leg.today_option_type
        spot = leg.today_entry_spot

        try:
            (cur_compact, cur_raw), (next_compact, next_raw) = resolve_current_and_next_week_expiry(self.client, inst)
        except Exception as exc:
            self._register_entry_failure(leg_key, f"expiry resolution failed: {exc}")
            return

        # Tier 0: current week, premium hard-AND distance-floor (see module
        # docstring's "Strike selection" section), using this instrument's
        # own band.
        try:
            chosen = select_strike(self.client, inst, cur_compact, option_type, spot)
        except Exception as exc:
            self._register_entry_failure(
                leg_key, f"option chain fetch failed for current week expiry {cur_compact}: {exc}"
            )
            return

        if chosen is not None:
            Log.info(f"[{leg_key}] {option_type} strike matched in current week expiry {cur_compact}: "
                     f"strike={chosen['strike']} ltp={chosen['ltp']} distance={chosen['distance_pct']:.2f}%")
        else:
            Log.info(f"[{leg_key}] No {option_type} strike in current week expiry {cur_compact} satisfies "
                     f"premium [{inst.premium_min},{inst.premium_max}] with distance >= "
                     f"{inst.distance_min_pct}% -- falling through to next week ({next_compact}).")

            # Tier 1: next week, THE SAME rule re-applied (project owner:
            # "if not found then move to next weekly option") -- not a
            # different, looser filter.
            try:
                chosen = select_strike(self.client, inst, next_compact, option_type, spot)
            except Exception as exc:
                self._register_entry_failure(
                    leg_key, f"option chain fetch failed for next week expiry {next_compact}: {exc}"
                )
                return

            if chosen is not None:
                Log.info(f"[{leg_key}] {option_type} strike matched in next week expiry {next_compact} "
                         f"(tier 1, same rule): strike={chosen['strike']} "
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
                    leg_key,
                    f"no {option_type} strike with premium in [{inst.premium_min},{inst.premium_max}] "
                    f"found in next week expiry {next_compact} either"
                )
                return

        # A strike was found -- clear any in-progress failure-retry bookkeeping
        # (harmless if it was already empty) so state.json doesn't carry a
        # stale attempt count/timestamp once entry actually succeeds.
        leg.entry_failure_attempt_count = 0
        leg.entry_failure_next_retry_at = ""

        quantity = config.lot_multiplier * chosen["lotsize"]
        # Built as a fresh Position and assigned to leg.position in ONE
        # statement (single STORE_ATTR, atomic under the GIL) rather than
        # mutated field-by-field on the existing `pos` object -- this method
        # now runs on a background thread (see _attempt_entry_bg) concurrently
        # with run_cycle's main thread reading `leg.position.symbol` every
        # tick for the OTHER instrument's turn. Multiple individual
        # attribute writes would let the main thread observe pos.symbol set
        # with pos.entry_px/quantity still at their defaults in the gap
        # between statements -- mirrors the VWAP_NoHA sibling's _enter_leg,
        # which builds its whole LegPosition() before the single
        # `leg.position = pos` assignment for the same reason.
        pos = Position(
            symbol=chosen["symbol"],
            quantity=quantity,
            option_type=option_type,
            entry_time=datetime.now(IST).isoformat(),
            entry_px=float(chosen["ltp"]),
            execution_id=self.execution_id,
        )
        leg.position = pos
        self._save_state()
        self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])
        self._enter_leg(leg_key, inst)

    # ---- entry / exit (single naked leg per instrument, resumable) ---------------
    def _enter_leg(self, leg_key: str, inst: InstrumentConfig):
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag

        if pos.entry_filled or leg_key in self._pending_fills:
            return  # already filled, or a background watcher is already tracking this order

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
        self._fill_executor.submit(self._watch_entry_fill, leg_key, inst, pos.entry_order_id,
                                    pos.symbol, pos.quantity)

    def _watch_entry_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                           symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                                   "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.entry_order_id == order_id:  # guard vs. a superseded/stale order
                # Correct entry_px to the broker's ACTUAL fill price now that
                # the order has confirmed complete -- entry_px was only ever
                # a pre-trade chain LTP snapshot until this point (MARKET
                # orders slip). Falls back to that snapshot if the broker
                # response doesn't include either field, so this can only
                # improve accuracy, never break a working fill confirmation.
                fill_px = fill_data.get("average_price") or fill_data.get("price")
                if fill_px is not None:
                    pos.entry_px = float(fill_px)
                pos.entry_filled = True
                leg.trade_count += 1
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled: {symbol} @ {pos.entry_px}")
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
        # WhatsApp self-alert on every genuine error-state transition (order
        # rejection, fill timeout, or any other place()/poll_fill failure
        # that routes here) -- fires once per transition, not on the
        # periodic _repush_active_errors re-push above. Dispatched via
        # _bg_executor (non-blocking, shared across both instruments) and
        # notify_whatsapp_error() itself never raises -- a WhatsApp/network
        # hiccup can never break this method or the calling run_cycle. The
        # message includes leg_key so a WhatsApp alert always identifies
        # which instrument tripped it.
        try:
            self._bg_executor.submit(
                notify_whatsapp_error, self.env,
                f"[{config.strategy_name}] {leg_key} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state -- re-pushes at most once per config.error_repush_interval_sec
        PER LEG so a single lost POST self-heals within a minute instead of
        leaving the UI blind indefinitely. Called unconditionally at the top
        of run_cycle(), every cycle, for both instruments."""
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
                self._bg_executor.submit(push_leg_error, self.env, leg_key, copy.copy(pos), action=action)
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to dispatch periodic error re-push: {exc}")

    def _push_leg_error_bg(self, leg_key: str, pos: "Position", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _bg_executor -- for the
        "clear"/on-resolution pushes from _resolve_leg_error, which run
        synchronously on run_cycle's own thread. `pos` is snapshotted with a
        shallow copy on THIS (calling) thread first -- executor.submit()
        evaluates its arguments immediately, only the function call itself
        is deferred, and `pos` is a live, mutable object this same cycle may
        reset moments later."""
        snapshot = copy.copy(pos)
        try:
            self._bg_executor.submit(push_leg_error, self.env, leg_key, snapshot, action=action, clear=clear)
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

        self._bg_executor.submit(_run)

    def _pop_pending_action(self, leg_key: str) -> Optional[dict]:
        return self._pending_action_cache.pop(leg_key, None)

    def _exit_leg(self, leg_key: str, inst: InstrumentConfig, reason: str):
        if leg_key in self._entry_pending:
            # _attempt_entry_bg publishes leg.position (with pos.symbol set)
            # BEFORE _enter_leg's place() call returns and adds leg_key to
            # _pending_fills -- so between those two points pos.symbol is
            # truthy but there is no resting/confirmed entry order yet, and
            # _entry_pending is the ONLY signal that window is still open.
            # Without this guard, run_cycle's SL check (or Force Exit, or
            # the past-universal-exit branch) running on the main thread
            # could call this and place a BUY exit for an entry that hasn't
            # even been submitted, or race _enter_leg's own place() call
            # writing pos.entry_order_id concurrently. Deferring here is
            # always safe: the entry-attempt thread will itself either reach
            # _pending_fills (letting a later cycle's _exit_leg proceed
            # normally) or _enter_error_mode (which every exit-triggering
            # branch already checks before reaching this call).
            return
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag
        if not pos.exit_reason:
            pos.exit_reason = reason

        if pos.exit_filled:
            self._finalize_exit(leg_key, inst, pos.exit_reason)
            return

        if leg_key in self._pending_fills:
            return  # exit order already in flight -- background watcher will resolve it

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
        self._fill_executor.submit(self._watch_exit_fill, leg_key, inst, pos.exit_order_id,
                                    pos.symbol, pos.quantity)

    def _watch_exit_fill(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                          symbol: str, quantity: int):
        strategy_tag = self.env.strategy_tag
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, symbol, inst.options_exchange,
                                   "BUY", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
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
            self._enter_error_mode(leg_key, "exit_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(leg_key, "exit_failed", "terminal", "", str(exc))
        except Exception as exc:
            Log.exception(f"[{leg_key}] Unexpected error while watching exit fill: {exc}")
            self._enter_error_mode(leg_key, "exit_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _close_position(self, leg_key: str, inst: InstrumentConfig, exit_px: float, reason: str):
        """Shared by _finalize_exit (normal path, LTP-derived exit price) and
        reconcile_pending_orders' exit-completed branch (broker-confirmed
        fill price) -- the trade-log write / today_realized_pnl update /
        WS-unsubscribe / position reset only need to happen once, however
        the exit price was determined."""
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        strategy_tag = self.env.strategy_tag
        self.store.state.today_realized_pnl += (pos.entry_px - exit_px) * pos.quantity  # short: entry high, exit low = profit
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
        self.price_stream.remove_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])
        leg.position = Position()
        self._save_state()

    def _finalize_exit(self, leg_key: str, inst: InstrumentConfig, reason: str):
        """Runs on the main thread's next run_cycle tick once _watch_exit_fill
        has set pos.exit_filled."""
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        Log.info(f"[{leg_key}] Position closed: {pos.symbol}")

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
            exit_px = self.price_stream.get_ltp(pos.symbol, inst.options_exchange, max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange, require_two_sided=True)
        if exit_px is None:
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self._close_position(leg_key, inst, exit_px, reason)

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    def _resolve_leg_error(self, leg_key: str, inst: InstrumentConfig, action: dict):
        if leg_key in self._pending_fills:
            # A Retry/Cancel resolution (or a resumed fill watcher) is
            # already in flight for this leg -- leave the new action
            # un-acked, picked up on a later cycle once it clears.
            return
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        was_exit = pos.error_state == "exit_failed"
        kind = pos.error_kind

        if action["action"] == "retry":
            self._pending_fills.add(leg_key)
            ack_pending_action(self.env, leg_key)
            self._fill_executor.submit(self._do_retry_resolution, leg_key, inst, was_exit, kind)
            return

        if action["action"] == "cancel":
            if was_exit:
                pos.exit_order_id = ""
                pos.exit_reason = ""
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
                leg.position = Position()
                self._save_state()
                self._push_leg_error_bg(leg_key, leg.position, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            # kind == "resting": one last honest re-price + bounded wait,
            # then an explicit cancelorder() if it still didn't fill.
            ack_pending_action(self.env, leg_key)
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(
                self._watch_entry_cancel, leg_key, inst, pos.error_order_id, pos.symbol, pos.quantity
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
                self._push_leg_error_bg(leg_key, pos, clear=True)
                ack_pending_action(self.env, leg_key)
                # "Manually Completed" finalizes immediately -- do not wait
                # for the next run_cycle pass to notice exit_filled.
                self._finalize_exit(leg_key, inst, pos.exit_reason)
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
                else:  # kind == "terminal"
                    pos.exit_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self._save_state()
                push_leg_error(self.env, leg_key, pos, clear=True)
                self._pending_fills.discard(leg_key)
                return

            # Entry side: run_cycle only ever calls _attempt_entry when
            # pos.symbol is empty -- an entry attempt already in error mode
            # has pos.symbol set, so Retry must resubmit the watcher itself.
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
            else:  # kind == "terminal" -- nothing resting, place a genuinely new order
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
            # _watch_entry_fill owns _pending_fills for this leg_key from here.
            self._fill_executor.submit(
                self._watch_entry_fill, leg_key, inst, resume_order_id, pos.symbol, pos.quantity
            )
        except Exception as exc:
            Log.exception(f"[{leg_key}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(leg_key)

    def _watch_entry_cancel(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                             symbol: str, quantity: int):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned."""
        strategy_tag = self.env.strategy_tag
        try:
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             symbol, inst.options_exchange, "SELL", quantity)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.error_order_id != order_id:
                return  # superseded by a newer action/order in the meantime -- do nothing
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
                leg.position = Position()
                self._save_state()
            push_leg_error(self.env, leg_key, leg.position, clear=True)
        except Exception as exc:
            Log.exception(f"[{leg_key}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    # ---- startup reconciliation ---------------------------------------------
    def reconcile_pending_orders(self):
        """Startup-only crash recovery: finds an order that was placed but
        never confirmed filled before the previous process instance stopped
        (killed, redeployed, crashed) -- for EACH instrument's leg
        independently. Called once from main(), before the scheduler
        starts. Mirrors Nifty_OI_WeeklyBuy_MonthlySell's
        reconcile_pending_orders -- the most recently hardened version of
        this pattern in the project -- adapted to this script's per-leg
        Position dict."""
        for leg_key in LEG_KEYS:
            inst = _inst_for_leg_key(leg_key)
            leg = self.store.state.legs[leg_key]
            pos = leg.position
            if pos.entry_order_id and not pos.entry_filled and not pos.error_state:
                self._reconcile_one(leg_key, inst, pos, pos.entry_order_id, "entry")
            elif pos.exit_order_id and not pos.exit_filled and not pos.error_state:
                self._reconcile_one(leg_key, inst, pos, pos.exit_order_id, "exit")
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
                push_leg_error(self.env, leg_key, pos, action="SELL")
                self._save_state()
                Log.error(f"[{leg_key}] reconcile: ambiguous pre-placeorder crash window -- "
                          f"flagged for manual verification against the broker.")

    def _reconcile_one(self, leg_key: str, inst: InstrumentConfig, pos: "Position", order_id: str, phase: str):
        action = "SELL" if phase == "entry" else "BUY"
        leg = self.store.state.legs[leg_key]
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
            self._save_state()
            return

        data = resp.get("data", {})
        status = str(data.get("order_status", "")).lower()
        Log.info(f"[{leg_key}] reconcile: {phase} order {order_id} status='{status}' (resuming after restart)")

        if status == "complete":
            fill_px = float(data.get("average_price") or data.get("price") or pos.entry_px or 0.0)
            if phase == "entry":
                pos.entry_px = fill_px
                pos.entry_filled = True
                leg.trade_count += 1
                self.price_stream.add_instruments([{"symbol": pos.symbol, "exchange": inst.options_exchange}])
                self._save_state()
                Log.info(f"[{leg_key}] reconcile: entry {order_id} was actually filled @ {fill_px} -- "
                         f"resuming as an open position.")
            else:
                Log.info(f"[{leg_key}] reconcile: exit {order_id} was actually filled @ {fill_px} -- "
                         f"closing the position.")
                self._close_position(leg_key, inst, fill_px, pos.exit_reason or "reconciled_after_restart")
            return

        if status in {"rejected", "cancelled", "canceled"}:
            if phase == "entry":
                leg.position = Position()
                Log.info(f"[{leg_key}] reconcile: entry {order_id} was genuinely rejected/cancelled -- "
                         f"clearing the position, safe to re-evaluate fresh.")
            else:
                pos.exit_order_id = ""
                pos.exit_filled = False
                Log.info(f"[{leg_key}] reconcile: exit {order_id} was genuinely rejected/cancelled -- "
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
        push_leg_error(self.env, leg_key, pos, action=action)
        Log.error(f"[{leg_key}] reconcile: {phase} order {order_id} still '{status}' after restart -- "
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
        """Force-closes every leg (both instruments) currently holding a
        position, regardless of the strategy's own SL/universal-exit logic.
        Returns True once ALL legs are flat. A leg already in error mode is
        left untouched -- Force Exit doesn't override an unresolved
        Retry/Cancel/Manual decision for that leg."""
        all_flat = True
        for leg_key in LEG_KEYS:
            inst = _inst_for_leg_key(leg_key)
            pos = self.store.state.legs[leg_key].position
            if pos.error_state:
                Log.warning(f"[{leg_key}] Force Exit waiting on an unresolved error "
                            f"({pos.error_state}/{pos.error_kind}) -- resolve it via "
                            f"Retry/Cancel/Manually Completed first.")
                all_flat = False
                continue
            if pos.symbol:
                all_flat = False
                self._exit_leg(leg_key, inst, reason="force_exit")
        return all_flat

    # ---- PnL reporting ---------------------------------------------------------
    def _open_positions_for_pnl(self) -> list:
        """Sums BOTH instruments' open legs -- report_pnl_tick pushes the
        combined list, and today_realized_pnl (a single shared running total
        on StrategyState) already accumulates both instruments' closed-leg
        PnL as they close (see _close_position)."""
        result = []
        for leg_key in LEG_KEYS:
            inst = _inst_for_leg_key(leg_key)
            pos = self.store.state.legs[leg_key].position
            if not pos.symbol or not pos.entry_filled:
                continue
            ltp = self.price_stream.get_ltp(pos.symbol, inst.options_exchange, config.ws_stale_seconds)
            if ltp is None:
                ltp = pos.entry_px  # last-known fallback -- never fabricate movement
            pnl = (pos.entry_px - ltp) * pos.quantity  # short leg
            result.append({
                "leg_key": leg_key, "symbol": pos.symbol, "direction": "SHORT",
                "quantity": -pos.quantity, "entry_price": pos.entry_px,
                "current_price": ltp, "pnl": pnl,
                "entry_time": pos.entry_time, "execution_id": pos.execution_id,
            })
        return result

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
                for leg_key in LEG_KEYS:
                    inst = _inst_for_leg_key(leg_key)
                    pos = self.store.state.legs[leg_key].position
                    if pos.error_state:
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

            past_universal_exit = self._past_universal_exit()

            if past_universal_exit:
                for leg_key in LEG_KEYS:
                    inst = _inst_for_leg_key(leg_key)
                    pos = self.store.state.legs[leg_key].position
                    if pos.error_state:
                        # Frozen awaiting a Retry/Cancel/Manual decision -- still
                        # check for a pending action every cycle even after
                        # hours, same reasoning as every sibling script: this
                        # must never become unreachable for the rest of the day.
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                        else:
                            Log.error(f"[{leg_key}] Universal exit time reached but still in error mode "
                                      f"({pos.error_state}) -- resolve it via Retry/Cancel/Manually "
                                      f"Completed NOW.")
                        continue
                    if pos.symbol:
                        Log.warning(f"[{leg_key}] Universal exit time reached; force-closing.")
                        self._exit_leg(leg_key, inst, reason="universal_exit")
                return

            within_entry = self._within_entry_window()

            # Evaluated sequentially, NIFTY then SENSEX, every cycle -- each
            # instrument's own block below is either a cheap in-memory state
            # check or dispatches to a background executor (fill watch,
            # push_leg_error, pending-action fetch, entry attempt). The
            # entry sequence's multi-round-trip data fetch (gap decision,
            # expiry resolution, up to two chain fetches) runs via
            # _attempt_entry_bg on _fill_executor, not inline here -- so it
            # can never delay the other instrument's SL check within the
            # same cycle (see _attempt_entry_bg's own docstring for the
            # 2026-07-24 VWAP_NoHA incident this mirrors). The only call left
            # genuinely inline on this thread per instrument is the cheap
            # single-symbol LTP fallback fetch a few lines below when the WS
            # cache is stale -- a single REST round trip, not a chain.
            for inst in INSTRUMENTS:
                leg_key = _leg_key_for(inst)
                leg = self.store.state.legs[leg_key]
                pos = leg.position

                if pos.error_state:
                    self._refresh_pending_action_bg(leg_key)
                    pending = self._pop_pending_action(leg_key)
                    if pending is not None:
                        self._resolve_leg_error(leg_key, inst, pending)
                    continue

                if pos.symbol:
                    exit_already_committed = bool(pos.exit_order_id) or pos.exit_filled
                    if exit_already_committed:
                        self._exit_leg(leg_key, inst, reason=pos.exit_reason or "unknown")
                        continue

                    # Only real exit condition besides universal-exit-time: the
                    # stop-loss. No opposite-signal/technical exit for this
                    # strategy (see module docstring).
                    option_ltp = self.price_stream.get_ltp(pos.symbol, inst.options_exchange,
                                                             max_age=config.ws_stale_seconds)
                    if option_ltp is None:
                        option_ltp = fetch_symbol_ltp(self.ltp_client, pos.symbol, inst.options_exchange,
                                                       require_two_sided=True)
                    if option_ltp is not None:
                        sl_trigger_px = pos.entry_px * (1 + config.sl_pct)
                        if option_ltp >= sl_trigger_px:
                            Log.info(f"[{leg_key}] Stop-loss hit: ltp={option_ltp} >= trigger={sl_trigger_px:.2f} "
                                     f"(entry={pos.entry_px}, sl_pct={config.sl_pct}).")
                            self._exit_leg(leg_key, inst, reason="stop_loss")
                    continue

                # No position yet today for this instrument.
                if leg.trade_count >= 1:
                    continue
                # entry_window_end (09:35) gates the FIRST entry attempt only,
                # per instrument. Once a data-fetch failure retry sequence has
                # actually started for THIS instrument
                # (entry_failure_attempt_count > 0) and hasn't been given up
                # on yet (today_no_trade still False -- see
                # _register_entry_failure), keep retrying past the window
                # until it succeeds or exhausts all 5 attempts: the project
                # owner's "try 5 times, 1 minute apart" instruction takes
                # priority for this failure path specifically. This
                # deliberately does NOT widen the window for the normal,
                # no-failure case -- a clean run still resolves well within
                # 09:30-09:35 exactly as before, independently per instrument.
                retry_in_progress = (
                    leg.entry_failure_attempt_count > 0 and not leg.today_no_trade
                )
                if not within_entry and not retry_in_progress:
                    continue
                self._attempt_entry_bg(leg_key, inst)
        except Exception as exc:
            Log.exception(f"Cycle failed: {exc}")
            # WhatsApp self-alert for a genuinely unexpected crash not
            # already routed through _enter_error_mode's own alert.
            # Throttled (cycle_failure_notify_interval_sec) since an outer
            # catch-all could otherwise fire every scheduler tick if the
            # same bug keeps recurring -- notify_whatsapp_error() itself
            # never raises and this is dispatched via _bg_executor
            # (non-blocking), so a WhatsApp/network hiccup here can never
            # compound the original failure. Single shared throttle (not
            # per-instrument) -- see __init__'s comment on
            # _last_cycle_failure_notify for why.
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
    print(f"Instruments          : {', '.join(i.name for i in INSTRUMENTS)}")
    for inst in INSTRUMENTS:
        print(f"  {inst.name:<8} premium [{inst.premium_min}, {inst.premium_max}]  "
              f"distance >= {inst.distance_min_pct}% OTM (no upper cap)")
    print(f"Entry window         : {config.entry_window_start} - {config.entry_window_end}")
    print(f"Universal exit       : >= {config.universal_exit_time}")
    print(f"Stop-loss            : {config.sl_pct * 100:.0f}% of entry premium")
    print(f"Max entries/instrument/day : 1")
    print("WARNING: NAKED OPTION SELLING -- NO HEDGE LEG -- UNDEFINED RISK")
    print("WARNING: PRODUCT IS NRML -- NO BROKER-SIDE FORCED SQUARE-OFF BACKSTOP.")
    print("         universal_exit_time's own exit order succeeding is the ONLY")
    print("         thing that closes each instrument's position before market close.")
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

    already_known = [
        {"symbol": inst.name, "exchange": inst.underlying_exchange} for inst in INSTRUMENTS
    ]
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        for leg_key in LEG_KEYS:
            inst = _inst_for_leg_key(leg_key)
            pos = state_store.state.legs[leg_key].position
            if pos.symbol:
                already_known.append({"symbol": pos.symbol, "exchange": inst.options_exchange})
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
    # (see reconcile_pending_orders' docstring), per leg; an already-fully-
    # filled leg needs no reconciliation, it just resumes normally once the
    # scheduler starts.
    engine.reconcile_pending_orders()

    for leg_key in LEG_KEYS:
        pos = state_store.state.legs[leg_key].position
        if pos.error_state:
            action = "SELL" if pos.error_state == "entry_failed" else "BUY"
            push_leg_error(env, leg_key, pos, action=action)
            Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                      f"({pos.error_state}/{pos.error_kind}) -- needs Retry/Cancel/Manually Completed.")
        elif pos.symbol:
            Log.info(f"[{leg_key}] Resuming an already-open position from before restart: "
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
