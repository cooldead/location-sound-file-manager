"""Pure card-offload logic: find recorder cards, plan the copy to the NAS, copy
with verification. No Qt.

A Sound Devices card looks like ``<card>/<Project>/<Day>/[<Take>/]files`` next
to the recorder's own folders (SOUNDDEV, SETTINGS, TRASH, ...). Zoom cards put
``<Project>/files`` at the root. Either way the first folder below the card
root is the project folder, and everything below it is copied to
``<library>/<destination project folder>/`` unchanged, which matches how the
library is already laid out.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Recorder housekeeping folders that are never offloaded.
SYSTEM_FOLDERS = {"SOUNDDEV", "SETTINGS", "TRASH", "MIDI_MAPPING", ".fseventsd", ".Trashes", ".Spotlight-V100",
                  "System Volume Information", "$RECYCLE.BIN", "LOST.DIR"}
# Sound Devices moves takes marked false into this folder.
FALSE_TAKES = "FALSETAKES"
ROOT_FILES = "(files at card root)"
# Where removable media are mounted (the Browse button starts here).
MEDIA_FOLDER = "/Volumes" if sys.platform == "darwin" else "/run/media"


@dataclass(frozen=True)
class Card:
    path: str
    label: str
    size: int = 0  # bytes, 0 if unknown
    device: str = ""  # e.g. /dev/sdc1 (Linux) or /dev/disk4 (macOS), for ejecting


def removable_mounts() -> list[Card]:
    """Mounted filesystems on removable media (SD card readers, card-slot
    recorders connected over USB). Fixed and USB hard disks are not listed;
    those can still be chosen with Browse."""
    if sys.platform == "darwin":
        return _mac_removable_mounts()
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", "PATH,MOUNTPOINT,RM,LABEL,SIZE"],
                             capture_output=True, text=True, timeout=5).stdout
        devices = json.loads(out).get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    cards = []

    def walk(nodes, removable=False):
        for node in nodes:
            rm = removable or node.get("rm") in (True, "1")
            mount = node.get("mountpoint")
            if mount and rm and mount not in ("/", "[SWAP]"):
                cards.append(Card(mount, node.get("label") or os.path.basename(mount), int(node.get("size") or 0),
                                  node.get("path") or ""))
            walk(node.get("children", []), rm)

    walk(devices)
    return cards


# "/dev/disk4s1 on /Volumes/NO NAME (msdos, local, nodev, ...)"
_MOUNT_LINE = re.compile(r"^(/dev/\S+) on (.+) \(([^)]*)\)$")
# diskutil answers per device node; a card keeps its node while it is mounted.
_diskutil_cache: dict[str, dict] = {}


def _mac_removable_mounts() -> list[Card]:
    """macOS: local mounts from `mount` (network shares are skipped without
    touching them, so a slow NAS never stalls the 3 s card poll), then
    `diskutil info` for each new device, cached."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    cards, seen = [], set()
    for line in out.splitlines():
        match = _MOUNT_LINE.match(line)
        if not match or not match.group(2).startswith("/Volumes/"):
            continue
        node, mount = match.group(1), match.group(2)
        seen.add(node)
        if node not in _diskutil_cache:
            _diskutil_cache[node] = _diskutil_info(node)
        card = card_from_diskutil(mount, _diskutil_cache[node])
        if card is not None:
            cards.append(card)
    for node in set(_diskutil_cache) - seen:
        del _diskutil_cache[node]
    return cards


def _diskutil_info(target: str) -> dict:
    try:
        out = subprocess.run(["diskutil", "info", "-plist", target], capture_output=True, timeout=10).stdout
        info = plistlib.loads(out)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return {}


def card_from_diskutil(mount: str, info: dict) -> Card | None:
    """A Card for a volume on removable media (what lsblk calls RM=1): SD
    slots and readers, recorders in USB mode. External hard disks and disk
    images are not cards."""
    if info.get("BusProtocol") == "Disk Image" or not (
            info.get("RemovableMedia") or info.get("BusProtocol") == "Secure Digital"):
        return None
    whole = info.get("ParentWholeDisk") or ""
    return Card(mount, info.get("VolumeName") or os.path.basename(mount), int(info.get("TotalSize") or 0),
                f"/dev/{whole}" if whole else info.get("DeviceNode") or "")


def looks_like_card(path: str) -> bool:
    """A recorder card: a SOUNDDEV folder, or WAV files in the top two levels."""
    if os.path.isdir(os.path.join(path, "SOUNDDEV")):
        return True

    def has_wav(folder: str, depth: int) -> bool:
        try:
            with os.scandir(folder) as entries:
                items = list(entries)
        except OSError:
            return False
        if any(e.name.lower().endswith((".wav", ".bwf")) and not e.name.startswith("._") for e in items):
            return True
        if depth == 0:
            return False
        return any(has_wav(e.path, depth - 1) for e in items[:200]
                   if e.is_dir(follow_symlinks=False) and not is_system_folder(e.name))

    return has_wav(path, 2)


def is_system_folder(name: str) -> bool:
    return name in SYSTEM_FOLDERS or name.startswith(".")


def eject(card: Card) -> str:
    """Unmount the card and power the reader slot down. Returns "" or an error."""
    if not card.device:
        return "the device of this card is not known"
    if sys.platform == "darwin":
        # Ejecting the whole disk unmounts every volume on it and releases the reader.
        try:
            done = subprocess.run(["diskutil", "eject", card.device], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            return str(error)
        return "" if done.returncode == 0 else (done.stderr or done.stdout).strip()
    for args in (["udisksctl", "unmount", "-b", card.device], ["udisksctl", "power-off", "-b", card.device]):
        try:
            done = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return str(error)
        if done.returncode != 0 and args[1] == "unmount":
            return (done.stderr or done.stdout).strip()
    return ""


def project_folder(path: str, card_root: str) -> str:
    """The card folder a file belongs to (first folder below the card root)."""
    parts = Path(path).relative_to(card_root).parts
    return parts[0] if len(parts) > 1 else ROOT_FILES


def day_folder(path: str, card_root: str) -> str:
    """Second-level folder (the recorder's day folder), or "" when there is none."""
    parts = Path(path).relative_to(card_root).parts
    return parts[1] if len(parts) > 2 else ""


def card_files(card_root: str, folders: set[str], *, include_false_takes: bool = False) -> list[str]:
    """Every file (audio and sidecars) inside the chosen project folders."""
    result = []
    for name in sorted(folders):
        if name == ROOT_FILES:
            with os.scandir(card_root) as entries:
                result += sorted(e.path for e in entries if e.is_file() and not e.name.startswith("."))
            continue
        if name == FALSE_TAKES and not include_false_takes:
            continue
        for folder, dirs, files in os.walk(os.path.join(card_root, name)):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in sorted(files):
                if f.startswith("._") or f == ".DS_Store":
                    continue
                result.append(os.path.join(folder, f))
    return result


@dataclass
class CopyItem:
    src: str
    dst: str
    size: int
    # "new", "same" (already there, identical size+date), "same audio" (already
    # there with the same audio, only its metadata bytes or date differ), "conflict"
    status: str = "new"
    rename_from: str = ""  # pending rename: the card name this file had

    @property
    def needs_copy(self) -> bool:
        return self.status == "new"

    @property
    def on_nas(self) -> bool:
        """The recording is already at its destination (not copied again)."""
        return self.status in ("same", "same audio")


def destination_for(src: str, card_root: str, library: str, folder_names: dict[str, str],
                    new_names: dict[str, str] | None = None) -> str:
    """<library>/<destination folder for the card folder>/<rest of the path>."""
    parts = Path(src).relative_to(card_root).parts
    if len(parts) == 1:
        target_folder = folder_names.get(ROOT_FILES, "")
        rest = parts
    else:
        target_folder = folder_names.get(parts[0], parts[0])
        rest = parts[1:]
    name = (new_names or {}).get(src)
    if name:
        rest = (*rest[:-1], name)
    return str(Path(library, target_folder, *rest)) if target_folder else str(Path(library, *rest))


def plan_copy(files: list[str], card_root: str, library: str, folder_names: dict[str, str],
              new_names: dict[str, str] | None = None) -> list[CopyItem]:
    """What to copy; files already present with the same size and date are
    skipped, and a different file in the way is a conflict (never overwritten)."""
    items = []
    seen: dict[str, str] = {}
    for src in files:
        stat = os.stat(src)
        dst = destination_for(src, card_root, library, folder_names, new_names)
        item = CopyItem(src, dst, stat.st_size, rename_from=os.path.basename(src) if new_names and src in new_names
                        else "")
        if dst in seen:
            item.status = "conflict"
        else:
            try:
                existing = os.stat(dst)
                same = existing.st_size == stat.st_size and abs(existing.st_mtime - stat.st_mtime) < 2.1
                item.status = "same" if same else "conflict"
            except FileNotFoundError:
                pass
        seen[dst] = src
        items.append(item)
    return items


@dataclass
class CopyProgress:
    done_bytes: int = 0
    total_bytes: int = 0
    done_files: int = 0
    total_files: int = 0
    current: str = ""
    phase: str = "copy"  # "copy" or "verify"
    started: float = field(default_factory=time.monotonic)

    @property
    def rate(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.done_bytes / elapsed if elapsed > 0.5 else 0.0

    @property
    def eta(self) -> float | None:
        rate = self.rate
        return (self.total_bytes - self.done_bytes) / rate if rate > 0 else None


class CopyCancelled(Exception):
    pass


@dataclass
class CopyResult:
    copied: list[CopyItem] = field(default_factory=list)
    skipped: list[CopyItem] = field(default_factory=list)
    failed: list[tuple[CopyItem, str]] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)


BLOCK = 8 << 20


def copy_items(items: list[CopyItem], *, verify: bool = True,
               progress: Callable[[CopyProgress], None] | None = None,
               cancelled: Callable[[], bool] = lambda: False) -> CopyResult:
    """Copy the "new" items. Each file is written to a temporary name next to
    its destination, checked (size, and an MD5 of the copy read back against the
    source's when verify is on), given the source's dates, then renamed into
    place. An existing file is never replaced."""
    result = CopyResult()
    todo = [i for i in items if i.needs_copy]
    result.skipped = [i for i in items if not i.needs_copy]
    state = CopyProgress(total_bytes=sum(i.size for i in todo) * (2 if verify else 1), total_files=len(todo))
    last = 0.0

    def report(force=False):
        nonlocal last
        now = time.monotonic()
        if progress and (force or now - last > 0.2):
            last = now
            progress(state)

    for item in todo:
        if cancelled():
            break
        state.current = item.dst
        temp = None
        try:
            folder = os.path.dirname(item.dst)
            _make_dirs(folder, result.created_dirs)
            temp = os.path.join(folder, f".sfm-part-{uuid.uuid4().hex}")
            source_hash = hashlib.md5()
            state.phase = "copy"
            with open(item.src, "rb") as src, open(temp, "wb") as dst:
                while True:
                    if cancelled():
                        raise CopyCancelled()
                    block = src.read(BLOCK)
                    if not block:
                        break
                    source_hash.update(block)
                    dst.write(block)
                    state.done_bytes += len(block)
                    report()
                dst.flush()
                os.fsync(dst.fileno())
            if os.path.getsize(temp) != item.size:
                raise OSError("the copy has a different size than the original")
            if verify:
                state.phase = "verify"
                copy_hash = hashlib.md5()
                with open(temp, "rb") as check:
                    while True:
                        if cancelled():
                            raise CopyCancelled()
                        block = check.read(BLOCK)
                        if not block:
                            break
                        copy_hash.update(block)
                        state.done_bytes += len(block)
                        report()
                if copy_hash.digest() != source_hash.digest():
                    raise OSError("verification failed: the copy does not match the card")
            shutil.copystat(item.src, temp)
            if os.path.lexists(item.dst):
                raise OSError("a file with this name appeared at the destination")
            os.rename(temp, item.dst)
            temp = None
            result.copied.append(item)
            state.done_files += 1
            report(force=True)
        except CopyCancelled:
            break
        except OSError as error:
            result.failed.append((item, str(error)))
        finally:
            if temp is not None:
                try:
                    os.remove(temp)
                except OSError:
                    pass
    report(force=True)
    return result


def _make_dirs(folder: str, created: list[str]) -> None:
    missing = []
    path = folder
    while not os.path.isdir(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for path in reversed(missing):
        os.mkdir(path)
        created.append(path)


def common_folder(paths: list[str]) -> str:
    """Deepest folder containing all paths (for "Open project folder")."""
    if not paths:
        return ""
    folders = [os.path.dirname(p) for p in paths]
    return os.path.commonpath(folders) if len(folders) > 1 else folders[0]


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def human_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02} min"
