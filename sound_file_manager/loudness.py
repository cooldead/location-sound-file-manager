"""Loudness measurement (ITU-R BS.1770-4 / EBU Tech 3341-3342) and delivery
guides for the platforms people deliver to. Nothing in here uses Qt.

Measured: integrated loudness (K-weighting, 400 ms blocks with 75 % overlap,
the -70 LUFS absolute and -10 LU relative gates), true peak (4x oversampled
below 96 kHz, 2x up to 192 kHz, as BS.1770 Annex 2 asks), sample peak, the
loudness range (EBU Tech 3342: 3 s short-term values every 100 ms, gated at
-70 LUFS and -20 LU, the 10th to 95th percentile) and the highest momentary
(400 ms) and short-term (3 s) loudness.

The guides are delivery specs for a **finished mix**. Production tracks are
usually mixed in post, so for them the check is a guide (dailies, rough cuts,
content that goes out without a mix). Some platforms publish their numbers,
others don't; each target says which, with where the numbers come from and
when they were checked, because specs change.

Netflix measures *dialogue-gated* loudness (Dolby Dialogue Intelligence, a
proprietary speech detector), which this can't reproduce; it measures the
whole programme (BS.1770-4), which is Netflix's own second check: a mix that
fails dialogue-gated is only flagged if it fails the full-programme
measurement too.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

CHECKED = "2026-10-17"          # when the targets below were last compared with their sources


# ------------------------------------------------------------------ filters (shared with effects.py)

def _response(biquads: list[tuple], w: np.ndarray) -> np.ndarray:
    z1, z2 = np.exp(-1j * w), np.exp(-2j * w)
    h = np.ones(len(w), complex)
    for b, a in biquads:
        h *= (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)
    return h


def impulse_response(biquads: list[tuple], rate: int, tail: float = 1e-10, max_taps: int = 1 << 17) -> np.ndarray:
    """The biquads' impulse response, from their sampled frequency response,
    cut where the energy left is `tail` of the total."""
    size = 1 << 18 if rate <= 96000 else 1 << 19
    w = 2 * np.pi * np.arange(size // 2 + 1) / size
    h = np.fft.irfft(_response(biquads, w), size)
    energy = np.cumsum((h * h)[::-1])[::-1]
    total = energy[0]
    below = energy < total * tail
    keep = int(np.argmax(below)) if total > 0 and below.any() else size
    return h[:min(max_taps, max(64, keep))].astype(np.float64)


class Fir:
    """FFT convolution with a fixed impulse response, block after block."""

    def __init__(self, taps: np.ndarray):
        self.taps = taps
        self.tail = np.zeros(len(taps) - 1)
        self._spectra: dict[int, np.ndarray] = {}

    def process(self, x: np.ndarray) -> np.ndarray:
        n, t = len(x), len(self.taps)
        size = 1 << max(1, (n + t - 2).bit_length())
        spectrum = self._spectra.get(size)
        if spectrum is None:
            spectrum = self._spectra[size] = np.fft.rfft(self.taps, size)
        y = np.fft.irfft(np.fft.rfft(x.astype(np.float64), size) * spectrum, size)[:n + t - 1]
        y[:t - 1] += self.tail
        self.tail = y[n:].copy()
        return y[:n]


def k_weighting(rate: int) -> list[tuple]:
    """The two K-weighting biquads at any rate (the BS.1770 analogue prototype, bilinear)."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh, vb = 10 ** (gain / 20), (10 ** (gain / 20)) ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = ((vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0), \
        (1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0)
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1 + k / q + k * k
    high = (1.0, -2.0, 1.0), (1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0)
    return [shelf, high]


# ------------------------------------------------------------------ true peak

TP_TAPS = 32                    # taps per phase: within 0.01 dB up to 20 kHz at 48 kHz (24 read 20 kHz 0.13 dB high)


def oversampling(rate: int) -> int:
    return 4 if rate < 96000 else 2 if rate < 192000 else 1


def interpolator(factor: int) -> np.ndarray:
    """A windowed-sinc interpolation filter for `factor`x oversampling, odd
    length, so its delay is a whole number of input samples (TP_TAPS / 2) and
    every factor-th output is the input sample itself."""
    n = np.arange(factor * TP_TAPS + 1) - factor * TP_TAPS / 2
    return np.sinc(n / factor) * np.kaiser(len(n), 8.0)


class TruePeak:
    """The peaks between the samples as well as at them, fed block by block.

    ``intervals(x)`` gives, per input sample, the highest |value| of the
    oversampled signal from that sample to the next. They lag the input by
    ``delay`` samples (the filter's delay)."""

    def __init__(self, rate: int):
        self.factor = oversampling(rate)
        self.delay = TP_TAPS // 2 if self.factor > 1 else 0
        self.fir = Fir(interpolator(self.factor)) if self.factor > 1 else None
        self.peak = 0.0

    def intervals(self, x: np.ndarray) -> np.ndarray:
        if self.fir is None:
            out = np.abs(x)
        else:
            stuffed = np.zeros(len(x) * self.factor)
            stuffed[::self.factor] = x
            out = np.abs(self.fir.process(stuffed)).reshape(len(x), self.factor).max(axis=1)
        if len(out):
            self.peak = max(self.peak, float(out.max()))
        return out


# ------------------------------------------------------------------ the meter

def _db(power: float) -> float:
    return -0.691 + 10 * math.log10(power) if power > 0 else -math.inf


class LoudnessMeter:
    """Loudness of one programme of one or more channels (equal weights, the
    BS.1770 rule for L/R; 5.1 surround weights aren't applied), fed block by
    block as (frames,) or (frames, channels) floats."""

    def __init__(self, rate: int, channels: int = 1, true_peak: bool = True):
        self.rate = rate
        self.firs = [Fir(impulse_response(k_weighting(rate), rate)) for _ in range(channels)]
        self.quarter = round(rate * 0.1)              # 100 ms; a block is 4 of them (400 ms, 75 % overlap)
        self.left = np.zeros(0)
        self.powers: list[float] = []
        self.peak = 0.0
        self.tp = [TruePeak(rate) for _ in range(channels)] if true_peak else None

    @property
    def true_peak(self) -> float:
        """Linear; the sample peak when not measured."""
        return max(t.peak for t in self.tp) if self.tp else self.peak

    def feed(self, x: np.ndarray) -> None:
        x = np.asarray(x, np.float64)
        if x.ndim == 1:
            x = x[:, None]
        if not len(x):
            return
        self.peak = max(self.peak, float(np.abs(x).max()))
        if self.tp:
            for c, meter in enumerate(self.tp):
                meter.intervals(x[:, c])
        power = sum(fir.process(x[:, c]) ** 2 for c, fir in enumerate(self.firs))
        joined = np.concatenate([self.left, power])
        whole = len(joined) // self.quarter * self.quarter
        if whole:
            self.powers.extend(joined[:whole].reshape(-1, self.quarter).mean(axis=1).tolist())
        self.left = joined[whole:]

    def _windows(self, quarters: int) -> np.ndarray:
        q = np.asarray(self.powers)
        if len(q) < quarters:
            return np.zeros(0)
        sums = np.cumsum(np.concatenate([[0.0], q]))
        return (sums[quarters:] - sums[:-quarters]) / quarters

    def integrated(self, gain_db: float = 0.0) -> float | None:
        """Integrated loudness, or what it would be after a gain: not simply
        loudness + gain, because the -70 LUFS gate then lets other blocks in."""
        blocks = self._windows(4) * 10 ** (gain_db / 10)
        if not len(blocks):
            return None
        loud = -0.691 + 10 * np.log10(np.maximum(blocks, 1e-20))
        kept = blocks[loud > -70]
        if not len(kept):
            return None
        relative = _db(kept.mean()) - 10
        kept = blocks[(loud > -70) & (loud > relative)]
        return _db(kept.mean()) if len(kept) else None

    def lra(self) -> float | None:
        """Loudness range in LU (EBU Tech 3342)."""
        short = self._windows(30)
        if not len(short):
            return None
        loud = -0.691 + 10 * np.log10(np.maximum(short, 1e-20))
        kept = short[loud > -70]
        if len(kept) < 2:
            return None
        relative = _db(kept.mean()) - 20
        values = loud[(loud > -70) & (loud > relative)]
        if len(values) < 2:
            return None
        low, high = np.percentile(values, [10, 95])
        return float(high - low)

    def gain_to(self, target: float) -> float | None:
        """The gain that brings the integrated loudness to target (a few steps,
        since the gate moves with the gain)."""
        level = self.integrated()
        if level is None:
            return None
        gain = target - level
        for _ in range(6):
            now = self.integrated(gain)
            if now is None or abs(now - target) < 0.001:
                break
            gain += target - now
        return gain

    def max_momentary(self) -> float | None:
        blocks = self._windows(4)
        return _db(float(blocks.max())) if len(blocks) else None

    def max_short_term(self) -> float | None:
        short = self._windows(30)
        return _db(float(short.max())) if len(short) else None


def to_db(linear: float) -> float:
    return 20 * math.log10(linear) if linear > 0 else -math.inf


# ------------------------------------------------------------------ delivery targets

@dataclass(frozen=True)
class Target:
    key: str
    name: str
    integrated: float                # LUFS / LKFS
    tolerance: float | None          # ± LU; None: only louder is corrected (no fail for quieter)
    true_peak: float                 # dBTP, the highest allowed
    gating: str                      # "dialogue" or "programme": how the platform measures
    official: bool                   # the numbers are from the platform's own document
    source: str                      # where they come from
    guide: str                       # what it means, how it's checked
    lra_max: float | None = None
    group: str = "Streaming"


TARGETS: tuple[Target, ...] = (
    Target("netflix", "Netflix", -27.0, 2.0, -2.0, "dialogue", True,
           "https://partnerhelp.netflixstudios.com/hc/en-us/articles/360001794307",
           "Netflix's Sound Mix Specifications: average loudness -27 LKFS ±2 LU, dialogue-gated (ITU-R BS.1770-1 "
           "with Dolby Dialogue Intelligence), over the whole programme; peaks no higher than -2 dBTP (Netflix "
           "suggests limiters at -2.3 dBTP or lower). QC measures dialogue-gated first: a pass is not flagged. Only "
           "if that fails is the full programme measured with BS.1770-4, and the mix is flagged if that fails too. "
           "This app measures the full programme (BS.1770-4), so it gives you Netflix's second check; a mix with "
           "little dialogue can read differently dialogue-gated. Atmos mixes are measured on a 5.1 re-render."),
    Target("amazon", "Amazon Prime Video", -24.0, 2.0, -2.0, "programme", False,
           "Delivery guides (e.g. toolsforfilm.com, RTW's loudness guide); Amazon's own spec is in its partner portal",
           "-24 LKFS ±2 LU integrated (BS.1770-4), peaks no higher than -2 dBTP. Amazon turns louder content "
           "down on playback. Not published openly by Amazon: confirm with the delivery spec you were given."),
    Target("appletv", "Apple TV+", -24.0, 2.0, -1.0, "programme", False,
           "Delivery guides (e.g. toolsforfilm.com); Apple's spec is on the Apple Video Partner Hub",
           "-24 LKFS ±2 LU integrated (BS.1770-4), peaks no higher than -1 dBTP. Not published openly by Apple: "
           "confirm with the delivery spec you were given."),
    Target("youtube", "YouTube", -14.0, None, -1.0, "programme", False,
           "Community measurements (YouTube's \"Stats for nerds\" content loudness); YouTube publishes no target",
           "YouTube turns videos louder than about -14 LUFS down to it, and leaves quieter ones as they are (they "
           "just play quieter than others). Peaks at -1 dBTP or lower survive its lossy encoding. There is no "
           "fail: louder than the target only means YouTube lowers it.", group="Online"),
    Target("spotify", "Spotify (video podcasts, audio)", -14.0, None, -1.0, "programme", True,
           "https://support.spotify.com/us/artists/article/loudness-normalization/",
           "Spotify plays everything at -14 LUFS (ITU 1770): louder is turned down, quieter is raised as far as its "
           "peaks allow, keeping 1 dB of headroom. Keep true peaks below -1 dBTP, or below -2 dBTP if the mix is "
           "louder than -14 LUFS.", group="Online"),
    Target("applepodcasts", "Apple Podcasts", -16.0, 1.0, -1.0, "programme", True,
           "https://podcasters.apple.com/support/893-audio-requirements",
           "Apple's audio requirements: overall loudness around -16 LKFS with ±1 dB tolerance (ITU-R BS.1770-5), "
           "true peaks not above -1 dBFS.", group="Online"),
    Target("ebu", "EBU R128 (European broadcast)", -23.0, 0.5, -1.0, "programme", True,
           "https://tech.ebu.ch/publications/r128",
           "EBU R128: -23 LUFS integrated ±0.5 LU (±1 LU for live programmes), true peaks no higher than -1 dBTP, "
           "measured over the whole programme (EBU Tech 3341/3342). Loudness range is reported; R128 sets no "
           "limit, but broadcasters often ask for 20 LU or less (less for short-form).", group="Broadcast"),
    Target("atsc", "ATSC A/85 (US broadcast, CALM Act)", -24.0, 2.0, -2.0, "programme", True,
           "https://www.atsc.org/atsc-documents/a85-techniques-for-establishing-and-maintaining-audio-loudness-for-digital-television/",
           "ATSC A/85 (the CALM Act's reference): -24 LKFS integrated; ±2 dB is the usual tolerance; true peaks "
           "no higher than -2 dBTP.", group="Broadcast"),
)

BY_KEY = {t.key: t for t in TARGETS}
BY_NAME = {t.name: t for t in TARGETS}


@dataclass
class Measurement:
    file: str
    what: str                        # a track name, or "all ticked tracks"
    integrated: float | None
    true_peak: float                 # dBTP
    sample_peak: float               # dBFS
    lra: float | None
    max_momentary: float | None
    max_short_term: float | None
    seconds: float
    notes: list[str] = field(default_factory=list)


def from_meter(meter: LoudnessMeter, file: str, what: str, seconds: float) -> Measurement:
    return Measurement(file, what, meter.integrated(), to_db(meter.true_peak), to_db(meter.peak), meter.lra(),
                       meter.max_momentary(), meter.max_short_term(), seconds)


@dataclass
class Verdict:
    ok: bool
    lines: list[str]                 # the findings, worst first


def check(m: Measurement, target: Target) -> Verdict:
    """Whether a measurement meets a target: problems first, then notes."""
    if m.integrated is None:
        return Verdict(False, ["Too quiet to measure (below -70 LUFS)."])
    problems, notes = [], []
    diff = m.integrated - target.integrated
    if target.tolerance is None:
        if diff > 0.05:
            notes.append(f"{diff:.1f} LU louder than {target.integrated:g}: {target.name} will turn it down by "
                         "that much.")
        elif diff < -0.05:
            notes.append(f"{-diff:.1f} LU quieter than {target.integrated:g}: it will play quieter than other "
                         "content" + (" unless Spotify can raise it." if target.key == "spotify" else "."))
    elif abs(diff) > target.tolerance + 1e-9:
        problems.append(f"{abs(diff):.1f} LU too {'loud' if diff > 0 else 'quiet'} (target {target.integrated:g} "
                        f"±{target.tolerance:g}): {'lower' if diff > 0 else 'raise'} it by {abs(diff):.1f} dB.")
    if m.true_peak > target.true_peak + 1e-9:
        problems.append(f"True peak {m.true_peak:.1f} dBTP is {m.true_peak - target.true_peak:.1f} dB over the "
                        f"{target.true_peak:g} dBTP limit: use a true-peak limiter.")
    if target.lra_max is not None and m.lra is not None and m.lra > target.lra_max:
        problems.append(f"Loudness range {m.lra:.1f} LU is over {target.lra_max:g} LU.")
    if not problems and not notes:
        notes.append("Within the target.")
    if target.gating == "dialogue":
        notes.append("Measured over the whole programme (BS.1770-4); the platform's dialogue-gated reading can "
                     "differ.")
    return Verdict(not problems, problems + notes)


def _feed_file(path: str, channels: list[int], together: bool, progress, cancelled):
    """Read a file once into one meter per track, or one for the tracks together."""
    from . import processing
    with open(path, "rb") as f:
        _layout, info, data, frames = processing._open(f, os.fstat(f.fileno()).st_size)
        reader = processing._Reader(f, info, data.data_offset, frames)
        channels = [c for c in channels if c < info.channels]
        if together:
            meters = {None: LoudnessMeter(info.sample_rate, len(channels))}
        else:
            meters = {c: LoudnessMeter(info.sample_rate) for c in channels}
        for a in range(0, frames, processing.BLOCK_FRAMES):
            if cancelled and cancelled():
                raise InterruptedError
            b = min(frames, a + processing.BLOCK_FRAMES)
            block = reader(a, b).astype(np.float64)
            for c, meter in meters.items():
                meter.feed(block[:, channels] if c is None else block[:, c])
            if progress:
                progress(b, frames)
    return meters, info, frames


def measure_file(path: str, channels: list[int], names: list[str], together: bool,
                 progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> list[Measurement]:
    """Measure tracks of a file: each on its own, or all of them as one programme."""
    meters, info, frames = _feed_file(path, channels, together, progress, cancelled)
    name = os.path.basename(path)
    seconds = frames / info.sample_rate if info.sample_rate else 0.0
    out = []
    for c, meter in meters.items():
        what = "ticked tracks together" if c is None else (names[c] if c < len(names) else f"Track {c + 1}")
        out.append(from_meter(meter, name, what, seconds))
    return out


def programme_gain(path: str, channels: list[int], target: float,
                   progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> float | None:
    """The one gain that brings these tracks, measured together as one
    programme (e.g. a stereo mix), to target; None if too quiet to measure."""
    meters, _info, _frames = _feed_file(path, channels, True, progress, cancelled)
    return meters[None].gain_to(target)


def copy_suffix(target: Target) -> str:
    """The name ending for copies made for a platform: "_Netflix", "_AppleTV",
    "_EBUR128" (the name before any bracket, letters and digits only)."""
    core = re.sub(r"[^A-Za-z0-9]", "", target.name.split(" (")[0])
    return "_" + (core or target.key)
