"""Both LEAPS strategies: the monthly hedge roll buys the new hedge FIRST.

Author 2026-11-17: "fix hedge rollover .. buy new hedge first then close
existing". Before, the roll sold the old hedge first and then looked for the
new one, so any failure after the sale (expiry lookup, no candidate, order
rejected) left the short with no hedge at all.

Imports each real deployed script, builds a real StrategyEngine and stubs only
the broker boundary (place / poll_fill / orderstatus / expiry / hedge
selection), asserting on the ORDER SEQUENCE and the state left behind.

Run: .venv/Scripts/python.exe -I test/test_leaps_hedge_roll_new_first.py
"""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

DEPLOYED = Path(__file__).resolve().parent.parent / "strategies" / "deployed"
SCRIPTS = {
    "RSI": DEPLOYED / "Nifty_LEAPS_OptionsSell_1_20260823190000.py",
    "HAMACD": DEPLOYED / "Nifty_LEAPS_HAMACD_2h_1.py",
}
OLD, NEW = "NIFTY24NOV2624300PE", "NIFTY29DEC2624300PE"


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


class Broker:
    """Records every order; fills are scripted per (symbol, action)."""

    def __init__(self, m, fills=None, place_fail=()):
        self.m = m
        self.orders = []
        self.fills = fills or {}
        self.place_fail = set(place_fail)
        self.status = {}

    def place(self, client, strategy, symbol, exchange, action, quantity):
        if (symbol, action) in self.place_fail:
            raise RuntimeError("Rejected: no margin")
        oid = f"O{len(self.orders) + 1}"
        self.orders.append((symbol, action, oid))
        return oid

    def poll_fill(self, client, order_id, strategy, symbol, exchange, action, quantity):
        out = self.fills.get((symbol, action), ("complete", 10.0))
        if out[0] == "complete":
            return {"order_status": "complete", "average_price": out[1]}
        if out[0] == "rejected":
            raise RuntimeError(f"Order {order_id} ended in status 'rejected'")
        raise self.m.OrderNeedsAttention(order_id, "resting at broker")


def build(m, broker, hedge=None, telegram=None):
    m.place = broker.place
    m.poll_fill = broker.poll_fill
    m.append_trade_log = lambda *a, **k: None
    m.resolve_hedge_monthly_expiry = lambda client, day: ("29DEC26", "29-Dec-26")
    m.select_hedge_strike = lambda client, exp, side, strike: hedge
    m.config.main_strike_retry_window_sec = 0          # one hedge attempt, no sleeping
    env = m.Environment()
    env.strategy_tag = "TEST"
    store = m.StateStore(env)
    store.save = lambda: None
    client = MagicMock()
    client.orderstatus.side_effect = lambda order_id, strategy: {
        "data": {"order_status": broker.status.get(order_id, "open"), "average_price": 9.0}}
    ps = MagicMock()
    ps.get_ltp.return_value = None
    eng = m.StrategyEngine(client, store, env, ps, execution_id=1, ltp_client=MagicMock())
    eng._notify_telegram_error_bg = lambda msg: (telegram if telegram is not None else []).append(msg)
    pos = eng.state.position
    pos.side, pos.short_symbol, pos.short_strike = "PE", "NIFTY29DEC2625000PE", 25000.0
    pos.quantity, pos.entry_px, pos.entry_filled = 65, 346.0, True
    pos.hedge_symbol, pos.hedge_strike, pos.hedge_expiry = OLD, 24300.0, "2026-11-24"
    pos.hedge_entry_px, pos.hedge_entry_filled = 20.0, True
    return eng


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))


def roll(eng):
    eng._pending_fills.add("position")
    eng._do_hedge_roll()


def settle(eng):
    eng._pending_fills.add("position")
    eng._settle_roll_and_leftovers()


def shutdown(eng):
    for ex in (eng._fill_executor, eng._bg_executor, eng._rsi_executor):
        ex.shutdown(wait=False)


def main():
    hedge = {"strike": 24300.0, "ltp": 30.0}
    for name, path in SCRIPTS.items():
        m = load(path, f"leaps_roll_{name.lower()}")

        # 1. normal roll: BUY new, then SELL old
        b = Broker(m, fills={(NEW, "BUY"): ("complete", 30.0), (OLD, "SELL"): ("complete", 2.5)})
        eng = build(m, b, hedge)
        roll(eng)
        seq = [(s, a) for s, a, _ in b.orders]
        check(f"{name}: BUY new hedge first, then SELL the old", seq == [(NEW, "BUY"), (OLD, "SELL")], seq)
        pos = eng.state.position
        check(f"{name}: position now holds the new hedge",
              pos.hedge_symbol == NEW and pos.hedge_entry_px == 30.0 and pos.hedge_expiry == "2026-12-29",
              (pos.hedge_symbol, pos.hedge_entry_px, pos.hedge_expiry))
        check(f"{name}: old hedge sold and booked",
              eng.state.leftover_hedges == [] and abs(eng.state.today_realized_pnl - (2.5 - 20.0) * 65) < 0.01,
              (eng.state.leftover_hedges, eng.state.today_realized_pnl))
        check(f"{name}: roll guard released", "position" not in eng._pending_fills)
        shutdown(eng)

        # 2. no new hedge found: nothing sold, old kept, alerted
        tg = []
        b = Broker(m)
        eng = build(m, b, None, tg)
        roll(eng)
        check(f"{name}: no new hedge -> no order at all, old hedge kept",
              b.orders == [] and eng.state.position.hedge_symbol == OLD, b.orders)
        check(f"{name}: no new hedge -> Telegram alert, roll marked done",
              len(tg) == 1 and eng.state.position.last_hedge_roll_date != "", tg)
        shutdown(eng)

        # 3. new hedge BUY rejected: the old hedge is never sold
        b = Broker(m, fills={(NEW, "BUY"): ("rejected", 0)})
        eng = build(m, b, hedge)
        roll(eng)
        seq = [(s, a) for s, a, _ in b.orders]
        check(f"{name}: rejected BUY -> old hedge NOT sold", seq == [(NEW, "BUY")], seq)
        check(f"{name}: rejected BUY -> old hedge still the position's hedge",
              eng.state.position.hedge_symbol == OLD and not eng.state.pending_roll)
        shutdown(eng)

        # 4. new hedge BUY place() fails: no order, old kept
        b = Broker(m, place_fail={(NEW, "BUY")})
        eng = build(m, b, hedge)
        roll(eng)
        check(f"{name}: BUY not placed -> old hedge kept, nothing pending",
              b.orders == [] and eng.state.position.hedge_symbol == OLD and not eng.state.pending_roll)
        shutdown(eng)

        # 5. BUY resting at the broker: old kept until it fills, then swapped and sold
        b = Broker(m, fills={(NEW, "BUY"): ("resting", 0), (OLD, "SELL"): ("complete", 3.0)})
        eng = build(m, b, hedge)
        roll(eng)
        seq = [(s, a) for s, a, _ in b.orders]
        check(f"{name}: resting BUY -> old hedge not sold yet", seq == [(NEW, "BUY")], seq)
        check(f"{name}: resting BUY -> recorded as pending, old still the hedge",
              eng.state.pending_roll.get("order_id") == "O1" and eng.state.position.hedge_symbol == OLD,
              eng.state.pending_roll)
        settle(eng)                                       # still open at the broker
        check(f"{name}: still resting -> nothing changes",
              len(b.orders) == 1 and eng.state.position.hedge_symbol == OLD)
        b.status["O1"] = "complete"
        settle(eng)
        seq = [(s, a) for s, a, _ in b.orders]
        check(f"{name}: BUY fills later -> swapped, then old SOLD",
              seq == [(NEW, "BUY"), (OLD, "SELL")] and eng.state.position.hedge_symbol == NEW
              and not eng.state.pending_roll and not eng.state.leftover_hedges, seq)
        shutdown(eng)

        # 6. old hedge SELL fails: kept as a leftover, retried, reported, Force Exit waits
        tg = []
        b = Broker(m, fills={(NEW, "BUY"): ("complete", 30.0)}, place_fail={(OLD, "SELL")})
        eng = build(m, b, hedge, tg)
        roll(eng)
        check(f"{name}: old SELL fails -> new hedge kept, old is a leftover",
              eng.state.position.hedge_symbol == NEW
              and [h["symbol"] for h in eng.state.leftover_hedges] == [OLD], eng.state.leftover_hedges)
        check(f"{name}: old SELL fails -> Telegram once", len(tg) == 1, tg)
        rows = [r["leg_key"] for r in eng._open_positions_for_pnl()]
        check(f"{name}: leftover shown in the P&L rows", rows[-1].endswith("_HEDGE_OLD"), rows)
        flat = eng.state.position
        saved_side, flat.side = flat.side, ""             # as if the position had closed
        m.ack_force_exit_complete = lambda *a, **k: None
        check(f"{name}: Force Exit is not complete while an old hedge is unsold",
              eng._force_exit_all() is False)
        flat.side = saved_side
        b.place_fail.clear()
        b.fills[(OLD, "SELL")] = ("complete", 1.0)
        settle(eng)
        check(f"{name}: retry sells the leftover", eng.state.leftover_hedges == []
              and (OLD, "SELL") in [(s, a) for s, a, _ in b.orders])
        shutdown(eng)

        # 7. position already unhedged: the roll buys a hedge and sells nothing
        b = Broker(m, fills={(NEW, "BUY"): ("complete", 30.0)})
        eng = build(m, b, hedge)
        p = eng.state.position
        p.hedge_symbol, p.hedge_expiry, p.hedge_entry_filled, p.hedge_entry_px = "", "", False, 0.0
        roll(eng)
        seq = [(s, a) for s, a, _ in b.orders]
        check(f"{name}: unhedged position -> roll buys a hedge, sells nothing",
              seq == [(NEW, "BUY")] and eng.state.position.hedge_symbol == NEW
              and not eng.state.leftover_hedges, seq)
        shutdown(eng)

        # 8. pending roll and leftovers survive a restart
        with tempfile.TemporaryDirectory() as d:
            env = m.Environment()
            st = m.StateStore(env)
            st.path = Path(d) / "s.json"
            st.state.pending_roll = {"symbol": NEW, "strike": 24300.0, "expiry": "2026-12-29",
                                     "quantity": 65, "order_id": "O9"}
            st.state.leftover_hedges = [{"symbol": OLD, "quantity": 65, "entry_px": 20.0,
                                         "entry_time": "", "expiry": "2026-11-24",
                                         "execution_id": 1, "sell_order_id": ""}]
            st.save()
            st2 = m.StateStore(env)
            st2.path = st.path
            st2.load()
            raw = json.loads(st.path.read_text())
            check(f"{name}: pending roll and leftovers persist across a restart",
                  st2.state.pending_roll.get("order_id") == "O9"
                  and st2.state.leftover_hedges[0]["symbol"] == OLD and "pending_roll" in raw)

    ok = sum(1 for p, _, _ in RESULTS if p)
    for passed, name, detail in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f"  -> {detail}"))
    print(f"{ok}/{len(RESULTS)} passed")
    return 0 if ok == len(RESULTS) else 1


def test_leaps_hedge_roll_new_first():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
