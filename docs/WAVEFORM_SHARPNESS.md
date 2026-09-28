# Waveform sharpness work log

## Request and decisions

The full-recording overview looks softer than the Windows version. No screenshot or identical Windows rendering is available, so the exact platform difference is unconfirmed. The user chose a quick preview followed by background refinement and caching.

## Findings

1. Large files previously used 480 sampled sections, expanded to 4096 displayed buckets. This repeats coarse values rather than recovering the detail between sample locations.
2. Fractional-scale partial repaints multiplied integer rectangles by the scale factor before converting to floating-point coordinates. At 150%, rounding changes the source-image alignment between repaints. A synthetic pixel test reproduced the mismatch.
3. A detail read for a narrow window was reused after resizing, even when the wider window needed more columns.
4. Cached amplitudes used 8 bits (256 steps). A fresh waveform can retain finer vertical detail than the cached version on a tall/high-DPI display.

## Changes implemented

- Draw the cached pixmap from one fixed origin and let Qt clip updates. Rebuild it when the display pixel ratio changes.
- Request new detail when a resize needs more columns; stop requesting once there is one bucket per sample. Suppress duplicate requests for the same view and width.
- Show an existing cached preview immediately. Otherwise show a quick sampled preview, then read the selected recording sequentially through the Rust reductions. Keep preview data ahead of the read position rather than blanking the unfinished portion.
- Limit refinement progress updates to four per second (plus completion). Changing selection cancels the read between chunks. Adjacent-file prefetch keeps its limited read budget.
- Store new waveforms as 16-bit peak/RMS values with an explicit complete/preview flag. Read old 8-bit caches as previews and upgrade them on selection; no blanket cache deletion.
- Add a checked **Detailed Overview** switch to the waveform context menu. Disabling it restores limited reads for large files; completed caches remain usable. Local caching is automatic. Shared-library writes still follow the existing settings.

## Validation notes

- Pixel-for-pixel full versus partial repaint tests pass at 100%, 125%, 150% and 200% scaling.
- Tests cover monitor-scale invalidation, detail after resize and the sample-resolution limit.
- Existing waveform, native-backend, player, cache and library-index tests passed after the initial changes.
- Dedicated tests pass for a transient missed by sampling, preserving the unread preview during refinement, cancellation without a false complete flag, legacy cache upgrades, 16-bit cache precision, complete local/shared cache reuse, limited prefetch reads and preview preservation after an I/O failure.
- Full suite: **201 tests pass with Rust**. With NumPy forced, **201 tests run successfully, with three Rust-specific tests skipped**. Existing Qt deprecation and SQLite resource warnings remain.
- App smoke test at 150% scale passes: an old preview is displayed, refined, cached, then reopened without calling the waveform audio reader. Test settings, cache and audio were isolated in a temporary directory.
- Visually inspected `build/waveform-sharpness-comparison.png`: the full read resolves gaps between bursts that the sampled overview merges. This uses synthetic audio with the large-file sampling threshold lowered for demonstration; it is not a Windows/Linux screenshot comparison.

## Costs and limits

The first detailed view reads the complete selected recording, so a large file on a slow NAS can take time to sharpen. Reopening uses the completed cache while size and modification time still match. New cache payloads are twice the old size (16 rather than 8 bits per level); the shared-cache storage estimate has been updated. There is no full-library rebuild or change to audio data.

Qt reference: [High DPI](https://doc.qt.io/qt-6/highdpi.html) and [QPaintEvent automatic clipping](https://doc.qt.io/qt-6/qpaintevent.html).

## Result

Implemented and verified in the source directory used by the current application-menu launcher. Restart the app to load the changes. **Detailed Overview** is on by default; old cached waveforms upgrade as they are selected. No real-library audio or shared index was modified during testing.
