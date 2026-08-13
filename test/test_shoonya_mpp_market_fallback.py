"""
Regression test for the 2026-08-13 production incident: Shoonya's
transform_data() fell back to sending a raw MARKET order ("MKT") when its
Market Price Protection (MPP) couldn't compute a safe price from a bad
quote -- but Shoonya's own ALGO_CHK compliance rule rejects ALL
API-submitted MARKET orders outright, so that "fallback" was actually a
guaranteed rejection, not a safe one (this file's own module already knew
this for SL-M orders -- see the pre-existing SL-M-via-trigger-price
fallback -- just not for plain MARKET, which has no trigger price to fall
back to).

Fix: retry the quote fetch once (a bad quote here was confirmed live to be
a momentary glitch -- a fresh quote for the same symbol 7 seconds later
was completely normal) before giving up; if a MARKET order still has no
valid quote after that, raise instead of silently sending a doomed MKT
order. SL-M's existing trigger-price-based fallback is unchanged.

Run: uv run pytest test/test_shoonya_mpp_market_fallback.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from broker.shoonya.mapping import transform_data as td

ORDER_DATA = {
    "apikey": "user123",
    "symbol": "NIFTY18AUG2624350PE",
    "exchange": "NFO",
    "action": "BUY",
    "quantity": "65",
    "pricetype": "MARKET",
    "price": "0",
    "trigger_price": "0",
    "disclosed_quantity": "0",
    "product": "MIS",
}

BAD_QUOTE = {"ltp": 24341.95, "bid": 0.0, "ask": 0.0, "tick_size": 0.05}
GOOD_QUOTE = {"ltp": 68.55, "bid": 68.40, "ask": 68.55, "tick_size": 0.05}


@pytest.fixture(autouse=True)
def _no_sleep_no_db():
    """Speeds up the retry-delay sleep and stubs the DB-backed symbol
    lookups transform_data() calls outside the MPP branch (unrelated to
    what this test is verifying)."""
    with patch.object(td.time, "sleep"), \
         patch.object(td, "get_br_symbol", return_value="NIFTY18AUG26P24350"):
        yield


def _run_transform(get_quotes_side_effect, pricetype="MARKET", trigger_price="0"):
    data = dict(ORDER_DATA, pricetype=pricetype, trigger_price=trigger_price)
    fake_broker_data_instance = MagicMock()
    fake_broker_data_instance.get_quotes.side_effect = get_quotes_side_effect
    fake_broker_data_cls = MagicMock(return_value=fake_broker_data_instance)
    with patch("broker.shoonya.api.data.BrokerData", fake_broker_data_cls):
        return td.transform_data(data, token="12345", auth_token="tok")


def test_market_order_raises_when_quote_stays_invalid_after_retry():
    """Both attempts return a bad quote -- must raise, never silently send
    a raw MKT order (guaranteed ALGO_CHK rejection on Shoonya)."""
    with pytest.raises(RuntimeError, match="MPP could not obtain a valid two-sided quote"):
        _run_transform(get_quotes_side_effect=[BAD_QUOTE, BAD_QUOTE])
    print("MARKET order with a persistently bad quote correctly raises instead of sending MKT PASS")


def test_market_order_recovers_via_retry():
    """First attempt bad, second attempt good -- the retry must catch it
    and convert to a protected LIMIT, matching the real incident (a good
    quote reappeared just 7 seconds after the bad one)."""
    result = _run_transform(get_quotes_side_effect=[BAD_QUOTE, GOOD_QUOTE])
    assert result["prctyp"] == "LMT", f"FAIL: expected LMT after retry recovered, got {result['prctyp']}"
    assert float(result["prc"]) > 0
    print(f"MARKET order recovered via retry: prctyp={result['prctyp']} prc={result['prc']} PASS")


def test_market_order_converts_normally_on_first_good_quote():
    """No retry needed -- must not regress the normal, already-working path."""
    result = _run_transform(get_quotes_side_effect=[GOOD_QUOTE])
    assert result["prctyp"] == "LMT"
    assert float(result["prc"]) > 0
    print(f"MARKET order converted normally on first good quote: prc={result['prc']} PASS")


def test_sl_m_still_falls_back_to_trigger_price_when_quote_bad():
    """SL-M's existing safe fallback (derive a protective limit from the
    trigger price, since SL-M always has one) must be unchanged -- only
    plain MARKET (no trigger price available) gets the new raise."""
    result = _run_transform(
        get_quotes_side_effect=[BAD_QUOTE, BAD_QUOTE], pricetype="SL-M", trigger_price="65.0",
    )
    assert result["prctyp"] == "SL-LMT", f"FAIL: expected SL-LMT fallback, got {result['prctyp']}"
    assert float(result["prc"]) > 0
    print(f"SL-M with bad quote still safely falls back via trigger price: prc={result['prc']} PASS")


def test_market_order_raises_when_get_quotes_itself_raises_both_attempts():
    """An exception during the quote fetch itself (not just a bad-shaped
    quote) must be treated the same as an invalid quote -- retried once,
    then raised for MARKET rather than falling through to MKT."""
    with pytest.raises(RuntimeError, match="MPP could not obtain a valid two-sided quote"):
        _run_transform(get_quotes_side_effect=[ConnectionError("timeout"), ConnectionError("timeout")])
    print("MARKET order with get_quotes() itself raising twice correctly raises instead of sending MKT PASS")
