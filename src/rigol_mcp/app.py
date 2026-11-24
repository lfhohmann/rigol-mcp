"""Shared state: configuration, the MCP instance, capture store and scope access."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

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
            read_only=_env_bool("RIGOL_READ_ONLY"),
            public_url=os.environ.get("RIGOL_PUBLIC_URL") or None,
        )


cfg = Config.from_env()
store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms, cfg.keep_exports)
scope_lock = threading.Lock()  # the scope handles one client at a time

mcp = FastMCP(
    "rigol",
    instructions=(
        "Controls a Rigol DS1000Z oscilloscope. Start with scope_status. Tools that change the "
        "scope (scope_run_control, scope_set_*, scope_capture, scope_write, and scope_export_memory "
        "with stop=true) must only be used when the user asks: real circuits may be connected. "
        "Null measurements are invalid on the scope (e.g. frequency of a DC signal)."
    ),
)


def configure(new_cfg: Config) -> None:
    """Replace the configuration (used by the CLI and tests)."""
    global cfg, store
    cfg = new_cfg
    store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms, cfg.keep_exports)


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
