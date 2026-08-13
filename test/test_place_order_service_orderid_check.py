"""
Regression test for the 2026-08-13 production incident on
Nifty_Sensex_VWAP_NoHA_Intraday: services/place_order_service.py's
place_order_with_auth() only checked the broker adapter's raw HTTP status
(res.status == 200) before declaring an order placement a success, never
checking that a real order_id actually came back. Shoonya (and any broker
following the same common REST pattern) returns HTTP 200 even when it
rejects an order at the business level -- confirmed live: an ALGO_CHK
rejection ("MKT Order type not allowed for API order") still produced
{"status": "success", "orderid": None}, which the calling strategy trusted
and then spent its full ~5-minute reprice/poll budget polling a
nonexistent order before finally erroring.

Run: uv run pytest test/test_place_order_service_orderid_check.py -v
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

# restx_api's __init__ eagerly imports every namespace, one of which
# (options_multiorder) imports FROM services.place_order_service --
# importing services.place_order_service directly as the very first thing
# triggers that chain mid-way through place_order_service's own module
# body, before place_order()/place_order_with_auth() are defined yet
# (circular import). Pre-importing restx_api first lets it fully resolve
# via its own independent path, so the later `from services import
# place_order_service` just returns the already-fully-initialized module.
import restx_api  # noqa: F401
from services import place_order_service


def _fake_res(status: int):
    return SimpleNamespace(status=status)


ORDER_DATA = {
    "symbol": "NIFTY18AUG2624350PE",
    "exchange": "NFO",
    "action": "BUY",
    "quantity": "65",
    "pricetype": "MARKET",
    "product": "MIS",
}


@pytest.fixture(autouse=True)
def _no_analyze_mode_no_events():
    with patch.object(place_order_service, "get_analyze_mode", return_value=False), \
         patch.object(place_order_service.bus, "publish"):
        yield


def test_http_200_with_missing_orderid_is_treated_as_failure():
    """The exact shape of the live incident: broker adapter returns
    HTTP 200 (res.status==200) but order_id is None because the broker
    rejected the order underneath. Must NOT be reported as success."""
    fake_broker_module = SimpleNamespace(
        place_order_api=lambda data, auth: (
            _fake_res(200),
            {"stat": "Not_Ok", "emsg": "Rejected : ALGO_CHK: MKT Order type not allowed for API order"},
            None,
        )
    )
    with patch.object(place_order_service, "import_broker_module", return_value=fake_broker_module):
        success, response, status_code = place_order_service.place_order_with_auth(
            ORDER_DATA, auth_token="tok", broker="shoonya", original_data=ORDER_DATA,
        )
    assert success is False, f"FAIL: HTTP 200 + orderid=None was reported as success: {response}"
    assert response["status"] == "error"
    assert status_code != 200, "FAIL: a failure response must not carry a 200 status code"
    print(f"HTTP 200 + orderid=None correctly reported as failure: {response} (status_code={status_code}) PASS")


def test_http_200_with_real_orderid_is_still_a_success():
    """Confirms the fix didn't break the normal, genuinely-successful path."""
    fake_broker_module = SimpleNamespace(
        place_order_api=lambda data, auth: (
            _fake_res(200),
            {"stat": "Ok", "norenordno": "26081300039029"},
            "26081300039029",
        )
    )
    with patch.object(place_order_service, "import_broker_module", return_value=fake_broker_module):
        success, response, status_code = place_order_service.place_order_with_auth(
            ORDER_DATA, auth_token="tok", broker="shoonya", original_data=ORDER_DATA,
        )
    assert success is True, f"FAIL: a genuine success (HTTP 200 + real orderid) was reported as failure: {response}"
    assert response == {"status": "success", "orderid": "26081300039029"}
    assert status_code == 200
    print(f"HTTP 200 + real orderid correctly reported as success: {response} PASS")


@pytest.mark.parametrize("response_data,expected_message", [
    ({"stat": "Not_Ok", "emsg": "Rejected : ALGO_CHK: MKT Order type not allowed for API order"},
     "Rejected : ALGO_CHK: MKT Order type not allowed for API order"),
    ({"message": "Insufficient funds"}, "Insufficient funds"),
    ({"errorMessage": "Invalid instrument"}, "Invalid instrument"),
    ({"error": "Session expired"}, "Session expired"),
    ({}, "Failed to place order"),
])
def test_rejection_message_extracted_from_broker_specific_field(response_data, expected_message):
    """response_data's shape varies by broker (emsg for Shoonya, message for
    Zerodha/Fyers, errorMessage for Dhan, error for several others) -- the
    real rejection reason must surface regardless of which field the
    broker used, not just fall back to a generic message for every broker
    except the one "message" previously special-cased."""
    fake_broker_module = SimpleNamespace(
        place_order_api=lambda data, auth: (_fake_res(200), response_data, None)
    )
    with patch.object(place_order_service, "import_broker_module", return_value=fake_broker_module):
        success, response, _status_code = place_order_service.place_order_with_auth(
            ORDER_DATA, auth_token="tok", broker="shoonya", original_data=ORDER_DATA,
        )
    assert success is False
    assert response["message"] == expected_message, (
        f"FAIL: expected message {expected_message!r}, got {response['message']!r} for response_data={response_data}"
    )
    print(f"response_data={response_data} -> message={response['message']!r} PASS")


def test_non_200_http_status_still_treated_as_failure():
    """Unchanged behavior: a genuine transport-level failure (non-200) must
    still fail regardless of order_id."""
    fake_broker_module = SimpleNamespace(
        place_order_api=lambda data, auth: (_fake_res(500), {"message": "Internal Server Error"}, None)
    )
    with patch.object(place_order_service, "import_broker_module", return_value=fake_broker_module):
        success, response, status_code = place_order_service.place_order_with_auth(
            ORDER_DATA, auth_token="tok", broker="shoonya", original_data=ORDER_DATA,
        )
    assert success is False
    assert status_code == 500
    print(f"Non-200 HTTP status correctly treated as failure: status_code={status_code} PASS")
