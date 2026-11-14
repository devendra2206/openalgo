"""Both LEAPS strategies: no exit or entry before the session's first candle closes.

Author 2026-11-14: "we should not close position at 9:16 ... trade closing, new
trade taking should happen at 11:16 only" (2h HA-MACD), and "for RSI 10:16".
Before the fix, the candle boundary moving from yesterday's last candle to
today's 09:15 bucket read as a new candle, so both strategies decided at 09:17
on yesterday's last candle.

Imports each real deployed script, stubs only the clock, and drives the real
StrategyEngine._new_candle_closed on a minimal engine object.

Run: .venv/Scripts/python.exe -I test/test_leaps_first_decision_time.py
"""
import datetime as _dt
import importlib.util
import sys
import types
from pathlib import Path

DEPLOYED = Path(__file__).resolve().parent.parent / "strategies" / "deployed"
SCRIPTS = {
    # name: (file, first candle close + settle, the last minute before it)
    "HAMACD": (DEPLOYED / "Nifty_LEAPS_HAMACD_2h_1.py", (11, 17), (11, 16)),
    "RSI": (DEPLOYED / "Nifty_LEAPS_OptionsSell_1_20260823190000.py", (10, 17), (10, 16)),
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


def engine(m, last_boundary, last_key):
    """Only what _new_candle_closed reads."""
    return types.SimpleNamespace(
        _last_candle_boundary=last_boundary,
        state=types.SimpleNamespace(last_candle_key=last_key),
    )


def at(m, hm, day=(2026, 11, 16)):
    return _dt.datetime(*day, *hm, 30)


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))


def main():
    for name, (path, first, before) in SCRIPTS.items():
        m = load(path, f"leaps_first_decision_{name.lower()}")
        closed = m.StrategyEngine._new_candle_closed
        yesterday_last = m.IST.localize(_dt.datetime(2026, 11, 13, 15, 15))

        # A process that ran yesterday and keeps running: the first ticks of
        # the morning must not count as a new candle.
        eng = engine(m, yesterday_last, "2026-11-13 15:15:00+05:30")
        fired = []
        for hm in ((9, 17), (9, 45), (10, 0), before):
            with Clock(m, at(m, hm)):
                if closed(eng):
                    fired.append(hm)
        check(f"{name}: no decision from 09:15 to {before[0]:02d}:{before[1]:02d}", fired == [], f"fired at {fired}")
        with Clock(m, at(m, first)):
            got = closed(eng)
        check(f"{name}: first decision at {first[0]:02d}:{first[1]:02d}", got is True, f"got {got}")

        # Started at 09:15 by the scheduler, with a persisted candle key from
        # an earlier run: the restart recovery must also wait.
        eng = engine(m, None, "2026-11-13 15:15:00+05:30")
        with Clock(m, at(m, (9, 17))):
            got = closed(eng)
        check(f"{name}: restart at 09:17 does not decide", got is False, f"got {got}")
        with Clock(m, at(m, first)):
            got = closed(eng)
        check(f"{name}: restart decides at {first[0]:02d}:{first[1]:02d}", got is True, f"got {got}")

    ok = sum(1 for p, _, _ in RESULTS if p)
    for passed, name, detail in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"  -> {detail}"))
    print(f"{ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


def test_leaps_first_decision_time():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
