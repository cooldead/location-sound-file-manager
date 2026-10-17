"""Built-in effects for copies of recordings, in a chain the user orders: a
filter, an equalizer, a gate, an expander, a compressor, a de-esser, a
limiter, loudness normalisation and gain / delay / polarity. Nothing in here
uses Qt.

The rack is modelled on Easy Effects (a PipeWire effects app for Linux), and
its presets can be imported (``import_easyeffects``), but no code comes from
it: Easy Effects is GPL-3.0 and runs other projects' LV2 plug-ins, while this
is our own numpy DSP, so the app stays MIT and needs nothing installed.

How the effects work, briefly:

* **Filter and equalizer** are the usual analogue-style biquads (Butterworth
  slopes, RBJ bells and shelves). numpy has no recursive filter, so each
  effect's exact complex frequency response is sampled finely and turned into
  its impulse response (an inverse FFT), cut where the tail is 100 dB down,
  and applied by FFT convolution. That *is* the IIR filter (minimum phase, no
  latency, no pre-ringing), only computed another way.
* **Dynamics** (gate, expander, compressor, de-esser) measure the level every
  ~0.7 ms (a "step"), run the attack/release smoothing of the gain at that
  rate in plain Python, and ramp the gain linearly between steps.
* **The limiter** looks 5 ms ahead (the audio is delayed by that much and the
  delay is taken off again): the gain is the running minimum of what each
  sample needs, then averaged over the look-ahead, so it reaches the needed
  gain before the peak and no sample comes out above the ceiling.
* **Loudness** normalises each track to a target loudness (EBU R128 /
  ITU-R BS.1770: K-weighting, 400 ms blocks, absolute and relative gates). It
  needs the loudness of everything before it in the chain, so the engine
  measures the whole track first (``Engine.prepare``), then processes it.

The original is never changed: ``processing.process_file`` writes a copy
(``Processed/10T03_ISO_FX.wav``) with the recorder's metadata.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import loudness
from .loudness import Fir as _Fir, LoudnessMeter, _response, k_weighting  # noqa: F401 (k_weighting: re-exported)

DEFAULT_SUFFIX = "_FX"
FEED = 1 << 16                  # frames run through a chain at a time (a multiple of every step size)
PREROLL_SECONDS = 2.0           # audio run through before a preview's start, so the dynamics have settled
IR_TAIL = 1e-10                 # a filter's impulse response is cut where the energy left is this small (-100 dB)
MAX_TAPS = 1 << 17


def impulse_response(biquads: list[tuple], rate: int) -> np.ndarray:
    return loudness.impulse_response(biquads, rate, IR_TAIL, MAX_TAPS)
EQ_BANDS = 32
F32 = np.float32


# ------------------------------------------------------------------ descriptions of the effects

@dataclass(frozen=True)
class Spec:
    """One setting of an effect, for the dialog and for checking loaded values."""
    key: str
    label: str
    kind: str = "float"              # "float", "choice", "bool" or "bands" (the equalizer's band list)
    default: object = 0.0
    low: float = 0.0
    high: float = 1.0
    step: float = 1.0
    decimals: int = 1
    unit: str = ""
    choices: tuple[str, ...] = ()
    tip: str = ""
    log: bool = False                # a frequency: the slider moves in octaves


@dataclass(frozen=True)
class EffectType:
    key: str
    label: str
    specs: tuple[Spec, ...]
    about: str
    linear: bool = False             # a fixed filter (has a frequency response to draw)


DETECTION = ("RMS", "Peak")
FILTER_KINDS = ("High-pass", "Low-pass", "Band-pass", "Band-reject")
SLOPES = ("12 dB/oct", "24 dB/oct", "36 dB/oct", "48 dB/oct")
BAND_TYPES = ("Off", "Bell", "Low shelf", "High shelf", "Notch", "High-pass", "Low-pass")


def _freq(key, label, default, low=10.0, high=24000.0, tip=""):
    return Spec(key, label, "float", default, low, high, 1.0, 0, " Hz", tip=tip, log=True)


def _ms(key, label, default, low, high, tip="", decimals=1):
    return Spec(key, label, "float", default, low, high, 1.0 if high > 50 else 0.1, decimals, " ms", tip=tip)


def _db(key, label, default, low, high, tip="", unit=" dB"):
    return Spec(key, label, "float", default, low, high, 0.5, 1, unit, tip=tip)


TYPES: dict[str, EffectType] = {t.key: t for t in (
    EffectType("filter", "Filter", (
        Spec("kind", "Type", "choice", "High-pass", choices=FILTER_KINDS,
             tip="High-pass removes rumble, wind and handling noise below the frequency; low-pass removes "
                 "hiss and whine above it."),
        _freq("freq", "Frequency", 80.0, tip="Where the filter starts (-3 dB)."),
        Spec("slope", "Slope", "choice", "24 dB/oct", choices=SLOPES,
             tip="How steeply it cuts past the frequency (high- and low-pass)."),
        Spec("q", "Q (band filters)", "float", 0.707, 0.1, 30.0, 0.1, 2,
             tip="Band-pass and band-reject only: higher is narrower."),
    ), "Cuts frequencies above or below a point. A high-pass at 60–100 Hz is the usual first step for "
       "dialogue.", linear=True),
    EffectType("equalizer", "Equalizer", (
        Spec("bands", "Bands", "bands", [{"type": "Bell", "freq": 1000.0, "gain": 0.0, "q": 1.0}]),
        _db("output", "Output", 0.0, -36, 36),
    ), f"A parametric equalizer with up to {EQ_BANDS} bands: bells, shelves, notches (e.g. 50/60 Hz hum) and "
       "filters.", linear=True),
    EffectType("gate", "Gate", (
        _db("threshold", "Opens above", -50.0, -90, 0, unit=" dBFS"),
        _db("hysteresis", "Closes this far below", 6.0, 0, 30,
            tip="So the gate doesn't flutter when the level sits near the threshold."),
        _db("range", "Turns down by", 20.0, 0, 90, tip="How far the gate lowers the sound while closed."),
        _ms("attack", "Attack (opening)", 1.0, 0.1, 100),
        _ms("hold", "Hold", 80.0, 0, 2000, decimals=0),
        _ms("release", "Release (closing)", 150.0, 1, 5000, decimals=0),
        Spec("detection", "Level", "choice", "Peak", choices=DETECTION),
    ), "Lowers the sound between lines, when the level drops below the threshold."),
    EffectType("expander", "Expander", (
        _db("threshold", "Threshold", -40.0, -90, 0, unit=" dBFS"),
        Spec("ratio", "Ratio", "float", 2.0, 1.0, 20.0, 0.1, 1, ":1",
             tip="Below the threshold, each dB quieter comes out this many dB quieter."),
        _db("range", "At most", 15.0, 0, 90, tip="The most the expander turns down."),
        _db("knee", "Knee", 6.0, 0, 24),
        _ms("attack", "Attack (opening)", 2.0, 0.1, 200),
        _ms("release", "Release (closing)", 100.0, 1, 5000, decimals=0),
        Spec("detection", "Level", "choice", "RMS", choices=DETECTION),
    ), "A gentler gate: quiet parts get quieter by a ratio, so room tone between lines drops without the "
       "gate's on/off sound."),
    EffectType("compressor", "Compressor", (
        _db("threshold", "Threshold", -20.0, -60, 0, unit=" dBFS"),
        Spec("ratio", "Ratio", "float", 3.0, 1.0, 20.0, 0.1, 1, ":1"),
        _db("knee", "Knee", 6.0, 0, 24),
        _ms("attack", "Attack", 10.0, 0.1, 500),
        _ms("release", "Release", 150.0, 1, 5000, decimals=0),
        _db("makeup", "Makeup gain", 0.0, -24, 36),
        Spec("detection", "Level", "choice", "RMS", choices=DETECTION),
    ), "Evens out loud and soft lines: above the threshold the level rises by 1/ratio."),
    EffectType("deesser", "De-esser", (
        _freq("freq", "Above", 5500.0, 1000, 16000, tip="Sibilance (s, sh, t) is usually 4–9 kHz."),
        _db("threshold", "Threshold", -30.0, -60, 0, unit=" dBFS"),
        Spec("ratio", "Ratio", "float", 4.0, 1.0, 20.0, 0.5, 1, ":1"),
        _db("max_reduction", "At most", 10.0, 0, 30),
        _ms("release", "Release", 60.0, 5, 1000, decimals=0),
        Spec("mode", "Turn down", "choice", "Split", choices=("Split", "Wide"),
             tip="Split lowers only the frequencies above; Wide lowers the whole sound."),
    ), "Tames harsh s and sh sounds by turning down the high frequencies while they are loud."),
    EffectType("limiter", "Limiter", (
        _db("ceiling", "Ceiling", -1.0, -30, 0, unit=" dBTP", tip="Nothing comes out above this."),
        Spec("true_peak", "True peak (also between the samples)", "bool", True,
             tip="Catches the peaks a DAC or a lossy encoder makes between the samples (4x oversampled, as "
                 "BS.1770 measures). Delivery specs give their limits in dBTP; leave this on for them."),
        _ms("lookahead", "Look-ahead", 5.0, 0.5, 20),
        _ms("release", "Release", 80.0, 5, 2000, decimals=0),
    ), "Stops peaks from going over a ceiling; it sees each peak coming, so nothing gets through."),
    EffectType("loudness", "Loudness", (
        Spec("platform", "Platform", "choice", "Custom", choices=("Custom",) + tuple(t.name for t in loudness.TARGETS),
             tip="Takes the target and the peak ceiling from a platform's delivery spec (see Loudness Check for "
                 "each one's guide)."),
        Spec("target", "Target", "float", -23.0, -40.0, -5.0, 0.5, 1, " LUFS",
             tip="EBU R128: -23 LUFS; ATSC A/85: -24 LKFS; Netflix -27 LKFS; online video is often -16 to -14."),
        _db("max_gain", "Raise by at most", 20.0, 0, 60),
        Spec("peak_limit", "Keep true peaks below the ceiling", "bool", True,
             tip="Raise less when the true peaks would go over; the report says when the target wasn't reached. "
                 "Untick when a limiter follows (it takes care of the peaks)."),
        _db("ceiling", "Peak ceiling", -1.0, -30, 0, unit=" dBTP"),
    ), "Sets each track's loudness (ITU-R BS.1770, the measure behind EBU R128 / LUFS) to a target, with one "
       "gain for the whole track. Each track is measured over the whole file first."),
    EffectType("gain", "Gain / Delay", (
        _db("gain", "Gain", 0.0, -60, 36),
        Spec("invert", "Invert polarity", "bool", False),
        Spec("delay", "Delay", "float", 0.0, 0.0, 1000.0, 0.1, 2, " ms",
             tip="Moves the track later, e.g. to line a boom up with a lav (sound travels ~0.34 m per ms)."),
    ), "Changes the level, flips the polarity or delays the track."),
)}


def clamp_params(kind: str, params: dict) -> dict:
    """The effect's settings with defaults filled in and values held to their ranges."""
    out = {}
    for spec in TYPES[kind].specs:
        value = params.get(spec.key, spec.default)
        if spec.kind == "float":
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(spec.default)
            if not math.isfinite(value):
                value = float(spec.default)
            value = min(spec.high, max(spec.low, value))
        elif spec.kind == "choice":
            value = value if value in spec.choices else spec.default
        elif spec.kind == "bool":
            value = bool(value)
        elif spec.kind == "bands":
            bands = []
            for band in (value if isinstance(value, list) else [])[:EQ_BANDS]:
                if not isinstance(band, dict):
                    continue
                bands.append({"type": band.get("type") if band.get("type") in BAND_TYPES else "Bell",
                              "freq": min(24000.0, max(10.0, _num(band.get("freq"), 1000.0))),
                              "gain": min(36.0, max(-36.0, _num(band.get("gain"), 0.0))),
                              "q": min(100.0, max(0.05, _num(band.get("q"), 1.0)))})
            value = bands
        out[spec.key] = value
    return out


def apply_platform(params: dict) -> dict:
    """A Loudness effect's settings with the target and ceiling of its platform."""
    target = loudness.BY_NAME.get(params.get("platform"))
    if target is not None:
        params = dict(params, target=target.integrated, ceiling=target.true_peak)
    return params


def _num(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


@dataclass
class Effect:
    """One entry in the chain."""
    kind: str
    params: dict = field(default_factory=dict)
    bypass: bool = False

    def __post_init__(self):
        if self.kind not in TYPES:
            raise ValueError(f"unknown effect “{self.kind}”")
        self.params = clamp_params(self.kind, self.params)
        if self.kind == "loudness":
            self.params = apply_platform(self.params)

    @property
    def label(self) -> str:
        return TYPES[self.kind].label

    def to_json(self) -> dict:
        return {"type": self.kind, "bypass": self.bypass, "params": copy.deepcopy(self.params)}

    @classmethod
    def from_json(cls, data: dict) -> "Effect":
        return cls(str(data.get("type")), dict(data.get("params") or {}), bool(data.get("bypass", False)))

    def describe(self) -> str:
        p = self.params
        if self.kind == "filter":
            if p["kind"] in ("High-pass", "Low-pass"):
                return f"{p['kind']} {p['freq']:g} Hz {p['slope']}"
            return f"{p['kind']} {p['freq']:g} Hz Q {p['q']:g}"
        if self.kind == "equalizer":
            on = [b for b in p["bands"] if b["type"] != "Off"]
            return f"EQ, {len(on)} band{'s' if len(on) != 1 else ''}"
        if self.kind == "gate":
            return f"Gate {p['threshold']:g} dBFS, -{p['range']:g} dB"
        if self.kind == "expander":
            return f"Expander {p['threshold']:g} dBFS {p['ratio']:g}:1"
        if self.kind == "compressor":
            return f"Compressor {p['threshold']:g} dBFS {p['ratio']:g}:1"
        if self.kind == "deesser":
            return f"De-esser {p['freq'] / 1000:g} kHz {p['threshold']:g} dBFS"
        if self.kind == "limiter":
            return f"Limiter {p['ceiling']:g} {'dBTP' if p['true_peak'] else 'dBFS'}"
        if self.kind == "loudness":
            if p["platform"] != "Custom":
                return f"Loudness {p['target']:g} LUFS ({p['platform']})"
            return f"Loudness {p['target']:g} LUFS"
        parts = []
        if p["gain"]:
            parts.append(f"{p['gain']:+g} dB")
        if p["invert"]:
            parts.append("polarity inverted")
        if p["delay"]:
            parts.append(f"delay {p['delay']:g} ms")
        return "Gain " + (", ".join(parts) or "0 dB")


def new_effect(kind: str) -> Effect:
    return Effect(kind)


def chain_to_json(chain: list[Effect]) -> str:
    return json.dumps([e.to_json() for e in chain])


def chain_from_json(text: str) -> list[Effect]:
    try:
        data = json.loads(text) if text else []
    except ValueError:
        return []
    chain = []
    for item in data if isinstance(data, list) else []:
        try:
            chain.append(Effect.from_json(item))
        except (ValueError, TypeError, AttributeError):
            continue
    return chain


def describe_chain(chain: list[Effect]) -> str:
    on = [e.describe() for e in chain if not e.bypass]
    return " → ".join(on) if on else "no effects"


def chain_problems(chain: list[Effect]) -> list[str]:
    problems = []
    if not [e for e in chain if not e.bypass]:
        problems.append("Add at least one effect (or switch one on).")
    if sum(1 for e in chain if e.kind == "loudness" and not e.bypass) > 1:
        problems.append("Use one Loudness effect at most (each track gets one gain).")
    return problems


# ------------------------------------------------------------------ filters

def biquad(kind: str, freq: float, rate: int, gain_db: float = 0.0, q: float = 0.7071) -> tuple:
    """RBJ "Audio EQ Cookbook" biquad: (b0, b1, b2), (a0, a1, a2)."""
    freq = min(max(freq, 1.0), rate * 0.49)
    w0 = 2 * math.pi * freq / rate
    cos, sin = math.cos(w0), math.sin(w0)
    q = max(q, 1e-3)
    alpha = sin / (2 * q)
    a = 10 ** (gain_db / 40)
    if kind == "lowpass":
        return ((1 - cos) / 2, 1 - cos, (1 - cos) / 2), (1 + alpha, -2 * cos, 1 - alpha)
    if kind == "highpass":
        return ((1 + cos) / 2, -(1 + cos), (1 + cos) / 2), (1 + alpha, -2 * cos, 1 - alpha)
    if kind == "bandpass":
        return (alpha, 0.0, -alpha), (1 + alpha, -2 * cos, 1 - alpha)
    if kind == "notch":
        return (1.0, -2 * cos, 1.0), (1 + alpha, -2 * cos, 1 - alpha)
    if kind == "bell":
        return (1 + alpha * a, -2 * cos, 1 - alpha * a), (1 + alpha / a, -2 * cos, 1 - alpha / a)
    root = 2 * math.sqrt(a) * alpha
    if kind == "lowshelf":
        return ((a * ((a + 1) - (a - 1) * cos + root), 2 * a * ((a - 1) - (a + 1) * cos),
                 a * ((a + 1) - (a - 1) * cos - root)),
                ((a + 1) + (a - 1) * cos + root, -2 * ((a - 1) + (a + 1) * cos), (a + 1) + (a - 1) * cos - root))
    if kind == "highshelf":
        return ((a * ((a + 1) + (a - 1) * cos + root), -2 * a * ((a - 1) + (a + 1) * cos),
                 a * ((a + 1) + (a - 1) * cos - root)),
                ((a + 1) - (a - 1) * cos + root, 2 * ((a - 1) - (a + 1) * cos), (a + 1) - (a - 1) * cos - root))
    raise ValueError(kind)


def butterworth_qs(order: int) -> list[float]:
    """The Q of each second-order section of an even-order Butterworth filter."""
    return [1 / (2 * math.cos(math.pi * (2 * i - 1) / (2 * order))) for i in range(1, order // 2 + 1)]


def sections(effect: Effect, rate: int) -> list[tuple]:
    """The biquads of a filter or equalizer effect."""
    p = effect.params
    if effect.kind == "filter":
        if p["kind"] in ("High-pass", "Low-pass"):
            order = 2 * (SLOPES.index(p["slope"]) + 1)
            kind = "highpass" if p["kind"] == "High-pass" else "lowpass"
            return [biquad(kind, p["freq"], rate, q=q) for q in butterworth_qs(order)]
        return [biquad("bandpass" if p["kind"] == "Band-pass" else "notch", p["freq"], rate, q=p["q"])]
    if effect.kind == "equalizer":
        names = {"Bell": "bell", "Low shelf": "lowshelf", "High shelf": "highshelf", "Notch": "notch",
                 "High-pass": "highpass", "Low-pass": "lowpass"}
        out = []
        for band in p["bands"]:
            if band["type"] == "Off" or (band["type"] in ("Bell", "Low shelf", "High shelf") and not band["gain"]):
                continue
            out.append(biquad(names[band["type"]], band["freq"], rate, band["gain"], band["q"]))
        return out
    return []


def response_db(effect: Effect, rate: int, freqs: np.ndarray) -> np.ndarray:
    """The effect's gain at each frequency in dB (for the dialog's graph)."""
    h = _response(sections(effect, rate), 2 * np.pi * np.minimum(freqs, rate / 2) / rate)
    db = 20 * np.log10(np.maximum(np.abs(h), 1e-9))
    if effect.kind == "equalizer":
        db += effect.params["output"]
    return db


def _db_to_gain(db):
    return np.power(10.0, np.asarray(db, np.float64) / 20.0)


def _gain_db(value: float) -> float:
    return 20 * math.log10(max(value, 1e-12))


# ------------------------------------------------------------------ processors

class Processor:
    latency = 0                      # frames by which the output lags (the engine takes it off)

    def process(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def summary(self) -> str:
        return ""


class LinearProcessor(Processor):
    def __init__(self, effect: Effect, rate: int):
        biquads = sections(effect, rate)
        self.gain = float(_db_to_gain(effect.params.get("output", 0.0)))
        self.fir = _Fir(impulse_response(biquads, rate)) if biquads else None

    def process(self, x):
        y = self.fir.process(x) if self.fir is not None else x.astype(np.float64)
        return y * self.gain if self.gain != 1.0 else y


def step_frames(rate: int) -> int:
    """The dynamics' measuring step: a power of two near 0.67 ms (32 at 44.1/48 kHz)."""
    return 1 << max(3, round(math.log2(rate / 1500)))


def _coef(ms: float, step_seconds: float) -> float:
    """The one-pole smoothing factor per step for a time constant in ms."""
    return math.exp(-step_seconds / (ms / 1000)) if ms > 0 else 0.0


class _Detector:
    """The level in dBFS per step: the step's peak, or RMS over ~10 ms."""

    def __init__(self, mode: str, rate: int, step: int):
        self.step, self.rms = step, mode == "RMS"
        self.width = max(1, round(0.010 * rate / step))
        self.history = np.zeros(self.width - 1)

    def levels(self, x: np.ndarray) -> np.ndarray:
        steps = x.reshape(-1, self.step)
        if not self.rms:
            return 20 * np.log10(np.maximum(np.abs(steps).max(axis=1), 1e-10))
        power = np.concatenate([self.history, (steps * steps).mean(axis=1)])
        sums = np.cumsum(np.concatenate([[0.0], power]))
        mean = (sums[self.width:] - sums[:-self.width]) / self.width
        self.history = power[len(power) - (self.width - 1):] if self.width > 1 else self.history
        # 10*log10 of a mean square; a full-scale sine reads -3 dBFS, as on most meters.
        return 10 * np.log10(np.maximum(mean, 1e-20))


class _Ramp:
    """Turns per-step gains (dB) into per-sample gains, ramping from one step to the next."""

    def __init__(self, step: int):
        self.step, self.last = step, 0.0
        self.frac = (np.arange(step) + 1) / step

    def samples(self, gains_db: np.ndarray) -> np.ndarray:
        g = _db_to_gain(gains_db)
        before = np.concatenate([[float(_db_to_gain(self.last))], g[:-1]])
        self.last = float(gains_db[-1]) if len(gains_db) else self.last
        return (before[:, None] + (g - before)[:, None] * self.frac).reshape(-1)


class DynamicsProcessor(Processor):
    """Gate, expander and compressor: a gain worked out per step from the level."""

    def __init__(self, effect: Effect, rate: int):
        self.kind, self.p = effect.kind, effect.params
        self.step = step_frames(rate)
        seconds = self.step / rate
        self.detector = _Detector(self.p["detection"], rate, self.step)
        self.ramp = _Ramp(self.step)
        self.attack = _coef(self.p["attack"], seconds)
        self.release = _coef(self.p["release"], seconds)
        self.hold_steps = round(self.p.get("hold", 0.0) / 1000 / seconds)
        self.gain = 0.0 if self.kind == "compressor" else -self.p["range"]   # dB now (gates start closed)
        self.open, self.hold_left = False, 0
        self.most = 0.0              # the most reduction, for the summary
        self.reduced_steps = self.steps = 0

    def _targets(self, levels: np.ndarray) -> list[float]:
        p = self.p
        if self.kind == "compressor":
            over = levels - p["threshold"]
            knee, slope = p["knee"], 1 / p["ratio"] - 1
            target = np.where(over <= -knee / 2, 0.0, slope * over)
            if knee > 0:
                inside = np.abs(over) < knee / 2
                target = np.where(inside, slope * (over + knee / 2) ** 2 / (2 * knee), target)
            return target.tolist()
        if self.kind == "expander":
            under = levels - p["threshold"]
            knee, slope = p["knee"], p["ratio"] - 1
            target = np.where(under >= knee / 2, 0.0, slope * under)
            if knee > 0:
                inside = np.abs(under) < knee / 2
                target = np.where(inside, slope * (under - knee / 2) ** 2 / (-2 * knee), target)
            return np.maximum(target, -p["range"]).tolist()
        # gate: open above the threshold, closed below threshold - hysteresis (after the hold)
        out = []
        opens, closes, shut = p["threshold"], p["threshold"] - p["hysteresis"], -p["range"]
        is_open, hold = self.open, self.hold_left
        for level in levels.tolist():
            if level >= opens:
                is_open, hold = True, self.hold_steps
            elif level < closes and is_open:
                if hold > 0:
                    hold -= 1
                else:
                    is_open = False
            out.append(0.0 if is_open else shut)
        self.open, self.hold_left = is_open, hold
        return out

    def gains(self, x: np.ndarray) -> np.ndarray:
        """Per-step gains in dB for block x (its length is a multiple of the step)."""
        targets = self._targets(self.detector.levels(x))
        out = []
        gain, attack, release = self.gain, self.attack, self.release
        if self.kind == "compressor":
            for target in targets:   # attack = more reduction
                gain = target + (gain - target) * (attack if target < gain else release)
                out.append(gain)
        else:
            for target in targets:   # attack = opening
                gain = target + (gain - target) * (attack if target > gain else release)
                out.append(gain)
        self.gain = gain
        out = np.asarray(out)
        if len(out):
            self.most = min(self.most, float(out.min()))
            self.reduced_steps += int((out < -1.0).sum())
            self.steps += len(out)
        return out

    def process(self, x):
        gains = self.gains(x)
        if self.kind == "compressor":
            gains = gains + self.p["makeup"]
        return x * self.ramp.samples(gains)

    def summary(self):
        if not self.steps:
            return ""
        share = 100 * self.reduced_steps / self.steps
        return f"down by up to {-self.most:.1f} dB, more than 1 dB down {share:.0f}% of the time"


def _highpass_linear(freq: float, rate: int) -> np.ndarray:
    """A linear-phase (symmetric, odd length) Blackman-windowed high-pass FIR."""
    taps = int(rate * 0.006) | 1
    n = np.arange(taps) - (taps - 1) / 2
    cut = min(freq, rate * 0.45) / rate
    low = 2 * cut * np.sinc(2 * cut * n) * np.blackman(taps)
    low /= low.sum()
    high = -low
    high[(taps - 1) // 2] += 1.0
    return high


class _Delay:
    def __init__(self, frames: int):
        self.history = np.zeros(frames)

    def process(self, x: np.ndarray) -> np.ndarray:
        if not len(self.history):
            return x
        joined = np.concatenate([self.history, x])
        self.history = joined[len(x):]
        return joined[:len(x)]


class DeesserProcessor(Processor):
    """The level above a frequency (a linear-phase high-pass, so it lines up
    with the delayed dry sound) drives a gain on that band (split) or on the
    whole sound (wide)."""

    def __init__(self, effect: Effect, rate: int):
        p = self.p = effect.params
        taps = _highpass_linear(p["freq"], rate)
        self.latency = (len(taps) - 1) // 2
        self.band = _Fir(taps)
        self.dry = _Delay(self.latency)
        self.step = step_frames(rate)
        self.detector = _Detector("Peak", rate, self.step)
        self.ramp = _Ramp(self.step)
        seconds = self.step / rate
        self.attack, self.release = _coef(0.5, seconds), _coef(p["release"], seconds)
        self.gain, self.most, self.active = 0.0, 0.0, 0

    def process(self, x):
        band = self.band.process(x)
        dry = self.dry.process(x.astype(np.float64))
        over = self.detector.levels(band) - self.p["threshold"]
        targets = np.maximum(np.where(over > 0, over * (1 / self.p["ratio"] - 1), 0.0),
                             -self.p["max_reduction"]).tolist()
        out, gain = [], self.gain
        for target in targets:
            gain = target + (gain - target) * (self.attack if target < gain else self.release)
            out.append(gain)
        self.gain = gain
        steps = np.asarray(out)
        if len(steps):
            self.most = min(self.most, float(steps.min()))
            self.active += int((steps < -1.0).sum())
        g = self.ramp.samples(steps)
        if self.p["mode"] == "Wide":
            return dry * g
        return dry + (g - 1.0) * band

    def summary(self):
        return f"down by up to {-self.most:.1f} dB" if self.most < -0.05 else "never acted (below the threshold)"


def _running_min(x: np.ndarray, width: int) -> np.ndarray:
    """min(x[i:i + width]) for each i where it fits (len(x) - width + 1 values), in log(width) passes."""
    if width <= 1:
        return x.copy()
    table, span = x, 1
    while span * 2 <= width:
        table = np.minimum(table[:-span], table[span:])
        span *= 2
    count = len(x) - width + 1
    return np.minimum(table[:count], table[width - span:width - span + count])


class LimiterProcessor(Processor):
    def __init__(self, effect: Effect, rate: int):
        p = effect.params
        self.ceiling = float(_db_to_gain(p["ceiling"]))
        self.lookahead = max(1, round(p["lookahead"] / 1000 * rate))
        # True peak: the peak of each stretch between two samples (oversampled). It
        # arrives `delay` samples late, so the audio is delayed by that much more.
        self.tp = loudness.TruePeak(rate) if p["true_peak"] else None
        self.last_interval = 0.0
        self.latency = self.lookahead + (self.tp.delay if self.tp else 0)
        self.step = step_frames(rate)
        self.release = _coef(p["release"], self.step / rate)
        self.need_history = np.ones(self.lookahead)    # what the last samples needed (gain)
        self.held_history = np.ones(self.lookahead)    # the smoothed gain of the last samples
        self.dry = _Delay(self.latency)
        self.gain_db = 0.0
        self.most = 0.0

    def process(self, x):
        n, lat = len(x), self.lookahead
        if self.tp is not None:
            # A sample's level: the highest point on either side of it, so both
            # samples around an inter-sample peak are turned down for it.
            intervals = self.tp.intervals(x.astype(np.float64))
            before = np.concatenate([[self.last_interval], intervals[:-1]])
            self.last_interval = float(intervals[-1])
            level = np.maximum(intervals, before)
        else:
            level = np.abs(x)
        need = np.minimum(1.0, self.ceiling / np.maximum(level, 1e-12))
        joined = np.concatenate([self.need_history, need])
        self.need_history = joined[n:]
        # Held: the least gain needed by any sample in the look-ahead (the last lat+1 samples).
        held = _running_min(joined, lat + 1)
        # Release per step: instant down, smooth up; never above what was held.
        step_min = 20 * np.log10(held.reshape(-1, self.step).min(axis=1))
        out, gain, release = [], self.gain_db, self.release
        for target in step_min.tolist():
            gain = target if target <= gain else target + (gain - target) * release
            out.append(gain)
        self.gain_db = gain
        if out:
            self.most = min(self.most, min(out))
        smooth = np.repeat(_db_to_gain(np.asarray(out)), self.step)
        # Averaging over lat+1 samples reaches the needed gain by the time the peak arrives.
        joined = np.concatenate([self.held_history, smooth])
        self.held_history = joined[n:]
        sums = np.cumsum(np.concatenate([[0.0], joined]))
        g = (sums[lat + 1:] - sums[:-(lat + 1)]) / (lat + 1)
        return self.dry.process(x.astype(np.float64)) * g

    def summary(self):
        return f"down by up to {-self.most:.1f} dB" if self.most < -0.05 else "no peaks reached the ceiling"


class GainProcessor(Processor):
    def __init__(self, effect: Effect, rate: int):
        p = effect.params
        self.gain = float(_db_to_gain(p["gain"])) * (-1.0 if p["invert"] else 1.0)
        self.delay = _Delay(round(p["delay"] / 1000 * rate))

    def process(self, x):
        return self.delay.process(x.astype(np.float64) * self.gain)


class LoudnessProcessor(Processor):
    """One gain for the whole track, set by the engine after measuring (``measured``)."""

    def __init__(self, effect: Effect, rate: int):
        self.p = effect.params
        self.gain_db: float | None = None
        self.note = ""

    def set_measurement(self, meter: "LoudnessMeter") -> None:
        p = self.p
        level = meter.integrated()
        if level is None:
            self.gain_db, self.note = 0.0, "too quiet to measure (below -70 LUFS), left as it was"
            return
        gain = meter.gain_to(p["target"])
        why = ""
        if gain > p["max_gain"]:
            gain, why = p["max_gain"], f"raised by the most allowed ({p['max_gain']:g} dB)"
        peak = meter.true_peak
        if p["peak_limit"] and peak > 0:
            room = p["ceiling"] - _gain_db(peak)
            if gain > room:
                gain, why = room, f"held back by its true peaks ({_gain_db(peak):.1f} dBTP)"
        self.gain_db = gain
        result = meter.integrated(gain)
        self.note = (f"measured {level:.1f} LUFS, {gain:+.1f} dB → {result:.1f} LUFS"
                     + (f"; target not reached: {why}" if abs(result - p["target"]) > 0.05 and why else ""))

    def process(self, x):
        return x * float(_db_to_gain(self.gain_db or 0.0))

    def summary(self):
        return self.note


def make_processor(effect: Effect, rate: int) -> Processor:
    if effect.kind in ("filter", "equalizer"):
        return LinearProcessor(effect, rate)
    if effect.kind in ("gate", "expander", "compressor"):
        return DynamicsProcessor(effect, rate)
    return {"deesser": DeesserProcessor, "limiter": LimiterProcessor, "gain": GainProcessor,
            "loudness": LoudnessProcessor}[effect.kind](effect, rate)


# ------------------------------------------------------------------ the engine

class _Channel:
    """One track running through a chain from some start frame on."""

    def __init__(self, processors: list[Processor], start: int):
        self.processors = processors
        self.latency = sum(p.latency for p in processors)
        self.fed = start
        self.buf = np.zeros(0, F32)
        self.buf_start = start - self.latency

    def pull(self, read: Callable[[int, int], np.ndarray], a: int, b: int) -> np.ndarray:
        """The chain's output for frames [a, b) (a no earlier than the last pull's end)."""
        pieces, end = [self.buf], self.buf_start + len(self.buf)
        while end < b:
            x = read(self.fed, self.fed + FEED).astype(np.float64)
            for processor in self.processors:
                x = processor.process(x)
            pieces.append(x.astype(F32))
            self.fed += FEED
            end += FEED
        buf = np.concatenate(pieces) if len(pieces) > 1 else self.buf
        out = buf[a - self.buf_start:b - self.buf_start]
        self.buf = buf[b - self.buf_start:]
        self.buf_start = b
        return out


class Engine:
    """Runs the chain on the tracks of one recording, for
    ``processing.process_file(engine=...)`` and previews. Each track has its
    own effect instances; processing goes on from where the last block ended,
    and starts PREROLL_SECONDS early (silence before the file's start)
    whenever it jumps. Delays from look-ahead are taken off."""

    def __init__(self, chain: list[Effect], rate: int, cancelled: Callable[[], bool] | None = None):
        self.chain = [e for e in chain if not e.bypass]
        self.rate, self.cancelled = rate, cancelled
        self.block_frames = 1 << 18
        self.channels: dict[int, _Channel] = {}
        self.next: dict[int, int] = {}
        self.measured: dict[int, LoudnessMeter] = {}
        self._cache: OrderedDict = OrderedDict()

    def close(self):
        self._cache.clear()

    def _read(self, reader, c: int):
        def read(a, b):
            key = (a, b)
            block = self._cache.get(key)
            if block is None:
                block = self._cache[key] = reader(a, b)
                while len(self._cache) > 6:
                    self._cache.popitem(last=False)
            return block[:, c]
        return read

    def needs_measuring(self) -> bool:
        return any(e.kind == "loudness" for e in self.chain)

    def prepare(self, reader, channels, first: int = 0, end: int | None = None,
                progress: Callable[[int, int], None] | None = None) -> None:
        """Measure the loudness before a Loudness effect, per track, over frames
        [first, end) (default: the whole file). Needed once per recording when
        the chain has one; previews measure just their stretch."""
        index = next((i for i, e in enumerate(self.chain) if e.kind == "loudness"), None)
        if index is None:
            return
        end = reader.frames if end is None else end
        runs, meters = {}, {}
        for c in channels:
            runs[c] = self._start(reader, c, first, self.chain[:index])
            meters[c] = LoudnessMeter(self.rate)
        # Block by block over all tracks, so each block is read from the file once.
        for a in range(first, end, self.block_frames):
            if self.cancelled and self.cancelled():
                raise InterruptedError
            b = min(end, a + self.block_frames)
            for c, run in runs.items():
                meters[c].feed(run.pull(self._read(reader, c), a, b).astype(np.float64))
            if progress:
                progress(b - first, max(1, end - first))
        self.measured.update(meters)
        self.channels.clear()
        self.next.clear()

    def _start(self, reader, c: int, a: int, effects: list[Effect]) -> _Channel:
        """A fresh run of effects on track c whose next output is frame a: from
        PREROLL_SECONDS before a (silence before the file's start), so the
        dynamics have settled, with the look-ahead delay run out."""
        processors = [make_processor(e, self.rate) for e in effects]
        if effects is self.chain:
            for processor in processors:
                if isinstance(processor, LoudnessProcessor) and c in self.measured:
                    processor.set_measurement(self.measured[c])
        run = _Channel(processors, a - round(PREROLL_SECONDS * self.rate) if a > 0 else 0)
        if run.buf_start < a:
            run.pull(self._read(reader, c), run.buf_start, a)
        return run

    def process(self, reader, a: int, b: int, c: int) -> np.ndarray:
        """Frames [a, b) of track c, processed (float32)."""
        if self.cancelled and self.cancelled():
            raise InterruptedError
        if self.needs_measuring() and c not in self.measured:
            self.prepare(reader, [c])
        channel = self.channels.get(c)
        if channel is None or self.next.get(c) != a:
            channel = self.channels[c] = self._start(reader, c, a, self.chain)
        out = channel.pull(self._read(reader, c), a, b)
        self.next[c] = b
        return out

    def report(self, names: list[str] | None = None) -> list[str]:
        """What each effect did, per track that ran (for the dialog and the result)."""
        lines = []
        for c, channel in sorted(self.channels.items()):
            track = names[c] if names and c < len(names) else f"Track {c + 1}"
            for effect, processor in zip(self.chain, channel.processors):
                text = processor.summary()
                if text:
                    lines.append(f"{track} · {effect.label}: {text}")
        return lines


# ------------------------------------------------------------------ presets

PRESET_FOLDER = "effects-presets"


def _builtin() -> dict[str, list[Effect]]:
    hpf = Effect("filter", {"kind": "High-pass", "freq": 80.0, "slope": "24 dB/oct"})
    return {
        "Dialogue: rumble filter": [hpf],
        "Dialogue: clean-up": [
            Effect("filter", {"kind": "High-pass", "freq": 80.0, "slope": "24 dB/oct"}),
            Effect("deesser", {}),
            Effect("compressor", {"threshold": -24.0, "ratio": 2.5, "attack": 10.0, "release": 150.0}),
            Effect("limiter", {"ceiling": -1.0}),
        ],
        "Dialogue: hum notch 50 Hz": [Effect("equalizer", {"bands": [
            {"type": "Notch", "freq": 50.0 * k, "gain": 0.0, "q": 12.0} for k in (1, 2, 3, 4)]})],
        "Dialogue: hum notch 60 Hz": [Effect("equalizer", {"bands": [
            {"type": "Notch", "freq": 60.0 * k, "gain": 0.0, "q": 12.0} for k in (1, 2, 3, 4)]})],
    }


def delivery_chain(target: "loudness.Target", gain_db: float | None = None) -> list[Effect]:
    """Loudness to a platform's target, then a true-peak limiter 0.3 dB under its
    limit (Netflix suggests -2.3 dBTP for its -2), which takes care of the peaks.
    gain_db: one fixed gain instead of each track's own Loudness (tracks measured
    together as one programme must all get the same gain, or the mix changes)."""
    limiter = Effect("limiter", {"ceiling": target.true_peak - 0.3, "true_peak": True})
    if gain_db is not None:
        return [Effect("gain", {"gain": gain_db}), limiter]
    return [Effect("loudness", {"platform": target.name, "peak_limit": False}), limiter]


def _delivery() -> dict[str, list[Effect]]:
    return {f"Delivery: {t.name} ({t.integrated:g} LUFS, {t.true_peak:g} dBTP)": delivery_chain(t)
            for t in loudness.TARGETS}


BUILTIN_PRESETS = {**_builtin(), **_delivery()}


def preset_dir(data_dir: str) -> str:
    return os.path.join(data_dir, PRESET_FOLDER)


def safe_preset_name(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name).strip(" .")


def list_presets(data_dir: str) -> list[str]:
    folder = preset_dir(data_dir)
    try:
        names = [n[:-5] for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        names = []
    return sorted(names, key=str.casefold)


def save_preset(data_dir: str, name: str, chain: list[Effect]) -> str:
    name = safe_preset_name(name)
    if not name:
        raise ValueError("give the preset a name")
    os.makedirs(preset_dir(data_dir), exist_ok=True)
    path = os.path.join(preset_dir(data_dir), name + ".json")
    temp = path + ".part"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump({"app": "Location Sound File Manager", "kind": "effects", "version": 1,
                   "chain": [e.to_json() for e in chain]}, f, indent=2)
    os.replace(temp, path)
    return path


def load_preset(data_dir: str, name: str) -> list[Effect]:
    if name in BUILTIN_PRESETS:
        return copy.deepcopy(BUILTIN_PRESETS[name])
    with open(os.path.join(preset_dir(data_dir), safe_preset_name(name) + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    return chain_from_json(json.dumps(data.get("chain", [])))


def delete_preset(data_dir: str, name: str) -> None:
    os.remove(os.path.join(preset_dir(data_dir), safe_preset_name(name) + ".json"))


# ------------------------------------------------------------------ Easy Effects presets

EE_FILTER = {"High-pass": ("filter", "High-pass"), "Low-pass": ("filter", "Low-pass"),
             "Band-pass": ("filter", "Band-pass"), "Notch": ("filter", "Band-reject"),
             "Bell": ("band", "Bell"), "Low-shelf": ("band", "Low shelf"), "High-shelf": ("band", "High shelf")}
EE_SKIPPED = {"loudness": "loudness compensation (equal-loudness EQ)",
              "rnnoise": "noise reduction (use Reduce Noise)",
              "deepfilternet": "deep noise remover (use Reduce Noise: its DeepFilterNet add-on)",
              "speex": "speech processor", "echo_canceller": "echo canceller"}
EE_BAND = {"Bell": "Bell", "Hi-pass": "High-pass", "Hi-shelf": "High shelf", "Lo-pass": "Low-pass",
           "Lo-shelf": "Low shelf", "Notch": "Notch"}


def _ee_slope(label) -> str:
    try:
        n = int(str(label).lstrip("x"))
    except ValueError:
        n = 1
    return SLOPES[min(len(SLOPES), max(1, n)) - 1]


def import_easyeffects(data: dict) -> tuple[list[Effect], list[str]]:
    """An Easy Effects preset (its .json, output or input pipeline) as our
    chain, and notes on what was approximated or left out."""
    section = next((data[k] for k in ("output", "input") if isinstance(data.get(k), dict)
                    and data[k].get("plugins_order")), None)
    if section is None:
        raise ValueError("this isn't an Easy Effects preset (no plugins_order)")
    chain: list[Effect] = []
    notes: list[str] = []
    skipped: list[str] = []
    for name in section["plugins_order"]:
        p = section.get(name)
        base = str(name).split("#")[0]
        if not isinstance(p, dict):
            continue
        bypass = bool(p.get("bypass", False))
        made: list[Effect] = []
        if base == "filter":
            kind = EE_FILTER.get(p.get("type"))
            if kind is None:
                skipped.append(f"filter type {p.get('type')}")
            elif kind[0] == "filter":
                made.append(Effect("filter", {"kind": kind[1], "freq": p.get("frequency", 1000.0),
                                              "slope": _ee_slope(p.get("slope")), "q": p.get("quality", 0.707)}))
            else:
                made.append(Effect("equalizer", {"bands": [{"type": kind[1], "freq": p.get("frequency", 1000.0),
                                                            "gain": p.get("gain", 0.0),
                                                            "q": p.get("quality", 0.707)}]}))
        elif base == "equalizer":
            bands = []
            left = p.get("left") or {}
            for n in range(int(_num(p.get("num-bands"), 0))):
                band = left.get(f"band{n}") or {}
                kind = EE_BAND.get(band.get("type"))
                if kind is None or band.get("mute"):
                    continue
                bands.append({"type": kind, "freq": band.get("frequency", 1000.0), "gain": band.get("gain", 0.0),
                              "q": band.get("q", 1.0)})
            if p.get("split-channels"):
                notes.append("Equalizer: left and right had their own bands; the left ones are used for every track.")
            if len(bands) > EQ_BANDS:
                notes.append(f"Equalizer: only the first {EQ_BANDS} bands are used.")
            if any(str(b.get("slope", "x1")) != "x1" for b in left.values() if isinstance(b, dict)):
                notes.append("Equalizer: band slopes steeper than x1 are made 12 dB/oct.")
            made.append(Effect("equalizer", {"bands": bands}))
        elif base == "gate":
            made.append(Effect("gate", {"threshold": p.get("curve-threshold", -24.0),
                                        "hysteresis": abs(_num(p.get("hysteresis-zone"), -6.0))
                                        if p.get("hysteresis") else 0.0,
                                        "range": abs(_num(p.get("reduction"), -24.0)),
                                        "attack": p.get("attack", 20.0), "release": p.get("release", 100.0),
                                        "hold": 0.0, "detection": "RMS"
                                        if (p.get("sidechain") or {}).get("mode") == "RMS" else "Peak"}))
        elif base in ("compressor", "expander"):
            if base == "compressor" and p.get("mode", "Downward") != "Downward":
                skipped.append(f"compressor in {p.get('mode')} mode")
            elif base == "expander" and p.get("mode", "Downward") != "Downward":
                skipped.append("upward expander")
            else:
                settings = {"threshold": p.get("threshold", -12.0), "ratio": p.get("ratio", 4.0),
                            "knee": abs(_num(p.get("knee"), -6.0)), "attack": p.get("attack", 20.0),
                            "release": p.get("release", 100.0),
                            "detection": "Peak" if (p.get("sidechain") or {}).get("mode") == "Peak" else "RMS"}
                if base == "compressor":
                    settings["makeup"] = p.get("makeup", 0.0)
                made.append(Effect(base, settings))
        elif base == "deesser":
            made.append(Effect("deesser", {"freq": p.get("f1-freq", 6000.0), "threshold": p.get("threshold", -18.0),
                                           "ratio": p.get("ratio", 3.0),
                                           "mode": "Wide" if p.get("mode") == "Wide" else "Split"}))
        elif base == "limiter":
            made.append(Effect("limiter", {"ceiling": p.get("threshold", 0.0), "lookahead": p.get("lookahead", 5.0),
                                           "release": max(5.0, _num(p.get("release"), 5.0))}))
        elif base == "autogain":
            made.append(Effect("loudness", {"target": p.get("target", -23.0)}))
            notes.append("Autogain follows the level as it plays; here it becomes one gain per track for the "
                         "whole file (Loudness).")
        elif base == "delay":
            made.append(Effect("gain", {"delay": p.get("time-l", 0.0), "invert": bool(p.get("invert-phase-l"))}))
        else:
            skipped.append(EE_SKIPPED.get(base, base.replace("_", " ")))
            continue
        made = [e for e in made if e.kind != "equalizer" or e.params["bands"]]
        gain_in, gain_out = _num(p.get("input-gain"), 0.0), _num(p.get("output-gain"), 0.0)
        if gain_in and made:
            made.insert(0, Effect("gain", {"gain": gain_in}))
        if gain_out and made:
            made.append(Effect("gain", {"gain": gain_out}))
        for effect in made:
            effect.bypass = bypass
        chain.extend(made)
    if skipped:
        notes.append("Not available here, left out: " + ", ".join(dict.fromkeys(skipped)) + ".")
    if chain:
        notes.append("Values are matched as closely as these effects allow; listen before making copies.")
    return chain, notes
