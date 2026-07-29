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

## How to sync with upstream

`main` must stay a byte-for-byte mirror of `upstream/main` at all times — it's
the fast-forward staging point, never merge into it directly, and never commit
customizations to it.

```bash
# 1. Fetch both remotes
git fetch upstream
git fetch origin

# 2. Fast-forward main to upstream/main (should ALWAYS be --ff-only;
#    if this fails, main has diverged and needs investigation first,
#    not a force-push)
git checkout main
git merge upstream/main --ff-only
git push origin main

# 3. Merge main into custom-strategies
git checkout custom-strategies
git merge main --no-ff -m "chore: merge upstream/main (via main) into custom-strategies"
```

**Expected conflicts — `frontend/dist/` only, every time.** Both branches
independently rebuild the frontend with content-hashed filenames (CI on
upstream's side, the local `commit-dist` step on this fork's side), so nearly
every asset shows up as a `rename/rename` or `modify/delete` conflict even
though nothing meaningfully differs. Resolve by taking `main`'s copy of the
whole directory wholesale — it's the latest canonical CI build, and this
fork's own `frontend/dist` on `custom-strategies` was itself just an older CI
build, not a hand-edited artifact:

```bash
rm -rf frontend/dist
git checkout main -- frontend/dist
git add -f frontend/dist   # -f required: frontend/dist is gitignored
```

**If anything OTHER than `frontend/dist/` conflicts**, stop and check it
against the "Customizations vs upstream" sections above before resolving —
that means upstream touched one of the files this fork has actually modified
(most likely `blueprints/python_strategy.py`), and the conflict needs to be
read, not just picked one side. Confirm no unexpected conflicts before
committing:

```bash
git diff --name-only --diff-filter=U   # should be empty after the dist fix
```

**Before committing the merge**, sanity-check that this fork's actual
customized files came through untouched (compare against the pre-merge tip,
not against upstream):

```bash
python -m py_compile app.py blueprints/python_strategy.py services/option_chain_service.py
git diff <pre-merge-commit> HEAD -- app.py blueprints/python_strategy.py services/option_chain_service.py
# should be empty unless upstream genuinely touched these files this time --
# see the per-file sections above for what to expect if it did
```

A file showing 0 lines of diff between `custom-strategies` and the
merge-base is **not actually customized** here (even if it looks
project-specific) — it was simply inherited from an earlier point in
upstream's own history, and a later upstream rewrite of it will merge in
cleanly with no conflict, silently replacing it entirely. This happened to
`CLAUDE.md` in the 2026-07-26 sync below: its detailed content (ZeroMQ bus
invariant, multi-session login guard, etc.) was never actually a fork
customization — upstream wrote it, this fork never touched it, and upstream's
later restructure (moving broker-specific detail into
`.claude/skills/broker-integration/`) replaced it wholesale, correctly. Before
assuming a big diff on a "no conflict" file is fine, grep for the specific
invariant/text you rely on in the new version — don't just trust that
"no conflict" means "nothing changed."

Finally:

```bash
git push origin custom-strategies
```

**Sync log:**
- **2026-07-26**: 22 commits pulled from upstream (skills additions, broker
  fixes, dependency bumps, `CLAUDE.md` restructure). Zero conflicts outside
  `frontend/dist/`. Verified `app.py`/`blueprints/python_strategy.py`/
  `services/option_chain_service.py` unchanged (0-line diff) and compiling
  clean post-merge; verified the ZeroMQ-bus and multi-session-login
  invariants survived `CLAUDE.md`'s restructure (moved, not dropped).

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

## `services/websocket_client.py` (+12 / -1 lines)

One-line fix (plus comment) to a real production crash: `self.lock` was a
plain `threading.Lock()` — under gunicorn's eventlet worker that's a
greenlet-cooperative lock, valid only within the hub's single OS thread. But
this file already runs its asyncio event loop on a genuine separate OS thread
via `_original_threading.Thread` (the existing `eventlet.patcher.original`
escape hatch, used because `asyncio.new_event_loop()` can't run inside a
green thread) — and `_handle_message()`, which only ever executes inside
that real thread, acquired the same `self.lock` that `subscribe()`/
`unsubscribe()` acquire from ordinary eventlet-green Flask-request code.
Acquiring/releasing a greenlet-cooperative lock across that real-vs-green OS
thread boundary crashes with `greenlet.error: Cannot switch to a different
thread` — observed taking down the Sandbox engine's position-feed
subscription path (`sandbox/websocket_execution_engine.py`) in production.
Fix: `self.lock = _original_threading.Lock()` — a genuine OS-native lock has
no greenlet affinity and works from either side.

Low conflict risk — a single-line change plus a comment block, isolated to
`__init__`.

---

## `websocket_proxy/server.py` (+15 / -3 lines)

Fixes an asymmetry in `subscribe_client()`: unlike `unsubscribe_client()`
(which already refcounts via `self.subscription_index` and only calls
`adapter.unsubscribe()` for the *last* client on a symbol), `subscribe_client()`
called `adapter.subscribe()` unconditionally for *every* client's request,
even when another client already held that exact `(symbol, exchange, mode)`
live. Several broker adapters (e.g. `broker/zerodha/streaming/zerodha_adapter.py`'s
`subscribe()`) unconditionally re-enqueue a fresh subscribe on every call
regardless of whether the token is already streaming — so a redundant
re-subscribe from one client (e.g. a strategy process's routine reconnect)
could reset/interrupt that symbol's tick delivery for every *other* client
already subscribed to it. This matters here specifically because multiple
independently-running strategy processes commonly share the same underlying
symbols (all 5 NIFTY/SENSEX scripts subscribe to `NIFTY.NSE_INDEX`, for
example) via one pooled broker adapter per user. Fix mirrors the existing
refcount check in reverse: skip the adapter call when
`self.subscription_index` already has a subscriber for that key.

Low conflict risk — an isolated ~15-line change inside one method's loop
body; only conflicts if upstream also touches `subscribe_client`'s
subscribe-vs-index-update ordering.

---

## `sandbox/websocket_execution_engine.py` (+24 / -1 lines)

Second instance of the same eventlet cross-thread lock crash class as
`services/websocket_client.py` above — a different lock object, same root
cause. `self._lock` was a plain `threading.Lock()` (greenlet-cooperative
under eventlet). It's acquired from two different thread contexts:

1. **The real OS thread**: `_on_market_data()` is registered as a
   `market_data` callback (`services/websocket_service.py`'s
   `register_market_data_callback()` → `client.register_callback(...)`) and
   gets invoked synchronously inside `services/websocket_client.py`'s
   `_handle_message()`, which only ever runs on that client's dedicated real
   OS thread (hosting its own `asyncio` event loop).
2. **Ordinary eventlet-green Flask request code**: `notify_order_placed()`,
   `notify_position_opened()`, `notify_position_closed()`,
   `_rebuild_order_index()` — all touch the same `self._lock`, called from
   normal API routes (placing/closing a sandbox order).

A greenlet-cooperative lock can't be waited on/released across that
real-vs-green OS thread boundary — eventlet's hub can't resume a suspended
greenlet belonging to a different native thread's stack, producing
`greenlet.error: Cannot switch to a different thread`. This one fires far
more often than the first instance, since it's on the hot path for every
incoming tick with a matching pending order or open position — i.e.
constantly during active trading, which matches the ~26/day, trading-hours-
clustered occurrences observed in production (2026-07-28) even AFTER the
first lock fix was deployed and the server restarted. Fixed the same way:
`self._lock = _original_threading.Lock()` (the file's own
`eventlet.patcher.original("threading")` escape hatch, added alongside).

Low conflict risk — an isolated change to `__init__` plus one added
module-level `if "eventlet" in sys.modules:` block, mirroring the existing
pattern already used in `services/websocket_client.py` and
`services/telegram_bot_service.py`.

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

## Strategy scripts (NOT tracked by git under `strategies/scripts/` — but a
## COPY of each is committed under `strategies/deployed/`)

`strategies/scripts/{MCX_CrudeOil_EMA9_RSI_Intraday, Nifty_Sensex_EMA34_RSI_Intraday,
Nifty_Sensex_Pivot_Supertrend_Intraday, Nifty_Sensex_VWAP_NoHA_Intraday,
Nifty_Sensex_Expiry_Batman, Nifty_Sensex_Pivot_EMA_Combined_Intraday}_*.py` —
the `strategies/scripts/` copies never show up in `git status`, so a
`git pull`/merge never touches them and they never conflict. `strategies/deployed/`
holds a byte-identical committed copy of each (kept in sync manually after any
edit) purely so this project's own history/reviews/PRs have something to diff
against — the live-served copy the Python Strategy Host actually runs is
always the `strategies/scripts/` one. Listed here purely so this doc is a
complete picture of "what's different from a stock OpenAlgo install," not
because git needs to track the `scripts/` copies.

**2026-07-26 additions:**
- New `Nifty_Sensex_Pivot_EMA_Combined_Intraday_1` — merges the Pivot+Supertrend
  and EMA34+RSI strategies into one deployed process, sharing the underlying
  data fetch/WebSocket subscription/strategy_tag/PnL/trade-log while keeping
  all 8 legs (4 per engine) fully independent. See the script's own module
  docstring for the full design writeup.
- `Nifty_Sensex_Pivot_Supertrend_Intraday_1`: candle interval changed 5m → 3m,
  entry logic reverted to the original single-closed-candle signal (a
  two-candle variant was tried and backtested worse on 3m/5m candles, better
  only on 10m — not worth the added complexity).
- Combined, `VWAP_NoHA`, and `Expiry_Batman`: `entry_px`/exit price now
  corrected to the broker's real `average_price` (from `orderstatus()`, via
  `poll_fill`'s return value) once a fill is confirmed, instead of relying
  solely on a pre-trade LTP snapshot / post-fill LTP-cache guess. Falls back
  to the old estimate if the broker doesn't supply `average_price`.

Summary of what's in them (all 5 original scripts, unless noted):
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
- **2026-07-28, all 6 scripts:** Periodic re-push for legs in error_state.
  `push_leg_error()` previously only fired once, on the transition into
  error_state — if that one POST was lost (server busy, transient network
  blip), the UI's error badge silently never appeared, even though
  state.json correctly tracked the error the whole time (confirmed in
  production, 2026-07-28: three legs sat in `exit_failed` for 1-4 hours
  with no UI error shown). New `StrategyEngine._repush_active_errors()`
  re-pushes at most once per `config.error_repush_interval_sec` (60s,
  new config field) for every leg still in `error_state`, called once per
  `run_cycle()`. Dispatched via the existing `_pnl_executor` (not
  `_fill_executor`), same reasoning as `report_pnl_tick`: must never queue
  behind a fill-watcher stuck for minutes in a reprice loop. New
  `_last_error_push: dict[str, datetime]` tracks per-leg last-push time and
  self-clears once a leg's error is resolved. Covered by
  `test/test_strategy_pnl_executor.py`.
- **2026-07-28, all 6 scripts:** `threading.stack_size(1024 * 1024)` set at
  module load, before any thread is created. Python's default (8MB per
  thread) reserves ~96MB of virtual address space across the ~12 threads
  these scripts run at once (fill-watchers, signal-refresh, PnL push, the
  trade-log writer, PriceStream's watchdog/WS threads) -- none of which do
  anything beyond simple polling loops and REST calls, nowhere near deep
  recursion. That ~96MB comes directly out of the `STRATEGY_MEMORY_LIMIT_MB`
  `RLIMIT_AS` cap (`blueprints/python_strategy.py`'s `set_resource_limits()`,
  1024MB by default) every strategy subprocess runs under -- confirmed in
  production as the actual ceiling behind `RuntimeError: can't start new
  thread` (2026-07-28, both on the Combined script's trade-log-writer spawn
  and Batman's repair-fire dispatch). 1MB per thread is a generous margin
  for this workload while reclaiming the bulk of that reserved space.

  **Companion `.env` changes (not a code diff, so not tracked by git —
  noted here for context since they were applied alongside this fix on
  the same day):** `STRATEGY_MEMORY_LIMIT_MB` raised `1024` → `2048`
  (doubles the `RLIMIT_AS` ceiling itself), and `OPENBLAS_NUM_THREADS` /
  `OMP_NUM_THREADS` / `MKL_NUM_THREADS` / `NUMEXPR_NUM_THREADS` /
  `NUMBA_NUM_THREADS` all set to `2` (previously unset, meaning OpenBLAS/
  MKL default to one native thread per CPU core — those threads bypass
  Python's `threading` module entirely, so the stack-size fix above has no
  effect on them). Confirmed on a live process
  (`MCX_CrudeOil_EMA9_RSI_Intraday_1`): `VmPeak` sits at a stable
  ~1017MB regardless of these code-level changes (measured identical
  before and after) — the actual protection came from doubling the
  ceiling, not from either thread change reducing real usage. Both
  settings require an `openalgo.service` restart to apply to newly-spawned
  strategy subprocesses (read once via `os.environ.get(...)` at process
  spawn time).
- **2026-07-27, all 6 scripts:** `Broker.connect()` now passes
  `auto_reconnect=False` to the `api()` client. The SDK's own built-in
  auto-reconnect thread (`openalgo` package's `feed.py`) was racing each
  script's own `PriceStream._watchdog_loop`, which already owns full
  reconnect + resubscribe on the same client — both independently calling
  `_do_connect()` on `self.ws` could tear down/replace the socket
  concurrently and immediately trigger another spurious close. Observed in
  production as a repeating ~45-50s "connection down" cycle that never
  settled (70-100+ reconnects in a single session). The watchdog is now the
  sole owner of reconnect for every script.
- **2026-07-29, 5 scripts (Combined, EMA34_RSI, Pivot_Supertrend, VWAP_NoHA,
  Expiry_Batman — NOT MCX, which never subscribes to `NSE_INDEX`/
  `BSE_INDEX`):** widened WebSocket staleness threshold during the first
  ~45 minutes after market open. `NIFTY.NSE_INDEX`/`SENSEX.BSE_INDEX` only
  tick when the index actually recalculates off its constituents trading,
  which is naturally burstier right at 09:15 open than the rest of the day
  — legitimate gaps wider than the flat `ws_stale_seconds` (20s) threshold.
  `PriceStream`'s watchdog was misreading that normal opening-minutes
  irregularity as a dead connection and forcing repeated resubscribes;
  confirmed in production (2026-07-29) all the way down to real
  Unsubscribe/resubscribe cycles at the broker adapter
  (`fyers_websocket_adapter`), 23-53 events per script, all confined to the
  09:15-09:52 window and self-resolving once tick cadence settled.
  New `Config.ws_stale_seconds_open` (60.0) and
  `Config.ws_post_open_grace_until` (`time(10, 0)`); new module-level
  `_current_ws_stale_threshold()` returns `ws_stale_seconds_open` for
  `09:15 <= now < ws_post_open_grace_until` and the normal
  `ws_stale_seconds` otherwise. Only `_watchdog_loop()`'s
  reconnect-triggering staleness check uses this — `get_ltp()`'s
  REST-fallback `max_age` is untouched everywhere, since a REST fallback is
  cheap and harmless regardless of time of day. Covered by
  `test/test_ws_stale_threshold.py`.
