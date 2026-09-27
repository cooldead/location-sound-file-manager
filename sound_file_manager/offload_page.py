"""The Offload page: card -> review & notes -> sound report -> copy to NAS -> open folder.

Nothing on the card is ever written. Edits made while reviewing are kept as
pending changes and written into the NAS copies after they are verified.
"""

from __future__ import annotations

import dataclasses
import os
import traceback
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableView, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import bwf, catalog, compat, duplicates, offload, report, settings
from .catalog import Recording
from .organize import MARKER_FILES
from .dialogs import RewriteDialog, show_report
from .file_model import COL, COLUMNS, EFFECTIVE_ROLE, REC_ROLE, RecordingsModel, RecordingsProxy
from .branding_dialog import load_branding
from .report_dialog import ReportDialog, ReportGroup, remembered_info
from .workers import run_job

HIDDEN_COLUMNS = ("Format", "FPS", "Time", "Recorder", "Project", "Folder")
FOLDER_ROLE = Qt.ItemDataRole.UserRole + 20
DAY_ROLE = Qt.ItemDataRole.UserRole + 21
STEPS = ["Card", "Review & notes", "Sound report", "Copy to NAS", "Send"]


class CardReader(QThread):
    """Reads every recording on the card (local and fast) off the GUI thread."""

    done = Signal(object, object, str)  # recordings, all files, error

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self):
        try:
            folders = {e.name for e in os.scandir(self.root) if e.is_dir() and not offload.is_system_folder(e.name)}
            folders.add(offload.ROOT_FILES)
            files = offload.card_files(self.root, folders, include_false_takes=True)
            recs = [catalog.read_recording(p) for p in files if catalog.is_audio_file(os.path.basename(p))]
            self.done.emit(recs, files, "")
        except Exception as error:  # noqa: BLE001
            self.done.emit([], [], f"{error}\n{traceback.format_exc()}")


class Planner(QThread):
    """Checks which files are already on the NAS (a stat per file over the network)."""

    done = Signal(object, int)

    def __init__(self, generation, files, root, library, names, new_names, parent=None):
        super().__init__(parent)
        self.args = (files, root, library, names, new_names)
        self.generation = generation

    def run(self):
        try:
            plan = offload.plan_copy(*self.args)
        except OSError:
            self.done.emit([], self.generation)
            return
        # A "conflict" is often the same recording: the NAS copy's metadata
        # was written later (a few bytes longer or shorter) or only its date
        # differs. The audio fingerprint tells; those count as on the NAS.
        targets: dict[str, int] = {}
        for item in plan:
            targets[item.dst] = targets.get(item.dst, 0) + 1
        for item in plan:
            if item.status != "conflict" or targets[item.dst] > 1 or not catalog.is_audio_file(item.dst):
                continue
            try:
                if duplicates.sample_hash(item.src) == duplicates.sample_hash(item.dst):
                    item.status = "same audio"
            except (OSError, bwf.WavError, AttributeError):
                pass
        self.done.emit(plan, self.generation)


class CopyThread(QThread):
    progress = Signal(object)
    finished_copy = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancelled = False
        self.result = None

    def run(self):
        try:
            self.result = self.fn(self)
        except Exception as error:  # noqa: BLE001
            self.result = error
        self.finished_copy.emit(self.result)


MERGE, NEW_FOLDER, SKIP = "merge", "new", "skip"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class ExistingProjectsDialog(QDialog):
    """The card has project folders the library already has under another
    folder (the same name written differently, or a similar name): copy
    into the existing folder, into a new folder, or not at all."""

    def __init__(self, rows: list[tuple[str, list[duplicates.CardProjectMatch]]], destination: str,
                 names: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Projects already in the library")
        self.destination = destination
        self.boxes: dict[str, QComboBox] = {}
        intro = QLabel("These folders on the card look like projects that are already in the library, in "
                       "another folder. Copying into the existing folder keeps each project in one place; "
                       "nothing in it is replaced (a different file with the same name is skipped).")
        intro.setWordWrap(True)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        kinds = {"same": "same name", "spelling": "same name, written differently", "similar": "similar name"}
        row = 0
        for folder, matches in rows:
            best = matches[0]
            title = QLabel(f"<b>{folder}</b> on the card: {_count(best.card_recordings, 'recording')}")
            details = []
            for match in matches:
                where = self.relative(match.library_folder)
                details.append(f"Library project <b>{match.library_project}</b> ({kinds[match.kind]}) in "
                               f"<b>{where}/</b>, {_count(match.library_files, 'file')}")
            everything = "It is" if best.card_recordings == 1 else f"All {best.card_recordings} are"
            if best.in_library == best.card_recordings:
                count = (f"<span style='color:#d13438'>{everything} already in the library</span>; copying "
                         "again would make duplicates.")
            elif best.in_library:
                count = (f"{best.in_library} of them {'is' if best.in_library == 1 else 'are'} already in the "
                         f"library; <b>{best.new_recordings} {'is' if best.new_recordings == 1 else 'are'} new</b>.")
            else:
                count = f"<b>{everything} new</b> to the library."
            info = QLabel("<br>".join(details + [count]))
            info.setWordWrap(True)
            box = QComboBox()
            for match in matches:
                box.addItem(f"Copy into {self.relative(match.library_folder)}/ (merge)",
                            (MERGE, self.relative(match.library_folder)))
            box.addItem(f"Copy into a new folder: {names.get(folder, folder) or '(the destination itself)'}",
                        (NEW_FOLDER, ""))
            box.addItem("Don't copy this folder", (SKIP, ""))
            box.setCurrentIndex(0 if best.new_recordings else box.count() - 1)
            self.boxes[folder] = box
            grid.addWidget(title, row, 0, 1, 2)
            grid.addWidget(info, row + 1, 0, 1, 2)
            grid.addWidget(QLabel("Action:"), row + 2, 0)
            grid.addWidget(box, row + 2, 1)
            if row:
                line = QFrame()
                line.setFrameShape(QFrame.Shape.HLine)
                grid.addWidget(line, row - 1, 0, 1, 2)
            row += 4
        grid.setRowStretch(row, 1)  # keep the rows together at the top
        body = QWidget()
        body.setLayout(grid)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        buttons = QDialogButtonBox()
        buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Keep Card Folder Names", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self.resize(640, min(160 + 150 * len(rows), 700))

    def relative(self, folder: str) -> str:
        return compat.relpath(folder, self.destination)

    def choices(self) -> dict[str, tuple[str, str]]:
        """Card folder -> (action, library folder below the destination for a merge)."""
        return {folder: box.currentData() for folder, box in self.boxes.items()}


class OffloadPage(QWidget):
    recordingSelected = Signal(object)  # Recording or None
    copiedToLibrary = Signal(list)  # destination folders, so the library rescans
    showInLibrary = Signal(str)  # a project folder
    releaseCard = Signal()  # before ejecting: stop playing card files
    log = Signal(str, dict)

    def __init__(self, qsettings, library_getter, parent=None, library_recs=lambda: []):
        super().__init__(parent)
        self.qsettings = qsettings
        self.library = library_getter
        self.library_recs = library_recs  # the Library's recordings, to find projects it already has
        self.in_library_paths: set[str] = set()  # card recordings already somewhere in the library
        self.skip_folders: set[str] = set()  # card folders not to tick ("Don't copy")
        self._checked_card: str | None = None  # the card the library check ran for
        self.card: offload.Card | None = None
        self.files: list[str] = []
        self.plan: list[offload.CopyItem] = []
        self.report_infos: dict[str, report.ReportInfo] = {}  # card project folder -> report details
        self.last_folders: list[str] = []
        self.done_names: dict[str, str] = {}  # card path -> name it was copied under (renames already written)
        self._reader: CardReader | None = None
        self._planner: Planner | None = None
        self._plan_generation = 0
        self._copy: CopyThread | None = None
        self._known_cards: list[offload.Card] = []
        self._build()
        self._fill_destinations()
        self._card_timer = QTimer(self, interval=3000)
        self._card_timer.timeout.connect(self.refresh_cards)
        self._card_timer.start()
        self._plan_timer = QTimer(self, singleShot=True, interval=400)
        self._plan_timer.timeout.connect(self._replan)
        QTimer.singleShot(0, self.refresh_cards)

    # ------------------------------------------------------------ UI

    def _build(self):
        self.steps = QLabel()
        self.steps.setTextFormat(Qt.TextFormat.RichText)

        self.card_box = QComboBox()
        self.card_box.setMinimumWidth(320)
        self.card_box.setPlaceholderText("Insert a card…")
        self.card_box.activated.connect(self._card_chosen)
        browse = QPushButton("Browse…")
        browse.setToolTip("Offload from any folder (a card reader not detected, or a copy of a card)")
        browse.clicked.connect(self._browse)
        self.card_info = QLabel()
        self.card_info.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Card:</b>"))
        top.addWidget(self.card_box)
        top.addWidget(browse)
        top.addWidget(self.card_info, 1)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Card folder", "Files", "Status", "NAS folder"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setToolTip("Check the days to copy. Double-click a NAS folder name to change it.")
        self.tree.itemChanged.connect(self._tree_item_changed)
        self.tree.currentItemChanged.connect(lambda *_: self._apply_scope())
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)

        self.model = RecordingsModel(self, editable=True)
        self.model.pendingChanged.connect(self._pending_changed)
        self.proxy = RecordingsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.set_allowed(set())
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 8)
        for col, (name, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(col, width)
            self.table.setColumnHidden(col, name in HIDDEN_COLUMNS)
        self.table.setColumnWidth(COL["Note"], 300)
        self.table.horizontalHeader().setSectionResizeMode(COL["★"], QHeaderView.ResizeMode.Fixed)
        self.table.sortByColumn(COL["Start TC"], Qt.SortOrder.AscendingOrder)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.clicked.connect(self._table_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        review_hint = QLabel("Double-click (or F2) a <b>file name, scene, take or note</b> to change it; click "
                             "<b>★</b> to circle a take. Changes stay pending (shown in bold) and are written "
                             "into the NAS copies; the card itself is never changed.")
        review_hint.setWordWrap(True)
        review_hint.setEnabled(False)
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self.table, 1)
        middle_layout.addWidget(review_hint)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.tree)
        self.split.addWidget(middle)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._build_panel())
        scroll.setMinimumWidth(330)
        self.split.addWidget(scroll)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([430, 800, 340])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.addWidget(self.steps)
        layout.addLayout(top)
        layout.addWidget(self.split, 1)
        self._update_steps()

    def _build_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        # 2-3 review and report
        review = QGroupBox("Review && report")
        review_layout = QVBoxLayout(review)
        self.pending_label = QLabel("No changes.")
        self.pending_label.setWordWrap(True)
        discard = QPushButton("Discard Changes")
        discard.clicked.connect(self._discard_pending)
        self.discard_button = discard
        self.report_label = QLabel()
        self.report_label.setWordWrap(True)
        report_button = QPushButton("Sound Report…")
        report_button.setToolTip("Fill in the report header and preview it")
        report_button.clicked.connect(self.edit_report)
        self.report_button = report_button
        review_layout.addWidget(self.pending_label)
        review_layout.addWidget(discard)
        review_layout.addSpacing(6)
        review_layout.addWidget(self.report_label)
        review_layout.addWidget(report_button)

        # 4 copy
        copy = QGroupBox("Copy to NAS")
        copy_layout = QVBoxLayout(copy)
        self.dest_box = QComboBox()
        self.dest_box.setToolTip("The folder the card's project folders are copied into")
        self.dest_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dest_box.setMinimumContentsLength(18)
        self.dest_box.activated.connect(self._destination_chosen)
        dest_browse = QPushButton("Browse…")
        dest_browse.setToolTip("Choose another destination folder")
        dest_browse.clicked.connect(self._browse_destination)
        self.dest_browse = dest_browse
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_box, 1)
        dest_row.addWidget(dest_browse)
        self.dest_label = QLabel()
        self.dest_label.setWordWrap(True)
        self.dest_label.setEnabled(False)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.verify = QCheckBox("Verify every copy (read back and compare)")
        self.verify.setChecked(settings.get(self.qsettings, "verify_copies"))
        self.verify.toggled.connect(lambda on: settings.put(self.qsettings, "verify_copies", on))
        self.save_report = QCheckBox("Save sound reports on the NAS")
        self.save_report.setChecked(settings.get(self.qsettings, "report_on_export"))
        self.save_report.toggled.connect(lambda on: (settings.put(self.qsettings, "report_on_export", on),
                                                     self._update_report_label()))
        self.copy_button = QPushButton("Copy to NAS")
        font = self.copy_button.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.copy_button.setFont(font)
        self.copy_button.setMinimumHeight(40)
        self.copy_button.clicked.connect(self.start_copy)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.progress_label.setVisible(False)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_copy)
        copy_layout.addWidget(QLabel("<b>Into:</b>"))
        copy_layout.addLayout(dest_row)
        copy_layout.addWidget(self.dest_label)
        copy_layout.addWidget(self.summary)
        copy_layout.addWidget(self.verify)
        self.report_per = QComboBox()
        self.report_per.addItem("One report per project (in the project folder)", "project")
        self.report_per.addItem("One report per day folder", "day")
        self.report_per.setCurrentIndex(max(self.report_per.findData(settings.get(self.qsettings, "report_per")), 0))
        self.report_per.currentIndexChanged.connect(
            lambda: (settings.put(self.qsettings, "report_per", self.report_per.currentData()),
                     self._update_report_label()))
        copy_layout.addWidget(self.save_report)
        copy_layout.addWidget(self.report_per)
        copy_layout.addWidget(self.copy_button)
        copy_layout.addWidget(self.progress)
        copy_layout.addWidget(self.progress_label)
        copy_layout.addWidget(self.cancel_button)

        # 5 send
        send = QGroupBox("Send")
        send_layout = QVBoxLayout(send)
        self.result_label = QLabel("After copying, open the project folder here to upload or share it.")
        self.result_label.setWordWrap(True)
        self.open_button = QToolButton()
        self.open_button.setText("Open Project Folder")
        self.open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.open_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.open_button.setSizePolicy(self.copy_button.sizePolicy())
        self.open_button.clicked.connect(lambda: self._open_folder(self.last_folders[0] if self.last_folders else ""))
        self.open_button.setEnabled(False)
        self.library_button = QPushButton("Show in Library")
        self.library_button.setEnabled(False)
        self.library_button.clicked.connect(
            lambda: self.showInLibrary.emit(self.last_folders[0] if self.last_folders else ""))
        self.eject_button = QPushButton("Eject Card…")
        self.eject_button.setEnabled(False)
        self.eject_button.clicked.connect(self.eject_card)
        send_layout.addWidget(self.result_label)
        send_layout.addWidget(self.open_button)
        send_layout.addWidget(self.library_button)
        send_layout.addWidget(self.eject_button)

        layout.addWidget(review)
        layout.addWidget(copy)
        layout.addWidget(send)
        layout.addStretch(1)
        return panel

    def _update_steps(self):
        if self.card is None:
            current = 0
        elif self._copy is not None:
            current = 3
        elif self.last_folders:
            current = 4
        elif self.model.pending or self.report_infos:
            current = 2
        else:
            current = 1
        parts = []
        for i, name in enumerate(STEPS):
            text = f"{i + 1}&nbsp;&nbsp;{name}"
            if i == current:
                parts.append(f"<span style='font-size:large'><b><u>{text}</u></b></span>")
            elif i < current:
                parts.append(f"<span style='color:gray'>{text} ✓</span>")
            else:
                parts.append(f"<span style='color:gray'>{text}</span>")
        self.steps.setText("&nbsp;&nbsp;&nbsp;→&nbsp;&nbsp;&nbsp;".join(parts))

    # ------------------------------------------------------------ cards

    def refresh_cards(self):
        if self._copy is not None:
            return
        cards = [c for c in offload.removable_mounts() if offload.looks_like_card(c.path)]
        if [c.path for c in cards] == [c.path for c in self._known_cards]:
            return
        new = [c for c in cards if c.path not in {k.path for k in self._known_cards}]
        self._known_cards = cards
        current = self.card.path if self.card else None
        self.card_box.blockSignals(True)
        self.card_box.clear()
        for card in cards:
            self.card_box.addItem(f"{card.label}  —  {offload.human_size(card.size)}  ({card.path})", card)
        manual = self.card is not None and current not in {c.path for c in cards} and os.path.isdir(current or "")
        if manual:
            self.card_box.addItem(f"{self.card.path}", self.card)
        self.card_box.blockSignals(False)
        if new:
            # A card was just inserted: read it (reading never changes anything).
            self.open_card(new[0])
        elif current and not manual and current not in {c.path for c in cards}:
            self.close_card("The card was removed.")
        elif self.card is not None:
            self.card_box.setCurrentIndex(max(self.card_box.findData(self.card), 0))

    def _card_chosen(self, index: int):
        card = self.card_box.itemData(index)
        if card is not None and (self.card is None or card.path != self.card.path):
            self.open_card(card)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Offload from folder", self.card.path if self.card else offload.MEDIA_FOLDER)
        if folder:
            card = offload.Card(folder, os.path.basename(folder) or folder)
            self.card_box.addItem(folder, card)
            self.card_box.setCurrentIndex(self.card_box.count() - 1)
            self.open_card(card)

    def open_card(self, card: offload.Card):
        if self._copy is not None:
            return
        # Forget the previous card's files before anything refers to the new one.
        self.model.set_recordings([], card.path)
        self.card = card
        self.done_names = {}
        self.files, self.plan = [], []
        self.in_library_paths, self.skip_folders = set(), set()
        self._checked_card = None
        self._plan_generation += 1  # results for the previous card are ignored
        self.last_folders = []
        self.report_infos = {}
        self.model.clear_pending()
        self.model.set_recordings([], card.path)
        self.tree.clear()
        self.card_info.setText("Reading the card…")
        index = self.card_box.findData(card)
        if index >= 0:
            self.card_box.setCurrentIndex(index)
        reader = CardReader(card.path, self)
        reader.done.connect(lambda recs, files, error, r=reader: self._card_read(recs, files, error, r))
        self._reader = reader
        self._reader.start()
        self._update_panel()

    def close_card(self, message: str = ""):
        self.card = None
        self._reader = None
        self._plan_generation += 1
        self.files, self.plan = [], []
        self.model.clear_pending()
        self.model.set_recordings([], "")
        self.tree.clear()
        self.card_info.setText(message)
        self.recordingSelected.emit(None)
        self._update_panel()

    def _card_read(self, recs, files, error, reader=None):
        if reader is not self._reader or self.card is None or reader.root != self.card.path:
            return  # a read of a card that is no longer shown
        self._reader = None
        if error:
            self.card_info.setText("Could not read the card.")
            QMessageBox.warning(self, "Offload", f"Could not read the card:\n{error}")
            return
        catalog.assign_projects(recs, self.card.path, [])
        self.files = files
        self.model.set_recordings(recs, self.card.path)
        total = sum(os.path.getsize(p) for p in files)
        recorders = sorted({report.recorder_name(r.recorder) for r in recs if r.recorder})
        self.card_info.setText(f"{len(recs)} recordings, {offload.human_size(total)}"
                               + (f" · {', '.join(recorders)}" if recorders else ""))
        self._fill_tree(recs)
        self.check_library_projects()
        self._replan()

    def check_library_projects(self, only: str | None = None) -> None:
        """Find card folders whose project the library already has in another
        folder, and let the user choose: merge into it, a new folder, or skip.
        `only` checks one card folder again (from the tree's menu)."""
        recs = [r for r in self.model.recs if not r.error]
        library, destination = self.library(), self.destination()
        library_recs = self.library_recs()
        if self.card is None or not library or not library_recs:
            return
        if only is None:
            self._checked_card = self.card.path
            self.in_library_paths = duplicates.recordings_in_library(recs, library_recs)
        if not destination or not self.in_library(destination):
            return  # NAS folder names are relative to a destination outside the library
        card_root = self.card.path
        matches = duplicates.card_project_matches(
            recs, lambda r: offload.project_folder(r.path, card_root), library_recs, library,
            settings.get(self.qsettings, "container_folders"))
        names = self.folder_names()
        below = compat.normpath(destination) + "/"
        rows = []
        for folder, candidates in sorted(matches.items(), key=lambda kv: kv[0].casefold()):
            if folder in (offload.ROOT_FILES, offload.FALSE_TAKES) or (only is not None and folder != only):
                continue
            usable = [m for m in candidates if (compat.normpath(m.library_folder) + "/").startswith(below)]
            target = compat.normpath(compat.join(destination, names.get(folder, folder)))
            if not usable or (only is None and any(compat.normpath(m.library_folder) == target for m in usable)):
                continue  # the copy already goes into the project's folder
            rows.append((folder, usable))
        if not rows:
            if only is not None:
                QMessageBox.information(self, "Offload", f"The library has no other folder for '{only}'.")
            return
        dialog = ExistingProjectsDialog(rows, destination, names, self)
        if dialog.exec():
            for folder, (action, relative) in dialog.choices().items():
                self.skip_folders.discard(folder)
                if action == SKIP:
                    self.skip_folders.add(folder)
                    self._tick_folder(folder, False)
                elif action == MERGE:
                    self._set_nas_folder(folder, relative)
        dialog.deleteLater()

    def library_ready(self) -> None:
        """The Library has loaded or finished a scan: check a card that was read
        before the library was there, and set its default ticks again."""
        if self.card is None or self._reader is not None or self._copy is not None or not self.model.recs:
            return
        if self._checked_card == self.card.path or not self.library_recs():
            return
        self.check_library_projects()
        self.plan = []  # the next plan sets the default ticks again
        self._plan_timer.start()

    def _tree_folder_item(self, folder: str) -> QTreeWidgetItem | None:
        for i in range(1, self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, FOLDER_ROLE) == folder:
                return item
        return None

    def _set_nas_folder(self, folder: str, name: str) -> None:
        item = self._tree_folder_item(folder)
        if item is not None and item.text(3) != name:
            item.setText(3, name)  # remembered and replanned by _tree_item_changed

    def _tick_folder(self, folder: str, on: bool) -> None:
        item = self._tree_folder_item(folder)
        if item is None:
            return
        for j in range(item.childCount()):
            item.child(j).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        folder = item.data(0, FOLDER_ROLE) if item is not None else None
        if not folder or folder in (offload.ROOT_FILES, offload.FALSE_TAKES):
            return
        menu = QMenu(self)
        action = menu.addAction("Find This Project in the Library…")
        action.triggered.connect(lambda: self.check_library_projects(only=folder))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ tree

    def _fill_tree(self, recs: list[Recording]):
        remembered = settings.get_json(self.qsettings, "offload_folder_names")
        by_folder: dict[str, dict[str, list[Recording]]] = defaultdict(lambda: defaultdict(list))
        for rec in recs:
            folder = offload.project_folder(rec.path, self.card.path)
            by_folder[folder][offload.day_folder(rec.path, self.card.path)].append(rec)
        self.tree.blockSignals(True)
        self.tree.clear()
        everything = QTreeWidgetItem(["All folders", str(len(recs)), "", ""])
        everything.setData(0, FOLDER_ROLE, None)
        self.tree.addTopLevelItem(everything)
        for folder in sorted(by_folder, key=lambda f: (f in (offload.FALSE_TAKES, offload.ROOT_FILES), f.casefold())):
            days = by_folder[folder]
            if folder == offload.ROOT_FILES:
                nas = remembered.get(folder, "")
            else:
                nas = remembered.get(folder, folder)
            item = QTreeWidgetItem([folder, str(sum(len(v) for v in days.values())), "", nas])
            item.setData(0, FOLDER_ROLE, folder)
            item.setData(0, DAY_ROLE, None)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            item.setToolTip(3, "The folder in the library these files go into. Double-click to change.")
            if folder == offload.FALSE_TAKES:
                item.setToolTip(0, "Takes you marked as false on the recorder")
            for day in sorted(days):
                child = QTreeWidgetItem([day or "(no day folder)", str(len(days[day])), "", ""])
                child.setData(0, FOLDER_ROLE, folder)
                child.setData(0, DAY_ROLE, day)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(True)
        self.tree.setCurrentItem(everything)
        self.tree.blockSignals(False)
        self._apply_scope()

    def _tree_double_clicked(self, item: QTreeWidgetItem, column: int):
        if column == 3 and item.data(0, DAY_ROLE) is None and item.data(0, FOLDER_ROLE):
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.tree.editItem(item, 3)

    def _tree_item_changed(self, item: QTreeWidgetItem, column: int):
        if column == 3:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name = item.text(3).strip().strip("/")
            if name != item.text(3):
                self.tree.blockSignals(True)
                item.setText(3, name)
                self.tree.blockSignals(False)
            remembered = settings.get_json(self.qsettings, "offload_folder_names")
            remembered[item.data(0, FOLDER_ROLE)] = name
            settings.put_json(self.qsettings, "offload_folder_names", remembered)
        self._plan_timer.start()
        self._apply_scope()

    def folder_names(self) -> dict[str, str]:
        names = {}
        for i in range(1, self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            names[item.data(0, FOLDER_ROLE)] = item.text(3).strip()
        return names

    def checked_days(self) -> set[tuple[str, str]]:
        days = set()
        for i in range(1, self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            for j in range(item.childCount()):
                child = item.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    days.add((child.data(0, FOLDER_ROLE), child.data(0, DAY_ROLE)))
        return days

    def _file_selected(self, path: str, days: set[tuple[str, str]]) -> bool:
        try:
            folder = offload.project_folder(path, self.card.path)
            day = offload.day_folder(path, self.card.path)
        except ValueError:
            return False  # not on this card
        if (folder, day) in days:
            return True
        # Files directly in a project folder (recorder CSV reports, markers) go
        # along when any day of that project is copied.
        return day == "" and any(f == folder for f, _ in days) and os.path.dirname(path) == compat.join(
            self.card.path, folder)

    def _apply_scope(self):
        item = self.tree.currentItem()
        if item is None or self.card is None:
            self.proxy.set_allowed(set())
            return
        folder, day = item.data(0, FOLDER_ROLE), item.data(0, DAY_ROLE)
        paths = set()
        for rec in self.model.recs:
            if folder is None:
                paths.add(rec.path)
            elif offload.project_folder(rec.path, self.card.path) == folder and (
                    day is None or offload.day_folder(rec.path, self.card.path) == day):
                paths.add(rec.path)
        self.proxy.set_allowed(paths)

    # ------------------------------------------------------------ destination

    def destination(self) -> str:
        """Where the copy goes: the chosen folder, else the library folder."""
        chosen = self.dest_box.currentData()
        return chosen or self.library()

    def _fill_destinations(self):
        raw = settings.get(self.qsettings, "offload_destinations")  # "" = the library folder
        recent = [d for d in raw if d]
        library = self.library()
        self.dest_box.blockSignals(True)
        self.dest_box.clear()
        if library:
            self.dest_box.addItem(f"Library folder — {library}", "")
            self.dest_box.setItemData(0, library, Qt.ItemDataRole.ToolTipRole)
        for folder in recent[:8]:
            if compat.normpath(folder) != compat.normpath(library or ""):
                self.dest_box.addItem(folder, folder)
                self.dest_box.setItemData(self.dest_box.count() - 1, folder, Qt.ItemDataRole.ToolTipRole)
        # The most recently used destination is the default.
        if raw and raw[0] and self.dest_box.findData(raw[0]) >= 0:
            self.dest_box.setCurrentIndex(self.dest_box.findData(raw[0]))
        else:
            self.dest_box.setCurrentIndex(0)
        self.dest_box.blockSignals(False)

    def _remember_destination(self, folder: str):
        recent = [d for d in settings.get(self.qsettings, "offload_destinations") if d and d != folder]
        settings.put(self.qsettings, "offload_destinations", ([folder] if folder else [""]) + recent[:7])

    def _destination_chosen(self, _index=None):
        self._remember_destination(self.dest_box.currentData() or "")
        self._destination_changed()

    def _browse_destination(self):
        folder = QFileDialog.getExistingDirectory(self, "Copy into folder", self.destination() or "/mnt")
        if not folder:
            return
        folder = compat.normpath(folder)
        if compat.normpath(self.library() or "") == folder:
            folder = ""
        self._remember_destination(folder)
        self._fill_destinations()
        self.dest_box.setCurrentIndex(max(self.dest_box.findData(folder), 0))
        self._destination_changed()

    def _destination_changed(self):
        self.last_folders = []
        self._plan_timer.start()
        self._update_panel()

    def in_library(self, folder: str) -> bool:
        library = self.library()
        return bool(library) and (compat.normpath(folder) + "/").startswith(compat.normpath(library) + "/")

    # ------------------------------------------------------------ plan

    def _replan(self):
        if self.card is None or not self.files:
            return
        library = self.destination()
        if not library or not os.path.isdir(library):
            self.summary.setText("<span style='color:#d13438'>The destination folder is not available. "
                                 "Is the NAS mounted?</span>")
            return
        new_names = dict(self.done_names)
        new_names.update({path: changes["name"] for path, changes in self.model.pending.items() if "name" in changes})
        self._plan_generation += 1
        self._planner = Planner(self._plan_generation, self.files, self.card.path, library, self.folder_names(),
                                new_names, self)
        self._planner.done.connect(self._planned)
        self._planner.start()

    def _planned(self, plan, generation):
        if generation != self._plan_generation:
            return
        self._planner = None
        first_plan = not self.plan
        self.plan = plan
        by_src = {item.src: item for item in plan}
        status = {}
        for rec in self.model.recs:
            item = by_src.get(rec.path)
            if item is None:
                continue
            status[rec.path] = {"new": "new", "same": "on NAS", "same audio": "on NAS (same audio, metadata differs)",
                                "conflict": "conflict: different file on NAS"}[item.status]
            if item.status == "new" and rec.path in self.in_library_paths:
                status[rec.path] = "in library (elsewhere)"
        self.model.set_status(status)
        # Tree status per day, and default ticks on first read: days with
        # anything new are ticked, days already on the NAS are not.
        self.tree.blockSignals(True)
        for i in range(1, self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            folder = item.data(0, FOLDER_ROLE)
            for j in range(item.childCount()):
                child = item.child(j)
                day = child.data(0, DAY_ROLE)
                items = [p for p in plan if offload.project_folder(p.src, self.card.path) == folder
                         and offload.day_folder(p.src, self.card.path) == day]
                # Recorder marker files (.daily_folder…) don't make a day new: merges
                # and empty-folder cleanup remove them from the NAS.
                new = sum(1 for p in items if p.status == "new" and os.path.basename(p.src) not in MARKER_FILES)
                conflicts = sum(1 for p in items if p.status == "conflict")
                # Recordings the library already has in another folder: copying them
                # again would make duplicates, so such days are not ticked.
                new_audio = [p for p in items if p.status == "new" and catalog.is_audio_file(os.path.basename(p.src))]
                elsewhere = bool(new_audio) and all(p.src in self.in_library_paths for p in new_audio)
                if conflicts:
                    text = f"{conflicts} conflict(s)"
                elif elsewhere:
                    text = "in library (elsewhere)"
                elif new and new == sum(1 for p in items if os.path.basename(p.src) not in MARKER_FILES):
                    text = "new"
                elif new:
                    text = f"{new} new"
                else:
                    text = "on NAS"
                child.setText(2, text)
                child.setToolTip(2, "These recordings are already in the library, in another folder"
                                 if elsewhere else "")
                if first_plan:
                    wanted = (new > 0 and folder != offload.FALSE_TAKES and folder not in self.skip_folders
                              and not elsewhere)
                    child.setCheckState(0, Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)
            nas = item.text(3).strip()
            target = compat.join(self.destination(), nas) if nas else self.destination()
            exists = bool(nas) and os.path.isdir(target)
            item.setText(2, "existing folder" if exists else "new folder")
            item.setToolTip(2, f"{target} {'already exists; files are added to it' if exists else 'will be created'}")
        self.tree.blockSignals(False)
        self._update_panel()

    def selected_plan(self) -> list[offload.CopyItem]:
        days = self.checked_days()
        return [item for item in self.plan if self._file_selected(item.src, days)]

    # ------------------------------------------------------------ review

    def _selection_changed(self, *_):
        rows = self.table.selectionModel().selectedRows()
        if len(rows) == 1:
            self.recordingSelected.emit(rows[0].data(REC_ROLE))

    def _table_clicked(self, index: QModelIndex):
        if index.column() == COL["★"]:
            self.model.toggle_circled(self.proxy.mapToSource(index).row())

    def _table_menu(self, pos):
        rows = [self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()]
        if not rows:
            return
        menu = QMenu(self)
        circle = menu.addAction("Toggle Circled ★")
        circle.triggered.connect(lambda: [self.model.toggle_circled(r) for r in rows])
        note = menu.addAction("Edit Note")
        note.triggered.connect(lambda: self.table.edit(self.proxy.index(self.table.currentIndex().row(), COL["Note"])))
        revert = menu.addAction("Undo Changes to Selected")
        revert.triggered.connect(lambda: self.model.clear_pending([self.model.recs[r].path for r in rows]))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _pending_changed(self):
        if any("name" in c for c in self.model.pending.values()):
            self._plan_timer.start()
        self._update_panel()

    def _discard_pending(self):
        if QMessageBox.question(self, "Discard changes", f"Discard {len(self.model.pending)} pending change(s)?") \
                == QMessageBox.StandardButton.Yes:
            self.model.clear_pending()

    def selected_recordings(self, effective: bool = True) -> list[Recording]:
        days = self.checked_days()
        recs = [r for r in self.model.recs if not r.error and self._file_selected(r.path, days)]
        return [self.model.effective(r) for r in recs] if effective else recs

    def report_groups(self) -> list[ReportGroup]:
        """One report per checked card project, with only that project's files."""
        by_folder: dict[str, list[Recording]] = defaultdict(list)
        for rec in self.selected_recordings():  # a pending rename keeps the file in its folder
            by_folder[offload.project_folder(rec.path, self.card.path)].append(rec)
        names = self.folder_names()
        groups = []
        for folder in sorted(by_folder, key=str.casefold):
            nas = names.get(folder, folder)
            title = folder if nas in ("", folder) else f"{folder}  →  {nas}"
            groups.append(ReportGroup(title, by_folder[folder], compat.join(self.destination(), nas),
                                      self.report_infos.get(folder), key=folder,
                                      project=nas or folder))
        return groups

    def edit_report(self):
        groups = self.report_groups() if self.card else []
        if not groups:
            QMessageBox.information(self, "Sound Report", "Check the days to copy first; the reports cover them.")
            return
        dialog = ReportDialog(groups, self.qsettings, mode="export", parent=self)
        if dialog.exec():
            for group, info in zip(groups, dialog.infos()):
                self.report_infos[group.key] = info
            self._update_panel()

    def _update_report_label(self):
        self.report_per.setEnabled(self.save_report.isChecked())
        if not self.save_report.isChecked():
            self.report_label.setText("No sound report will be saved.")
            return
        groups = self.report_groups() if self.card else []
        where = "each project folder" if self.report_per.currentData() == "project" else "each day folder"
        if not groups:
            self.report_label.setText("Sound reports: check the days to copy.")
        elif not self.report_infos:
            self.report_label.setText(f"{len(groups)} sound report(s), one per project, saved in {where}; filled in "
                                      "from the files and your saved details. Open them to add the director, "
                                      "comments, etc.")
        else:
            ready = sum(1 for g in groups if g.key in self.report_infos)
            self.report_label.setText(f"Sound reports: <b>{ready} of {len(groups)}</b> filled in; saved in {where}.")

    def _update_panel(self):
        pending = len(self.model.pending)
        self.pending_label.setText(f"<b>{pending}</b> file(s) with changes; they are written into the NAS copies."
                                   if pending else "No changes. Double-click a note, scene or take to edit it.")
        self.discard_button.setEnabled(bool(pending))
        self._update_report_label()
        destination = self.destination()
        if not destination:
            self.dest_label.setText("Choose a destination folder.")
        elif not os.path.isdir(destination):
            self.dest_label.setText("<span style='color:#d13438'>This folder is not available.</span>")
        elif self.in_library(destination):
            self.dest_label.setText("Inside the library, so the copies show up there.")
        else:
            self.dest_label.setText("Outside the library folder: the copies won't show in the Library page.")
        busy = self._copy is not None
        if self.card is None:
            self.summary.setText("Insert a card or choose a folder.")
            self.copy_button.setEnabled(False)
        else:
            chosen = self.selected_plan()
            new = [i for i in chosen if i.status == "new"]
            same = [i for i in chosen if i.on_nas]
            conflicts = [i for i in chosen if i.status == "conflict"]
            size = sum(i.size for i in new)
            text = f"<b>{len(new)}</b> file(s) to copy ({offload.human_size(size)})"
            if same:
                text += f", {len(same)} already on the NAS (skipped)"
            if conflicts:
                text += (f", <span style='color:#d13438'><b>{len(conflicts)} conflict(s)</b>: a different file "
                         "with the same name is on the NAS. They are skipped; rename them (File column) "
                         "or change the NAS folder.</span>")
            if not chosen:
                text = "Check the days to copy in the list on the left."
            self.summary.setText(text)
            touched = [i for i in chosen if i.src in self.model.pending and i.status != "conflict"]
            self.copy_button.setEnabled(not busy and bool(new or touched))
        self.report_button.setEnabled(self.card is not None and not busy)
        self.eject_button.setEnabled(self.card is not None and bool(self.card.device) and not busy)
        self.tree.setEnabled(not busy)
        self.dest_box.setEnabled(not busy)
        self.dest_browse.setEnabled(not busy)
        self._update_steps()

    # ------------------------------------------------------------ copy

    def start_copy(self):
        chosen = self.selected_plan()
        pending = dict(self.model.pending)
        recs_by_path = {r.path: r for r in self.model.recs}
        branding = load_branding(self.qsettings)
        infos = {}
        for group in self.report_groups():
            info = self.report_infos.get(group.key) or remembered_info(self.qsettings, group.recs, group.project)
            info.branding = branding
            infos[group.key] = info
        per_day = self.report_per.currentData() == "day"
        names, library, card_root = self.folder_names(), self.destination(), self.card.path
        make_report = self.save_report.isChecked()
        verify = self.verify.isChecked()
        embed = settings.get(self.qsettings, "write_embedded_filename")
        model = self.model
        qsettings = self.qsettings

        def remembered_info_for(group):
            info = remembered_info(qsettings, group)
            info.branding = branding
            return info

        def work(job):
            result = offload.copy_items(chosen, verify=verify, progress=job.progress.emit,
                                        cancelled=lambda: job.cancelled)
            done = {i.src: i for i in result.copied + [i for i in result.skipped if i.on_nas]}
            # Write the review changes into the NAS copies.
            written, need_rewrite, errors = [], [], []
            for src, changes in pending.items():
                item = done.get(src)
                if item is None or job.cancelled:
                    continue
                fields = {k: v for k, v in changes.items() if k in bwf.FIELDS}
                name = changes.get("name") if embed else None
                try:
                    bwf.update_metadata(item.dst, fields, filename=name)
                    _keep_card_dates(src, item.dst)
                    written.append(item)
                except bwf.NeedsRewrite:
                    need_rewrite.append((item, fields, name))
                except (OSError, bwf.WavError) as error:
                    errors.append(f"{os.path.basename(item.dst)}: changes not written ({error})")
            # One sound report per project (in its NAS folder), or per day folder.
            reports = []
            if make_report and not job.cancelled:
                groups: dict[tuple[str, str], list[Recording]] = defaultdict(list)
                for src, item in done.items():
                    rec = recs_by_path.get(src)
                    if rec is None or rec.error:
                        continue
                    folder = offload.project_folder(src, card_root)
                    day = offload.day_folder(src, card_root) if per_day else ""
                    groups[(folder, day)].append(dataclasses.replace(model.effective(rec), path=item.dst))
                for (folder, day), group in sorted(groups.items()):
                    base_info = infos.get(folder) or remembered_info_for(group)
                    group_info = report.ReportInfo(dict(base_info.fields), base_info.comments,
                                                   list(base_info.columns), base_info.orientation, base_info.style,
                                                   base_info.branding)
                    if per_day:
                        # Date, roll etc. describe just this day.
                        project_wide = report.detected_fields([r for (f, _), g in groups.items() if f == folder
                                                               for r in g])
                        for key, value in report.detected_fields(group).items():
                            if group_info.fields.get(key, "") in ("", project_wide.get(key, "")):
                                group_info.fields[key] = value
                    target = compat.join(library, names.get(folder, folder), day)
                    if folder == offload.ROOT_FILES:
                        target = compat.join(library, names.get(folder, ""), day)
                    base = _unique(compat.join(target, report.default_basename(group_info, group)))
                    try:
                        report.write_pdf(base + ".pdf", group_info, group)
                        report.write_csv(base + ".csv", group_info, group)
                        reports.append(base + ".pdf")
                    except OSError as error:
                        errors.append(f"Sound report in {target}: {error}")
            return result, written, need_rewrite, errors, reports

        self._copy = CopyThread(work, self)
        self._copy.progress.connect(self._copy_progress)
        self._copy.finished_copy.connect(lambda res: self._copy_finished(res, chosen, pending))
        self.progress.setVisible(True)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress_label.setVisible(True)
        self.progress_label.setText("Starting…")
        self.cancel_button.setVisible(True)
        self.copy_button.setEnabled(False)
        self._update_panel()
        self._copy.start()

    def _cancel_copy(self):
        if self._copy is not None:
            self._copy.cancelled = True
            self.progress_label.setText("Stopping after the current file…")

    def _copy_progress(self, state: offload.CopyProgress):
        if state.total_bytes:
            self.progress.setValue(int(state.done_bytes * 1000 / state.total_bytes))
        verb = "Verifying" if state.phase == "verify" else "Copying"
        self.progress_label.setText(
            f"{verb} {os.path.basename(state.current)}<br>"
            f"File {min(state.done_files + 1, state.total_files)} of {state.total_files} · "
            f"{offload.human_size(state.rate)}/s · {offload.human_time(state.eta)} left")

    def _copy_finished(self, outcome, chosen, pending):
        thread, self._copy = self._copy, None
        thread.wait()
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        if isinstance(outcome, Exception):
            QMessageBox.warning(self, "Copy to NAS", f"The copy stopped with an error:\n{outcome}")
            self._update_panel()
            self._replan()
            return
        result, written, need_rewrite, errors, reports = outcome
        if need_rewrite:
            recs = [dataclasses.replace(catalog.read_recording(i.dst)) for i, _, _ in need_rewrite]
            if RewriteDialog(recs, self).exec():
                def rewrite(job):
                    failed = []
                    for n, (item, fields, name) in enumerate(need_rewrite):
                        job.report(n, len(need_rewrite), os.path.basename(item.dst))
                        try:
                            bwf.update_metadata(item.dst, fields, filename=name, allow_rewrite=True)
                            written.append(item)
                        except (OSError, bwf.WavError) as error:
                            failed.append(f"{os.path.basename(item.dst)}: {error}")
                    return failed
                failed, error = run_job(self, "Writing changes", rewrite)
                errors += failed or ([str(error)] if error else [])
            else:
                errors += [f"{os.path.basename(i.dst)}: changes skipped (would need a full rewrite)"
                           for i, _, _ in need_rewrite]
        # The card is unchanged; the edits now live in the NAS copies. Renamed
        # files keep mapping to their new name so they show as already copied.
        for item in written:
            name = pending.get(item.src, {}).get("name")
            if name:
                self.done_names[item.src] = name
        self.model.clear_pending([i.src for i in written])
        library = self.destination()
        folders = []
        for item in result.copied + [i for i in result.skipped if i.on_nas]:
            relative = Path(item.dst).relative_to(library).parts
            folder = compat.join(library, relative[0]) if len(relative) > 1 else library
            if folder not in folders:
                folders.append(folder)
        self.last_folders = folders
        self._fill_open_menu()
        copied_size = sum(i.size for i in result.copied)
        lines = [f"{os.path.basename(i.dst)}: {message}" for i, message in result.failed] + errors
        summary = (f"Copied <b>{len(result.copied)}</b> file(s) ({offload.human_size(copied_size)})"
                   + (" and verified them" if self.verify.isChecked() and result.copied else "")
                   + f". {len(written)} with your changes written in."
                   + (f" {len(reports)} sound report(s) saved." if reports else ""))
        if result.failed:
            summary += f" <span style='color:#d13438'>{len(result.failed)} failed.</span>"
        self.result_label.setText(summary)
        self.open_button.setEnabled(bool(folders))
        self.library_button.setEnabled(bool(folders) and self.in_library(library))
        self.log.emit("offload", {"card": self.card.path if self.card else "",
                                  "copied": [[i.src, i.dst] for i in result.copied],
                                  "changes_written": [i.dst for i in written], "reports": reports})
        if lines:
            show_report(self, "Copy to NAS", summary.replace("<b>", "").replace("</b>", ""), lines)
        if self.in_library(library):
            self.copiedToLibrary.emit(folders)
        self._replan()
        self._update_panel()

    def _fill_open_menu(self):
        menu = QMenu(self.open_button)
        for folder in self.last_folders:
            action = menu.addAction(folder)
            action.triggered.connect(lambda _=False, f=folder: self._open_folder(f))
        self.open_button.setMenu(menu if len(self.last_folders) > 1 else None)
        if self.last_folders:
            self.open_button.setText(f"Open {os.path.basename(self.last_folders[0])}")

    def _open_folder(self, folder: str):
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def eject_card(self):
        if self.card is None:
            return
        if QMessageBox.question(self, "Eject card", f"Eject '{self.card.label}'?\n\nMake sure everything you need "
                                "is copied.") != QMessageBox.StandardButton.Yes:
            return
        self.releaseCard.emit()
        error = offload.eject(self.card)
        if error:
            QMessageBox.warning(self, "Eject card", f"Could not eject the card:\n{error}")
            return
        label = self.card.label
        self.close_card(f"'{label}' was ejected; you can remove it.")
        self._known_cards = [c for c in self._known_cards if c.label != label]

    def is_card_path(self, path: str) -> bool:
        return self.card is not None and path.startswith(self.card.path.rstrip("/") + "/")


def _keep_card_dates(src: str, dst: str) -> None:
    """After writing metadata into a copy, give it the card file's date again so
    a later offload of the same card still recognises it as already copied."""
    stat = os.stat(src)
    os.utime(dst, (stat.st_atime, stat.st_mtime))


def _unique(base: str) -> str:
    candidate, n = base, 2
    while os.path.exists(candidate + ".pdf") or os.path.exists(candidate + ".csv"):
        candidate = f"{base} ({n})"
        n += 1
    return candidate
