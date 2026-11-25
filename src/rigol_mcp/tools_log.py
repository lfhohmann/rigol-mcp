"""Background measurement logging: read built-in measurements every N seconds into a CSV."""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import app
from .captures import Capture
from .scope import ScopeError, parse_number

MAX_COLUMNS = 10
MAX_FAILURES = 5


@dataclass
class Column:
    name: str
    count: int = 0
    invalid: int = 0
    total: float = 0.0
    lo: float = math.inf
    hi: float = -math.inf

    def add(self, value: float | None) -> None:
        if value is None:
            self.invalid += 1
            return
        self.count += 1
        self.total += value
        self.lo, self.hi = min(self.lo, value), max(self.hi, value)

    def summary(self) -> dict:
        if not self.count:
            return {"valid": 0, "invalid": self.invalid}
        return {"valid": self.count, "invalid": self.invalid, "min": self.lo, "max": self.hi,
                "mean": self.total / self.count}


@dataclass
class LogJob:
    cap: Capture
    measurements: list[tuple[int, str]]
    interval_s: float
    duration_s: float
    started: float = field(default_factory=time.monotonic)
    stop_event: threading.Event = field(default_factory=threading.Event)
    rows: int = 0
    state: str = "running"  # running | finished | stopped | failed
    error: str | None = None
    last: dict | None = None
    columns: list[Column] = field(default_factory=list)

    def info(self) -> dict:
        elapsed = time.monotonic() - self.started
        out = {
            "id": self.cap.id,
            "state": self.state,
            "rows": self.rows,
            "elapsed_s": round(elapsed, 1),
            "interval_s": self.interval_s,
            "duration_s": self.duration_s,
            "measurements": [f"CH{ch} {item}" for ch, item in self.measurements],
            "capture": self.cap.to_dict(app.cfg.public_url),
            "last_row": self.last,
            "stats": {c.name: c.summary() for c in self.columns},
        }
        if self.error:
            out["error"] = self.error
        return out


_jobs: dict[str, LogJob] = {}
_jobs_lock = threading.Lock()


def _run(job: LogJob) -> None:
    failures = 0
    next_tick = time.monotonic()
    with job.cap.path.open("a", newline="") as fh:
        writer = csv.writer(fh)
        while not job.stop_event.is_set():
            if time.monotonic() - job.started >= job.duration_s:
                job.state = "finished"
                return
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            try:
                with app.scope_lock, app.connect() as s:  # short hold: other tools interleave
                    values = [parse_number(s.query(f":MEAS:ITEM? {item},CHAN{ch}")) for ch, item in job.measurements]
                failures = 0
            except (OSError, ScopeError) as exc:
                failures += 1
                job.error = f"{type(exc).__name__}: {exc}"
                if failures >= MAX_FAILURES:
                    job.state = "failed"
                    return
                values = [None] * len(job.measurements)
            elapsed = round(time.monotonic() - job.started, 3)
            writer.writerow([stamp, elapsed] + ["" if v is None else v for v in values])
            fh.flush()
            for col, v in zip(job.columns, values):
                col.add(v)
            job.rows += 1
            job.last = {"timestamp_utc": stamp, "elapsed_s": elapsed,
                        **{c.name: v for c, v in zip(job.columns, values)}}
            next_tick += job.interval_s
            job.stop_event.wait(max(0.0, next_tick - time.monotonic()))
        job.state = "stopped"


def _get(job_id: str) -> LogJob:
    with _jobs_lock:
        if not _jobs:
            raise FileNotFoundError("no measurement log has been started since the server started")
        if job_id in ("", "latest"):
            return max(_jobs.values(), key=lambda j: j.started)
        if job_id not in _jobs:
            raise FileNotFoundError(f"no log with id {job_id!r}; older logs are files, see scope_waveform_history(kind='logs')")
        return _jobs[job_id]


@app.tool()
def scope_log_start(
    channels: list[int] | None = None,
    items: list[str] | None = None,
    interval_s: float = 1.0,
    duration_s: float = 60.0,
) -> dict:
    """Start logging built-in measurements in the background, e.g. VPP and FREQ on CH1
    every second for 10 minutes, into a timestamped CSV. Returns at once; check progress
    with scope_log_status and stop early with scope_log_stop. One log runs at a time; other
    tools keep working meanwhile. Columns = every channel x item (max 10). interval_s >= 0.2,
    but each reading takes ~40-400 ms on the scope, so many columns slow the rate down.
    duration_s up to 86400 (24 h). Values only change while the scope is running.
    Side effect: items appear in the scope's on-screen measurement bar. Read-only otherwise."""
    channels = channels or [1]
    items = [i.upper() for i in (items or ["VPP"])]
    for ch in channels:
        app.check_channel(ch)
    measurements = [(ch, item) for ch in channels for item in items]
    if not 1 <= len(measurements) <= MAX_COLUMNS:
        raise ValueError(f"1-{MAX_COLUMNS} columns (channels x items) per log")
    if not 0.2 <= interval_s <= 3600:
        raise ValueError("interval_s must be 0.2-3600")
    if not 1 <= duration_s <= 86400:
        raise ValueError("duration_s must be 1-86400")
    if not app.cfg.host:
        raise ValueError("RIGOL_HOST is not set: give the scope's IP address or host name")
    with _jobs_lock:
        running = [j for j in _jobs.values() if j.state == "running"]
        if running:
            raise RuntimeError(f"log {running[0].cap.id} is still running; stop it with scope_log_stop first")
        cap = app.store.reserve("logs", ".csv")
        names = [f"CH{ch}_{item}" for ch, item in measurements]
        tmp = cap.path.with_name(cap.name + ".tmp")
        tmp.write_text(",".join(["timestamp_utc", "elapsed_s"] + names) + "\n")
        app.store.commit(cap)
        job = LogJob(cap, measurements, interval_s, duration_s, columns=[Column(n) for n in names])
        _jobs[cap.id] = job
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"log-{cap.id}").start()
    return {"started": True, **job.info(),
            "next": "scope_log_status(id) for progress, scope_log_stop(id) to end early"}


@app.tool()
def scope_log_status(id: str = "latest", recent_rows: int = 5) -> dict:
    """Progress of a measurement log ('latest' or an id): state, rows, last values and
    min/max/mean per column so far, plus the newest rows."""
    job = _get(id)
    out = job.info()
    if recent_rows > 0 and job.cap.path.exists():
        lines = job.cap.path.read_text().splitlines()
        out["recent"] = [lines[0]] + lines[1:][-recent_rows:]
    return out


@app.tool()
def scope_log_stop(id: str = "latest") -> dict:
    """Stop a running measurement log ('latest' or an id). The CSV keeps what was logged."""
    job = _get(id)
    if job.state == "running":
        job.stop_event.set()
        for _ in range(50):
            if job.state != "running":
                break
            time.sleep(0.1)
    return job.info()
