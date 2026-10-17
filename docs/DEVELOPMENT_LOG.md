# Development log

## 2026-09-25: first version

**Request:** an app to scan a folder of recorded sound files, sort them by project, play them, allow renaming, and read embedded data such as timecode. Name: now "Location Sound File Manager" (renamed later, see below).

**The library, as found:**
- About 21,000 WAVs (2 TB) on a CIFS share, from a Sound Devices 833 (`sKEY=` bext lines, `JUNK bext iXML fmt data [cue]`, `_ISO`/`_LR` take files in take folders) and a Zoom F8/F8n (`zKEY=`, `bext iXML fmt PAD data`).
- Almost all files carry iXML with PROJECT, SCENE, TAKE, TAPE, CIRCLED, NOTE, TIMECODE_RATE/FLAG, TRACK_LIST and FAMILY_UID.
- Some files are RF64 (a 7.7 GB table read); some have garbage after the last chunk; recovered files have no metadata or no fmt chunk at all.
- Folder layout is mixed: per-project folders, plus card dumps (`SD_1`, `SD_2`, `833 BACK UPS`) that contain project folders.

**The user's choices:**
- Grouping happens in the app, **plus** an optional "reorganize into folders" that moves files on disk after a preview.
- Renames update the filename inside the WAV, and scene/take/tape/note/circled/project are editable.

**Built:**
- Pure modules, with 60 unit tests (a synthetic BWF maker copies the real SD and Zoom layouts, plus RF64).
- The PySide6 GUI.
- The rename engine reused from video-renamer.

**Verified:**
- On copies of real files: in-place writes keep the audio md5 and file size; the rewrite path keeps the audio md5; ffprobe still reads the files.
- The GUI smoke test (offscreen, scratch config) on 34 copied real files: scan → projects; select → paused load and waveform; play advances the timecode (`23:29:09:00` → `23:29:10:03`); solo filter; rename with the embedded name while loaded; metadata on an `_ISO`/`_LR` family; move with empty-folder removal; three undos restore everything, including the `.take_folder` markers.
- Visually on the Wayland session (screenshot).

**Measurements that shaped the design:**
- Header scan: about 39 ms per file on the share (8,519 files in 332 s); a cached rescan of the same files takes 3.2 s.
- Waveform: the first version took 20 s on the 7.7 GB file (1,200 random reads at about 8–16 ms). Measured sequential throughput is 130 MB/s against 8 ms per random 32 KB read. Result: full reads up to 256 MB, sampled progressive reads above that. The 7.7 GB file now shows in 0.2 s and completes in 2.9 s.
- The bext description is only 256 bytes, so a 100-character note overflowed it on SD files. The note is now shortened in bext only; iXML keeps the full note.

- Full library (21,259 files, 222 projects): the first scan took about 10 min in the background; the app shows the cache in 1.1 s and a rescan takes 12 s. Sorting took up to 6.6 s until sort keys were cached per cell; it now takes about 0.75 s on any column. Search and project clicks are instant. 29 files are unreadable (28 "not a WAV file" among recovered files, 1 with no fmt chunk).

**Gotchas hit:**
- Unbuffered reads on CIFS return short reads, which misalign PCM frames. Fixed by using buffered reads.
- Setting `XDG_CONFIG_HOME` to a scratch dir hides `kdeglobals`, so Breeze paints toolbars with light header colours and the enabled buttons look invisible. This is a test-only artifact.
- mpv's default downmix of 8-channel poly WAVs assumes 7.1 and drops channel 4 ("LFE"). Replaced with an explicit `pan` mix.

**Open items / ideas:**
- Merge the obvious duplicate project names the scan surfaced (e.g. `NIGHT SHIFT`/`NIGHTSHIFT`, `SKYLINE`/`SKYLYNE`, `RedLine`/`Red Line`). The user decides which to merge.
- Only WAV/BWF files are scanned; there are also MP4/MOV/CR3 files in the library (camera files) that are ignored.

## 2026-09-25: branding, smoother waveform, Offload workflow

**Request:**
- Add the user's branding (`~/Pictures/branding`).
- Make the waveform smoother.
- Rework the interface around the workflow: record on set → bring the card home and make a sound report (new) → export to the NAS's Sound Backups → send the project out (the user handles sending; the app only opens the project folder).

**Findings:**
- An 833 card was mounted, laid out as `<Project>/<YYMMDD>/…` next to SOUNDDEV/SETTINGS/TRASH/MIDI_MAPPING/FALSETAKES. The NAS root also holds SOUNDDEV/SETTINGS/TRASH folders, from copying whole cards in the past.
- Three projects on the card (Meadow, Riverside, Orchard) are not on the NAS yet.
- Existing reports: Wave Agent PDFs, recorder CSVs, and hand-made CSVs with Project/Mixer/Date/Director and contact lines.

**Built:**
- Branding assets: logo, text-less mark for the toolbar, app icon.
- The Offload page, with pending review edits.
- Verified copy (MD5 read-back, temp names, never overwrite).
- Reports per day folder (PDF + CSV).
- Open Project Folder / Show in Library / Eject.
- Library: Sound Report and Open Project Folder actions.

**Waveform:**
- The old paint rebuilt a QPainterPath for every channel on every 40 ms playhead tick (about 77 ms each, i.e. more than a CPU core while playing).
- Now it renders into a pixmap once (numpy raster, about 4 ms) and moves the playhead by repainting 2 strips (0.06 ms).
- Coarse preview for large files, and prefetch of the next rows.

**Verified:**
- 72 unit tests.
- Scratch offload (a copy of 2 real projects plus FALSETAKES and SOUNDDEV; one file pre-placed on a scratch NAS): 15 copied and verified, the pre-placed one skipped, the note, circled take and rename written into the copies (the card unchanged), dates kept, 3 reports written, and a re-plan shows 0 to copy.
- Library smoke test re-run with no regressions.
- Screenshots on Wayland with the real card.

**Bug found by the test:** `1 - lightingT01_ISO.wav` has iXML followed by `\0L>\n\0…` (the stale tail of an older document), so it read as "no iXML" and edits were refused. The parser now cuts at the NUL; a cache migration re-reads 399 entries.

**Open items:**
- A first real offload (Meadow, Riverside, Orchard) is for the user to run.
- The NAS root's stray SOUNDDEV/SETTINGS/TRASH folders could be cleaned up. Not touched.

## 2026-09-25: sound report columns and page orientation

**Feedback:**
- "26Y08M20 / 24Y11M09 / … these are dates not rolls": the 833's TAPE field is the day folder. Roll / Card is no longer filled from tapes that look like `NNYNNMNN`.
- Take and Length wrapped onto two lines.
- The user asked for selectable columns that update dynamically, and a portrait option alongside landscape.

**Built:**
- `report.COLUMN_DEFS` (18 columns, including End TC, date, time, day folder, FPS, format, type, size, and one column per track). The report window has a tick-and-drag checklist and a Page (Landscape/Portrait) choice; the preview updates live and the choice is remembered. The CSV follows the same columns.
- Column widths come from the content, measured with real font metrics when Qt runs (character counts in tests), so short columns never wrap.
- When the page is too narrow, space is given up in this order: notes → tracks (never below the longest single track name) → smaller table text (8 → 6 pt, with a notice in the window) → file names.
- Qt's `nowrap` alone is not respected in a crowded table; the widths have to be right.

**Follow-up, same day:** the user showed their template, a bordered grid with `Filename | Scene | Take | TC Start | Dur | Trk1 | Trk2 | …` and a "Notes:" row under each take.
- Added the "Boxed" style (the new default, `border-collapse` table, notes as a full-width row) and kept "List".
- Headers were renamed to the template's.
- Default columns are now the template's.
- Circled takes are bold even without the ★ column.
- The "extra numbering" in scenes turned out to be one project's own scene names; nothing changed there.

## 2026-09-25: report branding, one report per project

**Request:**
- A menu for users to add their own branding to reports.
- With several projects selected, one report per project containing only that project's files.
- Several report windows at once, or a Next button.

**Built:**
- `report.Branding`: logo (built-in, none, or a file copied into app data), logo size, title, line under the title, header colour (text colour picked for contrast), and a page footer.
- The Branding window has a live preview and is reachable from the new Settings dropdown and from the report window.
- `ReportDialog` takes a list of `ReportGroup`s, with Previous/Next and Save All.
- Library: multi-select in the sidebar; `report_groups()` makes one group per project. Report windows are non-modal.
- Offload: a report group per card project, and per-project reports in the NAS project folder (default) or per day folder.

**Bugs found along the way:**
- Offload reports were grouped by each file's own folder, which would have made one report per 833 take folder.
- Opening a card set the new card before clearing the old card's files, so a panel refresh compared paths across cards (ValueError). The model is now cleared first, and the path check tolerates foreign paths.

**Verified:**
- 84 unit tests, including the dialog: per-group fields, shared layout, Save All.
- Scratch offload: 2 per-project reports in `Orchard/` and `Meadow/`.
- Library: 2 selected projects → 2 groups with the right folders, 2 windows open at once.
- Screenshot of the Branding window.

## 2026-09-25: destination picker, renamed to Location Sound File Manager

**Request:**
- Choose the output folder in the Copy to NAS panel.
- Remove the user's name from the app, but keep the branding images.
- New name: "Location Sound File Manager".

**Context:** with no destination choice, the first real offload went into whatever the library folder was set to, which wasn't the intended place.

**Built:**
- The *Into:* dropdown: "Library folder" plus recent destinations and Browse…
- The plan, reports, the "existing folder" status and Open Folder all use the destination. The library rescan and Show in Library only apply when the destination is inside the library.
- Report project name: when a card folder holds files from two projects (e.g. PILOT-D2 plus PILOT-D3 takes), the folder / NAS name is used instead of "A / B".
- The rename covers the app name, the toolbar ("LOCATION SOUND / File Manager"), the desktop file (`location-sound-file-manager.desktop`, old one removed), the docs, the PDF creator and the test names.
- Internal folders changed from `tp-sound-file-manager` to `location-sound-file-manager` (config, cache, data), and `settings.migrate_old_folders()` moves them on start, fixing saved paths in `settings.ini`.
- The user's real folders were migrated: settings, the 21 MB cache and history.
- LICENSE copyright is unchanged (it's ownership, not app branding).

## 2026-09-25: first-run Setup window

**Request (before a GitHub release):**
- A first-run pop-up for branding and sound report info (email and phone if wanted).
- An option for phone/email at the top of the report and/or in the footer.
- A default output folder, and a library folder if it differs.

**Built:**
- `SetupDialog`, shown once (`setup_done`), with "Skip for Now". It's reopened from Settings ▸ Setup….
- `Branding.contact_in_header` / `contact_in_footer` and `report.footer_text()`.
- `BrandingEditor` shared with the Branding window.
- Destination list: "" (the library folder) as the first entry now selects the library folder.

**Crash found:** `corrupted double-linked list` at the end of the full test run.
- Cause 1: report tests created a `QGuiApplication` that the dialog tests then used for widgets.
- Cause 2: dialogs in reference cycles were freed after the QApplication.
- Fixed with a shared `qt_app()`, explicit disposal in tests, and freeing the window in `__main__`. Six clean runs in a row afterwards.

## 2026-09-26: find duplicates

**Request:** in the Library, scan for duplicate files and duplicate projects. Merge them if the files inside differ, delete them if they are the same. _ISO and _LR of a take should be kept together in one folder.

**Built:**
- `duplicates.py` and the Find Duplicates window. Details in CLAUDE.md.

**Findings on the real library** (read-only dry runs):
- 21.6k recordings; ~11.7k are candidates, with up to ~620 GB in possible extra copies.
- 5 "spelling" groups, 3 "similar" groups, and ~110 folders that exist in several places (card dumps).
- A folder copied three times: 47 byte-identical and 7 audio-identical copies (second-card layout), 2.5 GB.

**Design fixes found while testing:**
- "Same project in several folders" first used the metadata project name, which flagged a project whose files were in another shoot's folder (the recorder's project name wasn't changed), plus FALSETAKES/Recovered. It now means folders with the same name in different places, and leftover folders are excluded.
- Identical vs different was judged per group. It's now judged per copy against the kept one.
- Merges proposed removing a twin that had an extra note. Such twins are now skipped, with the reason.
- Fingerprints: 99 → ~50 ms per file by avoiding CIFS read-ahead.

**Verified:**
- 96 unit tests.
- A GUI smoke test on a scratch library: remove to the holding folder, then undo; merge folders; merge a name (move plus project name), then undo.
- A screenshot of the window.

**Follow-ups from testing:**
- "Check/uncheck" wording.
- Wider keep-folder and project-name fields.
- Find Duplicate Projects runs in the background, with a progress bar and message.
- `_Removed Duplicates` is sorted by original project.
- Merge into a new folder, with a suggested name.
- Review & Delete window (removed-duplicates folder and `_ReviewForDeletion` files: play, put back, delete for good).
- Files that are not byte for byte the same can be copied (default) or moved next to the kept copy as `<name>_ReviewForDeletion`, instead of being left or removed on an audio match.
- The new-folder path is shown under its name.
- Speed: Find Duplicate Projects went from 29 s to 0.37 s (folder index, string paths; results checked identical to the old code on the real library). A first Duplicate Files scan fingerprints about a third of the files (one per distinct name in a candidate group). After a merge the project search re-runs automatically.
- Batch merge: check groups (or a category), merge them one after another with a progress bar and result per group, optionally each into a new folder named after it; empty folders removed; one undo step; failure report at the end. The cleanup code was split into work / finish steps shared by single and batch merges.
- _ISO/_LR rule: a take's files are always kept together and never renamed; only exact same-name repeats are removed (notes ignored, audio checked); never split a take. Keepers prefer copies with notes.
- Compare Marked Files: `_ReviewForDeletion` files paired with their closest copy, differences highlighted, play both, audio comparison, delete or keep the marked copy instead.
- Versions with notes are preferred: a copy with a note or circle the kept copy lacks replaces it in place (audio checked; the old copy goes to the holding folder), in single merges, batch merges and Duplicate Files.
- Versions with more tracks are preferred in merges: same take with a different channel count (e.g. with and without the mix tracks); the smaller file is removed or replaced only after verifying all its channels are in the larger one.

## 2026-09-26: mixer, colour waveform, year/month sidebar

**Request:** a layout closer to Sound Devices Wave Agent X: a channel mixer at the bottom with all its features, a cleaner waveform in a different colour per track. Also a busy bar while a single merge works, the Library sorted by year and month, and a Delete Empty Folders button (nested empty folders such as `Orchard/25Y10M31/.daily_folder` survived merges).

**Built:**
- Playback moved from libmpv to `audio_engine.py` (Qt Multimedia `QAudioSink` + a reader thread), mixed live by `mixer.py`. Why: mpv's `af` can't change per-channel gains smoothly or report per-channel levels.
- `mixer_panel.py`: master and channel strips (colour, M/S, automation A/✕, meter with hold and clip, fader, dB, link, pan), Solo Mode, Mute All, Arm All, Clear All Automation, Save/Load Automation (`.mix.json`), auto trim.
- Waveform: peak + RMS per bucket (2000 buckets, new cache format), `rasterize()` with a colour per track, anti-aliased, overlay or lanes, dB or linear; drag regions and loop; prev/next file buttons.
- Sidebar: year → month → project → day (or A–Z by name).
- Progress: a total of 0 means a moving bar; cleanups start busy, and retags and empty-folder removal report progress.
- Empty folders: removal is recursive; Delete Empty Folders in the Find Duplicates window (undoable); Delete for good prunes folders it empties.

**Verified:** 121 unit tests; GUI smoke tests (play with a muted master, meters, automation writes, region loop, file stepping, year/month tree, Delete Empty Folders + undo, earlier merge/batch scripts). A read-only dry run on the real library found 273 empty folders in ~7 s (none deleted).

## 2026-09-26: markers, zoom, sharper waveform, project counts

**Request:** Wave Agent style markers and zoom; a sharper waveform ("still very undefined"); the sidebar years and months should count projects, not files (files optional).

**Built:**
- Markers: add at the playhead (M), drag, name (double-click), delete, previous/next (, .); stored per path in `markers.sqlite` and moved with the files; recorder cue chunks shown read only.
- Zoom: wheel / buttons / keys, time ruler, scrollbar, follows the playhead; vertical zoom. Zoomed views are re-read from the file at pixel resolution.
- Sharper: 4096-bucket overview (was 2000, stretched and smeared on wide/HiDPI screens), full-colour peak outline instead of a dimmed half-tone edge.
- Sidebar numbers count projects by default; the # button switches to files or both.

**Verified:** 125 unit tests; smoke test with screenshots of the full and zoomed views, marker add/jump/move.

## 2026-09-27: tested with a real card, 32-bit float

**Real card (833, read-only mount):** 141 recordings read in 3 s; card folders matched the library by name (no project dialog, correctly). Findings, fixed:
- 10 files showed "conflict: different file on NAS" though they were the same recordings (audio identical; the NAS copies were 12 bytes shorter/longer, one only a different date). Conflicts are now fingerprinted: same audio = "on NAS (same audio, metadata differs)".
- A fully backed-up day was ticked because its `.daily_folder` marker was missing on the NAS (removed by empty-folder cleanup). Marker files no longer count as new.

**32-bit float:** parsing and decoding existed; added over-0 dBFS marks on the waveform, a NaN/infinity guard, a float WAV writer for tests and `tests/test_float.py`. Checked on a real Deity PR-2 file (80 min mono, peaks at +0.7 dBFS).

## 2026-09-27: Undo crash, split into track files

**Undo crash** (reported: rename a take, click Undo, the app quits): the core dumps showed a segfault inside the confirmation box ("Undo …? / Don't ask again"). The checkbox was passed as a temporary, and PySide doesn't give it to the box, so it was deleted at once and the box used a dead pointer. Reproduced offscreen (crash in `checkBox()` after `exec()`); fixed by keeping a reference. It happened with any undo while the confirmation was on, not just renames.

**Split into track files:** requested as "split wavs into files adopting track names into a folder with the scene and take name", with the option to keep the original by moving it into that folder. `splitter.py` + `SplitDialog` + `MainWindow.split_selected`; see CLAUDE.md for the metadata details. Decisions: the take's `_ISO`/`_LR` partner moves into the folder too (takes stay together); an original that isn't kept goes to `_Removed Duplicates` (undoable) rather than being deleted; track files aren't renamed by scene/take edits.

**Verified:** 171 unit tests (7 new in `tests/test_splitter.py`: sample-exact channels for 24-bit and float, metadata, odd frame counts, names, partners, clashes, RF64 header); offscreen GUI script: take edit + Undo through the confirmation box, split keeping the original, scene edit on a track file, undo; split without keeping, undo.

**Choose the tracks** (asked right after: "allow me to select which tracks are split off and which stay with the original file", then "have options for both"): each track has a tick in the Split dialog. The unticked tracks stay with the original, either kept whole (it moves into the take folder unchanged) or shrunk to those tracks (rewritten under its name in the take folder; the full file goes to `_Removed Duplicates`, so it can be put back and Undo restores it). Verified: 9 splitter tests (shrunk original sample-exact, TRK renumbering); offscreen script ticking tracks through the tree for both options, then Undo.

**Group tracks into one polywav** (asked next): select tracks in the Split dialog and *Group into One File…* (name suggested as `LAV1+LAV2`); ungrouped ticked tracks stay mono. Group files keep their tracks' iXML entries renumbered, and scene/take edits don't rename them (`is_track_file`). Verified: 10 splitter tests; offscreen script grouping three of four tracks, a take edit on the group file, Undo.

**Combine into polywav** (asked after the user looked for grouping with split mono files selected in the Library): select files of one take, set the order and name, keep or remove the sources. Refuses files that don't line up (length, rate, bits, format, timecode). Verified: combining split files gives back sample-identical audio, and a reordered combine follows the order (12 splitter tests); offscreen script split → combine (keep and remove) → Undo. Note for GUI scripts: with a card inserted the window opens on the Offload page, where Library actions are disabled; call `set_page("library")`.

**Combine name:** the default now starts with the scene and take (asked by the user), `2B-T001_BOOM+LAV.WAV` when the plain name is taken.

**Delete permanently** (asked: instead of moving files to the removed folder): an option in Split (originals that leave) and Combine (the files combined), with a confirmation listing the files; deletion happens after the new files check out, and there is no undo step for it. Verified offscreen: cancelling the confirmation changes nothing; split + delete and combine + delete leave only the new files and no undo.

**Safe / Dangerous mode** (asked: save those choices so they don't have to be made every time, as a dangerous vs safe mode): Settings → *Split and combine: files that are replaced*. Verified offscreen: safe mode never starts on delete and confirms; dangerous mode starts on the remembered delete and doesn't ask.

**Scene/take renames keep the rest of the name** (asked: `2BT001_BOOM` with take 002 → `2BT002_BOOM`, so a take's files don't clash): `scene_take_name` replaces the scene+take found inside the name. Verified: renamer tests; offscreen take edit on three files of one take, then Undo.

## 2026-09-27: macOS build of 1.5.0

Pulled the Linux 1.5.0 work (split/combine, table edits, notes box, Undo crash fix, Windows port) onto the Mac. No Mac-specific code was needed: the new modules are plain Python/numpy, and `compat.py` leaves macOS on the `os` functions. Only UI text: the notes box tooltip showed "Ctrl+Enter" (now `keys()`, ⌘↵) and the Split dialog's hints said "Ctrl-click", which on a Mac is ⌘-click (`dialogs.MULTI_CLICK`).

**Verified on macOS** (Apple silicon): 178 unit tests; an offscreen GUI script on scratch files: split (track levels checked), Undo through the confirmation box with "Don't ask again" (the crash), split → combine (sample-identical to the original) → Undo twice, a scene edit in the table renaming the file (iXML and bext), a note from the notes box, Undo of both. Then the built `.app` was launched and checked.

## 2026-09-28: macOS build of 1.6.0

Pulled the Linux 1.6.0 work (Rust waveform core, background waveform refinement, local card working copies with write protection and backup verification). Installed Rust with Homebrew for the Mac build; the crate has no dependencies and builds offline for arm64. Measured on Apple silicon: 13.6 ms (Rust) vs 57.4 ms (numpy) to reduce one minute of 6-track 24-bit audio.

**Mac fix:** `card_safety` compared paths after `normcase` + `realpath`, which change nothing on macOS, but Mac volumes (APFS, exFAT/FAT cards) ignore case and Unicode form: a write to `/Volumes/sd_card/...` passed the guard for a card protected as `/Volumes/SD_CARD`. Paths are now casefolded and NFC-normalised on macOS (test: `test_guard_ignores_case_and_unicode_form_on_mac`, which failed before).

**Verified on macOS:** 219 unit tests (the Rust tests run now instead of skipping); an offscreen GUI run with a FAT32 disk image as the card: local working copy, protection under both case spellings, a review note written only into the storage copy, the card unchanged (checksums), the working copy removed on close; the player with a 494 MB 6-track file (first waveform 0.1 s, complete 0.3 s) and playback; then the built `.app`.

**Full screen on macOS** (reported: selecting files in the Library in full screen minimised the app and left full screen). Reproduced offscreen with copies of the affected files: every selection showed and hid top-level `QToolButton`s, one per stereo channel pair (3 for a 4-track file): `ChannelStrip` called `self.link.setVisible(can_link)` before the button was in a layout, so it was shown as its own window. Linux never showed it visibly; on macOS a new window takes a full-screen app out of its Space. The button is now only hidden before it is placed. Also found: the window's minimum width grew when a file loaded (the file name label and the timecode label's digits), which resizes the window and also ends full screen when it passes the screen width; the title no longer sets a minimum width and the timecode reserves its width. Verified on a 1470×956 offscreen screen: full screen kept for all 7 files (13 stray windows before); new test `StripWindowTest` (fails on the old code).

## 2026-10-06: Reduce Noise (de-hiss and denoise)

Asked for processing: a de-hisser, a denoiser and a de-rustler. De-hiss and denoise are built in, with numpy only (no new dependency, no size cost): right-click → *Reduce Noise…* writes processed **copies** (`Processed/<name>_NR.wav` by default), never touching the original. The noise profile comes from a region marked on the waveform (room tone) or from each file's quietest frames. Tracks are picked by name, so a batch can process only the lavs. The dialog has an A/B preview (12 s from the playhead, switching while it plays).

De-rustle was left out on purpose: rustle is short and overlaps the voice, so a learned noise profile doesn't catch it; tools that do it well use a trained model. Discussed: DeepFilterNet (MIT/Apache, 48 kHz only) as an optional download (its standalone `deep-filter` binary is 26–37 MB per platform; the Python package needs PyTorch, hundreds of MB), and fine-tuning it with the user's own room tone and rustle as noise material (production tracks can't be the clean side). Not started.

**Verified:** 20 new tests (noise down and voice level kept within 0.5 dB, the reduction capped at the chosen amount, 0 dB changes nothing, de-hiss leaves below 1 kHz within 0.3 dB, blocks identical to one piece, unticked tracks byte for byte, metadata kept, failures and cancel leave no files, preview equals the written file, the dialog's choices); an offscreen GUI run (mark a region → Reduce Noise → the copy in the library → Undo removes it and the folder); a copy of a real 2-track 24-bit take: all metadata the same, the lav's noise floor 8 dB lower and the overall level 1.8 dB lower. Speed: 60 s of 8 tracks in 1.7 s.

**DeepFilterNet add-on** (asked right after: "lets do this"): a third choice in Reduce Noise, *De-rustle / voice cleanup*, using DeepFilterNet's standalone `deep-filter` 0.5.6, downloaded on request (checksums pinned per platform) and run per track in blocks (see CLAUDE.md for the measured quirks: 48 kHz only, float in, 16-bit out, 30 ms at the end with `-D`, slow model state). Verified: 12 new tests (a stand-in program; install with a good, a bad and a cancelled download; blocks line up; cancel kills the program; the real program on synthetic noise); the real install from GitHub into a scratch data folder; an offscreen GUI run in this mode on a copy of a real lav take (copy written and in the library, the other track byte for byte, coding history names DeepFilterNet, Undo). On that take, five-minute blocks with 10 s of pre-roll match one pass to within -64 dBFS overall.

**VST2 and VST3 plug-ins** (asked for VST/AU/AAX; then "only vst2 and 3 for now"): a fourth choice in the window, *Plug-in chain*, with an *Edit Chain…* window (scan, folders, chain, sliders per setting). VST2 by our own ctypes host (no SDK needed: the binary interface is a C struct and opcode numbers); VST3 through pedalboard as a downloaded add-on (GPL-3.0, kept out of the MIT builds). Plug-ins run only in a helper process (`--plugin-host`), so a crash can't take the app down. Each track goes mono through its own chain instances; latency is taken off. Verified: 10 new tests (latency and block sizes with a stand-in, per-track instances, bypass, scanning rules, the add-on install with a good and a bad checksum, a dying helper, the real helper) plus end-to-end tests with real plug-ins (DISTRHO DPF-Plugins v1.7, 3 Band EQ as VST2 and VST3: unchanged at defaults, copy and preview); an offscreen GUI run: scan of the DPF pack (23 effects; instruments and 4 unloadable files reported, no crash), a VST2 → VST3 EQ chain with Low at -24 dB on a copy of a real take (below 150 Hz 8 dB lower, 3–8 kHz unchanged, the other track byte for byte, coding history names the chain), Undo.



## 2026-10-17: Effects (an Easy Effects style rack, built in), v1.8.0

Asked to look at Easy Effects and make an add-on from it, then: "Instead of an add on just add it as a feature it should be built in to the app." Easy Effects is a live PipeWire effects app (GPL-3.0) whose effects are mostly LSP/Calf/ZaM LV2 plug-ins; it has no way to render files. So the rack is our own: right-click → *Effects…* with Filter, Equalizer, Gate, Expander, Compressor, De-esser, Limiter, Loudness (BS.1770 / EBU R128) and Gain/Delay, in any order, with presets (built-in dialogue presets, saved ones, and Easy Effects .json import), an A/B preview that follows every change and reports what each effect did, and copies written like Reduce Noise (`Processed/<name>_FX.wav`, metadata kept, unticked tracks byte for byte, Undo). Only effects useful for production dialogue were built; music effects (bass enhancer, exciter, crystalizer, crossfeed, reverb, pitch, autotune, convolver) were left out, and an import lists what it skipped.

**Verified:** 31 new tests (filter slope and cutoff, the response graph matching the audio, EQ bell/notch, a flat EQ unchanged, the compressor's static curve, untouched below threshold, gate and expander amounts, the limiter never over its ceiling, the de-esser lowering 8 kHz and not 300 Hz, look-ahead taken off, blocks identical to one pass, bypass, gain/delay/polarity, BS.1770 reference tone at 44.1 and 48 kHz, gating, normalising, peaks holding the gain back with a note, silence, JSON clamping, presets, an Easy Effects import, the copy (metadata, untouched track, loudness reached, progress only forward, coding history), preview equal to the copy, cancel, and the window's chain editing, EQ table, presets and every effect's panel); four community Easy Effects presets imported; an offscreen GUI run on a scratch library (a speech recording and a 2-track take): preset → preview report → Make Copies (both tracks -23.0 LUFS) → history log → Undo removes the copies and the folder. 293 tests in all.

## 2026-10-17: Loudness Check and delivery targets

Asked to look up standards like Netflix's "Loudness and True Peaks: How to Measure and When to Flag" and add guides for popular platforms such as YouTube and Netflix. Added a read-only **Loudness Check** window (integrated loudness, true peak, LRA, max short-term/momentary per track or for the ticked tracks together, a ✓/✗ verdict with what to do, the platform's guide with its source and whether the platform publishes it), true-peak mode for the limiter (default on), a Platform choice for the Loudness effect, and a delivery preset per platform. Targets: Netflix, Amazon Prime Video, Apple TV+, YouTube, Spotify, Apple Podcasts, EBU R128, ATSC A/85 (checked 2026-10-17; see CLAUDE.md for which are official).

**Verified:** 20 new tests (true peak at a quarter-rate sine and within 0.05 dB from 997 Hz to 19 kHz at 44.1/48/96 kHz, the EBU Tech 3342 LRA case, momentary and short-term maxima, two channels adding power, the gate-aware gain, verdicts for Netflix, EBU edges, YouTube and silence, the true-peak limiter's ceiling and timing, platform settings, every delivery preset passing its own check, measure_file each/together, the window and the hand-over to Effects). On a scratch copy of a real 3-track 24-bit take: Boom -29.5, Lav-1 -27.8, Lav-2 -29.9 LUFS (1 of 3 within Netflix) → Make Copies for Netflix → all three -27.0 LUFS, metadata kept. 313 tests in all.

**Fix Automatically** (asked next: "can you add a button to automatically make the changes?"): Loudness Check now makes the fixed copies itself after one confirmation (loudness to the target, true-peak limiter at the limit - 0.3 dB, `Processed/<name>_<Platform>.wav`), only for the files that failed when they were measured with the same ticks, and measures the copies right away. Tracks measured together get one gain, so a mix keeps its balance. The originals are never changed, so "automatically" means copies, as with every other process here; Undo deletes them. The copy writing in `effects_selected` became `MainWindow.write_effect_copies`, shared by both. *Make Copies for This Platform…* is now *Fix in Effects…*. **Verified:** 3 new tests (copy names, one gain for tracks together keeping their 10 dB difference and reaching -27.0, only failing files picked); an offscreen GUI run on a scratch library of two synthetic 2-track takes (all 4 tracks failing → 4 of 4 copies at -27.0 LUFS; then together → both copies -27.0, separate `_Netflix_2` names; two Undos left just the originals). 316 tests in all.

**Released as v1.8.0 for Linux** (2026-10-17), together with the v1.7.0 work (Reduce Noise, DeepFilterNet, VST2/VST3), which had not been released on its own.
