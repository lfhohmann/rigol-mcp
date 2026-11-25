"""Save and restore the scope's settings, and read the audit log of changes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import app
from .scope import Scope, parse_number
from .tools_control import _apply

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Restore order matters: probe before V/div (it changes the allowed range), V/div before
# offset (the scope rescales the offset when V/div changes), trigger source before level,
# sweep last.
CHANNEL_KEYS = ["PROB", "DISP", "COUP", "BWL", "INV", "SCAL", "OFFS"]
GLOBAL_KEYS = [
    ":TIM:MODE", ":TIM:MAIN:SCAL", ":TIM:MAIN:OFFS",
    ":ACQ:TYPE", ":ACQ:AVER", ":ACQ:MDEP",
    ":TRIG:MODE", ":TRIG:EDG:SOUR", ":TRIG:EDG:SLOP", ":TRIG:COUP", ":TRIG:HOLD", ":TRIG:EDG:LEV",
    ":TRIG:SWE",
]


def setting_keys() -> list[str]:
    keys = [f":CHAN{ch}:{k}" for ch in range(1, 5) for k in CHANNEL_KEYS]
    return keys + GLOBAL_KEYS


def _setups_dir() -> Path:
    path = Path(app.cfg.data_dir) / "setups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_settings(s: Scope) -> dict:
    settings = {k: s.query(k + "?") for k in setting_keys()}
    status = s.query(":TRIG:STAT?")
    settings["running"] = status != "STOP"
    return settings


def _same(a: str, b: str) -> bool:
    na, nb = parse_number(a), parse_number(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= 1e-9 * max(1.0, abs(na), abs(nb))
    return a.strip().upper() == b.strip().upper()


def _diff(saved: dict, current: dict) -> dict:
    out = {k: {"saved": saved[k], "current": current.get(k)}
           for k in setting_keys() if k in saved and not _same(str(saved[k]), str(current.get(k, "")))}
    if "running" in saved and saved["running"] != current.get("running"):
        out["running"] = {"saved": saved["running"], "current": current.get("running")}
    return out


def _save(settings: dict, name: str | None, idn: str) -> dict:
    now = datetime.now(timezone.utc)
    if name is None:
        name = "setup_" + now.strftime("%Y%m%dT%H%M%SZ")
    if not NAME_RE.match(name):
        raise ValueError("name: letters, digits, '_', '-', '.' only (max 64 chars)")
    data = {"name": name, "saved_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "idn": idn, "settings": settings}
    path = _setups_dir() / f"{name}.json"
    path.write_text(json.dumps(data, indent=1))
    return {"name": name, "file": str(path), "saved_utc": data["saved_utc"], "settings_saved": len(settings)}


def _load(name: str) -> dict:
    files = sorted(_setups_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if name == "latest":
        files = [f for f in files if not f.stem.startswith("auto-before-restore")]
        if not files:
            raise FileNotFoundError("no saved setups yet; use scope_setup_save")
        return json.loads(files[0].read_text())
    if not NAME_RE.match(name):
        raise ValueError("invalid setup name")
    path = _setups_dir() / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no setup named {name!r}; see scope_setup_list")
    return json.loads(path.read_text())


@app.tool()
def scope_setup_save(name: str | None = None) -> dict:
    """Save the scope's current settings (all 4 channels, timebase, acquisition, edge
    trigger, run/stop) to a named JSON file on the server. Read-only: nothing on the scope
    changes. Default name: setup_<UTC time>. Use before experiments, so
    scope_setup_restore can put everything back."""
    with app.scope_lock, app.connect() as s:
        idn = s.query("*IDN?")
        settings = _read_settings(s)
    return _save(settings, name, idn)


@app.tool()
def scope_setup_list() -> dict:
    """List saved setups, newest first."""
    files = sorted(_setups_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            out.append({"name": data["name"], "saved_utc": data["saved_utc"]})
        except (ValueError, KeyError):
            continue
    return {"setups": out}


@app.tool()
def scope_setup_restore(name: str = "latest", dry_run: bool = False) -> dict:
    """Put the scope back to a saved setup ('latest' or a name from scope_setup_list).
    Only settings that differ are sent, one at a time and each confirmed, in an order the
    scope accepts. The current state is saved first as 'auto-before-restore', so a restore
    can be undone. dry_run=true only lists the differences (read-only). CHANGES THE SCOPE
    unless dry_run: only when the user asks."""
    saved = _load(name)
    with app.scope_lock, app.connect() as s:
        current = _read_settings(s)
        diff = _diff(saved["settings"], current)
        result = {"setup": saved["name"], "saved_utc": saved["saved_utc"], "differences": diff}
        if dry_run or not diff:
            result["changed"] = False
            return result
        app.require_writes()
        backup = _save(current, "auto-before-restore", s.query("*IDN?"))
        commands = [f"{k} {saved['settings'][k]}" for k in setting_keys() if k in diff]
        if "running" in diff:
            commands.append(":RUN" if saved["settings"]["running"] else ":STOP")
        error = _apply(s, commands)
        after = _read_settings(s)
    remaining = _diff(saved["settings"], after)
    result.update({"changed": True, "commands_sent": len(commands), "error": error,
                   "still_different": remaining, "undo": f"scope_setup_restore('{backup['name']}')"})
    return result


@app.tool()
def scope_audit_log(limit: int = 30) -> dict:
    """Show the most recent setting changes made through this server, newest first: UTC
    time, client (IP and program for HTTP clients, 'stdio' otherwise), tool, the SCPI
    commands sent and the scope's error reply. Read-only."""
    entries = app.read_audit(limit)
    return {"file": str(Path(app.cfg.data_dir) / "audit.jsonl"), "entries": entries}
