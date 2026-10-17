"""Pure timecode sync of recordings with video clips.

A clip's start timecode and frame rate come from ffprobe (the tmcd track of a
MOV/MP4, or the MXF/stream tag). Clips and recordings are put on one clock,
real seconds since midnight, so takes are matched to clips by where they
overlap there. Nothing here changes a file; copies are written to new names.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from math import ceil, floor
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import compat, timecode as tc

if TYPE_CHECKING:
    from .catalog import Recording

VIDEO_EXTENSIONS = (".mov", ".mp4", ".m4v", ".mxf")
SYNCED_SUFFIX = "_synced"


def tool(name: str) -> str:
    """ffmpeg/ffprobe from PATH; on macOS also Homebrew's folders, which a .app's PATH lacks."""
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "darwin":
        for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
            if os.path.exists(compat.join(folder, name)):
                return compat.join(folder, name)
    return name


@dataclass
class Clip:
    path: str
    start: Fraction | None = None  # real seconds since midnight of the first frame
    rate: Fraction | None = None
    drop_frame: bool = False
    timecode: str = ""
    duration: float = 0.0
    audio_streams: int = 0
    width: int = 0
    height: int = 0
    date: str = ""  # "2025-10-27", from the file's creation time
    error: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def end(self) -> Fraction | None:
        return None if self.start is None else self.start + Fraction(self.duration)

    @property
    def day(self) -> Fraction:
        """Real seconds in a day of its timecode: 24 h of frames at 23.976 run 86,486.4 s."""
        return Fraction(86400 * tc.nominal_fps(self.rate)) / self.rate if self.rate else Fraction(86400)

    @property
    def start_timecode(self) -> str:
        """The start timecode written the standard way (ffmpeg takes only ":" and ";")."""
        return tc.frames_to_tc(round(self.start * self.rate), tc.nominal_fps(self.rate), self.drop_frame)


def _rate(text: str | None) -> Fraction | None:
    try:
        rate = Fraction(text or "0")
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def probe_clip(path: str) -> Clip:
    clip = Clip(path)
    try:
        out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        clip.error = f"ffprobe could not run: {error}"
        return clip
    if out.returncode:
        lines = out.stderr.decode("utf-8", "replace").strip().splitlines()
        clip.error = lines[-1] if lines else "not a video"
        return clip
    info = json.loads(out.stdout or b"{}")
    streams, fmt = info.get("streams", []), info.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        clip.error = "no video stream"
        return clip
    clip.rate = _rate(video.get("r_frame_rate")) or _rate(video.get("avg_frame_rate"))
    clip.width, clip.height = int(video.get("width") or 0), int(video.get("height") or 0)
    clip.audio_streams = sum(1 for s in streams if s.get("codec_type") == "audio")
    try:
        clip.duration = float(fmt.get("duration") or video.get("duration") or 0)
    except ValueError:
        clip.duration = 0.0
    tags = [s.get("tags", {}) for s in streams] + [fmt.get("tags", {})]
    clip.timecode = next((t["timecode"] for t in tags if t.get("timecode")), "")
    created = next((t["creation_time"] for t in tags if t.get("creation_time")), "")
    clip.date = created[:10]
    if not clip.timecode:
        clip.error = "no timecode in the file"
        return clip
    if clip.rate is None:
        clip.error = "no frame rate"
        return clip
    clip.drop_frame = ";" in clip.timecode
    frames = tc.tc_to_frames(clip.timecode, tc.nominal_fps(clip.rate), clip.drop_frame)
    if frames is None:
        clip.error = f"unreadable timecode {clip.timecode!r}"
        return clip
    clip.start = frames / clip.rate
    return clip


def find_clips(folder: str) -> list[str]:
    """Video files in the folder and below it, by path; not hidden or half-written
    files, nor synced copies made earlier."""
    found = []
    for parent, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            stem, ext = os.path.splitext(name)
            if ext.lower() in VIDEO_EXTENSIONS and not name.startswith(".") and not stem.endswith(SYNCED_SUFFIX):
                found.append(compat.join(compat.fwd(parent), name))
    return sorted(found, key=lambda p: compat.relpath(p, folder).lower())


def scan_folder(folder: str, progress=None, cancelled=lambda: False) -> list[Clip]:
    """Probe every clip in the folder (progress(done, total, name) after each)."""
    paths, clips = find_clips(folder), []
    for done, path in enumerate(paths):
        if cancelled():
            raise InterruptedError("cancelled")
        if progress:
            progress(done, len(paths), os.path.basename(path))
        clips.append(probe_clip(path))
    return clips


DATE_SLACK_DAYS = 1  # a clip's creation time is often UTC, a day off the recorder's local date


@dataclass
class Match:
    clip: Clip
    rec: Recording
    start: Fraction  # the take's first sample on the clip's clock (a day earlier or later over midnight)
    overlap: float  # seconds the two run together
    chosen: bool = False  # ticked by default: the take covering most of the clip

    @property
    def offset(self) -> Fraction:
        """Seconds from the clip's first frame to the take's first sample (< 0: the take starts first)."""
        return self.start - self.clip.start

    @property
    def offset_samples(self) -> int:
        return round(self.offset * self.rec.sample_rate)


def rec_start(rec) -> Fraction | None:
    if rec.time_reference is None or not rec.sample_rate:
        return None
    return Fraction(rec.time_reference, rec.sample_rate)


def _days_apart(a: str, b: str) -> int:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return 0


def match(recordings, clips) -> list[Match]:
    """Every (clip, take) pair that runs at the same time of day, clip by clip
    in the given order. A take is moved by a day either way when that brings it
    nearer the clip (a take or clip running over midnight)."""
    matches: list[Match] = []
    for c in clips:
        if c.start is None:
            continue
        found = []
        for r in recordings:
            start = rec_start(r)
            if start is None or (r.date and c.date and _days_apart(r.date, c.date) > DATE_SLACK_DAYS):
                continue
            start = min((start - c.day, start, start + c.day), key=lambda s: abs(s - c.start))
            overlap = min(c.end, start + Fraction(r.frames, r.sample_rate)) - max(c.start, start)
            if overlap > 0:
                found.append(Match(c, r, start, float(overlap)))
        if found:
            max(found, key=lambda m: (m.overlap, m.rec.channels)).chosen = True
        matches.extend(found)
    return matches


def by_clip(matches: list[Match]) -> list[list[Match]]:
    """The matches of each clip together, clips in their first order."""
    groups: dict[int, list[Match]] = {}
    for m in matches:
        groups.setdefault(id(m.clip), []).append(m)
    return list(groups.values())



def synced_path(clip: Clip, folder: str, suffix: str = SYNCED_SUFFIX) -> str:
    """Synced copies are always MOV: it holds the recorders' PCM as it is (MP4 doesn't)."""
    return compat.join(folder, os.path.splitext(clip.name)[0] + suffix + ".mov")


def _pcm_codec(rec) -> str:
    if rec.float_samples:
        return "pcm_f64le" if rec.bits == 64 else "pcm_f32le"
    return "pcm_u8" if rec.bits == 8 else f"pcm_s{rec.bits}le"


def _take_filter(m: Match, index: int, frames: int) -> str:
    """The take lined up with the clip, sample for sample, and as long as the clip."""
    shift = m.offset_samples
    line = f"adelay=delays={shift}S:all=1" if shift >= 0 else f"atrim=start_sample={-shift},asetpts=PTS-STARTPTS"
    return f"[{index + 1}:a]{line},apad,atrim=end_sample={frames}[take{index}]"


def write_synced_copy(matches: list[Match], dst: str, cancelled=lambda: False) -> str:
    """A copy of the clip (video and camera audio copied as they are, same
    timecode) with each take added as one more audio stream, lined up by
    timecode. Written under a temporary name and checked; never overwrites."""
    if not matches:
        raise ValueError("no takes to add")
    clip = matches[0].clip
    if os.path.exists(dst):
        raise FileExistsError(f"{os.path.basename(dst)} is already there")
    part = compat.join(os.path.dirname(dst), f".{os.path.basename(dst)}.part")
    cmd = [tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", clip.path]
    filters, maps, codecs = [], [], []
    for index, m in enumerate(matches):
        cmd += ["-i", m.rec.path]
        filters.append(_take_filter(m, index, round(Fraction(clip.duration) * m.rec.sample_rate)))
        stream = clip.audio_streams + index
        maps += ["-map", f"[take{index}]"]
        title = os.path.splitext(m.rec.name)[0]
        # MOV keeps a track's name in "name" (from title) and "handler_name"; editors show one or the other.
        codecs += [f"-c:a:{stream}", _pcm_codec(m.rec), f"-metadata:s:a:{stream}", f"title={title}",
                   f"-metadata:s:a:{stream}", f"handler_name={title}"]
    # Only the first video stream: MP4s can carry cover art or a second video stream as well.
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "0:a?", *maps,
            "-c:v", "copy", "-c:a", "copy", *codecs, "-map_metadata", "0", "-timecode", clip.start_timecode,
            "-f", "mov", part]
    try:
        _run(cmd, cancelled)
        check = probe_clip(part)
        if check.audio_streams != clip.audio_streams + len(matches) or abs(check.duration - clip.duration) > 0.5:
            raise RuntimeError(f"the copy of {clip.name} came out wrong ({check.error or 'streams or length differ'})")
        if os.path.exists(dst):
            raise FileExistsError(f"{os.path.basename(dst)} is already there")
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            os.remove(part)
    return dst


def _run(cmd: list[str], cancelled) -> None:
    """Run ffmpeg, killing it when cancelled() turns true."""
    with subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as proc:
        while proc.poll() is None:
            if cancelled():
                proc.kill()
                proc.wait()
                raise InterruptedError("cancelled")
            time.sleep(0.05)
        error = proc.stderr.read().decode("utf-8", "replace").strip()
    if proc.returncode:
        raise RuntimeError(error.splitlines()[-1] if error else f"ffmpeg failed ({proc.returncode})")


# ---- Timelines for editors ----
# Final Cut Pro and DaVinci Resolve import FCPXML (Resolve reads up to 1.9
# reliably across versions; Final Cut 10.6+ takes 1.10). Premiere Pro imports
# the older Final Cut Pro 7 XML ("xmeml"); it doesn't read FCPXML.
EDITORS = {
    "fcp": ("Final Cut Pro", ".fcpxml"),
    "resolve": ("DaVinci Resolve", ".fcpxml"),
    "premiere": ("Premiere Pro", ".xml"),
}
FCPXML_VERSION = {"fcp": "1.10", "resolve": "1.9"}


@dataclass
class Timeline:
    text: str
    extension: str
    left_out: list  # clips at another frame rate than the timeline's


@dataclass
class _Item:
    name: str
    path: str
    start: Fraction  # source time of its first frame/sample (seconds since midnight)
    length: Fraction  # source length
    at: Fraction  # where it sits on the timeline (on a frame)
    duration: Fraction  # how long it runs there (whole frames)
    lane: int = 0
    clip: Clip | None = None
    rec: object = None


def _seconds(value: Fraction) -> str:
    value = Fraction(value)
    return f"{value.numerator}s" if value.denominator == 1 else f"{value.numerator}/{value.denominator}s"


def _url(path: str) -> str:
    path = compat.fwd(path)
    return "file://" + ("" if path.startswith("/") else "/") + quote(path, safe="/:")


def _lanes(items: list[_Item], first: int, step: int) -> None:
    """Items running at the same time go on separate lanes (first, first+step, ...)."""
    ends: list[Fraction] = []
    for item in sorted(items, key=lambda i: (i.at, i.name)):
        free = next((k for k, end in enumerate(ends) if end <= item.at), None)
        if free is None:
            ends.append(item.at + item.duration)
            free = len(ends) - 1
        else:
            ends[free] = item.at + item.duration
        item.lane = first + step * free


def _unique(items) -> list:
    """In order, each object once (clips and recordings are dataclasses: not hashable)."""
    return list({id(i): i for i in items}.values())


@dataclass
class _Layout:
    rate: Fraction
    drop: bool
    origin: Fraction  # the timeline's first frame (time of day)
    end: Fraction
    videos: list[_Item]
    audios: list[_Item]
    left_out: list[Clip]


def _layout(matches: list[Match]) -> _Layout:
    """The clips at the timeline's rate and the chosen takes, placed on frames."""
    clips = _unique(m.clip for m in matches if m.clip.rate)
    rate = Counter(c.rate for c in clips).most_common(1)[0][0]
    left_out = [c for c in clips if c.rate != rate]
    kept = [c for c in clips if c.rate == rate]
    frame = 1 / rate
    # A clip's length comes as a float from ffprobe: whole frames here.
    videos = [_Item(os.path.splitext(c.name)[0], c.path, c.start, floor(Fraction(c.duration) * rate) * frame,
                    c.start, floor(Fraction(c.duration) * rate) * frame, clip=c) for c in kept]
    # Each take once, at its start on the clock of the first clip it was chosen for (over midnight).
    takes = {id(m.rec): m for m in reversed([m for m in matches if m.chosen and m.clip.rate == rate])}
    audios = []
    for m in reversed(list(takes.values())):
        r = m.rec
        start, length = m.start, Fraction(r.frames, r.sample_rate)
        # A take rarely starts on a frame: it goes on the next frame and plays
        # from that point in it, so the sound stays in sync to the sample.
        at = ceil(start * rate) * frame
        audios.append(_Item(os.path.splitext(r.name)[0], r.path, start, length, at,
                            floor((start + length - at) * rate) * frame, rec=r))
    _lanes(videos, 1, 1)
    _lanes(audios, -1, -1)
    origin = min([v.at for v in videos] + [floor(a.start * rate) * frame for a in audios])
    end = max(i.at + i.duration for i in videos + audios)
    drop = any(c.drop_frame for c in kept) and tc.nominal_fps(rate) % 30 == 0
    return _Layout(rate, drop, origin, end, videos, audios, left_out)


def timeline(matches: list[Match], editor: str, name: str) -> Timeline:
    """A timeline with the matched clips and their chosen takes, each at its own timecode."""
    if not any(m.clip.rate for m in matches):
        raise ValueError("no clips to put on a timeline")
    layout = _layout(matches)
    text = _xmeml(name, layout) if editor == "premiere" else _fcpxml(name, FCPXML_VERSION[editor], layout)
    return Timeline(text, EDITORS[editor][1], layout.left_out)


def write_timeline(timeline: Timeline, path: str) -> None:
    """Write under a temporary name, read it back as XML, then put it in place
    (replacing a file there only because the save dialog already asked)."""
    part = compat.join(os.path.dirname(path) or ".", f".{os.path.basename(path)}.part")
    try:
        with open(part, "w", encoding="utf-8") as f:
            f.write(timeline.text)
        ET.parse(part)  # ParseError is a SyntaxError
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.remove(part)


def _xml_text(root, doctype: str) -> str:
    ET.indent(root)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE {doctype}>\n' + ET.tostring(root, "unicode") + "\n"


def _fcpxml(name: str, version: str, layout: _Layout) -> str:
    rate, origin, end, videos, audios = layout.rate, layout.origin, layout.end, layout.videos, layout.audios
    root = ET.Element("fcpxml", version=version)
    resources = ET.SubElement(root, "resources")
    first = videos[0].clip
    ET.SubElement(resources, "format", id="r1", frameDuration=_seconds(1 / rate),
                  width=str(first.width or 1920), height=str(first.height or 1080))
    sequence_parent = ET.SubElement(ET.SubElement(ET.SubElement(root, "library"), "event", name=name),
                                    "project", name=name)
    sequence = ET.SubElement(sequence_parent, "sequence", format="r1", duration=_seconds(end - origin),
                             tcStart=_seconds(origin), tcFormat="DF" if layout.drop else "NDF",
                             audioLayout="stereo", audioRate="48k")
    gap = ET.SubElement(ET.SubElement(sequence, "spine"), "gap", name="Gap", offset=_seconds(origin),
                        start=_seconds(origin), duration=_seconds(end - origin))
    for number, item in enumerate(videos + audios, start=2):
        ref = f"r{number}"
        if item.clip is not None:
            asset = ET.SubElement(resources, "asset", id=ref, name=item.name, start=_seconds(item.start),
                                  duration=_seconds(item.length), hasVideo="1", format="r1",
                                  hasAudio="1" if item.clip.audio_streams else "0",
                                  audioSources=str(item.clip.audio_streams))
        else:
            asset = ET.SubElement(resources, "asset", id=ref, name=item.name, start=_seconds(item.start),
                                  duration=_seconds(item.length), hasVideo="0", hasAudio="1", audioSources="1",
                                  audioChannels=str(item.rec.channels), audioRate=str(item.rec.sample_rate))
        ET.SubElement(asset, "media-rep", kind="original-media", src=_url(item.path))
        # Connected to the gap, whose time starts at the timeline's start: offsets are times of day.
        ET.SubElement(gap, "asset-clip", ref=ref, name=item.name, lane=str(item.lane), offset=_seconds(item.at),
                      start=_seconds(item.at), duration=_seconds(item.duration))
    return _xml_text(root, "fcpxml")


def _xmeml(name: str, layout: _Layout) -> str:
    rate, drop, origin, end = layout.rate, layout.drop, layout.origin, layout.end
    videos, audios = layout.videos, layout.audios
    fps, ntsc = tc.nominal_fps(rate), "FALSE" if rate.denominator == 1 else "TRUE"

    def add(parent, tag, text=None, **attrs):
        node = ET.SubElement(parent, tag, **attrs)
        if text is not None:
            node.text = str(text)
        return node

    def add_rate(parent):
        node = add(parent, "rate")
        add(node, "timebase", fps)
        add(node, "ntsc", ntsc)

    def add_timecode(parent, start):
        frames = round(start * rate)
        node = add(parent, "timecode")
        add_rate(node)
        add(node, "string", tc.frames_to_tc(frames, fps, drop))
        add(node, "frame", frames)
        add(node, "displayformat", "DF" if drop else "NDF")

    def frames(value):
        return floor(value * rate)

    root = ET.Element("xmeml", version="4")
    sequence = add(root, "sequence", id="sequence-1")
    add(sequence, "name", name)
    add(sequence, "duration", frames(end - origin))
    add_rate(sequence)
    add_timecode(sequence, origin)
    media = add(sequence, "media")
    video_parent, audio_parent = add(media, "video"), add(media, "audio")
    first = videos[0].clip
    sequence_format = add(add(video_parent, "format"), "samplecharacteristics")
    add_rate(sequence_format)
    add(sequence_format, "width", first.width or 1920)
    add(sequence_format, "height", first.height or 1080)
    tracks: dict[tuple[int, int], ET.Element] = {}
    counter = iter(range(1, 1_000_000))

    def track(kind, lane, channel):
        if (lane, channel) not in tracks:
            tracks[(lane, channel)] = add(video_parent if kind == "video" else audio_parent, "track")
        return tracks[(lane, channel)]

    for item in videos + audios:
        file_id = f"file-{next(counter)}"
        channels = [0] if item.clip is not None else list(range(1, item.rec.channels + 1))
        linked = []
        for channel in channels:
            kind = "video" if item.clip is not None else "audio"
            clipitem = add(track(kind, item.lane, channel), "clipitem", id=f"clipitem-{next(counter)}")
            add(clipitem, "name", item.name)
            add(clipitem, "enabled", "TRUE")
            add(clipitem, "duration", frames(item.length))
            add_rate(clipitem)
            # Premiere places on whole frames from the file's start: a take is
            # within half a frame of its timecode here.
            at = frames(item.at - origin) if kind == "video" else round((item.start - origin) * rate)
            length = frames(item.duration) if kind == "video" else frames(item.length)
            add(clipitem, "start", at)
            add(clipitem, "end", at + length)
            add(clipitem, "in", 0)
            add(clipitem, "out", length)
            if channel <= 1:
                file = add(clipitem, "file", id=file_id)
                add(file, "name", os.path.basename(item.path))
                add(file, "pathurl", _url(item.path))
                add_rate(file)
                add(file, "duration", frames(item.length))
                add_timecode(file, item.start)
                file_media = add(file, "media")
                if kind == "video":
                    characteristics = add(add(file_media, "video"), "samplecharacteristics")
                    add(characteristics, "width", item.clip.width or 1920)
                    add(characteristics, "height", item.clip.height or 1080)
                    if item.clip.audio_streams:
                        add(add(file_media, "audio"), "channelcount", item.clip.audio_streams)
                else:
                    audio = add(file_media, "audio")
                    characteristics = add(audio, "samplecharacteristics")
                    add(characteristics, "depth", item.rec.bits)
                    add(characteristics, "samplerate", item.rec.sample_rate)
                    add(audio, "channelcount", item.rec.channels)
            else:
                add(clipitem, "file", id=file_id)
            if kind == "audio":
                source = add(clipitem, "sourcetrack")
                add(source, "mediatype", "audio")
                add(source, "trackindex", channel)
                linked.append((clipitem, list(tracks).index((item.lane, channel)) + 1))
        # A take's tracks are linked, so Premiere moves and trims them together.
        for clipitem, _ in linked if len(linked) > 1 else []:
            for other, track_index in linked:
                link = add(clipitem, "link")
                add(link, "linkclipref", other.get("id"))
                add(link, "mediatype", "audio")
                add(link, "trackindex", track_index)
                add(link, "clipindex", 1)
    return _xml_text(root, "xmeml")
