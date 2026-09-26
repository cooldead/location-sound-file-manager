"""Pure catalog logic: one Recording per WAV, project grouping, the scan cache.

No Qt here; the scanner runs in a worker thread and reports through callbacks.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, fields
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable

from . import bwf
from . import timecode as tc

AUDIO_EXTENSIONS = {".wav", ".bwf"}
# Where "remove duplicates" puts files (on the same share, so it is a rename
# and can be undone); never scanned.
REMOVED_FOLDER = "_Removed Duplicates"
CACHE_VERSION = 1
# Bumped when the parser learns to read more; entries it may have misread are
# dropped and read again on the next scan (not the whole cache).
PARSER_VERSION = 2

# Date-named day folders ("25Y10M27" from Sound Devices, "2024-05-17", "6-6-23")
# are never taken as a project name.
_DAY_FOLDER_RE = re.compile(r"^(\d{2}Y\d{2}M\d{2}|\d{4}-\d{2}-\d{2}|\d{1,2}[-.]\d{1,2}[-.]\d{2,4})$")
# "10T03_ISO", "Test 1T02_LR" (Sound Devices) and "56A-T005" (Zoom)
_FILENAME_TAKE_RE = re.compile(r"^(?P<scene>.+?)-?T(?P<take>\d{2,3})(?:_[A-Za-z0-9]+)?$")


@dataclass
class Recording:
    path: str
    size: int
    mtime: float
    form: str = ""
    sample_rate: int = 0
    bits: int = 0
    channels: int = 0
    frames: int = 0
    float_samples: bool = False
    meta_project: str = ""
    scene: str = ""
    take: str = ""
    tape: str = ""
    note: str = ""
    circled: bool = False
    ubits: str = ""
    time_reference: int | None = None
    tc_rate: str = ""  # "24000/1001"
    drop_frame: bool = False
    recorder: str = ""
    date: str = ""  # "2025-10-27"
    time: str = ""  # "17:55:45"
    tracks: list[str] = field(default_factory=list)
    family: str = ""
    original_filename: str = ""
    has_bext: bool = False
    has_ixml: bool = False
    truncated: bool = False
    error: str = ""
    # Filled in by assign_projects(), not cached.
    project: str = ""
    project_from: str = ""  # "metadata", "folder" or ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def rate(self) -> Fraction | None:
        return tc.parse_rate(self.tc_rate)

    def timecode_at(self, seconds: float = 0.0) -> str:
        """Timecode of a position in the file ("" if the file has no time reference)."""
        if self.time_reference is None or not self.sample_rate:
            return ""
        samples = self.time_reference + int(seconds * self.sample_rate)
        rate = self.rate
        if rate is None:
            return tc.seconds_to_clock(samples / self.sample_rate)
        return tc.samples_to_tc(samples, self.sample_rate, rate, self.drop_frame)

    @property
    def start_tc(self) -> str:
        return self.timecode_at(0.0)

    @property
    def end_tc(self) -> str:
        return self.timecode_at(self.duration)

    @property
    def format_label(self) -> str:
        if not self.sample_rate:
            return ""
        khz = f"{self.sample_rate / 1000:g} kHz"
        depth = f"{self.bits}-bit{' float' if self.float_samples else ''}"
        return f"{khz} · {depth} · {self.channels} ch"

    @property
    def rate_label(self) -> str:
        return tc.rate_label(self.rate, self.drop_frame)


CACHED_FIELDS = [f.name for f in fields(Recording) if f.name not in ("project", "project_from")]


def recording_from_info(path: str, size: int, mtime: float, info: bwf.WavInfo) -> Recording:
    speed = info.bext.get("SPEED", "")
    rate = info.ixml.get("TIMECODE_RATE") or speed
    flag = info.ixml.get("TIMECODE_FLAG")
    parsed_rate = tc.parse_rate(rate)
    rec = Recording(
        path=path, size=size, mtime=mtime, form=info.form,
        sample_rate=info.sample_rate, bits=info.bits, channels=info.channels, frames=info.frames,
        float_samples=info.format_tag == 3,
        meta_project=info.value("project"), scene=info.value("scene"), take=info.value("take"),
        tape=info.value("tape"), note=info.value("note"),
        circled=info.value("circled").upper() == "TRUE",
        ubits=(info.ixml.get("UBITS") or info.bext.get("UBITS", "")).lstrip("$"),
        time_reference=info.time_reference,
        tc_rate=f"{parsed_rate.numerator}/{parsed_rate.denominator}" if parsed_rate else "",
        drop_frame=tc.parse_drop_frame(flag) if flag else tc.parse_drop_frame(speed),
        recorder=info.originator, date=info.date, time=info.time,
        tracks=info.tracks, family=info.ixml.get("FAMILY_UID", ""),
        original_filename=info.ixml.get("ORIGINAL_FILENAME", ""),
        has_bext=info.has_bext, has_ixml=info.has_ixml, truncated=info.truncated,
    )
    if not rec.scene and not rec.take:
        # Recovered or metadata-less files: fall back to the recorder's naming.
        match = _FILENAME_TAKE_RE.match(Path(path).stem)
        if match:
            rec.scene, rec.take = match.group("scene").strip(), match.group("take")
    return rec


def read_recording(path: str, stat: os.stat_result | None = None) -> Recording:
    stat = stat or os.stat(path)
    try:
        info = bwf.read_info(path)
    except (OSError, bwf.WavError) as error:
        return Recording(path=path, size=stat.st_size, mtime=stat.st_mtime, error=str(error))
    return recording_from_info(path, stat.st_size, stat.st_mtime, info)


def folder_project(path: str, root: str, containers: Iterable[str]) -> str:
    """Project name from the folder structure: the first folder below root that
    is neither a card/backup container (e.g. "SD_1") nor a date-named day."""
    skip = {c.casefold() for c in containers}
    try:
        parts = Path(path).parent.relative_to(root).parts
    except ValueError:
        parts = Path(path).parent.parts[-1:]
    for part in parts:
        if part.casefold() in skip or _DAY_FOLDER_RE.match(part):
            continue
        return part
    return ""


def assign_projects(recordings: Iterable[Recording], root: str, containers: Iterable[str]) -> None:
    containers = list(containers)
    for rec in recordings:
        if rec.meta_project:
            rec.project, rec.project_from = rec.meta_project, "metadata"
        else:
            name = folder_project(rec.path, root, containers)
            rec.project, rec.project_from = (name, "folder") if name else ("", "")


def project_folder_of(recs: Iterable[Recording], root: str, containers: Iterable[str]) -> str:
    """The folder that holds a project: the folder named like the project if
    there is one, else the first folder below the library that is not a
    card/backup container; the most common one wins."""
    skip = {c.casefold() for c in containers}
    votes: dict[str, int] = {}
    recs = list(recs)
    for rec in recs:
        try:
            parts = Path(rec.path).relative_to(root).parts[:-1]
        except ValueError:
            continue
        chosen = None
        for i, part in enumerate(parts):
            if rec.project and part.casefold() == rec.project.casefold():
                chosen = parts[:i + 1]
                break
        if chosen is None:
            for i, part in enumerate(parts):
                if part.casefold() not in skip:
                    chosen = parts[:i + 1]
                    break
        if chosen:
            folder = os.path.join(root, *chosen)
            votes[folder] = votes.get(folder, 0) + 1
    if votes:
        return max(votes, key=lambda f: (votes[f], -len(f)))
    folders = [os.path.dirname(r.path) for r in recs]
    return os.path.commonpath(folders) if folders else ""


def day_of(rec: Recording) -> str:
    """Grouping below a project: recording date, else the tape name."""
    return rec.date or rec.tape or ""


def family_members(rec: Recording, recordings: Iterable[Recording]) -> list[Recording]:
    """Other files of the same polyphonic take (e.g. _ISO and _LR), same folder."""
    if not rec.family:
        return []
    return [r for r in recordings if r is not rec and r.family == rec.family and r.folder == rec.folder]


# ---------------------------------------------------------------- cache

class Cache:
    """SQLite cache of parsed recordings, keyed by path and checked by size+mtime."""

    def __init__(self, path: str | os.PathLike):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(os.fspath(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = self.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if row is None or int(row[0]) != CACHE_VERSION:
            self.db.execute("DROP TABLE IF EXISTS files")
            self.db.execute("DROP TABLE IF EXISTS peaks")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (str(CACHE_VERSION),))
        self.db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INT, mtime REAL, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS peaks (path TEXT PRIMARY KEY, size INT, mtime REAL, data BLOB)")
        self.db.execute("CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INT, mtime REAL, sample TEXT)")
        row = self.db.execute("SELECT value FROM meta WHERE key='parser'").fetchone()
        if row is None or int(row[0]) < PARSER_VERSION:
            # v2 reads iXML with a stale tail after a NUL (Sound Devices 833).
            self.db.execute("""DELETE FROM files WHERE json_extract(data, '$.has_ixml') = 0
                               AND coalesce(json_extract(data, '$.error'), '') = ''""")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('parser', ?)", (str(PARSER_VERSION),))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def get(self, path: str, size: int, mtime: float) -> Recording | None:
        row = self.db.execute("SELECT size, mtime, data FROM files WHERE path=?", (path,)).fetchone()
        if row is None or row[0] != size or abs(row[1] - mtime) > 1e-6:
            return None
        try:
            values = json.loads(row[2])
            return Recording(**{k: values[k] for k in CACHED_FIELDS if k in values})
        except (ValueError, TypeError):
            return None

    def put(self, rec: Recording, commit: bool = True) -> None:
        data = {k: v for k, v in asdict(rec).items() if k in CACHED_FIELDS}
        self.db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                        (rec.path, rec.size, rec.mtime, json.dumps(data)))
        if commit:
            self.db.commit()

    def all_under(self, root: str) -> list[Recording]:
        """Everything cached below root, to show at once while a rescan runs."""
        prefix = root.rstrip("/") + "/"
        recs = []
        for (data,) in self.db.execute("SELECT data FROM files WHERE substr(path, 1, ?) = ? ORDER BY path",
                                       (len(prefix), prefix)):
            try:
                values = json.loads(data)
                recs.append(Recording(**{k: values[k] for k in CACHED_FIELDS if k in values}))
            except (ValueError, TypeError):
                continue
        return recs

    def forget(self, paths: Iterable[str]) -> None:
        for path in paths:
            self.db.execute("DELETE FROM files WHERE path=?", (path,))
            self.db.execute("DELETE FROM peaks WHERE path=?", (path,))
            self.db.execute("DELETE FROM hashes WHERE path=?", (path,))
        self.db.commit()

    def prune(self, root: str, keep: set[str]) -> int:
        """Drop entries below root that the last scan did not see."""
        prefix = root.rstrip("/") + "/"
        stale = [p for (p,) in self.db.execute("SELECT path FROM files WHERE substr(path, 1, ?) = ?",
                                                (len(prefix), prefix)) if p not in keep]
        self.forget(stale)
        return len(stale)

    def get_hash(self, path: str, size: int, mtime: float) -> str | None:
        row = self.db.execute("SELECT size, mtime, sample FROM hashes WHERE path=?", (path,)).fetchone()
        if row is None or row[0] != size or abs(row[1] - mtime) > 1e-6:
            return None
        return row[2]

    def put_hash(self, path: str, size: int, mtime: float, sample: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?)", (path, size, mtime, sample))

    def get_peaks(self, path: str, size: int, mtime: float) -> bytes | None:
        row = self.db.execute("SELECT size, mtime, data FROM peaks WHERE path=?", (path,)).fetchone()
        if row is None or row[0] != size or abs(row[1] - mtime) > 1e-6:
            return None
        return row[2]

    def put_peaks(self, path: str, size: int, mtime: float, data: bytes) -> None:
        self.db.execute("INSERT OR REPLACE INTO peaks VALUES (?, ?, ?, ?)", (path, size, mtime, data))
        self.db.commit()

    def clear(self) -> None:
        self.db.execute("DELETE FROM files")
        self.db.execute("DELETE FROM peaks")
        self.db.execute("DELETE FROM hashes")
        self.db.commit()


# ---------------------------------------------------------------- scan

def is_audio_file(name: str) -> bool:
    # "._name.wav" are macOS AppleDouble resource forks, not audio.
    return not name.startswith("._") and os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS


def walk_audio(root: str, cancelled: Callable[[], bool] = lambda: False):
    """Yield (path, stat) for every audio file below root, skipping hidden folders."""
    stack = [root]
    while stack and not cancelled():
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                items = sorted(entries, key=lambda e: e.name.casefold())
        except OSError:
            continue
        subfolders = []
        for entry in items:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith(".") and entry.name != REMOVED_FOLDER:
                        subfolders.append(entry.path)
                elif entry.is_file() and is_audio_file(entry.name):
                    yield entry.path, entry.stat()
            except OSError:
                continue
        stack.extend(reversed(subfolders))


@dataclass
class ScanStats:
    found: int = 0
    parsed: int = 0
    cached: int = 0
    indexed: int = 0  # taken from the library's own index (library_index)
    errors: int = 0
    removed: int = 0
    seconds: float = 0.0
    index_saved: bool = False  # the library index was written (set by the scan thread)
    index_error: str = ""


def scan(root: str, cache: Cache, *, on_batch: Callable[[list[Recording]], None],
         on_progress: Callable[[ScanStats, str], None] | None = None,
         cancelled: Callable[[], bool] = lambda: False, batch_size: int = 250,
         index=None) -> ScanStats:
    """Walk root, using the cache where size and mtime match, then the
    library index (a library_index.LibraryIndex, optional). Recordings are
    delivered in batches so the UI can fill while a first (slow) scan runs.
    After a complete scan the index holds what was seen (it is not saved here)."""
    stats = ScanStats()
    everything: list[Recording] = []
    started = time.monotonic()
    seen: set[str] = set()
    batch: list[Recording] = []
    last_report = 0.0
    for path, stat in walk_audio(root, cancelled):
        seen.add(path)
        stats.found += 1
        rec = cache.get(path, stat.st_size, stat.st_mtime)
        if rec is None and index is not None:
            rec = index.get(path, stat.st_size, stat.st_mtime)
            if rec is not None:
                cache.put(rec, commit=False)
                stats.indexed += 1
        elif rec is not None:
            stats.cached += 1
        if rec is None:
            rec = read_recording(path, stat)
            cache.put(rec, commit=False)
            stats.parsed += 1
            if rec.error:
                stats.errors += 1
        batch.append(rec)
        if index is not None:
            everything.append(rec)
        if len(batch) >= batch_size:
            cache.db.commit()
            on_batch(batch)
            batch = []
        now = time.monotonic()
        if on_progress and now - last_report > 0.2:
            last_report = now
            on_progress(stats, path)
    cache.db.commit()
    if batch:
        on_batch(batch)
    if not cancelled():
        stats.removed = cache.prune(root, seen)
        if index is not None:
            index.replace_all(everything)
    stats.seconds = time.monotonic() - started
    return stats
