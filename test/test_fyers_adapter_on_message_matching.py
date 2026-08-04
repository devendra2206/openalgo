"""
Regression tests for FyersAdapter._on_message()'s symbol-matching fix
(2026-08-04).

Background: confirmed in production that the unlocked matching logic in
_on_message() had two compounding problems during a ~20min Fyers WebSocket
reconnect storm (repeated 403 handshake rejections):

1. No lock around reads/writes to active_subscriptions/hsm_to_symbol/
   symbol_to_hsm/subscription_callbacks, the same dicts subscribe_symbols()/
   unsubscribe_symbols() mutate under self.lock -- a real, unguarded race
   under any genuinely preemptive scheduling.
2. A "no match found, but only one subscription exists -- assume it's this
   one" fallback with ZERO textual verification. During the reconnect storm,
   active_subscriptions was transiently down to one entry (an MCX CRUDEOIL
   option), and this fallback blindly relabeled unrelated incoming ticks
   (NIFTY options, other MCX strikes) as that symbol's own data -- confirmed
   via ~107 "[DEBUG-TEMP] Single subscription match (fuzzy)" log lines, all
   showing a fyers_symbol that had no relation to the "matched" subscription.
   That symbol's feed then went fully stale (zero real ticks) for the rest
   of the day.

These tests confirm the fallback is gone (an unmatched tick with exactly one
active subscription is now dropped, not misattributed) and that the existing
correct-match paths (HSM token, original_symbol, fuzzy-but-verified symbol
name) still work.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.fyers.streaming.fyers_adapter import FyersAdapter


@pytest.fixture
def adapter(monkeypatch):
    """A real FyersAdapter with only the data-mapping seam stubbed --
    _on_message's matching logic (what these tests exercise) doesn't need a
    real Fyers payload shape, just a mapped_data dict with a non-empty
    'symbol' field to get past the early-return guard."""
    a = FyersAdapter(access_token="dummy", userid="dummy")
    monkeypatch.setattr(
        a.data_mapper, "map_fyers_data", lambda fyers_data, requested_type: {"symbol": "placeholder", "ltp": 100.0}
    )
    return a


def _register(adapter, symbol: str, exchange: str):
    full_symbol = f"{exchange}:{symbol}"
    adapter.active_subscriptions[full_symbol] = {"exchange": exchange, "symbol": symbol, "data_type": "SymbolUpdate"}
    calls = []
    adapter.subscription_callbacks[f"SymbolUpdate_{full_symbol}"] = lambda data: calls.append(data)
    return calls


def test_unmatched_tick_with_one_active_subscription_is_dropped_not_misattributed(adapter):
    """The exact production scenario: only one active subscription, and an
    incoming tick that has no real relationship to it (no HSM token match,
    no original_symbol match, no fyers_symbol textual overlap). Must be
    dropped -- the removed fallback would have delivered it under the lone
    subscription's name instead."""
    calls = _register(adapter, "CRUDEOIL17AUG267600PE", "MCX")

    # Completely unrelated symbol, no hsm_token at all -- exactly the shape
    # of the misattributed ticks seen in production (many were NIFTY options
    # while the sole subscription was this MCX contract).
    adapter._on_message({"type": "sf", "symbol": "NIFTY2680424600PE", "original_symbol": "NSE:NIFTY2680424600PE"})

    assert calls == [], "unmatched tick must be dropped, never delivered under an unrelated symbol's name"


def test_hsm_token_exact_match_still_delivers(adapter):
    """The primary, reliable match path is untouched by the fix."""
    calls = _register(adapter, "RELIANCE", "NSE")
    full_symbol = "NSE:RELIANCE"
    adapter.hsm_to_symbol["sf|nse|123"] = full_symbol

    adapter._on_message({"type": "sf", "hsm_token": "sf|nse|123", "symbol": "RELIANCE-EQ"})

    assert len(calls) == 1
    assert calls[0]["symbol"] == "RELIANCE"
    assert calls[0]["exchange"] == "NSE"


def test_original_symbol_exact_match_still_delivers(adapter):
    """The original_symbol exact-match path is untouched by the fix."""
    calls = _register(adapter, "TCS", "NSE")
    full_symbol = "NSE:TCS"

    adapter._on_message({"type": "sf", "original_symbol": full_symbol, "symbol": "TCS-EQ"})

    assert len(calls) == 1
    assert calls[0]["symbol"] == "TCS"


def test_fuzzy_symbol_name_match_still_requires_real_textual_overlap(adapter):
    """The fuzzy-by-symbol-name fallback stays -- it's still gated on an
    actual substring relationship, unlike the removed single-subscription
    guess. Confirms it still fires for a genuinely related fyers_symbol."""
    calls = _register(adapter, "INFY", "NSE")

    adapter._on_message({"type": "sf", "symbol": "INFY-EQ"})

    assert len(calls) == 1
    assert calls[0]["symbol"] == "INFY"


def test_two_active_subscriptions_unmatched_tick_is_dropped(adapter):
    """Sanity check the removed fallback's own stated trigger condition
    (len(active_subscriptions) == 1) is gone entirely, not just narrowed --
    an unmatched tick is dropped regardless of how many subscriptions are
    active."""
    calls_a = _register(adapter, "SBIN", "NSE")
    calls_b = _register(adapter, "HDFCBANK", "NSE")

    adapter._on_message({"type": "sf", "symbol": "COMPLETELY_UNRELATED_SYMBOL"})

    assert calls_a == []
    assert calls_b == []
