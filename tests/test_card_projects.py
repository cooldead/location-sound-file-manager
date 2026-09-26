import os
import unittest

from sound_file_manager import catalog, duplicates
from sound_file_manager.catalog import Recording

ROOT = "/lib"
CARD = "/card"


def rec(path, project="", tc=3600 * 48000, frames=48000, channels=2):
    return Recording(path=path, size=1000, mtime=1.0, sample_rate=48000, bits=24, channels=channels,
                     frames=frames, time_reference=tc, meta_project=project)


def card_folder(r):
    return r.path[len(CARD) + 1:].split("/")[0]


class CardProjectTests(unittest.TestCase):
    def setUp(self):
        self.library = [rec("/lib/Night Shift/250101/1T01.wav", "Night Shift"),
                        rec("/lib/Night Shift/250101/1T02.wav", "Night Shift", tc=3601 * 48000),
                        rec("/lib/Orchard/250301/5T01.wav", "Orchard", tc=7200 * 48000),
                        rec("/lib/Chapter 2/250401/9T01.wav", "Chapter 2", tc=9000 * 48000)]
        catalog.assign_projects(self.library, ROOT, [])

    def matches(self, card):
        catalog.assign_projects(card, CARD, [])
        return duplicates.card_project_matches(card, card_folder, self.library, ROOT, [])

    def test_same_project_written_differently_with_new_and_known_takes(self):
        card = [rec("/card/NIGHT SHIFT/250101/1T01.wav", "NIGHT SHIFT"),  # already in the library
                rec("/card/NIGHT SHIFT/250102/2T01.wav", "NIGHT SHIFT", tc=5000 * 48000)]  # new
        [match] = self.matches(card)["NIGHT SHIFT"]
        self.assertEqual((match.library_project, match.kind, match.library_folder),
                         ("Night Shift", "spelling", "/lib/Night Shift"))
        self.assertEqual((match.card_recordings, match.in_library, match.new_recordings, match.library_files),
                         (2, 1, 1, 2))

    def test_similar_name_and_metadata_project(self):
        card = [rec("/card/ORCHARD INC/250302/6T01.wav")]
        self.assertEqual(self.matches(card)["ORCHARD INC"][0].library_project, "Orchard")
        # The folder name says nothing, the iXML project does.
        card = [rec("/card/PROJ/250302/6T01.wav", "Orchard")]
        match = self.matches(card)["PROJ"][0]
        self.assertEqual((match.library_project, match.kind), ("Orchard", "same"))

    def test_other_numbers_and_unrelated_names_do_not_match(self):
        card = [rec("/card/Chapter 4/250402/9T01.wav"), rec("/card/Sunrise/250101/1T01.wav")]
        self.assertEqual(self.matches(card), {})

    def test_the_same_recording_needs_the_same_name_and_audio(self):
        card = [rec("/card/X/1T01.wav"),  # same name and audio as a library file
                rec("/card/X/1T09.wav"),  # other name
                rec("/card/X/1T02.wav", frames=96000)]  # same name, other length
        found = duplicates.recordings_in_library(card, self.library)
        self.assertEqual(found, {"/card/X/1T01.wav"})

    def test_similar_names(self):
        self.assertTrue(duplicates.similar_names("SUNRISE", "SUNRYSE"))
        self.assertTrue(duplicates.similar_names("Orchard", "ORCHARD INC"))
        self.assertFalse(duplicates.similar_names("CHAPTER 2", "CHAPTER 4"))
        self.assertFalse(duplicates.similar_names("AB", "ABC"))


if __name__ == "__main__":
    unittest.main()


class NestedFolderTests(unittest.TestCase):
    def test_finds_a_day_folder_inside_itself_and_plans_the_merge(self):
        lib = [rec("/lib/Proj/250101/1T01_ISO.wav", "Proj"),
               rec("/lib/Proj/250101/1T04_ISO.wav", "Proj", tc=3900 * 48000),
               rec("/lib/Proj/250101/250101/1T01_ISO.wav", "Proj"),  # the same file again
               rec("/lib/Proj/250101/250101/1T03_ISO.wav", "Proj", tc=3800 * 48000),  # new
               rec("/lib/Proj/250101/250101/1T04_ISO.wav", "Proj", tc=3950 * 48000),  # another recording
               rec("/lib/Other/250102/1T01.wav", "Other", tc=10)]
        for i, r in enumerate(lib):
            r.family = f"F{i}"
        [nested] = duplicates.find_nested_folders(lib, ROOT)
        self.assertEqual((nested.outer, nested.inner), ("/lib/Proj/250101", "/lib/Proj/250101/250101"))
        self.assertEqual(nested.counts(), (1, 1, 1))
        plan = {os.path.basename(a.rec.path): (a.kind, a.dst) for a in duplicates.plan_nested_merge(nested, lib)}
        self.assertEqual(plan, {"1T01_ISO.wav": ("remove", "/lib/Proj/250101/1T01_ISO.wav"),
                                "1T03_ISO.wav": ("move", "/lib/Proj/250101/1T03_ISO.wav"),
                                "1T04_ISO.wav": ("skip", "/lib/Proj/250101/1T04_ISO.wav")})

    def test_names_compare_loosely_and_the_library_folder_is_not_a_parent(self):
        lib = [rec("/lib/Proj/Day 1/day-1/a.wav"), rec("/lib/Proj/Proj2/a.wav", tc=5), rec("/lib/lib/a.wav", tc=6)]
        found = duplicates.find_nested_folders(lib, ROOT)
        self.assertEqual([n.inner for n in found], ["/lib/Proj/Day 1/day-1"])

    def test_only_the_deepest_of_three_levels_is_offered(self):
        lib = [rec("/lib/P/D/D/D/a.wav")]
        self.assertEqual([n.inner for n in duplicates.find_nested_folders(lib, ROOT)], ["/lib/P/D/D/D"])

    def test_a_copy_with_notes_takes_the_outer_place(self):
        outer, inner = rec("/lib/P/D/a.wav"), rec("/lib/P/D/D/a.wav")
        inner.note = "good take"
        [nested] = duplicates.find_nested_folders([outer, inner], ROOT)
        [action] = duplicates.plan_nested_merge(nested, [outer, inner])
        self.assertEqual((action.kind, action.other), ("replace", outer))
