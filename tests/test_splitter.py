"""Splitting multi-track WAVs into one file per track."""
import os
import tempfile
import unittest

import numpy as np

from sound_file_manager import bwf, catalog, splitter, waveform
from .wavmaker import make_wav


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _rec(self, name="10T03_ISO.wav", **kwargs):
        path = os.path.join(self.dir, name).replace(os.sep, "/")
        make_wav(path, filename=name, **kwargs)
        return catalog.read_recording(path)

    def _channels(self, path):
        info = bwf.read_info(path)
        with open(path, "rb") as f:
            layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
            data = layout.first(b"data")
            f.seek(data.data_offset)
            raw = f.read(data.size)
        return waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)

    def test_each_track_gets_its_own_file_with_metadata(self):
        rec = self._rec(levels=[0x100000, -0x200000], frames=4801)  # odd: the 24-bit data needs a pad byte
        plan = splitter.plan_split([rec], lambda r: [])
        self.assertEqual(plan.problems, [])
        files = plan.splits[0][1]
        self.assertEqual([os.path.basename(f.dst) for f in files], ["BOOM.wav", "LAV.wav"])
        self.assertEqual(os.path.basename(os.path.dirname(files[0].dst)), "10T03")
        written = splitter.split_file(rec.path, files)
        source = self._channels(rec.path)
        for channel, path in enumerate(written):
            out = catalog.read_recording(path)
            self.assertEqual((out.channels, out.frames, out.bits), (1, rec.frames, 24))
            self.assertEqual((out.scene, out.take, out.family), (rec.scene, rec.take, rec.family))
            self.assertEqual(out.time_reference, rec.time_reference)
            self.assertEqual(out.tracks, [rec.tracks[channel]])
            self.assertTrue(splitter.is_track_file(out))
            np.testing.assert_array_equal(self._channels(path)[:, 0], source[:, channel])
            info = bwf.read_info(path)
            self.assertEqual(info.ixml["PARENT_FILENAME"], "10T03_ISO.wav")
            self.assertEqual(info.ixml["CURRENT_FILENAME"], os.path.basename(path))
            self.assertEqual(info.bext["FILENAME"], os.path.basename(path))
            self.assertEqual(os.path.getsize(path) % 2, 0)
        self.assertEqual(sorted(os.listdir(os.path.dirname(written[0]))), ["BOOM.wav", "LAV.wav"])  # no temp files

    def test_float_files(self):
        samples = np.array([[0.5, -1.25, 0.1]] * 100, np.float32)
        rec = self._rec(float_samples=samples, extensible=True)
        files = [splitter.TrackFile(c, n, os.path.join(self.dir, "out", f"{c}.wav").replace(os.sep, "/"))
                 for c, n in enumerate(["A", "B", "C"])]
        for channel, path in enumerate(splitter.split_file(rec.path, files)):
            out = catalog.read_recording(path)
            self.assertTrue(out.float_samples)
            np.testing.assert_array_equal(self._channels(path)[:, 0], samples[:, channel])

    def test_names_repeats_and_unnamed_tracks(self):
        rec = self._rec()
        rec.tracks = ["Boom", "Boom"]
        rec.channels = 3
        files = splitter.plan_split([rec], lambda r: []).splits[0][1]
        self.assertEqual([os.path.basename(f.dst) for f in files], ["Boom.wav", "Boom_2.wav", "Track 3.wav"])

    def test_partners_move_along_and_clashes_block(self):
        iso = self._rec()
        lr = self._rec("10T03_LR.wav")
        plan = splitter.plan_split([iso], lambda r: [lr])
        self.assertEqual([(r.name, os.path.basename(os.path.dirname(p))) for r, p in plan.partners],
                         [("10T03_LR.wav", "10T03")])
        os.makedirs(os.path.join(self.dir, "10T03"))
        open(os.path.join(self.dir, "10T03", "BOOM.wav"), "w").close()
        self.assertEqual(len(splitter.plan_split([iso], lambda r: []).problems), 1)

    def test_mono_files_are_not_split(self):
        rec = self._rec()
        rec.channels = 1
        plan = splitter.plan_split([rec], lambda r: [])
        self.assertEqual((plan.splits, len(plan.skipped)), ([], 1))

    def test_chosen_tracks_and_the_original_kept_whole(self):
        rec = self._rec(levels=[0x100000, -0x200000, 0x300000])
        plan = splitter.plan_split([rec], lambda r: [], keep=False, picked={rec.path: {1}})
        self.assertEqual([os.path.basename(f.dst) for f in plan.splits[0][1]], ["LAV.wav"])
        # Tracks stay with it, so the original moves into the folder even if "keep" is off.
        self.assertEqual((plan.remainders, plan.originals_in_folder), ({}, {rec.path}))
        nothing = splitter.plan_split([rec], lambda r: [], picked={rec.path: set()})
        self.assertEqual((nothing.splits, len(nothing.skipped)), ([], 1))

    def test_shrunk_original_keeps_the_other_tracks(self):
        rec = self._rec(levels=[0x100000, -0x200000, 0x300000], frames=4801)
        plan = splitter.plan_split([rec], lambda r: [], picked={rec.path: {1}}, shrink=True)
        self.assertEqual(plan.originals_in_folder, set())
        rest = plan.remainders[rec.path]
        self.assertEqual((rest.picks, rest.dst), ((0, 2), plan.kept_path(rec)))
        written = splitter.split_file(rec.path, plan.outputs(rec, plan.splits[0][1]))
        self.assertEqual([os.path.basename(p) for p in written], ["LAV.wav", "10T03_ISO.wav"])
        shrunk = catalog.read_recording(written[1])
        self.assertEqual((shrunk.channels, shrunk.frames, shrunk.tracks), (2, rec.frames, ["BOOM", ""]))
        self.assertEqual((shrunk.scene, shrunk.take, shrunk.family), (rec.scene, rec.take, rec.family))
        self.assertFalse(splitter.is_track_file(shrunk))
        np.testing.assert_array_equal(self._channels(written[1]), self._channels(rec.path)[:, [0, 2]])

    def test_tracks_grouped_into_one_file(self):
        rec = self._rec(levels=[0x100000, -0x200000, 0x300000])
        plan = splitter.plan_split([rec], lambda r: [], groups={rec.path: [("", [1, 2])]})
        files = plan.splits[0][1]
        self.assertEqual([(os.path.basename(f.dst), f.picks) for f in files],
                         [("BOOM.wav", (0,)), ("LAV+Track 3.wav", (1, 2))])
        named = splitter.plan_split([rec], lambda r: [], picked={rec.path: {0, 2}},
                                    groups={rec.path: [("Lavs", [1, 2])]}).splits[0][1]
        self.assertEqual([(os.path.basename(f.dst), f.picks) for f in named], [("BOOM.wav", (0,)), ("Lavs.wav", (2,))])
        written = splitter.split_file(rec.path, files)
        group = catalog.read_recording(written[1])
        self.assertEqual((group.channels, group.tracks), (2, ["LAV", ""]))
        self.assertTrue(splitter.is_track_file(group))  # a scene/take edit doesn't rename it
        np.testing.assert_array_equal(self._channels(written[1]), self._channels(rec.path)[:, [1, 2]])
        original = catalog.Recording(written[0].rsplit("/", 1)[0] + "/10T04_ISO.wav", 0, 0, scene="10", take="04",
                                     tracks=["A", "B"], original_filename="10T03_ISO.wav")
        self.assertFalse(splitter.is_track_file(original))

    def test_combine_mono_files_back_into_a_polywav(self):
        rec = self._rec(levels=[0x100000, -0x200000, 0x300000], frames=4801)
        plan = splitter.plan_split([rec], lambda r: [])
        written = splitter.split_file(rec.path, plan.splits[0][1])
        parts = [catalog.read_recording(p) for p in written]
        self.assertEqual(splitter.combine_problems(parts), [])
        self.assertEqual(splitter.combined_name(parts), "10T03.wav")  # the take they came from
        dst = os.path.join(os.path.dirname(written[0]), "10T03.wav").replace(os.sep, "/")
        splitter.combine_files(written, dst)
        out = catalog.read_recording(dst)
        self.assertEqual((out.channels, out.frames, out.tracks), (3, rec.frames, ["BOOM", "LAV", ""]))
        self.assertEqual((out.scene, out.take, out.family, out.time_reference),
                         (rec.scene, rec.take, rec.family, rec.time_reference))
        np.testing.assert_array_equal(self._channels(dst), self._channels(rec.path))
        self.assertEqual(bwf.read_info(dst).bext["FILENAME"], "10T03.wav")
        # In another order, the tracks follow.
        other = dst.replace("10T03.wav", "LAV+BOOM.wav")
        splitter.combine_files([written[1], written[0]], other)
        self.assertEqual(catalog.read_recording(other).tracks, ["LAV", "BOOM"])
        np.testing.assert_array_equal(self._channels(other), self._channels(rec.path)[:, [1, 0]])
        # That name is now taken: the files' names are added.
        self.assertEqual(splitter.combined_name(parts[:2]), "10T03_BOOM+LAV.wav")

    def test_combined_name_from_scene_and_take(self):
        a = catalog.Recording(self.dir + "/Boom.wav", 0, 0, scene="2B", take="001",
                              original_filename="2B-T001.WAV")
        b = catalog.Recording(self.dir + "/Lav.wav", 0, 0, scene="2B", take="001",
                              original_filename="2B-T001.WAV")
        self.assertEqual(splitter.combined_name([a, b]), "2B-T001.wav")  # the recorder's style
        a.original_filename = b.original_filename = ""
        self.assertEqual(splitter.combined_name([a, b]), "2BT001.wav")
        a.scene = b.scene = ""
        self.assertEqual(splitter.combined_name([a, b]), "Boom+Lav.wav")

    def test_only_the_same_take_combines(self):
        a = self._rec("a.wav", frames=4800)
        b = self._rec("b.wav", frames=4700)
        c = self._rec("c.wav", frames=4800, time_reference=12345)
        self.assertEqual(splitter.combine_problems([a, a]), [])
        self.assertIn("length", splitter.combine_problems([a, b])[0])
        self.assertIn("timecode", splitter.combine_problems([a, c])[0])
        self.assertTrue(splitter.combine_problems([a]))

    def test_track_lines_in_bext(self):
        old = "sSCENE=1\r\nsTRK1=Boom\r\nsTRK2=Lav\r\nsFILENAME=1T1.WAV\r\n".encode().ljust(256, b"\0") + b"x" * 90
        new = splitter.track_bext(old, (1,), "Lav.WAV")
        self.assertEqual(bwf._cstr(new[:256]), "sSCENE=1\r\nsTRK1=Lav\r\nsFILENAME=Lav.WAV")
        self.assertEqual(new[256:], b"x" * 90)
        both = splitter.track_bext(old, (0, 1), "1T1.WAV")
        self.assertEqual(bwf._cstr(both[:256]), "sSCENE=1\r\nsTRK1=Boom\r\nsTRK2=Lav\r\nsFILENAME=1T1.WAV")

    def test_big_files_get_an_rf64_header(self):
        info = bwf.WavInfo(format_tag=1, channels=8, sample_rate=48000, bits=24, block_align=24)
        tf = splitter.TrackFile(0, "Boom", "/x/Boom.wav")
        frames = 1_500_000_000  # 4.5 GB of mono 24-bit
        header = splitter._header(info, frames, 3, {}, tf, "Boom.wav", "1T1.wav", 0, 8)
        self.assertEqual(header[:4], b"RF64")
        small = splitter._header(info, 48000, 3, {}, tf, "Boom.wav", "1T1.wav", 0, 8)
        self.assertEqual(small[:4], b"RIFF")


if __name__ == "__main__":
    unittest.main()
