"""
Regression tests for WebSocketClient._run_coroutine_and_wait
(services/websocket_client.py).

Background: subscribe()/unsubscribe()/unsubscribe_all() each schedule a
coroutine onto the asyncio event loop that runs on the client's own
dedicated real OS thread (see WebSocketClient.connect()), then need the
result back on the calling thread -- which, in production under
gunicorn+eventlet, is always an eventlet-patched green thread.

The previous implementation called
`asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=N)`
directly from the calling side. That Future's internal Condition/Lock is
the same eventlet-patched threading module used everywhere else in the
process, so resolving it from the loop's real OS thread while a greenlet
blocks in result()'s wait() crashes with "greenlet.error: Cannot switch
to a different thread" -- confirmed in production, 2026-07-29 (9
occurrences in one session, each immediately after subscribe()/
unsubscribe(), and the direct cause of two live option legs never
receiving a single WS tick that day).

The fix never touches the Future itself from the calling thread: the
Future's own add_done_callback runs on the loop thread (real OS thread,
safe) when the coroutine finishes, and signals completion via a genuine
`_original_threading.Event` -- no greenlet/thread affinity, safe from
either side.

These tests exercise `_run_coroutine_and_wait` directly against a real
asyncio loop on a background thread (the same shape as production),
without needing eventlet installed -- eventlet's patching is orthogonal
to whether the bridge itself is correct.
"""

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "services" / "websocket_client.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("websocket_client_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wc_module():
    return _load_module()


@pytest.fixture
def client_with_running_loop(wc_module):
    """A WebSocketClient with a real asyncio loop running on its own
    background thread -- mirrors connect()'s _run_event_loop pattern,
    without actually opening a websocket connection."""
    client = wc_module.WebSocketClient(api_key="test-key")
    loop = asyncio.new_event_loop()
    client.loop = loop

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    # give the loop a moment to start spinning
    for _ in range(50):
        if loop.is_running():
            break
        time.sleep(0.01)

    yield client

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


def test_returns_coroutine_result_from_loop_thread(client_with_running_loop):
    async def coro():
        await asyncio.sleep(0.05)
        return {"status": "success"}

    result = client_with_running_loop._run_coroutine_and_wait(coro(), timeout=5)
    assert result == {"status": "success"}


def test_propagates_exception_raised_inside_coroutine(client_with_running_loop):
    async def coro():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        client_with_running_loop._run_coroutine_and_wait(coro(), timeout=5)


def test_times_out_and_raises_when_coroutine_never_completes(client_with_running_loop):
    async def coro():
        await asyncio.sleep(10)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        client_with_running_loop._run_coroutine_and_wait(coro(), timeout=0.2)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


def test_future_result_is_only_ever_read_on_the_loop_thread(client_with_running_loop, monkeypatch):
    """The whole point of the fix: .result() on the concurrent.futures.Future
    returned by run_coroutine_threadsafe must only ever be invoked from the
    loop thread (inside the done-callback), never from the calling
    (potentially eventlet-green) thread -- that's the exact boundary crossing
    that crashed in production. Wrap Future.result to record which thread
    called it, then assert it was only ever the loop thread's ident."""
    import concurrent.futures

    caller_thread_ids = []
    original_result = concurrent.futures.Future.result

    def _tracking_result(self, timeout=None):
        caller_thread_ids.append(threading.get_ident())
        return original_result(self, timeout)

    monkeypatch.setattr(concurrent.futures.Future, "result", _tracking_result)

    async def coro():
        # A tiny delay, not a correctness requirement of the fix itself --
        # it exists purely so the loop thread can't possibly finish and
        # mark the Future done before the calling thread has even
        # registered add_done_callback(). concurrent.futures.Future runs a
        # done callback immediately, in whichever thread calls
        # add_done_callback(), if the future is ALREADY done at that
        # point -- a bare `return 42` coroutine can race that, making this
        # assertion flaky through no fault of the fix (an already-done
        # Future's .result() returns immediately without blocking, so it
        # never hits the crash-prone wait path either way -- this test is
        # just about confirming the common case runs on the loop thread).
        await asyncio.sleep(0.05)
        return 42

    calling_thread_id = threading.get_ident()
    result = client_with_running_loop._run_coroutine_and_wait(coro(), timeout=5)
    assert result == 42
    assert caller_thread_ids, "Future.result() was never called at all"
    assert calling_thread_id not in caller_thread_ids


def test_concurrent_calls_do_not_interfere(client_with_running_loop):
    """Multiple overlapping subscribe/unsubscribe-style calls (as would
    happen from concurrent Flask request greenlets) must each get their
    own correct result, not a mixed-up one from a different call."""
    async def coro(value):
        await asyncio.sleep(0.05)
        return value

    results = {}
    errors = []

    def worker(i):
        try:
            results[i] = client_with_running_loop._run_coroutine_and_wait(coro(i), timeout=5)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert results == {i: i for i in range(10)}
