# Location Sound File Manager

A Linux desktop app for browsing a library of production sound recordings: WAV/BWF files from field recorders such as the Sound Devices 8-series and Zoom F-series. It reads the metadata the recorders embed, groups recordings by project, plays them with a per-channel waveform, and lets you rename, re-tag and reorganise them. Every change is previewed first and can be undone.

## First run

The first start opens a **Setup** window (available again later under *Settings ▸ Setup…*):
- **Your details** for sound reports: name, phone and email, which are all optional.
- **Branding:** logo, title, a line under the title, header colour and page footer. Phone and email can go at the top of the report, in the footer of every page, or both.
- **Folders:** the default *Copy to NAS* output folder and, if it's different, the library folder the Library page shows.

A live preview shows how your reports will look.

## Workflow: Offload Card

The **Offload Card** page (Ctrl+1) follows the day-after-the-shoot routine step by step:

1. **Card:** insert the SD card. It's detected, read, and its project/day folders are listed. Days not yet on the NAS are ticked. The recorder's own folders (SOUNDDEV, SETTINGS, TRASH…) are never copied, and FALSETAKES only if you tick it.
2. **Review & notes:** play takes and double-click (or press F2 on) a file name, scene, take or note to change it; click ★ to circle a take. Changes show in bold and stay *pending*: the card is never written to.
3. **Sound report:** *Sound Report…* fills in the header from the files plus your saved details (name, phone, email and tone level are remembered for every report; director, client and producer per project), with a live preview of the branded PDF. The default *Boxed* style is a bordered grid (Filename, Scene, Take, TC Start, Dur, a box per track) with a Notes row under each take; *List* puts the notes in a column. Choose the table columns (tick and drag to reorder) and Landscape or Portrait; the preview updates as you change them. Columns size themselves to their content, and on a crowded page the table text gets smaller rather than breaking headers.
4. **Copy to NAS:** copies into `<destination>/<project>/<day>/…`, the same layout as the rest of the library. The destination (*Into:*) defaults to the library folder; *Browse…* picks any other folder, and recent destinations are remembered. Each file is written under a temporary name, read back and compared with the card, and only then put in place. Nothing is ever overwritten, and files already there are skipped. Your pending changes are written into the copies, and a PDF + CSV sound report is saved for each project, in its NAS folder (or one per day folder, if you prefer). With several projects on the card, the report window steps through one report per project. You can rename the NAS folder for a project (double-click it).
5. **Send:** *Open Project Folder* opens the copied project in the file manager for uploading. *Show in Library* and *Eject Card…* are next to it.

## Features

- **Scan a library folder**, including network shares. The first scan reads each file's headers; after that, results are cached, so rescans only check sizes and dates. The window fills in while a scan runs.
- **Grouped by project.** The project comes from the file's iXML metadata. Files without one fall back to their folder name, shown in grey italics. The sidebar lists every project with its recording days.
- **Metadata columns:** scene, take, circled ★, start timecode (frame-accurate, including drop frame), length, channels, track names, format, frame rate, date/time, note, recorder, folder. Right-click the header to show or hide columns. Search matches names, scenes, takes, notes, track names and timecode.
- **Player:** selecting a file loads it paused. Nothing plays until you press Space or Play, or double-click. The waveform shows one lane per channel with its track name; click or drag it to seek. The timecode display follows the playhead. *Monitor* plays all channels mixed, or solos one channel to both speakers.
- **Rename** (F2): one file, or many with find & replace or a pattern, with a live preview. By default the name stored inside the WAV (iXML `CURRENT_FILENAME`, BWF `sFILENAME`) is updated too; the original recorded name is kept.
- **Edit metadata** (Ctrl+E): project, scene, take, tape, note and circled, for one or many files. With several files, only fields you tick (or type in) change. The take's other files (`_ISO` / `_LR`) can be included automatically. To merge duplicate project names, right-click a project in the sidebar and choose *Set Project Name*.
- **Reorganize into folders** (Ctrl+Shift+M): move files into a structure like `{project}/{day}`, previewed in full, with clashes listed. Optionally, folders left empty are removed. Only audio files move.
- **Undo** (Ctrl+Z) for renames, moves and metadata edits. Every change is also logged to `~/.local/share/location-sound-file-manager/history.jsonl`.
- **Sound reports** (Ctrl+R) from the Library too: for the selected files, or a project or day (right-click in the sidebar). Select several projects (Ctrl/Shift-click in the sidebar) to get **one report per project**, each with only that project's files. Step through them with **Previous / Next**, save them one at a time or with **Save All** (each goes into its project folder). Report windows aren't modal, so several can be open at once.
- **Report branding** (Settings ▸ Report Branding…, or *Branding…* in the report window): your own logo (or the built-in one, or none) and its size, the report title, a line under the title, the table header colour and a page footer, with a live preview. A chosen logo is copied into the app's data folder, so it keeps working if the original moves.
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
| F2 | Rename (Library) / edit the cell (Offload) |
| Ctrl+E | Edit metadata |
| Ctrl+Shift+M | Reorganize into folders |
| Ctrl+Z | Undo |
| Ctrl+F | Search |
| F5 | Rescan |
| Ctrl+O | Choose library folder |
| Ctrl+1 / Ctrl+2 | Offload Card / Library page |
| Ctrl+R | Sound report (Library) |
| Ctrl+Shift+O | Open project folder (Library) |

## Install and run

Linux only (developed on CachyOS / KDE Plasma, Wayland). Needs Python 3.11+, PySide6 (Qt 6), libmpv with python-mpv, and numpy. On Arch/CachyOS:

```sh
sudo pacman -S pyside6 mpv python-mpv python-numpy
```

Then:

```sh
git clone https://github.com/cooldead/location-sound-file-manager
cd location-sound-file-manager
./install.sh               # adds it to the application menu
./run.sh [library-folder]  # or start it directly
python3 -m unittest        # tests
```

Card detection uses `lsblk`, and ejecting uses `udisksctl` (both are standard on desktop Linux).

## License

MIT
