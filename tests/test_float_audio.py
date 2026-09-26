import os
import struct
import tempfile
import unittest
from dataclasses import replace

import numpy as np

from sound_file_manager import bwf, catalog, duplicates, waveform
from sound_file_manager.audio_engine import AudioEngine
from sound_file_manager.mixer import MixerState
from tests import qt_app
from tests.wavmaker import chunk


class FloatAudioTests(unittest.TestCase):
    def test_float_wav_formats_and_headroom(self):
        qt_app()
        samples = np.array([[2.0, -2.0], [0.25, -0.5], [0, 0], [1.5, -1.5]], dtype='<f4')
        with tempfile.TemporaryDirectory() as folder:
            for extensible in (False, True):
                with self.subTest(extensible=extensible):
                    fmt = struct.pack('<HHIIHH', 0xfffe if extensible else 3, 2, 48000, 384000, 8, 32)
                    if extensible:
                        fmt += struct.pack('<HHI', 22, 32, 3)
                        fmt += struct.pack('<IHH8s', 3, 0, 0x10, b'\x80\x00\x00\xaa\x00\x38\x9b\x71')
                    body = b'WAVE' + chunk(b'fmt ', fmt) + chunk(b'fact', struct.pack('<I', 4)) + chunk(b'data', samples.tobytes())
                    path = os.path.join(folder, 'float.wav')
                    with open(path, 'wb') as f:
                        f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
                    info = bwf.read_info(path)
                    self.assertEqual((info.format_tag, info.bits), (3, 32))
                    rec = catalog.read_recording(path)
                    self.assertTrue(rec.float_samples)
                    self.assertIn('32-bit float', rec.format_label)
                    self.assertNotEqual(duplicates.audio_key(rec), duplicates.audio_key(replace(rec, float_samples=False)))
                    np.testing.assert_array_equal(waveform.decode(samples.tobytes(), 32, 2, True), samples)
                    levels = waveform.compute_peaks(path, 4)
                    self.assertEqual(levels.shape, (2, 2, 4))
                    self.assertTrue(np.isfinite(levels).all())
                    engine = AudioEngine()
                    try:
                        engine.open(path)
                        self.assertTrue(engine._float)
                        engine.mixer = MixerState.for_tracks(['L', 'R'], stereo_pair=True)
                        engine.mixer.auto_trim = True
                        engine.mixer.master_db = -12
                        mixed = np.frombuffer(engine._mix(samples, 0), np.float32).reshape(-1, 2)
                        # Over-range input is preserved until gain is applied.
                        np.testing.assert_allclose(mixed, samples * (10 ** (-12 / 20)), atol=1e-6)
                    finally:
                        engine.shutdown()
