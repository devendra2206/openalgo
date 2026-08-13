"""
Telegram subscriber — failure-only alerts (2026-08-13).

This channel now ONLY sends when an order placement/modify/cancel
genuinely fails -- every success/completion event below is a deliberate
no-op. Two reasons:

1. A real production incident (2026-08-13, Nifty_Sensex_VWAP_NoHA_Intraday)
   showed a broker rejection (Shoonya ALGO_CHK) silently reported as a
   "success" alert here, because place_order_service.py trusted the HTTP
   status alone -- fixed separately, but it highlighted that failures were
   never even routed to this channel to begin with (on_order_failed was a
   no-op). The user wants to actually notice a real failure, not be
   drowned in routine success confirmations.
2. Telegram's Bot API has no way to assign a custom notification sound
   per message -- sound is a setting the user sets per-chat in their own
   app. Making this channel failure-only means whatever sound the user
   assigns to this one chat effectively becomes "the failure sound",
   without needing new multi-bot infrastructure.

Uses telegram_alert_service.send_failure_alert() for the three failure
events; everything else is suppressed. See
subscribers/whatsapp_subscriber.py for the matching WhatsApp-side
decision (disabled entirely there, for a different reason).
"""

from services.telegram_alert_service import telegram_alert_service
from utils.logging import get_logger

logger = get_logger(__name__)


def on_order_placed(event):
    pass


def on_order_failed(event):
    telegram_alert_service.send_failure_alert(
        event.api_type, event.symbol, event.exchange, event.error_message,
        event.request_data, event.api_key,
    )


def on_smart_order_no_action(event):
    pass


def on_order_modified(event):
    pass


def on_order_modify_failed(event):
    telegram_alert_service.send_failure_alert(
        event.api_type, "", "", event.error_message,
        event.request_data, event.api_key, orderid=event.orderid,
    )


def on_order_cancelled(event):
    pass


def on_order_cancel_failed(event):
    telegram_alert_service.send_failure_alert(
        event.api_type, "", "", event.error_message,
        event.request_data, event.api_key, orderid=event.orderid,
    )


def on_all_orders_cancelled(event):
    pass


def on_position_closed(event):
    pass


def on_basket_completed(event):
    pass


def on_split_completed(event):
    pass


def on_options_completed(event):
    pass


def on_multiorder_completed(event):
    pass


def on_analyzer_error(event):
    pass
