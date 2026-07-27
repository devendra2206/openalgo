"""Shared platform-notification helper for OpenAlgo Python Strategy Host
scripts.

Kept as a separate, importable module (not copy-pasted into each script)
specifically for this one small, single-purpose piece of behavior that every
deployed script needs identically: telling the platform a trade just closed.
The rest of each script's platform-communication code (report_pnl_to_platform,
push_leg_error, check_pending_action, etc.) intentionally stays copy-pasted
per script for now -- each script is still edited/run standalone via the
Python Strategy Host's browser editor, and mass-extracting everything is a
bigger, riskier refactor than this file-by-file project currently wants to
take on in one pass. See docs/CUSTOMIZATIONS.md's "Not yet done" note on the
equivalent Flask-side extraction for the same reasoning.

Placed under strategies/scripts/ (the live-served, gitignored directory) with
a leading underscore purely for readability -- NOT because the Python
Strategy Host would otherwise mistake it for a strategy. It never scans this
directory; it only ever looks up an exact `{strategy_id}.py` filename driven
by strategy_configs.json/the database (see blueprints/python_strategy.py), so
a file with any name here is invisible to the strategy list regardless.

Import note for anything that loads a strategy script by file path rather
than running it as `python strategies/scripts/X.py` directly (mock tests,
historical replay harnesses): running a script directly auto-adds its own
directory to sys.path[0], so `import _strategy_platform_client` resolves
with zero extra setup. Loading via importlib.util.spec_from_file_location
does NOT do this automatically -- the loader must insert
str(Path(__file__).parent) (i.e. strategies/scripts/) into sys.path itself
before exec_module(), or this import will raise ModuleNotFoundError.
"""
import http.client
import json
import os
import socket
import urllib.request
from pathlib import Path
from typing import Optional


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal stdlib-only HTTP-over-Unix-domain-socket client -- same
    rationale as every strategy script's own copy: gunicorn can be bound via
    `--bind unix:/path/to/openalgo.sock` instead of a TCP port."""

    def __init__(self, socket_path: str, timeout: float = 3.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _post_json_local(host: str, path: str, payload: bytes, timeout: float = 3.0) -> None:
    """POST to the local OpenAlgo instance -- Unix domain socket, then plain
    TCP loopback on FLASK_PORT, then `host` (the public domain) as a last
    resort. Raises if every transport fails; caller decides whether to log
    and swallow (see notify_trade_closed)."""
    headers = {"Content-Type": "application/json"}

    socket_path = Path(__file__).resolve().parents[2] / "openalgo.sock"
    if socket_path.exists():
        conn = _UnixHTTPConnection(str(socket_path), timeout=timeout)
        try:
            conn.request("POST", path, body=payload, headers=headers)
            conn.getresponse().read()
            return
        finally:
            conn.close()

    last_exc: Optional[Exception] = None
    for base in (f"http://127.0.0.1:{os.getenv('FLASK_PORT', '5000')}", host.rstrip("/")):
        try:
            req = urllib.request.Request(f"{base}{path}", data=payload, method="POST", headers=headers)
            urllib.request.urlopen(req, timeout=timeout).close()
            return
        except Exception as exc:
            last_exc = exc
    raise last_exc


def notify_trade_closed(env, log_warning=None) -> None:
    """Fire-and-forget notification to the platform that a leg just closed
    (a new row was written to trades_{env.strategy_tag}.csv) -- triggers
    api_push_trade_closed's SSE broadcast so the dashboard's Today's Trades
    panel and the Trades page can re-fetch live instead of waiting on a
    manual refresh. Never raises: any failure is reported via `log_warning`
    (pass the caller's own Log.warning) if given, and swallowed either way --
    must never block or crash the calling strategy's scheduler loop over a
    reporting hiccup, same as every other platform-push call in these
    scripts (report_pnl_to_platform, push_leg_error).

    `env`: the calling script's own Environment instance -- read-only here,
    just needs .api_key, .host, .strategy_tag (identical shape across all 6
    deployed scripts)."""
    payload = json.dumps({"apikey": env.api_key}).encode("utf-8")
    path = f"/python/api/strategy/{env.strategy_tag}/trade_closed"
    try:
        _post_json_local(env.host, path, payload)
    except Exception as exc:
        if log_warning is not None:
            log_warning(f"notify_trade_closed failed: {exc}")


def filter_known_fields(cls, raw: dict) -> dict:
    """Drop any keys in `raw` that aren't fields of `cls` before it gets
    spread into that dataclass's constructor.

    Why this exists: a persisted strategy_state_{tag}.json survives across
    restarts AND across redeploys of a new script version onto an existing
    server. If a dataclass's field set ever changes between versions (a
    field renamed or removed -- this project has done exactly that, e.g.
    LegPosition gained exit_fill_px in one revision), the OLD state file on
    disk still has the old field names. The previous pattern --
    `SomeClass(**{**asdict(SomeClass()), **raw})` -- spreads `raw` with NO
    filtering, so a single stale key raises TypeError ("unexpected keyword
    argument") and crashes state loading entirely, taking the whole
    strategy process down before it even connects to the broker. Dropping
    the stale field instead (silently -- the caller can log it) is far
    safer than refusing to start over a field the current code doesn't even
    use anymore.

    `cls`: the dataclass itself (not an instance) -- e.g. LegPosition,
    OptionLeg, InstrumentLock. Uses `vars(cls())` rather than
    `dataclasses.fields()` so this needs no extra import beyond what every
    script already has."""
    known = set(vars(cls()).keys())
    return {k: v for k, v in raw.items() if k in known}
