"""Build small synthetic BWF files for the tests (layouts copied from real recorders)."""

from __future__ import annotations

import struct

SD_IXML = """<?xml version="1.0" encoding="UTF-8"?>
<BWFXML>
\t<IXML_VERSION>1.5</IXML_VERSION>
\t<PROJECT>{project}</PROJECT>
\t<SCENE>{scene}</SCENE>
\t<TAKE>{take}</TAKE>
\t<TAPE>25Y10M27</TAPE>
\t<CIRCLED>FALSE</CIRCLED>
\t<NOTE></NOTE>
\t<SPEED>
\t\t<NOTE></NOTE>
\t\t<TIMECODE_FLAG>NDF</TIMECODE_FLAG>
\t\t<TIMECODE_RATE>24000/1001</TIMECODE_RATE>
\t</SPEED>
\t<HISTORY>
\t\t<ORIGINAL_FILENAME>{filename}</ORIGINAL_FILENAME>
\t\t<CURRENT_FILENAME>{filename}</CURRENT_FILENAME>
\t</HISTORY>
\t<FILE_SET>
\t\t<FAMILY_UID>FAM1</FAMILY_UID>
\t</FILE_SET>
\t<TRACK_LIST>
\t\t<TRACK><CHANNEL_INDEX>2</CHANNEL_INDEX><INTERLEAVE_INDEX>2</INTERLEAVE_INDEX><NAME>LAV</NAME></TRACK>
\t\t<TRACK><CHANNEL_INDEX>1</CHANNEL_INDEX><INTERLEAVE_INDEX>1</INTERLEAVE_INDEX><NAME>BOOM</NAME></TRACK>
\t</TRACK_LIST>
</BWFXML>
"""


def chunk(cid: bytes, payload: bytes) -> bytes:
    return cid + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def fmt_chunk(channels=2, rate=48000, bits=24) -> bytes:
    align = channels * bits // 8
    return chunk(b"fmt ", struct.pack("<HHIIHH", 1, channels, rate, rate * align, align, bits))


def bext_chunk(description: str, time_reference: int = 0, originator="TEST REC") -> bytes:
    desc = description.encode("latin-1").ljust(256, b"\0")
    payload = (desc + originator.encode().ljust(32, b"\0") + b"REF".ljust(32, b"\0")
               + b"2025-10-27" + b"17:55:45" + struct.pack("<II", time_reference & 0xFFFFFFFF, time_reference >> 32)
               + struct.pack("<H", 1) + b"\0" * 64 + b"\0" * 190)
    return chunk(b"bext", payload)


def audio(frames=4800, channels=2, bits=24, level=0x100000) -> bytes:
    width = bits // 8
    sample = level.to_bytes(4, "little", signed=True)[:width]
    return sample * (frames * channels)


def make_wav(path, *, project="Proj", scene="10", take="03", filename="10T03_ISO.wav",
             layout="sd", ixml_padding=400, frames=4800, time_reference=48000 * 3600, extra_tail=b"",
             rf64=False, with_ixml=True, with_bext=True) -> bytes:
    """Write a WAV and return its audio data bytes.

    layout "sd": JUNK bext iXML fmt data (Sound Devices); "zoom": bext iXML fmt PAD data.
    """
    description = (f"sSPEED=023.976-ND\r\nsTAKE={take}\r\nsSCENE={scene}\r\nsFILENAME={filename}\r\n"
                   f"sTAPE=25Y10M27\r\nsCIRCLED=FALSE\r\nsNOTE=\r\n")
    if layout == "zoom":
        # Zoom uses the "z" prefix and writes no FILENAME line.
        description = "".join("z" + line[1:] + "\r\n" for line in description.split("\r\n")
                              if line and not line.startswith("sFILENAME"))
    ixml = SD_IXML.format(project=project, scene=scene, take=take, filename=filename).encode()
    ixml += b" " * ixml_padding
    data = audio(frames)
    chunks = []
    if rf64:
        chunks.append(chunk(b"ds64", struct.pack("<QQQI", 0, len(data), frames, 0)))
    elif layout == "sd":
        chunks.append(chunk(b"JUNK", b"\0" * 28))
    if with_bext:
        chunks.append(bext_chunk(description, time_reference))
    if with_ixml:
        chunks.append(chunk(b"iXML", ixml))
    chunks.append(fmt_chunk())
    if layout == "zoom":
        chunks.append(chunk(b"PAD ", b"\0" * 200))
    if rf64:
        chunks.append(b"data" + b"\xff\xff\xff\xff" + data)
    else:
        chunks.append(chunk(b"data", data))
    body = b"WAVE" + b"".join(chunks)
    if rf64:
        body = body.replace(struct.pack("<QQQI", 0, len(data), frames, 0),
                            struct.pack("<QQQI", len(body), len(data), frames, 0), 1)
        blob = b"RF64" + b"\xff\xff\xff\xff" + body
    else:
        blob = b"RIFF" + struct.pack("<I", len(body)) + body
    with open(path, "wb") as f:
        f.write(blob + extra_tail)
    return data
