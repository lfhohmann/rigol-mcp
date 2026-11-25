"""Runs around every tool call: interlock, change detection and notices.

kind of a tool call:
  "local": doesn't touch the scope (history, audit, sessions)
  "read":  reads the scope; never refused; gets change detection and notices
  "write": changes the scope; refused while another session is active unless
           ignore_other_sessions=true
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import app, sessions
from .sessions import Session, registry

CHANGE_DETECTION = os.environ.get("RIGOL_CHANGE_DETECTION", "on").strip().lower() not in ("0", "off", "false", "no")

LABELS = {
    "SCAL": "V/div", "OFFS": "offset", "COUP": "coupling", "PROB": "probe", "BWL": "bandwidth limit",
    "INV": "invert", "DISP": "on/off",
}
GLOBAL_LABELS = {
    ":TIM:MODE": "timebase mode", ":TIM:MAIN:SCAL": "time/div", ":TIM:MAIN:OFFS": "trigger position",
    ":ACQ:TYPE": "acquisition type", ":ACQ:AVER": "averages", ":ACQ:MDEP": "memory depth",
    ":TRIG:MODE": "trigger type", ":TRIG:EDG:SOUR": "trigger source", ":TRIG:EDG:SLOP": "trigger slope",
    ":TRIG:COUP": "trigger coupling", ":TRIG:HOLD": "trigger holdoff", ":TRIG:EDG:LEV": "trigger level",
    ":TRIG:SWE": "trigger sweep", "running": "run/stop",
}


def _label(key: str) -> str:
    if key.startswith(":CHAN"):
        ch, _, item = key[5:].partition(":")
        return f"CH{ch} {LABELS.get(item, item)}"
    return GLOBAL_LABELS.get(key, key)


def _short_args(kw: dict) -> str:
    parts = [f"{k}={v!r}" for k, v in kw.items() if v is not None and k != "ignore_other_sessions"]
    text = ", ".join(parts)
    return text if len(text) < 120 else text[:117] + "..."


@dataclass
class Pre:
    session: Session
    notices: list[str] = field(default_factory=list)
    refusal: dict | None = None
    ignored: list[Session] = field(default_factory=list)


def current_session() -> Session:
    return registry.get(app.session_var.get(), app.client_var.get())


def _read_settings() -> dict:
    from .tools_setup import _read_settings as read

    with app.scope_lock, app.connect() as s:
        return read(s)


def _changes_since(me: Session) -> list[str]:
    """Settings that changed since this session last looked, with who changed them."""
    from .tools_setup import _diff

    if me.snapshot is None:
        return []
    current = _read_settings()
    diff = _diff(me.snapshot, current)
    me.snapshot, me.snapshot_time = current, time.time()
    if not diff:
        return []
    entries = [e for e in app.read_audit(200) if e.get("session") != me.id and e.get("epoch", 0) >= me.last_activity]
    lines = []
    for key, change in diff.items():
        who = None
        for e in entries:
            cmds = e.get("commands", [])
            if key == "running" and any(c in (":RUN", ":STOP", ":SING") for c in cmds):
                who = e
            elif any(c.upper().startswith(key) for c in cmds):
                who = e
            if who:
                break
        if who:
            by = f"by {who.get('client')} [{str(who.get('session'))[:8]}] via {who.get('tool')}"
        elif entries:
            by = f"probably by {entries[0].get('client')} [{str(entries[0].get('session'))[:8]}], or on the front panel"
        else:
            by = "not through this server (front panel or another program)"
        lines.append(f"{_label(key)} changed {change['saved']} -> {change['current']} since your last call, {by}.")
    return lines


def before(tool: str, kind: str, kw: dict) -> Pre:
    me = current_session()
    pre = Pre(me, notices=registry.take_notices(me))
    if kind == "local":
        return pre
    if CHANGE_DETECTION:
        try:
            pre.notices += _changes_since(me)
        except Exception as exc:  # change detection must never break the actual call
            pre.notices.append(f"(change detection skipped: {exc})")
    pre.notices += registry.news_about_others(me)
    if kind != "write" or not sessions.INTERLOCK:
        return pre
    others = registry.others_active(me)
    if not others:
        return pre
    now = time.time()
    if kw.get("ignore_other_sessions"):
        pre.ignored = others
        return pre
    busy = [o.describe(now) for o in others]
    first = others[0]
    purpose = f" for '{first.purpose}'" if first.purpose else ""
    pre.refusal = {
        "refused": True,
        "reason": "Another session is using the scope. Nothing was sent to the scope.",
        "active_sessions": busy,
        "tell_your_user": (
            f"Another agent ({first.name}) is using the oscilloscope{purpose}; its last call was "
            f"{busy[0]['last_tool']} {busy[0]['last_activity']}. Changing settings now could disturb its work. "
            "Ask whether to go ahead anyway."
        ),
        "to_proceed": f"Only if your user agrees: call {tool} again with ignore_other_sessions=true.",
    }
    registry.notify(others, f"{me.name} tried {tool}({_short_args(kw)}) and was refused because you are using "
                            "the scope; it was told to ask its user before retrying.")
    return pre


def after(pre: Pre, tool: str, kind: str, kw: dict) -> None:
    me, now = pre.session, time.time()
    me.last_tool = tool
    if kind == "local" or pre.refusal:
        return
    me.last_activity = now
    if kind == "write":
        from .share import shared

        shared.clear()  # a shared reading must never predate a setting change
        me.last_write, me.last_write_desc = now, f"{tool}({_short_args(kw)})"
        if pre.ignored:
            registry.notify(pre.ignored, f"{me.name} ran {tool}({_short_args(kw)}) while you were using the scope "
                                         "(its user chose to go ahead). Check your settings.")
    if CHANGE_DETECTION:
        try:
            me.snapshot, me.snapshot_time = _read_settings(), now
        except Exception:
            me.snapshot = None


def attach(result, notices: list[str]):
    """Add notices to a tool result without changing its shape."""
    if not notices:
        return result
    if isinstance(result, dict):
        return {**result, "notices": notices}
    if isinstance(result, list):
        return [*result, json.dumps({"notices": notices})]
    return result
