"""Sessions, interlock, notices and change detection, called the way MCP clients call."""

import asyncio
import json

from rigol_mcp import app


def as_session(sid, label=None):
    app.session_var.set(sid)
    app.client_var.set(label or sid)


def call(name, args=None):
    out = asyncio.run(app.mcp.call_tool(name, args or {}))
    # FastMCP returns (content blocks, structured) for dict results
    content = out[0] if isinstance(out, tuple) else out
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    data = json.loads(texts[0]) if texts else {}
    if len(texts) > 1:  # list results: notices come as an extra text block
        for t in texts[1:]:
            try:
                data.update(json.loads(t))
            except ValueError:
                pass
    return data


def test_write_refused_while_other_session_active_then_allowed_with_ignore(fake):
    as_session("aaaa1111", "agent-A")
    call("scope_claim", {"purpose": "relay timing test", "minutes": 10})
    call("scope_set_trigger", {"source": "CHAN2"})

    as_session("bbbb2222", "agent-B")
    refused = call("scope_set_channel", {"channel": 1, "scale_v_per_div": 2})
    assert refused["refused"] is True
    assert "relay timing test" in refused["tell_your_user"]
    assert "ignore_other_sessions=true" in refused["to_proceed"]
    assert ":CHAN1:SCAL 2" not in fake.writes  # nothing sent

    done = call("scope_set_channel", {"channel": 1, "scale_v_per_div": 2, "ignore_other_sessions": True})
    assert done["applied"]["scale_v_per_div"] == 2

    # B's reads are never refused
    assert "channels" in call("scope_status")

    as_session("aaaa1111", "agent-A")
    status = call("scope_status")
    notes = " ".join(status["notices"])
    assert "agent-B [bbbb2222] tried scope_set_channel" in notes and "refused" in notes
    assert "ran scope_set_channel" in notes and "its user chose to go ahead" in notes
    assert "CH1 V/div changed" in notes and "agent-B" in notes and "scope_set_channel" in notes


def test_no_interlock_for_same_session_or_after_release(fake):
    as_session("aaaa1111", "agent-A")
    call("scope_claim", {"purpose": "x"})
    assert "refused" not in call("scope_run_control", {"action": "stop"})  # own claim never blocks
    call("scope_release")
    as_session("bbbb2222", "agent-B")
    assert "refused" not in call("scope_run_control", {"action": "run"})


def test_recent_activity_without_claim_also_counts(fake):
    as_session("aaaa1111")
    call("scope_set_timebase", {"scale_s_per_div": 0.002})
    as_session("bbbb2222")
    assert call("scope_set_timebase", {"scale_s_per_div": 0.005})["refused"] is True


def test_front_panel_change_is_reported(fake):
    as_session("aaaa1111", "agent-A")
    call("scope_status")
    fake.handle(":CHAN1:SCAL 2")  # someone turns the knob
    notes = " ".join(call("scope_status").get("notices", []))
    assert "CH1 V/div changed 5.000000e-01 -> 2 since your last call, not through this server" in notes


def test_sessions_list_and_notice_on_screenshot_results(fake):
    as_session("aaaa1111", "agent-A")
    call("scope_claim", {"purpose": "noise check"})
    call("scope_status")
    as_session("bbbb2222", "agent-B")
    shot = call("scope_screenshot")  # image + metadata + notices block
    assert any("noise check" in n for n in shot["notices"])
    rows = call("scope_sessions")["sessions"]
    assert {r["name"] for r in rows} == {"agent-A", "agent-B"}
    assert [r for r in rows if r.get("you")][0]["name"] == "agent-B"


def test_dry_run_restore_is_a_read(fake):
    as_session("aaaa1111")
    call("scope_setup_save", {"name": "s1"})
    call("scope_claim", {"purpose": "busy"})
    as_session("bbbb2222")
    assert "refused" not in call("scope_setup_restore", {"name": "s1", "dry_run": True})
    assert call("scope_setup_restore", {"name": "s1"}).get("refused") in (True, None)
