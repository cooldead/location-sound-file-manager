"""Dialogs. Each one only collects choices and shows a preview; the main window
applies the result (so every change goes through one place, with undo)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import library_index, organize, settings, waveform
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
        if not message and name != self.rec.name and os.path.lexists(os.path.join(self.rec.folder, name)) \
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
        self.confirm_undo = QCheckBox("Ask before undoing")
        self.confirm_undo.setChecked(settings.get(qsettings, "confirm_undo"))
        self.index = QCheckBox("Keep an index in the library folder")
        self.index.setChecked(settings.get(qsettings, "library_index"))
        self.index.setToolTip(f"Writes {library_index.INDEX_FOLDER}/ in the library folder after each scan")
        self.index_waves = QCheckBox("Also keep waveforms in the index")
        self.index_waves.setChecked(settings.get(qsettings, "library_index_waveforms"))
        self.index_waves.toggled.connect(self._waves_toggled)
        self.index.toggled.connect(self.index_waves.setEnabled)
        self.index_waves.setEnabled(self.index.isChecked())
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
        form.addRow("", self.confirm_undo)
        form.addRow("", clear)
        form.addRow("Library index:", self.index)
        meta, waves = self._estimate()
        count = len(self.recordings)
        index_hint = QLabel(
            f"Stores what the scan read from every file (about {human_bytes(meta)} for {count:,} recordings) in "
            f"a hidden {library_index.INDEX_FOLDER} folder in the library, so another computer's first scan "
            "doesn't open every file again. An index that is already there is always used; an entry is only "
            "trusted while the file's size and date are unchanged.")
        index_hint.setWordWrap(True)
        index_hint.setEnabled(False)
        form.addRow("", index_hint)
        form.addRow("", self.index_waves)
        waves_hint = QLabel(
            f"⚠ Takes about {human_bytes(waves)} on the share for {count:,} recordings (8 KB per track per "
            "file), filled in as files are first shown. Saves reading each WAV again to draw it on another "
            "computer.")
        waves_hint.setWordWrap(True)
        waves_hint.setEnabled(False)
        form.addRow("", waves_hint)
        box, _ = _buttons(self, "Save")
        layout = QVBoxLayout(self)
        layout.addLayout(form)
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
        settings.put(self.qsettings, "confirm_undo", self.confirm_undo.isChecked())
        settings.put(self.qsettings, "library_index", self.index.isChecked())
        settings.put(self.qsettings, "library_index_waveforms", self.index.isChecked() and self.index_waves.isChecked())
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
