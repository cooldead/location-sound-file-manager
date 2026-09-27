"""Windows: removable drives (card readers, recorders in USB mode) and eject,
through the Win32 API (ctypes). No Qt. Imported only on Windows.

Only the drive type is asked of every letter, which never touches the
network, so a slow NAS mapped to a letter can't stall the 3 s card poll.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DRIVE_REMOVABLE = 2
SEM_FAILCRITICALERRORS = 0x0001  # an empty card slot must not pop up "Insert a disk"

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_STORAGE_MEDIA_REMOVAL = 0x002D4804
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808

_kernel32.GetLogicalDrives.restype = wintypes.DWORD
_kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetDriveTypeW.restype = wintypes.UINT
_kernel32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
_kernel32.GetVolumeInformationW.restype = wintypes.BOOL
_kernel32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                          ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
_kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
_kernel32.SetThreadErrorMode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.SetThreadErrorMode.restype = wintypes.BOOL
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def removable_drives() -> list[tuple[str, str, int]]:
    """(root "E:/", label, size in bytes) of each removable drive with media in it."""
    old_mode = wintypes.DWORD()
    _kernel32.SetThreadErrorMode(SEM_FAILCRITICALERRORS, ctypes.byref(old_mode))
    try:
        drives = []
        mask = _kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if _kernel32.GetDriveTypeW(root) != DRIVE_REMOVABLE:
                continue
            label = ctypes.create_unicode_buffer(261)
            if not _kernel32.GetVolumeInformationW(root, label, len(label), None, None, None, None, 0):
                continue  # an empty slot of a card reader
            total = ctypes.c_ulonglong(0)
            _kernel32.GetDiskFreeSpaceExW(root, None, ctypes.byref(total), None)
            drives.append((f"{chr(65 + i)}:/", label.value, total.value))
        return drives
    finally:
        _kernel32.SetThreadErrorMode(old_mode, None)


def eject(drive: str) -> str:
    """Flush, lock, dismount and eject the volume ("E:"), like Safely Remove
    for the card. Returns "" or an error."""
    letter = drive.rstrip(":/\\")
    handle = INVALID_HANDLE_VALUE
    for access in (GENERIC_READ | GENERIC_WRITE, GENERIC_READ):
        handle = _kernel32.CreateFileW(f"\\\\.\\{letter}:", access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                       None, OPEN_EXISTING, 0, None)
        if handle != INVALID_HANDLE_VALUE:
            break
    if handle == INVALID_HANDLE_VALUE:
        return str(ctypes.WinError(ctypes.get_last_error()))
    returned = wintypes.DWORD()

    def ioctl(code, in_buffer=None, in_size=0) -> bool:
        return bool(_kernel32.DeviceIoControl(handle, code, in_buffer, in_size, None, 0, ctypes.byref(returned), None))

    try:
        # Another program (Explorer, an antivirus scan) may hold the volume for a moment.
        for _ in range(20):
            if ioctl(FSCTL_LOCK_VOLUME):
                break
            time.sleep(0.25)
        else:
            return "the card is in use by another program (close windows showing it and try again)"
        if not ioctl(FSCTL_DISMOUNT_VOLUME):
            return str(ctypes.WinError(ctypes.get_last_error()))
        allow = ctypes.c_ubyte(0)  # PREVENT_MEDIA_REMOVAL { FALSE }
        ioctl(IOCTL_STORAGE_MEDIA_REMOVAL, ctypes.byref(allow), 1)
        # A reader without an eject mechanism refuses this; the volume is
        # dismounted by now, so the card can be pulled either way.
        ioctl(IOCTL_STORAGE_EJECT_MEDIA)
        return ""
    finally:
        _kernel32.CloseHandle(handle)
