"""
Regression tests for the 2026-08-13 Telegram failure-only alert change:

- services/telegram_alert_service.py: send_failure_alert()/format_failure_details()
  (new) -- the Telegram channel now only ever sends when an order
  placement/modify/cancel genuinely fails.
- Success alerts: upstream replaced the per-channel subscribers with one
  alert router (/alerts page, 2026-11 merge). Telegram and WhatsApp now
  default to NO success groups (services/alerts/settings.py), which keeps
  this fork's behaviour for an install that never opens /alerts.
- Failure alerts: subscribers/telegram_failure_subscriber.py sends the three
  *_failed events to send_failure_alert(), unless Telegram is switched off
  on the /alerts page.

Run: uv run pytest test/test_telegram_failure_alerts.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from services.telegram_alert_service import TelegramAlertService
import subscribers.telegram_failure_subscriber as telegram_subscriber
from services.alerts import settings as alert_settings
from services.alerts.categories import CATEGORY_IDS
from services.alerts.registry import get_channel


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


@pytest.fixture
def telegram_on():
    with patch.object(telegram_subscriber, "_telegram_on", return_value=True):
        yield


@pytest.fixture
def nothing_saved(monkeypatch):
    """No /alerts settings stored: every channel uses its defaults."""
    import database.alert_channels_db as alert_channels_db

    monkeypatch.setattr(alert_channels_db, "load_channel", lambda name: None)
    alert_settings.clear_cache()
    yield
    alert_settings.clear_cache()


@pytest.mark.parametrize("name", ["telegram", "whatsapp"])
@pytest.mark.parametrize("category", sorted(CATEGORY_IDS))
def test_no_success_group_reaches_telegram_or_whatsapp_by_default(nothing_saved, name, category):
    """Every success/completion group is off for these two channels unless
    the trader turns it on at /alerts."""
    assert not alert_settings.settings_for(get_channel(name)).receives(category)


def test_failure_alert_respects_the_alerts_page_switch(monkeypatch):
    """Telegram switched off at /alerts sends nothing, failures included."""
    monkeypatch.setattr(telegram_subscriber, "_telegram_on", lambda: False)
    with patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        telegram_subscriber.on_order_failed(FakeEvent(
            api_type="placeorder", symbol="X", exchange="NFO", error_message="err",
            request_data={}, api_key="k",
        ))
    assert not mock_fail.called


def test_telegram_on_order_failed_calls_send_failure_alert(telegram_on):
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


def test_telegram_on_order_modify_failed_calls_send_failure_alert(telegram_on):
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


def test_telegram_on_order_cancel_failed_calls_send_failure_alert(telegram_on):
    event = FakeEvent(
        api_type="cancelorder", error_message="Order already complete",
        request_data={}, api_key="key123", orderid="ORD2",
    )
    with patch.object(telegram_subscriber.telegram_alert_service, "send_failure_alert") as mock_fail:
        telegram_subscriber.on_order_cancel_failed(event)
    assert mock_fail.called
    print("on_order_cancel_failed correctly calls send_failure_alert PASS")
