"""Synthetic benchmarks: python3 -m benchmarks.performance (no library access)."""
import tempfile
import time
import timeit
from unittest.mock import patch

import numpy as np

from sound_file_manager import catalog, compat, native_waveform, waveform
from sound_file_manager.file_model import COL, RecordingsModel
from tests.wavmaker import make_wav


def main():
    rec = catalog.Recording('/synthetic/Project/Day/12T001.wav', 1, 0, sample_rate=48000,
                            bits=24, channels=8, frames=480000, time_reference=48000,
                            tc_rate='24000/1001', scene='12', take='001', tracks=['Boom'] * 8)
    model = RecordingsModel()
    model.set_recordings([rec], '/synthetic')
    for label, fn in [('Scene cell', lambda: model._text(rec, COL['Scene'])),
                      ('Cached search text', lambda: model.search_text(0))]:
        elapsed = min(timeit.repeat(fn, number=21000, repeat=3))
        print(f'{label}: {elapsed * 1000:.2f} ms / 21,000 calls')

    # Inject network-like latency; this measures overlap, not actual NAS speed.
    with tempfile.TemporaryDirectory() as root:
        for i in range(40):
            make_wav(compat.join(root, f'{i}.wav'), frames=16)
        original = catalog.read_recording

        def delayed(path, stat):
            time.sleep(0.02)
            return original(path, stat)

        for workers in (1, 4):
            cache = catalog.Cache(compat.join(root, f'cache-{workers}.sqlite'))
            try:
                with patch.object(catalog, 'read_recording', delayed):
                    stats = catalog.scan(root, cache, on_batch=lambda batch: None, metadata_workers=workers)
                print(f'Scan, {workers} readers, 20 ms injected latency: {stats.seconds:.3f} s')
            finally:
                cache.close()

    raw = np.random.default_rng(42).integers(0, 256, (8 << 20) // 24 * 24, dtype=np.uint8).tobytes()
    cuts = np.linspace(0, len(raw) // 24, 4097, dtype=np.int64)
    def reduce():
        return waveform._reduce(raw, 24, 8, False, cuts)
    if native_waveform.AVAILABLE:
        native = reduce()
        rust = min(timeit.repeat(reduce, number=5, repeat=3)) / 5
    with patch.object(native_waveform, '_library', None):
        reference = reduce()
        numpy = min(timeit.repeat(reduce, number=5, repeat=3)) / 5
    print(f'Waveform, 8 MiB / 24-bit / 8 ch, NumPy: {numpy * 1000:.2f} ms')
    if native_waveform.AVAILABLE:
        np.testing.assert_array_equal(native[0], reference[0])
        np.testing.assert_allclose(native[1], reference[1], rtol=1e-12)
        print(f'Waveform, Rust: {rust * 1000:.2f} ms ({numpy / rust:.2f}x)')
    else:
        print('Rust not loaded; build with python3 scripts/build_native.py')


if __name__ == '__main__':
    main()
