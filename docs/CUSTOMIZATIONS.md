# Customizations vs. upstream (marketcalls/openalgo)

This document tracks every change made to files shared with upstream, so future
merges (`git merge upstream/main` into `custom-strategies`) are easier to reason
about. It does **not** cover the 5 strategy scripts under `strategies/scripts/`
— those are untracked by git entirely (not part of this repo's history) and are
maintained purely as local files; see the "Strategy scripts" section at the
bottom for what's in them anyway, since they're the bulk of the actual logic.

Repo setup: `origin` = `https://github.com/devendra2206/openalgo` (this fork),
`upstream` = `https://github.com/marketcalls/openalgo` (maintainer). All
customizations live on the `custom-strategies` branch; `main` stays a clean
mirror of upstream.

---

## `app.py` (+21 lines)

One addition: a CSRF exemption for the new Force Exit completion endpoint,
following the exact pattern already used for the other subprocess-facing
endpoints (`api_push_pnl`, `api_push_leg_error`, etc.):

```python
csrf.exempt(app.view_functions["python_strategy_bp.api_complete_force_exit"])
```

Low conflict risk — it's a single appended line next to an existing block of
similar exemptions.

---

## `blueprints/python_strategy.py` (+543 / -17 lines — largest change)

All additions are new module-level state and new route handlers on the
existing `python_strategy_bp` Blueprint; nothing pre-existing was rewritten
(hence the small `-17`, mostly whitespace/import adjustments).

**New in-memory state** (all in-memory only, same durability tier as the
pre-existing `STRATEGY_CONFIGS`/`RUNNING_STRATEGIES` — repopulated by the
strategy subprocess's next push after any Flask restart):
- `STRATEGY_PNL` / `STRATEGY_PNL_LOCK` — last-pushed PnL snapshot per strategy
- `STRATEGY_ERRORS` / `STRATEGY_ERRORS_LOCK` — legs currently in error mode
- `STRATEGY_ACTIONS` / `STRATEGY_ACTIONS_LOCK` — pending Retry/Cancel/Manual actions
- `STRATEGY_FORCE_EXIT` / `STRATEGY_FORCE_EXIT_LOCK` — pending Force Exit requests

**New SSE broadcast helpers:** `broadcast_pnl_update`, `broadcast_error_update`
(reuse the existing `_broadcast_sse`/`SSE_SUBSCRIBERS` plumbing, just a
different `"type"` field on the payload).

**New endpoints** (all under `/python/api/strategy/<id>/...`):

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /pnl` | subprocess (API key) | Push a live PnL snapshot |
| `GET /pnl` | browser (session) | Read last snapshot for page load |
| `POST /errors` | subprocess (API key) | Push/clear a leg's error state |
| `GET /errors` | browser (session) | List legs in error mode |
| `GET /pending_action` | subprocess (API key) | Poll for a Retry/Cancel/Manual decision |
| `POST /pending_action/ack` | subprocess (API key) | Confirm an action was consumed |
| `POST /action` | browser (session) | User submits Retry/Cancel/Manual from the UI |
| `GET /executions` | browser (session) | List execution runs for the Trades dropdown |
| `GET /trades` | browser (session) | Read `trades_{id}.csv` + open legs, optionally filtered by run |
| `POST /force_exit` | browser (session) | Request a full force-close + stop |
| `GET /force_exit` | subprocess (API key) | Poll whether force exit was requested |
| `POST /force_exit/complete` | subprocess (API key) | Report all-flat; triggers the actual process stop |

**New helpers:** `_empty_pnl_snapshot`, `_read_trade_log_rows`.

This is the file most likely to see upstream churn (it's an actively developed
blueprint) and the biggest diff — expect merge conflicts here more than
anywhere else. If a conflict shows up, it's almost always because upstream
added/changed something in the *existing* start/stop/schedule endpoints above
or below where these new ones were inserted — the new endpoints themselves are
additive and shouldn't semantically conflict with anything upstream does.

**Not yet done (documented, not shipped):** extracting all of the above into a
separate file (e.g. `blueprints/python_strategy_pnl_force_exit.py`) that
registers routes on the same `python_strategy_bp` via import side-effect,
shrinking this file's diff down to a single import line. Discussed and agreed
as the next step when there's time — see the corresponding conversation entry.

---

## `services/option_chain_service.py` (+27 / -12 lines)

Predates this session's Force Exit/PnL work (from an earlier customization
pass for MCX support). Two related fixes in `get_option_chain`:

1. **`expiry_date` now wins over `embedded_expiry`**, not the other way round.
   `expiry_date` is a required caller-supplied param; for MCX the `underlying`
   passed in is the *futures* symbol (its embedded expiry is the futures
   expiry, e.g. 19-AUG), which can differ from the *options* expiry the caller
   actually asked for (e.g. 17-AUG). Silently letting the embedded expiry win
   broke MCX option chains. Harmless for NFO/BFO where both values are
   normally identical anyway.

2. **MCX quote-symbol fix:** for MCX with an embedded expiry, keep the
   caller's exact futures symbol as the quote anchor instead of reducing to
   `base_symbol` (the NFO/BFO behavior). MCX commodities have no bare
   quotable spot/index the way NIFTY/SENSEX do — quoting the bare
   `"CRUDEOIL"` name fails with "Symbol not found."

Low conflict risk unless upstream touches the same ~15 lines of
`get_option_chain`'s expiry/quote-symbol resolution logic.

---

## Frontend

### `frontend/src/App.tsx` (+6 lines)
Three new lazy-loaded routes: `/python/:strategyId/pnl`,
`/python/:strategyId/trades`, `/python/:strategyId/errors`. Tiny, additive,
low conflict risk.

### `frontend/src/api/python-strategy.ts` (+77 lines)
New client methods: `getPnl`, `getTrades`, `getExecutions`, `getErrors`,
`postLegAction`, `forceExitStrategy`. Purely additive — no existing methods
were changed.

### `frontend/src/types/python-strategy.ts` (+77 lines)
New types backing the above: `PnlSnapshot`, `Trade`, `TradesResponse`,
`Execution`, `ExecutionsResponse`, `LegError`, `LegErrorsResponse`,
`LegAction`. Purely additive.

### `frontend/src/pages/python-strategy/PythonStrategyIndex.tsx` (+250 / -17 lines — 2nd largest change)
The most likely frontend file to conflict, since it's a page upstream also
iterates on. Changes:
- SSE handler extended to branch on `pnl_update`/`error_update` message types
  (in addition to the pre-existing status-update handling).
- New state: `pnlByStrategy`, `errorCountByStrategy`, `forceExitDialogOpen`,
  `strategyToForceExit`.
- New `stats.totalPnl` (summed live PnL across running strategies) and a 5th
  "Total PNL" tile in the stats bar (grid widened from 4 to 5 columns).
- Per-strategy card: PNL button (live amount, links to the PnL page), Trades
  button, error badge (links to the Errors page when a leg needs attention),
  and a destructive "Force Exit" button (confirm dialog → `forceExitStrategy`).
- Action-button row changed from Stop being the only `flex-1` button to Stop/
  Force Exit/Schedule/PNL/error-badge all sharing the row evenly.
- Strategy-cards grid changed from `md:grid-cols-2 lg:grid-cols-3` to a single
  column (one strategy per row) — a pure layout preference, not functional.

### New files (zero conflict risk — upstream doesn't know about these)
- `frontend/src/pages/python-strategy/PythonStrategyPnl.tsx`
- `frontend/src/pages/python-strategy/PythonStrategyTrades.tsx`
- `frontend/src/pages/python-strategy/PythonStrategyErrors.tsx`
- `docs/prd/python-strategies-order-error-recovery.md`

---

## Strategy scripts (NOT tracked by git — local files only)

`strategies/scripts/{MCX_CrudeOil_EMA9_RSI_Intraday, Nifty_Sensex_EMA34_RSI_Intraday,
Nifty_Sensex_Pivot_Supertrend_Intraday, Nifty_Sensex_VWAP_NoHA_Intraday,
Nifty_Sensex_Expiry_Batman}_*.py` — these never showed up in `git status` at
all, so a `git pull` never touches them and they never conflict. They hold
most of the actual behavior described above (PnL push cadence, Force Exit
handling, WS reconnect logic) but they're outside git's purview entirely.
Listed here purely so this doc is a complete picture of "what's different
from a stock OpenAlgo install," not because git needs to track them.

Summary of what's in them (all 5, unless noted):
- `Environment.timeout` reduced 120.0 → 10.0; a second `ltp_client` with a
  separate 3.0s timeout for the WS-stale LTP fallback specifically.
- Indicator-signal and chain-refresh REST calls genuinely backgrounded via
  `_fill_executor.submit()` (previously described as "background" in
  comments but were actually synchronous/inline).
- `report_pnl_tick()` — PnL pushed on its own 1-second APScheduler job,
  reading only the WebSocket price cache (never a REST call), replacing the
  old 15s-throttled `_maybe_report_pnl`.
- Order-error-recovery: `OrderNeedsAttention` exception, `error_state`/
  `error_kind`/`error_order_id` fields on each leg, Retry/Cancel/Manual
  handling, universal-exit-time bug fix (errored legs no longer get
  permanently stranded after hours).
- Force Exit: `check_force_exit`/`ack_force_exit_complete` polling, per-script
  `_handle_force_exit()` that force-closes every open leg (Batman closes
  SHORT/repair legs before LONG/straddle legs, per explicit requirement).
- Fixed a pre-existing bug: `check_pending_action()` was missing `apikey` in
  its request, meaning the entire Retry/Cancel/Manual mechanism silently
  401'd and never actually applied a user's action.
- `Log.exception()` added; top-level `run_cycle()` catch-all switched from
  manual `import traceback`/`traceback.format_exc()` to it.
- Fill-watcher (`_watch_entry_fill`/`_watch_exit_fill`) generic
  `except Exception` clause added — previously an unexpected exception type
  was silently swallowed by the executor with zero log line.
- WebSocket reconnect escalation: a per-symbol stale-cycle counter
  (`_stale_streak`) that escalates to a full connection reconnect after
  `ws_stale_reconnect_after` (3) consecutive stale cycles for the same
  symbol, instead of retrying the same narrow per-symbol resubscribe forever
  (confirmed in production logs: the old approach could retry 30+ times with
  zero recovery).
- EMA34_RSI/Pivot_Supertrend specifically: `PriceStream` converted from a
  fixed NIFTY/SENSEX-only list to the same dynamic (`add_instruments()`)
  pattern MCX/VWAP_NoHA/Batman already used, so an open option leg's LTP is
  actually WS-cached — previously `report_pnl_tick` could never see an open
  position for these two scripts specifically, since the option symbol was
  never subscribed at all.
