"""Size-checked, disposable working copies of card recordings and accompanying files."""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass

from . import card_safety, compat, offload
from .organize import MARKER_FILES


@dataclass(frozen=True)
class Entry:
    relative: str
    size: int
    mtime_ns: int


def manifest(root: str, cancelled=lambda: False) -> list[Entry]:
    """Strict traversal: unreadable paths and symlinks cannot silently disappear."""
    found = []

    def visit(folder, top=False):
        if cancelled():
            raise offload.CopyCancelled()
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name.casefold()):
                if entry.name.startswith('._') or entry.name == '.DS_Store' or entry.name in MARKER_FILES:
                    continue
                if top and (entry.name.startswith('.') or offload.is_system_folder(entry.name)):
                    continue
                if entry.is_symlink():
                    raise OSError(f"Cannot make a complete card working copy: symbolic link {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    if not entry.name.startswith('.'):
                        visit(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    found.append(Entry(compat.relpath(compat.fwd(entry.path), root), st.st_size, st.st_mtime_ns))
                else:
                    raise OSError(f"Unsupported card entry: {entry.path}")
    visit(root, True)
    return found


class CardWorkspace:
    def __init__(self, source: str, folder: str):
        self.source = source
        card_safety.assert_writable(folder)
        if card_safety.below(folder, source):
            raise PermissionError("The working copy must be stored outside the card.")
        os.makedirs(folder, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix='sfm-card-', dir=folder)
        self.root = compat.fwd(self._temporary.name)
        self.files: list[str] = []
        self.total_bytes = 0
        self.verified_matches: dict = {}

    @classmethod
    def prepare(cls, source: str, folder: str, *, progress=None, cancelled=lambda: False):
        card_safety.protect(source)
        card_safety.assert_writable(folder)
        before = manifest(source, cancelled)
        total = sum(e.size for e in before)
        card_safety.assert_writable(folder)
        os.makedirs(folder, exist_ok=True)
        if shutil.disk_usage(folder).free < total + (64 << 20):
            raise OSError(f"Not enough local space for a working copy ({offload.human_size(total)} needed). "
                          "Choose another Working Copy Folder or free local disk space.")
        workspace = cls(source, folder)
        try:
            plan = [offload.CopyItem(compat.join(source, e.relative), compat.join(workspace.root, e.relative), e.size)
                    for e in before]
            # This is a disposable review copy. Avoid rereading every byte and
            # forcing each file to stable storage before the user can review it.
            # copy_items still checks output sizes; the manifest below catches
            # source changes. Permanent offloads keep their verification policy.
            result = offload.copy_items(plan, verify=False, durable=False,
                                        progress=progress, cancelled=cancelled)
            if cancelled():
                raise offload.CopyCancelled()
            if result.failed or len(result.copied) != len(plan):
                message = result.failed[0][1] if result.failed else 'not every file was copied'
                raise OSError(f"Working copy incomplete: {message}")
            if manifest(source, cancelled) != before:
                raise OSError("Card contents changed while copying. Load the card again.")
            workspace.files = [item.dst for item in plan]
            workspace.total_bytes = total
            return workspace
        except BaseException:
            workspace.close()
            raise

    def original_path(self, path: str) -> str:
        return compat.join(self.source, compat.relpath(path, self.root))

    def close(self):
        self._temporary.cleanup()
