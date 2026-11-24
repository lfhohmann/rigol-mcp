import json

import pytest

from rigol_mcp import server
from rigol_mcp.scope import parse_number


def test_parse_number():
    assert parse_number("1.5e+00") == 1.5
    assert parse_number("9.9E37") is None
    assert parse_number("measure error!") is None


def test_status(fake):
    st = server.scope_status()
    assert st["idn"].startswith("RIGOL")
    assert st["channels"]["CH1"]["vpp"] == 3.3
    assert st["channels"]["CH2"] == {"on": False}


def test_measure_handles_errors_and_stray_newline(fake):
    m = server.scope_measure(1, ["RTIM", "VPP", "PDUT"])
    assert m == {"RTIM": None, "VPP": 3.3, "PDUT": None}


def test_waveform(fake):
    w = server.scope_waveform(1, max_points=10, include_csv=True)
    assert w["points"] == 1200
    assert len(w["samples"]) == 10
    assert w["csv_text"].count("\n") == 1201
    assert w["clipped_samples"] > 0 and "note" in w
    assert server.scope_waveform_history()["waveforms"][0]["channel"] == 1


def test_screenshot_history_and_retention(fake):
    ids = []
    for _ in range(5):
        img, meta = server.scope_screenshot()
        assert img.data.startswith(b"\x89PNG")
        ids.append(json.loads(meta)["id"])
    hist = server.scope_screenshot_history()["screenshots"]
    assert [h["id"] for h in hist] == ids[::-1][:3]  # newest first, only 3 kept
    _, latest = server.scope_screenshot_get()
    assert json.loads(latest)["id"] == ids[-1]
    with pytest.raises(FileNotFoundError):
        server.scope_screenshot_get(ids[0])  # pruned


def test_query_and_write_guards(fake):
    assert server.scope_query(":TIM:SCAL?") == "1.000000e-03"
    with pytest.raises(ValueError):
        server.scope_query(":TIM:SCAL 1")
    with pytest.raises(ValueError):
        server.scope_write("*RST")
    assert server.scope_write(":CHAN1:SCAL 1") == '0,"No error"'
    assert ":CHAN1:SCAL 1" in fake.writes


def test_read_only(fake):
    server.cfg.read_only = True
    with pytest.raises(PermissionError):
        server.scope_write(":RUN")


def test_missing_host(tmp_path):
    server.configure(server.Config(host="", data_dir=tmp_path))
    with pytest.raises(ValueError, match="RIGOL_HOST"):
        server.scope_status()
