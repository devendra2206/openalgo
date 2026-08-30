"""
Diagnostic: does OpenAlgo's Shoonya "D" (daily) candle match the actual
last 5-minute candle's close for the same trading day?

Background: this fork's broker/shoonya/api/data.py still builds daily
bars via _get_daily_from_intraday() -- aggregating 5-minute TPSeries
candles and taking the last one's close per IST calendar day. Manual
spot-checks against the /api/v1/history "D" endpoint (via algodev.co.in)
found several days where the reported daily close did NOT match that
day's own last 5-minute candle -- on some days it instead matched the
NEXT day's opening price, and on one day it was off by over 500 points.
This points to a day-boundary mis-grouping bug in the 5m-to-daily
aggregation (fixed upstream in openalgo main via b5cdf635 + 376f3c31,
which switched to Shoonya's own EODChartData endpoint instead -- not yet
pulled into this branch).

Run this ON THE SERVER (or anywhere with network access to your OpenAlgo
instance) to confirm the bug is actually present in the current deployed
build before deciding whether to pull in the upstream fix.

Usage:
    python3 verify_shoonya_daily_candle.py --host http://127.0.0.1:5000 --apikey <your_apikey>

Requires only the stdlib (urllib) -- no openalgo SDK dependency, so it
runs anywhere.
"""

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

UA = {"Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def call_history(host: str, apikey: str, symbol: str, exchange: str,
                  interval: str, start_date: str, end_date: str):
    payload = {
        "apikey": apikey,
        "symbol": symbol,
        "exchange": exchange,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "source": "api",   # bypass any local/cached DB path -- force a real broker fetch
    }
    req = urllib.request.Request(f"{host}/api/v1/history", data=json.dumps(payload).encode(),
                                  headers=UA, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"status": "error", "http_code": e.code, "body": e.read().decode()[:500]}
    except Exception as e:
        return {"status": "error", "body": str(e)}


def ts_to_ist_date(ts: int):
    return (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=5, minutes=30))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="http://127.0.0.1:5000",
                     help="OpenAlgo host, e.g. http://127.0.0.1:5000 or https://algodev.co.in")
    ap.add_argument("--apikey", required=True, help="API key from /apikey")
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--exchange", default="NSE_INDEX")
    ap.add_argument("--days", type=int, default=30, help="How many recent calendar days to check")
    args = ap.parse_args()

    end = datetime.now().date()
    start = end - timedelta(days=args.days)
    start_str, end_str = start.isoformat(), end.isoformat()

    print(f"Fetching D candles for {args.symbol}/{args.exchange} {start_str} -> {end_str} ...")
    d_resp = call_history(args.host, args.apikey, args.symbol, args.exchange, "D", start_str, end_str)
    if d_resp.get("status") != "success":
        print("D-interval request FAILED:", d_resp)
        return
    d_candles = d_resp.get("data", [])
    print(f"  {len(d_candles)} daily candles returned.")
    if not d_candles:
        print("No daily candles returned -- nothing to check. Try a wider --days or confirm "
              "the broker session on this server is logged in.")
        return

    print(f"Fetching 5m candles for the same range ...")
    m5_resp = call_history(args.host, args.apikey, args.symbol, args.exchange, "5m", start_str, end_str)
    if m5_resp.get("status") != "success":
        print("5m-interval request FAILED:", m5_resp)
        return
    m5_candles = m5_resp.get("data", [])
    print(f"  {len(m5_candles)} 5-min candles returned.")

    # Group 5m candles by IST calendar date, find each day's actual last close
    by_day = {}
    for c in m5_candles:
        d = ts_to_ist_date(c["timestamp"]).date()
        by_day.setdefault(d, []).append(c)

    actual_last_close = {}
    for d, rows in by_day.items():
        rows_sorted = sorted(rows, key=lambda r: r["timestamp"])
        actual_last_close[d] = rows_sorted[-1]["close"]

    print()
    print("=" * 90)
    print(f"{'Date':<12} {'D-close (reported)':<20} {'Actual last-5m close':<22} {'Match?':<8} {'Diff'}")
    print("=" * 90)

    mismatches = []
    for c in sorted(d_candles, key=lambda r: r["timestamp"]):
        d = ts_to_ist_date(c["timestamp"]).date()
        reported = c["close"]
        actual = actual_last_close.get(d)
        if actual is None:
            print(f"{d} {reported:<20} {'(no 5m data for this day)':<22}")
            continue
        diff = reported - actual
        match = abs(diff) < 0.5   # sub-point rounding tolerance
        print(f"{d!s:<12} {reported:<20.2f} {actual:<22.2f} {'YES' if match else 'NO':<8} {diff:+.2f}")
        if not match:
            mismatches.append((d, reported, actual, diff))

    print()
    if mismatches:
        print(f"BUG CONFIRMED: {len(mismatches)}/{len(d_candles)} daily candles have a close that "
              f"does NOT match that day's own last 5-minute candle.")
        print("Checking whether mismatched closes actually belong to the NEXT day's open instead:")
        by_day_open = {d: sorted(rows, key=lambda r: r["timestamp"])[0]["open"] for d, rows in by_day.items()}
        for d, reported, actual, diff in mismatches:
            next_day_candidates = [nd for nd in by_day_open if nd > d]
            next_day = min(next_day_candidates) if next_day_candidates else None
            next_open = by_day_open.get(next_day) if next_day else None
            shifted = next_open is not None and abs(reported - next_open) < 0.5
            print(f"  {d}: reported={reported:.2f}  next_day({next_day})_open={next_open}  "
                  f"{'MATCHES next-day open (off-by-one-candle bug)' if shifted else 'does not match next-day open either'}")
    else:
        print("NO MISMATCHES -- daily candles look consistent with the underlying 5m data on this run.")


if __name__ == "__main__":
    main()
