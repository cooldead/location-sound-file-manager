# Performance and the Rust waveform core

## Current implementation

- Catalog scanning uses four workers for uncached WAV headers, with at most eight reads pending. Directory traversal still uses its existing independent pools. Cache/index hits skip header reads, and SQLite access stays on the scan thread. Results arrive in batches of 250 or after 250 ms when a result becomes available. Cancelled/incomplete scans do not prune records or replace the shared index.
- Selecting a file prepares its descriptor, metadata and recorder cues in a two-worker pool. Cues reuse the same parsed layout and open descriptor. Results carry a selection generation; stale sources close in the background. Selecting a file stays paused. Explicit Play while loading is remembered only for that selection.
- Playback owns an `AudioSource` whose lock protects each read and close. Selection retires the previous source without waiting on network I/O. Release before a rename/write or card eject waits for outstanding reads and closes. Shutdown also drains preparation jobs.
- Table formatting computes only the requested column. Search text is cached per row and invalidated on replacement/reset; query words are split once.
- Rust handles waveform PCM decoding, peak detection and accumulation of squared samples for RMS in a single pass. It supports unsigned 8-bit PCM, signed 16/24/32-bit PCM and 32/64-bit float. NumPy still handles dB conversion, drawing and playback decoding. Overview, sampled preview and zoomed detail all use the Rust reductions when available.

Detailed overviews now refine the selected recording after a preview, cache 16-bit levels and maintain pixel alignment at fractional display scales. See the [waveform sharpness work log](WAVEFORM_SHARPNESS.md) for the read/cache tradeoffs and validation.

## Build the Rust core

Install a host Rust toolchain with Cargo, then run from the repository:

```sh
python3 scripts/build_native.py
python3 -c 'from sound_file_manager.native_waveform import AVAILABLE; print(AVAILABLE)'
```

The second command should print `True`. With rustup, `SFM_RUST_TOOLCHAIN=stable` selects the stable toolchain. The crate has no third-party dependencies and builds offline. It produces a host `.so`, `.dylib` or `.dll` in `sound_file_manager/_native/`; generated binaries are ignored by Git. Rebuild after changing the Rust source or switching host architecture. Normal CPU targets are used so release binaries do not require the builder's CPU features.

The interface is a versioned C ABI loaded with `ctypes.CDLL`, which releases Python's GIL during a call. Python owns the input/output buffers; Rust retains no pointers. This avoids a Python-version-specific extension dependency. A missing/incompatible library falls back to NumPy. Set `SFM_WAVEFORM_BACKEND=numpy` before starting the app to compare the fallback.

macOS, Windows and Linux AppImage build scripts compile and bundle the core. Arch builds require `rust` and now produce architecture-specific packages. Source-only installs can omit the native build and retain the Python/NumPy path.

## Checks and benchmarks

```sh
cargo test --offline --manifest-path native/waveform/Cargo.toml --target-dir build/rust
python3 -m unittest
SFM_WAVEFORM_BACKEND=numpy python3 -m unittest
python3 -m benchmarks.performance
```

The native tests compare all supported formats, signed values, channel ordering, float NaN/infinities and over-range values, partial frames, bucket boundaries, progressive results, sampled/zoomed reads and cache bytes. Concurrency tests cover overlapping header reads, cancellation, stale selection results, deferred Play and file release.

The benchmark uses only synthetic files and buffers. Its scan test injects 20 ms per header read to measure concurrency; it is not a NAS throughput measurement. The waveform test compares an approximately 8 MiB, 24-bit, eight-channel block. Measure actual cold scans, warm rescans and time to first waveform on the target NAS before tuning worker counts. Increasing concurrency can saturate a share instead of reducing latency.
