"""Test for strategy_reporting/server.py's relay error handling (added
2026-09-03): a transport-level failure reaching the main app (e.g. a
fresh connection refused under load) previously raised unhandled inside
_relay(), producing a bare 500. Now it's caught and turned into a
structured 503 so callers (nginx, the browser) can tell "main app
unreachable" apart from a genuine application error. This does not change
the happy path at all -- only what happens when _relay_client.request()
itself raises.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_KEY", "test-app-key-for-strategy-reporting-tests")


@pytest.fixture(scope="module")
def server_module():
    import strategy_reporting.server as server

    return server


@pytest.fixture()
def client(server_module):
    server_module.app.config["TESTING"] = True
    return server_module.app.test_client()


def test_relay_returns_503_when_main_app_unreachable(server_module, client, monkeypatch):
    import httpx

    def _raise_connect_error(*_args, **_kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(server_module._relay_client, "request", _raise_connect_error)

    # Any path not handled by a local route in server.py falls through to
    # the catch-all _relay() route.
    response = client.get("/python/some-path-not-handled-locally")

    assert response.status_code == 503
    body = response.get_json()
    assert body["status"] == "error"
    assert "unreachable" in body["message"].lower()


def test_relay_passes_through_successful_response(server_module, client, monkeypatch):
    """Sanity check the happy path is untouched by the new try/except --
    a successful upstream response is still relayed verbatim."""

    class _FakeUpstreamResponse:
        status_code = 200
        content = b'{"status": "success"}'
        headers = {"content-type": "application/json"}

    monkeypatch.setattr(
        server_module._relay_client, "request", lambda *a, **k: _FakeUpstreamResponse()
    )

    response = client.get("/python/some-path-not-handled-locally")

    assert response.status_code == 200
    assert response.data == b'{"status": "success"}'
