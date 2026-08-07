# CRUDEOIL 1H VWAP/EMA20 Crossover — Positional Long Options

Planning doc, written before implementation per `AUTHORING_CHECKLIST.md`'s own
recommendation to diff new scripts against the reference implementation
function-by-function rather than write from scratch. Delete once the strategy
is built and deployed (this is a planning artifact, not documentation to keep).

## Final spec (agreed with user, 2026-08-05)

| Parameter | Value |
|---|---|
| Instrument | CRUDEOIL futures (MCX), 1H candles |
| Indicators | Session VWAP (daily reset), EMA(20), RSI(14) |
| Product | NRML (positional, no daily square-off) |
| Entry evaluation | Closed 1H candles only, whole MCX session (09:00-23:30) |
| Crossover filters (both required) | (1) 2-candle VWAP slope >= 0.10% of price per candle, AND (2) absolute VWAP-EMA20 gap at the crossover candle >= 0.05% of futures price |
| CALL entry | VWAP crosses above EMA20 + both filters pass + RSI(14) > 50 -> BUY nearest strike >=100pts OTM CE |
| PUT entry | VWAP crosses below EMA20 + both filters pass + RSI(14) < 50 -> BUY nearest strike >=100pts OTM PE |
| CALL exit | VWAP crosses back below EMA20 |
| PUT exit | VWAP crosses back above EMA20 |
| Reversal | Exit + opposite entry on the same cycle, not staggered |
| Max position | One leg open at a time (CALL or PUT, never both) |
| Max trades/day | Unlimited -- no cap, flips only as often as genuine crossovers occur |
| Quantity | 2 lots |
| Expiry roll | 4 trading days before expiry: close current position (if open) and carry the active signal to the next monthly expiry's equivalent strike |
| VWAP warm-up | Skip candles until VWAP has >=3 bars for the session |

## Reference implementation

`MCX_CrudeOil_EMA9_RSI_Intraday_1_20260723150000.py` -- same instrument, same
broker/exchange plumbing, same PriceStream/fill-watcher/error-recovery/PnL
infra. Reused near-verbatim except where the spec genuinely differs (below).
`AUTHORING_CHECKLIST.md` followed throughout -- every item there was a real,
confirmed production bug in an earlier script.

## What's reused verbatim (proven infra, no reason to reinvent)

- `Broker`, `PriceStream` (ws + REST-fallback + watchdog), `StateStore`
  (atomic JSON save/load), `Environment` (env var loading + validation).
- `resolve_current_month_futures`, `fetch_symbol_ltp`,
  `fetch_symbol_bid_ask`, `_is_error_response`.
- `fetch_chain`, `_legs_with_strike` -- option chain fetch + filtering.
  `pick_atm_leg` is NOT reused (that picks nearest-to-spot; this strategy
  needs nearest->=100pt-OTM, see below).
- `OrderNeedsAttention`, `_reprice_and_wait_once`, `poll_fill`, `place` --
  order placement/fill-confirmation/reprice-on-partial machinery, unchanged.
  `place()`'s retry-only-clean-rejection rule applies identically regardless
  of BUY vs SELL.
- Trade log writer thread (`_migrate_trade_log_if_needed`,
  `_trade_log_writer_loop`, `_ensure_trade_log_thread`, `append_trade_log`).
- All `strategy_reporting` HTTP helpers verbatim:
  `_post_json_local`/`_get_json_local` (already targeting
  `STRATEGY_REPORTING_PORT`, not `FLASK_PORT`/socket -- checklist item),
  `report_pnl_to_platform`, `push_leg_error`, `check_pending_action`,
  `ack_pending_action`, `check_force_exit`, `ack_force_exit_complete`.
- `StrategyEngine`'s background-executor pattern: `_fill_executor`,
  `_bg_executor`, `_pnl_executor` (or reuse just `_bg_executor` for
  everything non-fill per the checklist's "reuse whichever already exists,
  don't add a redundant pool" -- MCX itself has a dead `_pnl_executor` per
  the 2026-08-04 note in `report_pnl_tick`'s own comment, so THIS script
  should NOT create one; `report_pnl_tick`'s isolation comes from being its
  own APScheduler job, not a dedicated pool).
- `_push_leg_error_bg`, `_refresh_pending_action_bg`, `_pop_pending_action`,
  `_repush_active_errors`, `_resolve_leg_error`, `_do_retry_resolution`,
  `_watch_entry_cancel`, `_refresh_force_exit_check_bg`, `_handle_force_exit`
  -- error-recovery state machine, unchanged in shape. `_resolve_leg_error`'s
  manual-completion branch calls `_finalize_exit`/entry-equivalent directly
  (checklist item -- immediate finalize, not next-cycle).
- `reconcile_pending_orders()` at startup, before the scheduler starts.

## What's genuinely new/different

### 1. Long options, not naked selling -- action direction flips
Every `place(..., action="SELL", ...)` for entry and `action="BUY"` for exit
in MCX becomes `action="BUY"` for entry and `action="SELL"` for exit here.
This also changes the risk framing entirely: bounded risk (premium paid),
not undefined risk -- the checklist's "close SHORT/naked legs before LONG
legs" ordering item doesn't apply (there is no short leg in this strategy at
all), and the "NAKED OPTION SELLING -- UNDEFINED RISK" banner MCX prints does
NOT apply here; banner should instead note "LONG OPTIONS -- risk capped at
premium paid, position is positional (NRML, no daily square-off)".

### 2. Positional (NRML), no universal_exit_time
MCX's `_past_universal_exit()`/force-close-at-time-X branch is **entirely
removed**, not adapted. Per the agreed spec, exit is signal-based only
(opposite crossover) or expiry-roll-triggered -- there is no daily
time-based close. This is a deliberate, explicit deviation from the
checklist's "product/universal_exit_time chosen together" rule, which
assumes every strategy closes out same-day or has *some* time-based
backstop; this one intentionally has neither, by spec (see user's "its
positional one" from this planning session). `run_cycle()` therefore has
one fewer branch than MCX's three-way (force-exit-pending /
past-universal-exit / normal) structure -- just force-exit-pending / normal.

### 3. Single leg (CALL xor PUT), with same-cycle reversal
MCX manages two concurrent legs (PE + CE) independently keyed by `leg_key`.
This strategy has exactly one conceptual leg whose *identity* (CE or PE)
flips on a reversal signal. Simpler state (`StrategyState.leg: LegPosition`,
not a dict of legs) but the reversal transition needs care:
- On a reversal signal (e.g. currently long CE, VWAP crosses back down ->
  PUT entry conditions also just became true on the same candle): call
  `_exit_leg()` for the open CE first, and only submit the new PUT entry
  once `_watch_exit_fill` confirms the CE exit filled (not before) -- doing
  both submissions in the same cycle without waiting for the first fill
  would violate "max position: one" if the entry fills before the exit
  does. Track a `_pending_reversal_to: Optional[str]` (the option_type to
  enter once the current exit confirms) set at exit-submission time, and
  have `_watch_exit_fill`'s completion callback trigger the new entry
  instead of `run_cycle`'s next pass waiting on a candle boundary --
  matches the checklist's "nothing that finalizes a fill may be gated
  behind a candle boundary" rule applied to the reversal-entry step too.

### 4. New signal computation -- VWAP/EMA20/RSI14 crossover with dual filter
Replaces `compute_instrument_signal`'s EMA9/RSI9 computation entirely.
Needs:
- Session VWAP on 1H candles, resetting at MCX session open (09:00 IST) --
  `ta.vwap`-equivalent computed manually as
  `cumsum(typical_price * volume) / cumsum(volume)` restarted each session,
  since 1H bars spanning a session boundary need the reset applied at the
  bar level (check `openalgo.ta` for an existing session-aware VWAP helper
  before hand-rolling one -- MCX/EMA9 doesn't need this, no existing
  in-repo precedent to copy from directly).
- EMA(20) computed continuously across day boundaries (same `ta.ema`
  pattern as MCX's EMA34/EMA9, just on 1H bars with period 20 -- a rolling
  indicator, unaffected by session resets, unlike VWAP).
- RSI(14) on 1H closes -- same `ta.rsi` call MCX already uses, period 14.
- Crossover detection: `vwap_prev2 vs ema20_prev2` sign compared against
  `vwap_prev1 vs ema20_prev1` sign (a sign flip = crossover on this candle),
  mirroring the EMA34_RSI-style two-candle comparison pattern already used
  elsewhere in this repo's strategies, adapted from EMA-vs-EMA to VWAP-vs-EMA.
- Slope filter: `(vwap_prev1 - vwap_prev2) / 2`, expressed as `% of
  futures_price per candle`, must exceed 0.10%.
- Gap filter: `abs(vwap_prev1 - ema20_prev1) / futures_price` at the
  crossover candle must exceed 0.05%.
- VWAP warm-up: skip candles until the current session has >=3 bars
  contributing to VWAP (a fresh day's VWAP with 1-2 bars is not meaningful).

### 5. Strike selection -- nearest listed strike >=100pts OTM, not ATM
`pick_atm_leg` picks nearest-to-spot. This strategy needs: from the fetched
chain, filter strikes on the OTM side (above spot for CE, below spot for
PE), then pick the one whose distance from spot is closest to but not less
than 100 points (i.e. `min(strike for strike in otm_strikes if abs(strike -
spot) >= 100, key=lambda s: abs(s - spot))`). New helper,
`pick_otm_leg(chain, option_type, spot, min_points=100)`, replacing
`pick_atm_leg` for this script only (MCX's own `pick_atm_leg` stays
untouched -- this is a new function, not a modification to the shared one,
since other deployed scripts may still rely on ATM selection).

### 6. Expiry roll -- genuinely new, no existing precedent to copy
Runs once per day (cheap check, e.g. at the top of `run_cycle` or its own
lightweight per-cycle check): if today is >=4 trading days before the
currently-held option's expiry AND a position is open, close it
(`_exit_leg` with `reason="expiry_roll"`) and, once the exit fill confirms,
immediately re-enter the SAME option_type on the NEXT monthly expiry at
that moment's freshly-computed nearest->=100pt-OTM strike (re-run
`resolve_current_month_futures`-equivalent for next month + `fetch_chain` +
`pick_otm_leg`). If no position is open when the 4-day threshold is
crossed, there's nothing to roll -- the next fresh entry simply resolves
against whichever expiry is current at that time (a "days to expiry" check
folded into expiry resolution, so a fresh entry inside the roll window
goes straight to next month rather than entering current-about-to-roll
expiry and immediately needing to roll again).

"4 trading days" needs a trading-day-count helper (skip Sat/Sun, and MCX
holiday calendar if one already exists in this repo -- check
`utils.market_hours`/similar before hand-rolling holiday logic; if none
exists, calendar-day count is an acceptable approximation and should be
noted as such in a comment, not silently assumed exact).

**Expiry format -- confirmed 2026-08-05 by reading `resolve_current_month_expiry`/
`resolve_current_month_futures`/`_compact_expiry` directly (not assumed):**
the broker's `client.expiry(symbol=inst.name, exchange=..., instrumenttype=
"options")` returns dates as `"DD-Mon-YY"` (e.g. `"17-Aug-26"`), parsed via
`strptime(raw, "%d-%b-%y")`. `_compact_expiry()` strips the dashes and
uppercases to the SHORT form used in every symbol string throughout this
codebase: `"17AUG26"` (2-digit day, 3-letter month, **2-digit year** -- not
4-digit). Reuse `_compact_expiry` verbatim; never hand-format a date string.

CRUDEOIL options are MONTHLY only (confirmed by the reference function's own
name/docstring -- no weekly expiry list to filter out). This means "next
month's expiry" for the roll is simply the **next entry in the same sorted
list** `client.expiry(...)` already returns, not something requiring
hand-rolled month arithmetic: fetch the list once, find the currently-held
expiry's index, `next_expiry = _compact_expiry(dates_raw[index + 1])`. Only
the "is today >=4 trading days before the CURRENT expiry" check needs actual
date math (comparing `datetime.now(IST).date()` against the parsed expiry
date) -- the month-rollover target itself does not.

**Confirmed non-blocking, by reading the actual code (not the skeleton) --
2026-08-05, in response to explicit "no run check blocking" instruction:**
- `run_cycle()`'s full body read end-to-end: every potentially-slow branch
  (`_refresh_mcx_session_window_bg`, `_refresh_force_exit_check_bg`,
  `_refresh_pending_action_bg`) is already dispatched to a background
  executor; `_enter_leg`/`_exit_leg` submit the order synchronously (fast --
  a single REST call to place, not to wait for a fill) and hand fill-waiting
  off to `_fill_executor` via `_watch_entry_fill`/`_watch_exit_fill`.
- `get_signal()` (the one place that could plausibly block on
  `client.history()`) read in full: it ONLY ever reads from
  `self._signal_cache` (in-memory) and fire-and-forget `.submit()`s a
  refresh to `_fill_executor` when the cached candle is stale for the
  current boundary -- `run_cycle` never itself waits on that network call.
  This pattern generalizes directly to a 1H strategy: swap
  `_current_candle_boundary(3)` (MCX's 3-minute candles) for
  `_current_candle_boundary(60)` (60-minute), same caching shape otherwise.
- The one inline synchronous network call in MCX's `run_cycle` is a
  single best-effort `fetch_symbol_ltp()` REST fallback, and ONLY when the
  WebSocket price cache is stale/missing -- not chronic, not every cycle.
  Same pattern reused here, unchanged.
- `scheduler_interval: int = 10` confirmed (10 seconds) -- comfortably
  within "runs within 1 minute," and safe to reuse unchanged given the
  above confirms zero blocking calls in the cycle path regardless of the
  1H signal timeframe (candle-boundary caching means most 10s cycles do
  zero network work at all, same as MCX today).

## Config dataclass additions (beyond MCX's `Config`)

```python
@dataclass
class Config:
    ema_period: int = 20
    rsi_period: int = 14
    vwap_slope_pct_threshold: float = 0.10   # % of price per candle, over 2 candles
    vwap_gap_pct_threshold: float = 0.05     # % of price, at crossover candle
    min_otm_points: float = 100.0
    quantity_lots: int = 2
    expiry_roll_days_before: int = 4
    intraday_interval: str = "1h"
    # NOT present: universal_exit_time, mis-square-off buffer (NRML, no daily close)
```

## Verification plan (per checklist section 5)

1. `uv run python -m py_compile strategies/deployed/<filename>.py`
2. Diff `run_cycle`, `_resolve_leg_error`, `_repush_active_errors` against
   MCX's function-by-function -- confirm nothing MCX-specific (two-leg dict
   iteration, universal-exit branch, SELL-to-enter) leaked through unchanged
   by copy-paste.
3. Confirm the reversal same-cycle sequencing (exit-confirms-before-entry)
   with a targeted read-through, since this is the one piece of run_cycle
   logic with no existing precedent in any deployed script to copy from.
4. Full pytest suite (existing baseline: 10 failed / 994 passed / 4 skipped
   / 14 errors on this dev machine -- confirm unchanged, this new script
   adds no new test dependencies).
