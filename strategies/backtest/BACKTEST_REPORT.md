# OI Weekly-Buy / Monthly-Sell — Historical Backtest Report

**Run date:** 2026-08-02
**Data:** Real 2023-2026 NIFTY options + spot dataset (`C:\Devendra\OpenAlgo\data23_to_26\`)
**Range:** 2023-01-02 → 2026-03-24 (800 trading days, first trade 2023-01-03, last 2026-03-23)
**Engine:** The unmodified `StrategyEngine`/`WeeklySideEngine`/`MonthlySideEngine` classes from
`strategies/deployed/Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py`, replayed against
historical data via `strategies/backtest/` (`oi_backtest_data.py`, `oi_backtest_client.py`,
`run_oi_backtest.py`). Same code path as production — only the data source and the clock differ.
**Runtime:** 22m 9s.

## Read this first: Monthly Sell's results are not trustworthy in this run

**Monthly Sell only produced 6 trades in 3+ years (1 CE, 5 PE) against 224 logged
`"strike selection failed"` warnings.** This is a limitation of this backtest's delta
approximation, not a finding about the live strategy.

This dataset has no IV/greeks — only OHLCV+OI — so `optiongreeks()` here is approximated with a
**linear moneyness heuristic** (documented in `oi_backtest_client.py`'s module docstring):
expected move = trailing 60-session realized spot volatility × √DTE; delta falls off linearly
from 0.5 at-the-money to 0 at one full expected-move away. This was the explicit tradeoff you
chose over assuming a flat IV for Black-Scholes.

The problem: NIFTY monthly strikes are listed in 100-point increments, and this linear model's
delta *decrement per 100-point step* turned out to be far steeper than a real option's delta
curve near the 0.20–0.25 band (roughly 6-8 percentage points of delta lost per single 100-point
step, against a target band only 5 points wide) — so the outward scan usually jumps clean over
the acceptable window between one candidate strike and the next, rather than landing inside it.
**Monthly Sell's own signal logic (both Weakening confirmations, same-side gating) was never
meaningfully exercised by this run** — it almost never got past strike selection to test the
condition at all. Weekly Buy needs no delta/IV and is unaffected by this — its results below are
a direct, trustworthy mechanics replay.

If you want a real read on Monthly Sell's historical performance, the fix is to widen the
heuristic's acceptance behavior (e.g. interpolate between adjacent 100-point candidates instead
of requiring the scan to land exactly inside the band) — flagging as a follow-up, not done here.

## Overall summary (all 4 legs, as reported by run_oi_backtest.py)

| Metric | Value |
|---|---|
| Total trades | 2,190 |
| Wins / Losses | 471 / 1,719 |
| Win rate | 21.5% |
| Total PnL | **-Rs 38,711** |
| Max drawdown | -Rs 1,22,161 |

| Leg | Trades | Win rate | Total PnL |
|---|---|---|---|
| WEEKLY_CE | 1,085 | 21.6% | **-Rs 52,140** |
| WEEKLY_PE | 1,099 | 21.4% | +Rs 1,089 |
| MONTHLY_CE | 1 | 100%* | +Rs 11,872 |
| MONTHLY_PE | 5 | 20.0%* | +Rs 468 |

\* Sample size far too small to mean anything — see caveat above.

**Practical read: this backtest is really a Weekly-Buy-only test** (2,184 of 2,190 trades). Its
overall PnL (-Rs 38,711) is driven almost entirely by Weekly CE/PE, since Monthly barely traded.

## Weekly Buy detail (the trustworthy part of this run)

| | WEEKLY_CE | WEEKLY_PE |
|---|---|---|
| Trades | 1,085 | 1,099 |
| Win rate | 21.6% | 21.4% |
| Avg win | +Rs 3,292 | +Rs 3,699 |
| Avg loss | -Rs 966 | -Rs 1,005 |
| Total PnL | -Rs 52,140 | +Rs 1,089 |

CE and PE have essentially identical win rates and per-trade averages, but wildly different
total PnL — CE lost heavily, PE was roughly flat. This is not a signal-logic asymmetry (CE and
PE run the identical code, parameterized only by side) — it's a tail-outcome difference: a
handful of large losses concentrated on the CE side account for most of the gap. Worth pulling
up in `trades.csv` if you want to look closer.

**Exit reason breakdown (Weekly only):** `opposite_signal` dominates (the 2-consecutive-candle
exit fires far more often than either the profit target or 15:15 universal exit) — median holding
time 40 minutes, mean 96 minutes (pulled up by the occasional multi-hour profit-target run).

**Yearly PnL (Weekly only):**

| Year | Trades | PnL |
|---|---|---|
| 2023 | 641 | -Rs 21,850 |
| 2024 | 707 | -Rs 5,656 |
| 2025 | 661 | -Rs 41,684 |
| 2026 (Jan-Mar only) | 175 | +Rs 18,138 |

2025 was the worst year by a wide margin. 2026 is a partial year (only through March 24) so its
positive number isn't a full-year read.

**Biggest single loss:** WEEKLY_PE, `NIFTY06JUN2421850PE`, -Rs 11,616, exited 2024-06-04 15:15
(universal exit time) — this is **2024 Lok Sabha election result day**, when NIFTY had its
largest single-session move in years (a sharp crash followed by a partial recovery). The
second-largest loss (-Rs 9,942) is a different strike on the *same day, same underlying move* —
a real, known market event distorting that day's numbers for any short-DTE options strategy, not
a strategy bug.

**Biggest single win:** WEEKLY_PE, `NIFTY06JUN2422550PE`, +Rs 31,385, same election day — the
strategy's own strike-freeze/OI-driven exit caught the other side of that exact move.

## Known limitations of this specific backtest (read before trusting any number above)

1. **Monthly Sell's delta approximation** — covered above, the dominant caveat.
2. **No slippage/spread modeled.** Every fill uses the 1-minute bar's `Close`, resampled to
   5-min — same convention the prior backtest against this dataset (`OPTION_DATA.md`) already
   documented for this data.
3. **No real market-holiday calendar.** `resolve_previous_trading_day()` in the live script only
   skips weekends (a documented limitation of the live script itself, not backtest-specific) —
   a trading day immediately after a real NSE holiday may silently fail to compute a reference
   and skip trading that day entirely. Not quantified here.
4. **`Open Interest` = 0 for the very first few minutes some contracts trade** (visible in the raw
   CSVs) — `classify_oi_premium` correctly reads this as `oi_change == 0` → `"flat"` (no signal),
   so this doesn't produce a false trade, just occasionally suppresses a real one right after a
   contract's reference point.
5. **This backtest silences INFO-level strategy logs** (only WARNING+) for a runtime reason (a
   full 3-year run at full logging would produce an enormous log file) — so entry/exit condition
   detail isn't in `results_full_run.log` the way it would be for a live/sandbox run. `trades.csv`
   has entry/exit price, time, symbol, and reason for every trade; re-run a short window without
   the log-level suppression (see `run_oi_backtest.py`'s `main()`) if you want full condition
   detail for a specific window.
6. **A position still open at the very end of the loaded window is force-closed at the last
   available price** (`exit_reason: backtest_window_end`) — only 2 trades hit this, negligible.

## Files produced

- `strategies/backtest/results_full/trades.csv` — all 2,190 trades, one row each.
- `strategies/backtest/results_full/summary.json` — the aggregate numbers above, machine-readable.
- `strategies/backtest/results_full_run.log` — full run log (WARNING+ only, ~512KB).
- `strategies/backtest/oi_backtest_data.py` / `oi_backtest_client.py` / `run_oi_backtest.py` — the
  backtest harness itself, reusable for a different date range or after fixing the Monthly delta
  heuristic (`--start`/`--end`/`--out`/`--strategy-tag` CLI args).

## Bottom line

- **Weekly Buy** (the part of this backtest actually worth trusting): net loss of ~Rs 51,000
  over 3+ years on a 21.5% win rate, concentrated in 2023 and especially 2025, with a
  large-loss/large-win pair on 2024's election-result day dominating the tails. This does not by
  itself mean the live strategy is broken — it's one parameter set (fixed 100% profit target, no
  IV/regime filter) over one specific 3-year window that includes a genuine tail event — but it's
  a real, honest number from real data, and worth sitting with before sizing this up live.
- **Monthly Sell**: no usable read from this run — the delta heuristic needs fixing first.
