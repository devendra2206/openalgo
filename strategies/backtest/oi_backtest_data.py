"""
Data loading/resampling utilities for the OI Weekly-Buy/Monthly-Sell historical
backtest, against the real 2023-2026 dataset under
C:\\Devendra\\OpenAlgo\\data23_to_26\\data\\ (see that folder's OPTION_DATA.md
for the raw CSV schemas -- spot uses ISO dates, options use day-first dates,
options additionally carry Volume/Open Interest columns spot doesn't have).

All resampling happens ONCE per file, cached in-process (this module is
imported once per backtest run, not per strategy cycle) -- a 3-year run reads
hundreds of expiry folders, each with 100-200 per-strike files, so avoiding
repeat disk reads matters.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_ROOT = Path(r"C:\Devendra\OpenAlgo\data23_to_26\data")
SPOT_CSV = DATA_ROOT / "2023to2026_Nifty_Spot.csv"
EXPIRY_CSV = DATA_ROOT / "Expiry.csv"

_STRIKE_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(\d+(?:\.\d+)?)(CE|PE)\.csv$", re.IGNORECASE)


@lru_cache(maxsize=1)
def load_expiry_dates() -> list[date]:
    """Every weekly NIFTY expiry date (2023-01-05 .. 2026-03-24), ascending.
    NSE's monthly contract IS simply the last weekly expiry of its calendar
    month -- this same list resolves both weekly and monthly expiries (see
    resolve_weekly_expiry/resolve_monthly_expiry in the deployed script,
    which this backtest replays unmodified)."""
    df = pd.read_csv(EXPIRY_CSV)
    return sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in df["Dates"])


@lru_cache(maxsize=1)
def load_spot_5min() -> pd.DataFrame:
    """1-min NIFTY spot -> 5-min OHLC bars, indexed by tz-naive IST timestamp
    (this project's IST datetimes are handled by the caller; kept naive here
    since every read/write in this backtest is already scoped to IST wall-clock
    time -- no other timezone ever enters this pipeline)."""
    df = pd.read_csv(SPOT_CSV)
    df["timestamp"] = pd.to_datetime(df["Date"] + " " + df["Time"], format="%Y-%m-%d %H:%M:%S")
    df = df.set_index("timestamp").sort_index()
    bars = df.resample("5min", origin="start_day", offset="9h15min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    )
    bars = bars.dropna(subset=["Close"])
    bars.columns = ["open", "high", "low", "close"]
    return bars


@lru_cache(maxsize=None)
def list_strikes_for_expiry(expiry_iso: str) -> list[tuple[float, str]]:
    """[(strike, "CE"|"PE"), ...] for every file in that expiry's folder."""
    folder = DATA_ROOT / expiry_iso[:4] / expiry_iso
    if not folder.is_dir():
        return []
    out = []
    for f in folder.iterdir():
        m = _STRIKE_FILENAME_RE.match(f.name)
        if m:
            out.append((float(m.group(1)), m.group(2).upper()))
    return sorted(out)


@lru_cache(maxsize=1)
def load_spot_daily_close() -> pd.Series:
    """Spot's daily close series, computed once -- optiongreeks()'s realized-
    vol heuristic previously re-resampled the FULL multi-year spot frame on
    every single call (once per strike scanned per Monthly evaluation per
    candle), which dominated backtest runtime. Cached here; callers slice by
    date, never resample again."""
    spot = load_spot_5min()
    return spot["close"].resample("1D").last().dropna()


@lru_cache(maxsize=4096)
def load_option_5min(expiry_iso: str, strike: float, option_type: str) -> pd.DataFrame | None:
    """One strike's full life (from first trade through expiry), 1-min ->
    5-min OHLCV+OI. Returns None if the file doesn't exist (a strike that
    never listed, or a typo'd request). OI forward-fills across a resample
    gap (it doesn't reset every minute like price does -- the last known
    reading is still the true open interest during a quiet gap); Volume sums
    within each 5-min bucket; price fields are pure OHLC of the 1-min bars in
    that bucket. A bucket with zero trades (no 1-min rows at all) is dropped,
    not forward-filled as a fake flat candle -- callers already treat a
    missing candle as "skip this cycle", matching the live client.history()
    contract."""
    strike_int = int(strike) if strike == int(strike) else strike
    path = DATA_ROOT / expiry_iso[:4] / expiry_iso / f"{expiry_iso}_{strike_int}{option_type}.csv"
    if not path.is_file():
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df["timestamp"] = pd.to_datetime(df["Date"] + " " + df["Time"], format="%d/%m/%Y %H:%M:%S")
    df = df.set_index("timestamp").sort_index()
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume", "Open Interest": "oi",
    })
    ohlc = df[["open", "high", "low", "close"]].resample(
        "5min", origin="start_day", offset="9h15min"
    ).agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    vol = df["volume"].resample("5min", origin="start_day", offset="9h15min").sum()
    oi = df["oi"].resample("5min", origin="start_day", offset="9h15min").last().ffill()
    bars = ohlc.join(vol).join(oi)
    bars = bars.dropna(subset=["close"])
    return bars


def resolve_option_symbol_parts(symbol: str, underlying: str) -> tuple[str, float, str] | None:
    """Parses a strategy-format symbol (e.g. "NIFTY13AUG2624300CE") back into
    (expiry_compact_ddmmmyy, strike, option_type) -- the inverse of the
    deployed script's own f"{UNDERLYING}{expiry_compact}{strike}{opt_type}"
    construction. Returns None if it doesn't parse as expected."""
    if not symbol.startswith(underlying):
        return None
    rest = symbol[len(underlying):]
    m = re.match(r"^(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$", rest)
    if not m:
        return None
    return m.group(1), float(m.group(2)), m.group(3)
