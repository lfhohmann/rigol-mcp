import json

import pytest

from rigol_mcp import app
from rigol_mcp.analysis import ByteStats, MinMaxDecimator, estimate_frequency
from rigol_mcp.scope import Preamble, parse_number
from rigol_mcp.tools_control import (
    scope_capture, scope_run_control, scope_set_acquire, scope_set_channel, scope_set_timebase,
    scope_set_trigger, scope_write,
)
from rigol_mcp.tools_export import scope_export_memory
from rigol_mcp.tools_read import (
    scope_measure, scope_measure_between, scope_measure_stats, scope_query, scope_screenshot,
    scope_screenshot_get, scope_screenshot_history, scope_status, scope_waveform, scope_waveform_history,
)

PRE = Preamble(1200, 1e-5, 0.0, 0.0, 0.02, -127.0, 0.0)


# --- pure analysis -------------------------------------------------------------------
def test_parse_number():
    assert parse_number("1.5e+00") == 1.5
    assert parse_number("9.9E37") is None
    assert parse_number("measure error!") is None


def test_frequency_estimate_square_and_noise():
    sq = bytes(190 if i % 120 < 60 else 60 for i in range(1200))
    assert estimate_frequency(sq, PRE) == pytest.approx(833.33, rel=1e-3)
    flat = bytes([100, 101, 102, 101] * 300)  # noise only
    assert estimate_frequency(flat, PRE) is None
    # noise spikes on the high level must not count as edges
    noisy = bytearray(sq)
    for i in range(5, 1200, 120):
        noisy[i] = 140  # dip, but stays above the lower hysteresis threshold
    assert estimate_frequency(bytes(noisy), PRE) == pytest.approx(833.33, rel=1e-3)
    # bursts (irregular edge spacing) have no single frequency
    burst = bytes(190 if (i % 20 < 10 and i % 400 < 100) else 60 for i in range(1200))
    assert estimate_frequency(burst, PRE) is None


def test_bytestats_and_decimator_streaming():
    data = bytes(range(256)) * 10
    whole = ByteStats(PRE)
    whole.add(data)
    parts = ByteStats(PRE)
    dec = MinMaxDecimator(len(data), 10)
    for i in range(0, len(data), 333):  # chunk size unrelated to bucket size
        parts.add(data[i:i + 333])
        dec.add(data[i:i + 333])
    assert whole.summary() == parts.summary()
    assert parts.summary()["clipped_samples"] == 20
    assert len(dec.lo) == 10 and min(dec.lo) == 0 and max(dec.hi) == 255


# --- read-only tools -----------------------------------------------------------------
def test_status_is_computed_from_waveform(fake):
    st = scope_status()
    ch1 = st["channels"]["CH1"]
    assert st["idn"].startswith("RIGOL")
    assert ch1["vpp"] == pytest.approx(130 * 0.02)
    assert ch1["freq_hz"] == pytest.approx(833.33, rel=1e-3)
    assert st["channels"]["CH2"] == {"on": False}
    assert not any(w.startswith(":MEAS") for w in fake.writes)


def test_measure_handles_errors_and_stray_newline(fake):
    assert scope_measure(1, ["RTIM", "VPP", "PDUT"]) == {"RTIM": None, "VPP": 3.3, "PDUT": None}


def test_measure_between(fake):
    out = scope_measure_between(1, 2, ["RDEL", "RPH", "FDEL"])
    assert out["values"] == {"RDEL": 1e-3, "RPH": 36.0, "FDEL": None}
    assert out["units"]["RPH"] == "deg"


def test_measure_stats(fake):
    out = scope_measure_stats(1, "VPP", samples=5)
    assert out["valid"] == 5 and out["mean"] == 3.3 and out["stdev"] == 0
    bad = scope_measure_stats(1, "RTIM", samples=3)
    assert bad["valid"] == 0 and bad["invalid"] == 3
    with pytest.raises(ValueError):
        scope_measure_stats(1, samples=1)


def test_waveform(fake):
    w = scope_waveform(1, max_points=10, include_csv=True)
    assert w["points"] == 1200 and len(w["samples"]) == 10
    assert w["csv_text"].count("\n") == 1201
    assert w["clipped_samples"] == 0 and "note" not in w
    assert scope_waveform_history()["waveforms"][0]["channel"] == 1


def test_screenshot_history_and_retention(fake):
    ids = []
    for _ in range(5):
        img, meta = scope_screenshot()
        assert img.data.startswith(b"\x89PNG")
        ids.append(json.loads(meta)["id"])
    hist = scope_screenshot_history()["screenshots"]
    assert [h["id"] for h in hist] == ids[::-1][:3]  # newest first, only 3 kept
    _, latest = scope_screenshot_get()
    assert json.loads(latest)["id"] == ids[-1]
    with pytest.raises(FileNotFoundError):
        scope_screenshot_get(ids[0])  # pruned


def test_query_guard(fake):
    assert scope_query(":TIM:SCAL?")["reply"] == "1.000000e-03"
    with pytest.raises(ValueError):
        scope_query(":TIM:SCAL 1")


# --- control tools -------------------------------------------------------------------
def test_run_control(fake):
    assert scope_run_control("stop")["trigger_status"] == "STOP"
    assert scope_run_control("run")["trigger_status"] == "TD"


def test_set_channel_reads_back(fake):
    out = scope_set_channel(1, scale_v_per_div=1, offset_v=-2, coupling="AC", bandwidth_limit=True, probe=10)
    assert out["applied"]["scale_v_per_div"] == 1 and out["applied"]["coupling"] == "AC"
    assert out["applied"]["bandwidth_limit"] == "20M" and out["applied"]["probe"] == 10
    assert fake.writes.index(":CHAN1:PROB 10") < fake.writes.index(":CHAN1:SCAL 1")
    assert out["error"] == '0,"No error"'
    with pytest.raises(ValueError):
        scope_set_channel(1)


def test_set_timebase_trigger_acquire(fake):
    assert scope_set_timebase(scale_s_per_div=0.005)["applied"]["scale_s_per_div"] == 0.005
    trig = scope_set_trigger(source="CHAN2", level_v=1.2, slope="NEG", sweep="NORM")["applied"]
    assert trig == {**trig, "source": "CHAN2", "level_v": 1.2, "slope": "NEG", "sweep": "NORM", "mode": "EDGE"}
    assert scope_set_acquire(type="AVER", averages=16)["applied"]["averages"] == "16"


def test_capture_triggers_and_returns_screenshot(fake):
    img, meta = scope_capture(timeout_s=5, source="CHAN1", level_v=1.0, slope="POS")
    meta = json.loads(meta)
    assert img.data.startswith(b"\x89PNG")
    assert meta["triggered"] and meta["channels"]["CH1"]["freq_hz"] == pytest.approx(833.33, rel=1e-3)
    assert ":SING" in fake.writes and ":TRIG:EDG:LEV 1" in fake.writes


def test_capture_restores_sweep_and_stays_stopped(fake):
    fake.state[":TRIG:SWE"] = "NORM"
    _, meta = scope_capture(timeout_s=5)
    meta = json.loads(meta)
    assert meta["trigger_sweep"] == "NORM" and meta["trigger_status"] == "STOP"
    fake.state[":TRIG:SWE"] = "AUTO"
    out = scope_capture(timeout_s=5, include_screenshot=False, restore_sweep=False)
    assert out["trigger_sweep"] == "SING"


def test_capture_timeout(fake):
    fake.trigger_after = None  # the event never happens
    out = scope_capture(timeout_s=0.6, include_screenshot=False)
    assert out["triggered"] is False and out["trigger_status"] == "WAIT"
    assert out["previous_sweep"] == "AUTO" and "scope_run_control" in out["note"]


def test_write_guards(fake):
    with pytest.raises(ValueError):
        scope_write("*RST")
    assert scope_write(":MATH:DISP ON")["error"] == '0,"No error"'


def test_read_only_blocks_every_write(fake):
    app.cfg.read_only = True
    for call in (lambda: scope_write(":RUN"), lambda: scope_run_control("stop"),
                 lambda: scope_set_channel(1, scale_v_per_div=1), lambda: scope_set_timebase(offset_s=0),
                 lambda: scope_set_trigger(level_v=1), lambda: scope_set_acquire(type="NORM"),
                 lambda: scope_capture(), lambda: scope_export_memory(1, stop=True)):
        with pytest.raises(PermissionError):
            call()
    assert scope_status()["channels"]["CH1"]["on"]  # reads still work


# --- full memory export --------------------------------------------------------------
def test_export_requires_stopped_scope(fake):
    with pytest.raises(RuntimeError, match="not stopped"):
        scope_export_memory(1)


def test_export_bin_streams_all_chunks(fake):
    out = scope_export_memory(1, stop=True, preview_points=50)
    assert out["points"] == 600_000  # 3 chunks of up to 250k
    data = open(out["capture"]["file"], "rb").read()
    assert len(data) == 600_000 and data[:60] == bytes([190]) * 60
    meta = json.load(open(out["metadata_file"]))
    assert meta["scaling"]["yincrement"] == 0.02
    assert out["stats"]["freq_hz"] == pytest.approx(833.33, rel=1e-2)
    assert len(out["preview"]["rows"]) == 50
    assert [w for w in fake.writes if w.startswith(":WAV:STAR")][:3] == [":WAV:STAR 1", ":WAV:STAR 250001", ":WAV:STAR 500001"]
    assert fake.state[":WAV:MODE"] == "NORM"  # restored


def test_export_csv_and_retention(fake):
    fake.memory_depth = 1000
    scope_run_control("stop")
    out = scope_export_memory(1, format="csv")
    lines = open(out["capture"]["file"]).read().splitlines()
    assert lines[0] == "time_s,volts" and len(lines) == 1001
    for _ in range(2):
        scope_export_memory(1)
    exports = scope_waveform_history(kind="exports")["exports"]
    assert len(exports) == 2  # keep_exports=2
    assert not app.store.root.joinpath("exports", out["capture"]["file"].split("/")[-1]).exists()
