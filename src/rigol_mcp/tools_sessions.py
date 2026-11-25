"""Tools to say what you're doing with the scope and to see who else is using it."""

from __future__ import annotations

import time

from . import app, sessions
from .guard import current_session
from .sessions import registry


@app.tool(kind="local")
def scope_claim(purpose: str, minutes: float = 30.0) -> dict:
    """Tell other agents you are using the scope and why, e.g. 'measuring relay switch-off
    delay on CH1/CH2'. While the claim lasts (1-480 min), other sessions' setting changes
    are refused unless their user agrees, and they see your purpose. Call it before a series
    of changes or captures; scope_release when done. Doesn't touch the scope."""
    if not purpose.strip():
        raise ValueError("purpose must say what you are doing")
    minutes = max(1.0, min(float(minutes), 480.0))
    me = current_session()
    now = time.time()
    me.purpose, me.claim_until = purpose.strip()[:200], now + minutes * 60
    others = [o for o in registry.others_active(me) if o.claimed(now)]
    out = {"claimed": True, "session": me.short, "purpose": me.purpose, "until_minutes": minutes,
           "interlock": "on" if sessions.INTERLOCK else "off (RIGOL_INTERLOCK=off: writes are never refused)"}
    if others:
        out["warning"] = "Another session also holds a claim; tell your user before changing settings."
        out["other_claims"] = [o.describe(now) for o in others]
        registry.notify(others, f"{me.name} also claimed the scope for '{me.purpose}'.")
    return out


@app.tool(kind="local")
def scope_release() -> dict:
    """End your claim on the scope (see scope_claim) so other agents aren't held back."""
    me = current_session()
    had = me.claim_until > time.time()
    me.claim_until = 0.0
    me.last_activity = min(me.last_activity, time.time() - sessions.WINDOW_S)  # no longer "recently active"
    return {"released": had, "session": me.short}


@app.tool(kind="local")
def scope_sessions() -> dict:
    """List sessions that used this server in the last hour: name, purpose, claim, last call
    and last setting change. 'you' marks your own session. Doesn't touch the scope."""
    me, now = current_session(), time.time()
    rows = []
    for s in registry.all():
        d = s.describe(now)
        d["active"] = bool(s.active(now))
        if s.id == me.id:
            d["you"] = True
        rows.append(d)
    return {"interlock": sessions.INTERLOCK, "active_window_s": sessions.WINDOW_S, "sessions": rows}
