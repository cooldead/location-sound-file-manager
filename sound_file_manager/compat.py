"""Pure: the few OS calls that differ on Windows. No Qt.

Paths inside the app always use "/" (project finding, the library index keys
and many prefix checks are string work on "/"). Windows accepts "/" in every
file API, and Qt's file dialogs already return it, so on Windows the path
helpers here turn "\\" into "/"; elsewhere they are the os.path functions.
"""

from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"

# Low-level os.open() on Windows defaults to text mode (CRLF translation).
O_RDONLY = os.O_RDONLY | getattr(os, "O_BINARY", 0)


if WINDOWS:
    import ctypes
    import msvcrt
    import ntpath
    from ctypes import wintypes

    def fwd(path) -> str:
        """path as a str with "/" separators."""
        return os.fspath(path).replace("\\", "/")

    def join(*parts) -> str:
        return fwd(ntpath.join(*parts))

    def normpath(path) -> str:
        return fwd(ntpath.normpath(path))

    def relpath(path, start) -> str:
        return fwd(ntpath.relpath(path, start))

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
    _ReadFile.restype = wintypes.BOOL
    _ERROR_HANDLE_EOF = 38

    def pread(fd: int, size: int, offset: int) -> bytes:
        """os.pread for Windows: ReadFile at an offset, so threads sharing an
        fd don't race on a seek position (the reader thread and the GUI)."""
        if size <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        done = wintypes.DWORD(0)
        overlapped = _Overlapped(Offset=offset & 0xFFFFFFFF, OffsetHigh=offset >> 32)
        if not _ReadFile(msvcrt.get_osfhandle(fd), buffer, size, ctypes.byref(done), ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error == _ERROR_HANDLE_EOF:
                return b""
            raise ctypes.WinError(error)
        return buffer.raw[:done.value]
else:
    def fwd(path) -> str:
        return os.fspath(path)

    join = os.path.join
    normpath = os.path.normpath
    relpath = os.path.relpath
    pread = os.pread
