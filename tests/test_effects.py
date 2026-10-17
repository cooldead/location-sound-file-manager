"""The effects rack (effects.py): each effect against known values, the
engine's blocks and look-ahead, copies through processing.process_file,
presets and Easy Effects imports; and the Effects window's choices."""
import json
import os
import shutil
import tempfile
import unittest

import numpy as np

from sound_file_manager import bwf, catalog, effects, processing, waveform
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


def tone(seconds, hz, level, rate=RATE):
    return level * np.sin(2 * np.pi * hz * np.arange(int(seconds * rate)) / rate)


def rms_db(x):
    return 10 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-30)


class Reader:
    """Like processing._Reader, for a mono array: frames [a, b), zeros outside."""

    def __init__(self, x):
        self.x, self.frames = np.asarray(x, np.float32), len(x)

    def __call__(self, a, b):
        out = np.zeros((b - a, 1), np.float32)
        lo, hi = max(a, 0), min(b, self.frames)
        if hi > lo:
            out[lo - a:hi - a, 0] = self.x[lo:hi]
        return out


def run(chain, x, step=None, rate=RATE):
    engine = effects.Engine(chain, rate)
    reader = Reader(x)
    if engine.needs_measuring():
        engine.prepare(reader, [0])
    step = step or engine.block_frames
    out = np.concatenate([engine.process(reader, a, min(len(x), a + step), 0) for a in range(0, len(x), step)])
    return out.astype(np.float64), engine


class FilterTest(unittest.TestCase):
    def test_butterworth_high_pass_slope(self):
        hp = effects.Effect("filter", {"kind": "High-pass", "freq": 100.0, "slope": "24 dB/oct"})
        for hz, expected in ((50, -24.1), (100, -3.01), (1000, 0.0)):
            x = tone(3, hz, 0.5)
            y, _ = run([hp], x)
            self.assertAlmostEqual(rms_db(y[RATE:]) - rms_db(x[RATE:]), expected, delta=0.1, msg=hz)

    def test_low_pass_and_response_graph_agree(self):
        lp = effects.Effect("filter", {"kind": "Low-pass", "freq": 2000.0, "slope": "12 dB/oct"})
        x = tone(2, 4000, 0.5)
        y, _ = run([lp], x)
        graph = effects.response_db(lp, RATE, np.array([4000.0]))[0]
        self.assertAlmostEqual(rms_db(y[RATE:]) - rms_db(x[RATE:]), graph, delta=0.05)

    def test_equalizer_bell_and_notch(self):
        eq = effects.Effect("equalizer", {"bands": [{"type": "Bell", "freq": 1000, "gain": 6, "q": 1},
                                                    {"type": "Notch", "freq": 60, "gain": 0, "q": 10}]})
        for hz, expected in ((1000, 6.0), (60, -60.0)):
            x = tone(3, hz, 0.1)
            y, _ = run([eq], x)
            change = rms_db(y[RATE:]) - rms_db(x[RATE:])
            if expected > 0:
                self.assertAlmostEqual(change, expected, delta=0.05)
            else:
                self.assertLess(change, -40)

    def test_flat_equalizer_changes_nothing(self):
        x = np.random.default_rng(1).normal(0, 0.1, RATE)
        y, _ = run([effects.Effect("equalizer", {"bands": [{"type": "Bell", "freq": 500, "gain": 0, "q": 1}]})], x)
        np.testing.assert_allclose(y, x.astype(np.float32), atol=1e-7)


class DynamicsTest(unittest.TestCase):
    def test_compressor_static_curve(self):
        # A sine at 0.5 reads -9 dBFS RMS: 21 dB over -30, at 4:1 that is 15.75 dB down.
        comp = effects.Effect("compressor", {"threshold": -30, "ratio": 4, "knee": 0, "detection": "RMS"})
        x = tone(3, 440, 0.5)
        y, engine = run([comp], x)
        self.assertAlmostEqual(rms_db(y[RATE:]) - rms_db(x[RATE:]), -15.75, delta=0.1)
        self.assertIn("down by up to", engine.report()[0])

    def test_below_threshold_is_untouched(self):
        comp = effects.Effect("compressor", {"threshold": -10, "ratio": 4, "knee": 0})
        x = tone(2, 440, 0.01)
        y, _ = run([comp], x)
        np.testing.assert_allclose(y, x.astype(np.float32), atol=1e-6)

    def test_gate_lowers_the_pauses_only(self):
        gate = effects.Effect("gate", {"threshold": -40, "range": 30, "release": 50, "hold": 0})
        t = np.arange(4 * RATE) / RATE
        loud = (t % 2) < 1
        x = np.where(loud, 0.3, 0.001) * np.sin(2 * np.pi * 300 * t)
        y, _ = run([gate], x)
        speech = loud & ((t % 2) > 0.1)
        pause = ~loud & ((t % 2) > 1.5)
        self.assertAlmostEqual(rms_db(y[speech]) - rms_db(x[speech]), 0.0, delta=0.1)
        self.assertAlmostEqual(rms_db(y[pause]) - rms_db(x[pause]), -30.0, delta=0.5)

    def test_expander_ratio_and_range(self):
        exp = effects.Effect("expander", {"threshold": -40, "ratio": 2, "range": 15, "knee": 0, "detection": "RMS"})
        x = tone(2, 300, 10 ** (-45 / 20) * np.sqrt(2))  # -45 dBFS RMS: 5 dB under, 2:1 → 5 dB down
        y, _ = run([exp], x)
        self.assertAlmostEqual(rms_db(y[RATE:]) - rms_db(x[RATE:]), -5.0, delta=0.15)
        x = tone(2, 300, 10 ** (-80 / 20))
        y, _ = run([exp], x)
        self.assertAlmostEqual(rms_db(y[RATE:]) - rms_db(x[RATE:]), -15.0, delta=0.15)

    def test_limiter_keeps_every_sample_below_the_ceiling(self):
        x = np.random.default_rng(2).normal(0, 0.5, 3 * RATE)
        y, engine = run([effects.Effect("limiter", {"ceiling": -6})], x)
        self.assertLessEqual(np.abs(y).max(), 10 ** (-6 / 20) * (1 + 1e-6))
        self.assertIn("down by up to", engine.report()[0])

    def test_deesser_split_lowers_highs_and_keeps_lows(self):
        d = effects.Effect("deesser", {"freq": 5000, "threshold": -40, "ratio": 10, "max_reduction": 12})
        low = tone(2, 300, 0.3)
        high = tone(2, 8000, 0.3)
        y, _ = run([d], low + high)
        spectrum = np.abs(np.fft.rfft(y[RATE:2 * RATE]))
        reference = np.abs(np.fft.rfft((low + high)[RATE:2 * RATE]))
        self.assertAlmostEqual(20 * np.log10(spectrum[300] / reference[300]), 0.0, delta=0.2)
        self.assertLess(20 * np.log10(spectrum[8000] / reference[8000]), -6)


class EngineTest(unittest.TestCase):
    def test_look_ahead_is_taken_off(self):
        x = np.zeros(2 * RATE)
        x[70000] = 0.01
        chain = [effects.Effect("deesser", {}), effects.Effect("limiter", {"lookahead": 10})]
        y, _ = run(chain, x)
        self.assertEqual(int(np.argmax(np.abs(y))), 70000)

    def test_blocks_give_the_same_result_as_one_piece(self):
        chain = [effects.Effect("filter", {}), effects.Effect("deesser", {}), effects.Effect("compressor", {}),
                 effects.Effect("gate", {}), effects.Effect("limiter", {})]
        x = np.random.default_rng(3).normal(0, 0.2, 3 * RATE)
        whole, _ = run(chain, x, step=len(x))
        parts, _ = run(chain, x, step=12345)
        np.testing.assert_array_equal(whole, parts)

    def test_bypassed_effects_are_skipped(self):
        x = tone(1, 50, 0.3)
        y, engine = run([effects.Effect("filter", {"freq": 1000}, bypass=True),
                         effects.Effect("gain", {"gain": -6})], x)
        np.testing.assert_allclose(y, x * 10 ** (-6 / 20), atol=1e-6)
        self.assertEqual([e.kind for e in engine.chain], ["gain"])

    def test_gain_delay_and_polarity(self):
        x = np.zeros(RATE)
        x[1000] = 0.5
        y, _ = run([effects.Effect("gain", {"gain": 6.0206, "invert": True, "delay": 1.0})], x)
        self.assertEqual(int(np.argmax(np.abs(y))), 1048)
        self.assertAlmostEqual(y[1048], -1.0, places=4)


class LoudnessTest(unittest.TestCase):
    def test_reference_tone(self):
        meter = effects.LoudnessMeter(RATE)
        meter.feed(tone(10, 997, 1.0))  # BS.1770: a full-scale 997 Hz sine is -3.01 LUFS
        self.assertAlmostEqual(meter.integrated(), -3.01, delta=0.01)
        meter = effects.LoudnessMeter(44100)
        meter.feed(tone(10, 997, 1.0, rate=44100))
        self.assertAlmostEqual(meter.integrated(), -3.01, delta=0.01)

    def test_gates_ignore_silence(self):
        meter = effects.LoudnessMeter(RATE)
        meter.feed(np.concatenate([tone(5, 997, 0.1), np.zeros(20 * RATE)]))
        # Without the gates the 20 s of silence would pull it to about -30; the
        # blocks across the tone's end still count (correctly), hence -23.14.
        self.assertAlmostEqual(meter.integrated(), -23.01, delta=0.2)

    def test_normalises_to_the_target(self):
        x = tone(10, 997, 0.05)
        y, engine = run([effects.Effect("loudness", {"target": -23})], x)
        meter = effects.LoudnessMeter(RATE)
        meter.feed(y)
        self.assertAlmostEqual(meter.integrated(), -23.0, delta=0.05)
        self.assertIn("→ -23.0 LUFS", engine.report()[0])

    def test_peaks_hold_it_back_and_it_says_so(self):
        x = tone(10, 997, 0.05)
        x[RATE] = 0.9  # one peak at -0.9 dBFS leaves no room to raise
        y, engine = run([effects.Effect("loudness", {"target": -10, "ceiling": -1})], x)
        self.assertLessEqual(np.abs(y).max(), 10 ** (-1 / 20) * 1.0001)
        self.assertIn("target not reached", engine.report()[0])

    def test_silence_is_left_alone(self):
        y, engine = run([effects.Effect("loudness", {})], np.zeros(RATE))
        self.assertFalse(y.any())
        self.assertIn("too quiet", engine.report()[0])


class ChainTest(unittest.TestCase):
    def test_json_round_trip_and_clamping(self):
        chain = [effects.Effect("compressor", {"ratio": 500, "threshold": "x"}), effects.Effect("limiter", {})]
        back = effects.chain_from_json(effects.chain_to_json(chain))
        self.assertEqual([e.to_json() for e in back], [e.to_json() for e in chain])
        self.assertEqual(back[0].params["ratio"], 20.0)
        self.assertEqual(back[0].params["threshold"], -20.0)
        self.assertEqual(effects.chain_from_json('[{"type": "nonsense"}, {"type": "gain"}]')[0].kind, "gain")
        self.assertEqual(effects.chain_from_json("not json"), [])

    def test_problems(self):
        self.assertTrue(effects.chain_problems([]))
        self.assertTrue(effects.chain_problems([effects.Effect("gain", bypass=True)]))
        self.assertTrue(effects.chain_problems([effects.Effect("loudness"), effects.Effect("loudness")]))
        self.assertFalse(effects.chain_problems([effects.Effect("loudness"), effects.Effect("limiter")]))

    def test_presets(self):
        data = _mkdtemp()
        self.assertEqual(effects.list_presets(data), [])
        chain = [effects.Effect("filter", {"freq": 120.0})]
        effects.save_preset(data, "My: lav", chain)
        self.assertEqual(effects.list_presets(data), ["My_ lav"])
        self.assertEqual(effects.load_preset(data, "My_ lav")[0].params["freq"], 120.0)
        effects.delete_preset(data, "My_ lav")
        self.assertEqual(effects.list_presets(data), [])
        for name in effects.BUILTIN_PRESETS:
            self.assertTrue(effects.load_preset(data, name))
        with self.assertRaises(ValueError):
            effects.save_preset(data, "  ", chain)


EE_PRESET = {"output": {
    "blocklist": [],
    "plugins_order": ["filter#0", "equalizer#0", "gate#0", "compressor#0", "deesser#0", "limiter#0", "autogain#0",
                      "rnnoise#0", "bass_enhancer#0"],
    "filter#0": {"bypass": False, "type": "High-pass", "frequency": 90.0, "slope": "x2", "quality": 0.7,
                 "input-gain": 0.0, "output-gain": 0.0},
    "equalizer#0": {"bypass": False, "num-bands": 2, "split-channels": False, "input-gain": -3.0, "output-gain": 0.0,
                    "left": {"band0": {"type": "Bell", "frequency": 3000.0, "gain": 2.5, "q": 1.2, "mute": False,
                                       "slope": "x1"},
                             "band1": {"type": "Lo-shelf", "frequency": 120.0, "gain": -4.0, "q": 0.7,
                                       "mute": False, "slope": "x1"}}},
    "gate#0": {"bypass": True, "curve-threshold": -45.0, "reduction": -18.0, "attack": 2.0, "release": 120.0,
               "hysteresis": True, "hysteresis-zone": -4.0},
    "compressor#0": {"mode": "Downward", "threshold": -18.0, "ratio": 3.0, "knee": -6.0, "attack": 15.0,
                     "release": 200.0, "makeup": 2.0, "sidechain": {"mode": "RMS"}},
    "deesser#0": {"f1-freq": 6500.0, "threshold": -25.0, "ratio": 5.0, "mode": "Split"},
    "limiter#0": {"threshold": -1.5, "lookahead": 5.0, "release": 10.0},
    "autogain#0": {"target": -20.0},
    "rnnoise#0": {},
    "bass_enhancer#0": {},
}}


class EasyEffectsImportTest(unittest.TestCase):
    def test_maps_what_exists_and_says_what_doesnt(self):
        chain, notes = effects.import_easyeffects(EE_PRESET)
        self.assertEqual([e.kind for e in chain], ["filter", "gain", "equalizer", "gate", "compressor", "deesser",
                                                   "limiter", "loudness"])
        self.assertEqual(chain[0].params["slope"], "24 dB/oct")
        self.assertEqual(chain[0].params["freq"], 90.0)
        self.assertEqual(chain[1].params["gain"], -3.0)  # the equalizer's input gain
        self.assertEqual([b["type"] for b in chain[2].params["bands"]], ["Bell", "Low shelf"])
        self.assertTrue(chain[3].bypass)
        self.assertEqual(chain[3].params["range"], 18.0)
        self.assertEqual(chain[4].params["knee"], 6.0)
        self.assertEqual(chain[6].params["ceiling"], -1.5)
        self.assertEqual(chain[7].params["target"], -20.0)
        text = " ".join(notes)
        self.assertIn("noise reduction (use Reduce Noise)", text)
        self.assertIn("bass enhancer", text)

    def test_not_a_preset(self):
        with self.assertRaises(ValueError):
            effects.import_easyeffects({"something": 1})


class CopyTest(unittest.TestCase):
    def setUp(self):
        self.dir = _mkdtemp().replace(os.sep, "/")

    def _audio(self, path):
        info = bwf.read_info(path)
        with open(path, "rb") as f:
            layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
            data = layout.first(b"data")
            f.seek(data.data_offset)
            raw = f.read(data.size)
        return waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)

    def test_copy_with_effects_and_untouched_tracks(self):
        frames = 3 * RATE
        rng = np.random.default_rng(4)
        samples = np.stack([rng.normal(0, 0.05, frames), rng.normal(0, 0.05, frames)], axis=1).astype(np.float32)
        src = f"{self.dir}/10T03_ISO.wav"
        make_wav(src, filename="10T03_ISO.wav", float_samples=samples)
        with open(src, "rb") as f:
            before = f.read()
        chain = [effects.Effect("filter", {"freq": 200.0}), effects.Effect("loudness", {"target": -20}),
                 effects.Effect("limiter", {"ceiling": -1})]
        settings = processing.Settings(mode="effects", label=effects.describe_chain(chain), channels=(1,))
        dst = processing.output_path(src, suffix=effects.DEFAULT_SUFFIX)
        progress = []
        processing.process_file(src, dst, None, settings, engine=effects.Engine(chain, RATE),
                                progress=lambda d, t: progress.append((d, t)))
        with open(src, "rb") as f:
            self.assertEqual(f.read(), before, "the original is untouched")
        self.assertTrue(dst.endswith("Processed/10T03_ISO_FX.wav"))
        rec, orig = catalog.read_recording(dst), catalog.read_recording(src)
        for key in ("scene", "take", "time_reference", "frames", "channels", "sample_rate", "bits"):
            self.assertEqual(getattr(rec, key), getattr(orig, key), key)
        out = self._audio(dst)
        np.testing.assert_array_equal(out[:, 0], samples[:, 0])
        expected, _ = run(chain, samples[:, 1])
        np.testing.assert_allclose(out[:, 1], expected, atol=1e-6)
        meter = effects.LoudnessMeter(RATE)
        meter.feed(out[:, 1].astype(np.float64))
        self.assertAlmostEqual(meter.integrated(), -20.0, delta=0.2)
        self.assertEqual(progress[-1], (frames, frames))
        self.assertEqual([d for d, _ in progress], sorted(d for d, _ in progress), "the bar only moves forward")
        with open(dst, "rb") as f:
            self.assertIn(b"effects: High-pass 200 Hz", f.read(4096 * 4))

    def test_preview_matches_the_copy_after_the_preroll(self):
        frames = 6 * RATE
        samples = np.random.default_rng(5).normal(0, 0.1, (frames, 1)).astype(np.float32)
        src = f"{self.dir}/1T01.wav"
        make_wav(src, filename="1T01.wav", float_samples=samples)
        chain = [effects.Effect("filter", {}), effects.Effect("compressor", {"release": 50.0})]
        settings = processing.Settings(mode="effects", channels=(0,))
        dst = processing.output_path(src)
        processing.process_file(src, dst, None, settings, engine=effects.Engine(chain, RATE))
        original, processed, _ = processing.preview(src, 3 * RATE, 4 * RATE, None, settings,
                                                    effects.Engine(chain, RATE))
        np.testing.assert_array_equal(original, samples[3 * RATE:4 * RATE])
        np.testing.assert_allclose(processed, self._audio(dst)[3 * RATE:4 * RATE], atol=1e-4)

    def test_cancel_leaves_nothing(self):
        src = f"{self.dir}/1T02.wav"
        make_wav(src, filename="1T02.wav", frames=RATE)
        settings = processing.Settings(mode="effects", channels=(0, 1))
        dst = processing.output_path(src, "beside", "_FX")
        with self.assertRaises(InterruptedError):
            processing.process_file(src, dst, None, settings, cancelled=lambda: True,
                                    engine=effects.Engine([effects.Effect("gain")], RATE))
        self.assertFalse(os.path.exists(dst))


class EffectsDialogTest(unittest.TestCase):
    def setUp(self):
        qt_app()
        from PySide6.QtCore import QSettings
        self.dir = _mkdtemp().replace(os.sep, "/")
        self.qs = QSettings(f"{self.dir}/settings.ini", QSettings.Format.IniFormat)

    def _dialog(self, recs):
        from sound_file_manager.effects_dialog import EffectsDialog
        return dispose(self, EffectsDialog(recs, self.qs, data_dir=self.dir))

    def _rec(self, name):
        path = f"{self.dir}/{name}"
        make_wav(path, filename=name)
        return catalog.read_recording(path)

    def test_chain_editing_and_choices(self):
        from PySide6.QtCore import Qt
        rec = self._rec("10T03_ISO.wav")
        dialog = self._dialog([rec])
        self.assertEqual([e.kind for e in dialog.chain], ["filter"])  # the default preset
        self.assertTrue(dialog.ok.isEnabled())
        dialog._add("compressor")
        dialog._add("loudness")
        dialog._add("limiter")
        dialog.list.setCurrentRow(3)
        dialog._add("loudness")
        self.assertFalse(dialog.ok.isEnabled(), "two loudness effects")
        dialog.list.setCurrentRow(dialog.chain.index(next(e for e in dialog.chain[3:] if e.kind == "loudness")))
        dialog._remove()
        self.assertEqual([e.kind for e in dialog.chain], ["filter", "compressor", "loudness", "limiter"])
        dialog.list.setCurrentRow(1)
        dialog._move(1)
        self.assertEqual([e.kind for e in dialog.chain], ["filter", "loudness", "compressor", "limiter"])
        dialog.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        self.assertTrue(dialog.chain[0].bypass)
        dialog.list.setCurrentRow(2)
        dialog._set(2, "ratio", 6.0)
        self.assertIn("6:1", dialog.list.item(2).text())
        settings = dialog.settings_for(rec)
        self.assertEqual(settings.mode, "effects")
        self.assertEqual(settings.channels, (0, 1))
        self.assertNotIn("High-pass", settings.label)
        dialog.suffix.setText("")
        self.assertFalse(dialog.ok.isEnabled())
        dialog.suffix.setText("_FX")
        dialog._accept()
        saved = effects.chain_from_json(self.qs.value("effects_chain"))
        self.assertEqual([e.kind for e in saved], ["filter", "loudness", "compressor", "limiter"])

    def test_equalizer_bands_editor(self):
        dialog = self._dialog([self._rec("1T01.wav")])
        dialog._replace_chain([effects.Effect("equalizer")])
        bands = dialog.chain[0].params["bands"]
        dialog._set(0, "bands", bands + [{"type": "Notch", "freq": 60.0, "gain": 0.0, "q": 8.0}])
        dialog._show_selected(0)
        self.assertEqual(len(dialog.chain[0].params["bands"]), 2)
        self.assertIsNotNone(dialog.graph)

    def test_presets_in_the_window(self):
        dialog = self._dialog([self._rec("1T02.wav")])
        effects.save_preset(self.dir, "Mine", [effects.Effect("gain", {"gain": -3})])
        dialog._fill_presets("Mine")
        self.assertEqual(dialog.presets.currentData(), ("mine", "Mine"))
        self.assertTrue(dialog.delete_button.isEnabled())
        dialog._load_preset()
        self.assertEqual([e.kind for e in dialog.chain], ["gain"])
        dialog._fill_presets("Dialogue: clean-up")
        self.assertFalse(dialog.delete_button.isEnabled())
        dialog._load_preset()
        self.assertEqual([e.kind for e in dialog.chain], ["filter", "deesser", "compressor", "limiter"])

    def test_every_effect_panel_builds(self):
        dialog = self._dialog([self._rec("1T03.wav")])
        dialog._replace_chain([effects.Effect(kind) for kind in effects.TYPES])
        for row in range(len(dialog.chain)):
            dialog.list.setCurrentRow(row)
            self.assertIsNotNone(dialog.settings_area.widget())


if __name__ == "__main__":
    unittest.main()
