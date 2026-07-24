# Python Strategies — Order Error Recovery (Manual Intervention)

Plan for handling an order that fails all automatic retries: the affected leg
enters a persistent **error mode**, the UI surfaces a **Retry / Cancel /
Mark as Manually Completed** action, and once resolved the leg returns to
normal automated (BAU) operation.

Status: **implemented and mock-simulation-validated on all 5 strategy scripts** (MCX, EMA34_RSI, Pivot_Supertrend, VWAP_NoHA, Batman) plus backend endpoints and frontend (badge + `/errors` page). VWAP_NoHA and Batman additionally needed the full async fill-watcher infrastructure (`_state_lock`/`_pending_fills`/`_fill_executor`/`_save_state`) retrofitted, since they were out of scope for the earlier threading-only pass. **Not yet deployed to the server.**

## Problem

Today `poll_fill()` has exactly one terminal failure path — after
`reprice_max_attempts` re-prices (via `modifyorder()`, keeping the same
order id/queue position), if still unfilled it **cancels the order and
raises** (see each script's own "Order placement robustness" module
docstring). The two callers handle that raise by:

- **Entry failure**: `_watch_entry_fill`'s exception handler clears the whole
  `LegPosition` and logs `Log.error(...)`. The strategy silently retries a
  fresh entry next cycle if the signal condition is still true.
- **Exit failure**: `_watch_exit_fill`'s exception handler clears
  `exit_order_id`, increments `pos.exit_reject_count`, and logs (escalating
  to `ERROR` at 3+ consecutive failures) `"POSITION STILL OPEN AT BROKER"`.
  The strategy keeps placing a fresh exit order forever, every cycle.

Both are silent from the UI's perspective — no durable, actionable signal
that a human needs to look at this leg, and no in-app way to intervene.

## Quick reference

| Option | What it does | When to use | Risk |
|---|---|---|---|
| **Retry** | If the order is still resting unfilled at the broker, re-prices it to the current market price and waits again — same order id, not a new one. Only if it was already rejected/cancelled (nothing resting) does it place a genuinely new order instead. | A temporary blocker that's now resolved — stale price, brief API hiccup, margin/token issue fixed. | **High** if the original order actually executed at the broker without the strategy knowing about it — placing a second one risks a duplicate position. Mitigated here on purpose: the "still resting" case never creates a second order, only the "already dead" case does, and only when nothing is genuinely live to duplicate. |
| **Mark as Manually Completed** | Tells the strategy the order was actually filled — by you, on the broker's own terminal — and lets you enter the real fill price so the strategy's records (and the eventual trade-log row) match reality. | You already placed or closed it manually and just need the strategy to catch up. | Very low, provided the price you enter matches what the broker actually shows. |
| **Cancel** | Skips this failed attempt and resumes normal operation. **Exit**: position stays open, no broker action, a fresh exit attempt happens automatically later if the exit signal still holds. **Entry**: gives a still-resting order one last re-price, then actively cancels it at the broker if that still doesn't fill, and returns to flat either way. | You've decided this specific attempt isn't worth chasing further right now. | A resting order abandoned on the **exit** side (by design — see Decisions) can still fill on its own afterward, leaving the strategy briefly out of sync with the broker until its next check catches up — see Edge Cases. |

## The two kinds of failure (this matters for what Retry actually does)

`poll_fill()`'s single `except (RuntimeError, TimeoutError)` today conflates
two genuinely different broker states, and the requested "Retry re-prices
the existing order" behavior is only possible for one of them:

1. **`terminal`** — the broker explicitly rejected or cancelled the order
   (bad margin, risk limits, etc.), *or* `poll_fill` itself gave up and
   already cancelled it after exhausting reprice attempts. **Nothing is
   resting at the broker.** There is no order id left to modify — a retry
   here can only mean placing a genuinely new order.
2. **`resting`** — reprice attempts are exhausted but the order is still
   live/unfilled at the broker (not yet cancelled). **A retry here means
   re-pricing that same order id one more time**, per the correction: don't
   throw it away and start over, push it again.

This means `poll_fill()`'s exhaustion behavior must change: instead of
always cancelling after `reprice_max_attempts`, it should **leave the order
resting** and raise a distinct signal so the caller knows there's something
live to act on, rather than immediately cancelling on the strategy's behalf.
A genuine broker rejection (order never became "open" at all) still raises
the `terminal` case immediately, unchanged.

```python
class OrderNeedsAttention(Exception):
    """poll_fill() exhausted its automatic reprice attempts but the order is
    still resting, unfilled, at the broker -- NOT cancelled. Distinguishes
    "nothing left to act on" (order_id is dead, see the plain RuntimeError/
    TimeoutError-from-rejection path, unchanged) from "still live, needs a
    human decision" (this)."""
    def __init__(self, order_id, message):
        super().__init__(message)
        self.order_id = order_id
```

`poll_fill()`'s change: after the last reprice attempt's `_poll_until` times
out, instead of the current cancel-then-raise, it raises
`OrderNeedsAttention(orderid, ...)` **without** calling `cancelorder()`. The
existing genuine-rejection path (`status in {"rejected","cancelled"}` inside
`_poll_until`) is untouched — that's already a `terminal` case with nothing
to modify, and continues to raise `RuntimeError` immediately as it does
today.

Also factor `poll_fill()`'s repeated "re-price once, then wait one bounded
window" body out into a small reusable piece (it already loops this exact
shape `reprice_max_attempts` times):

```python
def _reprice_and_wait_once(client, order_id, strategy, symbol, exchange, action, quantity) -> Optional[dict]:
    """One reprice-to-current-LTP + one fill_poll_timeout-bounded wait.
    Returns the fill data if it completed, None if still unfilled (order
    left resting either way -- never cancels). Used by poll_fill()'s own
    reprice loop AND by Entry-Cancel's one-last-chance flow, so the "give it
    a fair price, then wait" behavior only exists in one place."""
```

`poll_fill()` becomes a thin loop calling this `reprice_max_attempts` times
before raising `OrderNeedsAttention`; Entry-Cancel's resting path (below)
calls it exactly **once**, independently of whatever `poll_fill()` itself
already tried, and takes a different action on failure (cancel, not raise).

## Decisions (confirmed 2026-07-23)

- **Error scope is per-leg, not per-strategy.** If `NIFTY_PE`'s exit fails,
  `NIFTY_CE` and `SENSEX_*` legs in the same process keep entering/exiting
  normally on their own signals. Only the specific `leg_key` stops being
  touched by automated entry/exit logic until the user acts on it.
- **Retry re-prices the existing resting order — it does not place a new
  one** (this session's correction). This only applies to the `resting`
  failure kind. For a `terminal` failure (order already dead — rejected, or
  already cancelled), Retry has no existing order to modify, so it falls
  back to placing a genuinely new order, same as the original design.
- **"Cancel" resumes plain BAU tracking either way, but the two sides differ
  on what happens to a still-`resting` order** (refined across three rounds
  of correction this session):
  - **Exit-Cancel**: ignore the failed attempt entirely — **no broker-side
    action, no further re-pricing.** Clears `error_state`/`error_kind`/
    `error_order_id`/`exit_order_id`, leaves the position itself open
    (`entry_filled` stays true — the entry genuinely happened). The leg
    returns to plain "open position, no exit order in flight" — the *next*
    cycle where `exit_condition` is true places a completely fresh exit
    order through the ordinary `_exit_leg` path, exactly like any healthy
    leg. Nothing about the abandoned order is resumed or re-priced. Rationale:
    a resting exit (BUY-to-close) order that fills unexpectedly later merely
    closes a position the strategy already knows is open — low blast radius.
  - **Entry-Cancel**: because an unfilled entry order filling unexpectedly
    later would create a position from nothing that the strategy has already
    forgotten about, a `resting` order is never silently abandoned here. One
    last honest attempt, then an explicit cancel:
    1. Re-price it once more to the current LTP (`modifyorder()`, same
       mechanic Retry uses) — give it one fair last chance rather than
       killing a perfectly fillable order at a stale price.
    2. Wait for **one bounded poll window** (no further internal
       re-pricing beyond that single attempt).
    3. If it fills during that window — treat it as a normal successful
       entry (`entry_filled=True`, `trade_count += 1`) exactly as Retry
       would have.
    4. If it's **still** unfilled after that one window — actively call
       `client.cancelorder()` on it, then reset `leg.position` to blank.
       Only after the broker confirms (or the cancel attempt itself is
       made, best-effort) does the leg return to flat.
    A `terminal` entry failure (nothing resting) skips straight to step 4's
    outcome — reset to blank, no broker call needed since there's nothing
    live to cancel.
  - This whole sequence (steps 1–4) can take up to one `fill_poll_timeout`
    window, so — like every other order-touching operation in this
    codebase since the async-fill-watcher work earlier this session — it
    must run on the background `_fill_executor`, never inline in
    `run_cycle`. See `_watch_entry_cancel` below.
- **"Mark as Manually Completed" always requires a fill price from the
  user** before the leg can resume BAU (applies identically regardless of
  failure kind).
- **"Mark as Manually Completed" always requires a fill price from the
  user** before the leg can resume BAU (applies identically regardless of
  failure kind).

## Scope

Applies to all 5 strategy scripts (the 3 with the new async fill-watcher —
MCX, EMA34_RSI, Pivot_Supertrend — plus VWAP_NoHA and Batman, whose
synchronous `poll_fill()` call sites get the identical state-model/error
fields even though they aren't on a background thread). Batman's straddle
entry (LONG legs) and repair (SHORT legs) both route through this the same
way; its two-in-flight-legs-per-instrument shape doesn't need separate
design, just two independent `leg_key`s going through the same machinery
already in place for PE/CE.

## Data model changes (per script)

Add to `LegPosition` (and `OptionLeg`/`RepairLeg` for Batman, which already
share the same shape per the execution-id work earlier this session):

```python
@dataclass
class LegPosition:
    ...
    error_state: str = ""        # "" | "entry_failed" | "exit_failed"
    error_kind: str = ""         # "" | "terminal" | "resting"
    error_order_id: str = ""     # the order id Retry/Cancel act on when error_kind == "resting"
    error_message: str = ""      # last exception text, for display
    error_since: str = ""        # ISO timestamp, for display / staleness
    manual_exit_px: Optional[float] = None   # set only by a "manual" resolution on an exit
```

No new top-level state fields needed — living on the position itself means
`error_state` survives a strategy restart (persisted in `state.json`, same
as `entry_order_id` etc. today), so a process crash/restart while a leg is
in error mode doesn't lose the alert.

## Backend: new in-process store + endpoints (`blueprints/python_strategy.py`)

Follows the exact pattern already established for `STRATEGY_PNL` this
session — an in-memory dict, a lock, a push endpoint the strategy calls, a
pull endpoint the browser calls, and an SSE broadcast for live updates.

```python
STRATEGY_ERRORS: dict[str, dict[str, dict]] = {}   # {strategy_id: {leg_key: {...}}}
STRATEGY_ERRORS_LOCK = threading.Lock()

STRATEGY_ACTIONS: dict[str, dict[str, dict]] = {}  # {strategy_id: {leg_key: {...}}}
STRATEGY_ACTIONS_LOCK = threading.Lock()
```

### Strategy → platform (push, API-key auth, same pattern as `POST .../pnl`)

- **`POST /api/strategy/<id>/errors`** — strategy calls this the moment a leg
  enters error mode (in `_watch_entry_fill`/`_watch_exit_fill`'s
  `OrderNeedsAttention`/terminal exception branches, right after setting
  `pos.error_state`/`error_kind`). Body: `{apikey, leg_key, error_state,
  error_kind, error_message, symbol, quantity, action}` (`action` = the
  BUY/SELL that failed, so the UI can show "tried to SELL 75 x
  NIFTY...PE and failed"). Stores into `STRATEGY_ERRORS[strategy_id][leg_key]`
  and broadcasts `{"type": "error_update", ...}` over the existing SSE
  channel (`broadcast_status_update`'s sibling, same as `broadcast_pnl_update`).
  Also called with `error_state: ""` to clear an entry once resolved, so the
  UI drops the alert without a page refresh.

### Platform → strategy (pull — the strategy is a separate process, Flask
cannot reach into it directly; the strategy already polls its own PnL push
cadence, so this reuses the same "ask on your own cycle" shape rather than
inventing a new transport)

- **`GET /api/strategy/<id>/pending_action?leg_key=<key>`** — strategy-side
  helper (`check_pending_action(env, leg_key)`) calls this **only when
  `pos.error_state` is set** (never on the hot per-cycle path for healthy
  legs, so this adds zero overhead to normal operation). Returns
  `{"action": null}` or `{"action": "retry"|"cancel"|"manual", "fill_price":
  <float|null>}`. Once consumed, the strategy also calls a matching
  **`POST /api/strategy/<id>/pending_action/ack`** `{leg_key}` so Flask clears
  `STRATEGY_ACTIONS[strategy_id][leg_key]` — otherwise a stale action could
  be re-applied on a later, unrelated error for the same leg.

### Browser-facing (session auth, matches `GET .../pnl`, `GET .../trades`)

- **`GET /api/strategy/<id>/errors`** — initial page-load fetch, mirrors
  `api_get_pnl`.
- **`POST /api/strategy/<id>/action`** — body `{leg_key, action: "retry"|
  "cancel"|"manual", fill_price?: float}`. Validates `fill_price` is required
  and a positive number when `action == "manual"`. Writes into
  `STRATEGY_ACTIONS[strategy_id][leg_key]` for the strategy to pick up next
  cycle. Returns success immediately (fire-and-forget from the UI's
  perspective — the error card shows "action sent, waiting for strategy to
  confirm..." until the next `error_update` SSE event clears it).

## Strategy-script changes (all 5)

### 1. `poll_fill()` — the resting-vs-terminal split

```python
def poll_fill(client, orderid, strategy, symbol, exchange, action, quantity):
    ...
    for reprice_attempt in range(1, config.reprice_max_attempts + 1):
        ...
        result = _poll_until(...)
        if result is not None:
            return result
    # Exhausted reprice attempts -- order is still resting, UNFILLED, at the
    # broker. Do NOT cancel it here anymore (that's now a user decision via
    # Cancel, or implicit in a Retry that re-prices it again) -- surface it
    # as a distinct "needs a human" signal instead.
    raise OrderNeedsAttention(
        orderid,
        f"Order {orderid} still unfilled after {config.reprice_max_attempts} "
        f"reprice attempt(s) -- resting at broker, needs manual action.",
    )
```

The existing rejection/cancellation branch inside `_poll_until` (broker
explicitly returned `rejected`/`cancelled`) is untouched — that already
raises a plain `RuntimeError` immediately, which is the `terminal` case.

### 2. Entering error mode

```python
except OrderNeedsAttention as exc:
    pos.error_state = "entry_failed"   # or "exit_failed" in _watch_exit_fill
    pos.error_kind = "resting"
    pos.error_order_id = exc.order_id
    pos.error_message = str(exc)
    pos.error_since = datetime.now(IST).isoformat()
    self._save_state()
    push_leg_error(self.env, leg_key, pos, action="SELL")
except (RuntimeError, TimeoutError) as exc:
    pos.error_state = "entry_failed"
    pos.error_kind = "terminal"
    pos.error_order_id = ""            # nothing resting -- nothing to modify/cancel
    pos.error_message = str(exc)
    pos.error_since = datetime.now(IST).isoformat()
    self._save_state()
    push_leg_error(self.env, leg_key, pos, action="SELL")
```

The position is **kept** either way (not cleared), so a Retry/Cancel/Manual
action still has something to act on. `_exit_leg`'s equivalent branch mirrors
this with `error_state = "exit_failed"`, in place of today's endless silent
retry — the 3-strikes `ERROR` log escalation is removed since the UI alert
now serves that purpose.

### 3. Freezing the leg while in error mode

`run_cycle`'s per-leg gating gains one line at the very top of each leg's
block:

```python
if leg.position.error_state:
    action = check_pending_action(self.env, leg_key)  # only called for legs
    if action is not None:                              # actually in error --
        self._resolve_leg_error(leg_key, inst, action)   # near-zero overhead
    continue                                             # for healthy legs
```

This is what makes the pause per-leg rather than per-strategy — every other
`leg_key`'s block in the same `for option_type in (...)` loop, and every
other instrument in `for inst in INSTRUMENTS`, is untouched and keeps
evaluating its own entry/exit conditions normally.

### 4. Resolving the action

**Found during the mock-simulation validation (not obvious from reading the
code alone), and important enough to flag explicitly**: `_enter_leg` is only
ever called by `run_cycle` when `pos.symbol` is *empty* (`has_position` is
false). But an entry already in error mode has `pos.symbol` set from the
moment the attempt began — so simply clearing `entry_order_id` and expecting
"`_enter_leg` places a brand-new entry order next cycle" **never actually
happens**: `run_cycle` permanently routes that leg into the exit-checking
branch instead, since it looks like an open position. `_exit_leg` has no such
problem — it's called unconditionally via the `exit_already_committed` gate
in `run_cycle` regardless of `exit_condition`, and its body already resumes
watching whatever `exit_order_id` is current. **Conclusion: Retry (and
Entry-Cancel, which already accounted for this) must resubmit the
entry-side watcher itself, directly from `_resolve_leg_error` — it cannot
rely on the normal `run_cycle` → `_enter_leg` path for the entry side.**

```python
def _resolve_leg_error(self, leg_key, inst, action):
    leg = self.store.state.legs[leg_key]
    pos = leg.position
    was_exit = pos.error_state == "exit_failed"
    kind = pos.error_kind

    if action["action"] == "retry":
        if was_exit:
            # _exit_leg IS reliably called again later regardless of
            # exit_condition (see the exit_already_committed gate in
            # run_cycle), and its body unconditionally resumes watching
            # whatever exit_order_id is current -- so the exit side only
            # needs the reprice (if resting) + clearing the error fields.
            if kind == "resting":
                fresh_ltp = fetch_symbol_ltp(self.client, pos.symbol, inst.options_exchange)
                if fresh_ltp is not None:
                    self.client.modifyorder(
                        order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                        symbol=pos.symbol, action="BUY",
                        exchange=inst.options_exchange, price_type="LIMIT",
                        product=config.product, quantity=str(pos.quantity),
                        price=str(fresh_ltp), disclosed_quantity="0", trigger_price="0",
                    )
                # exit_order_id already equals error_order_id -- _exit_leg's
                # own guard stays false and it resumes watching this order.
            else:  # kind == "terminal" -- nothing resting, must place fresh
                pos.exit_order_id = ""   # _exit_leg places a brand-new close order next cycle
            pos.error_state = pos.error_kind = pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, leg_key, pos, clear=True)
            ack_pending_action(self.env, leg_key)
            return

        # Entry side: must resubmit the watcher directly (see the note above
        # this function) rather than clearing entry_order_id and hoping
        # run_cycle calls _enter_leg again -- it structurally won't.
        if kind == "resting":
            fresh_ltp = fetch_symbol_ltp(self.client, pos.symbol, inst.options_exchange)
            if fresh_ltp is not None:
                self.client.modifyorder(
                    order_id=pos.error_order_id, strategy=self.env.strategy_tag,
                    symbol=pos.symbol, action="SELL",
                    exchange=inst.options_exchange, price_type="LIMIT",
                    product=config.product, quantity=str(pos.quantity),
                    price=str(fresh_ltp), disclosed_quantity="0", trigger_price="0",
                )
            resume_order_id = pos.error_order_id
        else:  # kind == "terminal" -- nothing resting, place a genuinely new order
            resume_order_id = place(self.client, self.env.strategy_tag, pos.symbol,
                                     inst.options_exchange, "SELL", pos.quantity)
            pos.entry_order_id = resume_order_id
        pos.error_state = pos.error_kind = pos.error_order_id = ""
        self._save_state()
        push_leg_error(self.env, leg_key, pos, clear=True)
        ack_pending_action(self.env, leg_key)
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_entry_fill, leg_key, inst, resume_order_id, pos.symbol, pos.quantity
        )
        return

    if action["action"] == "cancel":
        if was_exit:
            # Ignore the failed attempt entirely -- no broker-side action, no
            # further re-pricing, regardless of error_kind. Position stays
            # open; a fresh exit_condition cycle places a brand-new order
            # normally later. See Decisions above for the exit-vs-entry
            # rationale split.
            pos.exit_order_id = ""
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            push_leg_error(self.env, leg_key, pos, clear=True)
            ack_pending_action(self.env, leg_key)
            return
        if kind == "terminal":
            # Nothing resting -- no broker call needed, straight to flat.
            leg.position = LegPosition()
            self._save_state()
            push_leg_error(self.env, leg_key, leg.position, clear=True)
            ack_pending_action(self.env, leg_key)
            return
        # kind == "resting": one last honest re-price + bounded wait, THEN an
        # explicit cancelorder() if it still didn't fill -- never silently
        # abandoned (see Decisions: an untracked entry order filling later
        # would create a position from nothing). Can take up to one
        # fill_poll_timeout window, so this runs on _fill_executor, not
        # inline here -- ack_pending_action happens inside the task itself
        # once the outcome (filled vs. cancelled) is actually known.
        self._pending_fills.add(leg_key)
        self._fill_executor.submit(
            self._watch_entry_cancel, leg_key, inst, pos.error_order_id,
            pos.symbol, pos.quantity
        )
        return  # _watch_entry_cancel does its own _save_state()/push_leg_error()/ack

    if action["action"] == "manual":
        fill_price = action["fill_price"]
        if was_exit:
            pos.exit_filled = True
            pos.manual_exit_px = fill_price
            # _exit_leg's normal `if pos.exit_filled: self._finalize_exit(...)`
            # path runs next cycle and MUST prefer manual_exit_px over a fresh
            # quote (see _finalize_exit change below) -- the user's price is
            # authoritative, not "whatever the option is trading at now".
        else:
            pos.entry_filled = True
            pos.entry_px = fill_price
            leg.trade_count += 1
        pos.error_state = ""
        pos.error_kind = ""
        pos.error_order_id = ""

    self._save_state()
    push_leg_error(self.env, leg_key, pos, clear=True)  # clears the UI alert
    ack_pending_action(self.env, leg_key)                # POST .../ack
```

### 4a. `_watch_entry_cancel` — Entry-Cancel's "one last chance" background task

Runs on `_fill_executor`, same as `_watch_entry_fill`/`_watch_exit_fill` —
never inline in `run_cycle` or in `_resolve_leg_error` itself, since it can
take up to one `fill_poll_timeout` window.

```python
def _watch_entry_cancel(self, leg_key: str, inst: InstrumentConfig, order_id: str,
                         symbol: str, quantity: int):
    strategy_tag = self.env.strategy_tag
    try:
        result = _reprice_and_wait_once(self.client, order_id, strategy_tag,
                                         symbol, inst.options_exchange, "SELL", quantity)
        leg = self.store.state.legs[leg_key]
        pos = leg.position
        if pos.error_order_id != order_id:
            return  # superseded by a newer action/order in the meantime -- do nothing
        if result is not None:
            # Filled during its one last honest chance -- a completely normal
            # successful entry, not an abandonment.
            pos.entry_filled = True
            leg.trade_count += 1
            pos.error_state = ""
            pos.error_kind = ""
            pos.error_order_id = ""
            self._save_state()
            Log.info(f"[{leg_key}] Entry filled during Cancel's final chance: {symbol}")
        else:
            # Still unfilled after the one re-price + wait -- actively kill it,
            # never leave an untracked entry order resting (see Decisions).
            try:
                self.client.cancelorder(order_id=order_id, strategy=strategy_tag)
            except Exception as exc:
                Log.warning(f"[{leg_key}] cancelorder failed while abandoning entry "
                            f"({exc}) -- clearing local position anyway; verify "
                            f"manually at the broker that nothing is resting.")
            leg.position = LegPosition()
            self._save_state()
    finally:
        with self._state_lock:
            self._pending_fills.discard(leg_key)
        push_leg_error(self.env, leg_key, self.store.state.legs[leg_key].position, clear=True)
        ack_pending_action(self.env, leg_key)
```

`_finalize_exit`'s one line changes from:

```python
exit_px = fetch_symbol_ltp(self.client, pos.symbol, inst.options_exchange)
```

to:

```python
exit_px = pos.manual_exit_px if pos.manual_exit_px is not None else \
    fetch_symbol_ltp(self.client, pos.symbol, inst.options_exchange)
```

### 5. Resuming BAU

No special-case code needed beyond the above — once `error_state` is
cleared, the leg falls straight back through the **existing**
`_enter_leg`/`_exit_leg`/`_finalize_exit` code paths next cycle exactly as
if nothing unusual had happened (a re-priced resting order that then fills
looks identical to a normal fill from the watcher's perspective; a
manually-completed leg looks identical to a normal fill from
`_finalize_exit`'s perspective). That's the point of reusing the same
fields rather than inventing a parallel "manually closed" trade type.

### 5a. Re-entrancy: the leg can fall back into error mode again

Retry and Cancel don't guarantee success — a re-priced order can *still*
fail to fill, and a fresh order placed after Cancel/Retry (the `terminal`
case) can *also* be rejected. This must loop, not dead-end after one
attempt.

The one exception is **Entry-Cancel's `resting` path**
(`_watch_entry_cancel`) — it is deliberately self-terminating, not
re-entrant: it ends in exactly one of two definitive outcomes (a normal
fill, or an explicit cancel-and-clear), never in `OrderNeedsAttention`/a
fresh error state. That's the point of Cancel — it is the one action
guaranteed to actually end the alert for that attempt, one way or the
other, rather than potentially looping back into another round of
Retry/Cancel/Manual.

It does, by construction, with no extra code needed: after `retry`/`cancel`
clear `error_state` and hand control back to the normal `_enter_leg`/
`_exit_leg` → watcher flow, that watcher is the *exact same*
`_watch_entry_fill`/`_watch_exit_fill` function that raised the error the
first time. If the resumed/fresh order fails again, it hits the identical
`OrderNeedsAttention`/`RuntimeError` branches from step 2 above, which
re-populate `error_state`/`error_kind`/`error_order_id`/`error_message` and
re-push to `STRATEGY_ERRORS` exactly as before. There is no one-shot special
case to work around — entering error mode is just the watcher's normal
failure path, so the leg presents the same Retry/Cancel/Manually-Completed
choice every time it fails, however many rounds that takes, until one of
the three actions actually resolves it (a fill, an explicit cancel, or a
manual price).

The one thing to verify explicitly once built: `push_leg_error`'s
`error_since` timestamp must be **overwritten** on each re-entry into error
mode (not left at its original value), so the UI shows how long *this*
attempt has been stuck, not the very first one from several retries ago.

### 6. State/trade-log consistency guarantee (every action, no exceptions)

Every branch of `_resolve_leg_error` (and `_watch_entry_cancel` for the
async Entry-Cancel `resting` path) ends with `self._save_state()` — `state.json`
reflects the outcome of every action, not just "manual," even though Retry/
exit-Cancel resolve immediately while resting-Entry-Cancel resolves a moment
later once its background task finishes. But **state.json** and **the trade
log CSV** are two different things, and only a genuinely *closed* trade
belongs in the CSV. What each action actually persists:

| Action | `state.json` (via `_save_state()`) | `trades_{strategy_id}.csv` row |
|---|---|---|
| **Retry** | Error fields cleared; order id preserved (resting) or cleared (terminal) — leg is now "in flight" again, not closed | **Not yet** — written later, automatically, by the normal `_watch_entry_fill`/`_watch_exit_fill` → `_finalize_exit` path *if and when* the retried order actually fills. Retry itself is not a trade outcome, so nothing to log yet. |
| **Cancel (entry, `terminal`)** | Position reset to blank `LegPosition()` immediately — nothing was resting, nothing to wait for | **None** — no fill ever happened. |
| **Cancel (entry, `resting`)** | **Two possible outcomes, decided by `_watch_entry_cancel`'s background task**: (a) the one-last-chance re-price fills → `entry_filled=True`, `trade_count += 1` (identical to a normal successful entry); (b) still unfilled → `cancelorder()` called, then position reset to blank. Either way `_save_state()` runs once the task resolves, not at the moment the button is clicked. | **None either way** — outcome (a) is an *opened* position (logged later on its eventual exit), outcome (b) never filled at all. |
| **Cancel (exit)** | `exit_order_id` cleared, position stays open, no order in flight; an abandoned `resting` order (if any) is deliberately left untouched at the broker (see Decisions) | **Not yet** — the CSV row still comes from `_finalize_exit` whenever a *later, fresh* exit attempt (automatic or manual) actually closes the leg. |
| **Manually Completed (entry)** | `entry_filled=True`, `entry_px=fill_price`, `trade_count += 1` | **None yet** — an entry isn't a CSV row on its own (closed trades are); the row is written later, normally, when this leg eventually exits (manually or automatically) and `_finalize_exit` runs. |
| **Manually Completed (exit)** | `exit_filled=True`, `manual_exit_px=fill_price` | **Written immediately** on the very next cycle — `_exit_leg` sees `pos.exit_filled` and calls `_finalize_exit`, which now uses `manual_exit_px` instead of a live quote, computes `pnl_rupees` off the user's real price, and appends the row exactly like an automatic exit does. This is the one action where a trade-log row is a direct, immediate consequence. |

The invariant to test explicitly once built: **after a "Manually Completed"
exit, refresh the Trades page and confirm the new row's `pnl_rupees` matches
`(entry_px - fill_price) * quantity` (or the sign-flipped formula for a LONG
leg) using the price the user typed, not whatever the option happens to be
quoting at when the page is refreshed.**

## Frontend changes

1. **Types** (`frontend/src/types/python-strategy.ts`): `LegError` (`leg_key,
   error_state, error_kind, error_message, symbol, quantity, action,
   error_since`), `LegErrorsResponse`.
2. **API client**: `getErrors(strategyId)`, `postLegAction(strategyId,
   leg_key, action, fill_price?)`.
3. **Strategy card** (`PythonStrategyIndex.tsx`): a red "⚠ N leg(s) need
   attention" badge/button appears next to PNL/Trades when
   `errorsByStrategy[strategy.id]?.length`, populated on load + live via a
   new `error_update` SSE branch (same `eventSource.onmessage` handler
   pattern as `pnl_update`).
4. **New page** `PythonStrategyErrors.tsx` (`/python/:strategyId/errors`,
   modeled on `PythonStrategyTrades.tsx`'s layout): one card per leg in
   error state — symbol, attempted action/qty, error message, `error_kind`
   (shown as "still resting at broker" vs. "rejected/cancelled"), "in error
   since" timestamp, and three buttons:
   - **Retry** — label reads "Re-price and retry" when `error_kind ===
     "resting"`, "Place new order" when `"terminal"` — the button's own
     label tells the user what will actually happen, since it differs by
     kind. `postLegAction(id, leg_key, "retry")`.
   - **Cancel** — label and behavior differ by side and kind:
     - `error_state === "exit_failed"`: "Ignore & keep position open" — no
       broker call, position stays open, a normal fresh exit attempt
       happens later if the exit signal is still true.
     - `error_state === "entry_failed"` **and** `error_kind === "terminal"`:
       "Abandon entry" — nothing resting, straight back to flat.
     - `error_state === "entry_failed"` **and** `error_kind === "resting"`:
       "Give it one more chance, then cancel" — re-prices the resting order
       once more, waits, and only cancels it at the broker if that still
       doesn't fill. This one is not instant like the others — show a
       "waiting for final decision..." state until the `error_update` SSE
       event reports the outcome (filled or cancelled).
   - **Mark as Manually Completed** — reveals an inline price input +
     Confirm button; `postLegAction(id, leg_key, "manual", fill_price)`.
   Each button shows a brief "sent, waiting for strategy to confirm..."
   state until the corresponding `error_update` SSE event (or the next
   poll) shows that leg's `error_state` cleared.

## Edge cases / open items to verify during implementation

- **`modifyorder()` on a resting order that fills or dies in the instant
  before the call lands.** Same race `poll_fill`'s own reprice loop already
  has to tolerate today — wrap the manual reprice's `modifyorder()` call in
  the same try/except pattern already used there, and let the resumed
  watcher's next `orderstatus()` poll simply discover the true terminal
  state instead of trusting the modify call's own response.
- **Race: user clicks Retry right as the strategy independently recovers.**
  Can't happen today (nothing auto-recovers from error mode by design —
  that's the whole point), but worth a test once built: two rapid actions
  for the same leg should be last-write-wins in `STRATEGY_ACTIONS`, and the
  `ack` step ensures a consumed action never double-applies.
- **Strategy restarted while a leg is in error mode.** `error_state`/
  `error_kind`/`error_order_id` persist in `state.json`, so on restart the
  leg comes back up already frozen and the UI badge should still show it
  (backend's `STRATEGY_ERRORS` is in-memory only, so it needs to be
  re-populated — simplest: on `main()` startup, after `state_store.load()`,
  push any leg with a non-empty `error_state` to the platform once, same
  call as when it first failed). If `error_kind == "resting"`, the order may
  have filled or been cancelled by the broker WHILE the process was down —
  since Cancel/Retry are the only two ways out and both act deliberately
  (Retry checks the order's live status via `orderstatus()`/`modifyorder()`
  before deciding what to do; Cancel simply stops tracking it), there's no
  silent auto-resume path that could act on stale assumptions.
- **An abandoned `resting` order (via Cancel) can still fill later, unseen.**
  Since Cancel deliberately drops the order id from state without cancelling
  it at the broker (this session's explicit instruction), that order remains
  live. If it fills on its own afterward, the strategy has no record of it —
  a real position/execution exists at the broker that this leg's state no
  longer reflects. This is an accepted, explicit tradeoff per the
  "ignore the failure" instruction, not an oversight — but it means the
  operator is trusting that a `resting` order about to be Cancelled is one
  they intend to reconcile themselves outside the app (e.g. check the
  broker's own order book), the same way `error_kind == "resting"` already
  told them it was still live before they clicked Cancel.
- **Batman's two leg *types* per side** (straddle `OptionLeg` + repair
  `RepairLeg`) — `_resolve_leg_error`'s BUY/SELL direction for the manual
  reprice must match whichever leg type actually errored: the straddle's
  LONG legs enter via BUY/exit via SELL, the repair leg's SHORT legs are the
  reverse. Not a hardcoded assumption.
- **Do NOT let the strategy auto-retry an errored leg from `_reset_day_if_needed`.**
  A day rollover must not silently discard an unresolved error (e.g. reset
  `trade_count` but leave `error_state`/`error_order_id` and the broken
  position alone) — otherwise a genuinely-still-open, still-erroring
  position from yesterday could get orphaned at midnight rollover.

## Rollout

Implement and test on **one script first** (recommend MCX, since it already
has the freshest async fill-watcher and the smallest single-instrument
blast radius), verify end-to-end via a mock-broker simulation (same harness
pattern used for the fill-watcher work earlier this session, extended with
an `orderstatus`/`modifyorder` mode that deliberately stays "open" long
enough to exhaust reprice attempts and force the `resting` error path, plus
a variant that returns `rejected` immediately to force the `terminal` path),
then replicate identically to the other 4.
