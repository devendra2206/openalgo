"""
LiveHistoryClient -- same surface as oi_backtest_client.BacktestClient
(history/quotes/expiry/optionchain/optiongreeks/placeorder/orderstatus/
modifyorder/cancelorder/connect/disconnect), backed by REAL broker history
via algodev.co.in's REST API instead of the local 2023-2026 CSV dataset.

Lets the UNMODIFIED StrategyEngine/WeeklySideEngine/MonthlySideEngine classes
from Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py replay against a
recent real week using genuine broker OHLCV+OI data. Reads (history/quotes/
expiry/optionchain) hit the real REST API; writes (placeorder/orderstatus)
are simulated locally -- filled at the historical bar's own close, exactly
like BacktestClient, so nothing ever actually trades.

Unlike BacktestClient, no CSV-path parsing is needed: every symbol the
strategy computes (e.g. "NIFTY18AUG2624300CE") is passed straight through to
the real history() endpoint, which resolves it against the broker's own
master contract.

Historical-quotes caveat
-------------------------------------------------------------------------
quotes()/optionchain(with_quotes=True) cannot call the broker's LIVE quote
endpoints for a past simulated instant (that would return TODAY's real-time
price, not the price as of the simulated day/time). Both are instead
synthesized from the same cached history() bars everything else uses --
quotes() takes the last closed candle's `close` at-or-before the simulated
clock; optionchain(with_quotes=True) does the same per-strike, with
bid=ask=close (no historical spread data available). This is a real
improvement over the old CSV backtest's synthetic moneyness-heuristic delta
(oi_backtest_client.py) -- Monthly Sell's Black-76 delta here runs on
genuine historical premiums, not an approximation -- but the zero-spread
assumption is still a simplification worth remembering when reading Monthly
Sell's results.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from opengreeks import black76

IST_SUFFIX = "+05:30"


class Clock:
    def __init__(self, start: datetime):
        self.current = start


class LiveHistoryClient:
    def __init__(self, clock: Clock, api_key: str, host: str = "https://algodev.co.in",
                 interval: str = "5m", fetch_start: str | None = None, fetch_end: str | None = None):
        self.clock = clock
        self.api_key = api_key
        self.host = host.rstrip("/")
        self.interval = interval
        # Wide fixed window fetched once per symbol on first use, then
        # sliced locally for every subsequent (possibly narrower/different)
        # request -- the deployed script issues many different [start,end]
        # windows for the same symbol (e.g. history_lookback_days=5 rolling
        # windows), and re-hitting the REST API for each would be both slow
        # and wasteful since the whole range is already historical/fixed.
        self._fetch_start = fetch_start
        self._fetch_end = fetch_end
        self._bars_cache: dict[tuple[str, str], pd.DataFrame | None] = {}
        self._expiry_cache: dict[tuple, dict] = {}
        self._chain_cache: dict[tuple, dict] = {}
        self._session = requests.Session()

        self.connected = True
        self.authenticated = True
        self._order_seq = 0
        self._orders: dict[str, dict] = {}
        self.placed_orders: list[tuple] = []
        self.rest_calls = 0

    # -- REST plumbing -------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        payload = {**payload, "apikey": self.api_key}
        self.rest_calls += 1
        resp = self._session.post(f"{self.host}/api/v1/{path}", json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _bars_for_symbol(self, symbol: str, exchange: str) -> pd.DataFrame | None:
        key = (symbol, exchange)
        if key in self._bars_cache:
            return self._bars_cache[key]
        raw = self._post("history", {
            "symbol": symbol, "exchange": exchange, "interval": self.interval,
            "start_date": self._fetch_start, "end_date": self._fetch_end,
        })
        if raw.get("status") != "success" or not raw.get("data"):
            self._bars_cache[key] = None
            return None
        df = pd.DataFrame(raw["data"])
        # tz-AWARE IST index -- matches the real openalgo SDK's own
        # client.history() contract exactly (see .venv's openalgo/data.py:
        # DataFrame on success with a tz-aware IST DatetimeIndex, error dict
        # on failure). The deployed script's `_is_error_response` does
        # `isinstance(obj, dict)` to distinguish the two -- returning a plain
        # dict on success here (an earlier version of this file did) would
        # make every successful history() call misread as an error.
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df = df.set_index("timestamp").sort_index()
        for col in ("open", "high", "low", "close", "volume", "oi"):
            if col not in df.columns:
                df[col] = 0.0
        self._bars_cache[key] = df
        return df

    def _last_close_at_or_before(self, symbol: str, exchange: str, at: datetime) -> float | None:
        bars = self._bars_for_symbol(symbol, exchange)
        if bars is None or bars.empty:
            return None
        eligible = bars[bars.index <= at]
        if eligible.empty:
            return None
        return float(eligible["close"].iloc[-1])

    # -- market data -----------------------------------------------------
    def history(self, symbol, exchange, interval, start_date, end_date):
        """Returns a pandas DataFrame on success (tz-aware IST index) or an
        error dict on failure -- matching the real openalgo SDK's contract,
        which the deployed script's `_is_error_response`/`fetch_candle_oi_premium`
        rely on to distinguish success from failure."""
        bars = self._bars_for_symbol(symbol, exchange)
        if bars is None or bars.empty:
            return {"status": "error", "message": f"no data for {symbol}.{exchange}"}
        IST = bars.index.tz
        start_ts = pd.Timestamp(start_date, tz=IST)
        end_ts = min(
            pd.Timestamp(end_date, tz=IST) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1),
            pd.Timestamp(self.clock.current),
        )
        rows = bars.loc[start_ts:end_ts]
        if rows.empty:
            return {"status": "error", "message": "no data"}
        return rows[["open", "high", "low", "close", "volume", "oi"]].copy()

    def quotes(self, symbol, exchange):
        px = self._last_close_at_or_before(symbol, exchange, self.clock.current)
        if px is None:
            return {"status": "error", "message": f"no quote for {symbol}.{exchange}"}
        return {"status": "success", "data": {"ltp": px, "bid": px, "ask": px}}

    def expiry(self, symbol, exchange, instrumenttype):
        key = (symbol, exchange, instrumenttype)
        if key not in self._expiry_cache:
            self._expiry_cache[key] = self._post(
                "expiry", {"symbol": symbol, "exchange": exchange, "instrumenttype": instrumenttype}
            )
        return self._expiry_cache[key]

    def optionchain(self, underlying, exchange, expiry_date, strike_count=20, with_quotes=False):
        if not with_quotes:
            key = (underlying, exchange, expiry_date, strike_count)
            if key not in self._chain_cache:
                self._chain_cache[key] = self._post("optionchain", {
                    "underlying": underlying, "exchange": exchange, "expiry_date": expiry_date,
                    "strike_count": strike_count, "with_quotes": False,
                })
            resp = self._chain_cache[key]
            if resp.get("status") != "success":
                return resp
            # fetch_option_chain_strikes only reads `strike` -- pass the real
            # listed strikes through as-is (time-invariant for a given
            # expiry, so no historical-time concern here).
            return {"status": "success", "chain": [{"strike": row["strike"]} for row in resp.get("chain", [])]}

        # with_quotes=True -- select_monthly_delta_strike's path. Real
        # listed strikes (cached, time-invariant) but each strike's own
        # premium is re-derived from cached history() bars at the
        # simulated clock -- see module docstring's historical-quotes
        # caveat for the bid=ask=close simplification.
        listing_key = (underlying, exchange, expiry_date, strike_count)
        if listing_key not in self._chain_cache:
            self._chain_cache[listing_key] = self._post("optionchain", {
                "underlying": underlying, "exchange": exchange, "expiry_date": expiry_date,
                "strike_count": strike_count, "with_quotes": False,
            })
        listing = self._chain_cache[listing_key]
        if listing.get("status") != "success":
            return listing

        spot_now = self._last_close_at_or_before(underlying, exchange, self.clock.current)
        chain = []
        for row in listing.get("chain", []):
            strike = row["strike"]
            entry = {"strike": strike}
            for side, opt_type in (("ce", "CE"), ("pe", "PE")):
                sym = f"{underlying}{expiry_date}{int(strike) if strike == int(strike) else strike}{opt_type}"
                # Options exchange is NEVER the same as the spot `exchange`
                # param passed in here (that's always NSE_INDEX/NSE for the
                # underlying) -- this backtest is NIFTY-only, whose options
                # always list on NFO, matching the deployed script's own
                # module-level OPTIONS_EXCHANGE constant.
                px = self._last_close_at_or_before(sym, "NFO", self.clock.current)
                if px is None or px <= 0:
                    entry[side] = {"ltp": 0, "bid": 0, "ask": 0}
                else:
                    entry[side] = {"ltp": px, "bid": px, "ask": px}
            chain.append(entry)
        return {"status": "success", "underlying_ltp": spot_now, "chain": chain}

    def optiongreeks(self, symbol, exchange, **kwargs):
        # Not called directly by the deployed script (it derives Monthly
        # delta locally from optionchain(with_quotes=True) -- see that
        # function's own 2026-08-10 docstring note) -- kept only for
        # interface parity with BacktestClient.
        return {"status": "error", "message": "optiongreeks() not used by this backtest path"}

    # -- orders (simulated, never real) -----------------------------------
    def placeorder(self, strategy, symbol, exchange, action, product, price_type,
                    quantity, price, trigger_price, disclosed_quantity):
        px = self._last_close_at_or_before(symbol, exchange, self.clock.current)
        if px is None:
            return {"status": "error", "message": f"no fill price available for {symbol}"}
        self._order_seq += 1
        order_id = f"LIVE{self._order_seq}"
        self._orders[order_id] = {"symbol": symbol, "exchange": exchange, "action": action,
                                   "quantity": int(quantity), "fill_price": px}
        self.placed_orders.append((symbol, action, int(quantity)))
        return {"status": "success", "orderid": order_id}

    def orderstatus(self, order_id, strategy):
        order = self._orders.get(order_id)
        if order is None:
            return {"data": {"order_status": "rejected"}}
        return {"data": {"order_status": "complete", "average_price": order["fill_price"],
                          "price": order["fill_price"]}}

    def modifyorder(self, **kwargs):
        return {"status": "success"}

    def cancelorder(self, order_id, strategy):
        return {"status": "success"}

    def connect(self):
        pass

    def disconnect(self):
        pass

    def subscribe_ltp(self, *a, **k):
        pass

    def unsubscribe_ltp(self, *a, **k):
        pass


class NullPriceStream:
    def get_ltp(self, symbol, exchange, max_age):
        return None

    def add_instruments(self, instruments):
        pass

    def remove_instruments(self, instruments):
        pass
