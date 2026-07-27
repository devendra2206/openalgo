"""
Regression tests for FyersWebSocketAdapter's bounded-wait background
reconnect (broker/fyers/streaming/fyers_websocket_adapter.py).

Background: subscribe() used to call connect() inline and block for up to
~15s (FyersAdapter.connect()'s time.sleep(0.1) poll loop waiting for the WS
handshake/auth). subscribe() is invoked synchronously from
websocket_proxy/server.py's asyncio event loop, which also handles ping/pong
for every connected proxy client -- a 15s block there freezes ping/pong for
everyone at once, tripping every other strategy process's WS ping-timeout
(10s in the openalgo SDK) simultaneously. That's a correlated mass-disconnect
unrelated to any single client's own health.

The fix dispatches connect() to a background thread and waits only a short,
bounded window (default 2s) for it to finish:
  - A genuine auth failure (expired/invalid token) is almost always rejected
    fast by the broker, well under 2s, so its real error message still
    reaches ConnectionPool.subscribe()'s auth-error keyword match
    (websocket_proxy/connection_manager.py) for token-refresh recovery.
  - Only the genuinely slow case (no response at all) falls through past the
    wait and returns a generic "retry shortly" message, capping the
    worst-case event-loop block at ~2s instead of ~15s.

These tests exercise the real FyersWebSocketAdapter class (not a
reimplementation), with only FyersWebSocketAdapter.connect() mocked out to
control timing/outcome -- everything else (locking, threading, the Event-
based bounded wait) is the actual production code path.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.fyers.streaming.fyers_websocket_adapter import FyersWebSocketAdapter


@pytest.fixture
def adapter():
    a = FyersWebSocketAdapter()
    a.connected = False
    a.fyers_adapter = None
    yield a
    a.disconnect()


def test_fast_auth_failure_returns_real_message(adapter):
    """A fast-failing connect() (immediate auth rejection) must return its
    REAL error message well under the wait window, so ConnectionPool's
    auth-error keyword match still triggers token-refresh recovery."""

    def fake_connect():
        time.sleep(0.05)
        return {"status": "error", "message": "invalid token"}

    adapter.connect = fake_connect
    start = time.time()
    result = adapter._trigger_background_reconnect(wait_seconds=2.0)
    elapsed = time.time() - start

    assert result is not None, "expected the real result for a fast failure, got None (timed out)"
    assert result["message"] == "invalid token"
    assert elapsed < 1.0, f"should return almost immediately on fast failure, took {elapsed:.2f}s"


def test_fast_success_returns_real_result(adapter):
    def fake_connect():
        time.sleep(0.05)
        adapter.connected = True
        return {"status": "success", "message": "Connected to Fyers WebSocket"}

    adapter.connect = fake_connect
    start = time.time()
    result = adapter._trigger_background_reconnect(wait_seconds=2.0)
    elapsed = time.time() - start

    assert result is not None
    assert result["status"] == "success"
    assert elapsed < 1.0


def test_slow_connect_is_capped_at_wait_seconds(adapter):
    """Simulates the original bug: connect() taking up to ~15s. The caller
    must not block that long -- it should give up at wait_seconds and
    return None, capping the event-loop stall instead of freezing."""

    def fake_connect():
        time.sleep(5.0)  # stands in for the real-world ~15s hang
        return {
            "status": "error",
            "message": "Failed to authenticate with Fyers HSM WebSocket (timeout)",
        }

    adapter.connect = fake_connect
    start = time.time()
    result = adapter._trigger_background_reconnect(wait_seconds=1.0)
    elapsed = time.time() - start

    assert result is None, f"expected None (timed out), got {result}"
    assert 0.9 <= elapsed < 2.0, f"should cap at ~wait_seconds (1.0s), took {elapsed:.2f}s"


def test_concurrent_callers_share_one_reconnect_attempt(adapter):
    """Multiple concurrent subscribe()-style callers arriving while a
    reconnect is already in flight must not each spawn their own background
    thread -- they should all observe the SAME attempt's result, and
    connect() must be invoked exactly once."""
    call_count = {"n": 0}
    count_lock = threading.Lock()

    def fake_connect():
        with count_lock:
            call_count["n"] += 1
        time.sleep(0.5)
        return {"status": "success", "message": "ok"}

    adapter.connect = fake_connect

    results = []
    results_lock = threading.Lock()

    def caller():
        r = adapter._trigger_background_reconnect(wait_seconds=2.0)
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=caller) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count["n"] == 1, f"connect() should be called exactly once, was called {call_count['n']}x"
    assert len(results) == 5
    assert all(r is not None and r["status"] == "success" for r in results)


def test_subscribe_does_not_block_past_wait_window(adapter):
    """End-to-end: subscribe() itself, with needs_reconnect True and a slow
    connect() in flight, must return promptly rather than hang -- this is
    the actual call site strategy scripts and the proxy depend on."""

    def fake_connect():
        time.sleep(5.0)
        return {"status": "success", "message": "ok"}

    adapter.connect = fake_connect

    start = time.time()
    response = adapter.subscribe("NIFTY", "NSE_INDEX", mode=1, depth_level=5)
    elapsed = time.time() - start

    # subscribe()'s internal wait defaults to 2s (see
    # _trigger_background_reconnect's default); it must not have waited for
    # the full 5s fake connect() to complete.
    assert elapsed < 3.0, f"subscribe() blocked for {elapsed:.2f}s -- should have returned near the 2s bound"
    assert response.get("status") == "error"
    assert "retry shortly" in response.get("message", "").lower()
