"""Main window: project tree, recordings table, details, player; every action
that changes files goes through do_renames() / do_metadata() here (with undo)."""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QActionGroup, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPixmap, QShortcut,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QTableView, QTextBrowser, QToolBar, QToolButton,
    QTreeView,
    QVBoxLayout, QWidget,
)

from . import bwf, catalog, compat, duplicates, offload, settings, splitter
from .offload import human_size
from .organize import find_empty_folders, remove_left_empty, remove_tree_if_empty
from .catalog import Recording
from .dialogs import (
    BatchRenameDialog, MetadataDialog, OrganizeDialog, RenameDialog, RewriteDialog, SettingsDialog, SplitDialog, CombineDialog,
    show_report,
)
from .file_model import (
    COL, COLUMNS, DAY_ROLE, NO_PROJECT, PERIOD_ROLE, PROJECT_ROLE, REC_ROLE, RECORDER_ROLE, RecordingsModel,
    RecordingsProxy, build_project_tree, in_period, period_label, recorder_label,
)
from .offload_page import OffloadPage
from .player import SEEK_STEP, PlayerWidget
from .report import ASSETS
from .branding_dialog import BrandingDialog, SetupDialog
from .duplicates_dialog import DuplicatesWindow
from .removed_dialog import RemovedDialog
from .report_dialog import ReportDialog, ReportGroup
from .renamer import (
    RenameError, RenameOp, apply_renames, partner_name, remove_empty_dirs, scene_take_name, undo_ops, validate_name,
)
from .workers import ScanThread, run_job

APP_NAME = "Location Sound File Manager"


def keys(shortcut: str) -> str:
    """A shortcut as the platform writes it: Ctrl+F here, ⌘F on a Mac."""
    return QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)


class MainWindow(QMainWindow):
    def __init__(self, library: str | None = None):
        super().__init__()
        self.qsettings = settings.open_settings()
        self.cache_file = str(settings.cache_path())
        self.undo_stack: list[dict] = []
        self.scan_thread: ScanThread | None = None
        self._scan_seen: set[str] = set()
        self._current_path: str | None = None
        self._report_windows: list = []
        self._duplicates_window = None
        self._removed_window = None

        if library:
            settings.put(self.qsettings, "library_folder", library)
        self.root = settings.get(self.qsettings, "library_folder")

        self._build_ui()
        self._build_actions()
        self._restore_state()
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        self.model = RecordingsModel(self, live_edit=True)
        self.model.editRequested.connect(self.apply_cell_edit)
        self.proxy = RecordingsProxy(self)
        self.proxy.setSourceModel(self.model)

        self.tree_model = QStandardItemModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Ctrl/Shift-click several projects, e.g. for a sound report per project.
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.selectionModel().currentChanged.connect(self._scope_changed)
        self._tree_built = False
        self.grouping = QComboBox()
        self.grouping.addItem("By year and month", "date")
        self.grouping.addItem("By project name", "name")
        self.grouping.addItem("By recorder", "recorder")
        self.grouping.setToolTip("How the sidebar lists projects: by year, then month; A to Z by name; or by "
                                 "the recorder that made them")
        self.grouping.setCurrentIndex(max(self.grouping.findData(settings.get(self.qsettings, "library_grouping")), 0))
        self.grouping.currentIndexChanged.connect(self._grouping_changed)
        tree_panel = QWidget()
        tree_layout = QVBoxLayout(tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        self.counts_button = QToolButton()
        self.counts_button.setText("#")
        self.counts_button.setToolTip("What the numbers in the sidebar count")
        self.counts_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        counts_menu = QMenu(self.counts_button)
        self._count_actions = {}
        for key, label in (("projects", "Count Projects"), ("files", "Count Files"),
                           ("both", "Count Projects and Files (projects / files)")):
            action = counts_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(settings.get(self.qsettings, "library_counts") == key)
            action.triggered.connect(lambda _on, k=key: self._counts_changed(k))
            self._count_actions[key] = action
        self.counts_button.setMenu(counts_menu)
        grouping_row = QHBoxLayout()
        grouping_row.setContentsMargins(0, 0, 0, 0)
        grouping_row.addWidget(self.grouping, 1)
        grouping_row.addWidget(self.counts_button)
        tree_layout.addLayout(grouping_row)
        tree_layout.addWidget(self.tree, 1)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Double-click File, Scene, Take or Note to edit it (written at once, undoable);
        # double-click ★ to circle a take; anything else plays.
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 8)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        header.setHighlightSections(False)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        for col, (_, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(col, width)
        header.setSectionResizeMode(COL["★"], QHeaderView.ResizeMode.Fixed)
        self.table.sortByColumn(COL["Date"], Qt.SortOrder.AscendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(self._table_double_clicked)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setMinimumWidth(240)
        # Notes for the selected take, written into the file (and its _ISO/_LR partner).
        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText("Select a recording to add notes")
        self.note_edit.setMinimumHeight(60)
        self.note_edit.setToolTip(f"Written into the file's metadata (iXML NOTE) with Save Note or {keys('Ctrl+Return')}")
        self.note_edit.textChanged.connect(self._note_edited)
        self.note_save = QPushButton("Save Note")
        self.note_save.setToolTip(f"Write the note into the file ({keys('Ctrl+Return')})")
        self.note_save.clicked.connect(self.save_note)
        self.note_revert = QPushButton("Revert")
        self.note_revert.clicked.connect(self._load_note)
        QShortcut(QKeySequence("Ctrl+Return"), self.note_edit, self.save_note,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("Ctrl+Enter"), self.note_edit, self.save_note,
                  context=Qt.ShortcutContext.WidgetShortcut)
        self._note_rec: Recording | None = None
        note_buttons = QHBoxLayout()
        note_buttons.setContentsMargins(0, 0, 0, 0)
        note_buttons.addStretch(1)
        note_buttons.addWidget(self.note_revert)
        note_buttons.addWidget(self.note_save)
        self.note_label = QLabel("<b>Notes</b>")
        notes = QWidget()
        notes_layout = QVBoxLayout(notes)
        notes_layout.setContentsMargins(0, 4, 0, 0)
        notes_layout.addWidget(self.note_label)
        notes_layout.addWidget(self.note_edit, 1)
        notes_layout.addLayout(note_buttons)
        # Details above, notes below; drag the divider to give either more room.
        inspector = QSplitter(Qt.Orientation.Vertical)
        inspector.addWidget(self.details)
        inspector.addWidget(notes)
        inspector.setStretchFactor(0, 1)
        inspector.setChildrenCollapsible(False)
        inspector.setSizes([520, 200])
        self._set_note_rec(None)

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search name, scene, take, note, track, timecode…  ({keys('Ctrl+F')})")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(lambda: self._apply_filter(text=True))
        self.search.textChanged.connect(self._search_timer.start)

        self.player = PlayerWidget(self.cache_file, self.qsettings)
        self.player.stepRequested.connect(self._step_file)
        self.player.message.connect(lambda text: self.statusBar().showMessage(text, 8000))

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self.search)
        middle_layout.addWidget(self.table, 1)

        self.h_split = QSplitter(Qt.Orientation.Horizontal)
        self.h_split.addWidget(tree_panel)
        self.h_split.addWidget(middle)
        self.h_split.addWidget(inspector)
        self.h_split.setStretchFactor(0, 0)
        self.h_split.setStretchFactor(1, 1)
        self.h_split.setStretchFactor(2, 0)
        self.h_split.setSizes([260, 900, 320])

        self.offload = OffloadPage(self.qsettings, lambda: self.root, self, library_recs=lambda: list(self.model.recs))
        self.offload.recordingSelected.connect(self._offload_selected)
        self.offload.copiedToLibrary.connect(self._copied_to_library)
        self.offload.showInLibrary.connect(self.show_folder_in_library)
        self.offload.releaseCard.connect(self._release_card)
        self.offload.log.connect(lambda action, details: self._log(action, **details))

        self.stack = QStackedWidget()
        self.stack.addWidget(self.offload)
        self.stack.addWidget(self.h_split)

        self.v_split = QSplitter(Qt.Orientation.Vertical)
        self.v_split.addWidget(self.stack)
        self.v_split.addWidget(self.player)
        self.v_split.setStretchFactor(0, 1)
        self.v_split.setSizes([520, 480])
        self.setCentralWidget(self.v_split)

        self.status_label = QLabel()
        self.scan_label = QLabel()
        self.scan_cancel = QPushButton("Stop scan")
        self.scan_cancel.setFlat(True)
        self.scan_cancel.clicked.connect(self._cancel_scan)
        self.scan_cancel.hide()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.scan_label)
        self.statusBar().addPermanentWidget(self.scan_cancel)

        self._tree_timer = QTimer(self, singleShot=True, interval=1500)
        self._tree_timer.timeout.connect(self._rebuild_tree)
        self.resize(1500, 900)

    def _action(self, text, slot, shortcut=None, icon=None, tip=None, context=None) -> QAction:
        action = QAction(text, self)
        if icon:
            action.setIcon(QIcon.fromTheme(icon))
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if context is not None:
            action.setShortcutContext(context)
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _build_actions(self):
        win = Qt.ShortcutContext.WindowShortcut
        self.act_folder = self._action("Library Folder…", self.choose_library, "Ctrl+O", "folder-open",
                                       "Choose the folder of recordings to manage")
        self.act_rescan = self._action("Rescan", self.start_scan, "F5", "view-refresh",
                                       "Look for new, changed and removed files")
        self.act_rename = self._action("Rename…", self.rename_selected, "F2", "edit-rename",
                                       "Rename the selected file(s)")
        self.act_metadata = self._action("Edit Metadata…", self.edit_metadata, "Ctrl+E", "document-edit",
                                         "Edit project, scene, take, tape, note and circled")
        self.act_organize = self._action("Reorganize into Folders…", self.organize_selected, "Ctrl+Shift+M",
                                         "folder-new", "Preview and move files into project folders")
        self.act_split = self._action("Split into Track Files…", self.split_selected, None, "edit-cut",
                                      "Make one mono file per track, named after the track, in a folder "
                                      "named after the take")
        self.act_combine = self._action("Combine into Polywav…", self.combine_selected, None, "insert-link",
                                        "Put the tracks of files of one take (e.g. split mono files) into one "
                                        "polywav")
        self.act_duplicates = self._action("Find Duplicates…", self.find_duplicates, "Ctrl+D", "edit-find",
                                           "Find duplicate recordings and duplicate projects, and merge or "
                                           "remove them")
        self.act_undo = self._action("Undo", self.undo, "Ctrl+Z", "edit-undo")
        self.act_undo.setEnabled(False)
        self.act_export = self._action("Export List as CSV…", self.export_csv, "Ctrl+Shift+E", "document-export",
                                       "Save the recordings shown as a CSV file")
        self.act_settings = self._action("Settings…", self.open_settings, "Ctrl+,", "configure")
        self.act_open_folder = self._action("Show in Folder", self.open_folder, None, "folder")
        self.act_copy_path = self._action("Copy Path", self.copy_path, "Ctrl+Shift+C", "edit-copy")
        self.act_circled = self._action("Circled Takes Only", lambda on: self._apply_filter(circled=on),
                                        "Ctrl+Shift+8", "starred", "Show only circled takes")
        self.act_circled.setCheckable(True)
        self.act_search = self._action("Search", lambda: (self.search.setFocus(), self.search.selectAll()), "Ctrl+F")
        self._action("Play/Pause", self.player.toggle_play, "Space", context=win)
        self._action("Back", lambda: self.player.seek_relative(-SEEK_STEP), "Left", context=win)
        self._action("Forward", lambda: self.player.seek_relative(SEEK_STEP), "Right", context=win)
        self._action("Stop", self.player.stop, "Ctrl+.", context=win)
        self._action("Loop", self.player.toggle_loop, "L", context=win)
        self._action("Add Marker", self.player.add_marker, "M", context=win)
        self._action("Previous Marker", lambda: self.player.jump_marker(-1), ",", context=win)
        self._action("Next Marker", lambda: self.player.jump_marker(1), ".", context=win)
        self._action("Quit", self.close, "Ctrl+Q")

        self.act_report = self._action("Sound Report…", self.sound_report, "Ctrl+R", "document-print",
                                       "Make a sound report (PDF / CSV) for the selected files or project")
        self.act_open_project = self._action("Open Project Folder", self.open_project_folder, "Ctrl+Shift+O",
                                             "folder-open", "Open the project's folder in the file manager")

        # Brand + page switch (always visible)
        main = QToolBar("Pages")
        main.setObjectName("pages_toolbar")
        main.setMovable(False)
        main.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.brand = QLabel()
        self.brand.setContentsMargins(6, 0, 10, 0)
        self.brand.setToolTip(APP_NAME)
        main.addWidget(self.brand)
        title = QLabel("<b>LOCATION SOUND</b><br><span style='font-size:small'>File Manager</span>")
        title.setContentsMargins(0, 0, 14, 0)
        main.addWidget(title)
        self.page_group = QActionGroup(self)
        self.act_page_offload = QAction(QIcon.fromTheme("media-flash"), "Offload Card", self)
        self.act_page_library = QAction(QIcon.fromTheme("folder-music"), "Library", self)
        for action, shortcut, page in ((self.act_page_offload, "Ctrl+1", "offload"),
                                       (self.act_page_library, "Ctrl+2", "library")):
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda _=False, p=page: self.set_page(p))
            self.page_group.addAction(action)
            main.addAction(action)
        self.act_page_offload.setToolTip(f"Card → review & notes → sound report → copy to NAS ({keys('Ctrl+1')})")
        self.act_page_library.setToolTip(f"Everything on the NAS: browse, play, rename, re-tag ({keys('Ctrl+2')})")
        main.addSeparator()
        self.addToolBar(main)

        toolbar = QToolBar("Library")
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for action in (self.act_rescan, None, self.act_rename, self.act_metadata, self.act_organize,
                       self.act_duplicates, None,
                       self.act_undo, None, self.act_report, self.act_open_project, None, self.act_circled,
                       self.act_export):
            if action is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(action)
        self.lib_toolbar = toolbar
        self.addToolBar(toolbar)

        right = QToolBar("Settings")
        right.setObjectName("settings_toolbar")
        right.setMovable(False)
        right.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        right.addWidget(spacer)
        self.act_branding = self._action("Report Branding…", self.edit_branding, None, "preferences-desktop-theme",
                                         "Your logo, title, colours and footer on sound reports")
        settings_menu = QMenu(self)
        self.act_setup = self._action("Setup…", self.open_setup, None, "preferences-system",
                                      "Your details, branding and folders (the first-run window)")
        settings_menu.addAction(self.act_setup)
        settings_menu.addAction(self.act_settings)
        settings_menu.addAction(self.act_branding)
        settings_button = QToolButton()
        settings_button.setText("Settings")
        settings_button.setIcon(QIcon.fromTheme("configure"))
        settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        settings_button.setMenu(settings_menu)
        right.addWidget(settings_button)
        self.addToolBar(right)
        self._update_brand()
        self.library_actions = [self.act_folder, self.act_rescan, self.act_rename, self.act_metadata,
                                self.act_duplicates,
                                self.act_organize, self.act_split, self.act_combine, self.act_undo, self.act_report, self.act_open_project,
                                self.act_circled, self.act_export, self.act_open_folder, self.act_copy_path,
                                self.act_search]
        self._update_actions()

    # ------------------------------------------------------------ state

    def _restore_state(self):
        geometry = self.qsettings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for key, widget in (("window/h_split", self.h_split), ("window/v_split2", self.v_split)):
            state = self.qsettings.value(key)
            if state is not None:
                widget.restoreState(state)
        header = self.qsettings.value("window/header")
        if header is None or not self.table.horizontalHeader().restoreState(header):
            self.table.setColumnHidden(COL["Status"], True)
        offload_split = self.qsettings.value("window/offload_split")
        if offload_split is not None:
            self.offload.split.restoreState(offload_split)

    def closeEvent(self, event):
        self._cancel_scan()
        if self.scan_thread is not None:
            self.scan_thread.wait(5000)
        self.qsettings.setValue("window/geometry", self.saveGeometry())
        self.qsettings.setValue("window/h_split", self.h_split.saveState())
        self.qsettings.setValue("window/v_split2", self.v_split.saveState())
        self.qsettings.setValue("window/header", self.table.horizontalHeader().saveState())
        self.qsettings.setValue("window/offload_split", self.offload.split.saveState())
        self.qsettings.setValue("window/page", "library" if self.stack.currentIndex() == 1 else "offload")
        self.player.shutdown()
        super().closeEvent(event)

    def _startup(self):
        if not settings.get(self.qsettings, "setup_done"):
            # First run: your details, branding and folders (can be skipped).
            if SetupDialog(self.qsettings, self, first_run=True).exec():
                self.root = settings.get(self.qsettings, "library_folder")
                self.offload._fill_destinations()
            settings.put(self.qsettings, "setup_done", True)
        cards = [c for c in offload.removable_mounts() if offload.looks_like_card(c.path)]
        self.set_page("offload" if cards else self.qsettings.value("window/page", "library"))
        if not self.root or not os.path.isdir(self.root):
            if self.root:
                QMessageBox.warning(self, APP_NAME, f"The library folder is not available:\n{self.root}\n\n"
                                    "Is the network share mounted? Choose it again or pick another folder.")
            if not self.choose_library():
                self._update_status()
                return
            return
        self.load_library()

    def choose_library(self) -> bool:
        start = self.root or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder of recordings", start)
        if not folder:
            return False
        self.root = compat.normpath(folder)
        settings.put(self.qsettings, "library_folder", self.root)
        self.load_library()
        return True

    def load_library(self):
        """Show what the cache knows at once, then rescan in the background."""
        self._cancel_scan()
        self.player.load(None)
        cache = catalog.Cache(self.cache_file)
        try:
            recs = cache.all_under(self.root)
        finally:
            cache.close()
        self._assign(recs)
        self.model.set_recordings(recs, self.root)
        self._rebuild_tree()
        self.setWindowTitle(self.root)
        self._index_waveforms_setting()
        self.offload._fill_destinations()
        self.offload._destination_changed()
        self.offload.library_ready()
        self.start_scan()

    def _index_waveforms_setting(self):
        self.player.set_library_index(self.root if self.root and os.path.isdir(self.root) else "",
                                      settings.get(self.qsettings, "library_index_waveforms_read"),
                                      settings.get(self.qsettings, "library_index_waveforms"))

    def _assign(self, recs):
        catalog.assign_projects(recs, self.root, settings.get(self.qsettings, "container_folders"))

    # ------------------------------------------------------------ scanning

    def start_scan(self):
        if not self.root:
            return
        if self.scan_thread is not None:
            return
        if not os.path.isdir(self.root):
            QMessageBox.warning(self, APP_NAME, f"The library folder is not available:\n{self.root}")
            return
        self._scan_seen = set()
        self.scan_thread = ScanThread(self.root, self.cache_file, self,
                                      read_index=settings.get(self.qsettings, "library_index_read"),
                                      write_index=settings.get(self.qsettings, "library_index"))
        self.scan_thread.batch.connect(self._scan_batch)
        self.scan_thread.progress.connect(self._scan_progress)
        self.scan_thread.finished_scan.connect(self._scan_finished)
        self.scan_label.setText("Scanning…")
        self.scan_cancel.show()
        self.act_rescan.setEnabled(False)
        self.scan_thread.start()

    def _cancel_scan(self):
        if self.scan_thread is not None:
            self.scan_thread.cancel()

    def _scan_batch(self, recs: list[Recording]):
        self._assign(recs)
        new = []
        for rec in recs:
            self._scan_seen.add(rec.path)
            row = self.model.row_of(rec.path)
            if row is None:
                new.append(rec)
            else:
                old = self.model.recs[row]
                if old.size != rec.size or old.mtime != rec.mtime or old.error != rec.error:
                    self.model.replace(rec.path, rec)
        self.model.append(new)
        if new:
            self._tree_timer.start()
            self._update_status()

    def _scan_progress(self, stats, path):
        known = len(self.model.recs)
        text = f"Scanning… {stats.found:,} of about {known:,} files" if known > stats.found else \
            f"Scanning… {stats.found:,} files"
        details = [f"{stats.indexed:,} from the library index"] if stats.indexed else []
        if stats.parsed:
            details.append(f"{stats.parsed:,} read")
        if stats.errors:
            details.append(f"{stats.errors:,} unreadable")
        if details:
            text += f" ({', '.join(details)})"
        self.scan_label.setText(text)
        self.scan_label.setToolTip(path)

    def _scan_finished(self, stats, error):
        thread, self.scan_thread = self.scan_thread, None
        if thread is not None:
            thread.wait()
        self.scan_cancel.hide()
        self.act_rescan.setEnabled(True)
        if error:
            self.scan_label.setText("Scan failed")
            QMessageBox.warning(self, APP_NAME, f"The scan failed:\n{error}")
            return
        if thread is not None and thread._cancel:
            self.scan_label.setText(f"Scan stopped after {stats.found:,} files")
        elif stats.unlisted:
            # Some folders could not be read: keep showing their files rather
            # than treating them as gone.
            self.scan_label.setText(f"Scanned {stats.found:,} files; {len(stats.unlisted):,} folder(s) could "
                                    "not be read (is the share still connected?)")
            self.scan_label.setToolTip("\n".join(stats.unlisted[:20]))
        else:
            gone = [r.path for r in self.model.recs if r.path not in self._scan_seen]
            if gone:
                keep = [r for r in self.model.recs if r.path in self._scan_seen]
                self._reset_keep_selection(keep)
            text = f"Scanned {stats.found:,} files in {stats.seconds:.0f} s"
            details = []
            if stats.indexed:
                details.append(f"{stats.indexed:,} from the library index")
            if stats.parsed:
                details.append(f"{stats.parsed:,} new or changed")
            if gone:
                details.append(f"{len(gone):,} gone")
            if stats.errors:
                details.append(f"{stats.errors:,} unreadable")
            self.scan_label.setText(text + (f" ({', '.join(details)})" if details else ""))
            self.scan_label.setToolTip("The library index was updated" if stats.index_saved else "")
            if stats.index_error:
                self.statusBar().showMessage(f"Could not update the library index: {stats.index_error}", 10000)
        self._rebuild_tree()
        self._update_status()
        self.offload.library_ready()
        if getattr(self, "_show_after_scan", None):
            folder, self._show_after_scan = self._show_after_scan, None
            self._show_folder(folder)
        if getattr(self, "_rescan_after", False):
            self._rescan_after = False
            QTimer.singleShot(0, self.start_scan)

    def _reset_keep_selection(self, recs: list[Recording]):
        selected = {r.path for r in self.selected_recordings()}
        self.model.set_recordings(recs, self.root)
        self._select_paths(selected)

    # ------------------------------------------------------------ tree / filter

    @staticmethod
    def _scope_of(index: QModelIndex) -> tuple:
        """(project, day, period, recorder) of a sidebar node; all None for "All recordings"."""
        if not index.isValid():
            return (None, None, None, None)
        return (index.data(PROJECT_ROLE), index.data(DAY_ROLE), index.data(PERIOD_ROLE), index.data(RECORDER_ROLE))

    @staticmethod
    def _is_scope(scope: tuple) -> bool:
        """A project, day, year, month or recorder (anything narrower than all recordings)."""
        return scope[0] is not None or scope[2] is not None or scope[3] is not None

    def _tree_indexes(self, parent: QModelIndex = QModelIndex()):
        for row in range(self.tree_model.rowCount(parent)):
            index = self.tree_model.index(row, 0, parent)
            yield index
            yield from self._tree_indexes(index)

    def _reveal(self, index: QModelIndex):
        parent = index.parent()
        while parent.isValid():
            self.tree.setExpanded(parent, True)
            parent = parent.parent()

    def _counts_changed(self, key: str):
        settings.put(self.qsettings, "library_counts", key)
        for k, action in self._count_actions.items():
            action.setChecked(k == key)
        self._rebuild_tree()

    def _grouping_changed(self, *_):
        settings.put(self.qsettings, "library_grouping", self.grouping.currentData())
        self._tree_built = False
        self._rebuild_tree()

    def _rebuild_tree(self):
        scope = self._scope_of(self.tree.currentIndex())
        expanded = {self._scope_of(i) for i in self._tree_indexes() if self.tree.isExpanded(i)}
        self.tree.selectionModel().blockSignals(True)
        by_date = self.grouping.currentData() == "date"
        build_project_tree(self.tree_model, self.model.recs, by_date=by_date,
                           counts=settings.get(self.qsettings, "library_counts"),
                           by_recorder=self.grouping.currentData() == "recorder")
        target = self.tree_model.index(0, 0)
        for index in self._tree_indexes():
            key = self._scope_of(index)
            if key in expanded:
                self.tree.setExpanded(index, True)
            if key == scope and self._is_scope(scope):
                target = index
        if not self._tree_built and by_date and self.model.recs and self.tree_model.rowCount() > 1:
            self.tree.setExpanded(self.tree_model.index(1, 0), True)  # the newest year
        self._tree_built = bool(self.model.recs)
        self._reveal(target)
        self.tree.setCurrentIndex(target)
        self.tree.selectionModel().blockSignals(False)
        scope = self._scope_of(target)
        if scope != (self.proxy.project, self.proxy.day, self.proxy.period, self.proxy.recorder):
            self.proxy.set_scope(*scope)
        self._update_status()

    def _scope_changed(self, current: QModelIndex, _previous=None):
        if current.isValid():
            self.proxy.set_scope(*self._scope_of(current))
            self._update_status()
            self._update_actions()

    def _apply_filter(self, text=False, circled=None):
        if text:
            self.proxy.set_text(self.search.text())
        if circled is not None:
            self.proxy.set_circled_only(circled)
        self._update_status()

    def _update_status(self):
        total = len(self.model.recs)
        projects = len({r.project or NO_PROJECT for r in self.model.recs})
        shown = self.proxy.rowCount()
        selected = len(self.table.selectionModel().selectedRows()) if self.table.selectionModel() else 0
        text = f"{total:,} recordings in {projects:,} projects"
        if shown != total:
            text += f" · showing {shown:,}"
        if selected > 1:
            text += f" · {selected:,} selected"
        if not self.root:
            text = f"Choose a library folder to start ({keys('Ctrl+O')})"
        self.status_label.setText(text)

    # ------------------------------------------------------------ selection

    def selected_recordings(self) -> list[Recording]:
        rows = sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row())
        return [index.data(REC_ROLE) for index in rows]

    def current_recording(self) -> Recording | None:
        index = self.table.currentIndex()
        if index.isValid() and self.table.selectionModel().isRowSelected(index.row(), QModelIndex()):
            return index.data(REC_ROLE)
        recs = self.selected_recordings()
        return recs[0] if len(recs) == 1 else None

    def _selection_changed(self, *_):
        recs = self.selected_recordings()
        rec = self.current_recording() if recs else None
        if rec is not None and rec.path != self._current_path:
            self._current_path = rec.path
            self.player.load(rec)
            self.player.prefetch(self._neighbours(rec))
        elif rec is None and not recs:
            self._current_path = None
            self.player.load(None)
        self._show_details(rec if rec is not None else None, len(recs))
        self._set_note_rec(rec)
        self._update_actions()
        self._update_status()

    def _step_file(self, step: int):
        """The player's previous / next buttons: move the selection in the table
        (or in the Offload review table)."""
        table = self.table if self.stack.currentIndex() == 1 else getattr(self.offload, "table", None)
        if table is None or table.model().rowCount() == 0:
            return
        current = table.currentIndex()
        row = min(max((current.row() if current.isValid() else -1) + step, 0), table.model().rowCount() - 1)
        index = table.model().index(row, 0)
        table.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect |
                                               QItemSelectionModel.SelectionFlag.Rows)
        table.scrollTo(index)

    def _neighbours(self, rec: Recording, count: int = 3) -> list[Recording]:
        """The next rows in the table (then the previous one), for waveform prefetch."""
        row = self.model.row_of(rec.path)
        if row is None:
            return []
        view_row = self.proxy.mapFromSource(self.model.index(row, 0)).row()
        rows = [view_row + i for i in range(1, count + 1)] + [view_row - 1]
        return [self.proxy.index(r, 0).data(REC_ROLE) for r in rows if 0 <= r < self.proxy.rowCount()]

    def _select_paths(self, paths: set[str], current: str | None = None):
        selection = self.table.selectionModel()
        selection.clearSelection()
        first = None
        for path in paths:
            row = self.model.row_of(path)
            if row is None:
                continue
            index = self.proxy.mapFromSource(self.model.index(row, 0))
            if not index.isValid():
                continue
            selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            if first is None or path == current:
                first = index
        if first is not None:
            selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(first)

    def _update_actions(self):
        if hasattr(self, "stack") and self.stack.currentIndex() != 1:
            return
        recs = self.selected_recordings() if hasattr(self, "table") else []
        ok = [r for r in recs if not r.error]
        self.act_rename.setEnabled(bool(recs))
        self.act_rename.setText("Rename…" if len(recs) <= 1 else f"Rename {len(recs)} Files…")
        self.act_metadata.setEnabled(bool(ok))
        self.act_organize.setEnabled(bool(recs))
        self.act_split.setEnabled(any(splitter.splittable(r) is None for r in recs))
        self.act_combine.setEnabled(len(ok) >= 2)
        self.act_open_folder.setEnabled(len(recs) >= 1)
        self.act_copy_path.setEnabled(bool(recs))
        for action in (self.act_folder, self.act_rescan, self.act_circled, self.act_export, self.act_search,
                       self.act_duplicates):
            action.setEnabled(True)
        self.act_rescan.setEnabled(self.scan_thread is None)
        self.act_undo.setEnabled(bool(self.undo_stack))
        has_scope = bool(recs) or self._is_scope(self._scope_of(self.tree.currentIndex()))
        self.act_report.setEnabled(has_scope)
        self.act_open_project.setEnabled(has_scope)

    def _show_details(self, rec: Recording | None, count: int):
        if rec is None:
            self.details.setHtml(f"<p>{count:,} files selected.</p>" if count > 1 else "")
            return
        rows = [("File", rec.name), ("Folder", rec.folder)]
        if rec.error:
            rows.append(("Problem", rec.error))
        else:
            project = rec.project + (" <i>(from folder)</i>" if rec.project_from == "folder" else "")
            rows += [("Project", None, project or "—"), ("Scene", rec.scene), ("Take", rec.take),
                     ("Circled", "★ yes" if rec.circled else "no"), ("Tape", rec.tape), ("Note", rec.note),
                     ("Start TC", rec.start_tc), ("End TC", rec.end_tc),
                     ("Frame rate", rec.rate_label), ("User bits", rec.ubits),
                     ("Length", f"{rec.duration:.3f} s"),
                     ("Format", rec.format_label + (f" · {rec.form}" if rec.form != "RIFF" else "")),
                     ("Size", f"{rec.size / 1e6:,.1f} MB"), ("Recorded", f"{rec.date} {rec.time}".strip()),
                     ("Recorder", rec.recorder)]
            if rec.original_filename and rec.original_filename != rec.name:
                rows.append(("Recorded as", rec.original_filename))
            family = catalog.family_members(rec, self.model.recs)
            if family:
                rows.append(("Same take", ", ".join(r.name for r in family)))
            meta = [name for name, present in (("BWF", rec.has_bext), ("iXML", rec.has_ixml)) if present]
            rows.append(("Metadata", " + ".join(meta) or "none"))
            if rec.truncated:
                rows.append(("Warning", "the audio data is incomplete (recording cut short?)"))
        html = ["<table cellspacing='0' cellpadding='2'>"]
        for row in rows:
            label, value = row[0], (row[2] if len(row) == 3 else escape(row[1] or ""))
            if value:
                html.append(f"<tr><td style='color:gray;padding-right:8px' valign='top'>{label}</td>"
                            f"<td>{value}</td></tr>")
        html.append("</table>")
        if not rec.error and rec.tracks:
            html.append("<p style='margin-top:8px'><b>Tracks</b></p><table cellspacing='0' cellpadding='1'>")
            for i, name in enumerate(rec.tracks, 1):
                html.append(f"<tr><td style='color:gray;padding-right:8px'>{i}</td><td>{escape(name)}</td></tr>")
            html.append("</table>")
        self.details.setHtml("".join(html))

    # ------------------------------------------------------------ editing in the table / inspector

    def _table_double_clicked(self, index: QModelIndex):
        """Editable cells open an editor (and never get here); ★ circles the
        take; anything else plays."""
        if index.column() == COL["★"]:
            rec = index.data(REC_ROLE)
            if rec is not None and not rec.error:
                self.apply_cell_edit(rec, "circled", not rec.circled)
            return
        self.player.toggle_play()

    def _take_partners(self, rec: Recording) -> list[Recording]:
        """The take's other files (_ISO / _LR) when edits include them."""
        if not settings.get(self.qsettings, "apply_to_take_family"):
            return []
        return [r for r in catalog.family_members(rec, self.model.recs) if not r.error]

    def apply_cell_edit(self, rec: Recording, key: str, value) -> None:
        """Write one edit from the table or the notes box at once (undoable).
        Scene / take changes rename the file to match (a setting), and the
        take's other files get the same change."""
        row = self.model.row_of(rec.path)
        if row is None:
            return
        rec = self.model.recs[row]
        partners = self._take_partners(rec)
        embed = settings.get(self.qsettings, "write_embedded_filename")
        if key == "name":
            pairs = [(rec, value)] + [(r, n) for r in partners if (n := partner_name(r.name, rec.name, value))]
            if self._check_new_names(pairs, "rename"):
                label = f"rename of {rec.name}" + _and_partners([r for r, _ in pairs[1:]])
                self.do_renames([(r, Path(r.path).with_name(n)) for r, n in pairs], embed=embed, label=label)
            return
        targets = [rec] + partners
        changes = {key: value}
        renames: list[tuple[Recording, str]] = []
        if key in ("scene", "take") and settings.get(self.qsettings, "rename_on_scene_take"):
            for r in targets:
                if splitter.is_track_file(r):
                    continue  # named after its track (a split take), not after the scene and take
                new_scene = value if key == "scene" else r.scene
                new_take = value if key == "take" else r.take
                name = scene_take_name(r.name, r.scene, r.take, new_scene, new_take)
                if name:
                    renames.append((r, name))
            if renames and not self._check_new_names(renames, key):
                return
        what = {"note": "note", "circled": "circle", "scene": "scene", "take": "take"}[key]
        label = f"{what} of {rec.name}" + _and_partners(partners)
        old_values = [(r.path, _values_of(r, changes)) for r in targets]
        done = self.do_metadata([(r, changes) for r in targets], title="Writing metadata")
        if not done:
            return
        metadata_part = {"kind": "metadata", "label": label,
                         "entries": [(path, values) for path, values in old_values if path in done]}
        self._log("metadata", changes=changes, files=sorted(done))
        renames = [(r, n) for r, n in renames if r.path in done]
        rename_part = None
        if renames:
            # The files were read again after the write: rename the current rows.
            current = [(self.model.recs[self.model.row_of(r.path)], n) for r, n in renames
                       if self.model.row_of(r.path) is not None]
            rename_part = self.do_renames([(r, Path(r.path).with_name(n)) for r, n in current], embed=embed,
                                          label=label, push_undo=False)
        if rename_part is not None:
            self._push_undo({"kind": "compound", "label": label, "parts": [metadata_part, rename_part]})
            self.statusBar().showMessage(
                "Renamed " + ", ".join(f"{r.name} → {n}" for r, n in renames) + f"  ({keys('Ctrl+Z')} undoes)", 10000)
        else:
            self._push_undo(metadata_part)

    def _check_new_names(self, pairs: list[tuple[Recording, str]], what: str) -> bool:
        """New names must be valid and free (also among themselves); says why not."""
        problems, seen = [], set()
        for r, name in pairs:
            error = validate_name(name)
            target = compat.join(r.folder, name)
            if error:
                problems.append(f"{name}: {error}")
            elif target in seen or (os.path.exists(target) and target.casefold() != r.path.casefold()):
                problems.append(f"{name}: a file with that name is already in {os.path.basename(r.folder)}")
            seen.add(target)
        if problems:
            QMessageBox.warning(self, APP_NAME, f"Nothing was changed. The {what} would give "
                                + ("this name, which can't be used:" if len(problems) == 1 else
                                   "names that can't be used:") + "\n\n" + "\n".join(problems))
            return False
        return True

    # Notes box (under the inspector)

    def _set_note_rec(self, rec: Recording | None) -> None:
        """Show the note of the selected recording, first offering to save an
        unsaved note of the previous one."""
        previous = self._note_rec
        if previous is not None and rec is not None and previous.path == rec.path:
            self._note_rec = rec
            if not self.note_save.isEnabled():  # nothing typed: show what the file says now
                self._load_note()
            return
        if previous is not None and self.note_save.isEnabled() and self.model.row_of(previous.path) is not None:
            answer = QMessageBox.question(
                self, "Unsaved note", f"Save the note you typed for {previous.name}?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                self._note_rec = None
                self.apply_cell_edit(previous, "note", self.note_edit.toPlainText().strip())
        self._note_rec = rec if rec is not None and not rec.error else None
        usable = self._note_rec is not None
        self.note_edit.setEnabled(usable)
        self.note_edit.setPlaceholderText("Notes for this take…" if usable else "Select one recording to add notes")
        self.note_label.setText(f"<b>Notes</b> · {escape(rec.name)}" if usable else "<b>Notes</b>")
        self._load_note()

    def _load_note(self) -> None:
        self.note_edit.blockSignals(True)
        self.note_edit.setPlainText(self._note_rec.note if self._note_rec is not None else "")
        self.note_edit.blockSignals(False)
        self._note_edited()

    def _note_edited(self) -> None:
        rec = self._note_rec
        dirty = rec is not None and self.note_edit.toPlainText().strip() != rec.note.strip()
        self.note_save.setEnabled(dirty)
        self.note_revert.setEnabled(dirty)

    def save_note(self) -> None:
        rec = self._note_rec
        if rec is None or not self.note_save.isEnabled():
            return
        self.apply_cell_edit(rec, "note", self.note_edit.toPlainText().strip())
        row = self.model.row_of(rec.path)
        if row is not None:
            self._note_rec = self.model.recs[row]
        self._load_note()

    # ------------------------------------------------------------ menus

    def _table_menu(self, pos):
        recs = self.selected_recordings()
        if not recs:
            return
        menu = QMenu(self)
        play = menu.addAction("Pause" if self.player.is_playing() else "Play")
        play.triggered.connect(self.player.toggle_play)
        play.setEnabled(len(recs) == 1)
        menu.addSeparator()
        for action in (self.act_rename, self.act_metadata, self.act_organize, self.act_split, self.act_combine):
            menu.addAction(action)
        family = self._family_of(recs)
        if family:
            select = menu.addAction(f"Select Other Files of This Take ({len(family)})")
            select.triggered.connect(lambda: self._select_paths({r.path for r in recs + family}))
        menu.addSeparator()
        report_action = menu.addAction("Sound Report for Selected…")
        report_action.triggered.connect(lambda: self.sound_report(recs))
        menu.addAction(self.act_open_folder)
        menu.addAction(self.act_open_project)
        menu.addAction(self.act_copy_path)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _tree_menu(self, pos):
        index = self.tree.indexAt(pos)
        scope = self._scope_of(index)
        if not self._is_scope(scope):
            return
        project, day, period, recorder = scope
        recs = self._recs_in_scope(*scope)
        menu = QMenu(self)
        if project is None:  # a year, a month, "no date" or a recorder
            label = recorder if recorder is not None else \
                period_label(period) + (f" {period[:4]}" if len(period or "") == 7 else "")
            projects = len({r.project or NO_PROJECT for r in recs})
            report_action = menu.addAction(f"Sound Reports for the {projects:,} Project"
                                           f"{'s' if projects != 1 else ''} in {label}…")
            report_action.triggered.connect(lambda: self.sound_report(recs))
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        label = project if day is None else f"{project} / {day}"
        rename = menu.addAction(f"Set Project Name for These {len(recs):,} Files…")
        rename.triggered.connect(lambda: self.edit_metadata(recs, focus="project"))
        organize_action = menu.addAction(f"Reorganize '{label}' into Folders…")
        organize_action.triggered.connect(lambda: self.organize_selected(recs))
        menu.addSeparator()
        chosen = {i.data(PROJECT_ROLE) for i in self.tree.selectionModel().selectedIndexes()
                  if self._is_scope(self._scope_of(i))}
        if len(chosen) > 1 and project in chosen:
            report_action = menu.addAction(f"Sound Reports for {len(chosen)} Projects…")
            report_action.triggered.connect(lambda: self.sound_report())
        else:
            report_action = menu.addAction(f"Sound Report for '{label}'…")
            report_action.triggered.connect(lambda: self.sound_report(recs))
        open_action = menu.addAction("Open Project Folder")
        open_action.triggered.connect(lambda: self.open_project_folder(recs))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _header_menu(self, pos):
        menu = QMenu(self)
        header = self.table.horizontalHeader()
        for col, (name, _) in enumerate(COLUMNS):
            if col == COL["File"]:
                continue
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(col))
            action.toggled.connect(lambda on, c=col: header.setSectionHidden(c, not on))
        menu.exec(header.mapToGlobal(pos))

    def _recs_in_scope(self, project, day, period=None, recorder=None) -> list[Recording]:
        return [r for r in self.model.recs
                if (project is None or (r.project or NO_PROJECT) == project)
                and (day is None or (catalog.day_of(r) or "(No date)") == day) and in_period(r, period)
                and (recorder is None or recorder_label(r) == recorder)]

    def _family_of(self, recs: list[Recording]) -> list[Recording]:
        chosen = {r.path for r in recs}
        extra: dict[str, Recording] = {}
        by_family: dict[tuple[str, str], list[Recording]] = {}
        for rec in self.model.recs:
            if rec.family:
                by_family.setdefault((rec.family, rec.folder), []).append(rec)
        for rec in recs:
            for other in by_family.get((rec.family, rec.folder), []) if rec.family else []:
                if other.path not in chosen:
                    extra[other.path] = other
        return list(extra.values())

    # ------------------------------------------------------------ pages

    def set_page(self, page: str):
        library = page == "library"
        self.stack.setCurrentIndex(1 if library else 0)
        (self.act_page_library if library else self.act_page_offload).setChecked(True)
        self.lib_toolbar.setVisible(library)
        # Disabled actions also give their shortcuts (F2, Ctrl+E, ...) back to
        # the Offload table, where F2 edits a cell.
        for action in self.library_actions:
            action.setVisible(library or action in (self.act_settings,))
        if library:
            self._update_actions()
        else:
            for action in self.library_actions:
                action.setEnabled(False)
        self.search.setEnabled(library)

    def _update_brand(self):
        dark = self.palette().color(self.palette().ColorRole.Window).lightness() < 128
        mark = QPixmap(str(ASSETS / ("mark-white.png" if dark else "mark-black.png")))
        ratio = self.devicePixelRatioF()
        mark = mark.scaledToHeight(int(38 * ratio), Qt.TransformationMode.SmoothTransformation)
        mark.setDevicePixelRatio(ratio)
        self.brand.setPixmap(mark)

    def changeEvent(self, event):
        if event.type() == event.Type.PaletteChange and hasattr(self, "brand"):
            self._update_brand()
        super().changeEvent(event)

    def _offload_selected(self, rec):
        if rec is None:
            if self.player.rec is not None and self.offload.is_card_path(self.player.rec.path):
                self.player.load(None)
            return
        if self.player.rec is None or self.player.rec.path != rec.path:
            self._current_path = rec.path
            self.player.load(rec)

    def _release_card(self):
        if self.player.rec is not None and self.offload.is_card_path(self.player.rec.path):
            self.player.load(None)

    def _copied_to_library(self, folders: list[str]):
        if self.scan_thread is None:
            self.start_scan()
        else:
            self._rescan_after = True

    def show_folder_in_library(self, folder: str):
        self.set_page("library")
        if self.scan_thread is not None:
            self._show_after_scan = folder
            return
        self._show_folder(folder)

    def _show_folder(self, folder: str):
        prefix = folder.rstrip("/") + "/"
        inside = [r for r in self.model.recs if r.path.startswith(prefix)]
        if not inside:
            return
        project = max({r.project for r in inside}, key=lambda p: sum(1 for r in inside if r.project == p))
        newest = max((r.date for r in inside if r.project == project), default="")
        matches = [i for i in self._tree_indexes()
                   if i.data(PROJECT_ROLE) == (project or NO_PROJECT) and i.data(DAY_ROLE) is None]
        # By date, a project is listed under each month it was recorded: show the latest.
        best = next((i for i in matches if i.data(PERIOD_ROLE) and newest.startswith(i.data(PERIOD_ROLE))),
                    matches[0] if matches else None)
        if best is not None:
            self._reveal(best)
            self.tree.setCurrentIndex(best)
            self.tree.scrollTo(best)

    # ------------------------------------------------------------ reports / project folders

    def _scope_recordings(self) -> list[Recording]:
        """Selected rows, or everything in the sidebar's project/day."""
        recs = [r for r in self.selected_recordings() if not r.error]
        if len(recs) > 1:
            return recs
        scope = self._scope_of(self.tree.currentIndex())
        if self._is_scope(scope):
            return [r for r in self._recs_in_scope(*scope) if not r.error]
        return recs

    def report_groups(self, recs: list[Recording] | None = None) -> list[ReportGroup]:
        """One report per project: from the sidebar selection (several projects
        or days), else the selected files, else the current project / day."""
        if recs is None:
            scopes = [self._scope_of(i) for i in self.tree.selectionModel().selectedIndexes()
                      if self._is_scope(self._scope_of(i))]
            selected = [r for r in self.selected_recordings() if not r.error]
            if len(scopes) > 1 or (scopes and scopes[0][0] is None and len(selected) <= 1):
                recs = [r for scope in scopes for r in self._recs_in_scope(*scope) if not r.error]
            elif len(selected) > 1:
                recs = selected
            else:
                recs = self._scope_recordings()
        by_project: dict[str, list[Recording]] = {}
        seen = set()
        for rec in recs:
            if rec.error or rec.path in seen:
                continue
            seen.add(rec.path)
            by_project.setdefault(rec.project or NO_PROJECT, []).append(rec)
        groups = []
        for project in sorted(by_project, key=str.casefold):
            group_recs = by_project[project]
            days = {catalog.day_of(r) for r in group_recs}
            title = project + (f" / {next(iter(days))}" if len(days) == 1 and len(by_project) == 1 and
                               len(group_recs) < len(self._recs_in_scope(project, None)) else "")
            groups.append(ReportGroup(title, group_recs, self.project_folder_of(group_recs),
                                      project=project if project != NO_PROJECT else ""))
        return groups

    def sound_report(self, recs=None):
        groups = self.report_groups(recs if isinstance(recs, list) else None)
        if not groups:
            QMessageBox.information(self, "Sound Report", "Select files, or one or more projects or days in the list.")
            return
        if len(groups) > 25 and QMessageBox.question(
                self, "Sound Report", f"This makes {len(groups)} separate reports (one per project). Continue?") \
                != QMessageBox.StandardButton.Yes:
            return
        # Not modal: several report windows can be open at once.
        dialog = ReportDialog(groups, self.qsettings, mode="save", parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda _result, d=dialog: self._report_closed(d))
        self._report_windows.append(dialog)
        dialog.show()

    def _report_closed(self, dialog):
        if dialog in self._report_windows:
            self._report_windows.remove(dialog)
        if dialog.saved:
            self._log("sound report", files=dialog.saved)
            self.statusBar().showMessage("Saved " + ", ".join(os.path.basename(p) for p in dialog.saved), 8000)

    def open_setup(self):
        old_root = self.root
        if SetupDialog(self.qsettings, self, first_run=False).exec():
            self.root = settings.get(self.qsettings, "library_folder")
            if self.root != old_root and self.root:
                self.load_library()
            else:
                self.offload._fill_destinations()
                self.offload._destination_changed()

    def edit_branding(self):
        BrandingDialog(self.qsettings, self._scope_recordings()[:8] or None, self).exec()

    def project_folder_of(self, recs: list[Recording]) -> str:
        return catalog.project_folder_of(recs, self.root, settings.get(self.qsettings, "container_folders"))

    def open_project_folder(self, recs=None):
        recs = recs if isinstance(recs, list) else self._scope_recordings()
        folder = self.project_folder_of(recs) if recs else ""
        if folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ------------------------------------------------------------ simple actions

    def open_folder(self):
        recs = self.selected_recordings()
        if recs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(recs[0].folder))

    def copy_path(self):
        recs = self.selected_recordings()
        if recs:
            QGuiApplication.clipboard().setText("\n".join(r.path for r in recs))

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export list", str(Path.home() / "recordings.csv"),
                                              "CSV files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            names = [name if name != "★" else "Circled" for name, _ in COLUMNS]
            writer.writerow(names + ["End TC", "Path"])
            for row in range(self.proxy.rowCount()):
                rec = self.proxy.index(row, 0).data(REC_ROLE)
                values = [self.proxy.index(row, col).data() or "" for col in range(len(COLUMNS))]
                values[COL["★"]] = "yes" if rec.circled else ""
                writer.writerow(values + [rec.end_tc, rec.path])
        self.statusBar().showMessage(f"Saved {self.proxy.rowCount():,} rows to {path}", 6000)

    def open_settings(self):
        old_root = self.root
        old_containers = settings.get(self.qsettings, "container_folders")
        old_index = settings.get(self.qsettings, "library_index")
        dialog = SettingsDialog(self.qsettings, self, recordings=self.model.recs)
        if not dialog.exec():
            return
        self._index_waveforms_setting()
        if dialog.clear_cache_requested:
            self._cancel_scan()
            if self.scan_thread is not None:
                self.scan_thread.wait()
                self.scan_thread = None
            cache = catalog.Cache(self.cache_file)
            cache.clear()
            cache.close()
        self.root = settings.get(self.qsettings, "library_folder")
        if self.root != old_root or dialog.clear_cache_requested:
            self.load_library()
        elif settings.get(self.qsettings, "container_folders") != old_containers:
            self._assign(self.model.recs)
            self.model.set_recordings(self.model.recs, self.root)
            self._rebuild_tree()
        if settings.get(self.qsettings, "library_index") and not old_index:
            # The index is written at the end of a scan.
            if self.scan_thread is None:
                self.start_scan()
            else:
                self._rescan_after = True

    # ------------------------------------------------------------ history

    def _log(self, action: str, **details):
        path = settings.history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **details},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _push_undo(self, entry: dict):
        self.undo_stack.append(entry)
        self.act_undo.setEnabled(True)
        self.act_undo.setText(f"Undo {entry['label']}")

    def undo(self):
        if not self.undo_stack:
            return
        entry = self.undo_stack[-1]
        if settings.get(self.qsettings, "confirm_undo"):
            box = QMessageBox(QMessageBox.Icon.Question, "Undo", f"Undo {entry['label']}?",
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            # Keep a reference: PySide doesn't hand the checkbox to the box, so a
            # temporary one is deleted at once and the box crashes using it.
            dont_ask = QCheckBox("Don't ask again")
            box.setCheckBox(dont_ask)
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            if dont_ask.isChecked():
                settings.put(self.qsettings, "confirm_undo", False)
        self.undo_stack.pop()
        parts = entry["parts"] if entry["kind"] == "compound" else [entry]
        # Parts are undone last-first (e.g. names written after a move, then the move).
        for part in reversed(parts):
            if part["kind"] == "rename":
                self._undo_renames(part)
            elif part["kind"] == "copies":
                self._undo_copies(part)
            else:
                self._undo_metadata(part)
        if entry["kind"] == "compound":
            self.start_scan()  # files come back from the removed-duplicates folder
        self.act_undo.setEnabled(bool(self.undo_stack))
        self.act_undo.setText(f"Undo {self.undo_stack[-1]['label']}" if self.undo_stack else "Undo")

    # ------------------------------------------------------------ renames / moves

    def rename_selected(self):
        recs = self.selected_recordings()
        if not recs:
            return
        embed = settings.get(self.qsettings, "write_embedded_filename")
        if len(recs) == 1:
            dialog = RenameDialog(recs[0], embed, self)
            if not dialog.exec():
                return
            pairs = [(recs[0], Path(recs[0].path).with_name(dialog.new_name()))]
            embed = dialog.embedded.isChecked()
        else:
            dialog = BatchRenameDialog(recs, embed, self)
            if not dialog.exec():
                return
            pairs = [(m.rec, m.dst) for m in dialog.moves if not m.unchanged and not m.error]
            embed = dialog.embedded.isChecked()
        label = f"rename of {pairs[0][0].name}" if len(pairs) == 1 else f"rename of {len(pairs)} files"
        self.do_renames(pairs, embed=embed, label=label)

    def organize_selected(self, recs=None):
        recs = recs if isinstance(recs, list) else self.selected_recordings()
        if not recs:
            return
        dialog = OrganizeDialog(recs, self.root, self.qsettings, self)
        if not dialog.exec():
            return
        moves = dialog.approved_moves()
        if not moves:
            return
        self.do_renames([(m.rec, m.dst) for m in moves], embed=False, label=f"move of {len(moves):,} files",
                        remove_empty=dialog.remove_empty.isChecked())

    def split_selected(self):
        """Split multi-track files into one file per track. The original is
        kept in the take folder, moved to the removed folder (undoable) or
        deleted permanently (then there is no undo step)."""
        recs = self.selected_recordings()
        if not recs:
            return
        dialog = SplitDialog(recs, lambda r: catalog.family_members(r, self.model.recs), self.qsettings, self)
        if not dialog.exec():
            return
        plan, root = dialog.plan, self.root
        if not plan.splits or plan.problems:
            return
        delete = dialog.delete.isChecked()
        leaving = [r for r, _ in plan.splits if r.path not in plan.originals_in_folder]
        originals = [(r, plan.kept_path(r)) for r, _ in plan.splits if r.path in plan.originals_in_folder]
        if not delete:
            originals += [(r, duplicates.removal_path(r.path, root, r.project)) for r in leaving]
        to_delete = [r.path for r in leaving] if delete else []
        moves = [RenameOp(Path(r.path), Path(dst)) for r, dst in originals + plan.partners]
        new_folders = [folder for folder in plan.folders if not os.path.isdir(folder)]
        released = self._release_player({r.path for r, _ in originals + plan.partners} | set(to_delete))
        total = sum(r.frames * r.channels * (r.bits // 8) for r, _ in plan.splits) or 1
        mb = 1 << 20

        def work(job):
            written, base = [], 0
            try:
                for rec, files in plan.splits:
                    job.report(base // mb, total // mb, f"Splitting {rec.name} into {len(files)} files…")
                    written += splitter.split_file(
                        rec.path, plan.outputs(rec, files), progress=lambda d, t, b=base: job.report((b + d) // mb, total // mb))
                    base += rec.frames * rec.channels * (rec.bits // 8)
                created: list[Path] = []
                job.report(0, 0, "Moving the original files…")
                applied = apply_renames(moves, created)
            except BaseException:
                # All or nothing: take the track files back out.
                for path in written:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                remove_empty_dirs([Path(f) for f in new_folders])
                raise
            # Only now, with every new file written and checked.
            deleted, delete_errors = _delete_files(to_delete)
            return written, applied, created, deleted, delete_errors

        result, error = run_job(self, "Splitting", work, cancellable=False)
        if error is not None:
            self._restore_player(released, {})
            QMessageBox.warning(self, APP_NAME, f"Nothing was split:\n{error}")
            return
        written, applied, created, deleted, delete_errors = result
        gone = {compat.fwd(op.src) for op in applied if catalog.REMOVED_FOLDER in op.dst.parts} | set(deleted)
        moved = {compat.fwd(op.src): compat.fwd(op.dst) for op in applied if compat.fwd(op.src) not in gone}
        self._drop_from_library(gone)
        self._refresh_moved(moved, reread=False)
        self._restore_player(released, moved)
        self._add_to_library(written)
        self._tree_timer.start()
        first = plan.splits[0][0].name
        label = f"split of {first}" if len(plan.splits) == 1 else f"split of {len(plan.splits)} files"
        if not to_delete:
            self._push_undo({"kind": "compound", "label": label, "parts": [
                {"kind": "copies", "label": label, "paths": written, "created_dirs": new_folders},
                {"kind": "rename", "label": label, "applied": applied, "created": created, "removed_dirs": [],
                 "removed_markers": [], "embed": False}]})
        else:
            # Undoing would delete the new files, now the only copy.
            self._forget_undo_for(set(deleted))
        self._log("split", files=[r.path for r, _ in plan.splits], written=written,
                  moved=[[a, b] for a, b in moved.items()], removed=sorted(gone - set(deleted)),
                  deleted_permanently=deleted)
        self._select_paths(set(written))
        summary = (f"Split {len(plan.splits):,} file(s) into {len(written) - len(plan.remainders):,} track files"
                   + (f" and {len(plan.remainders):,} shrunk original(s)" if plan.remainders else ""))
        if deleted:
            summary += f"; {len(deleted):,} original(s) deleted permanently (no undo)"
        elif gone:
            summary += f"; {len(gone):,} original(s) moved to {catalog.REMOVED_FOLDER}"
        if delete_errors:
            show_report(self, "Split", summary + ". Some originals could not be deleted:", delete_errors)
        else:
            self.statusBar().showMessage(summary + ("" if to_delete else f"  ({keys('Ctrl+Z')} undoes)"), 10000)

    def combine_selected(self):
        """Put the tracks of several files of one take into one polywav (undoable)."""
        recs = [r for r in self.selected_recordings() if not r.error]
        if len(recs) < 2:
            return
        dialog = CombineDialog(recs, self.qsettings, self)
        if not dialog.exec():
            return
        ordered, dst, root = dialog.ordered(), dialog.destination(), self.root
        moves = [RenameOp(Path(r.path), Path(duplicates.removal_path(r.path, root, r.project)))
                 for r in ordered] if dialog.remove.isChecked() else []
        to_delete = [r.path for r in ordered] if dialog.delete.isChecked() else []
        released = self._release_player({r.path for r in ordered} if moves or to_delete else set())

        def work(job):
            job.report(0, 100, f"Writing {os.path.basename(dst)}…")
            splitter.combine_files([r.path for r in ordered], dst,
                                   progress=lambda d, t: job.report(d * 100 // max(t, 1), 100))
            created: list[Path] = []
            try:
                applied = apply_renames(moves, created) if moves else []
            except BaseException:
                os.remove(dst)  # all or nothing
                raise
            deleted, delete_errors = _delete_files(to_delete)  # the new file is written and checked
            return applied, created, deleted, delete_errors

        result, error = run_job(self, "Combining", work, cancellable=False)
        if error is not None:
            self._restore_player(released, {})
            QMessageBox.warning(self, APP_NAME, f"Nothing was combined:\n{error}")
            return
        applied, created, deleted, delete_errors = result
        gone = {compat.fwd(op.src) for op in applied} | set(deleted)
        self._drop_from_library(gone)
        self._restore_player(released, {})
        self._add_to_library([dst])
        self._tree_timer.start()
        label = f"combining into {os.path.basename(dst)}"
        parts = [{"kind": "copies", "label": label, "paths": [dst]}]
        if applied:
            parts.append({"kind": "rename", "label": label, "applied": applied, "created": created,
                          "removed_dirs": [], "removed_markers": [], "embed": False})
        if to_delete:
            self._forget_undo_for(set(deleted))  # undoing would delete the only copy
        else:
            self._push_undo({"kind": "compound", "label": label, "parts": parts} if len(parts) > 1 else parts[0])
        self._log("combine", files=[r.path for r in ordered], written=dst, removed=sorted(gone - set(deleted)),
                  deleted_permanently=deleted)
        self._select_paths({dst})
        summary = f"Combined {len(ordered)} files into {os.path.basename(dst)}"
        if deleted:
            summary += f"; {len(deleted)} deleted permanently (no undo)"
        elif gone:
            summary += f"; they were moved to {catalog.REMOVED_FOLDER}"
        if delete_errors:
            show_report(self, "Combine", summary + ". Some files could not be deleted:", delete_errors)
        else:
            self.statusBar().showMessage(summary + ("" if to_delete else f"  ({keys('Ctrl+Z')} undoes)"), 10000)

    def do_renames(self, pairs: list[tuple[Recording, Path]], *, embed: bool, label: str,
                   remove_empty: bool = False, push_undo: bool = True) -> dict | None:
        """The single entry point for renames and moves. Returns the undo step
        (pushed unless push_undo is False, for a caller that combines steps)."""
        ops = [RenameOp(Path(rec.path), dst) for rec, dst in pairs]
        released = self._release_player({compat.fwd(op.src) for op in ops})
        root = self.root

        def work(job):
            created: list[Path] = []
            job.report(0, len(ops), "Moving files…" if remove_empty else "Renaming files…")
            applied = apply_renames(ops, created, progress=lambda d, t: job.report(d, t))
            removed_dirs, removed_markers = [], []
            if remove_empty:
                removed_dirs, removed_markers = remove_left_empty({op.src.parent for op in applied}, root)
            embed_errors = []
            if embed:
                for i, op in enumerate(applied):
                    job.report(i, len(applied), f"Updating the name inside {op.dst.name}…")
                    try:
                        bwf.update_metadata(op.dst, {}, filename=op.dst.name)
                    except (OSError, bwf.WavError) as error:
                        embed_errors.append(f"{op.dst.name}: name inside the file not updated ({error})")
            return applied, created, removed_dirs, removed_markers, embed_errors

        result, error = run_job(self, "Renaming" if not remove_empty else "Moving files", work, cancellable=False)
        if error is not None:
            self._restore_player(released, {})
            QMessageBox.warning(self, APP_NAME, f"Nothing was changed:\n{error}"
                                if isinstance(error, RenameError) else f"Failed: {error}")
            return None
        applied, created, removed_dirs, removed_markers, embed_errors = result
        mapping = {compat.fwd(op.src): compat.fwd(op.dst) for op in applied}
        self._refresh_moved(mapping, reread=embed)
        self._restore_player(released, mapping)
        entry = None
        if applied:
            entry = {"kind": "rename", "label": label, "applied": applied, "created": created,
                     "removed_dirs": removed_dirs, "removed_markers": removed_markers, "embed": embed}
            if push_undo:
                self._push_undo(entry)
            self._log("rename" if not remove_empty else "move",
                      files=[[compat.fwd(op.src), compat.fwd(op.dst)] for op in applied], embedded_name=embed)
        verb = "Moved" if remove_empty else "Renamed"
        summary = f"{verb} {len(applied):,} file(s)."
        if removed_dirs:
            summary += f" Removed {len(removed_dirs):,} folder(s) left empty."
        if embed_errors:
            show_report(self, verb, summary + " Some names inside the files could not be updated:", embed_errors)
        else:
            self.statusBar().showMessage(summary, 8000)
        return entry

    def _undo_renames(self, entry):
        reverse = undo_ops(entry["applied"])
        released = self._release_player({compat.fwd(op.src) for op in reverse})

        def work(job):
            # Folders removed as "left empty" come back (apply_renames creates
            # missing folders), and so do the recorders' empty marker files.
            applied = apply_renames(reverse, None, progress=lambda d, t: job.report(d, t))
            for folder in sorted(entry["removed_dirs"], key=lambda p: len(p.parts)):
                folder.mkdir(parents=True, exist_ok=True)
            for marker in entry["removed_markers"]:
                if not marker.name == ".DS_Store" and marker.parent.is_dir() and not marker.exists():
                    marker.touch()
            remove_empty_dirs(entry["created"])
            errors = []
            if entry["embed"]:
                for op in applied:
                    try:
                        bwf.update_metadata(op.dst, {}, filename=op.dst.name)
                    except (OSError, bwf.WavError) as error:
                        errors.append(f"{op.dst.name}: {error}")
            return applied, errors

        result, error = run_job(self, "Undoing", work, cancellable=False)
        if error is not None:
            self._restore_player(released, {})
            self.undo_stack.append(entry)
            self.act_undo.setEnabled(True)
            QMessageBox.warning(self, APP_NAME, f"Could not undo:\n{error}")
            return
        applied, errors = result
        mapping = {compat.fwd(op.src): compat.fwd(op.dst) for op in applied}
        self._refresh_moved(mapping, reread=entry["embed"])
        self._restore_player(released, mapping)
        self._log("undo " + entry["label"], files=[[compat.fwd(op.src), compat.fwd(op.dst)] for op in applied])
        if errors:
            show_report(self, "Undo", "Files are back, but some names inside them could not be restored:", errors)
        else:
            self.statusBar().showMessage(f"Undid {entry['label']}", 6000)

    def _refresh_moved(self, mapping: dict[str, str], reread: bool):
        self.player.markers.move(mapping)  # markers follow their files
        cache = catalog.Cache(self.cache_file)
        try:
            for old, new in mapping.items():
                row = self.model.row_of(old)
                if row is None:
                    continue
                if reread:
                    rec = catalog.read_recording(new)
                else:
                    stat = os.stat(new)
                    rec = dataclasses.replace(self.model.recs[row], path=new, size=stat.st_size, mtime=stat.st_mtime)
                self._assign([rec])
                cache.forget([old])
                cache.put(rec, commit=False)
                self.model.replace(old, rec)
                self._scan_seen.add(new)
            cache.db.commit()
        finally:
            cache.close()
        self._tree_timer.start()
        self._selection_changed()

    # ------------------------------------------------------------ duplicates

    def find_duplicates(self):
        if self._duplicates_window is None:
            window = DuplicatesWindow(lambda: list(self.model.recs), lambda: self.root,
                                      lambda: settings.get(self.qsettings, "container_folders"),
                                      self.cache_file, self, runner=self)
            window.cleanupRequested.connect(self.apply_cleanup)
            window.reviewRequested.connect(self.review_removed)
            window.compareRequested.connect(lambda: self.review_removed(compare=True))
            window.emptyFoldersRequested.connect(lambda: self.delete_empty_folders(window))
            window.playRequested.connect(lambda rec: (self.player.load(rec), self.player.toggle_play()))
            self._duplicates_window = window
        self._duplicates_window.show()
        self._duplicates_window.raise_()
        self._duplicates_window.activateWindow()

    def make_cleanup_work(self, plan, known_paths: set[str] | None = None, known_recs: list | None = None):
        """The file work of a cleanup, as fn(job) for a worker thread (job has
        report(done, total, text) and cancelled). Nothing here touches the UI.

        Every removal is checked byte for byte against the kept copy first.
        A file that is not the same file is left where it is, or (review_mode
        "copy" / "move") put next to the kept copy as <name>_ReviewForDeletion.
        Removed files go to the removed-duplicates folder (undoable) unless
        plan.permanent."""
        root = self.root
        reviewing = plan.review_mode in ("copy", "move")
        if known_recs is None:
            known_recs = list(self.model.recs)
        if known_paths is None:
            known_paths = {r.path for r in known_recs}
        # _ISO/_LR takes: the files of each take per folder, to never split one.
        partners: dict[tuple, set[str]] = {}
        for r in known_recs:
            if duplicates.is_take_file(r.name):
                partners.setdefault((r.folder, duplicates.take_key(r)), set()).add(r.path)
        leaving = {r.path for r, _, _ in plan.removals} | {r.path for r, _ in plan.moves} | \
                  {rep[0].path for rep in plan.replacements}

        def work(job):
            out = {"verified": [], "skipped": [], "applied": [], "created": [], "deleted": [], "retagged": [],
                   "need_rewrite": [], "copied": [], "reviewed": [], "removed_dirs": [], "removed_markers": [],
                   "stopped": False, "replaced": []}
            total = sum(r.size for r, _, _ in plan.removals) * 2
            done = [0]

            def advance(n):
                done[0] += n
                job.report(min(done[0], total), total)

            # Nothing measurable yet (a merge may have no checks at all): a moving bar.
            job.report(0, 0, "Getting ready…")

            # _ISO/_LR files never get review copies (they are never renamed).
            reviews = [(r, k) for r, k in plan.reviews if not duplicates.is_take_file(r.name)] if reviewing else []
            for rec, keep, level in plan.removals:
                if job.cancelled:
                    out["skipped"].append(f"{rec.name}: not checked (stopped)")
                    continue
                take_file = duplicates.is_take_file(rec.name)
                if take_file:
                    staying = partners.get((rec.folder, duplicates.take_key(rec)), set()) - {rec.path} - leaving
                    if staying:
                        out["skipped"].append(f"{rec.name} ({rec.folder}): kept together with "
                                              f"{', '.join(os.path.basename(p) for p in sorted(staying))}, which "
                                              "stays in that folder")
                        continue
                job.report(done[0], total, f"Checking {rec.name} against the kept copy…")
                different = False
                try:
                    if not os.path.exists(keep):
                        ok, why = False, "the copy to keep is missing"
                    elif level == "subset":
                        # The kept file has more tracks: every channel of this one must be in it.
                        ok = duplicates.channels_contained(rec.path, keep, cancelled=lambda: job.cancelled,
                                                           progress=advance)
                        why = "not all of its tracks are in the kept file"
                    elif duplicates.files_identical(rec.path, keep, cancelled=lambda: job.cancelled,
                                                    progress=lambda n: advance(n * 2)):
                        ok, why = True, ""
                    elif take_file:
                        # The same _ISO/_LR file again: removed when its audio matches,
                        # whatever its notes say; never a review copy.
                        ok = duplicates.audio_identical(rec.path, keep, cancelled=lambda: job.cancelled)
                        why = "its audio differs from the kept copy"
                    elif reviewing:
                        ok, different, why = False, True, "not byte for byte the same as the kept copy"
                    elif level == "audio":
                        ok = duplicates.audio_identical(rec.path, keep, cancelled=lambda: job.cancelled)
                        why = "its audio differs from the kept copy"
                    else:
                        ok, why = False, "not byte for byte the same as the kept copy"
                except InterruptedError:
                    ok, why = False, "not checked (stopped)"
                except (OSError, bwf.WavError) as error:
                    ok, why = False, str(error)
                if ok:
                    out["verified"].append(rec)
                elif different:
                    reviews.append((rec, keep))
                else:
                    out["skipped"].append(f"{rec.name} ({rec.folder}): kept, {why}")
            # Versions with notes replace the kept copy: only when the audio is the same.
            replacing = []
            for rec, old, *level in plan.replacements:
                level = level[0] if level else "audio"
                if job.cancelled:
                    break
                if duplicates.is_take_file(rec.name):
                    staying = partners.get((rec.folder, duplicates.take_key(rec)), set()) - {rec.path} - leaving
                    if staying:
                        out["skipped"].append(f"{rec.name} ({rec.folder}): not used to replace the kept copy, "
                                              "it stays with its _ISO/_LR partner")
                        continue
                job.report(0, 0, f"Checking {rec.name} against the copy it replaces…")
                try:
                    if level == "subset":
                        # More tracks: every channel of the file it replaces must be in it.
                        same = duplicates.channels_contained(old.path, rec.path, cancelled=lambda: job.cancelled)
                    else:
                        same = duplicates.audio_identical(rec.path, old.path, cancelled=lambda: job.cancelled)
                except (OSError, bwf.WavError, InterruptedError) as error:
                    same = False
                    out["skipped"].append(f"{rec.name}: {error}")
                    continue
                if same:
                    replacing.append((rec, old))
                else:
                    out["skipped"].append(f"{rec.name} ({rec.folder}): kept where it is, "
                                          + ("the file it would replace has tracks that are not in it"
                                             if level == "subset" else "its audio differs from the copy it would "
                                             "replace"))
            if job.cancelled:
                out["stopped"] = True
                return out
            # Where review copies go: next to the kept copy, where it ends up.
            final = {r.path: dst for r, dst in plan.moves}
            taken = set(known_paths) | set(final.values())
            targets = [(rec, duplicates.review_path(rec.path, os.path.dirname(final.get(keep, keep)), taken))
                       for rec, keep in reviews]
            # One all-or-nothing batch: moves, removals into the holding folder,
            # and review files when they are moved.
            ops = [RenameOp(Path(r.path), Path(dst)) for r, dst in plan.moves]
            if not plan.permanent:
                ops += [RenameOp(Path(r.path), Path(duplicates.removal_path(r.path, root, r.project)))
                        for r in out["verified"]]
            # The replaced copy goes to the holding folder (always, so it can be
            # undone) and the version with notes takes its place and name.
            for rec, old in replacing:
                ops.append(RenameOp(Path(old.path), Path(duplicates.removal_path(old.path, root, old.project))))
                ops.append(RenameOp(Path(rec.path), Path(old.path)))
            out["replaced"] = [(rec.path, old.path) for rec, old in replacing]
            if plan.review_mode == "move":
                ops += [RenameOp(Path(r.path), Path(dst)) for r, dst in targets]
                out["reviewed"] = [dst for _, dst in targets]
            job.report(0, 0, f"Moving {len(ops):,} files…" if len(ops) != 1 else "Moving 1 file…")
            out["applied"] = apply_renames(ops, out["created"], progress=lambda d, t: job.report(d, t)) if ops else []
            if plan.permanent:
                for rec in out["verified"]:
                    try:
                        os.remove(rec.path)
                        out["deleted"].append(rec.path)
                    except OSError as error:
                        out["skipped"].append(f"{rec.name}: could not delete ({error})")
            if plan.review_mode == "copy" and targets:
                items = [offload.CopyItem(r.path, dst, r.size) for r, dst in targets]
                job.report(0, 0, "Copying files for review…")
                result = offload.copy_items(items, verify=True, progress=lambda st: job.report(
                    st.done_bytes, max(st.total_bytes, 1), f"Copying {os.path.basename(st.current)} for review…"))
                out["copied"] = [i.dst for i in result.copied]
                out["reviewed"] = list(out["copied"])
                out["skipped"] += [f"{os.path.basename(i.src)}: review copy failed ({why})"
                                   for i, why in result.failed]
            for n, (rec, path, name) in enumerate(plan.retags):
                job.report(n, len(plan.retags), f"Writing the project name into {os.path.basename(path)}…")
                try:
                    bwf.update_metadata(path, {"project": name})
                    out["retagged"].append((path, rec.meta_project))
                except bwf.NeedsRewrite:
                    out["need_rewrite"].append((rec, path, name))
                except (OSError, bwf.WavError) as error:
                    out["skipped"].append(f"{os.path.basename(path)}: project name not written ({error})")
            if plan.remove_empty:
                job.report(0, 0, "Removing empty folders…")
                sources = {Path(r.path).parent for r in out["verified"]} | \
                          {Path(r.path).parent for r, _ in plan.moves} | \
                          {Path(r.path).parent for r, _ in replacing}
                if plan.review_mode == "move":
                    sources |= {Path(r.path).parent for r, _ in targets}
                out["removed_dirs"], out["removed_markers"] = remove_left_empty(sources, root)
            return out

        return work

    @staticmethod
    def cleanup_paths(plan) -> set[str]:
        return {r.path for r, _, _ in plan.removals} | {r.path for r, _ in plan.moves} | \
               {r.path for r, _, _ in plan.retags} | {r.path for r, _ in plan.reviews}

    def finish_cleanup(self, plan, out) -> dict:
        """After the file work: update the library and the log. Returns the
        undo parts, a summary and what was left alone (no dialogs here)."""
        skipped, retagged = out["skipped"], out["retagged"]
        applied = out["applied"]
        gone = {compat.fwd(op.src) for op in applied if catalog.REMOVED_FOLDER in Path(op.dst).parts} | set(out["deleted"])
        moved = {compat.fwd(op.src): compat.fwd(op.dst) for op in applied if compat.fwd(op.src) not in gone}
        self._drop_from_library(gone)
        self._refresh_moved(moved, reread=False)
        self._reread([p for p, _ in retagged])
        self._add_to_library(out["copied"])
        parts = []
        if applied:
            parts.append({"kind": "rename", "label": plan.label, "applied": applied, "created": out["created"],
                          "removed_dirs": out["removed_dirs"], "removed_markers": out["removed_markers"],
                          "embed": False})
        if out["copied"]:
            parts.append({"kind": "copies", "label": plan.label, "paths": list(out["copied"])})
        if retagged:
            parts.append({"kind": "metadata", "label": plan.label,
                          "entries": [(path, {"project": old}) for path, old in retagged]})
        self._log("duplicates", label=plan.label, removed=sorted(gone - set(out["deleted"])),
                  deleted_permanently=sorted(out["deleted"]), moved=[[a, b] for a, b in moved.items()],
                  project_renamed=[p for p, _ in retagged], review=out["reviewed"], review_mode=plan.review_mode)
        freed = sum(r.size for r in out["verified"])
        moved_count = len(moved) - (len(out["reviewed"]) if plan.review_mode == "move" else 0) - \
            len(out.get("replaced", []))
        pieces = []
        if out["verified"]:
            pieces.append(f"removed {len(out['verified'])} duplicate(s) ({human_size(freed)})"
                          + (" permanently" if plan.permanent else f" into “{catalog.REMOVED_FOLDER}”"))
        if moved_count:
            pieces.append(f"moved {moved_count} file(s) into the kept folder")
        if retagged:
            pieces.append(f"wrote the project name into {len(retagged)} file(s)")
        if out.get("replaced"):
            pieces.append(f"replaced {len(out['replaced'])} file(s) with a fuller version (notes or more tracks)")
        if out["reviewed"]:
            pieces.append(f"{'copied' if plan.review_mode == 'copy' else 'moved'} {len(out['reviewed'])} file(s) "
                          f"that weren't the same next to the kept copy as “…{duplicates.REVIEW_TAG}”")
        if out["removed_dirs"]:
            pieces.append(f"removed {len(out['removed_dirs'])} empty folder(s)")
        text = ", ".join(pieces) or "nothing changed"
        return {"parts": parts, "summary": text[0].upper() + text[1:] + ".", "skipped": skipped,
                "moved": moved, "freed": freed, "need_rewrite": out["need_rewrite"]}

    def write_rewrites(self, need_rewrite, skipped: list[str]) -> list:
        """Project names that only fit with a full rewrite: ask, then write.
        Returns the undo part (or None)."""
        if not need_rewrite:
            return None
        recs = [catalog.read_recording(path) for _, path, _ in need_rewrite]
        if not RewriteDialog(recs, self).exec():
            skipped += [f"{os.path.basename(p)}: project name skipped (would need a full rewrite)"
                        for _, p, _ in need_rewrite]
            return None
        written = []

        def rewrite(job):
            failed = []
            for n, (rec, path, name) in enumerate(need_rewrite):
                job.report(n, len(need_rewrite), os.path.basename(path))
                try:
                    bwf.update_metadata(path, {"project": name}, allow_rewrite=True)
                    written.append((path, rec.meta_project))
                except (OSError, bwf.WavError) as err:
                    failed.append(f"{os.path.basename(path)}: {err}")
            return failed
        failed, err = run_job(self, "Writing project names", rewrite)
        skipped += failed or ([str(err)] if err else [])
        self._reread([p for p, _ in written])
        return {"kind": "metadata", "label": "project names",
                "entries": [(path, {"project": old}) for path, old in written]} if written else None

    def cleanups_done(self, label: str, parts: list, summary: str, skipped: list[str]):
        """End of a cleanup or a batch: one undo step, the report, refresh."""
        if parts:
            self._push_undo({"kind": "compound", "label": label, "parts": parts})
        if skipped:
            show_report(self, "Duplicates", summary + " Some files were left alone:", skipped)
        else:
            self.statusBar().showMessage(summary, 12000)
        if self._duplicates_window is not None:
            self._duplicates_window.cleanup_finished()
        if self._removed_window is not None:
            self._removed_window.refresh()
        self._tree_timer.start()

    def apply_cleanup(self, plan):
        """Remove duplicates / merge one project (see make_cleanup_work)."""
        released = self._release_player(self.cleanup_paths(plan))
        work = self.make_cleanup_work(plan)
        result, error = run_job(self, "Removing duplicates" if not plan.moves else "Merging", work)
        if error is not None:
            self._restore_player(released, {})
            QMessageBox.warning(self, APP_NAME, f"Nothing was changed:\n{error}" if isinstance(error, RenameError)
                                else f"Failed: {error}")
            return
        if result["stopped"]:  # stopped while checking: nothing was changed
            self._restore_player(released, {})
            show_report(self, "Duplicates", "Stopped before anything was changed.", result["skipped"])
            return
        done = self.finish_cleanup(plan, result)
        extra = self.write_rewrites(done["need_rewrite"], done["skipped"])
        self._restore_player(released, done["moved"])
        self.cleanups_done(plan.label, done["parts"] + ([extra] if extra else []), done["summary"], done["skipped"])

    def _add_to_library(self, paths: list[str]):
        if not paths:
            return
        cache = catalog.Cache(self.cache_file)
        new = []
        try:
            for path in paths:
                rec = catalog.read_recording(path)
                self._assign([rec])
                cache.put(rec, commit=False)
                self._scan_seen.add(path)
                if self.model.row_of(path) is None:
                    new.append(rec)
            cache.db.commit()
        finally:
            cache.close()
        self.model.append(new)

    # ------------------------------------------------------------ review & delete

    def review_removed(self, compare: bool = False):
        if self._removed_window is None:
            window = RemovedDialog(lambda: self.root, lambda: list(self.model.recs), self)
            window.deleteRequested.connect(self.delete_for_good)
            window.restoreRequested.connect(self.put_back_removed)
            window.swapRequested.connect(self.keep_marked_copy)
            window.playRequested.connect(self._play_path)
            self._removed_window = window
        self._removed_window.refresh()
        if compare:
            self._removed_window.tabs.setCurrentIndex(1)
        self._removed_window.show()
        self._removed_window.raise_()
        self._removed_window.activateWindow()

    def _play_path(self, path: str):
        rec = catalog.read_recording(path)
        self._assign([rec])
        self._current_path = path
        self.player.load(rec)
        self.player.toggle_play()

    def delete_for_good(self, paths: list[str], what: str):
        """Permanently delete files (from Review & Delete; the user confirmed)."""
        if self.player.rec is not None and self.player.rec.path in paths:
            self.player.load(None)
        holding = compat.join(self.root, catalog.REMOVED_FOLDER)

        def work(job):
            deleted, failed = [], []
            for n, path in enumerate(paths):
                job.report(n, len(paths), f"Deleting {os.path.basename(path)}…")
                try:
                    os.remove(path)
                    deleted.append(path)
                except OSError as error:
                    failed.append(f"{os.path.basename(path)}: {error}")
            _tidy_holding(deleted, holding)
            # Folders that held only these files (e.g. review copies) go too.
            remove_left_empty({Path(p).parent for p in deleted}, self.root)
            return deleted, failed

        result, error = run_job(self, "Deleting", work, cancellable=False)
        if error is not None:
            QMessageBox.warning(self, APP_NAME, f"Deleting failed: {error}")
            return
        deleted, failed = result
        self._drop_from_library(set(deleted))
        self._forget_undo_for(set(deleted))
        self._log("delete for good", what=what, files=deleted)
        summary = f"Deleted {len(deleted)} file(s) for good."
        if failed:
            show_report(self, "Delete", summary + " Some could not be deleted:", failed)
        else:
            self.statusBar().showMessage(summary, 8000)
        if self._removed_window is not None:
            self._removed_window.refresh()

    def delete_empty_folders(self, parent=None):
        """Find folders anywhere in the library that hold nothing but recorder
        marker files, list them, and delete them if the user agrees (undoable)."""
        parent = parent or self
        root = self.root
        if not root:
            return

        def find(job):
            return find_empty_folders(root, cancelled=lambda: job.cancelled,
                                      progress=lambda n, path: job.report(0, 0, f"Looked in {n:,} folders… "
                                                                                f"{compat.relpath(path, root)}"))

        folders, error = run_job(parent, "Looking for empty folders", find)
        if error is not None:
            QMessageBox.warning(parent, APP_NAME, f"Could not look for empty folders: {error}")
            return
        if folders is None:
            return
        if not folders:
            QMessageBox.information(parent, "Delete Empty Folders", "There are no empty folders in the library.")
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Delete Empty Folders",
                          f"Delete {len(folders):,} empty folder{'s' if len(folders) != 1 else ''}?", parent=parent)
        box.setInformativeText("They hold no audio or other files, only the recorders' marker files "
                               "(.take_folder, .daily_folder, .DS_Store), also in any subfolders. Each one is "
                               "checked again just before it is deleted. You can undo this.\n\n" +
                               "\n".join(compat.relpath(f, root) for f in folders[:12]) +
                               (f"\n… and {len(folders) - 12:,} more (see Show Details)" if len(folders) > 12 else ""))
        box.setDetailedText("\n".join(compat.relpath(f, root) for f in folders))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("Delete Empty Folders")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        def remove(job):
            dirs, markers, kept = [], [], []
            for n, folder in enumerate(folders):
                job.report(n, len(folders), f"Deleting {compat.relpath(folder, root)}…")
                d, m = remove_tree_if_empty(folder)
                dirs += d
                markers += m
                if folder not in d:
                    kept.append(f"{compat.relpath(folder, root)}: no longer empty, or could not be deleted")
            # Parents emptied by this go as well.
            more_dirs, more_markers = remove_left_empty({f.parent for f in folders}, root)
            return dirs + more_dirs, markers + more_markers, kept

        result, error = run_job(parent, "Deleting empty folders", remove, cancellable=False)
        if error is not None:
            QMessageBox.warning(parent, APP_NAME, f"Deleting failed: {error}")
            return
        dirs, markers, kept = result
        if dirs:
            self._push_undo({"kind": "rename", "label": f"deleting {len(dirs):,} empty folders", "applied": [],
                             "created": [], "removed_dirs": dirs, "removed_markers": markers, "embed": False})
            self._log("delete empty folders", folders=[compat.fwd(d) for d in dirs])
        summary = f"Deleted {len(dirs):,} empty folder{'s' if len(dirs) != 1 else ''}."
        if kept:
            show_report(parent, "Delete Empty Folders", summary + " Some were left:", kept)
        else:
            self.statusBar().showMessage(summary, 8000)
            QMessageBox.information(parent, "Delete Empty Folders", summary)

    def put_back_removed(self, pairs: list[tuple[str, str]]):
        """Move files from the removed-duplicates folder back where they were."""
        blocked = [f"{os.path.basename(dst)}: something is already at {dst}" for _, dst in pairs
                   if os.path.lexists(dst)]
        ops = [RenameOp(Path(src), Path(dst)) for src, dst in pairs if not os.path.lexists(dst)]
        if not ops:
            show_report(self, "Put back", "Nothing was put back.", blocked)
            return
        released = self._release_player({src for src, _ in pairs})

        holding = compat.join(self.root, catalog.REMOVED_FOLDER)

        def work(job):
            created: list[Path] = []
            applied = apply_renames(ops, created, progress=lambda d, t: job.report(d, t))
            _tidy_holding([compat.fwd(op.src) for op in applied], holding)
            return applied, created

        result, error = run_job(self, "Putting files back", work, cancellable=False)
        self._restore_player(released, {})
        if error is not None:
            QMessageBox.warning(self, APP_NAME, f"Nothing was put back:\n{error}")
            return
        applied, created = result
        self._push_undo({"kind": "rename", "label": f"putting back {len(applied)} file(s)", "applied": applied,
                         "created": created, "removed_dirs": [], "removed_markers": [], "embed": False})
        self._add_to_library([compat.fwd(op.dst) for op in applied])
        self._log("put back", files=[[compat.fwd(op.src), compat.fwd(op.dst)] for op in applied])
        summary = f"Put {len(applied)} file(s) back."
        if blocked:
            show_report(self, "Put back", summary + " Some were left in the folder:", blocked)
        else:
            self.statusBar().showMessage(summary, 8000)
        if self._removed_window is not None:
            self._removed_window.refresh()
        self._tree_timer.start()

    def keep_marked_copy(self, marked: str, kept: str, target: str):
        """Keep a _ReviewForDeletion copy instead of its pair: the pair goes to
        the removed-duplicates folder, the marked copy takes its name (undoable)."""
        row = self.model.row_of(kept)
        project = self.model.recs[row].project if row is not None else ""
        released = self._release_player({marked, kept})
        ops = [RenameOp(Path(kept), Path(duplicates.removal_path(kept, self.root, project))),
               RenameOp(Path(marked), Path(target))]

        def work(job):
            created: list[Path] = []
            return apply_renames(ops, created, progress=lambda d, t: job.report(d, t)), created

        result, error = run_job(self, "Keeping the marked copy", work, cancellable=False)
        self._restore_player(released, {})
        if error is not None:
            QMessageBox.warning(self, APP_NAME, f"Nothing was changed:\n{error}")
            return
        applied, created = result
        self._drop_from_library({kept})
        self._refresh_moved({marked: target}, reread=False)
        self._push_undo({"kind": "compound", "label": f"keeping {os.path.basename(marked)}",
                         "parts": [{"kind": "rename", "label": "keep marked copy", "applied": applied,
                                    "created": created, "removed_dirs": [], "removed_markers": [],
                                    "embed": False}]})
        self._log("keep marked copy", marked=marked, kept_moved_to=compat.fwd(ops[0].dst), renamed_to=target)
        self.statusBar().showMessage(f"Kept {os.path.basename(marked)} as {os.path.basename(target)}; the other "
                                     f"copy is in “{catalog.REMOVED_FOLDER}”.", 8000)
        if self._removed_window is not None:
            self._removed_window.refresh()

    def _forget_undo_for(self, deleted: set[str]):
        """Drop undo steps that would need files that are now deleted for good."""
        def touches(entry) -> bool:
            parts = entry["parts"] if entry["kind"] == "compound" else [entry]
            for part in parts:
                if part["kind"] == "rename" and any(compat.fwd(op.dst) in deleted for op in part["applied"]):
                    return True
                if part["kind"] == "copies" and any(p in deleted for p in part["paths"]):
                    return True
            return False
        before = len(self.undo_stack)
        self.undo_stack = [e for e in self.undo_stack if not touches(e)]
        if len(self.undo_stack) != before:
            self.act_undo.setEnabled(bool(self.undo_stack))
            self.act_undo.setText(f"Undo {self.undo_stack[-1]['label']}" if self.undo_stack else "Undo")

    def _undo_copies(self, part):
        """Undo review copies: delete the copies (the originals were never touched)."""
        removed = []
        for path in part["paths"]:
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
        remove_empty_dirs([Path(p) for p in part.get("created_dirs", [])])
        self._drop_from_library(set(removed))
        self._log("undo " + part["label"], deleted_copies=removed)

    def _drop_from_library(self, paths: set[str]):
        if not paths:
            return
        cache = catalog.Cache(self.cache_file)
        try:
            cache.forget(paths)
        finally:
            cache.close()
        self._reset_keep_selection([r for r in self.model.recs if r.path not in paths])
        self._scan_seen -= paths

    # ------------------------------------------------------------ metadata

    def edit_metadata(self, recs=None, focus: str | None = None):
        recs = recs if isinstance(recs, list) else self.selected_recordings()
        recs = [r for r in recs if not r.error]
        if not recs:
            return
        family = self._family_of(recs)
        dialog = MetadataDialog(recs, family, settings.get(self.qsettings, "apply_to_take_family"), self)
        if focus in dialog.edits:
            dialog.edits[focus].setFocus()
            dialog.edits[focus].selectAll()
        if not dialog.exec():
            return
        changes = dialog.changes()
        targets = dialog.targets()
        label = (f"metadata edit of {targets[0].name}" if len(targets) == 1
                 else f"metadata edit of {len(targets):,} files")
        old_values = [(r.path, _values_of(r, changes)) for r in targets]
        done = self.do_metadata([(r, changes) for r in targets], title="Writing metadata")
        if done:
            entries = [(path, values) for path, values in old_values if path in done]
            self._push_undo({"kind": "metadata", "label": label, "entries": entries})
            self._log("metadata", changes=changes, files=sorted(done))

    def do_metadata(self, work_items: list[tuple[Recording, dict]], *, title: str) -> set[str]:
        """Write metadata; files that would need a full copy are only done after
        asking. Returns the paths that were written."""
        released = self._release_player({r.path for r, _ in work_items})

        def write(items, allow_rewrite):
            def work(job):
                written, need_rewrite, errors = [], [], []
                for i, (rec, changes) in enumerate(items):
                    if job.cancelled:
                        errors.append("Stopped: the remaining files were not changed.")
                        break
                    job.report(i, len(items), f"{rec.name}" + (" (copying the file)" if allow_rewrite else ""))
                    try:
                        bwf.update_metadata(rec.path, changes, allow_rewrite=allow_rewrite)
                        written.append(rec.path)
                    except bwf.NeedsRewrite:
                        need_rewrite.append((rec, changes))
                    except (OSError, bwf.WavError) as error:
                        errors.append(f"{rec.name}: {error}")
                return written, need_rewrite, errors
            return run_job(self, title, work)

        result, error = write(work_items, False)
        if error is not None:
            self._restore_player(released, {})
            QMessageBox.warning(self, APP_NAME, f"Writing failed: {error}")
            return set()
        written, need_rewrite, errors = result
        if need_rewrite:
            self._reread(written)
            if RewriteDialog([r for r, _ in need_rewrite], self).exec():
                result, error = write(need_rewrite, True)
                if error is not None:
                    errors.append(str(error))
                else:
                    written += result[0]
                    errors += result[2]
            else:
                errors += [f"{r.name}: skipped (would need a full rewrite)" for r, _ in need_rewrite]
        self._reread(written)
        self._restore_player(released, {})
        summary = f"Updated the metadata of {len(written):,} file(s)."
        if errors:
            show_report(self, "Metadata", summary + " Some files were not changed:", errors)
        else:
            self.statusBar().showMessage(summary, 8000)
        return set(written)

    def _undo_metadata(self, entry):
        items = []
        for path, values in entry["entries"]:
            row = self.model.row_of(path)
            if row is not None:
                items.append((self.model.recs[row], values))
        done = self.do_metadata(items, title="Undoing metadata edit")
        self._log("undo " + entry["label"], files=sorted(done))

    def _reread(self, paths):
        if not paths:
            return
        cache = catalog.Cache(self.cache_file)
        try:
            for path in paths:
                rec = catalog.read_recording(path)
                self._assign([rec])
                cache.put(rec, commit=False)
                self.model.replace(path, rec)
            cache.db.commit()
        finally:
            cache.close()
        self._tree_timer.start()
        self.proxy.invalidate()
        self._selection_changed()

    # ------------------------------------------------------------ player release

    def _release_player(self, paths: set[str]):
        """Close the playing file if it is about to change (SMB can refuse to
        rename or rewrite an open file). Returns the state to restore."""
        if self.player.rec is not None and self.player.rec.path in paths:
            return self.player.release()
        return None

    def _restore_player(self, released, mapping: dict[str, str]):
        if released is None:
            return
        rec, position, _was_playing = released
        path = mapping.get(rec.path, rec.path)
        row = self.model.row_of(path)
        if row is not None:
            self._current_path = path
            self.player.load(self.model.recs[row], position)


def _and_partners(partners: list[Recording]) -> str:
    """" and 101AT01_LR.wav" for undo labels."""
    if not partners:
        return ""
    return f" and {partners[0].name}" if len(partners) == 1 else f" and {len(partners)} other files of the take"


def _values_of(rec: Recording, changes: dict) -> dict:
    values = {}
    for key in changes:
        values[key] = rec.meta_project if key == "project" else getattr(rec, key)
    return values


def _delete_files(paths: list[str]) -> tuple[list[str], list[str]]:
    """Delete files for good: (deleted, error messages)."""
    deleted, errors = [], []
    for path in paths:
        try:
            os.remove(path)
            deleted.append(path)
        except OSError as error:
            errors.append(f"{os.path.basename(path)}: {error.strerror or error}")
    return deleted, errors


def _tidy_holding(paths, holding: str) -> None:
    """Remove folders inside the removed-duplicates folder that are now empty
    (including the holding folder itself when nothing is left)."""
    for folder in sorted({os.path.dirname(p) for p in paths}, key=len, reverse=True):
        while (folder + "/").startswith(holding.rstrip("/") + "/"):
            try:
                os.rmdir(folder)
            except OSError:
                break
            folder = os.path.dirname(folder)
