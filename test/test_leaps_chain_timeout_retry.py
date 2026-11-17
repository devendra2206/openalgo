"""Both LEAPS strategies: option chains on a 60s client, hedge lookups retried.

Author 2026-11-17. On 17-Nov 10:30 the LEAPS RSI entry found its main strike,
then the hedge's optionchain() timed out on the 12s client and the entry was
skipped until the next candle. The fix gives option chains their own 60s
client and retries the hedge (at entry and at the monthly roll) every 60s for
up to 600s, the window the main strike already had.

Imports each real deployed script, stubs only the broker boundary and the
sleep, and drives the real Broker.connect_chain_client and
StrategyEngine._select_hedge_with_retry.

Run: .venv/Scripts/python.exe -I test/test_leaps_chain_timeout_retry.py
"""
import importlib.util
import sys
import types
from pathlib import Path

DEPLOYED = Path(__file__).resolve().parent.parent / "strategies" / "deployed"
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


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))


class FakeTime:
    """Replaces the time module the helper imports: sleep advances the clock."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def run_helper(m, outcomes, force_exit_after=None):
    """Drive the real helper; outcomes is a list of hedge results per attempt."""
    calls = []
    chain_client = object()
    eng = types.SimpleNamespace(chain_client=chain_client, _force_exit_pending=False)

    def fake_select(client, expiry, side, strike):
        calls.append(client)
        i = len(calls) - 1
        if force_exit_after is not None and i + 1 >= force_exit_after:
            eng._force_exit_pending = True
        out = outcomes[i] if i < len(outcomes) else None
        if isinstance(out, Exception):
            raise out
        return out

    ft = FakeTime()
    orig_select, orig_time = m.select_hedge_strike, sys.modules.get("time")
    m.select_hedge_strike = fake_select
    sys.modules["time"] = ft
    try:
        got = m.StrategyEngine._select_hedge_with_retry(eng, "29DEC26", "PE", 25000.0, "Entry")
    finally:
        m.select_hedge_strike = orig_select
        sys.modules["time"] = orig_time
    return got, calls, ft, chain_client


def main():
    for name, path in SCRIPTS.items():
        m = load(path, f"leaps_chain_{name.lower()}")

        env = m.Environment()
        check(f"{name}: option-chain timeout is 60s", env.chain_timeout == 60.0, env.chain_timeout)
        check(f"{name}: order client timeout unchanged at 12s", env.timeout == 12.0, env.timeout)

        seen = {}
        orig_api = m.api
        m.api = lambda **kw: seen.update(kw) or "CHAIN"
        try:
            env.api_key = "K"
            got = m.Broker(env).connect_chain_client()
        finally:
            m.api = orig_api
        check(f"{name}: chain client built with the 60s timeout",
              got == "CHAIN" and seen.get("timeout") == 60.0, seen)

        hedge = {"strike": 24500.0, "ltp": 20.0}
        got, calls, ft, chain = run_helper(m, [None, RuntimeError("Request timed out"), hedge])
        check(f"{name}: hedge found on the 3rd attempt after a miss and a timeout",
              got == hedge and len(calls) == 3, f"got={got} calls={len(calls)}")
        check(f"{name}: hedge lookups use the chain client", all(c is chain for c in calls))
        check(f"{name}: waits 60s between attempts", ft.slept == [60.0, 60.0], ft.slept)

        got, calls, ft, _ = run_helper(m, [])
        check(f"{name}: gives up after the 600s window, never longer",
              got is None and len(calls) == 11 and sum(ft.slept) == 600.0,
              f"got={got} attempts={len(calls)} slept={sum(ft.slept)}")

        got, calls, ft, _ = run_helper(m, [], force_exit_after=2)
        check(f"{name}: a Force Exit stops the retries at once",
              got is None and len(calls) == 2, f"attempts={len(calls)}")

    ok = sum(1 for p, _, _ in RESULTS if p)
    for passed, name, detail in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"  -> {detail}"))
    print(f"{ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


def test_leaps_chain_timeout_retry():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
