"""Plug-in chains (plugins.py, plugin_host.py, vst2.py).

Most tests use stand-ins. With real plug-ins, set SFM_TEST_VST2 to a VST2
file and/or SFM_TEST_VST3 (plus SFM_PEDALBOARD, an unpacked pedalboard) to a
VST3 bundle of an effect that passes audio unchanged at its default settings
(e.g. DISTRHO's 3 Band EQ from DPF-Plugins)."""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import numpy as np

from sound_file_manager import catalog, plugins, processing
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


class FakePlugin:
    """Delays by `latency` frames and scales by `gain`; state = the gain."""

    def __init__(self, latency=0, gain=1.0):
        self.latency, self.gain, self.memory = latency, gain, np.zeros(latency, np.float32)
        self.closed = False

    name, vendor, channels = "Fake", "Test", (1, 1)

    def set_state(self, state):
        if state:
            self.gain = float(np.frombuffer(state, "<f4")[0])

    def reset(self):
        self.memory[:] = 0

    def process(self, mono):
        joined = np.concatenate([self.memory, np.asarray(mono, np.float32) * self.gain])
        self.memory = joined[len(joined) - self.latency:] if self.latency else self.memory
        return joined[:len(mono)]

    def close(self):
        self.closed = True


def entry(gain=1.0, **kwargs):
    state = base64.b64encode(np.array([gain], "<f4").tobytes()).decode()
    return plugins.ChainEntry("vst2", "/fake.so", "Fake", state, **kwargs)


class ChainEngineTest(unittest.TestCase):
    def run_chain(self, chain, latencies, x, block):
        made = iter([FakePlugin(latency=l) for l in latencies * x.shape[1]])
        with mock.patch.object(plugins, "open_plugin", lambda kind, path, rate=RATE: next(made)):
            engine = plugins.ChainEngine(chain, RATE)

            def read(a, b):
                out = np.zeros((b - a, x.shape[1]), np.float32)
                lo, hi = max(a, 0), min(b, len(x))
                out[lo - a:hi - a] = x[lo:hi]
                return out
            pieces = []
            for a in range(0, len(x), block):
                b = min(len(x), a + block)
                pieces.append(np.stack([engine.process(read, a, b, c) for c in range(x.shape[1])], axis=1))
            engine.close()
        return np.concatenate(pieces)

    def test_latency_is_taken_off_and_states_apply(self):
        x = np.random.default_rng(0).standard_normal((10000, 2)).astype(np.float32)
        out = self.run_chain([entry(0.5), entry(2.0, bypass=True), entry(3.0)], [37, 100], x, 3000)
        np.testing.assert_allclose(out, x * 1.5, atol=1e-6)

    def test_block_sizes_dont_matter(self):
        x = np.random.default_rng(1).standard_normal((5000, 1)).astype(np.float32)
        a = self.run_chain([entry(0.5)], [11], x, 5000)
        b = self.run_chain([entry(0.5)], [11], x, 777)
        np.testing.assert_array_equal(a, b)

    def test_process_file_runs_each_track_separately(self):
        folder = _mkdtemp().replace(os.sep, "/")
        src = f"{folder}/4T01_ISO.wav"
        x = np.random.default_rng(2).standard_normal((20000, 2)).astype(np.float32) * 0.1
        make_wav(src, filename="4T01_ISO.wav", float_samples=x)
        made = []

        def fake_open(kind, path, rate=RATE):
            made.append(FakePlugin(latency=64))
            return made[-1]
        with mock.patch.object(plugins, "open_plugin", fake_open):
            engine = plugins.ChainEngine([entry(0.5)], RATE)
            engine.block_frames = 6000
            dst = processing.output_path(src, "processed", "_FX")
            processing.process_file(src, dst, None, processing.Settings(mode="plugins", channels=(1,),
                                                                        label="Fake (VST2)"), engine=engine)
            engine.close()
        self.assertEqual(len(made), 1, "one instance for the one ticked track")
        self.assertTrue(made[0].closed)
        out, _ = processing.read_frames(dst, 0, 20000)
        np.testing.assert_array_equal(out[:, 0], x[:, 0])
        np.testing.assert_allclose(out[:, 1], x[:, 1] * 0.5, atol=1e-6)
        with open(dst, "rb") as f:
            self.assertIn(b"plug-ins: Fake (VST2)", f.read(4096))


class ChainJsonTest(unittest.TestCase):
    def test_round_trip_and_bad_input(self):
        chain = [entry(0.5), plugins.ChainEntry("vst3", "/x/EQ.vst3", "EQ", bypass=True)]
        self.assertEqual(plugins.chain_from_json(plugins.chain_to_json(chain)), chain)
        self.assertEqual(plugins.chain_from_json("not json"), [])
        self.assertEqual(plugins.chain_from_json('[{"format": "lv2", "path": "/a"}]'), [])
        self.assertEqual(plugins.describe_chain(chain), "Fake (VST2)")


class FindTest(unittest.TestCase):
    def test_finds_vst3_bundles_and_vst2_files_not_other_formats(self):
        root = _mkdtemp().replace(os.sep, "/")
        os.makedirs(f"{root}/EQ.vst3/Contents/x86_64-linux")
        open(f"{root}/EQ.vst3/Contents/x86_64-linux/EQ.so", "w").close()
        os.makedirs(f"{root}/sub")
        for name in ("Comp-vst.so", "Comp-ladspa.so", "Comp-dssi.so", "readme.txt"):
            open(f"{root}/sub/{name}", "w").close()
        os.makedirs(f"{root}/Thing.lv2")
        open(f"{root}/Thing.lv2/thing.so", "w").close()
        with mock.patch.object(plugins, "default_folders", lambda: {"vst2": [], "vst3": []}), \
                mock.patch.object(plugins, "VST2_SUFFIX", ".so"):
            found = plugins.find_candidates([root])
        self.assertEqual(found, [("vst2", f"{root}/sub/Comp-vst.so"), ("vst3", f"{root}/EQ.vst3")])


class PedalboardAddonTest(unittest.TestCase):
    def wheel_bytes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as whl:
            whl.writestr("pedalboard/__init__.py", "")
        return buffer.getvalue()

    def pypi(self, payload, digest=None):
        tags = plugins._wheel_tags()
        python, words = tags
        name = f"pedalboard-{plugins.PEDALBOARD_VERSION}-{python}-{python}-" + "_".join(words) + ".whl"
        info = {"urls": [
            {"filename": name.replace(f"-{python}-{python}-", f"-{python}t-{python}t-"), "url": "u-t", "size": 1,
             "digests": {"sha256": "x"}},
            {"filename": name, "url": "u-wheel", "size": len(payload),
             "digests": {"sha256": digest or hashlib.sha256(payload).hexdigest()}}]}

        def opener(url):
            if url == plugins.PYPI_URL:
                return io.BytesIO(json.dumps(info).encode())
            self.assertEqual(url, "u-wheel", "the free-threaded build is skipped")
            return io.BytesIO(payload)
        return opener

    @unittest.skipIf(plugins._wheel_tags() is None, "no pedalboard build for this computer")
    def test_install_checks_and_unpacks(self):
        data = _mkdtemp()
        payload = self.wheel_bytes()
        folder = plugins.install_pedalboard(data, opener=self.pypi(payload))
        self.assertEqual(plugins.pedalboard_installed(data), folder)
        with self.assertRaises(plugins.PluginError):
            plugins.install_pedalboard(_mkdtemp(), opener=self.pypi(payload, digest="0" * 64))
        plugins.uninstall_pedalboard(data)
        self.assertIsNone(plugins.pedalboard_installed(data))


class HostClientTest(unittest.TestCase):
    def test_a_dying_helper_is_reported_and_restarted(self):
        client = plugins.HostClient(_mkdtemp(), [sys.executable, "-c",
                                                         "import sys; sys.stdin.readline(); "
                                                         "sys.stderr.write('Segmentation fault\\n'); sys.exit(139)"])
        with self.assertRaises(plugins.HostDied) as caught:
            client.request({"cmd": "probe"})
        self.assertIn("crashed", str(caught.exception))
        with self.assertRaises(plugins.HostDied):
            client.request({"cmd": "probe"})  # started again
        client.close()

    def test_the_real_helper_answers(self):
        client = plugins.HostClient(_mkdtemp())
        try:
            with self.assertRaises(plugins.PluginError) as caught:
                client.request({"cmd": "probe", "format": "vst2", "path": "/nonexistent/x.so"})
            self.assertIn("can't load", str(caught.exception))
            with self.assertRaises(plugins.PluginError):
                client.request({"cmd": "nonsense"})
        finally:
            client.close()


class RealPluginTest(unittest.TestCase):
    """End to end through the helper with a real plug-in (see the module notes)."""

    def check(self, kind, path):
        folder = _mkdtemp().replace(os.sep, "/")
        src = f"{folder}/5T01_ISO.wav"
        x = np.random.default_rng(3).standard_normal((60000, 2)).astype(np.float32) * 0.1
        make_wav(src, filename="5T01_ISO.wav", float_samples=x)
        client = plugins.HostClient(folder)
        if os.environ.get("SFM_PEDALBOARD"):
            client.data_dir = folder
        try:
            probe, bad = plugins.probe_all(client, [(kind, path)])
            self.assertEqual(bad, [])
            opened = client.request({"cmd": "open", "format": kind, "path": path})
            state = client.request({"cmd": "state", "id": opened["id"]})["state"]
            chain = [plugins.ChainEntry(kind, path, probe[0]["name"], state)]
            dst = processing.output_path(src, "processed", "_FX")
            seen = []
            client.request({"cmd": "process", "chain": [vars(e) for e in chain], "src": src, "dst": dst,
                            "channels": [0], "label": plugins.describe_chain(chain)},
                           progress=lambda d, t: seen.append(d))
            self.assertEqual(seen[-1], 60000)
            out, _ = processing.read_frames(dst, 0, 60000)
            np.testing.assert_allclose(out, x, atol=1e-5)
            self.assertEqual(catalog.read_recording(dst).frames, 60000)
            preview = client.request({"cmd": "preview", "chain": [vars(e) for e in chain], "src": src,
                                      "first": 1000, "end": 5000, "channels": [0, 1]})
            np.testing.assert_allclose(np.load(preview["path"]), x[1000:5000], atol=1e-5)
            os.remove(preview["path"])
        finally:
            client.close()

    @unittest.skipUnless(os.environ.get("SFM_TEST_VST2"), "set SFM_TEST_VST2")
    def test_vst2(self):
        self.check("vst2", os.environ["SFM_TEST_VST2"])

    @unittest.skipUnless(os.environ.get("SFM_TEST_VST3") and os.environ.get("SFM_PEDALBOARD"),
                         "set SFM_TEST_VST3 and SFM_PEDALBOARD")
    def test_vst3(self):
        self.check("vst3", os.environ["SFM_TEST_VST3"])


if __name__ == "__main__":
    unittest.main()
