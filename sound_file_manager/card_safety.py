"""Application write guards for recorder cards and folders opened as cards."""
from __future__ import annotations

import os
import threading

_roots: set[str] = set()
_lock = threading.Lock()


def canonical(path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def below(path, root) -> bool:
    path, root = canonical(path), canonical(root)
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def protect(root) -> None:
    # Remember both the mount spelling and its resolved target for the session.
    with _lock:
        _roots.add(canonical(root))
        _roots.add(os.path.normcase(os.path.abspath(os.fspath(root))))


def assert_writable(*paths) -> None:
    with _lock:
        roots = tuple(_roots)
    if not roots:
        return
    for path in paths:
        resolved = canonical(path)
        for root in roots:
            try:
                protected = os.path.commonpath((resolved, root)) == root
            except ValueError:  # different drives on Windows
                protected = False
            if protected:
                raise PermissionError(f"Card contents are protected; choose a destination outside the card: {path}")
