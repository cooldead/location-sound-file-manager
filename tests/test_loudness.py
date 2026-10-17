"""Loudness measurement (loudness.py) against reference signals, the delivery
targets' verdicts, true-peak limiting, platform presets, and the Loudness
Check window."""
import os
import shutil
import tempfile
import unittest

import numpy as np

from sound_file_manager import catalog, effects, loudness
from . import dispose, qt_app
from .wavmaker import make_wav

_TEMP_DIRS: list[str] = []


def _mkdtemp() -> str:
    path = tempfile.mkdtemp()
    _TEMP_DIRS.append(path)
    return path


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


RATE = 48000


def sine(seconds, hz, dbfs=0.0, phase=0.0, rate=RATE, fade=True):
    t = np.arange(int(seconds * rate)) / rate
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * hz * t + phase)
    if fade:  # an abrupt start overshoots between samples (really), which isn't what these tests are about
        k = int(0.02 * rate)
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
        x[:k] *= ramp
        x[-k:] *= ramp[::-1]
    return x


def measure(x, rate=RATE, channels=1):
    meter = loudness.LoudnessMeter(rate, channels)
    meter.feed(x)
    return meter


class MeterTest(unittest.TestCase):
    def test_true_peak_finds_peaks_between_samples(self):
        # A quarter-rate sine at 45°: every sample is at 0.707, the wave reaches 1.0 in between.
        meter = measure(sine(2, RATE / 4, phase=np.pi / 4))
        self.assertAlmostEqual(loudness.to_db(meter.peak), -3.01, delta=0.01)
        self.assertAlmostEqual(loudness.to_db(meter.true_peak), 0.0, delta=0.02)

    def test_true_peak_accuracy_across_frequencies(self):
        for rate in (44100, 48000, 96000):
            for hz in (997, 6000, 12000, 19000):
                for phase in np.linspace(0, np.pi, 5):
                    tp = loudness.to_db(measure(sine(1, hz, -6.0, phase, rate), rate).true_peak)
                    self.assertAlmostEqual(tp, -6.0, delta=0.05, msg=(rate, hz, phase))

    def test_loudness_range_reference(self):
        # EBU Tech 3342 test case: 20 s at -20 dBFS then 20 s at -30 dBFS (1 kHz): LRA 10 ±1 LU.
        meter = measure(np.concatenate([sine(20, 1000, -20, fade=False), sine(20, 1000, -30, fade=False)]))
        self.assertAlmostEqual(meter.lra(), 10.0, delta=1.0)
        steady = measure(sine(20, 1000, -20))
        self.assertLess(steady.lra(), 0.5)

    def test_momentary_and_short_term_maximum(self):
        x = np.concatenate([sine(10, 997, -30), sine(3.5, 997, -10), sine(10, 997, -30)])
        meter = measure(x)
        self.assertAlmostEqual(meter.max_momentary(), -13.01, delta=0.1)
        self.assertAlmostEqual(meter.max_short_term(), -13.01, delta=0.2)

    def test_two_channels_add_their_power(self):
        x = sine(10, 997, -20)
        self.assertAlmostEqual(measure(np.stack([x, x], 1), channels=2).integrated(), -23.01 + 3.01, delta=0.02)

    def test_gain_to_allows_for_the_gate(self):
        # Two stretches 6 LU apart, the quieter just under the -70 gate: raising it lets those blocks in.
        x = np.concatenate([sine(10, 997, -62), sine(30, 997, -68)])
        meter = measure(x)
        gain = meter.gain_to(-23.0)
        self.assertAlmostEqual(meter.integrated(gain), -23.0, delta=0.002)
        self.assertNotAlmostEqual(meter.integrated() + gain, -23.0, delta=0.002)
        self.assertAlmostEqual(measure(x * 10 ** (gain / 20)).integrated(), -23.0, delta=0.01)

    def test_silence(self):
        meter = measure(np.zeros(RATE * 2))
        self.assertIsNone(meter.integrated())
        self.assertIsNone(meter.lra())


def _m(integrated, tp=-6.0):
    return loudness.Measurement("a.wav", "BOOM", integrated, tp, tp, 5.0, None, None, 10.0)


class CheckTest(unittest.TestCase):
    def test_netflix(self):
        netflix = loudness.BY_KEY["netflix"]
        verdict = loudness.check(_m(-26.0), netflix)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.lines[0], "Within the target.")
        self.assertIn("dialogue-gated", verdict.lines[-1])
        verdict = loudness.check(_m(-23.5), netflix)
        self.assertFalse(verdict.ok)
        self.assertIn("3.5 LU too loud", verdict.lines[0])
        verdict = loudness.check(_m(-27.0, tp=-1.5), netflix)
        self.assertFalse(verdict.ok)
        self.assertIn("0.5 dB over", verdict.lines[0])

    def test_edges_of_the_tolerance_pass(self):
        ebu = loudness.BY_KEY["ebu"]
        self.assertTrue(loudness.check(_m(-23.5), ebu).ok)
        self.assertFalse(loudness.check(_m(-23.6), ebu).ok)

    def test_youtube_only_turns_down(self):
        youtube = loudness.BY_KEY["youtube"]
        loud = loudness.check(_m(-10.0, tp=-1.5), youtube)
        self.assertTrue(loud.ok)
        self.assertIn("turn it down by that much", loud.lines[0])
        quiet = loudness.check(_m(-20.0), youtube)
        self.assertTrue(quiet.ok)
        self.assertIn("quieter", quiet.lines[0])

    def test_too_quiet_to_measure(self):
        self.assertFalse(loudness.check(_m(None), loudness.BY_KEY["ebu"]).ok)

    def test_every_target_is_complete(self):
        for target in loudness.TARGETS:
            self.assertTrue(target.guide and target.source and target.name, target.key)
            self.assertLess(target.true_peak, 0)
            self.assertIn(target.gating, ("dialogue", "programme"))


class Reader:
    def __init__(self, x):
        self.x, self.frames = np.asarray(x, np.float32), len(x)

    def __call__(self, a, b):
        out = np.zeros((b - a, 1), np.float32)
        lo, hi = max(a, 0), min(b, self.frames)
        if hi > lo:
            out[lo - a:hi - a, 0] = self.x[lo:hi]
        return out


def run(chain, x):
    engine = effects.Engine(chain, RATE)
    reader = Reader(x)
    if engine.needs_measuring():
        engine.prepare(reader, [0])
    return engine.process(reader, 0, len(x), 0).astype(np.float64), engine


def speechlike(seconds, seed=0):
    """Noise bursts with pauses and a hot high-frequency tone: loud peaks between samples."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    envelope = (np.sin(2 * np.pi * 0.7 * t) > -0.3) * (0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3 * t)))
    return np.clip(rng.normal(0, 0.15, n) * envelope + 0.3 * np.sin(2 * np.pi * 11000 * t + 0.7) * envelope, -1, 1)


class DeliveryTest(unittest.TestCase):
    def test_true_peak_limiter(self):
        x = speechlike(6)
        y, _ = run([effects.Effect("limiter", {"ceiling": -2.3, "true_peak": True})], x)
        self.assertLessEqual(loudness.to_db(measure(y).true_peak), -2.3 + 0.02)
        y, _ = run([effects.Effect("limiter", {"ceiling": -2.3, "true_peak": False})], x)
        self.assertLessEqual(loudness.to_db(measure(y).peak), -2.3 + 1e-6)

    def test_true_peak_limiter_keeps_time(self):
        x = np.zeros(RATE)
        x[30000] = 0.01
        y, _ = run([effects.Effect("limiter", {"true_peak": True})], x)
        self.assertEqual(int(np.argmax(np.abs(y))), 30000)

    def test_platform_sets_target_and_ceiling(self):
        effect = effects.Effect("loudness", {"platform": "Netflix", "target": -10.0})
        self.assertEqual((effect.params["target"], effect.params["ceiling"]), (-27.0, -2.0))
        self.assertIn("Netflix", effect.describe())

    def test_every_delivery_preset_passes_its_own_check(self):
        x = speechlike(20, seed=1)
        for target in loudness.TARGETS:
            y, _ = run(effects.delivery_chain(target), x)
            m = loudness.from_meter(measure(y), "x.wav", "track", 20.0)
            verdict = loudness.check(m, target)
            self.assertTrue(verdict.ok, (target.name, verdict.lines))
            self.assertAlmostEqual(m.integrated, target.integrated, delta=1.0, msg=target.name)
            self.assertIn(f"Delivery: {target.name}", " ".join(effects.BUILTIN_PRESETS))

    def test_loudness_holds_back_for_true_peaks(self):
        # Sample peaks 3 dB under the true peak: going by them would end 3 dB over the ceiling.
        x = sine(10, RATE / 4, -30, phase=np.pi / 4)
        y, engine = run([effects.Effect("loudness", {"target": -2.0, "ceiling": -6.0, "max_gain": 60})], x)
        self.assertAlmostEqual(loudness.to_db(measure(y).true_peak), -6.0, delta=0.05)
        self.assertIn("true peaks", engine.report()[0])


class MeasureFileTest(unittest.TestCase):
    def test_each_track_and_together(self):
        folder = _mkdtemp()
        boom = sine(10, 997, -20)
        lav = sine(10, 997, -30)
        path = os.path.join(folder, "1T01.wav")
        make_wav(path, filename="1T01.wav", float_samples=np.stack([boom, lav], 1).astype(np.float32))
        each = loudness.measure_file(path, [0, 1], ["BOOM", "LAV"], together=False)
        self.assertEqual([m.what for m in each], ["BOOM", "LAV"])
        self.assertAlmostEqual(each[0].integrated, -23.01, delta=0.05)
        self.assertAlmostEqual(each[1].integrated, -33.01, delta=0.05)
        together = loudness.measure_file(path, [0, 1], ["BOOM", "LAV"], together=True)
        self.assertEqual(len(together), 1)
        expected = -0.691 + 10 * np.log10(10 ** ((-23.01 + 0.691) / 10) + 10 ** ((-33.01 + 0.691) / 10))
        self.assertAlmostEqual(together[0].integrated, expected, delta=0.05)


class FixTest(unittest.TestCase):
    def test_copy_suffix(self):
        names = {t.key: loudness.copy_suffix(t) for t in loudness.TARGETS}
        self.assertEqual(names["netflix"], "_Netflix")
        self.assertEqual(names["appletv"], "_AppleTV")
        self.assertEqual(names["ebu"], "_EBUR128")
        self.assertEqual(names["spotify"], "_Spotify")
        self.assertEqual(len(set(names.values())), len(names))

    def test_tracks_together_get_one_gain(self):
        from sound_file_manager import processing
        folder = _mkdtemp()
        src = os.path.join(folder, "1T04.wav")
        make_wav(src, filename="1T04.wav",
                 float_samples=np.stack([sine(10, 997, -20), sine(10, 997, -30)], 1).astype(np.float32))
        netflix = loudness.BY_KEY["netflix"]
        gain = loudness.programme_gain(src, [0, 1], netflix.integrated)
        chain = effects.delivery_chain(netflix, gain)
        self.assertEqual([e.kind for e in chain], ["gain", "limiter"])
        dst = processing.output_path(src, "processed", loudness.copy_suffix(netflix))
        self.assertTrue(dst.endswith("Processed/1T04_Netflix.wav"))
        processing.process_file(src, dst, None, processing.Settings(mode="effects", channels=(0, 1)),
                                engine=effects.Engine(chain, RATE))
        together = loudness.measure_file(dst, [0, 1], ["BOOM", "LAV"], together=True)[0]
        self.assertTrue(loudness.check(together, netflix).ok)
        self.assertAlmostEqual(together.integrated, -27.0, delta=0.05)
        each = loudness.measure_file(dst, [0, 1], ["BOOM", "LAV"], together=False)
        self.assertAlmostEqual(each[0].integrated - each[1].integrated, 10.0, delta=0.05)  # the mix is kept
        self.assertIsNone(loudness.programme_gain(self._silent(folder), [0], -27.0))

    def _silent(self, folder):
        path = os.path.join(folder, "quiet.wav")
        make_wav(path, filename="quiet.wav", float_samples=np.zeros((RATE, 1), np.float32))
        return path


class LoudnessDialogTest(unittest.TestCase):
    def test_measure_and_check(self):
        qt_app()
        from PySide6.QtCore import QSettings
        from sound_file_manager import loudness_dialog
        folder = _mkdtemp().replace(os.sep, "/")
        path = f"{folder}/1T02.wav"
        make_wav(path, filename="1T02.wav",
                 float_samples=np.stack([sine(5, 997, -24), sine(5, 997, -10)], 1).astype(np.float32))
        qs = QSettings(f"{folder}/settings.ini", QSettings.Format.IniFormat)
        dialog = dispose(self, loudness_dialog.LoudnessCheckDialog([catalog.read_recording(path)], qs))
        self.assertEqual(dialog.target().key, "netflix")
        self.assertIn("-27", dialog.guide.toPlainText())
        # Measure without the progress window (run_job needs an event loop).
        dialog.results = [m for rec in dialog.recs for m in loudness.measure_file(
            rec.path, dialog.channels_for(rec), ["A", "B"], False)]
        dialog._fill()
        self.assertEqual(dialog.table.topLevelItemCount(), 2)
        self.assertTrue(dialog.table.topLevelItem(0).text(6).startswith("✓"))
        self.assertTrue(dialog.table.topLevelItem(1).text(6).startswith("✗"))
        dialog.platform.setCurrentIndex(dialog.platform.findData("youtube"))
        self.assertTrue(dialog.table.topLevelItem(1).text(6).startswith("✓"))
        self.assertEqual(qs.value("loudness_platform"), "youtube")
        dialog.copy_results()
        self.assertIn("1T02.wav", qt_app().clipboard().text())

    def test_fix_takes_only_the_files_that_failed(self):
        qt_app()
        from PySide6.QtCore import QSettings
        from sound_file_manager import loudness_dialog
        folder = _mkdtemp().replace(os.sep, "/")
        recs = []
        for name, level in (("1T05.wav", -24), ("1T06.wav", -10)):
            make_wav(f"{folder}/{name}", filename=name, float_samples=sine(5, 997, level)[:, None].astype(np.float32))
            recs.append(catalog.read_recording(f"{folder}/{name}"))
        qs = QSettings(f"{folder}/settings.ini", QSettings.Format.IniFormat)
        dialog = dispose(self, loudness_dialog.LoudnessCheckDialog(recs, qs, fix=lambda *a: None))
        self.assertTrue(dialog.fix_button.isVisibleTo(dialog))
        jobs, passing, measured = dialog.to_fix()
        self.assertEqual((len(jobs), passing, measured), (2, 0, False))   # not measured: every file
        dialog.measured = {rec.path: loudness.measure_file(rec.path, [0], ["A"], False) for rec in recs}
        dialog.measured_with = (frozenset(dialog.picked_names()), False)
        jobs, passing, measured = dialog.to_fix()
        self.assertEqual(([rec.name for rec, _ in jobs], passing, measured), (["1T06.wav"], 1, True))
        dialog.together.setChecked(True)                                   # measured another way: every file
        self.assertEqual(len(dialog.to_fix()[0]), 2)
        no_fix = dispose(self, loudness_dialog.LoudnessCheckDialog(recs, qs))
        self.assertFalse(no_fix.fix_button.isVisibleTo(no_fix))

    def test_effects_opens_with_the_platform_preset(self):
        qt_app()
        from PySide6.QtCore import QSettings
        from sound_file_manager.effects_dialog import EffectsDialog
        folder = _mkdtemp().replace(os.sep, "/")
        path = f"{folder}/1T03.wav"
        make_wav(path, filename="1T03.wav")
        qs = QSettings(f"{folder}/settings.ini", QSettings.Format.IniFormat)
        netflix = loudness.BY_KEY["netflix"]
        dialog = dispose(self, EffectsDialog([catalog.read_recording(path)], qs, data_dir=folder,
                                             chain=effects.delivery_chain(netflix)))
        self.assertTrue(dialog.presets.currentText().startswith("Delivery: Netflix"))
        dialog._set(0, "target", -20.0)  # own numbers: no longer Netflix's
        self.assertEqual(dialog.chain[0].params["platform"], "Custom")
        dialog._set(0, "platform", "EBU R128 (European broadcast)")
        self.assertEqual((dialog.chain[0].params["target"], dialog.chain[0].params["ceiling"]), (-23.0, -1.0))


if __name__ == "__main__":
    unittest.main()
