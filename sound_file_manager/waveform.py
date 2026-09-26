"""Pure waveform overview: per-channel peak levels for a WAV, read with bounded I/O.

The files live on a network share, so long recordings are not read in full:
past FULL_READ_LIMIT, evenly spaced blocks are sampled instead. That is plenty
for an overview the width of a window.
"""

from __future__ import annotations

import os
from typing import Callable

import numpy as np

from . import bwf

BUCKETS = 1200
FULL_READ_LIMIT = 256 << 20  # read files up to this size completely
PIECE_BYTES = 8 << 20
SAMPLED_SLOTS = 480  # blocks sampled from bigger files
SAMPLE_BYTES = 64 << 10
QUICK_PREVIEW_ABOVE = 48 << 20  # show a coarse outline first for files bigger than this
QUICK_PREVIEW_SLOTS = 48
FLOOR_DB = -60.0


def _decode(raw: bytes, bits: int, channels: int, is_float: bool) -> np.ndarray:
    """Interleaved PCM -> float32 array (frames, channels) in -1..1."""
    width = bits // 8
    usable = len(raw) - len(raw) % (width * channels)
    raw = raw[:usable]
    if is_float and bits == 32:
        samples = np.frombuffer(raw, "<f4")
    elif is_float and bits == 64:
        samples = np.frombuffer(raw, "<f8").astype(np.float32)
    elif bits == 16:
        samples = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        samples = ints.astype(np.float32) / 8388608.0
    elif bits == 32:
        samples = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    elif bits == 8:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise bwf.WavError(f"unsupported sample format ({bits}-bit)")
    return samples.reshape(-1, channels)


def _to_levels(peaks: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        db = 20 * np.log10(np.clip(peaks, 1e-9, None))
    return np.clip((db - FLOOR_DB) / -FLOOR_DB, 0.0, 1.0).astype(np.float32)


def compute_peaks(path: str, buckets: int = BUCKETS, *,
                  on_partial: Callable[[np.ndarray], None] | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> np.ndarray | None:
    """Peak level per bucket and channel, as float32 (channels, buckets) in 0..1
    on a dB scale (FLOOR_DB -> 0, 0 dBFS -> 1). Returns None if cancelled.

    on_partial receives intermediate results (same shape) so the display can
    fill in while a long file is still being read.
    """
    with open(path, "rb") as f:
        layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
        info = bwf._info_from(f, layout)
        data = layout.first(b"data")
        if data is None or not info.block_align or not info.channels:
            raise bwf.WavError("no audio data")
        frames = data.size // info.block_align
        if frames == 0:
            return np.zeros((info.channels, buckets), np.float32)
        buckets = min(buckets, frames)
        is_float = info.format_tag == 3
        peaks = np.zeros((info.channels, buckets), np.float32)
        edges = np.linspace(0, frames, buckets + 1).astype(np.int64)

        if data.size > QUICK_PREVIEW_ABOVE and on_partial and data.size <= FULL_READ_LIMIT:
            # A full read takes a second or two on the share; show a coarse
            # outline from a few spread-out blocks first.
            slots = min(QUICK_PREVIEW_SLOTS, buckets)
            slot_edges = np.linspace(0, frames, slots + 1).astype(np.int64)
            preview = np.zeros((info.channels, slots), np.float32)
            block_frames = max(SAMPLE_BYTES // info.block_align, 64)
            for i in range(slots):
                if cancelled():
                    return None
                f.seek(data.data_offset + int(slot_edges[i]) * info.block_align)
                chunk = _decode(f.read(block_frames * info.block_align), info.bits, info.channels, is_float)
                if len(chunk):
                    preview[:, i] = np.abs(chunk).max(axis=0)
            on_partial(_to_levels(preview[:, np.minimum((np.arange(buckets) * slots) // buckets, slots - 1)]))

        if data.size <= FULL_READ_LIMIT:
            # Sequential reads are fast on the share: read it all, piece by piece.
            piece_frames = max(PIECE_BYTES // info.block_align, 1)
            f.seek(data.data_offset)
            start = 0
            while start < frames:
                if cancelled():
                    return None
                count = min(piece_frames, frames - start)
                samples = np.abs(_decode(f.read(count * info.block_align), info.bits, info.channels, is_float))
                if not len(samples):
                    break
                end = start + len(samples)
                first = int(np.searchsorted(edges, start, side="right")) - 1
                last = int(np.searchsorted(edges, end, side="left"))
                for i in range(max(first, 0), min(last, buckets)):
                    lo, hi = max(edges[i], start), min(max(edges[i + 1], edges[i] + 1), end)
                    if hi > lo:
                        peaks[:, i] = np.maximum(peaks[:, i], samples[lo - start:hi - start].max(axis=0))
                start = end
                if on_partial:
                    on_partial(_to_levels(peaks))
        else:
            # Random reads cost ~10 ms each: sample one block per slot, coarse to
            # fine, and show the gaps filled from their left neighbour meanwhile.
            slots = min(SAMPLED_SLOTS, buckets)
            slot_edges = np.linspace(0, frames, slots + 1).astype(np.int64)
            slot_peaks = np.zeros((info.channels, slots), np.float32)
            done = np.zeros(slots, bool)
            block_frames = max(SAMPLE_BYTES // info.block_align, 64)
            for stride in (16, 4, 1):
                for i in range(0, slots, stride):
                    if done[i]:
                        continue
                    if cancelled():
                        return None
                    count = min(block_frames, max(slot_edges[i + 1] - slot_edges[i], 1))
                    f.seek(data.data_offset + int(slot_edges[i]) * info.block_align)
                    chunk = _decode(f.read(int(count) * info.block_align), info.bits, info.channels, is_float)
                    if len(chunk):
                        slot_peaks[:, i] = np.abs(chunk).max(axis=0)
                    done[i] = True
                filled = slot_peaks[:, np.maximum.accumulate(np.where(done, np.arange(slots), 0))]
                peaks = filled[:, np.minimum((np.arange(buckets) * slots) // buckets, slots - 1)]
                if on_partial and stride != 1:
                    on_partial(_to_levels(peaks))
    return _to_levels(peaks)


def to_bytes(peaks: np.ndarray) -> bytes:
    """Compact form for the cache: channel count, then uint8 levels."""
    channels = peaks.shape[0]
    return bytes([channels]) + (peaks * 255).round().astype(np.uint8).tobytes()


def from_bytes(data: bytes) -> np.ndarray:
    channels = data[0]
    levels = np.frombuffer(data[1:], np.uint8).astype(np.float32) / 255.0
    return levels.reshape(channels, -1)
