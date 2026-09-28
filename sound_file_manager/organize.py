"""Pure pattern logic for batch renames and "reorganize into project folders"."""

from __future__ import annotations

from . import card_safety

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .catalog import Recording, day_of
from .renamer import RenameOp, find_replace, plan_renames, validate_name

TOKENS = {
    "project": "project name",
    "day": "recording date, else tape",
    "date": "recording date (YYYY-MM-DD)",
    "tape": "tape / reel name",
    "scene": "scene",
    "take": "take",
    "recorder": "recorder model",
    "folder": "current folder name",
    "name": "current file name without extension",
    "filename": "current file name with extension",
    "n": "running number (n:3 pads to 3 digits)",
}
FALLBACKS = {"project": "No Project", "day": "No Date", "date": "No Date", "tape": "No Tape",
             "scene": "No Scene", "take": "No Take", "recorder": "Unknown Recorder"}
# Empty files recorders leave in day/take folders, and macOS Finder data.
MARKER_FILES = {".take_folder", ".daily_folder", ".DS_Store"}
_TOKEN_RE = re.compile(r"\{(\w+)(?::(\d+))?\}")
# Characters that are invalid on SMB shares and in Windows file names.
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_part(text: str) -> str:
    """Make a metadata value usable as one path component."""
    text = _UNSAFE_RE.sub("-", text).strip().rstrip(".")
    return text if text not in ("", ".", "..") else ""


def token_values(rec: Recording) -> dict[str, str]:
    recorder = re.sub(r"^SoundDev:\s*", "", rec.recorder).strip()
    return {
        "project": rec.project, "day": day_of(rec), "date": rec.date, "tape": rec.tape,
        "scene": rec.scene, "take": rec.take, "recorder": recorder,
        "folder": os.path.basename(rec.folder), "name": Path(rec.name).stem, "filename": rec.name,
    }


def expand(pattern: str, rec: Recording, number: int = 1, *, for_path: bool = True) -> str:
    """Replace {tokens} with the recording's values. With for_path, each value
    is made safe as a path component and empty ones get a fallback name.
    Unknown tokens are left untouched."""
    values = token_values(rec)

    def replace(match: re.Match) -> str:
        token, width = match.group(1), match.group(2)
        if token == "n":
            return str(number).zfill(int(width or 1))
        if token not in values:
            return match.group(0)
        value = values[token]
        if for_path:
            value = safe_part(value) or FALLBACKS.get(token, "")
        return value

    return _TOKEN_RE.sub(replace, pattern)


@dataclass
class Move:
    rec: Recording
    src: Path
    dst: Path
    error: str = ""

    @property
    def unchanged(self) -> bool:
        return self.src == self.dst


def plan_moves(recs: list[Recording], destination: str, pattern: str) -> list[Move]:
    """Where each recording goes under destination; errors (clashes, bad names)
    are filled in per move."""
    moves = []
    for number, rec in enumerate(recs, 1):
        relative = expand(pattern, rec, number).strip("/")
        parts = [p.strip() for p in relative.split("/") if p.strip()]
        src = Path(rec.path)
        if not parts:
            moves.append(Move(rec, src, src, "the pattern gives an empty path"))
            continue
        # The pattern names folders unless its last part holds the file name:
        # "{filename}" is used as is, "{name}..." gets the extension added.
        last = pattern.rstrip("/").rsplit("/", 1)[-1]
        if "{filename}" not in last:
            if "{name}" in last:
                parts[-1] += src.suffix
            else:
                parts.append(rec.name)
        error = next((f"'{p}': {m}" for p in parts if (m := validate_name(p))), "")
        dst = Path(destination).joinpath(*parts)
        moves.append(Move(rec, src, dst, error))
    _add_plan_errors(moves)
    return moves


def plan_renames_for(recs: list[Recording], new_names: list[str]) -> list[Move]:
    """Renames within each file's own folder (new_names are full file names)."""
    moves = []
    for rec, name in zip(recs, new_names):
        src = Path(rec.path)
        error = validate_name(name) or ""
        moves.append(Move(rec, src, src.with_name(name) if not error else src, error))
    _add_plan_errors(moves)
    return moves


def _add_plan_errors(moves: list[Move]) -> None:
    ops = [RenameOp(m.src, m.dst) for m in moves if not m.error and not m.unchanged]
    errors = plan_renames(ops)
    for move in moves:
        if not move.error and move.src in errors:
            move.error = errors[move.src]


def renamed(name: str, *, find: str = "", replace: str = "", regex: bool = False,
            case_sensitive: bool = True, pattern: str = "", rec: Recording | None = None,
            number: int = 1) -> str:
    """New file name for a batch rename: find/replace on the name without its
    extension, or a pattern like "{scene}T{take}_{name}". The extension is kept."""
    stem, ext = os.path.splitext(name)
    if pattern and rec is not None:
        stem = expand(pattern, rec, number, for_path=False)
    elif find:
        stem = find_replace(stem, find, replace, regex=regex, case_sensitive=case_sensitive)
    return stem + ext


def empty_tree(folder: Path) -> list[Path] | None:
    """The marker files in a folder and its subfolders when it holds nothing
    else (no audio, no other files; symlinks count as content), else None."""
    markers: list[Path] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    inner = empty_tree(Path(entry.path))
                    if inner is None:
                        return None
                    markers += inner
                elif entry.name in MARKER_FILES and entry.is_file(follow_symlinks=False):
                    markers.append(Path(entry.path))
                else:
                    return None
    except OSError:
        return None
    return markers


def remove_tree_if_empty(folder: Path) -> tuple[list[Path], list[Path]]:
    """Remove a folder and its subfolders if they hold only marker files
    (checked again right before). Returns (removed folders, removed markers)."""
    card_safety.assert_writable(folder)
    markers = empty_tree(folder)
    if markers is None:
        return [], []
    removed_markers, removed_dirs = [], []
    try:
        for marker in markers:
            card_safety.assert_writable(marker)
            marker.unlink()
            removed_markers.append(marker)
        subfolders = [Path(d) for d, _, _ in os.walk(folder)]
        for path in sorted(subfolders, key=lambda p: len(p.parts), reverse=True):
            card_safety.assert_writable(path)
            path.rmdir()
            removed_dirs.append(path)
    except OSError:
        pass
    return removed_dirs, removed_markers


def remove_left_empty(folders: set[Path], root: str) -> tuple[list[Path], list[Path]]:
    """Remove folders (with any subfolders, and then their now-empty parents,
    up to root) that only hold recorder marker files. Returns (removed
    folders, removed marker files)."""
    removed_dirs, removed_markers = [], []
    root_path = Path(root)
    pending = sorted(folders, key=lambda p: len(p.parts), reverse=True)
    seen = set()
    while pending:
        folder = pending.pop(0)
        if folder in seen or folder == root_path or not folder.is_relative_to(root_path):
            continue
        seen.add(folder)
        dirs, markers = remove_tree_if_empty(folder)
        removed_dirs += dirs
        removed_markers += markers
        if folder not in dirs:
            continue
        pending.append(folder.parent)
        pending.sort(key=lambda p: len(p.parts), reverse=True)
    return removed_dirs, removed_markers


def find_empty_folders(root: str, cancelled=lambda: False, progress=None) -> list[Path]:
    """Every folder below root that holds no files except marker files (in it
    or in its subfolders); only the topmost of such a tree is listed. Hidden
    folders are left alone. progress(folders_seen, path) is called as it goes."""
    found: list[Path] = []
    seen = [0]

    def visit(path: str) -> bool:
        if cancelled():
            return False
        seen[0] += 1
        if progress and seen[0] % 50 == 0:
            progress(seen[0], path)
        empty, empty_children = True, []
        try:
            with os.scandir(path) as entries:
                items = list(entries)
        except OSError:
            return False
        for entry in items:
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                if visit(entry.path):
                    empty_children.append(Path(entry.path))
                else:
                    empty = False
            elif entry.name in MARKER_FILES and entry.is_file(follow_symlinks=False):
                continue
            else:
                empty = False
        if not empty or path == root:
            found.extend(empty_children)
        return empty

    visit(root)
    return [] if cancelled() else sorted(found)
