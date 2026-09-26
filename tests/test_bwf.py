import os
import struct
import tempfile
import unittest
from pathlib import Path

from sound_file_manager import bwf

from .wavmaker import make_wav


def audio_bytes(path):
    with open(path, "rb") as f:
        layout = bwf.read_layout(f, os.path.getsize(path))
        data = layout.first(b"data")
        f.seek(data.data_offset)
        return f.read(data.size)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.wav")

    def tearDown(self):
        self.dir.cleanup()

    def test_sound_devices_layout(self):
        make_wav(self.path, project="Harbor", scene="Test 1", take="02")
        info = bwf.read_info(self.path)
        self.assertEqual((info.channels, info.sample_rate, info.bits), (2, 48000, 24))
        self.assertEqual(info.value("project"), "Harbor")
        self.assertEqual(info.value("scene"), "Test 1")
        self.assertEqual(info.value("take"), "02")
        self.assertEqual(info.value("circled"), "FALSE")
        self.assertEqual(info.tracks, ["BOOM", "LAV"])  # sorted by interleave index
        self.assertEqual(info.time_reference, 48000 * 3600)
        self.assertEqual(info.bext["SPEED"], "023.976-ND")
        self.assertEqual(info.ixml["TIMECODE_RATE"], "24000/1001")
        self.assertEqual(info.ixml["FAMILY_UID"], "FAM1")
        self.assertAlmostEqual(info.duration, 0.1)

    def test_zoom_layout_and_bext_fallback(self):
        make_wav(self.path, layout="zoom", with_ixml=False, scene="56A", take="005")
        info = bwf.read_info(self.path)
        self.assertFalse(info.has_ixml)
        self.assertEqual(info.value("scene"), "56A")
        self.assertEqual(info.value("take"), "005")
        self.assertEqual(info.value("project"), "")

    def test_rf64(self):
        data = make_wav(self.path, rf64=True)
        info = bwf.read_info(self.path)
        self.assertEqual(info.form, "RF64")
        self.assertEqual(info.data_size, len(data))

    def test_stale_ixml_tail_after_nul(self):
        # The 833 leaves the end of a previous, longer iXML after a NUL.
        make_wav(self.path, ixml_padding=0)
        raw = Path(self.path).read_bytes()
        end = raw.index(b"</BWFXML>") + len(b"</BWFXML>\n")
        size_at = raw.index(b"iXML") + 4
        size = struct.unpack("<I", raw[size_at:size_at + 4])[0]
        tail = b"\0L>\n" + b"\0" * 12  # even length keeps the chunk padding as it was
        raw = raw[:end] + tail + raw[end:]
        raw = raw[:size_at] + struct.pack("<I", size + len(tail)) + raw[size_at + 4:]
        raw = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
        Path(self.path).write_bytes(raw)
        info = bwf.read_info(self.path)
        self.assertTrue(info.has_ixml)
        self.assertEqual(info.value("project"), "Proj")
        bwf.update_metadata(self.path, {"circled": True})
        self.assertEqual(bwf.read_info(self.path).value("circled"), "TRUE")

    def test_trailing_garbage_is_ignored(self):
        make_wav(self.path, extra_tail=b"\x00\x01garbage!")
        self.assertEqual(bwf.read_info(self.path).value("project"), "Proj")

    def test_truncated_data(self):
        make_wav(self.path)
        with open(self.path, "r+b") as f:
            f.truncate(os.path.getsize(self.path) - 1000)
        info = bwf.read_info(self.path)
        self.assertTrue(info.truncated)
        with self.assertRaises(bwf.WavError):
            bwf.update_metadata(self.path, {"scene": "1"})

    def test_not_a_wav(self):
        with open(self.path, "wb") as f:
            f.write(b"\0" * 100)
        with self.assertRaises(bwf.WavError):
            bwf.read_info(self.path)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.wav")

    def tearDown(self):
        self.dir.cleanup()

    def test_in_place_edit_keeps_audio_and_size(self):
        data = make_wav(self.path)
        size = os.path.getsize(self.path)
        result = bwf.update_metadata(self.path, {"scene": "12B", "take": "7", "circled": True,
                                                 "note": "good one", "project": "New"})
        self.assertEqual(result.mode, "in-place")
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(audio_bytes(self.path), data)
        info = bwf.read_info(self.path)
        self.assertEqual(info.value("scene"), "12B")
        self.assertEqual(info.value("circled"), "TRUE")
        self.assertEqual(info.bext["SCENE"], "12B")
        self.assertEqual(info.bext["CIRCLED"], "TRUE")
        self.assertEqual(info.bext["NOTE"], "good one")
        self.assertEqual(info.tracks, ["BOOM", "LAV"])  # the rest of iXML survives

    def test_embedded_filename(self):
        make_wav(self.path, filename="10T03_ISO.wav")
        bwf.update_metadata(self.path, {}, filename="Scene 10 T03.wav")
        info = bwf.read_info(self.path)
        self.assertEqual(info.ixml["CURRENT_FILENAME"], "Scene 10 T03.wav")
        self.assertEqual(info.ixml["ORIGINAL_FILENAME"], "10T03_ISO.wav")
        self.assertEqual(info.bext["FILENAME"], "Scene 10 T03.wav")

    def test_rename_without_ixml_touches_only_bext(self):
        make_wav(self.path, with_ixml=False)
        size = os.path.getsize(self.path)
        self.assertEqual(bwf.update_metadata(self.path, {}, filename="x.wav").mode, "in-place")
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(bwf.read_info(self.path).bext["FILENAME"], "x.wav")

    def test_growth_absorbs_following_junk(self):
        # Zoom layout: iXML is followed by fmt, so no; SD: iXML followed by fmt too.
        # Build one with JUNK right after iXML by hand.
        make_wav(self.path, ixml_padding=0)
        raw = Path(self.path).read_bytes()
        at = raw.index(b"fmt ")
        raw = raw[:at] + b"JUNK" + struct.pack("<I", 2000) + b"\0" * 2000 + raw[at:]
        raw = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
        Path(self.path).write_bytes(raw)
        size = len(raw)
        result = bwf.update_metadata(self.path, {"note": "n" * 1500})
        self.assertEqual(result.mode, "in-place")
        self.assertEqual(os.path.getsize(self.path), size)
        self.assertEqual(bwf.read_info(self.path).value("note"), "n" * 1500)

    def test_needs_rewrite_writes_nothing(self):
        make_wav(self.path, ixml_padding=0)
        before = Path(self.path).read_bytes()
        with self.assertRaises(bwf.NeedsRewrite):
            bwf.update_metadata(self.path, {"note": "n" * 5000})
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_rewrite(self):
        data = make_wav(self.path, ixml_padding=0, extra_tail=b"TAIL")
        result = bwf.update_metadata(self.path, {"note": "n" * 5000}, allow_rewrite=True)
        self.assertEqual(result.mode, "rewrite")
        self.assertEqual(audio_bytes(self.path), data)
        info = bwf.read_info(self.path)
        self.assertEqual(info.value("note"), "n" * 5000)
        self.assertTrue(len(info.bext["NOTE"]) < 256)  # shortened copy in bext
        self.assertTrue(Path(self.path).read_bytes().endswith(b"TAIL"))
        self.assertEqual(os.listdir(self.dir.name), ["a.wav"])  # no temp file left

    def test_rewrite_adds_missing_ixml(self):
        data = make_wav(self.path, with_ixml=False)
        with self.assertRaises(bwf.NeedsRewrite):
            bwf.update_metadata(self.path, {"project": "Found"})
        bwf.update_metadata(self.path, {"project": "Found"}, allow_rewrite=True)
        info = bwf.read_info(self.path)
        self.assertEqual(info.value("project"), "Found")
        self.assertEqual(audio_bytes(self.path), data)

    def test_rewrite_rf64_updates_ds64(self):
        data = make_wav(self.path, rf64=True, ixml_padding=0)
        bwf.update_metadata(self.path, {"note": "n" * 3000}, allow_rewrite=True)
        raw = Path(self.path).read_bytes()
        self.assertEqual(raw[:4], b"RF64")
        riff_size = struct.unpack("<Q", raw[20:28])[0]
        self.assertEqual(riff_size, len(raw) - 8)
        self.assertEqual(audio_bytes(self.path), data)

    def test_bext_overflow_without_note_is_refused(self):
        make_wav(self.path)
        with self.assertRaises(bwf.WavError):
            bwf.update_metadata(self.path, {"scene": "s" * 300})

    def test_newlines_do_not_break_bext(self):
        make_wav(self.path)
        bwf.update_metadata(self.path, {"note": "one\ntwo"})
        info = bwf.read_info(self.path)
        self.assertEqual(info.bext["NOTE"], "one two")
        self.assertEqual(info.bext["TAPE"], "25Y10M27")

    def test_unknown_field(self):
        make_wav(self.path)
        with self.assertRaises(ValueError):
            bwf.update_metadata(self.path, {"bogus": "1"})


if __name__ == "__main__":
    unittest.main()
