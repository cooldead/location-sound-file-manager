"""App settings: one place for every setting's key and default."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from . import compat

DEFAULTS: dict[str, object] = {
    "setup_done": False,             # the first-run Setup window was shown
    "library_folder": "",            # the folder that is scanned ("" = ask on first start)
    # Folders directly below the library that hold cards/backups rather than one
    # project each; skipped when a file has no project in its metadata.
    "container_folders": ["SD_1", "SD_2", "833 BACK UPS"],
    "write_embedded_filename": True,  # renames also update the name stored inside the WAV
    "apply_to_take_family": True,     # metadata edits include the take's other files (_ISO/_LR)
    "rename_on_scene_take": True,     # editing scene or take in the Library renames the file to match
    "organize_pattern": "{project}/{day}",
    "organize_remove_empty": True,
    "split_keep_original": True,      # splitting moves the original into the take folder (else: removed folder)
    "safety_mode": "safe",            # "dangerous": split/combine keep permanent delete as a choice, no confirmation
    "combine_sources": "keep",        # after combining, the files: "keep", "remove" (removed folder), "delete"
    "split_delete_permanently": False,  # originals that leave a split are deleted instead of moved away
    "split_shrink_original": False,   # tracks not split off: the original is shrunk to them (else kept whole)
    "confirm_undo": True,
    "waveform_view": "overlay",       # "overlay" (all tracks in one lane) or "lanes"
    "waveform_scale": "db",           # "db" or "linear"
    "waveform_detailed": True,        # refine the selected file after a quick preview
    "waveform_collapsed": False,
    "mixer_master_db": 0.0,
    "mixer_exclusive_solo": False,
    "mixer_auto_trim": True,          # lower the mix by 1/sqrt(n) when n tracks play
    "mixer_folder": "",               # where mixer settings / automation are saved
    "mixer_collapsed": False,
    "library_counts": "projects",     # sidebar numbers: "projects", "files" or "both"
    # An index in the library (<library>/.sfm-index) so other computers don't
    # read every file again: use it (read) and update it (write), for file
    # metadata and for waveforms (large) separately.
    "library_index_read": True,
    "library_index": False,              # update the file metadata
    "library_index_waveforms_read": True,
    "library_index_waveforms": False,    # update the waveforms
    "library_grouping": "date",       # Library sidebar: "date" (year > month > project) or "name"
    "card_working_folder": "",       # default: local application cache / card-work
    # Offload
    "verify_copies": True,           # read each copy back and compare with the card
    "include_false_takes": False,    # also copy the recorder's FALSETAKES folder
    "report_on_export": True,        # save a sound report (PDF + CSV) in each copied day folder
    "report_personal": "{}",         # JSON: the mixer's own report fields (name, phone, email, tone)
    "report_projects": "{}",
    "report_orientation": "landscape",
    "report_style": "boxed",
    "report_branding": "{}",         # JSON: report.Branding fields (logo, title, company, accent, footer)
    "report_per": "project",         # Offload: one sound report per "project" or per "day" folder
    "report_columns": ["file", "scene", "take", "start_tc", "length", "track_columns", "notes"],         # JSON: per project name, {director, client, producer}
    "offload_folder_names": "{}",
    "offload_destinations": [],      # Copy to NAS destinations, most recent first ("" / none = library folder)    # JSON: card folder name -> NAS folder name chosen last time
}


# Settings that hold folders; on Windows a typed "C:\..." is read back with "/".
PATH_KEYS = {"card_working_folder", "library_folder", "mixer_folder", "offload_destinations"}


def get(settings: QSettings, key: str):
    default = DEFAULTS[key]
    if isinstance(default, list):
        value = settings.value(key, default)
        if isinstance(value, str):  # QSettings returns a 1-item list as a string
            value = [value]
        value = [str(v) for v in value] if value else []
        return [compat.fwd(v) for v in value] if key in PATH_KEYS else value
    value = settings.value(key, default, type(default))
    return compat.fwd(value) if key in PATH_KEYS else value


def put(settings: QSettings, key: str, value) -> None:
    settings.setValue(key, value)


def get_json(settings: QSettings, key: str) -> dict:
    try:
        value = json.loads(get(settings, key))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def put_json(settings: QSettings, key: str, value: dict) -> None:
    put(settings, key, json.dumps(value, ensure_ascii=False))


APP_DIR = "location-sound-file-manager"
OLD_APP_DIR = "tp-sound-file-manager"  # the app's folder name before it was renamed


def migrate_old_folders() -> list[str]:
    """Move settings, the scan cache, history and saved logos from the old
    folder name to the new one (once, only when the new one doesn't exist).
    Returns what was moved."""
    moved = []
    for location in (QStandardPaths.StandardLocation.GenericConfigLocation,
                     QStandardPaths.StandardLocation.GenericCacheLocation,
                     QStandardPaths.StandardLocation.GenericDataLocation):
        base = Path(QStandardPaths.writableLocation(location))
        old, new = base / OLD_APP_DIR, base / APP_DIR
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
                moved.append(str(new))
            except OSError:
                continue
    # Saved paths (e.g. a copied logo) still point at the old data folder.
    config = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation))
    ini = config / APP_DIR / "settings.ini"
    if ini.is_file():
        text = ini.read_text(encoding="utf-8", errors="surrogateescape")
        fixed = text.replace(f"/{OLD_APP_DIR}/", f"/{APP_DIR}/")
        if fixed != text:
            ini.write_text(fixed, encoding="utf-8", errors="surrogateescape")
    return moved


def open_settings() -> QSettings:
    """~/.config/location-sound-file-manager/settings.ini (macOS: in
    ~/Library/Application Support/location-sound-file-manager/, next to the
    history, where Mac apps keep their files; Qt would use ~/.config there too.
    Windows: %LOCALAPPDATA%\\location-sound-file-manager\\, also next to the
    history; this way both follow QStandardPaths' test mode)."""
    if sys.platform in ("darwin", "win32"):
        return QSettings(str(history_path().parent / "settings.ini"), QSettings.Format.IniFormat)
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, APP_DIR, "settings")


def branding_dir() -> Path:
    """~/.local/share/location-sound-file-manager/branding: copies of chosen logos."""
    return history_path().parent / "branding"


def cache_path() -> Path:
    """~/.cache/location-sound-file-manager/catalog.sqlite: scanned metadata and waveforms."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericCacheLocation)
    return Path(base) / APP_DIR / "catalog.sqlite"


def history_path() -> Path:
    """~/.local/share/location-sound-file-manager/history.jsonl: every change the app made to files."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(base) / APP_DIR / "history.jsonl"


def markers_path() -> Path:
    """~/.local/share/location-sound-file-manager/markers.sqlite: waveform markers per file."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    return Path(base) / APP_DIR / "markers.sqlite"
