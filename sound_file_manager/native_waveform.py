"""Optional Rust reductions through a small C ABI; ctypes releases the GIL.

Buffers are owned by Python/NumPy and never retained by Rust. Source installs
without the host library retain the NumPy implementation.
"""
import ctypes
import os
from pathlib import Path
import sys

import numpy as np


def _load():
    if os.environ.get("SFM_WAVEFORM_BACKEND") == "numpy":
        return None
    name = {"win32": "sfm_waveform.dll", "darwin": "libsfm_waveform.dylib"}.get(sys.platform, "libsfm_waveform.so")
    try:
        lib = ctypes.CDLL(str(Path(__file__).with_name("_native") / name))
        lib.sfm_waveform_abi.restype = ctypes.c_uint32
        if lib.sfm_waveform_abi() != 1:
            return None
        lib.sfm_waveform_reduce.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ]
        lib.sfm_waveform_reduce.restype = ctypes.c_int
        return lib
    except (OSError, AttributeError):
        return None


_library = _load()
AVAILABLE = _library is not None


def reduce(raw: bytes, bits: int, channels: int, is_float: bool, cuts):
    """Peak and sum of squares per channel/segment, or None without Rust."""
    if _library is None:
        return None
    if channels <= 0 or bits not in ((32, 64) if is_float else (8, 16, 24, 32)):
        raise ValueError("unsupported sample format")
    cuts = np.ascontiguousarray(cuts, dtype=np.uint64)
    if cuts.ndim != 1 or len(cuts) < 2:
        raise ValueError("expected segment boundaries")
    peaks = np.empty((channels, len(cuts) - 1), dtype=np.float32)
    sums = np.empty(peaks.shape, dtype=np.float64)
    # c_char_p keeps the immutable bytes alive, including embedded NULs.
    data = ctypes.c_char_p(raw)
    status = _library.sfm_waveform_reduce(data, len(raw), bits, channels, int(is_float),
                                         cuts.ctypes.data, len(cuts) - 1,
                                         peaks.ctypes.data, sums.ctypes.data)
    if status:
        raise ValueError("invalid waveform segment boundaries")
    return peaks, sums
