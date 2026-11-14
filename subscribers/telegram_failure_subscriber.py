"""Telegram failure alerts (fork, 141d29558, 2026-08-13; ported onto the
upstream /alerts system on 2026-11-14).

Upstream's alert router never sends a failure: order.failed and the like have
no alert group (services/alerts/categories.py). This fork wants exactly the
opposite on Telegram, a channel that sends ONLY failures:

1. A real incident (2026-08-13, Nifty_Sensex_VWAP_NoHA_Intraday): a Shoonya
   ALGO_CHK rejection was reported as a success, and failures were never
   routed to Telegram at all, so the trader had no way to notice one.
2. Telegram cannot give one message its own sound. Making the chat
   failure-only turns whatever sound the trader assigns to it into "the
   failure sound".

Success alerts for Telegram and WhatsApp are off by default for the same
reason (services/alerts/settings.py, _FORK_NO_SUCCESS_ALERTS_BY_DEFAULT).

The /alerts page's Telegram switch still applies: Telegram turned off there
sends nothing, failures included. Delivery itself is non-blocking:
send_failure_alert() queues on the Telegram alert service's own pool.
"""

from services.alerts.registry import get_channel
from services.alerts.settings import settings_for
from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)


def _telegram_on() -> bool:
    channel = get_channel("telegram")
    return channel is not None and settings_for(channel).enabled


def on_order_failed(event):
    if not _telegram_on():
        return
    telegram_alert_service.send_failure_alert(
        event.api_type,
        event.symbol,
        event.exchange,
        event.error_message,
        event.request_data,
        event.api_key,
    )


def on_order_modify_failed(event):
    if not _telegram_on():
        return
    telegram_alert_service.send_failure_alert(
        event.api_type,
        getattr(event, "symbol", ""),
        "",
        event.error_message,
        event.request_data,
        event.api_key,
        orderid=event.orderid,
    )


def on_order_cancel_failed(event):
    if not _telegram_on():
        return
    telegram_alert_service.send_failure_alert(
        event.api_type,
        "",
        "",
        event.error_message,
        event.request_data,
        event.api_key,
        orderid=event.orderid,
    )


def register(bus) -> None:
    """Subscribe the three failure handlers. Called from subscribers.register_all."""
    bus.subscribe("order.failed", on_order_failed, "telegram:order_failed")
    bus.subscribe("order.modify_failed", on_order_modify_failed, "telegram:modify_failed")
    bus.subscribe("order.cancel_failed", on_order_cancel_failed, "telegram:cancel_failed")
