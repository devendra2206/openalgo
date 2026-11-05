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
  2. That exact strike's own OI signal classifies config.short_verdict --
     "weakening", i.e. short build-up or long unwinding, meaning the
     premium has FALLEN since the reference (author 2026-11-01). The
     original rule here was "accumulation"; see config.short_verdict for
     the measured reason it was reversed. Computed by
     classify_oi_premium(premium_change, oi_change) against the day's
     Reference Snapshot (same
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

Cycle-end square-off / monthly rollover
------------------------------------------------------------------------
resolve_monthly_expiry() (day > 15 -> next month's contract) feeds ONLY
new-entry strike search -- it never force-closes an already-open position.
Separately, if an open position's OWN contract expires today, it is
force-closed (short then hedge) unconditionally at the 3:15 daily
decision -- terminal for the day, no new-position search follows.

Position checks at 11:30, 1:30 and 3:30 PM (every day that is NOT the
9:30 entry day) -- three slots since 2026-11-01, shifted fifteen minutes
later than the original single 3:15 PM decision for the closing call
auction. Each slot runs all three branches below.
------------------------------------------------------------------------
  - Resolve the active expiry (informational for whatever fires below).
  - Position open and its contract expires today -> force-close, stop.
  - Position open (not expiring today) -> fetch a FRESH OI verdict right
    now (never reuse anything cached earlier): still config.hold_verdict
    ("weakening") -> hold; no longer -> close short, then hedge, then
    search for a fresh qualifying strike and re-enter if one qualifies.
  - No position open (closed earlier today by a WS-driven SL hit) ->
    search for a new qualifying strike, re-enter if one qualifies.

Order sequence
------------------------------------------------------------------------
ENTRY  hedge BUY -> (on fill) short SELL
EXIT   short BUY -> (on fill) hedge SELL
Buying the long leg first reduces the margin the short blocks, and means a
failure part-way through leaves a long option rather than a naked short.
The close is the reverse for the same reason: selling the hedge first
would leave the short unhedged for those seconds.

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
import re
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
    reference_wait_time: time = time(9, 45)   # large-gap case waits for this candle to close
    # 09:45, not 09:35, since the closing call auction moved every decision
    # fifteen minutes later (author 2026-11-01: "take OI data at 9:30 and if
    # gap or gap down then take OI data at 9:45"). The reference and the
    # first 'current' read must still land on DIFFERENT candles; at the
    # 5-minute bootstrap interval 09:30 and 09:45 are three buckets apart, so
    # the collision this file documents below -- both resolving to the same
    # 09:15-09:30 15-minute bucket, every verdict forced to "flat", the day's
    # entry permanently lost -- cannot occur.

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
    # Everything else (the 11:30/13:30/15:30 position checks, later days)
    # stays on the
    # regular 15m interval -- already far enough from 09:30 to never collide.
    reference_gap_bootstrap_interval: str = "5m"

    # Strike qualification -- delta-closeness only, NO premium filter anywhere.
    monthly_delta_target: float = 0.27
    monthly_delta_low: float = 0.22     # widen-scan-stop heuristic only, never a hard filter
    monthly_delta_high: float = 0.32
    monthly_strike_round: int = 100
    # Which OI verdict qualifies a strike to be SHORTED, and which keeps an
    # open short alive at a position check (author 2026-11-01: "it should be
    # OI verdict should be weakening , then we will short option" and "you
    # will hold position till it in weakninig state").
    #
    # This reverses the original rule, and the reason is the payoff shape it
    # produced. "accumulation" requires premium_change > 0, so the old entry
    # sold a premium that had RISEN since the reference and then held while it
    # kept rising -- the hold condition coincided with the position LOSING.
    # Measured over 679 trades, the daily decision closed winners (429 exits,
    # mean +31.9 short points, 93% in profit) and left every loser to the stop
    # (245 exits, mean -57.6 points, 0% in profit): it cut winners at ~35
    # points and held losers to a ~53-point stop, for -Rs 98 a trade.
    # Shorting on "weakening" instead sells a falling premium and holds while
    # it keeps falling, so holding now coincides with being in profit:
    # wins averaged +54.8 short points against losses of -35.3.
    short_verdict: str = "weakening"
    hold_verdict: str = "weakening"
    monthly_expiry_roll_day_of_month: int = 15   # today.day > 15 resolves to next month's contract

    # Tiered, permanently-ratcheting stop-loss (checked continuously via WS LTP).
    sl_tier1_pct: float = 38.2           # exit once current_premium >= entry_premium * 1.382
    sl_tier2_trigger_pct: float = 50.0   # permanent ratchet trigger (premium <= 50% of entry, ever)

    # Hedge -- premium-target selected, same side as the short leg, further OTM.
    hedge_premium_pct_of_short: float = 20.0
    hedge_strike_scan_count: int = 30    # first width fetched when the short scan's chain is too narrow
    hedge_strike_scan_max: int = 100     # widen 30 -> 60 -> 100 (the API's own cap) until the target is reached

    # Position-check times (author 2026-11-01). Was ONE decision at 15:15;
    # now three, each shifted fifteen minutes later for the closing call
    # auction: "treade check at 11:30 , 1:30 and 3:30". Every slot runs the
    # same three branches (expiry force-close / fresh verdict / flat search),
    # so a signal flip is acted on within ~2 hours instead of waiting for the
    # close -- measured, that cut the tier-1 stop rate from 32% of trades to
    # 14% and the stop bill from -Rs 482,754 to -Rs 363,840.
    #
    # The 15:30 slot now sits AT the close rather than fifteen minutes before
    # it, so its fill is the closing price. The last complete 15-minute candle
    # at 15:30 is the 15:15-15:29 bucket.
    daily_decision_times: tuple = (time(11, 30), time(13, 30), time(15, 30))
    # The last slot, kept under the old single-decision name so any reader of
    # it resolves to a sane value instead of raising.
    daily_decision_time: time = time(15, 30)
    # Wait this long after a scheduled time before acting on it (author
    # 2026-11-01: "since api may not give candle close at 11:30 / 1:30 or 3:30
    # extraclty , pls add one minth buffer"). The broker publishes a candle a
    # moment after its boundary, so asking at exactly 11:30:00 can return the
    # 11:15 candle instead -- a whole slot stale.
    #
    # The buffer shifts only WHEN a check fires, never WHICH candle it wants:
    # the freshness gate still measures against the slot time itself, so an
    # 11:30 check firing at 11:31 still demands the candle that closed at
    # 11:30 and retries if the broker has not published it yet. Slot keys also
    # stay on the slot time, so a restart cannot replay a slot.
    decision_buffer_min: int = 1

    # Take the verdict's CURRENT premium/OI from the live quote instead of the
    # last closed candle (author 2026-11-02). The strike scan already holds it
    # in the optionchain response it fetched, and the hold check gets it in one
    # quotes() call rather than five days of 15-minute candles.
    #
    # It is a rule change as well as a speed-up: "current" becomes a live
    # reading at ~11:31 rather than the candle that closed at 11:30. The
    # reference side is untouched. It also makes the candle-freshness retry
    # redundant for the current side, which removes up to 75s of _eval_pending
    # being held per check -- the window the stop loss was blind in.
    #
    # A 0 or missing oi falls back to the candle path, so a broker that does
    # not publish OI in quotes still works rather than silently classifying
    # every candidate "flat".
    use_live_oi_for_current: bool = True

    # CYCLE-END SQUARE-OFF (author 2026-11-01: "on expiry at 3:30 PM , you
    # cloase all position" and "you will close all postion at current calendar
    # month expiry not trade position expiry"). On the CALENDAR month's expiry
    # date every open position is closed at this time, whatever contract it
    # holds -- past day 15 a new entry sits in next month's contract, and it is
    # still squared off on this month's expiry.
    #
    # Earlier checks that day are ORDINARY: the verdict branch runs on an open
    # position, and a flat leg may take a fresh entry -- past day 15 that lands
    # in next month's contract, and it is squared off here the same day along
    # with everything else. Entries stop only AT/AFTER this time.
    #
    # It must be one of daily_decision_times, because the check that performs
    # it is what runs -- asserted at startup rather than trusted.
    expiry_close_time: time = time(15, 30)

    # The continuous tiered SL acts only inside this window (author: "at 9:30
    # SL check and reference data check", "SL check timing ends at 3:30").
    # Outside it an LTP tick is ignored. Before 09:30 that is also necessary:
    # the day's Reference Snapshot is not yet fixed.
    #
    # The session runs to 15:40, so the last ten minutes are deliberately
    # unwatched -- a breach there is first seen at the next day's 09:30 scan,
    # at the next morning's price. That window is also when the 15:30 check
    # places its orders, so anything it opens is unwatched until tomorrow.
    # Stated because it is a consequence of the given times, not an oversight.
    sl_check_start_time: time = time(9, 30)
    sl_check_end_time: time = time(15, 30)
    entry_evaluation_time: time = time(9, 30)     # fires only on a cycle's own start day

    quantity: int = 65                  # NIFTY's current lot size == 1 lot
    capital_tag: float = 250_000.0      # reporting/return-on-capital display only

    # NRML mandatory -- positional, multi-day hold. No broker-side forced
    # square-off backstop; the strategy's own SL/daily-decision/expiry-day
    # closes are the ONLY things that ever close a position.
    product: str = "NRML"
    price_type: str = "MARKET"

    market_open: time = time(9, 15)
    # 15:40, not 15:30 (author 2026-11-01: "market session end at 3:40 PM").
    # This is what makes the 15:30 position check able to TRADE: dispatching
    # at 15:30 with a 15:30 close meant any order it placed landed after the
    # regular session and would have been rejected. With the close at 15:40
    # the slot has ten minutes of live session to work in.
    market_close: time = time(15, 40)
    # No grace past the session end -- the given times are the times. It
    # stays configurable only because the LAST position check must remain
    # reachable: _within_market_hours() bounds on max(market_close, last
    # slot), and with the close at 15:40 and the last slot at 15:30 there are
    # ten live minutes to act in, so 0 is correct here. Were a slot ever moved
    # onto the close itself, a `time` comparison would hold for the single
    # instant 15:40:00.000000 and that check would never fire -- which is the
    # defect this field was added for.
    post_close_grace_min: int = 0
    # Kept for parity with the sibling script's Config shape -- this
    # strategy's own exits (SL / daily-decision / expiry-day close) all
    # fire well before this; there is no separate "universal exit time"
    # sweep in this script (see module docstring's decision tree).
    monthly_universal_exit_time: time = time(15, 20)

    scheduler_interval: int = 10
    pnl_tick_interval: float = 0.8

    # Bounded retry for the 9:30 and position-check OI candle reads: the broker can lag a
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
    entry_trigger_verdict: str = ""     # config.short_verdict at entry, always
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
    # The position check that ordered this close ("HH:MM"), carried so the
    # re-entry search dispatched after the hedge exit confirms is anchored to
    # THAT slot. This strategy is time-anchored: the candle-freshness gate
    # measures against the slot that fired, and defaulting to the last slot of
    # the day would hand an 11:30 re-entry a 15:30 boundary -- a time in the
    # future, which no candle can be fresher than, so the search would burn
    # its retries and the re-entry would be lost.
    reentry_slot_hm: str = ""

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
    # "<ISO date> <HH:MM>" slot key -- guards each position-check dispatch to at
    # most once per calendar day, same resolved-regardless-of-outcome rule.
    last_daily_decision_date: str = ""
    # The CYCLE's expiry, as an ISO date ("2026-06-30"), captured ONCE on the
    # cycle's first fresh trade and NOT rewritten for the rest of the cycle.
    # THIS IS THE SQUARE-OFF KEY: at config.expiry_close_time on this date,
    # every open position is closed, whatever contract it holds.
    #
    # It is NOT the contract traded -- after the 15th a new entry goes into
    # next month's contract (pos.expiry_date), while this stays on the cycle's
    # own expiry. A trade taken 20-Jun holds the July contract and is still
    # closed on 30-Jun.
    #
    # "Already captured for this cycle" is read from this value itself: a date
    # still in the future belongs to the running cycle; empty, unreadable or
    # past means the next fresh trade captures again. It survives the position
    # object being reset, so the square-off still fires after a leg goes flat
    # mid-cycle.
    marked_expiry: str = ""
    # ISO date on which this leg's own contract was force-closed at expiry.
    # The force-close is "terminal for today, no new-position search
    # follows" -- with ONE check a day that was implicit in returning from
    # it, but with three checks the leg is simply flat when 13:30 arrives
    # and the flat branch would open a fresh short+hedge in NEXT month's
    # contract on expiry day: a day before get_cycle_start_date allows
    # the cycle to start, which then suppresses the real cycle-start day's
    # own entry evaluation. This field makes the terminality explicit.
    expiry_closed_date: str = ""


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
    # The cycle's expiry as last observed (ISO date). Always at or after the
    # day it was observed, because the broker's expiry list only carries
    # future dates. When it is seen to be in the PAST, the cycle that ended on
    # it is over and cycle_start_date is derived from it.
    cycle_expiry_seen: str = ""
    # First trading day of the current cycle (ISO date), derived above. Kept
    # so a restart on that day still recognises it as the cycle-start day.
    cycle_start_date: str = ""


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
        # Option-chain calls only: the short scan and the hedge's wider chain.
        # Shoonya quotes a chain in batches of 20 symbols with a 1s pause
        # between them. Measured live 05-Nov-2026: strike_count 16 6.3s,
        # 30 10.0s (timed out on the 10s above), 60 19.4s, 100 35.9s.
        # Orders, quotes and history keep their own shorter timeouts.
        self.hedge_chain_timeout = 60.0
        self.ws_url = os.getenv("WEBSOCKET_URL")
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "nifty_oi_positional_monthly_sell"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _with_buffer(t: time) -> time:
    """A scheduled time plus config.decision_buffer_min, for comparing against
    the clock. Built via datetime.combine because `time` has no arithmetic."""
    if not config.decision_buffer_min:
        return t
    base = datetime.combine(datetime.now(IST).date(), t)
    return (base + timedelta(minutes=config.decision_buffer_min)).time()


def _within_market_hours() -> bool:
    """True while run_cycle should keep ticking.

    The upper bound is the LATER of market_close and the last position
    check, plus post_close_grace_min. Bounding it at market_close alone
    made the 15:30 check unreachable, since a `time` comparison only holds
    for the instant 15:30:00.000000 -- the slot existed and never ran.
    """
    if config.test_mode:
        return True
    now = datetime.now(IST)
    last_slot = max(config.daily_decision_times)
    # Naive on purpose -- only .time() is read back, and a pytz tzinfo= here
    # would resolve to LMT (+05:53) as it does everywhere else in this file.
    end = datetime.combine(now.date(), max(config.market_close, last_slot))         + timedelta(minutes=config.post_close_grace_min)
    return config.market_open <= now.time() <= end.time()


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

    def connect_hedge_chain_client(self):
        """A client used ONLY for option-chain calls (the short scan and the
        hedge's wider chain), with Environment.hedge_chain_timeout -- see that
        setting. Kept separate so no order or quote call ever inherits a 60s
        wait."""
        return api(
            api_key=self.env.api_key,
            host=self.env.host,
            version=self.env.version,
            timeout=self.env.hedge_chain_timeout,
        )

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
        self.state.cycle_expiry_seen = data.get("cycle_expiry_seen", "")
        self.state.cycle_start_date = data.get("cycle_start_date", "")
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
            leg.expiry_closed_date = leg_raw.get("expiry_closed_date", "")
            leg.marked_expiry = leg_raw.get("marked_expiry", "")
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
            "cycle_expiry_seen": self.state.cycle_expiry_seen,
            "cycle_start_date": self.state.cycle_start_date,
            "reference": asdict(self.state.reference),
            "legs": {
                k: {
                    "trade_count": v.trade_count,
                    "last_entry_eval_date": v.last_entry_eval_date,
                    "last_daily_decision_date": v.last_daily_decision_date,
                    "expiry_closed_date": v.expiry_closed_date,
                    "marked_expiry": v.marked_expiry,
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


# NIFTY monthly expiry dates for the day, from the FUTURES list. One shared,
# per-day cache so the contract a new entry trades and the cycle date it
# captures can never come from two different calls -- see
# fetch_monthly_expiries. Keyed by the date, so it invalidates itself daily.
_MONTHLY_EXPIRY_CACHE: dict = {"day": None, "dates": None}
_MONTHLY_EXPIRY_LOCK = threading.Lock()


def clear_monthly_expiry_cache():
    with _MONTHLY_EXPIRY_LOCK:
        _MONTHLY_EXPIRY_CACHE["day"] = None
        _MONTHLY_EXPIRY_CACHE["dates"] = None


def fetch_monthly_expiries(client) -> list:
    """NIFTY monthly expiry dates at or after today, oldest first.

    Read from the FUTURES expiry list, not the options one. The options list
    mixes weeklies and monthlies with no flag (docs/api/symbol-services/
    expiry.md), so its monthly had to be guessed as "the last date listed in
    each month" -- and with the current month's monthly missing, that guess
    picks a mid-month weekly. NIFTY futures are listed only as monthly
    contracts and expire on the same day as that month's monthly options, so
    each date here IS a monthly, already holiday-adjusted by the exchange.

    Cached once per calendar day and shared by every caller. Raises
    RuntimeError -- never guesses -- when the call fails, the list is empty,
    or it holds two dates in one month (it would then not be the monthly-only
    list this relies on). Every caller already turns that into "no entry this
    time"."""
    today = datetime.now(IST).date()
    with _MONTHLY_EXPIRY_LOCK:
        if _MONTHLY_EXPIRY_CACHE["day"] == today and _MONTHLY_EXPIRY_CACHE["dates"]:
            return list(_MONTHLY_EXPIRY_CACHE["dates"])
    try:
        resp = client.expiry(symbol=UNDERLYING_SYMBOL, exchange=OPTIONS_EXCHANGE,
                             instrumenttype="futures")
    except Exception as exc:
        raise RuntimeError(f"NIFTY futures expiry call failed: {exc}") from exc
    if not isinstance(resp, dict) or resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve NIFTY futures expiries: {resp}")
    dates = sorted({datetime.strptime(raw, "%d-%b-%y").date() for raw in resp["data"]})
    dates = [d for d in dates if d >= today]
    if not dates:
        raise RuntimeError("No upcoming NIFTY futures expiry in the list.")
    seen_months: dict = {}
    for d in dates:
        key = (d.year, d.month)
        if key in seen_months:
            raise RuntimeError(
                f"NIFTY futures expiry list has two dates in {d.year}-{d.month:02d} "
                f"({seen_months[key]} and {d}) -- not the monthly-only list this "
                f"strategy relies on. Refusing to guess which is the monthly."
            )
        seen_months[key] = d
    with _MONTHLY_EXPIRY_LOCK:
        _MONTHLY_EXPIRY_CACHE["day"] = today
        _MONTHLY_EXPIRY_CACHE["dates"] = list(dates)
    return list(dates)


def resolve_monthly_expiry(client) -> tuple[str, str]:
    """The contract a NEW entry trades. Calendar day-of-month >
    monthly_expiry_roll_day_of_month (15) -> next month's monthly; on/before
    day 15 -> the current month's own monthly. Unchanged rule; only the source
    of the monthly date changed (fetch_monthly_expiries, the futures list).
    Used ONLY to decide which expiry a NEW entry searches against -- it never
    closes an already-open position (see module docstring's rollover
    section)."""
    today = datetime.now(IST).date()
    by_month = {(d.year, d.month): d for d in fetch_monthly_expiries(client)}
    current_month_key = (today.year, today.month)
    current_month_end = by_month.get(current_month_key)

    if today.day <= config.monthly_expiry_roll_day_of_month and current_month_end is not None:
        chosen = current_month_end
    else:
        # Past the roll day-of-month (or the current month's own expiry has
        # already passed) -- next month's monthly.
        future_keys = sorted(k for k in by_month if k > current_month_key)
        if not future_keys:
            raise RuntimeError("No next-month expiry available to roll to.")
        chosen = by_month[future_keys[0]]

    raw = chosen.strftime("%d-%b-%y")
    return _compact_expiry(raw), raw


def resolve_cycle_expiry_date(client) -> Optional["datetime.date"]:
    """The expiry the CURRENT CYCLE runs to: the earliest monthly at or after
    today. This is what gets captured on a cycle's first fresh trade and what
    the 15:30 square-off then fires on.

    Distinct from resolve_monthly_expiry(), which applies the day>15 roll and
    so from the 16th names NEXT month's contract -- a 20-Nov trade would
    capture December and the cycle would never end -- and from pos.expiry_date,
    the contract a position actually holds.

    On 03-Nov it returns 23-Nov; on 20-Nov, 23-Nov; on the expiry day itself,
    that day. Raises (via fetch_monthly_expiries) rather than guessing; the
    caller, get_cycle_expiry_date, turns that into None."""
    monthlies = fetch_monthly_expiries(client)
    return monthlies[0] if monthlies else None


def resolve_current_month_expiry_date(client) -> Optional["datetime.date"]:
    """The CURRENT CALENDAR month's own monthly, or None once it has passed.
    Currently unused -- the square-off keys on the captured cycle expiry --
    kept at the author's request, now reading the same futures-based source as
    everything else so the file has a single monthly rule."""
    today = datetime.now(IST).date()
    for d in fetch_monthly_expiries(client):
        if (d.year, d.month) == (today.year, today.month):
            return d
    return None


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


# Holiday-aware next trading day, from the platform's shared calendar --
# the same import Nifty_LEAPS_OptionsSell uses, with the same weekday-only
# fallback if the module is not importable in the strategy subprocess. The
# fallback matters less than it looks: a cycle start computed onto a holiday
# fails SAFE (today never matches it, so no 09:30 entry is taken), it does not
# trade on a wrong day.
try:
    from utils.trading_calendar import next_trading_day as _calendar_next_trading_day
except ImportError:
    _calendar_next_trading_day = None
try:
    from utils.trading_calendar import prev_trading_day as _calendar_prev_trading_day
    from utils.trading_calendar import special_sessions as _calendar_special_sessions
except ImportError:
    _calendar_prev_trading_day = None
    _calendar_special_sessions = None


def _cycle_next_trading_day(d) -> "datetime.date":
    """First trading day strictly after `d`, holiday-aware when possible."""
    if _calendar_next_trading_day is not None:
        try:
            return _calendar_next_trading_day(d)
        except Exception as exc:
            Log.warning(f"trading calendar unavailable ({exc}) -- falling back to "
                        f"weekday-only next-trading-day.")
    return _next_trading_day(d)


def fetch_previous_session_close(client, prev_day) -> Optional[dict]:
    """NIFTY's closing price for `prev_day`, for the 09:31 gap test.

    Read from 1-MINUTE candles of that day alone, never the 15-minute ones.
    Shoonya's 15m series folds the NEXT morning's 09:14 pre-open print into
    the previous day's last candle: on 05-Nov-2026 the 04-Nov 15:15 candle
    read close 24378.05 (that morning's pre-open) where at 22:11 the night
    before it read the real 23960.60. The gap test runs after the pre-open,
    so it always measured spot against the same morning's pre-open price:
    04-Nov logged +0.73% for a true +0.46%, 05-Nov -0.148% for a true
    +1.54%, and each day took the wrong reference mode. In the 1m series the
    pre-open is its own bar dated the new day, and the last bar of the
    previous day (15:29) carries the official close.

    Returns {"close", "timestamp"} or None on any failure -- the caller
    retries next cycle, never guesses."""
    try:
        bars = client.history(symbol=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                              interval="1m", start_date=prev_day.isoformat(),
                              end_date=prev_day.isoformat())
    except Exception as exc:
        Log.warning(f"history() (previous close, 1m) failed for {UNDERLYING_SYMBOL}: {exc}")
        return None
    if _is_error_response(bars) or bars is None or getattr(bars, "empty", True):
        Log.warning(f"No 1m {UNDERLYING_SYMBOL} candles for {prev_day}: {bars if _is_error_response(bars) else 'empty'}")
        return None
    try:
        same_day = [ts.date() == prev_day and ts.time() <= config.market_close for ts in bars.index]
        day_bars = bars[same_day]
    except Exception as exc:
        Log.warning(f"Could not read the 1m {UNDERLYING_SYMBOL} candles for {prev_day}: {exc}")
        return None
    if day_bars.empty:
        Log.warning(f"No 1m {UNDERLYING_SYMBOL} candle on {prev_day} at or before "
                    f"{config.market_close.strftime('%H:%M')}")
        return None
    last_ts = day_bars.index[-1]
    return {"close": float(day_bars.iloc[-1]["close"]), "timestamp": str(last_ts),
            "label": last_ts.strftime("%d-%b %H:%M")}


def _weekday_previous_day(today) -> "datetime.date":
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def resolve_previous_trading_day(today) -> "datetime.date":
    """The last REGULAR trading session before `today`: the day whose close
    is the previous-close reference.

    Holiday-aware, from the platform's trading calendar. It used to skip
    weekends only, so on 25-Nov-2026 it named 24-Nov, a trading holiday:
    the spot gap test still worked (it reads the last candle AT OR BEFORE
    that date), but every option's reference read history for 24-Nov alone,
    got nothing, and no strike could qualify on the new cycle's first day.

    Special sessions are skipped too (author 2026-11-04: "skip muhurt
    trading"). The calendar counts Diwali Muhurat -- Sunday 08-Nov-2026, a
    one-hour evening session -- as a trading day, and its thin last candle
    is not a close to measure a whole day's OI change from; 09-Nov takes
    Friday 06-Nov's close instead. In the calendar feed SPECIAL_SESSION
    entries are the Muhurat sessions (2025 and 2026 checked).

    Falls back to weekends-only if the calendar is not importable or fails."""
    if _calendar_prev_trading_day is not None and _calendar_special_sessions is not None:
        try:
            d = today
            for _ in range(10):
                d = _calendar_prev_trading_day(d)
                if d not in _calendar_special_sessions(d.year):
                    return d
        except Exception as exc:
            Log.warning(f"trading calendar unavailable ({exc}) -- falling back to "
                        f"weekday-only previous trading day.")
    return _weekday_previous_day(today)


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
                                      current_interval: Optional[str] = None,
                                      chain_client=None) -> Optional[tuple]:
    """Extends Nifty_OI_WeeklyBuy_MonthlySell's select_monthly_delta_strike --
    PURE delta-closeness ranking to config.monthly_delta_target (0.27), no
    premium filter anywhere. Keeps that function's expanding-band-scan
    skeleton (band_step_count=8, max_bands=2, ONE batched
    optionchain(with_quotes=True, with_greeks=True) call per band, delta read
    straight off that single response) but:

      1. Ranks ALL scanned round-100-strike candidates by delta-closeness
         to monthly_delta_target (not just tracking a single "best so far").
      2. Walks the ranked list, checking each candidate's OWN OI
         classification (via fetch_candle_oi_premium + classify_oi_premium,
         using `reference` for that candidate's own premium/OI deltas --
         the one necessarily per-candidate, non-batched step in this chain,
         since OI history isn't part of the optionchain() response) and
         returns the first candidate whose verdict is config.short_verdict.
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


    scanned: list[dict] = []   # each: {"strike", "delta", "premium", "diff"}
    last_chain_response: Optional[dict] = None

    for band in range(1, max_bands + 1):
        strike_count = band_step_count * band * 2
        # with_greeks=True so the server supplies delta (author 2026-11-02).
        # It is the platform's own Black-76, fed by _resolve_forward_price()'s
        # per-expiry SYNTHETIC FORWARD -- "the single source of truth for the
        # forward rule" (services/option_greeks_service.py).
        #
        # This script used to solve IV and delta itself from the chain's ltp,
        # which was both unnecessary work and slightly WRONG: it passed
        # `underlying_ltp` as the forward where the server passes
        # `forward_price`. Measured on a live response (underlying_ltp
        # 23440.80 vs forward_price 23419.85, a 21-point gap), that biased
        # every delta HIGH by +0.004 to +0.012 -- up to 12% of the 0.22-0.32
        # band's width. Feeding the right forward reproduced the server's
        # delta to four decimals on all 13 strikes, which is why taking the
        # server's value directly is the simpler fix.
        # chain_client carries hedge_chain_timeout: band 1 (16 strikes) took
        # 6.3s live on Shoonya and band 2 (32) ~10.5s, past the main 10s.
        resp = (chain_client or client).optionchain(
            underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
            expiry_date=_compact_expiry(expiry_raw), strike_count=strike_count,
            with_quotes=True, with_greeks=True,
        )
        if resp.get("status") != "success" or not resp.get("chain"):
            raise RuntimeError(f"optionchain() failed for expiry {expiry_raw}: {resp}")
        last_chain_response = resp

        premium_by_strike: dict[float, float] = {}
        # Per-leg delta straight from the response. A leg whose delta is absent
        # or zero is skipped rather than guessed at -- the same treatment the
        # local solve gave an unsolvable IV.
        delta_by_strike: dict[float, float] = {}
        # The chain carries a real per-leg `oi` (verified against a live
        # Shoonya response: 177515, 5239455, ...), so the CURRENT side of the
        # verdict is already in this one batched call -- no per-candidate
        # history pull. Collected here alongside the premium it must be
        # consistent with: both come from the same snapshot.
        oi_by_strike: dict[float, float] = {}
        for row in resp["chain"]:
            leg = row.get(side_key)
            if not leg or not leg.get("ltp"):
                continue
            if not leg.get("bid") or not leg.get("ask"):
                continue  # no genuine two-sided market -- see sibling script's identical guard
            premium_by_strike[float(row["strike"])] = float(leg["ltp"])
            if leg.get("oi"):
                oi_by_strike[float(row["strike"])] = float(leg["oi"])
            if leg.get("delta") is not None:
                delta_by_strike[float(row["strike"])] = float(leg["delta"])

        def _delta_at(strike: float) -> Optional[float]:
            """The server's delta for this strike, or None.

            None when the chain gave no premium, no delta, or a delta of
            exactly 0 -- the last of which is what an illiquid or unpriceable
            leg comes back as, and ranking it as "delta 0.0" would make it
            look like the furthest possible strike from 0.27 rather than
            unusable. All three are skipped, as an unsolvable IV used to be.
            """
            if premium_by_strike.get(strike) is None:
                return None
            d = delta_by_strike.get(strike)
            if d is None or d == 0.0:
                Log.warning(f"[MONTHLY_{option_type}] no usable delta from the chain for "
                            f"strike {strike} -- skipping it.")
                return None
            return d

        band_start = band_step_count * (band - 1) + 1
        band_end = band_step_count * band
        for step in range(band_start, band_end + 1):
            strike = atm_100 + direction * step * 100.0
            delta = _delta_at(strike)
            if delta is None:
                continue
            premium = premium_by_strike[strike]
            diff = abs(abs(delta) - config.monthly_delta_target)
            scanned.append({"strike": strike, "delta": delta, "premium": premium,
                            "diff": diff, "oi": oi_by_strike.get(strike)})

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
        # CURRENT side: the chain's own snapshot when it carried an oi for this
        # strike, else the candle path. The fallback is what keeps a broker
        # without OI in its chain working.
        current = None
        if config.use_live_oi_for_current and candidate.get("oi"):
            current = {"premium": candidate["premium"], "oi": candidate["oi"],
                       "timestamp": "chain"}
        if current is None:
            current = fetch_candle_oi_premium(client, symbol, OPTIONS_EXCHANGE,
                                               require_fresh_by=require_fresh_by,
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
        if verdict == config.short_verdict:
            return strike, candidate["delta"], candidate["premium"], verdict, last_chain_response

    Log.info(f"[MONTHLY_{option_type}] no candidate (of {len(ranked)} scanned) passed the "
              f"OI filter (need {config.short_verdict}) -- no qualifying strike this cycle.")
    return None


def select_hedge_strike(client, expiry_compact: str, option_type: str, sold_strike: float,
                         sold_premium: float, chain_response: Optional[dict] = None,
                         chain_client=None) -> Optional[dict]:
    """PREMIUM-based selection (unlike the short leg, the hedge IS chosen by
    premium target, per spec): the strike beyond sold_strike (further-OTM CE
    for a sold CE, further-OTM PE for a sold PE) whose premium is closest to
    hedge_premium_pct_of_short of the short's.

    The chain must actually REACH the target. `chain_response` is the short
    scan's own chain, only 16 strikes each side of ATM (800 points on
    NIFTY), and a 20%-premium strike usually sits further out than that.
    It used to be reused as-is, so the "closest" premium was simply the edge
    strike of that window: live 04-Nov-2026 the CE hedge was 24600CE at 60.00
    against a 24.31 target and the PE hedge 23050PE at 75.05 against 26.13,
    each exactly 16 strikes from ATM. Now it is reused only when some
    candidate is already priced at or below the target (so the closest is
    inside it); otherwise a wider chain is fetched, hedge_strike_scan_count
    then doubling up to hedge_strike_scan_max, until one is.

    If even the widest chain cannot reach the target, or a wider fetch
    fails, the closest strike already in hand is still taken: a hedge
    nearer the short costs more but protects more, and selling naked is the
    one outcome this never allows. Returns None only when no strike beyond
    the short has a usable premium at all."""
    target_premium = sold_premium * (config.hedge_premium_pct_of_short / 100.0)

    def _candidates_from(resp) -> list:
        legs = _legs_with_strike(resp, option_type)
        if option_type == "CE":
            return [leg for leg in legs if leg["strike"] > sold_strike and leg.get("ltp")]
        return [leg for leg in legs if leg["strike"] < sold_strike and leg.get("ltp")]

    def _reaches_target(cands) -> bool:
        return any(float(leg["ltp"]) <= target_premium for leg in cands)

    candidates = _candidates_from(chain_response) if chain_response is not None else []
    if not _reaches_target(candidates):
        count = config.hedge_strike_scan_count
        while True:
            try:
                # chain_client carries the long hedge_chain_timeout: a wide
                # chain takes 6-20s+ on Shoonya, past the main client's 10s.
                resp = (chain_client or client).optionchain(
                    underlying=UNDERLYING_SYMBOL, exchange=UNDERLYING_SPOT_EXCHANGE,
                    expiry_date=expiry_compact, strike_count=count,
                    with_quotes=True,   # ONE batched call per width
                )
            except Exception as exc:
                Log.warning(f"[HEDGE_{option_type}] optionchain() raised for expiry "
                            f"{expiry_compact} (strike_count={count}): {exc}")
                break
            if not isinstance(resp, dict) or resp.get("status") != "success" or not resp.get("chain"):
                Log.warning(f"[HEDGE_{option_type}] optionchain() failed for expiry "
                            f"{expiry_compact} (strike_count={count}): {resp}")
                break
            wider = _candidates_from(resp)
            if wider:
                candidates = wider
            if _reaches_target(candidates) or count >= config.hedge_strike_scan_max:
                break
            count = min(config.hedge_strike_scan_max, count * 2)

    if not candidates:
        Log.warning(f"[HEDGE_{option_type}] no {option_type} candidate strictly beyond sold "
                    f"strike {sold_strike} with a usable premium -- no hedge available.")
        return None

    best = min(candidates, key=lambda leg: abs(float(leg["ltp"]) - target_premium))
    if _reaches_target(candidates):
        Log.info(f"[HEDGE_{option_type}] hedge strike={best['strike']} premium={float(best['ltp']):.2f} "
                 f"(target {target_premium:.2f} = {config.hedge_premium_pct_of_short:.0f}% of "
                 f"{sold_premium:.2f})")
    else:
        Log.warning(f"[HEDGE_{option_type}] no strike in reach is priced down to the target "
                    f"{target_premium:.2f} -- taking the closest, strike={best['strike']} "
                    f"premium={float(best['ltp']):.2f} (nearer hedge: costs more, protects more).")
    return best


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


# Reference readings for the day, keyed (symbol, exchange, reference identity).
# The Reference Snapshot is fixed at 09:30, so a strike's reference premium/OI
# cannot change during the day -- without this, every candidate at every check
# re-pulled the identical previous-day candle. Keyed by the reference's own
# date/mode/time so a re-computed reference is never served a stale reading,
# and cleared by _reset_day_if_needed.
_REFERENCE_CACHE: dict = {}
_REFERENCE_CACHE_LOCK = threading.Lock()


def clear_reference_cache():
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_CACHE.clear()


def fetch_live_oi_premium(client, symbol: str, exchange: str) -> Optional[dict]:
    """Current premium + OI from the live quote, for config.use_live_oi_for_current.

    Returns None when the quote fails OR when oi is absent/zero -- the caller
    then falls back to the candle path. That guard matters: a broker whose
    quotes carry no OI would otherwise report oi_change == 0 for every
    candidate, which classify_oi_premium reads as "flat", and nothing would
    ever qualify. (Shoonya's quotes DO carry oi -- broker/shoonya/api/data.py
    -- but get_depth() deliberately returns 0, so the distinction is real.)
    """
    try:
        resp = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        Log.warning(f"quotes() failed for {symbol}.{exchange}: {exc}")
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        Log.warning(f"quotes() error response for {symbol}.{exchange}: {resp}")
        return None
    data = resp.get("data") or {}
    ltp = data.get("ltp")
    oi = data.get("oi")
    if not ltp or not oi:
        return None
    return {"premium": float(ltp), "oi": float(oi), "timestamp": "live"}


def fetch_reference_oi_premium(client, symbol: str, exchange: str,
                                reference: "ReferenceSnapshot") -> Optional[dict]:
    """OI + premium at the day's fixed Reference Time -- either the previous
    trading day's last closed candle (mode == "prev_close") or today's
    09:30 candle close (mode == "today_0935", read via the 5-minute
    bootstrap interval -- see config.reference_gap_bootstrap_interval's own
    comment for why: the regular 15m interval would collapse 09:30 and the
    first 09:35 "current" read onto the identical candle)."""
    cache_key = (symbol, exchange, reference.reference_date, reference.mode,
                 reference.reference_time_iso)
    with _REFERENCE_CACHE_LOCK:
        hit = _REFERENCE_CACHE.get(cache_key)
    if hit is not None:
        return hit

    reading = _fetch_reference_oi_premium_uncached(client, symbol, exchange, reference)
    if reading is not None:
        with _REFERENCE_CACHE_LOCK:
            _REFERENCE_CACHE[cache_key] = reading
    return reading


def _fetch_reference_oi_premium_uncached(client, symbol: str, exchange: str,
                                          reference: "ReferenceSnapshot") -> Optional[dict]:
    """The actual fetch. Only a SUCCESSFUL reading is cached -- a None is a
    transient failure and must be retried on the next check, not remembered."""
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


_APIKEY_IN_URL = re.compile(r"(apikey=)[^&#\s]+", re.IGNORECASE)


def _redact_url(url: str) -> str:
    """The URL with any apikey query value masked, for logging only.

    check_pending_action and check_force_exit carry the API key in the query
    string (`...?apikey=<key>`), and this script's Log is a bare
    StreamHandler with no redaction filter, so logging the raw URL writes the
    full key to the log on every failed call. That is exactly what
    Nifty_LEAPS_OptionsSell's otherwise-identical helpers do (verified
    2026-11-03 by executing them against a dead port); this port masks it.
    The path and every other parameter are kept, because the path is what
    identifies a 404."""
    return _APIKEY_IN_URL.sub(r"\1***", url)


def _post_json_local(env: "Environment", path: str, payload: bytes, timeout: float = 3.0):
    """POST to the dedicated strategy_reporting subprocess over plain TCP
    loopback on STRATEGY_REPORTING_PORT (default 8766). Retries once at 3x
    the timeout -- covers the post-restart burst where every running
    strategy reconnects at once. See AUTHORING_CHECKLIST.md section 3 for
    why this must never hit FLASK_PORT/openalgo.sock."""
    headers = {"Content-Type": "application/json"}
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    url_for_log = _redact_url(f"{base}{path}")
    last_exc: Optional[Exception] = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            req = urllib.request.Request(f"{base}{path}", data=payload, method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=attempt_timeout).close()
            return
        except urllib.error.HTTPError as exc:
            # Read the BODY, not just the status line -- str(HTTPError) is only
            # "HTTP Error 404: NOT FOUND", identical whether the 404 came from
            # the handler's own JSON or from Flask never matching the route.
            # Same reasoning as Nifty_LEAPS_OptionsSell's helper (2026-10-21).
            # The body is the SERVER's response, never this request's payload.
            body = ""
            try:
                body = exc.read().decode(errors="replace")[:300]
            except Exception:
                pass
            last_exc = exc
            Log.warning(f"_post_json_local POST {url_for_log} (timeout={attempt_timeout}s) "
                        f"-> HTTP {exc.code} {exc.reason}: {body}")
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_post_json_local POST {url_for_log} "
                        f"(timeout={attempt_timeout}s) failed: {exc}")
    raise last_exc


def _get_json_local(env: "Environment", path: str, timeout: float = 3.0) -> dict:
    """GET counterpart to _post_json_local -- same loopback-with-retry
    behavior. Raises if both attempts fail; caller treats it as "no action
    pending" rather than crashing the scheduler loop."""
    base = f"http://127.0.0.1:{os.getenv('STRATEGY_REPORTING_PORT', '8766')}"
    url_for_log = _redact_url(f"{base}{path}")
    last_exc: Optional[Exception] = None
    for attempt_timeout in (timeout, timeout * 3):
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=attempt_timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode(errors="replace")[:300]
            except Exception:
                pass
            last_exc = exc
            Log.warning(f"_get_json_local GET {url_for_log} (timeout={attempt_timeout}s) "
                        f"-> HTTP {exc.code} {exc.reason}: {body}")
        except Exception as exc:
            last_exc = exc
            Log.warning(f"_get_json_local GET {url_for_log} "
                        f"(timeout={attempt_timeout}s) failed: {exc}")
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
            # Finalize FIRST, before the "still entering" guard. An abandoned
            # half-built leg has its exits satisfied but never both entries
            # filled, so gating finalize behind that guard left it stuck at
            # "still entering" forever -- no trade log, no PnL, never flat.
            if pos.short_exit_filled and pos.hedge_exit_filled:
                self._finalize_exit(pos, pos.pending_exit_reason)
                return
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
            # Position checks (config.daily_decision_times) FIRST, then the
            # tiered SL. Both dispatch through the same per-leg _eval_pending
            # guard, so whichever is submitted first owns the leg for that
            # tick. They were once the other way round: the SL was submitted
            # on every tick, so the position check always found the leg busy
            # and returned -- an open leg never ran its 11:30 / 13:30 / 15:30
            # check, the expiry-day square-off included (live 04-Nov-2026: no
            # position-check line for any open leg all day). A due slot now
            # takes the leg for one tick and the SL resumes on the next; the
            # check itself keeps running the SL while it waits on a late
            # candle (_reclassify_with_retry). Keeping one guard is
            # deliberate: _exit's in-flight check is not atomic, so an SL and
            # a check running side by side could each send a buy-back.
            self._maybe_daily_decision_bg()
            # Continuous tiered-SL check -- every tick, not gated to a
            # candle boundary (spec item 5). A no-op on the tick a position
            # check was just dispatched (the guard above holds the leg).
            self._manage_open_position_bg()
        else:
            # Flat -- a position check's search and the 9:30 entry evaluation
            # can BOTH apply on an entry day now; see
            # _maybe_daily_decision_bg for why they cannot double-enter.
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

    def _place_and_watch_short(self, symbol):
        """Place the short SELL and confirm it. Assumes the hedge is ALREADY
        filled -- that is the only state this is ever called from.

        Shared by the entry chain and by every recovery path that ends with a
        filled hedge and no short (Retry on entry_failed, a hedge that fills
        during Cancel's final chance, a manually-completed hedge). One copy
        means the chain cannot differ between the normal path and a repair,
        which is exactly how the recovery layer fell out of step with the
        entry order in the first place.

        Does NOT touch _pending_fills -- the caller owns that for its whole
        chain, so nothing can evaluate the leg mid-repair."""
        try:
            order_id = place(self.engine.client, self.engine.env.strategy_tag, symbol,
                             OPTIONS_EXCHANGE, "SELL", config.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] short entry placeorder failed for "
                          f"{symbol}: {exc}")
            self._enter_error_mode("", "entry_failed", "terminal", "", str(exc))
            return

        pos = self.leg.position
        pos.entry_order_id = order_id
        self.engine.save_state()

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
            Log.exception(f"[MONTHLY_{self.option_type}] short entry fill-poll failed for "
                          f"{symbol}: {exc}")
            self._enter_error_mode(order_id, "entry_failed", "resting", order_id, str(exc))
            return

        pos = self.leg.position
        if pos.entry_order_id != order_id:
            return
        entry_px = float(fill.get("average_price") or fill.get("price") or 0.0)
        pos.entry_px = entry_px
        pos.entry_filled = True
        self.engine.price_stream.add_instruments([{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}])
        self.engine.save_state()
        Log.info(f"[MONTHLY_{self.option_type}] Short entry filled: {symbol}@{entry_px} "
                 f"qty={config.quantity} verdict={pos.entry_trigger_verdict} "
                 f"delta={pos.delta_at_entry:.3f} -- position fully open.")

    def _abandon_attempt(self, why, user_initiated=False):
        """Give up on a half-built entry.

        Author 2026-11-02: "any failuer on trade placing, it should be handled
        manully ... why are you seleling headeg?" -- so this places NO order.
        An earlier revision sold a live lone hedge automatically; that was an
        order on the operator's behalf that nobody asked for.

        Three cases, decided from the FILL FLAGS rather than from the error
        state's name (hedge-first inverted that mapping once already):

        * Nothing filled -> reset the leg to flat. Safe: there is no position.
        * Something filled, not user initiated -> leave the position EXACTLY
          as it is, in a terminal error state, and push it to the platform so
          a human resolves it. Critically it is NOT reset: the filled leg stays
          recorded, so it keeps its PnL row, keeps its WebSocket subscription,
          and error_state stops evaluate() entering again and buying a second
          hedge on top of the live one.
        * Something filled, user initiated -> the operator pressed Cancel,
          which is them saying they have dealt with it at the broker. Clear the
          leg, loudly, because the local record is being discarded while a
          position may still exist.
        """
        pos = self.leg.position
        short_live = bool(pos.short_symbol) and pos.entry_filled
        hedge_live = bool(pos.hedge_symbol) and pos.hedge_entry_filled

        if not short_live and not hedge_live:
            syms = [{"symbol": sym, "exchange": OPTIONS_EXCHANGE}
                    for sym in (pos.short_symbol, pos.hedge_symbol) if sym]
            if syms:
                self.engine.price_stream.remove_instruments(syms)
            self.leg.position = ShortHedgePosition()
            self.engine.save_state()
            Log.info(f"[MONTHLY_{self.option_type}] attempt abandoned ({why}) -- nothing was "
                     f"filled, leg is flat.")
            return

        held = ("a short and a hedge" if short_live and hedge_live
                else ("a short" if short_live else "a hedge"))

        if user_initiated:
            syms = [{"symbol": sym, "exchange": OPTIONS_EXCHANGE}
                    for sym in (pos.short_symbol, pos.hedge_symbol) if sym]
            if syms:
                self.engine.price_stream.remove_instruments(syms)
            self.leg.position = ShortHedgePosition()
            self.engine.save_state()
            Log.warning(f"[MONTHLY_{self.option_type}] Cancel on a leg that HOLDS {held} "
                        f"({why}) -- clearing the local record as instructed. The position "
                        f"itself is NOT closed by this; close it at the broker if it is "
                        f"still open.")
            return

        # Not user initiated: stop and ask for a human, through the SAME
        # mechanism every other failure in this file uses. _enter_error_mode
        # owns the whole convention -- it writes error_state/kind/order_id/
        # message/since, derives the action direction from the state, pushes
        # the leg to the platform so the Retry / Cancel / Manually Completed
        # buttons appear, logs at error level and notifies Telegram. Writing
        # those fields by hand here duplicated it and would drift from it.
        pos.pending_reentry_search = False
        pos.reentry_slot_hm = ""
        self._enter_error_mode(
            "",
            "entry_failed" if hedge_live and not short_live else "exit_failed",
            "terminal",
            "",
            f"Order placement failed ({why}) while this leg HOLDS {held} at the broker. "
            f"Nothing was closed automatically. Decide at the broker, then resolve here: "
            f"Manually Completed with the real fill price if the missing leg did go "
            f"through, or Cancel once you have closed the position yourself.",
        )

    def _manage_open_position_bg(self):
        self._dispatch_bg(self._manage_open_position)

    def manage_sl_only(self):
        """The stop-loss path alone, for ticks where the day's Reference
        Snapshot is not yet fixed (a gap day before 09:45).

        It repeats evaluate()'s guards rather than calling it, because
        everything else evaluate() reaches -- the entry search and the
        verdict re-check -- genuinely needs the reference. The SL does not:
        it compares the live LTP to entry_px. The OI line
        _manage_open_position logs needs the day's reference too, so it is
        simply skipped until that is fixed.
        """
        if self.leg_key in self.engine._pending_fills:
            return
        pos = self.leg.position
        if pos.error_state or not pos.short_symbol:
            return
        if not (pos.entry_filled and pos.hedge_entry_filled):
            return
        if pos.short_exit_order_id or pos.hedge_exit_order_id:
            return  # an exit is already in flight -- its watcher owns the leg
        self._manage_open_position_bg()

    def _maybe_evaluate_entry_bg(self):
        """Fires only on this cycle's own start day (the day after the
        prior contract's own expiry), at/after entry_evaluation_time
        (9:30), at most once per calendar day regardless of outcome."""
        # Same one-minute buffer as the position checks -- the 09:15-09:30
        # candle this reads is published a moment after 09:30. The gap-day
        # path waits for 09:45 separately, via reference_wait_time.
        if datetime.now(IST).time() < _with_buffer(config.entry_evaluation_time):
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
        """Fires once per SLOT in config.daily_decision_times (11:30 / 13:30 /
        15:30), on every day that is NOT this cycle's own 9:30 entry day
        (spec item 10 -- the two are mutually exclusive per day).

        Was once per calendar day at 15:15. The guard is now keyed on
        "<date> <HH:MM>" rather than the date alone, so three checks a day
        each run exactly once and a restart mid-session does not replay the
        slots already done. A state file written by the previous version
        holds a bare date, which matches no slot key -- so the first slot
        after an upgrade simply runs, which is the safe direction.

        Only the LATEST elapsed slot is run. If the process was down over
        11:30 and 13:30 and starts at 14:00, replaying 11:30 would act on a
        verdict that is two hours stale; the 13:30 check is the current view
        of the same position, so that is the one that fires."""
        now = datetime.now(IST)
        elapsed = [t for t in config.daily_decision_times
                   if now.time() >= _with_buffer(t)]
        if not elapsed:
            return
        slot_time = elapsed[-1]
        today = now.date()
        # The expiry-day force-close is terminal for the day: no further
        # check may search or enter, or this leg opens next month's contract
        # a day before the cycle starts.
        if self.leg.expiry_closed_date == today.isoformat():
            return
        # The entry day is NOT excluded (author 2026-11-01: "trade can be
        # taken at 9:30 AM or other timing at first day of new expiry"). The
        # two paths cannot double-enter: _maybe_evaluate_entry_bg marks
        # last_entry_eval_date once per day whatever the outcome, both paths
        # dispatch through the same per-leg _eval_pending guard so they
        # serialise, and _maybe_evaluate_entry_bg is only reached from
        # evaluate()'s FLAT branch -- so once either has opened a position
        # the other sees it and takes the verdict path instead.
        slot_key = f"{today.isoformat()} {slot_time.strftime('%H:%M')}"
        if self.leg.last_daily_decision_date == slot_key:
            return
        if self.leg_key in self.engine._eval_pending:
            return
        self.engine._eval_pending.add(self.leg_key)

        def _run():
            try:
                self._daily_decision(slot_time)
            except Exception as exc:
                Log.exception(f"[{self.leg_key}] Background position check "
                              f"({slot_key}) failed: {exc}")
            finally:
                self.leg.last_daily_decision_date = slot_key
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
        # IST.localize(), never tzinfo=IST: pytz resolves a tzinfo= argument to
        # the zone's LMT entry, +05:53, which puts this boundary 23 minutes
        # EARLY -- so a candle a whole slot stale satisfies require_fresh_by and
        # the freshness gate (and the decision buffer that depends on it) does
        # nothing. Measured: 11:30 became 11:30+05:53 instead of +05:30.
        trigger_boundary = IST.localize(
            datetime.combine(datetime.now(IST).date(), trigger_time))
        max_attempts = config.oi_freshness_max_retries
        for attempt in range(1, max_attempts + 1):
            is_last = attempt == max_attempts
            fresh_gate = None if is_last else trigger_boundary
            try:
                return select_qualifying_monthly_strike(
                    self.engine.client, spot, expiry_raw, self.option_type,
                    self.engine.state.reference, require_fresh_by=fresh_gate,
                    current_interval=current_interval,
                    chain_client=getattr(self.engine, "hedge_chain_client", None),
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
            self.engine.client, expiry_compact, self.option_type, strike, premium, chain_response,
            chain_client=getattr(self.engine, "hedge_chain_client", None),
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
        # CAPTURE the cycle's expiry, ONCE, on the first fresh trade of the
        # cycle. That date is what the 15:30 square-off fires on, and it does
        # NOT change for the rest of the cycle -- so a position opened after
        # the 15th in NEXT month's contract is still closed on it.
        #
        # Already captured for this cycle is decided from the stored value: a
        # stored date still in the future belongs to the running cycle and is
        # left alone; an empty, unreadable or past date means the next capture
        # is due. No extra flag, and it self-corrects across a restart.
        today = datetime.now(IST).date()
        captured = None
        if leg.marked_expiry:
            try:
                captured = datetime.strptime(leg.marked_expiry, "%Y-%m-%d").date()
            except ValueError:
                captured = None
        if captured is not None and captured >= today:
            Log.info(f"[MONTHLY_{self.option_type}] cycle expiry already captured as "
                     f"{leg.marked_expiry} -- not rewritten by this trade "
                     f"(contract traded: {expiry_date_iso}).")
        else:
            cycle_expiry = self.engine.get_cycle_expiry_date()
            if cycle_expiry is None:
                Log.warning(f"[MONTHLY_{self.option_type}] cycle expiry unavailable -- "
                            f"NOT captured. The square-off cannot fire until it is, so "
                            f"this leg must be watched manually.")
            else:
                leg.marked_expiry = cycle_expiry.isoformat()
                Log.info(f"[MONTHLY_{self.option_type}] CAPTURED cycle expiry "
                         f"{leg.marked_expiry} on this cycle's first fresh trade "
                         f"(contract traded: {expiry_date_iso}; square-off at "
                         f"{config.expiry_close_time.strftime('%H:%M')} on the captured "
                         f"date).")
        self.engine.save_state()

        # HEDGE FIRST, then the short (author 2026-11-01: "headges are bought
        # first then short position"). The long leg reduces the margin the
        # short then blocks, and a failure here leaves NO position rather than
        # the naked short the previous order could leave behind.
        try:
            hedge_order_id = place(self.engine.client, self.engine.env.strategy_tag, hedge_symbol,
                                   OPTIONS_EXCHANGE, "BUY", config.quantity)
        except Exception as exc:
            Log.exception(f"[MONTHLY_{self.option_type}] hedge entry placeorder failed for "
                          f"{hedge_symbol}: {exc}")
            self._enter_error_mode("", "hedge_entry_failed", "terminal", "", str(exc))
            return

        leg.trade_count += 1
        leg.position.hedge_entry_order_id = hedge_order_id
        self.engine.save_state()

        self.engine._pending_fills.add(self.leg_key)
        self.engine._fill_executor.submit(self._watch_entry_fill, hedge_order_id, symbol, hedge_symbol)

    def _watch_entry_fill(self, hedge_order_id, symbol, hedge_symbol):
        """Runs on _fill_executor. Confirms the HEDGE leg's fill, then --
        still on this same background thread -- places and confirms the
        SHORT leg's fill immediately after. _pending_fills is held for the
        WHOLE chain (hedge + short), released only in the outer `finally`,
        so evaluate() never touches this leg mid-chain.

        Hedge-before-short (author 2026-11-01). If the short cannot be placed
        or filled after the hedge is in, the leg is left holding a LONG option
        and routed to error recovery -- a bounded loss, unlike the naked short
        the previous order left behind when the hedge failed."""
        try:
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
                self._enter_error_mode(hedge_order_id, "hedge_entry_failed", "resting",
                                       hedge_order_id, str(exc))
                return

            pos = self.leg.position
            if pos.hedge_entry_order_id != hedge_order_id:
                return  # guard vs. a superseded/stale order
            hedge_px = float(hedge_fill.get("average_price") or hedge_fill.get("price") or 0.0)
            pos.hedge_entry_px = hedge_px
            pos.hedge_entry_filled = True
            self.engine.price_stream.add_instruments([{"symbol": hedge_symbol, "exchange": OPTIONS_EXCHANGE}])
            self.engine.save_state()
            Log.info(f"[MONTHLY_{self.option_type}] Hedge entry filled: {hedge_symbol}@{hedge_px} "
                     f"qty={config.quantity} -- margin benefit in place, selling the short now.")

            self._place_and_watch_short(symbol)
        finally:
            self.engine._pending_fills.discard(self.leg_key)

    # -------------------------------------------------------------------
    # Continuous tiered-SL management
    # -------------------------------------------------------------------
    def _manage_open_position(self):
        pos = self.leg.position

        # The SL acts only between sl_check_start_time and sl_check_end_time
        # (author 2026-11-01: "at 9:30 SL check"). Before 09:30 the day's
        # Reference Snapshot is not yet fixed, so a stop taken then would be
        # measured against a reference that does not exist; after the close
        # there is nothing to act on. An overnight gap is therefore judged on
        # where the premium sits at 09:30, not at the open -- measured over
        # 679 trades that was worth +Rs 563 and cut the worst single trade
        # from -Rs 27,430 to -Rs 18,255.
        now_t = datetime.now(IST).time()
        if now_t < config.sl_check_start_time or now_t > config.sl_check_end_time:
            return

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
        # on a signal change here (that is a position check's job: 11:30,
        # 13:30 or 15:30, per config.daily_decision_times).
        boundary = self.engine._last_candle_boundary
        boundary_key = boundary.isoformat() if boundary is not None else None
        # Measured against the DAY'S Reference Snapshot, the same basis the
        # position check decides on (_fresh_oi_reclassify). It used to read
        # pos.reference_premium / pos.reference_oi, which nothing ever sets,
        # so every line was "change vs zero" -- premium_chg equal to the
        # premium itself and a verdict of "accumulation" whatever the market
        # did (live 04-Nov-2026). Skipped while the reference is not yet
        # fixed (a gap day before 09:45, via manage_sl_only): there is no
        # honest change to report then.
        reference = self.engine.state.reference
        reference_ready = bool(reference is not None and reference.computed and reference.mode)
        if (reference_ready and boundary_key is not None
                and boundary_key != self._last_logged_boundary):
            current = fetch_candle_oi_premium(self.engine.client, pos.short_symbol, OPTIONS_EXCHANGE)
            ref_reading = (fetch_reference_oi_premium(self.engine.client, pos.short_symbol,
                                                      OPTIONS_EXCHANGE, reference)
                           if current is not None else None)
            if current is not None and ref_reading is not None:
                premium_change = current["premium"] - ref_reading["premium"]
                oi_change = current["oi"] - ref_reading["oi"]
                verdict = classify_oi_premium(premium_change, oi_change)
                Log.info(f"[MONTHLY_{self.option_type}] (open) candle={current['timestamp']} "
                         f"symbol={pos.short_symbol} premium={current['premium']:.2f} "
                         f"premium_chg={premium_change:+.2f} oi_chg={oi_change:+.0f} verdict={verdict} "
                         f"sl_price={sl_price:.2f} tier2_locked={pos.sl_tier2_locked}")
                self._last_logged_boundary = boundary_key

    # -------------------------------------------------------------------
    # Position checks (config.daily_decision_times)
    # -------------------------------------------------------------------
    def _daily_decision(self, slot_time: Optional[time] = None):
        """One position check. `slot_time` is the scheduled time this run
        belongs to (11:30 / 13:30 / 15:30) and is what the candle-freshness
        retry measures against; None falls back to the last slot."""
        try:
            self.engine.get_monthly_expiry()  # informational -- feeds whichever entry search fires below
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] daily-decision expiry resolution failed: {exc}")

        pos = self.leg.position
        today_key = datetime.now(IST).date().isoformat()

        # EVERY position is closed on the CURRENT CALENDAR MONTH's expiry date,
        # whatever contract it is actually holding (author 2026-11-01: "you
        # will close all postion at current calendar month expiry not trade
        # position expiry").
        #
        # From day 16 these are different dates and the difference is
        # deliberate. resolve_monthly_expiry rolls a new entry to NEXT month's
        # contract, so a trade opened on 20-Jun holds the JULY contract and is
        # MARKED July (leg.marked_expiry, for the audit trail) -- but it is
        # still squared off on the 30-Jun calendar expiry, after ten days,
        # rather than being carried to late July.
        #
        # The held contract's own expiry stays as a safety net below it: if the
        # calendar square-off somehow did not happen, a position must still
        # never be carried past the expiry of the contract it holds.
        # The square-off fires on the CAPTURED cycle expiry -- the date stored
        # on this leg by its cycle's first fresh trade -- not on a calendar
        # lookup made fresh each day. Those differ: on a cycle-start day like
        # 27-May the calendar month's own expiry (26-May) has already passed.
        captured_expiry = self.leg.marked_expiry
        calendar_expiry_today = bool(captured_expiry) and captured_expiry == today_key
        cm_expiry = captured_expiry or "uncaptured"
        own_expiry_today = bool(pos.expiry_date) and pos.expiry_date == today_key

        # On the calendar expiry date the day runs NORMALLY until the
        # square-off (author 2026-11-02: "any new postion taken on current
        # expiry last day, it will take position on next month, but at 3:30
        # all position will be closed" / "it hsould follow the rule").
        #
        # So an 11:30 or 13:30 entry IS taken, and past day 15 it lands in
        # next month's contract -- which is then squared off at 15:30 the same
        # day along with everything else. Entries stop only AT/AFTER the
        # square-off slot, where opening a position in the same breath as
        # closing them all would defeat it. An earlier revision refused
        # entries for the whole of that date, which was more than the rule.
        slot_now = slot_time or datetime.now(IST).time()
        entries_blocked = calendar_expiry_today and slot_now >= config.expiry_close_time

        if pos.short_symbol and (calendar_expiry_today or own_expiry_today):
            # Square off at config.expiry_close_time, not at the first check
            # that notices. slot_time is None only when something calls
            # _daily_decision directly; treat that as "now" rather than as the
            # square-off slot, so a stray call cannot close a position hours
            # early. The held contract's OWN expiry is the one exception and is
            # closed immediately -- carrying it past that is not a choice.
            # Defer to config.expiry_close_time. The only exception is a held
            # contract expiring today when today is NOT a calendar expiry --
            # then it is closed at once, because carrying it past its own
            # expiry is not a choice. An earlier revision wrote this as
            # "calendar and not own", which never deferred in the NORMAL case
            # (a position holding the current month's contract on its own
            # expiry day satisfies BOTH), so the square-off fired at 11:30
            # instead of 15:30 -- the opposite of the config's own comment.
            if slot_now < config.expiry_close_time and not (
                    own_expiry_today and not calendar_expiry_today):
                # Before the square-off slot the 11:30 and 13:30 checks are
                # ordinary: the verdict branch runs, a flip closes the
                # position, and a re-entry may follow. Only at/after
                # expiry_close_time do entries stop. Two earlier revisions
                # over-reached here -- one suppressed the verdict check for the
                # whole day, the other kept the check but refused all entries.
                pass
            else:
                why = ("held contract's own expiry" if own_expiry_today
                       else f"calendar month expiry {cm_expiry}")
                Log.info(f"[MONTHLY_{self.option_type}] square-off ({why}) at "
                         f"{config.expiry_close_time.strftime('%H:%M')} -- closing short "
                         f"then hedge, terminal for today. Marked expiry of the closed "
                         f"trade: {pos.expiry_date or self.leg.marked_expiry or 'unknown'}.")
                pos.pending_reentry_search = False
                pos.reentry_slot_hm = ""
                self.leg.expiry_closed_date = today_key
                self.engine.save_state()
                self._exit(pos, "expiry_day_close")
                return  # terminal -- no new-position search follows today

        if pos.short_symbol:
            verdict = self._reclassify_with_retry(pos, slot_time or config.daily_decision_time)
            if pos.error_state:
                Log.warning(f"[MONTHLY_{self.option_type}] position check: the leg went into error "
                            f"while the check waited (a stop-loss exit was refused) -- leaving it "
                            f"to the error flow, no second exit.")
                return
            if pos.short_exit_order_id or not pos.short_symbol:
                Log.info(f"[MONTHLY_{self.option_type}] position check: the stop-loss closed this "
                         f"position while the check waited for its candle -- nothing more to decide.")
                return
            if verdict is None:
                Log.warning(f"[MONTHLY_{self.option_type}] fresh OI re-check unavailable this slot -- "
                            f"holding, will retry at the next position check.")
                return
            if verdict == config.hold_verdict:
                Log.info(f"[MONTHLY_{self.option_type}] position check: still "
                         f"{config.hold_verdict} -- holding.")
                return
            Log.info(f"[MONTHLY_{self.option_type}] position check: no longer "
                     f"{config.hold_verdict} (verdict={verdict}) -- closing short then hedge, "
                     f"will search for a fresh qualifying strike once closed.")
            # A re-entry is armed unless entries are blocked -- i.e. unless we
            # are at/after the square-off slot on the calendar expiry date.
            pos.pending_reentry_search = not entries_blocked
            pos.reentry_slot_hm = ("" if entries_blocked
                                   else (slot_time or config.daily_decision_time).strftime("%H:%M"))
            if entries_blocked:
                self.leg.expiry_closed_date = today_key
            self._exit(pos, "opposite_signal")
            return

        # Flat -- closed earlier today by a WS-driven SL hit, or simply
        # never entered -- search for a fresh qualifying strike, anchored to
        # this slot.
        #
        # Except on the calendar expiry date: no FRESH entry that day. The
        # day's whole purpose is to close the cycle out at
        # config.expiry_close_time, and past day 15 a search resolves to NEXT
        # month's contract -- so an 11:30 entry would open a position that is
        # squared off at 15:30 the same day, paying both ways for nothing.
        # This guard belongs HERE, in the flat branch: an earlier revision put
        # it above the position-held branch, which silently stopped 11:30 and
        # 13:30 re-checking an OPEN position's verdict on that date -- the
        # opposite of what its own comment claimed.
        if entries_blocked:
            Log.info(f"[MONTHLY_{self.option_type}] calendar month expiry ({cm_expiry}) "
                     f"square-off slot -- flat and taking no fresh entry.")
            self.leg.expiry_closed_date = today_key
            self.engine.save_state()
            return
        self._search_and_enter_fresh(slot_time)

    def _fresh_oi_reclassify(self, pos, require_fresh_by: Optional[datetime] = None) -> Optional[str]:
        """Always re-fetched, NEVER cached -- fetch_candle_oi_premium's own
        "drop if still forming" guard is the candle-boundary check here,
        per AUTHORING_CHECKLIST.md's preferred pattern. May raise
        CandleNotYetFresh (propagated, not caught here) when require_fresh_by
        is given -- caller (_reclassify_with_retry) owns the retry loop."""
        # Same basis as the entry verdict: a live reading when the broker gives
        # one, else the candle. Keeping the two sides on different bases would
        # mean a position entered on a live verdict is judged on a candle one.
        current = None
        if config.use_live_oi_for_current:
            current = fetch_live_oi_premium(self.engine.client, pos.short_symbol,
                                            OPTIONS_EXCHANGE)
        if current is None:
            current = fetch_candle_oi_premium(self.engine.client, pos.short_symbol,
                                               OPTIONS_EXCHANGE,
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
        # IST.localize(), never tzinfo=IST: pytz resolves a tzinfo= argument to
        # the zone's LMT entry, +05:53, which puts this boundary 23 minutes
        # EARLY -- so a candle a whole slot stale satisfies require_fresh_by and
        # the freshness gate (and the decision buffer that depends on it) does
        # nothing. Measured: 11:30 became 11:30+05:53 instead of +05:30.
        trigger_boundary = IST.localize(
            datetime.combine(datetime.now(IST).date(), trigger_time))
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
                if self._wait_running_sl(config.oi_freshness_retry_delay_sec):
                    return None  # the SL closed the position while we waited
        return None  # unreachable in practice -- the last attempt's fresh_gate is None

    def _wait_running_sl(self, seconds: float) -> bool:
        """Sleep `seconds` while still running the tiered SL every
        scheduler_interval. This runs on the position check's own thread,
        which holds the leg's _eval_pending guard, so the SL cannot be
        dispatched separately meanwhile -- without this a late candle (up to
        oi_freshness_max_retries x oi_freshness_retry_delay_sec, 90s) would
        leave the short with no stop for that whole wait. Same thread, so the
        SL and the check still never send an exit side by side. Returns True
        once an exit is in flight, so the check stops instead of deciding on
        a position that is already being closed."""
        import time as _time
        pos = self.leg.position
        step = max(1.0, float(config.scheduler_interval))
        deadline = _time.monotonic() + seconds
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return False
            _time.sleep(min(step, remaining))
            try:
                self._manage_open_position()
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] SL check during position-check "
                              f"wait failed: {exc}")
            if pos.short_exit_order_id or not pos.short_symbol or pos.error_state:
                return True

    def _search_and_enter_fresh(self, slot_time: Optional[time] = None):
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
            result = self._select_qualifying_strike_with_retry(
                spot, expiry_raw, slot_time or config.daily_decision_time)
        except RuntimeError as exc:
            Log.warning(f"[MONTHLY_{self.option_type}] daily-decision strike selection failed: {exc}")
            return
        if result is None:
            Log.info(f"[MONTHLY_{self.option_type}] daily decision: no qualifying strike -- staying flat.")
            return
        strike, delta, premium, verdict, chain_response = result
        hedge = select_hedge_strike(
            self.engine.client, expiry_compact, self.option_type, strike, premium, chain_response,
            chain_client=getattr(self.engine, "hedge_chain_client", None),
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

        # A leg can be HALF BUILT -- under hedge-first, an entry_failed leaves
        # the hedge filled and the short never placed. Buying short_symbol then
        # would OPEN a new unmanaged long at the short strike rather than close
        # anything, and _finalize_exit would book (0 - exit_px) * qty because
        # entry_px is still 0.0. An unfilled leg is marked exit-satisfied so
        # the chain and _finalize_exit see one consistent picture.
        pos.pending_exit_reason = reason
        if not pos.entry_filled:
            Log.warning(f"[MONTHLY_{self.option_type}] exit ({reason}): the short was never "
                        f"filled -- nothing to buy back, going straight to the hedge.")
            pos.short_exit_order_id = ""
            pos.short_exit_filled = True
            pos.short_exit_fill_px = None
            self.engine.save_state()
            self.engine._pending_fills.add(self.leg_key)
            self.engine._fill_executor.submit(self._exit_hedge_leg)
            return
        if not pos.hedge_entry_filled:
            Log.warning(f"[MONTHLY_{self.option_type}] exit ({reason}): the hedge was never "
                        f"filled -- the short alone will be bought back.")
            pos.hedge_exit_order_id = ""
            pos.hedge_exit_filled = True
            pos.hedge_exit_fill_px = None

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

    def _exit_hedge_leg(self):
        """Sell the hedge and confirm it, without a short to buy back first.

        Used when the short was never filled -- _exit marks that leg's exit
        satisfied and comes straight here. Holds _pending_fills for its own
        chain and finalizes when both exit flags are set, so an abandoned
        half-built leg still produces a trade-log row, a PnL entry and a flat
        leg, instead of sitting at evaluate()'s "still entering" guard
        forever."""
        try:
            pos = self.leg.position
            if not pos.hedge_entry_filled:
                # Nothing was filled at all -- there is no order to place.
                pos.hedge_exit_order_id = ""
                pos.hedge_exit_filled = True
                pos.hedge_exit_fill_px = None
                self.engine.save_state()
                self._finalize_exit(pos, pos.pending_exit_reason)
                return
            try:
                hedge_order_id = place(self.engine.client, self.engine.env.strategy_tag,
                                       pos.hedge_symbol, OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge-only exit placeorder failed for "
                              f"{pos.hedge_symbol}: {exc}")
                self._enter_error_mode("", "hedge_exit_failed", "terminal", "", str(exc))
                return
            pos.hedge_exit_order_id = hedge_order_id
            pos.hedge_exit_filled = False
            self.engine.save_state()
            try:
                hedge_fill = poll_fill(self.engine.client, hedge_order_id,
                                       self.engine.env.strategy_tag, pos.hedge_symbol,
                                       OPTIONS_EXCHANGE, "SELL", pos.quantity)
            except OrderNeedsAttention as exc:
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "resting",
                                       exc.order_id, str(exc))
                return
            except (RuntimeError, TimeoutError) as exc:
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "terminal", "", str(exc))
                return
            except Exception as exc:
                Log.exception(f"[MONTHLY_{self.option_type}] hedge-only exit fill-poll failed: {exc}")
                self._enter_error_mode(hedge_order_id, "hedge_exit_failed", "resting",
                                       hedge_order_id, str(exc))
                return
            pos = self.leg.position
            if pos.hedge_exit_order_id != hedge_order_id:
                return
            pos.hedge_exit_fill_px = float(hedge_fill.get("average_price")
                                           or hedge_fill.get("price") or 0.0)
            pos.hedge_exit_filled = True
            self.engine.save_state()
            if pos.short_exit_filled:
                self._finalize_exit(pos, pos.pending_exit_reason)
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
        # A leg that never filled contributes nothing. Without this an
        # abandoned half-built position books (0 - exit_px) * qty against the
        # leg that was never opened, because entry_px is still 0.0.
        short_was_open = pos.entry_filled
        hedge_was_open = pos.hedge_entry_filled

        # Short: sold high (entry_px), bought back (short_exit_px) -- profit
        # when short_exit_px < entry_px. Hedge: bought (hedge_entry_px),
        # sold (hedge_exit_px) -- profit when hedge_exit_px > hedge_entry_px.
        short_pnl_rupees = ((pos.entry_px - short_exit_px) * pos.quantity
                            if short_was_open else 0.0)
        hedge_pnl_rupees = ((hedge_exit_px - pos.hedge_entry_px) * pos.quantity
                            if hedge_was_open else 0.0)
        total_pnl_rupees = short_pnl_rupees + hedge_pnl_rupees
        self.engine.state.today_realized_pnl += total_pnl_rupees

        now_iso = datetime.now(IST).isoformat()
        # One row per leg that was genuinely OPEN. Logging a leg that never
        # filled would put a phantom trade in the platform's trade book.
        if short_was_open:
            append_trade_log(self.engine.env.strategy_tag, self.leg_key, pos.short_symbol, pos.quantity,
                              pos.entry_time, pos.entry_px, now_iso, short_exit_px, reason,
                              pos.execution_id, is_short=True)
        if hedge_was_open:
            append_trade_log(self.engine.env.strategy_tag, f"{self.leg_key}_HEDGE", pos.hedge_symbol,
                              pos.quantity, pos.entry_time, pos.hedge_entry_px, now_iso, hedge_exit_px,
                              reason, pos.execution_id, is_short=False)
        Log.info(f"[MONTHLY_{self.option_type}] Position closed: short={pos.short_symbol} "
                 f"hedge={pos.hedge_symbol} reason={reason} short_pnl={short_pnl_rupees:.2f} "
                 f"hedge_pnl={hedge_pnl_rupees:.2f} total_pnl={total_pnl_rupees:.2f}")

        self.engine.price_stream.remove_instruments([
            {"symbol": pos.short_symbol, "exchange": OPTIONS_EXCHANGE},
            {"symbol": pos.hedge_symbol, "exchange": OPTIONS_EXCHANGE},
        ])
        self.engine._notify_trade_closed_bg()
        reentry_search = pos.pending_reentry_search
        reentry_slot_hm = pos.reentry_slot_hm
        # ANY exit on the held contract's own expiry day makes the day
        # terminal for this leg, not just the scheduled square-off. Without
        # this, a stop loss at 12:00 on expiry day leaves the leg flat and the
        # next check's flat branch searches -- and resolve_monthly_expiry's
        # day>15 rule hands it NEXT month's contract, opening a position a day
        # before that cycle is allowed to start, which then suppresses the
        # cycle-start day's own entry evaluation.
        today_iso = datetime.now(IST).date().isoformat()
        # The captured cycle expiry, for the same reason as the square-off.
        on_an_expiry_date = (pos.expiry_date and pos.expiry_date == today_iso) or (
            leg.marked_expiry == today_iso)
        # Terminal only for the SQUARE-OFF itself, or for an exit at/after its
        # slot. A stop loss at 12:00 on the calendar expiry date must NOT
        # block the 13:30 re-entry, because entries are allowed that day right
        # up to the square-off.
        if on_an_expiry_date and (reason == "expiry_day_close"
                                  or datetime.now(IST).time() >= config.expiry_close_time):
            # ANY exit on the marked expiry's own date makes the day terminal
            # for this leg, not just the scheduled square-off. Without this a
            # stop loss at 12:00 on that date leaves the leg flat, and the next
            # check's flat branch searches -- past day 15 that search resolves
            # to the FOLLOWING month's contract, opening a position a day
            # before its cycle starts and then suppressing that cycle-start
            # day's own entry evaluation.
            leg.expiry_closed_date = today_iso
            Log.info(f"[MONTHLY_{self.option_type}] exit fell on an expiry date "
                     f"({today_iso}) -- no further search or entry today.")
        leg.position = ShortHedgePosition()
        self.engine.save_state()

        # An exit that fell on an expiry date already marked the day terminal
        # above; honour that here too. Otherwise the leg logs "no further
        # search or entry today" and then immediately dispatches one.
        if reentry_search and leg.expiry_closed_date == today_iso:
            Log.info(f"[MONTHLY_{self.option_type}] re-entry suppressed -- the day is "
                     f"terminal for this leg ({today_iso}).")
            reentry_search = False

        if reentry_search:
            # Spec item 10's "close short, then hedge, then search for a
            # new qualifying strike and re-enter if one qualifies" --
            # backgrounded like every other entry-eval call (see
            # _maybe_evaluate_entry_bg's identical rationale).
            slot = None
            if reentry_slot_hm:
                try:
                    hh, mm = (int(x) for x in reentry_slot_hm.split(":"))
                    slot = time(hh, mm)
                except ValueError:
                    Log.warning(f"[{self.leg_key}] unreadable reentry slot "
                                f"{reentry_slot_hm!r} -- anchoring to the last check.")
            self._dispatch_bg(lambda: self._search_and_enter_fresh(slot))

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
                # Cancel on a terminal ENTRY-side error: give up on this
                # attempt. _abandon_attempt reads what is actually filled --
                # under hedge-first, entry_failed means a LONG is live and
                # must be closed, not discarded, and hedge_entry_failed means
                # nothing is live and the leg resets. Deciding that from the
                # error state's NAME is what broke when the order reversed.
                self.engine._push_leg_error_bg(self.leg_key, self.leg.position, clear=True)
                ack_pending_action(self.engine.env, self.leg_key)
                self._abandon_attempt("cancelled", user_initiated=True)
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
                # Hedge-first: a manually-completed HEDGE leaves the position
                # half built, so the short still has to be placed. Backgrounded
                # under _pending_fills like every other order chain.
                if pos.short_symbol and not pos.entry_filled and not pos.entry_order_id:
                    short_symbol = pos.short_symbol
                    self.engine._pending_fills.add(self.leg_key)

                    def _continue_into_short():
                        try:
                            self._place_and_watch_short(short_symbol)
                        finally:
                            self.engine._pending_fills.discard(self.leg_key)

                    self.engine._fill_executor.submit(_continue_into_short)
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
                # The hedge is the FIRST leg now, so resuming it must carry on
                # into the short afterwards -- _watch_entry_fill is exactly
                # that chain, starting from a hedge order id. Resuming the
                # hedge alone would leave short_symbol set with nothing filled,
                # which evaluate()'s "still entering" guard freezes forever.
                self.engine._fill_executor.submit(
                    self._watch_entry_fill, resume_order_id, pos.short_symbol, symbol
                )
            else:
                # entry_failed: the HEDGE is already filled and only the short
                # needs confirming, so resume the short alone. Handing this
                # order id to _watch_entry_fill would poll it as a BUY on the
                # hedge symbol, and the hedge_entry_order_id guard inside would
                # then return silently -- a live short whose fill is never
                # recorded.
                def _watch_short_only():
                    try:
                        fill = poll_fill(self.engine.client, resume_order_id,
                                         self.engine.env.strategy_tag, symbol,
                                         OPTIONS_EXCHANGE, "SELL", quantity)
                        p = self.leg.position
                        if p.entry_order_id != resume_order_id:
                            return
                        p.entry_px = float(fill.get("average_price") or fill.get("price") or 0.0)
                        p.entry_filled = True
                        self.engine.price_stream.add_instruments(
                            [{"symbol": symbol, "exchange": OPTIONS_EXCHANGE}]
                        )
                        self.engine.save_state()
                        Log.info(f"[MONTHLY_{self.option_type}] Retry-resumed short filled: "
                                 f"{symbol}@{p.entry_px} -- position fully open.")
                    except OrderNeedsAttention as exc:
                        self._enter_error_mode(resume_order_id, "entry_failed", "resting",
                                               exc.order_id, str(exc))
                    except (RuntimeError, TimeoutError) as exc:
                        self._enter_error_mode(resume_order_id, "entry_failed", "terminal", "", str(exc))
                    except Exception as exc:
                        Log.exception(f"[MONTHLY_{self.option_type}] Retry-resumed short fill-poll "
                                      f"failed: {exc}")
                        self._enter_error_mode(resume_order_id, "entry_failed", "resting",
                                               resume_order_id, str(exc))
                    finally:
                        self.engine._pending_fills.discard(self.leg_key)
                self.engine._fill_executor.submit(_watch_short_only)
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
                if is_hedge:
                    # The hedge is the FIRST leg: it filling means the position
                    # is only half built, so the chain must carry on into the
                    # short. Clearing the error here and stopping (which is
                    # what this did) left short_symbol set with nothing filled
                    # -- frozen by evaluate()'s "still entering" guard, with no
                    # stop loss and no exit.
                    Log.info(f"[MONTHLY_{self.option_type}] hedge filled on the final chance -- "
                             f"continuing into the short leg.")
                    self._place_and_watch_short(pos.short_symbol)
            else:
                try:
                    self.engine.client.cancelorder(order_id=order_id, strategy=self.engine.env.strategy_tag)
                except Exception as exc:
                    Log.warning(f"[MONTHLY_{self.option_type}] cancelorder failed while abandoning entry "
                                f"({exc}) -- proceeding; verify manually at the broker.")
                if is_hedge:
                    pos.hedge_entry_order_id = ""
                pos.error_state = ""
                pos.error_kind = ""
                pos.error_order_id = ""
                self.engine.save_state()
                # Closes whatever is genuinely filled before resetting -- under
                # hedge-first, an unfilled SHORT order leaves a live long that
                # a blind reset would have erased.
                self._abandon_attempt("cancel_final_chance", user_initiated=True)
            # Only clear the UI error if nothing raised a NEW one in between --
            # _place_and_watch_short and _abandon_attempt can both fail, and
            # clearing then hid the error locally with nothing shown to the
            # operator to resolve.
            if not self.leg.position.error_state:
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
                 execution_id: int, ltp_client, hedge_chain_client=None):
        self.client = client
        self.hedge_chain_client = hedge_chain_client
        self.state_store = state_store
        self.state = state_store.state
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id
        self.ltp_client = ltp_client

        self.monthly = {ot: MonthlyShortEngine(ot, self) for ot in OPTION_TYPES}

        self._monthly_expiry_cache: Optional[tuple] = None

        self._cm_expiry_date_cache: Optional[tuple] = None
        self._cycle_expiry_date_cache: Optional[tuple] = None
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
        # Date a gap day's wait for the 09:45 candle was logged; until then
        # _ensure_reference returns at once instead of re-measuring every tick.
        self._gap_wait_day: str = ""
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

    def get_cycle_expiry_date(self) -> Optional["datetime.date"]:
        """Cached per calendar day. Any failure returns None and is logged --
        a None here means "unknown", never "not an expiry day"."""
        today = datetime.now(IST).date().isoformat()
        if self._cycle_expiry_date_cache and self._cycle_expiry_date_cache[0] == today:
            return self._cycle_expiry_date_cache[1]
        try:
            d = resolve_cycle_expiry_date(self.client)
        except Exception as exc:
            Log.warning(f"resolve_cycle_expiry_date failed: {exc}")
            return None
        self._cycle_expiry_date_cache = (today, d)
        Log.info(f"Cycle expiry date resolved: {d}")
        return d

    def get_current_month_expiry_date(self) -> Optional["datetime.date"]:
        """Cached per calendar day, like get_cycle_start_date. A resolution
        failure returns None and is logged, never guessed."""
        today = datetime.now(IST).date().isoformat()
        if self._cm_expiry_date_cache and self._cm_expiry_date_cache[0] == today:
            return self._cm_expiry_date_cache[1]
        try:
            d = resolve_current_month_expiry_date(self.client)
        except Exception as exc:
            # Any failure, not just RuntimeError. This is called from
            # run_cycle and from _finalize_exit, so a transport error escaping
            # here aborted the whole cycle -- the continuous stop loss with it
            # -- on every tick, and could abort startup via
            # reconcile_pending_orders.
            Log.warning(f"resolve_current_month_expiry_date failed: {exc}")
            return None
        self._cm_expiry_date_cache = (today, d)
        Log.info(f"Current calendar month expiry resolved: {d}")
        return d

    def get_cycle_start_date(self) -> Optional["datetime.date"]:
        """First trading day of the current cycle, derived by looking FORWARD.

        The broker's expiry list only ever carries dates at or after today, so
        the previous expiry cannot be read from it -- which is why the old
        backward-looking resolver returned None on every day. Instead, the
        cycle's expiry is observed each day (forward-only, the same principle
        resolve_hedge_monthly_expiry relies on) and persisted. On the first
        trading day that stored date is in the past, the cycle has ended and
        the new one starts on the first trading day after it.

        Cached per calendar day; persisted so a restart on the cycle-start day
        still recognises it. None until one cycle expiry has been seen to pass
        -- correct on first deployment, which never lands on a cycle start
        with no history behind it."""
        today = datetime.now(IST).date()
        today_key = today.isoformat()
        if self._cycle_start_date_cache and self._cycle_start_date_cache[0] == today_key:
            return self._cycle_start_date_cache[1]

        seen = None
        if self.state.cycle_expiry_seen:
            try:
                seen = datetime.fromisoformat(self.state.cycle_expiry_seen).date()
            except ValueError:
                Log.warning(f"Unreadable stored cycle expiry "
                            f"{self.state.cycle_expiry_seen!r} -- ignoring it.")
                seen = None

        changed = False
        if seen is not None and seen < today:
            # The cycle that expired on `seen` is over.
            start = _cycle_next_trading_day(seen)
            if self.state.cycle_start_date != start.isoformat():
                self.state.cycle_start_date = start.isoformat()
                changed = True
                Log.info(f"Cycle that expired {seen} has ended -- new cycle starts {start}.")

        # Advance the observed expiry only once the previous one has passed,
        # so it always names the RUNNING cycle's expiry.
        if seen is None or seen < today:
            current = self.get_cycle_expiry_date()
            if current is not None and self.state.cycle_expiry_seen != current.isoformat():
                self.state.cycle_expiry_seen = current.isoformat()
                changed = True
        if changed:
            self.save_state()

        cycle_start = None
        if self.state.cycle_start_date:
            try:
                cycle_start = datetime.fromisoformat(self.state.cycle_start_date).date()
            except ValueError:
                cycle_start = None
        self._cycle_start_date_cache = (today_key, cycle_start)
        Log.info(f"Cycle start date resolved: {cycle_start} "
                 f"(cycle expiry observed: {self.state.cycle_expiry_seen or 'none yet'})")
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
            self._cm_expiry_date_cache = None
            self._cycle_expiry_date_cache = None
            clear_reference_cache()
            clear_monthly_expiry_cache()
            self.save_state()

    def _ensure_reference(self):
        """Reused unchanged from Nifty_OI_WeeklyBuy_MonthlySell -- computed
        once, fixed for the rest of the day."""
        ref = self.state.reference
        today_key = datetime.now(IST).date().isoformat()
        if ref.reference_date == today_key and ref.computed:
            return

        now = datetime.now(IST).time()
        # +config.decision_buffer_min, same as every other scheduled read: the
        # previous day's closing candle is long published, but the gap test
        # also needs today's spot and the broker can lag a moment at the open.
        if now < _with_buffer(config.reference_check_time):
            return  # too early, wait for 09:30 (+buffer)

        # A gap day is decided at 09:30 (+buffer); after that there is nothing
        # to check until the 09:45 candle has closed. Return here without
        # re-reading spot or the previous close, and without logging -- the
        # wait used to be re-measured and printed every 10s (90 identical
        # lines on 04-Nov-2026). At 09:46 the code below runs once and
        # re-measures the gap, which decides the mode. One difference from
        # before: the old loop would switch to the previous-close reference
        # at the first tick in 09:31-09:45 where the gap dipped to 0.5% or
        # less; now only the 09:46 reading can do that.
        if self._gap_wait_day == today_key and now < _with_buffer(config.reference_wait_time):
            return

        spot_now = self.get_spot_ltp()
        if spot_now is None:
            Log.warning("[Reference] spot LTP unavailable, retrying next cycle.")
            return

        prev_day = resolve_previous_trading_day(datetime.now(IST).date())
        # pytz timezones must be applied via .localize(), never passed as a
        # naive tzinfo= argument -- see Nifty_OI_WeeklyBuy_MonthlySell's
        # identical comment for the LMT-offset corruption this avoids.
        # Derived from config.market_close, not a literal 15:30: the session
        # ends at 15:40, and asking for the candle at-or-before 15:30 would
        # take a reference that ignores the last ten minutes of the previous
        # day -- every premium_change and oi_change all day is measured from
        # this one number.
        # 1-minute candles of prev_day only -- see fetch_previous_session_close
        # for why the 15-minute candle is not the previous close on Shoonya.
        prev_close_data = fetch_previous_session_close(self.client, prev_day)
        if prev_close_data is None:
            Log.warning("[Reference] previous day's close unavailable, retrying next cycle.")
            return
        prev_close = prev_close_data["close"]

        gap_pct = (spot_now - prev_close) / prev_close * 100.0
        # Both inputs in the log, so a wrong gap can be checked from the log
        # alone: yesterday's close with the candle it came from, and NIFTY's
        # price with the time it was read -- 09:31 for the 09:30 check, 09:46
        # for the 09:45 one on a gap day (author 2026-11-05).
        prev_label = prev_close_data.get("label") or prev_close_data["timestamp"]
        gap_inputs = (f"yesterday close={prev_close:.2f} ({prev_label}) | "
                      f"NIFTY at {datetime.now(IST).strftime('%H:%M:%S')}={spot_now:.2f}")

        if abs(gap_pct) <= config.gap_threshold_pct:
            ref.reference_date = today_key
            ref.gap_pct = gap_pct
            ref.mode = "prev_close"
            ref.reference_time_iso = prev_day.isoformat()
            ref.computed = True
            Log.info(f"[Reference] gap={gap_pct:+.3f}% (<= {config.gap_threshold_pct}%) "
                     f"[{gap_inputs}] -> Reference Time = previous day's close ({prev_day})")
            return

        # The gap path's 09:45 read is a CANDLE read, so it takes the buffer
        # too -- asking at exactly 09:45:00 can return the 09:40 bucket.
        # ref.reference_time_iso below stays anchored at 09:30: the buffer
        # moves when we look, never which candle we want.
        if now < _with_buffer(config.reference_wait_time):
            if self._gap_wait_day != today_key:
                self._gap_wait_day = today_key
                Log.info(f"[Reference] gap={gap_pct:+.3f}% (> {config.gap_threshold_pct}%) "
                         f"[{gap_inputs}] -- reference will be fixed when the "
                         f"{config.reference_wait_time.strftime('%H:%M')} candle closes; "
                         f"no further checks until then.")
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
        Log.info(f"[Reference] gap={gap_pct:+.3f}% (> {config.gap_threshold_pct}%) "
                 f"[{gap_inputs}] -> Reference Time = today 09:30 (5m bootstrap)")

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
                # The SL needs no reference -- it compares LTP to entry_px --
                # and on a gap day the reference is not fixed until 09:45.
                # Returning outright here left an open position unwatched for
                # that whole window, which moving reference_wait_time from
                # 09:35 to 09:45 made ten minutes longer. Everything that DOES
                # need the reference (entry search, verdict re-check) still
                # waits for it.
                for ot in OPTION_TYPES:
                    self.monthly[ot].manage_sl_only()
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
            # Walked in the order the legs are PLACED -- hedge first since
            # 2026-11-01. Reversing the entry without reversing this chain
            # would misread where a crash landed.
            if pos.hedge_entry_order_id and not pos.hedge_entry_filled:
                self._reconcile_one(leg_key, leg, pos, pos.hedge_entry_order_id, "hedge_entry_failed",
                                     pos.hedge_symbol, "BUY")
            elif pos.entry_order_id and not pos.entry_filled:
                self._reconcile_one(leg_key, leg, pos, pos.entry_order_id, "entry_failed", pos.short_symbol, "SELL")
            elif pos.short_exit_order_id and not pos.short_exit_filled:
                self._reconcile_one(leg_key, leg, pos, pos.short_exit_order_id, "exit_failed", pos.short_symbol, "BUY")
            elif pos.hedge_exit_order_id and not pos.hedge_exit_filled:
                self._reconcile_one(leg_key, leg, pos, pos.hedge_exit_order_id, "hedge_exit_failed",
                                     pos.hedge_symbol, "SELL")
            elif pos.hedge_symbol and not pos.hedge_entry_order_id and not pos.hedge_entry_filled:
                # The narrow crash window: _enter() persists the leg as
                # "attempting entry" (both symbols set, no order id yet)
                # BEFORE calling place(). Since 2026-11-01 the FIRST order
                # placed is the hedge BUY, so this is the window that one
                # opens. Never guess -- flag for a human to verify against
                # the broker directly.
                pos.error_state = "hedge_entry_failed"
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
                push_leg_error(self.env, leg_key, pos, action="BUY")
                self.save_state()
                Log.error(f"[{leg_key}] reconcile: ambiguous pre-placeorder crash window -- "
                          f"flagged for manual verification against the broker.")
            elif pos.hedge_entry_filled and not pos.entry_order_id and not pos.entry_filled:
                # Hedge filled, then the process stopped before the short's
                # order id was recorded. The leg holds a LONG option and
                # nothing else; evaluate() would freeze it forever on its
                # "still entering" guard, with no stop loss and no exit.
                # (The mirror of this existed under the old short-first order
                # too -- a filled short with no hedge order id -- and was not
                # handled; it is covered by the same branch shape now.)
                pos.error_state = "entry_failed"
                pos.error_kind = "terminal"
                pos.error_order_id = ""
                pos.error_message = (
                    "Restart interrupted this leg after the HEDGE filled but before the "
                    "short's order was recorded -- the account may hold a long option with "
                    "no short against it, and an order for that short may or may not exist. "
                    "Verify against the broker before choosing Retry (risks a duplicate "
                    "short) -- prefer Manually Completed with the real fill price if the "
                    "short did go through, or Cancel to close the hedge out."
                )
                pos.error_since = datetime.now(IST).isoformat()
                push_leg_error(self.env, leg_key, pos, action="SELL")
                self.save_state()
                Log.error(f"[{leg_key}] reconcile: hedge filled but the short was never "
                          f"recorded -- flagged for manual verification.")

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
            if error_state in ("entry_failed", "hedge_entry_failed"):
                # An ENTRY-side order that genuinely never filled. What to do
                # depends on what the leg actually HOLDS, not on which of the
                # two states it is -- hedge-first inverted that mapping, and
                # the previous code's "short leg remains open and naked, will
                # retry the hedge next cycle" was wrong twice over: the short
                # had never been placed, and nothing retries it, because
                # evaluate()'s "still entering" guard freezes a leg whose
                # symbols are set with nothing filled.
                side = self.monthly[leg_key.split("_")[1]]
                if not pos.entry_filled and not pos.hedge_entry_filled:
                    leg.trade_count = max(0, leg.trade_count - 1)
                    syms = [{"symbol": sym, "exchange": OPTIONS_EXCHANGE}
                            for sym in (pos.short_symbol, pos.hedge_symbol) if sym]
                    if syms:
                        self.price_stream.remove_instruments(syms)
                    leg.position = ShortHedgePosition()
                    Log.info(f"[{leg_key}] reconcile: entry order {order_id} genuinely "
                             f"rejected/cancelled and nothing is filled -- clearing the leg, "
                             f"safe to re-evaluate fresh.")
                    self.save_state()
                    return
                Log.warning(f"[{leg_key}] reconcile: entry order {order_id} genuinely "
                            f"rejected/cancelled, but the leg HOLDS a filled leg "
                            f"(short={pos.entry_filled}, hedge={pos.hedge_entry_filled}) -- "
                            f"closing it out rather than discarding the position.")
                side._abandon_attempt("reconcile_rejected")
                return
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
        # Each leg reports INDEPENDENTLY of the other's fill state. This used
        # to skip the whole position unless the SHORT had filled, which was
        # safe only while the short was placed first. Since 2026-11-01 the
        # HEDGE is bought first, so that gate hid a genuinely filled long
        # option for the whole window between the two fills -- and hid it
        # permanently whenever the short then failed and the leg went to error
        # recovery still holding the hedge. A position the account holds must
        # appear in PnL whatever order it was built in.
        open_positions = []
        for leg_key, leg in self.state.legs.items():
            pos = leg.position
            if pos.short_symbol and pos.entry_filled:
                short_ltp = self.price_stream.get_ltp(pos.short_symbol, OPTIONS_EXCHANGE,
                                                      config.ws_stale_seconds)
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

        Ordering: this routes through _exit() like every other close, so it
        is short BUY first, then hedge SELL -- the reverse of the entry's
        hedge-then-short, and correct for the same reason. Closing the naked
        (undefined-risk) leg first means the remaining seconds are spent
        holding a long option; selling the hedge first would leave the short
        unhedged exactly when something is already going wrong enough to
        warrant a force exit. Verified 2026-11-01 when the entry order was
        reversed: all four close reasons (stop_loss, opposite_signal,
        expiry_day_close, force_exit) share this one chain, so none of them
        needed changing."""
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
    print("Order sequence       : ENTRY hedge BUY -> short SELL | "
          "EXIT short BUY -> hedge SELL")
    print(f"Decision buffer      : +{config.decision_buffer_min} min after each "
          f"scheduled time (candle asked for is still the scheduled one)")
    print(f"Current OI source    : {'live quote/chain' if config.use_live_oi_for_current else 'closed candle'}"
          f"   (reference: closed candle, cached once per day)")
    print(f"Entry evaluation time: {config.entry_evaluation_time.strftime('%H:%M')} (cycle-start day only)")
    if config.expiry_close_time not in config.daily_decision_times:
        raise SystemExit(
            f"config error: expiry_close_time "
            f"{config.expiry_close_time.strftime('%H:%M')} is not one of the position "
            f"checks ("
            + ", ".join(t.strftime('%H:%M') for t in config.daily_decision_times)
            + "). The expiry-day square-off runs inside a position check, so a time "
              "that is not a check would never fire and the position would be carried "
              "into expiry unmanaged."
        )
    print("Position checks      : "
          + " ".join(t.strftime('%H:%M') for t in config.daily_decision_times)
          + " (non-entry days)")
    print(f"Short when verdict   : {config.short_verdict}   "
          f"(hold while {config.hold_verdict})")
    print(f"SL check window      : {config.sl_check_start_time.strftime('%H:%M')}"
          f"-{config.sl_check_end_time.strftime('%H:%M')}")
    print(f"Expiry-day square-off: {config.expiry_close_time.strftime('%H:%M')} "
          f"(held contract's own expiry date)")
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
    hedge_chain_client = broker.connect_hedge_chain_client()

    price_stream = PriceStream(client)

    already_known = [{"symbol": UNDERLYING_SYMBOL, "exchange": UNDERLYING_SPOT_EXCHANGE}]
    # NOT gated on current_day == today: current_day only tracks the daily
    # reset (set once a scheduled cycle actually runs, not yet at this point
    # in startup), unrelated to whether an open position still exists. This
    # strategy holds positions for weeks (monthly expiry), so on any restart
    # after the entry day -- i.e. virtually every restart -- current_day is
    # still whatever day was last saved, never "today" yet, and gating on it
    # here silently skipped re-subscribing every open leg's own option
    # symbols to the price feed. Confirmed live 2026-08-28 on the sibling
    # LEAPS strategy (identical pattern): LTP froze at entry price and PnL
    # stayed at Rs0 for days once the price stream lost the symbol. Whether
    # a leg needs resubscribing depends only on whether it actually has an
    # open position right now.
    for leg in state_store.state.legs.values():
        if leg.position.short_symbol:
            already_known.append({"symbol": leg.position.short_symbol, "exchange": OPTIONS_EXCHANGE})
        if leg.position.hedge_symbol:
            already_known.append({"symbol": leg.position.hedge_symbol, "exchange": OPTIONS_EXCHANGE})
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
    # The previous-close reference and the cycle start both depend on the
    # platform's holiday list. If it cannot be read, the calendar quietly
    # degrades to weekends-only (it logs, never raises), and a day after a
    # holiday would read the holiday's empty history -- so say so at startup.
    try:
        from utils.trading_calendar import trading_holidays as _th
        _n_hol = len(_th(datetime.now(IST).date().year))
    except Exception:
        _n_hol = -1
    if _n_hol > 0:
        print(f"Trading Calendar   : OK ({_n_hol} holidays this year)")
    else:
        print("Trading Calendar   : NOT LOADED -- weekends only; the day after a holiday "
              "will find no reference candle")
    print("=" * 70)

    engine = StrategyEngine(client, state_store, env, price_stream, execution_id=execution_id,
                             ltp_client=ltp_client, hedge_chain_client=hedge_chain_client)

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
