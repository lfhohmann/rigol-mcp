"""Timestamped storage for screenshots and waveforms, with a keep-the-last-N retention."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"screenshots": ("screenshot", ".png"), "waveforms": ("waveform", ".csv")}
NAME_RE = re.compile(
    r"^(?P<prefix>screenshot|waveform)_(?P<id>\d{8}T\d{6}\.\d{3}Z)(?:_ch(?P<ch>[1-4]))?\.(?:png|csv)$"
)


@dataclass
class Capture:
    kind: str
    name: str
    path: Path
    id: str
    channel: int | None

    @property
    def timestamp(self) -> str:
        """ISO 8601 UTC time the capture was taken."""
        dt = datetime.strptime(self.id, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def to_dict(self, base_url: str | None = None) -> dict:
        out = {
            "id": self.id,
            "timestamp_utc": self.timestamp,
            "file": str(self.path),
            "bytes": self.path.stat().st_size,
        }
        if self.channel is not None:
            out["channel"] = self.channel
        if base_url:
            out["url"] = f"{base_url.rstrip('/')}/{self.kind}/{self.name}"
        return out


def _new_id() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S.") + f"{now.microsecond // 1000:03d}Z"


class CaptureStore:
    """Files live in <root>/screenshots and <root>/waveforms. keep=0 means keep everything."""

    def __init__(self, root: Path, keep_screenshots: int = 100, keep_waveforms: int = 100):
        self.root = Path(root)
        self.keep = {"screenshots": keep_screenshots, "waveforms": keep_waveforms}
        self._lock = threading.Lock()

    def dir(self, kind: str) -> Path:
        path = self.root / kind
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(f"capture folder {path} is not writable: {exc}") from exc
        return path

    def _save(self, kind: str, data: bytes, channel: int | None = None) -> Capture:
        prefix, ext = KINDS[kind]
        suffix = f"_ch{channel}" if channel is not None else ""
        with self._lock:
            folder = self.dir(kind)
            cap_id = _new_id()
            while (folder / f"{prefix}_{cap_id}{suffix}{ext}").exists():
                cap_id = _new_id()  # same millisecond; try again
            path = folder / f"{prefix}_{cap_id}{suffix}{ext}"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            self._prune(kind)
        return Capture(kind, path.name, path, cap_id, channel)

    def save_screenshot(self, png: bytes) -> Capture:
        return self._save("screenshots", png)

    def save_waveform(self, channel: int, csv_text: str) -> Capture:
        return self._save("waveforms", csv_text.encode(), channel)

    def list(self, kind: str, limit: int | None = None) -> list[Capture]:
        """Captures of one kind, newest first."""
        folder = self.root / kind
        if not folder.is_dir():
            return []
        caps = []
        for path in folder.iterdir():
            m = NAME_RE.match(path.name)
            if m and KINDS[kind][0] == m["prefix"]:
                ch = int(m["ch"]) if m["ch"] else None
                caps.append(Capture(kind, path.name, path, m["id"], ch))
        caps.sort(key=lambda c: c.id, reverse=True)
        return caps[:limit] if limit else caps

    def get(self, kind: str, ref: str = "latest") -> Capture:
        """Look up by 'latest', by id (20261124T050014.123Z) or by file name."""
        caps = self.list(kind)
        if not caps:
            raise FileNotFoundError(f"no {kind} saved yet")
        if ref in ("", "latest"):
            return caps[0]
        for cap in caps:
            if ref in (cap.id, cap.name):
                return cap
        raise FileNotFoundError(f"no {kind[:-1]} with id {ref!r}")

    def _prune(self, kind: str) -> None:
        keep = self.keep[kind]
        if keep <= 0:
            return
        for cap in self.list(kind)[keep:]:
            cap.path.unlink(missing_ok=True)
