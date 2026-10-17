"""Noise reduction for copies of recordings: de-hiss and denoise. Nothing in
here uses Qt.

Both are spectral noise reduction. The audio is cut into overlapping frames
(about 43 ms, a quarter of that apart), and each frame's spectrum is compared
with a *noise profile*: the noise's level at every frequency, learned from a
stretch of room tone the user marks, or estimated from the quietest frames of
the file. Frequencies that are close to the noise level are turned down, by
at most the chosen reduction; louder ones (the voice) pass. The gains are
smoothed over neighbouring frames and frequencies so the leftover noise
doesn't warble ("musical noise"). De-hiss does the same above a frequency
only, for the steady hiss of preamps and radio mics.

The original is never changed: a processed copy is written under a new name
(``10T03_ISO_NR.wav``), with the recorder's metadata, so it keeps its
timecode, scene and take. The new name matters: Find Duplicates takes files
with the same name and length as the same recording without reading them.

Long files are processed in blocks with margins. The frames sit on one grid
for the whole file and the smoothing only reaches a few frames, so the result
is the same, sample for sample, as processing the file in one piece.
"""

from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import bwf, card_safety, compat, splitter, waveform

MODES = ("denoise", "dehiss", "deepfilter", "plugins", "effects")  # deepfilter.py, plugins.py, effects.py
ENGINE_MODES = ("deepfilter", "plugins", "effects")  # these run through an engine object instead of a noise profile
PROCESSED_FOLDER = "Processed"
DEFAULT_SUFFIX = "_NR"
BLOCK_FRAMES = 1 << 18          # about 5.5 s at 48 kHz per block
SMOOTH_FRAMES = 2               # gain smoothing reaches this many frames each way
SMOOTH_BINS = 2                 # ... and this many frequency bins
PROFILE_FRAMES = 400            # quietest frames kept when estimating a profile
QUIET_SHARE = 0.1               # the quietest 10 % of a file count as its noise


@dataclass
class Settings:
    mode: str = "denoise"            # "denoise" (all frequencies), "dehiss" (above hiss_from_hz) or "deepfilter"
    reduction_db: float = 12.0       # the most any frequency is turned down
    threshold_db: float = 3.0        # how far above the noise level a frequency still counts as noise
    hiss_from_hz: float = 4000.0     # de-hiss: where it starts (full strength from here, fading in below)
    channels: tuple[int, ...] | None = None  # the tracks to process (0-based); None = all; others pass unchanged
    label: str = ""                  # plugins / effects: the chain, for the coding history

    def describe(self) -> str:
        if self.mode == "deepfilter":
            return f"DeepFilterNet voice cleanup, up to {self.reduction_db:g} dB"
        if self.mode == "plugins":
            return f"plug-ins: {self.label or 'chain'}"
        if self.mode == "effects":
            return f"effects: {self.label or 'chain'}"
        what = f"de-hiss from {self.hiss_from_hz / 1000:g} kHz" if self.mode == "dehiss" else "denoise"
        return f"{what}, up to {self.reduction_db:g} dB, threshold {self.threshold_db:g} dB"


def fft_size(rate: int) -> int:
    """About 43 ms of audio, as a power of two (2048 at 44.1/48 kHz)."""
    size = 256
    while size * 2 <= rate * 0.0465:
        size *= 2
    return size


def _window(n: int) -> np.ndarray:
    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)  # periodic Hann


def _margin(n: int) -> int:
    hop = n // 4
    return n + (SMOOTH_FRAMES + 2) * hop


# ------------------------------------------------------------------ profiles

@dataclass
class Profile:
    """The noise's power at each frequency bin, per channel: shape (channels, bins)."""
    power: np.ndarray
    rate: int
    source: str = ""                 # e.g. "marked region 0:01:02–0:01:05", for the dialog

    @property
    def fft(self) -> int:
        return (self.power.shape[1] - 1) * 2


def _frames_power(x: np.ndarray, n: int) -> np.ndarray:
    """Power spectra of the frames of one channel x (whole frames only): (frames, bins)."""
    hop = n // 4
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    spectra = np.fft.rfft(frames * _window(n), axis=1)
    return (spectra.real ** 2 + spectra.imag ** 2).astype(np.float32)


def learn_profile(samples: np.ndarray, rate: int, source: str = "") -> Profile:
    """The noise profile of a stretch that is only noise (room tone): the
    average power at each frequency. samples: (frames, channels) floats."""
    n = fft_size(rate)
    power = np.stack([_frames_power(samples[:, c], n).mean(axis=0) for c in range(samples.shape[1])])
    return Profile(power, rate, source)


class ProfileEstimator:
    """Estimates a noise profile from a whole file, fed block by block: the
    average spectrum of its quietest frames (QUIET_SHARE of them, at most
    PROFILE_FRAMES). Works when a take has some pauses between lines; a take
    with speech all the way through should use a marked region instead."""

    def __init__(self, rate: int, channels: int):
        self.rate, self.n = rate, fft_size(rate)
        self.kept: list[tuple[np.ndarray, np.ndarray]] = [(np.zeros(0, np.float32),
                                                           np.zeros((0, self.n // 2 + 1), np.float32))
                                                          for _ in range(channels)]
        self.total = 0

    def feed(self, samples: np.ndarray) -> None:
        if len(samples) < self.n:
            return
        self.total += (len(samples) - self.n) // (self.n // 4) + 1
        for c in range(samples.shape[1]):
            power = _frames_power(samples[:, c], self.n)
            energy, spectra = self.kept[c]
            energy = np.concatenate([energy, power.sum(axis=1)])
            spectra = np.concatenate([spectra, power])
            if len(energy) > PROFILE_FRAMES:
                keep = np.argpartition(energy, PROFILE_FRAMES)[:PROFILE_FRAMES]
                energy, spectra = energy[keep], spectra[keep]
            self.kept[c] = (energy, spectra)

    def profile(self) -> Profile:
        want = max(1, min(PROFILE_FRAMES, int(self.total * QUIET_SHARE)))
        rows = []
        for energy, spectra in self.kept:
            if not len(energy):
                rows.append(np.zeros(self.n // 2 + 1, np.float32))
                continue
            quiet = np.argsort(energy)[:want]
            rows.append(spectra[quiet].mean(axis=0))
        return Profile(np.stack(rows), self.rate, "estimated from the quietest parts of the file")


# ------------------------------------------------------------------ the reduction

def _box(a: np.ndarray, reach: int, axis: int) -> np.ndarray:
    """Moving average over 2*reach+1 values along axis (edges average what exists)."""
    if reach <= 0 or a.shape[axis] == 0:
        return a
    pad = [(0, 0)] * a.ndim
    pad[axis] = (reach, reach)
    sums = np.cumsum(np.pad(a, pad), axis=axis, dtype=np.float64)
    zero = np.zeros_like(np.take(sums, [0], axis=axis))
    sums = np.concatenate([zero, sums], axis=axis)
    width = 2 * reach + 1
    total = np.take(sums, range(width, sums.shape[axis]), axis=axis) - \
        np.take(sums, range(0, sums.shape[axis] - width), axis=axis)
    ones = np.pad(np.ones(a.shape[axis]), (reach, reach))
    count = np.convolve(ones, np.ones(width), mode="valid")
    shape = [1] * a.ndim
    shape[axis] = -1
    return (total / count.reshape(shape)).astype(np.float32)


def _weights(settings: Settings, rate: int, bins: int) -> np.ndarray:
    """How much of the reduction applies at each bin: 1 everywhere for
    denoise; for de-hiss 0 below an octave under hiss_from_hz, rising (raised
    cosine in log frequency) to 1 at hiss_from_hz."""
    if settings.mode != "dehiss":
        return np.ones(bins, np.float32)
    freqs = np.arange(bins) * rate / ((bins - 1) * 2)
    top = max(1.0, settings.hiss_from_hz)
    position = np.clip(np.log2(np.maximum(freqs, 1e-3) / (top / 2)), 0.0, 1.0)
    return (0.5 - 0.5 * np.cos(np.pi * position)).astype(np.float32)


def _gains(power: np.ndarray, noise: np.ndarray, settings: Settings, weights: np.ndarray) -> np.ndarray:
    """Gain per frame and bin, 1 = unchanged. power: (frames, bins)."""
    threshold = noise * (10 ** (settings.threshold_db / 10))
    clean = np.sqrt(np.clip(1.0 - threshold / np.maximum(power, 1e-20), 0.0, 1.0))
    clean = _box(_box(clean, SMOOTH_FRAMES, 0), SMOOTH_BINS, 1)
    floor = 10 ** (-max(0.0, settings.reduction_db) / 20)
    gains = floor + (1.0 - floor) * clean
    return 1.0 - weights * (1.0 - gains)


def _process_segment(x: np.ndarray, n: int, noise: np.ndarray, settings: Settings, weights: np.ndarray) -> np.ndarray:
    """Analyse and resynthesise one channel segment whose start is on the
    frame grid. The ends (one margin each) are not exact; callers drop them."""
    hop = n // 4
    count = (len(x) - n) // hop + 1
    window = _window(n)
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop][:count]
    spectra = np.fft.rfft(frames * window, axis=1)
    power = (spectra.real ** 2 + spectra.imag ** 2).astype(np.float32)
    spectra *= _gains(power, noise, settings, weights)
    pieces = np.fft.irfft(spectra, n, axis=1).astype(np.float32) * window
    out = np.zeros(len(x), np.float32)
    norm = np.zeros(len(x), np.float32)
    square = window * window
    # Overlap-add: frames four hops (= n) apart don't overlap, so each quarter
    # of them lies end to end and is added in one go.
    for i in range(4):
        chunk = pieces[i::4]
        if not len(chunk):
            continue
        start = i * hop
        end = min(len(x), start + len(chunk) * n)
        out[start:end] += chunk.reshape(-1)[:end - start]
        norm[start:end] += np.tile(square, len(chunk))[:end - start]
    return out / np.maximum(norm, 1e-6)


def reduce(samples: np.ndarray, profile: Profile, settings: Settings, rate: int,
           read: Callable[[int, int], np.ndarray] | None = None, start: int = 0, total: int | None = None) -> np.ndarray:
    """Process frames [start, start + len(samples)) of a recording.

    samples: (frames, channels) floats. ``read(a, b)`` returns frames [a, b)
    of the whole recording (zeros outside it) so the margins can come from
    around the block; without it the block is taken as the whole recording.
    Channels not in settings.channels are returned unchanged."""
    n = fft_size(rate)
    if profile.fft != n:
        raise ValueError("the noise profile was made at another sample rate")
    hop, margin = n // 4, _margin(n)
    length = len(samples)
    if read is None:
        whole = samples

        def read(a, b):
            out = np.zeros((b - a, samples.shape[1]), np.float32)
            lo, hi = max(a, 0), min(b, len(whole))
            if hi > lo:
                out[lo - a:hi - a] = whole[lo:hi]
            return out
    a = (start // hop) * hop - margin
    b = -(-(start + length) // hop) * hop + margin
    around = read(a, b)
    out = np.array(samples, np.float32, copy=True)
    weights = _weights(settings, rate, n // 2 + 1)
    for c in (settings.channels if settings.channels is not None else range(samples.shape[1])):
        if c >= samples.shape[1]:
            continue
        done = _process_segment(np.ascontiguousarray(around[:, c]), n, profile.power[c], settings, weights)
        out[:, c] = done[start - a:start - a + length]
    return out


# ------------------------------------------------------------------ files

def output_path(src: str, folder_mode: str = "processed", suffix: str = DEFAULT_SUFFIX) -> str:
    """Where the processed copy goes: ``<folder>/Processed/<name><suffix>.wav``
    (folder_mode "processed") or next to the original ("beside"). A name that
    is taken gets _2, _3, ..."""
    folder, name = os.path.split(src)
    stem, ext = os.path.splitext(name)
    if folder_mode == "processed":
        folder = compat.join(folder, PROCESSED_FOLDER)
    suffix = suffix or DEFAULT_SUFFIX
    candidate = compat.join(folder, f"{stem}{suffix}{ext}")
    number = 2
    while os.path.lexists(candidate):
        candidate = compat.join(folder, f"{stem}{suffix}_{number}{ext}")
        number += 1
    return candidate


def encode(samples: np.ndarray, bits: int, is_float: bool, rng: np.random.Generator | None = None) -> np.ndarray:
    """float (frames, channels) -> little-endian PCM bytes as uint8 (frames, channels, width).
    16-bit gets TPDF dither; 24/32-bit are rounded."""
    samples = np.ascontiguousarray(samples)
    frames, channels = samples.shape
    if is_float and bits == 32:
        return samples.astype("<f4").view(np.uint8).reshape(frames, channels, 4)
    if is_float and bits == 64:
        return samples.astype("<f8").view(np.uint8).reshape(frames, channels, 8)
    if bits == 16:
        rng = rng or np.random.default_rng(0)
        dither = (rng.random(samples.shape, np.float32) - rng.random(samples.shape, np.float32))
        ints = np.clip(np.round(samples.astype(np.float64) * 32768.0 + dither), -32768, 32767).astype("<i2")
        return ints.view(np.uint8).reshape(frames, channels, 2)
    if bits == 24:
        ints = np.clip(np.round(samples.astype(np.float64) * 8388608.0), -8388608, 8388607).astype("<i4")
        return ints.view(np.uint8).reshape(frames, channels, 4)[:, :, :3]
    if bits == 32:
        ints = np.clip(np.round(samples.astype(np.float64) * 2147483648.0), -2147483648, 2147483647).astype("<i4")
        return ints.view(np.uint8).reshape(frames, channels, 4)
    if bits == 8:
        ints = np.clip(np.round(samples.astype(np.float64) * 128.0 + 128.0), 0, 255).astype(np.uint8)
        return ints.reshape(frames, channels, 1)
    raise bwf.WavError(f"unsupported sample format ({bits}-bit)")


class _Reader:
    """Frames of a WAV's data chunk as floats, zeros outside it."""

    def __init__(self, f, info: bwf.WavInfo, data_offset: int, frames: int):
        self.f, self.info, self.offset, self.frames = f, info, data_offset, frames

    def raw(self, a: int, b: int) -> bytes:
        self.f.seek(self.offset + a * self.info.block_align)
        raw = self.f.read((b - a) * self.info.block_align)
        if len(raw) < (b - a) * self.info.block_align:
            raise bwf.WavError("the file ended early")
        return raw

    def __call__(self, a: int, b: int) -> np.ndarray:
        info = self.info
        out = np.zeros((b - a, info.channels), np.float32)
        lo, hi = max(a, 0), min(b, self.frames)
        if hi > lo:
            out[lo - a:hi - a] = waveform.decode(self.raw(lo, hi), info.bits, info.channels, info.format_tag == 3)
        return out


def _open(f, size: int):
    layout = bwf.read_layout(f, size)
    info = bwf._info_from(f, layout)
    if info.format_tag not in (1, 3):
        raise bwf.WavError(f"format {info.format_tag} isn't supported")
    data = layout.first(b"data")
    if data is None or data.truncated or info.block_align != info.channels * (info.bits // 8):
        raise bwf.WavError("the audio can't be read for processing")
    return layout, info, data, data.size // info.block_align


def estimate_file_profile(path: str, progress: Callable[[int, int], None] | None = None,
                          cancelled: Callable[[], bool] | None = None) -> Profile:
    """A noise profile from a whole file's quietest frames (see ProfileEstimator)."""
    with open(path, "rb") as f:
        _layout, info, data, frames = _open(f, os.fstat(f.fileno()).st_size)
        reader = _Reader(f, info, data.data_offset, frames)
        estimator = ProfileEstimator(info.sample_rate, info.channels)
        for a in range(0, frames, BLOCK_FRAMES):
            if cancelled and cancelled():
                raise InterruptedError
            estimator.feed(reader(a, min(frames, a + BLOCK_FRAMES)))
            if progress:
                progress(min(frames, a + BLOCK_FRAMES), frames)
    return estimator.profile()


def region_profile(path: str, first: int, end: int) -> Profile:
    """A noise profile from frames [first, end) of a file (a marked stretch of room tone)."""
    with open(path, "rb") as f:
        _layout, info, data, frames = _open(f, os.fstat(f.fileno()).st_size)
        first, end = max(0, first), min(frames, end)
        if end - first < fft_size(info.sample_rate):
            raise ValueError("the marked region is too short to learn the noise from (mark at least half a second)")
        samples = _Reader(f, info, data.data_offset, frames)(first, end)
    return learn_profile(samples, info.sample_rate)


def read_frames(path: str, first: int, end: int) -> tuple[np.ndarray, int]:
    """Frames [first, end) of a file as floats, and its sample rate (for previews)."""
    with open(path, "rb") as f:
        _layout, info, data, frames = _open(f, os.fstat(f.fileno()).st_size)
        return _Reader(f, info, data.data_offset, frames)(max(0, first), min(frames, end)), info.sample_rate


def _engine_block(engine, reader, samples: np.ndarray, a: int, b: int, channels) -> np.ndarray:
    out = np.array(samples, np.float32, copy=True)
    for c in channels:
        out[:, c] = engine.process(reader, a, b, c)
    return out


def _check_engine(settings: Settings, engine, rate: int) -> None:
    if settings.mode == "plugins" and engine is None:
        raise ValueError("no plug-in chain to run")
    if settings.mode == "effects" and engine is None:
        raise ValueError("no effects to run")
    if settings.mode == "deepfilter":
        if engine is None:
            raise ValueError("the DeepFilterNet add-on isn't installed")
        if rate != 48000:
            raise ValueError(f"DeepFilterNet only works on 48 kHz recordings (this one is {rate:,} Hz)")


def preview(path: str, first: int, end: int, profile: Profile | None, settings: Settings,
            engine=None) -> tuple[np.ndarray, np.ndarray, int]:
    """The original and processed frames [first, end) of a file, and its rate.
    engine: a deepfilter.Engine for settings.mode "deepfilter"."""
    with open(path, "rb") as f:
        _layout, info, data, frames = _open(f, os.fstat(f.fileno()).st_size)
        reader = _Reader(f, info, data.data_offset, frames)
        first, end = max(0, first), min(frames, end)
        original = reader(first, end)
        _check_engine(settings, engine, info.sample_rate)
        if settings.mode in ENGINE_MODES:
            channels = [c for c in (settings.channels if settings.channels is not None else range(info.channels))
                        if c < info.channels]
            if settings.mode == "effects":  # loudness: measured on the preview's stretch only
                engine.prepare(reader, channels, first, end)
            return original, _engine_block(engine, reader, original, first, end, channels), info.sample_rate
        return original, reduce(original, profile, settings, info.sample_rate, reader, first), info.sample_rate


def processed_ixml(old_payload: bytes | None, name: str, parent: str) -> bytes:
    """The original's iXML for the processed copy: a new FILE_UID and a
    HISTORY naming the file it came from; take, tracks and file set unchanged."""
    root = bwf._ixml_root(old_payload) if old_payload else None
    if root is None:
        root = ET.Element("BWFXML")
        ET.SubElement(root, "IXML_VERSION").text = "1.61"
    parent_uid = (root.findtext("FILE_UID") or "").strip()
    splitter._set(root, "FILE_UID", uuid.uuid4().hex.upper())
    history = root.find("HISTORY")
    if history is None:
        history = ET.SubElement(root, "HISTORY")
    if history.find("ORIGINAL_FILENAME") is None:
        splitter._set(history, "ORIGINAL_FILENAME", parent)
    splitter._set(history, "CURRENT_FILENAME", name)
    splitter._set(history, "PARENT_FILENAME", parent)
    if parent_uid:
        splitter._set(history, "PARENT_UID", parent_uid)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8") + \
        b" " * bwf.REWRITE_IXML_HEADROOM


def processed_bext(old: bytes, name: str, info: bwf.WavInfo, settings: Settings) -> bytes:
    """The bext with the file name updated and a coding history line that
    says what was done (post can see the copy is processed)."""
    picks = tuple(range(info.channels))
    payload = splitter.track_bext(old, picks, name)
    fixed = payload[:602].ljust(602, b"\0")
    history = bwf._cstr(payload[602:]).rstrip("\r\n")
    mode = "mono" if info.channels == 1 else "multitrack"
    line = (f"A=PCM,F={info.sample_rate},W={info.bits},M={mode},"
            f"T=Location Sound File Manager {settings.describe()}")
    history = (history + "\r\n" if history else "") + line + "\r\n"
    return fixed + history.encode("latin-1", "replace")


def process_file(src: str, dst: str, profile: Profile | None, settings: Settings,
                 progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, engine=None) -> str:
    """Write a processed copy of src to dst (which must not exist): same
    format, length and metadata. Written under a temporary name, checked,
    then renamed; on any error nothing is left behind. Tracks that aren't
    processed are copied byte for byte. For settings.mode "deepfilter" pass a
    deepfilter.Engine (no profile needed); the blocks then follow its size."""
    card_safety.assert_writable(dst)
    if os.path.lexists(dst):
        raise bwf.WavError(f"{os.path.basename(dst)} already exists")
    with open(src, "rb") as f:
        layout, info, data, frames = _open(f, os.fstat(f.fileno()).st_size)
        _check_engine(settings, engine, info.sample_rate)
        if settings.mode not in ENGINE_MODES and (profile is None or profile.rate != info.sample_rate
                                              or profile.power.shape[0] != info.channels):
            raise ValueError("the noise profile doesn't fit this file (another sample rate or track count)")
        chunks = {}
        for chunk in layout.chunks:
            if chunk.id in (b"bext", b"iXML") + splitter.COPIED_CHUNKS and chunk.id not in chunks and not chunk.truncated:
                f.seek(chunk.data_offset)
                chunks[chunk.id] = f.read(chunk.size)
        name, parent = os.path.basename(dst), os.path.basename(src)
        meta = []
        if b"bext" in chunks and len(chunks[b"bext"]) >= 346:
            meta.append(bwf._chunk_bytes(b"bext", processed_bext(chunks[b"bext"], name, info, settings)))
        meta.append(bwf._chunk_bytes(b"iXML", processed_ixml(chunks.get(b"iXML"), name, parent)))
        copied = [bwf._chunk_bytes(cid, chunks[cid]) for cid in splitter.COPIED_CHUNKS if cid in chunks]
        header = splitter._wav_header(info.format_tag, info.channels, info.sample_rate, info.bits, frames, meta, copied)
        reader = _Reader(f, info, data.data_offset, frames)
        width = info.bits // 8
        picked = [c for c in (settings.channels if settings.channels is not None else range(info.channels))
                  if c < info.channels]
        rng = np.random.default_rng(0)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        temp = compat.join(os.path.dirname(dst), f".{name}.part")
        measuring = settings.mode == "effects" and picked and engine.needs_measuring()
        if measuring and progress:  # the loudness pass is the first half of the bar, writing the second
            outer, progress = progress, lambda d, t: outer(frames // 2 + d // 2, frames)
        try:
            if measuring:
                engine.prepare(reader, picked, progress=(lambda d, t: outer(d // 2, frames)) if progress else None)
            with open(temp, "wb") as out:
                out.write(header)
                step = engine.block_frames if settings.mode in ENGINE_MODES else BLOCK_FRAMES
                for a in range(0, frames, step):
                    if cancelled and cancelled():
                        raise InterruptedError
                    b = min(frames, a + step)
                    raw = reader.raw(a, b)
                    block = np.frombuffer(raw, np.uint8).reshape(b - a, info.channels, width).copy()
                    if picked:
                        samples = waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)
                        if settings.mode in ENGINE_MODES:
                            done = _engine_block(engine, reader, samples, a, b, picked)
                        else:
                            done = reduce(samples, profile, settings, info.sample_rate, reader, a)
                        coded = encode(done[:, picked], info.bits, info.format_tag == 3, rng)
                        block[:, picked, :] = coded
                    out.write(block.tobytes())
                    if progress:
                        progress(b, frames)
                if (frames * info.block_align) & 1:
                    out.write(b"\0")
            check = bwf.read_info(temp)
            if check.channels != info.channels or check.frames != frames or check.sample_rate != info.sample_rate:
                raise bwf.WavError(f"{name} did not check out after writing")
            if os.path.lexists(dst):
                raise bwf.WavError(f"{name} appeared while processing")
            os.replace(temp, dst)
            return dst
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise


def reduction_db(original: np.ndarray, processed: np.ndarray) -> float:
    """How much quieter the processed audio is overall, in dB (for the dialog)."""
    a = float(np.mean(np.square(original, dtype=np.float64)))
    b = float(np.mean(np.square(processed, dtype=np.float64)))
    if a <= 0 or b <= 0:
        return 0.0
    return 10 * np.log10(a / b)

