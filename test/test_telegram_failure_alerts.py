"""
Regression tests for the 2026-08-13 Telegram failure-only alert change:

- services/telegram_alert_service.py: send_failure_alert()/format_failure_details()
  (new) -- the Telegram channel now only ever sends when an order
  placement/modify/cancel genuinely fails.
- subscribers/telegram_subscriber.py: every success/completion handler is
  now a no-op; the three *_failed handlers call send_failure_alert().
- subscribers/whatsapp_subscriber.py: every handler is now a no-op
  (WhatsApp order alerts disabled entirely).

Run: uv run pytest test/test_telegram_failure_alerts.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from services.telegram_alert_service import TelegramAlertService
import subscribers.telegram_subscriber as telegram_subscriber
import subscribers.whatsapp_subscriber as whatsapp_subscriber


class FakeEvent:
    """Minimal stand-in for the OrderEvent dataclasses -- only needs the
    attributes the subscriber handlers actually read."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.fixture
def service():
    return TelegramAlertService()


@pytest.fixture(autouse=True)
def _bot_active_and_linked():
    """Common guards send_failure_alert checks before doing any work --
    patched to "everything is set up" by default so each test only has to
    override what it's actually testing."""
    with patch.object(TelegramAlertService, "is_bot_active", return_value=True), \
         patch("services.telegram_alert_service.get_username_by_apikey", return_value="trader1"), \
         patch(
             "services.telegram_alert_service.get_telegram_user_by_username",
             return_value={"telegram_id": 12345, "notifications_enabled": True},
         ), \
         patch("services.telegram_alert_service.alert_executor") as mock_executor:
        yield mock_executor


def test_send_failure_alert_dispatches_with_broker_reason(service, _bot_active_and_linked):
    """The real incident this whole change traces back to: a genuine
    broker rejection reason (Shoonya's ALGO_CHK message) must actually
    reach the alert text, now that place_order_service.py correctly
    populates error_message end to end."""
    service.send_failure_alert(
        "placeorder", "NIFTY18AUG2624350PE", "NFO",
        "Rejected : ALGO_CHK: MKT Order type not allowed for API order",
        {"strategy": "VWAP_NoHA", "action": "BUY", "quantity": 65},
        api_key="key123",
    )
    assert _bot_active_and_linked.submit.called, "FAIL: no alert was dispatched"
    args = _bot_active_and_linked.submit.call_args[0]
    message = args[2]  # submit(send_alert_sync, telegram_id, message)
    assert "🔴" in message and "ORDER FAILED" in message
    assert "ALGO_CHK: MKT Order type not allowed for API order" in message
    assert "NIFTY18AUG2624350PE" in message
    assert "VWAP_NoHA" in message
    print(f"Failure alert correctly includes the real broker rejection reason:\n{message}\nPASS")


def test_send_failure_alert_for_modify_uses_orderid_not_symbol(service, _bot_active_and_linked):
    """OrderModifyFailedEvent/OrderCancelFailedEvent carry orderid, not
    symbol/exchange (confirmed against events/order_events.py) -- must
    format sensibly with those fields empty."""
    service.send_failure_alert(
        "modifyorder", "", "", "Order not found",
        {}, api_key="key123", orderid="26081300039029",
    )
    args = _bot_active_and_linked.submit.call_args[0]
    message = args[2]
    assert "MODIFY FAILED" in message
    assert "26081300039029" in message
    assert "Order not found" in message
    print(f"Modify failure alert correctly uses orderid:\n{message}\nPASS")


def test_send_failure_alert_skips_when_bot_inactive(service):
    with patch.object(TelegramAlertService, "is_bot_active", return_value=False), \
         patch("services.telegram_alert_service.alert_executor") as mock_executor:
        service.send_failure_alert("placeorder", "SYM", "NFO", "some error", {}, api_key="key123")
    assert not mock_executor.submit.called, "FAIL: alert dispatched while bot is inactive"
    print("Correctly skipped: bot inactive PASS")


def test_send_failure_alert_skips_when_notifications_disabled(service):
    with patch.object(TelegramAlertService, "is_bot_active", return_value=True), \
         patch("services.telegram_alert_service.get_username_by_apikey", return_value="trader1"), \
         patch(
             "services.telegram_alert_service.get_telegram_user_by_username",
             return_value={"telegram_id": 12345, "notifications_enabled": False},
         ), \
         patch("services.telegram_alert_service.alert_executor") as mock_executor:
        service.send_failure_alert("placeorder", "SYM", "NFO", "some error", {}, api_key="key123")
    assert not mock_executor.submit.called, "FAIL: alert dispatched while notifications disabled"
    print("Correctly skipped: notifications_enabled=False PASS")


def test_send_failure_alert_skips_when_no_linked_telegram_user(service):
    with patch.object(TelegramAlertService, "is_bot_active", return_value=True), \
         patch("services.telegram_alert_service.get_username_by_apikey", return_value="trader1"), \
         patch("services.telegram_alert_service.get_telegram_user_by_username", return_value=None), \
         patch("services.telegram_alert_service.alert_executor") as mock_executor:
        service.send_failure_alert("placeorder", "SYM", "NFO", "some error", {}, api_key="key123")
    assert not mock_executor.submit.called, "FAIL: alert dispatched with no linked telegram user"
    print("Correctly skipped: no linked telegram user PASS")


@pytest.mark.parametrize("handler_name", [
    "on_order_placed", "on_smart_order_no_action", "on_order_modified",
    "on_order_cancelled", "on_all_orders_cancelled", "on_position_closed",
    "on_basket_completed", "on_split_completed", "on_options_completed",
    "on_multiorder_completed", "on_analyzer_error",
])
def test_telegram_success_handlers_are_all_no_ops(handler_name):
    """This channel is now failure-only end to end -- every non-failure
    handler must not call send_failure_alert/send_order_alert at all."""
    handler = getattr(telegram_subscriber, handler_name)
    with patch.object(telegram_subscriber.telegram_alert_service, "send_order_alert") as mock_send, \
         patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        handler(FakeEvent(api_type="placeorder", request_data={}, response_data={}, api_key="k"))
    assert not mock_send.called and not mock_fail.called, (
        f"FAIL: {handler_name} should be a no-op but dispatched an alert"
    )
    print(f"{handler_name} correctly does nothing PASS")


def test_telegram_on_order_failed_calls_send_failure_alert():
    event = FakeEvent(
        api_type="placeorder", symbol="NIFTY18AUG2624350PE", exchange="NFO",
        error_message="Rejected : ALGO_CHK: MKT Order type not allowed for API order",
        request_data={"strategy": "VWAP_NoHA"}, api_key="key123",
    )
    with patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        telegram_subscriber.on_order_failed(event)
    mock_fail.assert_called_once_with(
        "placeorder", "NIFTY18AUG2624350PE", "NFO",
        "Rejected : ALGO_CHK: MKT Order type not allowed for API order",
        {"strategy": "VWAP_NoHA"}, "key123",
    )
    print("on_order_failed correctly calls send_failure_alert with the right fields PASS")


def test_telegram_on_order_modify_failed_calls_send_failure_alert():
    event = FakeEvent(
        api_type="modifyorder", error_message="Order not found",
        request_data={}, api_key="key123", orderid="ORD1",
    )
    with patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        telegram_subscriber.on_order_modify_failed(event)
    assert mock_fail.called
    kwargs = mock_fail.call_args
    assert kwargs.kwargs.get("orderid") == "ORD1" or "ORD1" in kwargs.args
    print("on_order_modify_failed correctly calls send_failure_alert with orderid PASS")


def test_telegram_on_order_cancel_failed_calls_send_failure_alert():
    event = FakeEvent(
        api_type="cancelorder", error_message="Order already complete",
        request_data={}, api_key="key123", orderid="ORD2",
    )
    with patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        telegram_subscriber.on_order_cancel_failed(event)
    assert mock_fail.called
    print("on_order_cancel_failed correctly calls send_failure_alert PASS")


@pytest.mark.parametrize("handler_name", [
    "on_order_placed", "on_order_failed", "on_smart_order_no_action",
    "on_order_modified", "on_order_modify_failed", "on_order_cancelled",
    "on_order_cancel_failed", "on_all_orders_cancelled", "on_position_closed",
    "on_basket_completed", "on_split_completed", "on_options_completed",
    "on_multiorder_completed", "on_analyzer_error",
])
def test_whatsapp_handlers_are_all_no_ops(handler_name):
    """WhatsApp order alerts disabled entirely (2026-08-13) -- every
    handler, including the failure ones, must do nothing."""
    handler = getattr(whatsapp_subscriber, handler_name)
    with patch("services.whatsapp_alert_service.whatsapp_alert_service.send_order_alert") as mock_send:
        handler(FakeEvent(
            api_type="placeorder", symbol="X", exchange="NFO", error_message="err",
            request_data={}, response_data={}, api_key="k", orderid="O1",
        ))
    assert not mock_send.called, f"FAIL: whatsapp {handler_name} should be a no-op"
    print(f"whatsapp.{handler_name} correctly does nothing PASS")
