# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Shoonya Broking Parameters https://shoonya.com/api-documentation

import time

from database.token_db import get_br_symbol, get_symbol_info
from utils.logging import get_logger
from utils.mpp_slab import calculate_protected_price, get_instrument_type_from_symbol

logger = get_logger(__name__)

# One retry, brief pause, before giving up on a bad MPP quote -- confirmed
# live (2026-08-13) that a bad quote (an option's GetQuotes returning the
# underlying index's data, bid=0/ask=0) can be a momentary glitch: a fresh
# quote for the SAME symbol 7 seconds later was completely normal
# (real bid/ask). Cheap enough to always try before falling back.
_MPP_QUOTE_RETRY_DELAY_SEC = 0.5


def transform_data(data, token, auth_token=None):
    """
    Transforms the new API request structure to the current expected structure.
    For market orders, fetches quotes and adjusts price using MPP (Market Price Protection):
    - EQ/FUT: Price < 100: 2%, 100-500: 1%, > 500: 0.5%
    - OPT (CE/PE): Price < 10: 5%, 10-100: 3%, 100-500: 2%, > 500: 1%

    Args:
        data: Order data dictionary
        token: Instrument token
        auth_token: Authentication token for fetching quotes (passed from order_api)
    """
    userid = data["apikey"]
    symbol = get_br_symbol(data["symbol"], data["exchange"])
    # Handle special characters in symbol
    if symbol and "&" in symbol:
        symbol = symbol.replace("&", "%26")

    # Default values
    price = str(data.get("price", "0"))
    order_type = map_order_type(data["pricetype"])
    action = data["action"].upper()

    # Apply Market Price Protection for MARKET and SL-M orders
    # Shoonya blocks both MKT and SL-MKT order types for API orders
    if data["pricetype"] in ("MARKET", "SL-M"):
        original_type = data["pricetype"]
        logger.info(
            f"MPP: {original_type} order detected for Symbol={data['symbol']}, Exchange={data['exchange']}, Action={action}"
        )
        mpp_converted = False
        if auth_token:
            # Lazy import to avoid circular dependency
            from broker.shoonya.api.data import BrokerData

            broker_data = BrokerData(auth_token)

            # Up to 2 attempts (1 retry) before giving up on a bad quote --
            # see _MPP_QUOTE_RETRY_DELAY_SEC's docstring: confirmed live
            # that a bad quote here is often a momentary glitch, not a
            # persistent one.
            for attempt in range(1, 3):
                try:
                    quote_data = broker_data.get_quotes(data["symbol"], data["exchange"])
                    logger.info(
                        f"MPP Quote Response (attempt {attempt}/2): Symbol={data['symbol']}, "
                        f"Exchange={data['exchange']}, LTP={quote_data.get('ltp')}, "
                        f"Bid={quote_data.get('bid')}, Ask={quote_data.get('ask')}, "
                        f"TickSize={quote_data.get('tick_size')}"
                    )

                    instrument_type = get_instrument_type_from_symbol(data["symbol"])
                    tick_size = quote_data.get("tick_size")

                    ltp = float(quote_data.get("ltp", 0))
                    bid = float(quote_data.get("bid", 0))
                    ask = float(quote_data.get("ask", 0))

                    # A real, live two-sided quote always has bid>0 AND ask>0
                    # during market hours -- requiring both, not just ltp>0, is a
                    # defensive guard against a bad/placeholder quote silently
                    # feeding a nonsensical protected price into a real order.
                    # Confirmed in production (2026-08-10): Shoonya's GetQuotes
                    # for a specific BFO SENSEX option token returned
                    # ltp=78525.19 (the underlying INDEX level, not the option's
                    # own ~450 premium) with bid=0.0/ask=0.0 -- the old ltp>0-only
                    # check happily computed a "protected" price from that
                    # (77739.95), which the broker rejected outright, but left
                    # the leg unentered for 46 minutes until manually retried.
                    if ltp > 0 and bid > 0 and ask > 0:
                        protected_price = calculate_protected_price(
                            price=ltp,
                            action=action,
                            symbol=data["symbol"],
                            instrument_type=instrument_type,
                            tick_size=tick_size,
                        )
                        price = str(protected_price)
                        order_type = "LMT" if original_type == "MARKET" else "SL-LMT"
                        mpp_converted = True
                        logger.info(
                            f"MPP Conversion Complete: Symbol={data['symbol']}, "
                            f"OrderType={original_type}->{order_type}, FinalPrice={protected_price}"
                        )
                        break

                    logger.warning(
                        f"MPP Warning (attempt {attempt}/2): quote looks invalid for "
                        f"Symbol={data['symbol']}, Exchange={data['exchange']} "
                        f"(ltp={ltp}, bid={bid}, ask={ask}) -- missing a genuine "
                        f"two-sided market (or ltp<=0)."
                    )
                except Exception as e:
                    logger.error(
                        f"MPP Error (attempt {attempt}/2): Failed to apply MPP for "
                        f"Symbol={data['symbol']}, Exchange={data['exchange']}, Error={str(e)}."
                    )
                if attempt < 2:
                    time.sleep(_MPP_QUOTE_RETRY_DELAY_SEC)
        else:
            logger.warning(
                f"MPP Warning: No auth token available for Symbol={data['symbol']}. "
                f"Cannot fetch quotes for MPP adjustment"
            )

        # A MARKET order has no trigger/reference price to fall back on --
        # unlike SL-M below, there is no safe way to build a protective
        # LIMIT without a genuine quote. Shoonya rejects raw MKT orders
        # from the API outright (ALGO_CHK), so silently sending one here
        # is not a "safe fallback", it is a GUARANTEED rejection --
        # confirmed live (2026-08-13): "Rejected : ALGO_CHK: MKT Order
        # type not allowed for API order", which place_order_service.py
        # then mishandled as a false "success" (see that file's own fix,
        # same incident) rather than the clean, retryable rejection this
        # raise now produces instead. Raising here is caught by
        # place_order_api's caller (place_order_service.place_order_with_auth's
        # existing try/except around broker_module.place_order_api) and
        # surfaces as a normal {"status": "error", ...} response -- the
        # strategy's own place_order_max_attempts retry loop (a "clean
        # rejection, nothing was placed" case) picks it up from there,
        # the same recovery path a manual UI Retry already proved works.
        if original_type == "MARKET" and not mpp_converted:
            raise RuntimeError(
                f"MPP could not obtain a valid two-sided quote for Symbol={data['symbol']}, "
                f"Exchange={data['exchange']} after 2 attempts -- refusing to send an "
                f"unprotected MARKET order (Shoonya rejects MKT for API orders outright)."
            )

        # A missing auth token, a zero LTP or a quote exception must NOT leave an
        # SL-M falling through as SL-MKT — that is the exact price type Shoonya
        # rejects for API orders, so the order would be dead on arrival. Unlike
        # MARKET (which has no reference price without a quote), an SL-M always
        # carries a trigger price, so derive the protective limit from the
        # trigger and still send SL-LMT. Same fallback as
        # broker/tradesmart/mapping/transform_data.py::_apply_mpp.
        if original_type == "SL-M" and not mpp_converted:
            order_type = "SL-LMT"
            trigger = float(data.get("trigger_price") or 0)
            if trigger > 0:
                # The caller's trigger price is already tick-valid, so it is the
                # safe limit. Add the MPP buffer on top only when the master
                # contract yields a tick size: with tick_size=None,
                # calculate_protected_price rounds to 2 decimals, which is
                # off-tick on a 0.05-tick instrument — that would trade a
                # rejection on price type for a rejection on price. No quote
                # means no tick size from the API, so read it from SymToken.
                price = str(trigger)
                try:
                    info = get_symbol_info(data["symbol"], data["exchange"])
                    tick_size = getattr(info, "tick_size", None)
                    if tick_size:
                        price = str(
                            calculate_protected_price(
                                price=trigger,
                                action=action,
                                symbol=data["symbol"],
                                instrument_type=get_instrument_type_from_symbol(data["symbol"]),
                                tick_size=tick_size,
                            )
                        )
                except Exception as e:
                    logger.error(
                        f"MPP Fallback Error: could not protect off the trigger for "
                        f"Symbol={data['symbol']}, Error={str(e)}. Using the trigger price as-is."
                    )
                    price = str(trigger)
            logger.warning(
                f"MPP Fallback: quote-based conversion did not run for Symbol={data['symbol']}; "
                f"sending SL-M->SL-LMT priced off the trigger ({trigger}) at {price}"
            )

    # Basic mapping
    transformed = {
        "uid": userid,
        "actid": userid,
        "exch": data["exchange"],
        "tsym": symbol,
        "qty": str(data["quantity"]),
        "prc": price,
        "trgprc": str(data.get("trigger_price", "0")),
        "dscqty": str(data.get("disclosed_quantity", "0")),
        "prd": map_product_type(data["product"]),
        "trantype": "B" if action == "BUY" else "S",
        "prctyp": order_type,
        "mkt_protection": "0",
        "ret": "DAY",
        "ordersource": "API",
    }

    # Log order data without sensitive fields
    safe_log = {k: v for k, v in transformed.items() if k not in ("uid", "actid")}
    logger.info(f"Transformed order data: {safe_log}")
    return transformed


def transform_modify_order_data(data, token):
    # Handle special characters in symbol
    symbol = data["symbol"]
    if symbol and "&" in symbol:
        symbol = symbol.replace("&", "%26")

    result = {
        "uid": data["apikey"],
        "exch": data["exchange"],
        "norenordno": data["orderid"],
        "prctyp": map_order_type(data["pricetype"]),
        "prc": str(data["price"]),
        "qty": str(data["quantity"]),
        "tsym": symbol,
        "ret": "DAY",
        "dscqty": str(data.get("disclosed_quantity") or 0),
    }

    # Only include trigger price for SL/SL-M orders
    # Sending trgprc=0 for LIMIT orders causes "Trigger price invalid - 0.00" error
    if data["pricetype"] in ["SL", "SL-M"]:
        result["trgprc"] = str(data.get("trigger_price") or 0)

    return result


def map_order_type(pricetype):
    """
    Maps the new pricetype to the existing order type.
    """
    order_type_mapping = {"MARKET": "MKT", "LIMIT": "LMT", "SL": "SL-LMT", "SL-M": "SL-MKT"}
    return order_type_mapping.get(pricetype, "MARKET")  # Default to MARKET if not found


def map_product_type(product):
    """
    Maps the new product type to the existing product type.
    """
    product_type_mapping = {
        "CNC": "C",
        "NRML": "M",
        "MIS": "I",
    }
    return product_type_mapping.get(product, "I")  # Default to DELIVERY if not found


def reverse_map_product_type(product):
    """
    Maps the new product type to the existing product type.
    """
    reverse_product_type_mapping = {
        "C": "CNC",
        "M": "NRML",
        "I": "MIS",
    }
    return reverse_product_type_mapping.get(product)
