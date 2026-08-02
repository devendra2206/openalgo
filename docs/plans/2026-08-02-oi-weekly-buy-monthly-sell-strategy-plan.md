# NIFTY OI-Based Weekly Buy + Monthly Sell — Implementation Plan

**Status:** Design — not yet built, open questions block coding
**Date:** 2026-08-02
**Source spec:** `C:\Devendra\OpenAlgo\logs\OI based weekly buy and monthly sell.txt`
  ("NIFTY Intraday OI-Based Trading System – Functional Specification, Final Version")
**Backtest reference:** `OI Intraday Buy_*.pdf` / `OI Intraday Sell_*.pdf` (same folder),
  analyzed in this session — 358.8% / 70.1% return over 2024-12-05→2026-06-30 on
  ₹50K / ₹2.5L capital respectively, 26.1% / 2.9% max drawdown. That backtest's
  entry/exit rules are **not** derivable from the PDFs (they only show fills, not
  the signal engine) — this plan implements the separately-provided functional
  spec, and its fidelity to what actually produced that backtest is unverified.

---

## 1. Signal logic — CORRECTED (2026-08-02, supersedes the source .txt on this point)

The source spec's Weekly and Monthly signal rules **cross-referenced CE and PE OI
against each other** ("Weekly Bullish = CE OI decreases AND PE OI increases", and
symmetrically in Monthly's own confirmation, §10 of the .txt). After review this was
corrected to a **fully side-independent** model — CE and PE are two parallel systems
that never reference each other's OI or premium, at any layer. This section is the
authoritative logic; the raw .txt's cross-referenced version is superseded.

### 1.1 Reference Engine (unchanged from the spec)

Fixed once per day, computed at market open:

- Gap% = (09:30 spot − prev close) / prev close × 100
- `|Gap%| ≤ 0.5%` (GAP_THRESHOLD) → Reference Time = **previous day's close**
- `|Gap%| > 0.5%` → wait for the 09:35 candle → Reference Time = **today 09:35**

### 1.2 Per-side OI+Premium interpretation (the corrected core rule)

Applied **independently** to CE and to PE — each side uses only its own candle data
(its own premium close vs. its own premium at Reference Time; its own OI vs. its own
Reference OI). Standard OI-interpretation table, applied per-side:

| Premium | OI | Meaning | Verdict for that side | Used by |
|---|---|---|---|---|
| ↑ | ↑ | Long Build-up | **Accumulation** (bullish, can be sharp for Short Covering specifically) | Weekly Buy trigger |
| ↓ | ↑ | Short Build-up | **Weakening** (bearish, often weaker for Long Unwinding specifically) | Monthly Sell confirmation |
| ↑ | ↓ | Short Covering | **Accumulation** (bullish, can be sharp) | Weekly Buy trigger |
| ↓ | ↓ | Long Unwinding | **Weakening** (bearish, often weaker) | Monthly Sell confirmation |

Both rows of each verdict feed a different decision, not just Weekly: **Accumulation**
(either bullish quadrant) is the trigger for that side's **Weekly Buy** (§1.3).
**Weakening** (either bearish quadrant) is the trigger for that side's **Monthly Sell's
own confirmation** (§1.4) — sell into de-accumulation/unwinding on that specific
option, not into strength. CE's own table result never depends on PE's numbers, and
vice versa, in either use.

### 1.3 Weekly Buy Engine — two independent legs (CE and PE can both be open)

- CE accumulation (per §1.2, using OTM1 CE's own premium+OI) → **Buy Weekly OTM1 CE**
- PE accumulation (per §1.2, using OTM1 PE's own premium+OI) → **Buy Weekly OTM1 PE**
- These are independent triggers — both can fire and both legs can be open
  concurrently (a CE leg and a PE leg), each with its own Strike Freeze and its own
  exit tracking. Re-entry on the same side while that side's leg is already open is
  a no-op (see §3's PositionManager note) — signal evaluation and logging continue,
  but no duplicate order is placed.
- Weekly exit (per leg, independently): 2 consecutive opposite-direction signals on
  that side's own strike, OR 15:15, OR **+100% profit on that leg** (then
  immediately re-select a fresh OTM1 strike for that side off current spot and
  re-enter if that side's signal still holds).

### 1.4 Monthly Sell Engine — two independent legs (CE and PE can both be open), gated same-side only

- **Monthly CE Sell** requires, both true, **CE-only**:
  1. Weekly's own CE signal shows **Weakening** (bearish quadrants: Short Build-up or Long Unwinding, §1.2) — confirmation #1, same side, never PE
  2. Monthly CE's own premium+OI (§1.2, applied to the selected Monthly CE strike) also shows **Weakening** (bearish quadrants) — confirmation #2
  → Sell Selected Monthly CE
- **Monthly PE Sell** requires, both true, **PE-only**:
  1. Weekly's own PE signal shows **Weakening** (bearish quadrants, §1.2) — confirmation #1, same side, never CE
  2. Monthly PE's own premium+OI (§1.2, applied to the selected Monthly PE strike) also shows **Weakening** (bearish quadrants) — confirmation #2
  → Sell Selected Monthly PE

Both confirmations use the **same** bearish/weakening criterion — no mixed
bullish/bearish combination. Note this means **Weekly's own OI+premium signal now
drives two different outcomes depending on which quadrant it lands in**: Accumulation
(bullish quadrants) on a side triggers that side's own **Weekly Buy** (§1.3);
Weakening (bearish quadrants) on the *same* side instead feeds that side's own
**Monthly Sell gate** (confirmation #1 above). A given side is therefore never
simultaneously eligible for both a fresh Weekly buy and a fresh Monthly sell gate —
its own signal state is in exactly one quadrant-group at a time.
- PE data never enters the CE decision at any point (gate or confirmation), and vice
  versa. Both Monthly legs can be open concurrently if both Weekly sides are active.
- Expiry: current month if >20 days to expiry, else next month (per-side, evaluated independently, will normally agree since both use the same date logic)
- Strike: nearest 100-pt strike where |Delta| is 0.20–0.25 (via `optiongreeks`), selected independently per side
- Exit (per leg, independently): **2 consecutive opposite-direction signals on that
  side's own Monthly strike only** — no profit target, no stop-loss, exactly as
  specified (confirmed decision, §7.2 below)

### 1.5 Strike Freeze (unchanged from the spec, now per-leg)

Once any leg is open (Weekly CE, Weekly PE, Monthly CE, or Monthly PE — up to 4
concurrent legs total), that leg's executed strike is locked: every subsequent
candle re-evaluates §1.2 on *that exact strike* only, never re-picks off current
spot or delta. A new strike for that side is only chosen after that specific leg
closes — the other 3 legs (if open) are entirely unaffected.

---

## 2. Data-source mapping (verified against this codebase, not assumed)

| Spec need | OpenAlgo call | Verified detail |
|---|---|---|
| 5-min candle close, OHLC | `client.history(symbol, exchange, interval="5m", start_date, end_date)` | `docs/api/market-data/history.md` confirms `5m` is a documented interval; `strategies/deployed/*` already use this pattern for 3m candles |
| Current OI per candle | same `history()` call | **Verified at code level**, not just docs: `services/history_service.py:134-138` force-adds an `oi` column (0 if broker omits it) before `df.to_dict(orient="records")` — the public doc page just doesn't mention the field, but every `/history` response carries it. Fyers' own adapter (`broker/fyers/api/data.py:483-547`) populates real OI per candle for derivative exchanges (NFO here) |
| Reference OI at **previous day's close** | same `history()` call, requesting yesterday's session, take last candle's `oi` | Depends on the row above being real (confirmed for Fyers/NFO) |
| Reference OI at **today 09:35** | same `history()` call once that candle has closed, OR live capture via `client.depth()`/`client.multiquotes()` at that instant | Either works; `history()` is simpler (one code path for both reference cases) |
| Monthly strike delta (0.20–0.25) | `client.optiongreeks(symbol=, exchange="NFO")` | `openalgo/options.py:83`, response includes `greeks.delta`; internally fetches spot + computes IV itself — no need to source IV separately. **Uncached, live REST round-trip per call** — call it only when selecting a *new* monthly strike (position flat), never on every 5-min tick |
| Expiry dates (weekly / monthly) | `client.expiry(symbol=, exchange=, instrumenttype="options")` (`services/expiry_service.py:12`) returns all live expiries sorted ascending, **no weekly/monthly flag** — must pick by date-distance logic ourselves (nearest = weekly; the one closest to but not past month-end, or >20-day check per spec, = monthly) | No strategy in this repo currently calls `client.expiry()` — this would be the first; Batman instead takes a pre-known `expiry_date` into `optionchain()`. Need to write+test this selection logic carefully |
| Live LTP for open position (target/exit) & spot (signal strike calc) | WebSocket `subscribe_ltp()` via `PriceStream`, matching Batman/Combined/VWAP/MCX's existing pattern | OI is **not** in the WS feed (`feed.py` quote/LTP/depth parsing never surfaces it) — OI is REST-only, polled once per 5-min candle close, never streamed |

**Net effect on the websocket layer:** this strategy's WS footprint is still *lighter or
comparable* to the existing 4 even with up to 4 concurrent legs (Weekly CE, Weekly PE,
Monthly CE, Monthly PE) — at most 5 symbols total (spot + 4 legs), similar order of
magnitude to Combined's up to 6-7. It only needs live ticks for (a) NIFTY spot and (b)
whatever strike(s) are currently open, never for OI itself. OI is fetched via REST
`history()` on the 5-minute cadence the spec already specifies, so there's no tension
between "OI needs polling" and "ticks need streaming" — they're cleanly separate concerns
on separate cadences.

---

## 3. Proposed architecture

**One combined deployed script**, not two — recommendation, not yet confirmed (see open questions):

- The spec's own "Recommended Architecture" section describes 4 *modules* (Reference Engine, Weekly OI Engine, Monthly OI Engine, Position Manager), not 4 processes.
- Monthly Sell needs the Weekly engine's **live, current-cycle signal** as a hard gate ("a monthly trade can only be initiated when the corresponding Weekly OI signal is active"). In one process this is just a shared in-memory value; across two processes it would need either a shared state file polled by both (race-prone: which side is authoritative if they disagree for one tick?) or an IPC mechanism — pure added fragility for no benefit.
- Matches this repo's existing convention: `Nifty_Sensex_Pivot_EMA_Combined_Intraday_1_*.py` already runs two independent engines (Pivot, EMA/RSI) in one script with one combined PnL report, exactly this shape.

Internal module layout (mirroring the spec's own section 13, one file):

```
ReferenceEngine   — gap calc at 09:30, decides Reference Time, persists it for the day
WeeklyCEEngine    — OTM1 CE selection, own-side OI+premium calc (§1.2), buy/exit
WeeklyPEEngine    — OTM1 PE selection, own-side OI+premium calc (§1.2), buy/exit --
                    fully independent of WeeklyCEEngine, same logic mirrored on PE
MonthlyCEEngine   — gated by WeeklyCEEngine's own current signal (same side only),
                    delta strike selection, own-side OI+premium confirmation, sell/exit
MonthlyPEEngine   — gated by WeeklyPEEngine's own current signal (same side only),
                    fully independent of MonthlyCEEngine
PositionManager   — strike-freeze bookkeeping (shared dataclass per open leg: expiry,
                    strike, type, reference OI/premium, entry time), one slot each for
                    Weekly-CE / Weekly-PE / Monthly-CE / Monthly-PE -- up to 4 concurrent
                    legs, each independently frozen and independently exited (§1.5).
                    **No re-entry on the same side while that side's leg is already
                    open**: e.g. if Weekly CE is long and a fresh CE-accumulation signal
                    recurs on a later candle, it's logged (needed for the "2 consecutive
                    opposite signal" exit tracking) but does not place a second buy —
                    only an opposite-direction signal on CE's own strike, the profit
                    target, or 15:15 can end that CE leg. This guard applies
                    independently per leg-slot, so a Weekly PE entry is never blocked
                    by an open Weekly CE (and vice versa) — only same-side re-entry is
                    suppressed.
PriceStream       — copied verbatim from an already-hardened script (Batman or MCX,
                    whichever is most recently patched at build time), see §3.1
StateStore        — reused verbatim from Batman's JSON dataclass-tree pattern
```

### 3.1 WebSocket reliability — copy the hardened implementation, do not reimplement

This session spent multiple days finding and fixing real production bugs in exactly this
layer: Fyers HSM batch-mixing, a dead `unsubscribe_symbols()`, the proxy's
redundant-subscribe-skip masking a stuck symbol, a pointless `unsubscribe_ltp()` before
every retry `subscribe_ltp()`, Shoonya's own independent `already_ws_subscribed` staleness
bug, multi-strategy startup collisions, and — most recently — Fyers' `/data/symbol-token`
API echoing back a different symbol string than it was given. Every one of those fixes now
lives in shared, already-tested code (`websocket_proxy/server.py`,
`broker/fyers/streaming/*`, `broker/shoonya/streaming/*`) or in the `PriceStream` class
pattern used by all 4 currently-deployed scripts. This new strategy inherits every one of
those fixes automatically **provided it doesn't route around them**:

- **Copy `PriceStream` (and its watchdog) byte-for-byte** from an existing deployed script
  rather than writing a new implementation. A hand-rolled version risks silently
  reintroducing any of the bugs above (e.g. calling `unsubscribe_ltp()` before a stale
  retry's `subscribe_ltp()` — the exact bug fixed 2026-07-30). Only the instrument list and
  callback wiring should differ from the source script; the reconnect/backoff/staleness
  logic itself must not be touched.
- **Never batch multiple symbols into one `subscribe_ltp()`/`subscribe_quote()` call from
  strategy code.** The proxy-side fix already guards against cross-*process* batch-mixing,
  but a strategy that itself requests several symbols in one call still asks the SDK to
  bundle them — call once per symbol, matching every other deployed script's convention.
- **Bounded WS footprint by design**: this strategy only ever needs live ticks for NIFTY
  spot plus whichever leg(s) are currently open — at most 5 symbols concurrently (spot +
  Weekly CE + Weekly PE + Monthly CE + Monthly PE), comparable to Combined's up to 6-7.
  The bound is fixed regardless of how many legs are open, so it's still a small, known
  surface area rather than an open-ended one.
- **Stagger this strategy's own scheduled start/watchdog cycle** against the other 4 (soon
  5) running strategies, reusing the existing `_stagger_offset_seconds()` mechanism
  (sorted-strategy-id-based offset already applied to `CronTrigger`/interval scheduling) —
  this strategy's subscribe burst must not land in the same window as another strategy's
  resubscribe, which is exactly the condition that caused the original HSM batch-mixing
  symptom.
- **OI fetches (`history()`) are a completely separate REST path from the WS layer** — a
  failed or slow OI fetch on one 5-min candle must never touch `PriceStream`'s connection
  state, and a WS reconnect must never block or skip an OI evaluation. Keeping these two
  concerns structurally separate (per §2's data-source mapping) is itself a reliability
  property: a bug in one cannot cascade into "multiple drops and resubscriptions" in the
  other.
- **Validation before going live**: after deployment, run the same production log checks
  used to validate this session's fixes —
  `journalctl -u openalgo.service | grep -E "not in mappings|stale/missing ticks|connection down"`
  scoped to this strategy's symbols — through at least one full session covering an entry,
  an open-position hold period, and an exit, before trusting it unattended.

---

## 4. Order management & exception handling (reusing existing conventions, not inventing new ones)

- `place()` helper identical in shape to Batman/Combined/MCX: wraps `client.placeorder(...)`, retries **only** on a clean broker rejection (`status != "success"` with no exception raised), **never** retries an ambiguous exception (network timeout mid-request) — matches the explicit design already documented and reasoned about in this codebase (duplicate-order risk).
- Sandbox-first: deploy and validate against the Sandbox engine before flipping to live broker, exactly as CLAUDE.md's platform-wide convention states (1 Crore sandbox capital, exchange-aligned square-off) — this strategy sizes its own capital far below that, so sandbox validation costs nothing.
- `logger.exception()` (never bare `except: pass`, never `traceback.print_exc()`) at every broker-call boundary — `history()`, `optiongreeks()`, `expiry()`, `placeorder()` — each independently retriable/loggable, since a single failed OI fetch on one candle must not crash the whole 5-min loop (log, skip this candle's signal evaluation, retry next candle).
- Rate-limit awareness: `history()` calls are subject to `_enforce_rate_limit()` (3 req/s) in `services/history_service.py` — with 2 engines each needing CE+PE OI on their own strike every 5 minutes, that's ≤4 calls per 5-minute window, far under the limit even accounting for occasional expiry/reference-day lookups.

## 5. PnL reporting (matching existing convention exactly)

Reuse `report_pnl_to_platform()` verbatim (as seen in the Combined strategy): one combined realized+unrealized PnL snapshot posted to `/python/api/strategy/{tag}/pnl` per tick, `open_positions` list carrying per-leg detail so Weekly vs Monthly PnL stays recoverable from the trade log's `leg` column without a second reporting path.

Per this session's earlier convention (added to Combined/MCX/VWAP/Batman): every entry log line must print the **condition values that fired it** — for this strategy that means logging Reference Time, Reference OI, Current OI, and OI-change% for both CE and PE at the moment of entry, not just "Bullish signal, bought X".

## 6. Optimization notes

- `optiongreeks()` is a live, uncached REST call (confirmed — no caching/rate-limit note anywhere in its docstring or docs). Call it **only** when the Monthly engine needs a *new* strike (i.e., flat and about to enter) — never poll it every 5-min tick just to "check" delta on an already-frozen strike, since the spec's own Strike Freeze rule (§11) says the frozen strike's *OI*, not its delta, drives continuation/exit.
- `expiry()` similarly only needs calling once per day (or once per new-position event) to determine current weekly/monthly expiry — cache the result in the daily reference state rather than re-querying every candle.
- 5-min candle cadence naturally throttles everything else — no separate optimization needed beyond what the spec already specifies.

---

## 7. Decisions (resolved 2026-08-02)

1. **Deployment shape: one combined script.** Confirmed.
2. **Monthly Sell risk: exactly as specified, no added SL/target.** Confirmed — the OI-flip exit (§12 of the spec) is the only exit, deliberately, per the source expert's design. Not adding a safety net.
3. **Weekly Buy: no stop-loss beyond the spec's own 100%-profit / 2-opposite-signal / 15:15 exits.** Same reasoning as #2 — build exactly as specified.
4. **Underlying: NIFTY only.** Matches the backtest's strike range (23000–26000).
5. **Quantity: 65 per leg (both Weekly Buy and Monthly Sell).** NIFTY's current lot size is 65 (`docs/prompt/LotSize.md`) — so the backtest's "qty 65" already **is** exactly 1 lot; both proposed options converged on this number, no ambiguity to resolve.
6. **Capital tags: ₹50,000 (Buy) / ₹2,50,000 (Sell)** — matches the backtest, for direct comparison against the numbers already reported.
7. **GAP_THRESHOLD: 0.5%** — spec default, exposed as a config constant (not hardcoded inline) so it can be tuned later without a redeploy-from-scratch.
8. **Weekly OTM1 strike-step: use NIFTY's actual listed strike ladder** (via option-chain / `SymToken` data), not an assumed fixed increment — the spec only states a fixed 100-pt rounding for the *Monthly* strike (§9), not Weekly OTM1 (§1), so Weekly picks the next real listed strike beyond ATM in either direction.
9. **Product type: MIS** (intraday) — matches the strategy's own "Intraday" naming and every other deployed script in this repo.
10. **No re-entry on the same side while that side's leg is already open** — evaluated per leg-slot (Weekly-CE, Weekly-PE, Monthly-CE, Monthly-PE independently), not per engine. Signal evaluation and logging still run every candle — needed for the "2 consecutive opposite signal" exit tracking — but a repeat same-direction signal while already in that leg is a no-op on the order side. Only an opposite signal on that leg's own strike, the profit target (Weekly), or the 15:15 cutoff (Weekly) closes it; a fresh entry on that side is only evaluated once that specific leg is flat. See §3's `PositionManager` note for the exact mechanics.
11. **Signal logic corrected to fully side-independent (CE-only / PE-only, never cross-referenced), superseding the source .txt's cross-referenced version — see §1.** This applies at every layer: Weekly signal generation, the Weekly→Monthly gate, and Monthly's own OI confirmation. Consequence: **up to 4 concurrent legs are now possible** (Weekly CE, Weekly PE, Monthly CE, Monthly PE simultaneously), not the 2-leg-max (one Weekly + one Monthly) the original spec text implied — Position Manager, WS footprint estimates, and PnL reporting throughout this plan are written against the corrected 4-leg model.
12. **Monthly Sell requires BOTH confirmations to show Weakening (bearish quadrants) — not a mixed bullish-gate/bearish-confirmation combination.** Corrected twice in this thread: first to "Monthly's own check uses Weakening" (§1.4 step 2), then further to "the Weekly-side gate (§1.4 step 1) also uses Weakening, not Accumulation." Final rule: for Monthly CE Sell, Weekly CE's own signal must show Weakening AND Monthly CE's own signal must show Weakening (mirrored exactly for PE). Weekly's own OI+premium signal thus drives two different outcomes depending on which quadrant-group it's in on a given side: Accumulation → that side's Weekly Buy; Weakening → that side's Monthly Sell gate — never both at once for the same side.

---

## 8. Next step

All open questions are resolved — §2–§7 is now the direct basis for the deployed script. Every data dependency has already been verified against actual SDK/service code in this session (not assumed from docs alone), so implementation can proceed directly from this plan without further research.
