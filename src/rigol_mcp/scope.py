"""Minimal SCPI client for Rigol DS1000Z oscilloscopes over a raw TCP socket (port 5555)."""

from __future__ import annotations

import socket
from dataclasses import dataclass

INVALID = 9.0e37  # the scope reports 9.9E37 when a measurement is not valid


class ScopeError(RuntimeError):
    """Connection or protocol error talking to the scope."""


def parse_number(text: str) -> float | None:
    """Parse a numeric reply. Returns None for invalid results (9.9E37, 'measure error!')."""
    try:
        value = float(text)
    except ValueError:
        return None
    return None if abs(value) >= INVALID else value


class Scope:
    """One TCP connection to the scope. Use as a context manager."""

    def __init__(self, host: str, port: int = 5555, timeout: float = 10.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise ScopeError(f"cannot connect to scope at {host}:{port}: {exc}") from exc
        self.file = self.sock.makefile("rb")

    def __enter__(self) -> Scope:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.file.close()
        self.sock.close()

    def write(self, command: str) -> None:
        self.sock.sendall((command + "\n").encode())

    def query(self, command: str) -> str:
        self.write(command)
        line = self._readline()
        # A failed query (e.g. 'measure error!') can leave a stray blank line behind.
        while line == "":
            line = self._readline()
        return line

    def _readline(self) -> str:
        raw = self.file.readline()
        if not raw:
            raise ScopeError("connection closed by scope")
        return raw.decode(errors="replace").strip()

    def block(self, command: str) -> bytes:
        """Send a query that returns an IEEE 488.2 definite-length block (#N<len><data>)."""
        self.write(command)
        c = self.file.read(1)
        while c in (b"\r", b"\n", b" "):
            c = self.file.read(1)
        if c != b"#":
            raise ScopeError(f"expected binary block, got {c!r}")
        digits = int(self.file.read(1))
        size = int(self.file.read(digits))
        data = self.file.read(size)
        if len(data) != size:
            raise ScopeError(f"short read: {len(data)} of {size} bytes")
        self.file.read(1)  # trailing newline
        return data

    def preamble(self) -> Preamble:
        return Preamble.parse(self.query(":WAV:PRE?"))

    def waveform(self, channel: int) -> Waveform:
        """On-screen waveform (NORMal mode, 1200 points)."""
        self.write(f":WAV:SOUR CHAN{channel}")
        self.write(":WAV:MODE NORM")
        self.write(":WAV:FORM BYTE")
        pre = self.preamble()
        raw = self.block(":WAV:DATA?")
        return Waveform(pre, raw)

    def screenshot(self) -> bytes:
        """The display as PNG bytes."""
        return self.block(":DISP:DATA? ON,OFF,PNG")


@dataclass
class Preamble:
    points: int
    xinc: float
    xorig: float
    xref: float
    yinc: float
    yorig: float
    yref: float

    @classmethod
    def parse(cls, text: str) -> Preamble:
        f = text.split(",")
        return cls(int(float(f[2])), *map(float, f[4:10]))

    def volts(self, byte: int) -> float:
        return (byte - self.yorig - self.yref) * self.yinc

    def time(self, index: int) -> float:
        return self.xorig + (index - self.xref) * self.xinc


@dataclass
class Waveform:
    pre: Preamble
    raw: bytes

    @property
    def volts(self) -> list[float]:
        table = [self.pre.volts(b) for b in range(256)]
        return [table[b] for b in self.raw]

    @property
    def times(self) -> list[float]:
        return [self.pre.time(i) for i in range(len(self.raw))]

    @property
    def clipped(self) -> int:
        """Samples at the ADC limits, i.e. off the top or bottom of the screen."""
        return self.raw.count(0) + self.raw.count(255)
