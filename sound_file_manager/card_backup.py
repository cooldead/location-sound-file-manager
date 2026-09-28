"""Conservative whole-card backup verification; metadata is only a shortlist."""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from . import card_safety, duplicates


@dataclass(frozen=True)
class Difference:
    src: str
    dst: str
    reason: str


@dataclass(frozen=True)
class BackupResult:
    total: int
    matched: int
    differences: list[Difference] = field(default_factory=list)

    @property
    def complete(self):
        return self.total > 0 and self.matched == self.total


def identity(path):
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def verify(plan, recordings, library_recordings, library, *, cancelled=lambda: False,
           progress=lambda done, total: None, cache=None):
    """Verify complete bytes, including metadata and accompanying files.

    Renamed recordings are shortlisted from the library catalog. Sidecars may
    follow a verified recording into another directory. No recursive NAS scan.
    Only successful comparisons are cached, with both file stat identities.
    """
    cache = cache if cache is not None else {}
    # Windows st_ctime can be creation time, so restored mtimes cannot safely
    # invalidate a previous match there. Re-read instead of trusting the cache.
    reuse_matches = sys.platform != "win32"
    by_key = defaultdict(list)
    library_prefix = os.path.normcase(os.path.abspath(library)).rstrip(os.sep) + os.sep
    for rec in library_recordings:
        key = duplicates.audio_key(rec)
        if key is not None and os.path.normcase(os.path.abspath(rec.path)).startswith(library_prefix):
            by_key[key].append(rec.path)
    recs = {r.path: r for r in recordings}
    moved = defaultdict(set)
    def match(item):
        if cancelled():
            return None
        candidates = [item.dst]
        rec = recs.get(item.src)
        if rec is not None:
            candidates.extend(by_key.get(duplicates.audio_key(rec), []))
        else:
            candidates.extend(os.path.join(folder, os.path.basename(item.src))
                              for folder in tuple(moved[os.path.dirname(item.src)]))
        found = failed = False
        for dst in dict.fromkeys(candidates):
            if cancelled():
                return None
            if not card_safety.below(dst, library) or card_safety.below(dst, os.path.dirname(item.src)):
                continue
            try:
                before = (identity(item.src), identity(dst))
                found = True
                key = (item.src, dst, before)
                equal = (reuse_matches and key in cache) or duplicates.files_identical(item.src, dst, cancelled=cancelled)
                if equal and not cancelled() and before == (identity(item.src), identity(dst)):
                    cache[key] = True
                    return dst
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                failed = True
        reason = "Could not verify" if failed else ("Different" if found else "New or not found")
        return Difference(item.src, item.dst, reason)

    matched = done = 0
    differences = []
    # Four independent sequential reads overlap NAS latency. Sidecars follow
    # recordings so their candidate folders can use verified recording matches.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="card-verify") as pool:
        for audio in (True, False):
            items = [item for item in plan if (item.src in recs) == audio]
            # Keep both pending reads and queued work bounded.
            for start in range(0, len(items), 8):
                if cancelled():
                    return BackupResult(len(plan), matched, differences)
                batch = items[start:start + 8]
                for item, dst in zip(batch, pool.map(match, batch)):
                    if isinstance(dst, Difference):
                        differences.append(dst)
                    elif dst is not None:
                        moved[os.path.dirname(item.src)].add(os.path.dirname(dst))
                        matched += 1
                    done += 1
                    progress(done, len(plan))
    return BackupResult(len(plan), matched, differences)
