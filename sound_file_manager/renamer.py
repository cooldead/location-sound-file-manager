"""Pure rename logic: validation, clash detection, safe batch apply, patterns.

Nothing in here touches Qt so it can be unit tested on its own.
"""

from __future__ import annotations

import errno
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_NAME_BYTES = 255


class RenameError(Exception):
    pass


@dataclass(frozen=True)
class RenameOp:
    src: Path
    dst: Path


def validate_name(name: str) -> str | None:
    """Return an error message for an unusable file name, or None if it is fine."""
    if not name.strip():
        return "name is empty"
    if name in (".", ".."):
        return "name cannot be '.' or '..'"
    if "/" in name:
        return "name cannot contain '/'"
    if "\0" in name:
        return "name cannot contain a NUL character"
    if len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
        return f"name is longer than {MAX_NAME_BYTES} bytes"
    if sys.platform == "win32":
        bad = sorted(set(name) & set(_WINDOWS_BAD_CHARS))
        if bad:
            return "name cannot contain " + " ".join(f"'{c}'" for c in bad) + " on Windows"
        if name != name.rstrip(" ."):
            return "name cannot end with a space or '.' on Windows"
        if name.split(".")[0].strip().upper() in _WINDOWS_RESERVED:
            return f"'{name.split('.')[0]}' is a reserved name on Windows"
    return None


_WINDOWS_BAD_CHARS = '\\:*?"<>|'
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def target_for(src: Path, text: str, suffix: str) -> Path:
    """Destination for a name typed by the user, relative to src's folder.

    "/" separates subfolders, so "Day 2/10T03" moves the file into
    Day 2 (created when renaming). Spaces around each part are dropped.
    Raises RenameError for an unusable path.
    """
    parts = [part.strip() for part in text.split("/")]
    *folders, stem = parts
    for folder in folders:
        if not folder:
            raise RenameError("folder name is empty (check for '//' or a leading '/')")
        message = validate_name(folder)
        if message:
            raise RenameError(f"folder '{folder}': {message}")
    if not stem:
        raise RenameError("name is empty")
    return src.parent.joinpath(*folders, stem + suffix)


def folder_for(base: Path, text: str) -> Path:
    """A folder path typed by the user ("a" or "a/b"), inside base."""
    parts = [part.strip() for part in text.split("/")]
    for part in parts:
        if not part:
            raise RenameError("folder name is empty (check for '//' or a leading/trailing '/')")
        message = validate_name(part)
        if message:
            raise RenameError(f"folder '{part}': {message}")
    return base.joinpath(*parts)


def _folder_blocker(folder: Path) -> Path | None:
    """The first existing ancestor of folder that is not a directory, if any."""
    path = folder
    while not os.path.lexists(path):
        path = path.parent
    return None if path.is_dir() else path


def plan_renames(ops: list[RenameOp]) -> dict[Path, str]:
    """Check a batch of renames without touching the disk.

    Returns a mapping of source path -> error message. An empty mapping means
    the whole batch can be applied.
    """
    errors: dict[Path, str] = {}
    sources = {op.src for op in ops}

    targets: dict[Path, list[Path]] = {}
    for op in ops:
        targets.setdefault(op.dst, []).append(op.src)

    for op in ops:
        if op.src == op.dst:
            continue
        message = validate_name(op.dst.name)
        if message is None and op.dst.parent != op.src.parent:
            blocker = _folder_blocker(op.dst.parent)
            if blocker is not None:
                message = f"'{blocker.name}' is a file, not a folder"
        if message is None and not os.path.lexists(op.src):
            message = "file no longer exists"
        if message is None and op.src.is_dir() and op.dst.is_relative_to(op.src):
            message = "a folder cannot be moved into itself"
        if message is None and len(targets[op.dst]) > 1:
            message = f"more than one file would be named '{op.dst.name}'"
        if (
            message is None
            and op.dst not in sources
            and os.path.lexists(op.dst)
            and not _same_file(op.src, op.dst)
        ):
            message = f"'{op.dst.name}' already exists"
        if message is not None:
            errors[op.src] = message
    return errors


def apply_renames(ops: list[RenameOp], created_dirs: list[Path] | None = None,
                  progress: Callable[[int, int], None] | None = None) -> list[RenameOp]:
    """Apply a batch of renames/moves, or none of them.

    Every file is first moved to a unique temporary name and only then to its
    final name, so swaps (a->b, b->a) and chains (a->b, b->c) work. Missing
    target folders are created and appended to created_dirs (so an undo can
    remove them again). On any failure the files already moved are put back,
    folders created here are removed, and RenameError is raised.
    Returns the renames that were applied (no-ops removed), for undo.
    progress(done, total) is called as files move (two steps per file).
    """
    created: list[Path] = []
    ops = [op for op in ops if op.src != op.dst]
    if not ops:
        return []
    errors = plan_renames(ops)
    if errors:
        src, message = next(iter(errors.items()))
        raise RenameError(f"{src.name}: {message}")

    staged: list[tuple[RenameOp, Path]] = []
    finished: list[tuple[RenameOp, Path]] = []
    try:
        for op in ops:
            temp = op.src.with_name(f".sfm-tmp-{uuid.uuid4().hex}{op.src.suffix}")
            os.rename(op.src, temp)
            staged.append((op, temp))
            if progress:
                progress(len(staged), 2 * len(ops))
        for op, temp in staged:
            # os.rename silently replaces an existing file, so re-check right
            # before the final move in case something appeared meanwhile.
            if os.path.lexists(op.dst):
                raise RenameError(f"'{op.dst.name}' already exists")
            _make_dirs(op.dst.parent, created)
            os.rename(temp, op.dst)
            finished.append((op, temp))
            if progress:
                progress(len(ops) + len(finished), 2 * len(ops))
    except (OSError, RenameError) as error:
        stuck = _roll_back(staged, finished)
        remove_empty_dirs(created)
        if isinstance(error, OSError) and error.errno == errno.EXDEV:
            message = "files can only be moved within the same drive/partition"
        else:
            message = str(error)
        if stuck:
            message += "\n\nCould not restore:\n" + "\n".join(stuck)
        raise RenameError(message) from error
    if created_dirs is not None:
        created_dirs.extend(created)
    return ops


def _make_dirs(folder: Path, created: list[Path]) -> None:
    missing = []
    while not os.path.lexists(folder):
        missing.append(folder)
        folder = folder.parent
    for path in reversed(missing):
        os.mkdir(path)
        created.append(path)


def remove_empty_dirs(dirs: list[Path]) -> None:
    """Remove the given folders, deepest first, if they are empty."""
    for path in sorted(set(dirs), key=lambda p: len(p.parts), reverse=True):
        try:
            os.rmdir(path)
        except OSError:
            pass


def _roll_back(staged: list[tuple[RenameOp, Path]], finished: list[tuple[RenameOp, Path]]) -> list[str]:
    stuck = []
    left_at_target = set()
    for op, temp in reversed(finished):
        try:
            os.rename(op.dst, temp)
        except OSError as error:
            left_at_target.add(op)
            stuck.append(f"{op.dst} ({error})")
    for op, temp in reversed(staged):
        if op in left_at_target:
            continue
        try:
            os.rename(temp, op.src)
        except OSError as error:
            stuck.append(f"{temp} -> {op.src.name} ({error})")
    return stuck


def translate(path: Path, mapping: dict[Path, Path]) -> Path:
    """Where path ended up after the renames in mapping (old -> new), also for
    paths inside a renamed folder."""
    if path in mapping:
        return mapping[path]
    for old, new in mapping.items():
        if path.is_relative_to(old):
            return new / path.relative_to(old)
    return path


def undo_ops(applied: list[RenameOp]) -> list[RenameOp]:
    """The renames that reverse a previously applied batch."""
    return [RenameOp(op.dst, op.src) for op in reversed(applied)]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def find_replace(text: str, find: str, replace: str, *, regex: bool = False, case_sensitive: bool = True) -> str:
    """Replace every occurrence of find. Raises ValueError for a bad regex."""
    if not find:
        return text
    flags = 0 if case_sensitive else re.IGNORECASE
    if regex:
        try:
            return re.sub(find, replace, text, flags=flags)
        except re.error as error:
            raise ValueError(f"invalid regular expression: {error}") from error
    if case_sensitive:
        return text.replace(find, replace)
    return re.sub(re.escape(find), lambda _: replace, text, flags=flags)
