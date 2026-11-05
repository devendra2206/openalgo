"""Both LEAPS strategies: the 18th-of-month hedge roll date honours holidays.

Live 05-Nov-2026 the OI script's server log showed "Trading Calendar : NOT
LOADED": `from utils.trading_calendar import ...` cannot resolve in a strategy
subprocess (only strategies/scripts is on sys.path). The LEAPS scripts had the
same import, so a weekday-only check ran, and an 18th that is a weekday holiday
became a roll date the strategy never runs on -- the roll fires only when
today == hedge_roll_date_for_month(), so that month's roll was skipped. The
calendar now reads client.holidays(year) from the OpenAlgo API.

Run: .venv/Scripts/python.exe -I test/test_leaps_calendar.py
"""
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

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


def entry(day, kind, closed):
    return {"date": day, "holiday_type": kind, "closed_exchanges": closed, "open_exchanges": []}


NFO = ["NSE", "BSE", "NFO", "BFO", "CDS", "BCD"]
# Shapes copied from the live 2026 list; the 18th-of-month dates are made up
# to exercise the rule (no real 2026 holiday falls on an 18th).
DATA_2026 = [
    entry("2026-02-19", "SETTLEMENT_HOLIDAY", []),
    entry("2026-11-08", "SPECIAL_SESSION", []),
    entry("2026-11-24", "TRADING_HOLIDAY", NFO),
    entry("2026-12-18", "TRADING_HOLIDAY", NFO),        # Friday 18th closed
    entry("2026-12-17", "TRADING_HOLIDAY", NFO),        # and the 17th too
    entry("2026-03-18", "TRADING_HOLIDAY", ["MCX"]),    # MCX-only: NFO trades
    entry("2026-02-18", "SETTLEMENT_HOLIDAY", []),      # settlement only: NFO trades
]


class Client:
    def __init__(self, data):
        self.data, self.calls = data, []

    def holidays(self, year=None):
        self.calls.append(year)
        return {"status": "success", "year": year, "timezone": "Asia/Kolkata",
                "data": list(self.data) if year == 2026 else []}


RESULTS = []


def check(label, name, fn):
    try:
        fn()
        RESULTS.append((True, f"[{label}] {name}", ""))
    except AssertionError as exc:
        RESULTS.append((False, f"[{label}] {name}", str(exc)))
    except Exception as exc:
        RESULTS.append((False, f"[{label}] {name}", f"{type(exc).__name__}: {exc}"))


def reset(m, client):
    m.set_calendar_client(client)
    with m._CALENDAR_LOCK:
        m._CALENDAR["years"].clear()
        m._CALENDAR["retry_at"].clear()
        m._CALENDAR["warned_at"] = None


def run_for(label, m):
    roll = m.hedge_roll_date_for_month

    def holidays_applied():
        c = Client(DATA_2026)
        reset(m, c)
        assert roll(2026, 12) == date(2026, 12, 16), "18th and 17th closed -> Wed 16th"
        assert roll(2026, 11) == date(2026, 11, 18), "an ordinary 18th stays"
        assert roll(2026, 3) == date(2026, 3, 18), "an MCX-only closure does not move it"
        assert roll(2026, 2) == date(2026, 2, 18), "a settlement holiday does not move it"
        assert roll(2026, 4) == date(2026, 4, 17), "Saturday 18-Apr -> Friday 17th"
        assert c.calls == [2026], f"fetched once and kept: {c.calls}"

    def empty_list_is_not_loaded():
        c = Client([])
        reset(m, c)
        assert roll(2026, 12) == date(2026, 12, 18), "weekends only while not loaded"
        assert roll(2026, 12) == date(2026, 12, 18)
        assert c.calls == [2026], f"a miss is not re-asked every tick: {c.calls}"
        c.data = DATA_2026
        with m._CALENDAR_LOCK:
            m._CALENDAR["retry_at"][2026] = 0.0     # retry window passed
        assert roll(2026, 12) == date(2026, 12, 16), "used once it is available"

    def no_client_falls_back():
        reset(m, None)
        assert roll(2026, 12) == date(2026, 12, 18)

    def platform_import_gone():
        code = "\n".join(ln for ln in Path(m.__file__).read_text(encoding="utf-8").splitlines()
                         if not ln.strip().startswith("#"))
        assert "from utils.trading_calendar" not in code
        assert "set_calendar_client(client)" in code, "main() must connect the calendar"

    for name, fn in (("NFO holidays move the 18th roll date, others do not", holidays_applied),
                     ("an empty holiday list is not loaded, and is retried later",
                      empty_list_is_not_loaded),
                     ("no calendar connected -> weekends only", no_client_falls_back),
                     ("the unreachable platform import is gone", platform_import_gone)):
        check(label, name, fn)
    reset(m, None)


def main():
    for label, path in SCRIPTS.items():
        run_for(label, load(path, f"leaps_cal_{label.lower()}"))
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
