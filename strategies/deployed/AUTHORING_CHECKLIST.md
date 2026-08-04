# Writing a new deployed strategy — checklist

Read this **before** writing a new script in `strategies/deployed/`, and run
through it again before considering the script done. It exists so the
run_cycle/error-handling/order-management review this project keeps needing
doesn't have to happen from scratch, one bug at a time, for every new script.

**Reference implementations, in order of trust:**
1. `MCX_CrudeOil_EMA9_RSI_Intraday_1_20260723150000.py` — the most mature,
   most-copied-from script. When in doubt, read what it does and match it.
2. `Nifty_Sensex_Expiry_Batman_1_20260714010000.py` — the reference for
   multi-leg (long + short) risk-priority ordering.

Every item below was a **real, confirmed bug** found in a deployed script at
some point — most in a single review pass across 7 strategies on 2026-08-04.
This is not a hypothetical checklist.

---

## 1. `run_cycle()` structure

- [ ] **Nothing that resolves a pending Retry/Cancel/Manual action, or
  finalizes an already-confirmed exit fill, may be gated behind a candle
  boundary, a "not ready yet" flag, or any other condition that can stay
  false/true for an extended period.** Those checks are cheap (in-memory
  flags, or a background-cached result) and must run every scheduler tick.
  Only the heavy, genuinely candle-dependent signal logic
  (entry/exit-condition evaluation) should be gated.
  *(Found in Nifty_OI_WeeklyBuy_MonthlySell: the whole per-leg `evaluate()`
  sweep — including exit-finalize and error-resolution — was gated behind
  `_new_candle_closed()`, so a confirmed exit fill or a Manually-Completed
  click could sit unprocessed for up to 5 minutes.)*

- [ ] **The Force-Exit-pending branch must resolve pending Retry/Cancel/
  Manual actions on any errored leg BEFORE returning.** `run_cycle()`
  `return`s right after that branch every cycle while a Force Exit is
  pending, so it is the *only* code path that runs — if it doesn't check
  pending actions, a leg stuck in `error_state` while Force Exit is pending
  can never be resolved (its own error-check lives in the normal per-leg
  loop below, which is unreachable). This deadlocks both the leg and Force
  Exit itself (which can never reach "all flat") until a manual restart.
  ```python
  if self._force_exit_pending:
      for leg_key in LEG_KEYS:
          leg = self.store.state.legs[leg_key]
          if leg.position.error_state:
              self._refresh_pending_action_bg(leg_key)
              pending = self._pop_pending_action(leg_key)
              if pending is not None:
                  self._resolve_leg_error(leg_key, inst, pending)
      if self._handle_force_exit():
          ...
      return
  ```

- [ ] **The past-universal-exit-time branch must ALSO check pending actions
  for errored legs**, not just skip/log them. Same reasoning as above — if
  this branch `continue`s or `return`s without checking, and it's the only
  branch reachable for the rest of the day, an errored leg is stranded
  until tomorrow.

- [ ] **The normal per-leg loop checks pending actions for errored legs
  too** — this is usually already right, but confirm it.

- [ ] `check_force_exit` is dispatched to a background executor **every
  cycle, not throttled**. A human just clicked the button and expects it
  picked up fast; the only guard needed is "don't resubmit if already
  in flight."

## 2. Order & fill management

- [ ] `place()` retries only a **clean rejection** response (nothing was
  placed, safe to retry) up to `config.place_order_max_attempts` times.
  It must **never** retry a raised exception — the outcome is ambiguous
  and retrying risks a duplicate order at the broker.

- [ ] Fill confirmation (`poll_fill`, which can block for
  `fill_poll_timeout * (1 + reprice_max_attempts)` seconds — commonly
  tuned to a ~5 minute ceiling) **always** runs on a background executor
  (`_fill_executor`, sized to the leg count), never inline in
  `run_cycle()`. Placing the order itself is fast and stays synchronous;
  only *waiting for the fill* is backgrounded.

- [ ] **Every leg has an async fill watcher pair**
  (`_watch_entry_fill`/`_watch_exit_fill`) that owns `_pending_fills` for
  that leg from submission until it resolves (success, or
  `_enter_error_mode`). `evaluate()`/`run_cycle()` must check
  `if leg_key in self._pending_fills: return` before doing anything else
  for that leg.

- [ ] **"Manually Completed" finalizes immediately** — call
  `_finalize_exit()` (or the entry equivalent) directly from
  `_resolve_leg_error`'s manual branch, not just set
  `exit_filled=True`/`manual_exit_px` and wait for the next `evaluate()`
  pass to notice. The user is telling you the trade is already done; the
  CSV/state must reflect that in the same click, not on some later cycle.

- [ ] **`product` (MIS vs NRML) and `universal_exit_time` must be chosen
  together, deliberately:**
  - If `MIS`: `universal_exit_time` must have a **real buffer** before the
    broker's own MIS square-off cutoff — never set it to the *exact same
    instant*. `Nifty_OI_WeeklyBuy_MonthlySell` had
    `universal_exit_time = time(15, 15)` while the broker rejects "MIS
    orders cannot be placed after square-off time (15:15 IST)" — the
    strategy's own exit order was **guaranteed to be rejected every single
    time it fired**, confirmed in production 2026-08-04. Since MIS has the
    broker's own forced square-off as a backstop, this is recoverable but
    ugly (the leg ends up in `error_state`, needing manual resolution) —
    don't rely on it.
  - If `NRML`: there is **no broker-side forced square-off backstop at
    all**. `universal_exit_time` becomes the *only* thing that closes the
    position — give it real margin before `market_close` (MCX's
    `23:15` vs `23:55` close — a 40 minute buffer — is the model to copy),
    and treat any failure to close as a genuine overnight-carry risk, not
    a recoverable one.

- [ ] **When multiple legs must be force-closed sequentially (Force Exit,
  universal-exit-time, aggregate-PnL breach), close SHORT/naked
  (undefined/unbounded risk) legs BEFORE LONG/premium-capped (bounded
  risk) legs.** This is the convention established for
  `Nifty_Sensex_Expiry_Batman` (straddle + repair legs) and
  `Nifty_OI_WeeklyBuy_MonthlySell` (Weekly Buy / Monthly Sell) — whichever
  leg carries the worse tail risk gets closed first when you can't close
  everything atomically.

- [ ] **`reconcile_pending_orders()` runs once, at startup, before the
  scheduler starts.** It resolves the narrow "process died between
  recording an order_id and confirming the fill" window by querying the
  broker directly (`orderstatus()`), never guessing. If `pos.symbol` is
  set with no `entry_order_id` at all (crashed between recording the
  attempt and `place()` returning), flag it into `error_state` for manual
  review — never assume it did or didn't reach the broker.

- [ ] **Never assume a symbol is exclusive to this strategy.** Nothing in
  this codebase cross-checks a strategy's local position state against the
  broker's actual net position, or against what any other strategy (or
  manual trading) is doing on the same symbol. `poll_fill()` only reports
  whether *this strategy's own order_id* filled — it has no idea if that
  fill happened to net against another strategy's position, silently
  leaving that strategy's own state believing it holds something the
  broker no longer has. If a new strategy could ever share a symbol with
  an existing one, that's a real, structural risk — avoid the overlap
  rather than trying to code around it.

## 3. Error handling & platform reporting

**This is the single most repeated bug class found across strategies.**
`push_leg_error`, `check_pending_action`, and `check_force_exit` are
synchronous local HTTP calls. Calling any of them **directly on the main
`run_cycle` thread** blocks the scheduler for however long that call takes —
confirmed in production (2026-08-04): visiting `/health` froze the single
gunicorn+eventlet worker long enough that these calls hit their own timeout,
stalling `run_cycle()` and delaying every OTHER leg's evaluation that same
cycle. `EMA34_RSI` and `Pivot_Supertrend` had this bug live in
`_resolve_leg_error` (every Retry/Cancel/Manual resolution) AND in all three
`run_cycle` per-leg loops — meaning it fired on *every single cycle* any leg
sat in `error_state`, not just rarely.

- [ ] **`_post_json_local`/`_get_json_local` target `STRATEGY_REPORTING_PORT`
  (default 8766), not `FLASK_PORT`/`openalgo.sock`.** As of 2026-08-05, PnL
  push, leg-error push, pending-action poll/ack, and Force Exit
  poll/complete are served by a dedicated `strategy_reporting` subprocess
  (see `docs/CUSTOMIZATIONS.md`'s `strategy_reporting/` section and
  `strategy_reporting/server.py`'s module docstring), not the main
  gunicorn app — this is what made the executor-backgrounding items below
  a defense-in-depth measure rather than the only line of defense: even a
  fully synchronous call to the wrong target here would no longer share a
  worker with unrelated main-app traffic like `/traffic/api/stats`. Copy
  the fallback chain from any already-migrated script (e.g. MCX's
  `_post_json_local`), don't reintroduce a `openalgo.sock` check for these
  6 calls — that socket belongs to the main app, not this subprocess (which
  is TCP-loopback only, see `server.py`'s `main()` for why no Unix socket).

- [ ] A dedicated single-worker executor exists for this purpose (any name
  is fine — `_bg_executor`, `_pnl_executor`, `_signal_executor` are all
  used across the existing scripts; reuse whichever one already exists in
  your script rather than adding a redundant pool). It must be separate
  from `_fill_executor`, since a fill watcher can legitimately block that
  pool for minutes in a reprice loop.

- [ ] `_push_leg_error_bg(leg_key, pos, action="", clear=False)` exists and
  dispatches `push_leg_error(...)` via that executor, snapshotting `pos`
  with `copy.copy()` on the calling thread first (it's a live, mutable
  object the same cycle may reset moments later).

- [ ] `_refresh_pending_action_bg(leg_key)` / `_pop_pending_action(leg_key)`
  exist: the former dispatches `check_pending_action(...)` to the
  executor and caches the result (guarded per leg_key so a slow check
  isn't resubmitted on top of itself); the latter pops and returns the
  cached result once.

- [ ] **`_resolve_leg_error`'s branches use `self._push_leg_error_bg(...)`,
  never the raw `push_leg_error(...)` function** — this method runs
  synchronously on `run_cycle`'s own thread every time it's called.

- [ ] **Every `check_pending_action(...)` call site inside `run_cycle`**
  (force-exit branch, past-universal-exit branch, normal per-leg loop) uses
  `self._refresh_pending_action_bg(leg_key)` / `self._pop_pending_action(leg_key)`,
  never the raw synchronous function.

- [ ] `_enter_error_mode`'s own `push_leg_error(...)` call **may stay raw**
  — it's called from `_watch_entry_fill`/`_watch_exit_fill`/
  `_do_retry_resolution` (already background-executor threads) far more
  often than from `run_cycle`'s own thread, and this is MCX's own accepted
  design for that specific, comparatively rare transition-into-error-state
  event. Don't "fix" this one without a reason — it's not the bug class
  above.

- [ ] Likewise `_do_retry_resolution` and `_watch_entry_cancel`'s raw
  `push_leg_error(...)` calls are fine as-is — both already run on
  `_fill_executor`'s background thread (submitted via
  `self._fill_executor.submit(...)`), so there is nothing to background
  further.

- [ ] **`_repush_active_errors()` exists and is called unconditionally at
  the top of `run_cycle()`, every cycle.** `push_leg_error()` only fires
  once, on the transition into `error_state` — if that one POST is lost
  (server busy, transient network blip), the UI's error badge silently
  never appears even though `state.json` correctly tracks the error the
  whole time (confirmed in production, 2026-07-28: three legs sat in
  `exit_failed` for 1-4 hours with no UI error shown). Re-push at most
  once per `config.error_repush_interval_sec` for every leg still in
  `error_state`. Check `config.error_repush_interval_sec` isn't just
  copy-pasted into the config dataclass with nothing actually calling it
  — that happened once already.

## 4. PnL reporting

- [ ] `report_pnl_tick` is registered as its **own separate APScheduler
  job** (a distinct `id=` from `strategy_cycle`, its own
  `IntervalTrigger`), not folded into `run_cycle()`. This alone gives it
  isolation from the trading-logic job — `max_instances=1` on each job id
  means a slow `report_pnl_to_platform()` call queues behind *itself*, not
  behind leg evaluation. A dedicated executor for this specific push is
  redundant on top of that (a `_pnl_executor` created for this purpose and
  never actually used was found and removed from one script) — don't add
  one unless you have a concrete reason the separate-job isolation isn't
  enough.

- [ ] `report_pnl_to_platform()` is fire-and-forget: short timeout, any
  failure logged and swallowed, must never raise into the scheduler.

## 5. Before calling it done

- [ ] `uv run python -m py_compile strategies/deployed/<your_script>.py`
- [ ] If a test file exists for a sibling script with similar structure,
  consider whether this script needs one too (not every deployed script
  has one — `py_compile` alone is the accepted minimum for the simpler
  ones).
- [ ] Diff your new script's `run_cycle`, `_resolve_leg_error`, and
  `_repush_active_errors`/error-push plumbing against MCX's (or Batman's,
  if it's a multi-leg long+short strategy) function-by-function — not just
  "does a function with this name exist," but read the body. Every bug in
  this checklist was found exactly that way, not by running the strategy
  and waiting for it to fail.
