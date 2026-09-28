"""Rust/NumPy parity for all PCM formats and waveform read strategies."""
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from sound_file_manager import compat, native_waveform, waveform
from .wavmaker import make_wav


@unittest.skipUnless(native_waveform.AVAILABLE, 'build the Rust library with scripts/build_native.py')
class NativeWaveformTests(unittest.TestCase):
    def test_all_formats_and_partial_frames(self):
        rng = np.random.default_rng(42)
        for channels in (1, 2, 8):
            for bits, floating in ((8, False), (16, False), (24, False), (32, False),
                                   (32, True), (64, True)):
                with self.subTest(channels=channels, bits=bits, floating=floating):
                    if floating:
                        values = rng.normal(size=(137, channels)).astype('<f4' if bits == 32 else '<f8')
                        values.flat[:5] = [np.nan, np.inf, -np.inf, 2.0, -2.0]
                        raw = values.tobytes()
                    else:
                        raw = rng.integers(0, 256, 137 * channels * (bits // 8), dtype=np.uint8).tobytes()
                    if channels * (bits // 8) > 1:
                        raw += b'\xff'  # incomplete frame is ignored
                    cuts = np.array([0, 1, 16, 99, 137])
                    actual = waveform._reduce(raw, bits, channels, floating, cuts)
                    with patch.object(native_waveform, '_library', None):
                        expected = waveform._reduce(raw, bits, channels, floating, cuts)
                    np.testing.assert_array_equal(actual[0], expected[0])
                    np.testing.assert_allclose(actual[1], expected[1], rtol=1e-12)

    def test_complete_sampled_and_zoomed_waveforms(self):
        with tempfile.TemporaryDirectory() as root:
            path = compat.join(root, 'sample.wav')
            samples = np.random.default_rng(7).normal(0, 0.3, (1003, 3)).astype(np.float32)
            make_wav(path, float_samples=samples)
            for limit in (1, 1 << 30):
                for start, end in ((0, None), (127, 977), (0, 1), (1, 1)):
                    with self.subTest(limit=limit, start=start, end=end):
                        partials, reference_partials = [], []
                        with patch.object(waveform, 'FULL_READ_LIMIT', limit), \
                             patch.object(waveform, 'PIECE_BYTES', 132), \
                             patch.object(waveform, 'QUICK_PREVIEW_ABOVE', 1):
                            result = waveform.compute_peaks(path, 37, first_frame=start, end_frame=end,
                                                            on_partial=partials.append)
                            with patch.object(native_waveform, '_library', None):
                                reference = waveform.compute_peaks(path, 37, first_frame=start, end_frame=end,
                                                                  on_partial=reference_partials.append)
                        np.testing.assert_allclose(result, reference, atol=1e-7)
                        self.assertEqual(waveform.to_bytes(result), waveform.to_bytes(reference))
                        self.assertEqual(len(partials), len(reference_partials))
                        for a, b in zip(partials, reference_partials):
                            np.testing.assert_allclose(a, b, atol=1e-7)

    def test_bad_cuts_rejected_and_cancellation_preserved(self):
        for cuts in ([0, 7, 2], [0, 1, 9], [1, 2]):
            with self.assertRaises(ValueError):
                native_waveform.reduce(b'\0' * 4, 16, 1, False, cuts)
        with tempfile.TemporaryDirectory() as root:
            path = compat.join(root, 'sample.wav')
            make_wav(path)
            self.assertIsNone(waveform.compute_peaks(path, cancelled=lambda: True))
