"""
Synthetic 5-minute candle data for the OI Weekly Buy + Monthly Sell
simulation (test_oi_weekly_monthly_simulation.py). Two trading days:

  Day 1 (2026-08-03, small gap -> Reference Time = previous day's close):
    Walks WEEKLY_CE through every quadrant of the OI table in sequence
    (Long Build-up -> entry, then Short Build-up x2 -> exit), independently
    exercises WEEKLY_PE with its own separate data (never touching CE's
    numbers), and walks MONTHLY_CE/MONTHLY_PE through all four
    gate x confirmation combinations (open x open, open x closed,
    closed x open, closed x closed) to prove the AND-of-two-same-side-checks
    and the never-cross-references-the-other-side properties both hold.

  Day 2 (2026-08-04, large gap -> Reference Time = today's 09:35 candle):
    Confirms the OTHER reference mode computes correctly, and that Day 1's
    state (positions, reference, trade counts) doesn't leak into Day 2.

All symbols/strikes are illustrative, not resolved from a real option
chain -- the simulation's FakeClient serves these directly, so the exact
strike numbers don't need to correspond to real NIFTY levels.
"""


import pytz

IST = pytz.timezone("Asia/Kolkata")

WEEKLY_CE_SYMBOL = "NIFTY06AUG2624300CE"
WEEKLY_PE_SYMBOL = "NIFTY06AUG2623700PE"
# Monthly strikes are chosen to land on the FIRST step select_monthly_delta_strike()
# scans (ATM_100 +/- 100), given Day1 spot ~24,072 -> atm_100 = round(24072/100)*100
# = 24,100: CE scans 24,200 first (direction +1, step 1); PE scans 24,000 first
# (direction -1, step 1). Keeping this to step 1 means the FakeClient only needs a
# delta entry for exactly these two symbols -- the scan finds them immediately.
MONTHLY_CE_SYMBOL = "NIFTY27AUG2624200CE"
MONTHLY_PE_SYMBOL = "NIFTY27AUG2624000PE"

DAY0 = "2026-07-31"   # previous trading day (Friday) -- only its close matters
DAY1 = "2026-08-03"   # Monday, small gap
DAY2 = "2026-08-04"   # Tuesday, large gap


def _ts(day: str, hhmm: str) -> str:
    return f"{day} {hhmm}:00+05:30"


def _candle(day, hhmm, close, oi):
    return {"timestamp": _ts(day, hhmm), "open": close, "high": close, "low": close,
            "close": close, "volume": 1000, "oi": oi}


# ---------------------------------------------------------------------------
# NIFTY SPOT -- only the previous close (Day0 15:25 candle, last of the
# session) and each day's 09:30 mark matter for the Reference Engine's gap
# calculation. Day1: 24,000 -> 24,072 (+0.30%, <= 0.5% threshold -> small
# gap). Day2: 24,050 -> 24,254 (+0.85%, > 0.5% threshold -> large gap).
# ---------------------------------------------------------------------------
SPOT_CANDLES = {
    DAY0: [_candle(DAY0, "15:25", 24000.0, 0)],
    DAY1: [
        _candle(DAY1, "09:15", 24060.0, 0),
        _candle(DAY1, "09:20", 24068.0, 0),
        _candle(DAY1, "09:25", 24070.0, 0),
        _candle(DAY1, "09:30", 24072.0, 0),
    ] + [_candle(DAY1, f"{h:02d}:{m:02d}", 24072.0, 0)
         for h in (9, 10, 11, 12, 13, 14, 15) for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
         if (h, m) > (9, 30) and (h, m) <= (15, 30)],
    DAY2: [
        _candle(DAY2, "09:15", 24040.0, 0),
        _candle(DAY2, "09:20", 24180.0, 0),
        _candle(DAY2, "09:25", 24230.0, 0),
        _candle(DAY2, "09:30", 24254.0, 0),   # gap check candle: (24254-24000)/24000 = +1.06%... see note
        _candle(DAY2, "09:35", 24260.0, 0),   # the day's fixed Reference Time (large-gap case)
    ] + [_candle(DAY2, f"{h:02d}:{m:02d}", 24260.0, 0)
         for h in (9, 10, 11, 12, 13, 14, 15) for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
         if (h, m) > (9, 35) and (h, m) <= (15, 30)],
}


# ---------------------------------------------------------------------------
# WEEKLY CE -- Day 1 walk: Long Build-up (accumulation) -> ENTRY, then
# Short Build-up x2 consecutive (weakening) -> EXIT after 2 opposite
# candles. Reference = Day0 15:25 candle (premium=100, oi=50,000).
# ---------------------------------------------------------------------------
WEEKLY_CE_CANDLES = {
    DAY0: [_candle(DAY0, "15:25", 100.0, 50_000)],
    DAY1: [
        # Every candle below is compared against the FIXED Day-0 reference
        # (premium=100, oi=50,000) for as long as the leg stays open --
        # Strike Freeze (plan doc SS1.5) re-evaluates the table against the
        # entry's own Reference every candle, NOT against the previous
        # candle. So "verdict" below depends on each row's absolute
        # premium/oi vs. (100, 50,000), not on the row-to-row delta.
        _candle(DAY1, "09:30", 100.0, 50_000),                # flat vs reference -- no signal yet
        _candle(DAY1, "09:35", 112.0, 54_000),                 # >100, >50k -> Long Build-up -> Accumulation -> ENTRY
        _candle(DAY1, "09:40", 118.0, 56_000),                 # >100, >50k -> still Accumulation -> streak resets to 0
        _candle(DAY1, "09:45", 95.0, 58_000),                  # <100, >50k -> Short Build-up -> Weakening (1st)
        _candle(DAY1, "09:50", 90.0, 60_000),                  # <100, >50k -> Weakening (2nd) -> EXIT (opposite_signal)
        # ---- SEQUENTIAL SECOND CYCLE (2026-08-07 request: "weekly PE buy
        # then exit then weekly CE buy... similar for selling side") ----
        # CE stays flat/weakening (no entry -- weakening isn't the Weekly
        # trigger) through the window WEEKLY_PE's own second cycle plays out
        # below (10:00-10:05), then freshly enters AFTER PE has fully
        # exited again, demonstrating one side handing off to the other
        # sequentially, not just simultaneously (already proven at 09:35).
        _candle(DAY1, "10:10", 130.0, 58_000),                 # >100, >50k -> Accumulation -> ENTRY (2nd time)
        _candle(DAY1, "10:15", 65.0, 62_000),                  # <100, >50k -> Weakening (1st)
        _candle(DAY1, "10:20", 55.0, 64_000),                  # <100, >50k -> Weakening (2nd) -> EXIT (2nd time)
    ],
}

# ---------------------------------------------------------------------------
# WEEKLY PE -- Day 1, entirely independent data from CE. Shows Short
# Covering (premium up, OI down = Accumulation) at 09:35 -> ENTRY, proving
# PE's own trigger fires off its OWN data at the same time CE's own data is
# doing something unrelated (CE was also accumulating at 09:35 above --
# both can and do fire independently, same cycle, per plan doc SS1.3). Then
# walked through to a +100% PROFIT TARGET exit at 09:55 (entry_px=99 ->
# target >= 198), immediately followed by a same-cycle RE-ENTRY (spec SS5:
# "immediately re-select a fresh OTM1 strike... and re-enter if that side's
# signal still holds") -- 09:55's own premium/OI vs. the (unchanged) Day-0
# reference is ALSO Short Covering/Accumulation, so the re-entry fires.
# ---------------------------------------------------------------------------
WEEKLY_PE_CANDLES = {
    DAY0: [_candle(DAY0, "15:25", 90.0, 40_000)],
    DAY1: [
        _candle(DAY1, "09:30", 90.0, 40_000),
        # premium UP, OI DOWN -> Short Covering -> Accumulation -> ENTRY
        _candle(DAY1, "09:35", 99.0, 37_000),
        _candle(DAY1, "09:40", 100.0, 36_500),
        _candle(DAY1, "09:45", 101.0, 36_000),
        _candle(DAY1, "09:50", 102.0, 35_500),
        # premium 200 >= 2x entry_px(99) -> +100% profit target hit -> EXIT,
        # then immediate re-entry check using this SAME candle's own numbers:
        # premium UP vs ref(90), OI DOWN vs ref(40,000) -> Short Covering ->
        # Accumulation -> RE-ENTER at the same strike.
        _candle(DAY1, "09:55", 200.0, 38_000),
        # ---- SEQUENTIAL SECOND CYCLE -- PE's re-entered position now
        # exits again via 2 consecutive weakening (still vs. the SAME fixed
        # Day-0 reference, 90/40,000) -- this is the "PE... exit" half of
        # "weekly PE buy then exit then weekly CE buy". WEEKLY_CE's own
        # fresh entry (above) starts only at 10:10, strictly AFTER this. ----
        _candle(DAY1, "10:00", 60.0, 42_000),                  # <90, >40k -> Weakening (1st)
        _candle(DAY1, "10:05", 50.0, 44_000),                  # <90, >40k -> Weakening (2nd) -> EXIT (2nd time)
    ],
}

# ---------------------------------------------------------------------------
# MONTHLY CE -- Day 1. Gate (Weekly CE's own signal) is Weakening starting
# at 09:45 (see WEEKLY_CE_CANDLES above: 09:45 candle is the 1st weakening
# reading). Monthly CE's OWN strike also shows Weakening at 09:45 -> BOTH
# confirmations true -> ENTRY (Sell). This directly tests the "gate open x
# confirm open -> enter" combination. Then walked through to its OWN exit:
# 2 consecutive candles showing the OPPOSITE verdict (Accumulation) on this
# leg's own frozen strike, vs. its own Day-0 reference (250, 80,000) --
# spec SS12: this is the ONLY exit condition for Monthly Sell, no
# profit/SL.
# ---------------------------------------------------------------------------
MONTHLY_CE_CANDLES = {
    DAY0: [_candle(DAY0, "15:25", 250.0, 80_000)],
    DAY1: [
        _candle(DAY1, "09:30", 250.0, 80_000),
        _candle(DAY1, "09:35", 260.0, 84_000),   # weekly gate not yet weakening -- no entry check fires
        # 09:45: weekly CE gate = weakening (confirmation #1 true this
        # candle). Monthly CE's own premium DOWN, OI UP -> Short Build-up
        # -> Weakening (confirmation #2 true) -> SELL Monthly CE.
        _candle(DAY1, "09:45", 240.0, 88_000),
        # >250, >80,000 -> Long Build-up -> Accumulation (opposite of the
        # entry's own Weakening trigger) -- 1st consecutive opposite candle.
        _candle(DAY1, "09:50", 260.0, 90_000),
        # >250, >80,000 again -> Accumulation -- 2nd consecutive opposite
        # candle -> EXIT (opposite_signal), per consecutive_opposite_exit=2.
        _candle(DAY1, "09:55", 270.0, 95_000),
        # ---- SEQUENTIAL SECOND CYCLE ("similar for selling side"):
        # gated by WEEKLY_CE's own SECOND weakening window (10:15/10:20,
        # after CE's own fresh 10:10 re-entry above) -- Monthly CE re-enters
        # fresh (it's been flat since its 09:55 exit) at 10:15, then exits
        # again via 2 consecutive accumulation vs. the SAME Day-0 reference. ----
        # 10:15: weekly CE gate = weakening (confirmation #1, CE's own 1st
        # weakening candle of its second cycle). Monthly CE's own <250,>80k
        # -> Weakening (confirmation #2) -> SELL Monthly CE (2nd time).
        _candle(DAY1, "10:15", 235.0, 105_000),
        _candle(DAY1, "10:25", 270.0, 110_000),  # >250,>80k -> Accumulation (1st opposite)
        _candle(DAY1, "10:30", 280.0, 115_000),  # >250,>80k -> Accumulation (2nd) -> EXIT (2nd time)
    ],
}

# ---------------------------------------------------------------------------
# DAY 3 -- GAP DOWN case (2026-08-05, Wednesday; previous trading day is
# Day 2, 2026-08-04, whose spot closed at 24,260). Day 3's 09:30 spot is
# 24,066 -- (24066-24260)/24260 = -0.80%, magnitude > 0.5% threshold, and
# NEGATIVE this time (Day 2 covered a positive large gap) -> Reference Time
# = today's 09:35 candle, same as Day 2's mode but proving the DOWN
# direction works identically (the Reference Engine's gap check is
# magnitude-only, |Gap%|, so this also incidentally guards against a
# sign-handling bug that a gap-up-only test could never catch). WEEKLY_PE
# walks Long Build-up (a quadrant no other day's PE data has exercised
# live -- Day 1's PE used Short Covering) off this 09:35 reference -> ENTRY.
# ---------------------------------------------------------------------------
DAY3 = "2026-08-05"

SPOT_CANDLES_DAY3 = {
    DAY3: [
        _candle(DAY3, "09:15", 24200.0, 0),
        _candle(DAY3, "09:20", 24140.0, 0),
        _candle(DAY3, "09:25", 24090.0, 0),
        _candle(DAY3, "09:30", 24066.0, 0),   # gap check candle: -0.80% vs Day2's 24,260 close
        _candle(DAY3, "09:35", 24060.0, 0),   # the day's fixed Reference Time (large-gap case)
        _candle(DAY3, "09:40", 24055.0, 0),
        _candle(DAY3, "15:25", 24080.0, 0),   # Day 3's own close -- Day 4's prev_close reference
    ],
}

# Same PE strike as Day 1 (23,700) -- Day 3's spot (~24,066) still sits in
# the same [23,700, 24,300] gap of the weekly option-chain ladder.
WEEKLY_PE_CANDLES_DAY3 = {
    DAY3: [
        _candle(DAY3, "09:35", 80.0, 30_000),   # the day's Reference Time itself
        # premium UP, OI UP -> Long Build-up -> Accumulation -> ENTRY
        _candle(DAY3, "09:40", 92.0, 34_000),
        # Day 3's own session close -- becomes Day 4's previous-day-close
        # reference for this strike (Day 4 uses the small-gap/prev_close path).
        _candle(DAY3, "15:25", 95.0, 36_000),
    ],
}

# WEEKLY_CE's own Day 3 close (no CE activity on Day 3 itself in this
# simulation -- this candle exists purely to give Day 4's prev_close
# reference something real to read).
WEEKLY_CE_CANDLES_DAY3_CLOSE = {
    DAY3: [_candle(DAY3, "15:25", 105.0, 52_000)],
}


# ---------------------------------------------------------------------------
# DAY 4 -- SMALL GAP DOWN case (2026-08-06, Thursday; previous trading day
# is Day 3, spot close 24,080). Day 4's 09:30 spot is 23,960 --
# (23960-24080)/24080 = -0.50%, at/under the 0.5% threshold and NEGATIVE
# (Day 1 covered a small gap UP) -> Reference Time = previous day's close,
# proving the small-gap path also works correctly for a DOWN move (a
# same-direction-only test suite could hide a sign bug in the `<=` gap
# check the same way an up-only large-gap test could).
#
# Also covers two logical paths no earlier day exercises:
#   - STREAK INTERRUPTION on WEEKLY_CE: weakening(1) -> accumulation
#     (resets to 0) -> weakening(1) -> weakening(2) -> exit. Distinguishes a
#     correct CONSECUTIVE counter from a buggy cumulative one -- a cumulative
#     counter would wrongly exit one candle earlier (it would reach "2"
#     counting the pre-reset weakening candle too).
#   - UNIVERSAL EXIT TIME on WEEKLY_PE: entered early, held with an
#     unchanging (perpetually-accumulating-vs-reference) reading all day so
#     no OI-based exit or profit-target ever fires, then force-closed at
#     15:15 purely by the clock.
# ---------------------------------------------------------------------------
# Uses a DIFFERENT weekly expiry ("13-Aug-26") than Days 1-3 ("06-Aug-26"):
# 2026-08-06 (Day 4's own date, chosen to land on Day 3 + 1) happens to BE
# that earlier expiry's own date, which correctly triggers the strategy's
# own "roll to next expiry if today is expiry day" safety logic (see
# resolve_weekly_expiry) -- rather than fight that (correct) behavior, Day
# 4 simulates trading under the NEXT weekly expiry instead, same strikes.
DAY4 = "2026-08-06"
WEEKLY_CE_SYMBOL_DAY4 = "NIFTY13AUG2624300CE"
WEEKLY_PE_SYMBOL_DAY4 = "NIFTY13AUG2623700PE"

SPOT_CANDLES_DAY4 = {
    DAY4: [
        _candle(DAY4, "09:15", 24010.0, 0),
        _candle(DAY4, "09:20", 23990.0, 0),
        _candle(DAY4, "09:25", 23970.0, 0),
        _candle(DAY4, "09:30", 23960.0, 0),   # gap check candle: -0.50% vs Day3's 24,080 close
    ] + [_candle(DAY4, f"{h:02d}:{m:02d}", 23960.0, 0)
         for h in (9, 10, 11, 12, 13, 14, 15) for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
         if (h, m) > (9, 30) and (h, m) <= (15, 15)],
}

# Same CE strike as Day 1 (24,300) -- Day 4's spot (~23,960) still sits in
# the same option-chain gap.
WEEKLY_CE_CANDLES_DAY4 = {
    DAY4: [
        _candle(DAY4, "09:30", 105.0, 52_000),                 # flat vs Day3 close reference -- no signal
        _candle(DAY4, "09:35", 115.0, 55_000),                 # >105, >52k -> Long Build-up -> Accumulation -> ENTRY
        _candle(DAY4, "09:40", 90.0, 58_000),                  # <105, >52k -> Short Build-up -> Weakening (1st)
        _candle(DAY4, "09:45", 120.0, 60_000),                 # >105, >52k -> Long Build-up -> Accumulation -> streak RESETS to 0
        _candle(DAY4, "09:50", 95.0, 62_000),                  # <105, >52k -> Weakening (1st, after reset)
        _candle(DAY4, "09:55", 92.0, 64_000),                  # <105, >52k -> Weakening (2nd) -> EXIT (opposite_signal)
    ],
}

WEEKLY_PE_CANDLES_DAY4 = {
    DAY4: [
        _candle(DAY4, "09:30", 95.0, 36_000),                  # flat vs Day3 close reference -- no signal
        _candle(DAY4, "09:35", 105.0, 40_000),                 # >95, >36k -> Long Build-up -> Accumulation -> ENTRY
        # Last candle of the day for this symbol -- every later cycle's
        # history() call returns this SAME candle as "latest closed" (no
        # newer data), which keeps reading as the identical Accumulation
        # verdict vs. the fixed reference every time (resets any streak to
        # 0, never reaches 2 consecutive weakening) and never approaches
        # the +100% profit target (premium never moves again) -- isolating
        # the 15:15 universal exit time as the ONLY thing that can close
        # this leg in this simulation.
        _candle(DAY4, "09:40", 106.0, 41_000),
    ],
}

# ---------------------------------------------------------------------------
# MONTHLY PE -- Day 1. Tests "gate open x confirm CLOSED -> no entry":
# Weekly PE's own gate signal is Accumulation the whole window (see
# WEEKLY_PE_CANDLES -- it never goes to Weakening in this simulation), so
# the Monthly PE gate (confirmation #1) never opens at all -- Monthly PE's
# own strike shows Weakening-looking numbers at 09:45 (which WOULD satisfy
# confirmation #2 in isolation), but since confirmation #1 never opens, no
# order should ever be placed for MONTHLY_PE in this simulation. This is
# the "closed x open -> no entry" case.
# ---------------------------------------------------------------------------
MONTHLY_PE_CANDLES = {
    DAY0: [_candle(DAY0, "15:25", 230.0, 70_000)],
    DAY1: [
        _candle(DAY1, "09:30", 230.0, 70_000),
        _candle(DAY1, "09:35", 225.0, 73_000),
        _candle(DAY1, "09:45", 210.0, 76_000),   # own-side "weakening"-shaped, but gate closed until 10:00 (below)
        # ---- SEQUENTIAL SECOND CYCLE ("similar for selling side"):
        # gated by WEEKLY_PE's own SECOND weakening window (10:00/10:05,
        # PE's own 2nd exit, see WEEKLY_PE_CANDLES) -- the gate that stayed
        # closed through 09:50 (proven by test_day1_monthly_pe_never_enters_
        # because_gate_never_opens, which only runs to 09:50) finally opens
        # here. Monthly PE's own <230 premium / >70k OI -> Weakening
        # (confirmation #2) -> SELL Monthly PE.
        _candle(DAY1, "10:00", 200.0, 82_000),
        _candle(DAY1, "10:15", 250.0, 88_000),   # >230,>70k -> Accumulation (1st opposite)
        _candle(DAY1, "10:20", 260.0, 92_000),   # >230,>70k -> Accumulation (2nd) -> EXIT
    ],
}


# ---------------------------------------------------------------------------
# DAY 2 -- large gap (Reference Time = today 09:35). Minimal candle set:
# just enough to prove the Reference Engine computes `mode="today_0935"`
# correctly and that no Day-1 state (positions, trade counts) carries over.
# WEEKLY_CE again walks Long Build-up -> entry, this time referenced off
# the 09:35 candle instead of the previous day's close. Day 2's spot
# (~24,260) still sits in the same [23,700, 24,300] gap in the weekly
# option-chain ladder as Day 1's spot did, so OTM1 CE correctly resolves to
# the SAME strike as Day 1 (24,300) -- same symbol, freshly re-entered
# since Day 1's leg was already exited and the day reset cleared it.
# ---------------------------------------------------------------------------
WEEKLY_CE_CANDLES_DAY2 = {
    DAY2: [
        _candle(DAY2, "09:30", 130.0, 45_000),
        _candle(DAY2, "09:35", 130.0, 45_000),   # the day's Reference Time itself
        _candle(DAY2, "09:40", 145.0, 49_000),   # premium UP, OI UP -> Long Build-up -> ENTRY
    ],
}


# ---------------------------------------------------------------------------
# DAY 5 -- SIDEWAYS/QUIET day (2026-08-11, self-contained prev-close on
# 2026-08-10, independent of the Day1-4 chain). Both CE and PE oscillate
# choppily all session but their premium NEVER exceeds their own Day-0-
# equivalent reference -- Accumulation requires premium > reference (either
# quadrant), so keeping premium <= reference all day guarantees every
# candle classifies as Weakening or flat, NEVER Accumulation -- proves the
# "no false positives on a quiet/range-bound day" property: zero orders
# placed for any of the 4 legs across a full session.
# ---------------------------------------------------------------------------
DAY5 = "2026-08-11"
PREV_DAY5 = "2026-08-10"
WEEKLY_CE_SYMBOL_DAY5 = "NIFTY13AUG2624300CE"
WEEKLY_PE_SYMBOL_DAY5 = "NIFTY13AUG2623700PE"

SPOT_CANDLES_DAY5 = {
    PREV_DAY5: [_candle(PREV_DAY5, "15:25", 24000.0, 0)],
    DAY5: [
        _candle(DAY5, "09:30", 24010.0, 0),   # gap +0.04% -- small, prev_close mode, not the focus here
    ] + [_candle(DAY5, f"{h:02d}:{m:02d}", 24010.0, 0)
         for h in (9, 10, 11, 12, 13, 14, 15) for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)
         if (h, m) > (9, 30) and (h, m) <= (15, 30)],
}

_CE_OSCILLATION = [78, 92, 85, 99, 70, 88, 95, 76, 90, 83, 97, 72, 86, 94, 80]   # all <= reference (100)
_PE_OSCILLATION = [65, 79, 72, 89, 60, 75, 82, 66, 77, 70, 84, 62, 73, 81, 68]   # all <= reference (90)

def _times_from(start_hhmm: str, count: int, step_min: int = 5) -> list[str]:
    """count 5-min-aligned "HH:MM" strings starting at start_hhmm (inclusive)."""
    h0, m0 = (int(x) for x in start_hhmm.split(":"))
    total0 = h0 * 60 + m0
    out = []
    for i in range(count):
        total = total0 + i * step_min
        out.append(f"{total // 60:02d}:{total % 60:02d}")
    return out


_DAY5_TIMES = _times_from("09:35", len(_CE_OSCILLATION))

WEEKLY_CE_CANDLES_DAY5 = {
    PREV_DAY5: [_candle(PREV_DAY5, "15:25", 100.0, 50_000)],
    DAY5: [
        _candle(DAY5, hhmm, float(px), 50_000 + i * 500)
        for i, (hhmm, px) in enumerate(zip(_DAY5_TIMES, _CE_OSCILLATION))
    ],
}
WEEKLY_PE_CANDLES_DAY5 = {
    PREV_DAY5: [_candle(PREV_DAY5, "15:25", 90.0, 40_000)],
    DAY5: [
        _candle(DAY5, hhmm, float(px), 40_000 + i * 400)
        for i, (hhmm, px) in enumerate(zip(_DAY5_TIMES, _PE_OSCILLATION))
    ],
}
