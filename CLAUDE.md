# Location Sound File Manager: notes for working on this project

A PySide6 (Qt 6) + libmpv desktop app for Linux (CachyOS, KDE Plasma, Wayland). It manages a library of production sound WAV/BWF files: scanning, grouping by project, playback with a waveform, renaming, metadata editing and reorganising into folders. The user's library is on a CIFS/SMB NAS share with about 21,000 WAVs (2 TB).

For the history of how and why things were built, see `docs/DEVELOPMENT_LOG.md`. For features, see `README.md`.

## Run and test

```sh
./run.sh [library]         # the app  (./install.sh adds it to the app menu)
python3 -m unittest        # unit tests (tests/), no GUI or network needed
```
- **System packages:** `pyside6 mpv python-mpv python-numpy`.
- **GUI testing:** use throwaway scripts that drive `MainWindow` directly, with `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_DATA_HOME` pointing at a scratch dir so the real settings, cache and history stay untouched. Symlink `~/.config/kdeglobals` into the scratch config dir, or Breeze paints the toolbar with light header colours (a test artifact, not a bug). Set `window.player.mpv["ao"] = "null"` to stay silent. `QT_QPA_PLATFORM=offscreen` works, because the player needs no GL.
- **Never test writes on the real library.** Copy files into a scratch folder first.

## Layout

| Module | Role |
|---|---|
| `bwf.py` | **Pure** RIFF/RF64 chunk walker, bext + iXML parsing (`WavInfo`), `update_metadata()` (in place, or a verified full rewrite only with `allow_rewrite`) |
| `timecode.py` | **Pure** rate parsing ("24000/1001", "023.976-ND", "29.97DF"), samples → SMPTE (drop frame per 12M) |
| `catalog.py` | **Pure** `Recording` (one row), project assignment (metadata → folder fallback), SQLite `Cache` (metadata + waveform peaks), `scan()` |
| `organize.py` | **Pure** `{token}` patterns, move/rename planning with clash errors, `remove_left_empty()` |
| `renamer.py` | **Pure**, copied from video-renamer: validate, plan, two-phase apply with rollback, undo ops |
| `waveform.py` | **Pure** (numpy) per-channel peaks in dB, with bounded, progressive I/O |
| `player.py` | `PlayerWidget` (libmpv, audio only, `vo=null`) + `WaveformView`; peaks computed in a `QThreadPool` job, tagged by generation |
| `file_model.py` | `RecordingsModel` (table), `RecordingsProxy` (project/day/search/circled filter, natural sort), sidebar tree builder |
| `dialogs.py` | Rename, batch rename, metadata, organize, settings, rewrite confirmation, report. Dialogs only collect choices and preview |
| `workers.py` | `ScanThread`; `run_job()` = a function in a QThread behind a modal progress dialog |
| `main_window.py` | Toolbar (brand, Offload/Library switch), the stacked pages above the shared player, and everything for the Library. `do_renames()` and `do_metadata()` are the only paths that change files; undo stack; history log |
| `offload.py` | **Pure** card detection (`lsblk` removable mounts + SOUNDDEV/WAV check), card folders, `plan_copy()` (new/same/conflict), `copy_items()` (temp name, MD5 read-back verify, copystat, never overwrite), eject via udisksctl |
| `offload_page.py` | The Offload page: card reader thread, project/day tree with ticks and NAS folder names, the review table (editable `RecordingsModel`), report + copy panel, `CopyThread` (copy → write pending edits into the copies → reports per day folder) |
| `report.py` | Sound report rows/header detection (pure), CSV, branded PDF via `QTextDocument` + `QPdfWriter` with our own page numbering |
| `report_dialog.py` | `ReportGroup` (one report: title, files, folder, info) and `ReportDialog`: Previous/Next over groups (header fields per group; columns, style, page and branding shared), Save / Save All, export mode returns `infos()`. Remembers personal and per-project fields |
| `branding_dialog.py` | `BrandingEditor` (shared form), `ReportPreview`, the Report Branding window and the first-run `SetupDialog` (details, branding, output + library folders; sets `setup_done`); `load_branding` / `save_branding` (JSON in settings); chosen logos are copied to `~/.local/share/location-sound-file-manager/branding/` |
| `assets/` | Logo (black/white), mark without text (toolbar), app icon. From the user's branding folder |
| `settings.py` | Setting keys and defaults; paths: `~/.config/location-sound-file-manager/settings.ini`, `~/.cache/location-sound-file-manager/catalog.sqlite`, `~/.local/share/location-sound-file-manager/history.jsonl` |

## Design decisions (don't undo without a reason)

- **The user approves every change** (a standing preference). Previews for batch renames and moves, a per-field "Change" tick for multi-file metadata, and an explicit prompt, with sizes, before any full-file rewrite. Nothing autoplays.
- **Metadata writes:**
  - iXML is the source of truth; the bext description is a 256-byte legacy mirror. Only keys the recorder already wrote there are updated (no new lines are added), and a long NOTE is shortened *in bext only*.
  - In place means: pad the new iXML with spaces to the old chunk size, or absorb a directly following JUNK/PAD chunk. Otherwise `NeedsRewrite` is raised before anything is written.
  - A rewrite goes to `.sfm-tmp-*.wav` in the same folder, uses `copy_file_range` (server-side copy on SMB), fixes the RIFF size or the RF64 ds64 size, verifies the data size, then `os.replace`. Bytes after the last chunk are kept.
  - Every write is re-read and checked (the fmt bytes and data size are unchanged, the values read back).
  - Renaming a file without iXML updates only the bext FILENAME line, if there is one; it doesn't add iXML just for that.
- **Parser tolerance:** it stops at garbage after the last chunk (seen on real files), retries an odd-sized chunk without a pad byte, and marks a data chunk cut short (`truncated`). Truncated files are never written to.
- **Project** = iXML PROJECT; otherwise the first folder below the library that is neither a "container" (SD_1, SD_2, 833 BACK UPS; configurable) nor date-named (`25Y10M27`, `2024-05-17`). Projects are computed at load time, not cached, so a settings change applies without a rescan.
- **Scanning on the NAS:** the first read costs about 39 ms per file (about 14 min for the library); a cached rescan takes about 3 s per 8,500 files. At startup the cache is shown at once, then a rescan runs and merges its results (new rows appended, changed rows replaced, rows gone removed at the end).
- **Waveform I/O on SMB:** sequential reads run at about 130 MB/s, a random read costs about 8 ms. Files up to 256 MB are read fully in 8 MB pieces; larger ones are sampled (480 × 64 KB, coarse to fine). Both paths emit partial results. Use buffered reads: CIFS returns short reads (rsize 4 MB), which misaligned frames with `buffering=0`. Peaks are cached as uint8 per channel.
- **Monitoring:** poly files have no channel layout, and mpv's default downmix guesses 7.1 and drops the "LFE" channel. "All channels" is therefore a normalised `pan` to stereo, and solo is `pan=stereo|c0=cN|c1=cN`.
- **The player releases the file** (`mpv stop`) before a rename or write that affects it, then reloads at the same position (paused). SMB may refuse to change open files.
- **Take families** (Sound Devices `_ISO` + `_LR`) share the iXML FAMILY_UID in the same folder; metadata edits offer to include them.
- **Moves** only move audio files. "Remove folders left empty" treats `.take_folder`, `.daily_folder` and `.DS_Store` as empty and deletes them. Undo recreates the folders and the empty markers (not `.DS_Store`).

- **Offload never writes to the card.** Review edits are pending in `RecordingsModel.pending` (path → changes) and written into the NAS copies after verification. The copy then gets the card file's mtime back (`_keep_card_dates`), so a later offload of the same card sees it as "same" (size + mtime within 2 s). Renames already written are remembered for the session (`done_names`).
- **The copy layout** is `<library>/<NAS folder name>/<path below the card's project folder>`, which matches the existing library (833: `<Project>/<YYMMDD day>/[take folder/]`). The NAS folder name defaults to the card folder name and is remembered per card folder name.
- **Card switching** bumps a plan generation, and reader results are checked against the current card, because late results for the previous card used to arrive after the switch.
- **Library actions are disabled on the Offload page** so their shortcuts (F2, Ctrl+E…) go to the review table.
- **Waveform drawing:** rendered once into a pixmap by numpy rasterisation (about 4 ms at 2400×300 for 8 channels, against 77 ms for Python polygons). Playhead moves repaint only two 5-px strips. Files over 48 MB get a coarse preview (48 blocks) before the full read. The next 3 rows are prefetched into the peaks cache at low priority and cancelled when the selection changes.
- **iXML with a stale tail:** the 833 rewrites iXML in place and can leave the end of a longer, older document after a NUL. The parser cuts at the first NUL / `</BWFXML>`. Cache `PARSER_VERSION` 2 re-reads only the entries without iXML.
- **Reports are grouped by project**: in the Library by the recordings' project (from the sidebar multi-selection, the selected rows, or the current scope), and in Offload by card project folder. Offload writes one report per project into `<library>/<NAS folder>/` by default, or per card day folder (`report_per`), grouped by card folder and day, never by each file's own folder (833 take folders would give a report per take).
- **Library report windows are non-modal** (`WA_DeleteOnClose`, kept in `_report_windows`), so several can be open at once.
- **PDF:** `documentLayout().setPaintDevice(writer)` is required, otherwise the layout is made at screen DPI and the text comes out tiny.

- **Tests and Qt:** always use `tests.qt_app()` (one `QApplication`); a bare `QGuiApplication` from one test module later used for widgets corrupts the heap. Dialogs sit in reference cycles, so tests delete them with `tests.dispose()`, and `__main__` frees the window before interpreter exit.

## Conventions

- Match the existing style: pure logic in modules without Qt, with unit tests. Dialogs are thin. Comments explain *why*.
- The repository is **public**: https://github.com/cooldead/location-sound-file-manager (MIT). Never commit personal details: real client/project names (the tests use made-up ones), NAS addresses or paths, recorder serial numbers, emails or phone numbers. The history was started fresh at v1.0.0 for this reason.
- The desktop file is a template (`@APPDIR@`); `install.sh` fills in the path.
- Releases: `gh release create vX.Y.Z`. The first release was v1.0.0 (2026-09-25).
