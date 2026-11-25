"""Shared reads: identical read calls within a short window reuse one scope reading.

If agent B asks for the same status/screenshot/waveform/measurement that agent A got
(or is getting right now) less than RIGOL_SHARE_WINDOW_S ago, B receives A's result,
clearly marked, instead of queueing another scope conversation. fresh=true always reads
again. Any write clears everything, so a shared result never predates a setting change.
"""

from __future__ import annotations

import json
import os
import threading
import time

try:
    WINDOW_S = float(os.environ.get("RIGOL_SHARE_WINDOW_S", "2"))
except ValueError:
    WINDOW_S = 2.0


class Shared:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, object, str]] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._generation = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._generation += 1

    def _info(self, entry, waited: bool) -> dict:
        age = round(time.monotonic() - entry[0], 2)
        how = "was read at the same time for" if waited else f"was read {age} s ago by"
        return {"shared": True, "age_s": age, "from": entry[2],
                "note": f"This result {how} {entry[2]} and is shared to spare the scope. "
                        "Call again with fresh=true for a new reading."}

    def run(self, key: str, compute, who: str):
        """Return (result, shared_info or None)."""
        if WINDOW_S <= 0:
            return compute(), None
        with self._lock:
            entry = self._entries.get(key)
            if entry and time.monotonic() - entry[0] <= WINDOW_S:
                return entry[1], self._info(entry, False)
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = self._inflight[key] = threading.Event()
                generation = self._generation
        if not leader:
            event.wait(timeout=180)
            with self._lock:
                entry = self._entries.get(key)
            if entry:
                return entry[1], self._info(entry, True)
            return compute(), None  # the other call failed or a write intervened: read ourselves
        try:
            result = compute()
            with self._lock:
                if generation == self._generation:  # no write happened meanwhile
                    self._entries[key] = (time.monotonic(), result, who)
            return result, None
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()


shared = Shared()


def key_for(tool: str, kw: dict) -> str:
    args = {k: v for k, v in kw.items() if k not in ("fresh", "ignore_other_sessions")}
    return tool + json.dumps(args, sort_keys=True, default=str)


def mark(result, info: dict | None):
    if not info:
        return result
    if isinstance(result, dict):
        return {**result, "shared": info}
    if isinstance(result, list):
        return [*result, json.dumps({"shared": info})]
    return result
