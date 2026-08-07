# NIFTY Opening Range Option Selling — Intraday Strategy Plan

Source spec: "Opening Range Option Selling Strategy" playbook (7-page PDF,
supplied by user, 2026-08-06). Planning doc written before implementation,
per `strategies/deployed/AUTHORING_CHECKLIST.md`'s own recommendation to
diff a new script against a reference implementation function-by-function
rather than write from scratch. Delete once the strategy is built and
deployed (planning artifact, not documentation to keep).

## Spec, as given (and corrected per user, 2026-08-06)

| Parameter | Value |
|---|---|
| Instrument | NIFTY index options (NFO) |
| Reference levels | Previous trading day's High (PDH) and Low (PDL), from the daily candle |
| Setup window | 09:15–09:30 (observe only, no trading) |
| Signal | Where the 09:15–09:30 (15-min) candle **closes** relative to PDH/PDL |
| Decision time | 09:31, once, per day |
| Strike selection | **NOT ATM** (source doc's literal wording) — **delta ≈ 0.40** via `optiongreeks()`, per explicit correction from the user. Nearest listed strike whose \|Delta\| is closest to 0.40, scanned outward from spot (same scanning shape as the OI strategy's `select_monthly_delta_strike`, different target band) |
| Expiry | Nearest weekly NIFTY expiry — **rolled to the NEXT expiry if today IS the expiry date** (explicit correction from the user; same same-day-roll-avoidance guard as the OI strategy's `resolve_weekly_expiry`, NOT Batman's deliberate same-day-expiry trading) |
| Scenario A — closes INSIDE range (PDL < close < PDH) | SELL delta-0.40 Call + SELL delta-0.40 Put (short strangle), stop-loss 1.75× entry premium **per leg independently** |
| Scenario B — closes ABOVE PDH | SELL delta-0.40 Put only, stop-loss 2× entry premium |
| Scenario C — closes BELOW PDL | SELL delta-0.40 Call only, stop-loss 2× entry premium |
| Quantity | **1 lot per leg** (explicit, from the user) |
| Product | `NRML` (see §5 below — required to make the literal 15:15 exit time safe) |
| Square-off | **Exactly 15:15**, all open legs, unconditionally (explicit, from the user) |
| Re-entry | Not specified in the source doc — read as **one decision per day**, no re-entry after a leg is stopped out or squared off |

The doc is explicitly "educational content," a marketing-style playbook, not
a line-by-line functional spec like the OI strategy's source .txt — a few
mechanics below are still genuinely unspecified and carry a recommendation
rather than a transcription.

## Reference implementations

Per `AUTHORING_CHECKLIST.md`'s trust order:
1. **`MCX_CrudeOil_EMA9_RSI_Intraday_1_20260723150000.py`** — naked option
   selling, order placement/fill/error-recovery/PnL infra, most mature.
2. **`Nifty_Sensex_Expiry_Batman_1_20260714010000.py`** — the only existing
   script that enters **two option legs simultaneously as one decision**
   (its long straddle entry), and already implements "close SHORT/naked
   legs before LONG legs" sequential force-close ordering — relevant for
   this strategy's own Scenario A (two independent short legs opened
   together). Note Batman's straddle is *long*, ours is *short* — only the
   "two legs, one entry event" shape is being borrowed, not the risk
   direction.

Neither existing script has a premium-multiple stop-loss (this codebase's
existing SL/exit logic is all signal-based — crossover, consecutive-verdict,
profit-target — never "N× the entry premium"). This is new, no in-repo
precedent to copy verbatim.

## What's reused verbatim

- `Broker`, `PriceStream`, `StateStore`, `Environment` — same as every
  deployed script.
- `place()`, `poll_fill()`, `_reprice_and_wait_once()`,
  `OrderNeedsAttention` — order placement/fill machinery, unchanged.
- Error-recovery state machine (`_enter_error_mode`, `_resolve_leg_error`,
  `_do_retry_resolution`, `_repush_active_errors`, `_push_leg_error_bg`,
  `_refresh_pending_action_bg`) — unchanged shape, per leg.
- `strategy_reporting` HTTP helpers (`_post_json_local`/`_get_json_local`
  targeting `STRATEGY_REPORTING_PORT`), PnL reporting as its own
  APScheduler job (not folded into `run_cycle`).
- Trade log writer thread, `reconcile_pending_orders()` at startup.
- Delta-band strike scan: `select_monthly_delta_strike`'s shape (scan
  outward from ATM in listed-strike steps, `optiongreeks()` per candidate,
  uncached/live, only called when selecting a NEW strike) is directly
  reusable — just re-targeted from the OI strategy's 0.20–0.25 band to
  "closest to 0.40" here (see §"Strike selection" below for the exact
  target/tolerance).
- Weekly expiry resolution with same-day-roll: `resolve_weekly_expiry`'s
  exact logic (roll to the NEXT expiry if today IS the nearest expiry)
  is reusable near-verbatim from the OI strategy.

## What's genuinely new

### 1. PDH/PDL — previous day's daily candle
`client.history(interval="D", ...)`, previous trading day's high/low. Needs
its own resolve-previous-trading-day helper (skip weekends; NSE holiday
calendar not currently used anywhere in this repo per the CRUDEOIL plan's
same finding — calendar-day lookback with a "did we get a bar back"
sanity check is the accepted precedent, not exact-holiday-aware).

### 2. Opening-range candle read (09:15–09:30, 15-min)
A single one-shot read at/after 09:30 of NIFTY spot's own 15-minute candle,
compared against PDH/PDL. This is a **one-time daily gate**, not a
recurring per-candle signal like every other deployed script — closer in
shape to the OI strategy's once-daily Reference Engine (gap%, computed once
at 09:30, fixed for the day) than to a crossover-style repeating signal.

### 3. Strike selection — delta ≈ 0.40, not ATM
Per explicit correction: NOT `pick_atm_leg`. Reuses the OI strategy's
`select_monthly_delta_strike` scanning shape (`optiongreeks()`, live,
uncached, only called when selecting a NEW strike — never on every candle)
but re-targeted: scan outward from ATM in listed-strike steps for each of
CE/PE independently, pick the strike whose `|delta|` is closest to **0.40**
(a small tolerance band, e.g. 0.37–0.43, same "closest within range" shape
as the OI strategy, rather than an exact-match requirement that could find
nothing on a given day's strike ladder/IV).

### 4. Expiry — nearest weekly, same-day roll-avoidance
Per explicit correction: reuse `resolve_weekly_expiry`'s exact logic (OI
strategy) — nearest upcoming NIFTY weekly expiry, but if today IS that
expiry date, roll to the NEXT one instead. This is the OPPOSITE of Batman's
behavior (Batman deliberately trades the same-day-expiring contract) — do
not copy Batman's `resolve_current_week_expiry` for this piece, only its
simultaneous-two-leg-entry shape (§6 below).

### 5. Three-way mutually exclusive entry (once per day)
Straightforward branch on `close` vs `PDH`/`PDL`. The **inside-range case
enters 2 legs (CE+PE) as one decision**; the two directional cases enter
exactly 1 leg.

### 6. Strangle leg sequencing — simultaneous (finalized)
Both legs of Scenario A submitted together, not gated on each other's fill
— same shape as Batman's `_enter_straddle`. For a **short** strangle the
downside of a transient partial-fill state (one leg filled, one still
resting) is no worse than Scenario B/C's single-leg case — there's no
unbounded-risk window from an unmatched leg the way Batman's long-straddle
math has, so simultaneous submission carries over safely.

### 7. Premium-multiple stop-loss — software-monitored (finalized)
Values are exactly as documented: **1.75× entry premium** per leg for
Scenario A, **2× entry premium** for Scenario B/C's single leg. Mechanism:
software-monitored, matching this codebase's established pattern —
track each leg's live LTP via `PriceStream`, and when
`current_ltp >= entry_premium * multiple` (loss trigger for a short),
submit a market BUY-to-close via the existing `_exit_leg`/`place()` path,
same shape as every other deployed script's exit mechanism. No `SL-M`
broker order type — this codebase has no existing precedent for it, and
software-monitoring keeps the fill/error-recovery machinery identical to
every other script. Per checklist §1, this check must run every scheduler
tick, never gated behind a candle boundary — a stop-loss needs to react
promptly by nature.

### 8. Square-off — exactly 15:15, `product=NRML` (finalized)
Per explicit correction, the exit fires at exactly 15:15 as the doc states.
`AUTHORING_CHECKLIST.md` §2's real bug class (`Nifty_OI_WeeklyBuy_MonthlySell`
shipped `product=MIS` + `universal_exit_time=15:15`, the exact instant the
broker's own MIS square-off cutoff rejects new orders — guaranteed
rejection every time, confirmed in production) means `product=MIS` is not
safe at this exact time. **`product=NRML`** (matching Batman's precedent)
has no broker-enforced cutoff at all, so 15:15 works safely as specified,
at the cost of MIS's margin benefit on short options — an acceptable
trade-off to honor the exact exit time requested rather than shifting it
earlier.

### 9. Quantity — 1 lot per leg (finalized)
`quantity_lots: int = 1`, explicit from the user.

### 10. Re-entry — one decision per day (finalized)
Once the 09:31 trade is placed, no new entry today regardless of whether/
when a leg is stopped out — simplest reading consistent with the doc's
actual text, no re-entry trigger condition invented.

## Verification plan (per checklist §5, once built)

1. `uv run python -m py_compile strategies/deployed/<filename>.py`
2. Diff `run_cycle`, `_resolve_leg_error`, `_repush_active_errors` against
   MCX's and Batman's function-by-function.
3. Confirm the software-monitored SL path is never gated behind a candle
   boundary — checklist §1's "nothing that finalizes an exit may wait on a
   'not ready yet' condition" applies directly to a stop-loss, which by
   nature needs to react promptly, not once per candle.
4. Confirm `product=NRML` + `universal_exit_time=15:15` matches checklist
   §2's explicit pairing rule (NRML has no broker-enforced cutoff, so this
   combination is safe as specified).
5. Full pytest suite, confirm baseline unchanged from this repo's existing
   count.
