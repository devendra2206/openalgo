"""
Regression tests for the 2026-07-30 fix to ShoonyaWebSocketAdapter.subscribe()
(broker/shoonya/streaming/shoonya_adapter.py).

Background: websocket_proxy/server.py's subscribe_client() was fixed
(REDUNDANT_SUBSCRIBE_STALE_BYPASS_SEC) to force a real adapter.subscribe()
call when a shared symbol looks genuinely stuck, instead of trusting "another
client already holds it" as proof of health. Shoonya's OWN adapter has an
independent, second "already subscribed" check -- subscribe()'s
already_ws_subscribed, matched by correlation_id prefix -- that can ALSO
silently skip sending the real WebSocket subscribe frame, even when the proxy
correctly decided a genuine resubscribe attempt was needed. This is a second,
independent instance of the same bug class, one layer deeper.

Unlike Fyers, Shoonya's unsubscribe() genuinely clears this bookkeeping --
but the companion strategy-side fix (PriceStream no longer calls
unsubscribe_ltp() before subscribe_ltp(), since that call was a no-op for
Fyers) means that reset no longer happens automatically for ANY broker,
Shoonya included. Without this fix, Shoonya's cheap per-symbol retry path
would become permanently unable to force a real resubscribe once a token's
bookkeeping goes stale.

Fix: ShoonyaWebSocketAdapter._is_token_genuinely_stale() -- a token
registered as already_ws_subscribed is only trusted as genuinely streaming
if it has ticked recently (_token_last_tick, updated in
_process_market_message). Otherwise subscribe() bypasses the skip and sends
a real WS resubscribe frame anyway.

These tests exercise the real ShoonyaWebSocketAdapter methods via
__new__() (bypassing __init__'s real ZMQ/network setup, irrelevant to this
pure decision logic and its bookkeeping).
"""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import order matters here: websocket_proxy/__init__.py itself imports
# ShoonyaWebSocketAdapter, and shoonya_adapter.py imports BaseBrokerWebSocketAdapter
# from websocket_proxy.base_adapter -- importing websocket_proxy.base_adapter
# directly first (bypassing the package __init__) avoids the circular import
# that direct-importing shoonya_adapter first would otherwise trigger.
import websocket_proxy.base_adapter  # noqa: F401

from broker.shoonya.streaming.shoonya_adapter import ShoonyaWebSocketAdapter


def _make_adapter():
    """A ShoonyaWebSocketAdapter instance with only the state
    _is_token_genuinely_stale/subscribe/_process_market_message actually
    touch -- constructed via __new__ to skip __init__'s real ZMQ bind and
    connection setup."""
    import threading

    a = ShoonyaWebSocketAdapter.__new__(ShoonyaWebSocketAdapter)
    a.logger = MagicMock()
    a.connected = True
    a.lock = threading.Lock()
    a.subscriptions = {}
    a.token_to_symbol = {}
    a._token_to_cids = {}
    a._token_last_tick = {}
    a._token_first_subscribed_at = {}
    a.ws_subscription_refs = {}
    a.zmq_port = 5555
    return a


TOKEN = "26000"
SYMBOL, EXCHANGE, MODE = "NIFTY", "NSE_INDEX", 2


def test_not_stale_when_recently_ticked():
    a = _make_adapter()
    now = time.time()
    a._token_first_subscribed_at[TOKEN] = now - 500
    a._token_last_tick[TOKEN] = now - 5

    assert a._is_token_genuinely_stale(TOKEN) is False


def test_stale_when_used_to_tick_then_went_silent():
    a = _make_adapter()
    now = time.time()
    a._token_first_subscribed_at[TOKEN] = now - 500
    a._token_last_tick[TOKEN] = now - (ShoonyaWebSocketAdapter.SUBSCRIBE_STALE_BYPASS_SEC + 1)

    assert a._is_token_genuinely_stale(TOKEN) is True


def test_stale_when_never_ticked_and_subscribed_a_while():
    a = _make_adapter()
    now = time.time()
    a._token_first_subscribed_at[TOKEN] = now - (
        ShoonyaWebSocketAdapter.SUBSCRIBE_STALE_BYPASS_SEC + 1
    )
    # _token_last_tick deliberately has no entry -- never ticked

    assert a._is_token_genuinely_stale(TOKEN) is True


def test_not_stale_when_never_ticked_but_just_subscribed():
    a = _make_adapter()
    now = time.time()
    a._token_first_subscribed_at[TOKEN] = now - 2  # just now

    assert a._is_token_genuinely_stale(TOKEN) is False


def test_not_stale_when_no_baseline_known():
    a = _make_adapter()
    # Neither dict has an entry for this token.
    assert a._is_token_genuinely_stale(TOKEN) is False


def _subscribe_kwargs(token=TOKEN):
    return dict(symbol=SYMBOL, exchange=EXCHANGE, mode=MODE, depth_level=5)


def test_subscribe_sends_real_ws_frame_for_genuinely_fresh_symbol(monkeypatch):
    a = _make_adapter()
    monkeypatch.setattr(
        a, "_validate_subscription_params", lambda symbol, exchange, mode: True
    )
    monkeypatch.setattr(
        a, "_get_token_info", lambda symbol, exchange: {"token": TOKEN, "brexchange": "NSE"}
    )
    monkeypatch.setattr(
        a,
        "_create_subscription",
        lambda symbol, exchange, mode, depth_level, token_info: {
            "symbol": symbol, "exchange": exchange, "mode": mode,
            "depth_level": depth_level, "token": TOKEN, "scrip": f"NSE|{TOKEN}",
        },
    )
    ws_calls = []
    monkeypatch.setattr(a, "_websocket_subscribe", lambda sub: ws_calls.append(sub))

    a.subscribe(**_subscribe_kwargs())

    assert len(ws_calls) == 1


def test_subscribe_skips_real_ws_frame_when_already_subscribed_and_healthy(monkeypatch):
    """The existing, correct optimization: a SECOND subscribe for a token
    that's already subscribed AND genuinely ticking must not resend the WS
    frame."""
    a = _make_adapter()
    monkeypatch.setattr(
        a, "_validate_subscription_params", lambda symbol, exchange, mode: True
    )
    monkeypatch.setattr(
        a, "_get_token_info", lambda symbol, exchange: {"token": TOKEN, "brexchange": "NSE"}
    )
    monkeypatch.setattr(
        a,
        "_create_subscription",
        lambda symbol, exchange, mode, depth_level, token_info: {
            "symbol": symbol, "exchange": exchange, "mode": mode,
            "depth_level": depth_level, "token": TOKEN, "scrip": f"NSE|{TOKEN}",
        },
    )
    ws_calls = []
    monkeypatch.setattr(a, "_websocket_subscribe", lambda sub: ws_calls.append(sub))

    a.subscribe(**_subscribe_kwargs())  # first call: genuinely fresh
    assert len(ws_calls) == 1

    a._token_last_tick[TOKEN] = time.time()  # ticking normally
    a.subscribe(**_subscribe_kwargs())  # second call: already subscribed, healthy

    assert len(ws_calls) == 1, "must not resend WS frame for a healthy, already-subscribed token"


def test_subscribe_bypasses_and_resends_when_already_subscribed_but_stuck(monkeypatch):
    """The core fix: a token registered as already_ws_subscribed but with
    zero ticks since it was first subscribed (well past the bypass window)
    must get a real, fresh WS resubscribe frame anyway."""
    a = _make_adapter()
    monkeypatch.setattr(
        a, "_validate_subscription_params", lambda symbol, exchange, mode: True
    )
    monkeypatch.setattr(
        a, "_get_token_info", lambda symbol, exchange: {"token": TOKEN, "brexchange": "NSE"}
    )
    monkeypatch.setattr(
        a,
        "_create_subscription",
        lambda symbol, exchange, mode, depth_level, token_info: {
            "symbol": symbol, "exchange": exchange, "mode": mode,
            "depth_level": depth_level, "token": TOKEN, "scrip": f"NSE|{TOKEN}",
        },
    )
    ws_calls = []
    monkeypatch.setattr(a, "_websocket_subscribe", lambda sub: ws_calls.append(sub))

    a.subscribe(**_subscribe_kwargs())  # first call: genuinely fresh
    assert len(ws_calls) == 1

    # Simulate time passing with zero ticks ever received for this token.
    a._token_first_subscribed_at[TOKEN] = time.time() - (
        ShoonyaWebSocketAdapter.SUBSCRIBE_STALE_BYPASS_SEC + 1
    )

    a.subscribe(**_subscribe_kwargs())  # retry: stuck, must bypass and resend

    assert len(ws_calls) == 2, "must send a real WS resubscribe frame for a stuck token"


def test_process_market_message_updates_token_last_tick():
    a = _make_adapter()
    a.token_to_symbol[TOKEN] = (SYMBOL, EXCHANGE)
    correlation_id = f"{SYMBOL}_{EXCHANGE}_{MODE}_abc"
    a.subscriptions[correlation_id] = {
        "symbol": SYMBOL, "exchange": EXCHANGE, "mode": MODE,
        "depth_level": 5, "token": TOKEN, "scrip": f"NSE|{TOKEN}",
    }
    a._token_to_cids[TOKEN] = {correlation_id}
    a.market_cache = MagicMock()
    a.market_cache.update.return_value = {}

    import broker.shoonya.streaming.shoonya_adapter as shoonya_adapter_module

    real_process_message = shoonya_adapter_module.ShoonyaWebSocketAdapter._process_market_message
    # Avoid publishing to a real ZMQ socket -- stub only the final publish step.
    a.publish_market_data = MagicMock()
    a._should_process_message = lambda msg_type, mode: False  # skip normalization entirely

    before = time.time()
    real_process_message(a, {"t": "tf", "tk": TOKEN})

    assert TOKEN in a._token_last_tick
    assert a._token_last_tick[TOKEN] >= before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
