"""
Regression tests for the 2026-07-30 fix to websocket_proxy/server.py's
redundant-subscribe skip (WebSocketProxy.subscribe_client).

Background: subscribe_client() has an optimization -- if ANY client already
holds a (symbol, exchange, mode) subscription, skip re-sending subscribe to
the broker adapter, since a redundant re-subscribe can disrupt an
already-healthy shared feed for every OTHER client subscribed to the same
symbol. Confirmed in production (2026-07-30) that this assumption breaks
once the underlying broker-level subscription itself has silently gone bad:
two INDEPENDENT strategy processes (Batman, Combined) both retried
unsubscribe/subscribe for NIFTY.NSE_INDEX every 15-30s for 7+ minutes with
zero recovery, because each one's attempt saw the OTHER's stale bookkeeping
entry in subscription_index and got silently skipped -- neither could ever
force a real adapter.subscribe() call to actually refresh the broken
broker-side subscription.

Fix: WebSocketProxy._is_subscription_genuinely_stale() -- a symbol currently
held by another client is only trusted as "healthy, skip the redundant
subscribe" if it has ticked recently. If it's been held for a while with
zero ticks ever, or it used to tick and has gone silent, the skip is
bypassed and a real adapter.subscribe() fires anyway.

These tests exercise the real WebSocketProxy methods (not a
reimplementation) via WebSocketProxy.__new__() + manually-set instance
state, avoiding the ZMQ socket bind / port checks in __init__ (irrelevant to
this pure decision logic and its bookkeeping).
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websocket_proxy.server import WebSocketProxy


def _make_proxy():
    """A WebSocketProxy instance with only the state
    _is_subscription_genuinely_stale/_log_stale_symbols actually touch --
    constructed via __new__ to skip __init__'s real ZMQ bind/port checks."""
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.subscription_index = {}
    proxy.subscription_first_held_at = {}
    proxy.last_message_time = {}
    proxy._last_stale_symbol_check = 0.0
    proxy._last_stale_symbol_warn = {}
    proxy._stale_check_interval = 30
    proxy._stale_tick_warn_seconds = 120
    return proxy


SUB_KEY = ("NIFTY", "NSE_INDEX", 1)


def test_not_stale_when_recently_ticked():
    proxy = _make_proxy()
    now = time.time()
    proxy.subscription_first_held_at[SUB_KEY] = now - 500
    proxy.last_message_time[SUB_KEY] = now - 5  # ticked 5s ago

    assert proxy._is_subscription_genuinely_stale(SUB_KEY, now) is False


def test_stale_when_used_to_tick_then_went_silent():
    proxy = _make_proxy()
    now = time.time()
    proxy.subscription_first_held_at[SUB_KEY] = now - 500
    proxy.last_message_time[SUB_KEY] = now - (
        WebSocketProxy.REDUNDANT_SUBSCRIBE_STALE_BYPASS_SEC + 1
    )

    assert proxy._is_subscription_genuinely_stale(SUB_KEY, now) is True


def test_stale_when_never_ticked_and_held_a_while():
    """The exact production scenario: subscribed since the mass-reconnect,
    zero ticks ever since (last_message_time has no entry at all)."""
    proxy = _make_proxy()
    now = time.time()
    proxy.subscription_first_held_at[SUB_KEY] = now - (
        WebSocketProxy.REDUNDANT_SUBSCRIBE_STALE_BYPASS_SEC + 1
    )
    # last_message_time deliberately has no entry for SUB_KEY

    assert proxy._is_subscription_genuinely_stale(SUB_KEY, now) is True


def test_not_stale_when_never_ticked_but_just_subscribed():
    """Must NOT bypass the skip for a symbol that's genuinely just been
    subscribed a moment ago -- that's the normal "too early to judge" case,
    not a stuck subscription."""
    proxy = _make_proxy()
    now = time.time()
    proxy.subscription_first_held_at[SUB_KEY] = now - 2  # just now

    assert proxy._is_subscription_genuinely_stale(SUB_KEY, now) is False


def test_not_stale_when_held_since_unknown():
    """Defensive: if held_since was somehow never recorded (shouldn't
    happen given subscribe_client always sets it), don't treat that as
    proof of staleness -- absence of data is not evidence of a stuck feed."""
    proxy = _make_proxy()
    now = time.time()
    # Neither subscription_first_held_at nor last_message_time has an entry.

    assert proxy._is_subscription_genuinely_stale(SUB_KEY, now) is False


def test_log_stale_symbols_flags_never_ticked_symbol(caplog):
    """Regression for the bug caught mid-investigation: the first version
    of _log_stale_symbols skipped any sub_key with last_message_time still
    None, reasoning "too early to judge" -- which meant a symbol broken
    from its very first subscribe (last_message_time permanently None)
    would NEVER get flagged. Must now warn using subscription_first_held_at
    as the fallback clock."""
    proxy = _make_proxy()
    now = time.time()
    proxy._last_stale_symbol_check = now - 60  # force the interval gate open
    proxy.subscription_index = {SUB_KEY: {12345}}
    proxy.subscription_first_held_at[SUB_KEY] = now - 130  # older than threshold=120
    # last_message_time deliberately has no entry -- never ticked

    import logging

    with caplog.at_level(logging.WARNING):
        proxy._log_stale_symbols()

    assert any(
        "NEVER ticked" in r.message and "NIFTY" in r.message for r in caplog.records
    ), f"expected a NEVER-ticked warning, got: {[r.message for r in caplog.records]}"


def test_log_stale_symbols_does_not_flag_freshly_held_never_ticked_symbol():
    """A symbol subscribed moments ago with no tick yet must NOT be flagged
    -- that's the ordinary startup window, not a stuck feed."""
    proxy = _make_proxy()
    now = time.time()
    proxy._last_stale_symbol_check = now - 60
    proxy.subscription_index = {SUB_KEY: {12345}}
    proxy.subscription_first_held_at[SUB_KEY] = now - 5  # just subscribed

    proxy._log_stale_symbols()

    assert proxy._last_stale_symbol_warn == {}
