"""Pure: an index kept inside the library, so another computer (or a new
cache) doesn't have to open every file on the share again. No Qt.

``<library>/.sfm-index/metadata.json.gz`` holds every recording's metadata,
keyed by its path below the library (so /Volumes/share and /mnt/share both
match) and checked by size + mtime like the local cache. It is read whole
at the start of a scan (a few MB) and written after a complete scan when
something changed: to a temp file next to it, then renamed, so a reader on
another computer sees the old or the new index, never half of one.

Optionally ``waveforms/<hash[:2]>/<hash>.lvl`` hold the waveform levels, one
small file per recording (each written the same way; no shared database on
the share, so two computers can't corrupt it).

Reading an index is always safe: an entry is only used when size and mtime
still match the file, otherwise the file is read as before.
"""

from __future__ import annotations

from . import card_safety

import gzip
import hashlib
import json
import os
import struct
import unicodedata
import uuid
from dataclasses import asdict
from typing import Iterable

from . import compat
from .catalog import CACHED_FIELDS, PARSER_VERSION, Recording

INDEX_FOLDER = ".sfm-index"  # hidden, so the scanner and folder tools skip it
METADATA_FILE = "metadata.json.gz"
WAVEFORM_FOLDER = "waveforms"
INDEX_VERSION = 1
# Two computers see the same SMB timestamp, but as floats that went through
# different clients; well below any real change.
MTIME_TOLERANCE = 0.01
_LEVELS_HEADER = struct.Struct("<4sQd")  # magic, size, mtime of the WAV
_LEVELS_MAGIC = b"SFMW"


def index_folder(root: str) -> str:
    return compat.join(root, INDEX_FOLDER)


def relative_key(root: str, path: str) -> str | None:
    """The path below root, NFC-normalised (macOS may hand out decomposed
    names for the same file), or None for a path outside root."""
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    return unicodedata.normalize("NFC", path[len(prefix):])


def _same(size: int, mtime: float, entry_size: int, entry_mtime: float) -> bool:
    return size == entry_size and abs(mtime - entry_mtime) <= MTIME_TOLERANCE


def _write_atomic(path: str, data: bytes) -> None:
    card_safety.assert_writable(path)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    temp = compat.join(folder, f".tmp-{uuid.uuid4().hex}")
    try:
        with open(temp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


class LibraryIndex:
    """The metadata part: {key: (size, mtime, fields)}."""

    def __init__(self, root: str):
        self.root = root.rstrip("/")
        self.entries: dict[str, tuple[int, float, dict]] = {}
        self._loaded: dict[str, tuple[int, float, dict]] = {}

    @property
    def path(self) -> str:
        return compat.join(index_folder(self.root), METADATA_FILE)

    @classmethod
    def load(cls, root: str) -> LibraryIndex:
        """The index of root; empty when there is none or it can't be used
        (unreadable, another version, or written by an older parser)."""
        index = cls(root)
        try:
            with open(index.path, "rb") as f:
                doc = json.loads(gzip.decompress(f.read()))
        except (OSError, ValueError, EOFError):
            return index
        if (not isinstance(doc, dict) or doc.get("version") != INDEX_VERSION
                or doc.get("parser", 0) < PARSER_VERSION):
            return index
        for key, entry in doc.get("files", {}).items():
            try:
                size, mtime, values = entry
                index.entries[key] = (int(size), float(mtime), dict(values))
            except (TypeError, ValueError):
                continue
        index._loaded = dict(index.entries)
        return index

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, path: str, size: int, mtime: float) -> Recording | None:
        key = relative_key(self.root, path)
        entry = self.entries.get(key) if key is not None else None
        if entry is None or not _same(size, mtime, entry[0], entry[1]):
            return None
        try:
            values = {k: entry[2][k] for k in CACHED_FIELDS if k in entry[2]}
            return Recording(**{**values, "path": path, "size": size, "mtime": mtime})
        except TypeError:
            return None

    def replace_all(self, recs: Iterable[Recording]) -> None:
        """Make the index what a complete scan saw."""
        entries = {}
        for rec in recs:
            key = relative_key(self.root, rec.path)
            if key is None:
                continue
            values = {k: v for k, v in asdict(rec).items() if k in CACHED_FIELDS and k not in ("path", "size", "mtime")}
            entries[key] = (rec.size, rec.mtime, values)
        self.entries = entries

    @property
    def changed(self) -> bool:
        if self.entries.keys() != self._loaded.keys():
            return True
        return any(value[2] != self._loaded[key][2] or not _same(value[0], value[1], *self._loaded[key][:2])
                   for key, value in self.entries.items())

    def save(self) -> None:
        doc = {"version": INDEX_VERSION, "parser": PARSER_VERSION,
               "files": {k: [size, mtime, values] for k, (size, mtime, values) in sorted(self.entries.items())}}
        _write_atomic(self.path, gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(),
                                               compresslevel=6))
        self._loaded = dict(self.entries)


# ---------------------------------------------------------------- waveforms

def waveform_path(root: str, path: str) -> str | None:
    key = relative_key(root, path)
    if key is None:
        return None
    digest = hashlib.sha1(key.encode()).hexdigest()
    return compat.join(index_folder(root), WAVEFORM_FOLDER, digest[:2], digest + ".lvl")


def has_waveforms(root: str) -> bool:
    return os.path.isdir(compat.join(index_folder(root), WAVEFORM_FOLDER))


def read_levels(root: str, path: str, size: int, mtime: float) -> bytes | None:
    """The waveform blob (waveform.to_bytes) stored for this file, if it still matches."""
    target = waveform_path(root, path)
    if target is None:
        return None
    try:
        with open(target, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) <= _LEVELS_HEADER.size:
        return None
    magic, entry_size, entry_mtime = _LEVELS_HEADER.unpack_from(data)
    if magic != _LEVELS_MAGIC or not _same(size, mtime, entry_size, entry_mtime):
        return None
    return data[_LEVELS_HEADER.size:]


def write_levels(root: str, path: str, size: int, mtime: float, blob: bytes) -> bool:
    target = waveform_path(root, path)
    if target is None:
        return False
    _write_atomic(target, _LEVELS_HEADER.pack(_LEVELS_MAGIC, size, mtime) + blob)
    return True


def waveform_bytes(channels: int, buckets: int) -> int:
    """Stored size of one file's waveform: peak + RMS per channel and bucket,
    plus headers, rounded up to the 4 KB a small file takes on most shares."""
    raw = _LEVELS_HEADER.size + 5 + 4 * channels * buckets
    return -(-raw // 4096) * 4096
