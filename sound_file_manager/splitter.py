"""Split a polyphonic WAV into one mono file per track. Nothing in here uses Qt.

The track files go into a folder named after the take (the file name without
its _ISO / _LR ending, e.g. ``8MT01/``) and are named after their track names
(``8MT01/Boom.WAV``). Each one keeps the recorder's metadata: the bext chunk
(time reference, so the timecode stays the same) and the iXML with the track
list cut down to its own track, the same FAMILY_UID (so the files stay one
take) and a HISTORY that names the file it came from. Cue points are copied.
"""

from __future__ import annotations

from . import card_safety

import os
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import bwf, compat
from .catalog import Recording
from .organize import safe_part
from .renamer import split_take_name

COPIED_CHUNKS = (b"cue ", b"LIST")  # copied as they are (frame positions stay valid)
BLOCK_BYTES = 4 * 1024 * 1024


@dataclass
class TrackFile:
    channel: int  # 0-based interleave position in the source
    track: str  # the track name as in the source ("" if unnamed)
    dst: str
    channels: tuple[int, ...] = ()  # several tracks (what stays with a shrunk original); empty: just `channel`

    @property
    def picks(self) -> tuple[int, ...]:
        return self.channels or (self.channel,)


@dataclass
class SplitPlan:
    """What splitting some recordings will do. The recordings of one take go
    into one folder; their other files (e.g. the _LR) are moved in with them."""
    folders: dict[str, list[TrackFile]] = field(default_factory=dict)  # folder -> files it gets
    splits: list[tuple[Recording, list[TrackFile]]] = field(default_factory=list)  # the mono track files
    # A shrunk original: the tracks not split off, under the original's name in the take folder.
    remainders: dict[str, TrackFile] = field(default_factory=dict)
    originals_in_folder: set[str] = field(default_factory=set)  # originals moved (whole) into the take folder
    partners: list[tuple[Recording, str]] = field(default_factory=list)  # moved along: (rec, new path)
    problems: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # selected files that can't be split (and why)

    def kept_path(self, rec: Recording) -> str:
        """Where the original goes when it is kept: into its take folder."""
        return compat.join(take_folder(rec), rec.name)

    def outputs(self, rec: Recording, files: list[TrackFile]) -> list[TrackFile]:
        """Everything written from one recording (track files, then a shrunk original)."""
        return files + ([self.remainders[rec.path]] if rec.path in self.remainders else [])


def take_folder(rec: Recording) -> str:
    return compat.join(rec.folder, split_take_name(rec.name)[0])


def track_stem(track: str, number: int) -> str:
    return safe_part(track) or f"Track {number}"


def is_track_file(rec: Recording) -> bool:
    """A file named after its tracks, not its scene and take (made by a
    split): a mono file named after its track, or a file in a folder named
    after the take it was recorded as, whose name doesn't start with that."""
    if len(rec.tracks) == 1:
        stem = re.escape(track_stem(rec.tracks[0], 1))
        if re.fullmatch(stem + r"(_\d+)?", Path(rec.name).stem, re.IGNORECASE):
            return True
    core = split_take_name(rec.original_filename)[0] if rec.original_filename else ""
    if not core or os.path.basename(rec.folder).casefold() != core.casefold():
        return False
    stem = Path(rec.name).stem.casefold()
    # Not one named after the scene and take (e.g. the original, renamed after a take change).
    built = rec.scene and rec.take and re.search(re.escape(rec.scene.strip().casefold()) + ".*"
                                                 + re.escape(rec.take.strip().casefold()), stem)
    return not stem.startswith(core.casefold()) and not built


def splittable(rec: Recording) -> str | None:
    """Why a recording can't be split, or None."""
    if rec.error:
        return rec.error
    if rec.channels < 2:
        return "it has only one track"
    if rec.bits % 8 or rec.bits not in (8, 16, 24, 32, 64):
        return f"{rec.bits}-bit audio isn't supported"
    return None


def plan_split(recs: list[Recording],
               family_of: Callable[[Recording], list[Recording]], keep: bool = True,
               picked: dict[str, set[int]] | None = None, shrink: bool = False,
               groups: dict[str, list[tuple[str, list[int]]]] | None = None) -> SplitPlan:
    """picked: the tracks split off per recording (all when missing). The
    others stay with the original: it moves into the take folder whole (it
    has them anyway), or with shrink it is written again with only those
    tracks and the full file goes where an original that isn't kept goes.
    groups: per recording, (name, tracks) that go into one poly file each
    instead of a mono file per track (only their ticked tracks count)."""
    plan = SplitPlan()
    picked, groups = picked or {}, groups or {}
    chosen = []
    for rec in recs:
        why = splittable(rec)
        if why:
            plan.skipped.append(f"{rec.name}: {why}")
        else:
            chosen.append(rec)
    chosen_paths = {r.path for r in chosen}
    used: dict[str, set[str]] = {}
    for rec in list(chosen):
        picks = sorted(c for c in picked.get(rec.path, range(rec.channels)) if 0 <= c < rec.channels)
        if not picks:
            plan.skipped.append(f"{rec.name}: no tracks ticked")
            chosen.remove(rec)
            continue
        rest = [c for c in range(rec.channels) if c not in picks]
        folder = take_folder(rec)
        names = used.setdefault(folder, set())
        names.add(rec.name.casefold())  # a track named like the file can't take its name
        ext = os.path.splitext(rec.name)[1] or ".wav"
        files = []
        tracks = list(rec.tracks) + [""] * (rec.channels - len(rec.tracks))
        if rest and shrink:
            plan.remainders[rec.path] = TrackFile(rest[0], "", plan.kept_path(rec), tuple(rest))
        elif rest or keep:
            plan.originals_in_folder.add(rec.path)
        group_of = {}
        for group_name, members in groups.get(rec.path, []):
            members = tuple(c for c in sorted(members) if c in picks and c not in group_of)
            for c in members:
                group_of[c] = (group_name, members)
        for channel in picks:
            if channel in group_of:
                group_name, members = group_of[channel]
                if channel != members[0]:
                    continue  # written with the group's first track
                stem = safe_part(group_name) or "+".join(track_stem(tracks[c], c + 1) for c in members)
            else:
                members, stem = (), track_stem(tracks[channel], channel + 1)
            name, n = stem + ext, 2
            while name.casefold() in names:
                name, n = f"{stem}_{n}{ext}", n + 1
            names.add(name.casefold())
            label = group_name if members else tracks[channel]
            files.append(TrackFile(channel, label, compat.join(folder, name), members if len(members) > 1 else ()))
        plan.splits.append((rec, files))
        plan.folders.setdefault(folder, []).extend(files)
    chosen_paths = {r.path for r in chosen}
    moved = {r.path for r in chosen}
    for rec in chosen:
        for partner in family_of(rec):
            if partner.path not in chosen_paths and partner.path not in moved:
                moved.add(partner.path)
                plan.partners.append((partner, compat.join(take_folder(rec), partner.name)))
    # Nothing may already be where a new or moved file goes.
    targets = [f.dst for fs in plan.folders.values() for f in fs] + [p for _, p in plan.partners] + \
              [plan.kept_path(r) for r, _ in plan.splits
               if r.path in plan.originals_in_folder or r.path in plan.remainders]
    seen = set()
    for target in targets:
        key = target.casefold()
        if key in seen:
            plan.problems.append(f"{os.path.basename(target)}: two files would get this name in "
                                 f"{os.path.basename(os.path.dirname(target))}")
        elif os.path.lexists(target):
            plan.problems.append(f"{compat.relpath(target, os.path.dirname(os.path.dirname(target)))}: "
                                 "already exists")
        seen.add(key)
    for folder in plan.folders:
        if os.path.exists(folder) and not os.path.isdir(folder):
            plan.problems.append(f"{os.path.basename(folder)}: a file with the folder's name is in the way")
    return plan


# ---------------------------------------------------------------- writing

def _set(parent: ET.Element, tag: str, text: str) -> ET.Element:
    node = parent.find(tag)
    if node is None:
        node = ET.SubElement(parent, tag)
    node.text = text
    return node


def _set_index(index: int) -> str:
    return chr(ord("A") + index) if index < 26 else str(index + 1)


def track_ixml(old_payload: bytes | None, picks: tuple[int, ...], track: str, name: str, parent: str,
               index: int, total: int) -> bytes:
    """The iXML for one track file, or a shrunk original with the tracks in
    picks (see the module notes)."""
    root = bwf._ixml_root(old_payload) if old_payload else None
    if root is None:
        root = ET.Element("BWFXML")
        ET.SubElement(root, "IXML_VERSION").text = "1.61"
    parent_uid = (root.findtext("FILE_UID") or "").strip()
    _set(root, "FILE_UID", uuid.uuid4().hex.upper())
    history = root.find("HISTORY")
    if history is None:
        history = ET.SubElement(root, "HISTORY")
    if history.find("ORIGINAL_FILENAME") is None:
        _set(history, "ORIGINAL_FILENAME", parent)
    _set(history, "CURRENT_FILENAME", name)
    _set(history, "PARENT_FILENAME", parent)
    if parent_uid:
        _set(history, "PARENT_UID", parent_uid)
    file_set = root.find("FILE_SET")
    if file_set is None:
        file_set = ET.SubElement(root, "FILE_SET")
        _set(file_set, "FAMILY_UID", parent_uid or uuid.uuid4().hex.upper())
    _set(file_set, "TOTAL_FILES", str(total))
    _set(file_set, "FILE_SET_INDEX", _set_index(index))
    old_list = root.find("TRACK_LIST")
    ordered = []
    if old_list is not None:
        for node in old_list.findall("TRACK"):
            key = node.findtext("INTERLEAVE_INDEX") or node.findtext("CHANNEL_INDEX") or "0"
            try:
                ordered.append((int(key.strip()), node))
            except ValueError:
                ordered.append((len(ordered) + 1, node))
        ordered.sort(key=lambda item: item[0])
        root.remove(old_list)
    track_list = ET.SubElement(root, "TRACK_LIST")
    ET.SubElement(track_list, "TRACK_COUNT").text = str(len(picks))
    for position, channel in enumerate(picks, 1):
        if channel < len(ordered):
            node = ordered[channel][1]
        else:
            node = ET.Element("TRACK")
            ET.SubElement(node, "CHANNEL_INDEX").text = str(channel + 1)
            ET.SubElement(node, "NAME").text = track if len(picks) == 1 else ""
        _set(node, "INTERLEAVE_INDEX", str(position))
        track_list.append(node)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8") + \
        b" " * bwf.REWRITE_IXML_HEADROOM


def track_bext(old: bytes, picks: tuple[int, ...], name: str) -> bytes:
    """The bext payload with the description's track lines cut down to the
    tracks in picks (renumbered from TRK1) and the file name updated."""
    text = bwf._cstr(old[:bwf.BEXT_DESCRIPTION_SIZE])
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = []
    for line in re.split(r"\r?\n", text):
        match = re.match(r"^([a-z])TRK(\d+)=(.*)$", line)
        if match:
            if int(match.group(2)) - 1 in picks:
                number = picks.index(int(match.group(2)) - 1) + 1
                lines.append(f"{match.group(1)}TRK{number}={match.group(3)}")
            continue
        match = re.match(r"^([a-z])FILENAME=", line)
        lines.append(f"{match.group(1)}FILENAME={name}" if match else line)
    description = newline.join(line for line in lines if line)
    if description:
        description += newline
    raw = description.encode("latin-1", "replace")
    if len(raw) > bwf.BEXT_DESCRIPTION_SIZE:  # a longer file name: leave that line out
        description = newline.join(line for line in lines if line and "FILENAME=" not in line) + newline
        raw = description.encode("latin-1", "replace")[:bwf.BEXT_DESCRIPTION_SIZE]
    return raw.ljust(bwf.BEXT_DESCRIPTION_SIZE, b"\0") + old[bwf.BEXT_DESCRIPTION_SIZE:]


def split_file(src: str, files: list[TrackFile],
               progress: Callable[[int, int], None] | None = None) -> list[str]:
    """Write the track files of one recording. They are written under temporary
    names and only get their names when all are complete and check out; on any
    error nothing is left behind. Returns the paths written."""
    card_safety.assert_writable(*(f.dst for f in files))
    with open(src, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        layout = bwf.read_layout(f, size)
        info = bwf._info_from(f, layout)
        if info.format_tag not in (1, 3):
            raise bwf.WavError(f"format {info.format_tag} isn't supported")
        width = info.bits // 8
        data = layout.first(b"data")
        if data is None or info.channels < 2 or info.block_align != info.channels * width:
            raise bwf.WavError("not a multi-track file this can split")
        frames = data.size // info.block_align
        chunks = {}
        for chunk in layout.chunks:
            if chunk.id in (b"bext", b"iXML") + COPIED_CHUNKS and chunk.id not in chunks and not chunk.truncated:
                f.seek(chunk.data_offset)
                chunks[chunk.id] = f.read(chunk.size)

        parent = os.path.basename(src)
        outs, temps, written = [], [], []
        try:
            for index, tf in enumerate(files):
                name = os.path.basename(tf.dst)
                temp = compat.join(os.path.dirname(tf.dst), f".{name}.part")
                header = _header(info, frames, width, chunks, tf, name, parent, index, len(files))
                os.makedirs(os.path.dirname(tf.dst), exist_ok=True)
                out = open(temp, "wb")
                temps.append(temp)
                outs.append(out)
                out.write(header)
            f.seek(data.data_offset)
            total = frames * info.block_align
            block = max(1, BLOCK_BYTES // info.block_align) * info.block_align
            done = 0
            while done < total:
                raw = f.read(min(block, total - done))
                if not raw:
                    raise bwf.WavError("the file ended early")
                count = len(raw) // info.block_align
                raw = raw[:count * info.block_align]
                samples = np.frombuffer(raw, np.uint8).reshape(count, info.channels, width)
                for tf, out in zip(files, outs):
                    out.write(samples[:, list(tf.picks), :].tobytes())
                done += len(raw)
                if progress:
                    progress(done, total)
            for tf, out in zip(files, outs):
                if (frames * width * len(tf.picks)) & 1:
                    out.write(b"\0")
            for out in outs:
                out.close()
            for temp, tf in zip(temps, files):
                check = bwf.read_info(temp)
                if check.channels != len(tf.picks) or check.frames != frames:
                    raise bwf.WavError(f"{os.path.basename(temp)} did not check out after writing")
            for temp, tf in zip(temps, files):
                if os.path.lexists(tf.dst):
                    raise bwf.WavError(f"{os.path.basename(tf.dst)} appeared while splitting")
            for temp, tf in zip(temps, files):
                os.replace(temp, tf.dst)
                written.append(tf.dst)
            return written
        except BaseException:
            for out in outs:
                out.close()
            for temp in temps:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            for path in written:  # renamed before a later one failed
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise


def _header(info: bwf.WavInfo, frames: int, width: int, chunks: dict[bytes, bytes], tf: TrackFile,
            name: str, parent: str, index: int, total: int) -> bytes:
    meta = []
    if b"bext" in chunks and len(chunks[b"bext"]) >= 346:
        meta.append(bwf._chunk_bytes(b"bext", track_bext(chunks[b"bext"], tf.picks, name)))
    meta.append(bwf._chunk_bytes(b"iXML", track_ixml(chunks.get(b"iXML"), tf.picks, tf.track, name, parent,
                                                    index, total)))
    copied = [bwf._chunk_bytes(cid, chunks[cid]) for cid in COPIED_CHUNKS if cid in chunks]
    return _wav_header(info.format_tag, len(tf.picks), info.sample_rate, info.bits, frames, meta, copied)


def _wav_header(format_tag: int, channels: int, rate: int, bits: int, frames: int, meta: list[bytes],
                copied: list[bytes]) -> bytes:
    """Everything before the audio: metadata chunks, fmt, then the data header
    (RIFF, or RF64 over 4 GB)."""
    width = bits // 8
    align = channels * width
    fmt = struct.pack("<HHIIHH", format_tag, channels, rate, rate * align, align, bits)
    if format_tag == 3:
        fmt += struct.pack("<H", 0)
    parts = list(meta) + [bwf._chunk_bytes(b"fmt ", fmt)]
    if format_tag == 3:
        parts.append(bwf._chunk_bytes(b"fact", struct.pack("<I", min(frames, 0xFFFFFFFF))))
    parts += copied
    data_size = frames * align
    body = b"".join(parts)
    riff_size = 4 + 28 + 8 + len(body) + 8 + data_size + (data_size & 1)  # WAVE + JUNK/ds64 + ... + data
    if riff_size <= 0xFFFFFFFF:
        # A JUNK chunk where a ds64 would go, as recorders do.
        return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + bwf._chunk_bytes(b"JUNK", b"\0" * 28) + body + \
            b"data" + struct.pack("<I", data_size)
    ds64 = struct.pack("<QQQI", riff_size, data_size, frames, 0)
    return b"RF64" + b"\xff\xff\xff\xff" + b"WAVE" + bwf._chunk_bytes(b"ds64", ds64) + body + \
        b"data" + b"\xff\xff\xff\xff"


# ---------------------------------------------------------------- combining

def combine_problems(recs: list[Recording]) -> list[str]:
    """Why these files can't become one polywav (empty if they can): they must
    be the same take, i.e. line up sample for sample."""
    if len(recs) < 2:
        return ["select two or more files"]
    problems = [f"{r.name}: {r.error}" for r in recs if r.error]
    if problems:
        return problems
    first = recs[0]
    for key, label in (("sample_rate", "sample rate"), ("bits", "bit depth"), ("float_samples", "sample format"),
                       ("frames", "length")):
        if len({getattr(r, key) for r in recs}) > 1:
            problems.append(f"the files have different {label}s ("
                            + ", ".join(f"{r.name}: {_describe(r, key)}" for r in recs) + ")")
    stamps = {r.time_reference for r in recs if r.time_reference is not None}
    if len(stamps) > 1:
        problems.append("the files start at different timecodes, so they aren't the same take")
    if first.bits % 8 or first.bits not in (8, 16, 24, 32, 64):
        problems.append(f"{first.bits}-bit audio isn't supported")
    return problems


def _describe(rec: Recording, key: str) -> str:
    if key == "frames":
        return f"{rec.frames:,} samples"
    if key == "float_samples":
        return "float" if rec.float_samples else "integer"
    if key == "sample_rate":
        return f"{rec.sample_rate / 1000:g} kHz"
    return f"{rec.bits}-bit"


def combined_name(recs: list[Recording]) -> str:
    """A suggested name (without folder): the scene and take, in the style of
    the take the files came from ("2B-T001") or else "<scene>T<take>". When
    that name is taken in the folder (e.g. by the original), the files' names
    are added: "2B-T001_BOOM+LAV.WAV"."""
    ext = os.path.splitext(recs[0].name)[1] or ".wav"
    joined = "+".join(Path(r.name).stem for r in recs)
    first = recs[0]
    scene, take = first.scene.strip(), first.take.strip()
    cores = {split_take_name(r.original_filename)[0] for r in recs if r.original_filename}
    core = cores.pop() if len(cores) == 1 else ""
    if scene and take:
        built = re.fullmatch(rf"{re.escape(scene)}[-_ ]?(?:T|TK|TAKE)?[-_ ]?{re.escape(take)}", core, re.IGNORECASE)
        base = core if built else safe_part(f"{scene}T{take}")
    else:
        base = core
    if not base:
        return joined + ext
    if not os.path.lexists(compat.join(first.folder, base + ext)):
        return base + ext
    return f"{base}_{joined}{ext}"


def combine_ixml(payloads: list[bytes | None], names: list[list[str]], name: str) -> bytes:
    """The iXML of a combined file: the first file's, with the track lists of
    all files in order (INTERLEAVE_INDEX renumbered) and a new FILE_UID."""
    root = bwf._ixml_root(payloads[0]) if payloads[0] else None
    if root is None:
        root = ET.Element("BWFXML")
        ET.SubElement(root, "IXML_VERSION").text = "1.61"
    _set(root, "FILE_UID", uuid.uuid4().hex.upper())
    history = root.find("HISTORY")
    if history is None:
        history = ET.SubElement(root, "HISTORY")
    _set(history, "CURRENT_FILENAME", name)
    nodes = []
    for payload, tracks in zip(payloads, names):
        source = bwf._ixml_root(payload) if payload else None
        ordered = []
        if source is not None:
            for node in source.findall("TRACK_LIST/TRACK"):
                key = node.findtext("INTERLEAVE_INDEX") or node.findtext("CHANNEL_INDEX") or "0"
                try:
                    ordered.append((int(key.strip()), node))
                except ValueError:
                    ordered.append((len(ordered) + 1, node))
            ordered.sort(key=lambda item: item[0])
        for i, track in enumerate(tracks):
            if i < len(ordered):
                nodes.append(ordered[i][1])
            else:
                node = ET.Element("TRACK")
                ET.SubElement(node, "CHANNEL_INDEX").text = str(len(nodes) + 1)
                ET.SubElement(node, "NAME").text = track
                nodes.append(node)
    old = root.find("TRACK_LIST")
    if old is not None:
        root.remove(old)
    track_list = ET.SubElement(root, "TRACK_LIST")
    ET.SubElement(track_list, "TRACK_COUNT").text = str(len(nodes))
    for position, node in enumerate(nodes, 1):
        _set(node, "INTERLEAVE_INDEX", str(position))
        track_list.append(node)
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8") + \
        b" " * bwf.REWRITE_IXML_HEADROOM


def combine_bext(old: bytes, tracks: list[str], name: str) -> bytes:
    """The first file's bext with TRK lines for all tracks (only if it had
    track lines, and only if they fit) and the file name updated."""
    text = bwf._cstr(old[:bwf.BEXT_DESCRIPTION_SIZE])
    newline = "\r\n" if "\r\n" in text else "\n"
    lines, prefix = [], ""
    for line in re.split(r"\r?\n", text):
        match = re.match(r"^([a-z])TRK\d+=", line)
        if match:
            prefix = match.group(1)
            continue
        match = re.match(r"^([a-z])FILENAME=", line)
        if line:
            lines.append(f"{match.group(1)}FILENAME={name}" if match else line)
    with_tracks = lines + [f"{prefix}TRK{i}={t}" for i, t in enumerate(tracks, 1)] if prefix else lines
    for candidate in (with_tracks, lines, [l for l in lines if "FILENAME=" not in l]):
        raw = (newline.join(candidate) + newline if candidate else "").encode("latin-1", "replace")
        if len(raw) <= bwf.BEXT_DESCRIPTION_SIZE:
            break
    raw = raw[:bwf.BEXT_DESCRIPTION_SIZE]
    return raw.ljust(bwf.BEXT_DESCRIPTION_SIZE, b"\0") + old[bwf.BEXT_DESCRIPTION_SIZE:]


def combine_files(srcs: list[str], dst: str, progress: Callable[[int, int], None] | None = None) -> str:
    """Write one polywav with the tracks of srcs in order (they must pass
    combine_problems). Written under a temporary name, checked, then renamed."""
    card_safety.assert_writable(dst)
    files, infos, datas, layouts = [], [], [], []
    temp = compat.join(os.path.dirname(dst), f".{os.path.basename(dst)}.part")
    out = None
    try:
        for src in srcs:
            f = open(src, "rb")
            files.append(f)
            layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
            info = bwf._info_from(f, layout)
            data = layout.first(b"data")
            if info.format_tag not in (1, 3) or data is None or info.block_align != info.channels * info.bits // 8:
                raise bwf.WavError(f"{os.path.basename(src)} can't be combined")
            infos.append(info)
            datas.append(data)
            layouts.append(layout)
        first = infos[0]
        width = first.bits // 8
        frames = min(d.size // i.block_align for d, i in zip(datas, infos))
        chunks = {}
        f = files[0]
        for chunk in layouts[0].chunks:
            if chunk.id in (b"bext", b"iXML") + COPIED_CHUNKS and chunk.id not in chunks and not chunk.truncated:
                f.seek(chunk.data_offset)
                chunks[chunk.id] = f.read(chunk.size)
        payloads = []
        for f, layout in zip(files, layouts):
            ixml = layout.first(b"iXML")
            if ixml is not None and not ixml.truncated:
                f.seek(ixml.data_offset)
                payloads.append(f.read(ixml.size))
            else:
                payloads.append(None)
        names = [list(i.tracks) + [""] * (i.channels - len(i.tracks)) for i in infos]
        names = [n[:i.channels] for n, i in zip(names, infos)]
        flat = [t for n in names for t in n]
        name = os.path.basename(dst)
        meta = []
        if b"bext" in chunks and len(chunks[b"bext"]) >= 346:
            meta.append(bwf._chunk_bytes(b"bext", combine_bext(chunks[b"bext"], flat, name)))
        meta.append(bwf._chunk_bytes(b"iXML", combine_ixml(payloads, names, name)))
        copied = [bwf._chunk_bytes(cid, chunks[cid]) for cid in COPIED_CHUNKS if cid in chunks]
        channels = sum(i.channels for i in infos)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        out = open(temp, "wb")
        out.write(_wav_header(first.format_tag, channels, first.sample_rate, first.bits, frames, meta, copied))
        for f, d in zip(files, datas):
            f.seek(d.data_offset)
        block = max(1, BLOCK_BYTES // (channels * width))
        done = 0
        while done < frames:
            count = min(block, frames - done)
            parts = []
            for f, i in zip(files, infos):
                raw = f.read(count * i.block_align)
                if len(raw) < count * i.block_align:
                    raise bwf.WavError("a file ended early")
                parts.append(np.frombuffer(raw, np.uint8).reshape(count, i.channels, width))
            out.write(np.concatenate(parts, axis=1).tobytes())
            done += count
            if progress:
                progress(done, frames)
        if (frames * channels * width) & 1:
            out.write(b"\0")
        out.close()
        check = bwf.read_info(temp)
        if check.channels != channels or check.frames != frames:
            raise bwf.WavError(f"{name} did not check out after writing")
        if os.path.lexists(dst):
            raise bwf.WavError(f"{name} appeared while combining")
        os.replace(temp, dst)
        return dst
    except BaseException:
        if out is not None:
            out.close()
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    finally:
        for f in files:
            f.close()
