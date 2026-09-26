"""Qt models: the recordings table, its filter proxy and the project sidebar tree."""

from __future__ import annotations

import dataclasses
import os
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication

from .catalog import Recording, day_of
from .timecode import format_duration

NO_PROJECT = "(No project)"
NO_DAY = "(No date)"

# (header, width hint)
COLUMNS = [
    ("File", 220), ("Scene", 70), ("Take", 50), ("★", 28), ("Start TC", 100), ("Length", 64),
    ("Ch", 36), ("Tracks", 220), ("Format", 170), ("FPS", 70), ("Date", 90), ("Time", 70),
    ("Note", 160), ("Recorder", 150), ("Project", 150), ("Folder", 300), ("Status", 160),
]
COL = {name: i for i, (name, _) in enumerate(COLUMNS)}
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
REC_ROLE = Qt.ItemDataRole.UserRole + 2
EFFECTIVE_ROLE = Qt.ItemDataRole.UserRole + 3  # the recording with pending edits applied
EDITABLE_COLUMNS = {"File": "name", "Scene": "scene", "Take": "take", "Note": "note"}


def _natural(text: str):
    """Sort key where "T2" < "T10" (numbers compare as numbers)."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
                 for part in re.split(r"(\d+)", text) if part)


class RecordingsModel(QAbstractTableModel):
    """The recordings table. With editable=True (the Offload review), File,
    Scene, Take, Note and ★ can be changed in place; those changes are kept
    as *pending* edits per path (nothing is written) and shown highlighted."""

    pendingChanged = Signal()

    def __init__(self, parent=None, editable: bool = False):
        super().__init__(parent)
        self.recs: list[Recording] = []
        self.root = ""
        self.editable = editable
        self.pending: dict[str, dict] = {}  # path -> {"name"|"scene"|"take"|"note"|"circled": value}
        self.status: dict[str, str] = {}  # path -> text for the Status column
        self._row_of: dict[str, int] = {}
        self._keys: dict[tuple[int, int], object] = {}  # sort keys; sorting 21k rows asks millions of times

    def effective(self, rec: Recording) -> Recording:
        changes = self.pending.get(rec.path)
        if not changes:
            return rec
        values = {k: v for k, v in changes.items() if k != "name"}
        if "name" in changes:
            values["path"] = os.path.join(rec.folder, changes["name"])
        return dataclasses.replace(rec, **values)

    def set_pending(self, rec: Recording, key: str, value) -> None:
        original = rec.name if key == "name" else getattr(rec, key)
        changes = self.pending.setdefault(rec.path, {})
        if value == original:
            changes.pop(key, None)
        else:
            changes[key] = value
        if not changes:
            del self.pending[rec.path]
        self._changed(rec.path)
        self.pendingChanged.emit()

    def clear_pending(self, paths=None) -> None:
        for path in list(self.pending if paths is None else paths):
            self.pending.pop(path, None)
            self._changed(path)
        self.pendingChanged.emit()

    def set_status(self, status: dict[str, str]) -> None:
        self.status = status
        col = COL["Status"]
        for key in [k for k in self._keys if k[1] == col]:
            del self._keys[key]
        if self.recs:
            self.dataChanged.emit(self.index(0, col), self.index(len(self.recs) - 1, col))

    def _changed(self, path: str) -> None:
        row = self._row_of.get(path)
        if row is not None:
            for col in range(len(COLUMNS)):
                self._keys.pop((row, col), None)
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def flags(self, index):
        flags = super().flags(index)
        if self.editable and index.isValid() and COLUMNS[index.column()][0] in EDITABLE_COLUMNS \
                and not self.recs[index.row()].error:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        key = EDITABLE_COLUMNS.get(COLUMNS[index.column()][0])
        if key is None:
            return False
        rec = self.recs[index.row()]
        value = str(value).strip()
        if key == "name":
            ext = os.path.splitext(rec.name)[1]
            if not value:
                return False
            if not value.lower().endswith(ext.lower()):
                value += ext
            if "/" in value or "\0" in value:
                return False
        self.set_pending(rec, key, value)
        return True

    def toggle_circled(self, row: int) -> None:
        rec = self.recs[row]
        self.set_pending(rec, "circled", not self.effective(rec).circled)

    def set_recordings(self, recs: list[Recording], root: str):
        self.beginResetModel()
        self.recs = recs
        self.root = root
        self._row_of = {r.path: i for i, r in enumerate(recs)}
        self._keys.clear()
        self.endResetModel()

    def append(self, recs: list[Recording]):
        if not recs:
            return
        start = len(self.recs)
        self.beginInsertRows(QModelIndex(), start, start + len(recs) - 1)
        for i, rec in enumerate(recs, start):
            self._row_of[rec.path] = i
        self.recs.extend(recs)
        self.endInsertRows()

    def row_of(self, path: str) -> int | None:
        return self._row_of.get(path)

    def replace(self, old_path: str, rec: Recording):
        row = self._row_of.pop(old_path, None)
        if row is None:
            return
        self.recs[row] = rec
        self._row_of[rec.path] = row
        for col in range(len(COLUMNS)):
            self._keys.pop((row, col), None)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.recs)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section][0]
            if role == Qt.ItemDataRole.ToolTipRole and section == COL["★"]:
                return "Circled take"
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        original = self.recs[index.row()]
        rec = self.effective(original)
        col = index.column()
        if role == REC_ROLE:
            return original
        if role == EFFECTIVE_ROLE:
            return rec
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == COL["Status"]:
                return self.status.get(original.path, "")
            return self._text(rec, col)
        if role == SORT_ROLE:
            return self.sort_key(index.row(), col)
        if role == Qt.ItemDataRole.ToolTipRole:
            if rec.error:
                return f"Could not read this file: {rec.error}"
            if col == COL["Project"] and rec.project_from == "folder":
                return "No project in the file's metadata; taken from its folder"
            if col == COL["Tracks"] and rec.tracks:
                return "\n".join(f"{i + 1}: {name}" for i, name in enumerate(rec.tracks))
            if col in (COL["File"], COL["Folder"]):
                return rec.path
            if col == COL["Note"] and rec.note:
                return rec.note
        pending = self.pending.get(original.path, {})
        key = EDITABLE_COLUMNS.get(COLUMNS[col][0]) or ("circled" if col == COL["★"] else None)
        if key and key in pending:
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QApplication.palette().color(QPalette.ColorRole.Link))
            if role == Qt.ItemDataRole.FontRole:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ItemDataRole.ToolTipRole:
                was = original.name if key == "name" else getattr(original, key)
                return f"Changed (not written yet). On the card: {was if was not in ('', None) else '(empty)'}"
        if role == Qt.ItemDataRole.ForegroundRole and col == COL["Status"] and original.path in self.status:
            text = self.status[original.path]
            if text.startswith(("conflict", "failed")):
                return QBrush(QColor("#d13438"))
            if text.startswith(("on NAS", "copied")):
                return QBrush(QApplication.palette().color(QPalette.ColorRole.PlaceholderText))
        if role == Qt.ItemDataRole.ForegroundRole:
            if rec.error or (col == COL["Project"] and rec.project_from == "folder"):
                return QBrush(QApplication.palette().color(QPalette.ColorRole.PlaceholderText))
        if role == Qt.ItemDataRole.FontRole and col == COL["Project"] and rec.project_from == "folder":
            font = QFont()
            font.setItalic(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL["Take"], COL["Ch"], COL["Length"], COL["★"]):
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def _text(self, rec: Recording, col: int) -> str:
        if col == COL["File"]:
            return rec.name
        if rec.error:
            return {COL["Note"]: "⚠ " + rec.error, COL["Folder"]: self._folder(rec),
                    COL["Project"]: rec.project}.get(col, "")
        return {
            COL["Scene"]: rec.scene, COL["Take"]: rec.take, COL["★"]: "★" if rec.circled else "",
            COL["Start TC"]: rec.start_tc, COL["Length"]: format_duration(rec.duration),
            COL["Ch"]: str(rec.channels), COL["Tracks"]: ", ".join(t for t in rec.tracks if t),
            COL["Format"]: rec.format_label + (" · RF64" if rec.form in ("RF64", "BW64") else ""),
            COL["FPS"]: rec.rate_label, COL["Date"]: rec.date, COL["Time"]: rec.time, COL["Note"]: rec.note,
            COL["Recorder"]: rec.recorder, COL["Project"]: rec.project, COL["Folder"]: self._folder(rec),
        }.get(col, "")

    def _folder(self, rec: Recording) -> str:
        if self.root:
            try:
                relative = os.path.relpath(rec.folder, self.root)
                return "" if relative == "." else relative
            except ValueError:
                pass
        return rec.folder

    def sort_key(self, row: int, col: int):
        key = self._keys.get((row, col))
        if key is None:
            key = self._keys[(row, col)] = self._sort_key(self.effective(self.recs[row]), col)
        return key

    def _sort_key(self, rec: Recording, col: int):
        if col == COL["Length"]:
            return rec.duration
        if col == COL["Ch"]:
            return rec.channels
        if col == COL["★"]:
            return 1 if rec.circled else 0
        if col == COL["Start TC"]:
            return rec.time_reference if rec.time_reference is not None else -1
        if col == COL["Date"]:
            return f"{rec.date} {rec.time}"
        return _natural(self._text(rec, col))


class RecordingsProxy(QSortFilterProxyModel):
    """Filters by the sidebar selection (project / day), a search text and circled-only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.project: str | None = None  # None = all
        self.day: str | None = None
        self.text = ""
        self.circled_only = False
        self.allowed: set[str] | None = None  # only these paths (the Offload page's tree selection)

    def set_allowed(self, paths: set[str] | None):
        self.allowed = paths
        self.invalidateFilter()

    def set_scope(self, project: str | None, day: str | None):
        self.project, self.day = project, day
        self.invalidateFilter()

    def set_text(self, text: str):
        self.text = text.casefold().strip()
        self.invalidateFilter()

    def set_circled_only(self, value: bool):
        self.circled_only = value
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        rec: Recording = self.sourceModel().recs[row]
        if self.allowed is not None and rec.path not in self.allowed:
            return False
        if self.project is not None and (rec.project or NO_PROJECT) != self.project:
            return False
        if self.day is not None and (day_of(rec) or NO_DAY) != self.day:
            return False
        if self.circled_only and not rec.circled:
            return False
        if self.text:
            haystack = " ".join((rec.name, rec.scene, rec.take, rec.note, rec.project, rec.tape,
                                 " ".join(rec.tracks), rec.folder, rec.start_tc)).casefold()
            return all(word in haystack for word in self.text.split())
        return True

    def lessThan(self, left, right):
        model = self.sourceModel()
        a, b = model.sort_key(left.row(), left.column()), model.sort_key(right.row(), right.column())
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


PROJECT_ROLE = Qt.ItemDataRole.UserRole + 10
DAY_ROLE = Qt.ItemDataRole.UserRole + 11


def build_project_tree(model: QStandardItemModel, recs: list[Recording]) -> None:
    """All recordings, then one node per project with its recording days."""
    model.clear()
    counts: dict[str, dict[str, int]] = {}
    for rec in recs:
        days = counts.setdefault(rec.project or NO_PROJECT, {})
        day = day_of(rec) or NO_DAY
        days[day] = days.get(day, 0) + 1
    everything = QStandardItem(f"All recordings  ({len(recs):,})")
    everything.setData(None, PROJECT_ROLE)
    everything.setData(None, DAY_ROLE)
    bold = everything.font()
    bold.setBold(True)
    everything.setFont(bold)
    everything.setEditable(False)
    model.appendRow(everything)
    for project in sorted(counts, key=lambda p: (p == NO_PROJECT, p.casefold())):
        days = counts[project]
        item = QStandardItem(f"{project}  ({sum(days.values()):,})")
        item.setData(project, PROJECT_ROLE)
        item.setData(None, DAY_ROLE)
        item.setEditable(False)
        item.setToolTip(project)
        for day in sorted(days, key=lambda d: (d == NO_DAY, d)):
            child = QStandardItem(f"{day}  ({days[day]:,})")
            child.setData(project, PROJECT_ROLE)
            child.setData(day, DAY_ROLE)
            child.setEditable(False)
            item.appendRow(child)
        model.appendRow(item)
