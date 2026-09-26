"""The channel mixer's state: faders, pans, mutes, solos, links and automation.

Pure Python and numpy (no Qt), so it is easy to test. The audio engine asks
it for a gain matrix (channels x 2) at a position in the file; the mixer
panel edits it.

Automation is per channel and per parameter ("gain" in dB, "pan" -1..1,
"mute" 0/1), as breakpoints at file positions (frames). While a channel is
armed and the file plays, its current values are written over what was
there; otherwise recorded moves play back.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field

import numpy as np

MIN_DB = -60.0  # fader bottom above "off"
MAX_DB = 12.0
OFF = float("-inf")
PARAMS = ("gain", "pan", "mute")
FILE_VERSION = 1

# Track colours, in channel order (as on a Wave Agent style mixer).
TRACK_COLORS = [
    "#27c4d8", "#6fcf4a", "#f08a4b", "#3d8bfd", "#e8746a", "#b07cf0", "#f2c14e", "#2fc79a",
    "#ec6fb0", "#8fb3ff", "#d9d65a", "#ff9f6e", "#5ad1f0", "#a4e06c", "#c792ea", "#f47c7c",
]


def track_color(channel: int) -> str:
    return TRACK_COLORS[channel % len(TRACK_COLORS)]


def db_to_gain(db: float) -> float:
    return 0.0 if db == OFF or db <= MIN_DB - 0.05 else 10 ** (db / 20)


def gain_to_db(gain: float) -> float:
    return 20 * math.log10(gain) if gain > 0 else OFF


def format_db(db: float) -> str:
    if db == OFF or db <= MIN_DB - 0.05:
        return "-inf"
    return f"{db:+.1f} dB" if abs(db) >= 0.05 else "0.0 dB"


def format_pan(pan: float) -> str:
    value = round(pan * 100)
    return "C" if value == 0 else (f"L{-value}" if value < 0 else f"R{value}")


def pan_gains(pan: float) -> tuple[float, float]:
    """Constant-power pan, scaled so the centre is unity in both speakers
    (a centred mono track sounds as loud as before), hard left/right +3 dB."""
    angle = (min(max(pan, -1.0), 1.0) + 1) * math.pi / 4
    return math.cos(angle) * math.sqrt(2), math.sin(angle) * math.sqrt(2)


@dataclass
class Channel:
    name: str = ""
    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    solo: bool = False
    armed: bool = False
    link: bool = False  # linked with the next channel (a stereo pair)


class Lane:
    """Breakpoints (frame, value) of one automated parameter."""

    def __init__(self, points: list[tuple[int, float]] | None = None):
        self.frames: list[int] = []
        self.values: list[float] = []
        for frame, value in sorted(points or []):
            self.frames.append(int(frame))
            self.values.append(float(value))

    def __bool__(self):
        return bool(self.frames)

    def value_at(self, frame: int, step: bool = False) -> float | None:
        if not self.frames:
            return None
        i = bisect.bisect_right(self.frames, frame)
        if i == 0:
            return self.values[0]
        if i == len(self.frames) or step:
            return self.values[i - 1]
        f0, f1 = self.frames[i - 1], self.frames[i]
        v0, v1 = self.values[i - 1], self.values[i]
        if f1 == f0 or math.isinf(v0) or math.isinf(v1):
            return v0
        return v0 + (v1 - v0) * (frame - f0) / (f1 - f0)

    def write(self, start: int, end: int, value: float) -> None:
        """Replace everything in [start, end] by the value (held over the range)."""
        if end < start:
            start, end = end, start
        lo = bisect.bisect_left(self.frames, start)
        hi = bisect.bisect_right(self.frames, end)
        self.frames[lo:hi] = [start, end] if end > start else [start]
        self.values[lo:hi] = [value, value] if end > start else [value]

    def points(self) -> list[list]:
        return [[f, None if math.isinf(v) else v] for f, v in zip(self.frames, self.values)]


@dataclass
class MixerState:
    channels: list[Channel] = field(default_factory=list)
    master_db: float = 0.0
    master_mute: bool = False
    exclusive_solo: bool = False  # a new solo replaces the others
    auto_trim: bool = True  # lower the mix by 1/sqrt(n) when n tracks play, so poly files don't clip
    automation: dict[tuple[int, str], Lane] = field(default_factory=dict)
    # Where each armed channel's last write ended (to join up the next one).
    _writing: dict[int, int] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ setup

    @classmethod
    def for_tracks(cls, names: list[str], stereo_pair: bool = False) -> "MixerState":
        state = cls([Channel(name) for name in names])
        if stereo_pair and len(names) == 2:
            state.set_link(0, True)
        return state

    def same_layout(self, names: list[str]) -> bool:
        return [c.name for c in self.channels] == list(names)

    def reset(self) -> None:
        for ch in self.channels:
            ch.gain_db, ch.pan, ch.mute, ch.solo, ch.armed, ch.link = 0.0, 0.0, False, False, False, False
        self.master_db, self.master_mute = 0.0, False
        self.automation.clear()
        self._writing.clear()

    # ------------------------------------------------------------ links

    def partner(self, index: int) -> int | None:
        """The other channel of a linked pair."""
        if self.channels[index].link and index + 1 < len(self.channels):
            return index + 1
        if index > 0 and self.channels[index - 1].link:
            return index - 1
        return None

    def group(self, index: int) -> list[int]:
        other = self.partner(index)
        return sorted([index] if other is None else [index, other])

    def set_link(self, index: int, on: bool) -> None:
        """Link a channel with the next one: faders, mutes and solos move
        together and the pair is panned hard left / right."""
        if index + 1 >= len(self.channels):
            return
        if on:
            # A channel is in one pair at most.
            if index > 0:
                self.channels[index - 1].link = False
            self.channels[index + 1].link = False
            left, right = self.channels[index], self.channels[index + 1]
            left.link = True
            right.gain_db, right.mute, right.solo, right.armed = left.gain_db, left.mute, left.solo, left.armed
            left.pan, right.pan = -1.0, 1.0
        else:
            self.channels[index].link = False
            self.channels[index].pan = self.channels[index + 1].pan = 0.0

    # ------------------------------------------------------------ edits (linked channels follow)

    def set_gain(self, index: int, db: float) -> list[int]:
        db = OFF if db <= MIN_DB - 0.05 else min(db, MAX_DB)
        for i in self.group(index):
            self.channels[i].gain_db = db
        return self.group(index)

    def set_pan(self, index: int, pan: float) -> list[int]:
        pan = min(max(pan, -1.0), 1.0)
        self.channels[index].pan = pan
        other = self.partner(index)
        if other is not None:
            self.channels[other].pan = -pan  # a pair narrows / widens symmetrically
        return self.group(index)

    def set_mute(self, index: int, on: bool) -> list[int]:
        for i in self.group(index):
            self.channels[i].mute = on
        return self.group(index)

    def set_solo(self, index: int, on: bool) -> list[int]:
        group = self.group(index)
        if on and self.exclusive_solo:
            for i, ch in enumerate(self.channels):
                if i not in group:
                    ch.solo = False
        for i in group:
            self.channels[i].solo = on
        return group

    def set_armed(self, index: int, on: bool) -> list[int]:
        for i in self.group(index):
            self.channels[i].armed = on
            self._writing.pop(i, None)
        return self.group(index)

    def mute_all(self, on: bool = True) -> None:
        for ch in self.channels:
            ch.mute = on

    def arm_all(self, on: bool = True) -> None:
        for i, ch in enumerate(self.channels):
            ch.armed = on
        self._writing.clear()

    def clear_solos(self) -> None:
        for ch in self.channels:
            ch.solo = False

    # ------------------------------------------------------------ what is heard

    def audible(self, index: int) -> bool:
        """Mute always wins; when anything is soloed only soloed channels play."""
        ch = self.channels[index]
        if ch.mute:
            return False
        return ch.solo or not any(c.solo for c in self.channels)

    def values_at(self, index: int, frame: int | None) -> tuple[float, float, bool]:
        """(gain dB, pan, mute) of a channel at a position, with automation
        played back unless the channel is armed (then it is being written)."""
        ch = self.channels[index]
        gain, pan, mute = ch.gain_db, ch.pan, ch.mute
        if frame is not None and not ch.armed:
            lane = self.automation.get((index, "gain"))
            if lane:
                value = lane.value_at(frame)
                gain = OFF if value is None or math.isinf(value) else value
            lane = self.automation.get((index, "pan"))
            if lane:
                pan = lane.value_at(frame)
            lane = self.automation.get((index, "mute"))
            if lane:
                mute = bool(lane.value_at(frame, step=True))
        return gain, pan, mute

    def matrix(self, frame: int | None = None) -> np.ndarray:
        """Gains (channels, 2) from each channel to the left / right speaker
        at a position, master and automatic trim included."""
        n = len(self.channels)
        out = np.zeros((n, 2), np.float32)
        if self.master_mute or not n:
            return out
        soloing = any(c.solo for c in self.channels)
        playing = 0
        for i, ch in enumerate(self.channels):
            gain_db, pan, mute = self.values_at(i, frame)
            if mute or (soloing and not ch.solo):
                continue
            gain = db_to_gain(gain_db)
            if gain <= 0:
                continue
            playing += 1
            left, right = pan_gains(pan)
            out[i] = (gain * left, gain * right)
        trim = 1 / math.sqrt(playing) if self.auto_trim and playing > 1 else 1.0
        return out * (db_to_gain(self.master_db) * trim)

    def channel_gains(self, frame: int | None = None) -> np.ndarray:
        """Post-fader gain per channel, before mute and solo (for the meters)."""
        return np.array([db_to_gain(self.values_at(i, frame)[0]) for i in range(len(self.channels))], np.float32)

    # ------------------------------------------------------------ automation

    def has_automation(self, index: int | None = None) -> bool:
        return any(lane for (i, _), lane in self.automation.items() if index is None or i == index)

    def record(self, frame: int, jumped: bool = False) -> list[int]:
        """Write the armed channels' current values up to this position (call
        it regularly while playing). jumped: the playhead moved (seek or loop),
        so start a new stretch instead of joining up. Returns the channels written."""
        written = []
        for i, ch in enumerate(self.channels):
            if not ch.armed:
                continue
            start = frame if jumped else self._writing.get(i, frame)
            if start > frame:
                start = frame
            for param, value in (("gain", ch.gain_db), ("pan", ch.pan), ("mute", float(ch.mute))):
                self.automation.setdefault((i, param), Lane()).write(start, frame, value)
            self._writing[i] = frame
            written.append(i)
        return written

    def stop_recording(self) -> None:
        self._writing.clear()

    def clear_automation(self, index: int | None = None) -> None:
        for key in [k for k in self.automation if index is None or k[0] == index]:
            del self.automation[key]
        if index is None:
            self._writing.clear()
        else:
            self._writing.pop(index, None)

    # ------------------------------------------------------------ files

    def to_dict(self) -> dict:
        return {
            "version": FILE_VERSION,
            "master_db": None if math.isinf(self.master_db) else self.master_db,
            "master_mute": self.master_mute,
            "exclusive_solo": self.exclusive_solo,
            "auto_trim": self.auto_trim,
            "channels": [{"name": c.name, "gain_db": None if math.isinf(c.gain_db) else c.gain_db, "pan": c.pan,
                          "mute": c.mute, "solo": c.solo, "link": c.link} for c in self.channels],
            "automation": [{"channel": i, "param": param, "points": lane.points()}
                           for (i, param), lane in sorted(self.automation.items()) if lane],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1)

    def apply_dict(self, data: dict) -> list[str]:
        """Load settings saved by to_dict onto the current channels (matched by
        position). Returns warnings, e.g. when the track names differ."""
        if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
            raise ValueError("not a mixer file")
        warnings = []
        saved = data["channels"]
        if len(saved) != len(self.channels):
            warnings.append(f"The file has {len(saved)} channels, this recording {len(self.channels)}: "
                            "only the first ones were set.")
        elif any(str(s.get("name", "")) != c.name for s, c in zip(saved, self.channels)):
            warnings.append("The track names differ from the saved ones; channels were matched by number.")

        def db(value):
            return OFF if value is None else float(value)

        self.reset()
        for ch, s in zip(self.channels, saved):
            ch.gain_db, ch.pan = db(s.get("gain_db", 0.0)), float(s.get("pan", 0.0))
            ch.mute, ch.solo, ch.link = bool(s.get("mute")), bool(s.get("solo")), bool(s.get("link"))
        if self.channels:
            self.channels[-1].link = False
        self.master_db, self.master_mute = db(data.get("master_db", 0.0)), bool(data.get("master_mute"))
        self.exclusive_solo = bool(data.get("exclusive_solo", self.exclusive_solo))
        self.auto_trim = bool(data.get("auto_trim", True))
        for entry in data.get("automation", []):
            i, param = int(entry["channel"]), str(entry["param"])
            if 0 <= i < len(self.channels) and param in PARAMS:
                self.automation[(i, param)] = Lane([(f, OFF if v is None else v) for f, v in entry["points"]])
        return warnings

    def copy_static(self, other: "MixerState") -> None:
        """Take over another state's fader settings (not its automation)."""
        for mine, theirs in zip(self.channels, other.channels):
            mine.gain_db, mine.pan, mine.mute, mine.solo, mine.link = (
                theirs.gain_db, theirs.pan, theirs.mute, theirs.solo, theirs.link)
        self.master_db, self.master_mute = other.master_db, other.master_mute
        self.exclusive_solo, self.auto_trim = other.exclusive_solo, other.auto_trim
