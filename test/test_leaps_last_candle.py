"""Both LEAPS strategies: the previous day's last candle must be the real one.

Shoonya folds the next morning's 09:14 pre-open print into the previous day's
last intraday candle (docs/CUSTOMIZATIONS.md, 2026-11-05). Live on 05-Nov-2026
both strategies logged the 04-Nov 15:15 candle with close=24378.05 -- that
morning's pre-open -- where the real close was 23960.60, and RSI 84.29 where
the real value is 76.83.

Imports each real deployed script, stubs only the broker client and the clock,
and drives the real _restore_last_candles and the real signal functions.

Run: .venv/Scripts/python.exe -I test/test_leaps_last_candle.py
"""
import datetime as _dt
import importlib.util
import logging
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

DEPLOYED = Path(r"C:\Devendra\OpenAlgo\openalgo\strategies\deployed")
SCRIPTS = {
    "RSI": DEPLOYED / "Nifty_LEAPS_OptionsSell_1_20260823190000.py",
    "HAMACD": DEPLOYED / "Nifty_LEAPS_HAMACD_2h_1.py",
}


def load(path, name):
    spc = types.ModuleType("_strategy_platform_client")
    spc.notify_trade_closed = lambda *a, **k: None
    spc.notify_telegram_error = lambda *a, **k: None
    spc.filter_known_fields = lambda cls, raw: dict(raw or {})
    sys.modules["_strategy_platform_client"] = spc
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self, m, when):
        self.m = m

        class F(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return m.IST.localize(when) if tz is not None else when
        self.f = F

    def __enter__(self):
        self.orig = self.m.datetime
        self.m.datetime = self.f

    def __exit__(self, *a):
        self.m.datetime = self.orig


def ist(m, s):
    return m.IST.localize(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M"))


def frame(m, rows):
    idx = pd.DatetimeIndex([ist(m, t) for t, *_ in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["open", "high", "low", "close"])


def live_shape(m, last_label="2026-11-04 15:15"):
    """The real 05-Nov-2026 shape: 04-Nov's last candle altered, 1m correct."""
    bars = frame(m, [
        ("2026-11-04 14:15", 23930.0, 23945.0, 23890.0, 23921.70),
        (last_label, 23911.70, 24378.05, 23911.70, 24378.05),       # as the broker serves it
        ("2026-11-05 09:15", 24376.55, 24442.20, 24296.30, 24366.70),
    ])
    one = frame(m, [
        ("2026-11-04 15:15", 23911.70, 23911.70, 23911.70, 23911.70),
        ("2026-11-04 15:20", 23911.70, 23915.00, 23905.00, 23911.70),
        ("2026-11-04 15:29", 23960.60, 23960.60, 23960.60, 23960.60),
        ("2026-11-05 09:14", 24378.05, 24378.05, 24378.05, 24378.05),  # pre-open, dated the new day
        ("2026-11-05 09:15", 24376.55, 24391.95, 24296.30, 24340.35),
    ])
    return bars, one


class Client:
    def __init__(self, by_interval, fail_1m=False):
        self.by_interval, self.fail_1m, self.calls = by_interval, fail_1m, []

    def history(self, **k):
        self.calls.append((k["interval"], k["start_date"], k["end_date"]))
        if k["interval"] == "1m" and self.fail_1m:
            raise ConnectionError("1m down")
        return self.by_interval[k["interval"]]


RESULTS = []


def case(name):
    def deco(fn):
        def run(*a):
            try:
                fn(*a)
                RESULTS.append((True, name, ""))
            except AssertionError as exc:
                RESULTS.append((False, name, str(exc)))
            except Exception as exc:
                RESULTS.append((False, name, f"{type(exc).__name__}: {exc}"))
        return run
    return deco


def run_for(label, m):
    when = _dt.datetime(2026, 11, 5, 9, 17)

    @case(f"[{label}] 04-Nov 15:15 is rebuilt from 04-Nov's own 1m bars; today untouched")
    def t1():
        m._LAST_CANDLE_FIX.clear()
        bars, one = live_shape(m)
        c = Client({"1m": one})
        before = bars.copy()
        with Clock(m, when):
            out = m._restore_last_candles(c, bars)
        last = out.loc[ist(m, "2026-11-04 15:15")]
        assert (last["open"], last["high"], last["low"], last["close"]) == \
            (23911.70, 23960.60, 23905.00, 23960.60), last.to_dict()
        assert out.loc[ist(m, "2026-11-05 09:15")].to_dict() == \
            before.loc[ist(m, "2026-11-05 09:15")].to_dict(), "today's candle must not change"
        assert out.loc[ist(m, "2026-11-04 14:15")].to_dict() == \
            before.loc[ist(m, "2026-11-04 14:15")].to_dict(), "only a day's LAST candle changes"
        assert bars.equals(before), "the caller's frame must not be mutated"
        assert c.calls == [("1m", "2026-11-04", "2026-11-04")], c.calls

    @case(f"[{label}] a completed day is fetched once, then served from the cache")
    def t2():
        m._LAST_CANDLE_FIX.clear()
        bars, one = live_shape(m)
        c = Client({"1m": one})
        with Clock(m, when):
            m._restore_last_candles(c, bars)
            out = m._restore_last_candles(c, bars)
        assert len(c.calls) == 1, f"second check must not refetch 1m: {c.calls}"
        assert out.loc[ist(m, "2026-11-04 15:15"), "close"] == 23960.60

    @case(f"[{label}] 1m failure keeps the broker's candles, warns, and retries next time")
    def t3():
        m._LAST_CANDLE_FIX.clear()
        bars, one = live_shape(m)
        lines = []
        h = logging.Handler()
        h.emit = lambda r: lines.append(r.getMessage())
        m.Log.logger.addHandler(h)
        try:
            with Clock(m, when):
                out = m._restore_last_candles(Client({"1m": one}, fail_1m=True), bars)
        finally:
            m.Log.logger.removeHandler(h)
        assert out.equals(bars), "on failure the candles are returned as served"
        assert any("[Candles] 1m history failed" in ln for ln in lines), lines
        assert not m._LAST_CANDLE_FIX, "a failure must not be cached"

    @case(f"[{label}] a candle whose timestamp no 1m bar matches is left as served")
    def t4():
        m._LAST_CANDLE_FIX.clear()
        bars, one = live_shape(m)
        one = one[one.index.strftime("%Y-%m-%d") != "2026-11-04"]   # no 04-Nov 1m bars at all
        with Clock(m, when):
            out = m._restore_last_candles(Client({"1m": one}), bars)
        assert out.loc[ist(m, "2026-11-04 15:15"), "close"] == 24378.05
        assert m._LAST_CANDLE_FIX.get(_dt.date(2026, 11, 4), "absent") is None

    @case(f"[{label}] the real signal function computes on the restored close")
    def t5():
        m._LAST_CANDLE_FIX.clear()
        # 6 days of 1h-style candles (09:15 .. 15:15), each day's last one
        # altered to the next morning's open, with 1m bars giving its real close
        days = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02",
                "2026-11-03", "2026-11-04"]
        times = ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
        rng = np.random.default_rng(7)
        price, rows, one_rows, real = 23500.0, [], [], {}
        for di, d in enumerate(days):
            for t in times:
                o = price
                price = price + rng.normal(0, 40)
                rows.append([f"{d} {t}", o, max(o, price) + 5, min(o, price) - 5, price])
            real[d] = price
            one_rows.append((f"{d} 15:15", rows[-1][1], rows[-1][2], rows[-1][3], price))
            one_rows.append((f"{d} 15:29", price, price, price, price))
            gap_open = price * (1.012 if di % 2 == 0 else 0.99)        # next morning's pre-open
            rows[-1][4] = gap_open                                     # the broker's fold
            rows[-1][2] = max(rows[-1][2], gap_open)
            rows[-1][3] = min(rows[-1][3], gap_open)
            price = gap_open
        rows.append(["2026-11-05 09:15", price, price + 20, price - 20, price + 10])
        rows.append(["2026-11-05 10:15", price + 10, price + 30, price, price + 15])  # forming
        bars = frame(m, [tuple(r) for r in rows])
        one = frame(m, one_rows)
        truth = bars.copy()
        for d in days:
            ts = ist(m, f"{d} 15:15")
            seg = one[one.index.strftime("%Y-%m-%d") == d]
            truth.loc[ts, ["open", "high", "low", "close"]] = [
                seg["open"].iloc[0], seg["high"].max(), seg["low"].min(), seg["close"].iloc[-1]]
        c = Client({"1m": one, m.config.intraday_interval: bars})
        cfg = m.config   # 7 days x 7 bars = 49 closed bars; MACD needs slow + signal + 2 = 37
        with Clock(m, _dt.datetime(2026, 11, 5, 10, 17)):
            if label == "RSI":
                cur, prev, key = m._get_rsi_signal(c)
                closed = truth.iloc[:-1]["close"].to_numpy(float)
                want = np.asarray(m.ta.rsi(closed, cfg.rsi_period))
                assert key.startswith("2026-11-05 09:15"), key
                assert abs(cur - want[-1]) < 1e-9 and abs(prev - want[-2]) < 1e-9, (cur, prev, want[-2:])
                raw = np.asarray(m.ta.rsi(bars.iloc[:-1]["close"].to_numpy(float), cfg.rsi_period))
                assert abs(raw[-2] - want[-2]) > 0.5, "the fixture must actually move the prev RSI"
            else:
                bull, mv, sv, key = m._get_hamacd_signal(c)
                closed = truth.iloc[:-1]
                o, h, lo, cl = (closed[k].to_numpy(float) for k in ("open", "high", "low", "close"))
                _, ha_c = m._heikin_ashi(o, h, lo, cl)
                basis = ha_c if cfg.macd_on_ha_close else cl
                ml, sl, _ = m.ta.macd(basis, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
                assert abs(mv - float(np.asarray(ml)[-1])) < 1e-9, (mv, np.asarray(ml)[-1])
                assert abs(sv - float(np.asarray(sl)[-1])) < 1e-9, (sv, np.asarray(sl)[-1])
        assert [x[0] for x in c.calls].count("1m") == 1, c.calls

    for fn in (t1, t2, t3, t4, t5):
        fn()


def main():
    for label, path in SCRIPTS.items():
        run_for(label, load(path, f"leaps_{label.lower()}"))
    print("\n" + "=" * 74)
    ok = sum(1 for p, _, _ in RESULTS if p)
    for passed, name, err in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        if not passed:
            print(f"        -> {err}")
    print("=" * 74)
    print(f"{ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
