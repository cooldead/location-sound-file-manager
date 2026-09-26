"""Pure timecode maths: frame rates, samples since midnight -> SMPTE timecode."""

from __future__ import annotations

import re
from fractions import Fraction

# Rates written as decimals by recorders ("23.976", "29.97DF") map to their exact
# NTSC fractions; anything else is taken at face value.
_NTSC = {
    "23.976": Fraction(24000, 1001), "23.98": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001), "47.952": Fraction(48000, 1001),
    "59.94": Fraction(60000, 1001), "119.88": Fraction(120000, 1001),
}


def parse_rate(text: str | None) -> Fraction | None:
    """Parse "24000/1001", "23.976", "023.976-ND", "29.97DF", "25" -> exact rate."""
    if not text:
        return None
    text = text.strip()
    match = re.match(r"^(\d+)\s*/\s*(\d+)$", text)
    if match:
        num, den = int(match.group(1)), int(match.group(2))
        return Fraction(num, den) if num and den else None
    match = re.match(r"^0*(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    number = match.group(1)
    if number in _NTSC:
        return _NTSC[number]
    try:
        rate = Fraction(number)
    except ValueError:
        return None
    return rate if rate > 0 else None


def parse_drop_frame(text: str | None) -> bool:
    """True for bext speeds like "29.97DF" / "029.970-DF" and iXML flag "DF"."""
    if not text:
        return False
    text = text.strip().upper()
    return text == "DF" or bool(re.search(r"(?<!N)DF\b|[\d.\-\s]D$", text))


def nominal_fps(rate: Fraction) -> int:
    """Frames counted per timecode second: 24 for 23.976, 30 for 29.97."""
    return max(1, round(rate))


def rate_label(rate: Fraction | None, drop: bool = False) -> str:
    if rate is None:
        return ""
    if rate.denominator == 1:
        text = str(rate.numerator)
    else:
        text = f"{float(rate):.3f}".rstrip("0").rstrip(".")
    return f"{text} DF" if drop else text


def frames_to_tc(frames: int, fps: int, drop: bool = False) -> str:
    """Frame count -> "HH:MM:SS:FF" (";" before frames for drop frame).

    Drop frame skips frame numbers 0 and 1 (0-3 at 60 fps) at the start of
    every minute except each tenth minute, per SMPTE 12M.
    """
    drop = drop and fps % 30 == 0
    if drop:
        skip = fps // 15
        per_ten_minutes = fps * 600 - skip * 9
        per_minute = fps * 60 - skip
        tens, rest = divmod(frames, per_ten_minutes)
        frames += skip * 9 * tens
        if rest > skip:
            frames += skip * ((rest - skip) // per_minute)
    frames %= fps * 86400
    ff = frames % fps
    total_seconds = frames // fps
    hh, rest = divmod(total_seconds, 3600)
    mm, ss = divmod(rest, 60)
    return f"{hh:02}:{mm:02}:{ss:02}{';' if drop else ':'}{ff:02}"


def samples_to_tc(samples: int, sample_rate: int, rate: Fraction, drop: bool = False) -> str:
    """Samples since midnight (BWF time reference) -> timecode at the given rate."""
    frames = int(Fraction(samples) * rate / sample_rate)
    return frames_to_tc(frames, nominal_fps(rate), drop)


def seconds_to_clock(seconds: float, *, millis: bool = False) -> str:
    """Wall clock style "HH:MM:SS(.mmm)" for files without a frame rate."""
    seconds = max(seconds, 0.0)
    whole = int(seconds)
    hh, rest = divmod(whole, 3600)
    mm, ss = divmod(rest, 60)
    text = f"{hh % 24:02}:{mm:02}:{ss:02}"
    if millis:
        text += f".{int((seconds - whole) * 1000):03}"
    return text


def format_duration(seconds: float) -> str:
    """Length like "7:28" or "1:02:03"."""
    whole = int(max(seconds, 0) + 0.5)
    hh, rest = divmod(whole, 3600)
    mm, ss = divmod(rest, 60)
    return f"{hh}:{mm:02}:{ss:02}" if hh else f"{mm}:{ss:02}"
