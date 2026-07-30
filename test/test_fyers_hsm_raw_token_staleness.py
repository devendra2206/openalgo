"""
Regression tests for the 2026-07-30 raw-layer diagnostic added to
FyersHSMWebSocket (broker/fyers/streaming/fyers_hsm_websocket.py) during the
PriceStream/NIFTY.NSE_INDEX staleness investigation.

Background: earlier fixes in this investigation (websocket_proxy/server.py's
stale-bypass, broker/fyers/streaming/fyers_adapter.py's batch-mixing and
unsubscribe cleanup) made the SYSTEM self-heal within minutes instead of
staying permanently broken, but did not explain WHY a symbol's feed goes
dead for those minutes in the first place. Every diagnostic added until now
lived above FyersHSMWebSocket -- the actual raw binary WebSocket client
that decodes Fyers' HSM protocol -- so none of them could distinguish
"Fyers never sent anything for this token" from "it arrived here but got
lost somewhere in OpenAlgo's own dispatch chain."

_token_last_seen tracks, per HSM token string, the last time ANY frame
referencing that exact token was parsed at this raw layer -- updated in
_parse_snapshot_data and _parse_update_data, before any symbol-mapping or
callback dispatch. _log_stale_tokens (run periodically from
_health_check_loop) warns when a currently-subscribed token has gone
silent at THIS layer, distinguishing "never seen at all" (points upstream,
at Fyers' own server) from "was seen before, went quiet" (same, but with a
duration).

These tests exercise the real FyersHSMWebSocket._log_stale_tokens() via a
minimally-constructed instance (bypassing __init__'s real network setup),
so the diagnostic's decision logic is verified without needing an actual
WebSocket connection or hand-built binary protocol frames.
"""

import os
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.fyers.streaming.fyers_hsm_websocket import FyersHSMWebSocket


def _make_client():
    """A FyersHSMWebSocket instance with only the state _log_stale_tokens
    actually touches -- constructed via __new__ to skip __init__'s real
    network/auth setup (irrelevant to this pure diagnostic logic)."""
    client = FyersHSMWebSocket.__new__(FyersHSMWebSocket)
    client._pending_hsm_symbols = []
    client._token_last_seen = {}
    client._token_stale_warn_threshold = 60.0
    client._token_stale_last_warn = {}
    client.symbol_mappings = {}
    import logging

    client.logger = logging.getLogger("fyers_hsm_websocket")
    return client


TOKEN = "sf|NSE|26000"


def test_no_warning_for_recently_seen_token():
    client = _make_client()
    client._pending_hsm_symbols = [TOKEN]
    client._token_last_seen[TOKEN] = time.time() - 5

    with_warnings = []
    client.logger.warning = lambda msg: with_warnings.append(msg)

    client._log_stale_tokens()

    assert with_warnings == []


def test_warns_never_seen_token_distinctly():
    """The "never seen at all" case must be worded distinctly from "went
    silent" -- it's the strongest possible signal that Fyers itself never
    sent anything for this token, not that OpenAlgo lost it partway."""
    client = _make_client()
    client._pending_hsm_symbols = [TOKEN]
    # _token_last_seen deliberately has no entry -- never seen

    warnings = []
    client.logger.warning = lambda msg: warnings.append(msg)

    client._log_stale_tokens()

    matching = [msg for msg in warnings if TOKEN in msg]
    assert matching, f"expected a warning mentioning the token, got: {warnings}"
    assert "never seen at all" in matching[0]


def test_warns_gone_silent_token_distinctly():
    client = _make_client()
    client._pending_hsm_symbols = [TOKEN]
    client._token_last_seen[TOKEN] = time.time() - (
        client._token_stale_warn_threshold + 5
    )

    warnings = []
    client.logger.warning = lambda msg: warnings.append(msg)

    client._log_stale_tokens()

    matching = [msg for msg in warnings if TOKEN in msg]
    assert matching, f"expected a warning mentioning the token, got: {warnings}"
    assert "silent for" in matching[0]


def test_throttles_repeat_warnings_within_threshold():
    """Must not re-warn on every health-check cycle (every 30s) once
    already warned within the same stale window -- only once per
    threshold, same throttling shape as the proxy-side diagnostics."""
    client = _make_client()
    client._pending_hsm_symbols = [TOKEN]
    # never seen

    warnings = []
    client.logger.warning = lambda msg: warnings.append(msg)

    client._log_stale_tokens()
    client._log_stale_tokens()  # immediately again -- should be throttled

    assert len(warnings) == 1


def test_no_symbols_subscribed_is_a_noop():
    client = _make_client()
    client._pending_hsm_symbols = []

    warnings = []
    client.logger.warning = lambda msg: warnings.append(msg)

    client._log_stale_tokens()

    assert warnings == []


def test_parse_snapshot_data_updates_token_last_seen():
    """_parse_snapshot_data must record _token_last_seen the moment a
    topic_name is resolved, regardless of scrip/index/depth type -- this is
    the actual production wiring the diagnostic depends on."""
    client = _make_client()
    client.subscriptions = {}
    client.symbol_mappings = {}

    topic_name = "if|NSE|NIFTY"
    topic_name_bytes = topic_name.encode("utf-8")
    # Layout expected by _parse_snapshot_data: topic_id (H, native byte
    # order to match struct.unpack("H", ...) in the source), name_len (B),
    # name bytes. Only topic_name matters for this test.
    data = bytearray()
    data += struct.pack("H", 100)  # topic_id
    data += bytes([len(topic_name_bytes)])
    data += topic_name_bytes

    before = time.time()
    client._parse_snapshot_data(data, 0)

    assert topic_name in client._token_last_seen
    assert client._token_last_seen[topic_name] >= before
