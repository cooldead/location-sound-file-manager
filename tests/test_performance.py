"""Concurrency, cache invalidation and UI responsiveness regression tests."""
import dataclasses
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QModelIndex, QTimer

from sound_file_manager import catalog, compat
from sound_file_manager.audio_engine import AudioEngine, AudioSource
from sound_file_manager.file_model import COL, RecordingsModel, RecordingsProxy
from sound_file_manager.player import PlayerWidget

from . import dispose, qt_app
from .wavmaker import make_wav


class ConcurrentScanTests(unittest.TestCase):
    def test_uncached_reads_overlap_and_cache_stays_on_owner_thread(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(8):
                make_wav(compat.join(root, f'{i}.wav'))
            cache = catalog.Cache(compat.join(root, 'cache.sqlite'))
            self.addCleanup(cache.close)
            original = catalog.read_recording
            barrier = threading.Barrier(4)
            thread_ids = set()
            lock = threading.Lock()

            def read(path, stat):
                with lock:
                    thread_ids.add(threading.get_ident())
                barrier.wait(timeout=3)
                return original(path, stat)

            got = []
            with patch.object(catalog, 'read_recording', read):
                stats = catalog.scan(root, cache, on_batch=got.extend, metadata_workers=4)
            self.assertEqual(len(thread_ids), 4)
            self.assertEqual((stats.parsed, stats.found, len(got)), (8, 8, 8))
            with patch.object(catalog, 'read_recording', side_effect=AssertionError('cache miss')):
                stats = catalog.scan(root, cache, on_batch=lambda batch: None)
            self.assertEqual(stats.cached, 8)

    def test_cancelled_reads_never_prune_or_publish_index(self):
        with tempfile.TemporaryDirectory() as root:
            path = compat.join(root, 'new.wav')
            make_wav(path)
            cache = catalog.Cache(compat.join(root, 'cache.sqlite'))
            self.addCleanup(cache.close)
            stale = catalog.Recording(compat.join(root, 'old.wav'), 1, 0)
            cache.put(stale)
            cancel = threading.Event()
            original = catalog.read_recording

            def read(path, stat):
                rec = original(path, stat)
                cancel.set()
                return rec

            from sound_file_manager.library_index import LibraryIndex
            index = LibraryIndex(root)
            index.replace_all([stale])
            previous = dict(index.entries)
            with patch.object(catalog, 'read_recording', read):
                stats = catalog.scan(root, cache, on_batch=lambda batch: None,
                                     cancelled=cancel.is_set, index=index)
            self.assertEqual(stats.removed, 0)
            self.assertIsNotNone(cache.get(stale.path, 1, 0))
            self.assertEqual(index.entries, previous)


class TablePerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def test_plain_cell_does_not_format_timecode(self):
        model = RecordingsModel()
        rec = catalog.Recording('/fake/a.wav', 1, 0, scene='12')
        with patch.object(catalog.Recording, 'timecode_at', side_effect=AssertionError('unneeded timecode')):
            self.assertEqual(model._text(rec, COL['Scene']), '12')

    def test_search_reuses_text_and_invalidates_replaced_and_reset_rows(self):
        model = RecordingsModel()
        proxy = RecordingsProxy()
        proxy.setSourceModel(model)
        rec = catalog.Recording('/fake/a.wav', 1, 0, note='first')
        model.set_recordings([rec], '/fake')
        with patch.object(catalog.Recording, 'timecode_at', return_value='01:02:03:04') as tc:
            proxy.set_text('first')
            self.assertTrue(proxy.filterAcceptsRow(0, QModelIndex()))
            proxy.set_text('01:02')
            self.assertTrue(proxy.filterAcceptsRow(0, QModelIndex()))
            self.assertEqual(tc.call_count, 1)
        model.replace(rec.path, dataclasses.replace(rec, note='second'))
        proxy.set_text('first')
        self.assertFalse(proxy.filterAcceptsRow(0, QModelIndex()))
        proxy.set_text('second')
        self.assertTrue(proxy.filterAcceptsRow(0, QModelIndex()))
        model.set_recordings([rec], '/fake')
        self.assertFalse(proxy.filterAcceptsRow(0, QModelIndex()))


class AsyncSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = [compat.join(self.tmp.name, name) for name in ('a.wav', 'b.wav')]
        for path in (self.a, self.b):
            make_wav(path)
        self.player = dispose(self, PlayerWidget())
        self.addCleanup(self.player.shutdown)
        self.ra, self.rb = map(catalog.read_recording, (self.a, self.b))

    def until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(condition(), 'background work did not finish')

    def test_slow_previous_open_does_not_block_ui_or_replace_selection(self):
        entered, unblock = threading.Event(), threading.Event()
        sources = []

        def prepare(path):
            source = AudioSource(path)
            sources.append(source)
            if path == self.a:
                entered.set()
                unblock.wait(3)
            return source

        with patch('sound_file_manager.player.AudioSource', prepare):
            self.player.load(self.ra)
            self.assertTrue(entered.wait(2))
            try:
                ticked = []
                QTimer.singleShot(0, lambda: ticked.append(True))
                self.player.load(self.rb, start=0.02)
                self.until(lambda: self.player.engine.path == self.b)
                self.assertTrue(ticked)
                self.assertEqual(self.player.engine.position(), 960)
                self.assertFalse(self.player.engine.playing)
            finally:
                unblock.set()
            self.until(lambda: not self.player._opening)
            self.player.engine.wait_closed()
            self.assertEqual(self.player.engine.path, self.b)
            self.assertIsNone(next(s for s in sources if s.path == self.a).fd)

    def test_release_closes_pending_source_and_never_adopts_it(self):
        with patch('sound_file_manager.player.AudioSource', wraps=AudioSource):
            self.player.load(self.ra, start=0.02)
            state = self.player.release()
            self.assertEqual(state[0].path, self.a)
            self.assertEqual(state[1], 0.02)
            self.assertFalse(self.player._opening)
            self.assertIsNone(self.player.engine.path)
            self.app.processEvents()
            self.assertIsNone(self.player.engine.path)
        # Windows also verifies that no handle prevents the rename.
        os.rename(self.a, self.a + '.moved')

    def test_explicit_play_waits_for_open_and_selection_does_not_autoplay(self):
        with patch.object(self.player.engine, 'play') as play:
            self.player.load(self.ra)
            self.player.toggle_play()
            self.until(lambda: self.player.engine.path == self.a)
            play.assert_called_once()
            self.player.load(self.rb)
            self.until(lambda: self.player.engine.path == self.b)
            play.assert_called_once()

    def test_selection_close_does_not_wait_for_blocked_reader(self):
        engine = self.player.engine
        source = AudioSource(self.a)
        entered, unblock = threading.Event(), threading.Event()
        original = source.read

        def slow_read(size, offset):
            with source.lock:
                entered.set()
                unblock.wait(3)
            return original(size, offset)

        with patch.object(source, 'read', slow_read):
            engine.adopt(source)
            self.assertTrue(entered.wait(2))
            try:
                started = time.monotonic()
                engine.close(wait=False)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertIsNone(engine.path)
                self.assertFalse(unblock.is_set())
            finally:
                unblock.set()
            engine.wait_closed()
            self.assertIsNone(source.fd)
