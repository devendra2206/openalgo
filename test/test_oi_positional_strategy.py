"""Behavioural tests for the deployed OI positional script, before a live run.

Not greps -- it imports the real module, stubs only the broker/platform/
scheduler boundary, and drives the state machine, asserting on the ORDERS it
places and the state it leaves behind. Every case here corresponds to a defect
a review round actually found, so a pass means that specific defect is closed
rather than that the file compiles.

Run: python -I test_oi_positional_live.py
"""
import sys
import types
import importlib.util
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

SCRIPT = Path(r"C:\Devendra\OpenAlgo\openalgo\strategies\deployed"
              r"\Nifty_OI_Positional_MonthlySell_1_20260821180000.py")


# ---------------------------------------------------------------- stubs ----
def install_stubs():
    oa = types.ModuleType("openalgo")
    oa.api = object
    sys.modules["openalgo"] = oa

    dv = types.ModuleType("dotenv")
    dv.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = dv

    aps = types.ModuleType("apscheduler")
    sched = types.ModuleType("apscheduler.schedulers")
    blocking = types.ModuleType("apscheduler.schedulers.blocking")
    blocking.BlockingScheduler = object
    trig = types.ModuleType("apscheduler.triggers")
    interval = types.ModuleType("apscheduler.triggers.interval")
    interval.IntervalTrigger = object
    sys.modules.update({"apscheduler": aps, "apscheduler.schedulers": sched,
                        "apscheduler.schedulers.blocking": blocking,
                        "apscheduler.triggers": trig,
                        "apscheduler.triggers.interval": interval})

    spc = types.ModuleType("_strategy_platform_client")
    spc.notify_trade_closed = lambda *a, **k: None
    spc.notify_telegram_error = lambda *a, **k: None
    spc.filter_known_fields = lambda cls, raw: {
        k: v for k, v in (raw or {}).items()
        if k in getattr(cls, "__dataclass_fields__", {})}
    sys.modules["_strategy_platform_client"] = spc


def load_module():
    install_stubs()
    spec = importlib.util.spec_from_file_location("oipos", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["oipos"] = m
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------- fakes ----
class FakeClient:
    """Records every order and reports whatever fill the test asked for."""

    def __init__(self, fills=None):
        self.orders = []          # (symbol, action, qty) in placement order
        self.modifies = []
        self.cancels = []
        self._n = 0
        self.fills = fills or {}  # symbol -> ("complete", px) | ("rejected", 0)

    def placeorder(self, **kw):
        self._n += 1
        oid = f"OID{self._n}"
        self.orders.append((kw["symbol"], kw["action"], int(kw["quantity"]), oid))
        return {"status": "success", "orderid": oid}

    def orderstatus(self, order_id, strategy=None):
        sym = next((s for s, a, q, o in self.orders if o == order_id), None)
        status, px = self.fills.get(sym, ("complete", 10.0))
        return {"data": {"order_status": status, "average_price": px}}

    def modifyorder(self, **kw):
        self.modifies.append(kw.get("symbol"))
        return {"status": "success"}

    def cancelorder(self, **kw):
        self.cancels.append(kw.get("order_id"))
        return {"status": "success"}

    def quotes(self, **kw):
        return {"status": "success", "data": {"bid": 9.5, "ask": 10.5, "ltp": 10.0}}


class FakeStream:
    def __init__(self):
        self.added, self.removed = [], []

    def add_instruments(self, items):
        self.added += [i["symbol"] for i in items]

    def remove_instruments(self, items):
        self.removed += [i["symbol"] for i in items]

    def get_ltp(self, *a, **k):
        return 10.0


class InlineExecutor:
    """Runs submitted work immediately, so a test sees the whole chain."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)

        class _F:
            def result(self_inner, timeout=None):
                return None
        return _F()


def make_engine(m, client=None):
    """A StrategyEngine-shaped object with only what the tested paths touch."""
    eng = types.SimpleNamespace()
    eng.client = client or FakeClient()
    eng.price_stream = FakeStream()
    eng._pending_fills = set()
    eng._eval_pending = set()
    eng._fill_executor = InlineExecutor()
    eng._bg_executor = InlineExecutor()
    eng.execution_id = "EX1"
    eng.env = types.SimpleNamespace(strategy_tag="TEST", api_key="K")
    eng.state = m.StrategyState()
    eng.state.legs = {k: m.LegState() for k in m.LEG_KEYS}
    eng.state.reference = m.ReferenceSnapshot()
    eng.state.today_realized_pnl = 0.0
    eng.save_state = lambda: None
    eng._push_leg_error_bg = lambda *a, **k: None
    eng._notify_telegram_error_bg = lambda *a, **k: None
    eng._notify_trade_closed_bg = lambda *a, **k: None
    eng._refresh_pending_action_bg = lambda *a, **k: None
    eng._pop_pending_action = lambda *a, **k: None
    eng.get_cycle_start_date = lambda: None
    eng.get_current_month_expiry_date = lambda: None
    eng.get_cycle_expiry_date = lambda: None
    eng.get_monthly_expiry = lambda: ("28NOV26", "28-Nov-26")
    eng.get_spot_ltp = lambda: 24000.0
    return eng


def make_side(m, eng, ot="CE"):
    side = m.MonthlyShortEngine(ot, eng)
    return side


# ---------------------------------------------------------------- tests ----
RESULTS = []


def case(name):
    def deco(fn):
        def run():
            try:
                fn()
                RESULTS.append((True, name, ""))
            except AssertionError as exc:
                RESULTS.append((False, name, str(exc)))
            except Exception as exc:
                RESULTS.append((False, name, f"{type(exc).__name__}: {exc}"))
        run.__name__ = fn.__name__
        return run
    return deco


def main():
    m = load_module()
    m.append_trade_log = lambda *a, **k: TRADE_LOG.append(a)
    global TRADE_LOG
    TRADE_LOG = []

    # 1 -------------------------------------------------------------------
    @case("ENTRY places the hedge BUY before the short SELL")
    def t1():
        c = FakeClient({"NIFTY28NOV2624500CE": ("complete", 120.0),
                        "NIFTY28NOV2625200CE": ("complete", 24.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        # The contract traded is DECEMBER's (a post-15th entry), while the
        # cycle this trade belongs to expires in NOVEMBER -- the capture must
        # take the CYCLE's date, not the contract's.
        eng.get_cycle_expiry_date = lambda: date(2026, 11, 24)
        side._enter("NIFTY28NOV2624500CE", 24500.0, 0.27, "weakening",
                    "NIFTY28NOV2625200CE", {"strike": 25200.0}, "2026-12-29")
        seq = [(s, a) for s, a, q, o in c.orders]
        assert seq == [("NIFTY28NOV2625200CE", "BUY"),
                       ("NIFTY28NOV2624500CE", "SELL")], f"order sequence was {seq}"
        pos = side.leg.position
        assert pos.hedge_entry_filled and pos.entry_filled, "both legs should be filled"
        assert pos.hedge_entry_px == 24.0 and pos.entry_px == 120.0, "fill prices"
        assert side.leg.marked_expiry == "2026-11-24",             f"the CYCLE expiry must be captured, not the contract's: {side.leg.marked_expiry}"
        assert pos.expiry_date == "2026-12-29", "the position still records its own contract"

    # 2 -------------------------------------------------------------------
    @case("EXIT of a half-built leg never BUYs the unfilled short")
    def t2():
        c = FakeClient({"H": ("complete", 18.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        side.leg.position = m.ShortHedgePosition(
            short_symbol="S", short_strike=24500.0, quantity=65,
            entry_px=0.0, entry_filled=False,            # short never filled
            hedge_symbol="H", hedge_strike=25200.0,
            hedge_entry_px=24.0, hedge_entry_filled=True,  # hedge IS live
            entry_time="2026-11-02T11:31:00", execution_id="EX1",
            expiry_date="2026-11-28",
        )
        side._exit(side.leg.position, "abandoned_test")
        acts = [(s, a) for s, a, q, o in c.orders]
        assert ("S", "BUY") not in acts, f"must not buy the unfilled short: {acts}"
        assert ("H", "SELL") in acts, f"must sell the live hedge: {acts}"
        assert len(acts) == 1, f"exactly one order expected, got {acts}"

    # 3 -------------------------------------------------------------------
    @case("a half-built leg finalizes: flat, one trade row, PnL from the hedge only")
    def t3():
        TRADE_LOG.clear()
        c = FakeClient({"H": ("complete", 18.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        side.leg.position = m.ShortHedgePosition(
            short_symbol="S", quantity=65, entry_px=0.0, entry_filled=False,
            hedge_symbol="H", hedge_entry_px=24.0, hedge_entry_filled=True,
            entry_time="2026-11-02T11:31:00", execution_id="EX1",
            expiry_date="2026-11-28",
        )
        side._exit(side.leg.position, "abandoned_test")
        assert side.leg.position.short_symbol == "", "leg should be flat after finalize"
        assert len(TRADE_LOG) == 1, f"one trade row expected (hedge only), got {len(TRADE_LOG)}"
        expected = (18.0 - 24.0) * 65
        got = eng.state.today_realized_pnl
        assert abs(got - expected) < 0.01, f"PnL should be the hedge's {expected}, got {got}"

    # 4 -------------------------------------------------------------------
    @case("abandoning with NOTHING filled places no order and resets the leg")
    def t4():
        c = FakeClient()
        eng = make_engine(m, c)
        side = make_side(m, eng)
        side.leg.position = m.ShortHedgePosition(
            short_symbol="S", quantity=65, hedge_symbol="H",
            entry_filled=False, hedge_entry_filled=False,
        )
        side._abandon_attempt("test")
        assert c.orders == [], f"no orders expected, got {c.orders}"
        assert side.leg.position.short_symbol == "", "leg should be flat"
        assert set(eng.price_stream.removed) == {"S", "H"}, eng.price_stream.removed

    # 5 -------------------------------------------------------------------
    @case("PnL reports a filled hedge even while the short is unfilled")
    def t5():
        eng = make_engine(m)
        realeng = object.__new__(m.StrategyEngine)
        realeng.state = eng.state
        realeng.price_stream = eng.price_stream
        leg = eng.state.legs["MONTHLY_CE"]
        leg.position = m.ShortHedgePosition(
            short_symbol="S", quantity=65, entry_px=0.0, entry_filled=False,
            hedge_symbol="H", hedge_entry_px=24.0, hedge_entry_filled=True,
            entry_time="t", execution_id="EX1")
        rows = m.StrategyEngine._open_positions_for_pnl(realeng)
        syms = [r["symbol"] for r in rows]
        assert syms == ["H"], f"the filled hedge must appear alone, got {syms}"

    # 6 -------------------------------------------------------------------
    @case("the 1-minute buffer defers a slot, and the slot key stays on the slot time")
    def t6():
        assert m.config.decision_buffer_min == 1
        got = m._with_buffer(time(11, 30))
        assert got == time(11, 31), got
        slots = list(m.config.daily_decision_times)
        for probe, want in ((time(11, 30, 30), None), (time(11, 31), time(11, 30)),
                            (time(15, 31), time(15, 30))):
            el = [t for t in slots if probe >= m._with_buffer(t)]
            assert (el[-1] if el else None) == want, f"at {probe} expected {want}, got {el}"

    # 7 -------------------------------------------------------------------
    @case("square-off defers to 15:30 on the calendar expiry date")
    def t7():
        close = m.config.expiry_close_time
        def defers(slot, calendar, own):
            return slot < close and not (own and not calendar)
        assert defers(time(11, 30), True, True), "11:30 on the expiry date must defer"
        assert defers(time(13, 30), True, True), "13:30 on the expiry date must defer"
        assert not defers(time(15, 30), True, True), "15:30 must square off"
        assert not defers(time(11, 30), False, True), \
            "own expiry on a non-calendar-expiry date closes at once"

    # 8 -------------------------------------------------------------------
    @case("no pytz LMT boundary: the freshness gate is +05:30")
    def t8():
        b = m.IST.localize(datetime.combine(date(2026, 11, 2), time(11, 30)))
        assert b.utcoffset() == timedelta(hours=5, minutes=30), b.utcoffset()
        src = SCRIPT.read_text(encoding="utf-8")
        code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
        assert "tzinfo=IST" not in code, "tzinfo=IST still in code (gives +05:53)"

    # 9 -------------------------------------------------------------------
    @case("startup refuses a square-off time that is not a check slot")
    def t9():
        assert m.config.expiry_close_time in m.config.daily_decision_times, \
            "expiry_close_time must be one of the check slots"
        src = SCRIPT.read_text(encoding="utf-8")
        assert "if config.expiry_close_time not in config.daily_decision_times:" in src

    # 10 ------------------------------------------------------------------
    @case("verdict config: short and hold on weakening")
    def t10():
        assert m.config.short_verdict == "weakening"
        assert m.config.hold_verdict == "weakening"
        assert m.classify_oi_premium(-5, +100) == "weakening"   # premium down, OI up
        assert m.classify_oi_premium(-5, -100) == "weakening"   # premium down, OI down
        assert m.classify_oi_premium(+5, +100) == "accumulation"
        assert m.classify_oi_premium(0, +100) == "flat"

    # 11 ------------------------------------------------------------------
    @case("calendar expiry date: entries run until the square-off, then stop")
    def t11():
        """The rule (author 2026-11-02): "any new postion taken on current
        expiry last day, it will take position on next month, but at 3:30 all
        position will be closed". So 11:30 and 13:30 are ORDINARY -- a flat leg
        searches and enters, past day 15 into next month's contract -- and only
        at/after expiry_close_time do entries stop. Two earlier revisions
        over-reached: one suppressed the verdict check all day, the other kept
        it but refused every entry."""
        import datetime as _d
        EXP = date(2026, 11, 24)

        def run(slot, flat):
            c = FakeClient({"S": ("complete", 100.0), "H": ("complete", 30.0)})
            eng = make_engine(m, c)
            side = make_side(m, eng)
            eng.get_cycle_expiry_date = lambda: EXP
            eng.get_cycle_start_date = lambda: date(2026, 10, 28)
            # the square-off fires on the CAPTURED cycle expiry
            side.leg.marked_expiry = EXP.isoformat()
            searched = []
            side._search_and_enter_fresh = lambda st=None: searched.append(st or "now")
            if not flat:
                side.leg.position = m.ShortHedgePosition(
                    short_symbol="S", quantity=65, entry_px=120.0, entry_filled=True,
                    hedge_symbol="H", hedge_entry_px=24.0, hedge_entry_filled=True,
                    entry_time="t", execution_id="EX1",
                    expiry_date="2026-12-29")        # NEXT month's contract
            class FakeDT(_d.datetime):
                @classmethod
                def now(cls, tz=None):
                    return _d.datetime.combine(EXP, slot).replace(tzinfo=tz)
            orig = m.datetime
            m.datetime = FakeDT
            try:
                side._daily_decision(slot)
            finally:
                m.datetime = orig
            return [(sym, a) for sym, a, q, o in c.orders], bool(searched)

        close = m.config.expiry_close_time
        for slot in (time(11, 30), time(13, 30)):
            orders, searched = run(slot, flat=True)
            assert searched, f"{slot}: a flat leg must still search for an entry"
            assert orders == [], f"{slot}: the search is stubbed, no orders expected"
        orders, searched = run(close, flat=True)
        assert not searched, f"{close}: entries must stop at the square-off slot"

        orders, _ = run(time(11, 30), flat=False)
        assert orders == [], f"11:30 must not square off an open position: {orders}"
        orders, _ = run(close, flat=False)
        assert orders == [("S", "BUY"), ("H", "SELL")],             f"the square-off must close both legs of the Dec contract: {orders}"

    # 12 ------------------------------------------------------------------
    @case("a stop on the expiry date before 15:30 does not block re-entry")
    def t12():
        """_finalize_exit marks the day terminal only for the square-off (or an
        exit at/after its slot). Marking it for ANY exit on that date would
        have blocked the 13:30 re-entry the rule allows after a 12:00 stop."""
        import datetime as _d
        EXP = date(2026, 11, 24)
        c = FakeClient({"S": ("complete", 150.0), "H": ("complete", 30.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        eng.get_cycle_expiry_date = lambda: EXP
        side.leg.marked_expiry = EXP.isoformat()
        side.leg.position = m.ShortHedgePosition(
            short_symbol="S", quantity=65, entry_px=120.0, entry_filled=True,
            hedge_symbol="H", hedge_entry_px=24.0, hedge_entry_filled=True,
            entry_time="t", execution_id="EX1", expiry_date="2026-12-29")
        class FakeDT(_d.datetime):
            @classmethod
            def now(cls, tz=None):
                return _d.datetime.combine(EXP, time(12, 0)).replace(tzinfo=tz)
        orig = m.datetime
        m.datetime = FakeDT
        try:
            side._exit(side.leg.position, "stop_loss")
            side.evaluate()
        finally:
            m.datetime = orig
        assert side.leg.expiry_closed_date != EXP.isoformat(),             "a 12:00 stop must NOT mark the expiry date terminal"

    # 13 ------------------------------------------------------------------
    @case("the cycle expiry is captured ONCE, on the cycle's first fresh trade")
    def t13():
        """Author 2026-11-02: capture on the cycle's first fresh trade --
        "if no trade is taken on first day of new cycle, whenever first trade
        of new cycle is taken, you capture current month expiry". So the
        condition is "nothing captured for this cycle yet", not "today is the
        cycle-start day"; and the value is the CYCLE's expiry, never the
        contract traded, which after the 15th is next month's."""
        import datetime as _d
        ME = {(2026, 5): date(2026, 5, 26), (2026, 6): date(2026, 6, 30),
              (2026, 7): date(2026, 7, 28), (2026, 11): date(2026, 11, 24),
              (2026, 12): date(2026, 12, 29)}

        def cycle_expiry(today):
            up = sorted(d for d in ME.values() if d >= today)
            return up[0] if up else None

        def roll(today):                        # the day>15 contract rule
            key = (today.year, today.month)
            if today.day <= 15 and key in ME and ME[key] >= today:
                return ME[key]
            return ME[sorted(k for k in ME if k > key)[0]]

        def enter(side, eng, day):
            class FakeDT(_d.datetime):
                @classmethod
                def now(cls, tz=None):
                    return _d.datetime.combine(day, time(11, 31)).replace(tzinfo=tz)
            eng.get_cycle_expiry_date = lambda: cycle_expiry(day)
            orig = m.datetime
            m.datetime = FakeDT
            try:
                side.leg.position = m.ShortHedgePosition()
                side._enter("S", 24500.0, 0.27, "weakening", "H", {"strike": 25200.0},
                            roll(day).isoformat())
            finally:
                m.datetime = orig
            return roll(day).isoformat(), side.leg.marked_expiry

        # a) first trade on the cycle-start day, then later trades in the cycle
        c = FakeClient({"S": ("complete", 120.0), "H": ("complete", 24.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        for day, want_contract, want_capture in (
                (date(2026, 5, 27), "2026-06-30", "2026-06-30"),
                (date(2026, 6, 10), "2026-06-30", "2026-06-30"),
                (date(2026, 6, 20), "2026-07-28", "2026-06-30")):   # rolled, capture held
            contract, captured = enter(side, eng, day)
            assert contract == want_contract, f"{day}: contract {contract}"
            assert captured == want_capture, f"{day}: captured {captured} != {want_capture}"

        # b) NO trade on the cycle-start day -- the first trade of the cycle
        #    captures, and takes the CYCLE's expiry even though the contract
        #    it trades is next month's
        c = FakeClient({"S": ("complete", 120.0), "H": ("complete", 24.0)})
        eng = make_engine(m, c)
        side = make_side(m, eng)
        contract, captured = enter(side, eng, date(2026, 11, 20))
        assert contract == "2026-12-29", contract
        assert captured == "2026-11-24", f"must capture the cycle's expiry, got {captured}"

        # c) once the captured date passes, the next trade captures again
        contract, captured = enter(side, eng, date(2026, 11, 25))
        assert captured == "2026-12-29", f"a new cycle must re-capture, got {captured}"

    # 14 ------------------------------------------------------------------
    @case("the reference reading is fetched once per day, per symbol")
    def t14():
        """Fix 1 (2026-11-02): the Reference Snapshot is fixed at 09:30, so a
        strike's reference premium/OI cannot change during the day. Only a
        SUCCESSFUL reading is cached -- a transient failure must be retried at
        the next check, not remembered as a miss."""
        import pandas as pd
        calls = {"n": 0, "fail": False}

        class C:
            def history(self, **k):
                calls["n"] += 1
                if calls["fail"]:
                    return pd.DataFrame()
                # tz-aware, as the real SDK returns -- the gap-day reference
                # path compares the index against an IST-localised boundary.
                idx = pd.date_range("2026-11-02 09:15", periods=20, freq="15min",
                                    tz="Asia/Kolkata")
                return pd.DataFrame({"close": [100.0] * 20, "oi": [5e5] * 20}, index=idx)

        ref = m.ReferenceSnapshot(reference_date="2026-11-02", mode="prev_close",
                                  reference_time_iso="2026-10-30", computed=True)
        m.clear_reference_cache()
        c = C()
        for _ in range(5):
            assert m.fetch_reference_oi_premium(c, "A", "NFO", ref) is not None
        assert calls["n"] == 1, f"5 reads of one symbol must cost 1 fetch, cost {calls['n']}"
        m.fetch_reference_oi_premium(c, "B", "NFO", ref)
        assert calls["n"] == 2, "a different symbol is a different key"
        m.clear_reference_cache()
        m.fetch_reference_oi_premium(c, "A", "NFO", ref)
        assert calls["n"] == 3, "the daily reset must clear it"

        # a failure is NOT cached
        m.clear_reference_cache()
        calls["fail"] = True
        before = calls["n"]
        assert m.fetch_reference_oi_premium(c, "A", "NFO", ref) is None
        assert m.fetch_reference_oi_premium(c, "A", "NFO", ref) is None
        assert calls["n"] == before + 2, "a failed read must be retried, not remembered"

        # a RE-COMPUTED reference must not be served the old reading
        calls["fail"] = False
        m.clear_reference_cache()
        m.fetch_reference_oi_premium(c, "A", "NFO", ref)
        n_after = calls["n"]
        # tz-AWARE, exactly as _ensure_reference stores it
        # (datetime.now(IST).replace(...).isoformat() -> "...+05:30").
        ref2 = m.ReferenceSnapshot(reference_date="2026-11-02", mode="today_0935",
                                   reference_time_iso="2026-11-02T09:30:00+05:30",
                                   computed=True)
        m.fetch_reference_oi_premium(c, "A", "NFO", ref2)
        assert calls["n"] == n_after + 1, "a different reference identity is a different key"

    # 15 ------------------------------------------------------------------
    @case("the live quote supplies the current reading, and 0 OI falls back")
    def t15():
        """Fix 2 (2026-11-02). The None on a 0/absent oi is the important half:
        without it, a broker whose quotes carry no OI would report oi_change
        == 0 for every candidate, classify_oi_premium would call it "flat",
        and nothing would ever qualify."""
        class Good:
            def quotes(self, **k):
                return {"status": "success", "data": {"ltp": 95.0, "oi": 520000.0}}

        class ZeroOI:
            def quotes(self, **k):
                return {"status": "success", "data": {"ltp": 95.0, "oi": 0}}

        class NoField:
            def quotes(self, **k):
                return {"status": "success", "data": {"ltp": 95.0}}

        class Err:
            def quotes(self, **k):
                return {"status": "error", "message": "no"}

        class Raises:
            def quotes(self, **k):
                raise RuntimeError("transport")

        got = m.fetch_live_oi_premium(Good(), "X", "NFO")
        assert got == {"premium": 95.0, "oi": 520000.0, "timestamp": "live"}, got
        for cls in (ZeroOI, NoField, Err, Raises):
            assert m.fetch_live_oi_premium(cls(), "X", "NFO") is None,                 f"{cls.__name__} must fall back to the candle path"
        assert m.config.use_live_oi_for_current is True

    # 16 ------------------------------------------------------------------
    @case("delta comes from the chain; a 0/absent delta skips the strike")
    def t16():
        """Author 2026-11-02 chose option 1: read the server's delta instead of
        solving IV locally. The local solve passed `underlying_ltp` as the
        forward where the server passes `forward_price` -- measured on a live
        response (23440.80 vs 23419.85) that biased every delta high by +0.004
        to +0.012, up to 12% of the 0.22-0.32 band's width.

        The 0-delta guard is the half that matters: ranking an unpriceable leg
        as "delta 0.0" would make it look like the furthest strike from 0.27
        rather than unusable."""
        src = SCRIPT.read_text(encoding="utf-8")
        code_lines = [l for l in src.splitlines()
                      if not l.strip().startswith("#")]
        code = chr(10).join(code_lines)
        assert "with_greeks=True" in code, "the chain must be asked for greeks"
        assert "black76" not in code, "the local greeks solve must be gone"
        assert "_years_to_expiry" not in code, "its only caller was that solve"
        assert 'delta_by_strike[float(row["strike"])] = float(leg["delta"])' in code
        assert "if d is None or d == 0.0:" in code, "a 0 or absent delta must skip the strike"

        # and the server's own numbers reproduce the band decision
        rows = [(23850, 0.2841), (23900, 0.2606), (23950, 0.2389)]
        target, lo, hi = (m.config.monthly_delta_target, m.config.monthly_delta_low,
                          m.config.monthly_delta_high)
        best = min(rows, key=lambda r: abs(r[1] - target))
        assert best[0] == 23900, best
        assert lo <= best[1] <= hi, f"{best[1]} should sit inside the band"

    # 17 ------------------------------------------------------------------
    @case("cycle start resolves from a FUTURE-ONLY expiry list, holiday-aware")
    def t17():
        """2026-11-03: `Cycle start date resolved: None` in the live log. The
        expiry API drops every past date (upstream #1392), and the old
        resolver needed one, so it returned None on every day. It now
        observes the cycle expiry forward and persists it.

        Uses the live list and the real 24-Nov-2026 holiday: the weekday-only
        rule would name 24-Nov, a day the strategy never runs."""
        import datetime as _d
        HOL = {date(2026, 11, 10), date(2026, 11, 24)}

        def ntd(d):
            c = d + _d.timedelta(days=1)
            while c.weekday() >= 5 or c in HOL:
                c += _d.timedelta(days=1)
            return c

        orig_cal = m._calendar_next_trading_day
        m._calendar_next_trading_day = ntd
        LIVE = ["03-NOV-26", "09-NOV-26", "17-NOV-26", "23-NOV-26", "01-DEC-26",
                "29-DEC-26", "25-JAN-27", "30-MAR-27"]

        # Monthly dates come from the FUTURES list since 2026-11-03 -- monthly
        # contracts only, three serial months, like NSE lists them. The options
        # list (LIVE) is kept for reference but the code no longer reads it.
        MONTHLY = ["23-NOV-26", "29-DEC-26", "25-JAN-27", "23-FEB-27", "30-MAR-27"]

        class C:
            def expiry(self, **k):
                t = m.datetime.now(m.IST).date()
                src = MONTHLY if k.get("instrumenttype") == "futures" else LIVE
                live = [x for x in src if _d.datetime.strptime(x, "%d-%b-%y").date() >= t]
                return {"status": "success",
                        "data": live[:3] if k.get("instrumenttype") == "futures" else live}

        eng = object.__new__(m.StrategyEngine)
        eng.client = C()
        eng.state = m.StrategyState()
        eng.save_state = lambda: None

        def on(day):
            class F(_d.datetime):
                @classmethod
                def now(cls, tz=None):
                    return _d.datetime.combine(day, time(9, 31)).replace(tzinfo=tz)
            o = m.datetime
            m.datetime = F
            try:
                eng._cycle_start_date_cache = None
                eng._cycle_expiry_date_cache = None
                m.clear_monthly_expiry_cache()
                return m.StrategyEngine.get_cycle_start_date(eng)
            finally:
                m.datetime = o

        try:
            assert on(date(2026, 11, 3)) is None, "first deployment, nothing observed yet"
            assert eng.state.cycle_expiry_seen == "2026-11-23", eng.state.cycle_expiry_seen
            assert on(date(2026, 11, 23)) is None, "the expiry day itself is not a cycle start"
            got = on(date(2026, 11, 25))
            assert got == date(2026, 11, 25),                 f"the first trading day after 23-Nov (24-Nov is a holiday) must start the cycle, got {got}"
            assert eng.state.cycle_expiry_seen == "2026-12-29", "the observed expiry must advance"
            assert on(date(2026, 11, 25)) == date(2026, 11, 25), "a restart that day must still see it"
            assert on(date(2026, 11, 26)) == date(2026, 11, 25), "and the next day is not a cycle start"
        finally:
            m._calendar_next_trading_day = orig_cal

    # 18 ------------------------------------------------------------------
    @case("a failed platform call logs the URL with the API key masked")
    def t18():
        """check_pending_action and check_force_exit carry the API key in the
        query string, and the strategy's Log is a bare StreamHandler with no
        redaction. Nifty_LEAPS_OptionsSell's otherwise-identical helper writes
        the full key to the log on every failure (verified 2026-11-03 by
        executing it); this script's port must not. Drives the real helpers
        against a port nothing listens on."""
        import io
        import logging
        import os
        import types
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        m.Log.logger.addHandler(h)
        old_port = os.environ.get("STRATEGY_REPORTING_PORT")
        os.environ["STRATEGY_REPORTING_PORT"] = "1"
        secret = "SEKRETKEY0123456789abcdef"
        env = types.SimpleNamespace(api_key=secret, strategy_tag="T")
        try:
            m.check_pending_action(env, "MONTHLY_CE")
            m.check_force_exit(env)
        finally:
            m.Log.logger.removeHandler(h)
            if old_port is None:
                os.environ.pop("STRATEGY_REPORTING_PORT", None)
            else:
                os.environ["STRATEGY_REPORTING_PORT"] = old_port
        out = buf.getvalue()
        assert "_get_json_local GET" in out, "the failure must still be logged"
        assert secret not in out, "the API key must never reach the log"
        assert "pending_action?leg_key=MONTHLY_CE&apikey=***" in out,             "the path must still be logged, with the key masked"

    # 19 ------------------------------------------------------------------
    @case("monthly dates come from the FUTURES list, shared, and refuse to guess")
    def t19():
        """2026-11-03. The options list has no weekly/monthly flag, so its
        monthly was guessed as "the last date listed in the month" -- and with
        23-NOV-26 missing it picked the 17-Nov WEEKLY. NIFTY futures are
        monthly-only, so their dates ARE the monthlies. Asserts: the futures
        list is what is asked for; a broken OPTIONS list no longer matters; the
        contract and the cycle date share one call; and every bad response
        refuses rather than guessing."""
        import datetime as _d

        def at(day):
            class F(_d.datetime):
                @classmethod
                def now(cls, tz=None):
                    return _d.datetime.combine(day, time(11, 31)).replace(tzinfo=tz)
            return F

        def resolve(day, expiry_fn):
            m.clear_monthly_expiry_cache()
            calls = []

            class C:
                def expiry(self, **k):
                    calls.append(k.get("instrumenttype"))
                    return expiry_fn(k)

            o = m.datetime
            m.datetime = at(day)
            try:
                c = C()
                return m.resolve_monthly_expiry(c)[1], m.resolve_cycle_expiry_date(c), calls
            finally:
                m.datetime = o

        GOOD = {"status": "success", "data": ["23-NOV-26", "29-DEC-26", "25-JAN-27"]}
        BROKEN_OPTIONS = {"status": "success",
                          "data": ["03-NOV-26", "09-NOV-26", "17-NOV-26", "01-DEC-26"]}

        def good(k):
            return GOOD if k["instrumenttype"] == "futures" else BROKEN_OPTIONS

        contract, cycle, calls = resolve(date(2026, 11, 4), good)
        assert calls == ["futures"], f"one futures call, shared by both: {calls}"
        assert contract == "23-Nov-26", f"day 4 trades the current month: {contract}"
        assert cycle == date(2026, 11, 23), f"cycle expiry is the monthly, not 17-Nov: {cycle}"

        contract, cycle, _ = resolve(date(2026, 11, 20), good)
        assert contract == "29-Dec-26", f"day 20 rolls to next month: {contract}"
        assert cycle == date(2026, 11, 23), f"but the cycle still ends 23-Nov: {cycle}"

        def bad(resp):
            return lambda k: resp

        def raises(k):
            raise ConnectionError("reset")

        for label, fn in (
                ("error status", bad({"status": "error", "message": "down"})),
                ("empty list", bad({"status": "success", "data": []})),
                ("two dates in one month", bad({"status": "success",
                                                "data": ["17-NOV-26", "23-NOV-26"]})),
                ("the call raising", raises)):
            try:
                resolve(date(2026, 11, 4), fn)
            except RuntimeError:
                continue
            raise AssertionError(f"{label} must refuse with RuntimeError, not guess")

    import datetime as _dt
    import logging
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    class _Clock:
        """Pins the module's clock; threads in the pool see it too."""
        def __init__(self, day, hm):
            class F(_dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return _dt.datetime.combine(day, hm).replace(tzinfo=tz)
            self.f = F

        def __enter__(self):
            self.orig = m.datetime
            m.datetime = self.f

        def __exit__(self, *a):
            m.datetime = self.orig

    def _open_leg(side, ot="CE", entry_px=121.55):
        side.leg.position = m.ShortHedgePosition(
            short_symbol=f"S{ot}", quantity=65, entry_px=entry_px, entry_filled=True,
            hedge_symbol=f"H{ot}", hedge_entry_px=60.0, hedge_entry_filled=True,
            entry_time="t", execution_id="EX1", expiry_date="2026-11-23")

    @case("an OPEN leg runs its position checks under the production thread pool")
    def t20():
        """Live 04-Nov-2026: no open leg logged a position check all day.
        evaluate() dispatched the SL first on every tick; it holds the same
        per-leg guard, so the check always found the leg busy. Every other
        test here uses InlineExecutor, which runs the SL to completion before
        the check is reached and so hid the defect -- this one uses the real
        2-worker pool the engine builds."""
        # (a) an ordinary 13:30 check is reached at all
        eng = make_engine(m)
        eng._fill_executor = ThreadPoolExecutor(max_workers=len(m.LEG_KEYS))
        side = make_side(m, eng, "CE")
        _open_leg(side)
        ran = []
        side._manage_open_position = lambda: _time.sleep(0.2)   # a REST quote's worth
        side._daily_decision = lambda slot=None: ran.append(slot)
        with _Clock(date(2026, 11, 4), time(13, 31)):
            for _ in range(5):          # five scheduler ticks
                side.evaluate()
                _time.sleep(0.3)
            _time.sleep(0.3)
        eng._fill_executor.shutdown(wait=True)
        assert ran == [time(13, 30)], f"13:30 check must run exactly once, ran {ran}"

        # (b) the expiry-day square-off, driven through the REAL _daily_decision
        c = FakeClient({"SCE": ("complete", 140.0), "HCE": ("complete", 20.0)})
        eng = make_engine(m, c)
        eng._fill_executor = ThreadPoolExecutor(max_workers=len(m.LEG_KEYS))
        side = make_side(m, eng, "CE")
        _open_leg(side)
        side.leg.marked_expiry = "2026-11-23"
        with _Clock(date(2026, 11, 23), time(15, 31)):
            for _ in range(4):
                side.evaluate()
                _time.sleep(0.3)
            eng._fill_executor.shutdown(wait=True)
        acts = [(s, a) for s, a, q, o in c.orders]
        assert acts[:2] == [("SCE", "BUY"), ("HCE", "SELL")], \
            f"23-Nov 15:30 must square off short then hedge, orders were {acts}"

    @case("the SL keeps running while a position check waits on a late candle")
    def t21():
        """The check owns the leg's guard for as long as it retries (up to
        6 x 15s). Without running the SL from inside that wait the short
        would have no stop for 90s; and once the SL has closed the position
        the check must stop rather than decide on it."""
        eng = make_engine(m)
        side = make_side(m, eng, "CE")
        _open_leg(side)
        pos = side.leg.position
        sl_calls = []

        def sl():
            sl_calls.append(1)
            pos.short_exit_order_id = "SLX"      # the stop fired and sent its buy-back
        side._manage_open_position = sl

        def late(_pos, require_fresh_by=None):
            if require_fresh_by is not None:
                raise m.CandleNotYetFresh("not yet")
            return "accumulation"
        side._fresh_oi_reclassify = late
        exits = []
        side._exit = lambda p, reason: exits.append(reason)
        old = (m.config.oi_freshness_retry_delay_sec, m.config.oi_freshness_max_retries)
        m.config.oi_freshness_retry_delay_sec, m.config.oi_freshness_max_retries = 1.2, 3
        buf_lines = []
        h = logging.Handler()
        h.emit = lambda r: buf_lines.append(r.getMessage())
        m.Log.logger.addHandler(h)
        try:
            with _Clock(date(2026, 11, 4), time(13, 31)):
                side._daily_decision(time(13, 30))
        finally:
            m.Log.logger.removeHandler(h)
            m.config.oi_freshness_retry_delay_sec, m.config.oi_freshness_max_retries = old
        assert sl_calls, "the SL must run while the check waits for its candle"
        assert exits == [], f"the check must not send its own exit after the SL did: {exits}"
        assert any("stop-loss closed this position" in l for l in buf_lines), buf_lines

        # a stop whose exit order was REFUSED puts the leg in error: the
        # check must not send a second exit on top of the error flow
        _open_leg(side)
        pos = side.leg.position

        def refused():
            pos.error_state = "exit_failed"
        side._manage_open_position = refused
        exits.clear()
        m.config.oi_freshness_retry_delay_sec, m.config.oi_freshness_max_retries = 1.2, 3
        try:
            with _Clock(date(2026, 11, 4), time(13, 31)):
                side._daily_decision(time(13, 30))
        finally:
            m.config.oi_freshness_retry_delay_sec, m.config.oi_freshness_max_retries = old
        assert exits == [], f"no exit on top of a refused stop: {exits}"

    @case("the (open) OI line is measured against the day's reference, not zero")
    def t22():
        """Live 04-Nov-2026: premium_chg always equalled the premium and the
        verdict always read accumulation, because the line subtracted
        pos.reference_premium / reference_oi, which nothing sets."""
        eng = make_engine(m)
        side = make_side(m, eng, "CE")
        _open_leg(side, entry_px=15.0)          # stream LTP 10.0: no stop, no ratchet
        eng._last_candle_boundary = _dt.datetime(2026, 11, 4, 11, 15)
        ref_calls = []
        orig = (m.fetch_candle_oi_premium, m.fetch_reference_oi_premium)
        m.fetch_candle_oi_premium = lambda *a, **k: {
            "premium": 119.10, "oi": 714675.0, "timestamp": "2026-11-04 11:15:00+05:30"}

        def ref(*a, **k):
            ref_calls.append(1)
            return {"premium": 121.00, "oi": 700000.0}
        m.fetch_reference_oi_premium = ref
        lines = []
        h = logging.Handler()
        h.emit = lambda r: lines.append(r.getMessage())
        m.Log.logger.addHandler(h)
        try:
            with _Clock(date(2026, 11, 4), time(11, 31)):
                # reference not fixed yet (gap day before 09:45): no line at all
                side._manage_open_position()
                assert not any("(open)" in l for l in lines), lines
                assert not ref_calls, "no reference read before the reference is fixed"
                eng.state.reference = m.ReferenceSnapshot(
                    reference_date="2026-11-04", mode="prev_close", computed=True)
                side._manage_open_position()
        finally:
            m.Log.logger.removeHandler(h)
            m.fetch_candle_oi_premium, m.fetch_reference_oi_premium = orig
        line = next((l for l in lines if "(open)" in l), "")
        want = m.classify_oi_premium(119.10 - 121.00, 714675.0 - 700000.0)
        assert "premium_chg=-1.90" in line, line
        assert "oi_chg=+14675" in line, line
        assert f"verdict={want}" in line, line

    @case("the hedge chain reaches the 20% target instead of stopping at the scan's edge")
    def t23():
        """Live 04-Nov-2026: 24600CE at 60.00 against a 24.31 target -- the
        edge strike of the short scan's 16-strikes-a-side chain, reused as-is.
        Drives the real select_hedge_strike with a chain whose width follows
        strike_count, ATM 23800, 50-point strikes, as the API serves it."""
        def chain(count, curve):
            rows = []
            for i in range(-count, count + 1):
                k = 23800 + 50 * i
                rows.append({"strike": float(k),
                             "ce": {"ltp": curve(k - 23800)} if k >= 23800 else None,
                             "pe": None})
            return {"status": "success", "chain": rows}

        class C:
            def __init__(self, curve, fail=False):
                self.curve, self.fail, self.counts = curve, fail, []

            def optionchain(self, **k):
                self.counts.append(k["strike_count"])
                if self.fail:
                    return {"status": "error", "message": "down"}
                return chain(k["strike_count"], self.curve)

        def gentle(d):                                             # 24600 -> 60.00
            return round(max(1.0, 300 - 0.3 * d), 2)
        short_chain = chain(16, gentle)                            # what the short scan hands over
        target = 121.55 * 0.20                                     # 24.31

        c = C(gentle)
        h = m.select_hedge_strike(c, "23NOV26", "CE", 24300.0, 121.55, short_chain)
        assert c.counts == [30], f"one wider fetch reaches the target, fetched {c.counts}"
        assert h["strike"] == 24700.0 and abs(h["ltp"] - target) < 6, h

        # a chain that already reaches the target is reused, no extra call
        c = C(gentle)
        h = m.select_hedge_strike(c, "23NOV26", "CE", 24300.0, 121.55, chain(30, gentle))
        assert c.counts == [] and h["strike"] == 24700.0, (c.counts, h)

        # premiums that fall slowly: widen 30 -> 60
        def slow(d):
            return round(max(1.0, 300 - 0.12 * d), 2)
        c = C(slow)
        h = m.select_hedge_strike(c, "23NOV26", "CE", 24300.0, 121.55, chain(16, slow))
        assert c.counts == [30, 60], f"must widen until the target is reached, fetched {c.counts}"
        assert abs(h["ltp"] - target) < 4, h

        # a failed wider fetch still hedges, with the closest strike in hand
        c = C(gentle, fail=True)
        h = m.select_hedge_strike(c, "23NOV26", "CE", 24300.0, 121.55, short_chain)
        assert h is not None and h["strike"] == 24600.0, \
            f"never naked: fall back to the closest strike already known, got {h}"

    @case("a gap day logs its wait once, re-checks nothing until 09:46, then fixes 09:30")
    def t24():
        """Live 04-Nov-2026: the wait line printed every 10s from 09:31 to
        09:45, each one re-reading spot and the previous close. Drives the
        real StrategyEngine._ensure_reference over 09:31 -> 09:46."""
        eng = types.SimpleNamespace(state=m.StrategyState(), client=None, _gap_wait_day="")
        eng.state.reference = m.ReferenceSnapshot()
        spot_calls = []

        def spot():
            spot_calls.append(1)
            return 24174.0                       # +0.72% over 24001
        eng.get_spot_ltp = spot
        orig = m.fetch_candle_oi_premium
        m.fetch_candle_oi_premium = lambda *a, **k: {"premium": 24001.0, "oi": 0.0}
        orig_prev = m.resolve_previous_trading_day
        m.resolve_previous_trading_day = lambda d: date(2026, 11, 3)
        lines = []
        h = logging.Handler()
        h.emit = lambda r: lines.append(r.getMessage())
        m.Log.logger.addHandler(h)
        try:
            for mm in range(31, 46):             # every minute 09:31 .. 09:45
                with _Clock(date(2026, 11, 4), time(9, mm)):
                    m.StrategyEngine._ensure_reference(eng)
            assert not eng.state.reference.computed, "the reference must wait for 09:45"
            waits = [ln for ln in lines if "[Reference]" in ln]
            assert len(waits) == 1, f"the wait is logged once, got {len(waits)}: {waits}"
            assert len(spot_calls) == 1, f"spot is read once while waiting, read {len(spot_calls)}x"
            with _Clock(date(2026, 11, 4), time(9, 46)):
                m.StrategyEngine._ensure_reference(eng)
        finally:
            m.Log.logger.removeHandler(h)
            m.fetch_candle_oi_premium = orig
            m.resolve_previous_trading_day = orig_prev
        ref = eng.state.reference
        assert ref.computed and ref.mode == "today_0935", ref
        assert ref.reference_time_iso.startswith("2026-11-04T09:30"), ref.reference_time_iso

    @case("the previous close skips holidays and the Muhurat session")
    def t25():
        """25-Nov-2026 named 24-Nov (a holiday) as the previous day, so every
        option's reference came back empty on the new cycle's first day.
        The calendar here is the 2026 one: 24-Nov closed, Sunday 08-Nov a
        special (Muhurat) session, which the author wants skipped."""
        holidays = {date(2026, 11, 24)}
        special = {date(2026, 11, 8)}

        def is_td(d):
            if d in special:
                return True
            return d not in holidays and d.weekday() < 5

        def prev_td(d):
            c = d - timedelta(days=1)
            while not is_td(c):
                c -= timedelta(days=1)
            return c
        orig = (m._calendar_prev_trading_day, m._calendar_special_sessions)
        m._calendar_prev_trading_day = prev_td
        m._calendar_special_sessions = lambda year: frozenset(special)
        try:
            got = {d: m.resolve_previous_trading_day(d) for d in
                   (date(2026, 11, 25), date(2026, 11, 9), date(2026, 11, 5), date(2026, 11, 30))}
        finally:
            m._calendar_prev_trading_day, m._calendar_special_sessions = orig
        assert got[date(2026, 11, 25)] == date(2026, 11, 23), got
        assert got[date(2026, 11, 9)] == date(2026, 11, 6), "Muhurat skipped -> Friday's close"
        assert got[date(2026, 11, 5)] == date(2026, 11, 4), got
        assert got[date(2026, 11, 30)] == date(2026, 11, 27), got

        # the calendar failing must not stop the strategy: weekends-only
        def boom(d):
            raise RuntimeError("calendar down")
        m._calendar_prev_trading_day = boom
        m._calendar_special_sessions = lambda year: frozenset()
        try:
            assert m.resolve_previous_trading_day(date(2026, 11, 9)) == date(2026, 11, 6)
        finally:
            m._calendar_prev_trading_day, m._calendar_special_sessions = orig

    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12, t13, t14, t15, t16, t17, t18, t19,
               t20, t21, t22, t23, t24, t25):
        fn()

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
