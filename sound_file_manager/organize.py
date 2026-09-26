"""Pure pattern logic for batch renames and "reorganize into project folders"."""

from __future__ import annotations

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


def remove_left_empty(folders: set[Path], root: str) -> tuple[list[Path], list[Path]]:
    """Remove folders (and their now-empty parents, up to root) that only hold
    recorder marker files. Returns (removed folders, removed marker files)."""
    removed_dirs, removed_markers = [], []
    root_path = Path(root)
    pending = sorted(folders, key=lambda p: len(p.parts), reverse=True)
    seen = set()
    while pending:
        folder = pending.pop(0)
        if folder in seen or folder == root_path or not folder.is_relative_to(root_path):
            continue
        seen.add(folder)
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        if any(e.name not in MARKER_FILES or e.is_dir() for e in entries):
            continue
        try:
            for marker in entries:
                marker.unlink()
                removed_markers.append(marker)
            folder.rmdir()
            removed_dirs.append(folder)
        except OSError:
            continue
        pending.append(folder.parent)
        pending.sort(key=lambda p: len(p.parts), reverse=True)
    return removed_dirs, removed_markers
