"""MCP server exposing a Rigol DS1000Z oscilloscope to AI agents."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from . import __version__
from .captures import CaptureStore
from .scope import Scope, parse_number

DEFAULT_MEASUREMENTS = ["VPP", "VMAX", "VMIN", "VAVG", "VRMS", "FREQ", "PER", "PDUT", "RTIM", "FTIM"]
BLOCKED_WRITES = {"*RST", ":SYST:RES", ":SYSTEM:RESET"}


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
            read_only=_env_bool("RIGOL_READ_ONLY"),
            public_url=os.environ.get("RIGOL_PUBLIC_URL") or None,
        )


cfg = Config.from_env()
store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms)
_scope_lock = threading.Lock()  # the scope handles one client at a time

mcp = FastMCP(
    "rigol",
    instructions=(
        "Controls a Rigol DS1000Z oscilloscope. Start with scope_status. Settings are only "
        "changed with scope_write, and only when the user asks: real circuits may be connected. "
        "Measurements that are null are invalid on the scope (e.g. frequency of a DC signal)."
    ),
)


def configure(new_cfg: Config) -> None:
    """Replace the configuration (used by the CLI and tests)."""
    global cfg, store
    cfg = new_cfg
    store = CaptureStore(cfg.data_dir, cfg.keep_screenshots, cfg.keep_waveforms)


def _connect() -> Scope:
    if not cfg.host:
        raise ValueError("RIGOL_HOST is not set: give the scope's IP address or host name")
    return Scope(cfg.host, cfg.port, cfg.timeout)


def _channel(ch: int) -> int:
    if ch not in (1, 2, 3, 4):
        raise ValueError("channel must be 1-4")
    return ch


@mcp.tool()
def scope_status() -> dict:
    """Overview of the scope: model, trigger, timebase, sample rate, and for each channel
    whether it is on, its V/div, offset, coupling, probe ratio, plus Vpp, Vavg, Vrms and
    frequency. Invalid measurements are null. Start here."""
    with _scope_lock, _connect() as s:
        out = {
            "idn": s.query("*IDN?"),
            "trigger_status": s.query(":TRIG:STAT?"),
            "trigger_mode": s.query(":TRIG:MODE?"),
            "trigger_source": s.query(":TRIG:EDG:SOUR?"),
            "trigger_slope": s.query(":TRIG:EDG:SLOP?"),
            "trigger_level_v": parse_number(s.query(":TRIG:EDG:LEV?")),
            "timebase_s_per_div": parse_number(s.query(":TIM:SCAL?")),
            "timebase_offset_s": parse_number(s.query(":TIM:OFFS?")),
            "sample_rate_sps": parse_number(s.query(":ACQ:SRAT?")),
            "channels": {},
        }
        for ch in range(1, 5):
            if s.query(f":CHAN{ch}:DISP?") != "1":
                out["channels"][f"CH{ch}"] = {"on": False}
                continue
            out["channels"][f"CH{ch}"] = {
                "on": True,
                "v_per_div": parse_number(s.query(f":CHAN{ch}:SCAL?")),
                "offset_v": parse_number(s.query(f":CHAN{ch}:OFFS?")),
                "coupling": s.query(f":CHAN{ch}:COUP?"),
                "probe": parse_number(s.query(f":CHAN{ch}:PROB?")),
                "vpp": parse_number(s.query(f":MEAS:ITEM? VPP,CHAN{ch}")),
                "vavg": parse_number(s.query(f":MEAS:ITEM? VAVG,CHAN{ch}")),
                "vrms": parse_number(s.query(f":MEAS:ITEM? VRMS,CHAN{ch}")),
                "freq_hz": parse_number(s.query(f":MEAS:ITEM? FREQ,CHAN{ch}")),
            }
        return out


@mcp.tool()
def scope_measure(channel: int, items: list[str] | None = None) -> dict:
    """Run the scope's built-in measurements on one channel (1-4).
    items: any of VMAX VMIN VPP VTOP VBASE VAMP VAVG VRMS OVER PRES MAR MPAR PER FREQ
    RTIM FTIM PWID NWID PDUT NDUT. Default: a common set. Times are in seconds, duty
    cycles are fractions (0.25 = 25%). Invalid results are null."""
    ch = _channel(channel)
    items = items or DEFAULT_MEASUREMENTS
    with _scope_lock, _connect() as s:
        return {item.upper(): parse_number(s.query(f":MEAS:ITEM? {item},CHAN{ch}")) for item in items}


@mcp.tool()
def scope_waveform(channel: int, max_points: int = 200, include_csv: bool = False) -> dict:
    """Download the on-screen waveform of one channel (1200 samples) and return summary
    stats plus up to max_points evenly spaced [time_s, volts] samples (0 = stats only,
    1200 = every sample). include_csv=True also returns every sample as CSV text (~30 KB).
    Each download is saved with a timestamp; see scope_waveform_history."""
    ch = _channel(channel)
    with _scope_lock, _connect() as s:
        times, volts, raw = s.waveform(ch)
    clipped = sum(1 for b in raw if b in (0, 255))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["time_s", "volts"])
    writer.writerows(zip(times, volts))
    csv_text = buf.getvalue()
    cap = store.save_waveform(ch, csv_text)

    result = {
        "capture": cap.to_dict(cfg.public_url),
        "points": len(volts),
        "dt_s": times[1] - times[0] if len(times) > 1 else None,
        "t_start_s": times[0],
        "t_end_s": times[-1],
        "vmin": min(volts),
        "vmax": max(volts),
        "vpp": max(volts) - min(volts),
        "mean": statistics.fmean(volts),
        "stdev": statistics.pstdev(volts),
        "clipped_samples": clipped,
    }
    if clipped:
        result["note"] = "signal goes off screen (clipped); change V/div or offset for valid values"
    if max_points > 0:
        step = max(1, len(volts) // max_points)
        result["samples"] = [[round(times[i], 9), round(volts[i], 5)] for i in range(0, len(volts), step)]
    if include_csv:
        result["csv_text"] = csv_text
    return result


@mcp.tool()
def scope_screenshot():
    """Capture the scope's display as a PNG. Every screenshot is saved with a UTC
    timestamp (the last RIGOL_KEEP_SCREENSHOTS are kept); see scope_screenshot_history."""
    with _scope_lock, _connect() as s:
        png = s.screenshot()
    cap = store.save_screenshot(png)
    return [Image(data=png, format="png"), json.dumps(cap.to_dict(cfg.public_url))]


@mcp.tool()
def scope_screenshot_history(limit: int = 20) -> dict:
    """List saved screenshots, newest first (id, UTC timestamp, size, and URL when served
    over HTTP). Does not touch the scope. Use scope_screenshot_get to view one."""
    caps = store.list("screenshots", limit)
    out = {"kept_max": cfg.keep_screenshots or "unlimited", "screenshots": [c.to_dict(cfg.public_url) for c in caps]}
    if cfg.public_url:
        out["gallery"] = f"{cfg.public_url.rstrip('/')}/screenshots"
    return out


@mcp.tool()
def scope_screenshot_get(id: str = "latest"):
    """Return a saved screenshot without touching the scope. id is 'latest' (default)
    or an id from scope_screenshot_history."""
    cap = store.get("screenshots", id)
    return [Image(data=cap.path.read_bytes(), format="png"), json.dumps(cap.to_dict(cfg.public_url))]


@mcp.tool()
def scope_waveform_history(limit: int = 20) -> dict:
    """List saved waveform CSVs, newest first, with channel and UTC timestamp."""
    caps = store.list("waveforms", limit)
    return {"kept_max": cfg.keep_waveforms or "unlimited", "waveforms": [c.to_dict(cfg.public_url) for c in caps]}


@mcp.tool()
def scope_query(command: str) -> str:
    """Send a read-only SCPI query (must end with '?') and return the reply,
    e.g. ':CHAN1:BWL?', ':ACQ:MDEP?', ':TRIG:EDG:SLOP?'."""
    command = command.strip()
    if not command.endswith("?"):
        raise ValueError("scope_query only accepts queries ending in '?'; use scope_write to change settings")
    with _scope_lock, _connect() as s:
        return s.query(command)


@mcp.tool()
def scope_write(command: str) -> str:
    """Change a scope setting with a SCPI command, e.g. ':CHAN1:SCAL 1', ':TIM:SCAL 0.001',
    ':RUN', ':STOP', ':SING', ':AUT'. ONLY use when the user asked to change the scope:
    real circuits may be connected. Returns the scope's error queue ('0,"No error"')."""
    if cfg.read_only:
        raise PermissionError("this server is read-only (RIGOL_READ_ONLY is set)")
    command = command.strip()
    if command.endswith("?"):
        raise ValueError("use scope_query for queries")
    if command.upper() in BLOCKED_WRITES:
        raise ValueError("reset is blocked; do it on the scope's front panel")
    with _scope_lock, _connect() as s:
        s.write(command)
        return s.query(":SYST:ERR?")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="rigol-mcp", description="MCP server for Rigol DS1000Z oscilloscopes")
    parser.add_argument("--version", action="version", version=f"rigol-mcp {__version__}")
    parser.add_argument("--scope", metavar="HOST", help="scope IP or host name (env RIGOL_HOST)")
    parser.add_argument("--http", action="store_true", default=os.environ.get("RIGOL_TRANSPORT") == "http",
                        help="serve over HTTP with a screenshot gallery instead of stdio (env RIGOL_TRANSPORT=http)")
    parser.add_argument("--bind", default=os.environ.get("RIGOL_HTTP_HOST", "0.0.0.0"), help="HTTP bind address")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RIGOL_HTTP_PORT", "8790")), help="HTTP port")
    args = parser.parse_args(argv)

    new_cfg = Config.from_env()
    if args.scope:
        new_cfg.host = args.scope
    configure(new_cfg)

    if args.http:
        from .web import serve

        serve(args.bind, args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
