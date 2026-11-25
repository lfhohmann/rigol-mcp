"""Shared reads and short waits behind long jobs."""

import asyncio
import json
import time

from rigol_mcp import app


def result_dict(out):
    content = out[0] if isinstance(out, tuple) else out
    data = {}
    for c in content:
        if getattr(c, "type", "") == "text":
            try:
                data.update(json.loads(c.text))
            except ValueError:
                pass
    return data


async def timed(name, args, delay=0.0):
    await asyncio.sleep(delay)
    t = time.monotonic()
    out = await app.mcp.call_tool(name, args)
    return result_dict(out), time.monotonic() - t


def test_identical_reads_share_one_scope_reading(fake):
    async def two():
        return await asyncio.gather(timed("scope_status", {}), timed("scope_status", {}))

    (a, _), (b, _) = asyncio.run(two())
    assert fake.query_log.count("*IDN?") == 1  # the scope was read once
    assert ("shared" in a) != ("shared" in b)  # exactly one of them is marked as shared
    marked = a if "shared" in a else b
    assert "fresh=true" in marked["shared"]["note"]

    fresh, _ = asyncio.run(timed("scope_status", {"fresh": True}))
    assert "shared" not in fresh and fake.query_log.count("*IDN?") == 2

    cached, _ = asyncio.run(timed("scope_status", {}))
    assert cached["shared"]["age_s"] < 2

    asyncio.run(timed("scope_set_timebase", {"scale_s_per_div": 0.002}))  # a write clears sharing
    after, _ = asyncio.run(timed("scope_status", {}))
    assert "shared" not in after and fake.query_log.count("*IDN?") == 3


def test_different_args_are_not_shared(fake):
    asyncio.run(timed("scope_measure", {"channel": 1, "items": ["VPP"]}))
    other, _ = asyncio.run(timed("scope_measure", {"channel": 1, "items": ["FREQ"]}))
    assert "shared" not in other


def test_reads_slip_in_between_export_chunks(fake):
    fake.memory_depth = 2_500_000  # 10 chunks
    fake.chunk_delay = 0.25
    fake.handle(":STOP")

    async def both():
        return await asyncio.gather(timed("scope_export_memory", {"channel": 1}),
                                    timed("scope_status", {"fresh": True}, delay=0.6))

    (export, export_s), (status, status_s) = asyncio.run(both())
    assert export_s > 2.4 and status_s < 1.0  # the read did not wait for the whole export
    assert export["points"] == 2_500_000  # and the export stayed intact (raw mode re-asserted)
    data = open(export["capture"]["file"], "rb").read()
    assert len(data) == 2_500_000 and data[:60] == bytes([190]) * 60
    assert "channels" in status


def test_reads_work_while_capture_waits(fake):
    fake.trigger_after = 2.0

    async def both():
        return await asyncio.gather(timed("scope_capture", {"timeout_s": 5, "include_screenshot": False}),
                                    timed("scope_status", {"fresh": True}, delay=0.5))

    (cap, cap_s), (status, status_s) = asyncio.run(both())
    assert cap["triggered"] and cap_s > 1.9
    assert status_s < 1.0 and status["trigger_status"] == "WAIT"  # saw the armed scope
