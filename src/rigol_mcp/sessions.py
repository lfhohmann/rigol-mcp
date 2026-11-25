"""Who is using the scope: sessions, claims, the interlock and per-session notices.

Every MCP connection is a session (HTTP: the Mcp-Session-Id header; stdio: one session).
A session is "active" while it holds a claim or touched the scope within the interlock
window. Writes from other sessions are refused while one is active, unless the caller
passes ignore_other_sessions=true (after asking its user). Sessions learn about each
other through notices attached to their next tool response; nothing is pushed.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


INTERLOCK = os.environ.get("RIGOL_INTERLOCK", "on").strip().lower() not in ("0", "off", "false", "no")
WINDOW_S = _env_float("RIGOL_INTERLOCK_WINDOW_S", 300.0)
FORGET_AFTER_S = 3600.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


@dataclass
class Session:
    id: str
    label: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_activity: float = 0.0  # last time it touched the scope
    last_tool: str = ""
    last_write: float = 0.0
    last_write_desc: str = ""
    purpose: str = ""
    claim_until: float = 0.0
    notices: list[str] = field(default_factory=list)
    seen_activity: dict[str, float] = field(default_factory=dict)  # other id -> their activity we told about
    snapshot: dict | None = None  # scope settings at the end of this session's last scope call
    snapshot_time: float = 0.0

    @property
    def short(self) -> str:
        return self.id[:8]

    @property
    def name(self) -> str:
        return f"{self.label} [{self.short}]" if self.label and self.label != self.id else f"session {self.short}"

    def claimed(self, now: float) -> bool:
        return self.claim_until > now

    def active(self, now: float) -> bool:
        return self.claimed(now) or (self.last_activity and now - self.last_activity < WINDOW_S)

    def describe(self, now: float) -> dict:
        out = {"session": self.short, "name": self.label or None, "last_tool": self.last_tool or None,
               "last_activity": ago(now - self.last_activity) if self.last_activity else None}
        if self.purpose:
            out["purpose"] = self.purpose
        if self.claimed(now):
            out["claimed_for"] = f"{int((self.claim_until - now) // 60)} more min"
        if self.last_write:
            out["last_change"] = f"{self.last_write_desc} ({ago(now - self.last_write)})"
        return out


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def get(self, sid: str, label: str) -> Session:
        now = time.time()
        with self._lock:
            for old in [k for k, s in self._sessions.items() if now - s.last_seen > FORGET_AFTER_S and not s.claimed(now)]:
                del self._sessions[old]
            s = self._sessions.get(sid)
            if s is None:
                s = self._sessions[sid] = Session(sid, label)
            elif label and s.label != label:
                s.label = label
            s.last_seen = now
            return s

    def find(self, sid: str) -> Session | None:
        with self._lock:
            return self._sessions.get(sid)

    def others_active(self, me: Session) -> list[Session]:
        now = time.time()
        with self._lock:
            return [s for s in self._sessions.values() if s.id != me.id and s.active(now)]

    def all(self) -> list[Session]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)

    def notify(self, targets: list[Session], text: str) -> None:
        with self._lock:
            for s in targets:
                s.notices.append(f"[{utc_now()}] {text}")
                del s.notices[:-20]  # keep the inbox small

    def take_notices(self, me: Session) -> list[str]:
        with self._lock:
            out, me.notices = me.notices, []
            return out

    def news_about_others(self, me: Session) -> list[str]:
        """One line per other active session whose activity this session hasn't heard about."""
        now = time.time()
        lines = []
        with self._lock:
            for s in self._sessions.values():
                if s.id == me.id or not s.active(now):
                    continue
                if me.seen_activity.get(s.id) == s.last_activity:
                    continue
                me.seen_activity[s.id] = s.last_activity
                d = s.describe(now)
                what = f" working on '{s.purpose}'" if s.purpose else ""
                lines.append(f"{s.name} is also using the scope{what} (last call: {d['last_tool']}, "
                             f"{d['last_activity']}).")
        return lines

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()


registry = Registry()
