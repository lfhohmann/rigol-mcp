"""A tiny fake DS1000Z that speaks enough SCPI over TCP for the tests."""

import socket
import struct
import threading
import zlib


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


def block(data: bytes) -> bytes:
    size = str(len(data)).encode()
    return b"#" + str(len(size)).encode() + size + data + b"\n"


class FakeScope:
    REPLIES = {
        "*IDN?": "RIGOL TECHNOLOGIES,DS1104Z,FAKE0000000001,00.04.04.SP3",
        ":TRIG:STAT?": "TD", ":TRIG:MODE?": "EDGE", ":TRIG:EDG:SOUR?": "CHAN1",
        ":TRIG:EDG:SLOP?": "POS", ":TRIG:EDG:LEV?": "1.400000e+00",
        ":TIM:SCAL?": "1.000000e-03", ":TIM:OFFS?": "0.000000e+00", ":ACQ:SRAT?": "2.500000e+08",
        ":CHAN1:DISP?": "1", ":CHAN2:DISP?": "0", ":CHAN3:DISP?": "0", ":CHAN4:DISP?": "0",
        ":CHAN1:SCAL?": "5.000000e-01", ":CHAN1:OFFS?": "0.000000e+00", ":CHAN1:COUP?": "DC",
        ":CHAN1:PROB?": "1.000000e+00", ":SYST:ERR?": '0,"No error"',
        # preamble: format,type,points,count,xinc,xorig,xref,yinc,yorig,yref
        ":WAV:PRE?": "0,0,1200,1,1.000000e-05,-6.000000e-03,0,4.000000e-02,-27,127",
    }
    MEASURE = {"VPP": "3.300000e+00", "FREQ": "1.000000e+03", "VAVG": "1.650000e+00", "VRMS": "2.000000e+00"}

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.writes = []
        threading.Thread(target=self._serve, daemon=True).start()

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
            cmd = raw.decode().strip()
            if cmd.startswith(":MEAS:ITEM?"):
                item = cmd.split()[1].split(",")[0].upper()
                if item == "RTIM":
                    conn.sendall(b"measure error!\n\n")  # real scopes leave a stray newline
                else:
                    conn.sendall((self.MEASURE.get(item, "9.9E37") + "\n").encode())
            elif cmd == ":WAV:DATA?":
                conn.sendall(block(bytes((i % 256) for i in range(1200))))
            elif cmd.startswith(":DISP:DATA?"):
                conn.sendall(block(tiny_png()))
            elif cmd in self.REPLIES:
                conn.sendall((self.REPLIES[cmd] + "\n").encode())
            elif cmd.endswith("?"):
                conn.sendall(b"0\n")
            else:
                self.writes.append(cmd)
        conn.close()

    def close(self):
        self.sock.close()
