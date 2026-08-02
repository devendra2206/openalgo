"""
BacktestClient -- implements the same surface the deployed strategy calls on
`client` (history/quotes/expiry/optionchain/optiongreeks/placeorder/
orderstatus/modifyorder/cancelorder/connect/disconnect), backed by the real
2023-2026 historical dataset instead of a live broker. This lets the
UNMODIFIED StrategyEngine/WeeklySideEngine/MonthlySideEngine classes from
Nifty_OI_WeeklyBuy_MonthlySell_1_20260802000000.py run against real history --
same code path as production, only the data source differs.

Delta approximation (Monthly Sell's strike selection)
-------------------------------------------------------
This dataset has no IV/greeks -- only OHLCV+OI. Rather than assume a flat IV
and run real Black-Scholes (which would bake in an unverified constant),
optiongreeks() here uses a plain moneyness heuristic grounded in the data
itself:

  1. expected_move_pct = trailing 60-session realized daily volatility of
     spot (computed from the REAL spot series up to "now") * sqrt(DTE).
  2. z = |strike - spot| / spot / expected_move_pct
  3. delta = max(0.0, 0.5 - 0.5*z)   -- linear falloff: 0.5 at-the-money,
     0 at z=1 (i.e. at one full expected-move away). A delta in [0.20, 0.25]
     therefore corresponds to z in [0.5, 0.6] of that expected move.

This is a deliberate simplification (no normal CDF, no assumed IV constant)
-- expect it to pick strikes in the right NEIGHBORHOOD of what a real
0.20-0.25 broker-quoted delta would select, not an exact match. Treat
Monthly Sell's backtest results as directionally indicative, not as precise
as Weekly Buy's (which needs no delta at all -- just nearest listed OTM1
strike, same selection live and here).
"""

from __future__ import annotations

import functools
import math
from datetime import date, datetime

import pandas as pd

import oi_backtest_data as data

IST_SUFFIX = "+05:30"
UNDERLYING = "NIFTY"
_MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _raw_ddmmmyy(d: date) -> str:
    return f"{d.day:02d}-{_MONTH_ABBR[d.month]}-{d.strftime('%y')}"


def _ts_str(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S") + IST_SUFFIX


class Clock:
    """Shared mutable "now" for the whole backtest -- BacktestClient reads
    it to decide what data would have been visible at that instant; the
    driver script advances it. Kept separate from the deployed script's own
    `datetime.now(IST)` patch (see run_oi_backtest.py's _FixedDateTime) so
    both stay trivially in sync -- the driver sets both from one loop."""
    def __init__(self, start: datetime):
        self.current = start


class BacktestOrder:
    __slots__ = ("order_id", "symbol", "exchange", "action", "quantity", "fill_price")

    def __init__(self, order_id, symbol, exchange, action, quantity, fill_price):
        self.order_id = order_id
        self.symbol = symbol
        self.exchange = exchange
        self.action = action
        self.quantity = quantity
        self.fill_price = fill_price


class BacktestClient:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.connected = True
        self.authenticated = True
        self._order_seq = 0
        self._orders: dict[str, BacktestOrder] = {}
        self.placed_orders: list[tuple] = []  # [(symbol, action, quantity), ...] for assertions/reporting

        expiries = data.load_expiry_dates()
        self._expiry_raw_all = [_raw_ddmmmyy(d) for d in expiries]
        self._raw_to_iso = {_raw_ddmmmyy(d): d.isoformat() for d in expiries}

    # -- internal helpers ---------------------------------------------------
    def _bars_for_symbol(self, symbol: str, exchange: str) -> pd.DataFrame | None:
        if symbol == UNDERLYING and exchange in ("NSE_INDEX", "NSE"):
            return data.load_spot_5min()
        parts = data.resolve_option_symbol_parts(symbol, UNDERLYING)
        if parts is None:
            return None
        expiry_compact, strike, opt_type = parts
        # expiry_compact is "13AUG26" (no dashes) -- recover the raw
        # "13-Aug-26" form to look up the ISO folder key.
        raw = f"{expiry_compact[:2]}-{expiry_compact[2:5].capitalize()}-{expiry_compact[5:]}"
        iso = self._raw_to_iso.get(raw)
        if iso is None:
            return None
        return data.load_option_5min(iso, strike, opt_type)

    def _last_close_at_or_before(self, symbol: str, exchange: str, at: datetime) -> float | None:
        bars = self._bars_for_symbol(symbol, exchange)
        if bars is None or bars.empty:
            return None
        # bars.index is tz-naive (raw IST wall-clock from the CSVs); `at` is
        # tz-aware (IST-localized, from the script's own datetime.now(IST)) --
        # strip tzinfo rather than localize the index, since every timestamp
        # in this whole pipeline is already IST wall-clock with no other
        # timezone ever entering it.
        at_naive = at.replace(tzinfo=None)
        eligible = bars[bars.index <= at_naive]
        if eligible.empty:
            return None
        return float(eligible["close"].iloc[-1])

    @staticmethod
    @functools.lru_cache(maxsize=4096)
    def _realized_vol_pct_cached(as_of_date: date, lookback_sessions: int = 60) -> float:
        daily_close = data.load_spot_daily_close()
        daily_close = daily_close[daily_close.index.date <= as_of_date]
        daily_close = daily_close.tail(lookback_sessions + 1)
        if len(daily_close) < 5:
            return 0.15  # not enough history yet (very start of the dataset) -- a reasonable flat fallback
        rets = daily_close.pct_change().dropna()
        daily_std = float(rets.std())
        return daily_std * math.sqrt(252)

    def _realized_vol_pct(self, as_of: datetime, lookback_sessions: int = 60) -> float:
        """Annualized realized daily volatility of spot, trailing
        `lookback_sessions` sessions ending at/before `as_of`'s date --
        used only by optiongreeks()'s moneyness heuristic (see module
        docstring). Cached per calendar date: it only changes once a day,
        but was previously recomputed (and previously re-resampling the
        WHOLE spot series) on every single optiongreeks() call -- once per
        strike scanned per Monthly evaluation per candle, which dominated
        backtest runtime."""
        return self._realized_vol_pct_cached(as_of.date(), lookback_sessions)

    # -- market data ----------------------------------------------------
    def history(self, symbol, exchange, interval, start_date, end_date):
        bars = self._bars_for_symbol(symbol, exchange)
        if bars is None or bars.empty:
            return {"status": "error", "message": f"no data for {symbol}.{exchange}"}
        now_naive = self.clock.current.replace(tzinfo=None)
        start_ts = pd.Timestamp(start_date)
        end_ts = min(pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1),
                     pd.Timestamp(now_naive))
        # .loc[] slice on a sorted DatetimeIndex is a binary search -- an
        # elementwise `.date` comparison across the WHOLE index (previously
        # here) re-extracts .date for every row in the frame on every call,
        # regardless of how narrow the requested window is.
        rows = bars.loc[start_ts:end_ts]
        if rows.empty:
            return {"status": "error", "message": "no data"}
        # Vectorized instead of iterrows()/per-row dict building -- iterrows()
        # constructs a full pandas Series per row (dtype coercion and all),
        # which dominated backtest runtime (profiled: 56s of a 59s run spent
        # in history()/iterrows() alone for just a 3-day window). This does
        # the same conversion in a handful of column-level operations.
        rows = rows.copy()
        rows["timestamp"] = rows.index.strftime("%Y-%m-%d %H:%M:%S") + IST_SUFFIX
        cols = ["timestamp", "open", "high", "low", "close"]
        if "oi" in rows.columns:
            rows["volume"] = rows["volume"].fillna(0)
            rows["oi"] = rows["oi"].fillna(0)
            cols += ["volume", "oi"]
        out = rows[cols].to_dict(orient="records")
        return {"status": "success", "data": out}

    def quotes(self, symbol, exchange):
        px = self._last_close_at_or_before(symbol, exchange, self.clock.current)
        if px is None:
            return {"status": "error", "message": f"no quote for {symbol}.{exchange}"}
        return {"status": "success", "data": {"ltp": px, "bid": px, "ask": px}}

    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": list(self._expiry_raw_all)}

    def optionchain(self, underlying, exchange, expiry_date, strike_count=20):
        iso = self._raw_to_iso.get(expiry_date)
        if iso is None:
            return {"status": "error", "message": f"unknown expiry {expiry_date}"}
        strikes = sorted({s for s, _ot in data.list_strikes_for_expiry(iso)})
        if not strikes:
            return {"status": "error", "message": f"no strikes listed for {expiry_date}"}
        return {"status": "success", "data": [{"strike": s} for s in strikes]}

    def optiongreeks(self, symbol, exchange, **kwargs):
        parts = data.resolve_option_symbol_parts(symbol, UNDERLYING)
        if parts is None:
            return {"status": "error", "message": f"cannot parse {symbol}"}
        expiry_compact, strike, _opt_type = parts
        raw = f"{expiry_compact[:2]}-{expiry_compact[2:5].capitalize()}-{expiry_compact[5:]}"
        iso = self._raw_to_iso.get(raw)
        if iso is None:
            return {"status": "error", "message": f"unknown expiry in {symbol}"}
        expiry_d = date.fromisoformat(iso)
        now = self.clock.current
        dte = max((expiry_d - now.date()).days, 1)
        spot = self._last_close_at_or_before(UNDERLYING, "NSE_INDEX", now)
        if spot is None:
            return {"status": "error", "message": "no spot data to derive delta from"}
        vol_pct = self._realized_vol_pct(now)
        expected_move_pct = vol_pct * math.sqrt(dte / 365.0)
        if expected_move_pct <= 0:
            return {"status": "error", "message": "zero expected move"}
        z = abs(strike - spot) / spot / expected_move_pct
        delta = max(0.0, 0.5 - 0.5 * z)
        return {"status": "success", "greeks": {"delta": delta}}

    # -- orders -----------------------------------------------------------
    def placeorder(self, strategy, symbol, exchange, action, product, price_type,
                    quantity, price, trigger_price, disclosed_quantity):
        px = self._last_close_at_or_before(symbol, exchange, self.clock.current)
        if px is None:
            return {"status": "error", "message": f"no fill price available for {symbol}"}
        self._order_seq += 1
        order_id = f"BT{self._order_seq}"
        self._orders[order_id] = BacktestOrder(order_id, symbol, exchange, action, int(quantity), px)
        self.placed_orders.append((symbol, action, int(quantity)))
        return {"status": "success", "orderid": order_id}

    def orderstatus(self, order_id, strategy):
        order = self._orders.get(order_id)
        if order is None:
            return {"data": {"order_status": "rejected"}}
        return {"data": {"order_status": "complete", "average_price": order.fill_price, "price": order.fill_price}}

    def modifyorder(self, **kwargs):
        return {"status": "success"}

    def cancelorder(self, order_id, strategy):
        return {"status": "success"}

    # -- lifecycle / websocket (unused -- driver passes a no-op price stream) --
    def connect(self):
        pass

    def disconnect(self):
        pass

    def subscribe_ltp(self, *a, **k):
        pass

    def unsubscribe_ltp(self, *a, **k):
        pass


class NullPriceStream:
    """Forces every LTP read through BacktestClient.quotes() -- same role as
    the simulation test suite's FakePriceStream."""
    def get_ltp(self, symbol, exchange, max_age):
        return None

    def add_instruments(self, instruments):
        pass

    def remove_instruments(self, instruments):
        pass
