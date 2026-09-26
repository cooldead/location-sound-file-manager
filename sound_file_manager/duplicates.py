"""Pure duplicate finding: duplicate recordings and duplicate projects, and
the plan for merging projects. No Qt.

Finding is cheap and never reads whole files over the network:

1. Recordings are grouped by what the recorder captured: sample rate,
   channels, bit depth, length in frames and start timecode (the name is used
   instead of the timecode when a file has none). Different takes almost never
   share all of these.
2. Candidates in a group are fingerprinted from their fmt chunk and three
   256 KB blocks of audio (start, middle, end).

Removing is careful: right before a file is removed it is compared byte for
byte with the copy that is kept (see files_identical / audio_identical).
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import bwf
from .catalog import REMOVED_FOLDER, Recording, day_of, project_folder_of

SAMPLE_BLOCK = 64 << 10
# Added to the name of a file that is kept for review instead of removed
# because it is not byte for byte the same as the copy that is kept.
REVIEW_TAG = "_ReviewForDeletion"
COMPARE_BLOCK = 8 << 20
# Metadata compared between copies of the same audio.
META_FIELDS = ("name", "project", "scene", "take", "tape", "note", "circled")


# ---------------------------------------------------------------- fingerprints

def audio_key(rec: Recording) -> tuple | None:
    """What the recorder captured; None for files that could not be read."""
    if rec.error or not rec.frames:
        return None
    anchor = rec.time_reference if rec.time_reference is not None else ("name", rec.name.casefold())
    return (rec.sample_rate, rec.channels, rec.bits, rec.frames, anchor)


def sample_hash(path: str) -> str:
    """Fingerprint of the audio: the fmt chunk, the data size and three blocks
    of the data (start, middle, end). Metadata is not included, so copies
    whose notes or names differ still match.

    Unbuffered reads with read-ahead turned off: on a network share every
    buffered read pulls in megabytes (measured 99 ms per file, now ~50 ms).
    """
    digest = hashlib.md5()
    fd = os.open(path, os.O_RDONLY)
    try:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_RANDOM)
        elif hasattr(fcntl, "F_RDAHEAD"):  # macOS has no fadvise; this turns read-ahead off
            fcntl.fcntl(fd, fcntl.F_RDAHEAD, 0)
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as f:
            layout = bwf.read_layout(f, os.fstat(fd).st_size)
        fmt, data = layout.first(b"fmt "), layout.first(b"data")
        digest.update(os.pread(fd, fmt.size, fmt.data_offset))
        if data is None:
            return digest.hexdigest()
        digest.update(str(data.size).encode())
        for start in sorted({0, max(data.size // 2 - SAMPLE_BLOCK // 2, 0), max(data.size - SAMPLE_BLOCK, 0)}):
            digest.update(os.pread(fd, min(SAMPLE_BLOCK, data.size - start), data.data_offset + start))
    finally:
        os.close(fd)
    return digest.hexdigest()


def _same_stream(a, b, length: int, cancelled: Callable[[], bool], progress: Callable[[int], None] | None) -> bool:
    done = 0
    while done < length:
        if cancelled():
            raise InterruptedError()
        n = min(COMPARE_BLOCK, length - done)
        if a.read(n) != b.read(n):
            return False
        done += n
        if progress:
            progress(n)
    return True


def files_identical(a: str, b: str, *, cancelled: Callable[[], bool] = lambda: False,
                    progress: Callable[[int], None] | None = None) -> bool:
    """Every byte the same."""
    size = os.path.getsize(a)
    if size != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return _same_stream(fa, fb, size, cancelled, progress)


def audio_identical(a: str, b: str, *, cancelled: Callable[[], bool] = lambda: False,
                    progress: Callable[[int], None] | None = None) -> bool:
    """The fmt chunk and every byte of audio the same (metadata may differ)."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        la = bwf.read_layout(fa, os.fstat(fa.fileno()).st_size)
        lb = bwf.read_layout(fb, os.fstat(fb.fileno()).st_size)
        fmt_a, fmt_b = la.first(b"fmt "), lb.first(b"fmt ")
        data_a, data_b = la.first(b"data"), lb.first(b"data")
        if data_a is None or data_b is None or data_a.size != data_b.size or data_a.truncated or data_b.truncated:
            return False
        fa.seek(fmt_a.data_offset)
        fb.seek(fmt_b.data_offset)
        if fa.read(fmt_a.size) != fb.read(fmt_b.size):
            return False
        fa.seek(data_a.data_offset)
        fb.seek(data_b.data_offset)
        return _same_stream(fa, fb, data_a.size, cancelled, progress)


def more_tracks_candidate(a: Recording, b: Recording) -> bool:
    """Same name, format, length and start, but a different number of tracks:
    the smaller file may be a reduced copy of the larger one (e.g. a Zoom file
    without its mix tracks). Checked with channels_contained before acting."""
    return (a.name.casefold() == b.name.casefold() and a.channels != b.channels and a.frames == b.frames
            and a.sample_rate == b.sample_rate and a.bits == b.bits and a.time_reference is not None
            and a.time_reference == b.time_reference and not a.error and not b.error)


def channels_contained(small: str, big: str, *, cancelled: Callable[[], bool] = lambda: False,
                       progress: Callable[[int], None] | None = None) -> bool:
    """Every channel of the small file is, sample for sample, one of the big
    file's channels, over the whole recording."""
    import numpy as np
    with open(small, "rb") as fs, open(big, "rb") as fb:
        ls = bwf.read_layout(fs, os.fstat(fs.fileno()).st_size)
        lb = bwf.read_layout(fb, os.fstat(fb.fileno()).st_size)
        si, bi = bwf._info_from(fs, ls), bwf._info_from(fb, lb)
        ds, db = ls.first(b"data"), lb.first(b"data")
        if (ds is None or db is None or ds.truncated or db.truncated or si.bits != bi.bits
                or si.sample_rate != bi.sample_rate or si.frames != bi.frames or si.channels >= bi.channels
                or si.format_tag != bi.format_tag):
            return False
        width = si.bits // 8
        frames_per_block = max(COMPARE_BLOCK // bi.block_align, 1)
        fs.seek(ds.data_offset)
        fb.seek(db.data_offset)
        candidates = [set(range(bi.channels)) for _ in range(si.channels)]
        left = si.frames
        while left > 0:
            if cancelled():
                raise InterruptedError()
            n = min(frames_per_block, left)
            a = np.frombuffer(fs.read(n * si.block_align), np.uint8)
            b = np.frombuffer(fb.read(n * bi.block_align), np.uint8)
            if len(a) != n * si.block_align or len(b) != n * bi.block_align:
                return False
            a = a.reshape(n, si.channels, width)
            b = b.reshape(n, bi.channels, width)
            for i, options in enumerate(candidates):
                for j in list(options):
                    if not np.array_equal(a[:, i], b[:, j]):
                        options.discard(j)
                if not options:
                    return False
            left -= n
            if progress:
                progress(n * (si.block_align + bi.block_align))
    return True


# Fingerprints read at once. Over Wi-Fi to the NAS: 86 ms per file one at a
# time, 57 ms with 4, no better with 16 or 32 (the share serves a few opens at
# once). Wired on Linux it measured about the same either way.
FINGERPRINT_WORKERS = 4


class Fingerprints:
    """sample_hash() with a cache (anything with get_hash/put_hash)."""

    def __init__(self, cache=None, workers: int = FINGERPRINT_WORKERS):
        self.cache = cache
        self.workers = workers
        self.memo: dict[str, str] = {}

    def prefetch(self, recs: list[Recording], progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] = lambda: False) -> None:
        """Fingerprint many files at once: reads over the network are
        latency-bound, so a few in parallel is much faster. The cache is only
        touched from the calling thread."""
        todo = []
        for rec in recs:
            if rec.path in self.memo:
                continue
            value = self.cache.get_hash(rec.path, rec.size, rec.mtime) if self.cache is not None else None
            if value is None:
                todo.append(rec)
            else:
                self.memo[rec.path] = value
        done = len(recs) - len(todo)
        if progress:
            progress(done, len(recs))
        if not todo:
            return
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(self.workers) as pool:
            futures = {pool.submit(sample_hash, rec.path): rec for rec in todo}
            for future in as_completed(futures):
                rec = futures[future]
                if cancelled():
                    for other in futures:
                        other.cancel()
                    break
                try:
                    value = future.result()
                except (OSError, bwf.WavError):
                    value = None
                if value is not None:
                    self.memo[rec.path] = value
                    if self.cache is not None:
                        self.cache.put_hash(rec.path, rec.size, rec.mtime, value)
                done += 1
                if progress:
                    progress(done, len(recs))
        if self.cache is not None and hasattr(self.cache, "db"):
            self.cache.db.commit()

    def __call__(self, rec: Recording) -> str:
        if rec.path in self.memo:
            return self.memo[rec.path]
        value = self.cache.get_hash(rec.path, rec.size, rec.mtime) if self.cache is not None else None
        if value is None:
            value = sample_hash(rec.path)
            if self.cache is not None:
                self.cache.put_hash(rec.path, rec.size, rec.mtime, value)
        self.memo[rec.path] = value
        return value


# ---------------------------------------------------------------- duplicate files

@dataclass
class FileGroup:
    recs: list[Recording]
    keeper: Recording
    differences: dict[str, list[str]] = field(default_factory=dict)  # field -> the differing values

    @property
    def replacement(self) -> Recording | None:
        """A copy with notes the kept one lacks: it takes the kept copy's place
        (versions with notes are preferred)."""
        for rec in self.recs:
            if rec is not self.keeper and adds_notes(rec, self.keeper):
                return rec
        return None

    @property
    def identical(self) -> bool:
        """All copies probably byte-identical (same size, same metadata)."""
        return not self.differences and len({r.size for r in self.recs}) == 1

    def differs(self, rec: Recording) -> dict[str, list[str]]:
        """How a copy differs from the kept one (empty: probably identical)."""
        return metadata_differences([self.keeper, rec])

    def copy_identical(self, rec: Recording) -> bool:
        """The same recording with the same name and metadata as the kept file:
        a duplicate to remove (checked fully before removal, see check_level).
        An _ISO/_LR file with the same name counts as the same file whatever
        its notes say (its audio is checked before removal)."""
        if is_take_file(rec.name) and rec.name.casefold() == self.keeper.name.casefold():
            return True
        base = self.replacement or self.keeper
        return rec is not base and not metadata_differences([base, rec])

    def check_level(self, rec: Recording) -> str:
        """How a removal of this copy is checked: "identical" (every byte of the
        file) when the sizes match, else "audio" (every byte of the audio; the
        recorder's second card can lay the file out slightly differently)."""
        base = self.replacement or self.keeper
        return "identical" if rec.size == base.size and not metadata_differences([base, rec]) else "audio"

    @property
    def extras(self) -> list[Recording]:
        return [r for r in self.recs if r is not self.keeper]

    @property
    def wasted(self) -> int:
        return sum(r.size for r in self.extras)


def keeper_score(rec: Recording, root: str, containers: Iterable[str]) -> tuple:
    """Lower is better: a file in its project's own folder beats one in a
    card dump (SD_1, 833 BACK UPS, ...), which beats one already removed;
    between otherwise equal copies, one with a note (or circled) wins so the
    note is not lost."""
    skip = {c.casefold() for c in containers}
    try:
        parts = Path(rec.path).relative_to(root).parts[:-1]
    except ValueError:
        parts = Path(rec.path).parts[:-1]
    score = 0
    if REMOVED_FOLDER in parts:
        score += 1000
    if any(p.casefold() in skip for p in parts):
        score += 100
    if rec.project and any(p.casefold() == rec.project.casefold() for p in parts):
        score -= 10
    if rec.note.strip() or rec.circled:
        score -= 1
    return (score, len(parts), rec.mtime, rec.path)


def find_duplicate_files(recs: list[Recording], fingerprint: Callable[[Recording], str], root: str,
                         containers: Iterable[str], *, progress: Callable[[int, int], None] | None = None,
                         cancelled: Callable[[], bool] = lambda: False) -> list[FileGroup]:
    containers = list(containers)
    by_key: dict[tuple, list[Recording]] = defaultdict(list)
    for rec in recs:
        key = audio_key(rec)
        if key is not None:
            by_key[key].append(rec)
    candidates = [group for group in by_key.values() if len(group) > 1]
    # Copies with the same name, length, format and start timecode (to the
    # sample) are taken as the same recording without reading them; only
    # different names in a group (an _ISO and an _LR, or a renamed copy) need a
    # fingerprint, one per name. Removal re-checks every file byte for byte.
    subgroups: list[list[list[Recording]]] = []
    representatives = []
    for group in candidates:
        by_name: dict[str, list[Recording]] = defaultdict(list)
        for rec in group:
            by_name[rec.name.casefold()].append(rec)
        names = list(by_name.values())
        subgroups.append(names)
        if len(names) > 1:
            representatives += [same[0] for same in names]
    if hasattr(fingerprint, "prefetch"):
        fingerprint.prefetch(representatives, progress, cancelled)
    total = len(representatives)
    done = 0
    groups = []
    for names in subgroups:
        if cancelled():
            return groups
        if len(names) == 1:
            merged = [names[0]]
        else:
            by_hash: dict[str, list[Recording]] = defaultdict(list)
            for same in names:
                try:
                    by_hash[fingerprint(same[0])].extend(same)
                except (OSError, bwf.WavError):
                    pass
                done += 1
                if progress and not hasattr(fingerprint, "prefetch"):
                    progress(done, total)
            merged = list(by_hash.values())
        for same in merged:
            if len(same) < 2:
                continue
            same = sorted(same, key=lambda r: keeper_score(r, root, containers))
            groups.append(FileGroup(same, same[0], metadata_differences(same)))
    align_take_keepers(groups, root, containers)
    groups.sort(key=lambda g: (g.keeper.project.casefold(), g.keeper.path))
    return groups


_TAKE_SUFFIX_RE = re.compile(r"_(ISO|LR|MIX|X1|X2)$", re.IGNORECASE)
_ISO_LR_RE = re.compile(r"_(ISO|LR)$", re.IGNORECASE)


def has_notes(rec: Recording) -> bool:
    return bool(rec.note.strip()) or rec.circled


def adds_notes(better: Recording, base: Recording) -> bool:
    """better is the same file as base (name and metadata) except that it has a
    note or a circle that base lacks: the version to prefer."""
    if not has_notes(better) or has_notes(base) or better.name.casefold() != base.name.casefold():
        return False
    return set(metadata_differences([base, better])) <= {"note", "circled", "project"}


def is_take_file(name: str) -> bool:
    """An _ISO or _LR file of a take. These are always kept together in one
    folder and never renamed or given review copies; only an exact repeat of
    the same file (same name, same audio) is removed."""
    return bool(_ISO_LR_RE.search(Path(name).stem))


def take_key(rec: Recording) -> tuple:
    """The take a file belongs to: Sound Devices writes a take as several files
    (_ISO, _LR) that share a file-set id; else the same start, length and
    name without the suffix."""
    if rec.family:
        return ("family", rec.family)
    stem = _TAKE_SUFFIX_RE.sub("", Path(rec.name).stem).casefold()
    return ("take", rec.time_reference, rec.frames, stem)


def align_take_keepers(groups: list[FileGroup], root: str, containers: Iterable[str]) -> None:
    """Keep the files of one take (_ISO and _LR) together: choose, per take, the
    folder that has a copy of every duplicated file of the take (the best one
    by keeper_score), and keep the copies in that folder."""
    containers = list(containers)
    by_take: dict[tuple, list[FileGroup]] = defaultdict(list)
    for group in groups:
        by_take[take_key(group.keeper)].append(group)
    for takes in by_take.values():
        if len(takes) < 2:
            continue
        folders = None
        for group in takes:
            here = {r.folder for r in group.recs}
            folders = here if folders is None else folders & here
        if not folders:
            continue  # no folder has the whole take; keep each group's own choice
        best = min(folders, key=lambda f: min(keeper_score(r, root, containers)
                                              for g in takes for r in g.recs if r.folder == f))
        for group in takes:
            group.keeper = next(r for r in group.recs if r.folder == best)


def metadata_differences(recs: list[Recording]) -> dict[str, list[str]]:
    result = {}
    for name in META_FIELDS:
        values = []
        for rec in recs:
            value = getattr(rec, name)
            value = ("★" if value else "no") if name == "circled" else str(value)
            if value not in values:
                values.append(value)
        if len(values) > 1:
            result[name] = values
    return result


# ---------------------------------------------------------------- duplicate projects

# Folders that hold leftovers rather than a project; project merges leave them alone.
NOT_PROJECT_FOLDERS = {"falsetakes", "trash", "recovered", REMOVED_FOLDER.casefold()}


def normalize(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def _digits(name: str) -> list[str]:
    return re.findall(r"\d+", name)


def similar_names(a: str, b: str) -> bool:
    """A typo (SUNRISE / SUNRYSE) or an added word (ORCHARD / ORCHARD INC).
    Names that differ in their numbers are different days or parts
    (CHAPTER 2 / CHAPTER 4), not the same project."""
    na, nb = normalize(a), normalize(b)
    if _digits(a) != _digits(b) or min(len(na), len(nb)) < 3:
        return False
    short, long_ = sorted((na, nb), key=len)
    prefix = long_.startswith(short) and not re.search(r"\d", long_[len(short):])
    ratio = difflib.SequenceMatcher(None, na, nb).ratio() if min(len(na), len(nb)) >= 5 else 0
    return prefix or ratio >= 0.85


def _relative_parts(path: str, root: str) -> list[str] | None:
    """Path parts below root (plain string work: this runs for every file)."""
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix):].split("/")


def _in_leftovers(path: str, root: str) -> bool:
    parts = _relative_parts(path, root)
    return bool(parts) and any(p.casefold() in NOT_PROJECT_FOLDERS for p in parts[:-1])


class _FolderIndex:
    """Files by every folder they are in, for "the files below these folders"."""

    def __init__(self, recs: Iterable[Recording]):
        self.below: dict[str, list[Recording]] = defaultdict(list)
        for rec in recs:
            folder = os.path.dirname(rec.path)
            while folder and folder != "/":
                self.below[folder].append(rec)
                parent = os.path.dirname(folder)
                if parent == folder:
                    break
                folder = parent

    def files(self, folders: Iterable[str]) -> list[Recording]:
        seen, result = set(), []
        for folder in folders:
            for rec in self.below.get(folder.rstrip("/"), []):
                if id(rec) not in seen:
                    seen.add(id(rec))
                    result.append(rec)
        return result


@dataclass
class ProjectGroup:
    kind: str  # "spelling", "similar" or "folders"
    names: list[str]  # project names (kind "folders": the shared folder name)
    locations: list[str]  # the folders the group's files are in
    files: list[Recording] = field(default_factory=list)

    @property
    def label(self) -> str:
        return {"spelling": "Same name, written differently", "similar": "Similar names",
                "folders": "Same folder in several places"}[self.kind]

    @property
    def retag(self) -> bool:
        """Merging sets the kept project name in the files (not for plain folder copies)."""
        return self.kind != "folders"

    def files_in(self, location: str) -> list[Recording]:
        prefix = location.rstrip("/") + "/"
        return [r for r in self.files if r.path.startswith(prefix)]


def project_locations(recs: Iterable[Recording], root: str, containers: Iterable[str]) -> dict[str, list[str]]:
    """Project name -> the folders its files are in: one per top-level place
    (the project's own folder, a card dump such as SD_1/<project>, ...).
    Leftover folders (FALSETAKES, Recovered, TRASH) are not counted."""
    containers = list(containers)
    skip = {c.casefold() for c in containers}
    by_place: dict[tuple[str, str], list[Recording]] = defaultdict(list)
    for rec in recs:
        if not rec.project or rec.error:
            continue
        parts = _relative_parts(rec.path, root)
        if parts is None or any(p.casefold() in NOT_PROJECT_FOLDERS for p in parts[:-1]):
            continue
        top = parts[0] if len(parts) > 1 else ""
        if top.casefold() in skip and len(parts) > 2:
            top = os.path.join(parts[0], parts[1])
        by_place[(rec.project, top)].append(rec)
    result: dict[str, list[str]] = defaultdict(list)
    for (project, _), group in by_place.items():
        folder = project_folder_of(group, root, containers)
        if folder and os.path.normpath(folder) != os.path.normpath(root) and folder not in result[project]:
            result[project].append(folder)
    return dict(result)


def find_duplicate_projects(recs: list[Recording], root: str, containers: Iterable[str]) -> list[ProjectGroup]:
    containers = list(containers)
    locations = project_locations(recs, root, containers)
    names = sorted(locations, key=str.casefold)
    groups: list[ProjectGroup] = []
    used: set[str] = set()
    usable = [r for r in recs if not r.error and not _in_leftovers(r.path, root)]
    index = _FolderIndex(usable)

    def name_group(kind, members):
        folders = sorted({f for m in members for f in locations[m]})
        wanted = set(members)
        files = [r for r in index.files(folders) if r.project in wanted]
        return ProjectGroup(kind, members, folders, files)

    # 1. Same name when spaces, punctuation and case are ignored.
    by_norm: dict[str, list[str]] = defaultdict(list)
    for name in names:
        by_norm[normalize(name)].append(name)
    for members in by_norm.values():
        if len(members) > 1 and normalize(members[0]):
            groups.append(name_group("spelling", members))
            used.update(members)

    # 2. Similar names: a typo (SUNRISE / SUNRYSE) or an added word (ORCHARD / ORCHARD
    # INC). Names that differ in their numbers are different days or parts
    # (CHAPTER 2 / CHAPTER 4), not duplicates.
    free = [n for n in names if n not in used and len(normalize(n)) >= 3]
    parent = {n: n for n in free}

    def find(n):
        while parent[n] != n:
            n = parent[n]
        return n

    for i, a in enumerate(free):
        for b in free[i + 1:]:
            if similar_names(a, b):
                parent[find(b)] = find(a)
    clusters: dict[str, list[str]] = defaultdict(list)
    for n in free:
        clusters[find(n)].append(n)
    for members in clusters.values():
        if len(members) > 1:
            groups.append(name_group("similar", sorted(members, key=str.casefold)))

    # 3. Folders with the same name in different places: usually a card copied
    # into the library more than once (<project>, SD_1/<project>, ...).
    by_folder_name: dict[str, set[str]] = defaultdict(set)
    for folders in locations.values():
        for folder in folders:
            by_folder_name[normalize(os.path.basename(folder))].add(folder)
    for key, folders in sorted(by_folder_name.items()):
        folders = sorted(folders, key=lambda f: (f.count("/"), f))
        if len(folders) < 2 or not key:
            continue
        groups.append(ProjectGroup("folders", [os.path.basename(folders[0])], folders, index.files(folders)))
    return groups


# ---------------------------------------------------------------- nested folders

@dataclass
class NestedFolder:
    """A folder inside a folder with the same name, e.g. Project/250101/250101:
    usually a day folder copied into itself. Merging moves the inner files up."""
    outer: str
    inner: str
    outer_files: list[Recording]  # below outer, but not below inner
    inner_files: list[Recording]

    def destination(self, rec: Recording) -> str:
        """Where an inner file goes: the same place, one level up."""
        return self.outer + rec.path[len(self.inner):]

    def counts(self) -> tuple[int, int, int]:
        """(already in the outer folder, new, a different file in the way)."""
        by_path = {r.path: r for r in self.outer_files}
        same = new = clash = 0
        for rec in self.inner_files:
            other = by_path.get(self.destination(rec))
            if other is None:
                new += 1
            elif audio_key(other) is not None and audio_key(other) == audio_key(rec):
                same += 1
            else:
                clash += 1
        return same, new, clash


def find_nested_folders(recs: list[Recording], root: str) -> list[NestedFolder]:
    """Folders whose parent has the same name (ignoring case, spaces and
    punctuation), below a folder of the library. When they are nested more
    than twice (A/A/A), only the deepest pair is listed: merge it first."""
    usable = [r for r in recs if not r.error and not _in_leftovers(r.path, root)]
    index = _FolderIndex(usable)
    prefix = root.rstrip("/") + "/"
    pairs = {}
    for folder in index.below:
        if not folder.startswith(prefix):
            continue
        parent = os.path.dirname(folder)
        if not parent.startswith(prefix):
            continue  # the parent is the library itself
        name = normalize(os.path.basename(folder))
        if name and name == normalize(os.path.basename(parent)):
            pairs[folder] = parent
    result = []
    for inner, outer in sorted(pairs.items(), key=lambda kv: kv[0].casefold()):
        if any(o == inner for o in pairs.values()):
            continue  # a deeper pair inside this one goes first
        inner_prefix = inner + "/"
        outer_files = [r for r in index.files([outer]) if not r.path.startswith(inner_prefix)]
        result.append(NestedFolder(outer, inner, outer_files, index.files([inner])))
    return result


def plan_nested_merge(nested: NestedFolder, recs: list[Recording]) -> list[Action]:
    """Move the inner folder's files up into the outer one: a file already
    there (the same recording at the same place) is removed after a byte check,
    a copy with notes the outer one lacks takes its place, a different file in
    the way stays where it is. Nothing is overwritten."""
    by_path = {r.path: r for r in recs}
    taken = set(by_path)
    actions = []
    for rec in sorted(nested.inner_files, key=lambda r: r.path):
        dst = nested.destination(rec)
        other = by_path.get(dst)
        if other is None:
            if dst in taken:
                actions.append(Action("skip", rec, dst, "another file is already going there"))
                continue
            taken.add(dst)
            actions.append(Action("move", rec, dst, "one level up, out of the nested folder"))
            continue
        if audio_key(other) is None or audio_key(other) != audio_key(rec):
            actions.append(Action("skip", rec, dst, "a different file with this name is already there"))
            continue
        differs = metadata_differences([other, rec])
        if adds_notes(rec, other):
            actions.append(Action("replace", rec, dst, "has notes the outer copy lacks: it takes its place",
                                  "audio", other=other))
        elif differs and not is_take_file(rec.name):
            actions.append(Action("differs", rec, dst, f"the same recording is in the outer folder, but its "
                                  f"{', '.join(differs)} differs"))
        else:
            level = "identical" if other.size == rec.size and not differs else "audio"
            actions.append(Action("remove", rec, dst, "the same file is already in the outer folder", level))
    return keep_takes_together(actions)


# ---------------------------------------------------------------- card offload

def recording_key(rec: Recording) -> tuple | None:
    """"The same recording" without reading the file: the same name and the
    same audio key (as in Fingerprints, where such copies are not read)."""
    key = audio_key(rec)
    return None if key is None else (rec.name.casefold(), key)


@dataclass
class CardProjectMatch:
    """A project folder on a card that the library already has (by name)."""
    card_folder: str
    library_project: str
    library_folder: str  # where most of its files are
    kind: str  # "same" (same name), "spelling" or "similar"
    library_files: int  # files of the project in that folder
    card_recordings: int  # readable recordings in the card folder
    in_library: int  # of those, already somewhere in the library

    @property
    def new_recordings(self) -> int:
        return self.card_recordings - self.in_library


def card_project_matches(card_recs: list[Recording], card_folder_of: Callable[[Recording], str],
                         library_recs: list[Recording], root: str,
                         containers: Iterable[str]) -> dict[str, list[CardProjectMatch]]:
    """Card folder -> the library projects it may be (best first). A card
    folder is compared by its own name and by the project names in its
    files' metadata, like Find Duplicates compares projects."""
    containers = list(containers)
    locations = project_locations(library_recs, root, containers)
    in_library = {k for k in map(recording_key, library_recs) if k is not None}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for rec in library_recs:
        for folder in locations.get(rec.project, ()):
            if rec.path.startswith(folder.rstrip("/") + "/"):
                counts[(rec.project, folder)] += 1
    by_folder: dict[str, list[Recording]] = defaultdict(list)
    for rec in card_recs:
        by_folder[card_folder_of(rec)].append(rec)
    result: dict[str, list[CardProjectMatch]] = {}
    for folder, recs in by_folder.items():
        readable = [r for r in recs if recording_key(r) is not None]
        names = {folder} | {r.meta_project for r in readable if r.meta_project}
        found: dict[str, str] = {}
        for project in locations:
            kinds = ["same" if project == name else "spelling" if normalize(project) == normalize(name)
                     else "similar" if similar_names(project, name) else "" for name in names]
            kind = next((k for k in ("same", "spelling", "similar") if k in kinds), "")
            if kind:
                found[project] = kind
        matches = []
        for project, kind in found.items():
            best = max(locations[project], key=lambda f: counts[(project, f)])
            matches.append(CardProjectMatch(folder, project, best, kind, counts[(project, best)], len(readable),
                                            sum(1 for r in readable if recording_key(r) in in_library)))
        order = {"same": 0, "spelling": 1, "similar": 2}
        matches.sort(key=lambda m: (order[m.kind], -m.library_files, m.library_project.casefold()))
        if matches:
            result[folder] = matches
    return result


def recordings_in_library(card_recs: Iterable[Recording], library_recs: Iterable[Recording]) -> set[str]:
    """Paths of the card recordings that are already somewhere in the library."""
    known = {k for k in map(recording_key, library_recs) if k is not None}
    return {r.path for r in card_recs if recording_key(r) in known}


# ---------------------------------------------------------------- merge plan

@dataclass
class Action:
    kind: str  # "remove" (an identical copy is kept), "move", "retag" (project name only), "skip",
    # "replace" (this copy has notes the kept one lacks: it takes the kept copy's place, which is removed),
    # or "differs" (the same recording as the kept one, but its metadata differs: left alone, or kept for review)
    rec: Recording
    dst: str = ""  # move: the new path; remove: the copy that is kept
    reason: str = ""
    # remove / replace: what is compared first: "identical" (the whole file), "audio"
    # (the audio data), or "subset" (every channel of the smaller file is in the larger one)
    level: str = "identical"
    other: Recording | None = None  # replace: the copy that is replaced (dst is its path)


def relative_in_project(rec: Recording, folders: Iterable[str]) -> str:
    """The file's path below the group folder it is in (the deepest match)."""
    for folder in sorted(folders, key=len, reverse=True):
        prefix = folder.rstrip("/") + "/"
        if rec.path.startswith(prefix):
            return rec.path[len(prefix):]
    return os.path.basename(rec.path)


def plan_project_merge(recs: list[Recording], group: ProjectGroup, target_folder: str,
                       fingerprint: Callable[[Recording], str], target_name: str = "",
                       move_files: bool = True, root: str = "", containers: Iterable[str] = ()) -> list[Action]:
    """What merging the group into target_folder does to each file.

    Files already in the target folder stay. With move_files, every other file
    is removed if the same recording is already in the target folder (or is
    being moved there), else moved in at the same place below its folder. The
    best copy of a recording (by keeper_score, when root is given) is the one
    moved, so merging into a new, empty folder works too. With a target_name
    (name groups), files get that project name. Without move_files only the
    project name changes.
    """
    containers = list(containers)
    target_prefix = target_folder.rstrip("/") + "/"
    moving: dict[str, int] = {}  # path of a file planned to move -> index of its action
    actions_dst: dict[str, str] = {}  # its destination
    in_target = [r for r in group.files if r.path.startswith(target_prefix)]
    kept: dict[tuple, list[Recording]] = defaultdict(list)  # recordings that end up in the target folder
    for rec in in_target:
        kept[audio_key(rec)].append(rec)
    taken = {r.path for r in recs}
    by_path = {r.path: r for r in recs}
    renamed = bool(target_name) and group.retag
    others = [r for r in group.files if not r.path.startswith(target_prefix)]
    order = (lambda r: keeper_score(r, root, containers)) if root else (lambda r: r.path)
    if move_files and hasattr(fingerprint, "prefetch"):
        # Read the fingerprints the loop below will ask for, several at a time:
        # copies of a kept recording under another name (and those kept files).
        wanted = {}
        for rec in others:
            for other in kept.get(audio_key(rec), []):
                if other.name.casefold() != rec.name.casefold():
                    wanted[rec.path], wanted[other.path] = rec, other
        if wanted:
            fingerprint.prefetch(list(wanted.values()))
    actions = []
    for rec in sorted(in_target, key=lambda r: r.path):
        if renamed and rec.project != target_name:
            actions.append(Action("retag", rec, reason=f"project name {rec.project} → {target_name}"))
    for rec in sorted(others, key=order):
        name_change = f"project name {rec.project} → {target_name}" if renamed and rec.project != target_name else ""
        if not move_files:
            if name_change:
                actions.append(Action("retag", rec, reason=name_change))
            continue
        twin = None
        for other in kept.get(audio_key(rec), []):
            if other.name.casefold() == rec.name.casefold():
                twin = other  # same recording and name: no need to read (removal re-checks byte for byte)
                break
        for other in kept.get(audio_key(rec), []) if twin is None else []:
            try:
                if fingerprint(other) == fingerprint(rec):
                    twin = other
                    break
            except (OSError, bwf.WavError):
                continue
        if twin is not None:
            differs = metadata_differences([twin, rec])
            differs.pop("project", None)  # a merge sets the project name anyway
            if adds_notes(rec, twin):
                # Versions with notes are preferred: this copy takes the kept
                # copy's place (checked to be the same audio first).
                planned = moving.pop(twin.path, None)
                kept[audio_key(rec)] = [r for r in kept[audio_key(rec)] if r is not twin] + [rec]
                if planned is not None:
                    # The twin was going to be moved in: move this copy instead.
                    dst = actions_dst.pop(twin.path)
                    actions[planned] = Action("remove", twin, rec.path, "a copy with notes is kept instead",
                                              "audio")
                    actions.append(Action("move", rec, dst, name_change or "has notes the other copy lacks"))
                    moving[rec.path] = len(actions) - 1
                    actions_dst[rec.path] = dst
                else:
                    actions.append(Action("replace", rec, twin.path, "has notes the kept copy lacks: it takes "
                                          "its place", "audio", other=twin))
                continue
            if is_take_file(rec.name):
                if twin.name.casefold() == rec.name.casefold():
                    # The exact same _ISO/_LR file again: only one is kept,
                    # whatever other differences (notes, ...) there are.
                    level = "identical" if twin.size == rec.size and not differs else "audio"
                    actions.append(Action("remove", rec, twin.path, "the same file is already in the kept folder",
                                          level))
                    continue
                twin = None  # a differently named take file is kept (and moved in)
            elif differs:
                actions.append(Action("differs", rec, twin.path, f"same recording as the one kept, but its "
                                      f"{', '.join(differs)} differs"))
            else:
                # Checked against the kept copy where it is now (before anything moves).
                level = "identical" if twin.size == rec.size else "audio"
                actions.append(Action("remove", rec, twin.path, "the same recording is kept in the kept folder",
                                      level))
            if twin is not None:
                continue
        dst = os.path.join(target_folder, relative_in_project(rec, group.locations))
        if dst in taken:
            other = by_path.get(dst)
            planned = None
            if other is None:
                mover = next((p for p, d in actions_dst.items() if d == dst), None)
                if mover is not None:
                    planned = moving.get(mover)
                    other = by_path.get(mover)
            if other is not None and more_tracks_candidate(rec, other):
                if rec.channels > other.channels:
                    # The version with more tracks is preferred; the smaller file
                    # must be fully contained in it (checked before anything moves).
                    if planned is not None:
                        actions[planned] = Action("remove", other, rec.path, f"its tracks are all in the "
                                                  f"{rec.channels}-track version, which is kept", "subset")
                        moving.pop(other.path, None)
                        actions_dst.pop(other.path, None)
                        actions.append(Action("move", rec, dst, f"has more tracks ({rec.channels} instead of "
                                              f"{other.channels})"))
                        moving[rec.path] = len(actions) - 1
                        actions_dst[rec.path] = dst
                    else:
                        actions.append(Action("replace", rec, dst, f"has more tracks ({rec.channels} instead of "
                                              f"{other.channels}); the other file's tracks are all in it", "subset",
                                              other=other))
                else:
                    actions.append(Action("remove", rec, dst, f"its {rec.channels} tracks are all in the "
                                          f"{other.channels}-track file already there", "subset"))
                continue
            actions.append(Action("skip", rec, dst, "a different file with this name is already there"))
            continue
        taken.add(dst)
        kept[audio_key(rec)].append(rec)  # later copies of this recording are duplicates of it
        actions.append(Action("move", rec, dst, name_change))
        moving[rec.path] = len(actions) - 1
        actions_dst[rec.path] = dst
    return keep_takes_together(actions)


def keep_takes_together(actions: list[Action]) -> list[Action]:
    """Never split an _ISO/_LR take: if one file of a take stays where it is
    (skipped or different), the others of that take in the same folder stay
    too, instead of being moved or removed."""
    staying: set[tuple] = set()
    for action in actions:
        if action.kind in ("skip", "differs") and is_take_file(action.rec.name):
            staying.add((action.rec.folder, take_key(action.rec)))
    if not staying:
        return actions
    result = []
    for action in actions:
        key = (action.rec.folder, take_key(action.rec))
        if key in staying and action.kind in ("move", "remove", "replace") and is_take_file(action.rec.name):
            action = Action("skip", action.rec, action.dst, "kept together with its _ISO/_LR partner, which "
                            "has to stay where it is")
        result.append(action)
    return result


def suggest_keep_folder(group: ProjectGroup, root: str, containers: Iterable[str]) -> list[str]:
    """The group's folders, best first: outside the card dumps, most files, least deep."""
    skip = {c.casefold() for c in containers}

    def in_dump(folder):
        parts = _relative_parts(folder.rstrip("/") + "/x", root) or []
        return any(p.casefold() in skip for p in parts[:-1])
    return sorted(group.locations, key=lambda f: (in_dump(f), -len(group.files_in(f)), f.count("/"), f))


def default_project_name(group: ProjectGroup) -> str:
    """For name groups: the name most files use."""
    if not group.retag:
        return ""
    counts = {n: sum(1 for r in group.files if r.project == n) for n in group.names}
    folder_names = {os.path.basename(f.rstrip("/")).casefold() for f in group.locations}
    # Most files first; on a tie the name of a folder, then the more readable
    # spelling (with spaces), then alphabetical for a stable choice.
    return max(group.names, key=lambda n: (counts[n], n.casefold() in folder_names, n.count(" "), n))


def refresh_group(group: ProjectGroup, recs: list[Recording], root: str) -> ProjectGroup:
    """The group again, with its files taken from the library as it is now
    (earlier merges in a batch may have moved some of them)."""
    usable = [r for r in recs if not r.error and not _in_leftovers(r.path, root)]
    index = _FolderIndex(usable)
    locations = [f for f in group.locations if os.path.isdir(f)]
    files = index.files(locations)
    if group.retag:
        wanted = set(group.names)
        files = [r for r in files if r.project in wanted]
    return ProjectGroup(group.kind, list(group.names), locations, files)


def suggested_folder_name(group: ProjectGroup, target_name: str = "") -> str:
    """A name for a new keep folder: the kept project name, else the group's."""
    from .organize import safe_part
    return safe_part(target_name or group.names[0]) or "Merged Project"


def removal_path(path: str, root: str, project: str = "") -> str:
    """Where a removed duplicate goes:
    <root>/_Removed Duplicates/<its project>/<its path in the library>,
    so it is clear which project it belonged to and where it was."""
    from .organize import safe_part
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        relative = path.lstrip("/")
    if relative.startswith(".."):
        relative = path.lstrip("/")
    return os.path.join(root, REMOVED_FOLDER, safe_part(project) or "No Project", relative)


def is_review_copy(name: str) -> bool:
    """name_ReviewForDeletion.wav, or a numbered one: name_ReviewForDeletion (2).wav"""
    return bool(re.search(re.escape(REVIEW_TAG) + r"(\s*\(\d+\))?$", Path(name).stem))


def review_path(path: str, folder: str, taken: set[str] | None = None) -> str:
    """<folder>/<name>_ReviewForDeletion<ext>, numbered if that name is taken."""
    stem, ext = os.path.splitext(os.path.basename(path))
    taken = taken if taken is not None else set()
    candidate = os.path.join(folder, f"{stem}{REVIEW_TAG}{ext}")
    n = 2
    while candidate in taken or os.path.lexists(candidate):
        candidate = os.path.join(folder, f"{stem}{REVIEW_TAG} ({n}){ext}")
        n += 1
    taken.add(candidate)
    return candidate


def pair_review_copies(recs: list[Recording]) -> list[tuple[Recording, Recording | None, str]]:
    """Each file marked _ReviewForDeletion with its closest copy:
    (review file, copy or None, how it was matched: "same name" when the
    untagged file is in the same folder, "same recording" when a file with the
    same length/format/timecode was found elsewhere)."""
    untagged = [r for r in recs if not is_review_copy(r.name) and not r.error]
    by_path = {r.path: r for r in untagged}
    by_key: dict[tuple, list[Recording]] = defaultdict(list)
    for rec in untagged:
        by_key[audio_key(rec)].append(rec)
    pairs = []
    for rec in recs:
        if not is_review_copy(rec.name):
            continue
        stem, ext = os.path.splitext(rec.name)
        original = re.sub(r"\s*\(\d+\)$", "", stem.split(REVIEW_TAG)[0]) + ext
        match = by_path.get(os.path.join(rec.folder, original))
        how = "same name" if match else ""
        if match is None:
            candidates = by_key.get(audio_key(rec), [])
            candidates = sorted(candidates, key=lambda r: (r.name.casefold() != original.casefold(),
                                                           r.folder != rec.folder, r.path))
            if candidates:
                match, how = candidates[0], "same recording"
        pairs.append((rec, match, how))
    return sorted(pairs, key=lambda p: p[0].path)


COMPARE_FIELDS = [("name", "File name"), ("folder", "Folder"), ("size", "Size"), ("project", "Project"),
                  ("scene", "Scene"), ("take", "Take"), ("tape", "Tape"), ("note", "Note"), ("circled", "Circled"),
                  ("start_tc", "Start TC"), ("duration", "Length"), ("channels", "Channels"), ("tracks", "Tracks"),
                  ("format_label", "Format"), ("recorder", "Recorder"), ("date", "Date"), ("time", "Time")]


def compare_rows(a: Recording, b: Recording | None) -> list[tuple[str, str, str, bool]]:
    """(label, value of a, value of b, differs) for a side-by-side view."""
    def value(rec, key):
        if rec is None:
            return ""
        v = getattr(rec, key)
        if key == "size":
            return f"{v:,} bytes"
        if key == "circled":
            return "★ yes" if v else "no"
        if key == "tracks":
            return ", ".join(t for t in v if t)
        if key == "duration":
            return f"{v:.3f} s"
        return str(v)
    rows = []
    for key, label in COMPARE_FIELDS:
        va, vb = value(a, key), value(b, key)
        rows.append((label, va, vb, b is not None and va != vb))
    return rows


def removed_items(root: str) -> list[tuple[str, str, str, int]]:
    """Files in the removed-duplicates folder: (path, project folder, original
    path in the library, size)."""
    base = os.path.join(root, REMOVED_FOLDER)
    items = []
    for folder, dirs, files in os.walk(base):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            parts = Path(path).relative_to(base).parts
            project = parts[0] if len(parts) > 1 else ""
            original = os.path.join(root, *parts[1:]) if len(parts) > 1 else os.path.join(root, *parts)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            items.append((path, project, original, size))
    return items


def summarize(recs: list[Recording]) -> str:
    days = sorted({day_of(r) for r in recs if day_of(r)})
    span = f"{days[0]} – {days[-1]}" if len(days) > 1 else (days[0] if days else "")
    return f"{len(recs)} files" + (f", {span}" if span else "")
