"""
Regression test for a 2026-08-01 production finding: Fyers' /data/symbol-token
API does not reliably echo back the exact symbol string it was given, even for
a single, unbatched request. Confirmed in production for MCX option contracts:
requesting "MCX:CRUDEOIL17AUG267850CE" got back a `validSymbol` key of
"MCX:CRUDEOIL26AUG7850CE" (day-of-month dropped) -- while the underlying
fytoken/HSM token resolved correctly and ticks kept flowing under the hood.

FyersTokenConverter.convert_symbols_to_hsm() used to trust that echoed key as
the value stored in token_mappings (hsm_token -> symbol). Downstream,
FyersAdapter.subscribe_symbols() joins this value against its own
get_br_symbol() output to resolve which OpenAlgo symbol a tick belongs to. Any
mismatch between what we sent and what Fyers echoed back broke that join
permanently for the symbol's HSM token -- "HSM token not in mappings" on every
single subsequent tick, with the WS layer falling back to REST polling
mid-trade (see docs/CUSTOMIZATIONS.md's 2026-08-01 entry).

This is the same underlying Fyers API unreliability already documented for the
2026-07-30 fix (that one was about NOT preserving symbol *order/pairing*
across multiple symbols in one request; this one is about NOT preserving the
symbol *string* even for a single request) -- so the fix follows the same
philosophy: don't trust Fyers to round-trip what we sent. When the request is
unambiguous (exactly one brsymbol requested, exactly one valid symbol
returned), use our own sent brsymbol as the mapping value instead.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.fyers.streaming.fyers_token_converter import FyersTokenConverter


def _mock_response(valid_symbol: dict, invalid_symbol=None):
    resp = MagicMock()
    resp.json.return_value = {
        "s": "ok",
        "validSymbol": valid_symbol,
        "invalidSymbol": invalid_symbol or [],
    }
    return resp


def test_single_symbol_uses_our_own_brsymbol_not_fyers_echo():
    """The core regression: Fyers echoes a differently-formatted symbol string
    for the one instrument we asked about; we must still map the HSM token
    back to what WE sent, not what Fyers echoed."""
    converter = FyersTokenConverter(access_token="app:token")

    sent_brsymbol = "MCX:CRUDEOIL17AUG267850CE"
    fyers_echoed_symbol = "MCX:CRUDEOIL26AUG7850CE"  # day-of-month dropped
    fytoken = "1120260817573462"  # ex_sg=1120 (mcx_fo), suffix=573462

    with patch(
        "broker.fyers.streaming.fyers_token_converter.requests.post",
        return_value=_mock_response({fyers_echoed_symbol: fytoken}),
    ):
        hsm_tokens, token_mappings, invalid_symbols = converter.convert_symbols_to_hsm(
            [sent_brsymbol], data_type="SymbolUpdate"
        )

    assert invalid_symbols == []
    assert len(hsm_tokens) == 1
    hsm_token = hsm_tokens[0]
    assert hsm_token == "sf|mcx_fo|573462"
    # The critical assertion: mapping value is OUR sent brsymbol, not Fyers' echo.
    assert token_mappings[hsm_token] == sent_brsymbol
    assert token_mappings[hsm_token] != fyers_echoed_symbol


def test_single_symbol_matching_echo_still_works():
    """When Fyers happens to echo back the exact same string, behavior is
    unchanged (still uses our sent brsymbol, which is identical anyway)."""
    converter = FyersTokenConverter(access_token="app:token")
    sent_brsymbol = "NSE:RELIANCE-EQ"
    fytoken = "1010260817123456"

    with patch(
        "broker.fyers.streaming.fyers_token_converter.requests.post",
        return_value=_mock_response({sent_brsymbol: fytoken}),
    ):
        hsm_tokens, token_mappings, invalid_symbols = converter.convert_symbols_to_hsm(
            [sent_brsymbol], data_type="SymbolUpdate"
        )

    assert invalid_symbols == []
    assert token_mappings[hsm_tokens[0]] == sent_brsymbol


def test_multi_symbol_batch_falls_back_to_echoed_key_ambiguous():
    """With multiple symbols requested in one call, we can't unambiguously
    correlate which returned entry corresponds to which sent brsymbol -- keep
    the old (still-fallible, but no worse than before) behavior of trusting
    Fyers' echoed key rather than guessing a pairing."""
    converter = FyersTokenConverter(access_token="app:token")
    sent = ["MCX:CRUDEOIL17AUG267850CE", "MCX:CRUDEOIL17AUG267900CE"]
    fytoken_a = "1120260817573462"
    fytoken_b = "1120260817573621"

    with patch(
        "broker.fyers.streaming.fyers_token_converter.requests.post",
        return_value=_mock_response({sent[0]: fytoken_a, sent[1]: fytoken_b}),
    ):
        hsm_tokens, token_mappings, invalid_symbols = converter.convert_symbols_to_hsm(
            sent, data_type="SymbolUpdate"
        )

    assert invalid_symbols == []
    assert len(hsm_tokens) == 2
    # Ambiguous multi-symbol case: values are whatever Fyers echoed (unchanged
    # pre-existing behavior), not silently remapped.
    assert set(token_mappings.values()) == set(sent)


def test_warns_when_echoed_symbol_differs_from_sent():
    converter = FyersTokenConverter(access_token="app:token")
    sent_brsymbol = "MCX:CRUDEOIL17AUG267850CE"
    fyers_echoed_symbol = "MCX:CRUDEOIL26AUG7850CE"
    fytoken = "1120260817573462"

    with patch(
        "broker.fyers.streaming.fyers_token_converter.requests.post",
        return_value=_mock_response({fyers_echoed_symbol: fytoken}),
    ):
        with patch.object(converter.logger, "warning") as mock_warning:
            converter.convert_symbols_to_hsm([sent_brsymbol], data_type="SymbolUpdate")

    assert any(
        "different symbol string" in str(call.args[0]) for call in mock_warning.call_args_list
    )
