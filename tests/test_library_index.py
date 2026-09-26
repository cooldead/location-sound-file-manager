import gzip
import json
import os
import shutil
import tempfile
import unicodedata
import unittest

from sound_file_manager import catalog, library_index, waveform
from sound_file_manager.library_index import LibraryIndex

from .wavmaker import make_wav


class LibraryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "lib")
        os.makedirs(os.path.join(self.root, "Proj", "250101"))
        for take in ("01", "02"):
            make_wav(os.path.join(self.root, "Proj", "250101", f"10T{take}_ISO.wav"), take=take,
                     filename=f"10T{take}_ISO.wav")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def scan(self, root, cache_name, index=None):
        cache = catalog.Cache(os.path.join(self.tmp, cache_name))
        try:
            recs = []
            stats = catalog.scan(root, cache, on_batch=recs.extend, index=index)
            return stats, recs
        finally:
            cache.close()

    def test_another_computer_reads_nothing_when_the_index_matches(self):
        index = LibraryIndex.load(self.root)
        stats, recs = self.scan(self.root, "a.sqlite", index)
        self.assertEqual(stats.parsed, 2)
        self.assertTrue(index.changed)
        index.save()
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".sfm-index", "metadata.json.gz")))

        # The same share mounted somewhere else, with an empty cache.
        other = os.path.join(self.tmp, "mounted elsewhere")
        os.symlink(self.root, other)
        index2 = LibraryIndex.load(other)
        stats2, recs2 = self.scan(other, "b.sqlite", index2)
        self.assertEqual((stats2.parsed, stats2.indexed), (0, 2))
        self.assertEqual(sorted(r.path for r in recs2), sorted(r.path.replace(self.root, other) for r in recs))
        self.assertEqual([r.take for r in sorted(recs2, key=lambda r: r.path)], ["01", "02"])
        self.assertFalse(index2.changed)

    def test_a_changed_file_is_read_again(self):
        index = LibraryIndex.load(self.root)
        self.scan(self.root, "a.sqlite", index)
        index.save()
        path = os.path.join(self.root, "Proj", "250101", "10T01_ISO.wav")
        make_wav(path, take="07", filename="10T01_ISO.wav")
        os.utime(path, (1_000_000, 1_000_000))
        index2 = LibraryIndex.load(self.root)
        stats, recs = self.scan(self.root, "b.sqlite", index2)
        self.assertEqual((stats.parsed, stats.indexed), (1, 1))
        self.assertEqual({r.name: r.take for r in recs}["10T01_ISO.wav"], "07")
        self.assertTrue(index2.changed)

    def test_gone_files_leave_the_index(self):
        index = LibraryIndex.load(self.root)
        self.scan(self.root, "a.sqlite", index)
        index.save()
        os.remove(os.path.join(self.root, "Proj", "250101", "10T02_ISO.wav"))
        index2 = LibraryIndex.load(self.root)
        self.scan(self.root, "a.sqlite", index2)
        self.assertEqual(list(index2.entries), ["Proj/250101/10T01_ISO.wav"])
        self.assertTrue(index2.changed)

    def test_the_index_folder_is_not_scanned(self):
        index = LibraryIndex.load(self.root)
        self.scan(self.root, "a.sqlite", index)
        index.save()
        library_index.write_levels(self.root, os.path.join(self.root, "x.wav"), 1, 1.0, b"x")
        stats, _ = self.scan(self.root, "b.sqlite")
        self.assertEqual(stats.found, 2)

    def test_an_index_from_an_older_parser_or_a_bad_file_is_ignored(self):
        index = LibraryIndex.load(self.root)
        self.scan(self.root, "a.sqlite", index)
        index.save()
        with open(index.path, "rb") as f:
            doc = json.loads(gzip.decompress(f.read()))
        doc["parser"] = catalog.PARSER_VERSION - 1
        with open(index.path, "wb") as f:
            f.write(gzip.compress(json.dumps(doc).encode()))
        self.assertEqual(len(LibraryIndex.load(self.root)), 0)
        with open(index.path, "wb") as f:
            f.write(b"not gzip")
        self.assertEqual(len(LibraryIndex.load(self.root)), 0)

    def test_keys_are_relative_and_nfc(self):
        decomposed = unicodedata.normalize("NFD", "/lib/Café/a.wav")
        self.assertEqual(library_index.relative_key("/lib/", decomposed), "Café/a.wav")
        self.assertEqual(library_index.relative_key("/lib", "/lib/Café/a.wav"), "Café/a.wav")
        self.assertIsNone(library_index.relative_key("/lib", "/library/a.wav"))
        self.assertIsNone(library_index.relative_key("/lib", "/media/card/a.wav"))

    def test_waveform_levels(self):
        path = os.path.join(self.root, "Proj", "250101", "10T01_ISO.wav")
        levels = waveform.compute_peaks(path, 64)
        blob = waveform.to_bytes(levels)
        self.assertTrue(library_index.write_levels(self.root, path, 100, 5.0, blob))
        self.assertEqual(library_index.read_levels(self.root, path, 100, 5.0), blob)
        self.assertIsNone(library_index.read_levels(self.root, path, 101, 5.0))  # file changed
        self.assertIsNone(library_index.read_levels(self.root, path, 100, 6.0))
        self.assertTrue(library_index.has_waveforms(self.root))
        self.assertFalse(library_index.write_levels(self.root, "/elsewhere/a.wav", 1, 1.0, blob))
        leftovers = [n for _, _, names in os.walk(os.path.join(self.root, ".sfm-index")) for n in names
                     if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_waveform_size_estimate(self):
        # 6 tracks x 4096 buckets x peak + RMS = 48 KB, rounded to 4 KB blocks.
        self.assertEqual(library_index.waveform_bytes(6, 4096), 53248)


if __name__ == "__main__":
    unittest.main()
