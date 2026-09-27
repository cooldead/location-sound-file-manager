"""Dialogs. Each one only collects choices and shows a preview; the main window
applies the result (so every change goes through one place, with undo)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from html import escape as escape_html
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QMenu, QPlainTextEdit, QPushButton, QRadioButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import compat, library_index, organize, settings, splitter, waveform
from .catalog import REMOVED_FOLDER
from .catalog import Recording
from .renamer import validate_name

ERROR_COLOR = QColor("#d13438")
MAX_PREVIEW_ROWS = 5000


def _buttons(dialog: QDialog, ok_text: str) -> tuple[QDialogButtonBox, QPushButton]:
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok = box.button(QDialogButtonBox.StandardButton.Ok)
    ok.setText(ok_text)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    return box, ok


def _tokens_help() -> str:
    return "Tokens: " + ", ".join(f"{{{name}}}" for name in organize.TOKENS)


def _tokens_tooltip() -> str:
    return "\n".join(f"{{{name}}}  {text}" for name, text in organize.TOKENS.items())


# ---------------------------------------------------------------- rename

class RenameDialog(QDialog):
    """Rename one file (the extension is kept)."""

    def __init__(self, rec: Recording, write_embedded: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename")
        self.rec = rec
        stem, self.ext = os.path.splitext(rec.name)
        self.edit = QLineEdit(stem)
        self.edit.selectAll()
        self.error = QLabel()
        self.error.setStyleSheet(f"color: {ERROR_COLOR.name()}")
        self.embedded = QCheckBox("Also update the file name stored inside the WAV (iXML / BWF)")
        self.embedded.setChecked(write_embedded)
        self.embedded.setEnabled(rec.has_bext or rec.has_ixml)
        box, self.ok = _buttons(self, "Rename")
        row = QHBoxLayout()
        row.addWidget(self.edit, 1)
        row.addWidget(QLabel(self.ext))
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"New name for <b>{rec.name}</b>:"))
        layout.addLayout(row)
        layout.addWidget(self.error)
        layout.addWidget(self.embedded)
        if rec.original_filename and rec.original_filename != rec.name:
            layout.addWidget(QLabel(f"Recorded as: {rec.original_filename}"))
        layout.addWidget(box)
        self.edit.textChanged.connect(self._check)
        self.resize(520, self.sizeHint().height())
        self._check()

    def new_name(self) -> str:
        return self.edit.text().strip() + self.ext

    def _check(self):
        name = self.new_name()
        message = validate_name(name) if self.edit.text().strip() else "name is empty"
        if not message and name != self.rec.name and os.path.lexists(compat.join(self.rec.folder, name)) \
                and name.casefold() != self.rec.name.casefold():
            message = f"'{name}' already exists"
        self.error.setText(message or "")
        self.ok.setEnabled(not message and name != self.rec.name)


class BatchRenameDialog(QDialog):
    """Find & replace or a pattern over many file names, with a live preview."""

    def __init__(self, recs: list[Recording], write_embedded: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Rename {len(recs)} files")
        self.recs = recs
        self.moves: list[organize.Move] = []

        self.find_mode = QRadioButton("Find and replace")
        self.pattern_mode = QRadioButton("Pattern")
        self.find_mode.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.find_mode)
        group.addButton(self.pattern_mode)

        self.find = QLineEdit()
        self.replace = QLineEdit()
        self.regex = QCheckBox("Regular expression")
        self.case = QCheckBox("Match case")
        self.case.setChecked(True)
        self.pattern = QLineEdit("{scene}T{take}_{name}")
        self.pattern.setToolTip(_tokens_tooltip())
        self.embedded = QCheckBox("Also update the file names stored inside the WAVs (iXML / BWF)")
        self.embedded.setChecked(write_embedded)

        grid = QGridLayout()
        grid.addWidget(self.find_mode, 0, 0, 1, 4)
        grid.addWidget(QLabel("Find:"), 1, 0)
        grid.addWidget(self.find, 1, 1)
        grid.addWidget(QLabel("Replace with:"), 1, 2)
        grid.addWidget(self.replace, 1, 3)
        options = QHBoxLayout()
        options.addWidget(self.regex)
        options.addWidget(self.case)
        options.addStretch(1)
        grid.addLayout(options, 2, 1, 1, 3)
        grid.addWidget(self.pattern_mode, 3, 0, 1, 4)
        grid.addWidget(QLabel("Name:"), 4, 0)
        grid.addWidget(self.pattern, 4, 1, 1, 3)
        hint = QLabel(_tokens_help() + ". The extension is kept.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        grid.addWidget(hint, 5, 1, 1, 3)

        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["Current name", "New name", ""])
        self.preview.setRootIsDecorated(False)
        self.preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.summary = QLabel()
        box, self.ok = _buttons(self, "Rename")

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.embedded)
        layout.addWidget(box)
        self.resize(900, 600)

        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._update)
        for widget in (self.find, self.replace, self.pattern):
            widget.textChanged.connect(self._timer.start)
        for widget in (self.regex, self.case, self.find_mode, self.pattern_mode):
            widget.toggled.connect(self._timer.start)
        self._update()

    def _update(self):
        names, error = [], ""
        for number, rec in enumerate(self.recs, 1):
            try:
                if self.pattern_mode.isChecked():
                    names.append(organize.renamed(rec.name, pattern=self.pattern.text(), rec=rec, number=number))
                else:
                    names.append(organize.renamed(rec.name, find=self.find.text(), replace=self.replace.text(),
                                                  regex=self.regex.isChecked(),
                                                  case_sensitive=self.case.isChecked()))
            except ValueError as exc:
                error = str(exc)
                break
        self.preview.clear()
        if error:
            self.moves = []
            self.summary.setText(f"<span style='color:{ERROR_COLOR.name()}'>{error}</span>")
            self.ok.setEnabled(False)
            return
        self.moves = organize.plan_renames_for(self.recs, names)
        _fill_preview(self.preview, [(m.src.name, m.dst.name, m) for m in self.moves])
        changed = [m for m in self.moves if not m.unchanged and not m.error]
        errors = [m for m in self.moves if m.error]
        self.summary.setText(f"{len(changed)} to rename, {len(self.moves) - len(changed) - len(errors)} unchanged"
                             + (f", <b>{len(errors)} with problems</b> (fix them to continue)" if errors else ""))
        self.ok.setEnabled(bool(changed) and not errors)


def _fill_preview(tree: QTreeWidget, rows: list[tuple[str, str, organize.Move]]):
    tree.setUpdatesEnabled(False)
    items = []
    for old, new, move in rows[:MAX_PREVIEW_ROWS]:
        status = move.error or ("unchanged" if move.unchanged else "")
        item = QTreeWidgetItem([old, new, status])
        item.setToolTip(0, str(move.src))
        item.setToolTip(1, str(move.dst))
        if move.error:
            for col in range(3):
                item.setForeground(col, QBrush(ERROR_COLOR))
        elif move.unchanged:
            for col in range(3):
                item.setForeground(col, QBrush(tree.palette().placeholderText().color()))
        items.append(item)
    tree.addTopLevelItems(items)
    if len(rows) > MAX_PREVIEW_ROWS:
        tree.addTopLevelItem(QTreeWidgetItem([f"… and {len(rows) - MAX_PREVIEW_ROWS:,} more", "", ""]))
    tree.setUpdatesEnabled(True)


# ---------------------------------------------------------------- metadata

class MetadataDialog(QDialog):
    """Edit project/scene/take/tape/note/circled for one or many files.

    With several files, a field is only changed if its "Change" box is checked
    (typing checks it), so differing values are never flattened by accident.
    """

    LABELS = [("project", "Project"), ("scene", "Scene"), ("take", "Take"), ("tape", "Tape"), ("note", "Note")]

    def __init__(self, recs: list[Recording], family_extra: list[Recording], include_family: bool, parent=None):
        super().__init__(parent)
        self.recs = recs
        self.family_extra = family_extra
        self.setWindowTitle("Edit metadata" if len(recs) == 1 else f"Edit metadata of {len(recs)} files")
        self.edits: dict[str, QLineEdit] = {}
        self.ticks: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Change</b>"), 0, 0)
        for row, (key, label) in enumerate(self.LABELS, 1):
            values = {getattr(r, "meta_project" if key == "project" else key) for r in recs}
            edit = QLineEdit()
            tick = QCheckBox()
            if len(values) == 1:
                edit.setText(next(iter(values)))
            else:
                edit.setPlaceholderText(f"{len(values)} different values (unchanged unless you type here)")
            if key == "project":
                current = {r.project for r in recs if r.project_from == "folder"}
                if current and values == {""}:
                    edit.setPlaceholderText(f"not set in the file (shown as '{sorted(current)[0]}' from its folder)")
            edit.textEdited.connect(lambda _text, t=tick: t.setChecked(True))
            grid.addWidget(tick, row, 0, Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(QLabel(label + ":"), row, 1)
            grid.addWidget(edit, row, 2)
            self.edits[key], self.ticks[key] = edit, tick
        self.circled = QComboBox()
        self.circled.addItem("Keep as is", None)
        self.circled.addItem("★ Circled", True)
        self.circled.addItem("Not circled", False)
        circled_values = {r.circled for r in recs}
        if len(recs) == 1:
            self.circled.setItemText(0, f"Keep as is ({'circled' if recs[0].circled else 'not circled'})")
        elif len(circled_values) == 1:
            self.circled.setItemText(0, f"Keep as is (all {'circled' if recs[0].circled else 'not circled'})")
        grid.addWidget(QLabel("Circled:"), len(self.LABELS) + 1, 1)
        grid.addWidget(self.circled, len(self.LABELS) + 1, 2)

        self.family = QCheckBox()
        self.family.setChecked(include_family and bool(family_extra))
        self.family.setVisible(bool(family_extra))
        if family_extra:
            names = ", ".join(r.name for r in family_extra[:4]) + (" …" if len(family_extra) > 4 else "")
            self.family.setText(f"Also apply to the take's other {len(family_extra)} file(s): {names}")
            self.family.setToolTip("Polyphonic recorders write one take as several files (e.g. _ISO and _LR). "
                                   "Keeping them in step keeps them matched in an NLE.")
        info = QLabel("Changes are written into the files' iXML and BWF metadata. Files are changed in place "
                      "when the new metadata fits; if any would need to be copied in full, you'll be asked first.")
        info.setWordWrap(True)
        info.setEnabled(False)
        box, self.ok = _buttons(self, "Write metadata")
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.family)
        layout.addWidget(info)
        layout.addWidget(box)
        self.resize(620, self.sizeHint().height())

    def changes(self) -> dict:
        result = {}
        for key, _ in self.LABELS:
            if self.ticks[key].isChecked():
                result[key] = self.edits[key].text().strip()
        if self.circled.currentData() is not None:
            result["circled"] = self.circled.currentData()
        return result

    def targets(self) -> list[Recording]:
        return self.recs + (self.family_extra if self.family.isChecked() else [])

    def accept(self):
        if not self.changes():
            self.reject()
            return
        super().accept()


# ---------------------------------------------------------------- organize

class OrganizeDialog(QDialog):
    """Preview moving files into folders built from their metadata."""

    PRESETS = ["{project}/{day}", "{project}", "{project}/{tape}", "{project}/{date}/{scene}",
               "{recorder}/{project}/{day}"]

    def __init__(self, recs: list[Recording], destination: str, qsettings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Reorganize {len(recs):,} files into folders")
        self.recs = recs
        self.qsettings = qsettings
        self.moves: list[organize.Move] = []

        self.destination = QLineEdit(destination)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.pattern = QComboBox()
        self.pattern.setEditable(True)
        self.pattern.addItems(self.PRESETS)
        self.pattern.setCurrentText(settings.get(qsettings, "organize_pattern"))
        self.pattern.setToolTip(_tokens_tooltip())
        self.remove_empty = QCheckBox("Remove folders that are left empty (the recorders' empty "
                                      ".take_folder / .daily_folder markers and .DS_Store files are removed with them)")
        self.remove_empty.setChecked(settings.get(qsettings, "organize_remove_empty"))

        form = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(self.destination, 1)
        row.addWidget(browse)
        form.addRow("Into folder:", row)
        form.addRow("Folders:", self.pattern)
        hint = QLabel(_tokens_help() + ". File names stay the same unless the last part uses {name} or "
                      "{filename}. Empty values become e.g. 'No Date'.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)

        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["Now", "Moves to", ""])
        self.preview.setRootIsDecorated(False)
        self.preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        note = QLabel("Only the audio files are moved. Other files in their folders (sound reports, CSV/XML, "
                      "video) stay where they are.")
        note.setWordWrap(True)
        note.setEnabled(False)
        box, self.ok = _buttons(self, "Move files")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.summary)
        layout.addWidget(note)
        layout.addWidget(self.remove_empty)
        layout.addWidget(box)
        self.resize(1100, 700)

        self._timer = QTimer(self, singleShot=True, interval=250)
        self._timer.timeout.connect(self._update)
        self.destination.textChanged.connect(self._timer.start)
        self.pattern.currentTextChanged.connect(self._timer.start)
        self._update()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Move into folder", self.destination.text())
        if folder:
            self.destination.setText(folder)

    def _relative(self, path: Path) -> str:
        base = self.destination.text().strip()
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    def _update(self):
        destination = self.destination.text().strip()
        self.preview.clear()
        if not destination or not os.path.isdir(destination):
            self.moves = []
            self.summary.setText(f"<span style='color:{ERROR_COLOR.name()}'>Choose an existing folder.</span>")
            self.ok.setEnabled(False)
            return
        self.moves = organize.plan_moves(self.recs, destination, self.pattern.currentText().strip())
        rows = sorted(((self._relative(m.src), self._relative(m.dst), m) for m in self.moves),
                      key=lambda r: (not r[2].error, r[2].unchanged, r[1].casefold()))
        _fill_preview(self.preview, rows)
        moving = [m for m in self.moves if not m.unchanged and not m.error]
        errors = [m for m in self.moves if m.error]
        folders = {m.dst.parent for m in moving}
        text = f"{len(moving):,} files move into {len(folders):,} folders, " \
               f"{len(self.moves) - len(moving) - len(errors):,} are already in place."
        if errors:
            text += (f" <b>{len(errors):,} can't be moved</b> (listed first) and will be skipped; "
                     "adding {name} or {scene}/{take} to the pattern usually resolves name clashes.")
        self.summary.setText(text)
        self.ok.setEnabled(bool(moving))

    def accept(self):
        settings.put(self.qsettings, "organize_pattern", self.pattern.currentText().strip())
        settings.put(self.qsettings, "organize_remove_empty", self.remove_empty.isChecked())
        super().accept()

    def approved_moves(self) -> list[organize.Move]:
        return [m for m in self.moves if not m.unchanged and not m.error]


def dangerous_mode(qsettings: QSettings) -> bool:
    return settings.get(qsettings, "safety_mode") == "dangerous"


def dangerous_label() -> QLabel:
    label = QLabel(f"<span style='color:{ERROR_COLOR.name()}'><b>Dangerous mode</b>: your last choices are "
                   "used, and files set to be deleted are deleted permanently without asking (Settings).</span>")
    label.setWordWrap(True)
    return label


def confirm_permanent_delete(parent: QWidget, recs: list[Recording]) -> bool:
    names = [r.name for r in recs]
    box = QMessageBox(QMessageBox.Icon.Warning, "Delete permanently",
                      f"Delete {len(names)} file{'s' if len(names) != 1 else ''} permanently?", parent=parent)
    box.setInformativeText("They are deleted only after the new files are written and checked, but they don't "
                           "go to the trash, and Undo can't bring them back.\n\n" + "\n".join(names[:12])
                           + (f"\n… and {len(names) - 12} more" if len(names) > 12 else ""))
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    box.button(QMessageBox.StandardButton.Yes).setText("Delete Permanently")
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


class SplitDialog(QDialog):
    """Preview splitting multi-track files into one file per track, with a
    tick per track: ticked tracks are split off, the others stay with the
    original (kept whole, or shrunk to just those tracks)."""

    def __init__(self, recs: list[Recording], family_of, qsettings: QSettings, parent=None):
        super().__init__(parent)
        self.recs, self.family_of, self.qsettings = recs, family_of, qsettings
        self.plan = splitter.SplitPlan()
        self.picked = {r.path: set(range(r.channels)) for r in recs if splitter.splittable(r) is None}
        self.groups: dict[str, list[tuple[str, list[int]]]] = {path: [] for path in self.picked}
        self.setWindowTitle("Split into track files")
        intro = QLabel("Ticked tracks become their own mono files, named after the track, in a folder named "
                       "after the take; select several and <i>Group into One File</i> to keep them together in "
                       "one polywav. The new files keep the metadata (scene, take, timecode, notes); the "
                       "take's other files (e.g. the _LR) move into the folder with them.")
        intro.setWordWrap(True)
        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["File / track", ""])
        self.preview.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.preview.header().setStretchLastSection(True)
        self.preview.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.group_button = QPushButton("Group into One File…")
        self.group_button.setToolTip("Put the selected tracks of a file into one polywav instead of a mono file each")
        self.ungroup_button = QPushButton("Ungroup")
        self.group_button.clicked.connect(self._group_selected)
        self.ungroup_button.clicked.connect(self._ungroup_selected)
        self.preview.itemSelectionChanged.connect(self._selection_changed)
        self.preview.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._track_menu)
        self.group_hint = QLabel("To put tracks into one polywav: select them (Ctrl- or Shift-click the track "
                                 "names), then <i>Group into One File…</i> (or right-click).")
        self.group_hint.setWordWrap(True)
        self.group_hint.setEnabled(False)
        tick_all, untick_all = QPushButton("Tick All Tracks"), QPushButton("Untick All")
        tick_all.clicked.connect(lambda: self._tick_all(True))
        untick_all.clicked.connect(lambda: self._tick_all(False))
        ticks = QHBoxLayout()
        ticks.addWidget(tick_all)
        ticks.addWidget(untick_all)
        ticks.addStretch(1)
        ticks.addWidget(self.group_button)
        ticks.addWidget(self.ungroup_button)

        self.rest_box = QGroupBox("Tracks you don't split off stay with the original:")
        self.whole = QRadioButton("It keeps all its tracks: it moves into the folder unchanged")
        self.shrink = QRadioButton("It is shrunk to just those tracks (same name and metadata, in the folder); "
                                   f"the full file goes to “{REMOVED_FOLDER}”")
        (self.shrink if settings.get(qsettings, "split_shrink_original") else self.whole).setChecked(True)
        rest_layout = QVBoxLayout(self.rest_box)
        rest_layout.addWidget(self.whole)
        rest_layout.addWidget(self.shrink)
        self.keep = QCheckBox("Keep the original file: move it into the folder with the track files")
        self.keep.setChecked(settings.get(qsettings, "split_keep_original"))
        self.keep.setToolTip(f"For files split into all their tracks. Otherwise the original goes to "
                             f"“{REMOVED_FOLDER}” in the library, where you can put it back or delete it for good.")
        self.gone_box = QGroupBox("Originals that aren't kept (and full files that are shrunk):")
        self.to_removed = QRadioButton(f"Move them to “{REMOVED_FOLDER}” (they can be put back; Undo works)")
        self.delete = QRadioButton("Delete them permanently (after the new files are written and checked; "
                                   "can't be undone)")
        self.dangerous = dangerous_mode(qsettings)
        # Safe mode never starts on a permanent delete.
        (self.delete if self.dangerous and settings.get(qsettings, "split_delete_permanently")
         else self.to_removed).setChecked(True)
        gone_layout = QVBoxLayout(self.gone_box)
        gone_layout.addWidget(self.to_removed)
        gone_layout.addWidget(self.delete)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        box, self.ok = _buttons(self, "Split")
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.preview, 1)
        layout.addLayout(ticks)
        layout.addWidget(self.group_hint)
        layout.addWidget(self.summary)
        layout.addWidget(self.rest_box)
        layout.addWidget(self.keep)
        layout.addWidget(self.gone_box)
        if self.dangerous:
            layout.addWidget(dangerous_label())
        layout.addWidget(box)
        self.resize(820, 700)
        self.keep.toggled.connect(self._update)
        self.whole.toggled.connect(self._update)
        self.delete.toggled.connect(self._update)
        self.preview.itemChanged.connect(self._track_ticked)
        self._update()

    def _tick_all(self, on: bool):
        for rec in self.recs:
            if rec.path in self.picked:
                self.picked[rec.path] = set(range(rec.channels)) if on else set()
        self._update()

    def _selected_tracks(self) -> list[tuple[str, int]]:
        found = []
        for item in self.preview.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "group":
                found += [(data[1], c) for c in self.groups[data[1]][data[2]][1]]
            elif data:
                found.append(tuple(data))
        return list(dict.fromkeys(found))

    def _selection_changed(self):
        # The group button stays clickable and explains itself; greyed out it went unnoticed.
        tracks = self._selected_tracks()
        self.ungroup_button.setEnabled(any(c in members for p, c in tracks for _, members in self.groups[p]))

    def _track_menu(self, pos):
        item = self.preview.itemAt(pos)
        if item is not None and not item.isSelected():
            self.preview.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        menu.addAction("Group into One File…", self._group_selected)
        ungroup = menu.addAction("Ungroup", self._ungroup_selected)
        ungroup.setEnabled(self.ungroup_button.isEnabled())
        menu.exec(self.preview.viewport().mapToGlobal(pos))

    def _group_selected(self):
        tracks = self._selected_tracks()
        if len({p for p, _ in tracks}) > 1:
            QMessageBox.information(self, "Group into one file", "Select tracks of one file only: a group "
                                    "becomes one polywav made from one recording.")
            return
        if len(tracks) < 2:
            QMessageBox.information(self, "Group into one file", "Select two or more tracks of a file first: "
                                    "Ctrl-click (or Shift-click) the track names in the list, then group them.")
            return
        path = tracks[0][0]
        channels = sorted(c for _, c in tracks)
        rec = next(r for r in self.recs if r.path == path)
        names = list(rec.tracks) + [""] * (rec.channels - len(rec.tracks))
        suggested = "+".join(splitter.track_stem(names[c], c + 1) for c in channels)
        name, ok = QInputDialog.getText(self, "Group into one file",
                                        f"Name of the file for tracks {', '.join(str(c + 1) for c in channels)} "
                                        "(without the extension):", text=suggested)
        if not ok:
            return
        name = organize.safe_part(name.strip()) or suggested
        self._remove_from_groups(path, set(channels))
        self.groups[path].append((name, channels))
        self.picked[path] |= set(channels)
        self._update()

    def _ungroup_selected(self):
        by_path: dict[str, set[int]] = {}
        for path, channel in self._selected_tracks():
            by_path.setdefault(path, set()).add(channel)
        for path, channels in by_path.items():
            # Ungrouping one track of a group ungroups the whole group.
            whole = {c for _, members in self.groups[path] if channels & set(members) for c in members}
            self._remove_from_groups(path, whole)
        self._update()

    def _remove_from_groups(self, path: str, channels: set[int]):
        kept = []
        for name, members in self.groups[path]:
            members = [c for c in members if c not in channels]
            if len(members) >= 2:
                kept.append((name, members))
        self.groups[path] = kept

    def _track_ticked(self, item: QTreeWidgetItem, column: int):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if column != 0 or not data or data[0] == "group":
            return
        path, channel = data
        if item.checkState(0) == Qt.CheckState.Checked:
            self.picked[path].add(channel)
        else:
            self.picked[path].discard(channel)
        QTimer.singleShot(0, self._update)  # not while Qt is still in the item's change

    def _update(self):
        shrink = self.shrink.isChecked()
        self.plan = splitter.plan_split(self.recs, self.family_of, keep=self.keep.isChecked(),
                                        picked=self.picked, shrink=shrink, groups=self.groups)
        partial = any(self.picked[p] != set(range(r.channels)) and self.picked[p]
                      for r in self.recs if (p := r.path) in self.picked)
        self.rest_box.setEnabled(partial)
        self.keep.setEnabled(any(self.picked[r.path] == set(range(r.channels))
                                 for r in self.recs if r.path in self.picked))
        self.gone_box.setEnabled(bool(self.removed_originals()))
        scroll = self.preview.verticalScrollBar().value()
        self.preview.blockSignals(True)
        self.preview.clear()
        tops: dict[str, QTreeWidgetItem] = {}

        def folder_item(folder: str) -> QTreeWidgetItem:
            if folder not in tops:
                top = QTreeWidgetItem([os.path.basename(folder) + "/",
                                       "folder exists" if os.path.isdir(folder) else "new folder"])
                top.setToolTip(0, folder)
                self.preview.addTopLevelItem(top)
                top.setExpanded(True)
                tops[folder] = top
            return tops[folder]

        new_names = {(rec.path, c): os.path.basename(tf.dst) for rec, files in self.plan.splits
                     for tf in files for c in tf.picks}
        for rec in self.recs:
            if rec.path not in self.picked:
                continue
            chosen = self.picked[rec.path]
            rest = rec.channels - len(chosen)
            gone = "is deleted permanently" if self.delete.isChecked() else f"goes to “{REMOVED_FOLDER}”"
            if rec.path in self.plan.remainders:
                fate = f"the original: shrunk to the {rest} track(s) not split off; the full file {gone}"
            elif rec.path in self.plan.originals_in_folder:
                fate = "the original, moved in" + (" (it keeps all its tracks)" if rest else "")
            elif chosen:
                fate = "the original " + gone
            else:
                fate = "not split (no tracks ticked)"
            item = QTreeWidgetItem(folder_item(splitter.take_folder(rec)), [rec.name, fate])
            item.setExpanded(True)
            tracks = list(rec.tracks) + [""] * (rec.channels - len(rec.tracks))
            group_items = {}
            for index, (name, members) in enumerate(self.groups[rec.path]):
                ticked = [c for c in members if c in chosen]
                goes = new_names.get((rec.path, ticked[0]), "") if ticked else ""
                group_items[members[0]] = (index, members, goes, len(ticked))
            parent_of = {}
            for channel in range(rec.channels):
                if channel in group_items:  # the group's row goes where its first track is
                    index, members, goes, count = group_items[channel]
                    node = QTreeWidgetItem(item, [f"Group: {self.groups[rec.path][index][0]}",
                                                  f"→ {goes} (one file, {count} track{'s' if count != 1 else ''})"
                                                  if goes else "no tracks ticked"])
                    node.setData(0, Qt.ItemDataRole.UserRole, ("group", rec.path, index))
                    for c in members:
                        parent_of[c] = node
                on = channel in chosen
                goes = new_names.get((rec.path, channel), "")
                grouped = channel in parent_of
                child = QTreeWidgetItem(parent_of.get(channel, item),
                                        [f"{channel + 1}  {tracks[channel] or '(no name)'}",
                                         ("in the group" if grouped else f"→ {goes}") if on and goes
                                         else "stays with the original"])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                child.setData(0, Qt.ItemDataRole.UserRole, (rec.path, channel))
        for rec, dst in self.plan.partners:
            QTreeWidgetItem(folder_item(os.path.dirname(dst)), [rec.name, "the take's other file, moved in"])
        self.preview.expandAll()
        self.preview.blockSignals(False)
        self.preview.verticalScrollBar().setValue(scroll)
        self._selection_changed()

        tracks = sum(len(files) for _, files in self.plan.splits)
        lines = [f"{len(self.plan.splits):,} file(s) give {tracks:,} new file(s)"
                 + (f", {len(self.plan.remainders):,} shrunk original(s)" if self.plan.remainders else "")
                 + "."] if self.plan.splits else []
        red = ERROR_COLOR.name()
        if self.plan.problems:
            lines.append(f"<span style='color:{red}'><b>Nothing can be split until this is sorted out:</b><br>"
                         + "<br>".join(escape_html(p) for p in self.plan.problems) + "</span>")
        skipped = [s for s in self.plan.skipped if not s.endswith("no tracks ticked")]
        if skipped:
            lines.append("Not split: " + "; ".join(escape_html(s) for s in skipped))
        self.summary.setText("<br>".join(lines))
        self.ok.setEnabled(bool(self.plan.splits) and not self.plan.problems)

    def removed_originals(self) -> list[Recording]:
        """The originals that leave (not kept, or shrunk)."""
        return [r for r, _ in self.plan.splits if r.path not in self.plan.originals_in_folder]

    def accept(self):
        gone = self.removed_originals()
        if gone and self.delete.isChecked() and not self.dangerous and not confirm_permanent_delete(self, gone):
            return
        settings.put(self.qsettings, "split_keep_original", self.keep.isChecked())
        settings.put(self.qsettings, "split_shrink_original", self.shrink.isChecked())
        settings.put(self.qsettings, "split_delete_permanently", self.delete.isChecked())
        super().accept()


class CombineDialog(QDialog):
    """Choose the order and name for combining files of one take into a polywav."""

    def __init__(self, recs: list[Recording], qsettings: QSettings, parent=None):
        super().__init__(parent)
        self.recs, self.qsettings = recs, qsettings
        self.setWindowTitle("Combine into polywav")
        intro = QLabel("The tracks of these files go into one polywav, in this order (drag, or use the "
                       "buttons). It keeps the first file's metadata (scene, take, timecode, notes) and every "
                       "file's track names.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        for rec in recs:
            tracks = ", ".join(t or "(no name)" for t in (list(rec.tracks) + [""] * rec.channels)[:rec.channels])
            item = QListWidgetItem(f"{rec.name}    {rec.channels} track{'s' if rec.channels != 1 else ''}: {tracks}")
            item.setData(Qt.ItemDataRole.UserRole, rec.path)
            self.list.addItem(item)
        up, down = QPushButton("Move Up"), QPushButton("Move Down")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        moves = QVBoxLayout()
        moves.addWidget(up)
        moves.addWidget(down)
        moves.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        row.addLayout(moves)
        suggested = splitter.combined_name(recs)
        # Only the name is editable; the extension stays the recordings' own.
        self.ext = os.path.splitext(suggested)[1]
        self.name = QLineEdit(os.path.splitext(suggested)[0])
        name_row = QHBoxLayout()
        name_row.addWidget(self.name, 1)
        name_row.addWidget(QLabel(self.ext))
        folder = QLabel(f"In {recs[0].folder}")
        folder.setEnabled(False)
        folder.setWordWrap(True)
        self.keep = QRadioButton("Keep the files as they are")
        self.remove = QRadioButton(f"Move them to “{REMOVED_FOLDER}” (they can be put back)")
        self.delete = QRadioButton("Delete them permanently (after the new file is written and checked; "
                                   "can't be undone)")
        self.dangerous = dangerous_mode(qsettings)
        chosen = settings.get(qsettings, "combine_sources")
        if chosen == "delete" and not self.dangerous:
            chosen = "remove"  # safe mode never starts on a permanent delete
        {"remove": self.remove, "delete": self.delete}.get(chosen, self.keep).setChecked(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        box, self.ok = _buttons(self, "Combine")
        form = QFormLayout()
        form.addRow("New file:", name_row)
        form.addRow("", folder)
        after = QGroupBox("Afterwards, the files combined:")
        after_layout = QVBoxLayout(after)
        after_layout.addWidget(self.keep)
        after_layout.addWidget(self.remove)
        after_layout.addWidget(self.delete)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row, 1)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addWidget(after)
        if self.dangerous:
            layout.addWidget(dangerous_label())
        layout.addWidget(box)
        self.resize(720, 480)
        self.name.textChanged.connect(self._update)
        self.list.model().rowsMoved.connect(self._update)
        self._update()

    def _move(self, step: int):
        row = self.list.currentRow()
        if row < 0 or not 0 <= row + step < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(row + step, item)
        self.list.setCurrentRow(row + step)
        self._update()

    def ordered(self) -> list[Recording]:
        by_path = {r.path: r for r in self.recs}
        return [by_path[self.list.item(i).data(Qt.ItemDataRole.UserRole)] for i in range(self.list.count())]

    def destination(self) -> str:
        return compat.join(self.recs[0].folder, self.name.text().strip() + self.ext)

    def _update(self):
        problems = splitter.combine_problems(self.recs)
        stem = self.name.text().strip()
        name = stem + self.ext
        error = validate_name(name) if stem else "type a name"
        if error:
            problems.append(f"New file: {error}")
        elif os.path.lexists(self.destination()):
            problems.append(f"{name} is already in that folder")
        channels = sum(r.channels for r in self.recs)
        if problems:
            self.summary.setText(f"<span style='color:{ERROR_COLOR.name()}'><b>Can't combine:</b><br>"
                                 + "<br>".join(escape_html(p) for p in problems) + "</span>")
        else:
            self.summary.setText(f"{len(self.recs)} files give one {channels}-track file.")
        self.ok.setEnabled(not problems)

    def accept(self):
        if self.delete.isChecked() and not self.dangerous and not confirm_permanent_delete(self, self.recs):
            return
        settings.put(self.qsettings, "combine_sources", "remove" if self.remove.isChecked() else
                     "delete" if self.delete.isChecked() else "keep")
        super().accept()


# ---------------------------------------------------------------- settings

def human_bytes(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1000
    return ""


def index_estimates(recs: list[Recording], root: str) -> tuple[int, int]:
    """(metadata index bytes, waveform bytes) for these recordings: the
    metadata measured on a sample, the waveforms from the track counts."""
    if not recs:
        return 0, 0
    sample = recs[:: max(len(recs) // 400, 1)]
    index = library_index.LibraryIndex(root)
    index.replace_all(sample)
    packed = len(gzip.compress(json.dumps({k: list(v) for k, v in index.entries.items()}).encode(), 6))
    metadata = packed * len(recs) // max(len(sample), 1)
    waves = sum(library_index.waveform_bytes(max(r.channels, 1), waveform.BUCKETS) for r in recs if not r.error)
    return metadata, waves


class SettingsDialog(QDialog):
    def __init__(self, qsettings: QSettings, parent=None, recordings: list[Recording] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.qsettings = qsettings
        self.recordings = recordings or []
        self.clear_cache_requested = False
        self.folder = QLineEdit(settings.get(qsettings, "library_folder"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.containers = QPlainTextEdit("\n".join(settings.get(qsettings, "container_folders")))
        self.containers.setFixedHeight(90)
        self.embedded = QCheckBox("Renaming also updates the file name stored inside the WAV")
        self.embedded.setChecked(settings.get(qsettings, "write_embedded_filename"))
        self.family = QCheckBox("Metadata edits include the take's other files (_ISO / _LR) by default")
        self.family.setChecked(settings.get(qsettings, "apply_to_take_family"))
        self.scene_take = QCheckBox("Changing a scene or take in the Library table renames the file to match "
                                    "(e.g. scene 8M, take 01 → 8MT01)")
        self.scene_take.setChecked(settings.get(qsettings, "rename_on_scene_take"))
        self.confirm_undo = QCheckBox("Ask before undoing")
        self.confirm_undo.setChecked(settings.get(qsettings, "confirm_undo"))
        # The library index: reading and writing, for file metadata and waveforms.
        def box(key, tip):
            check = QCheckBox()
            check.setChecked(settings.get(qsettings, key))
            check.setToolTip(tip)
            return check
        self.index_read = box("library_index_read", "Take file metadata from the library's index when a file is not "
                              "in this computer's cache, instead of opening the file")
        self.index = box("library_index", f"Write what the scan read into {library_index.INDEX_FOLDER}/ in the "
                         "library after each complete scan")
        self.index_waves_read = box("library_index_waveforms_read", "Draw a waveform from the library's index "
                                    "instead of reading the whole WAV")
        self.index_waves = box("library_index_waveforms", "Save each waveform into the library's index when it is "
                               "first drawn (uses space on the share)")
        self.index_waves.toggled.connect(self._waves_toggled)
        self._estimates = None
        clear = QPushButton("Clear scan cache…")
        clear.setToolTip("Forget all cached metadata and waveforms; the next scan reads every file again")
        clear.clicked.connect(self._clear)

        form = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow("Library folder:", row)
        form.addRow("Container folders:", self.containers)
        hint = QLabel("One per line. Folders that hold memory-card or backup dumps rather than one project; "
                      "skipped when a file's project is taken from its folder names (only for files with no "
                      "project in their metadata).")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        form.addRow("", hint)
        form.addRow("", self.embedded)
        form.addRow("", self.family)
        form.addRow("", self.scene_take)
        form.addRow("", self.confirm_undo)
        form.addRow("", clear)
        meta, waves = self._estimate()
        count = len(self.recordings)
        index_box = QGroupBox(f"Library index (shared with other computers, in {library_index.INDEX_FOLDER}/ "
                              "in the library folder)")
        grid = QGridLayout(index_box)
        self.index_status = QLabel(self._index_status())
        self.index_status.setWordWrap(True)
        grid.addWidget(self.index_status, 0, 0, 1, 3)
        grid.addWidget(QLabel("<b>Use</b>"), 1, 1, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(QLabel("<b>Update</b>"), 1, 2, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(QLabel("File metadata"), 2, 0)
        grid.addWidget(self.index_read, 2, 1, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(self.index, 2, 2, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(QLabel("Waveforms"), 3, 0)
        grid.addWidget(self.index_waves_read, 3, 1, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(self.index_waves, 3, 2, Qt.AlignmentFlag.AlignHCenter)
        grid.setColumnStretch(0, 1)
        index_hint = QLabel(
            "<b>Use</b>: a file or waveform this computer hasn't cached yet is taken from the index instead of "
            "being read from the share (only while the file's size and date are unchanged). Changes nothing.<br>"
            f"<b>Update</b> writes to the library: file metadata after each complete scan (about "
            f"{human_bytes(meta)} for {count:,} recordings); waveforms as each file is first drawn "
            f"(⚠ about {human_bytes(waves)} on the share, 8 KB per track per file).")
        index_hint.setWordWrap(True)
        index_hint.setEnabled(False)
        grid.addWidget(index_hint, 4, 0, 1, 3)
        mode_box = QGroupBox("Split and combine: files that are replaced")
        self.safe_mode = QRadioButton(f"Safe mode: the dialogs always start with moving them to “{REMOVED_FOLDER}” "
                                      "(can be put back, Undo works). Deleting permanently can still be chosen "
                                      "each time, and asks first.")
        self.dangerous_mode = QRadioButton("Dangerous mode: your last choices are kept, including deleting "
                                           "permanently, and files are deleted without asking. Deleted files "
                                           "can't be brought back.")
        self.dangerous_mode.setToolTip("For when you've settled on your choices in the Split and Combine windows")
        (self.dangerous_mode if settings.get(qsettings, "safety_mode") == "dangerous" else
         self.safe_mode).setChecked(True)
        mode_layout = QVBoxLayout(mode_box)
        for radio in (self.safe_mode, self.dangerous_mode):
            # Long choices wrap in a label next to the button.
            row = QHBoxLayout()
            text = QLabel(radio.text())
            text.setWordWrap(True)
            text.mousePressEvent = lambda _e, r=radio: r.setChecked(True)
            radio.setText("")
            row.addWidget(radio, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            mode_layout.addLayout(row)
        box, _ = _buttons(self, "Save")
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(index_box)
        layout.addWidget(mode_box)
        layout.addWidget(box)
        self.resize(640, self.sizeHint().height())

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Library folder", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def _estimate(self) -> tuple[int, int]:
        if self._estimates is None:
            self._estimates = index_estimates(self.recordings, self.folder.text().strip())
        return self._estimates

    def _index_status(self) -> str:
        """What the library folder holds now (one or two requests to the share)."""
        root = self.folder.text().strip()
        path = compat.join(library_index.index_folder(root), library_index.METADATA_FILE)
        try:
            stat = os.stat(path)
        except OSError:
            return "This library has no index yet."
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        waves = "with waveforms" if library_index.has_waveforms(root) else "no waveforms"
        return f"This library has an index: file metadata {human_bytes(stat.st_size)}, updated {when}; {waves}."

    def _waves_toggled(self, on: bool):
        if not on or settings.get(self.qsettings, "library_index_waveforms"):
            return
        waves = self._estimate()[1]
        free = ""
        try:
            free = f"\n\nFree space on the share now: {human_bytes(shutil.disk_usage(self.folder.text().strip()).free)}."
        except OSError:
            pass
        answer = QMessageBox.warning(
            self, "Waveforms in the library index",
            f"Keeping waveforms in the library will use about {human_bytes(waves)} on the share for the "
            f"{len(self.recordings):,} recordings in it now, and more as the library grows. They are written "
            "as each file's waveform is first drawn." + free + "\n\nKeep waveforms in the library?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            self.index_waves.setChecked(False)

    def _clear(self):
        self.clear_cache_requested = True
        self.sender().setText("Cache will be cleared when you save")
        self.sender().setEnabled(False)

    def accept(self):
        settings.put(self.qsettings, "library_folder", self.folder.text().strip())
        settings.put(self.qsettings, "container_folders",
                     [line.strip() for line in self.containers.toPlainText().splitlines() if line.strip()])
        settings.put(self.qsettings, "write_embedded_filename", self.embedded.isChecked())
        settings.put(self.qsettings, "apply_to_take_family", self.family.isChecked())
        settings.put(self.qsettings, "rename_on_scene_take", self.scene_take.isChecked())
        settings.put(self.qsettings, "confirm_undo", self.confirm_undo.isChecked())
        settings.put(self.qsettings, "safety_mode", "dangerous" if self.dangerous_mode.isChecked() else "safe")
        settings.put(self.qsettings, "library_index_read", self.index_read.isChecked())
        settings.put(self.qsettings, "library_index", self.index.isChecked())
        settings.put(self.qsettings, "library_index_waveforms_read", self.index_waves_read.isChecked())
        settings.put(self.qsettings, "library_index_waveforms", self.index_waves.isChecked())
        super().accept()


class RewriteDialog(QDialog):
    """Ask before files are copied in full (slow on a network share)."""

    def __init__(self, recs: list[Recording], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Some files need a full rewrite")
        total = sum(r.size for r in recs)
        text = QLabel(f"The new metadata doesn't fit in the space these {len(recs)} file(s) have for it. "
                      f"Writing it means copying each file in full ({total / 1e9:.2f} GB in total), then "
                      "replacing the original once the copy is checked. The other files were already updated.")
        text.setWordWrap(True)
        tree = QTreeWidget()
        tree.setHeaderLabels(["File", "Size"])
        tree.setRootIsDecorated(False)
        for rec in recs:
            item = QTreeWidgetItem([rec.name, f"{rec.size / 1e6:,.0f} MB"])
            item.setToolTip(0, rec.path)
            tree.addTopLevelItem(item)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        box = QDialogButtonBox()
        box.addButton("Rewrite these files", QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton("Skip them", QDialogButtonBox.ButtonRole.RejectRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(tree, 1)
        layout.addWidget(box)
        self.resize(640, 420)


def show_report(parent: QWidget, title: str, summary: str, lines: list[str]):
    """A result summary with the details (errors, skipped files) listed below."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    label = QLabel(summary)
    label.setWordWrap(True)
    details = QPlainTextEdit("\n".join(lines))
    details.setReadOnly(True)
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    box.rejected.connect(dialog.reject)
    box.accepted.connect(dialog.accept)
    layout = QVBoxLayout(dialog)
    layout.addWidget(label)
    if lines:
        layout.addWidget(details, 1)
    layout.addWidget(box)
    dialog.resize(720, 420 if lines else dialog.sizeHint().height())
    dialog.exec()
