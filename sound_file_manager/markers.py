"""Waveform markers: a named position (frame) in a recording.

Markers you add are kept in a small SQLite file in the app's data folder,
keyed by the file's path, and follow the file when the app renames or moves
it. The WAVs themselves are not changed. Cue markers the recorder wrote into
a file (bwf.read_cues) are shown too, read only.
"""

from __future__ import annotations

import bisect
import os
import sqlite3
from dataclasses import dataclass


@dataclass
class Marker:
    frame: int
    name: str = ""
    from_file: bool = False  # a cue point in the WAV (read only)


def next_marker(markers: list[Marker], frame: int, step: int, tolerance: int = 0) -> Marker | None:
    """The marker after (step 1) or before (step -1) a position."""
    frames = sorted(m.frame for m in markers)
    if not frames:
        return None
    if step > 0:
        i = bisect.bisect_right(frames, frame + tolerance)
        target = frames[i] if i < len(frames) else None
    else:
        i = bisect.bisect_left(frames, frame - tolerance)
        target = frames[i - 1] if i > 0 else None
    return next((m for m in markers if m.frame == target), None) if target is not None else None


def default_name(markers: list[Marker]) -> str:
    """"Marker 1", "Marker 2"… the first free number."""
    used = {m.name for m in markers}
    n = 1
    while f"Marker {n}" in used:
        n += 1
    return f"Marker {n}"


class MarkerStore:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS markers (path TEXT, frame INT, name TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS markers_path ON markers (path)")
        self.db.commit()

    def get(self, path: str) -> list[Marker]:
        rows = self.db.execute("SELECT frame, name FROM markers WHERE path=? ORDER BY frame", (path,))
        return [Marker(int(frame), name or "") for frame, name in rows]

    def put(self, path: str, markers: list[Marker]) -> None:
        """Replace a file's own markers (cue points from the file are not stored)."""
        with self.db:
            self.db.execute("DELETE FROM markers WHERE path=?", (path,))
            self.db.executemany("INSERT INTO markers VALUES (?, ?, ?)",
                                [(path, int(m.frame), m.name) for m in markers if not m.from_file])

    def move(self, mapping: dict[str, str]) -> None:
        """Files were renamed / moved (old path -> new path)."""
        with self.db:
            for old, new in mapping.items():
                if old != new:
                    self.db.execute("DELETE FROM markers WHERE path=?", (new,))
                    self.db.execute("UPDATE markers SET path=? WHERE path=?", (new, old))

    def close(self) -> None:
        self.db.close()
