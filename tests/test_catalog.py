import os
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from sound_file_manager import catalog, compat, organize
from sound_file_manager import timecode as tc
from sound_file_manager.catalog import Recording

from .wavmaker import make_wav


class TimecodeTest(unittest.TestCase):
    def test_parse_rate(self):
        self.assertEqual(tc.parse_rate("24000/1001"), Fraction(24000, 1001))
        self.assertEqual(tc.parse_rate("023.976-ND"), Fraction(24000, 1001))
        self.assertEqual(tc.parse_rate("29.97DF"), Fraction(30000, 1001))
        self.assertEqual(tc.parse_rate("25"), 25)
        self.assertIsNone(tc.parse_rate(""))
        self.assertIsNone(tc.parse_rate("abc"))

    def test_drop_frame_flag(self):
        self.assertTrue(tc.parse_drop_frame("29.97DF"))
        self.assertTrue(tc.parse_drop_frame("DF"))
        self.assertFalse(tc.parse_drop_frame("023.976-ND"))
        self.assertFalse(tc.parse_drop_frame("NDF"))

    def test_ndf(self):
        self.assertEqual(tc.frames_to_tc(0, 24), "00:00:00:00")
        self.assertEqual(tc.frames_to_tc(24 * 3600 + 23, 24), "01:00:00:23")
        # A real Sound Devices file: 3090111025 samples at 48 kHz, 23.976 NDF.
        self.assertEqual(tc.samples_to_tc(3090111025, 48000, Fraction(24000, 1001)), "17:51:53:00")

    def test_drop_frame(self):
        self.assertEqual(tc.frames_to_tc(1799, 30, drop=True), "00:00:59;29")
        self.assertEqual(tc.frames_to_tc(1800, 30, drop=True), "00:01:00;02")
        self.assertEqual(tc.frames_to_tc(17982, 30, drop=True), "00:10:00;00")
        # One real hour of 29.97 DF is exactly 01:00:00;00.
        self.assertEqual(tc.samples_to_tc(48000 * 3600, 48000, Fraction(30000, 1001), True), "01:00:00;00")

    def test_labels(self):
        self.assertEqual(tc.rate_label(Fraction(24000, 1001)), "23.976")
        self.assertEqual(tc.rate_label(Fraction(30000, 1001), True), "29.97 DF")
        self.assertEqual(tc.format_duration(448.4), "7:28")
        self.assertEqual(tc.format_duration(3723), "1:02:03")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, relative, **kwargs):
        path = compat.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        make_wav(path, **kwargs)
        return path

    def test_recording_fields(self):
        path = self.make("Harbor/25Y10M27/Test 1T02/Test 1T02_LR.wav", project="Harbor", scene="Test 1", take="02")
        rec = catalog.read_recording(path)
        self.assertEqual(rec.meta_project, "Harbor")
        self.assertEqual(rec.start_tc, "00:59:56:09")  # 1 h of samples = 86313.7 frames at 23.976
        self.assertEqual(rec.rate_label, "23.976")
        self.assertEqual(rec.format_label, "48 kHz · 24-bit · 2 ch")
        self.assertEqual(rec.tracks, ["BOOM", "LAV"])
        self.assertEqual(rec.family, "FAM1")

    def test_filename_fallback_for_scene_take(self):
        path = compat.join(self.root, "56A-T005.WAV")
        make_wav(path, with_ixml=False, with_bext=False)
        rec = catalog.read_recording(path)
        self.assertEqual((rec.scene, rec.take), ("56A", "005"))
        path = compat.join(self.root, "Test 1T02_ISO.wav")
        make_wav(path, with_ixml=False, with_bext=False)
        rec = catalog.read_recording(path)
        self.assertEqual((rec.scene, rec.take), ("Test 1", "02"))

    def test_broken_file_is_recorded_with_error(self):
        path = compat.join(self.root, "bad.wav")
        Path(path).write_bytes(b"nope")
        rec = catalog.read_recording(path)
        self.assertTrue(rec.error)

    def test_folder_project(self):
        containers = ["SD_1", "833 BACK UPS"]
        root = self.root
        self.assertEqual(catalog.folder_project(f"{root}/SD_1/Birch 6-6-23/a.wav", root, containers), "Birch 6-6-23")
        self.assertEqual(catalog.folder_project(f"{root}/Lantern/25Y11M06/6T02/a.wav", root, containers),
                         "Lantern")
        self.assertEqual(catalog.folder_project(f"{root}/25Y11M06/a.wav", root, containers), "")
        self.assertEqual(catalog.folder_project(f"{root}/a.wav", root, containers), "")

    def test_assign_projects_prefers_metadata(self):
        recs = [Recording(path=f"{self.root}/Folder/a.wav", size=1, mtime=0, meta_project="Meta"),
                Recording(path=f"{self.root}/Folder/b.wav", size=1, mtime=0)]
        catalog.assign_projects(recs, self.root, [])
        self.assertEqual([(r.project, r.project_from) for r in recs],
                         [("Meta", "metadata"), ("Folder", "folder")])

    def test_scan_uses_cache_and_prunes(self):
        a = self.make("P/a.wav")
        self.make("P/b.WAV")
        Path(self.root, "P", "._a.wav").write_bytes(b"x")  # AppleDouble, skipped
        Path(self.root, ".hidden").mkdir()
        make_wav(compat.join(self.root, ".hidden", "c.wav"))
        cache = catalog.Cache(compat.join(self.root, "cache", "c.sqlite"))
        got = []
        stats = catalog.scan(self.root, cache, on_batch=got.extend)
        self.assertEqual((stats.found, stats.parsed), (2, 2))
        self.assertEqual(sorted(os.path.basename(r.path) for r in got), ["a.wav", "b.WAV"])
        stats = catalog.scan(self.root, cache, on_batch=lambda b: None)
        self.assertEqual((stats.parsed, stats.cached), (0, 2))
        os.remove(a)
        stats = catalog.scan(self.root, cache, on_batch=lambda b: None)
        self.assertEqual(stats.removed, 1)
        cache.close()


class OrganizeTest(unittest.TestCase):
    def rec(self, path, **kw):
        values = dict(path=path, size=1, mtime=0, project="Harbor", date="2025-10-27", scene="10", take="03")
        values.update(kw)
        return Recording(**values)

    def test_expand(self):
        rec = self.rec("/r/x/10T03_ISO.wav", recorder="SoundDev: 833 WS1")
        self.assertEqual(organize.expand("{project}/{date}", rec), "Harbor/2025-10-27")
        self.assertEqual(organize.expand("{recorder}|{name}|{n:3}", rec, 7, for_path=False), "833 WS1|10T03_ISO|007")
        self.assertEqual(organize.expand("{tape}", rec), "No Tape")
        self.assertEqual(organize.expand("{unknown}", rec), "{unknown}")
        self.assertEqual(organize.expand("{project}", self.rec("/a.wav", project="A/B: C")), "A-B- C")

    def test_plan_moves(self):
        with tempfile.TemporaryDirectory() as root:
            a = compat.join(root, "a.wav")
            b = compat.join(root, "sub", "a.wav")
            os.makedirs(os.path.dirname(b))
            Path(a).write_text("a")
            Path(b).write_text("b")
            moves = organize.plan_moves([self.rec(a), self.rec(b)], root, "{project}/{date}")
            self.assertEqual(moves[0].dst, Path(root, "Harbor", "2025-10-27", "a.wav"))
            self.assertIn("more than one", moves[0].error)  # both files would be Harbor/2025-10-27/a.wav
            moves = organize.plan_moves([self.rec(a)], root, "{project}/{scene}T{take}_{name}")
            self.assertEqual(moves[0].dst, Path(root, "Harbor", "10T03_a.wav"))
            moves = organize.plan_moves([self.rec(a)], root, "")
            self.assertTrue(moves[0].error)

    def test_renamed(self):
        rec = self.rec("/r/10T03_ISO.wav")
        self.assertEqual(organize.renamed("10T03_ISO.wav", find="T03", replace="T04"), "10T04_ISO.wav")
        self.assertEqual(organize.renamed("10T03_ISO.wav", pattern="{project}_{name}", rec=rec),
                         "Harbor_10T03_ISO.wav")
        self.assertEqual(organize.renamed("a.WAV"), "a.WAV")


class RemoveLeftEmptyTest(unittest.TestCase):
    def test_removes_marker_only_folders_up_to_root(self):
        with tempfile.TemporaryDirectory() as root:
            take = Path(root, "Proj", "25Y04M16", "P001T01")
            take.mkdir(parents=True)
            (take / ".take_folder").touch()
            (take.parent / ".daily_folder").touch()
            keep = Path(root, "Other", "day")
            keep.mkdir(parents=True)
            (keep / "report.csv").touch()
            dirs, markers = organize.remove_left_empty({take, keep}, root)
            self.assertEqual(dirs, [take, take.parent, take.parent.parent])
            self.assertEqual(len(markers), 2)
            self.assertTrue(keep.is_dir())
            self.assertTrue(Path(root).is_dir())

    def test_never_leaves_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent, "lib")
            root.mkdir()
            dirs, _ = organize.remove_left_empty({root}, str(root))
            self.assertEqual(dirs, [])
            self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()
