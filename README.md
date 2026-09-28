# Location Sound File Manager

A desktop app for Linux, macOS and Windows for browsing a library of production sound recordings: WAV/BWF files from field recorders such as the Sound Devices 8-series and Zoom F-series. It reads the metadata the recorders embed, groups recordings by project, plays them with a per-channel waveform, and lets you rename, re-tag and reorganise them. Every change is previewed first and can be undone.

## Download

| Platform | Release | Download |
|---|---|---|
| **Linux**: Arch, CachyOS, Manjaro | [v1.6.0 for Linux](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.6.0-linux) | `location-sound-file-manager-1.6.0-1-any.pkg.tar.zst`: install with `sudo pacman -U location-sound-file-manager-1.6.0-1-any.pkg.tar.zst` (pulls in PySide6, Qt Multimedia and numpy). Adds the app to the menu; remove it with `sudo pacman -R location-sound-file-manager`. |
| **Linux**: any distribution | [v1.6.0 for Linux](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.6.0-linux) | `Location-Sound-File-Manager-1.6.0-x86_64.AppImage`: everything inside (Python, Qt, numpy), nothing to install. Make it executable (`chmod +x`, or Properties ▸ Permissions) and open it. Needs a distribution from 2022 or later (glibc 2.35+). |
| **Linux**: from source | [v1.6.0 for Linux](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.6.0-linux) | `Location-Sound-File-Manager-1.6.0-linux.tar.gz`: extract it, install the packages below, then run `./install.sh` in the extracted folder ([details](#linux)). Or clone the repository. |
| **macOS** (Apple silicon) | [v1.5.0 for macOS](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.5.0-macos) | `Location-Sound-File-Manager-1.5.0-macOS-arm64.zip`: extract it and move **Location Sound File Manager.app** to Applications. Python, Qt and numpy are inside. It is ad-hoc signed, not notarized: macOS may ask you to allow it in System Settings ▸ Privacy & Security the first time. |
| **Windows** 10 and 11 (64-bit) | [v1.4.0 for Windows](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.4.0-windows) | `Location-Sound-File-Manager-1.4.0-windows-x64.zip`: extract it anywhere and open **Location Sound File Manager.exe** in the extracted folder. Python, Qt and numpy are inside. It is not code-signed: Windows SmartScreen may warn the first time (More info ▸ Run anyway). |

The Linux, macOS and Windows releases are built separately from the shared source. The Linux packages are at v1.6.0, the macOS app at v1.5.0, and the Windows app at v1.4.0. Platform builds are published separately from the shared source. You can also build the Mac or Windows app from source ([macOS](#macos), [Windows](#windows)). All releases: [github.com/cooldead/location-sound-file-manager/releases](https://github.com/cooldead/location-sound-file-manager/releases).


## First run

For source builds, the optional Rust waveform core speeds up peak/RMS processing. Build it with `python3 scripts/build_native.py` after installing Rust/Cargo. See [performance and build notes](docs/PERFORMANCE.md) for benchmarks, fallback behavior and platform packaging.

The first start opens a **Setup** window (available again later under *Settings ▸ Setup…*):
- **Your details** for sound reports: name, phone and email, which are all optional.
- **Branding:** logo, title, a line under the title, header colour and page footer. Phone and email can go at the top of the report, in the footer of every page, or both.
- **Folders:** the default *Copy to NAS* output folder and, if it's different, the library folder the Library page shows.

A live preview shows how your reports will look.

## Workflow: Offload Card

The **Offload Card** page (Ctrl+1) follows the day-after-the-shoot routine step by step:

1. **Card:** insert the SD card. The app makes a temporary local copy with file-size and source-change checks before listing its project/day folders. Review and playback use that copy; the card stays protected from app writes. Use **Working Copy Folder…** to choose a local disk with enough free space. Days not yet on storage are checked. The recorder's own folders (SOUNDDEV, SETTINGS, TRASH…) are never copied, and FALSETAKES only if you check it.
   - **Already stored:** a background check verifies all recordings and accompanying files, including unticked days and false takes. A notice appears only when every eligible file matches in full; recorder/system files are excluded. Changed WAV metadata prevents a complete match. Temporary copies are removed when the card session closes. [Details and working notes](docs/CARD_WORKSPACE.md).
   - **Projects already in the library:** when a card folder looks like a project the library already has in another folder (the same name written differently, e.g. `NIGHT SHIFT` / `Night Shift`, or a similar name, compared by folder name and by the project in the files' metadata), a window says so, with how many of the card's recordings are already in the library and how many are new. Choose per folder: **copy into the existing project folder** (merge; the default when there is something new), copy into a new folder, or don't copy it (the default when everything is already there). The choice is remembered for that card folder name. Right-click a card folder ▸ *Find This Project in the Library…* to ask again. Days whose recordings are all already in the library in another folder show *in library (elsewhere)* and are not checked, so a card offloaded twice doesn't make duplicates.
2. **Review & notes:** play takes and double-click (or press F2 on) a file name, scene, take or note to change it; click ★ to circle a take. Changes show in bold and stay *pending*: the card is never written to.
3. **Sound report:** *Sound Report…* fills in the header from the files plus your saved details (name, phone, email and tone level are remembered for every report; director, client and producer per project), with a live preview of the branded PDF. The default *Boxed* style is a bordered grid (Filename, Scene, Take, TC Start, Dur, a box per track) with a Notes row under each take; *List* puts the notes in a column. Choose the table columns (check and drag to reorder) and Landscape or Portrait; the preview updates as you change them. Columns size themselves to their content, and on a crowded page the table text gets smaller rather than breaking headers.
4. **Copy to Storage:** copies into `<destination>/<project>/<day>/…`, the same layout as the rest of the library. The destination (*Into:*) defaults to the library folder; *Browse…* picks any other folder, and recent destinations are remembered. Each file is written under a temporary name, read back and compared with the local working copy, and only then put in place. Nothing is ever overwritten, and files already there are skipped. Your pending changes are written into the copies, and a PDF + CSV sound report is saved for each project, in its storage folder (or one per day folder, if you prefer). With several projects on the card, the report window steps through one report per project. You can rename storage folder for a project (double-click it).
5. **Send:** *Open Project Folder* opens the copied project in the file manager for uploading. *Show in Library* and *Eject Card…* are next to it.

## Features

- **Scan a library folder**, including network shares. The first scan reads each file's headers; after that, results are cached, so rescans only check sizes and dates. The window fills in while a scan runs.
- **Library index** (Settings ▸ Library index): an index in a hidden `.sfm-index` folder in the library, shared by every computer that opens it, so a computer that hasn't cached a file yet (another computer, or this one after its cache is cleared) doesn't open it on the share. Two options each for **file metadata** and **waveforms**: **Use** (on by default; reads the index, changes nothing, and an entry is only trusted while its file's size and date are unchanged) and **Update** (off by default; writes to the library: file metadata after each complete scan, a few MB; waveforms as each file is first drawn, about 16 KB per track per file, several hundred MB for a large library, with the estimate and free space shown before it is turned on). Settings shows whether the library already has an index.
- **Fast rescans on a network share:** folders are listed and files checked many at a time, because each check mostly waits for the network (Wi-Fi to a NAS: 16,700 files in about 70 s instead of 270 s). If a folder can't be read during a scan (the share dropped for a moment), its files are kept, not forgotten.
- **Grouped by project.** The project comes from the file's iXML metadata. Files without one fall back to their folder name, shown in grey italics. The sidebar lists projects **by year, then month** (newest first; only months with recordings), then the projects recorded that month and their days. Click a year or month to see all its recordings; right-click it for one sound report per project. The numbers count **projects** by default; the **#** button next to the box switches to counting files, or both (tooltips always show both). The box above the sidebar switches to an A–Z project list, or **by recorder**: each recorder (e.g. *Sound Devices 833*, *ZOOM F8*; serial numbers left out), then the projects it recorded and their days.
- **Metadata columns:** scene, take, circled ★, start timecode (frame-accurate, including drop frame), length, channels, track names, format, frame rate, date/time, note, recorder, folder. Right-click the header to show or hide columns. Search matches names, scenes, takes, notes, track names and timecode.
- **Player:** selecting a file loads it paused. Nothing plays until you press Space or Play, or double-click. Transport: play/pause, stop, loop (L), previous/next file, a position counter and the timecode at the playhead.
- **Waveform** in colour, one colour per track (the same as its mixer strip): all tracks in one lane (*Combined*, loudest behind so every track stays visible) or one lane each (*Lanes*), on a dB or linear scale. It shows the peak envelope and a brighter RMS body, anti-aliased. Tracks you can't hear (muted or soloed out) turn grey. Click to seek; drag to select a region (drag its edges to change it; double-click or Esc clears it); with loop on, the region (or the whole file) repeats.
- **Detailed overview:** the selected recording shows a quick or cached preview, then sharpens in the background as the full audio is read. The result is cached with 16-bit amplitude precision. Changing selection cancels refinement; adjacent-file prefetch keeps limited reads. Right-click the waveform and untick *Detailed Overview* to limit large-file reads. Old waveform caches upgrade when selected. [Implementation and validation notes](docs/WAVEFORM_SHARPNESS.md).
- **Zoom:** the mouse wheel zooms around the pointer (Shift+wheel scrolls), or use − / + / Fit / *zoom to region* in the header (or − + 0 with the waveform clicked). Zoomed in, the visible part is read again from the file at one point per pixel, so it stays sharp down to single samples, with a time ruler and a scrollbar; while playing, the view follows the playhead. ▲ / ▼ zoom the height.
- **Markers:** press M (or ◆+) to drop a marker at the playhead, or right-click *Add Marker Here*. Drag a marker to move it, double-click it to name it, select it and press Delete to remove it; , and . (or ◀◆ / ◆▶) jump to the previous / next marker. Markers are kept in the app's data folder (the WAVs are not changed) and follow the file when the app renames or moves it. Cue markers a recorder wrote into the file are shown in blue (read only).
- **32-bit float recordings** (e.g. from recorders with 32-bit float capture) play, draw and meter correctly. Peaks above 0 dBFS, which only float files can hold, are marked red on the waveform; pull the fader down to hear them without clipping (nothing is lost in the file).
- **Channel mixer** at the bottom: a strip per track with its colour and name, mute and solo, a peak meter with hold and clip light, a fader (−∞ to +12 dB; double-click for 0 dB), pan, and a link button that pairs a track with the next one as a stereo pair (faders, mutes and solos together, panned left/right; `_LR` files and L/R track pairs are linked automatically). A master strip has a stereo meter, fader and mute. *Solo Mode* (add to the solo, or exclusive), *Mute All*, *Arm All*, *Clear All Automation*. **Automation:** arm a track (A) and play, and its fader, pan and mute moves are recorded; unarmed, they play back and the fader follows (✕ clears a track's automation). *Save / Load Automation* keeps the mixer settings and automation in a `.mix.json` file. The mix is remembered per file for the session; the next file with the same tracks starts with the same fader settings. With several tracks playing, the mix is lowered by 1/√n so a poly file doesn't clip (*More* ▸ switch off). The waveform and mixer sections can each be collapsed.
- **Edit in the table** (Library): double-click **File**, **Scene**, **Take** or **Note** to change it in place, or double-click **★** to circle (or un-circle) a take. Changes are written into the file straight away and can be undone (Ctrl+Z). Changing the scene or take also **renames the file to match**: scene 8M, take 01 gives `8MT01` (a name already built from scene and take keeps its style, e.g. `8M-T01`), keeping endings such as `_ISO` / `_LR` and anything else around the scene and take (`2BT001_BOOM` → `2BT002_BOOM`), so a take's files keep distinct names (Settings can turn this off). The take's other files (`_ISO` / `_LR`) get the same change and are renamed with it. A file without metadata gets it added (the app asks first, since that copies the file once; it leaves room so later edits don't).
- **Notes box** under the inspector: type notes for the selected take and press *Save Note* (or Ctrl+Enter); they go into the file (and its `_ISO` / `_LR` partner). Selecting another file with an unsaved note asks whether to save it.
- **Rename** (F2): one file, or many with find & replace or a pattern, with a live preview. By default the name stored inside the WAV (iXML `CURRENT_FILENAME`, BWF `sFILENAME`) is updated too; the original recorded name is kept.
- **Edit metadata** (Ctrl+E): project, scene, take, tape, note and circled, for one or many files. With several files, only fields you check (or type in) change. The take's other files (`_ISO` / `_LR`) can be included automatically. To merge duplicate project names, right-click a project in the sidebar and choose *Set Project Name*.
- **Reorganize into folders** (Ctrl+Shift+M): move files into a structure like `{project}/{day}`, previewed in full, with clashes listed. Optionally, folders left empty are removed. Only audio files move.
- **Split into track files** (right-click in the Library): a multi-track file becomes one mono file per track, named after its track (`Boom.wav`, `Lav 1.wav`; unnamed tracks become `Track 3.wav`), in a folder named after the take (`8MT01/`) next to it. The new files keep the scene, take, notes and timecode, and stay one take (same family id). The take's other files (e.g. the `_LR`) move into the folder with them. Each track has a tick: only ticked tracks are split off. Select several tracks and click *Group into One File…* to keep them together in one polywav (named e.g. `LAV1+LAV2.wav`, or a name you type) instead of a mono file each. The tracks you leave unticked stay with the original, which either keeps all its tracks (it moves into the folder unchanged) or is **shrunk to just those tracks** (same name and metadata, in the folder; the full file goes to `_Removed Duplicates`). When every track is split off, *Keep the original file* moves the original into that folder too; otherwise it goes to `_Removed Duplicates`, from where it can be put back or deleted for good. Originals that leave can also be **deleted permanently** instead (asked to confirm; only after the new files are written and checked; no undo for that split). Editing the scene or take of a track file doesn't rename it. Undo removes the track files and puts everything back.
- **Combine into polywav** (right-click several files of one take, e.g. split mono files): their tracks go into one polywav in the order you choose (drag or Move Up/Down), keeping the first file's metadata and every file's track names. The files must line up: same length, sample rate, bit depth and start timecode. The name suggested is the scene and take (`2B-T001.WAV`, in the recorder's style, or `<scene>T<take>`); when that's taken, e.g. by the original in the same folder, the files' names are added (`2B-T001_BOOM+BOOMSAFE.WAV`). Afterwards the files are kept, moved to `_Removed Duplicates` (undoable), or deleted permanently (asked to confirm, after the new file is checked; no undo).
- **Safe / Dangerous mode** (Settings): in safe mode (the default) the Split and Combine windows always start with moving replaced files to `_Removed Duplicates`, and deleting permanently asks each time. Dangerous mode keeps your last choices, including deleting permanently, and deletes without asking; the windows then show a red *Dangerous mode* line.
- **Undo** (Ctrl+Z) for renames, moves, splits, combines and metadata edits. Every change is also logged to `~/.local/share/location-sound-file-manager/history.jsonl`.
- **Sound reports** (Ctrl+R) from the Library too: for the selected files, or a project or day (right-click in the sidebar). Select several projects (Ctrl/Shift-click in the sidebar) to get **one report per project**, each with only that project's files. Step through them with **Previous / Next**, save them one at a time or with **Save All** (each goes into its project folder). Report windows aren't modal, so several can be open at once.
- **Report branding** (Settings ▸ Report Branding…, or *Branding…* in the report window): your own logo (or the built-in one, or none) and its size, the report title, a line under the title, the table header colour and a page footer, with a live preview. A chosen logo is copied into the app's data folder, so it keeps working if the original moves.
- **Find Duplicates** (Ctrl+D) in the Library:
  - **Duplicate files:** recordings that exist more than once, e.g. a card copied into two places. Copies are matched by the recording itself (length, format, start timecode, plus an audio fingerprint), so an `_ISO` and an `_LR` of the same take are never duplicates of each other, and a take's `_ISO` and `_LR` are kept together in one folder. Identical copies are checked for removal; copies whose name or metadata differs are shown with the differences and left for you to decide. You choose which copy to keep.
  - **Duplicate projects:** the same project name written differently (`NIGHT SHIFT` / `NIGHTSHIFT`), similar names (a typo, or an added word), and folders with the same name in several places. Numbered days such as `DAY 2` / `DAY 4` are not duplicates. **Merge** into the folder and name you keep, or into a **new folder** (a name is suggested), with a preview. **Batch merge:** check several groups (or a whole category, e.g. all "same folder" groups) and *Merge Checked*: each group gets its suggested keep folder, or optionally a new folder named after it, and its own progress bar and result; empty folders are removed, the whole batch is one undo step, and a report lists anything that failed. Files already there are removed, different ones are moved in (keeping their place below the folder), and the project name is written into the files.
  - **Safety:** before anything is removed it is compared byte for byte with the copy that is kept. Files that are not the same file (other notes or names, or a different layout) are, as you choose, copied or moved next to the kept copy as `name_ReviewForDeletion.wav`, or left where they are. Removed files go to a `_Removed Duplicates` folder in the library, sorted into a folder per original project with their original path kept (undoable, and you delete that folder yourself when you're sure), or are deleted permanently if you choose so.
  - **Versions with more tracks are preferred:** when a merge finds the same take (same name, length and timecode) with a different number of tracks, e.g. a Zoom file with and without its TrL/TrR mix, the version with more tracks is kept, but only after checking that every track of the smaller file is in it sample for sample; the smaller file goes to `_Removed Duplicates`.
  - **Versions with notes are preferred:** when another copy of the same file has a note (or a circle) that the kept copy lacks, it takes the kept copy's place (same folder, same name) after an audio check; the copy without notes goes to `_Removed Duplicates`.
  - **_ISO and _LR files** of a take are always kept together in one folder and are never renamed or given review copies: merges move both into the kept project folder, a take is never split (if one file has to stay, its partner stays with it), and only an exact repeat of the same file (e.g. `101AT01_LR` in two folders) is removed, whatever else differs.
  - **Nested folders** (third tab): a folder inside a folder with the same name, e.g. `Project/250101/250101` (a day folder copied into itself), listed with how many of its files the outer folder already has, how many are new, and how many are in the way. **Merge** (or check several and *Merge Checked*, one undo step) moves the inner files up one level so the structure stays `Project/Day/files`: files already there are removed after a byte-for-byte check, a copy with notes takes the outer copy's place, a different file with the same name stays where it is, and the emptied inner folder is removed. When folders are nested three deep, the deepest pair is offered first.
  - **Compare Marked Files** (in the Find Duplicates window): every `_ReviewForDeletion` file next to its closest copy, with the differences highlighted, play buttons for both, a byte-for-byte audio comparison, and *Keep Marked Copy Instead*. Check the marked files to delete (*Delete Checked…*), or *Delete All Marked Files…*, which (like *Delete Everything…* in `_Removed Duplicates`) shows a strong warning and only goes ahead when you type DELETE.
  - **Delete Empty Folders** (in the Find Duplicates window): finds every folder in the library, at any depth, that holds nothing but the recorders' marker files (`.take_folder`, `.daily_folder`, `.DS_Store`), lists them, and deletes them when you confirm (each one is checked again first; undoable). Merges and *Delete for good* also remove folders they leave empty, including empty subfolders.
  - **Review & Delete** (in the Find Duplicates window): see what is in `_Removed Duplicates` (by project) and every file marked `_ReviewForDeletion`, play them, put removed files back where they were, or delete them for good.
- **Open Project Folder** (Ctrl+Shift+O) for a project in the Library.
- **Export** the current list as CSV (Ctrl+Shift+E).

## Safety

- Metadata is written **in place** when it fits in the space the recorder reserved, which is almost always. Every write is read back and checked, and the audio data is never touched.
- If a file would need a **full rewrite** (copying the whole file), the app lists those files and their total size and asks first. The copy is checked before it replaces the original.
- Files whose audio data is incomplete (recordings cut short) are never written to.
- Renames are all-or-nothing: if one fails, the whole batch is put back.

## Keyboard

| Key | Action |
|---|---|
| Space | Play / pause |
| ← / → | Back / forward 5 s |
| Ctrl+. | Stop (back to start) |
| L | Loop on / off |
| M | Add a marker at the playhead |
| , / . | Previous / next marker |
| wheel, − + 0 | Zoom the waveform (Shift+wheel scrolls) |
| F2 | Rename (Library) / edit the cell (Offload) |
| Ctrl+E | Edit metadata |
| Ctrl+Shift+M | Reorganize into folders |
| Ctrl+D | Find duplicates (Library) |
| Ctrl+Z | Undo |
| Ctrl+F | Search |
| F5 | Rescan |
| Ctrl+O | Choose library folder |
| Ctrl+1 / Ctrl+2 | Offload Card / Library page |
| Ctrl+R | Sound report (Library) |
| Ctrl+Shift+O | Open project folder (Library) |

## Install and run

### Linux

Developed on CachyOS / KDE Plasma, Wayland. Needs Python 3.11+, PySide6 (Qt 6) with Qt Multimedia, and numpy. On Arch/CachyOS:

```sh
sudo pacman -S pyside6 qt6-multimedia qt6-multimedia-ffmpeg python-numpy
```

The easiest way on Arch-based systems is the **Arch package** from the [Linux release](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.6.0-linux) (`sudo pacman -U …pkg.tar.zst`); on other distributions, the **AppImage**. If you used `./install.sh` before, delete `~/.local/share/applications/location-sound-file-manager.desktop` so the menu opens the installed version.

To run it from the source instead, get the app, either the [Linux release](https://github.com/cooldead/location-sound-file-manager/releases/tag/v1.6.0-linux):

```sh
tar xzf Location-Sound-File-Manager-1.6.0-linux.tar.gz
cd location-sound-file-manager-1.6.0

./install.sh               # adds it to the application menu
./run.sh [library-folder]  # or start it directly
```

or the source, which `git pull` keeps up to date:

```sh
git clone https://github.com/cooldead/location-sound-file-manager
cd location-sound-file-manager
./install.sh               # adds it to the application menu
./run.sh [library-folder]  # or start it directly
python3 -m unittest        # tests
```

Card detection uses `lsblk`, and ejecting uses `udisksctl` (both are standard on desktop Linux).

**Building the packages:** `linux/build_packages.sh [X.Y.Z]` makes the tarball, the Arch package (with `makepkg`) and an AppImage (PyInstaller in a private venv under `build/`) in `dist/linux/`. An AppImage only runs on distributions whose glibc is at least as new as the build machine's, so the release AppImage is built on Ubuntu 22.04 by GitHub Actions (`.github/workflows/linux-packages.yml`) when a `vX.Y.Z-linux` release is published.

### macOS

Needs macOS 12 or later and Python 3.11+ (for example `brew install python`). To build a self-contained app (Python, Qt and numpy inside, about 130 MB):

```sh
git clone https://github.com/cooldead/location-sound-file-manager
cd location-sound-file-manager
macos/build_app.sh --install   # builds dist/Location Sound File Manager.app and copies it to ~/Applications
```

The script makes a private `.venv` in the project folder; `./run.sh` uses it too, so you can also start the app from the folder, and `.venv/bin/python -m unittest` runs the tests. The first time the app reads a card or a network share, macOS asks for permission.

Cards are found with `diskutil` (volumes on removable media such as SD slots and readers). Ejecting ejects the whole card. Shortcuts use ⌘ where Linux uses Ctrl. Settings, history and markers are kept in `~/Library/Application Support/location-sound-file-manager/`, and the scan cache in `~/Library/Caches/location-sound-file-manager/`.

### Windows

Needs Windows 10 or 11 (64-bit) and Python 3.11+ (from python.org, or `winget install Python.Python.3.13`). To build a self-contained app (Python, Qt and numpy inside, about 170 MB, a 70 MB zip), in PowerShell:

```powershell
git clone https://github.com/cooldead/location-sound-file-manager
cd location-sound-file-manager
powershell -ExecutionPolicy Bypass -File windows\build_app.ps1 -Install
```

This builds `dist\Location Sound File Manager\Location Sound File Manager.exe` and a zip next to it. `-Install` also copies it to `%LOCALAPPDATA%\Programs\` and adds a Start menu shortcut. The script makes a private `.venv` in the project folder; `run.bat [library]` uses it too, so you can also start the app from the folder, and `.venv\Scripts\python -m unittest` runs the tests.

A network library can be a mapped drive (`Z:\Sound`) or a UNC path (`\\nas\share\Sound`). Cards are the drives Windows reports as removable (SD slots and readers, recorders in USB mode). Ejecting dismounts the card like *Safely Remove*. Settings, history and markers are kept in `%LOCALAPPDATA%\location-sound-file-manager\`, and the scan cache in `%LOCALAPPDATA%\cache\location-sound-file-manager\`. Names with `\ : * ? " < > |` can't be used on Windows, so renames refuse them.

## License

MIT
