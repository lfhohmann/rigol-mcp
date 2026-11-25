"""A small stateful fake DS1000Z that speaks enough SCPI over TCP for the tests.

Signal on every channel: a square wave, 120 samples per period, low byte 60 / high byte 190
(no clipping). On screen: 1200 points at 10 us/sample, so 833.3 Hz.
"""

import socket
import struct
import threading
import time
import zlib

PERIOD = 120
XINC = 1e-5


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


def block(data: bytes) -> bytes:
    size = str(len(data)).encode()
    return b"#" + str(len(size)).encode() + size + data + b"\n"


def square(start: int, count: int) -> bytes:
    return bytes(190 if (i % PERIOD) < PERIOD // 2 else 60 for i in range(start, start + count))


class FakeScope:
    MEASURE = {"VPP": "3.300000e+00", "FREQ": "1.000000e+03", "VAVG": "1.650000e+00", "VRMS": "2.000000e+00",
               "RDELAY": "1.000000e-03", "RPHASE": "3.600000e+01"}

    def __init__(self, memory_depth: int = 600_000, trigger_after: float = 0.3):
        self.memory_depth = memory_depth
        self.trigger_after = trigger_after  # seconds after :SING until the "event" happens
        self.state = {
            "*IDN": "RIGOL TECHNOLOGIES,DS1104Z,FAKE0000000001,00.04.04.SP3",
            ":TRIG:MODE": "EDGE", ":TRIG:EDG:SOUR": "CHAN1", ":TRIG:EDG:SLOP": "POS",
            ":TRIG:EDG:LEV": "1.400000e+00", ":TRIG:SWE": "AUTO", ":TRIG:COUP": "DC", ":TRIG:HOLD": "1.600000e-08",
            ":TIM:SCAL": "1.000000e-03", ":TIM:OFFS": "0.000000e+00", ":TIM:MAIN:SCAL": "1.000000e-03",
            ":TIM:MAIN:OFFS": "0.000000e+00", ":TIM:MODE": "MAIN", ":ACQ:SRAT": "1.000000e+05",
            ":ACQ:MDEP": str(memory_depth), ":ACQ:TYPE": "NORM", ":ACQ:AVER": "2",
            ":CHAN1:DISP": "1", ":CHAN2:DISP": "0", ":CHAN3:DISP": "0", ":CHAN4:DISP": "0",
            ":CHAN1:SCAL": "5.000000e-01", ":CHAN1:OFFS": "0.000000e+00", ":CHAN1:COUP": "DC",
            ":CHAN1:PROB": "1.000000e+00", ":CHAN1:BWL": "OFF", ":CHAN1:INV": "0",
            ":WAV:MODE": "NORM", ":WAV:STAR": "1", ":WAV:STOP": "1200", ":WAV:SOUR": "CHAN1",
        }
        self.vary = False  # make VPP change on every reading (for logging tests)
        self.counter = 0
        self.run_state = "RUN"  # RUN | SINGLE | STOP
        self.armed_at = 0.0
        self.writes: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    # --- trigger state machine -------------------------------------------------------
    def trig_status(self) -> str:
        if self.run_state == "RUN":
            return "TD"
        if self.run_state == "SINGLE":
            if self.trigger_after is not None and time.monotonic() - self.armed_at >= self.trigger_after:
                self.run_state = "STOP"
                return "STOP"
            return "WAIT"
        return "STOP"

    def preamble(self) -> str:
        points = self.memory_depth if self.state[":WAV:MODE"] == "RAW" else 1200
        # format,type,points,count,xinc,xorig,xref,yinc,yorig,yref
        return f"0,{2 if points > 1200 else 0},{points},1,{XINC:e},-6.000000e-03,0,2.000000e-02,-127,0"

    # --- socket handling -------------------------------------------------------------
    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        f = conn.makefile("rb")
        for raw in f:
            reply = self.handle(raw.decode().strip())
            if reply is not None:
                conn.sendall(reply)
        conn.close()

    def handle(self, cmd: str):
        if cmd.endswith("?") or cmd.startswith(":MEAS:ITEM?") or cmd.startswith(":DISP:DATA?"):
            return self.query(cmd)
        self.writes.append(cmd)
        head, _, value = cmd.partition(" ")
        if head == ":RUN":
            self.run_state = "RUN"
        elif head == ":STOP":
            self.run_state = "STOP"
        elif head == ":SING":
            self.run_state, self.armed_at = "SINGLE", time.monotonic()
            self.state[":TRIG:SWE"] = "SING"  # like the real scope
        elif value:
            if head.endswith((":DISP", ":INV")):
                value = "1" if value in ("ON", "1") else "0"
            self.state[head] = value
        return None

    def query(self, cmd: str) -> bytes:
        if cmd.startswith(":MEAS:ITEM?"):
            args = cmd.split(None, 1)[1].split(",")
            if args[0].upper() == "RTIM":
                return b"measure error!\n\n"  # real scopes leave a stray newline
            if self.vary and args[0].upper() == "VPP":
                self.counter += 1
                return f"{3.0 + (self.counter % 5) * 0.1:e}\n".encode()
            return (self.MEASURE.get(args[0].upper(), "9.9E37") + "\n").encode()
        if cmd == ":WAV:DATA?":
            if self.state[":WAV:MODE"] == "RAW":
                start, stop = int(self.state[":WAV:STAR"]), int(self.state[":WAV:STOP"])
                stop = min(stop, self.memory_depth)
                return block(square(start - 1, stop - start + 1))
            return block(square(0, 1200))
        if cmd.startswith(":DISP:DATA?"):
            return block(tiny_png())
        key = cmd[:-1]
        if key == ":TRIG:STAT":
            return (self.trig_status() + "\n").encode()
        if key == ":WAV:PRE":
            return (self.preamble() + "\n").encode()
        if key == "*OPC":
            return b"1\n"
        if key == ":SYST:ERR":
            return b'0,"No error"\n'
        return (self.state.get(key, "0") + "\n").encode()

    def close(self):
        self.sock.close()
