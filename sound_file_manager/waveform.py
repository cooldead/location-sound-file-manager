"""Pure waveform overview: per-channel peak and RMS levels for a WAV, read with bounded I/O.

The files live on a network share, so long recordings are not read in full:
past FULL_READ_LIMIT, evenly spaced blocks are sampled instead. That is plenty
for an overview the width of a window.

Levels are float32 arrays shaped (2, channels, buckets): [0] the peak and
[1] the RMS of each bucket, on a dB scale (FLOOR_DB -> 0, 0 dBFS -> 1).
"""

from __future__ import annotations

import os
from typing import Callable

import numpy as np

from . import bwf

BUCKETS = 4096  # wider than most screens in device pixels, so the full view is sharp
FULL_READ_LIMIT = 256 << 20  # read files up to this size completely
PIECE_BYTES = 8 << 20
SAMPLED_SLOTS = 480  # blocks sampled from bigger files
SAMPLE_BYTES = 64 << 10
QUICK_PREVIEW_ABOVE = 48 << 20  # show a coarse outline first for files bigger than this
QUICK_PREVIEW_SLOTS = 48
FLOOR_DB = -60.0
MAGIC = b"\xffW2"  # cache blobs with peak + RMS (older blobs held peaks only)


def decode(raw: bytes, bits: int, channels: int, is_float: bool) -> np.ndarray:
    """Interleaved PCM -> float32 array (frames, channels) in -1..1."""
    width = bits // 8
    usable = len(raw) - len(raw) % (width * channels) if width else 0
    raw = raw[:usable]
    if is_float and bits == 32:
        samples = np.frombuffer(raw, "<f4")
    elif is_float and bits == 64:
        samples = np.frombuffer(raw, "<f8").astype(np.float32)
    elif bits == 16:
        samples = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        # Left-align signed PCM in int32: the sign bit lands in bit 31.
        # This avoids expanding all three bytes together and allocating a
        # sign mask/where result for every sample (notably costly on ARM64).
        ints = b[:, 2].astype(np.int32)
        ints <<= 24
        ints |= b[:, 1].astype(np.int32) << 16
        ints |= b[:, 0].astype(np.int32) << 8
        samples = ints.astype(np.float32) * (1.0 / 2147483648.0)
    elif bits == 32:
        samples = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    elif bits == 8:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise bwf.WavError(f"unsupported sample format ({bits}-bit)")
    return samples.reshape(-1, channels)


_decode = decode  # the old name


def _to_levels(values: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        db = 20 * np.log10(np.clip(values, 1e-9, None))
    return np.clip((db - FLOOR_DB) / -FLOOR_DB, 0.0, 1.0).astype(np.float32)


def _levels(peaks: np.ndarray, sums: np.ndarray, counts: np.ndarray) -> np.ndarray:
    rms = np.sqrt(sums / np.maximum(counts, 1))
    return np.stack([_to_levels(peaks), _to_levels(np.minimum(rms, peaks))])


def compute_peaks(path: str, buckets: int = BUCKETS, *, first_frame: int = 0, end_frame: int | None = None,
                  on_partial: Callable[[np.ndarray], None] | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> np.ndarray | None:
    """Peak and RMS level per bucket and channel, float32 (2, channels, buckets)
    in 0..1 on a dB scale. Returns None if cancelled.

    on_partial receives intermediate results (same shape) so the display can
    fill in while a long file is still being read. first_frame / end_frame
    limit it to part of the file (a zoomed-in view).
    """
    with open(path, "rb") as f:
        layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
        info = bwf._info_from(f, layout)
        data = layout.first(b"data")
        if data is None or not info.block_align or not info.channels:
            raise bwf.WavError("no audio data")
        total = data.size // info.block_align
        first_frame = min(max(int(first_frame), 0), total)
        end_frame = total if end_frame is None else min(max(int(end_frame), first_frame), total)
        frames = end_frame - first_frame
        base = data.data_offset + first_frame * info.block_align  # where the range starts
        range_size = frames * info.block_align
        if frames == 0:
            return np.zeros((2, info.channels, buckets), np.float32)
        buckets = min(buckets, frames)
        is_float = info.format_tag == 3
        channels = info.channels

        def read_block(start_frame: int, count: int) -> np.ndarray:
            f.seek(base + start_frame * info.block_align)
            return np.abs(decode(f.read(count * info.block_align), info.bits, channels, is_float))

        def sampled(slots: int, strides, partial: bool) -> np.ndarray | None:
            # Random reads cost ~10 ms each: one block per slot, coarse to fine,
            # with the gaps filled from their left neighbour meanwhile.
            slot_edges = np.linspace(0, frames, slots + 1).astype(np.int64)
            peak = np.zeros((channels, slots), np.float32)
            sums = np.zeros((channels, slots), np.float64)
            counts = np.zeros(slots, np.int64)
            done = np.zeros(slots, bool)
            block_frames = max(SAMPLE_BYTES // info.block_align, 64)
            result = None
            for stride in strides:
                for i in range(0, slots, stride):
                    if done[i]:
                        continue
                    if cancelled():
                        return None
                    count = int(min(block_frames, max(slot_edges[i + 1] - slot_edges[i], 1)))
                    chunk = read_block(int(slot_edges[i]), count)
                    if len(chunk):
                        peak[:, i] = chunk.max(axis=0)
                        sums[:, i] = (chunk.astype(np.float64) ** 2).sum(axis=0)
                        counts[i] = len(chunk)
                    done[i] = True
                fill = np.maximum.accumulate(np.where(done, np.arange(slots), 0))
                spread = np.minimum((np.arange(buckets) * slots) // buckets, slots - 1)
                pick = fill[spread]
                result = _levels(peak[:, pick], sums[:, pick], counts[pick])
                if partial and on_partial and stride != strides[-1]:
                    on_partial(result)
            return result

        if range_size > FULL_READ_LIMIT:
            return sampled(min(SAMPLED_SLOTS, buckets), (16, 4, 1), True)

        if range_size > QUICK_PREVIEW_ABOVE and on_partial:
            # A full read takes a second or two on the share; show a coarse
            # outline from a few spread-out blocks first.
            preview = sampled(min(QUICK_PREVIEW_SLOTS, buckets), (1,), False)
            if preview is None:
                return None
            on_partial(preview)

        # Sequential reads are fast on the share: read it all, piece by piece.
        peak = np.zeros((channels, buckets), np.float32)
        sums = np.zeros((channels, buckets), np.float64)
        counts = np.zeros(buckets, np.int64)
        piece_frames = max(PIECE_BYTES // info.block_align, 1)
        f.seek(base)
        start = 0
        while start < frames:
            if cancelled():
                return None
            count = min(piece_frames, frames - start)
            samples = np.abs(decode(f.read(count * info.block_align), info.bits, channels, is_float))
            if not len(samples):
                break
            end = start + len(samples)
            # Bucket k holds frames [ceil(k*frames/buckets), ceil((k+1)*frames/buckets)).
            first, last = (start * buckets) // frames, ((end - 1) * buckets) // frames
            ks = np.arange(first + 1, last + 1, dtype=np.int64)
            cuts = np.concatenate([[0], -((-ks * frames) // buckets) - start]).astype(np.int64)
            ids = np.arange(first, last + 1)
            peak[:, ids] = np.maximum(peak[:, ids], np.maximum.reduceat(samples, cuts, axis=0).T)
            sums[:, ids] += np.add.reduceat(samples.astype(np.float64) ** 2, cuts, axis=0).T
            counts[ids] += np.diff(np.append(cuts, len(samples)))
            start = end
            if on_partial:
                on_partial(_levels(peak, sums, counts))
    return _levels(peak, sums, counts)


def to_bytes(levels: np.ndarray) -> bytes:
    """Compact form for the cache: magic, channel count, then uint8 peak and RMS levels."""
    channels = levels.shape[1]
    return MAGIC + bytes([channels]) + (levels * 255).round().astype(np.uint8).tobytes()


def from_bytes(data: bytes) -> np.ndarray | None:
    """Levels from the cache, or None for a blob in an older format."""
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 1:
        return None
    channels = data[len(MAGIC)]
    levels = np.frombuffer(data[len(MAGIC) + 1:], np.uint8).astype(np.float32) / 255.0
    if not channels or len(levels) % (2 * channels):
        return None
    return levels.reshape(2, channels, -1)


def to_linear(levels: np.ndarray) -> np.ndarray:
    """dB-scale levels (0..1) back to linear amplitude (0..1)."""
    return np.where(levels > 0, 10 ** ((levels * -FLOOR_DB + FLOOR_DB) / 20), 0.0).astype(np.float32)


# ---------------------------------------------------------------- drawing

def column_levels(levels: np.ndarray, width: int) -> np.ndarray:
    """Resample (2, channels, buckets) levels to one value per pixel column:
    the loudest bucket when several fall in a column, else interpolated."""
    buckets = levels.shape[2]
    width = max(int(width), 1)
    if buckets >= width:
        edges = (np.arange(width + 1) * buckets) // width
        return np.maximum.reduceat(levels, edges[:-1], axis=2)
    x = np.clip((np.arange(width) + 0.5) * buckets / width - 0.5, 0, buckets - 1)
    lo = np.floor(x).astype(np.int64)
    hi = np.minimum(lo + 1, buckets - 1)
    t = (x - lo).astype(np.float32)
    return levels[..., lo] * (1 - t) + levels[..., hi] * t


def _hex(color: str) -> np.ndarray:
    color = color.lstrip("#")
    return np.array([int(color[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def rasterize(levels: np.ndarray, width: int, height: int, *, colors: list[str], audible: list[bool] | None = None,
              mode: str = "overlay", scale: str = "db", background: str = "#15171b",
              gain: float = 1.0) -> np.ndarray:
    """Draw levels (2, channels, buckets) as ARGB32 pixels (height, width).

    Each channel has its colour: the peak envelope in a darker shade, the RMS
    body in full colour, with anti-aliased edges. "overlay" draws all channels
    in one lane, in each column the loudest behind and the quietest in front
    (so every track stays visible); "lanes" gives each channel its own lane.
    Channels that are not audible (muted / not soloed) are drawn grey.
    gain > 1 zooms the height (vertical zoom), clipping at the lane edge."""
    channels = levels.shape[1]
    width, height = max(int(width), 1), max(int(height), 1)
    audible = [True] * channels if audible is None else list(audible) + [True] * (channels - len(audible))
    amp = column_levels(levels, width)
    if scale == "linear":
        amp = np.clip(to_linear(amp) * gain, 0.0, 1.0)
    elif gain != 1.0:
        amp = np.where(amp > 0, np.clip(amp + 20 * np.log10(gain) / -FLOOR_DB, 0.0, 1.0), 0.0)
    bg = _hex(background)
    grey = np.array([120, 124, 132], np.float32)
    colour = np.stack([_hex(colors[c % len(colors)]) if audible[c] else grey * 0.8 for c in range(channels)])
    # Peaks in the full colour (a crisp outline), the RMS body a little lighter.
    edge = colour
    body = np.minimum(colour * 1.18 + 28, 255)
    image = np.empty((height, width, 3), np.float32)
    image[:] = bg

    def paint(target, ys, mid, half, peak, rms, edge_rgb, body_rgb):
        # Coverage of each pixel by the envelope, anti-aliased over one pixel only
        # (+0.5 px: a thin centre line in silence).
        distance = np.abs(ys - mid)
        for level, rgb in ((peak, edge_rgb), (rms, body_rgb)):
            cover = np.clip(level * half + 0.5 - distance, 0.0, 1.0)[..., None]
            target *= 1 - cover
            target += cover * rgb

    if mode == "lanes":
        lane = height / channels
        for c in range(channels):
            top, bottom = int(round(c * lane)), int(round((c + 1) * lane))
            if bottom <= top:
                continue
            ys = (np.arange(top, bottom, dtype=np.float32) + 0.5)[:, None]
            paint(image[top:bottom], ys, (top + bottom) / 2, max((bottom - top) / 2 - 1.5, 0.5),
                  amp[0, c][None, :], amp[1, c][None, :], edge[c], body[c])
    else:
        ys = (np.arange(height, dtype=np.float32) + 0.5)[:, None]
        # Loudest first: sort the channels per column by peak, descending.
        order = np.argsort(-amp[0], axis=0, kind="stable")
        peaks = np.take_along_axis(amp[0], order, 0)
        rmss = np.take_along_axis(amp[1], order, 0)
        for rank in range(channels):
            chosen = order[rank]
            paint(image, ys, height / 2, max(height / 2 - 2, 0.5), peaks[rank][None, :], rmss[rank][None, :],
                  edge[chosen][None, :, :], body[chosen][None, :, :])
    rgb = np.clip(image + 0.5, 0, 255).astype(np.uint32)
    return (0xFF000000 | (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]).astype(np.uint32)
