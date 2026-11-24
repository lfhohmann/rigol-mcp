"""Host-side waveform analysis (no numpy: everything works on raw ADC bytes)."""

from __future__ import annotations

import math
from collections import Counter

from .scope import Preamble, Waveform


class ByteStats:
    """Streaming statistics over 8-bit ADC samples via a 256-bin histogram.
    Memory use is constant, so it works for 24M-point memory dumps."""

    def __init__(self, pre: Preamble):
        self.pre = pre
        self.hist: Counter[int] = Counter()

    def add(self, chunk: bytes) -> None:
        self.hist.update(chunk)

    @property
    def count(self) -> int:
        return sum(self.hist.values())

    def summary(self) -> dict:
        n = self.count
        if not n:
            return {"points": 0}
        v = self.pre.volts
        lo, hi = min(self.hist), max(self.hist)
        mean = sum(v(b) * c for b, c in self.hist.items()) / n
        meansq = sum(v(b) ** 2 * c for b, c in self.hist.items()) / n
        return {
            "points": n,
            "vmin": v(lo),
            "vmax": v(hi),
            "vpp": v(hi) - v(lo),
            "mean": mean,
            "rms": math.sqrt(meansq),
            "stdev": math.sqrt(max(meansq - mean**2, 0.0)),
            "clipped_samples": self.hist[0] + self.hist[255],
        }


def estimate_frequency(raw: bytes, pre: Preamble, hysteresis: float = 0.15, max_jitter: float = 0.2) -> float | None:
    """Frequency from rising crossings of the mid level, with hysteresis so noise on a
    level doesn't count as edges. None if the signal is flat, has < 2 rising edges, or the
    edges are irregular (spread of periods > max_jitter of the mean, e.g. data bursts)."""
    if not raw:
        return None
    lo_b, hi_b = min(raw), max(raw)
    if hi_b - lo_b < 8:  # under ~3% of full scale: treat as flat/noise
        return None
    mid = (lo_b + hi_b) / 2
    band = (hi_b - lo_b) * hysteresis
    upper, lower = mid + band, mid - band
    state = None
    rising: list[int] = []
    for i, b in enumerate(raw):
        if b >= upper:
            if state is False:
                rising.append(i)
            state = True
        elif b <= lower:
            state = False
    if len(rising) < 2:
        return None
    gaps = [b - a for a, b in zip(rising, rising[1:])]
    mean_gap = sum(gaps) / len(gaps)
    if len(gaps) > 1 and (max(gaps) - min(gaps)) > max_jitter * mean_gap:
        return None  # bursts or irregular edges: one frequency would be misleading
    period = mean_gap * pre.xinc
    return 1.0 / period if period > 0 else None


def waveform_summary(wf: Waveform) -> dict:
    stats = ByteStats(wf.pre)
    stats.add(wf.raw)
    out = stats.summary()
    out["freq_hz"] = estimate_frequency(wf.raw, wf.pre)
    return out


class MinMaxDecimator:
    """Streaming min/max per bucket, for a small preview of a huge record."""

    def __init__(self, total: int, buckets: int):
        self.size = max(1, math.ceil(total / max(buckets, 1)))
        self.lo: list[int] = []
        self.hi: list[int] = []
        self.pos = 0

    def add(self, chunk: bytes) -> None:
        i = 0
        while i < len(chunk):
            bucket = self.pos // self.size
            end = min(len(chunk), i + (bucket + 1) * self.size - self.pos)
            part = chunk[i:end]
            if bucket == len(self.lo):
                self.lo.append(min(part))
                self.hi.append(max(part))
            else:
                self.lo[bucket] = min(self.lo[bucket], min(part))
                self.hi[bucket] = max(self.hi[bucket], max(part))
            self.pos += end - i
            i = end

    def preview(self, pre: Preamble) -> list[list[float]]:
        """[[t_start_s, vmin, vmax], ...]"""
        return [
            [round(pre.time(k * self.size), 9), round(pre.volts(lo), 5), round(pre.volts(hi), 5)]
            for k, (lo, hi) in enumerate(zip(self.lo, self.hi))
        ]
