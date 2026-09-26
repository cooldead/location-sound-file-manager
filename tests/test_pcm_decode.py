import unittest

import numpy as np

from sound_file_manager.waveform import decode


class PCMDecodeTests(unittest.TestCase):
    def test_signed_24_bit_samples_and_channel_order(self):
        values = [-8388608, -8388607, -65536, -256, -1, 0, 1, 255, 256, 65535, 8388606, 8388607]
        raw = b''.join(v.to_bytes(3, 'little', signed=True) for v in values)
        expected = (np.array(values, dtype=np.float32) / 8388608).reshape(-1, 3)
        np.testing.assert_array_equal(decode(raw, 24, 3, False), expected)

    def test_random_pcm_and_incomplete_frame(self):
        raw = np.random.default_rng(42).integers(0, 256, 24000, dtype=np.uint8).tobytes()
        expected = np.array([int.from_bytes(raw[i:i+3], 'little', signed=True)
                             for i in range(0, len(raw), 3)], dtype=np.float32) / 8388608
        np.testing.assert_array_equal(decode(raw + b'\xff\x01', 24, 8, False), expected.reshape(-1, 8))
        self.assertEqual(decode(b'', 24, 8, False).shape, (0, 8))
