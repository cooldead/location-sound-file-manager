"""The DeepFilterNet add-on (deepfilter.py), with a stand-in for the program.

The stand-in behaves like deep-filter 0.5.6 as measured: it writes 16-bit,
and with -D drops the last 1440 samples. It halves the audio, and records the
-a value it was given. Set DEEPFILTER_PROGRAM to a real deep-filter to also
run the real-program test."""
import hashlib
import io
import os
import stat
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

from sound_file_manager import bwf, catalog, deepfilter, processing
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


STAND_IN = r'''#!{python}
import sys, os
sys.path.insert(0, {root!r})
import numpy as np
from sound_file_manager import bwf, waveform, splitter, processing
args = sys.argv[1:]
out_dir = args[args.index("-o") + 1]
atten = args[args.index("-a") + 1]
src = args[-1]
if os.environ.get("STAND_IN_SLEEP"):
    import time; time.sleep(30)
info = bwf.read_info(src)
with open(src, "rb") as f:
    layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
    d = layout.first(b"data"); f.seek(d.data_offset); raw = f.read(d.size)
x = waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)
if info.bits != 32:
    sys.exit("TooWide")
y = x[:len(x) - 1440] * 0.5
with open(os.path.join(out_dir, os.path.basename(src)), "wb") as f:
    f.write(splitter._wav_header(1, 1, 48000, 16, len(y), [], []))
    f.write(processing.encode(y, 16, False).tobytes())
with open(os.path.join(out_dir, "atten.txt"), "w") as f:
    f.write(atten)
'''

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stand_in(folder: str) -> str:
    path = os.path.join(folder, "deep-filter")
    with open(path, "w") as f:
        f.write(STAND_IN.format(python=sys.executable, root=ROOT))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def speech_like(frames, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(frames) / 48000
    return (0.2 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t))
            + 0.01 * rng.standard_normal(frames)).astype(np.float32)


@unittest.skipIf(sys.platform == "win32", "the stand-in is a script with a #! line")
class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = _mkdtemp().replace(os.sep, "/")
        self.program = stand_in(self.dir)

    def test_enhance_keeps_length_and_level_through_16_bit(self):
        x = speech_like(48000) * 0.05  # quiet: raised before, lowered after
        y = deepfilter.enhance(x, self.program, 12, _mkdtemp())
        self.assertEqual(len(y), len(x))
        np.testing.assert_allclose(y, x * 0.5, atol=0.05 / 32768 * 4)
        self.assertTrue(np.array_equal(deepfilter.enhance(np.zeros(100, np.float32), self.program, 12,
                                                          _mkdtemp()), np.zeros(100)))

    def test_blocks_line_up_and_fade(self):
        x = speech_like(200000)[:, None]

        def read(a, b):
            out = np.zeros((b - a, 1), np.float32)
            lo, hi = max(a, 0), min(b, len(x))
            out[lo - a:hi - a] = x[lo:hi]
            return out
        engine = deepfilter.Engine(self.program, 20)
        try:
            y = np.concatenate([engine.process(read, a, min(len(x), a + 70000), 0) for a in range(0, len(x), 70000)])
        finally:
            engine.close()
        self.assertFalse(os.path.exists(engine.workdir))
        # The stand-in has no state, so blocks must give one pass's result.
        np.testing.assert_allclose(y, x[:, 0] * 0.5, atol=1e-4)

    def test_process_file_with_the_engine(self):
        src = f"{self.dir}/3T01_ISO.wav"
        make_wav(src, filename="3T01_ISO.wav", float_samples=np.stack([speech_like(100000, 1), speech_like(100000, 2)], 1))
        settings = processing.Settings(mode="deepfilter", reduction_db=15, channels=(1,))
        engine = deepfilter.Engine(self.program, settings.reduction_db)
        dst = processing.output_path(src, "processed", "_DF")
        with mock.patch.object(deepfilter.Engine, "block_frames", 30000):
            processing.process_file(src, dst, None, settings, engine=engine)
        with open(f"{engine.workdir}/out/atten.txt") as f:
            self.assertEqual(f.read(), "15")
        engine.close()
        original, _ = processing.read_frames(src, 0, 100000)
        out, _ = processing.read_frames(dst, 0, 100000)
        np.testing.assert_array_equal(out[:, 0], original[:, 0])
        np.testing.assert_allclose(out[:, 1], original[:, 1] * 0.5, atol=1e-4)
        rec = catalog.read_recording(dst)
        self.assertEqual((rec.scene, rec.take, rec.frames), (catalog.read_recording(src).scene,
                                                             catalog.read_recording(src).take, 100000))
        with open(dst, "rb") as f:
            self.assertIn(b"DeepFilterNet voice cleanup, up to 15 dB", f.read(4096))

    def test_cancel_stops_the_program(self):
        x = speech_like(48000)
        with mock.patch.dict(os.environ, {"STAND_IN_SLEEP": "1"}):
            calls = []

            def cancelled():
                calls.append(1)
                return len(calls) > 3
            with self.assertRaises(InterruptedError):
                deepfilter.enhance(x, self.program, 12, _mkdtemp(), cancelled)

    def test_failures_are_reported(self):
        bad = os.path.join(self.dir, "bad")
        with open(bad, "w") as f:
            f.write("#!/bin/sh\necho 'thread main panicked: TooWide' >&2\nexit 101\n")
        os.chmod(bad, 0o755)
        with self.assertRaises(deepfilter.AddonError) as caught:
            deepfilter.enhance(speech_like(4800), bad, 12, _mkdtemp())
        self.assertIn("TooWide", str(caught.exception))

    def test_only_48_khz_and_only_with_the_add_on(self):
        src = f"{self.dir}/3T02.wav"
        make_wav(src, filename="3T02.wav")
        with self.assertRaises(ValueError):
            processing.process_file(src, processing.output_path(src), None, processing.Settings(mode="deepfilter"))
        self.assertFalse(os.path.exists(processing.output_path(src).rsplit("/", 1)[0] + "/3T02_NR.wav"))


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.dir = _mkdtemp().replace(os.sep, "/")
        self.payload = b"#!/bin/sh\nexit 0\n" * 1000
        digest = hashlib.sha256(self.payload).hexdigest()
        self.patch = mock.patch.object(deepfilter, "asset", lambda: ("deep-filter-test", digest, len(self.payload)))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def opener(self, payload):
        def open_url(url):
            self.url = url
            return io.BytesIO(payload)
        return open_url

    def test_install_checks_the_download(self):
        self.assertIsNone(deepfilter.installed(self.dir))
        seen = []
        path = deepfilter.install(self.dir, progress=lambda d, t: seen.append((d, t)), opener=self.opener(self.payload))
        self.assertEqual(path, deepfilter.installed(self.dir))
        self.assertTrue(self.url.endswith("/v0.5.6/deep-filter-test"))
        self.assertEqual(seen[-1], (len(self.payload), len(self.payload)))
        if sys.platform != "win32":
            self.assertTrue(os.access(path, os.X_OK))
        deepfilter.uninstall(self.dir)
        self.assertIsNone(deepfilter.installed(self.dir))

    def test_a_wrong_download_leaves_nothing(self):
        with self.assertRaises(deepfilter.AddonError):
            deepfilter.install(self.dir, opener=self.opener(self.payload[:-1] + b"X"))
        self.assertIsNone(deepfilter.installed(self.dir))
        self.assertEqual(os.listdir(deepfilter.addon_dir(self.dir)), [])
        with self.assertRaises(InterruptedError):
            deepfilter.install(self.dir, opener=self.opener(self.payload), cancelled=lambda: True)
        self.assertEqual(os.listdir(deepfilter.addon_dir(self.dir)), [])

    def test_every_platform_has_a_pinned_checksum(self):
        for (system, machine), (name, digest, size) in deepfilter.ASSETS.items():
            self.assertIn(deepfilter.VERSION, name)
            self.assertEqual(len(digest), 64)
            self.assertGreater(size, 10_000_000)


@unittest.skipUnless(os.environ.get("DEEPFILTER_PROGRAM"), "set DEEPFILTER_PROGRAM to a real deep-filter")
class RealProgramTest(unittest.TestCase):
    def test_noise_goes_down_and_the_length_is_kept(self):
        rng = np.random.default_rng(3)
        x = speech_like(48000 * 4) + (0.02 * rng.standard_normal(48000 * 4)).astype(np.float32)
        x[:48000] = (0.02 * rng.standard_normal(48000)).astype(np.float32)  # noise only
        y = deepfilter.enhance(x, os.environ["DEEPFILTER_PROGRAM"], 100, _mkdtemp())
        self.assertEqual(len(y), len(x))
        db = lambda v: 10 * np.log10(np.mean(np.square(v, dtype=np.float64)))
        self.assertLess(db(y[:48000]), db(x[:48000]) - 10)


if __name__ == "__main__":
    unittest.main()
