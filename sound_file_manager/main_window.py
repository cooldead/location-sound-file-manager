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
    QAction, QActionGroup, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPixmap, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QTableView, QTextBrowser, QToolBar, QToolButton,
    QTreeView,
    QVBoxLayout, QWidget,
)

from . import bwf, catalog, offload, settings
from .organize import remove_left_empty
from .catalog import Recording
from .dialogs import (
    BatchRenameDialog, MetadataDialog, OrganizeDialog, RenameDialog, RewriteDialog, SettingsDialog, show_report,
)
from .file_model import (
    COL, COLUMNS, DAY_ROLE, NO_PROJECT, PROJECT_ROLE, REC_ROLE, RecordingsModel, RecordingsProxy,
    build_project_tree,
)
from .offload_page import OffloadPage
from .player import SEEK_STEP, PlayerWidget
from .report import ASSETS
from .branding_dialog import BrandingDialog, SetupDialog
from .report_dialog import ReportDialog, ReportGroup
from .renamer import RenameError, RenameOp, apply_renames, remove_empty_dirs, undo_ops
from .workers import ScanThread, run_job

APP_NAME = "Location Sound File Manager"


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

        if library:
            settings.put(self.qsettings, "library_folder", library)
        self.root = settings.get(self.qsettings, "library_folder")

        self._build_ui()
        self._build_actions()
        self._restore_state()
        QTimer.singleShot(0, self._startup)

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        self.model = RecordingsModel(self)
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

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
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
        self.table.doubleClicked.connect(lambda _index: self.player.toggle_play())

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.setMinimumWidth(240)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, scene, take, note, track, timecode…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(lambda: self._apply_filter(text=True))
        self.search.textChanged.connect(self._search_timer.start)

        self.player = PlayerWidget(self.cache_file)
        self.player.volume.setValue(settings.get(self.qsettings, "volume"))

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self.search)
        middle_layout.addWidget(self.table, 1)

        self.h_split = QSplitter(Qt.Orientation.Horizontal)
        self.h_split.addWidget(self.tree)
        self.h_split.addWidget(middle)
        self.h_split.addWidget(self.details)
        self.h_split.setStretchFactor(0, 0)
        self.h_split.setStretchFactor(1, 1)
        self.h_split.setStretchFactor(2, 0)
        self.h_split.setSizes([260, 900, 320])

        self.offload = OffloadPage(self.qsettings, lambda: self.root, self)
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
        self.v_split.setSizes([700, 170])
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
        self.act_page_offload.setToolTip("Card → review & notes → sound report → copy to NAS (Ctrl+1)")
        self.act_page_library.setToolTip("Everything on the NAS: browse, play, rename, re-tag (Ctrl+2)")
        main.addSeparator()
        self.addToolBar(main)

        toolbar = QToolBar("Library")
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for action in (self.act_rescan, None, self.act_rename, self.act_metadata, self.act_organize, None,
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
                                self.act_organize, self.act_undo, self.act_report, self.act_open_project,
                                self.act_circled, self.act_export, self.act_open_folder, self.act_copy_path,
                                self.act_search]
        self._update_actions()

    # ------------------------------------------------------------ state

    def _restore_state(self):
        geometry = self.qsettings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        for key, widget in (("window/h_split", self.h_split), ("window/v_split", self.v_split)):
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
        self.qsettings.setValue("window/v_split", self.v_split.saveState())
        self.qsettings.setValue("window/header", self.table.horizontalHeader().saveState())
        self.qsettings.setValue("window/offload_split", self.offload.split.saveState())
        self.qsettings.setValue("window/page", "library" if self.stack.currentIndex() == 1 else "offload")
        settings.put(self.qsettings, "volume", self.player.volume.value())
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
        self.root = os.path.normpath(folder)
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
        self.offload._fill_destinations()
        self.offload._destination_changed()
        self.start_scan()

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
        self.scan_thread = ScanThread(self.root, self.cache_file, self)
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
        text = f"Scanning… {stats.found:,} files"
        if stats.parsed:
            text += f" ({stats.parsed:,} read"
            text += f", {stats.errors:,} unreadable)" if stats.errors else ")"
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
        else:
            gone = [r.path for r in self.model.recs if r.path not in self._scan_seen]
            if gone:
                keep = [r for r in self.model.recs if r.path in self._scan_seen]
                self._reset_keep_selection(keep)
            text = f"Scanned {stats.found:,} files in {stats.seconds:.0f} s"
            details = []
            if stats.parsed:
                details.append(f"{stats.parsed:,} new or changed")
            if gone:
                details.append(f"{len(gone):,} gone")
            if stats.errors:
                details.append(f"{stats.errors:,} unreadable")
            self.scan_label.setText(text + (f" ({', '.join(details)})" if details else ""))
        self._rebuild_tree()
        self._update_status()
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

    def _rebuild_tree(self):
        current = self.tree.currentIndex()
        scope = (current.data(PROJECT_ROLE), current.data(DAY_ROLE)) if current.isValid() else (None, None)
        expanded = set()
        for row in range(self.tree_model.rowCount()):
            index = self.tree_model.index(row, 0)
            if self.tree.isExpanded(index):
                expanded.add(index.data(PROJECT_ROLE))
        self.tree.selectionModel().blockSignals(True)
        build_project_tree(self.tree_model, self.model.recs)
        target = self.tree_model.index(0, 0)
        for row in range(self.tree_model.rowCount()):
            index = self.tree_model.index(row, 0)
            project = index.data(PROJECT_ROLE)
            if project in expanded:
                self.tree.setExpanded(index, True)
            if project == scope[0] and project is not None:
                target = index
                if scope[1] is not None:
                    for child_row in range(self.tree_model.rowCount(index)):
                        child = self.tree_model.index(child_row, 0, index)
                        if child.data(DAY_ROLE) == scope[1]:
                            target = child
        self.tree.setCurrentIndex(target)
        self.tree.selectionModel().blockSignals(False)
        project, day = target.data(PROJECT_ROLE), target.data(DAY_ROLE)
        if (project, day) != (self.proxy.project, self.proxy.day):
            self.proxy.set_scope(project, day)
        self._update_status()

    def _scope_changed(self, current: QModelIndex, _previous=None):
        if current.isValid():
            self.proxy.set_scope(current.data(PROJECT_ROLE), current.data(DAY_ROLE))
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
            text = "Choose a library folder to start (Ctrl+O)"
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
        self._update_actions()
        self._update_status()

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
        self.act_open_folder.setEnabled(len(recs) >= 1)
        self.act_copy_path.setEnabled(bool(recs))
        for action in (self.act_folder, self.act_rescan, self.act_circled, self.act_export, self.act_search):
            action.setEnabled(True)
        self.act_rescan.setEnabled(self.scan_thread is None)
        self.act_undo.setEnabled(bool(self.undo_stack))
        has_scope = bool(recs) or (self.tree.currentIndex().isValid()
                                   and self.tree.currentIndex().data(PROJECT_ROLE) is not None)
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
        for action in (self.act_rename, self.act_metadata, self.act_organize):
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
        if not index.isValid() or index.data(PROJECT_ROLE) is None:
            return
        project, day = index.data(PROJECT_ROLE), index.data(DAY_ROLE)
        recs = self._recs_in_scope(project, day)
        menu = QMenu(self)
        label = project if day is None else f"{project} / {day}"
        rename = menu.addAction(f"Set Project Name for These {len(recs):,} Files…")
        rename.triggered.connect(lambda: self.edit_metadata(recs, focus="project"))
        organize_action = menu.addAction(f"Reorganize '{label}' into Folders…")
        organize_action.triggered.connect(lambda: self.organize_selected(recs))
        menu.addSeparator()
        chosen = {i.data(PROJECT_ROLE) for i in self.tree.selectionModel().selectedIndexes()
                  if i.data(PROJECT_ROLE) is not None}
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

    def _recs_in_scope(self, project, day) -> list[Recording]:
        return [r for r in self.model.recs
                if (r.project or NO_PROJECT) == project and (day is None or (catalog.day_of(r) or "(No date)") == day)]

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
        for row in range(self.tree_model.rowCount()):
            index = self.tree_model.index(row, 0)
            if index.data(PROJECT_ROLE) == (project or NO_PROJECT):
                self.tree.setCurrentIndex(index)
                self.tree.scrollTo(index)
                break

    # ------------------------------------------------------------ reports / project folders

    def _scope_recordings(self) -> list[Recording]:
        """Selected rows, or everything in the sidebar's project/day."""
        recs = [r for r in self.selected_recordings() if not r.error]
        if len(recs) > 1:
            return recs
        index = self.tree.currentIndex()
        if index.isValid() and index.data(PROJECT_ROLE) is not None:
            return [r for r in self._recs_in_scope(index.data(PROJECT_ROLE), index.data(DAY_ROLE)) if not r.error]
        return recs

    def report_groups(self, recs: list[Recording] | None = None) -> list[ReportGroup]:
        """One report per project: from the sidebar selection (several projects
        or days), else the selected files, else the current project / day."""
        if recs is None:
            scopes = [(i.data(PROJECT_ROLE), i.data(DAY_ROLE)) for i in self.tree.selectionModel().selectedIndexes()
                      if i.data(PROJECT_ROLE) is not None]
            selected = [r for r in self.selected_recordings() if not r.error]
            if len(scopes) > 1:
                recs = [r for project, day in scopes for r in self._recs_in_scope(project, day) if not r.error]
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
        """The folder that holds a project: the folder named like the project if
        there is one, else the first folder below the library; the most common wins."""
        containers = {c.casefold() for c in settings.get(self.qsettings, "container_folders")}
        votes: dict[str, int] = {}
        for rec in recs:
            try:
                parts = Path(rec.path).relative_to(self.root).parts[:-1]
            except ValueError:
                continue
            chosen = None
            for i, part in enumerate(parts):
                if rec.project and part.casefold() == rec.project.casefold():
                    chosen = parts[:i + 1]
                    break
            if chosen is None:
                for i, part in enumerate(parts):
                    if part.casefold() not in containers:
                        chosen = parts[:i + 1]
                        break
            if chosen:
                folder = os.path.join(self.root, *chosen)
                votes[folder] = votes.get(folder, 0) + 1
        if votes:
            return max(votes, key=votes.get)
        return offload.common_folder([r.path for r in recs])

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
        dialog = SettingsDialog(self.qsettings, self)
        if not dialog.exec():
            return
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
            box.setCheckBox(QCheckBox("Don't ask again"))
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            if box.checkBox().isChecked():
                settings.put(self.qsettings, "confirm_undo", False)
        self.undo_stack.pop()
        if entry["kind"] == "rename":
            self._undo_renames(entry)
        else:
            self._undo_metadata(entry)
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

    def do_renames(self, pairs: list[tuple[Recording, Path]], *, embed: bool, label: str,
                   remove_empty: bool = False):
        """The single entry point for renames and moves."""
        ops = [RenameOp(Path(rec.path), dst) for rec, dst in pairs]
        released = self._release_player({str(op.src) for op in ops})
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
            return
        applied, created, removed_dirs, removed_markers, embed_errors = result
        mapping = {str(op.src): str(op.dst) for op in applied}
        self._refresh_moved(mapping, reread=embed)
        self._restore_player(released, mapping)
        if applied:
            self._push_undo({"kind": "rename", "label": label, "applied": applied, "created": created,
                             "removed_dirs": removed_dirs, "removed_markers": removed_markers, "embed": embed})
            self._log("rename" if not remove_empty else "move",
                      files=[[str(op.src), str(op.dst)] for op in applied], embedded_name=embed)
        verb = "Moved" if remove_empty else "Renamed"
        summary = f"{verb} {len(applied):,} file(s)."
        if removed_dirs:
            summary += f" Removed {len(removed_dirs):,} folder(s) left empty."
        if embed_errors:
            show_report(self, verb, summary + " Some names inside the files could not be updated:", embed_errors)
        else:
            self.statusBar().showMessage(summary, 8000)

    def _undo_renames(self, entry):
        reverse = undo_ops(entry["applied"])
        released = self._release_player({str(op.src) for op in reverse})

        def work(job):
            # Folders removed as "left empty" come back (apply_renames creates
            # missing folders), and so do the recorders' empty marker files.
            applied = apply_renames(reverse, None, progress=lambda d, t: job.report(d, t))
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
        mapping = {str(op.src): str(op.dst) for op in applied}
        self._refresh_moved(mapping, reread=entry["embed"])
        self._restore_player(released, mapping)
        self._log("undo " + entry["label"], files=[[str(op.src), str(op.dst)] for op in applied])
        if errors:
            show_report(self, "Undo", "Files are back, but some names inside them could not be restored:", errors)
        else:
            self.statusBar().showMessage(f"Undid {entry['label']}", 6000)

    def _refresh_moved(self, mapping: dict[str, str], reread: bool):
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


def _values_of(rec: Recording, changes: dict) -> dict:
    values = {}
    for key in changes:
        values[key] = rec.meta_project if key == "project" else getattr(rec, key)
    return values
