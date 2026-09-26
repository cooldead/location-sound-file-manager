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
    """Filters by the sidebar selection (year / month / project / day), a search text and circled-only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.project: str | None = None  # None = all
        self.day: str | None = None
        self.period: str | None = None  # a year / month (see in_period)
        self.recorder: str | None = None  # a recorder_label
        self.text = ""
        self.circled_only = False
        self.allowed: set[str] | None = None  # only these paths (the Offload page's tree selection)

    def set_allowed(self, paths: set[str] | None):
        self.allowed = paths
        self.invalidateFilter()

    def set_scope(self, project: str | None, day: str | None, period: str | None = None,
                  recorder: str | None = None):
        self.project, self.day, self.period, self.recorder = project, day, period, recorder
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
        if not in_period(rec, self.period):
            return False
        if self.recorder is not None and recorder_label(rec) != self.recorder:
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
PERIOD_ROLE = Qt.ItemDataRole.UserRole + 12  # "2026" or "2026-09" ("" = no date, None = any)
RECORDER_ROLE = Qt.ItemDataRole.UserRole + 13  # a recorder_label, None = any
NO_RECORDER = "(Unknown recorder)"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def recorder_label(rec: Recording) -> str:
    """The recorder's model without its serial number ("Sound Devices 833")."""
    text = rec.recorder.strip()
    if text.startswith("SoundDev:"):
        model = text.split(":", 1)[1].split()
        text = "Sound Devices " + (model[0] if model else "")
    return text or NO_RECORDER


def in_period(rec: Recording, period: str | None) -> bool:
    """Whether a recording falls in a sidebar period: a year "2026", a month
    "2026-09", "" for recordings without a date, or None for any."""
    if period is None:
        return True
    if period == "":
        return not rec.date
    return rec.date.startswith(period)


def period_label(period: str) -> str:
    if not period:
        return NO_DAY
    if len(period) == 7 and period[5:].isdigit() and 1 <= int(period[5:]) <= 12:
        return MONTHS[int(period[5:]) - 1]
    return period


def _count(recs: list[Recording], counts: str) -> str:
    """"  (12)": projects ("projects"), recordings ("files") or both ("12 / 1,182")."""
    files = len(recs)
    projects = len({r.project or NO_PROJECT for r in recs})
    if counts == "files":
        return f"  ({files:,})"
    if counts == "both":
        return f"  ({projects:,} / {files:,})"
    return f"  ({projects:,})"


def _count_tip(recs: list[Recording]) -> str:
    projects = len({r.project or NO_PROJECT for r in recs})
    return f"{projects:,} project{'s' if projects != 1 else ''}, {len(recs):,} recording{'s' if len(recs) != 1 else ''}"


def _node(text: str, project, day, period, tip: str = "", recorder: str | None = None) -> QStandardItem:
    item = QStandardItem(text)
    item.setData(recorder, RECORDER_ROLE)
    item.setData(project, PROJECT_ROLE)
    item.setData(day, DAY_ROLE)
    item.setData(period, PERIOD_ROLE)
    item.setEditable(False)
    if tip:
        item.setToolTip(tip)
    return item


def _project_nodes(parent: QStandardItem | QStandardItemModel, recs: list[Recording], period,
                   counts: str = "projects", recorder: str | None = None) -> None:
    """One node per project (alphabetical) with its recording days. Project
    and day nodes show their recordings only when files are counted."""
    tally: dict[str, dict[str, int]] = {}
    for rec in recs:
        days = tally.setdefault(rec.project or NO_PROJECT, {})
        day = day_of(rec) or NO_DAY
        days[day] = days.get(day, 0) + 1
    show_files = counts in ("files", "both")
    for project in sorted(tally, key=lambda p: (p == NO_PROJECT, p.casefold())):
        days = tally[project]
        total = sum(days.values())
        item = _node(f"{project}  ({total:,})" if show_files else project, project, None, period,
                     f"{project}: {total:,} recording{'s' if total != 1 else ''}", recorder)
        for day in sorted(days, key=lambda d: (d == NO_DAY, d)):
            item.appendRow(_node(f"{day}  ({days[day]:,})" if show_files else day, project, day, period,
                                 f"{days[day]:,} recording{'s' if days[day] != 1 else ''}", recorder))
        parent.appendRow(item)


def build_project_tree(model: QStandardItemModel, recs: list[Recording], by_date: bool = False,
                       counts: str = "files", by_recorder: bool = False) -> None:
    """All recordings, then either one node per project with its recording days,
    or (by_date) years, newest first -> the months that have recordings ->
    the projects recorded that month -> their days. counts: what the numbers
    count, "projects", "files" or "both" (tooltips always give both).
    by_recorder: one node per recorder (most recordings first) -> its projects
    -> their days."""
    model.clear()
    everything = _node(f"All recordings{_count(recs, counts)}", None, None, None, _count_tip(recs))
    bold = everything.font()
    bold.setBold(True)
    everything.setFont(bold)
    model.appendRow(everything)
    if by_recorder:
        by_label: dict[str, list[Recording]] = {}
        for rec in recs:
            by_label.setdefault(recorder_label(rec), []).append(rec)
        for label in sorted(by_label, key=lambda k: (k == NO_RECORDER, -len(by_label[k]), k.casefold())):
            group = by_label[label]
            item = _node(f"{label}{_count(group, counts)}", None, None, None, f"{label}: {_count_tip(group)}", label)
            item.setFont(bold)
            _project_nodes(item, group, None, counts, label)
            model.appendRow(item)
        return
    if not by_date:
        _project_nodes(model, recs, None, counts)
        return
    by_month: dict[str, list[Recording]] = {}
    for rec in recs:
        month = rec.date[:7] if len(rec.date) >= 7 and rec.date[:4].isdigit() else ""
        by_month.setdefault(month, []).append(rec)
    years: dict[str, list[str]] = {}
    for month in by_month:
        if month:
            years.setdefault(month[:4], []).append(month)
    for year in sorted(years, reverse=True):
        months = sorted(years[year], reverse=True)
        year_recs = [r for m in months for r in by_month[m]]
        year_item = _node(f"{year}{_count(year_recs, counts)}", None, None, year, f"{year}: {_count_tip(year_recs)}")
        year_item.setFont(bold)
        for month in months:
            month_item = _node(f"{period_label(month)}{_count(by_month[month], counts)}", None, None, month,
                               f"{period_label(month)} {year}: {_count_tip(by_month[month])}")
            _project_nodes(month_item, by_month[month], month, counts)
            year_item.appendRow(month_item)
        model.appendRow(year_item)
    if "" in by_month:
        undated = _node(f"{NO_DAY}{_count(by_month[''], counts)}", None, None, "",
                        f"Recordings without a date in their metadata: {_count_tip(by_month[''])}")
        undated.setFont(bold)
        _project_nodes(undated, by_month[""], "", counts)
        model.appendRow(undated)
