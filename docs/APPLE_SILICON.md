# Apple silicon performance

The Python interpreter, Qt and NumPy in the Mac bundle run natively on ARM64.
The audio calculations use NumPy's compiled array operations; a full rewrite
in another language is not required for this optimization.

The 24-bit PCM decoder now assembles signed samples directly into left-aligned
32-bit integers. This reduces temporary arrays while keeping the decoded
float32 samples exactly equal to the previous implementation. Playback and
waveform generation both use this decoder.

Measure it with synthetic data (no library access):

```sh
.venv/bin/python -m benchmarks.pcm_decode
```

On the development ARM64 Mac with NumPy 2.5.3, decoding eight channels measured:

| Input | Previous | Updated |
|---|---:|---:|
| 4,096 frames (playback block) | 0.073 ms | 0.060 ms |
| About 8 MB (waveform block) | 7.323 ms | 5.176 ms |

These are decoder timings, not whole-app speedups. Network scans and uncached
waveforms can still be limited by NAS latency and bandwidth. Timings vary with
system load. The benchmark checks sample equality before measuring.

## 32-bit float WAVs

Standard IEEE-float WAV and WAVE_FORMAT_EXTENSIBLE float WAV files are supported
by the parser, library, decoder and player. Tests cover both headers and samples
above 0 dBFS. Input samples retain their headroom through decoding and gain
adjustment; only the final speaker output is limited to prevent clipping at the
device. Lower the mixer gain to listen to over-range material. The waveform
overview currently tops out at 0 dBFS; it does not display the excess headroom.

Duplicate candidates now distinguish integer PCM from floating-point recordings,
including comparisons of versions with different track counts.
