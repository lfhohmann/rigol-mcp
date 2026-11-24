"""Read-only tools: they never change what the scope shows or how it acquires."""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
import time
from typing import Literal

from mcp.server.fastmcp import Image

from . import app
from .analysis import waveform_summary
from .scope import parse_number

mcp = app.mcp
DEFAULT_MEASUREMENTS = ["VPP", "VMAX", "VMIN", "VAVG", "VRMS", "FREQ", "PER", "PDUT", "RTIM", "FTIM"]
PAIR_ITEMS = {
    "RDEL": "RDELay",  # rising edge of A to rising edge of B, seconds
    "FDEL": "FDELay",  # falling edge of A to falling edge of B, seconds
    "RPH": "RPHase",  # phase between rising edges, degrees
    "FPH": "FPHase",  # phase between falling edges, degrees
}


@mcp.tool()
def scope_status() -> dict:
    """Overview of the scope: model, run state, trigger, timebase, sample rate, memory depth,
    and for each channel that is on: V/div, offset, coupling, probe, plus Vpp, Vmin, Vmax,
    mean, RMS and an estimated frequency computed from the on-screen waveform (fast; use
    scope_measure for the scope's own measurements). Invalid values are null. Start here."""
    with app.scope_lock, app.connect() as s:
        out = {
            "idn": s.query("*IDN?"),
            "trigger_status": s.query(":TRIG:STAT?"),
            "trigger_sweep": s.query(":TRIG:SWE?"),
            "trigger_mode": s.query(":TRIG:MODE?"),
            "trigger_source": s.query(":TRIG:EDG:SOUR?"),
            "trigger_slope": s.query(":TRIG:EDG:SLOP?"),
            "trigger_level_v": parse_number(s.query(":TRIG:EDG:LEV?")),
            "timebase_s_per_div": parse_number(s.query(":TIM:SCAL?")),
            "timebase_offset_s": parse_number(s.query(":TIM:OFFS?")),
            "sample_rate_sps": parse_number(s.query(":ACQ:SRAT?")),
            "memory_depth": s.query(":ACQ:MDEP?"),
            "acquire_type": s.query(":ACQ:TYPE?"),
            "channels": {},
        }
        for ch in range(1, 5):
            if s.query(f":CHAN{ch}:DISP?") != "1":
                out["channels"][f"CH{ch}"] = {"on": False}
                continue
            info = {
                "on": True,
                "v_per_div": parse_number(s.query(f":CHAN{ch}:SCAL?")),
                "offset_v": parse_number(s.query(f":CHAN{ch}:OFFS?")),
                "coupling": s.query(f":CHAN{ch}:COUP?"),
                "probe": parse_number(s.query(f":CHAN{ch}:PROB?")),
            }
            summary = waveform_summary(s.waveform(ch))
            info.update({k: summary[k] for k in ("vpp", "vmin", "vmax", "mean", "rms", "freq_hz")})
            if summary["clipped_samples"]:
                info["note"] = "trace goes off screen: vpp/vmin/vmax/rms are not reliable"
            out["channels"][f"CH{ch}"] = info
        out["note"] = "Channel values are computed from the 1200-point screen waveform; freq_hz is an estimate."
        return out


@mcp.tool()
def scope_measure(channel: int, items: list[str] | None = None) -> dict:
    """Run the scope's built-in measurements on one channel (1-4). Each takes ~40-400 ms.
    items: any of VMAX VMIN VPP VTOP VBASE VAMP VAVG VRMS OVER PRES MAR MPAR PER FREQ
    RTIM FTIM PWID NWID PDUT NDUT TVMAX TVMIN PSLEWRATE NSLEWRATE VUPper VMID VLOWer
    VARIance PVRMS PPULses NPULses PEDGes NEDGes. Default: a common set. Times are in
    seconds, duty cycles are fractions (0.25 = 25%). Invalid results are null."""
    ch = app.check_channel(channel)
    items = items or DEFAULT_MEASUREMENTS
    with app.scope_lock, app.connect() as s:
        return {item.upper(): parse_number(s.query(f":MEAS:ITEM? {item},CHAN{ch}")) for item in items}


@mcp.tool()
def scope_measure_between(
    channel_a: int,
    channel_b: int,
    items: list[Literal["RDEL", "FDEL", "RPH", "FPH"]] | None = None,
) -> dict:
    """Measure timing between two channels with the scope's built-in measurements.
    RDEL / FDEL: delay from A's rising (falling) edge to B's rising (falling) edge, seconds.
    RPH / FPH: phase between the rising (falling) edges, degrees. Default: all four.
    Invalid results (e.g. no matching edge on screen) are null."""
    a, b = app.check_channel(channel_a), app.check_channel(channel_b)
    items = items or list(PAIR_ITEMS)
    out = {}
    with app.scope_lock, app.connect() as s:
        for item in items:
            name = PAIR_ITEMS[item.upper()]
            out[item.upper()] = parse_number(s.query(f":MEAS:ITEM? {name},CHAN{a},CHAN{b}"))
    units = {"RDEL": "s", "FDEL": "s", "RPH": "deg", "FPH": "deg"}
    return {"channel_a": a, "channel_b": b, "values": out, "units": {k: units[k] for k in out}}


@mcp.tool()
def scope_measure_stats(channel: int, item: str = "VPP", samples: int = 10, interval_s: float = 0.0) -> dict:
    """Repeat one built-in measurement several times and return statistics (mean, min,
    max, stdev). Useful for jitter, drift and noisy signals. samples: 2-100. Each reading
    takes ~40-400 ms on the scope, so 10 samples take a few seconds. Read-only."""
    ch = app.check_channel(channel)
    if not 2 <= samples <= 100:
        raise ValueError("samples must be 2-100")
    values, invalid = [], 0
    started = time.monotonic()
    with app.scope_lock, app.connect() as s:
        for k in range(samples):
            v = parse_number(s.query(f":MEAS:ITEM? {item},CHAN{ch}"))
            if v is None:
                invalid += 1
            else:
                values.append(v)
            if interval_s > 0 and k < samples - 1:
                time.sleep(interval_s)
    out = {"item": item.upper(), "channel": ch, "samples": samples, "valid": len(values), "invalid": invalid,
           "elapsed_s": round(time.monotonic() - started, 2)}
    if values:
        out.update({
            "mean": statistics.fmean(values),
            "min": min(values),
            "max": max(values),
            "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "values": values,
        })
    return out


@mcp.tool()
def scope_waveform(channel: int, max_points: int = 200, include_csv: bool = False) -> dict:
    """Download the on-screen waveform of one channel (1200 samples) and return summary
    stats plus up to max_points evenly spaced [time_s, volts] samples (0 = stats only,
    1200 = every sample). include_csv=True also returns every sample as CSV text (~30 KB).
    Each download is saved with a timestamp; see scope_waveform_history. For the full
    acquisition memory (up to 24M points) use scope_export_memory."""
    ch = app.check_channel(channel)
    with app.scope_lock, app.connect() as s:
        wf = s.waveform(ch)
    times, volts = wf.times, wf.volts

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["time_s", "volts"])
    writer.writerows(zip(times, volts))
    csv_text = buf.getvalue()
    cap = app.store.save_waveform(ch, csv_text)

    summary = waveform_summary(wf)
    result = {
        "capture": cap.to_dict(app.cfg.public_url),
        "dt_s": wf.pre.xinc,
        "t_start_s": times[0] if times else None,
        "t_end_s": times[-1] if times else None,
        **summary,
    }
    if summary.get("clipped_samples"):
        result["note"] = "signal goes off screen (clipped); change V/div or offset for valid values"
    if max_points > 0 and volts:
        step = max(1, math.ceil(len(volts) / max_points))
        result["samples"] = [[round(times[i], 9), round(volts[i], 5)] for i in range(0, len(volts), step)]
    if include_csv:
        result["csv_text"] = csv_text
    return result


@mcp.tool()
def scope_screenshot():
    """Capture the scope's display as a PNG. Every screenshot is saved with a UTC
    timestamp (the last RIGOL_KEEP_SCREENSHOTS are kept); see scope_screenshot_history."""
    with app.scope_lock, app.connect() as s:
        png = s.screenshot()
    cap = app.store.save_screenshot(png)
    return [Image(data=png, format="png"), json.dumps(cap.to_dict(app.cfg.public_url))]


@mcp.tool()
def scope_screenshot_history(limit: int = 20) -> dict:
    """List saved screenshots, newest first (id, UTC timestamp, size, and URL when served
    over HTTP). Does not touch the scope. Use scope_screenshot_get to view one."""
    caps = app.store.list("screenshots", limit)
    out = {"kept_max": app.cfg.keep_screenshots or "unlimited",
           "screenshots": [c.to_dict(app.cfg.public_url) for c in caps]}
    if app.cfg.public_url:
        out["gallery"] = f"{app.cfg.public_url.rstrip('/')}/screenshots"
    return out


@mcp.tool()
def scope_screenshot_get(id: str = "latest"):
    """Return a saved screenshot without touching the scope. id is 'latest' (default)
    or an id from scope_screenshot_history."""
    cap = app.store.get("screenshots", id)
    return [Image(data=cap.path.read_bytes(), format="png"), json.dumps(cap.to_dict(app.cfg.public_url))]


@mcp.tool()
def scope_waveform_history(limit: int = 20, kind: Literal["waveforms", "exports"] = "waveforms") -> dict:
    """List saved screen waveforms (kind='waveforms') or full-memory exports
    (kind='exports'), newest first, with channel, UTC timestamp, size and URL."""
    caps = app.store.list(kind, limit)
    keep = app.store.keep[kind]
    return {"kept_max": keep or "unlimited", kind: [c.to_dict(app.cfg.public_url) for c in caps]}


@mcp.tool()
def scope_query(command: str) -> str:
    """Send a read-only SCPI query (must end with '?') and return the reply,
    e.g. ':CHAN1:BWL?', ':ACQ:MDEP?', ':TRIG:EDG:SLOP?'."""
    command = command.strip()
    if not command.endswith("?"):
        raise ValueError("scope_query only accepts queries ending in '?'; use scope_write to change settings")
    with app.scope_lock, app.connect() as s:
        return s.query(command)
