"""Full acquisition-memory export (RAW mode), streamed to disk in chunks."""

from __future__ import annotations

import json
import time
from typing import Literal

from . import app
from .analysis import ByteStats, MinMaxDecimator, estimate_frequency
from .scope import Preamble, ScopeError

mcp = app.mcp
CHUNK = 250_000  # DS1000Z maximum points per :WAV:DATA? read in BYTE format


@app.tool()
def scope_export_memory(
    channel: int,
    format: Literal["bin", "csv"] = "bin",
    stop: bool = False,
    preview_points: int = 200,
) -> dict:
    """Save the channel's FULL acquisition memory (up to 24M points, not just the 1200 on
    screen) to a file, and return stats plus a min/max preview. The scope must be stopped
    (after scope_capture, or STOP pressed); pass stop=true to stop it first (CHANGES THE
    SCOPE: only when the user asks). Transfer runs at about 0.5 M points/s on a DS1000Z, so
    3M points take ~6 s and 24M points ~50 s, blocking other scope calls meanwhile.
    Use this for accurate stats on narrow pulses or bursts: the 1200-point screen data
    keeps peaks when decimating, so it (and the scope's own measurements) overstate RMS
    and mean for spiky signals.
    format 'bin': raw 8-bit ADC bytes plus a .json with the scaling
    (volts = (byte - yorigin - yreference) * yincrement); compact and fast.
    format 'csv': time_s,volts rows (about 25 bytes per point, so 24M points is ~600 MB)."""
    ch = app.check_channel(channel)
    started = time.monotonic()
    with app.scope_lock, app.connect(timeout=max(app.cfg.timeout, 30.0)) as s:
        status = s.query(":TRIG:STAT?")
        if status != "STOP":
            if not stop:
                raise RuntimeError(
                    f"the scope is not stopped (trigger status {status}). Press STOP, use scope_capture, "
                    "or call again with stop=true if the user agrees to stop the scope."
                )
            app.require_writes()
            s.write(":STOP")
            app.audit([":STOP"])
            for _ in range(50):
                if s.query(":TRIG:STAT?") == "STOP":
                    break
                time.sleep(0.1)
        if s.query(f":CHAN{ch}:DISP?") != "1":
            raise ValueError(f"CH{ch} is off; only channels that are on have data")

        s.write(f":WAV:SOUR CHAN{ch}")
        s.write(":WAV:MODE RAW")
        s.write(":WAV:FORM BYTE")
        pre = Preamble.parse(s.query(":WAV:PRE?"))
        total = pre.points
        if total <= 0:
            raise ScopeError("the scope reported 0 points in memory")
        sample_rate = s.query(":ACQ:SRAT?")
        memory_depth = s.query(":ACQ:MDEP?")

        ext = ".bin" if format == "bin" else ".csv"
        cap = app.store.reserve("exports", ext, ch)
        tmp = cap.path.with_name(cap.name + ".tmp")
        stats = ByteStats(pre)
        preview = MinMaxDecimator(total, preview_points if preview_points > 0 else 1)
        # Frequency from a decimated copy: 1 byte in N keeps memory small for 24M points.
        stride = max(1, total // 1_000_000)
        decimated = bytearray()
        table = [f"{pre.volts(b):.6g}" for b in range(256)]
        try:
            with tmp.open("wb") as out:
                if format == "csv":
                    out.write(b"time_s,volts\n")
                start = 1
                while start <= total:
                    stop_pt = min(start + CHUNK - 1, total)
                    s.write(f":WAV:STAR {start}")
                    s.write(f":WAV:STOP {stop_pt}")
                    chunk = s.block(":WAV:DATA?")
                    if not chunk:
                        raise ScopeError(f"empty chunk at point {start}")
                    stats.add(chunk)
                    preview.add(chunk)
                    decimated += chunk[(-(start - 1)) % stride::stride]
                    if format == "bin":
                        out.write(chunk)
                    else:
                        base = start - 1
                        out.write("".join(
                            f"{pre.time(base + i):.9g},{table[b]}\n" for i, b in enumerate(chunk)
                        ).encode())
                    start += len(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            s.write(":WAV:MODE NORM")  # leave the readout mode as other tools expect
            s.write(":WAV:STAR 1")

    elapsed = time.monotonic() - started
    summary = stats.summary()
    dec_pre = Preamble(total, pre.xinc * stride, pre.xorig, pre.xref, pre.yinc, pre.yorig, pre.yref)
    meta = {
        "channel": ch,
        "format": format,
        "points": summary["points"],
        "memory_depth": memory_depth,
        "sample_rate_sps": float(sample_rate),
        "dt_s": pre.xinc,
        "t_start_s": pre.time(0),
        "duration_s": pre.xinc * summary["points"],
        "scaling": {"xincrement": pre.xinc, "xorigin": pre.xorig, "xreference": pre.xref,
                    "yincrement": pre.yinc, "yorigin": pre.yorig, "yreference": pre.yref,
                    "volts": "(byte - yorigin - yreference) * yincrement",
                    "time_s": "xorigin + (index - xreference) * xincrement"},
    }
    cap.sidecar.write_text(json.dumps(meta, indent=2))
    app.store.commit(cap)

    stats_out = {k: v for k, v in summary.items() if k != "points"}
    stats_out["freq_hz"] = estimate_frequency(bytes(decimated), dec_pre)
    result = {
        "capture": cap.to_dict(app.cfg.public_url),
        "metadata_file": str(cap.sidecar),
        **meta,
        "stats": stats_out,
        "elapsed_s": round(elapsed, 2),
        "throughput_mb_s": round(summary["points"] / 1e6 / elapsed, 2) if elapsed else None,
    }
    if summary.get("clipped_samples"):
        result["note"] = "part of the signal is off screen (clipped); vmin/vmax/rms are not reliable"
    if preview_points > 0:
        result["preview"] = {"columns": ["t_start_s", "vmin", "vmax"], "rows": preview.preview(pre)}
    return result
