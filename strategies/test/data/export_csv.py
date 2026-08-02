"""
One-off exporter: dumps oi_simulation_data.py's synthetic dataset (already
validated via test_oi_weekly_monthly_simulation.py's 21 passing tests) into
two human-readable/editable CSV files:

  nifty_spot_5min.csv   -- NIFTY spot, 5-min OHLC (oi always 0, index has none)
  options_oi_5min.csv   -- all 6 option-strike symbols' 5-min OHLC + OI

Run: .venv/Scripts/python.exe strategies/test/data/export_csv.py
(regenerates both CSVs from the Python source of truth -- edit
oi_simulation_data.py and re-run this if the underlying scenario data
changes, rather than hand-editing the CSVs out of sync with it.)
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import oi_simulation_data as sim

OUT_DIR = Path(__file__).resolve().parent
FIELDS = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "oi"]


def _rows_for(symbol: str, candles_by_day: dict) -> list[dict]:
    rows = []
    for day, candles in candles_by_day.items():
        for c in candles:
            rows.append({"symbol": symbol, **c})
    return rows


def main():
    spot_rows = (
        _rows_for("NIFTY", sim.SPOT_CANDLES)
        + _rows_for("NIFTY", sim.SPOT_CANDLES_DAY3)
        + _rows_for("NIFTY", sim.SPOT_CANDLES_DAY4)
        + _rows_for("NIFTY", sim.SPOT_CANDLES_DAY5)
    )
    spot_rows.sort(key=lambda r: r["timestamp"])

    options_rows = (
        _rows_for(sim.WEEKLY_CE_SYMBOL, sim.WEEKLY_CE_CANDLES)
        + _rows_for(sim.WEEKLY_CE_SYMBOL, sim.WEEKLY_CE_CANDLES_DAY2)
        + _rows_for(sim.WEEKLY_PE_SYMBOL, sim.WEEKLY_PE_CANDLES)
        + _rows_for(sim.WEEKLY_PE_SYMBOL, sim.WEEKLY_PE_CANDLES_DAY3)
        + _rows_for(sim.MONTHLY_CE_SYMBOL, sim.MONTHLY_CE_CANDLES)
        + _rows_for(sim.MONTHLY_PE_SYMBOL, sim.MONTHLY_PE_CANDLES)
        # Day 4 rolls to a new weekly expiry (13-Aug-26, see
        # oi_simulation_data.py's Day 4 note) -- its own prev_close
        # reference needs Day 3's closing values duplicated under the NEW
        # symbol strings too, not just the "06-Aug-26" ones Day 3 itself
        # traded under.
        + _rows_for(sim.WEEKLY_CE_SYMBOL_DAY4, sim.WEEKLY_CE_CANDLES_DAY3_CLOSE)
        + _rows_for(sim.WEEKLY_CE_SYMBOL_DAY4, sim.WEEKLY_CE_CANDLES_DAY4)
        + _rows_for(sim.WEEKLY_PE_SYMBOL_DAY4, sim.WEEKLY_PE_CANDLES_DAY3)
        + _rows_for(sim.WEEKLY_PE_SYMBOL_DAY4, sim.WEEKLY_PE_CANDLES_DAY4)
        # Day 5 (sideways/quiet) reuses the same 13-Aug-26/24300/23700
        # symbols as Day 4 -- same weekly expiry still applies, same
        # option-chain gap for this spot range. Its own PREV_DAY5 close
        # candles carry its own prev_close reference; date-scoped lookups
        # keep this from mixing with Day 4's data despite the shared symbol.
        + _rows_for(sim.WEEKLY_CE_SYMBOL_DAY5, sim.WEEKLY_CE_CANDLES_DAY5)
        + _rows_for(sim.WEEKLY_PE_SYMBOL_DAY5, sim.WEEKLY_PE_CANDLES_DAY5)
    )
    options_rows.sort(key=lambda r: (r["symbol"], r["timestamp"]))

    spot_path = OUT_DIR / "nifty_spot_5min.csv"
    with spot_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(spot_rows)
    print(f"wrote {spot_path} ({len(spot_rows)} rows)")

    options_path = OUT_DIR / "options_oi_5min.csv"
    with options_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(options_rows)
    print(f"wrote {options_path} ({len(options_rows)} rows)")

    symbols = sorted({r["symbol"] for r in options_rows})
    print(f"option strikes covered ({len(symbols)}): {', '.join(symbols)}")


if __name__ == "__main__":
    main()
