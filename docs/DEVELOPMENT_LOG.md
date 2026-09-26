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
