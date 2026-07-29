"""
Regression tests for the widened post-open WebSocket staleness threshold
(`_current_ws_stale_threshold()`) across the 5 deployed scripts that
subscribe to NIFTY.NSE_INDEX/SENSEX.BSE_INDEX (Batman, Combined,
EMA34_RSI, Pivot_Supertrend, VWAP_NoHA -- not MCX, which doesn't
subscribe to either and doesn't need this).

Background: NIFTY.NSE_INDEX/SENSEX.BSE_INDEX only get a new tick when
their underlying constituents actually trade and the index recalculates.
In the first ~45 minutes after 09:15, this is naturally burstier than the
rest of the day, with legitimate gaps wider than the normal 20s
`ws_stale_seconds` threshold. At the flat threshold, PriceStream's
watchdog was misdiagnosing that normal opening-minutes irregularity as a
dead connection and forcing repeated resubscribes/reconnects -- confirmed
in production (2026-07-29) to reach all the way to a real
Unsubscribe/resubscribe cycle at the broker adapter (`fyers_websocket_
adapter`), 23-53 events per script confined to the 09:15-09:52 window,
self-resolving once tick cadence settled.

The fix: `_current_ws_stale_threshold()` returns a wider threshold
(`ws_stale_seconds_open`, 60s) during a defined grace window
(09:15 <= now < `ws_post_open_grace_until`, 10:00), and the normal
`ws_stale_seconds` (20s) outside it. Only the WATCHDOG's
reconnect-triggering check uses this widened value -- get_ltp()'s
REST-fallback threshold is unchanged everywhere.

These tests import the Combined script (representative of all 5) as a
module via importlib and call `_current_ws_stale_threshold()` directly
with the module's own `datetime` monkeypatched to a fixed, controlled
time -- no live process, no network.
"""

import importlib.util
import sys
import threading
from datetime import datetime as real_datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "strategies"
    / "deployed"
    / "Nifty_Sensex_Pivot_EMA_Combined_Intraday_1_20260726000000.py"
)


def _ensure_real_openalgo_sdk_loaded():
    """See test_strategy_pnl_executor.py's identical helper for why this
    is needed -- the repo root is itself importable as a package literally
    named `openalgo`, which shadows the pip-installed SDK under pytest's
    rootdir-on-sys.path behavior."""
    existing = sys.modules.get("openalgo")
    if existing is not None and hasattr(existing, "api"):
        return

    site_pkg_init = REPO_ROOT / ".venv" / "Lib" / "site-packages" / "openalgo" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "openalgo", site_pkg_init, submodule_search_locations=[str(site_pkg_init.parent)]
    )
    real_openalgo = importlib.util.module_from_spec(spec)
    sys.modules["openalgo"] = real_openalgo
    spec.loader.exec_module(real_openalgo)


def _load_script_module():
    _ensure_real_openalgo_sdk_loaded()
    spec = importlib.util.spec_from_file_location("combined_strategy_script_ws", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script_module():
    return _load_script_module()


class _FixedDateTime:
    """Stand-in for the `datetime` class the script imports at module
    level (`from datetime import datetime, ...`), so `datetime.now(IST)`
    inside `_current_ws_stale_threshold()` returns a controlled instant
    instead of the real wall clock."""

    _fixed: "real_datetime" = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed


def _freeze_time(script_module, monkeypatch, hour: int, minute: int):
    fixed = real_datetime(2026, 7, 29, hour, minute, 0, tzinfo=script_module.IST)
    _FixedDateTime._fixed = fixed
    monkeypatch.setattr(script_module, "datetime", _FixedDateTime)


def test_widened_threshold_right_at_open(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 9, 15)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds_open


def test_widened_threshold_mid_grace_window(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 9, 45)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds_open


def test_normal_threshold_right_before_open(script_module, monkeypatch):
    """09:14 -- before the grace window even starts -- must use the
    normal (tighter) threshold, not the widened one."""
    _freeze_time(script_module, monkeypatch, 9, 14)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds


def test_normal_threshold_at_grace_window_boundary(script_module, monkeypatch):
    """Exactly 10:00 -- the grace window is a half-open interval
    [09:15, 10:00) -- must have already reverted to the normal threshold."""
    _freeze_time(script_module, monkeypatch, 10, 0)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds


def test_normal_threshold_midday(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 12, 30)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds


def test_normal_threshold_late_afternoon(script_module, monkeypatch):
    _freeze_time(script_module, monkeypatch, 15, 20)
    assert script_module._current_ws_stale_threshold() == script_module.config.ws_stale_seconds


def test_widened_value_is_actually_wider(script_module):
    """Sanity check on the values themselves, independent of timing --
    the whole point of this fix only holds if the widened threshold is
    genuinely larger than the normal one."""
    assert script_module.config.ws_stale_seconds_open > script_module.config.ws_stale_seconds


def test_get_ltp_max_age_is_unaffected_by_widened_threshold(script_module, monkeypatch):
    """The widened threshold must only affect the watchdog's own
    reconnect-triggering check, never get_ltp()'s REST-fallback max_age --
    that stays at the tight ws_stale_seconds everywhere, including during
    the grace window, since a REST fallback is cheap and harmless."""
    _freeze_time(script_module, monkeypatch, 9, 20)  # inside the grace window
    price_stream = script_module.PriceStream.__new__(script_module.PriceStream)
    price_stream._lock = threading.Lock()
    price_stream._cache = {}
    # get_ltp() itself takes max_age as an explicit argument from the
    # caller (always config.ws_stale_seconds at every call site in
    # run_cycle/report_pnl_tick) -- it has no dependency on
    # _current_ws_stale_threshold() at all. This test documents that
    # separation explicitly rather than just asserting by omission.
    assert price_stream.get_ltp("NIFTY", "NSE_INDEX", max_age=script_module.config.ws_stale_seconds) is None
