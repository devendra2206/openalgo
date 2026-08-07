"""
Shared httpx client module with connection pooling support for all broker
APIs. HTTP/1.1 only -- HTTP/2 is disabled unconditionally, see
_create_http_client's comment for the eventlet-concurrency corruption this
avoids.
"""

from typing import Optional

import httpx

from utils.logging import get_logger

# Set up logging
logger = get_logger(__name__)

# Global httpx client for connection pooling
_httpx_client = None


def get_httpx_client() -> httpx.Client:
    """
    Returns the shared, process-wide HTTP/1.1 client (HTTP/2 is disabled --
    see _create_http_client's comment for why).

    Returns:
        httpx.Client: The shared configured HTTP client
    """
    global _httpx_client

    if _httpx_client is None:
        _httpx_client = _create_http_client()
        logger.debug("Created shared HTTP/1.1 client")
    return _httpx_client


def request(method: str, url: str, **kwargs) -> httpx.Response:
    """
    Make an HTTP request using the shared HTTP/1.1 client.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: URL to request
        **kwargs: Additional arguments to pass to the request

    Returns:
        httpx.Response: The HTTP response

    Raises:
        httpx.HTTPError: If the request fails
    """
    import time

    from flask import g

    client = get_httpx_client()

    # Track actual broker API call time for latency monitoring
    broker_api_start = time.time()
    response = client.request(method, url, **kwargs)
    broker_api_end = time.time()

    # Store broker API time in Flask's g object for latency tracking
    if hasattr(g, "latency_tracker"):
        broker_api_time_ms = (broker_api_end - broker_api_start) * 1000
        g.broker_api_time = broker_api_time_ms
        logger.debug(f"Broker API call took {broker_api_time_ms:.2f}ms")

    # Log the actual HTTP version used (info level for visibility)
    if response.http_version:
        logger.info(f"Request used {response.http_version} - URL: {url[:50]}...")

    return response


# Shortcut methods for common HTTP methods
def get(url: str, **kwargs) -> httpx.Response:
    """
    Send a GET request.

    Args:
        url (str): The URL to send the GET request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    """
    Send a POST request.

    Args:
        url (str): The URL to send the POST request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("POST", url, **kwargs)


def put(url: str, **kwargs) -> httpx.Response:
    """
    Send a PUT request.

    Args:
        url (str): The URL to send the PUT request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("PUT", url, **kwargs)


def delete(url: str, **kwargs) -> httpx.Response:
    """
    Send a DELETE request.

    Args:
        url (str): The URL to send the DELETE request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("DELETE", url, **kwargs)


def _create_http_client() -> httpx.Client:
    """
    Create a new HTTP client with latency tracking. HTTP/2 is disabled
    unconditionally (see the comment where the client is constructed) --
    only HTTP/1.1 is used.

    Returns:
        httpx.Client: A configured HTTP client with timing hooks
    """
    import time

    from flask import g

    # Event hooks for tracking broker API timing
    def log_request(request):
        """Hook called before request is sent"""
        request.extensions["start_time"] = time.time()
        logger.debug(f"Starting request to {request.url}")

    def log_response(response):
        """Hook called after response is received"""
        try:
            start_time = response.request.extensions.get("start_time")
            if start_time:
                duration_ms = (time.time() - start_time) * 1000

                # Store broker API time in Flask's g object for latency tracking
                try:
                    from flask import has_request_context

                    if has_request_context() and hasattr(g, "latency_tracker"):
                        g.broker_api_time = duration_ms
                        logger.debug(f"Broker API call took {duration_ms:.2f}ms")
                except (RuntimeError, AttributeError):
                    # Not in Flask request context or g not available
                    pass

                logger.debug(f"Request completed in {duration_ms:.2f}ms")
        except Exception as e:
            logger.exception(f"Error in response hook: {e}")

    try:
        # HTTP/2 is disabled unconditionally, not just in standalone mode.
        # HTTP/2 multiplexes multiple logical requests over ONE shared TCP
        # connection via a single mutable h2 state machine -- this process
        # runs one shared httpx.Client instance accessed concurrently by
        # many eventlet green threads (one per request/strategy). If
        # eventlet's cooperative scheduler switches between two green
        # threads at the wrong moment while both touch the same h2
        # connection object, that state machine corrupts. Confirmed in
        # production (2026-08-07): "Invalid input
        # ConnectionInputs.RECV_WINDOW_UPDATE in state
        # ConnectionState.CLOSED" recurring across multiple, PURELY
        # SYNCHRONOUS call sites (get_quotes, TPSeries chunk fetch -- no
        # asyncio involved at all), i.e. this is an eventlet-concurrency
        # risk inherent to HTTP/2 multiplexing itself, not a symptom of any
        # one broker call's own code. The standalone-mode comment already
        # anticipated this exact class of "protocol negotiation issue" and
        # worked around it there; this makes that the default everywhere,
        # since integrated mode hit the same risk. HTTP/1.1 doesn't share
        # it -- each request gets its own pooled connection instead of a
        # shared mutable multiplexed one.
        client = httpx.Client(
            http2=False,
            http1=True,  # Always enable HTTP/1.1 for compatibility
            timeout=120.0,  # Increased timeout for large historical data requests
            limits=httpx.Limits(
                max_keepalive_connections=40,  # Increased from 20 for multi-strategy environments
                max_connections=100,  # Increased from 50 for 10+ concurrent strategies
                keepalive_expiry=30.0,  # Reduced from 120s to recycle stale connections faster
            ),
            # Add verify parameter to handle SSL/TLS issues in standalone mode
            verify=True,  # Can be set to False for debugging SSL issues (not recommended for production)
            # Add event hooks for latency tracking
            event_hooks={"request": [log_request], "response": [log_response]},
        )

        logger.debug("HTTP/2 disabled unconditionally -- see this function's own comment for why")

        return client

    except Exception as e:
        logger.exception(f"Failed to create HTTP client: {e}")
        raise


def cleanup_httpx_client() -> None:
    """
    Closes the global httpx client and releases its resources.

    Should be called when the application is shutting down to prevent
    resource leaks.

    Returns:
        None
    """
    global _httpx_client

    if _httpx_client is not None:
        _httpx_client.close()
        _httpx_client = None
        logger.info("Closed HTTP client")
