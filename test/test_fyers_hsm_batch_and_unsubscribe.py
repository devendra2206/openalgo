"""
Regression tests for two Fyers HSM streaming fixes made during the 2026-07-30
production staleness investigation (Combined/Batman both showed NIFTY.NSE_INDEX
going permanently stale, recovering only after a full strategy restart, while
a fresh manual /websocket/test subscribe to the same symbol got live ticks --
ruling out a broker-wide outage).

1. FyersWebSocketAdapter._flush_hsm_batch used to collapse EVERY symbol queued
   within its 0.15s window into ONE FyersAdapter.subscribe_symbols() call --
   including symbols from entirely different strategy processes, since this
   adapter is a single shared instance for the whole (single-user) deployment.
   Fyers' /data/symbol-token API can mis-pair tokens when a request lists
   multiple symbols together (documented for index+options mixed in one call).
   Fix: one subscribe_symbols() call per symbol, removing any possibility of
   cross-symbol mixing.

2. FyersAdapter.unsubscribe_symbols() used to be a pure no-op (log-only) --
   defined but never called from anywhere, so active_subscriptions/
   symbol_to_hsm/hsm_to_symbol entries were NEVER cleared on unsubscribe,
   creating permanent "ghost" mappings that could misdirect a token Fyers
   later reuses for a different instrument. Fix: unsubscribe_symbols() now
   clears this adapter's own tracking, wired in from
   FyersWebSocketAdapter.unsubscribe() -- but only once no sibling mode
   (Quote vs Depth) subscription remains for the same symbol, since those
   three dicts are keyed by symbol alone, not symbol+mode (protects the
   pre-existing issue #1093 fix for that exact sibling case).

These tests exercise the real FyersWebSocketAdapter/FyersAdapter classes, with
FyersAdapter's HSM-facing methods stubbed (no real network/WebSocket/DB access)
so the actual batching, locking, and cleanup logic under test is production
code, not a reimplementation.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.fyers.streaming.fyers_adapter import FyersAdapter
from broker.fyers.streaming.fyers_websocket_adapter import FyersWebSocketAdapter


class FakeFyersAdapter:
    """Stubs just the HSM-facing surface FyersWebSocketAdapter calls into --
    records every subscribe_quote/subscribe_depth call so tests can assert
    on how many calls were made and what each one contained."""

    def __init__(self):
        self.quote_calls: list[list[dict]] = []
        self.depth_calls: list[list[dict]] = []
        self.subscription_callbacks: dict = {}

    def subscribe_quote(self, symbol_info, callback):
        self.quote_calls.append(list(symbol_info))
        return True

    def subscribe_depth(self, symbol_info, callback):
        self.depth_calls.append(list(symbol_info))
        return True

    def disconnect(self, clear_mappings=True):
        pass


@pytest.fixture
def ws_adapter():
    a = FyersWebSocketAdapter()
    a.fyers_adapter = FakeFyersAdapter()
    a.connected = True
    yield a
    # Cancel any pending batch timer so it doesn't fire after the test ends.
    with a._hsm_batch_lock:
        if a._hsm_batch_timer is not None:
            a._hsm_batch_timer.cancel()
            a._hsm_batch_timer = None
        a._hsm_batch_queue.clear()


def test_flush_issues_one_call_per_symbol_not_one_combined_call(ws_adapter):
    """The core fix: queuing 3 distinct symbols in the same batch window must
    result in 3 separate subscribe_quote() calls (one symbol each), never one
    call listing all 3 together -- that's the condition that let two
    strategies' resubscribes for two different symbols get silently mixed
    into a single Fyers request."""
    ws_adapter._enqueue_hsm_subscribe("SymbolUpdate", "NSE_INDEX", "NIFTY", lambda d: None)
    ws_adapter._enqueue_hsm_subscribe("SymbolUpdate", "BFO", "SENSEX06AUG2677500CE", lambda d: None)
    ws_adapter._enqueue_hsm_subscribe("SymbolUpdate", "NFO", "NIFTY04AUG2624200PE", lambda d: None)

    ws_adapter._flush_hsm_batch()

    calls = ws_adapter.fyers_adapter.quote_calls
    assert len(calls) == 3, f"expected 3 separate calls, got {len(calls)}: {calls}"
    for call in calls:
        assert len(call) == 1, f"each call must carry exactly 1 symbol, got {call}"

    called_symbols = {c[0]["symbol"] for c in calls}
    assert called_symbols == {"NIFTY", "SENSEX06AUG2677500CE", "NIFTY04AUG2624200PE"}


def test_flush_groups_by_data_type_separately(ws_adapter):
    """A Quote subscribe and a Depth subscribe queued together must still go
    to their respective subscribe_quote/subscribe_depth methods, each as its
    own single-symbol call."""
    ws_adapter._enqueue_hsm_subscribe("SymbolUpdate", "NSE", "TCS", lambda d: None)
    ws_adapter._enqueue_hsm_subscribe("DepthUpdate", "NSE", "TCS", lambda d: None)

    ws_adapter._flush_hsm_batch()

    assert len(ws_adapter.fyers_adapter.quote_calls) == 1
    assert len(ws_adapter.fyers_adapter.depth_calls) == 1
    assert ws_adapter.fyers_adapter.quote_calls[0] == [{"exchange": "NSE", "symbol": "TCS"}]
    assert ws_adapter.fyers_adapter.depth_calls[0] == [{"exchange": "NSE", "symbol": "TCS"}]


def test_flush_dedupes_same_symbol_last_writer_wins(ws_adapter):
    """Two enqueues for the identical (data_type, symbol, exchange) collapse
    to a single call -- unchanged pre-existing dedup behavior, just verifying
    the per-symbol-call rewrite didn't lose it."""
    first_cb_called = []
    second_cb_called = []

    ws_adapter._enqueue_hsm_subscribe(
        "SymbolUpdate", "NSE_INDEX", "NIFTY", lambda d: first_cb_called.append(d)
    )
    ws_adapter._enqueue_hsm_subscribe(
        "SymbolUpdate", "NSE_INDEX", "NIFTY", lambda d: second_cb_called.append(d)
    )

    ws_adapter._flush_hsm_batch()

    assert len(ws_adapter.fyers_adapter.quote_calls) == 1
    # The registry should hold the LAST callback registered for this key.
    registered_cb = ws_adapter._hsm_callback_registry["SymbolUpdate_NSE_INDEX:NIFTY"]
    registered_cb({"exchange": "NSE_INDEX", "symbol": "NIFTY"})
    assert second_cb_called and not first_cb_called


def test_flush_skips_when_not_connected(ws_adapter):
    ws_adapter.connected = False
    ws_adapter._enqueue_hsm_subscribe("SymbolUpdate", "NSE_INDEX", "NIFTY", lambda d: None)

    ws_adapter._flush_hsm_batch()

    assert ws_adapter.fyers_adapter.quote_calls == []


class _StubTokenConverter:
    def convert_openalgo_symbols_to_hsm(self, symbols, data_type):
        # token id derived deterministically from symbol so tests can assert
        # on specific hsm_token values without a real DB/network lookup.
        tokens = [f"tok_{s['symbol']}" for s in symbols]
        mappings = {f"tok_{s['symbol']}": f"BR_{s['symbol']}" for s in symbols}
        return tokens, mappings, []


@pytest.fixture
def raw_adapter(monkeypatch):
    """A real FyersAdapter with just the DB/network-touching seams stubbed:
    token conversion (normally a DB lookup) and get_br_symbol (normally a DB
    lookup too) and the outward-facing HSM ws_client.subscribe_symbols call."""
    a = FyersAdapter(access_token="dummy", userid="dummy")
    a.connected = True
    a.token_converter = _StubTokenConverter()
    a.ws_client = type(
        "WS", (), {"subscribe_symbols": lambda self, tokens, mappings: None, "disconnect": lambda self: None}
    )()

    def fake_get_br_symbol(symbol, exchange):
        return f"BR_{symbol}"

    monkeypatch.setattr(
        "broker.fyers.streaming.fyers_adapter.get_br_symbol", fake_get_br_symbol
    )
    return a


def test_unsubscribe_symbols_clears_hsm_tracking(raw_adapter):
    """The core fix: unsubscribe_symbols() must actually remove
    active_subscriptions/symbol_to_hsm/hsm_to_symbol for the given symbol --
    previously this method didn't touch any of these dicts, leaving a
    permanent ghost mapping that could misdirect a reused HSM token."""
    symbols = [{"exchange": "NSE_INDEX", "symbol": "NIFTY"}]
    raw_adapter.subscribe_symbols(symbols, "SymbolUpdate", callback=lambda d: None)

    assert "NSE_INDEX:NIFTY" in raw_adapter.active_subscriptions
    assert "NSE_INDEX:NIFTY" in raw_adapter.symbol_to_hsm
    assert raw_adapter.hsm_to_symbol.get("tok_NIFTY") == "NSE_INDEX:NIFTY"

    raw_adapter.unsubscribe_symbols(symbols)

    assert "NSE_INDEX:NIFTY" not in raw_adapter.active_subscriptions
    assert "NSE_INDEX:NIFTY" not in raw_adapter.symbol_to_hsm
    assert "tok_NIFTY" not in raw_adapter.hsm_to_symbol


def test_unsubscribe_symbols_does_not_touch_other_symbols(raw_adapter):
    """Unsubscribing one symbol must leave a DIFFERENT, still-subscribed
    symbol's tracking completely untouched."""
    symbols = [
        {"exchange": "NSE_INDEX", "symbol": "NIFTY"},
        {"exchange": "BSE_INDEX", "symbol": "SENSEX"},
    ]
    raw_adapter.subscribe_symbols(symbols, "SymbolUpdate", callback=lambda d: None)

    raw_adapter.unsubscribe_symbols([{"exchange": "NSE_INDEX", "symbol": "NIFTY"}])

    assert "NSE_INDEX:NIFTY" not in raw_adapter.active_subscriptions
    assert "BSE_INDEX:SENSEX" in raw_adapter.active_subscriptions
    assert "BSE_INDEX:SENSEX" in raw_adapter.symbol_to_hsm
    assert raw_adapter.hsm_to_symbol.get("tok_SENSEX") == "BSE_INDEX:SENSEX"


def test_unsubscribe_symbols_empty_list_is_a_noop(raw_adapter):
    """Guards the early return -- must not raise on an empty list."""
    raw_adapter.unsubscribe_symbols([])


@pytest.fixture
def ws_adapter_with_real_fyers_adapter(monkeypatch):
    """FyersWebSocketAdapter wired to a REAL FyersAdapter (stubbed only at
    the DB/network seams) so the sibling-subscription guard in
    FyersWebSocketAdapter.unsubscribe() can be tested end to end against the
    actual FyersAdapter.unsubscribe_symbols() implementation."""
    ws = FyersWebSocketAdapter()
    ws.connected = True

    fa = FyersAdapter(access_token="dummy", userid="dummy")
    fa.connected = True
    fa.token_converter = _StubTokenConverter()
    fa.ws_client = type(
        "WS", (), {"subscribe_symbols": lambda self, tokens, mappings: None, "disconnect": lambda self: None}
    )()
    ws.fyers_adapter = fa

    monkeypatch.setattr(
        "broker.fyers.streaming.fyers_adapter.get_br_symbol",
        lambda symbol, exchange: f"BR_{symbol}",
    )
    return ws, fa


def test_unsubscribe_preserves_sibling_mode_hsm_tracking(ws_adapter_with_real_fyers_adapter):
    """Unsubscribing Quote (mode 2) while Depth (mode 3) is still subscribed
    on the SAME symbol must NOT clear FyersAdapter's active_subscriptions/
    hsm_to_symbol for that symbol -- those dicts are keyed by symbol alone,
    so clearing them would silently break the still-active Depth sibling's
    routing too (the exact issue #1093 class of bug)."""
    ws, fa = ws_adapter_with_real_fyers_adapter
    exchange, symbol = "NSE_INDEX", "NIFTY"

    fa.subscribe_symbols(
        [{"exchange": exchange, "symbol": symbol}], "SymbolUpdate", callback=lambda d: None
    )
    ws.subscriptions[f"{exchange}:{symbol}:2"] = {"symbol": symbol, "exchange": exchange, "mode": 2}
    ws.subscriptions[f"{exchange}:{symbol}:3"] = {"symbol": symbol, "exchange": exchange, "mode": 3}

    ws.unsubscribe(symbol, exchange, mode=2)

    full_symbol = f"{exchange}:{symbol}"
    assert full_symbol in fa.active_subscriptions, (
        "sibling Depth subscription's HSM tracking must survive Quote unsubscribe"
    )
    assert full_symbol in fa.hsm_to_symbol.values()


def test_unsubscribe_clears_hsm_tracking_once_last_sibling_gone(ws_adapter_with_real_fyers_adapter):
    """Once the LAST mode subscription for a symbol is unsubscribed,
    FyersAdapter's HSM tracking for it must finally be cleared."""
    ws, fa = ws_adapter_with_real_fyers_adapter
    exchange, symbol = "NSE_INDEX", "NIFTY"

    fa.subscribe_symbols(
        [{"exchange": exchange, "symbol": symbol}], "SymbolUpdate", callback=lambda d: None
    )
    ws.subscriptions[f"{exchange}:{symbol}:2"] = {"symbol": symbol, "exchange": exchange, "mode": 2}

    ws.unsubscribe(symbol, exchange, mode=2)

    full_symbol = f"{exchange}:{symbol}"
    assert full_symbol not in fa.active_subscriptions
    assert full_symbol not in fa.symbol_to_hsm
