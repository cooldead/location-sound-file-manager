"""Pure Broadcast Wave (BWF) reading and metadata writing. Nothing in here uses Qt.

Production recorders (Sound Devices, Zoom F-series, ...) store their metadata
in two places inside the WAV:

* the ``bext`` chunk: originator, date/time, the time reference (samples since
  midnight, i.e. the start timecode) and a 256-byte description made of
  ``sKEY=VALUE`` lines (the prefix letter varies by maker: ``s`` Sound Devices,
  ``z`` Zoom);
* the ``iXML`` chunk: project, scene, take, tape, circled, note, frame rate,
  track names and the file-set (polyphonic family) id.

Writes are done in place whenever the new metadata fits in the existing chunks
(recorders pad iXML generously, and a directly following JUNK/PAD chunk can be
absorbed). Otherwise the whole file has to be copied, which only happens when
the caller explicitly allows it.
"""

from __future__ import annotations

import os
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import compat

BEXT_DESCRIPTION_SIZE = 256
FILLER_CHUNKS = {b"JUNK", b"junk", b"PAD ", b"FLLR", b"filr"}
REWRITE_IXML_HEADROOM = 4096  # spaces after the iXML document when a file is rewritten
WAV_FORMS = (b"RIFF", b"RF64", b"BW64")
_CHUNK_ID_RE = re.compile(rb"^[\x20-\x7e]{4}$")
_BEXT_LINE_RE = re.compile(r"^([a-z])([A-Z0-9_]+)=(.*)$")

# Editable fields: key -> (iXML tag, bext description key or None)
FIELDS = {
    "project": ("PROJECT", None),
    "scene": ("SCENE", "SCENE"),
    "take": ("TAKE", "TAKE"),
    "tape": ("TAPE", "TAPE"),
    "note": ("NOTE", "NOTE"),
    "circled": ("CIRCLED", "CIRCLED"),
}


class WavError(Exception):
    pass


class NeedsRewrite(WavError):
    """The change does not fit in place; the whole file would have to be copied."""


@dataclass
class Chunk:
    id: bytes
    offset: int  # position of the 8-byte chunk header
    size: int  # payload size (from ds64 for an RF64 data chunk)
    truncated: bool = False

    @property
    def data_offset(self) -> int:
        return self.offset + 8

    @property
    def end(self) -> int:
        return self.offset + 8 + self.size + (self.size & 1)


@dataclass
class Layout:
    form: bytes
    chunks: list[Chunk]
    file_size: int
    ds64_offset: int | None = None

    def first(self, cid: bytes) -> Chunk | None:
        return next((c for c in self.chunks if c.id == cid), None)

    @property
    def parsed_end(self) -> int:
        return self.chunks[-1].end if self.chunks else 12


@dataclass
class WavInfo:
    form: str = ""
    format_tag: int = 0
    channels: int = 0
    sample_rate: int = 0
    bits: int = 0
    block_align: int = 0
    data_size: int = 0
    truncated: bool = False
    bext: dict[str, str] = field(default_factory=dict)  # description keys, prefix removed
    originator: str = ""
    originator_reference: str = ""
    date: str = ""
    time: str = ""
    time_reference: int | None = None
    ixml: dict[str, str] = field(default_factory=dict)  # flattened, see _flatten_ixml
    tracks: list[str] = field(default_factory=list)
    has_bext: bool = False
    has_ixml: bool = False

    @property
    def frames(self) -> int:
        return self.data_size // self.block_align if self.block_align else 0

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    def value(self, key: str) -> str:
        """An editable field (see FIELDS), from iXML first, then bext."""
        tag, bext_key = FIELDS[key]
        text = self.ixml.get(tag, "")
        if not text and bext_key:
            text = self.bext.get(bext_key, "")
        return text.strip()


def read_layout(f, file_size: int) -> Layout:
    """Walk the RIFF chunks. Stops quietly at garbage (some recorders leave
    junk after the last chunk) and marks a data chunk cut short by a crash."""
    header = f.read(12)
    if len(header) < 12 or header[:4] not in WAV_FORMS or header[8:12] != b"WAVE":
        raise WavError("not a WAV file")
    layout = Layout(header[:4], [], file_size)
    ds64_data_size = None
    pos = 12
    while pos + 8 <= file_size:
        f.seek(pos)
        head = f.read(8)
        cid, size = head[:4], struct.unpack("<I", head[4:])[0]
        if not _CHUNK_ID_RE.match(cid):
            # Some writers forget the pad byte after an odd-sized chunk.
            if layout.chunks and layout.chunks[-1].size & 1 and pos - 1 > layout.chunks[-1].data_offset:
                f.seek(pos - 1)
                retry = f.read(8)
                if len(retry) == 8 and _CHUNK_ID_RE.match(retry[:4]):
                    pos -= 1
                    cid, size = retry[:4], struct.unpack("<I", retry[4:])[0]
                else:
                    break
            else:
                break
        if cid == b"ds64" and layout.ds64_offset is None:
            layout.ds64_offset = pos
            payload = f.read(min(size, 24))
            if len(payload) >= 16:
                ds64_data_size = struct.unpack("<Q", payload[8:16])[0]
        if cid == b"data" and size == 0xFFFFFFFF and ds64_data_size is not None:
            size = ds64_data_size
        chunk = Chunk(cid, pos, size)
        if chunk.data_offset + size > file_size:
            chunk.size = max(file_size - chunk.data_offset, 0)
            chunk.truncated = True
            layout.chunks.append(chunk)
            break
        layout.chunks.append(chunk)
        pos = chunk.end
    if layout.first(b"fmt ") is None:
        raise WavError("no fmt chunk")
    return layout


def _cstr(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("latin-1").strip()


def parse_bext_description(text: str) -> tuple[dict[str, str], str]:
    """sKEY=VALUE lines -> ({KEY: VALUE}, prefix letter)."""
    values: dict[str, str] = {}
    prefix = ""
    for line in re.split(r"\r?\n", text):
        match = _BEXT_LINE_RE.match(line.strip("\r"))
        if match:
            prefix = prefix or match.group(1)
            values.setdefault(match.group(2), match.group(3))
    return values, prefix


def _ixml_root(payload: bytes) -> ET.Element | None:
    # Recorders rewrite iXML in place: a shorter document over a longer one
    # leaves the old tail behind (seen on the 833: "</BWFXML>\n\0L>\n\0\0...").
    # Only what comes before the first NUL / after the root's end tag counts.
    text = payload.split(b"\0", 1)[0]
    end = text.rfind(b"</BWFXML>")
    if end >= 0:
        text = text[:end + len(b"</BWFXML>")]
    text = text.rstrip(b" \t\r\n")
    if not text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _flatten_ixml(root: ET.Element) -> tuple[dict[str, str], list[str]]:
    """Top-level text elements by tag, plus SPEED/*, HISTORY/*, FILE_SET/*
    children, plus the track names in interleave order."""
    values: dict[str, str] = {}
    for child in root:
        if len(child) == 0:
            values.setdefault(child.tag, (child.text or "").strip())
    for group in ("SPEED", "HISTORY", "FILE_SET"):
        node = root.find(group)
        if node is not None:
            for child in node:
                values.setdefault(child.tag, (child.text or "").strip())
    tracks: list[tuple[int, str]] = []
    for track in root.iterfind("TRACK_LIST/TRACK"):
        index = track.findtext("INTERLEAVE_INDEX") or track.findtext("CHANNEL_INDEX") or "0"
        try:
            order = int(index.strip())
        except ValueError:
            order = len(tracks) + 1
        tracks.append((order, (track.findtext("NAME") or "").strip()))
    return values, [name for _, name in sorted(tracks)]


def read_info(path: str | os.PathLike) -> WavInfo:
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        layout = read_layout(f, size)
        return _info_from(f, layout)


def _info_from(f, layout: Layout) -> WavInfo:
    info = WavInfo(form=layout.form.decode("ascii"))
    fmt = layout.first(b"fmt ")
    f.seek(fmt.data_offset)
    raw = f.read(min(fmt.size, 40))
    if len(raw) < 16:
        raise WavError("fmt chunk too short")
    info.format_tag, info.channels, info.sample_rate, _, info.block_align, info.bits = struct.unpack(
        "<HHIIHH", raw[:16])
    if info.format_tag == 0xFFFE and len(raw) >= 26:
        info.format_tag = struct.unpack("<H", raw[24:26])[0]
    data = layout.first(b"data")
    if data is not None:
        info.data_size = data.size
        info.truncated = data.truncated

    bext = layout.first(b"bext")
    if bext is not None and bext.size >= 346:
        f.seek(bext.data_offset)
        raw = f.read(346)
        info.has_bext = True
        info.bext, _ = parse_bext_description(_cstr(raw[:256]))
        info.originator = _cstr(raw[256:288])
        info.originator_reference = _cstr(raw[288:320])
        info.date = _cstr(raw[320:330])
        info.time = _cstr(raw[330:338])
        low, high = struct.unpack("<II", raw[338:346])
        info.time_reference = low | (high << 32)

    ixml = layout.first(b"iXML")
    if ixml is not None and ixml.size and not ixml.truncated:
        f.seek(ixml.data_offset)
        root = _ixml_root(f.read(ixml.size))
        if root is not None:
            info.has_ixml = True
            info.ixml, info.tracks = _flatten_ixml(root)
    if info.time_reference is None and info.ixml.get("TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO"):
        try:
            info.time_reference = int(info.ixml["TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO"]) | (
                int(info.ixml.get("TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI") or 0) << 32)
        except ValueError:
            pass
    if not info.tracks:
        numbered = []
        for key, name in info.bext.items():
            match = re.fullmatch(r"TRK(\d+)", key)
            if match:
                numbered.append((int(match.group(1)), name.strip()))
        info.tracks = [name for _, name in sorted(numbered)]
    return info


# ---------------------------------------------------------------- writing

def _ixml_value(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def build_ixml(old_payload: bytes | None, changes: dict, filename: str | None) -> bytes:
    """New iXML document with the changes applied (a minimal one if there was none)."""
    root = _ixml_root(old_payload) if old_payload else None
    if root is None:
        root = ET.Element("BWFXML")
        ET.SubElement(root, "IXML_VERSION").text = "1.61"
    for key, value in changes.items():
        tag = FIELDS[key][0]
        node = root.find(tag)
        if node is None:
            node = ET.SubElement(root, tag)
        node.text = _ixml_value(value)
    if filename is not None:
        history = root.find("HISTORY")
        if history is None:
            history = ET.SubElement(root, "HISTORY")
        if history.find("ORIGINAL_FILENAME") is None:
            # Remember where the file came from before its first rename.
            original = ET.SubElement(history, "ORIGINAL_FILENAME")
            original.text = filename
        current = history.find("CURRENT_FILENAME")
        if current is None:
            current = ET.SubElement(history, "CURRENT_FILENAME")
        current.text = filename
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


def _single_line(text: str) -> str:
    return re.sub(r"[\r\n]+", " ", text)


def build_bext_description(old: bytes, changes: dict, filename: str | None) -> bytes:
    """Update existing sKEY=VALUE lines (keys the recorder did not write are not
    added). Raises WavError if the result does not fit in 256 bytes."""
    text = _cstr(old)
    newline = "\r\n" if "\r\n" in text else "\n"
    updates = {}
    for key, value in changes.items():
        bext_key = FIELDS[key][1]
        if bext_key:
            updates[bext_key] = _single_line(_ixml_value(value))
    if filename is not None:
        updates["FILENAME"] = _single_line(filename)
    lines = []
    for line in re.split(r"\r?\n", text):
        match = _BEXT_LINE_RE.match(line)
        if match and match.group(2) in updates:
            line = f"{match.group(1)}{match.group(2)}={updates[match.group(2)]}"
        lines.append(line)
    new_text = newline.join(lines)
    if text.endswith(("\n",)) and not new_text.endswith(newline):
        new_text += newline
    encoded = new_text.encode("latin-1", errors="replace")
    overflow = len(encoded) - BEXT_DESCRIPTION_SIZE
    if overflow > 0 and "NOTE" in updates:
        # The description is only a legacy mirror of iXML, which keeps the full
        # note; shorten the note's copy here so it fits.
        note = updates["NOTE"].encode("latin-1", errors="replace")
        keep = max(len(note) - overflow, 0)
        short = note[:keep].decode("latin-1")
        encoded = re.sub(rb"(?m)^([a-z]NOTE=).*?(?=\r?$)", lambda m: m.group(1) + short.encode("latin-1"),
                         encoded, count=1)
    if len(encoded) > BEXT_DESCRIPTION_SIZE:
        raise WavError(
            f"the BWF description would be {len(encoded)} bytes (the limit is {BEXT_DESCRIPTION_SIZE}); "
            "shorten the note")
    return encoded.ljust(BEXT_DESCRIPTION_SIZE, b"\0")


@dataclass
class WriteResult:
    mode: str  # "in-place", "rewrite" or "unchanged"
    bytes_copied: int = 0


def update_metadata(path: str | os.PathLike, changes: dict, *, filename: str | None = None,
                    allow_rewrite: bool = False) -> WriteResult:
    """Write field changes (keys from FIELDS) and/or a new embedded filename.

    Raises NeedsRewrite when it does not fit in place and allow_rewrite is
    False (nothing is written in that case), WavError for other problems.
    The result is read back and checked before returning.
    """
    for key in changes:
        if key not in FIELDS:
            raise ValueError(f"unknown field {key!r}")
    if not changes and filename is None:
        return WriteResult("unchanged")
    path = os.fspath(path)
    with open(path, "rb") as f:
        layout = read_layout(f, os.fstat(f.fileno()).st_size)
        data = layout.first(b"data")
        if data is None or data.truncated:
            raise WavError("the audio data is incomplete (a recording cut short?); not writing to it")
        bext = layout.first(b"bext")
        new_description = None
        if bext is not None and bext.size >= BEXT_DESCRIPTION_SIZE:
            f.seek(bext.data_offset)
            new_description = build_bext_description(f.read(BEXT_DESCRIPTION_SIZE), changes, filename)
        ixml = layout.first(b"iXML")
        old_ixml = None
        if ixml is not None:
            f.seek(ixml.data_offset)
            old_ixml = f.read(ixml.size)
            if old_ixml and _ixml_root(old_ixml) is None:
                raise WavError("the existing iXML metadata could not be read; not replacing it")
        new_ixml = build_ixml(old_ixml, changes, filename)
        fmt = layout.first(b"fmt ")
        f.seek(fmt.data_offset)
        fmt_bytes = f.read(fmt.size)

    if ixml is None and not changes:
        # A rename of a file without iXML: only the bext FILENAME line (if the
        # recorder wrote one) is updated; no iXML chunk is added for it.
        if new_description is None or "FILENAME" not in parse_bext_description(_cstr(new_description))[0]:
            return WriteResult("unchanged")
        with open(path, "r+b") as f:
            f.seek(bext.data_offset)
            f.write(new_description)
            f.flush()
            os.fsync(f.fileno())
        _verify(path, data, fmt_bytes, {}, None)
        return WriteResult("in-place")

    plan = _plan_in_place(layout, ixml, len(new_ixml))
    if plan is not None:
        _write_in_place(path, bext, new_description, ixml, new_ixml, plan)
        result = WriteResult("in-place")
    elif not allow_rewrite:
        raise NeedsRewrite("the new metadata does not fit in the file's existing space")
    else:
        result = WriteResult("rewrite", _rewrite(path, layout, bext, new_description, new_ixml))
    _verify(path, data, fmt_bytes, changes, filename)
    return result


def _plan_in_place(layout: Layout, ixml: Chunk | None, needed: int) -> int | None:
    """New iXML chunk size if the document fits in place, else None."""
    if ixml is None:
        return None
    if needed <= ixml.size:
        return ixml.size
    index = layout.chunks.index(ixml)
    if index + 1 < len(layout.chunks):
        following = layout.chunks[index + 1]
        if following.id in FILLER_CHUNKS and following.offset == ixml.end:
            room = following.end - ixml.data_offset
            if needed <= room:
                return room
    return None


def _write_in_place(path: str, bext: Chunk | None, description: bytes | None,
                    ixml: Chunk, new_ixml: bytes, new_size: int) -> None:
    # Pad with spaces: trailing whitespace after the root element is valid XML
    # and is what recorders use themselves.
    payload = new_ixml.ljust(new_size, b" ")
    with open(path, "r+b") as f:
        if bext is not None and description is not None:
            f.seek(bext.data_offset)
            f.write(description)
        if new_size != ixml.size:
            f.seek(ixml.offset + 4)
            f.write(struct.pack("<I", new_size))
        f.seek(ixml.data_offset)
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def _copy_range(src, dst, offset: int, length: int) -> None:
    """Copy bytes between files; server-side on filesystems that support it
    (SMB/CIFS, NFS, btrfs), otherwise through a buffer. macOS has no
    copy_file_range, so there it is always the buffer."""
    src.flush()
    dst.flush()
    dst_pos = dst.tell()
    copied = 0
    try:
        while copied < length and hasattr(os, "copy_file_range"):
            n = os.copy_file_range(src.fileno(), dst.fileno(), length - copied,
                                   offset + copied, dst_pos + copied)
            if n == 0:
                break
            copied += n
    except OSError:
        pass
    src.seek(offset + copied)
    dst.seek(dst_pos + copied)
    while copied < length:
        block = src.read(min(8 << 20, length - copied))
        if not block:
            raise WavError("file ended while copying")
        dst.write(block)
        copied += len(block)


def _chunk_bytes(cid: bytes, payload: bytes) -> bytes:
    pad = b"\0" if len(payload) & 1 else b""
    return cid + struct.pack("<I", len(payload)) + payload + pad


def _rewrite(path: str, layout: Layout, bext: Chunk | None, description: bytes | None,
             new_ixml: bytes) -> int:
    """Copy the file with new bext/iXML chunks to a temp file next to it, check
    it, then atomically replace the original. Returns the bytes copied."""
    folder = os.path.dirname(path) or "."
    temp = compat.join(folder, f".sfm-tmp-{uuid.uuid4().hex}.wav")
    had_ixml = layout.first(b"iXML") is not None
    # Room to grow, as recorders leave: later edits (a longer note, a new name)
    # then fit in place instead of copying the whole file again.
    new_ixml = new_ixml + b" " * REWRITE_IXML_HEADROOM
    ds64_new = None
    try:
        with open(path, "rb") as src, open(temp, "wb") as dst:
            dst.write(layout.form + b"\0\0\0\0WAVE")
            for chunk in layout.chunks:
                if chunk.id == b"ds64":
                    ds64_new = dst.tell()
                if chunk is bext and description is not None:
                    src.seek(chunk.data_offset)
                    payload = bytearray(src.read(chunk.size))
                    payload[:BEXT_DESCRIPTION_SIZE] = description
                    dst.write(_chunk_bytes(b"bext", bytes(payload)))
                    continue
                if chunk.id == b"iXML":
                    dst.write(_chunk_bytes(b"iXML", new_ixml))
                    continue
                if chunk.id == b"data" and not had_ixml:
                    dst.write(_chunk_bytes(b"iXML", new_ixml))
                header_and_payload = 8 + chunk.size
                _copy_range(src, dst, chunk.offset, header_and_payload)
                if chunk.size & 1:
                    dst.write(b"\0")
            # Anything after the last readable chunk is kept as it was.
            tail = layout.file_size - layout.parsed_end
            if tail > 0:
                _copy_range(src, dst, layout.parsed_end, tail)
            total = dst.tell()
            riff_size = total - 8
            if layout.form == b"RIFF":
                if riff_size > 0xFFFFFFFF:
                    raise WavError("the file would exceed 4 GB")
                dst.seek(4)
                dst.write(struct.pack("<I", riff_size))
            else:
                dst.seek(4)
                dst.write(b"\xff\xff\xff\xff")
                if ds64_new is not None:
                    dst.seek(ds64_new + 8)
                    dst.write(struct.pack("<Q", riff_size))
            dst.flush()
            os.fsync(dst.fileno())
        with open(temp, "rb") as f:
            check = read_layout(f, os.fstat(f.fileno()).st_size)
        new_data, old_data = check.first(b"data"), layout.first(b"data")
        if new_data is None or new_data.size != old_data.size or new_data.truncated:
            raise WavError("the copy did not verify (audio data size differs)")
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return total


def _verify(path: str, old_data: Chunk, fmt_bytes: bytes, changes: dict, filename: str | None) -> None:
    with open(path, "rb") as f:
        layout = read_layout(f, os.fstat(f.fileno()).st_size)
        data = layout.first(b"data")
        fmt = layout.first(b"fmt ")
        f.seek(fmt.data_offset)
        if f.read(fmt.size) != fmt_bytes or data is None or data.size != old_data.size:
            raise WavError("verification failed after writing: the audio format or data changed")
        info = _info_from(f, layout)
    for key, value in changes.items():
        if info.ixml.get(FIELDS[key][0], "") != _ixml_value(value).strip():
            raise WavError(f"verification failed after writing: {key} did not read back")
    if filename is not None and info.ixml.get("CURRENT_FILENAME") != filename:
        raise WavError("verification failed after writing: the filename did not read back")


def read_cues(path: str) -> list[tuple[int, str]]:
    """Cue markers a recorder or editor wrote into the file: (frame, label),
    from the "cue " chunk and the labels in a LIST/adtl chunk. Read only."""
    with open(path, "rb") as f:
        layout = read_layout(f, os.fstat(f.fileno()).st_size)
        cue = layout.first(b"cue ")
        if cue is None or cue.size < 4:
            return []
        f.seek(cue.data_offset)
        payload = f.read(min(cue.size, 1 << 20))
        count = struct.unpack_from("<I", payload, 0)[0]
        points: dict[int, int] = {}
        for i in range(min(count, (len(payload) - 4) // 24)):
            cue_id, _position, _chunk, _chunk_start, _block_start, offset = \
                struct.unpack_from("<II4sIII", payload, 4 + i * 24)
            points[cue_id] = offset
        labels: dict[int, str] = {}
        for chunk in layout.chunks:
            if chunk.id != b"LIST" or chunk.size < 4:
                continue
            f.seek(chunk.data_offset)
            body = f.read(min(chunk.size, 1 << 20))
            if body[:4] != b"adtl":
                continue
            pos = 4
            while pos + 8 <= len(body):
                sub_id, sub_size = struct.unpack_from("<4sI", body, pos)
                if sub_id in (b"labl", b"note") and sub_size >= 4:
                    cue_id = struct.unpack_from("<I", body, pos + 8)[0]
                    text = body[pos + 12:pos + 8 + sub_size].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
                    if sub_id == b"labl" or cue_id not in labels:
                        labels[cue_id] = text
                pos += 8 + sub_size + (sub_size & 1)
    return sorted((frame, labels.get(cue_id, "") or f"Cue {n + 1}")
                  for n, (cue_id, frame) in enumerate(sorted(points.items(), key=lambda kv: kv[1])))
