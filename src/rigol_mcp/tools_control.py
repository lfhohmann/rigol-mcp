"""Tools that change the scope. Every one checks RIGOL_READ_ONLY first, and agents are
told to use them only when the user asks."""

from __future__ import annotations

import json
import time
from typing import Literal

from mcp.server.fastmcp import Image

from . import app
from .analysis import waveform_summary
from .scope import Scope, parse_number

mcp = app.mcp
BLOCKED_WRITES = {"*RST", ":SYST:RES", ":SYSTEM:RESET"}

RUN_COMMANDS = {
    "run": ":RUN",
    "stop": ":STOP",
    "single": ":SING",
    "autoscale": ":AUT",
    "force_trigger": ":TFOR",
    "clear": ":CLE",
}


COMMAND_GAP_S = 0.05  # the DS1000Z's SCPI service can hang when flooded with back-to-back writes


def _apply(s: Scope, commands: list[str]) -> str:
    """Send setting commands one at a time (paced, each confirmed with *OPC?), then return
    the scope's error queue."""
    for cmd in commands:
        s.write(cmd)
        s.query("*OPC?")  # wait until the scope has processed it
        time.sleep(COMMAND_GAP_S)
    error = s.query(":SYST:ERR?")
    app.audit(commands, error)
    return error


def _onoff(value: bool) -> str:
    return "ON" if value else "OFF"


@app.tool(kind="write")
def scope_run_control(action: Literal["run", "stop", "single", "autoscale", "force_trigger", "clear"], ignore_other_sessions: bool = False) -> dict:
    """Run/stop acquisition. CHANGES THE SCOPE: only when the user asks.
    run: continuous acquisition. stop: freeze the display. single: arm one trigger, then
    stop (scope_capture does this and waits for the result). autoscale: let the scope pick
    V/div, timebase and trigger (changes many settings). force_trigger: trigger now.
    clear: clear the display.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    with app.scope_lock, app.connect() as s:
        error = _apply(s, [RUN_COMMANDS[action]])
        if action == "autoscale":
            time.sleep(1.0)  # autoscale takes a moment to settle
        return {"action": action, "trigger_status": s.query(":TRIG:STAT?"), "error": error}


@app.tool(kind="write")
def scope_set_channel(
    channel: int,
    display: bool | None = None,
    scale_v_per_div: float | None = None,
    offset_v: float | None = None,
    coupling: Literal["DC", "AC", "GND"] | None = None,
    probe: float | None = None,
    bandwidth_limit: bool | None = None,
    invert: bool | None = None,
    ignore_other_sessions: bool = False,
) -> dict:
    """Change one channel's vertical settings; only the arguments you pass are changed.
    CHANGES THE SCOPE: only when the user asks. probe is the attenuation ratio (1, 10,
    100, ...). bandwidth_limit=True turns on the 20 MHz limit. Returns the values the
    scope actually applied (it rounds to steps it supports) and its error queue.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    ch = app.check_channel(channel)
    cmds = []
    if display is not None:
        cmds.append(f":CHAN{ch}:DISP {_onoff(display)}")
    if probe is not None:  # before scale: the probe ratio changes the allowed V/div range
        cmds.append(f":CHAN{ch}:PROB {probe:g}")
    if coupling is not None:
        cmds.append(f":CHAN{ch}:COUP {coupling}")
    if scale_v_per_div is not None:
        cmds.append(f":CHAN{ch}:SCAL {scale_v_per_div:g}")
    if offset_v is not None:
        cmds.append(f":CHAN{ch}:OFFS {offset_v:g}")
    if bandwidth_limit is not None:
        cmds.append(f":CHAN{ch}:BWL {'20M' if bandwidth_limit else 'OFF'}")
    if invert is not None:
        cmds.append(f":CHAN{ch}:INV {_onoff(invert)}")
    if not cmds:
        raise ValueError("nothing to change: pass at least one setting")
    with app.scope_lock, app.connect() as s:
        error = _apply(s, cmds)
        return {
            "channel": ch,
            "applied": {
                "display": s.query(f":CHAN{ch}:DISP?") == "1",
                "scale_v_per_div": parse_number(s.query(f":CHAN{ch}:SCAL?")),
                "offset_v": parse_number(s.query(f":CHAN{ch}:OFFS?")),
                "coupling": s.query(f":CHAN{ch}:COUP?"),
                "probe": parse_number(s.query(f":CHAN{ch}:PROB?")),
                "bandwidth_limit": s.query(f":CHAN{ch}:BWL?"),
                "invert": s.query(f":CHAN{ch}:INV?") == "1",
            },
            "error": error,
        }


@app.tool(kind="write")
def scope_set_timebase(
    scale_s_per_div: float | None = None,
    offset_s: float | None = None,
    mode: Literal["MAIN", "XY", "ROLL"] | None = None,
    ignore_other_sessions: bool = False,
) -> dict:
    """Change the horizontal settings; only the arguments you pass are changed.
    CHANGES THE SCOPE: only when the user asks. scale_s_per_div e.g. 0.001 = 1 ms/div.
    offset_s moves the trigger point (positive = later). Returns applied values.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    cmds = []
    if mode is not None:
        cmds.append(f":TIM:MODE {mode}")
    if scale_s_per_div is not None:
        cmds.append(f":TIM:MAIN:SCAL {scale_s_per_div:g}")
    if offset_s is not None:
        cmds.append(f":TIM:MAIN:OFFS {offset_s:g}")
    if not cmds:
        raise ValueError("nothing to change: pass at least one setting")
    with app.scope_lock, app.connect() as s:
        error = _apply(s, cmds)
        return {
            "applied": {
                "scale_s_per_div": parse_number(s.query(":TIM:MAIN:SCAL?")),
                "offset_s": parse_number(s.query(":TIM:MAIN:OFFS?")),
                "mode": s.query(":TIM:MODE?"),
                "sample_rate_sps": parse_number(s.query(":ACQ:SRAT?")),
            },
            "error": error,
        }


TriggerSource = Literal["CHAN1", "CHAN2", "CHAN3", "CHAN4", "AC"]


@app.tool(kind="write")
def scope_set_trigger(
    source: TriggerSource | None = None,
    level_v: float | None = None,
    slope: Literal["POS", "NEG", "RFAL"] | None = None,
    sweep: Literal["AUTO", "NORM", "SING"] | None = None,
    coupling: Literal["DC", "AC", "LFR", "HFR"] | None = None,
    holdoff_s: float | None = None,
    ignore_other_sessions: bool = False,
) -> dict:
    """Set up an edge trigger; only the arguments you pass are changed (the trigger type is
    switched to EDGE). CHANGES THE SCOPE: only when the user asks. slope: POS rising,
    NEG falling, RFAL either. sweep: AUTO (free-runs without trigger), NORM (waits for
    triggers), SING (one shot; prefer scope_capture). Returns applied values.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    cmds = [":TRIG:MODE EDGE"]
    if source is not None:
        cmds.append(f":TRIG:EDG:SOUR {source}")
    if slope is not None:
        cmds.append(f":TRIG:EDG:SLOP {slope}")
    if level_v is not None:
        cmds.append(f":TRIG:EDG:LEV {level_v:g}")
    if coupling is not None:
        cmds.append(f":TRIG:COUP {coupling}")
    if holdoff_s is not None:
        cmds.append(f":TRIG:HOLD {holdoff_s:g}")
    if sweep is not None:
        cmds.append(f":TRIG:SWE {sweep}")
    with app.scope_lock, app.connect() as s:
        error = _apply(s, cmds)
        return {
            "applied": {
                "mode": s.query(":TRIG:MODE?"),
                "source": s.query(":TRIG:EDG:SOUR?"),
                "level_v": parse_number(s.query(":TRIG:EDG:LEV?")),
                "slope": s.query(":TRIG:EDG:SLOP?"),
                "sweep": s.query(":TRIG:SWE?"),
                "coupling": s.query(":TRIG:COUP?"),
                "holdoff_s": parse_number(s.query(":TRIG:HOLD?")),
            },
            "trigger_status": s.query(":TRIG:STAT?"),
            "error": error,
        }


@app.tool(kind="write")
def scope_set_acquire(
    type: Literal["NORM", "AVER", "PEAK", "HRES"] | None = None,
    averages: int | None = None,
    memory_depth: str | None = None,
    ignore_other_sessions: bool = False,
) -> dict:
    """Change acquisition settings. CHANGES THE SCOPE: only when the user asks.
    type: NORM, AVER (averaging), PEAK (peak detect), HRES (high resolution).
    averages: 2, 4, 8, ... 1024 (used with AVER). memory_depth: 'AUTO' or a point count the
    scope supports for the number of channels on (e.g. 12000, 120000, 1200000, 12000000,
    24000000 with one channel). Deeper memory means longer scope_export_memory transfers.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    cmds = []
    if type is not None:
        cmds.append(f":ACQ:TYPE {type}")
    if averages is not None:
        cmds.append(f":ACQ:AVER {averages}")
    if memory_depth is not None:
        cmds.append(f":ACQ:MDEP {memory_depth}")
    if not cmds:
        raise ValueError("nothing to change: pass at least one setting")
    with app.scope_lock, app.connect() as s:
        error = _apply(s, cmds)
        return {
            "applied": {
                "type": s.query(":ACQ:TYPE?"),
                "averages": s.query(":ACQ:AVER?"),
                "memory_depth": s.query(":ACQ:MDEP?"),
                "sample_rate_sps": parse_number(s.query(":ACQ:SRAT?")),
            },
            "error": error,
        }


@app.tool(kind="write")
def scope_capture(
    timeout_s: float = 10.0,
    channels: list[int] | None = None,
    source: TriggerSource | None = None,
    level_v: float | None = None,
    slope: Literal["POS", "NEG", "RFAL"] | None = None,
    include_screenshot: bool = True,
    restore_sweep: bool = True,
    ignore_other_sessions: bool = False,
):
    """Single-shot capture: optionally set up an edge trigger, arm SINGLE, wait for the
    trigger (up to timeout_s, max 120), then return a screenshot and a summary of each
    channel (Vpp, mean, RMS, frequency estimate). CHANGES THE SCOPE: only when the user
    asks. After the trigger the scope stays stopped on the capture, and (restore_sweep)
    the trigger sweep goes back to what it was (e.g. NORM or AUTO), so pressing RUN
    resumes as before. Trigger settings passed here (source, level, slope) stay changed.
    Tell the user to cause the event after calling, or set a timeout long enough for them
    to do it. On timeout the scope stays armed in SINGLE.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    timeout_s = max(0.5, min(float(timeout_s), 120.0))
    setup = [":TRIG:MODE EDGE"] if any(v is not None for v in (source, level_v, slope)) else []
    if source is not None:
        setup.append(f":TRIG:EDG:SOUR {source}")
    if slope is not None:
        setup.append(f":TRIG:EDG:SLOP {slope}")
    if level_v is not None:
        setup.append(f":TRIG:EDG:LEV {level_v:g}")

    def status_now() -> str:
        with app.scope_lock, app.connect() as s:  # brief hold: others get a turn while we wait
            return s.query(":TRIG:STAT?")

    with app.scope_lock, app.connect() as s:
        previous_sweep = s.query(":TRIG:SWE?")
        error = _apply(s, setup + [":SING"])
    armed_at = time.monotonic()
    # Wait for the scope to leave STOP (armed), then for it to stop again (triggered).
    status = status_now()
    while status == "STOP" and time.monotonic() - armed_at < 1.0:
        time.sleep(0.05)
        status = status_now()
    while status != "STOP" and time.monotonic() - armed_at < timeout_s:
        time.sleep(0.25)
        status = status_now()
    waited = round(time.monotonic() - armed_at, 2)
    if status != "STOP":
        return {"triggered": False, "waited_s": waited, "trigger_status": status, "error": error,
                "previous_sweep": previous_sweep,
                "note": "no trigger within the timeout; the scope is still armed (SINGLE). "
                        f"To give up: scope_set_trigger(sweep='{previous_sweep[:4]}') and scope_run_control('run')."}

    with app.scope_lock, app.connect(timeout=max(app.cfg.timeout, 15.0)) as s:
        chans = channels or [ch for ch in range(1, 5) if s.query(f":CHAN{ch}:DISP?") == "1"]
        summary = {}
        for ch in chans:
            app.check_channel(ch)
            info = waveform_summary(s.waveform(ch))
            summary[f"CH{ch}"] = {k: info[k] for k in ("vpp", "vmin", "vmax", "mean", "rms", "freq_hz", "clipped_samples")}
        png = s.screenshot() if include_screenshot else None
        sweep_now = s.query(":TRIG:SWE?")
        if restore_sweep and previous_sweep and sweep_now != previous_sweep:
            error = _apply(s, [f":TRIG:SWE {previous_sweep}"])
            sweep_now = s.query(":TRIG:SWE?")
        run_state = s.query(":TRIG:STAT?")

    result = {"triggered": True, "waited_s": waited, "channels": summary, "error": error,
              "trigger_sweep": sweep_now, "trigger_status": run_state,
              "next": "the scope is stopped on this capture; scope_export_memory can save the full record, "
                      "scope_run_control('run') resumes"}
    if png is None:
        return result
    cap = app.store.save_screenshot(png)
    result["screenshot"] = cap.to_dict(app.cfg.public_url)
    return [Image(data=png, format="png"), json.dumps(result)]


@app.tool(kind="write")
def scope_write(command: str, ignore_other_sessions: bool = False) -> dict:
    """Send any SCPI setting command (no reply) for things the typed tools don't cover,
    e.g. ':MATH:DISP ON', ':MEAS:STAT:DISP ON'. CHANGES THE SCOPE: only when the user asks.
    Prefer scope_set_* and scope_run_control. Returns the scope's error queue.
    ignore_other_sessions: Only set true after YOUR USER agreed to go ahead although another
    session is using the scope; otherwise leave false and the call is refused while one is."""
    app.require_writes()
    command = command.strip()
    if command.endswith("?"):
        raise ValueError("use scope_query for queries")
    if command.upper() in BLOCKED_WRITES:
        raise ValueError("reset is blocked; do it on the scope's front panel")
    with app.scope_lock, app.connect() as s:
        return {"command": command, "error": _apply(s, [command])}
