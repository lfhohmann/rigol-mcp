"""Shared state: configuration, the MCP instance, capture store and scope access."""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import typing
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP

from .captures import CaptureStore
from .scope import Scope


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "rigol-mcp"


@dataclass
class Config:
    host: str = ""
    port: int = 5555
    timeout: float = 10.0
    data_dir: Path = field(default_factory=_default_data_dir)
    keep_screenshots: int = 100
    keep_waveforms: int = 100
    keep_exports: int = 10
    keep_logs: int = 50
    read_only: bool = False
    public_url: str | None = None  # base URL for links to the gallery/files (HTTP mode)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            host=os.environ.get("RIGOL_HOST", ""),
            port=int(os.environ.get("RIGOL_PORT", "5555")),
            timeout=float(os.environ.get("RIGOL_TIMEOUT", "10")),
            data_dir=Path(os.environ["RIGOL_DATA_DIR"]) if os.environ.get("RIGOL_DATA_DIR") else _default_data_dir(),
            keep_screenshots=int(os.environ.get("RIGOL_KEEP_SCREENSHOTS", "100")),
            keep_waveforms=int(os.environ.get("RIGOL_KEEP_WAVEFORMS", "100")),
            keep_exports=int(os.environ.get("RIGOL_KEEP_EXPORTS", "10")),
            keep_logs=int(os.environ.get("RIGOL_KEEP_LOGS", "50")),
            read_only=_env_bool("RIGOL_READ_ONLY"),
            public_url=os.environ.get("RIGOL_PUBLIC_URL") or None,
        )


cfg = Config.from_env()
store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms, cfg.keep_exports, cfg.keep_logs)
class FairLock:
    """A first-come, first-served lock. Long jobs (exports, captures, logs) take it once per
    short exchange; a plain Lock could let them grab it again ahead of waiting readers."""

    def __init__(self):
        self._cond = threading.Condition()
        self._queue: list[int] = []
        self._next = 0
        self._owner = None
        self._depth = 0

    def acquire(self) -> bool:
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:  # re-entrant for the same thread
                self._depth += 1
                return True
            ticket = self._next
            self._next += 1
            self._queue.append(ticket)
            while self._owner is not None or self._queue[0] != ticket:
                self._cond.wait()
            self._queue.pop(0)
            self._owner, self._depth = me, 1
            return True

    def release(self) -> None:
        with self._cond:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._cond.notify_all()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


scope_lock = FairLock()  # the scope handles one conversation at a time

mcp = FastMCP(
    "rigol",
    instructions=(
        "Controls a Rigol DS1000Z oscilloscope. Start with scope_status. Tools that change the "
        "scope (scope_run_control, scope_set_*, scope_capture, scope_write, scope_setup_restore, and "
        "scope_export_memory with stop=true) must only be used when the user asks: real circuits may "
        "be connected. Before changing several settings, call scope_setup_save so they can be restored. "
        "Several agents may share this scope: call scope_claim(purpose) before a series of changes and "
        "scope_release after. If a change is refused because another session is using the scope, tell "
        "your user who and why, and retry with ignore_other_sessions=true only if they agree. Read the "
        "'notices' in results: they say what other sessions or people changed since your last call. "
        "Null measurements are invalid on the scope (e.g. frequency of a DC signal)."
    ),
)


def configure(new_cfg: Config) -> None:
    """Replace the configuration (used by the CLI and tests)."""
    global cfg, store
    disconnect()
    cfg = new_cfg
    store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms, cfg.keep_exports, cfg.keep_logs)


# Who is calling (set per HTTP request by the auth middleware) and which tool is running.
client_var: contextvars.ContextVar[str] = contextvars.ContextVar("rigol_client", default="stdio")
session_var: contextvars.ContextVar[str] = contextvars.ContextVar("rigol_session", default="stdio")
tool_var: contextvars.ContextVar[str] = contextvars.ContextVar("rigol_tool", default="direct")


def identify(ctx: Context | None) -> tuple[str, str]:
    """(session id, readable name) of the caller. HTTP: the Mcp-Session-Id header, named by
    an optional X-Agent header, else the client's own name and IP. stdio/tests: context vars."""
    try:
        request = ctx.request_context.request if ctx is not None else None
    except (AttributeError, LookupError, ValueError):
        request = None
    if request is None or not hasattr(request, "headers"):
        return session_var.get(), client_var.get()
    headers = request.headers
    ip = request.client.host if getattr(request, "client", None) else "?"
    sid = headers.get("mcp-session-id") or f"{ip}-{id(ctx.session):x}"
    name = headers.get("x-agent", "").strip()[:60]
    if not name:
        try:
            name = f"{ctx.session.client_params.clientInfo.name}@{ip}"
        except AttributeError:
            name = f"{headers.get('user-agent', '?').split(' ')[0][:30]}@{ip}"
    return sid, name


def tool(kind="read", share: bool = False, **kwargs):
    """Register a synchronous tool.

    kind: "local" (no scope access), "read", "write", or a function of the call's keyword
    arguments returning one of those (e.g. export is a write only with stop=true). Writes go
    through the interlock; reads and writes get change detection and notices (guard.py).

    FastMCP would run sync tools on its event loop, so a long call (a 50 s export, a 60 s
    capture) would freeze the server for every client. This runs the tool in a worker
    thread instead; the scope lock still serializes access to the scope itself. Returns
    the original function so it can be called directly (without the guard)."""

    def decorate(fn):
        @functools.wraps(fn)
        async def run_in_thread(*args, mcp_ctx: Context | None = None, **kw):
            session, client = identify(mcp_ctx)

            def call():
                from . import guard

                tokens = (client_var.set(client), session_var.set(session), tool_var.set(fn.__name__))
                try:
                    k = kind(kw) if callable(kind) else kind
                    pre = guard.before(fn.__name__, k, kw)
                    if pre.refusal:
                        guard.after(pre, fn.__name__, k, kw)
                        return guard.attach(pre.refusal, pre.notices)
                    try:
                        if share and not kw.get("fresh"):
                            from .share import key_for, mark, shared

                            result, info = shared.run(key_for(fn.__name__, kw), lambda: fn(*args, **kw), client)
                            result = mark(result, info)
                        else:
                            result = fn(*args, **kw)
                    finally:
                        guard.after(pre, fn.__name__, k, kw)
                    return guard.attach(result, pre.notices)
                finally:
                    tool_var.reset(tokens[2])
                    session_var.reset(tokens[1])
                    client_var.reset(tokens[0])

            return await anyio.to_thread.run_sync(call)

        # Resolve string annotations here, in the tool's own module, so FastMCP builds the
        # input schema from real types (it would otherwise look them up in this module).
        hints = typing.get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn)
        params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()]
        # FastMCP fills a Context-typed parameter itself and leaves it out of the input schema.
        params.append(inspect.Parameter("mcp_ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context))
        run_in_thread.__signature__ = sig.replace(parameters=params, return_annotation=hints.get("return", sig.return_annotation))
        run_in_thread.__annotations__ = {**hints, "mcp_ctx": Context}
        mcp.tool(**kwargs)(run_in_thread)
        return fn

    return decorate


_audit_lock = threading.Lock()


def audit(commands: list[str], error: str | None = None, **extra) -> None:
    """Append one line per setting change to <data dir>/audit.jsonl."""
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "epoch": round(time.time(), 3),
        "session": session_var.get(),
        "client": client_var.get(),
        "tool": tool_var.get(),
        "commands": commands,
        "error": error,
        **extra,
    }
    path = Path(cfg.data_dir) / "audit.jsonl"
    with _audit_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")


def read_audit(limit: int = 50) -> list[dict]:
    path = Path(cfg.data_dir) / "audit.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()[-limit:] if limit > 0 else path.read_text().splitlines()
    return [json.loads(line) for line in reversed(lines) if line.strip()]


_conn: Scope | None = None
_conn_key: tuple | None = None
_conn_used = 0.0
IDLE_CHECK_S = 20.0


class _Borrowed:
    """The server's single, persistent connection to the scope, lent out while the caller
    holds scope_lock. Kept open between calls (no connection churn, never two connections
    from this server at once). Any error drops it, so a half-read reply can't leak into the
    next caller; the next call reconnects."""

    def __init__(self, timeout: float | None):
        self.timeout = timeout or cfg.timeout

    def __enter__(self) -> Scope:
        global _conn, _conn_key, _conn_used
        if not cfg.host:
            raise ValueError("RIGOL_HOST is not set: give the scope's IP address or host name")
        key = (cfg.host, cfg.port)
        if _conn is not None and (_conn_key != key or time.monotonic() - _conn_used > IDLE_CHECK_S):
            # Long idle: make sure the scope hasn't dropped the connection before handing it out.
            try:
                if _conn_key != key:
                    raise OSError("scope address changed")
                _conn.sock.settimeout(2.0)
                _conn.query("*IDN?")
            except Exception:
                _conn.close()
                _conn = None
        if _conn is None:
            _conn, _conn_key = Scope(cfg.host, cfg.port, self.timeout), key
        _conn.sock.settimeout(self.timeout)
        _conn_used = time.monotonic()
        return _conn

    def __exit__(self, exc_type, *rest):
        global _conn
        if exc_type is not None and _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None


def connect(timeout: float | None = None) -> _Borrowed:
    """Use as `with app.scope_lock, app.connect() as s:`."""
    return _Borrowed(timeout)


def disconnect() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def check_channel(ch: int) -> int:
    if ch not in (1, 2, 3, 4):
        raise ValueError("channel must be 1-4")
    return ch


def require_writes() -> None:
    if cfg.read_only:
        raise PermissionError("this server is read-only (RIGOL_READ_ONLY is set)")
