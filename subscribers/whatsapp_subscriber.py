"""
WhatsApp subscriber — order alerts disabled (2026-08-13).

This WhatsApp integration is a self-hosted WhatsApp Web protocol
connection, linked as one of the user's own devices (see
services/whatsapp_alert_service.py's module docstring) -- not the
official Business API. Messages sent this way arrive on the user's phone
already marked read (a self-linked-device echo, not a genuine push from
another party), making it useless as a "notice this immediately" channel.
Telegram (subscribers/telegram_subscriber.py) now covers the one thing
that actually matters here -- a failure-only alert that arrives as a real
unread push -- so every handler below is now a no-op rather than sending
notifications the user can't rely on seeing.
"""


def on_order_placed(event):
    pass


def on_order_failed(event):
    pass


def on_smart_order_no_action(event):
    pass


def on_order_modified(event):
    pass


def on_order_modify_failed(event):
    pass


def on_order_cancelled(event):
    pass


def on_order_cancel_failed(event):
    pass


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
