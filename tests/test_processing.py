"""De-hiss and denoise (processing.py): the reduction, and processed copies."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np

from sound_file_manager import bwf, catalog, processing, waveform
from .wavmaker import make_wav


_TEMP_DIRS: list[str] = []


def _mkdtemp() -> str:
    """A temporary folder removed when this module's tests end (/tmp is RAM on many Linux systems)."""
    path = tempfile.mkdtemp()
    _TEMP_DIRS.append(path)
    return path


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


RATE = 48000


def tone(frames, hz, level, rate=RATE, phase=0.0):
    return (level * np.sin(2 * np.pi * hz * np.arange(frames) / rate + phase)).astype(np.float32)


def band_noise(frames, low, high, level, seed, rate=RATE):
    """White noise limited to [low, high) Hz, at an RMS of about `level`."""
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(frames))
    freqs = np.fft.rfftfreq(frames, 1 / rate)
    spectrum[(freqs < low) | (freqs >= high)] = 0
    x = np.fft.irfft(spectrum, frames)
    return (x / np.sqrt(np.mean(x ** 2)) * level).astype(np.float32)


def rms_db(x):
    return 10 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-30)


class ReduceTest(unittest.TestCase):
    def test_noise_goes_down_and_the_voice_stays(self):
        frames = RATE * 4
        noise = band_noise(frames, 50, 20000, 0.01, seed=1)
        voice = np.zeros(frames, np.float32)
        voice[RATE * 2:] = tone(RATE * 2, 440, 0.3) + tone(RATE * 2, 1200, 0.15)
        x = (voice + noise)[:, None]
        profile = processing.learn_profile(x[:RATE], RATE)
        out = processing.reduce(x, profile, processing.Settings(reduction_db=18), RATE)[:, 0]
        quiet = slice(RATE // 2, RATE * 2 - RATE // 4)
        self.assertLess(rms_db(out[quiet]), rms_db(x[quiet, 0]) - 12, "noise-only part is turned down")
        loud = slice(RATE * 2 + RATE // 4, RATE * 4 - RATE // 4)
        self.assertLess(abs(rms_db(out[loud]) - rms_db(voice[loud])), 0.5, "the voice keeps its level")

    def test_reduction_is_capped_at_the_chosen_amount(self):
        x = band_noise(RATE * 2, 50, 20000, 0.01, seed=2)[:, None]
        profile = processing.learn_profile(x, RATE)
        out = processing.reduce(x, profile, processing.Settings(reduction_db=6, threshold_db=12), RATE)[:, 0]
        inner = slice(RATE // 4, -RATE // 4)
        self.assertAlmostEqual(rms_db(x[inner, 0]) - rms_db(out[inner]), 6.0, delta=0.5)

    def test_zero_reduction_changes_nothing(self):
        x = (band_noise(RATE, 50, 20000, 0.05, seed=3) + tone(RATE, 300, 0.2))[:, None]
        profile = processing.learn_profile(x, RATE)
        out = processing.reduce(x, profile, processing.Settings(reduction_db=0), RATE)
        np.testing.assert_allclose(out, x, atol=1e-5)

    def test_dehiss_leaves_low_frequencies_alone(self):
        frames = RATE * 3
        hiss = band_noise(frames, 6000, 20000, 0.01, seed=4)
        rumble = band_noise(frames, 60, 800, 0.01, seed=5)
        x = (hiss + rumble)[:, None]
        profile = processing.learn_profile(x, RATE)
        settings = processing.Settings(mode="dehiss", reduction_db=18, hiss_from_hz=4000)
        out = processing.reduce(x, profile, settings, RATE)[:, 0]
        inner = slice(RATE // 4, -RATE // 4)

        def bands(signal):
            spectrum = np.abs(np.fft.rfft(signal[inner])) ** 2
            freqs = np.fft.rfftfreq(len(signal[inner]), 1 / RATE)
            return 10 * np.log10(spectrum[freqs < 1000].sum()), 10 * np.log10(spectrum[freqs > 6000].sum())

        low_before, high_before = bands(x[:, 0])
        low_after, high_after = bands(out)
        self.assertLess(abs(low_after - low_before), 0.3, "below an octave under 4 kHz: untouched")
        self.assertLess(high_after, high_before - 12, "the hiss is turned down")

    def test_blocks_give_the_same_result_as_one_piece(self):
        frames = 50000
        x = np.stack([band_noise(frames, 50, 20000, 0.02, seed=6) + tone(frames, 700, 0.1),
                      band_noise(frames, 50, 20000, 0.03, seed=7)], axis=1)
        profile = processing.learn_profile(x[:8000], RATE)
        settings = processing.Settings(reduction_db=15)
        whole = processing.reduce(x, profile, settings, RATE)

        def read(a, b):
            out = np.zeros((b - a, 2), np.float32)
            lo, hi = max(a, 0), min(b, frames)
            out[lo - a:hi - a] = x[lo:hi]
            return out

        pieces = [processing.reduce(x[a:a + 7777], profile, settings, RATE, read, a) for a in range(0, frames, 7777)]
        np.testing.assert_allclose(np.concatenate(pieces), whole, atol=1e-6)

    def test_only_the_chosen_channels_change(self):
        x = np.stack([band_noise(RATE, 50, 20000, 0.02, seed=8), band_noise(RATE, 50, 20000, 0.02, seed=9)], axis=1)
        profile = processing.learn_profile(x, RATE)
        out = processing.reduce(x, profile, processing.Settings(channels=(1,)), RATE)
        np.testing.assert_array_equal(out[:, 0], x[:, 0])
        self.assertLess(rms_db(out[:, 1]), rms_db(x[:, 1]) - 6)

    def test_estimated_profile_comes_from_the_quiet_parts(self):
        frames = RATE * 10
        noise = band_noise(frames, 50, 20000, 0.01, seed=10)
        voice = np.zeros(frames, np.float32)
        voice[RATE * 2:RATE * 8] = tone(RATE * 6, 500, 0.3)
        x = (voice + noise)[:, None]
        estimator = processing.ProfileEstimator(RATE, 1)
        for a in range(0, frames, 30000):
            estimator.feed(x[a:a + 30000])
        estimated = estimator.profile()
        learned = processing.learn_profile(x[:RATE * 2], RATE)
        ratio = 10 * np.log10(estimated.power[0, 5:-5].mean() / learned.power[0, 5:-5].mean())
        self.assertLess(abs(ratio), 3.0, "about the room tone's level, not the voice's")

    def test_fft_size_follows_the_rate(self):
        self.assertEqual(processing.fft_size(48000), 2048)
        self.assertEqual(processing.fft_size(44100), 2048)
        self.assertEqual(processing.fft_size(96000), 4096)
        self.assertEqual(processing.fft_size(192000), 8192)


class EncodeTest(unittest.TestCase):
    def test_24_bit_round_trips_exactly(self):
        rng = np.random.default_rng(0)
        ints = rng.integers(-8388608, 8388607, size=(1000, 3), dtype=np.int32)
        raw = b"".join(int(v).to_bytes(4, "little", signed=True)[:3] for v in ints.reshape(-1))
        samples = waveform.decode(raw, 24, 3, False)
        self.assertEqual(processing.encode(samples, 24, False).tobytes(), raw)

    def test_float_and_clipping(self):
        samples = np.array([[0.5, -2.0], [1.5, 0.25]], np.float32)
        self.assertEqual(processing.encode(samples, 32, True).tobytes(), samples.astype("<f4").tobytes())
        clipped = waveform.decode(processing.encode(samples, 24, False).tobytes(), 24, 2, False)
        self.assertLessEqual(clipped.max(), 1.0)
        self.assertGreaterEqual(clipped.min(), -1.0)


class FileTest(unittest.TestCase):
    def setUp(self):
        self.dir = _mkdtemp().replace(os.sep, "/")

    def _float_wav(self, name, samples, **kwargs):
        path = f"{self.dir}/{name}"
        make_wav(path, filename=name, float_samples=samples, **kwargs)
        return path

    def _bytes(self, path):
        with open(path, "rb") as f:
            return f.read()

    def _audio(self, path):
        info = bwf.read_info(path)
        with open(path, "rb") as f:
            layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
            data = layout.first(b"data")
            f.seek(data.data_offset)
            raw = f.read(data.size)
        return raw, waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)

    def test_output_names(self):
        src = f"{self.dir}/10T03_ISO.wav"
        self.assertEqual(processing.output_path(src), f"{self.dir}/Processed/10T03_ISO_NR.wav")
        self.assertEqual(processing.output_path(src, "beside", "_DN"), f"{self.dir}/10T03_ISO_DN.wav")
        open(f"{self.dir}/10T03_ISO_NR.wav", "wb").close()
        self.assertEqual(processing.output_path(src, "beside"), f"{self.dir}/10T03_ISO_NR_2.wav")

    def test_processed_copy_keeps_metadata_and_untouched_tracks(self):
        frames = RATE * 2
        samples = np.stack([band_noise(frames, 50, 20000, 0.02, seed=11) + tone(frames, 300, 0.2),
                            band_noise(frames, 50, 20000, 0.02, seed=12)], axis=1)
        src = self._float_wav("10T03_ISO.wav", samples, scene="10", take="03", time_reference=48000 * 3600)
        before = self._bytes(src)
        profile = processing.learn_profile(samples[:RATE // 2], RATE)
        settings = processing.Settings(channels=(1,), reduction_db=12)
        dst = processing.output_path(src)
        with mock.patch.object(processing, "BLOCK_FRAMES", 12345):
            processing.process_file(src, dst, profile, settings)
        self.assertEqual(self._bytes(src), before, "the original is untouched")
        rec, orig = catalog.read_recording(dst), catalog.read_recording(src)
        for key in ("scene", "take", "project", "time_reference", "frames", "channels", "sample_rate", "bits",
                    "float_samples", "tracks"):
            self.assertEqual(getattr(rec, key), getattr(orig, key), key)
        self.assertEqual(os.listdir(os.path.dirname(dst)), ["10T03_ISO_NR.wav"], "no temporary file left")
        raw_out, out = self._audio(dst)
        raw_in, original = self._audio(src)
        np.testing.assert_array_equal(out[:, 0], original[:, 0])
        expected = processing.reduce(original, profile, settings, RATE)
        np.testing.assert_allclose(out[:, 1], expected[:, 1], atol=1e-6)
        with open(dst, "rb") as f:
            layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
            chunks = {}
            for chunk in layout.chunks:
                f.seek(chunk.data_offset)
                chunks[chunk.id] = f.read(chunk.size)
        self.assertIn(b"FILENAME=10T03_ISO_NR.wav", chunks[b"bext"][:256])
        self.assertIn(b"Location Sound File Manager denoise", chunks[b"bext"][602:])
        self.assertIn(b"<PARENT_FILENAME>10T03_ISO.wav</PARENT_FILENAME>", chunks[b"iXML"])

    def test_24_bit_file_and_failures_leave_nothing(self):
        src = f"{self.dir}/1T01.wav"
        make_wav(src, filename="1T01.wav", frames=RATE)
        profile = processing.region_profile(src, 0, RATE // 2)
        dst = processing.output_path(src)
        processing.process_file(src, dst, profile, processing.Settings())
        self.assertEqual(bwf.read_info(dst).frames, RATE)
        with self.assertRaises(bwf.WavError):  # never overwrites
            processing.process_file(src, dst, profile, processing.Settings())
        other = processing.Profile(profile.power, 44100)
        with self.assertRaises(ValueError):
            processing.process_file(src, processing.output_path(src), other, processing.Settings())
        self.assertEqual(sorted(os.listdir(os.path.dirname(dst))), ["1T01_NR.wav"])

    def test_cancel_leaves_nothing(self):
        src = f"{self.dir}/1T02.wav"
        make_wav(src, filename="1T02.wav", frames=RATE)
        profile = processing.estimate_file_profile(src)
        dst = processing.output_path(src, "beside")
        with self.assertRaises(InterruptedError):
            processing.process_file(src, dst, profile, processing.Settings(), cancelled=lambda: True)
        self.assertFalse(os.path.exists(dst))
        self.assertEqual(sorted(os.listdir(self.dir)), ["1T02.wav"])

    def test_region_too_short(self):
        src = f"{self.dir}/1T03.wav"
        make_wav(src, filename="1T03.wav", frames=RATE)
        with self.assertRaises(ValueError):
            processing.region_profile(src, 0, 100)

    def test_preview_matches_the_written_file(self):
        frames = RATE * 2
        samples = (band_noise(frames, 50, 20000, 0.02, seed=13) + tone(frames, 300, 0.2))[:, None]
        src = self._float_wav("2T01.wav", samples)
        profile = processing.estimate_file_profile(src)
        settings = processing.Settings()
        original, processed, rate = processing.preview(src, 30000, 60000, profile, settings)
        self.assertEqual(rate, RATE)
        dst = processing.output_path(src)
        processing.process_file(src, dst, profile, settings)
        np.testing.assert_allclose(processed, self._audio(dst)[1][30000:60000], atol=1e-6)
        np.testing.assert_array_equal(original, samples[30000:60000])


if __name__ == "__main__":
    unittest.main()
