import math
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from sound_file_manager import compat, mixer, organize, waveform
from sound_file_manager.catalog import Recording
from sound_file_manager.mixer import OFF, Lane, MixerState

from .wavmaker import make_wav


class MixerStateTest(unittest.TestCase):
    def test_unity_centre_and_trim(self):
        state = MixerState.for_tracks(["BOOM"])
        np.testing.assert_allclose(state.matrix(), [[1.0, 1.0]], atol=1e-6)
        state = MixerState.for_tracks(["A", "B", "C", "D"])
        np.testing.assert_allclose(state.matrix()[:, 0], [0.5] * 4, atol=1e-6)  # 1/sqrt(4)
        state.auto_trim = False
        np.testing.assert_allclose(state.matrix()[:, 0], [1.0] * 4, atol=1e-6)

    def test_mute_solo(self):
        state = MixerState.for_tracks(["A", "B", "C"])
        state.set_solo(1, True)
        self.assertEqual([state.audible(i) for i in range(3)], [False, True, False])
        self.assertEqual(list(state.matrix()[:, 0] > 0), [False, True, False])
        state.set_mute(1, True)  # mute wins over solo
        self.assertFalse(state.audible(1))
        state.set_mute(1, False)
        state.set_solo(2, True)
        self.assertTrue(state.audible(1) and state.audible(2))
        state.exclusive_solo = True
        state.set_solo(0, True)
        self.assertEqual([c.solo for c in state.channels], [True, False, False])

    def test_link_pans_and_follows(self):
        state = MixerState.for_tracks(["L", "R", "C"])
        state.set_link(0, True)
        self.assertEqual((state.channels[0].pan, state.channels[1].pan), (-1.0, 1.0))
        state.set_gain(1, -6.0)
        self.assertEqual(state.channels[0].gain_db, -6.0)
        state.set_mute(0, True)
        self.assertTrue(state.channels[1].mute)
        self.assertFalse(state.channels[2].mute)
        m = state.matrix()
        self.assertEqual(m[0, 1], 0.0)  # hard left
        state.set_pan(0, -0.5)
        self.assertEqual(state.channels[1].pan, 0.5)
        self.assertTrue(MixerState.for_tracks(["a", "b"], stereo_pair=True).channels[0].link)

    def test_fader_off_and_master(self):
        state = MixerState.for_tracks(["A"])
        state.set_gain(0, -100)
        self.assertEqual(state.channels[0].gain_db, OFF)
        self.assertEqual(state.matrix()[0, 0], 0.0)
        state.set_gain(0, 0.0)
        state.master_db = -6.0
        self.assertAlmostEqual(state.matrix()[0, 0], 10 ** (-6 / 20), places=5)
        state.master_mute = True
        self.assertEqual(state.matrix().max(), 0.0)

    def test_lane(self):
        lane = Lane([(0, 0.0), (100, -10.0)])
        self.assertEqual(lane.value_at(50), -5.0)
        self.assertEqual(lane.value_at(50, step=True), 0.0)
        self.assertEqual(lane.value_at(500), -10.0)
        lane.write(20, 60, -3.0)
        self.assertEqual(lane.frames, [0, 20, 60, 100])
        self.assertEqual(lane.value_at(40), -3.0)

    def test_record_and_playback(self):
        state = MixerState.for_tracks(["A", "B"])
        state.set_armed(0, True)
        state.record(0, jumped=True)
        state.set_gain(0, -20.0)
        state.record(1000)
        state.set_gain(0, 0.0)
        state.record(2000)
        state.set_armed(0, False)
        self.assertTrue(state.has_automation(0))
        self.assertFalse(state.has_automation(1))
        # Each write holds the value over the stretch since the previous one.
        self.assertAlmostEqual(state.values_at(0, 0)[0], -20.0)
        self.assertAlmostEqual(state.values_at(0, 1500)[0], 0.0)
        # The fader position itself doesn't matter while automation plays back.
        state.set_gain(0, -40.0)
        self.assertAlmostEqual(state.values_at(0, 0)[0], -20.0)
        state.clear_automation(0)
        self.assertEqual(state.values_at(0, 0)[0], -40.0)

    def test_save_load(self):
        state = MixerState.for_tracks(["BOOM", "LAV"])
        state.set_gain(1, OFF)
        state.set_pan(0, 0.25)
        state.set_armed(0, True)
        state.record(0, True)
        state.record(480)
        data = state.to_dict()
        other = MixerState.for_tracks(["BOOM", "LAV"])
        self.assertEqual(other.apply_dict(data), [])
        self.assertEqual(other.channels[1].gain_db, OFF)
        self.assertEqual(other.channels[0].pan, 0.25)
        self.assertTrue(other.has_automation(0))
        warnings = MixerState.for_tracks(["X", "Y", "Z"]).apply_dict(data)
        self.assertEqual(len(warnings), 1)
        with self.assertRaises(ValueError):
            other.apply_dict({"nope": 1})

    def test_formatting(self):
        self.assertEqual(mixer.format_db(OFF), "-inf")
        self.assertEqual(mixer.format_db(0.0), "0.0 dB")
        self.assertEqual(mixer.format_db(-6.04), "-6.0 dB")
        self.assertEqual(mixer.format_pan(-0.5), "L50")
        self.assertEqual(mixer.format_pan(0.0), "C")
        left, right = mixer.pan_gains(-1.0)
        self.assertAlmostEqual(left, math.sqrt(2))
        self.assertAlmostEqual(right, 0.0)


class WaveformLevelsTest(unittest.TestCase):
    def test_peak_and_rms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = compat.join(tmp, "a.wav")
            make_wav(path, frames=48000, levels=[0x400000, 0x100000])
            levels = waveform.compute_peaks(path, buckets=100)
            self.assertEqual(levels.shape, (2, 2, 100))
            # Constant samples: peak == RMS; half scale = -6 dB.
            np.testing.assert_allclose(levels[0], levels[1], atol=1e-5)
            self.assertAlmostEqual(float(levels[0, 0, 0]), (20 * math.log10(0.5) + 60) / 60, places=3)
            blob = waveform.to_bytes(levels)
            np.testing.assert_allclose(waveform.from_bytes(blob), levels, atol=1 / 255)
            self.assertIsNone(waveform.from_bytes(bytes([2]) + bytes(200)))  # an old peaks-only blob

    def test_rasterize(self):
        levels = np.zeros((2, 2, 50), np.float32)
        levels[:, 0, 10:20] = 1.0
        for mode in ("overlay", "lanes"):
            pixels = waveform.rasterize(levels, 200, 60, colors=["#ff0000", "#00ff00"], mode=mode)
            self.assertEqual(pixels.shape, (60, 200))
            red = (pixels >> 16) & 0xFF
            self.assertGreater(red[:, 50:70].max(), 200)  # the loud stretch of channel 1
            self.assertLess(red[:, 150:].max(), 60)  # silence: background (+ a faint centre line)
        self.assertEqual(waveform.column_levels(levels, 25).shape, (2, 2, 25))  # downsampled by max
        self.assertEqual(waveform.column_levels(levels, 25)[0, 0].max(), 1.0)


class EmptyFoldersTest(unittest.TestCase):
    def test_nested_markers_only(self):
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "Orchard" / "25Y10M31").mkdir(parents=True)
            (r / "Orchard" / "25Y10M31" / ".daily_folder").touch()
            (r / "SD_1" / "Night walk").mkdir(parents=True)
            (r / "Keep" / "day").mkdir(parents=True)
            (r / "Keep" / "day" / "1T01.wav").write_bytes(b"x")
            (r / "Keep" / "empty take").mkdir()
            (r / ".hidden").mkdir()
            found = organize.find_empty_folders(root)
            self.assertEqual(found, [r / "Keep" / "empty take", r / "Orchard", r / "SD_1"])
            dirs, markers = organize.remove_tree_if_empty(r / "Orchard")
            self.assertIn(r / "Orchard", dirs)
            self.assertEqual(markers, [r / "Orchard" / "25Y10M31" / ".daily_folder"])
            self.assertFalse((r / "Orchard").exists())
            self.assertEqual(organize.remove_tree_if_empty(r / "Keep"), ([], []))

    def test_left_empty_climbs_through_empty_subfolders(self):
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "Show" / "day1" / "take").mkdir(parents=True)
            (r / "Show" / "day2").mkdir()
            (r / "Show" / "day2" / ".daily_folder").touch()
            dirs, _ = organize.remove_left_empty({r / "Show" / "day1" / "take"}, root)
            self.assertFalse((r / "Show").exists())  # day2 held only a marker
            self.assertTrue(r.exists())


class PeriodTreeTest(unittest.TestCase):
    def test_year_month_project(self):
        from PySide6.QtGui import QStandardItemModel

        from sound_file_manager.file_model import PERIOD_ROLE, PROJECT_ROLE, build_project_tree, in_period

        from . import qt_app
        qt_app()
        recs = [Recording(f"/l/{n}.wav", 1, 0, project=p, date=d) for n, (p, d) in enumerate(
            [("Alpha", "2026-09-01"), ("Alpha", "2026-08-30"), ("Beta", "2026-09-10"), ("Old", "2024-11-09"),
             ("Nodate", "")])]
        model = QStandardItemModel()
        build_project_tree(model, recs, by_date=True)
        tops = [model.index(r, 0).data() for r in range(model.rowCount())]
        self.assertEqual(tops, ["All recordings  (5)", "2026  (3)", "2024  (1)", "(No date)  (1)"])
        year = model.index(1, 0)
        months = [model.index(r, 0, year).data() for r in range(model.rowCount(year))]
        self.assertEqual(months, ["September  (2)", "August  (1)"])
        september = model.index(0, 0, year)
        projects = [(model.index(r, 0, september).data(PROJECT_ROLE), model.index(r, 0, september).data(PERIOD_ROLE))
                    for r in range(model.rowCount(september))]
        self.assertEqual(projects, [("Alpha", "2026-09"), ("Beta", "2026-09")])
        self.assertTrue(in_period(recs[0], "2026") and in_period(recs[0], "2026-09"))
        self.assertFalse(in_period(recs[1], "2026-09"))
        self.assertTrue(in_period(recs[4], "") and not in_period(recs[0], ""))
        build_project_tree(model, recs, by_date=False)
        self.assertEqual(model.index(1, 0).data(), "Alpha  (2)")


class StereoGuessTest(unittest.TestCase):
    def test_looks_stereo(self):
        from sound_file_manager.player import looks_stereo
        self.assertTrue(looks_stereo(["", ""], "101AT01_LR.wav"))
        self.assertTrue(looks_stereo(["TrL", "TrR"], "a.wav"))
        self.assertTrue(looks_stereo(["L", "R"], "a.wav"))
        self.assertFalse(looks_stereo(["BOOM", "LAV"], "a.wav"))
        self.assertFalse(looks_stereo(["L", "R", "C"], "a_LR.wav"))


class MarkersTest(unittest.TestCase):
    def test_store_and_move(self):
        from sound_file_manager.markers import Marker, MarkerStore, default_name, next_marker
        store = MarkerStore(":memory:")
        store.put("/a.wav", [Marker(100, "Marker 1"), Marker(50, "clap"), Marker(9, "cue", from_file=True)])
        self.assertEqual([(m.frame, m.name) for m in store.get("/a.wav")], [(50, "clap"), (100, "Marker 1")])
        store.move({"/a.wav": "/b.wav"})
        self.assertEqual(store.get("/a.wav"), [])
        self.assertEqual(len(store.get("/b.wav")), 2)
        markers = store.get("/b.wav")
        self.assertEqual(default_name(markers), "Marker 2")
        self.assertEqual(next_marker(markers, 50, 1).name, "Marker 1")
        self.assertEqual(next_marker(markers, 60, -1).name, "clap")
        self.assertIsNone(next_marker(markers, 100, 1))

    def test_read_cues(self):
        import struct

        from sound_file_manager import bwf
        with tempfile.TemporaryDirectory() as tmp:
            path = compat.join(tmp, "c.wav")
            make_wav(path, frames=48000)
            cue = struct.pack("<I", 2) + struct.pack("<II4sIII", 1, 0, b"data", 0, 0, 24000) + \
                struct.pack("<II4sIII", 2, 0, b"data", 0, 0, 1200)
            label = struct.pack("<I", 1) + b"plane\0"
            adtl = b"adtl" + b"labl" + struct.pack("<I", len(label)) + label
            extra = b"cue " + struct.pack("<I", len(cue)) + cue + b"LIST" + struct.pack("<I", len(adtl)) + adtl
            with open(path, "r+b") as f:
                f.seek(0, 2)
                f.write(extra)
                size = f.tell()
                f.seek(4)
                f.write(struct.pack("<I", size - 8))
            self.assertEqual(bwf.read_cues(path), [(1200, "Cue 1"), (24000, "plane")])
            self.assertEqual(bwf.read_cues(compat.join(tmp, "c.wav"))[1][1], "plane")

    def test_range_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = compat.join(tmp, "r.wav")
            make_wav(path, frames=4800)
            part = waveform.compute_peaks(path, 50, first_frame=1000, end_frame=2000)
            self.assertEqual(part.shape, (2, 2, 50))


class ProjectCountTest(unittest.TestCase):
    def test_counts(self):
        from PySide6.QtGui import QStandardItemModel

        from sound_file_manager.file_model import build_project_tree

        from . import qt_app
        qt_app()
        recs = [Recording(f"/l/{n}.wav", 1, 0, project=p, date="2026-09-0" + str(n % 9 + 1))
                for n, p in enumerate(["A", "A", "A", "B"])]
        model = QStandardItemModel()
        build_project_tree(model, recs, by_date=True, counts="projects")
        year = model.index(1, 0)
        self.assertEqual(model.index(0, 0).data(), "All recordings  (2)")
        self.assertEqual(year.data(), "2026  (2)")
        month = model.index(0, 0, year)
        self.assertEqual(model.index(0, 0, month).data(), "A")
        build_project_tree(model, recs, by_date=True, counts="both")
        self.assertEqual(model.index(1, 0).data(), "2026  (2 / 4)")


class RecorderTreeTest(unittest.TestCase):
    def test_by_recorder(self):
        from PySide6.QtGui import QStandardItemModel

        from sound_file_manager.file_model import (
            NO_RECORDER, PROJECT_ROLE, RECORDER_ROLE, build_project_tree, recorder_label,
        )

        from . import qt_app
        qt_app()
        recs = [Recording(f"/l/{n}.wav", 1, 0, project=p, recorder=r, date="2026-09-01") for n, (p, r) in enumerate(
            [("A", "SoundDev: 833 XX0000000000"), ("B", "SoundDev: 833 XX0000000000"), ("A", "ZOOM F8"), ("C", "")])]
        self.assertEqual(recorder_label(recs[0]), "Sound Devices 833")  # no serial number
        self.assertEqual(recorder_label(recs[3]), NO_RECORDER)
        model = QStandardItemModel()
        build_project_tree(model, recs, counts="both", by_recorder=True)
        tops = [model.index(r, 0).data() for r in range(model.rowCount())]
        self.assertEqual(tops, ["All recordings  (3 / 4)", "Sound Devices 833  (2 / 2)", "ZOOM F8  (1 / 1)",
                                f"{NO_RECORDER}  (1 / 1)"])
        sd = model.index(1, 0)
        children = [(model.index(r, 0, sd).data(PROJECT_ROLE), model.index(r, 0, sd).data(RECORDER_ROLE))
                    for r in range(model.rowCount(sd))]
        self.assertEqual(children, [("A", "Sound Devices 833"), ("B", "Sound Devices 833")])
