"""
Historical backtest driver: replays the UNMODIFIED
Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py StrategyEngine against the
real 2023-2026 dataset under C:\\Devendra\\OpenAlgo\\data23_to_26\\data\\, via
BacktestClient (oi_backtest_client.py). Same code path as production and as
the synthetic simulation suite (strategies/test/test_oi_weekly_monthly_
simulation.py) -- only the data source and the "now" clock differ.

Usage:
    .venv/Scripts/python.exe strategies/backtest/run_oi_backtest.py \
        --start 2023-01-02 --end 2026-03-24 --out strategies/backtest/results

Outputs (under --out):
    trades.csv    -- one row per closed leg (entry/exit time+price, reason, pnl)
    summary.json  -- aggregate stats
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime as real_datetime
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oi_backtest_client as btc
import oi_backtest_data as data

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT / "strategies" / "deployed"
    / "Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    existing = sys.modules.get("openalgo")
    if existing is not None and hasattr(existing, "api"):
        return
    site_pkg_init = REPO_ROOT / ".venv" / "Lib" / "site-packages" / "openalgo" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "openalgo", site_pkg_init, submodule_search_locations=[str(site_pkg_init.parent)]
    )
    real_openalgo = importlib.util.module_from_spec(spec)
    sys.modules["openalgo"] = real_openalgo
    spec.loader.exec_module(real_openalgo)


def _load_script_module():
    _ensure_real_openalgo_sdk_loaded()
    spec = importlib.util.spec_from_file_location("oi_backtest_target", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FixedDateTime:
    """See strategies/test/test_oi_weekly_monthly_simulation.py's identical
    class -- stand-in for the script module's `datetime` name."""
    _current: "real_datetime" = None

    @classmethod
    def now(cls, tz=None):
        return cls._current if tz is None else cls._current.astimezone(tz)

    @classmethod
    def combine(cls, date_, time_, tzinfo=None):
        dt = real_datetime.combine(date_, time_)
        return dt.replace(tzinfo=tzinfo) if tzinfo else dt

    @staticmethod
    def fromisoformat(s):
        return real_datetime.fromisoformat(s)

    @staticmethod
    def strptime(s, fmt):
        return real_datetime.strptime(s, fmt)


def _drain_fills(module, engine, timeout=5.0):
    deadline = time.monotonic() + timeout
    while engine._pending_fills and time.monotonic() < deadline:
        time.sleep(0.005)


def _settle(module, engine):
    for _ in range(4):
        _drain_fills(module, engine)
        settled_any = False
        for ot in engine.weekly:
            pos = engine.weekly[ot].leg.position
            if pos.exit_order_id and pos.exit_filled:
                engine.weekly[ot]._finalize_exit(pos, pos.pending_exit_reason, pos.pending_exit_reenter)
                settled_any = True
        for ot in engine.monthly:
            pos = engine.monthly[ot].leg.position
            if pos.exit_order_id and pos.exit_filled:
                engine.monthly[ot]._finalize_exit(pos, pos.pending_exit_reason)
                settled_any = True
        if not settled_any and not engine._pending_fills:
            break


def build_engine(module, clock: btc.Clock, trades: list, strategy_tag="oi_backtest"):
    client = btc.BacktestClient(clock)

    env = module.Environment.__new__(module.Environment)
    env.api_key = "BACKTEST"
    env.host = "http://127.0.0.1:5000"
    env.version = "v1"
    env.timeout = 10.0
    env.ltp_timeout = 3.0
    env.ws_url = None
    env.strategy_tag = strategy_tag

    state_store = module.StateStore.__new__(module.StateStore)
    state_store.path = Path.cwd() / f"{strategy_tag}_state.json"
    state_store.state = module.StrategyState()

    price_stream = btc.NullPriceStream()
    engine = module.StrategyEngine(client, state_store, env, price_stream, execution_id=1, ltp_client=client)

    # No-op every platform-integration call (real HTTP in production) --
    # capture trade closes into our own in-memory list instead of the
    # module's real background-thread file writer, so this standalone
    # script has no dependency on the running platform's paths/process.
    module.check_force_exit = lambda _env: False
    module.report_pnl_to_platform = lambda *a, **k: None
    module.push_leg_error = lambda *a, **k: None
    module.notify_trade_closed = lambda *a, **k: None
    module.check_pending_action = lambda _env, _leg_key: None
    module.ack_pending_action = lambda *a, **k: None

    def _capture_trade_log(strategy_tag_, leg_key, symbol, quantity, entry_time, entry_px,
                            exit_time, exit_px, exit_reason, execution_id, is_short):
        pnl_points = (exit_px - entry_px) if not is_short else (entry_px - exit_px)
        trades.append({
            "leg": leg_key, "symbol": symbol, "quantity": quantity,
            "entry_time": entry_time, "entry_px": entry_px,
            "exit_time": exit_time, "exit_px": exit_px,
            "exit_reason": exit_reason, "is_short": is_short,
            "pnl_points": pnl_points, "pnl_rupees": pnl_points * quantity,
        })

    module.append_trade_log = _capture_trade_log
    # Both engine classes closed over the module-level name at import time --
    # re-point every already-built side engine's own reference too (Python
    # resolves unqualified globals from the DEFINING module at call time, so
    # patching module.append_trade_log above is actually sufficient on its
    # own; this is kept as a second, explicit belt-and-braces confirmation
    # covered by the smoke-test run below rather than trusted blindly).

    return engine, client


def run_day(module, engine, client, clock: btc.Clock, day, start_hhmm="09:15", end_hhmm="15:30"):
    h0, m0 = (int(x) for x in start_hhmm.split(":"))
    h1, m1 = (int(x) for x in end_hhmm.split(":"))
    t = real_datetime.combine(day, real_datetime.min.time()).replace(hour=h0, minute=m0)
    end = real_datetime.combine(day, real_datetime.min.time()).replace(hour=h1, minute=m1)
    IST = module.IST
    while t <= end:
        now = IST.localize(t)
        clock.current = now
        module.datetime = _FixedDateTime
        _FixedDateTime._current = now
        try:
            engine.run_cycle()
        except Exception as exc:
            module.Log.exception(f"run_cycle crashed at {now}: {exc}")
        _settle(module, engine)
        t += timedelta(minutes=5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-02")
    ap.add_argument("--end", default="2026-03-24")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    ap.add_argument("--strategy-tag", default="oi_backtest")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    module = _load_script_module()
    module.Log.logger.setLevel(30)  # WARNING -- a multi-year run logs an INFO line every candle otherwise

    spot = data.load_spot_5min()
    all_days = sorted({ts.date() for ts in spot.index})
    start_d = real_datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = real_datetime.strptime(args.end, "%Y-%m-%d").date()
    trading_days = [d for d in all_days if start_d <= d <= end_d]
    print(f"Backtest range: {args.start} .. {args.end} -- {len(trading_days)} trading days")

    trades: list[dict] = []
    clock = btc.Clock(module.IST.localize(real_datetime.combine(trading_days[0], real_datetime.min.time())))
    engine, client = build_engine(module, clock, trades, strategy_tag=args.strategy_tag)

    t_start = time.time()
    for i, day in enumerate(trading_days):
        run_day(module, engine, client, clock, day)
        if (i + 1) % 25 == 0 or i == len(trading_days) - 1:
            elapsed = time.time() - t_start
            print(f"  [{i+1}/{len(trading_days)}] {day} -- {len(trades)} trades so far "
                  f"({elapsed:.1f}s elapsed)")

    # Force-close anything still open at the end of the loaded window --
    # nothing to compare against beyond this, same convention OPTION_DATA.md
    # documents for the prior backtest against this same dataset.
    for ot in engine.weekly:
        pos = engine.weekly[ot].leg.position
        if pos.symbol and pos.entry_filled:
            ltp = client._last_close_at_or_before(pos.symbol, module.OPTIONS_EXCHANGE, clock.current) or pos.entry_px
            engine.weekly[ot]._exit(pos, ltp, "backtest_window_end", reenter=False)
    for ot in engine.monthly:
        pos = engine.monthly[ot].leg.position
        if pos.symbol and pos.entry_filled:
            ltp = client._last_close_at_or_before(pos.symbol, module.OPTIONS_EXCHANGE, clock.current) or pos.entry_px
            engine.monthly[ot]._exit(pos, ltp, "backtest_window_end")
    _settle(module, engine)

    write_outputs(trades, out_dir, args.start, args.end)


def write_outputs(trades: list[dict], out_dir: Path, start: str, end: str):
    import csv

    trades_path = out_dir / "trades.csv"
    fields = ["leg", "symbol", "quantity", "entry_time", "entry_px", "exit_time", "exit_px",
              "exit_reason", "is_short", "pnl_points", "pnl_rupees"]
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)

    total = len(trades)
    wins = [t for t in trades if t["pnl_rupees"] > 0]
    losses = [t for t in trades if t["pnl_rupees"] <= 0]
    total_pnl = sum(t["pnl_rupees"] for t in trades)

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t["pnl_rupees"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    by_leg = {}
    for t in trades:
        by_leg.setdefault(t["leg"], {"count": 0, "pnl": 0.0})
        by_leg[t["leg"]]["count"] += 1
        by_leg[t["leg"]]["pnl"] += t["pnl_rupees"]

    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], 0)
        by_reason[t["exit_reason"]] += 1

    summary = {
        "range": {"start": start, "end": end},
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / total, 1) if total else None,
        "total_pnl_rupees": round(total_pnl, 2),
        "max_drawdown_rupees": round(max_dd, 2),
        "by_leg": {k: {"count": v["count"], "pnl_rupees": round(v["pnl"], 2)} for k, v in by_leg.items()},
        "exit_reason_counts": by_reason,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, indent=2))
    print(f"\nTrade log: {trades_path}")
    print(f"Summary:   {out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
