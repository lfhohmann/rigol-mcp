import asyncio
import json
import time

import pytest

from rigol_mcp import app
from rigol_mcp.tools_control import scope_set_channel
from rigol_mcp.tools_log import scope_log_start, scope_log_status, scope_log_stop
from rigol_mcp.tools_setup import scope_audit_log, scope_setup_list, scope_setup_restore, scope_setup_save


def call(name, args=None):
    """Call a tool the way an MCP client does (through the async thread wrapper)."""
    return asyncio.run(app.mcp.call_tool(name, args or {}))


# --- setups ----------------------------------------------------------------------------
def test_setup_save_diff_restore_and_undo(fake):
    saved = scope_setup_save("bench")
    assert saved["name"] == "bench" and saved["settings_saved"] > 30
    scope_set_channel(1, scale_v_per_div=2, offset_v=-1)
    fake.handle(":STOP")

    dry = scope_setup_restore("bench", dry_run=True)
    assert dry["changed"] is False
    assert set(dry["differences"]) == {":CHAN1:SCAL", ":CHAN1:OFFS", "running"}

    out = scope_setup_restore("bench")
    assert out["changed"] and out["still_different"] == {} and out["error"] == '0,"No error"'
    assert fake.state[":CHAN1:SCAL"] == "5.000000e-01" and fake.run_state == "RUN"
    sent = [w for w in fake.writes if w.startswith(":CHAN1:") or w == ":RUN"][-3:]
    assert sent == [":CHAN1:SCAL 5.000000e-01", ":CHAN1:OFFS 0.000000e+00", ":RUN"]  # scale before offset

    assert out["undo"] == "scope_setup_restore('auto-before-restore')"
    assert scope_setup_restore("auto-before-restore", dry_run=True)["differences"][":CHAN1:SCAL"]["saved"] == "2"
    assert [s["name"] for s in scope_setup_list()["setups"]][0] == "auto-before-restore"
    assert scope_setup_restore("latest", dry_run=True)["setup"] == "bench"  # 'latest' skips auto backups


def test_setup_restore_nothing_to_do_and_guards(fake):
    scope_setup_save()
    assert scope_setup_restore()["changed"] is False
    with pytest.raises(ValueError):
        scope_setup_save("../evil")
    with pytest.raises(FileNotFoundError):
        scope_setup_restore("missing")
    scope_set_channel(1, scale_v_per_div=2)
    app.cfg.read_only = True
    assert scope_setup_restore(dry_run=True)["differences"]  # dry run still allowed
    with pytest.raises(PermissionError):
        scope_setup_restore()


# --- audit -----------------------------------------------------------------------------
def test_audit_records_tool_and_commands(fake):
    call("scope_set_channel", {"channel": 1, "scale_v_per_div": 1})
    call("scope_run_control", {"action": "stop"})
    entries = scope_audit_log()["entries"]
    assert entries[0]["tool"] == "scope_run_control" and entries[0]["commands"] == [":STOP"]
    assert entries[1]["tool"] == "scope_set_channel" and entries[1]["commands"] == [":CHAN1:SCAL 1"]
    assert entries[0]["client"] == "stdio" and entries[0]["timestamp_utc"].endswith("Z")


# --- threading -------------------------------------------------------------------------
def test_slow_tool_does_not_block_other_calls(fake):
    fake.trigger_after = 1.0

    async def both():
        order = []

        async def capture():
            await app.mcp.call_tool("scope_capture", {"timeout_s": 5, "include_screenshot": False})
            order.append("capture")

        async def history():
            await asyncio.sleep(0.2)
            await app.mcp.call_tool("scope_screenshot_history", {})
            order.append("history")

        await asyncio.gather(capture(), history())
        return order

    assert asyncio.run(both()) == ["history", "capture"]


# --- measurement logging ---------------------------------------------------------------
def test_log_runs_in_background_and_finishes(fake):
    fake.vary = True
    started = scope_log_start(channels=[1], items=["VPP", "FREQ"], interval_s=0.2, duration_s=1.0)
    assert started["state"] == "running"
    with pytest.raises(RuntimeError, match="still running"):
        scope_log_start()
    time.sleep(1.5)
    st = scope_log_status()
    assert st["state"] == "finished" and 4 <= st["rows"] <= 7
    vpp = st["stats"]["CH1_VPP"]
    assert vpp["valid"] == st["rows"] and vpp["min"] < vpp["max"]
    lines = open(st["capture"]["file"]).read().splitlines()
    assert lines[0] == "timestamp_utc,elapsed_s,CH1_VPP,CH1_FREQ" and len(lines) == st["rows"] + 1


def test_log_stop_early_and_validation(fake):
    scope_log_start(interval_s=0.2, duration_s=60)
    time.sleep(0.5)
    stopped = scope_log_stop()
    assert stopped["state"] == "stopped" and stopped["rows"] >= 2
    for bad in ({"interval_s": 0.05}, {"duration_s": 0}, {"channels": [5]}, {"items": ["VPP"] * 11}):
        with pytest.raises(ValueError):
            scope_log_start(**bad)
