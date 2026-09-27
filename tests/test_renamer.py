import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from sound_file_manager import renamer
from sound_file_manager.renamer import RenameError, RenameOp


class RenamerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, name, content=None):
        path = self.dir / name
        path.write_text(name if content is None else content)
        return path

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())


class ValidateNameTest(unittest.TestCase):
    def test_rejects_bad_names(self):
        for name in ["", "   ", ".", "..", "a/b", "a\0b", "x" * 256]:
            self.assertIsNotNone(renamer.validate_name(name), repr(name))

    def test_accepts_normal_names(self):
        for name in ["clip.mp4", "My Show S01E02.mkv", "ünïcødé.webm", "x" * 255]:
            self.assertIsNone(renamer.validate_name(name), name)


class PlanTest(RenamerTestCase):
    def test_clash_with_existing_file(self):
        a = self.make("a.mp4")
        self.make("b.mp4")
        errors = renamer.plan_renames([RenameOp(a, self.dir / "b.mp4")])
        self.assertIn("already exists", errors[a])

    def test_duplicate_targets_in_batch(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        errors = renamer.plan_renames([RenameOp(a, self.dir / "c.mp4"), RenameOp(b, self.dir / "c.mp4")])
        self.assertEqual(set(errors), {a, b})

    def test_target_freed_by_batch_is_ok(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        self.assertEqual(renamer.plan_renames([RenameOp(a, self.dir / "b.mp4"), RenameOp(b, self.dir / "c.mp4")]), {})

    def test_missing_source(self):
        errors = renamer.plan_renames([RenameOp(self.dir / "gone.mp4", self.dir / "x.mp4")])
        self.assertIn("no longer exists", errors[self.dir / "gone.mp4"])

    def test_file_in_the_way_of_folder(self):
        a = self.make("a.mp4")
        self.make("Show")
        errors = renamer.plan_renames([RenameOp(a, self.dir / "Show" / "Season 1" / "a.mp4")])
        self.assertIn("'Show' is a file", errors[a])

    def test_move_into_new_folder_is_ok(self):
        a = self.make("a.mp4")
        self.assertEqual(renamer.plan_renames([RenameOp(a, self.dir / "New" / "a.mp4")]), {})

    def test_noop_is_not_an_error(self):
        a = self.make("a.mp4")
        self.assertEqual(renamer.plan_renames([RenameOp(a, a)]), {})


class ApplyTest(RenamerTestCase):
    def test_simple_rename(self):
        a = self.make("a.mp4")
        applied = renamer.apply_renames([RenameOp(a, self.dir / "b.mp4")])
        self.assertEqual(self.names(), ["b.mp4"])
        self.assertEqual(applied, [RenameOp(a, self.dir / "b.mp4")])

    def test_swap(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        renamer.apply_renames([RenameOp(a, b), RenameOp(b, a)])
        self.assertEqual(a.read_text(), "b.mp4")
        self.assertEqual(b.read_text(), "a.mp4")
        self.assertEqual(self.names(), ["a.mp4", "b.mp4"])

    def test_chain(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        c = self.dir / "c.mp4"
        renamer.apply_renames([RenameOp(a, b), RenameOp(b, c)])
        self.assertEqual(b.read_text(), "a.mp4")
        self.assertEqual(c.read_text(), "b.mp4")

    def test_refuses_invalid_batch_without_touching_disk(self):
        a = self.make("a.mp4")
        self.make("b.mp4")
        with self.assertRaises(RenameError):
            renamer.apply_renames([RenameOp(a, self.dir / "b.mp4")])
        self.assertEqual(self.names(), ["a.mp4", "b.mp4"])

    def test_rolls_back_on_failure(self):
        a, b, c = self.make("a.mp4"), self.make("b.mp4"), self.make("c.mp4")
        ops = [RenameOp(a, self.dir / "x.mp4"), RenameOp(b, self.dir / "y.mp4"), RenameOp(c, self.dir / "z.mp4")]
        real_rename = os.rename

        def failing_rename(src, dst):
            if Path(dst).name == "z.mp4":
                raise OSError("disk on fire")
            real_rename(src, dst)

        with mock.patch("sound_file_manager.renamer.os.rename", side_effect=failing_rename):
            with self.assertRaises(RenameError):
                renamer.apply_renames(ops)
        self.assertEqual(self.names(), ["a.mp4", "b.mp4", "c.mp4"])
        self.assertEqual(a.read_text(), "a.mp4")

    def test_undo(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        applied = renamer.apply_renames([RenameOp(a, self.dir / "c.mp4"), RenameOp(b, a)])
        self.assertEqual(self.names(), ["a.mp4", "c.mp4"])
        renamer.apply_renames(renamer.undo_ops(applied))
        self.assertEqual(self.names(), ["a.mp4", "b.mp4"])
        self.assertEqual(a.read_text(), "a.mp4")


class SubfolderTest(RenamerTestCase):
    def test_target_for(self):
        a = self.dir / "a.mp4"
        self.assertEqual(renamer.target_for(a, "b", ".mp4"), self.dir / "b.mp4")
        self.assertEqual(renamer.target_for(a, "Show / Season 1/ Ep 1 ", ".mp4"), self.dir / "Show" / "Season 1" / "Ep 1.mp4")
        for bad in ["", "Show/", "/abs", "a//b", "../up", "Show/../x", "./x"]:
            with self.assertRaises(RenameError, msg=bad):
                renamer.target_for(a, bad, ".mp4")

    def test_folder_for(self):
        self.assertEqual(renamer.folder_for(self.dir, " Show / S1 "), self.dir / "Show" / "S1")
        for bad in ["", "a/", "..", "a/../b", "/abs"]:
            with self.assertRaises(RenameError, msg=bad):
                renamer.folder_for(self.dir, bad)

    def test_creates_nested_folders_and_undo_removes_them(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        created = []
        applied = renamer.apply_renames([
            RenameOp(a, self.dir / "Show" / "Season 1" / "E01.mp4"),
            RenameOp(b, self.dir / "Show" / "Season 1" / "E02.mp4"),
        ], created)
        self.assertEqual((self.dir / "Show" / "Season 1" / "E01.mp4").read_text(), "a.mp4")
        self.assertEqual(created, [self.dir / "Show", self.dir / "Show" / "Season 1"])
        renamer.apply_renames(renamer.undo_ops(applied))
        renamer.remove_empty_dirs(created)
        self.assertEqual(self.names(), ["a.mp4", "b.mp4"])

    def test_existing_folder_is_kept(self):
        a = self.make("a.mp4")
        (self.dir / "Movies").mkdir()
        self.make("Movies/other.mkv")
        created = []
        applied = renamer.apply_renames([RenameOp(a, self.dir / "Movies" / "a.mp4")], created)
        self.assertEqual(created, [])
        renamer.apply_renames(renamer.undo_ops(applied))
        renamer.remove_empty_dirs(created)
        self.assertTrue((self.dir / "Movies" / "other.mkv").exists())

    def test_non_empty_created_folder_survives_cleanup(self):
        folder = self.dir / "New"
        folder.mkdir()
        (folder / "keep.txt").write_text("x")
        renamer.remove_empty_dirs([folder])
        self.assertTrue(folder.exists())

    def test_failed_move_removes_created_folders(self):
        a, b = self.make("a.mp4"), self.make("b.mp4")
        real_rename = os.rename

        def failing_rename(src, dst):
            if Path(dst).name == "E02.mp4":
                raise OSError("nope")
            real_rename(src, dst)

        with mock.patch("sound_file_manager.renamer.os.rename", side_effect=failing_rename):
            with self.assertRaises(RenameError):
                renamer.apply_renames([
                    RenameOp(a, self.dir / "Show" / "E01.mp4"),
                    RenameOp(b, self.dir / "Show" / "E02.mp4"),
                ])
        self.assertEqual(self.names(), ["a.mp4", "b.mp4"])


class FolderRenameTest(RenamerTestCase):
    def test_rename_folder_and_undo(self):
        (self.dir / "Show").mkdir()
        self.make("Show/ep.mkv")
        applied = renamer.apply_renames([RenameOp(self.dir / "Show", self.dir / "Show (2026)")])
        self.assertTrue((self.dir / "Show (2026)" / "ep.mkv").exists())
        renamer.apply_renames(renamer.undo_ops(applied))
        self.assertTrue((self.dir / "Show" / "ep.mkv").exists())

    def test_folder_into_new_parent(self):
        (self.dir / "S1").mkdir()
        created = []
        renamer.apply_renames([RenameOp(self.dir / "S1", self.dir / "Show" / "Season 1")], created)
        self.assertTrue((self.dir / "Show" / "Season 1").is_dir())
        self.assertEqual(created, [self.dir / "Show"])

    def test_folder_into_itself_refused(self):
        (self.dir / "A").mkdir()
        errors = renamer.plan_renames([RenameOp(self.dir / "A", self.dir / "A" / "B")])
        self.assertIn("into itself", errors[self.dir / "A"])

    def test_folder_clash(self):
        (self.dir / "A").mkdir()
        (self.dir / "B").mkdir()
        self.assertIn("already exists", renamer.plan_renames([RenameOp(self.dir / "A", self.dir / "B")])[self.dir / "A"])

    def test_translate(self):
        mapping = {Path("/v/Show"): Path("/v/Show (2026)"), Path("/v/a.mkv"): Path("/v/b.mkv")}
        self.assertEqual(renamer.translate(Path("/v/Show/S1/ep.mkv"), mapping), Path("/v/Show (2026)/S1/ep.mkv"))
        self.assertEqual(renamer.translate(Path("/v/a.mkv"), mapping), Path("/v/b.mkv"))
        self.assertEqual(renamer.translate(Path("/v/Showtime/x.mkv"), mapping), Path("/v/Showtime/x.mkv"))


class FindReplaceTest(unittest.TestCase):
    def test_find_replace(self):
        self.assertEqual(renamer.find_replace("a.b.c", ".", " "), "a b c")
        self.assertEqual(renamer.find_replace("Hello hello", "HELLO", "x", case_sensitive=False), "x x")
        self.assertEqual(renamer.find_replace("ep12", r"ep(\d+)", r"E\1", regex=True), "E12")
        self.assertEqual(renamer.find_replace("a", "a", r"\1", case_sensitive=False), r"\1")
        with self.assertRaises(ValueError):
            renamer.find_replace("x", "(", "", regex=True)


if __name__ == "__main__":
    unittest.main()


class SceneTakeNameTest(unittest.TestCase):
    def test_sound_devices_names(self):
        from sound_file_manager.renamer import scene_take_name, split_take_name
        self.assertEqual(split_take_name("101AT01_ISO.wav"), ("101AT01", "_ISO", ".wav"))
        self.assertEqual(split_take_name("8MT01.WAV"), ("8MT01", "", ".WAV"))
        self.assertEqual(scene_take_name("101AT01_ISO.wav", "101A", "01", "101A", "02"), "101AT02_ISO.wav")
        self.assertEqual(scene_take_name("101AT01_LR.wav", "101A", "01", "102", "01"), "102T01_LR.wav")
        self.assertEqual(scene_take_name("8MT01_1.WAV", "8M", "01", "8M", "03"), "8MT03_1.WAV")  # mono split files

    def test_style_kept_or_default(self):
        from sound_file_manager.renamer import scene_take_name
        self.assertEqual(scene_take_name("8M-T01.wav", "8M", "01", "8M", "02"), "8M-T02.wav")
        self.assertEqual(scene_take_name("12_03.wav", "12", "03", "12", "04"), "12_04.wav")
        # A name that isn't built from scene and take gets the Sound Devices style.
        self.assertEqual(scene_take_name("POD00001.WAV", "", "", "4", "02"), "4T02.WAV")
        self.assertEqual(scene_take_name("Anchor Audio.WAV", "1", "1", "2", "1"), "2T1.WAV")

    def test_other_parts_of_the_name_are_kept(self):
        from sound_file_manager.renamer import scene_take_name
        self.assertEqual(scene_take_name("2BT001_BOOM.WAV", "2B", "001", "2B", "002"), "2BT002_BOOM.WAV")
        self.assertEqual(scene_take_name("2B-T001_BOOM+BOOMSAFE.WAV", "2B", "001", "3", "001"),
                         "3-T001_BOOM+BOOMSAFE.WAV")
        self.assertEqual(scene_take_name("Day2_2BT001 alt.wav", "2B", "001", "2B", "004"), "Day2_2BT004 alt.wav")
        self.assertEqual(scene_take_name("2BT001_LAV_ISO.wav", "2B", "001", "2B", "002"), "2BT002_LAV_ISO.wav")
        # Not a match inside a longer number or word: the default style instead.
        self.assertEqual(scene_take_name("2BT0012.wav", "2B", "001", "2B", "002"), "2BT002.wav")

    def test_no_rename(self):
        from sound_file_manager.renamer import scene_take_name
        self.assertIsNone(scene_take_name("POD00001.WAV", "", "", "4", ""))  # take still empty
        self.assertIsNone(scene_take_name("8MT01.wav", "8M", "01", "8M", "01"))
        self.assertIsNone(scene_take_name("8MT01.wav", "8m", "01", "8M", "01"))  # same, only case: no rename

    def test_partner_name(self):
        from sound_file_manager.renamer import partner_name
        self.assertEqual(partner_name("101AT01_LR.wav", "101AT01_ISO.wav", "101AT02_ISO.wav"), "101AT02_LR.wav")
        self.assertIsNone(partner_name("101AT01_LR.wav", "101AT01_ISO.wav", "101AT02.wav"))  # ending dropped
        self.assertIsNone(partner_name("99T01_LR.wav", "101AT01_ISO.wav", "101AT02_ISO.wav"))  # another take
        self.assertIsNone(partner_name("101AT01_ISO.wav", "101AT01_ISO.wav", "101AT02_ISO.wav"))  # the file itself
