"""Detailed overviews find real transients and remain cheap after caching."""
import itertools
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from sound_file_manager import catalog, compat, library_index, waveform
from sound_file_manager.player import _PeaksJob
from . import qt_app
from .wavmaker import make_wav


class WaveformRefinementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = compat.join(self.tmp.name, 'synthetic.wav')
        samples = np.full((48000, 1), 0.01, np.float32)
        samples[85, 0] = 1.0  # outside the first 64 frames of the first sampled slot
        make_wav(self.path, float_samples=samples)
        self.rec = catalog.read_recording(self.path)
        self.cache_file = compat.join(self.tmp.name, 'cache.sqlite')

    def cache_blob(self):
        cache = catalog.Cache(self.cache_file)
        try:
            return cache.get_peaks(self.rec.path, self.rec.size, self.rec.mtime)
        finally:
            cache.close()

    def put_blob(self, blob):
        cache = catalog.Cache(self.cache_file)
        try:
            cache.put_peaks(self.rec.path, self.rec.size, self.rec.mtime, blob)
        finally:
            cache.close()

    def test_full_read_finds_peak_that_sampled_overview_misses(self):
        partials = []
        with patch.object(waveform, 'FULL_READ_LIMIT', 1), \
             patch.object(waveform, 'QUICK_PREVIEW_ABOVE', 1), \
             patch.object(waveform, 'SAMPLE_BYTES', 64):
            coarse = waveform.compute_peaks(self.path)
            detailed = waveform.compute_peaks(self.path, full_read=True, on_partial=partials.append)
        self.assertLess(coarse[0].max(), 0.5)
        self.assertEqual(detailed[0].max(), 1.0)
        self.assertGreaterEqual(len(partials), 2)
        self.assertLess(partials[0][0].max(), 0.5)
        np.testing.assert_array_equal(partials[-1], detailed)

    def test_refinement_keeps_unread_preview_and_can_cancel(self):
        preview = np.full((2, 1, waveform.BUCKETS), 0.42, np.float32)
        partials = []
        with patch.object(waveform, 'PIECE_BYTES', 512), \
             patch.object(waveform.time, 'monotonic', side_effect=itertools.count(step=0.3)):
            result = waveform.compute_peaks(self.path, full_read=True, preview=preview,
                                            on_partial=partials.append, cancelled=lambda: bool(partials))
        self.assertIsNone(result)
        self.assertEqual(len(partials), 1)
        np.testing.assert_array_equal(partials[0][..., -100:], preview[..., -100:])
        self.assertFalse(np.array_equal(partials[0][..., :10], preview[..., :10]))

    def test_precise_cache_and_legacy_compatibility(self):
        values = np.linspace(0, 1, 5003, dtype=np.float32).reshape(1, 1, -1).repeat(2, axis=0)
        blob = waveform.to_bytes(values)
        self.assertTrue(waveform.is_complete(blob))
        recovered = waveform.from_bytes(blob)
        self.assertLess(np.max(np.abs(recovered - values)), 1 / 65535)
        self.assertGreater(len(np.unique(recovered)), 256)
        self.assertFalse(waveform.is_complete(waveform.to_bytes(values, complete=False)))
        legacy = waveform.LEGACY_MAGIC + bytes([1]) + (values * 255).round().astype(np.uint8).tobytes()
        np.testing.assert_allclose(waveform.from_bytes(legacy), values, atol=1 / 255)
        self.assertFalse(waveform.is_complete(legacy))
        self.assertIsNone(waveform.from_bytes(blob[:-1]))

    def test_selected_job_refines_old_cache_then_reuses_complete_result(self):
        coarse = np.zeros((2, 1, waveform.BUCKETS), np.float32)
        legacy = waveform.LEGACY_MAGIC + bytes([1]) + (coarse * 255).astype(np.uint8).tobytes()
        self.put_blob(legacy)
        partials, results = [], []
        job = _PeaksJob(1, self.rec, self.cache_file, lambda g: True)
        job.signals.partial.connect(lambda g, p: partials.append(p))
        job.signals.done.connect(lambda g, p, e: results.append((p, e)))
        job.run()
        np.testing.assert_array_equal(partials[0], coarse)
        self.assertEqual(results[-1][0][0].max(), 1.0)
        self.assertEqual(results[-1][1], '')
        self.assertTrue(waveform.is_complete(self.cache_blob()))
        cached = []
        again = _PeaksJob(2, self.rec, self.cache_file, lambda g: True)
        again.signals.done.connect(lambda g, p, e: cached.append((p, e)))
        with patch.object(waveform, 'compute_peaks', side_effect=AssertionError('unnecessary WAV read')) as read:
            again.run()
        read.assert_not_called()
        self.assertEqual(cached[-1][1], '')
        self.assertEqual(cached[-1][0][0].max(), 1.0)

    def test_cancelled_job_saves_preview_without_marking_complete(self):
        current = [True]
        job = _PeaksJob(1, self.rec, self.cache_file, lambda g: current[0])
        job.signals.partial.connect(lambda g, p: current.__setitem__(0, False))
        with patch.object(waveform, 'QUICK_PREVIEW_ABOVE', 1):
            job.run()
        blob = self.cache_blob()
        self.assertIsNotNone(waveform.from_bytes(blob))
        self.assertFalse(waveform.is_complete(blob))

    def test_prefetch_and_disabled_detail_keep_limited_reads(self):
        original = waveform.compute_peaks
        for prefetch, detailed in ((True, True), (False, False)):
            with self.subTest(prefetch=prefetch):
                with patch.object(waveform, 'compute_peaks', wraps=original) as read, \
                     patch.object(waveform, 'FULL_READ_LIMIT', 1):
                    _PeaksJob(1, self.rec, None, lambda g: True,
                              prefetch=prefetch, detailed=detailed).run()
                self.assertFalse(read.call_args.kwargs['full_read'])

    def test_complete_shared_cache_skips_audio_read(self):
        full = waveform.compute_peaks(self.path, full_read=True)
        library_index.write_levels(self.tmp.name, self.rec.path, self.rec.size, self.rec.mtime,
                                   waveform.to_bytes(full))
        job = _PeaksJob(1, self.rec, self.cache_file, lambda g: True,
                        index=(self.tmp.name, True, False))
        with patch.object(waveform, 'compute_peaks', side_effect=AssertionError('unnecessary WAV read')) as read:
            job.run()
        read.assert_not_called()
        self.assertTrue(waveform.is_complete(self.cache_blob()))

    def test_failed_refinement_preserves_preview(self):
        preview = np.full((2, 1, waveform.BUCKETS), 0.4, np.float32)
        self.put_blob(waveform.to_bytes(preview, complete=False))
        results = []
        job = _PeaksJob(1, self.rec, self.cache_file, lambda g: True)
        job.signals.done.connect(lambda g, p, e: results.append((p, e)))
        with patch.object(waveform, 'compute_peaks', side_effect=OSError('share unavailable')):
            job.run()
        np.testing.assert_allclose(results[-1][0], preview, atol=1 / 65535)
        self.assertIn('share unavailable', results[-1][1])
        self.assertFalse(waveform.is_complete(self.cache_blob()))
