"""32-bit (and 64-bit) float WAVs: parsing, levels, playback decoding,
metadata writes and duplicate checks."""

import os
import tempfile
import unittest

import numpy as np

from sound_file_manager import bwf, catalog, compat, duplicates, waveform

from .wavmaker import make_wav


def tone(frames=4800, channels=2, peak=0.5):
    t = np.arange(frames) / 48000
    wave = np.sin(2 * np.pi * 440 * t) * peak
    return np.stack([wave * (0.5 ** c) for c in range(channels)], axis=1).astype(np.float32)


class FloatWavTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return compat.join(self.tmp.name, name)

    def test_parse_plain_extensible_and_64(self):
        for name, kwargs in (("plain.wav", {}), ("ext.wav", {"extensible": True}),
                             ("f64.wav", {"float_bits": 64}), ("f64ext.wav", {"float_bits": 64, "extensible": True})):
            path = self.path(name)
            make_wav(path, float_samples=tone(), **kwargs)
            info = bwf.read_info(path)
            self.assertEqual(info.format_tag, 3, name)
            rec = catalog.read_recording(path)
            self.assertEqual(rec.error, "", name)
            self.assertTrue(rec.float_samples, name)
            self.assertEqual(rec.frames, 4800, name)
            self.assertIn("float", rec.format_label, name)

    def test_levels_and_over_zero_dbfs(self):
        samples = tone(peak=0.5)
        samples[2000:2100, 0] = 3.0  # +9.5 dBFS: only a float file can hold this
        path = self.path("over.wav")
        make_wav(path, float_samples=samples, extensible=True)
        levels = waveform.compute_peaks(path, buckets=48)
        self.assertEqual(levels.shape, (2, 2, 48))
        self.assertEqual(float(levels[0, 0].max()), 1.0)  # shown at full scale…
        self.assertAlmostEqual(float(levels[0, 1].max()), (20 * np.log10(0.25) + 60) / 60, places=2)
        pixels = waveform.rasterize(levels, 96, 60, colors=["#27c4d8", "#6fcf4a"], mode="lanes")
        red = ((pixels >> 16) & 0xFF == 255) & ((pixels >> 8) & 0xFF == 59)
        self.assertTrue(red[:30].any())  # …with a red over mark in channel 1's lane
        self.assertFalse(red[30:].any())  # channel 2 never reaches full scale

    def test_damaged_float_samples(self):
        samples = tone()
        samples[10, 0], samples[11, 1], samples[12, 0] = np.nan, np.inf, -np.inf
        raw = samples.tobytes()
        decoded = waveform.decode(raw, 32, 2, True)
        self.assertTrue(np.isfinite(decoded).all())
        path = self.path("nan.wav")
        make_wav(path, float_samples=samples)
        self.assertTrue(np.isfinite(waveform.compute_peaks(path, buckets=10)).all())

    def test_metadata_write_keeps_audio(self):
        path = self.path("meta.wav")
        data = make_wav(path, float_samples=tone(), extensible=True)
        bwf.update_metadata(path, {"note": "plane overhead", "scene": "12"})
        info = bwf.read_info(path)
        self.assertEqual((info.value("note"), info.value("scene"), info.format_tag), ("plane overhead", "12", 3))
        with open(path, "rb") as f:
            layout = bwf.read_layout(f, os.path.getsize(path))
            chunk = layout.first(b"data")
            f.seek(chunk.data_offset)
            self.assertEqual(f.read(chunk.size), data)

    def test_duplicates_on_float(self):
        a, b, c = self.path("a.wav"), self.path("b.wav"), self.path("c.wav")
        make_wav(a, float_samples=tone())
        make_wav(b, float_samples=tone(), scene="99")  # same audio, other metadata
        make_wav(c, float_samples=tone(channels=1))
        self.assertEqual(duplicates.sample_hash(a), duplicates.sample_hash(b))
        self.assertTrue(duplicates.audio_identical(a, b))
        self.assertTrue(duplicates.channels_contained(c, a))  # the mono file is channel 1 of the stereo one


class FloatEngineTest(unittest.TestCase):
    def test_engine_reads_float(self):
        from sound_file_manager.audio_engine import AudioEngine, _pread_all
        from sound_file_manager.mixer import MixerState

        from . import qt_app
        qt_app()
        with tempfile.TemporaryDirectory() as tmp:
            path = compat.join(tmp, "e.wav")
            samples = tone(peak=2.0)  # peaks above 0 dBFS
            make_wav(path, float_samples=samples, extensible=True)
            engine = AudioEngine()
            try:
                engine.open(path)
                self.assertTrue(engine._float)
                self.assertEqual((engine.channels, engine.frames), (2, 4800))
                raw = _pread_all(engine._fd, 100 * engine._align, engine._offset)
                decoded = waveform.decode(raw, engine._bits, engine.channels, engine._float)
                np.testing.assert_allclose(decoded, samples[:100], rtol=1e-6)
                engine.mixer = MixerState.for_tracks(["A", "B"])
                mixed = np.frombuffer(engine._mix(decoded, 0), np.float32)
                self.assertLessEqual(float(np.abs(mixed).max()), 1.0)  # the output never exceeds full scale
                self.assertGreater(float(engine._meters[-1][2][0]), 1.0)  # but the master meter shows the over
            finally:
                engine.shutdown()
                engine.deleteLater()
