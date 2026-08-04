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

**If the merge touched `frontend/` at all, also actually build it — don't
rely on the merge being conflict-free as proof it's fine:**

```bash
cd frontend && npm run build
```

`py_compile` only proves the *Python* customizations still parse; nothing
above verifies the frontend customizations still typecheck against whatever
upstream's dependencies moved to. A fork-only frontend file (git conflicts
with nothing, since upstream has never touched it) can still silently break
because upstream renamed a shared dependency somewhere else entirely — see
the `react-router-dom` incident in the Frontend section below. `git diff
--stat` showing frontend/dist as the only frontend change is NOT the same
thing as the frontend actually building.

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
- **2026-08-04**: upstream/main fast-forwarded and merged into
  `custom-strategies`, zero conflicts including `frontend/dist/`. Merge
  itself was clean and fully verified against every customized *Python*
  file (0-line diff or reviewed-benign additive change on all of them, full
  test suite passing) — but the frontend was never actually built as part
  of that verification, only diffed. Days later, `npm run build` failed on
  the server: `PythonStrategyPnl.tsx`/`PythonStrategyTrades.tsx`/
  `PythonStrategyErrors.tsx` (fork-only files, see "New files" below) still
  imported from `react-router-dom`, which upstream had renamed to
  `react-router` at an earlier sync. Fixed by updating the three imports;
  root-caused and added the missing `npm run build` verification step to
  this procedure (see "Before committing the merge" above) so a fork-only
  frontend file rotting against a renamed dependency gets caught before
  push, not after a server rebuild days later.
- **2026-07-29**: 39 commits pulled from upstream (broker funds/order-API
  fixes across Angel/Dhan/Kotak/Upstox/Zerodha, Kotak order-streaming
  adapter, Strategy Builder payoff audit, frontend `frontend/dist` rebuild).
  Zero conflicts, including `frontend/dist/` — git resolved it automatically
  this time. Confirmed via `git log upstream-old..upstream-new -- <file>`
  that upstream touched none of this fork's customized core files
  (`app.py`, `blueprints/python_strategy.py`,
  `services/option_chain_service.py`, `services/websocket_client.py`,
  `sandbox/websocket_execution_engine.py`, `websocket_proxy/server.py`,
  `broker/fyers/streaming/fyers_websocket_adapter.py`) or `CLAUDE.md` in
  this batch — 0-line diff against the pre-merge tip on all of them,
  compiling clean, and this fork's full test suite (29 tests) still passing
  post-merge.
- **2026-07-26**: 22 commits pulled from upstream (skills additions, broker
  fixes, dependency bumps, `CLAUDE.md` restructure). Zero conflicts outside
  `frontend/dist/`. Verified `app.py`/`blueprints/python_strategy.py`/
  `services/option_chain_service.py` unchanged (0-line diff) and compiling
  clean post-merge; verified the ZeroMQ-bus and multi-session-login
  invariants survived `CLAUDE.md`'s restructure (moved, not dropped).

---

## Test Cases

Regression tests for anything touching the deployed strategy scripts live
under **`strategies/test/`** (a second `testpaths` entry in
`pyproject.toml`, alongside the top-level `test/` used for everything
else) — run either with `uv run pytest strategies/test/` or just
`uv run pytest`, which picks up both locations automatically.

| File | Covers |
|---|---|
| `strategies/test/test_strategy_pnl_executor.py` | Dedicated `_pnl_executor` isolation from `_fill_executor`, reduced thread stack size, periodic error re-push (`_repush_active_errors`), `report_pnl_tick`'s staleness threshold |
| `strategies/test/test_ws_stale_threshold.py` | `_current_ws_stale_threshold()`'s post-open grace window (widened threshold 09:15-10:00, normal threshold otherwise) |
| `strategies/test/test_candle_boundary_refresh.py` | `_current_candle_boundary()`/`_candle_key_boundary()`, `get_signal()`'s cache-based refresh dispatch, and a full simulated one-day session confirming the fetch-count reduction |

Core-code (non-strategy-script) regression tests stay in the top-level
`test/` directory as usual, e.g.:
- `test/test_websocket_client_bridge.py` — the eventlet cross-thread
  `concurrent.futures.Future` bridge fix in `services/websocket_client.py`
- `test/test_fyers_reconnect.py` — the Fyers adapter's non-blocking
  background reconnect

These two sets are loaded by different script-import helpers
(`strategies/test/*` loads a deployed script by file path via
`importlib.util.spec_from_file_location`, working around the `openalgo`
package-name collision described in each file's own
`_ensure_real_openalgo_sdk_loaded()` docstring; `test/*` imports core
modules normally) — keep new strategy-script tests in `strategies/test/`
and new core-code tests in `test/`, matching what they exercise.

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

**2026-08-05 — superseded by `strategy_reporting/` (see its own section
below), a bigger change than the same-process file-split originally
discussed here.** Root cause: `/traffic/api/stats` (57 sequential SQLite
queries) blocked the single gunicorn+eventlet worker long enough that
strategy subprocesses' PnL/error/pending-action/force-exit reporting calls
timed out — confirmed live even on a strategy script with the best available
client-side isolation, proving a same-process file split alone (which only
reduces future merge diff size, not runtime coupling) wouldn't have fixed
the actual incident. `strategy_reporting/` runs these routes in a genuinely
separate OS process instead. **This file (`blueprints/python_strategy.py`)
itself received ZERO changes as part of that work** — deliberately, per the
"file most likely to see upstream churn" note above; `strategy_reporting/`
relays every other `/python/*` request straight through to this file
unchanged, and reaches its still-in-process `broadcast_*`/
`stop_strategy_process` functions via a new ZMQ bridge rather than an
import. So the merge-conflict profile documented above is completely
unaffected either way.

**2026-07-30 follow-up — stagger multiple strategies' auto-start so they
don't all subscribe to shared symbols in the same instant.** Companion fix
to `websocket_proxy/server.py`'s stale-bypass work above: confirmed in
production that several strategies auto-starting together (either the daily
scheduled cron firing for strategies sharing an `hour:minute` start_time, or
a core app restart re-launching every previously-running strategy) each
independently subscribe to the same shared symbols (e.g. `NIFTY.NSE_INDEX`)
on the SAME single-user broker adapter connection at nearly the same
instant — exactly the condition behind the `NIFTY.NSE_INDEX` staleness
incidents, and it recurs every trading day at market open, not just during
manual restarts.

Two call sites, two mechanisms:
- `schedule_strategy()` (the daily cron path): new `_stagger_offset_seconds()`
  gives each strategy a deterministic `second` offset (0s, 5s, 10s, ... by
  sorted `strategy_id` position) added to its `CronTrigger`, so strategies
  sharing an `hour:minute` start_time fire a few seconds apart instead of in
  the same second.
- `restore_strategy_states()` (the core-restart recovery path): a plain
  `sleep(5)` between each back-to-back subprocess restart in the loop — but
  only between restarts, never before the first, so the common
  single-strategy-restart case is unaffected.

A few seconds' stagger is immaterial for entry timing but enough that
concurrently-launched strategies are much less likely to land in the same
instant (or the same 0.15s Fyers HSM batch window,
`fyers_websocket_adapter.py`'s `HSM_BATCH_DELAY_SEC`) every trading day.

Covered by 6 new tests in `test/test_python_strategy_edge_cases.py`
(offset determinism/ordering, `CronTrigger.second` wiring, and the
sleep-between-not-before-first behavior via a monkeypatched `sleep`).

---

## `strategy_reporting/` (new package, fork-only, 2026-08-05)

**Why:** `/traffic/api/stats` runs ~57 sequential SQLite queries per call
(`NullPool` — a fresh connection per query). Under gunicorn+eventlet, which
only yields at monkey-patched socket I/O (not SQLite/file I/O), that
endpoint blocks the single worker for its full multi-second duration —
during which strategy subprocesses' own loopback reporting calls
(`report_pnl_to_platform`, `check_pending_action`, `check_force_exit`, etc.)
timed out, confirmed live in production even on a strategy script that
already had the best available client-side isolation (a separate
APScheduler job, backgrounded platform calls). A same-process fix (reducing
that one endpoint's query count) removes the specific trigger; it doesn't
protect against the next slow endpoint. The user chose the structural fix.

**What it is:** a genuinely separate OS process
(`strategy_reporting/server.py`, spawned from `app.py` exactly like
`websocket_proxy/app_integration.py`'s `_spawn_websocket_subprocess()` —
same `subprocess.Popen`/`atexit`/SIGTERM→SIGKILL shape), running a plain
threaded WSGI server (`werkzeug.serving.run_simple(..., threaded=True)`,
deliberately not eventlet — no Socket.IO involvement in this process, so
eventlet's usual justification doesn't apply, and plain OS threads mean
nothing that ever blocks the main gunicorn worker can affect this one).

It implements locally (own DB tables, own dual auth — API-key for
subprocess calls via `database.auth_db.verify_api_key`, session-cookie for
browser calls via `utils.session.check_session_validity` configured with
the same `APP_KEY` so it validates the identical signed cookie) exactly the
routes that were actually timing out: PnL push/read, leg-error push/read,
pending Retry/Cancel/Manual actions, Force Exit request/poll/complete, and
trade/execution/PnL history reads. **Everything else under `/python/*`**
(start/stop/schedule/upload/CRUD, logs, status, the SSE stream itself) is
relayed unchanged to the unmodified main process — see the
`blueprints/python_strategy.py` section above for why that file was
deliberately left untouched rather than migrated. One nginx rule
(`location /python { proxy_pass http://127.0.0.1:8766; }` — see
`install/install.sh`/`install/install-multi.sh`) routes the whole `/python`
prefix here; the subprocess does the local-vs-relay dispatch internally.

**New shared state:** `database/strategy_reporting_db.py` — `StrategyPnl`,
`StrategyLegError`, `StrategyPendingAction`, `StrategyForceExit` tables in
the same `openalgo.db` (via `create_db_engine()`, matching
`database/scalping_db.py`'s precedent — no separate DB file needed for this
amount of state). Replaces what used to be in-process dicts
(`STRATEGY_PNL`/`STRATEGY_ERRORS`/`STRATEGY_ACTIONS`/`STRATEGY_FORCE_EXIT`
in `blueprints/python_strategy.py`) — those dicts' *code* in
`blueprints/python_strategy.py` is untouched (unreachable dead weight from
nginx's perspective, harmless, not worth the diff to remove) since that
file was never edited.

**New ZMQ bus** (`ZMQ_REPORTING_PORT`, default 5565, see `CLAUDE.md`'s
ZeroMQ bus section) so the new subprocess can tell the main process to (a)
broadcast a live SSE update via the still-unchanged `broadcast_pnl_update`/
`broadcast_error_update`/`broadcast_trade_update`/`broadcast_status_update`
functions, or (b) actually stop a strategy's OS process after a completed
Force Exit via the still-unchanged `stop_strategy_process` — both called
from `strategy_reporting/broadcast_bridge.py` (runs inside the main
process, started from `app.py`) rather than imported into the new
subprocess, since only the main process holds the actual Popen handle /
SSE subscriber list.

**2026-08-05 follow-up — the receive loop must NOT be a genuine OS thread
under eventlet.** Shipped first with this fork's usual
`eventlet.patcher.original("threading")` escape hatch, matching every
other "needs a real blocking loop" case in this codebase
(`services/websocket_client.py`, `sandbox/websocket_execution_engine.py`).
Wrong here specifically: unlike those cases, this loop's whole job is to
call back INTO code (`broadcast_*`) that touches `SSE_SUBSCRIBERS`' `queue.Queue`
objects and `SSE_LOCK` — the eventlet-monkey-patched primitives, since the
whole gunicorn process is patched. Signaling those from a genuine,
unpatched OS thread is the exact `greenlet.error: Cannot switch to a
different thread` class of bug already fixed in
`services/websocket_client.py`'s `_run_coroutine_and_wait` (production
incident, 2026-07-29) — except here it didn't crash, it silently degraded:
observed in production as "PnL/trade price updating, just very slowly."
A foreign thread's `queue.put()` can leave the SSE generator's
`q.get()` without a notification eventlet's hub actually acts on, so the
update only surfaces once something else gives the hub a reason to poll
(worst case, `api_strategy_events`' own 30s heartbeat cycle).

Fixed: under eventlet, the receive loop runs as a genuine `eventlet.spawn()`
green thread, not an OS thread — only the individual blocking `socket.recv()`
call leaves the green world, via `eventlet.tpool.execute()` (eventlet's own
documented, hub-safe bridge for "one blocking call on a background native
thread, result handed back to the calling greenlet"). Everything
downstream, including every `broadcast_*` call, runs on the calling
greenlet — safe by construction, no cross-thread signaling into
eventlet-patched primitives at all. Outside eventlet (this fork's own
Windows/dev-machine testing has no `eventlet` installed at all) there's no
greenlet/native-thread split to worry about, so the original real-OS-thread
approach is kept for that path. Caught by a synthetic SSE relay latency
test before understanding the root cause — worth remembering the test
initially pointed at the wrong layer (the relay itself, which turned out
to already be fast) before the actual cross-thread signaling bug was
found by re-reading this exact class of fix already on record in
`services/websocket_client.py`.

**Strategy scripts** (`strategies/deployed/*.py`, all 7): `_post_json_local`/
`_get_json_local` retarget from `openalgo.sock`/`FLASK_PORT` to
`STRATEGY_REPORTING_PORT` (default 8766, TCP loopback only — no Unix socket
for this component, see `server.py`'s `main()` for why) for exactly the 6
reporting calls. The now-unused `_UnixHTTPConnection` class was removed
from all 7 (confirmed dead — see `strategies/deployed/AUTHORING_CHECKLIST.md`).

**Known gap:** Docker deployment (`install/install-docker.sh`) uses
Docker Compose service names as nginx upstream targets and has no
`docker-compose.yml` checked into this repo (generated by the install
script) — adding `strategy_reporting` there needs its own container/service
definition this work didn't attempt, since it couldn't be validated without
a real Docker environment. Direct/systemd installs (`install.sh`,
`install-multi.sh`) and this fork's own `update-custom.sh` are fully
covered.

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

## `services/websocket_client.py` (+12 / -1 lines, plus a 2026-07-29 follow-up)

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

**2026-07-29 follow-up — a second, distinct instance of the same crash
class, this time inside a third-party primitive we can't directly patch:**
`subscribe()`, `unsubscribe()`, and `unsubscribe_all()` each called
`asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=N)`
directly from the calling side to get the result of a coroutine running on
the loop's real OS thread. `run_coroutine_threadsafe()`'s returned
`concurrent.futures.Future` uses the same eventlet-patched `threading`
module as everywhere else in the process for its internal
Condition/Lock — resolving it from the loop's real thread while a greenlet
blocks in `result()`'s wait crashes with the identical `greenlet.error:
Cannot switch to a different thread`. Confirmed in production, 2026-07-29:
fired 9 times in a single session (`journalctl -u openalgo.service | grep
-c "Cannot switch to a different thread"`), each immediately after a
`subscribe()`/`unsubscribe()` call (e.g. right after
`sandbox/websocket_execution_engine.py`'s `Position feed: subscribing
NFO:...`/`BFO:...` log lines) — and directly responsible for two live
option legs never receiving a single WS tick for 45+ minutes that day
despite ~20 full PriceStream reconnects, since the crash fires unhandled
inside eventlet's hub (outside any try/except, never reaching
`log/errors.jsonl`) and can leave the hub's timer/semaphore bookkeeping in
a bad state for whatever else is running at that moment.

Fix: new `_run_coroutine_and_wait()` helper never touches the
`concurrent.futures.Future` itself from the calling thread. Its
`add_done_callback` runs on the loop thread (real OS thread, safe) when the
coroutine finishes — reads the result there, and signals completion via a
genuine `_original_threading.Event` (no greenlet/thread affinity, safe from
either side) that the calling thread waits on instead. `disconnect()`'s own
`run_coroutine_threadsafe()` call was untouched — it's fire-and-forget,
never calls `.result()`, so it was never affected. Covered by
`test/test_websocket_client_bridge.py`.

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

**2026-07-30 follow-up — the refcount skip above is permanent and
unrecoverable once the underlying broker-level subscription itself goes
bad.** Confirmed in production: Batman and Combined both independently
subscribe to `NIFTY.NSE_INDEX`; a fresh manual `/websocket/test` subscribe to
the same symbol got live ticks (ruling out a broker outage), yet both
strategies retried their own unsubscribe/resubscribe cycle every 15-30s for
7+ minutes straight with zero recovery. Root cause: each one's resubscribe
attempt saw the OTHER's still-present bookkeeping entry in
`subscription_index` and got silently skipped by the fix above — neither
could ever force a real `adapter.subscribe()` call, because "someone already
holds this" was being trusted as proof the feed was healthy, with no check
that data was actually still flowing. (Two earlier, narrower theories —
Fyers' batch-subscribe token/symbol mixing, and a dead-code `unsubscribe_symbols()`
never clearing stale HSM mappings — were investigated and fixed first, see
the `broker/fyers/streaming/` section below, but turned out not to be the
operative cause of *this* specific incident: proxy-level `DEBUG-TEMP` logging
showed zero real adapter-level subscribe/unsubscribe calls firing at all
during the stale window, meaning the Fyers-layer fixes were never even being
exercised.)

Fix: new `_is_subscription_genuinely_stale()` — a symbol currently held by
another client is only trusted as healthy if `last_message_time` shows a
tick within `REDUNDANT_SUBSCRIBE_STALE_BYPASS_SEC` (30s). New
`subscription_first_held_at` dict (set when a sub_key transitions from
unheld to held, cleared alongside `subscription_index` in all three places
it goes back to empty — `unsubscribe_client`'s two branches and
`cleanup_client`) lets this distinguish "just subscribed, too early to
judge" from "held a long time with zero ticks ever," treating both
"never ticked" and "used to tick, now silent" as equally stale. When stale,
the skip is bypassed and a real `adapter.subscribe()` fires regardless of
who else's bookkeeping entry is present.

Also corrected a related bug caught mid-investigation in `_log_stale_symbols()`
(added earlier in this same investigation as a diagnostic): its first version
skipped any sub_key with `last_message_time` still `None`, reasoning "too
early to judge" — which meant a symbol broken from its very first subscribe
would never get flagged, since that field would stay `None` for its entire
remaining lifetime. Now uses `subscription_first_held_at` as a fallback
clock so "never ticked, held a long time" is flagged too.

Covered by `test/test_websocket_proxy_stale_symbol_bypass.py` (7 tests,
constructing `WebSocketProxy` via `__new__()` to exercise the real decision
methods without the ZMQ socket bind/port checks in `__init__`).

Still carries the `[DEBUG-TEMP]`-tagged correlation/diagnostic logging from
earlier in the same investigation (not yet removed — left in place to
confirm this fix in production).

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

## `broker/fyers/streaming/fyers_websocket_adapter.py` + `fyers_adapter.py`

**2026-07-30**: root-caused and fixed a production issue where `NIFTY.NSE_INDEX`
went permanently stale in the Combined and Batman strategies simultaneously
(other symbols on the same connection kept ticking fine), recovering only
after a full strategy-process restart — while a fresh manual
`/websocket/test` subscribe to the identical symbol got live ticks the whole
time, ruling out a broker-side outage. Both strategies independently
subscribe to `NIFTY.NSE_INDEX`; since this is a single-user deployment, all
running strategies share the exact same `FyersAdapter`/`FyersWebSocketAdapter`
instance and connection.

Root cause, two compounding gaps in the same shared adapter:

1. **`_flush_hsm_batch()` mixed unrelated callers' symbols into one Fyers
   request.** Subscribe requests are queued and, after a `HSM_BATCH_DELAY_SEC`
   (0.15s) debounce, flushed as ONE combined `subscribe_symbols()` call per
   data type — a deliberate optimization so a burst of subscribes collapses
   into one Fyers symbol-token POST. But the queue has no concept of *which
   strategy* enqueued an item: if Combined's watchdog resubscribed to
   `NIFTY.NSE_INDEX` within 0.15s of Batman's watchdog resubscribing to a
   different symbol, both landed in the same flush and got sent to Fyers as
   one multi-symbol request. Fyers' `/data/symbol-token` API is documented
   (see `FyersAdapter.subscribe_symbols`'s existing comment) to not reliably
   preserve/pair multiple symbols correctly when several are requested
   together — the same class of bug already known for "index + options mixed
   in one call," just triggered here by two different strategy processes
   colliding in time instead of one strategy's own request. Once the HSM
   token→symbol mapping is scrambled, ticks still match something via the
   normal fast-path token lookup — they just silently route to the wrong
   symbol's subscriber, with no error or warning anywhere. This explains why
   restarting all strategies together after a core deploy reproduces the bug
   reliably (every process's startup subscribe clusters into the same
   window), while restarting them one at a time avoids it (each gets an
   isolated flush).

   **Fix**: `_flush_hsm_batch()` now issues one `subscribe_symbols()` call per
   symbol instead of grouping every symbol in the window into one call. This
   removes the batch-mixing condition entirely, at the cost of the call-
   collapsing efficiency the queue existed for (a mass-restart of several
   strategies now makes N individual WebSocket subscribe frames over the
   already-open Fyers connection instead of 1 combined one — cheap local DB
   token lookups either way, no additional Fyers REST/rate-limit exposure).

2. **`FyersAdapter.unsubscribe_symbols()` was dead code — defined but never
   called from anywhere** (`grep -rn "unsubscribe_symbols" broker/fyers/`
   found only its own definition). It logged a message and did nothing,
   meaning `active_subscriptions`/`symbol_to_hsm`/`hsm_to_symbol` entries were
   never removed on unsubscribe — permanent "ghost" mappings for the
   adapter's entire lifetime, which could misdirect a token Fyers later
   reuses for a different instrument. (The actual production-facing
   unsubscribe path, `FyersWebSocketAdapter.unsubscribe()`, already did its
   own separate cleanup of callback-routing dicts correctly — this gap was
   specifically in `FyersAdapter`'s own token-mapping state, one layer
   deeper.)

   **Fix**: `unsubscribe_symbols()` now actually clears those three dicts, and
   `FyersWebSocketAdapter.unsubscribe()` calls it — but only once no sibling
   mode (Quote vs Depth) subscription remains for the same symbol, since
   those dicts are keyed by symbol alone, not symbol+mode, and clearing them
   while a sibling subscription is still live would break that sibling's
   routing too (the same class of bug issue #1093 already fixed for the
   callback-registry side).

Covered by `test/test_fyers_hsm_batch_and_unsubscribe.py` (9 tests: one-call-
per-symbol verification, data-type grouping, dedup-last-writer-wins, the
not-connected skip path, HSM-tracking clear/preserve-other-symbols/empty-list
for `unsubscribe_symbols()`, and the sibling-mode-preserved vs
last-sibling-cleared cases end to end through `FyersWebSocketAdapter.unsubscribe()`).
Pre-existing `test/test_fyers_reconnect.py` still passes unchanged.

Also carries temporary `[DEBUG-TEMP]`-tagged logging (not yet removed) added
earlier in the same investigation, in `fyers_adapter.py`'s `_on_message` and
`websocket_proxy/server.py`'s `subscribe_client`/`unsubscribe_client`/
`cleanup_client`, plus matching `[COMBINED]`/`[MCX]`/`[BATMAN]` correlation
tags in those three strategy scripts' `PriceStream` watchdogs — left in place
to confirm the fix in production; safe to strip once confirmed stable.

**2026-07-30, second follow-up — the recovery mechanism is now confirmed
fixed (self-heals within minutes instead of staying broken permanently),
but the ORIGINAL question — why a symbol's feed goes dead for those minutes
in the first place — was still unconfirmed.** Every diagnostic added until
now lived above `broker/fyers/streaming/fyers_hsm_websocket.py` — the actual
raw binary WebSocket client that decodes Fyers' HSM protocol frames — so
none of them could distinguish "Fyers never sent anything for this token"
from "it arrived at the wire but got lost somewhere in OpenAlgo's own
dispatch chain above this file."

New in `fyers_hsm_websocket.py`:
- `_token_last_seen: dict[str, float]` — per-HSM-token-string last-seen
  timestamp, updated in `_parse_snapshot_data` and `_parse_update_data` the
  moment a frame referencing that exact `topic_name` is parsed, before any
  symbol-mapping or callback dispatch runs.
- `_log_stale_tokens()` — run periodically from the existing
  `_health_check_loop` (which already checks connection-wide
  `_last_message_time`, i.e. "is ANYTHING arriving") — this checks each
  individually subscribed token instead, so a symbol whose own data never
  arrives can be told apart from "the whole connection is dead." Warns
  distinctly for "never seen at all" (points upstream, at Fyers' own
  server) vs. "was seen before, went silent for Ns" (same conclusion, with
  a duration), throttled to once per `_token_stale_warn_threshold` (60s)
  per token.
- A new `else` branch in `_parse_update_data`'s inner `sf|`/`if|`/`dp|`
  dispatch: previously, if a topic_id was a known subscription but its
  initial snapshot was never successfully parsed into
  `scrips_data`/`index_data`/`depth_data` (e.g. an exception during the
  first snapshot), every subsequent update frame for that exact token fell
  through the if/elif chain with **zero logging anywhere** — a second,
  independent silent-drop mechanism this closes visibility on.

Covered by `test/test_fyers_hsm_raw_token_staleness.py` (6 tests). This is
pure diagnostic logging — no behavior change, no fix — added specifically
to gather the one piece of evidence this investigation never actually
collected: what Fyers is doing at the wire level during the next
occurrence.

Medium conflict risk if upstream touches the same Fyers HSM batching/
subscription code — isolated to `broker/fyers/streaming/`, no shared
interface changes.

---

## `broker/shoonya/streaming/shoonya_adapter.py`

**2026-07-30**, ahead of a planned live deployment on Shoonya: found and
fixed the same bug *class* as `websocket_proxy/server.py`'s
`REDUNDANT_SUBSCRIBE_STALE_BYPASS_SEC` fix above, recurring one layer
deeper, specific to this broker.

Shoonya's `subscribe()` has its own, independent "already subscribed" check
(`already_ws_subscribed`, matched by correlation_id prefix) — if a
`(symbol, exchange, mode)` already has a tracked subscription, it skips
sending the real WebSocket subscribe frame entirely, registering only new
client-side bookkeeping. This is a second, broker-specific instance of
"bookkeeping says subscribed" being trusted as proof of health, independent
of and invisible to the proxy-level fix, which only forces a real
`adapter.subscribe()` *call* — it can't see or override what Shoonya's own
adapter code does once that call arrives.

This interacts badly with the same-day PriceStream fix (`strategies/`
section below): unlike Fyers, Shoonya's `unsubscribe()` genuinely clears
this bookkeeping — but PriceStream no longer calls `unsubscribe_ltp()`
before `subscribe_ltp()` (a correct fix for Fyers, where that call was a
pure no-op). For Shoonya, that reset used to be exactly what let a stuck
token's `already_ws_subscribed` bookkeeping clear so a real resubscribe
could go out. Without a matching fix here, Shoonya's cheap per-symbol retry
path would become **permanently** unable to force a real resubscribe once a
token's bookkeeping goes stale — the same failure mode just fixed for
Fyers/the proxy, reintroduced for Shoonya by a fix that was correct for a
different broker.

Fix: new `_is_token_genuinely_stale()` — an `already_ws_subscribed` token is
only trusted as healthy if `_token_last_tick` (new, updated in
`_process_market_message` the moment a genuine tick for that token is
received) shows activity within `SUBSCRIBE_STALE_BYPASS_SEC` (30s).
Otherwise `subscribe()` bypasses the skip and sends a real WS resubscribe
frame regardless of existing bookkeeping. New `_token_first_subscribed_at`
(set the moment a token is genuinely freshly subscribed, cleared alongside
`token_to_symbol`/`market_cache` on full unsubscribe) lets this distinguish
"just subscribed, too early to judge" from "subscribed a while with zero
ticks ever" — mirrors `websocket_proxy/server.py`'s
`subscription_first_held_at`/`_is_subscription_genuinely_stale` exactly.

Covered by `test/test_shoonya_subscribe_stale_bypass.py` (9 tests:
staleness-decision determinism, fresh-subscribe sends a real WS frame,
already-subscribed-and-healthy correctly skips a redundant frame,
already-subscribed-but-stuck correctly bypasses and resends, and
`_process_market_message` updating `_token_last_tick`).

Low conflict risk — isolated to `broker/shoonya/streaming/`, no shared
interface changes.

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

**"Zero conflict risk" means git found no textual conflict — it does NOT mean
the file is unaffected by upstream's changes elsewhere.** Incident, 2026-08-04:
these three "new, upstream doesn't know about them" files still imported from
`react-router-dom`. Upstream had renamed the dependency to `react-router` (v8)
network-wide at some earlier sync, and every OTHER page picked that up because
git's merge touched them directly — these three didn't, because upstream has
never touched them and never will, so nothing about the merge itself would
ever surface the mismatch. `npm run build` (`tsc -b`) failed on the server
with `TS2307: Cannot find module 'react-router-dom'` days after a clean,
zero-conflict merge had already been pushed. A file being "new/unowned by
upstream" is a reason it can't merge-conflict, not a reason it's safe to skip
verifying — it can still silently rot against a dependency the rest of the
codebase moved on from. **Actually run `cd frontend && npm run build` after
every merge that touches `frontend/` at all** (not just when `frontend/dist/`
shows the expected conflict) — the pre-merge sanity checks above only
`py_compile` the Python customizations; nothing was verifying the frontend
customizations actually still typecheck against current dependencies.

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
  `strategies/test/test_strategy_pnl_executor.py`.
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
  `strategies/test/test_ws_stale_threshold.py`.
- **2026-07-29, same 5 scripts — follow-up found during a targeted PnL-review
  pass:** `report_pnl_tick()`'s own `get_ltp()` call still used the flat
  `config.ws_stale_seconds` (20s), not `_current_ws_stale_threshold()`
  introduced by the fix above. Result: a leg entered during the 09:15-10:00
  post-open grace window could flicker in and out of the pushed PnL payload
  every ~20s even on a connection the watchdog now correctly judges healthy
  (up to 60s of gap tolerated there) — the exact visual symptom of the WS
  bug already fixed today, but recurring every morning from this threshold
  mismatch alone, independent of any actual connection problem. Changed the
  `max_age` argument in each script's `report_pnl_tick()` to call
  `_current_ws_stale_threshold()` instead, so both the watchdog's
  reconnect-triggering check and the PnL-reporting freshness check agree on
  what counts as stale at any given moment. Deliberately did NOT add a REST
  fallback to `report_pnl_tick()` itself — it stays WS-only by design (a
  1-second job doing a REST `quotes()` call per open leg would spam the
  broker all day); this fix only aligns the threshold, not the fallback
  behavior. Covered by `test_report_pnl_tick_uses_current_ws_stale_threshold`
  in `strategies/test/test_strategy_pnl_executor.py`.

  **Noted but explicitly out of scope for this fix:** `Expiry_Batman`'s
  `_aggregate_unrealized_pnl()` has the exact same `config.ws_stale_seconds`
  gap, but it's a decision-affecting function (drives the
  universal-exit-PnL breach check), not a display-only one — changing it
  needs separate, more careful consideration and was not requested.
- **2026-07-29, 5 scripts (Combined, EMA34_RSI, Pivot_Supertrend, MCX,
  VWAP_NoHA — NOT Expiry_Batman, which has no candle-based indicator
  signal at all: confirmed by grep, it's a pure straddle-entry +
  spot-move-triggered repair strategy with zero `client.history()`/
  `get_signal()` calls):** `get_signal()`/`_get_option_signal()`'s
  indicator-refresh throttle replaced a pure rolling timer ("has
  `indicator_refresh_interval` (15s) passed since the last fetch?") with
  one that's aware of where the actual candle boundaries fall. The old
  timer had no idea a 3-minute (2-minute for VWAP_NoHA's option-level
  signal) candle only closes once every 180s (120s) — it fetched roughly
  12 (8) times per candle, ~11 (7) of them wasted re-confirming the same
  still-open candle, and the one useful fetch could land anywhere up to
  15s late relative to the true close depending on timing phase.

  New `_current_candle_boundary(interval_minutes)` computes the
  start-of-bucket timestamp for "now" (e.g. 16:16:42 → 16:15:00 for a 3m
  bucket). New `_candle_key_boundary(candle_key)` parses a cached signal's
  own `candle_key` (`str(bars.index[-1])`) back into a datetime for direct
  comparison. `get_signal()` now fetches only when the CACHED signal's own
  candle is behind the current boundary — once it catches up, no further
  fetches happen until the next boundary arrives, and if a fetch attempt
  doesn't yield the new candle yet (broker-side finalization lag), it
  keeps retrying every tick instead of waiting out a fixed interval
  (bounded by the pre-existing `_signal_refresh_pending` guard, so at most
  one fetch is ever in flight per instrument/leg). Net effect, confirmed
  by a full simulated 09:15-15:30 session in
  `strategies/test/test_candle_boundary_refresh.py`: ~151 fetches over the whole
  session versus ~1500 under the old rolling-only throttle — roughly a
  10x reduction — while also cutting worst-case new-candle detection
  latency to ~`scheduler_interval` (10s) + one broker round-trip, down
  from up to `indicator_refresh_interval` (15s) + round-trip + a possible
  extra scheduler cycle.

  Combined/Pivot_Supertrend's independent `due_daily` check (daily pivot
  refresh, its own 600s cadence) is unaffected — it still triggers on its
  own schedule regardless of candle-boundary state, since the daily pivot
  has nothing to do with 3-minute candles.
- **2026-07-29, 4 scripts (Combined, MCX, VWAP_NoHA, Expiry_Batman —
  EMA34_RSI/Pivot_Supertrend intentionally left untouched, since those
  standalone scripts are no longer deployed):** `PriceStream._watchdog_loop()`'s
  full-reconnect escalation rule replaced. The OLD rule was "ANY single
  tracked symbol stale for `ws_stale_reconnect_after` (3) consecutive
  cycles → full reconnect," shared across every symbol on the connection.
  Confirmed in production, 2026-07-29 (MCX): a single thinly-traded option
  leg stayed stale for an extended stretch — genuine low liquidity, not a
  broken feed — while the futures contract on the SAME connection ticked
  fine the whole time. The old rule escalated anyway, tearing down and
  disrupting the healthy futures stream for no possible benefit, since
  reconnecting cannot make an illiquid contract start trading.

  Three coordinated changes:
  1. **Majority-based escalation** — a full reconnect now only fires when
     more than half of tracked symbols are simultaneously stuck past the
     streak limit (`len(symbols_at_limit) > len(all_keys) / 2`), a real
     signal the whole connection is broken, not just one contract. A
     minority/single stale symbol never escalates on its own.
  2. **Per-symbol backoff** (`_symbol_backoff_step`/`_symbol_next_retry_at`,
     new `PriceStream` instance state) — independent retry pacing per
     `(symbol, exchange)`, using the same `(1, 2, 5, 10, 30)` backoff shape
     as the connection-wide path, but scoped so one chronically stale
     symbol's growing wait never slows down retries for a different
     symbol. Cleared in `remove_instruments()` alongside `_stale_streak`,
     and reset the moment a symbol produces a fresh tick again.
  3. **REST cross-check before escalating** — new
     `_confirm_genuinely_broken_via_rest()` calls the broker's `quotes()`
     REST endpoint for the majority-stale symbols before actually
     reconnecting. If REST shows the price has genuinely moved since the
     last cached tick, the WS feed really is failing to deliver and the
     reconnect proceeds. If REST also shows the exact same frozen price,
     it's thin liquidity, not a broken feed — the disruptive reconnect is
     skipped and per-symbol backoff keeps retrying instead. A REST call
     that itself errors counts as "can't confirm," not as proof of
     brokenness (needs only one symbol to show real movement to proceed).

  Covered by `strategies/test/test_pricestream_reconnect_backoff.py`,
  including a full simulated 09:15-15:30 session with one permanently
  thin symbol confirming zero full reconnects across the entire day, and
  a companion test confirming a genuine simultaneous outage across all
  tracked symbols still correctly escalates.

- **2026-07-30, same 4 scripts — per-symbol retry no longer calls
  `unsubscribe_ltp()` before `subscribe_ltp()`.** Confirmed in production
  (Combined and Batman both stuck on `NIFTY.NSE_INDEX` for 7+ minutes,
  retrying every 15-30s) that this unsubscribe/subscribe pair never once
  self-recovered a stuck feed, while a single clean manual subscribe (no
  preceding unsubscribe) via `/websocket/test` recovered it every time it
  was tried — a fully consistent pattern across multiple occurrences.
  Root cause: Fyers' HSM protocol has no real per-symbol unsubscribe (see
  the `broker/fyers/streaming/` section's `unsubscribe_symbols()` entry
  above) — the "unsubscribe" step only ever cleared OpenAlgo's own
  tracking dicts, it never told Fyers to actually stop the token. So every
  retry cycle was, from Fyers' perspective, a redundant re-subscribe
  request for a token it already considered active, sent immediately
  after OpenAlgo wiped its own bookkeeping for it — plausibly confusing or
  rate-limited by Fyers' server given it repeated every 15-30s for
  minutes on end. The per-symbol retry path now calls `subscribe_ltp()`
  alone, since a subscribe to an already-subscribed token is a safe,
  idempotent re-affirmation on its own.

  Covered by a new test in `strategies/test/test_pricestream_reconnect_backoff.py`
  (`test_per_symbol_retry_never_calls_unsubscribe`) asserting
  `unsubscribe_ltp` is never called on the per-symbol retry path while
  `subscribe_ltp` still is; all 11 tests in that file pass.

- **2026-08-01, `broker/fyers/streaming/fyers_token_converter.py` —
  `FyersTokenConverter.convert_symbols_to_hsm()` no longer trusts Fyers'
  echoed symbol string as the HSM-token-to-symbol mapping key for
  unambiguous single-symbol requests.** Root-caused via production log
  evidence: `CRUDEOIL17AUG267850CE`/`PE` and the NIFTY index showed "HSM
  token not in mappings" on every single tick (11,646 occurrences in one
  ~3.5 hour window), while other MCX symbols on the same connection
  (`CRUDEOIL17AUG267900PE`, `CRUDEOIL19AUG26FUT`) ticked fine. The warning
  log itself carried the smoking gun: `fyers_symbol=CRUDEOIL17AUG26C7850`
  (the correct live-tick format) vs. `original_symbol=MCX:CRUDEOIL26AUG7850CE`
  (day-of-month dropped) — same instrument (numeric HSM token matched the
  DB's token exactly), different string.

  This is the same class of Fyers `/data/symbol-token` API unreliability
  already documented in the 2026-07-30 entry above (that fix stopped
  trusting the API to preserve symbol *order/pairing* across a
  multi-symbol request), just one layer deeper: even a single, unbatched
  request — confirmed via `_flush_hsm_batch()`, which already sends one
  symbol per call in production — can get back a `validSymbol` key that
  doesn't match what was sent, while the fytoken/HSM token underneath
  still resolves to the correct instrument. `FyersAdapter.subscribe_symbols()`
  joins on that returned string against its own `get_br_symbol()` output
  (a DB lookup), so any mismatch silently breaks the HSM-token-to-symbol
  join forever for that token.

  Side effect confirmed by reading the fallback chain in
  `fyers_adapter.py`'s tick-routing code: when exactly one symbol is
  subscribed, a last-resort "single subscription match" fallback papers
  over the failure. MCX's normal trading pattern is **two** concurrent
  subscriptions (futures contract + whichever option leg is open), so
  that fallback doesn't apply — the tick is logged as "No HSM token
  match" and silently dropped, forcing the strategy onto its REST-quotes
  fallback ("WS LTP stale/missing") for the rest of that instrument's
  life on the connection. No wrong orders or corrupted positions result
  (REST fallback still drives PnL/exits correctly), but it defeats the
  live WS feed for that symbol during an open trade.

  **Fix**: when the request is unambiguous (exactly one brsymbol sent,
  exactly one valid symbol returned), `token_mappings[hsm_token]` is set
  to the brsymbol we sent, not the string Fyers echoed back. Batched
  multi-symbol requests (no real caller does this today, but kept for
  safety) keep the prior behavior rather than risk guessing a wrong
  pairing among several ambiguous candidates.

  Covered by `test/test_fyers_token_converter_symbol_echo.py` (4 tests:
  single-symbol echo-mismatch uses our own sent brsymbol, matching-echo
  case is unchanged, ambiguous multi-symbol batch keeps old behavior, and
  a mismatch logs a warning). Pre-existing
  `test/test_fyers_hsm_batch_and_unsubscribe.py`,
  `test/test_fyers_hsm_raw_token_staleness.py`, and
  `test/test_fyers_reconnect.py` (21 tests total) still pass unchanged.
