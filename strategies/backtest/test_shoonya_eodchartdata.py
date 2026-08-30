"""
Direct test: does Shoonya's EODChartData endpoint actually return correct
daily candles right now, in this deployment -- as opposed to the old
_get_daily_from_intraday() (5m-aggregation) workaround currently used by
broker/shoonya/api/data.py's get_history(interval="D", ...)?

Background: this fork's get_history("D") uses 5m-aggregation because a
comment in the file says EODChartData returned 405 Method Not Allowed
post-OAuth. Upstream openalgo main has since switched back to
EODChartData (commits b5cdf635 + 376f3c31), suggesting that was fixed
upstream. This script calls EODChartData DIRECTLY (bypassing the
deployed get_history() entirely) using the SAME auth mechanism
(get_chart_api_response, already present in this file) to see whether it
actually works against the live broker session on THIS server, and
cross-checks its daily closes against both the old 5m-aggregation
close and the raw 5m data's own actual last close (ground truth).

Must run inside the OpenAlgo app's own environment (uv-managed venv,
from the repo root) so it can import database/broker modules and reach
the live broker session + BROKER_API_KEY from .env.

Usage (from the openalgo repo root):
    uv run python3 strategies/backtest/test_shoonya_eodchartdata.py --days 30
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import utils.config  # noqa: F401  -- loads .env (BROKER_API_KEY etc.) as a side effect

from database.auth_db import Auth, db_session, get_auth_token
from broker.shoonya.api.data import BrokerData, get_chart_api_response

SYMBOL = "NIFTY"
EXCHANGE = "NSE_INDEX"
EOD_SYMBOL = "Nifty 50"   # Shoonya's own display name for NIFTY under NSE (per upstream's EOD_INDEX_SYMBOLS)


def find_active_auth():
    row = (
        db_session.query(Auth)
        .filter(Auth.broker == "shoonya", Auth.is_revoked == False)  # noqa: E712
        .first()
    )
    if row is None:
        raise RuntimeError("No active (non-revoked) Shoonya auth row found in the auth table.")
    return row.name


def parse_eod_candle(candle):
    if isinstance(candle, str):
        candle = json.loads(candle)
    ssboe = candle.get("ssboe")
    if ssboe is not None:
        ts = int(ssboe)
    else:
        ts = int(datetime.strptime(candle["time"], "%d-%b-%Y").timestamp())
    return {
        "timestamp": ts,
        "open": float(candle.get("into", 0)),
        "high": float(candle.get("inth", 0)),
        "low": float(candle.get("intl", 0)),
        "close": float(candle.get("intc", 0)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    name = find_active_auth()
    print(f"Using active Shoonya auth row: name={name!r}")
    auth_token = get_auth_token(name, bypass_cache=True)
    if not auth_token:
        print("Could not resolve a decrypted auth token for this user -- is the broker session valid?")
        return

    end = datetime.now().date()
    start = end - timedelta(days=args.days)
    start_str, end_str = start.isoformat(), end.isoformat()
    start_ts = int(datetime.strptime(start_str + " 00:00:00", "%Y-%m-%d %H:%M:%S").timestamp())
    end_ts = int(datetime.strptime(end_str + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp())

    print(f"Range: {start_str} -> {end_str}")
    print()

    # --- 1) Call EODChartData DIRECTLY (bypasses the deployed get_history("D") entirely) ---
    print("Calling EODChartData directly ...")
    eod_payload = {"sym": f"NSE:{EOD_SYMBOL}", "from": str(start_ts), "to": str(end_ts)}
    eod_raw = get_chart_api_response("/NorenWClientAPI/EODChartData", auth_token, payload=eod_payload)
    if isinstance(eod_raw, dict):
        print(f"  EODChartData FAILED: stat={eod_raw.get('stat')} emsg={eod_raw.get('emsg')}")
        eod_by_day = {}
    else:
        eod_candles = [parse_eod_candle(c) for c in eod_raw]
        eod_by_day = {}
        for c in eod_candles:
            d = datetime.utcfromtimestamp(c["timestamp"]).date()
            eod_by_day[d] = c["close"]
        print(f"  {len(eod_candles)} EODChartData candles returned.")

    # --- 2) Old deployed code path: get_history("D") via 5m-aggregation ---
    print("Calling the currently-deployed get_history('D') (5m-aggregation) ...")
    bd = BrokerData(auth_token)
    old_d_df = bd.get_history(SYMBOL, EXCHANGE, "D", start_str, end_str)
    old_by_day = {}
    for _, row in old_d_df.iterrows():
        d = datetime.utcfromtimestamp(int(row["timestamp"])).date()
        old_by_day[d] = float(row["close"])
    print(f"  {len(old_by_day)} old-code daily candles returned.")

    # --- 3) Ground truth: raw 5m candles' own actual last close per IST day ---
    print("Calling get_history('5m') for ground truth ...")
    m5_df = bd.get_history(SYMBOL, EXCHANGE, "5m", start_str, end_str)
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    m5_df = m5_df.copy()
    m5_df["ist_date"] = m5_df["timestamp"].apply(lambda ts: datetime.fromtimestamp(int(ts), ist).date())
    actual_by_day = {}
    for d, grp in m5_df.groupby("ist_date"):
        actual_by_day[d] = float(grp.sort_values("timestamp")["close"].iloc[-1])
    print(f"  {len(actual_by_day)} days of ground-truth 5m data.")
    print()

    all_days = sorted(set(eod_by_day) | set(old_by_day) | set(actual_by_day))
    print("=" * 100)
    print(f"{'Date':<12} {'EODChartData':<15} {'Old(5m-agg)':<15} {'Actual(5m gt)':<15} {'EOD ok?':<9} {'Old ok?'}")
    print("=" * 100)
    eod_mismatches = 0
    old_mismatches = 0
    for d in all_days:
        eod_c = eod_by_day.get(d)
        old_c = old_by_day.get(d)
        actual_c = actual_by_day.get(d)
        eod_ok = "-" if (eod_c is None or actual_c is None) else ("YES" if abs(eod_c - actual_c) < 0.5 else "NO")
        old_ok = "-" if (old_c is None or actual_c is None) else ("YES" if abs(old_c - actual_c) < 0.5 else "NO")
        if eod_ok == "NO":
            eod_mismatches += 1
        if old_ok == "NO":
            old_mismatches += 1
        print(f"{d!s:<12} {eod_c if eod_c is not None else '-':<15} "
              f"{old_c if old_c is not None else '-':<15} "
              f"{actual_c if actual_c is not None else '-':<15} {eod_ok:<9} {old_ok}")

    print()
    print(f"EODChartData mismatches: {eod_mismatches}/{len(all_days)}")
    print(f"Old 5m-aggregation mismatches: {old_mismatches}/{len(all_days)}")
    if eod_mismatches == 0 and eod_by_day:
        print("-> EODChartData is reliable on this server RIGHT NOW. Safe to pull in the upstream fix.")
    elif not eod_by_day:
        print("-> EODChartData call itself failed -- see the error above before deciding anything.")
    else:
        print("-> EODChartData ALSO shows mismatches -- do not assume it's a clean fix without investigating further.")


if __name__ == "__main__":
    main()
