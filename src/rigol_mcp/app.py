"""Shared state: configuration, the MCP instance, capture store and scope access."""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import typing
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

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
scope_lock = threading.Lock()  # the scope handles one client at a time

mcp = FastMCP(
    "rigol",
    instructions=(
        "Controls a Rigol DS1000Z oscilloscope. Start with scope_status. Tools that change the "
        "scope (scope_run_control, scope_set_*, scope_capture, scope_write, scope_setup_restore, and "
        "scope_export_memory with stop=true) must only be used when the user asks: real circuits may "
        "be connected. Before changing several settings, call scope_setup_save so they can be restored. "
        "Null measurements are invalid on the scope (e.g. frequency of a DC signal)."
    ),
)


def configure(new_cfg: Config) -> None:
    """Replace the configuration (used by the CLI and tests)."""
    global cfg, store
    cfg = new_cfg
    store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms, cfg.keep_exports, cfg.keep_logs)


# Who is calling (set per HTTP request by the auth middleware) and which tool is running.
client_var: contextvars.ContextVar[str] = contextvars.ContextVar("rigol_client", default="stdio")
tool_var: contextvars.ContextVar[str] = contextvars.ContextVar("rigol_tool", default="direct")


def tool(**kwargs):
    """Register a synchronous tool. FastMCP would run sync tools on its event loop, so a
    long call (a 50 s export, a 60 s capture) would freeze the server for every client.
    This runs the tool in a worker thread instead; the scope lock still serializes access
    to the scope itself. Returns the original function so it can be called directly."""

    def decorate(fn):
        @functools.wraps(fn)
        async def run_in_thread(*args, **kw):
            client = client_var.get()

            def call():
                tokens = (client_var.set(client), tool_var.set(fn.__name__))
                try:
                    return fn(*args, **kw)
                finally:
                    tool_var.reset(tokens[1])
                    client_var.reset(tokens[0])

            return await anyio.to_thread.run_sync(call)

        # Resolve string annotations here, in the tool's own module, so FastMCP builds the
        # input schema from real types (it would otherwise look them up in this module).
        hints = typing.get_type_hints(fn, include_extras=True)
        sig = inspect.signature(fn)
        run_in_thread.__signature__ = sig.replace(
            parameters=[p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()],
            return_annotation=hints.get("return", sig.return_annotation),
        )
        run_in_thread.__annotations__ = hints
        mcp.tool(**kwargs)(run_in_thread)
        return fn

    return decorate


_audit_lock = threading.Lock()


def audit(commands: list[str], error: str | None = None, **extra) -> None:
    """Append one line per setting change to <data dir>/audit.jsonl."""
    entry = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
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


def connect(timeout: float | None = None) -> Scope:
    if not cfg.host:
        raise ValueError("RIGOL_HOST is not set: give the scope's IP address or host name")
    return Scope(cfg.host, cfg.port, timeout or cfg.timeout)


def check_channel(ch: int) -> int:
    if ch not in (1, 2, 3, 4):
        raise ValueError("channel must be 1-4")
    return ch


def require_writes() -> None:
    if cfg.read_only:
        raise PermissionError("this server is read-only (RIGOL_READ_ONLY is set)")
