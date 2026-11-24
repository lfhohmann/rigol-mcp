"""Minimal SCPI client for Rigol DS1000Z oscilloscopes over a raw TCP socket (port 5555)."""

from __future__ import annotations

import socket

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

    def waveform(self, channel: int) -> tuple[list[float], list[float], bytes]:
        """On-screen waveform (NORMal mode, 1200 points): (times_s, volts, raw_bytes)."""
        self.write(f":WAV:SOUR CHAN{channel}")
        self.write(":WAV:MODE NORM")
        self.write(":WAV:FORM BYTE")
        preamble = self.query(":WAV:PRE?").split(",")
        raw = self.block(":WAV:DATA?")
        xinc, xorig, xref, yinc, yorig, yref = map(float, preamble[4:10])
        volts = [(b - yorig - yref) * yinc for b in raw]
        times = [xorig + (i - xref) * xinc for i in range(len(raw))]
        return times, volts, raw

    def screenshot(self) -> bytes:
        """The display as PNG bytes."""
        return self.block(":DISP:DATA? ON,OFF,PNG")
