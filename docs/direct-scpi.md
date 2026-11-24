# Talking to the scope directly (no MCP)

The scope accepts SCPI commands on a raw TCP socket. This page is for scripts, or for agents
without MCP. Don't run it at the same time as rigol-mcp: the scope handles one client at a time.

- Address: your scope's IP (here `192.168.1.50`), TCP port `5555`. No VISA or drivers needed.
- Protocol: send an ASCII command ending in `\n`. Commands ending in `?` return one reply line.
  Binary replies (waveforms, screenshots) use an IEEE block: `#`, one digit N, N digits giving
  the byte count, then the bytes and a trailing `\n`.
- Check that you can reach it: `echo '*IDN?' | nc -w 3 192.168.1.50 5555`

## Rules

- Read-only by default. Don't change settings (V/div, timebase, trigger, run/stop, `*RST`)
  unless the user asks. Something real may be connected to the probes.
- Use one connection at a time and close it when you're done. The scope handles concurrent
  clients poorly.
- `9.9E37` in a measurement means "invalid". For example, a flat DC signal has no frequency,
  and a clipped signal has no valid Vpp. Report it as invalid, not as a number.
  Some measurements (such as `RTIM` when no clean edge is visible) reply `measure error!` and
  leave an extra blank line behind. The helper below skips it.

## Python helper (standard library only)

```python
import socket

class Scope:
    def __init__(self, host="192.168.1.50", port=5555, timeout=10):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.f = self.s.makefile("rb")

    def write(self, cmd):
        self.s.sendall((cmd + "\n").encode())

    def query(self, cmd):
        self.write(cmd)
        return self.f.readline().decode().strip()

    def block(self, cmd):
        self.write(cmd)
        c = self.f.read(1)
        while c in b"\r\n ":  # a failed query can leave a stray blank line
            c = self.f.read(1)
        assert c == b"#", f"expected IEEE block, got {c!r}"
        n = int(self.f.read(1))
        size = int(self.f.read(n))
        data = self.f.read(size)
        self.f.read(1)  # trailing newline
        return data

    def waveform(self, ch=1):
        """Return (times_s, volts) for the on-screen waveform (1200 points)."""
        self.write(f":WAV:SOUR CHAN{ch}")
        self.write(":WAV:MODE NORM")
        self.write(":WAV:FORM BYTE")
        pre = self.query(":WAV:PRE?").split(",")
        xinc, xor, xref, yinc, yor, yref = map(float, pre[4:10])
        raw = self.block(":WAV:DATA?")
        volts = [(b - yor - yref) * yinc for b in raw]
        times = [xor + (i - xref) * xinc for i in range(len(raw))]
        return times, volts

    def screenshot(self, path="screen.png"):
        open(path, "wb").write(self.block(":DISP:DATA? ON,OFF,PNG"))
        return path

    def close(self):
        self.s.close()
```

## Useful commands

| Command | Returns |
|---|---|
| `*IDN?` | model and firmware |
| `:CHAN<n>:DISP?` | 1 if channel n is on |
| `:CHAN<n>:SCAL?` / `:CHAN<n>:COUP?` | V/div, coupling |
| `:TIM:SCAL?` | timebase, seconds per division |
| `:ACQ:SRAT?` | sample rate |
| `:TRIG:STAT?` | TD, WAIT, RUN, AUTO or STOP |
| `:MEAS:ITEM? VPP,CHAN1` | peak-to-peak voltage |
| `:MEAS:ITEM? VAVG,CHAN1` | average voltage |
| `:MEAS:ITEM? FREQ,CHAN1` | frequency |
| `:MEAS:ITEM? VRMS,CHAN1` | RMS voltage |
| `:MEAS:ITEM? VMAX,CHAN1` / `VMIN` | max / min voltage |
| `:MEAS:ITEM? PDUT,CHAN1` | positive duty cycle |
| `:MEAS:ITEM? RTIM,CHAN1` | rise time |

## Typical first look

1. Run `*IDN?` to confirm the connection.
2. For each channel 1 to 4, check `:CHAN<n>:DISP?`. For channels that are on, read
   V/div, coupling, VPP, VAVG and FREQ.
3. Take a screenshot and look at it to see what's on the display.
4. For analysis (ripple, noise, edges), download the waveform and work with the samples.

Full SCPI reference: Rigol "MSO1000Z/DS1000Z Programming Guide".
