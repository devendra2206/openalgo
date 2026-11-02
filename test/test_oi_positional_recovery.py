"""Recovery-path simulation for the deployed OI positional script.

Every case here is a defect a review round found in the hedge-first change --
the recovery layer inferring which leg was live from the error state's NAME,
which the reversal inverted. A pass means that specific failure is closed.

Reuses the stubs/fakes from test_oi_positional_live.py.

Run: python -I test_oi_recovery.py
"""
import sys
from datetime import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from test_oi_positional_strategy import (  # noqa: E402
    FakeClient, FakeStream, InlineExecutor, load_module, make_engine, make_side, SCRIPT,
)

RESULTS = []


def case(name, fn):
    try:
        fn()
        RESULTS.append((True, name, ""))
    except AssertionError as exc:
        RESULTS.append((False, name, str(exc)))
    except Exception as exc:
        RESULTS.append((False, name, f"{type(exc).__name__}: {exc}"))


def half_built(m, side, short_filled=False, hedge_filled=True, **kw):
    """The shape hedge-first leaves behind: hedge in, short not."""
    side.leg.position = m.ShortHedgePosition(
        short_symbol="S", short_strike=24500.0, quantity=65,
        entry_px=120.0 if short_filled else 0.0, entry_filled=short_filled,
        hedge_symbol="H", hedge_strike=25200.0,
        hedge_entry_px=24.0 if hedge_filled else 0.0, hedge_entry_filled=hedge_filled,
        entry_time="2026-11-02T11:31:00", execution_id="EX1",
        expiry_date="2026-11-28", **kw)
    return side.leg.position


def main():
    m = load_module()
    m.append_trade_log = lambda *a, **k: None
    m.ack_pending_action = lambda *a, **k: None

    # ---------------------------------------------------------------- 1 ----
    def t_retry_entry_failed():
        """Retry on entry_failed must resume the SHORT, polled as a SELL on the
        short symbol. Handing that order id to _watch_entry_fill polled it as a
        BUY on the hedge symbol and the guard inside returned silently -- a
        live short whose fill was never recorded."""
        c = FakeClient({"S": ("complete", 118.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side)
        pos.error_state, pos.error_kind, pos.error_order_id = "entry_failed", "terminal", ""
        side._resolve_leg_error({"action": "retry"})
        acts = [(s, a) for s, a, q, o in c.orders]
        assert acts == [("S", "SELL")], f"expected one short SELL, got {acts}"
        assert side.leg.position.entry_filled, "the short's fill must be recorded"
        assert side.leg.position.entry_px == 118.0, side.leg.position.entry_px

    # ---------------------------------------------------------------- 2 ----
    def t_retry_hedge_failed():
        """Retry on hedge_entry_failed must run the FULL chain -- the hedge is
        the first leg now, so resuming it alone left short_symbol set with
        nothing filled, frozen by evaluate()'s 'still entering' guard."""
        c = FakeClient({"H": ("complete", 23.0), "S": ("complete", 121.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side, hedge_filled=False)
        pos.error_state, pos.error_kind, pos.error_order_id = "hedge_entry_failed", "terminal", ""
        side._resolve_leg_error({"action": "retry"})
        acts = [(s, a) for s, a, q, o in c.orders]
        assert acts == [("H", "BUY"), ("S", "SELL")], f"expected hedge then short, got {acts}"
        p = side.leg.position
        assert p.hedge_entry_filled and p.entry_filled, "both legs must end filled"

    # ---------------------------------------------------------------- 3 ----
    def t_cancel_terminal_closes_live_hedge():
        """Cancel is an explicit human instruction ("I have dealt with it at
        the broker"), so it clears the local record -- but it must place NO
        order (author 2026-11-02: "any failuer on trade placing, it should be
        handled manully ... why are you seleling headeg?")."""
        c = FakeClient({"H": ("complete", 20.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side)
        pos.error_state, pos.error_kind, pos.error_order_id = "entry_failed", "terminal", ""
        side._resolve_leg_error({"action": "cancel"})
        assert c.orders == [], f"Cancel must not place any order, got {c.orders}"
        assert side.leg.position.short_symbol == "", "leg should end flat after Cancel"

    # ---------------------------------------------------------------- 4 ----
    def t_cancel_terminal_nothing_live():
        """Cancel on a terminal hedge_entry_failed (nothing filled) resets."""
        c = FakeClient()
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side, hedge_filled=False)
        pos.error_state, pos.error_kind, pos.error_order_id = "hedge_entry_failed", "terminal", ""
        side._resolve_leg_error({"action": "cancel"})
        assert c.orders == [], f"no orders expected, got {c.orders}"
        assert side.leg.position.short_symbol == "", "leg should end flat"

    # ---------------------------------------------------------------- 5 ----
    def t_manual_hedge_continues_into_short():
        """A manually-completed HEDGE leaves the position half built, so the
        short still has to be placed."""
        c = FakeClient({"S": ("complete", 119.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side, hedge_filled=False)
        pos.error_state, pos.error_kind, pos.error_order_id = "hedge_entry_failed", "resting", "OIDX"
        side._resolve_leg_error({"action": "manual", "fill_price": 22.5})
        acts = [(s, a) for s, a, q, o in c.orders]
        assert acts == [("S", "SELL")], f"the short must follow the manual hedge, got {acts}"
        p = side.leg.position
        assert p.hedge_entry_px == 22.5 and p.hedge_entry_filled, "manual hedge fill recorded"
        assert p.entry_filled, "short must end filled"

    # ---------------------------------------------------------------- 6 ----
    def t_reconcile_rejected_with_live_hedge():
        """Startup recovery: a rejected short entry while the hedge is FILLED
        must close the hedge out, not discard the position."""
        c = FakeClient({"H": ("complete", 21.0)})
        c.fills["S"] = ("rejected", 0.0)
        eng = make_engine(m, c)
        side = make_side(m, eng)
        realeng = object.__new__(m.StrategyEngine)
        realeng.state = eng.state
        realeng.client = c
        realeng.price_stream = eng.price_stream
        realeng.env = eng.env
        realeng.monthly = {"CE": side}
        realeng.save_state = lambda: None
        realeng._push_leg_error_bg = lambda *a, **k: None
        side.engine = eng          # side keeps its own fake engine for orders
        leg = eng.state.legs["MONTHLY_CE"]
        pos = half_built(m, side)
        pos.entry_order_id = c.placeorder(symbol="S", action="SELL", quantity="65")["orderid"]
        pos.error_state = "entry_failed"
        base = len(c.orders)       # keep the setup order resolvable by orderstatus()
        m.StrategyEngine._reconcile_one(
            realeng, "MONTHLY_CE", leg, pos, pos.entry_order_id, "entry_failed", "S", "SELL")
        acts = [(s, a) for s, a, q, o in c.orders[base:]]
        assert acts == [], f"startup recovery must place no order, got {acts}"
        p = leg.position
        assert p.hedge_entry_filled, "the filled hedge must stay recorded"
        assert p.error_state and p.error_kind == "terminal",             f"expected a terminal error for manual handling, got {p.error_state}"

    # ---------------------------------------------------------------- 7 ----
    def t_reconcile_rejected_nothing_live():
        """Same, but nothing filled -> clear the leg, place nothing."""
        c = FakeClient()
        c.fills["S"] = ("rejected", 0.0)
        eng = make_engine(m, c)
        side = make_side(m, eng)
        realeng = object.__new__(m.StrategyEngine)
        realeng.state = eng.state
        realeng.client = c
        realeng.price_stream = eng.price_stream
        realeng.env = eng.env
        realeng.monthly = {"CE": side}
        realeng.save_state = lambda: None
        realeng._push_leg_error_bg = lambda *a, **k: None
        leg = eng.state.legs["MONTHLY_CE"]
        pos = half_built(m, side, hedge_filled=False)
        pos.entry_order_id = c.placeorder(symbol="S", action="SELL", quantity="65")["orderid"]
        pos.error_state = "entry_failed"
        base = len(c.orders)
        m.StrategyEngine._reconcile_one(
            realeng, "MONTHLY_CE", leg, pos, pos.entry_order_id, "entry_failed", "S", "SELL")
        assert c.orders[base:] == [], f"no orders expected, got {c.orders[base:]}"
        assert leg.position.short_symbol == "", "leg should be cleared"

    # ---------------------------------------------------------------- 8 ----
    def t_exit_order_is_short_then_hedge():
        """A fully built position closes short BUY first, then hedge SELL --
        the reverse of the entry, so the naked leg goes first."""
        c = FakeClient({"S": ("complete", 100.0), "H": ("complete", 30.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        half_built(m, side, short_filled=True, hedge_filled=True)
        side._exit(side.leg.position, "stop_loss")
        acts = [(s, a) for s, a, q, o in c.orders]
        assert acts == [("S", "BUY"), ("H", "SELL")], f"expected short then hedge, got {acts}"
        # Both exit legs are confirmed; the normal chain finalizes on the next
        # evaluate() tick (the finalize-FIRST branch), not inline.
        p = side.leg.position
        assert p.short_exit_filled and p.hedge_exit_filled, "both exits should be confirmed"
        side.evaluate()
        assert side.leg.position.short_symbol == "", "evaluate() should finalize and flatten"

    # ---------------------------------------------------------------- 9 ----
    def t_force_exit_uses_same_chain():
        """Force exit must not invent its own ordering."""
        src = SCRIPT.read_text(encoding="utf-8")
        total = src.count("self._exit(pos,") + src.count("side._exit(pos,")
        assert total == 4, f"expected 4 _exit call sites, one per close reason, got {total}"
        for reason in ('"stop_loss"', '"expiry_day_close"', '"opposite_signal"', '"force_exit"'):
            assert f"_exit(pos, {reason})" in src, f"{reason} must go through _exit"

    # --------------------------------------------------------------- 10 ----
    def t_abandon_not_user_initiated_waits_for_a_human():
        """A failure the STRATEGY hit (not a Cancel) must place no order, must
        NOT reset the leg -- so the filled hedge keeps its PnL row and its
        subscription -- and must raise a terminal error for manual handling.
        error_state also stops evaluate() entering again and stacking a second
        hedge on the live one."""
        c = FakeClient()
        eng = make_engine(m, c)
        side = make_side(m, eng)
        half_built(m, side)                       # hedge filled, short not
        side._abandon_attempt("placeorder_failed")
        assert c.orders == [], f"no order may be placed, got {c.orders}"
        p = side.leg.position
        assert p.hedge_symbol == "H" and p.hedge_entry_filled,             "the filled hedge must stay recorded, not be discarded"
        assert p.error_state == "entry_failed" and p.error_kind == "terminal",             f"expected a terminal error for manual handling, got {p.error_state}/{p.error_kind}"
        assert "manully" not in p.error_message.lower()  # it is spelled correctly
        assert "Manually Completed" in p.error_message, "the message must say how to resolve it"

    def t_abandon_nothing_filled_still_resets():
        """With nothing filled there is no position, so resetting is safe and
        no error should be raised."""
        c = FakeClient()
        eng = make_engine(m, c)
        side = make_side(m, eng)
        half_built(m, side, hedge_filled=False)
        side._abandon_attempt("placeorder_failed")
        assert c.orders == []
        assert side.leg.position.short_symbol == "", "leg should be flat"
        assert side.leg.position.error_state == "", "no error for an empty attempt"

    def t_no_double_exit_in_flight():
        """A second _exit while one is in flight must be a no-op."""
        c = FakeClient({"S": ("complete", 100.0), "H": ("complete", 30.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        pos = half_built(m, side, short_filled=True, hedge_filled=True)
        pos.short_exit_order_id = "ALREADY"
        pos.short_exit_filled = False
        side._exit(pos, "stop_loss")
        assert c.orders == [], f"an in-flight exit must block a second one, got {c.orders}"

    for name, fn in [
        ("retry(entry_failed) resumes the SHORT as a SELL", t_retry_entry_failed),
        ("retry(hedge_entry_failed) runs the full hedge->short chain", t_retry_hedge_failed),
        ("cancel(terminal, hedge live) closes the hedge instead of discarding it",
         t_cancel_terminal_closes_live_hedge),
        ("cancel(terminal, nothing live) resets and places nothing",
         t_cancel_terminal_nothing_live),
        ("manual hedge completion continues into the short", t_manual_hedge_continues_into_short),
        ("reconcile(rejected, hedge live) raises a manual-handling error, places nothing", t_reconcile_rejected_with_live_hedge),
        ("reconcile(rejected, nothing live) clears the leg", t_reconcile_rejected_nothing_live),
        ("exit order is short BUY then hedge SELL", t_exit_order_is_short_then_hedge),
        ("force exit funnels through the same single chain", t_force_exit_uses_same_chain),
        ("abandon (strategy-side failure) places no order and waits for a human",
         t_abandon_not_user_initiated_waits_for_a_human),
        ("abandon with nothing filled still resets cleanly",
         t_abandon_nothing_filled_still_resets),
        ("an in-flight exit blocks a second one", t_no_double_exit_in_flight),
    ]:
        case(name, fn)

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
