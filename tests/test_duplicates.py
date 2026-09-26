import os
import tempfile
import unittest
from pathlib import Path

from sound_file_manager import bwf, catalog, duplicates as d

from .wavmaker import make_wav


class DuplicatesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.containers = ["SD_1", "SD_2"]

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, relative, **kwargs):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        kwargs.setdefault("filename", os.path.basename(relative))
        make_wav(path, **kwargs)
        return path

    def recs(self):
        recs = [catalog.read_recording(p) for p, _ in catalog.walk_audio(self.root)]
        catalog.assign_projects(recs, self.root, self.containers)
        return recs

    def test_finds_identical_and_metadata_differences(self):
        a = self.make("Harbor/day1/1T01_ISO.wav", project="Harbor")
        b = self.make("SD_1/Harbor/day1/1T01_ISO.wav", project="Harbor")  # a card dump copy
        self.make("SD_2/Harbor/day1/1T01_ISO.wav", project="Harbor")
        bwf.update_metadata(os.path.join(self.root, "SD_2/Harbor/day1/1T01_ISO.wav"), {"note": "boom bumped"})
        # Same length and timecode, different audio (an _LR mix of the same take): not a duplicate.
        self.make("Harbor/day1/1T01_LR.wav", project="Harbor", level=0x200000)
        self.make("Harbor/day1/2T01_ISO.wav", project="Harbor", time_reference=48000 * 7200)
        groups = d.find_duplicate_files(self.recs(), d.Fingerprints(), self.root, self.containers)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.keeper.path, a)  # the project folder beats the card dumps
        self.assertEqual(len(group.extras), 2)
        self.assertEqual(group.differences, {"note": ["", "boom bumped"]})
        self.assertFalse(group.identical)
        self.assertTrue(group.copy_identical(next(r for r in group.recs if r.path == b)))  # judged per copy
        # An _ISO/_LR with the same name is the same file whatever its note says.
        self.assertTrue(group.copy_identical(
            next(r for r in group.recs if r.path.endswith("SD_2/Harbor/day1/1T01_ISO.wav"))))
        # It has a note the kept copy lacks: it is the version kept (in the kept copy's place).
        self.assertEqual(group.replacement.path, os.path.join(self.root, "SD_2/Harbor/day1/1T01_ISO.wav"))
        self.assertEqual(group.check_level(next(r for r in group.recs if r.path == b)), "audio")
        self.assertTrue(d.files_identical(a, b))
        self.assertFalse(d.files_identical(a, os.path.join(self.root, "SD_2/Harbor/day1/1T01_ISO.wav")))
        self.assertTrue(d.audio_identical(a, os.path.join(self.root, "SD_2/Harbor/day1/1T01_ISO.wav")))
        self.assertFalse(d.audio_identical(a, os.path.join(self.root, "Harbor/day1/1T01_LR.wav")))

    def test_iso_and_lr_are_kept_together(self):
        # The take folder has both files; the flat copy in the day folder has only
        # the _ISO (and would win on its own as it is less deep).
        self.make("Harbor/day1/1T01/1T01_ISO.wav", project="Harbor")
        self.make("Harbor/day1/1T01/1T01_LR.wav", project="Harbor", level=0x200000)
        self.make("Harbor/day1/1T01_ISO.wav", project="Harbor")
        self.make("SD_1/Harbor/day1/1T01/1T01_LR.wav", project="Harbor", level=0x200000)
        groups = d.find_duplicate_files(self.recs(), d.Fingerprints(), self.root, self.containers)
        self.assertEqual(len(groups), 2)  # the _ISO and the _LR each have a duplicate; never each other
        keepers = {os.path.relpath(g.keeper.path, self.root) for g in groups}
        self.assertEqual(keepers, {"Harbor/day1/1T01/1T01_ISO.wav", "Harbor/day1/1T01/1T01_LR.wav"})

    def test_plain_file_with_other_notes_differs(self):
        self.make("Harbor/day1/5T01.wav", project="Harbor", time_reference=5)
        other = self.make("SD_1/Harbor/day1/5T01.wav", project="Harbor", time_reference=5)
        bwf.update_metadata(other, {"note": "different"})
        group = d.find_duplicate_files(self.recs(), d.Fingerprints(), self.root, self.containers)[0]
        self.assertFalse(group.copy_identical(next(r for r in group.recs if r.path == other)))

    def test_merge_removes_same_named_take_file_despite_notes(self):
        self.make("Harbor/d/101AT01_ISO.wav", project="Harbor", time_reference=1)
        self.make("Harbor/d/101AT01_LR.wav", project="Harbor", time_reference=1, level=0x200000)
        noted = self.make("SD_1/Harbor/d/101AT01_ISO.wav", project="Harbor", time_reference=1)
        bwf.update_metadata(noted, {"note": "a note"})
        self.make("SD_1/Harbor/d/101AT01_LR.wav", project="Harbor", time_reference=1, level=0x200000)
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints())
        # The _ISO with the note replaces the kept one (preferred); the _LR repeat is removed.
        self.assertEqual(sorted((a.kind, a.rec.name, a.level) for a in actions),
                         [("remove", "101AT01_LR.wav", "identical"), ("replace", "101AT01_ISO.wav", "audio")])

    def test_copy_with_a_note_is_preferred(self):
        self.make("SD_1/Harbor/d/102AT01_ISO.wav", project="Harbor", time_reference=1)
        noted = self.make("SD_2/Harbor/d/102AT01_ISO.wav", project="Harbor", time_reference=1)
        bwf.update_metadata(noted, {"note": "plane at the end"})
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints(),
                                       root=self.root, containers=self.containers)
        moved = next(a for a in actions if a.kind == "move")
        self.assertEqual(moved.rec.path, noted)  # the copy with the note is the one kept

    def test_version_with_notes_replaces_the_kept_copy(self):
        self.make("Harbor/d/102AT01_ISO.wav", project="Harbor", time_reference=1)
        self.make("Harbor/d/wild.wav", project="Harbor", time_reference=9)
        noted = self.make("SD_1/Harbor/d/102AT01_ISO.wav", project="Harbor", time_reference=1)
        bwf.update_metadata(noted, {"note": "plane at the end"})
        noted_wild = self.make("SD_1/Harbor/d/wild.wav", project="Harbor", time_reference=9)
        bwf.update_metadata(noted_wild, {"circled": True})
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints())
        by = {os.path.relpath(a.rec.path, self.root): a for a in actions}
        replace = by["SD_1/Harbor/d/102AT01_ISO.wav"]
        self.assertEqual((replace.kind, replace.level), ("replace", "audio"))
        self.assertEqual(replace.dst, os.path.join(self.root, "Harbor/d/102AT01_ISO.wav"))
        self.assertEqual(replace.other.path, replace.dst)
        self.assertEqual(by["SD_1/Harbor/d/wild.wav"].kind, "replace")  # a circle counts as a note too
        # Duplicate Files: the copy with the note is the replacement for the kept one.
        groups = d.find_duplicate_files(recs, d.Fingerprints(), self.root, self.containers)
        iso = next(g for g in groups if g.keeper.name == "102AT01_ISO.wav")
        self.assertEqual(iso.replacement.path, noted)

    def test_two_different_notes_are_not_replaced(self):
        kept = self.make("Harbor/d/wild.wav", project="Harbor", time_reference=9)
        bwf.update_metadata(kept, {"note": "one"})
        other = self.make("SD_1/Harbor/d/wild.wav", project="Harbor", time_reference=9)
        bwf.update_metadata(other, {"note": "two"})
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints())
        self.assertEqual([a.kind for a in actions], ["differs"])

    def test_version_with_more_tracks_wins(self):
        mix, boom, safe = 0x010000, 0x020000, 0x030000
        small = self.make("Harbor/B-ROLL-T008.WAV", project="Harbor", time_reference=5, levels=[boom, safe])
        big = self.make("SD_2/Harbor/B-ROLL-T008.WAV", project="Harbor", time_reference=5,
                        levels=[mix, mix, boom, safe])
        other = self.make("SD_2/Harbor/B-ROLL-T009.WAV", project="Harbor", time_reference=6, levels=[boom, 0x777])
        self.make("Harbor/B-ROLL-T009.WAV", project="Harbor", time_reference=6, levels=[mix, mix, boom, safe])
        self.assertTrue(d.channels_contained(small, big))
        self.assertFalse(d.channels_contained(other, big))  # 0x777 is in no channel of the big file
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = {os.path.relpath(a.rec.path, self.root): a
                   for a in d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints())}
        replace = actions["SD_2/Harbor/B-ROLL-T008.WAV"]
        self.assertEqual((replace.kind, replace.level, replace.other.path), ("replace", "subset", small))
        smaller = actions["SD_2/Harbor/B-ROLL-T009.WAV"]
        self.assertEqual((smaller.kind, smaller.level), ("remove", "subset"))  # checked (and here refused) later

    def test_take_is_never_split(self):
        self.make("Harbor/d/101AT01_ISO.wav", project="Harbor", time_reference=1)  # a different file, same name
        self.make("SD_1/Harbor/d/101AT01_ISO.wav", project="Harbor", time_reference=2)
        self.make("SD_1/Harbor/d/101AT01_LR.wav", project="Harbor", time_reference=2, level=0x200000)
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "Harbor"), d.Fingerprints())
        kinds = {a.rec.name: a.kind for a in actions}
        self.assertEqual(kinds, {"101AT01_ISO.wav": "skip", "101AT01_LR.wav": "skip"})  # the _LR stays with it
        self.assertIn("partner", next(a for a in actions if a.rec.name == "101AT01_LR.wav").reason)

    def test_pair_review_copies(self):
        self.make("Harbor/d/5T01.wav", project="Harbor", time_reference=5)
        self.make("Harbor/d/5T01_ReviewForDeletion.wav", project="Harbor", time_reference=5)
        self.make("Other/9T01_ReviewForDeletion (2).wav", project="Harbor", time_reference=5)
        self.make("Stray/7T01_ReviewForDeletion.wav", project="Harbor", time_reference=77)
        pairs = {os.path.relpath(r.path, self.root): (m and os.path.relpath(m.path, self.root), how)
                 for r, m, how in d.pair_review_copies(self.recs())}
        self.assertEqual(pairs["Harbor/d/5T01_ReviewForDeletion.wav"], ("Harbor/d/5T01.wav", "same name"))
        self.assertEqual(pairs["Other/9T01_ReviewForDeletion (2).wav"], ("Harbor/d/5T01.wav", "same recording"))
        self.assertEqual(pairs["Stray/7T01_ReviewForDeletion.wav"], (None, ""))
        rows = d.compare_rows(self.recs()[0], None)
        self.assertEqual(rows[0][0], "File name")

    def test_fingerprint_cache(self):
        path = self.make("P/a.wav")
        rec = catalog.read_recording(path)
        cache = catalog.Cache(os.path.join(self.root, "c.sqlite"))
        first = d.Fingerprints(cache)(rec)
        self.assertEqual(cache.get_hash(rec.path, rec.size, rec.mtime), first)
        self.assertEqual(d.Fingerprints(cache)(rec), first)
        cache.close()

    def test_removed_folder_is_not_scanned(self):
        self.make(f"{catalog.REMOVED_FOLDER}/Harbor/a.wav")
        self.assertEqual(self.recs(), [])
        self.assertEqual(d.removal_path(os.path.join(self.root, "SD_1/x/a.wav"), self.root, "Harbor"),
                         os.path.join(self.root, catalog.REMOVED_FOLDER, "Harbor", "SD_1/x/a.wav"))
        self.assertEqual(d.removal_path(os.path.join(self.root, "a.wav"), self.root, "A/B: C"),
                         os.path.join(self.root, catalog.REMOVED_FOLDER, "A-B- C", "a.wav"))
        self.assertEqual(d.removal_path(os.path.join(self.root, "a.wav"), self.root),
                         os.path.join(self.root, catalog.REMOVED_FOLDER, "No Project", "a.wav"))

    def test_review_names_and_removed_items(self):
        folder = os.path.join(self.root, "Harbor")
        os.makedirs(folder)
        taken = set()
        first = d.review_path("/x/1T01_ISO.wav", folder, taken)
        self.assertEqual(os.path.basename(first), "1T01_ISO_ReviewForDeletion.wav")
        self.assertTrue(d.is_review_copy(first))
        self.assertEqual(os.path.basename(d.review_path("/y/1T01_ISO.wav", folder, taken)),
                         "1T01_ISO_ReviewForDeletion (2).wav")
        removed = self.make(f"{catalog.REMOVED_FOLDER}/Harbor/SD_1/Harbor/day1/1T01.wav")
        items = d.removed_items(self.root)
        self.assertEqual(items[0][:3], (removed, "Harbor", os.path.join(self.root, "SD_1/Harbor/day1/1T01.wav")))

    def test_project_groups(self):
        self.make("SD_1/Night Shift/1T01.wav", project="NIGHT SHIFT")
        self.make("SD_1/Night Shift/NIGHTSHIFT/2T01.wav", project="NIGHTSHIFT", time_reference=1)
        self.make("SD_2/SKYLINE/1T01.wav", project="SKYLINE", time_reference=2)
        self.make("SD_2/SKYLYNE/1T01.wav", project="SKYLYNE", time_reference=3)
        self.make("CHAPTER 2/1T01.wav", project="CHAPTER 2", time_reference=4)
        self.make("CHAPTER 4/1T01.wav", project="CHAPTER 4", time_reference=5)
        self.make("Lakeside/1T01.wav", project="Lakeside", time_reference=6)
        self.make("SD_1/Lakeside/1T01.wav", project="Lakeside", time_reference=6)
        self.make("FALSETAKES/9T01.wav", project="Lakeside", time_reference=7)
        groups = d.find_duplicate_projects(self.recs(), self.root, self.containers)
        found = {(g.kind, tuple(g.names)) for g in groups}
        self.assertIn(("spelling", ("NIGHT SHIFT", "NIGHTSHIFT")), found)
        self.assertIn(("similar", ("SKYLINE", "SKYLYNE")), found)
        self.assertIn(("folders", ("Lakeside",)), found)
        self.assertFalse(any("CHAPTER 2" in g.names for g in groups))  # different days, not duplicates
        lakeside = next(g for g in groups if g.kind == "folders")
        self.assertEqual(len(lakeside.files), 2)  # FALSETAKES is left alone
        self.assertEqual(d.normalize("Pine Tree Bakery"), d.normalize("Pinetreebakery"))

    def test_merge_plan(self):
        keep = os.path.join(self.root, "Harbor")
        self.make("Harbor/day1/1T01.wav", project="Harbor", time_reference=1)
        self.make("SD_1/Harbor/day1/1T01.wav", project="Harbor", time_reference=1)  # identical -> remove
        self.make("SD_1/Harbor/day2/5T01.wav", project="Harbor", time_reference=5)  # only here -> move
        self.make("SD_1/Harbor/day1/1T02.wav", project="Harbor", time_reference=9)  # -> move
        self.make("Harbor/day1/1T02.wav", project="Harbor", time_reference=8)  # different file, same name
        noted = self.make("SD_2/Harbor/day1/1T01.wav", project="Harbor", time_reference=1)
        bwf.update_metadata(noted, {"note": "keep me"})  # same audio, but a note the kept file lacks
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        actions = d.plan_project_merge(recs, group, keep, d.Fingerprints())
        by_file = {os.path.relpath(a.rec.path, self.root): a for a in actions}
        self.assertEqual(by_file["SD_1/Harbor/day1/1T01.wav"].kind, "remove")
        self.assertEqual(by_file["SD_1/Harbor/day2/5T01.wav"].kind, "move")
        self.assertEqual(by_file["SD_1/Harbor/day2/5T01.wav"].dst, os.path.join(keep, "day2/5T01.wav"))
        self.assertEqual(by_file["SD_1/Harbor/day1/1T02.wav"].kind, "skip")
        self.assertNotIn("Harbor/day1/1T01.wav", by_file)  # already in the kept folder
        # It has a note the kept copy lacks: versions with notes are preferred.
        self.assertEqual(by_file["SD_2/Harbor/day1/1T01.wav"].kind, "replace")

    def test_merge_into_a_new_folder(self):
        self.make("SD_1/Harbor/day1/1T01.wav", project="Harbor", time_reference=1)
        self.make("SD_2/Harbor/day1/1T01.wav", project="Harbor", time_reference=1)  # identical copy
        self.make("SD_2/Harbor/day2/5T01.wav", project="Harbor", time_reference=5)  # only here
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        new = os.path.join(self.root, d.suggested_folder_name(group))
        self.assertEqual(new, os.path.join(self.root, "Harbor"))
        actions = d.plan_project_merge(recs, group, new, d.Fingerprints(), root=self.root, containers=self.containers)
        kinds = sorted((a.kind, os.path.relpath(a.rec.path, self.root)) for a in actions)
        self.assertEqual(kinds, [("move", "SD_1/Harbor/day1/1T01.wav"), ("move", "SD_2/Harbor/day2/5T01.wav"),
                                 ("remove", "SD_2/Harbor/day1/1T01.wav")])
        removal = next(a for a in actions if a.kind == "remove")
        # Checked against the moving copy where it is now, before it moves.
        self.assertEqual(removal.dst, os.path.join(self.root, "SD_1/Harbor/day1/1T01.wav"))
        self.assertEqual({a.dst for a in actions if a.kind == "move"},
                         {os.path.join(new, "day1/1T01.wav"), os.path.join(new, "day2/5T01.wav")})

    def test_batch_helpers(self):
        self.make("Harbor/day1/1T01.wav", project="Harbor", time_reference=1)
        self.make("Harbor/day1/1T02.wav", project="Harbor", time_reference=2)
        self.make("SD_1/Harbor/day1/1T01.wav", project="Harbor", time_reference=1)
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "folders")
        self.assertEqual(d.suggest_keep_folder(group, self.root, self.containers)[0], os.path.join(self.root, "Harbor"))
        self.assertEqual(d.default_project_name(group), "")
        # An earlier merge moved the card copy away: the refreshed group no longer has it.
        os.remove(os.path.join(self.root, "SD_1/Harbor/day1/1T01.wav"))
        fresh = d.refresh_group(group, self.recs(), self.root)
        self.assertEqual(len(fresh.files), 2)
        self.assertEqual(fresh.locations, [os.path.join(self.root, "Harbor"), os.path.join(self.root, "SD_1/Harbor")])

    def test_name_merge_retags(self):
        self.make("SD_1/Night Shift/1T01.wav", project="NIGHT SHIFT", time_reference=1)
        self.make("SD_1/Night Shift/NIGHTSHIFT/2T01.wav", project="NIGHTSHIFT", time_reference=2)
        recs = self.recs()
        group = next(g for g in d.find_duplicate_projects(recs, self.root, self.containers) if g.kind == "spelling")
        actions = d.plan_project_merge(recs, group, os.path.join(self.root, "SD_1/Night Shift"), d.Fingerprints(),
                                       target_name="NIGHT SHIFT")
        self.assertEqual([(a.kind, a.rec.project) for a in actions], [("retag", "NIGHTSHIFT")])
        self.assertEqual(d.default_project_name(group), "NIGHT SHIFT")  # a tie: the folder's name wins
        only_names = d.plan_project_merge(recs, group, os.path.join(self.root, "SD_1/Night Shift"),
                                          d.Fingerprints(), target_name="NIGHTSHIFT", move_files=False)
        self.assertEqual([a.rec.project for a in only_names], ["NIGHT SHIFT"])


if __name__ == "__main__":
    unittest.main()
