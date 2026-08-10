"""
===============================================================================
Nifty & Sensex Expiry-Day Straddle + Repair ("Batman")
===============================================================================
Version     : 1.3.0
Platform    : OpenAlgo Hosted Strategy
OpenAlgo    : >= 2.0.1.5
Python      : >= 3.11
Ported from : Tradetron export "Nifty_sensex_Expiry_batman (1).json"
              -- see Nifty_Sensex_Expiry_Batman_ANALYSIS.md for the full
              decoded-logic writeup and the open design decision below.

*** THIS STRATEGY IS NOT A NAKED SELLER LIKE THE OTHER THREE IN THIS
PROJECT. IT BUYS A LONG STRADDLE, THEN CONVERTS PART OF IT INTO A NAKED
SHORT POSITION VIA A "REPAIR" LEG. RISK PROFILE CHANGES MID-TRADE. ***

Description
-----------
Runs ONLY on the underlying's own expiry day (NIFTY Thursday-ish weekly,
SENSEX its own weekly expiry -- whatever `client.expiry()` resolves as the
nearest upcoming expiry, checked against today's date). Two independent
instrument tracks (NIFTY, SENSEX), each entering/exiting once per day:

1. ENTRY (once/day/instrument, after 09:25, only if today == that
   instrument's own current-week expiry date): BUY 1 lot ATM PE + 1 lot ATM
   CE (a long straddle), NRML. The CONTRACT actually traded is the CURRENT
   (expiring-today) week's expiry -- per explicit instruction, reverting an
   earlier next-week-roll choice for this strategy specifically. That
   earlier choice existed because a same-day-expiring contract has minimal
   time value left and an extreme gamma/theta cliff in its final hours,
   and next week's contract backtested a smaller worst-day/max-drawdown at
   a modest cost to total return (see ANALYSIS.md) -- that finding still
   stands, this is a deliberate choice to trade the current contract
   anyway. `resolve_current_week_expiry()` now serves both roles: gating
   entry (is today this instrument's own expiry day?) AND resolving the
   actual traded contract -- the separate `resolve_next_week_expiry()`
   helper has been removed since nothing calls it anymore.

2. REPAIR (once per SIDE per day, PE and CE independently): the CYCLE
   right after the straddle leg on that side fills (no artificial delay --
   per explicit instruction, the short leg goes on immediately, not after
   any wait), search the (current-week-expiry) option chain for whichever
   strike is CURRENTLY trading closest to `entry_price / repair_n`
   (repair_n = 4 for NIFTY, 3 for SENSEX) and SELL `repair_n` lots of it.
   The source JSON's "Repair Once" block has NO explicit trigger condition
   -- see the ANALYSIS.md design-decision section for the full history of
   this choice.
   This is a real options "repair" technique: N lots at ~1/N the original
   premium collects roughly the FULL original premium back, but the moment
   it fires, that side's risk profile changes from "long option, risk
   capped at premium paid" to "long 1 lot + short N lots at a further OTM
   strike" -- i.e. a ratio spread with UNDEFINED risk on continued adverse
   moves past the short strike. Fires at most once per side per day.

3. UNIVERSAL EXIT (per instrument, force-closes EVERY open leg -- both
   straddle legs and any fired repair legs): aggregate unrealized PnL across
   all of that instrument's open legs <= -Rs 5,000, OR time >= 15:25.

Live price feed: WebSocket, not REST polling (v1.3.0)
------------------------------------------------------------------------
Same rationale as the other three strategies (see Pivot+Supertrend's
module docstring for the full writeup of why Shoonya's `multiquotes()` in
this OpenAlgo installation is NOT a true batch call). Batman needs BOTH a
fixed and a dynamic subscription set, unlike either of the other patterns
used elsewhere in this project:
  - The 2 underlyings' LTP is needed continuously, EVERY cycle, all day,
    on every non-expiry day too (used only for ATM strike selection at
    entry, but the old REST fetch ran unconditionally every cycle while
    `not inst_state.traded_today` -- which is true all day on a non-expiry
    day, since entry never fires). This was actually the single most
    wasteful REST-polling spot in this whole project family: continuous
    per-cycle polling with a payoff of "maybe used once, on 1-in-5ish
    days." `PriceStream.add_instruments()` is called once at startup for
    both underlyings, same fixed-list treatment as Pivot+Supertrend.
  - The option legs' own LTP (needed live for the aggregate unrealized-PnL
    universal-exit check) is NOT known until each leg's symbol is actually
    resolved at runtime -- the straddle PE/CE symbols at entry
    (`_enter_straddle`), the repair PE/CE symbols only if/when repair
    fires (`_maybe_fire_repair`) -- so those are added dynamically,
    exactly like the VWAP strategy's ATM-lock pattern. A same-day restart
    resuming mid-session (any leg already `entry_filled` and not `closed`
    in the loaded state) also gets its symbol added at startup, same
    resumability guarantee as VWAP.
  - A closed/abandoned leg's symbol IS explicitly unsubscribed via
    `PriceStream.remove_instruments` (`_finalize_close`, `_resolve_leg_error`'s
    terminal-cancel branch, and `_watch_entry_cancel`'s abandon path) --
    without this, a long-running process accumulates dead subscriptions
    that the watchdog eventually escalates into full reconnects.
  - Same per-symbol staleness detection/resubscribe and SDK
    `auto_reconnect` handling as the other three strategies.

Order placement robustness (v1.3.0)
------------------------------------------------------------------------
Same underlying bug as the other three strategies, but Batman has THREE
separate entry/exit code paths (straddle entry, repair entry, force-close
exit), each with its own instance of it -- and the repair-entry case was
actually WORSE than a simple infinite-repoll loop:
  - `_maybe_fire_repair` set `repair_leg.fired = True` BEFORE placing the
    order. Its own guard is `if repair_leg.fired or not
    straddle_leg.entry_filled: return` -- so a rejected/cancelled repair
    order used to PERMANENTLY disable that side's repair for the rest of
    the day (the function would never even be called again), not just
    loop on a dead order id. Fixed: on rejection, the whole `repair_leg`
    resets to a blank `RepairLeg()` (fired=False included) so the NEXT
    cycle's `_maybe_fire_repair` call re-attempts search-and-sell from
    scratch.
  - `_enter_straddle`: a rejected/cancelled entry order now resets JUST
    that side's leg (`inst_state.pe` or `inst_state.ce`) to blank, so the
    next cycle re-picks the ATM strike and places a fresh order for that
    side specifically -- the OTHER side's already-filled leg (if only one
    side was rejected) is left untouched.
  - `_close_open_leg` (used for both universal-exit paths, and both LONG
    straddle legs and SHORT repair legs): a rejected/cancelled exit order
    clears only `exit_order_id` (the position is still open at the
    broker) so the next cycle places a brand-new close order. A repeated
    failure is surfaced via the error-mode/Retry-Cancel-Manual system (see
    `_enter_error_mode`), not a local counter -- same highest-risk-failure
    treatment as the other four strategies' naked short/naked leg cases,
    here applying to BOTH the long straddle legs and the short repair legs.
  - `place()` retries up to 3 attempts (1.5s apart) before raising. A
    `TimeoutError` from `poll_fill` now means it already tried RE-PRICING
    the stale order (via `modifyorder()`, to the current LTP) up to
    `config.reprice_max_attempts` times, then cancelled it -- so it's
    treated the same as a rejection (clear and retry fresh). See
    `poll_fill`'s own docstring for the full mechanism.

Persistent trade log (state.json intentionally left minimal)
------------------------------------------------------------------
Same pattern as the other three strategies: `state.json` tracks each leg's
CURRENTLY open position only. `append_trade_log()` writes one row per
closed leg to `trades_{STRATEGY_ID}.csv` via a background thread. Unlike
the other three (all naked SHORT sellers), this strategy has a MIX of long
(straddle) and short (repair) legs, so the trade log now carries an
explicit `direction` column ("LONG"/"SHORT") so PnL sign is computed
correctly for both (`exit_px - entry_px` for LONG, `entry_px - exit_px`
for SHORT).

Design notes carried over from the other three strategies in this family
------------------------------------------------------------------------
  - Resumable, leg-by-leg entry/exit via persisted order IDs.
  - State file unique per strategy_tag (STRATEGY_ID), anchored to this
    script's own directory.
  - `client.history()`/`quotes()`/`optionchain()` can return an error dict
    instead of the expected shape on a bad broker session -- handled
    explicitly.

Notes / Assumptions (please verify against your installed `openalgo` SDK):
  * `client.expiry(symbol=, exchange=, instrumenttype='options')` returns
    dates in "DD-MMM-YY" format; the nearest upcoming one is treated as
    "this instrument's current week expiry" for both the entry-day gate
    and the option leg's own expiry.
  * `client.optionchain(underlying=, exchange=, expiry_date=, strike_count=)`
    -> chain with per-strike PE/CE `symbol`/`ltp`/`lotsize`. The repair
    leg's chain fetch uses a wider `strike_count` than the entry's ATM
    fetch, since a strike trading at ~1/4 of ATM premium is typically much
    further OTM than the default ATM window.
  * `client.placeorder(...)` uses `price_type` per the official Python
    library docs/README.

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

# Python's default thread stack reservation is 8MB. This process runs
# several threads at once (fill-watchers, the Force Exit background check,
# repair-fire dispatches, PnL push, the trade-log writer, plus
# PriceStream's own watchdog/WS threads) -- none of which do anything
# beyond simple polling loops and REST calls, nowhere near deep recursion.
# At the default size that adds up to tens of MB of virtual address space
# reserved purely for stacks, out of the STRATEGY_MEMORY_LIMIT_MB
# RLIMIT_AS cap (blueprints/python_strategy.py's set_resource_limits(),
# 1024MB by default) every strategy subprocess runs under -- confirmed in
# production as the actual ceiling behind "RuntimeError: can't start new
# thread" (2026-07-28, on this script's own repair-fire dispatch, and on
# the Combined script specifically). Must be called before any thread is
# created; affects every threading.Thread from here on, including ones
# spawned internally by ThreadPoolExecutor.
threading.stack_size(1024 * 1024)  # 1MB, generous for these workloads

try:
    from _strategy_platform_client import notify_trade_closed, notify_whatsapp_error, filter_known_fields
except ImportError:
    # Shared helper (strategies/scripts/_strategy_platform_client.py) not
    # present alongside this script -- e.g. it was copied out standalone.
    # Degrade gracefully: the live "trade just closed" SSE push and WhatsApp
    # failure alerts simply won't fire, but nothing else about the strategy
    # is affected.
    def notify_trade_closed(env, log_warning=None):
        pass

    def notify_whatsapp_error(env, message, log_warning=None):
        pass

    # Same behavior as the shared module's version (see its own docstring) --
    # trivial and dependency-free enough to duplicate exactly rather than
    # degrade, unlike notify_trade_closed above which genuinely needs
    # network/env access it can't have as a standalone fallback.
    def filter_known_fields(cls, raw):
        known = set(vars(cls()).keys())
        return {k: v for k, v in raw.items() if k in known}

load_dotenv()

print("🔁 OpenAlgo Python Bot is running.")

###############################################################################
# CONFIGURATION
###############################################################################
@dataclass
class InstrumentConfig:
    name: str                    # "NIFTY" / "SENSEX"
    underlying_exchange: str     # NSE_INDEX / BSE_INDEX
    options_exchange: str        # NFO / BFO
    repair_n: int                # lots sold in repair AND the divisor for entry_px/N target
                                  # (4 for NIFTY, 3 for SENSEX, per source)


INSTRUMENTS = [
    InstrumentConfig(name="NIFTY", underlying_exchange="NSE_INDEX", options_exchange="NFO", repair_n=4),
    InstrumentConfig(name="SENSEX", underlying_exchange="BSE_INDEX", options_exchange="BFO", repair_n=3),
]


@dataclass
class Config:
    strategy_name: str = "Nifty & Sensex Expiry-Day Straddle + Repair (Batman)"
    version: str = "1.2.0"

    entry_start: time = time(9, 25)             # entry fires only if now > this (strict, per source)
    universal_exit_time: time = time(15, 25)    # force-close everything at/after this
    universal_exit_pnl: float = -5000.0         # aggregate unrealized PnL floor per instrument
    market_close: time = time(15, 30)

    # The source JSON's "Repair Once" block has NO explicit trigger
    # condition. Per explicit instruction, the repair (short) leg fires the
    # very first cycle after the straddle leg on that side is fully filled
    # -- no artificial delay. See ANALYSIS.md for the design-decision history.
    repair_chain_strike_count: int = 30   # wider than the ATM entry's chain fetch -- a strike
                                           # priced at ~1/N of ATM premium is usually much further OTM

    entry_chain_strike_count: int = 10    # kept wide (unlike the other 4 strategies' entry
                                           # chain fetches) -- _enter_straddle fires only ONCE
                                           # per instrument per day, so the latency win from a
                                           # narrower chain is negligible here, while a wider
                                           # window gives pick_atm_leg's own min(|strike-spot|)
                                           # room to self-correct if this strategy's `spot`
                                           # snapshot is slightly stale vs. the backend's live
                                           # underlying LTP anchor at request time.

    lot_multiplier: int = 1           # lots per straddle leg (repair leg quantity = repair_n lots)

    product: str = "NRML"
    price_type: str = "MARKET"

    scheduler_interval: int = 10
    pnl_tick_interval: float = 0.8                # seconds between PnL pushes -- runs on its OWN scheduler
                                                  # job (see report_pnl_tick), decoupled from
                                                  # scheduler_interval, since it's cache-only/read-only and
                                                  # doesn't share the blocking-call risk that interval guards

    # WebSocket LTP cache: a tick older than this is treated as stale and
    # falls back to a one-off REST client.quotes() call for that symbol.
    ws_stale_seconds: float = 20.0
    # NIFTY.NSE_INDEX/SENSEX.BSE_INDEX only get a new tick when their
    # underlying constituents actually trade and the index recalculates --
    # in the first ~45 minutes after 09:15 this is naturally burstier than
    # the rest of the day, with legitimate gaps wider than 20s between
    # recalculations. At the normal threshold, the watchdog was
    # misdiagnosing that normal opening-minutes irregularity as a dead
    # connection and forcing repeated resubscribes/reconnects -- confirmed
    # in production (2026-07-29) to reach all the way to a real
    # Unsubscribe/resubscribe cycle at the broker adapter, which itself
    # takes a moment to recover, compounding the next check's gap. A wider
    # threshold during this specific window only affects how fast the
    # WATCHDOG reacts to a genuinely dead connection (up to
    # ws_stale_seconds_open instead of ws_stale_seconds before it notices
    # and reconnects) -- it does not affect get_ltp()'s REST-fallback
    # behavior, which stays at the tighter ws_stale_seconds everywhere.
    ws_stale_seconds_open: float = 60.0
    ws_post_open_grace_until: time = time(10, 0)
    ws_watchdog_interval: float = 15.0       # how often the reconnect watchdog checks staleness
    # Consecutive stale watchdog cycles (same symbol, in a row) before giving
    # up on the cheap per-symbol resubscribe and escalating to a full
    # reconnect -- confirmed in production that per-symbol resubscribe alone
    # can retry 30+ times with zero recovery while the connection stays
    # reported connected/authenticated, so something in the connection's own
    # state needs a clean reset, not another poke at the same symbol.
    ws_stale_reconnect_after: int = 3

    # report_pnl_tick()'s WS price cache can stay stale for an EXTENDED
    # period during a genuine broker-side outage (confirmed in production,
    # 2026-07-30: both the WS feed AND REST quotes() failed for a specific
    # NIFTY option contract for 2+ hours, while the broker's OWN historical
    # data endpoint kept working fine -- a real, if unusual, broker-side
    # partial outage). Previously such a leg just vanished from the pushed
    # PnL payload for the ENTIRE outage. Now falls back to a REST quotes()
    # call, throttled to at most once per this interval per leg -- frequent
    # enough to recover visibility within a reasonable window, rare enough
    # that a 1-second job doing this doesn't spam the broker for the whole
    # outage's duration.
    pnl_rest_fallback_interval_sec: float = 900.0   # 15 minutes

    fill_poll_interval: float = 2.0
    # 5s per wait-cycle (1 initial + 59 reprices) = 60 x 5s = 300s (5 min)
    # total before giving up and raising OrderNeedsAttention -- each reprice
    # crosses the spread with a fresh bid/ask (see _reprice_and_wait_once),
    # not just the last-traded price.
    fill_poll_timeout: float = 5.0
    reprice_max_attempts: int = 59   # times poll_fill() re-prices a stale unfilled order before giving up

    place_order_max_attempts: int = 3
    place_order_retry_delay: float = 1.5

    # push_leg_error() only pushed once, on the transition into error_state --
    # a single lost POST (server busy, transient network blip) left the UI's
    # error badge silently blank for hours even though state.json correctly
    # tracked the error the whole time (confirmed in production, 2026-07-28:
    # three legs sat in exit_failed for 1-4 hours with no UI error shown).
    # Re-pushing at this interval for every leg still in error_state means a
    # single lost push self-heals within a minute instead of indefinitely.
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
        """Use inside an `except` block instead of manually building a
        traceback string (import traceback / traceback.format_exc()) -- this
        captures the current exception's traceback via the standard logging
        exc_info mechanism instead of jamming it into the message text."""
        Log.logger.exception(message)


###############################################################################
# MODELS
###############################################################################
@dataclass
class OptionLeg:
    symbol: str = ""
    quantity: int = 0
    entry_time: str = ""
    entry_px: float = 0.0
    entry_order_id: str = ""
    entry_filled: bool = False
    exit_order_id: str = ""
    exit_filled: bool = False
    # Real average fill price for the EXIT order, captured from poll_fill's
    # orderstatus() response the moment the exit is confirmed complete (see
    # _watch_exit_fill). None until then; _finalize_close prefers this over
    # the WS/REST LTP-cache fallback once it's available -- entry already
    # has this correction (see _watch_entry_fill); this brings the exit
    # side up to the same standard.
    exit_fill_px: Optional[float] = None
    closed: bool = False
    execution_id: int = 0          # which process run OPENED this leg -- captured at entry so a
                                    # mid-position restart still tags the eventual close correctly
    # Order error recovery (see docs/prd/python-strategies-order-error-recovery.md) --
    # set when poll_fill() exhausts its automatic retries. Kept on the leg (not a
    # separate structure) so it survives a strategy restart via state.json, same as
    # entry_order_id etc. Cleared once the user resolves it via Retry/Cancel/Manual.
    error_state: str = ""           # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""            # "" | "terminal" (order already dead) | "resting" (still live, unfilled)
    error_order_id: str = ""        # the order id Retry/Cancel act on when error_kind == "resting"
    error_message: str = ""         # last exception text, for display
    error_since: str = ""           # ISO timestamp this error (or its latest re-entry) began
    manual_exit_px: Optional[float] = None  # set only by a "manual" resolution on an exit


@dataclass
class RepairLeg(OptionLeg):
    fired: bool = False   # search-and-sell has executed (at most once per side per day)


@dataclass
class InstrumentState:
    traded_today: bool = False
    exited_today: bool = False
    pe: OptionLeg = field(default_factory=OptionLeg)
    ce: OptionLeg = field(default_factory=OptionLeg)
    pe_repair: RepairLeg = field(default_factory=RepairLeg)
    ce_repair: RepairLeg = field(default_factory=RepairLeg)


@dataclass
class StrategyState:
    current_day: str = ""
    instruments: dict = field(default_factory=lambda: {i.name: InstrumentState() for i in INSTRUMENTS})
    last_updated: str = ""
    today_realized_pnl: float = 0.0  # sum of closed legs' pnl_rupees today -- pushed via report_pnl_to_platform
    last_execution_id: int = 0       # incremented once per process start (see main()) -- the Trades UI's
                                      # execution dropdown groups trades_{id}.csv rows by this number


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
        # Was 120.0 -- far too long for a call made synchronously inside
        # run_cycle() (indicator/chain refresh, LTP fallback). A single broker
        # hiccup could block the whole scheduler for up to 2 minutes PER call,
        # and several such calls stacking in one cycle caused a real ~11-minute
        # stall in production (2026-07-24). Order placement/poll_fill already
        # have their own retry/reprice loops on top of this, so a short
        # per-call ceiling just makes a stuck call fail fast instead of
        # hanging -- it doesn't change normal-case behavior at all.
        self.timeout = 10.0
        # A single-symbol quotes() call (the WS-stale LTP fallback used
        # directly in run_cycle's main-thread loop) should never legitimately
        # need anywhere near 10s -- a healthy broker answers it in well under
        # a second. Giving it its own short ceiling (via a second lightweight
        # client, see Broker.connect_ltp_client) bounds run_cycle's own
        # worst-case wall-clock time much tighter than sharing the 10s
        # history()/optionchain() timeout would. This matters most here --
        # up to 8 legs (2 instruments x straddle+repair x PE/CE) can each
        # need their own fallback call in the same cycle.
        self.ltp_timeout = 3.0
        self.ws_url = os.getenv("WEBSOCKET_URL")
        self.strategy_tag = (
            os.getenv("OPENALGO_STRATEGY_TAG")
            or os.getenv("STRATEGY_ID")
            or "nifty_sensex_expiry_batman"
        )

    def validate(self):
        if not self.api_key:
            raise ValueError("OPENALGO_API_KEY environment variable not found.")


def _within_market_hours() -> bool:
    """Shared by StrategyEngine and PriceStream's reconnect watchdog -- the
    feed goes silent outside market hours by design, so staleness checks
    must not fire (and force pointless reconnects) overnight."""
    if config.test_mode:
        return True
    now = datetime.now(IST).time()
    return time(9, 15) <= now <= config.market_close


def _current_ws_stale_threshold() -> float:
    """Widened staleness threshold for PriceStream's watchdog during the
    post-open grace window (see config.ws_stale_seconds_open's docstring)
    -- only affects how fast the watchdog reacts to a stale symbol, not
    get_ltp()'s REST-fallback threshold, which stays at ws_stale_seconds."""
    now = datetime.now(IST).time()
    if time(9, 15) <= now < config.ws_post_open_grace_until:
        return config.ws_stale_seconds_open
    return config.ws_stale_seconds


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
            # PriceStream._watchdog_loop already owns full reconnect +
            # resubscribe on this same client. Leaving the SDK's own
            # auto_reconnect thread enabled races it: both call
            # _do_connect() on self.ws independently, which can tear
            # down/replace the socket concurrently and immediately trigger
            # another spurious close -- observed in production as a
            # ~45-50s repeating "connection down" cycle that never
            # settles. The watchdog is the single owner of reconnect.
            auto_reconnect=False,
        )
        Log.info("Connected to OpenAlgo")
        return self.client

    def connect_ltp_client(self):
        """A second, independent client used ONLY for the WS-stale LTP
        fallback (quotes()) -- deliberately NOT sharing self.client's
        self.timeout attribute, since that's a plain mutable instance
        attribute read at call-time; mutating it around one call while a
        background thread is mid-flight on the same client would race. A
        second lightweight httpx-backed client is cheap (no auth handshake,
        just a pooled HTTP client) and avoids that entirely."""
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
# LIVE PRICE STREAM (WebSocket, replaces per-cycle REST quotes -- see the
# module docstring's "Live price feed" section). Subscribes to a mix of a
# FIXED set (the 2 underlyings, added once at startup) and a DYNAMICALLY
# growing set (each straddle/repair leg's option symbol, added the moment
# it's resolved at runtime) -- the same `add_instruments()` design as the
# VWAP strategy handles both cases identically.
###############################################################################
class PriceStream:
    """Subscribes to LTP mode for a growing set of instruments over
    OpenAlgo's shared WebSocket proxy. Keeps an in-memory, thread-safe
    {(symbol, exchange): (ltp, tick_time)} cache updated by the push
    callback. A background watchdog thread detects a stale/silent feed
    during market hours and reconnects -- fully if the connection itself
    is down, or per-symbol (leaving a healthy symbol's feed undisturbed)
    if only some symbol(s) are stale while the connection is otherwise
    healthy. If a symbol stays stale across several consecutive cycles
    despite that per-symbol resubscribe, escalates to a full reconnect
    instead (see _watchdog_loop) -- confirmed in production that the
    per-symbol path alone can retry 30+ times with zero recovery while the
    connection itself stays reported connected/authenticated the whole
    time, so something in that connection's own state needs a clean reset,
    not another poke at the same symbol."""

    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple] = {}
        self._instruments: dict[tuple, dict] = {}  # (symbol, exchange) -> {"symbol":.., "exchange":..}
        self._stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        # Consecutive watchdog cycles a given (symbol, exchange) has been
        # found stale IN A ROW -- see _watchdog_loop's escalation policy.
        self._stale_streak: dict[tuple, int] = {}
        # Per-symbol backoff for the per-symbol resubscribe retry path --
        # independent of any OTHER symbol's own backoff, so one chronically
        # stale symbol's growing wait never slows down retries for a
        # different symbol that just started going stale.
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
        """Subscribe to new (symbol, exchange) pairs not already tracked --
        safe to call repeatedly / with overlapping entries. Used at startup
        (both underlyings, plus any leg already open on a same-day restart)
        and live, the moment each straddle/repair leg's symbol is resolved."""
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
        """Called once a leg's option symbol is fully closed -- without this,
        every distinct strike traded stays subscribed and watched by
        _watchdog_loop for the rest of this long-lived process's life, an
        unbounded (across days) WS-subscription accumulation."""
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
                 + (f" and (re)subscribed: {all_instruments}" if all_instruments else " (no symbols yet)"))

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
        """Before paying the cost of a disruptive full reconnect (which
        briefly drops EVERY tracked symbol's stream, not just the stuck
        ones), confirm via a REST quotes() call that at least one stale
        symbol's price has genuinely moved since its last cached tick. If
        it has, the WS feed really is failing to deliver and a reconnect
        is warranted. If REST also shows the SAME frozen price for every
        stale symbol, they simply aren't trading right now (thin
        liquidity) -- reconnecting cannot fix that, so the caller should
        skip it and let per-symbol backoff keep retrying instead.

        A REST call that itself fails/errors counts as "can't confirm
        either way" and is skipped for that symbol (not treated as proof
        of brokenness) -- this only needs ONE symbol to show real
        movement to return True."""
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
        """Two-tier recovery, escalating only when warranted:

        1. Connection-down (transport itself dead) -> full reconnect
           immediately.
        2. Connection alive, but some symbols' ticks are stale -> per-symbol
           resubscribe, each on its OWN independent backoff
           (_symbol_backoff_step/_symbol_next_retry_at). Only escalates to a
           full reconnect if a MAJORITY of tracked symbols are
           simultaneously stuck past ws_stale_reconnect_after consecutive
           cycles, AND _confirm_genuinely_broken_via_rest() shows real
           price movement the WS feed isn't delivering -- not just thin
           liquidity. Confirmed in production, 2026-07-29 (MCX): a single
           thinly-traded option leg stayed stale for an extended stretch
           while the futures contract on the SAME connection ticked fine
           the whole time -- the OLD "any one symbol escalates" rule would
           force repeated full reconnects that could never fix a liquidity
           problem, disrupting the healthy stream for nothing.
        """
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
            # 2026-07-30: dropped the unsubscribe_ltp() call that used to run
            # before this subscribe. Fyers' HSM protocol has no real
            # per-symbol unsubscribe -- unsubscribe_symbols() only clears
            # OpenAlgo's own tracking, it never tells Fyers to actually stop
            # the token. So every 15-30s retry cycle was telling Fyers
            # "give me this token" for a token Fyers already considered
            # active, right after wiping our own bookkeeping for it --
            # confirmed in production that this redundant unsub/resub
            # churn, repeated for minutes, never once self-recovered the
            # feed, while a single clean subscribe (no preceding
            # unsubscribe) via manual /websocket/test consistently did,
            # every time it was tried. A subscribe to an already-subscribed
            # token is a safe, idempotent re-affirmation on its own.
            Log.warning(f"[DEBUG-TEMP][BATMAN] about to subscribe_ltp: {due_names}")
            try:
                self.client.subscribe_ltp(due_for_retry, on_data_received=self._on_tick)
                Log.warning(f"[DEBUG-TEMP][BATMAN] subscribe_ltp returned: {due_names}")
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
        insts_data = data.get("instruments", {})
        for inst in INSTRUMENTS:
            raw = insts_data.get(inst.name, {})
            inst_state = InstrumentState()
            inst_state.traded_today = raw.get("traded_today", False)
            inst_state.exited_today = raw.get("exited_today", False)
            inst_state.pe = OptionLeg(**{**asdict(OptionLeg()), **filter_known_fields(OptionLeg, raw.get("pe", {}))})
            inst_state.ce = OptionLeg(**{**asdict(OptionLeg()), **filter_known_fields(OptionLeg, raw.get("ce", {}))})
            inst_state.pe_repair = RepairLeg(**{**asdict(RepairLeg()), **filter_known_fields(RepairLeg, raw.get("pe_repair", {}))})
            inst_state.ce_repair = RepairLeg(**{**asdict(RepairLeg()), **filter_known_fields(RepairLeg, raw.get("ce_repair", {}))})
            self.state.instruments[inst.name] = inst_state
        Log.info(f"State loaded from {self.path}")
        return self.state

    def save(self):
        self.state.last_updated = datetime.now(IST).isoformat()
        payload = {
            "current_day": self.state.current_day,
            "last_updated": self.state.last_updated,
            "today_realized_pnl": self.state.today_realized_pnl,
            "last_execution_id": self.state.last_execution_id,
            "instruments": {
                name: {
                    "traded_today": inst.traded_today,
                    "exited_today": inst.exited_today,
                    "pe": asdict(inst.pe),
                    "ce": asdict(inst.ce),
                    "pe_repair": asdict(inst.pe_repair),
                    "ce_repair": asdict(inst.ce_repair),
                }
                for name, inst in self.state.instruments.items()
            },
        }
        with self.path.open("w") as fp:
            json.dump(payload, fp, indent=4)


###############################################################################
# HELPERS
###############################################################################
def _compact_expiry(expiry_ddmmmyy_dash: str) -> str:
    return expiry_ddmmmyy_dash.replace("-", "").upper()


def resolve_current_week_expiry(client, inst: InstrumentConfig):
    """Returns (compact_symbol_str, date) for the nearest upcoming expiry.
    Used both to gate entry (is today this instrument's own expiry day?)
    and, per explicit instruction, as the actual contract the straddle and
    repair legs trade -- always the current/expiring-today contract, not
    next week's (see module docstring for the tradeoff this reverses)."""
    resp = client.expiry(symbol=inst.name, exchange=inst.options_exchange, instrumenttype="options")
    if resp.get("status") != "success" or not resp.get("data"):
        raise RuntimeError(f"Could not resolve expiry for {inst.name}: {resp}")
    today = datetime.now(IST).date()
    for raw in resp["data"]:
        d = datetime.strptime(raw, "%d-%b-%y").date()
        if d >= today:
            return _compact_expiry(raw), d
    last = resp["data"][-1]
    return _compact_expiry(last), datetime.strptime(last, "%d-%b-%y").date()


def _is_error_response(obj) -> bool:
    return isinstance(obj, dict)


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


def fetch_ltp(client, inst: InstrumentConfig) -> Optional[float]:
    return fetch_symbol_ltp(client, inst.name, inst.underlying_exchange)


def fetch_symbol_bid_ask(client, symbol: str, exchange: str) -> tuple[Optional[float], Optional[float]]:
    """Used only by the reprice loop (_reprice_and_wait_once) -- crossing the
    spread with a fresh bid/ask is a genuinely different, more aggressive
    price than the last-traded price fetch_symbol_ltp returns, and is what
    actually gets a resting order filled rather than just re-quoting the
    same stale level."""
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



def fetch_chain(client, inst: InstrumentConfig, expiry: str, strike_count: int):
    resp = client.optionchain(
        underlying=inst.name, exchange=inst.underlying_exchange,
        expiry_date=expiry, strike_count=strike_count,
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


def pick_leg_by_target_ltp(chain: dict, option_type: str, target_price: float) -> Optional[dict]:
    """Repair leg's strike selection: whichever strike is CURRENTLY trading
    closest to target_price (entry_px / repair_n) -- the OpenAlgo analogue
    of the source's `find_strike(..., 'ltp', target, ...)`."""
    legs = [l for l in _legs_with_strike(chain, option_type) if l.get("ltp") is not None and l["ltp"] > 0]
    if not legs:
        return None
    return min(legs, key=lambda l: abs(l["ltp"] - target_price))


class OrderNeedsAttention(Exception):
    """poll_fill() exhausted its automatic reprice attempts but the order is
    still resting, UNFILLED, at the broker -- not cancelled. Distinguishes
    "nothing left to act on" (order_id is dead -- a genuine broker rejection
    or cancellation, still raised as a plain RuntimeError immediately,
    unchanged) from "still live, needs a human decision" (this). See
    docs/prd/python-strategies-order-error-recovery.md."""
    def __init__(self, order_id: str, message: str):
        super().__init__(message)
        self.order_id = order_id


def _reprice_and_wait_once(client, order_id: str, strategy: str, symbol: str, exchange: str,
                            action: str, quantity: int) -> Optional[dict]:
    """One reprice-to-a-fresh-crossing-price (modifyorder(), keeping the same
    order id/queue position) + one fill_poll_timeout-bounded wait. Reprices
    to the current ASK for a BUY and the current BID for a SELL -- crossing
    the spread against a fresh quote each attempt, rather than re-quoting the
    last-traded price, since that's what actually gets a resting order filled
    instead of just re-posting at the same stale level. Returns the fill data
    if it completed, None if still unfilled (order left resting either way --
    this never cancels). Shared by poll_fill()'s own reprice loop AND by
    Entry-Cancel's one-last-chance flow (_watch_entry_cancel), so this "give
    it a fair, aggressive price, then wait" behavior only exists in one
    place."""
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
    """Polls order status until a terminal state or config.fill_poll_timeout
    (15s). On timeout, actively RE-PRICES the same order (via
    _reprice_and_wait_once, keeping its order id/queue position) crossing the
    spread with a fresh bid/ask each time -- up to config.reprice_max_attempts
    (19) times, for a combined ceiling of ~5 minutes (fill_poll_timeout x
    (1 + reprice_max_attempts)). If it's STILL resting after all of those,
    raises OrderNeedsAttention WITHOUT cancelling it -- entering error mode is
    now a user decision (Retry/Cancel/Manually Completed), not an automatic
    cancel. A genuine broker rejection/cancellation (order never became
    fillable at all) still raises RuntimeError immediately, unchanged.
    See docs/prd/python-strategies-order-error-recovery.md."""
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
    """Places an order. Only retries a CLEAN rejection response (the broker
    explicitly returned a non-"success" status -- nothing was placed, safe
    to retry) up to config.place_order_max_attempts times. Deliberately does
    NOT retry a raised exception (network timeout, connection reset, etc.)
    -- that case is ambiguous: the request may have already reached the
    broker and succeeded even though the client never got the response, so
    retrying could place a genuine DUPLICATE order. No idempotency key
    exists anywhere in this stack (confirmed against the openalgo SDK's
    placeorder() signature and services/place_order_service.py), so
    surfacing the exception immediately -- exactly once, no retry -- is the
    only safe choice. A genuine persistent rejection still raises after the
    last retry, and is handled by the caller (see the module docstring's
    "Order placement robustness" section)."""
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
# TRADE LOG (background thread -- see append_trade_log)
###############################################################################
_trade_log_queue: "queue.Queue" = queue.Queue()
_trade_log_thread: Optional[threading.Thread] = None
_trade_log_thread_lock = threading.Lock()

_TRADE_LOG_HEADER = ["leg", "symbol", "quantity", "direction", "entry_time", "entry_px",
                     "exit_time", "exit_px", "pnl_points", "pnl_rupees",
                     "exit_reason", "execution_id"]


def _migrate_trade_log_if_needed(strategy_tag: str):
    """One-time migration: a trades_{strategy_tag}.csv written before
    execution_id tracking existed has a header missing that column.
    Rewrite the file ONCE -- add the column, backfill every existing row
    with execution_id="legacy" -- so old and new rows share one consistent
    schema going forward. Safe to call on every startup: a no-op once the
    header already matches (checked BEFORE the trade-log writer thread
    starts, so there's no concurrent-write race with this rewrite)."""
    log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
    if not log_path.exists():
        return
    with log_path.open("r", newline="") as fp:
        rows = list(csv.reader(fp))
    if not rows or "execution_id" in rows[0]:
        return  # empty file, or already migrated
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
            (strategy_tag, leg_key, symbol, quantity, direction,
             entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id) = item
            log_path = Path(__file__).resolve().parent / f"trades_{strategy_tag}.csv"
            is_new = not log_path.exists()
            # LONG: sell high, bought low = profit. SHORT: sell high, buy back low = profit.
            pnl_points = (exit_px - entry_px) if direction == "LONG" else (entry_px - exit_px)
            pnl_rupees = pnl_points * quantity
            # Display quantity signed negative for a SHORT leg so it reads
            # correctly in the Trades UI even though direction is also its
            # own column -- pnl above already used the unsigned quantity.
            display_quantity = -quantity if direction == "SHORT" else quantity
            with log_path.open("a", newline="") as fp:
                writer = csv.writer(fp)
                if is_new:
                    writer.writerow(_TRADE_LOG_HEADER)
                writer.writerow([leg_key, symbol, display_quantity, direction, entry_time, round(entry_px, 2),
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


def append_trade_log(strategy_tag: str, leg_key: str, symbol: str, quantity: int, direction: str,
                      entry_time: str, entry_px: float, exit_time: str, exit_px: float,
                      exit_reason: str, execution_id: int):
    _ensure_trade_log_thread()
    _trade_log_queue.put((strategy_tag, leg_key, symbol, quantity, direction,
                          entry_time, entry_px, exit_time, exit_px, exit_reason, execution_id))




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
    ownership bug. 2026-08-05: moved off the main app's openalgo.sock/
    FLASK_PORT -- these reporting calls used to share the single
    gunicorn+eventlet worker with every other route in the app, so an
    unrelated slow endpoint elsewhere (confirmed in production:
    /traffic/api/stats) could block the worker long enough that these
    timed out even though nothing about the strategy was wrong. The
    dedicated subprocess is immune to that by construction (a separate
    process, plain OS threads, not eventlet). Raises if both attempts
    fail; caller logs and swallows."""
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
    """Push a PnL snapshot to the OpenAlgo Python Strategy Host so the UI's
    PNL button can show live PnL without the platform having to poll/parse
    this process's logs. Fire-and-forget: stdlib-only (no new dependency),
    short timeout, any failure is logged and swallowed -- must never block
    or crash the main scheduler loop over a reporting hiccup.

    `open_positions`: list of dicts, each {leg_key, symbol, direction
    ("LONG" for a straddle leg, "SHORT" for a fired repair leg), quantity,
    entry_price, current_price, pnl} -- pnl per leg already signed correctly
    by the caller (see _aggregate_unrealized_pnl's LONG/SHORT handling), so
    this function just sums them for unrealized_pnl."""
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
    behavior (2026-08-07: see that function's docstring for why the
    env.host/Cloudflare fallback was dropped). Raises if both attempts
    fail; caller treats it as "no action pending" rather than crashing
    the scheduler loop over a reporting hiccup."""
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


def push_leg_error(env: "Environment", leg_key: str, leg: "OptionLeg",
                    action: str = "", clear: bool = False):
    """Push (or clear) this leg's error-mode state to the platform so the UI's
    error badge/page reflects it live, without polling this process's logs.
    Fire-and-forget, same style as report_pnl_to_platform -- must never block
    or crash the scheduler loop over a reporting hiccup. `clear=True` is used
    once a Retry/Cancel/Manual action has actually resolved the leg (leg's
    error_state is already "" by then), so the platform drops the alert.
    See docs/prd/python-strategies-order-error-recovery.md."""
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
    """Pull whether the user has issued a Retry/Cancel/Manual action for this
    leg from the platform. Only ever called for a leg currently in error mode
    (near-zero legs, near-zero overhead on the normal per-cycle path). Returns
    None on any failure (transport error, or genuinely nothing pending) --
    degrades to "nothing pending yet, try again next cycle" rather than
    raising, matching report_pnl_to_platform's best-effort style."""
    # apikey MUST be in the query string -- api_check_pending_action requires
    # it and returns 401 without it (this was missing entirely until now,
    # which meant every call silently 401'd and this function always
    # reported "nothing pending", regardless of what the user submitted).
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
    """Pull whether the user has clicked the Force Exit button for this
    strategy. Called once per cycle (near-zero overhead) -- returns False on
    any transport failure, matching check_pending_action's best-effort
    style (degrades to "not requested" rather than raising)."""
    path = f"/python/api/strategy/{env.strategy_tag}/force_exit?apikey={env.api_key}"
    try:
        data = _get_json_local(env, path)
    except Exception as exc:
        Log.warning(f"check_force_exit failed: {exc}")
        return False
    return bool(data.get("requested"))


def ack_force_exit_complete(env: "Environment"):
    """Tell the platform every open leg has been force-closed and confirmed
    flat -- this is what actually stops the strategy process (see
    api_complete_force_exit). Fire-and-forget, same style as
    report_pnl_to_platform."""
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
        # Short-timeout client used for the WS-stale LTP fallback calls made
        # directly in run_cycle -- see Broker.connect_ltp_client(). Falls
        # back to the main client if not supplied (e.g. a test harness
        # constructing StrategyEngine directly) so this stays optional.
        self.ltp_client = ltp_client if ltp_client is not None else client
        self.store = store
        self.env = env
        self.price_stream = price_stream
        self.execution_id = execution_id  # this process run's number -- see main()
        # The current week's expiry only rolls at week boundaries, not intraday --
        # resolving it fresh via client.expiry() on every cycle's gating check
        # (and again at entry, and again at repair) added 3 REST round-trips per
        # cycle for a value that can't change all day. Cached per instrument,
        # cleared in _reset_day_if_needed alongside other daily state.
        self._expiry_cache: dict[str, tuple] = {}
        # poll_fill() can block for up to fill_poll_timeout * (1 + reprice_max_attempts)
        # seconds (a stale/stuck order re-pricing against a moving market) -- doing that
        # INSIDE run_cycle would stall every other instrument/leg's entry/exit check for
        # the same duration. Order confirmation now happens on a per-leg background task
        # instead (see _watch_entry_fill/_watch_exit_fill), submitted to one shared,
        # module-instance-lifetime executor rather than a raw thread per order (FD
        # hygiene: threads/executors are shared singletons, never spun up per-call).
        # _state_lock serializes every self.store.save() call (main thread and
        # background tasks alike) -- StateStore.save() writes the whole state to one
        # shared JSON file, so two concurrent writers could corrupt it even though each
        # leg's own fields are only ever touched by one thread at a time (guarded by
        # _pending_fills below). _pending_fills tracks which leg_keys already have an
        # active watcher so run_cycle doesn't submit a duplicate one next cycle.
        self._state_lock = threading.Lock()
        self._pending_fills: set[str] = set()
        self._last_error_push: dict[str, datetime] = {}
        # Throttles the outer run_cycle except-clause's WhatsApp alert --
        # separate from _last_error_push above, which only governs the UI
        # error-badge re-push. In-memory/per-instance, not persisted.
        self._last_cycle_failure_notify: Optional[datetime] = None
        self._fill_executor = ThreadPoolExecutor(
            max_workers=len(INSTRUMENTS) * 4, thread_name_prefix="fillwatch"
        )
        # Separate, single-worker pool purely for the Force Exit check
        # (check_force_exit, a quick local HTTP call). If all _fill_executor
        # workers are simultaneously busy watching fills (each can block up
        # to fill_poll_timeout * (1 + reprice_max_attempts) seconds -- worst
        # case at the highest-risk moment, an end-of-day/expiry-day unwind),
        # a Force Exit check submitted to that SAME pool would just queue
        # silently behind them for minutes with no log line and no
        # escalation, exactly when a human is trying to intervene fastest.
        self._bg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bgcheck")
        # Dedicated single worker for report_pnl_tick's platform push --
        # separate from _fill_executor so a fill-watcher blocked for minutes
        # (reprice loop) can never make the live PnL display go stale too;
        # PnL pushes are small/fast and only need to run one at a time.
        self._pnl_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pnltick")
        # report_pnl_tick()'s throttled REST fallback -- see
        # config.pnl_rest_fallback_interval_sec's docstring. Keyed by
        # leg_key; not cleared on leg close, same as _last_error_push above --
        # a fixed, small leg-key-space dict, structurally bounded regardless.
        self._pnl_last_known_price: dict[str, float] = {}
        self._pnl_rest_fallback_last_attempt: dict[str, datetime] = {}
        # Guards the entry/repair chain-fetch background dispatch (see
        # _enter_straddle_bg/_maybe_fire_repair_bg) -- separate from
        # _pending_fills since it tracks "a chain fetch is in flight", not
        # "a fill is being watched".
        self._chain_pending: set[str] = set()
        self._force_exit_pending: bool = False
        self._force_exit_check_pending: bool = False
        # check_pending_action is also a synchronous local HTTP call made
        # inline from run_cycle -- same blocking-bug class as
        # check_force_exit/notify_trade_closed above. Result is consumed by
        # the caller, so (unlike push_leg_error/notify_trade_closed, which
        # are pure fire-and-forget) it needs a cache rather than plain
        # dispatch-and-forget.
        self._pending_action_cache: dict = {}
        self._pending_action_inflight: set = set()
        # Guards the week-expiry background dispatch (see
        # _refresh_week_expiry_bg) -- run_cycle's own expiry-day gate used to
        # call _get_week_expiry() inline (a real client.expiry() round-trip
        # on the first call each day, and on every retry after a failure,
        # since a failed call never populates _expiry_cache).
        self._expiry_pending: set[str] = set()

    def _save_state(self):
        """Every self.store.save() call in this engine goes through here --
        StateStore.save() writes the entire state to one shared JSON file, so
        concurrent writers (run_cycle vs. a background fill-watch task) could
        otherwise corrupt it or drop an update."""
        with self._state_lock:
            self.store.save()

    def _get_week_expiry(self, inst: InstrumentConfig) -> tuple:
        cached = self._expiry_cache.get(inst.name)
        if cached is None:
            cached = resolve_current_week_expiry(self.client, inst)
            self._expiry_cache[inst.name] = cached
        return cached

    def _refresh_week_expiry_bg(self, inst: InstrumentConfig):
        """Background dispatch for populating _expiry_cache -- used by
        run_cycle's own expiry-day gate, which must never call
        _get_week_expiry() (a real client.expiry() round-trip) inline on the
        main scheduler thread. Guarded per instrument so a slow call, or one
        that outlives a single scheduler_interval, isn't resubmitted on top
        of itself; a failed attempt leaves the cache empty and simply
        retries on the next cycle that finds it still missing."""
        guard_key = inst.name
        if guard_key in self._expiry_pending:
            return
        self._expiry_pending.add(guard_key)

        def _run():
            try:
                self._expiry_cache[inst.name] = resolve_current_week_expiry(self.client, inst)
            except Exception as exc:
                Log.warning(f"[{inst.name}] Background week-expiry refresh failed: {exc}")
            finally:
                self._expiry_pending.discard(guard_key)

        self._fill_executor.submit(_run)

    # ---- state helpers -----------------------------------------------------
    def _reset_day_if_needed(self):
        today_key = datetime.now(IST).date().isoformat()
        if self.store.state.current_day != today_key:
            # Don't wipe an unresolved error -- a leg still frozen awaiting
            # Retry/Cancel/Manually Completed may have a genuinely open
            # position at the broker that this reset would otherwise silently
            # orphan. Defer the whole day-rollover (same pattern MCX uses when
            # its own daily resolution isn't ready yet) until every leg is clear.
            for inst_state in self.store.state.instruments.values():
                for leg in (inst_state.pe, inst_state.ce, inst_state.pe_repair, inst_state.ce_repair):
                    if leg.error_state:
                        Log.error("Day rollover deferred -- at least one leg is still in "
                                  "error mode and needs Retry/Cancel/Manually Completed first.")
                        return
            Log.info(f"New day detected ({today_key}); resetting daily state.")
            self.store.state.current_day = today_key
            self.store.state.today_realized_pnl = 0.0
            self.store.state.instruments = {i.name: InstrumentState() for i in INSTRUMENTS}
            self._expiry_cache.clear()
            self._save_state()

    def _within_market_hours(self) -> bool:
        return _within_market_hours()

    def _past_universal_exit_time(self) -> bool:
        if config.test_mode:
            return False
        return datetime.now(IST).time() >= config.universal_exit_time

    # ---- entry (long straddle, once/day/instrument) -------------------------
    def _enter_straddle_bg(self, inst: InstrumentConfig, inst_state: InstrumentState, spot: float,
                            condition_desc: str = ""):
        """Dispatch wrapper for _enter_straddle -- its chain fetch
        (fetch_chain -> client.optionchain()) is a real broker round-trip on
        the main client (up to Environment.timeout), so it runs on
        _fill_executor instead of inline in run_cycle, where it would block
        every other instrument's signal/exit check for the same duration.
        Guarded per instrument so a slow fetch that outlives one
        scheduler_interval isn't resubmitted on top of itself."""
        guard_key = f"{inst.name}_entry_chain"
        if guard_key in self._chain_pending:
            return
        self._chain_pending.add(guard_key)

        def _run():
            try:
                self._enter_straddle(inst, inst_state, spot, condition_desc=condition_desc)
            except Exception as exc:
                Log.exception(f"[{inst.name}] Straddle entry (background) failed: {exc}")
            finally:
                self._chain_pending.discard(guard_key)

        self._fill_executor.submit(_run)

    def _enter_straddle(self, inst: InstrumentConfig, inst_state: InstrumentState, spot: float,
                         condition_desc: str = ""):
        strategy_tag = self.env.strategy_tag
        # The CURRENT (expiring-today) week's expiry -- per explicit
        # instruction, reverting the earlier next-week-roll choice for this
        # strategy specifically. See module docstring for the tradeoff this
        # reverses (a same-day-expiring contract has an extreme gamma/theta
        # cliff in its final hours; the backtested ANALYSIS.md sweep found
        # next-week's contract gave a smaller worst-day/max-drawdown at a
        # modest cost to total return -- that finding still stands, this is
        # a deliberate choice to trade the current contract anyway).
        expiry_str, _ = self._get_week_expiry(inst)
        chain = fetch_chain(self.client, inst, expiry_str, config.entry_chain_strike_count)

        for option_type, leg in (("PE", inst_state.pe), ("CE", inst_state.ce)):
            if not leg.symbol:
                atm_leg = pick_atm_leg(chain, option_type, spot)
                quantity = config.lot_multiplier * atm_leg["lotsize"]
                Log.info(f"[{inst.name}_{option_type}] Straddle entry: "
                         f"strike={atm_leg['strike']} symbol={atm_leg['symbol']}@{atm_leg['ltp']} qty={quantity}"
                         + (f" | condition: {condition_desc}" if condition_desc else ""))
                new_leg = OptionLeg(
                    symbol=atm_leg["symbol"], quantity=quantity,
                    entry_time=datetime.now(IST).isoformat(), entry_px=float(atm_leg["ltp"]),
                    execution_id=self.execution_id,
                )
                if option_type == "PE":
                    inst_state.pe = new_leg
                else:
                    inst_state.ce = new_leg
                self._save_state()
                self.price_stream.add_instruments(
                    [{"symbol": atm_leg["symbol"], "exchange": inst.options_exchange}]
                )

        for option_type, attr in (("PE", "pe"), ("CE", "ce")):
            leg_key = f"{inst.name}_{option_type}"
            leg = getattr(inst_state, attr)
            if leg.entry_filled or leg.error_state or leg_key in self._pending_fills:
                continue
            if not leg.entry_order_id:
                # place() can raise (either a RuntimeError after exhausting its
                # own retries on a persistent clean rejection, or an immediate
                # ambiguous exception it deliberately never retries) -- uncaught,
                # that would escape to _enter_straddle_bg's own broad except,
                # which only logs and never sets error_state, so this leg would
                # silently retry every cycle forever with zero UI visibility.
                try:
                    leg.entry_order_id = place(self.client, strategy_tag, leg.symbol,
                                                inst.options_exchange, "BUY", leg.quantity)
                except Exception as exc:
                    Log.exception(f"[{leg_key}] place() failed for straddle entry: {exc}")
                    self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                    continue
                self._save_state()
            # Fill confirmation happens off this thread -- see module-level note
            # on _state_lock/_pending_fills/_fill_executor. place() above is the
            # only REST call left on the signal-to-order critical path.
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(self._watch_entry_fill, leg_key, leg.entry_order_id)

    def _watch_entry_fill(self, leg_key: str, order_id: str):
        """Generic across all 4 leg types (straddle PE/CE = LONG entry=BUY,
        repair PE/CE = SHORT entry=SELL) -- _resolve_leg_key looks up which one
        leg_key refers to and its BUY/SELL direction."""
        inst, leg, direction = self._resolve_leg_key(leg_key)
        strategy_tag = self.env.strategy_tag
        entry_action = "BUY" if direction == "LONG" else "SELL"
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, leg.symbol,
                                   inst.options_exchange, entry_action, leg.quantity)
            inst, leg, _ = self._resolve_leg_key(leg_key)  # re-fetch: may have changed while polling
            if leg.entry_order_id == order_id:  # guard vs. a superseded/stale order
                leg.entry_filled = True
                # entry_px was set from a pre-trade LTP snapshot when the order
                # was placed -- correct it to the real average fill price now
                # that the order is confirmed complete, since this feeds the
                # repair-strike target and the aggregate-PnL stop-loss check.
                # Fall back to the pre-trade snapshot only if the broker
                # didn't supply a usable average_price.
                avg_price = float(fill_data.get("average_price") or 0.0)
                if avg_price > 0:
                    leg.entry_px = avg_price
                inst_state = self.store.state.instruments[inst.name]
                if inst_state.pe.entry_filled and inst_state.ce.entry_filled:
                    inst_state.traded_today = True
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled: {leg.symbol} @ {leg.entry_px}")
        except OrderNeedsAttention as exc:
            self._enter_error_mode(leg_key, "entry_failed", "resting", exc.order_id, str(exc))
        except (RuntimeError, TimeoutError) as exc:
            self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
        except Exception as exc:
            # Anything unexpected (malformed broker response, unhandled SDK
            # exception, etc.) -- without this, the executor would silently
            # drop it (nobody calls .result() on this future), leaving the
            # leg stuck with no log line and no error-state entry. Treated as
            # "resting" (not "terminal") since we don't actually know the
            # order's true fate -- safer to let the user Retry/Cancel it
            # explicitly than to assume it's dead.
            Log.exception(f"[{leg_key}] Unexpected error while watching entry fill: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _resolve_leg_key(self, leg_key: str):
        """Maps a leg_key string back to (inst, leg, direction) -- leg_key
        format: '{INST}_{PE|CE}' (straddle, LONG, entry=BUY/exit=SELL) or
        '{INST}_{PE|CE}_repair' (repair, SHORT, entry=SELL/exit=BUY)."""
        is_repair = leg_key.endswith("_repair")
        base = leg_key[: -len("_repair")] if is_repair else leg_key
        inst_name, option_type = base.rsplit("_", 1)
        inst = next(i for i in INSTRUMENTS if i.name == inst_name)
        inst_state = self.store.state.instruments[inst.name]
        if is_repair:
            leg = inst_state.pe_repair if option_type == "PE" else inst_state.ce_repair
            direction = "SHORT"
        else:
            leg = inst_state.pe if option_type == "PE" else inst_state.ce
            direction = "LONG"
        return inst, leg, direction

    def _enter_error_mode(self, leg_key: str, error_state: str, error_kind: str,
                          error_order_id: str, message: str):
        """See docs/prd/python-strategies-order-error-recovery.md. Called
        from every watcher's exception handler -- and, by construction, will
        be called AGAIN on the same leg if a subsequent Retry/Cancel-driven
        attempt also fails, since it's the normal failure path, not a
        one-shot special case. error_since is overwritten every time so the
        UI shows how long the CURRENT attempt has been stuck."""
        inst, leg, direction = self._resolve_leg_key(leg_key)
        leg.error_state = error_state
        leg.error_kind = error_kind
        leg.error_order_id = error_order_id
        leg.error_message = message
        leg.error_since = datetime.now(IST).isoformat()
        self._save_state()
        Log.error(f"[{leg_key}] {error_state} ({error_kind}): {message}")
        entry_action = "BUY" if direction == "LONG" else "SELL"
        exit_action = "SELL" if direction == "LONG" else "BUY"
        action = entry_action if error_state == "entry_failed" else exit_action
        push_leg_error(self.env, leg_key, leg, action=action)
        self._last_error_push[leg_key] = datetime.now(IST)
        # WhatsApp self-alert on every genuine error-state transition (order
        # rejection, fill timeout, or any other place()/poll_fill failure
        # that routes here) -- fires once per transition, not on the
        # periodic _repush_active_errors re-push. Dispatched via
        # _pnl_executor (non-blocking, same pool this file already uses for
        # push_leg_error) and notify_whatsapp_error() itself never raises --
        # a WhatsApp/network hiccup can never break this method or the
        # calling run_cycle.
        try:
            self._pnl_executor.submit(
                notify_whatsapp_error, self.env,
                f"[{config.strategy_name}] {leg_key} {error_state} ({error_kind}): {message}",
                log_warning=Log.warning,
            )
        except Exception as exc:
            Log.warning(f"Failed to dispatch WhatsApp error notification: {exc}")

    def _repush_active_errors(self):
        """push_leg_error() only fires once, on the transition into
        error_state (above) -- if that one POST is lost (server busy,
        transient network blip), the UI's error badge silently never
        appears, even though state.json correctly tracks the error the
        whole time (confirmed in production, 2026-07-28: three legs sat in
        exit_failed for 1-4 hours with no UI error shown). Re-pushes at
        most once per config.error_repush_interval_sec for every leg still
        in error_state, so a single lost push self-heals within a minute
        instead of leaving the UI blind indefinitely. Dispatched via
        _pnl_executor (not _fill_executor) -- same reasoning as
        report_pnl_tick: must never queue behind a fill-watcher stuck for
        minutes in a reprice loop."""
        now = datetime.now(IST)
        for inst in INSTRUMENTS:
            inst_state = self.store.state.instruments[inst.name]
            for leg_key_suffix, leg, direction in (
                ("PE", inst_state.pe, "LONG"), ("CE", inst_state.ce, "LONG"),
                ("PE_repair", inst_state.pe_repair, "SHORT"), ("CE_repair", inst_state.ce_repair, "SHORT"),
            ):
                leg_key = f"{inst.name}_{leg_key_suffix}"
                if not leg.error_state:
                    self._last_error_push.pop(leg_key, None)
                    continue
                last = self._last_error_push.get(leg_key)
                if last is not None and (now - last).total_seconds() < config.error_repush_interval_sec:
                    continue
                self._last_error_push[leg_key] = now
                entry_action = "BUY" if direction == "LONG" else "SELL"
                exit_action = "SELL" if direction == "LONG" else "BUY"
                action = entry_action if leg.error_state == "entry_failed" else exit_action
                try:
                    self._pnl_executor.submit(push_leg_error, self.env, leg_key, leg, action=action)
                except Exception as exc:
                    Log.warning(f"[{leg_key}] Failed to dispatch periodic error re-push: {exc}")

    def _push_leg_error_bg(self, leg_key: str, leg: "OptionLeg", action: str = "", clear: bool = False):
        """Fire-and-forget push_leg_error via _pnl_executor -- same
        blocking-bug class as _repush_active_errors above (which already
        dispatches this way), for the "clear"/on-resolution pushes from
        _resolve_leg_error, which run synchronously on run_cycle's own
        thread (unlike _enter_error_mode's push, already called from a
        watcher's own background thread). `leg` is snapshotted with a
        shallow copy on THIS (calling) thread before handing off --
        executor.submit() evaluates its arguments immediately, only the
        function call itself is deferred -- since leg is a live, mutable
        OptionLeg this same cycle may reset moments later."""
        snapshot = copy.copy(leg)
        try:
            self._pnl_executor.submit(push_leg_error, self.env, leg_key, snapshot, action=action, clear=clear)
        except Exception as exc:
            Log.warning(f"[{leg_key}] Failed to dispatch push_leg_error: {exc}")

    def _refresh_pending_action_bg(self, leg_key: str):
        """Dispatches check_pending_action to _bg_executor instead of
        blocking run_cycle() -- mirrors _refresh_force_exit_check_bg, but
        keyed per leg_key (multiple legs can be in error state at once) and
        caches the result for _pop_pending_action() to consume, since
        (unlike push_leg_error/notify_trade_closed) the caller needs the
        return value, not just fire-and-forget. Guarded per leg_key so a
        slow check that outlives one cycle isn't resubmitted on top of
        itself."""
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
        """The last background-fetched pending action for this leg, if any
        -- consumed once (popped), so a fetched action is never applied
        twice."""
        return self._pending_action_cache.pop(leg_key, None)

    # ---- repair (search-and-sell, at most once per side per day) -----------
    def _maybe_fire_repair_bg(self, inst: InstrumentConfig, inst_state: InstrumentState,
                               option_type: str, straddle_leg: OptionLeg, repair_leg: RepairLeg):
        """Dispatch wrapper for _maybe_fire_repair -- its chain fetch
        (fetch_chain -> client.optionchain()) is a real broker round-trip on
        the main client, so it runs on _fill_executor instead of inline in
        run_cycle. Guarded per leg so a slow fetch that outlives one
        scheduler_interval isn't resubmitted on top of itself. Cheap when
        repair_leg.fired is already True (just a fill-watcher resume check),
        but still dispatched through the same guard for simplicity/safety."""
        guard_key = f"{inst.name}_{option_type}_repair_chain"
        if guard_key in self._chain_pending:
            return
        self._chain_pending.add(guard_key)

        def _run():
            try:
                self._maybe_fire_repair(inst, inst_state, option_type, straddle_leg, repair_leg)
            except Exception as exc:
                Log.exception(f"[{inst.name}_{option_type}_repair] Repair fire (background) failed: {exc}")
            finally:
                self._chain_pending.discard(guard_key)

        self._fill_executor.submit(_run)

    def _maybe_fire_repair(self, inst: InstrumentConfig, inst_state: InstrumentState,
                            option_type: str, straddle_leg: OptionLeg, repair_leg: RepairLeg):
        # Fires the very first cycle after the straddle leg is fully filled --
        # no artificial delay, per explicit instruction (the short repair
        # leg goes on immediately, not after any wait).
        leg_key = f"{inst.name}_{option_type}_repair"
        if repair_leg.fired:
            # Already fired earlier today (or before a restart). If the fill
            # was never confirmed (process died between place() and the
            # watcher resolving it), not frozen in error, and not already
            # being watched in this process, resume the watcher here --
            # otherwise this leg's `fired=True` latch permanently orphans a
            # live naked short with nothing tracking its fill.
            if (not repair_leg.entry_filled and not repair_leg.error_state
                    and repair_leg.entry_order_id and leg_key not in self._pending_fills):
                Log.warning(f"[{leg_key}] Resuming fill watch for a repair order placed "
                            f"before a restart (order_id={repair_leg.entry_order_id}).")
                self._pending_fills.add(leg_key)
                self._fill_executor.submit(self._watch_entry_fill, leg_key, repair_leg.entry_order_id)
            return
        if not straddle_leg.entry_filled:
            return

        target_price = straddle_leg.entry_px / inst.repair_n
        try:
            # The CURRENT week's expiry, matching the straddle leg's own
            # contract (see _enter_straddle for the rationale reversal).
            expiry_str, _ = self._get_week_expiry(inst)
            chain = fetch_chain(self.client, inst, expiry_str, config.repair_chain_strike_count)
        except Exception as exc:
            Log.warning(f"[{inst.name}_{option_type}_repair] chain fetch failed: {exc}")
            return

        match = pick_leg_by_target_ltp(chain, option_type, target_price)
        if match is None:
            Log.warning(f"[{inst.name}_{option_type}_repair] no strike found near target={target_price:.2f}")
            return

        quantity = inst.repair_n * match["lotsize"]
        Log.info(f"[{inst.name}_{option_type}_repair] Repair fired: target={target_price:.2f} "
                  f"strike={match['strike']} symbol={match['symbol']}@{match['ltp']} qty={quantity}")

        repair_leg.symbol = match["symbol"]
        repair_leg.quantity = quantity
        repair_leg.entry_time = datetime.now(IST).isoformat()
        repair_leg.entry_px = float(match["ltp"])
        repair_leg.fired = True
        repair_leg.execution_id = self.execution_id
        self._save_state()
        self.price_stream.add_instruments(
            [{"symbol": match["symbol"], "exchange": inst.options_exchange}]
        )

        strategy_tag = self.env.strategy_tag
        if not repair_leg.entry_order_id:
            # place() can raise -- the comment below already documents the
            # intent that a genuine failure here should set error_state via
            # _enter_error_mode, but this call site had no try/except to
            # actually do that; an uncaught raise would escape all the way to
            # _maybe_fire_repair_bg's own broad except (log-only, no
            # error_state), leaving a naked-short repair attempt silently
            # retrying every cycle forever with zero UI visibility.
            try:
                repair_leg.entry_order_id = place(self.client, strategy_tag, repair_leg.symbol,
                                                   inst.options_exchange, "SELL", repair_leg.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for repair entry: {exc}")
                self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                return
            self._save_state()
        # Fill confirmation happens off this thread (see _enter_straddle's note
        # on _state_lock/_pending_fills/_fill_executor). On a genuine failure,
        # _enter_error_mode sets error_state -- NOT the old "reset every field
        # back to a fresh RepairLeg()" behavior, since repair_leg.fired staying
        # True (this function's own "if repair_leg.fired: return" guard at the
        # top) is now exactly what keeps this leg frozen until the user
        # resolves it via Retry/Cancel/Manually Completed, instead of silently
        # retrying search-and-sell from scratch.
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(self._watch_entry_fill, leg_key, repair_leg.entry_order_id)

    # ---- universal exit (aggregate PnL or time, per instrument) -------------
    def _close_open_leg(self, inst: InstrumentConfig, leg_key: str, leg: OptionLeg,
                         direction: str, reason: str):
        strategy_tag = self.env.strategy_tag
        if leg.closed:
            return
        if leg.error_state:
            # Frozen awaiting a Retry/Cancel/Manual decision -- do NOT auto-force
            # a close through an errored leg (see run_cycle's per-leg error check,
            # which is what actually drives this leg's resolution). Other legs on
            # this same instrument are unaffected -- _force_close_instrument calls
            # this once per leg independently.
            Log.error(f"[{leg_key}] Force-close requested but this leg is still in "
                      f"error mode ({leg.error_state}) -- resolve it via "
                      f"Retry/Cancel/Manually Completed NOW.")
            return
        if leg.exit_filled:
            self._finalize_close(inst, leg_key, leg, direction, reason)
            return
        if leg_key in self._pending_fills:
            return  # exit order already in flight -- background watcher will resolve it

        exit_action = "SELL" if direction == "LONG" else "BUY"
        if not leg.exit_order_id:
            # See the entry call sites' matching comment -- an uncaught
            # place() failure here is the more dangerous direction: it would
            # leave a position (straddle or naked-short repair) open
            # indefinitely with no error_state/UI alert, and would also
            # silently defeat the SHORT-before-LONG Force Exit ordering (the
            # instrument would never actually go flat).
            try:
                leg.exit_order_id = place(self.client, strategy_tag, leg.symbol,
                                           inst.options_exchange, exit_action, leg.quantity)
            except Exception as exc:
                Log.exception(f"[{leg_key}] place() failed for exit: {exc}")
                self._enter_error_mode(leg_key, "exit_failed", "terminal", "", str(exc))
                return
            self._save_state()

        # Fill confirmation happens off this thread -- see _enter_straddle's
        # note on _state_lock/_pending_fills/_fill_executor. place() above is
        # the only REST call left on the exit-signal-to-order path.
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(self._watch_exit_fill, leg_key, leg.exit_order_id)

    def _watch_exit_fill(self, leg_key: str, order_id: str):
        inst, leg, direction = self._resolve_leg_key(leg_key)
        strategy_tag = self.env.strategy_tag
        exit_action = "SELL" if direction == "LONG" else "BUY"
        try:
            fill_data = poll_fill(self.client, order_id, strategy_tag, leg.symbol, inst.options_exchange,
                                   exit_action, leg.quantity)
            inst, leg, _ = self._resolve_leg_key(leg_key)  # re-fetch: may have changed while polling
            if leg.exit_order_id == order_id:  # guard vs. a superseded/stale order
                leg.exit_filled = True
                # Capture the broker's real average fill price for this exit
                # -- _finalize_close prefers this over the WS/REST LTP-cache
                # fallback once it's available (see that method). Brings the
                # exit side up to the same standard as entry (see
                # _watch_entry_fill's matching correction).
                avg_price = float(fill_data.get("average_price") or 0.0)
                if avg_price > 0:
                    leg.exit_fill_px = avg_price
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

    def _finalize_close(self, inst: InstrumentConfig, leg_key: str, leg: OptionLeg,
                         direction: str, reason: str):
        """Runs on the main thread's next run_cycle tick once _watch_exit_fill
        has set leg.exit_filled -- the trade-log write and today_realized_pnl
        update don't need to be on the background thread's own critical path,
        and keeping them here means the watcher thread's body stays minimal."""
        strategy_tag = self.env.strategy_tag
        Log.info(f"[{leg_key}] Position closed: {leg.symbol} ({direction})")

        # Preference order: (1) a "Manually Completed" resolution's
        # user-supplied fill price -- authoritative for that path, and the
        # only one available since the normal watcher never confirmed a
        # fill there (see docs/prd/python-strategies-order-error-recovery.md);
        # (2) the broker's REAL average fill price, captured by
        # _watch_exit_fill the moment the exit order confirmed complete --
        # this is what the trade log is SUPPOSED to reflect, not an
        # LTP-cache guess; (3)/(4) WS/REST LTP as a last-resort estimate,
        # only reached if the broker somehow didn't supply average_price.
        exit_px = leg.manual_exit_px
        if exit_px is None:
            exit_px = leg.exit_fill_px
        if exit_px is None:
            exit_px = self.price_stream.get_ltp(leg.symbol, inst.options_exchange,
                                                 max_age=config.ws_stale_seconds)
        if exit_px is None:
            exit_px = fetch_symbol_ltp(self.ltp_client, leg.symbol, inst.options_exchange, require_two_sided=True)
        if exit_px is not None:
            # LONG: bought low (entry), sell high (exit) = profit. SHORT:
            # sell high (entry), buy back low (exit) = profit -- same
            # formula the async trade-log writer uses, kept in sync here so
            # today_realized_pnl (pushed via report_pnl_to_platform)
            # updates immediately rather than waiting on that thread.
            pnl_points = (exit_px - leg.entry_px) if direction == "LONG" else (leg.entry_px - exit_px)
            self.store.state.today_realized_pnl += pnl_points * leg.quantity
            self._save_state()
            try:
                append_trade_log(strategy_tag, leg_key, leg.symbol, leg.quantity, direction,
                                  leg.entry_time, leg.entry_px,
                                  datetime.now(IST).isoformat(), exit_px, reason,
                                  leg.execution_id)
            except Exception as exc:
                Log.warning(f"[{leg_key}] Failed to append trade log: {exc}")
            # Dispatched to the background executor, NOT called inline --
            # _finalize_close runs on the MAIN scheduler thread (see
            # _close_open_leg's synchronous call into it), and
            # notify_trade_closed makes a real blocking network call
            # (_post_json_local, 3s default timeout). Calling it inline here
            # would stall every OTHER leg's check for up to 3s on a slow/stuck
            # local Flask response -- exactly the class of bug this codebase
            # already fixed elsewhere for every other REST call on this thread.
            try:
                self._fill_executor.submit(notify_trade_closed, self.env, log_warning=Log.warning)
            except Exception as exc:
                # .submit() itself can raise (e.g. RuntimeError: can't start
                # new thread, a transient OS-level thread-creation hiccup --
                # confirmed NOT a leak, just occasional resource contention)
                # before it ever returns a Future. Uncaught, this crashed
                # _finalize_close and aborted the whole leg-close cycle in
                # production. This push is fire-and-forget/best-effort (see
                # notify_trade_closed's own docstring) -- losing one live SSE
                # nudge is harmless; crashing leg finalization over it is not.
                Log.warning(f"[{leg_key}] Failed to dispatch notify_trade_closed: {exc}")
        else:
            # Both the WS cache and the REST fallback failed at this exact
            # moment -- the exit already filled at the broker (that's the
            # only way this function is reached), so leaving leg.closed
            # False (exit_filled stays True) means _close_open_leg's own
            # `if leg.exit_filled: self._finalize_close(...)` guard retries
            # this same price resolution again next cycle, instead of
            # silently and permanently losing this trade's PnL/log row.
            Log.warning(f"[{leg_key}] Could not fetch exit LTP for trade log -- "
                        f"will retry next cycle instead of finalizing.")
            return

        self.price_stream.remove_instruments(
            [{"symbol": leg.symbol, "exchange": inst.options_exchange}]
        )
        leg.closed = True
        self._save_state()

    # ---- order error recovery (Retry / Cancel / Manually Completed) --------
    # See docs/prd/python-strategies-order-error-recovery.md for the full
    # design rationale behind every branch here. Direction-aware throughout:
    # the straddle's LONG legs enter via BUY/exit via SELL, the repair leg's
    # SHORT legs are the reverse -- _resolve_leg_key supplies `direction`.
    def _resolve_leg_error(self, leg_key: str, inst: InstrumentConfig, action: dict):
        if leg_key in self._pending_fills:
            # A Retry/Cancel resolution (or a resumed fill watcher) is already
            # in flight for this leg -- e.g. the user submitted a second
            # action (Cancel-again, or switching to Retry) before the first
            # one's background watcher finished (up to fill_poll_timeout,
            # ~60s). error_state isn't cleared until that watcher completes,
            # so without this guard a second call here would dispatch a
            # SECOND concurrent watcher/place() against the same order --
            # two threads racing modifyorder/cancelorder, or worse, a
            # duplicate order from a second terminal-retry place(). The new
            # action is simply left un-acked and gets picked up on a later
            # cycle once this leg's _pending_fills entry clears.
            return
        _, leg, direction = self._resolve_leg_key(leg_key)
        was_exit = leg.error_state == "exit_failed"
        kind = leg.error_kind
        entry_action = "BUY" if direction == "LONG" else "SELL"
        exit_action = "SELL" if direction == "LONG" else "BUY"

        if action["action"] == "retry":
            # The actual broker calls (reprice via modifyorder, or a fresh
            # place() for a terminal rejection) run on _fill_executor, not
            # inline -- a user clicking Retry must not block run_cycle's
            # per-cycle signal/exit checks for every other leg for a full
            # broker round-trip. Guard + ack happen here, synchronously and
            # cheaply, so run_cycle's error-check dispatch can't re-fire this
            # same action again on the next cycle while resolution is still
            # in flight.
            self._pending_fills.add(leg_key)
            ack_pending_action(self.env, leg_key)
            self._fill_executor.submit(
                self._do_retry_resolution, leg_key, inst, direction, was_exit, kind
            )
            return

        if action["action"] == "cancel":
            if was_exit:
                # Ignore the failed attempt entirely -- no broker-side action,
                # no further re-pricing, regardless of error_kind. Position
                # stays open; a fresh force-close pass places a brand-new
                # order normally later (see run_cycle's per-instrument checks).
                leg.exit_order_id = ""
                leg.error_state = ""
                leg.error_kind = ""
                leg.error_order_id = ""
                self._save_state()
                self._push_leg_error_bg(leg_key, leg, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            if kind == "terminal":
                # Nothing resting -- no broker call needed. Unlike the other 4
                # strategies' single-naked-leg shape, resetting this leg back
                # to blank (rather than clearing just the position) matches
                # this file's own original terminal-rejection behavior for
                # straddle/repair entries -- next cycle re-picks the strike
                # (straddle) or re-runs search-and-sell (repair) from scratch.
                if leg.symbol:
                    self.price_stream.remove_instruments(
                        [{"symbol": leg.symbol, "exchange": inst.options_exchange}]
                    )
                if leg_key.endswith("_repair"):
                    for field_name, value in asdict(RepairLeg()).items():
                        setattr(leg, field_name, value)
                else:
                    setattr(self.store.state.instruments[inst.name],
                            "pe" if leg_key.endswith("_PE") else "ce", OptionLeg())
                self._save_state()
                self._push_leg_error_bg(leg_key, leg, clear=True)
                ack_pending_action(self.env, leg_key)
                return
            # kind == "resting": one last honest re-price + bounded wait, then
            # an explicit cancelorder() if it still didn't fill -- never
            # silently abandoned (an untracked entry order filling later would
            # create a position from nothing). Can take up to one
            # fill_poll_timeout window, so runs on _fill_executor, not inline.
            # Ack'd here, immediately, NOT deferred until _watch_entry_cancel
            # finishes -- otherwise error_state stays set and a later cycle's
            # error-check could re-read this same still-"pending" action from
            # the platform and dispatch a SECOND concurrent _watch_entry_cancel
            # against the same order_id (two threads racing modifyorder/
            # cancelorder on one order). Mirrors how the "retry" branch above
            # already acks before dispatch.
            ack_pending_action(self.env, leg_key)
            self._pending_fills.add(leg_key)
            self._fill_executor.submit(
                self._watch_entry_cancel, leg_key, leg.error_order_id
            )
            return

        if action["action"] == "manual":
            fill_price = action["fill_price"]
            if was_exit:
                leg.exit_filled = True
                leg.manual_exit_px = fill_price
                # _close_open_leg's normal `if leg.exit_filled: self._finalize_close(...)`
                # path runs next cycle (or the per-instrument error-check pass
                # above) and uses manual_exit_px (see _finalize_close).
            else:
                leg.entry_filled = True
                leg.entry_px = fill_price
                if leg_key.endswith("_repair"):
                    pass  # no trade_count/traded_today concept for the repair leg itself
                else:
                    inst_state = self.store.state.instruments[inst.name]
                    if inst_state.pe.entry_filled and inst_state.ce.entry_filled:
                        inst_state.traded_today = True
            leg.error_state = ""
            leg.error_kind = ""
            leg.error_order_id = ""
            self._save_state()
            self._push_leg_error_bg(leg_key, leg, clear=True)
            ack_pending_action(self.env, leg_key)

    def _do_retry_resolution(self, leg_key: str, inst: InstrumentConfig, direction: str,
                              was_exit: bool, kind: str):
        """The actual broker calls behind a Retry action (reprice via
        modifyorder, or a fresh place() for a terminal rejection) -- moved
        off the main scheduler thread by _resolve_leg_error, which has
        already added leg_key to _pending_fills and ack'd the action before
        submitting this. Discards leg_key from _pending_fills itself UNLESS
        it hands off to _watch_entry_fill, which owns that guard from then
        on (mirrors _watch_entry_fill/_watch_exit_fill/_watch_entry_cancel's
        own finally-discard pattern)."""
        entry_action = "BUY" if direction == "LONG" else "SELL"
        exit_action = "SELL" if direction == "LONG" else "BUY"
        try:
            _, leg, _ = self._resolve_leg_key(leg_key)
            if was_exit:
                # _close_open_leg IS reliably called again on a later cycle
                # (run_cycle's per-instrument error-check pass, plus the
                # universal-exit-time/aggregate-PnL branches while they're
                # active) -- and its body unconditionally resumes watching
                # whatever exit_order_id is currently set. So the exit side
                # only needs the reprice (if resting) + clearing the error
                # fields; the normal flow does the rest.
                if kind == "resting":
                    # Cross the spread (ask for BUY, bid for SELL) rather than
                    # re-quote the last-traded price -- matches
                    # _reprice_and_wait_once's approach, which is what
                    # actually gets a resting order filled on a thin book.
                    bid, ask = fetch_symbol_bid_ask(self.ltp_client, leg.symbol, inst.options_exchange)
                    fresh_price = ask if exit_action == "BUY" else bid
                    if fresh_price is not None:
                        try:
                            self.client.modifyorder(
                                order_id=leg.error_order_id, strategy=self.env.strategy_tag,
                                symbol=leg.symbol, action=exit_action,
                                exchange=inst.options_exchange, price_type="LIMIT",
                                product=config.product, quantity=str(leg.quantity),
                                price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                            )
                        except Exception as exc:
                            Log.warning(f"[{leg_key}] Retry's reprice failed ({exc}) -- "
                                        f"resuming the watcher on the order as-is anyway.")
                else:  # kind == "terminal" -- nothing resting, must place fresh
                    leg.exit_order_id = ""  # _close_open_leg places a brand-new close order next cycle
                leg.error_state = ""
                leg.error_kind = ""
                leg.error_order_id = ""
                self._save_state()
                push_leg_error(self.env, leg_key, leg, clear=True)
                self._pending_fills.discard(leg_key)
                return

            # Entry side: unlike exit, nothing in run_cycle calls
            # _enter_straddle/_maybe_fire_repair again for a leg that already
            # has a symbol (has_position-equivalent is already true) --
            # Retry has to directly (re)submit the watcher itself.
            if kind == "resting":
                # Cross the spread (ask for BUY, bid for SELL) rather than
                # re-quote the last-traded price -- matches
                # _reprice_and_wait_once's approach, which is what actually
                # gets a resting order filled on a thin book.
                bid, ask = fetch_symbol_bid_ask(self.ltp_client, leg.symbol, inst.options_exchange)
                fresh_price = ask if entry_action == "BUY" else bid
                if fresh_price is not None:
                    try:
                        self.client.modifyorder(
                            order_id=leg.error_order_id, strategy=self.env.strategy_tag,
                            symbol=leg.symbol, action=entry_action,
                            exchange=inst.options_exchange, price_type="LIMIT",
                            product=config.product, quantity=str(leg.quantity),
                            price=str(fresh_price), disclosed_quantity="0", trigger_price="0",
                        )
                    except Exception as exc:
                        Log.warning(f"[{leg_key}] Retry's reprice failed ({exc}) -- resuming "
                                    f"the watcher on the order as-is anyway.")
                resume_order_id = leg.error_order_id
            else:  # kind == "terminal" -- nothing resting, place a genuinely new order
                # If THIS retry attempt's own place() fails, re-enter error
                # mode with a fresh message/timestamp (see _enter_error_mode's
                # own docstring -- it's explicitly designed to be called again
                # on a repeated failure) instead of falling through to the
                # outer except, which only logs and would leave the UI
                # showing the stale pre-retry error text.
                try:
                    resume_order_id = place(self.client, self.env.strategy_tag, leg.symbol,
                                             inst.options_exchange, entry_action, leg.quantity)
                except Exception as exc:
                    Log.exception(f"[{leg_key}] Retry's fresh place() failed again: {exc}")
                    self._enter_error_mode(leg_key, "entry_failed", "terminal", "", str(exc))
                    self._pending_fills.discard(leg_key)
                    return
                leg.entry_order_id = resume_order_id
            leg.error_state = ""
            leg.error_kind = ""
            leg.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, leg_key, leg, clear=True)
            # _watch_entry_fill owns _pending_fills for this leg_key from here.
            self._fill_executor.submit(self._watch_entry_fill, leg_key, resume_order_id)
        except Exception as exc:
            Log.exception(f"[{leg_key}] Retry resolution failed unexpectedly: {exc}")
            self._pending_fills.discard(leg_key)

    def _watch_entry_cancel(self, leg_key: str, order_id: str):
        """Entry-Cancel's one-last-chance flow for a still-`resting` order --
        never silently abandoned. Deliberately self-terminating (unlike the
        other watchers): ends in exactly one of two definitive outcomes, never
        back in error mode -- UNLESS something genuinely unexpected happens
        (see the outer except below), in which case it re-enters error mode
        rather than leaving the leg stuck. _pending_fills is ALWAYS released
        in the finally -- with _resolve_leg_error's re-entry guard now in
        place, a leg whose guard never clears would be permanently
        unresolvable via Retry/Cancel/Manual without a process restart."""
        strategy_tag = self.env.strategy_tag
        try:
            inst, leg, direction = self._resolve_leg_key(leg_key)
            entry_action = "BUY" if direction == "LONG" else "SELL"
            result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                             leg.symbol, inst.options_exchange, entry_action, leg.quantity)
            inst, leg, direction = self._resolve_leg_key(leg_key)  # re-fetch: may have changed while polling
            if leg.error_order_id != order_id:
                return  # superseded by a newer action/order in the meantime -- do nothing
            if result is not None:
                leg.entry_filled = True
                avg_price = float(result.get("average_price") or 0.0)
                if avg_price > 0:
                    leg.entry_px = avg_price
                if leg_key.endswith("_repair"):
                    pass
                else:
                    inst_state = self.store.state.instruments[inst.name]
                    if inst_state.pe.entry_filled and inst_state.ce.entry_filled:
                        inst_state.traded_today = True
                leg.error_state = ""
                leg.error_kind = ""
                leg.error_order_id = ""
                self._save_state()
                Log.info(f"[{leg_key}] Entry filled during Cancel's final chance: {leg.symbol} @ {leg.entry_px}")
            else:
                try:
                    self.client.cancelorder(order_id=order_id, strategy=strategy_tag)
                except Exception as exc:
                    Log.warning(f"[{leg_key}] cancelorder failed while abandoning entry "
                                f"({exc}) -- clearing local position anyway; verify "
                                f"manually at the broker that nothing is resting.")
                if leg.symbol:
                    self.price_stream.remove_instruments(
                        [{"symbol": leg.symbol, "exchange": inst.options_exchange}]
                    )
                if leg_key.endswith("_repair"):
                    for field_name, value in asdict(RepairLeg()).items():
                        setattr(leg, field_name, value)
                else:
                    setattr(self.store.state.instruments[inst.name],
                            "pe" if leg_key.endswith("_PE") else "ce", OptionLeg())
                self._save_state()
            # NOT ack_pending_action() here -- _resolve_leg_error's cancel/resting
            # branch already acked THIS action before dispatching us. Acking again
            # here would risk discarding a genuinely NEW action a user submitted
            # for this same leg while we were running (up to fill_poll_timeout),
            # since the platform's ack has no action-identity/token check -- it
            # would silently swallow that new action instead of leaving it for
            # the next cycle to pick up once _pending_fills clears in the finally.
            _, fresh_leg, _ = self._resolve_leg_key(leg_key)
            push_leg_error(self.env, leg_key, fresh_leg, clear=True)
        except Exception as exc:
            # _reprice_and_wait_once can raise (a RuntimeError if the order
            # landed rejected/cancelled during the wait, or an unguarded
            # client.orderstatus() failure) -- uncaught, that would leave this
            # leg's _pending_fills entry stuck forever, silently blocking all
            # future Retry/Cancel/Manual actions via _resolve_leg_error's
            # re-entry guard. Re-enter error mode instead so it stays
            # actionable; "resting" since we genuinely don't know the order's
            # final fate.
            Log.exception(f"[{leg_key}] Unexpected error during Cancel's final chance: {exc}")
            self._enter_error_mode(leg_key, "entry_failed", "resting", order_id, str(exc))
        finally:
            self._pending_fills.discard(leg_key)

    def _force_close_instrument(self, inst: InstrumentConfig, inst_state: InstrumentState, reason: str):
        """Force-closes every open leg of this instrument, regardless of the
        strategy's own signal/exit logic -- used by both the universal-exit-
        time and aggregate-pnl-breach triggers in run_cycle. SHORT (repair)
        legs are closed BEFORE LONG (straddle) legs, matching
        _handle_force_exit's SHORT-before-LONG order (2026-08-04: unified --
        this function previously closed LONG-then-SHORT specifically here).
        Explicit requirement: eliminate the higher-risk side first. A repair
        (SHORT) leg is a naked short option with undefined risk; a straddle
        (LONG) leg's risk is bounded by the premium already paid. If
        something goes wrong partway through this close sequence (a
        rejection, a stuck order), the safer residual state is a stuck LONG
        leg (bounded loss) rather than a stuck naked SHORT leg (unbounded
        loss)."""
        # If a repair leg is stuck in error_state, the straddle legs below
        # must NOT be closed this pass -- otherwise the hedge is removed
        # while the naked short stays open and unmanaged, exactly what the
        # SHORT-before-LONG requirement exists to prevent.
        instrument_blocked = False
        for leg_key_suffix, leg, direction in (
            ("PE_repair", inst_state.pe_repair, "SHORT"), ("CE_repair", inst_state.ce_repair, "SHORT"),
        ):
            if not leg.entry_filled or leg.closed:
                continue
            leg_key = f"{inst.name}_{leg_key_suffix}"
            if leg.error_state:
                Log.warning(f"[{leg_key}] Force-close ({reason}) waiting on an unresolved error "
                            f"({leg.error_state}/{leg.error_kind}) -- resolve it via "
                            f"Retry/Cancel/Manually Completed first.")
                instrument_blocked = True
                continue
            self._close_open_leg(inst, leg_key, leg, direction, reason)
            if leg.error_state:
                # _close_open_leg's place() can fail and enter error mode
                # DURING this same call -- the check above only reflects this
                # leg's state BEFORE the call. Without re-checking here, a
                # place() failure on this exact cycle would leave
                # instrument_blocked False, and the LONG loop right below
                # would then close the straddle in the very same pass,
                # defeating the SHORT-before-LONG guarantee on the first
                # rejection instead of the (already correct) next cycle.
                instrument_blocked = True

        for leg_key_suffix, leg, direction in (
            ("PE", inst_state.pe, "LONG"), ("CE", inst_state.ce, "LONG"),
        ):
            if not leg.entry_filled or leg.closed:
                continue
            leg_key = f"{inst.name}_{leg_key_suffix}"
            if instrument_blocked:
                Log.warning(f"[{leg_key}] Force-close ({reason}) holding this straddle leg open -- "
                            f"{inst.name}'s repair leg has an unresolved error and must be "
                            f"resolved first (SHORT-before-LONG requirement).")
                continue
            if leg.error_state:
                Log.warning(f"[{leg_key}] Force-close ({reason}) waiting on an unresolved error "
                            f"({leg.error_state}/{leg.error_kind}) -- resolve it via "
                            f"Retry/Cancel/Manually Completed first.")
                continue
            self._close_open_leg(inst, leg_key, leg, direction, reason)

        # Only mark this instrument fully exited once every entry_filled leg
        # actually closed. _close_open_leg now catches a rejected/cancelled
        # exit order and returns cleanly (rather than raising) so a stuck
        # leg doesn't abort the whole cycle -- but that means this function
        # always reaches this point regardless of whether a leg's close
        # actually succeeded. Setting exited_today=True unconditionally here
        # would permanently stop retrying a leg that's still open at the
        # broker (run_cycle skips any instrument with exited_today=True).
        # Checking .closed on every entry_filled leg keeps the retry alive
        # next cycle for whichever leg(s) didn't finish.
        all_closed = all(
            leg.closed for leg in
            (inst_state.pe, inst_state.ce, inst_state.pe_repair, inst_state.ce_repair)
            if leg.entry_filled
        )
        if all_closed:
            inst_state.exited_today = True
        self._save_state()

    def _aggregate_unrealized_pnl(self, inst: InstrumentConfig,
                                   inst_state: InstrumentState) -> Optional[float]:
        total = 0.0
        any_open = False
        for leg, direction in ((inst_state.pe, "LONG"), (inst_state.ce, "LONG"),
                                (inst_state.pe_repair, "SHORT"), (inst_state.ce_repair, "SHORT")):
            # exit_filled-but-not-closed means the exit is already confirmed
            # at the broker and only waiting on a price-resolution retry
            # (see _finalize_close) -- economically flat already, so it must
            # NOT count as "open" here. Otherwise a leg stuck retrying its
            # exit price (e.g. an illiquid near-zero-premium option late in
            # the session) can make this whole function return None every
            # cycle, silently disabling the aggregate-PnL stop-loss check for
            # every OTHER leg of this instrument too.
            if not leg.entry_filled or leg.closed or leg.exit_filled:
                continue
            any_open = True
            # Live option LTP from the WebSocket cache (pushed, not polled)
            # -- falls back to a single one-off REST quotes() call for
            # just this symbol if the feed is stale/missing.
            ltp = self.price_stream.get_ltp(leg.symbol, inst.options_exchange, max_age=config.ws_stale_seconds)
            if ltp is None:
                ltp = fetch_symbol_ltp(self.ltp_client, leg.symbol, inst.options_exchange, require_two_sided=True)
            if ltp is None:
                return None  # can't compute a trustworthy aggregate this cycle
            total += (ltp - leg.entry_px) * leg.quantity if direction == "LONG" else (leg.entry_px - ltp) * leg.quantity
        return total if any_open else None

    def report_pnl_tick(self):
        """Runs on its OWN scheduler job at config.pnl_tick_interval (0.8s),
        completely decoupled from run_cycle's scheduler_interval (10s). PnL
        is purely observational -- it never feeds a trading decision (the
        real decision-affecting aggregate, used for the universal_exit_pnl
        breach check, stays in _aggregate_unrealized_pnl, untouched) -- so
        this can refresh far more often than the main cycle without any of
        the blocking-call risk scheduler_interval exists to protect
        against. Reads the WebSocket price cache first, falling back to a
        THROTTLED REST quotes() call (at most once per
        config.pnl_rest_fallback_interval_sec per leg) only once the WS
        cache has gone stale -- frequent enough to recover visibility
        during a genuine broker-side outage, rare enough that this 0.8s
        job doesn't spam the broker for the outage's whole duration. Falls
        back further to the last successfully-fetched price (WS or REST)
        if even the throttled REST attempt fails or isn't due yet, so a
        leg stays visible with its best-known price rather than
        disappearing outright."""
        try:
            open_positions = []
            for inst in INSTRUMENTS:
                inst_state = self.store.state.instruments[inst.name]
                for leg_key_suffix, leg, direction in (
                    ("PE", inst_state.pe, "LONG"), ("CE", inst_state.ce, "LONG"),
                    ("PE_repair", inst_state.pe_repair, "SHORT"), ("CE_repair", inst_state.ce_repair, "SHORT"),
                ):
                    if not leg.entry_filled or leg.closed:
                        continue
                    leg_key = f"{inst.name}_{leg_key_suffix}"
                    ltp = self.price_stream.get_ltp(
                        leg.symbol, inst.options_exchange, max_age=_current_ws_stale_threshold()
                    )
                    if ltp is not None:
                        self._pnl_last_known_price[leg_key] = ltp
                    else:
                        now = datetime.now(IST)
                        last_attempt = self._pnl_rest_fallback_last_attempt.get(leg_key)
                        due = (last_attempt is None or (now - last_attempt).total_seconds()
                               >= config.pnl_rest_fallback_interval_sec)
                        if due:
                            self._pnl_rest_fallback_last_attempt[leg_key] = now
                            rest_ltp = fetch_symbol_ltp(self.ltp_client, leg.symbol, inst.options_exchange, require_two_sided=True)
                            if rest_ltp is not None:
                                self._pnl_last_known_price[leg_key] = rest_ltp
                        ltp = self._pnl_last_known_price.get(leg_key)
                    if ltp is None:
                        continue
                    pnl = ((ltp - leg.entry_px) * leg.quantity if direction == "LONG"
                           else (leg.entry_px - ltp) * leg.quantity)
                    open_positions.append({
                        "leg_key": leg_key, "symbol": leg.symbol, "direction": direction,
                        # Display quantity signed negative for a SHORT leg so
                        # it reads correctly in the Trades/PnL UI -- the pnl
                        # calc above already uses the unsigned leg.quantity
                        # with its own direction-based sign flip.
                        "quantity": -leg.quantity if direction == "SHORT" else leg.quantity,
                        "entry_price": leg.entry_px, "current_price": ltp, "pnl": pnl,
                        "entry_time": leg.entry_time, "execution_id": leg.execution_id,
                    })
            try:
                self._pnl_executor.submit(
                    report_pnl_to_platform, self.env, self.store.state.today_realized_pnl,
                    open_positions,
                )
            except Exception as exc:
                # .submit() itself can raise (transient thread-creation
                # hiccup, same class as notify_trade_closed's dispatch) --
                # never let it crash this 0.8s job.
                Log.warning(f"Failed to dispatch report_pnl_to_platform: {exc}")
        except Exception as exc:
            Log.exception(f"report_pnl_tick failed: {exc}")

    # ---- main cycle -----------------------------------------------------
    def _refresh_force_exit_check_bg(self):
        """Dispatches check_force_exit (a synchronous local HTTP call) to the
        background executor every cycle instead of running it inline on the
        main scheduler thread -- guarded so a slow check that outlives one
        scheduler_interval isn't resubmitted on top of itself. In the normal/
        fast case (a local unix-socket call, typically well under a second)
        this means a fresh check completes every single cycle, giving
        essentially real-time Force Exit detection while still never
        blocking run_cycle even in a rare slow-local-HTTP case."""
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
        """Force-closes every leg currently holding a position, regardless of
        the strategy's own signal/exit logic -- called from run_cycle while a
        Force Exit is pending (see check_force_exit). SHORT (repair) legs are
        closed BEFORE LONG (straddle) legs, per explicit requirement for this
        button -- _force_close_instrument (used by the universal-exit-time/
        aggregate-pnl-breach triggers) now uses this same SHORT-before-LONG
        order (2026-08-04: unified; it previously closed LONG-then-SHORT).
        Reuses _close_open_leg, so it's idempotent/resumable across cycles
        and a leg already in error mode is left untouched (the user must
        resolve it via Retry/Cancel/Manual first). Returns True only once
        every leg is fully flat, which is what lets run_cycle report
        completion back to the platform."""
        all_flat = True
        for inst in INSTRUMENTS:
            inst_state = self.store.state.instruments[inst.name]

            # Repair (SHORT) legs first. If one is stuck in error_state, this
            # instrument's straddle (LONG) legs below must NOT be closed this
            # pass -- otherwise the hedge is removed while the naked short
            # stays open and unmanaged, exactly what the SHORT-before-LONG
            # requirement exists to prevent.
            instrument_blocked = False
            for leg_key_suffix, leg, direction in (
                ("PE_repair", inst_state.pe_repair, "SHORT"), ("CE_repair", inst_state.ce_repair, "SHORT"),
            ):
                if not leg.entry_filled or leg.closed:
                    continue
                leg_key = f"{inst.name}_{leg_key_suffix}"
                if leg.error_state:
                    Log.warning(f"[{leg_key}] Force Exit waiting on an unresolved error "
                                f"({leg.error_state}/{leg.error_kind}) -- resolve it via "
                                f"Retry/Cancel/Manually Completed first.")
                    all_flat = False
                    instrument_blocked = True
                    continue
                all_flat = False
                self._close_open_leg(inst, leg_key, leg, direction, "force_exit")
                if leg.error_state:
                    # _close_open_leg's place() can fail and enter error mode
                    # DURING this same call -- the check above only reflects
                    # this leg's state BEFORE the call. Without re-checking
                    # here, a place() failure on this exact cycle would leave
                    # instrument_blocked False, and the LONG loop right below
                    # would then close the straddle in the very same pass,
                    # defeating the SHORT-before-LONG guarantee on the first
                    # rejection instead of the (already correct) next cycle.
                    instrument_blocked = True

            for leg_key_suffix, leg, direction in (
                ("PE", inst_state.pe, "LONG"), ("CE", inst_state.ce, "LONG"),
            ):
                if not leg.entry_filled or leg.closed:
                    continue
                leg_key = f"{inst.name}_{leg_key_suffix}"
                if instrument_blocked:
                    Log.warning(f"[{leg_key}] Force Exit holding this straddle leg open -- "
                                f"{inst.name}'s repair leg has an unresolved error and must be "
                                f"resolved first (SHORT-before-LONG requirement).")
                    all_flat = False
                    continue
                if leg.error_state:
                    Log.warning(f"[{leg_key}] Force Exit waiting on an unresolved error "
                                f"({leg.error_state}/{leg.error_kind}) -- resolve it via "
                                f"Retry/Cancel/Manually Completed first.")
                    all_flat = False
                    continue
                all_flat = False
                self._close_open_leg(inst, leg_key, leg, direction, "force_exit")
        return all_flat

    def run_cycle(self):
        try:
            self._reset_day_if_needed()
            self._repush_active_errors()

            self._refresh_force_exit_check_bg()
            if self._force_exit_pending:
                # _handle_force_exit leaves any leg already in error_state
                # untouched (Force Exit doesn't override an unresolved
                # Retry/Cancel/Manual decision) -- but this branch returns
                # right after, and while force_exit_pending stays True this
                # is the ONLY branch that runs. Without checking here too, a
                # user's Retry/Cancel/Manual click on that errored leg would
                # never be consumed, permanently deadlocking both the leg and
                # Force Exit until a manual restart. Resolve it first, same
                # as the per-instrument error-check loop elsewhere in this
                # function, so a just-resolved leg can be force-closed in
                # this same cycle by _handle_force_exit right after.
                for inst in INSTRUMENTS:
                    inst_state = self.store.state.instruments[inst.name]
                    for leg_key, leg in (
                        (f"{inst.name}_PE", inst_state.pe),
                        (f"{inst.name}_CE", inst_state.ce),
                        (f"{inst.name}_PE_repair", inst_state.pe_repair),
                        (f"{inst.name}_CE_repair", inst_state.ce_repair),
                    ):
                        if leg.error_state:
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

            today = datetime.now(IST).date()
            now_time = datetime.now(IST).time()
            past_universal_exit_time = self._past_universal_exit_time()

            for inst in INSTRUMENTS:
                inst_state = self.store.state.instruments[inst.name]

                # Check pending actions for any leg currently in error mode, and
                # finalize any leg whose exit already completed in the background
                # but hasn't been written to the trade log yet -- both regardless
                # of what phase this instrument is in below (exited_today/
                # traded_today/aggregate PnL), since an errored or
                # filled-but-not-yet-finalized leg must always get resolved.
                # Without this, a leg force-closed for an aggregate-PnL breach
                # that then recovers before the async fill lands would never be
                # revisited by _close_open_leg again (only the universal-exit-time
                # and aggregate-PnL branches below call it). See
                # docs/prd/python-strategies-order-error-recovery.md.
                for leg_key, leg, direction in (
                    (f"{inst.name}_PE", inst_state.pe, "LONG"),
                    (f"{inst.name}_CE", inst_state.ce, "LONG"),
                    (f"{inst.name}_PE_repair", inst_state.pe_repair, "SHORT"),
                    (f"{inst.name}_CE_repair", inst_state.ce_repair, "SHORT"),
                ):
                    if leg.error_state:
                        self._refresh_pending_action_bg(leg_key)
                        pending = self._pop_pending_action(leg_key)
                        if pending is not None:
                            self._resolve_leg_error(leg_key, inst, pending)
                    elif leg.exit_filled and not leg.closed:
                        self._finalize_close(inst, leg_key, leg, direction, reason="force_close")

                if inst_state.exited_today:
                    continue

                if past_universal_exit_time:
                    if inst_state.traded_today:
                        Log.warning(f"[{inst.name}] Universal exit time reached; force-closing.")
                        self._force_close_instrument(inst, inst_state, reason="universal_exit_time")
                    continue

                if inst_state.traded_today:
                    agg_pnl = self._aggregate_unrealized_pnl(inst, inst_state)
                    if agg_pnl is not None and agg_pnl <= config.universal_exit_pnl:
                        Log.warning(f"[{inst.name}] Aggregate unrealized PnL {agg_pnl:.0f} breached "
                                    f"{config.universal_exit_pnl} -> force-closing.")
                        self._force_close_instrument(inst, inst_state, reason="universal_exit_pnl")
                        continue

                    self._maybe_fire_repair_bg(inst, inst_state, "PE", inst_state.pe, inst_state.pe_repair)
                    self._maybe_fire_repair_bg(inst, inst_state, "CE", inst_state.ce, inst_state.ce_repair)
                    continue

                if not (now_time > config.entry_start) and not config.test_mode:
                    continue

                cached_expiry = self._expiry_cache.get(inst.name)
                if cached_expiry is None:
                    # Not populated yet -- dispatch the fetch in the
                    # background and skip entry THIS cycle rather than
                    # blocking every other instrument's check on the main
                    # scheduler thread for a real client.expiry() round-trip.
                    self._refresh_week_expiry_bg(inst)
                    continue
                _, expiry_date = cached_expiry
                if expiry_date != today and not config.test_mode:
                    continue  # not this instrument's expiry day -- no entry

                # Live underlying LTP from the WebSocket cache (pushed, not
                # polled) -- falls back to a single one-off REST quotes()
                # call for just this instrument if the feed is stale/missing.
                spot = self.price_stream.get_ltp(
                    inst.name, inst.underlying_exchange, max_age=config.ws_stale_seconds
                )
                if spot is None:
                    spot = fetch_ltp(self.ltp_client, inst)
                if spot is None:
                    continue

                condition_desc = (
                    f"now={now_time} > entry_start={config.entry_start}, "
                    f"expiry_date={expiry_date} == today={today}, spot={spot:.2f}"
                )
                self._enter_straddle_bg(inst, inst_state, spot, condition_desc=condition_desc)

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
    print(f"Entry (once/day)     : > {config.entry_start}, ONLY on that instrument's own expiry day")
    print(f"Universal exit       : PnL <= {config.universal_exit_pnl} OR time >= {config.universal_exit_time}")
    print(f"Repair               : fires immediately once the straddle leg on that side fills")
    print("⚠️  LONG STRADDLE THAT CONVERTS TO NAKED SHORT VIA REPAIR -- RISK PROFILE CHANGES MID-TRADE ⚠️")
    if config.test_mode:
        print("⚠️  TEST MODE ENABLED -- market-hours/entry-day/entry-time checks are BYPASSED")
    print("=" * 70)


def main():
    print_banner()

    env = Environment()
    _migrate_trade_log_if_needed(env.strategy_tag)
    state_store = StateStore(env)
    state_store.load()

    # New execution number for this process run -- see OptionLeg.execution_id
    # and the Trades UI's execution dropdown.
    state_store.state.last_execution_id += 1
    execution_id = state_store.state.last_execution_id
    state_store.save()

    broker = Broker(env)
    client = broker.connect()
    ltp_client = broker.connect_ltp_client()

    price_stream = PriceStream(client)
    price_stream.start()

    # Fixed set: both underlyings, needed continuously for ATM strike
    # selection at entry. Dynamic set: any leg already entry_filled and not
    # closed from a same-day restart (straddle and/or repair legs that
    # opened earlier today before this process started).
    seed_instruments = [
        {"symbol": inst.name, "exchange": inst.underlying_exchange} for inst in INSTRUMENTS
    ]
    today_key = datetime.now(IST).date().isoformat()
    if state_store.state.current_day == today_key:
        for inst in INSTRUMENTS:
            inst_state = state_store.state.instruments[inst.name]
            for leg in (inst_state.pe, inst_state.ce, inst_state.pe_repair, inst_state.ce_repair):
                if leg.entry_filled and not leg.closed:
                    seed_instruments.append({"symbol": leg.symbol, "exchange": inst.options_exchange})
    price_stream.add_instruments(seed_instruments)

    print()
    print("=" * 70)
    print("HEALTH CHECK")
    print("=" * 70)
    print(f"OpenAlgo Connected : {broker.connected}")
    print(f"State File         : OK ({state_store.path})")
    print(f"Execution ID       : {execution_id}")
    print(f"Price Stream       : starting ({seed_instruments})")
    print("=" * 70)

    engine = StrategyEngine(client, state_store, env, price_stream, execution_id=execution_id,
                             ltp_client=ltp_client)

    # Restart while a leg was in error mode: STRATEGY_ERRORS on the platform
    # side is in-memory only, so re-push any already-erroring leg once here --
    # otherwise the UI badge would silently vanish across a restart even
    # though the leg is still frozen awaiting a decision. See
    # docs/prd/python-strategies-order-error-recovery.md.
    for inst_name, inst_state in state_store.state.instruments.items():
        for suffix, leg in (("PE", inst_state.pe), ("CE", inst_state.ce),
                            ("PE_repair", inst_state.pe_repair), ("CE_repair", inst_state.ce_repair)):
            if leg.error_state:
                leg_key = f"{inst_name}_{suffix}"
                direction = "SHORT" if suffix.endswith("_repair") else "LONG"
                entry_action = "BUY" if direction == "LONG" else "SELL"
                exit_action = "SELL" if direction == "LONG" else "BUY"
                action = entry_action if leg.error_state == "entry_failed" else exit_action
                push_leg_error(env, leg_key, leg, action=action)
                Log.error(f"[{leg_key}] Resuming with an unresolved error from before restart "
                          f"({leg.error_state}/{leg.error_kind}) -- "
                          f"needs Retry/Cancel/Manually Completed.")

    Log.info("Strategy Initialization Complete. Starting scheduler...")

    scheduler = BlockingScheduler(timezone=IST)
    scheduler.add_job(
        engine.run_cycle,
        trigger=IntervalTrigger(seconds=config.scheduler_interval),
        id="strategy_cycle",
        max_instances=1,
        coalesce=True,
    )
    # Separate, faster job purely for PnL -- see report_pnl_tick's own
    # docstring for why this is safe to run far more often than the main
    # strategy cycle (cache-only, never a REST call).
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
        # scheduler.start() shouldn't normally raise anything else (job
        # exceptions are caught/logged by APScheduler itself), but if it
        # ever does, run the exact same cleanup instead of leaking the
        # WebSocket connection and both thread pools silently.
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
