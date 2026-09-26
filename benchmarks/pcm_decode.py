"""Run from the repository: .venv/bin/python -m benchmarks.pcm_decode.
Synthetic data only; compare the former decoder with the current one.
"""
import platform
import timeit

import numpy as np

from sound_file_manager.waveform import decode


def previous(raw):
    b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
    values = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    values = np.where(values & 0x800000, values - 0x1000000, values)
    return values.astype(np.float32) / 8388608.0


def main():
    print(f'{platform.machine()}, NumPy {np.__version__}')
    for frames in (4096, (8 << 20) // 24):
        raw = np.random.default_rng(42).integers(0, 256, frames * 24, dtype=np.uint8).tobytes()
        np.testing.assert_array_equal(previous(raw), decode(raw, 24, 8, False).ravel())
        times = [min(timeit.repeat(fn, number=20, repeat=5)) / 20
                 for fn in (lambda: previous(raw), lambda: decode(raw, 24, 8, False))]
        print(f'{frames:,} frames, 8 channels: {times[0]*1000:.3f} → {times[1]*1000:.3f} ms '
              f'({times[0]/times[1]:.2f}x throughput)')


if __name__ == '__main__':
    main()
