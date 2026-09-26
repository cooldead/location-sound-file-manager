"""Find Duplicates window: duplicate recordings and duplicate projects.

The window only finds and previews. What to change is handed to the main
window as a Cleanup, which verifies every removal byte for byte, applies it
with undo, and updates the library."""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel,
    QLineEdit,
    QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSplitter, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import catalog, duplicates
from .catalog import REMOVED_FOLDER, Recording
from .offload import human_size
from .renamer import validate_name

ITEM_ROLE = Qt.ItemDataRole.UserRole + 30
LOCATION_ROLE = Qt.ItemDataRole.UserRole + 31  # a group's folder row: that folder
NEW_FOLDER = "\0new"  # the "New folder…" entry of the keep folder list
RED = QColor("#d13438")


@dataclass
class Cleanup:
    """Changes for the main window to apply.
    removals: (recording, path of the copy that is kept, "identical" | "audio")
    moves: (recording, new path); retags: (recording, path after the moves, new project name)."""
    label: str
    removals: list[tuple[Recording, str, str]] = field(default_factory=list)
    moves: list[tuple[Recording, str]] = field(default_factory=list)
    retags: list[tuple[Recording, str, str]] = field(default_factory=list)
    permanent: bool = False
    remove_empty: bool = True
    # Files that are not byte for byte the same as the kept copy: "skip" (left
    # where they are), "copy" or "move" (next to the kept copy, renamed
    # <name>_ReviewForDeletion).
    review_mode: str = "skip"
    reviews: list[tuple[Recording, str]] = field(default_factory=list)  # known different: (recording, kept copy)
    # Preferred versions (with notes, or with more tracks): (that copy, the copy it
    # replaces, how it is checked first: "audio" or "subset").
    replacements: list[tuple] = field(default_factory=list)


class _Worker(QThread):
    progress = Signal(int, int)
    done = Signal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(self.fn(self), "")
        except Exception as error:  # noqa: BLE001
            self.done.emit(None, f"{error}\n{traceback.format_exc()}")


def confirm_removal(parent, count: int, size: int, moves: int = 0, retags: int = 0,
                    different: int = 0) -> tuple[bool, bool, bool, str]:
    """Ask how to remove; returns (ok, permanent, remove_empty, review_mode)."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Remove duplicates")
    lines = []
    if count:
        lines.append(f"<b>{count}</b> file(s) will be removed ({human_size(size)}). Before each one is removed it "
                     "is compared byte for byte with the copy that is kept.")
    if different:
        lines.append(f"<b>{different}</b> file(s) are the same recording as the kept copy but not the same file "
                     "(different notes or names).")
    if moves:
        lines.append(f"<b>{moves}</b> file(s) will be moved into the kept folder.")
    if retags:
        lines.append(f"<b>{retags}</b> file(s) will get the kept project name (written into the file).")
    text = QLabel("<br><br>".join(lines))
    text.setWordWrap(True)
    hold = QRadioButton(f"Move removed files into “{REMOVED_FOLDER}” in the library (can be undone; "
                        "review and delete that folder later)")
    delete = QRadioButton("Delete them permanently (cannot be undone)")
    hold.setChecked(True)
    group = QButtonGroup(dialog)
    group.addButton(hold)
    group.addButton(delete)

    review_box = QGroupBox("Files that are not byte for byte the same as the kept copy")
    review_copy = QRadioButton(f"Copy them next to the kept copy, renamed “name{duplicates.REVIEW_TAG}.wav” "
                               "(the original stays where it is)")
    review_move = QRadioButton(f"Move them next to the kept copy, renamed “name{duplicates.REVIEW_TAG}.wav”")
    review_leave = QRadioButton("Leave them where they are")
    review_copy.setChecked(True)
    review_group = QButtonGroup(dialog)
    review_layout = QVBoxLayout(review_box)
    for button in (review_copy, review_move, review_leave):
        review_group.addButton(button)
        review_layout.addWidget(button)
    review_hint = QLabel("Find them later under Review & Delete, or by searching the Library for "
                         f"“{duplicates.REVIEW_TAG}”.")
    review_hint.setEnabled(False)
    review_hint.setWordWrap(True)
    review_layout.addWidget(review_hint)

    empty = QCheckBox("Remove folders that are left empty")
    empty.setChecked(True)
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText("Go Ahead")
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    layout = QVBoxLayout(dialog)
    layout.addWidget(text)
    if count:
        layout.addWidget(hold)
        layout.addWidget(delete)
    if count or different:
        layout.addWidget(review_box)
    layout.addWidget(empty)
    layout.addWidget(box)
    dialog.resize(660, dialog.sizeHint().height())
    ok = dialog.exec() == QDialog.DialogCode.Accepted
    if ok and delete.isChecked() and count:
        ok = QMessageBox.warning(parent, "Delete permanently", f"Permanently delete {count} file(s)? This cannot "
                                 "be undone.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) \
            == QMessageBox.StandardButton.Yes
    mode = "copy" if review_copy.isChecked() else "move" if review_move.isChecked() else "skip"
    return ok, delete.isChecked(), empty.isChecked(), mode


class DuplicatesWindow(QDialog):
    cleanupRequested = Signal(object)  # Cleanup
    playRequested = Signal(object)  # Recording
    reviewRequested = Signal()  # open Review & Delete
    compareRequested = Signal()  # open Review & Delete on the marked-files comparison
    emptyFoldersRequested = Signal()  # find and delete empty folders in the library

    def __init__(self, recs_getter, root_getter, containers_getter, cache_file: str, parent=None, runner=None):
        super().__init__(parent)
        self.runner = runner  # the main window: does the file work of (batch) merges
        self.setWindowTitle("Find Duplicates")
        self.recs_getter, self.root_getter, self.containers_getter = recs_getter, root_getter, containers_getter
        self.cache_file = cache_file
        self.file_groups: list[duplicates.FileGroup] = []
        self.project_groups: list[duplicates.ProjectGroup] = []
        self.plan: list[duplicates.Action] = []
        self._worker: _Worker | None = None

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_files_tab(), "Duplicate Files")
        self.tabs.addTab(self._build_projects_tab(), "Duplicate Projects")
        self.tabs.addTab(self._build_nested_tab(), "Nested Folders")
        review = QPushButton("Review && Delete…")
        review.setToolTip(f"See what is in “{REMOVED_FOLDER}” and the files marked “{duplicates.REVIEW_TAG}”, "
                          "put files back or delete them for good")
        review.clicked.connect(self.reviewRequested)
        compare = QPushButton("Compare Marked Files…")
        compare.setToolTip(f"Find every file marked “{duplicates.REVIEW_TAG}”, see it next to its closest copy, "
                           "listen to both and compare them")
        compare.clicked.connect(self.compareRequested)
        empty = QPushButton("Delete Empty Folders…")
        empty.setToolTip("Find every folder in the library (at any depth) that holds no files except the "
                         "recorders' marker files (.take_folder, .daily_folder, .DS_Store) and delete them "
                         "after you confirm. Undoable.")
        empty.clicked.connect(self.emptyFoldersRequested)
        bottom = QHBoxLayout()
        bottom.addWidget(empty)
        bottom.addStretch(1)
        bottom.addWidget(compare)
        bottom.addWidget(review)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(bottom)
        self.resize(1250, 800)

    # ------------------------------------------------------------ files tab

    def _build_files_tab(self) -> QWidget:
        page = QWidget()
        self.scan_button = QPushButton("Scan for Duplicate Files")
        self.scan_button.clicked.connect(self.scan_files)
        self.files_progress = QProgressBar()
        self.files_progress.hide()
        self.files_cancel = QPushButton("Stop")
        self.files_cancel.hide()
        self.files_cancel.clicked.connect(self._cancel)
        self.files_status = QLabel("Finds recordings that are in the library more than once (same audio), "
                                   "for example a card copied into two places.")
        self.files_status.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.scan_button)
        top.addWidget(self.files_progress, 1)
        top.addWidget(self.files_cancel)

        self.files_tree = QTreeWidget()
        self.files_tree.setHeaderLabels(["Recording / copy", "Folder", "Size", "Status"])
        self.files_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.files_tree.setColumnWidth(0, 330)
        self.files_tree.setColumnWidth(1, 480)
        self.files_tree.setColumnWidth(2, 80)
        self.files_tree.itemChanged.connect(lambda *_: self._update_files_summary())
        self.files_tree.itemDoubleClicked.connect(self._play_item)
        self.files_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files_tree.customContextMenuRequested.connect(self._files_menu)

        tick_identical = QPushButton("Check Extra Copies of Identical Files")
        tick_identical.clicked.connect(lambda: self._tick_all(identical_only=True))
        untick = QPushButton("Uncheck All")
        untick.clicked.connect(lambda: self._tick_all(none=True))
        self.files_summary = QLabel()
        self.remove_button = QPushButton("Remove Checked Copies…")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.remove_ticked)
        bottom = QHBoxLayout()
        bottom.addWidget(tick_identical)
        bottom.addWidget(untick)
        bottom.addWidget(self.files_summary, 1)
        bottom.addWidget(self.remove_button)
        hint = QLabel("The copy kept is marked “keep” (right-click another copy to keep that one instead). "
                      "Copies with different metadata or names are not checked; the Status column shows what "
                      "differs. Double-click a copy to play it.")
        hint.setWordWrap(True)
        hint.setEnabled(False)

        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(self.files_status)
        layout.addWidget(self.files_tree, 1)
        layout.addWidget(hint)
        layout.addLayout(bottom)
        return page

    def _busy(self, busy: bool):
        self.scan_button.setEnabled(not busy)
        self.find_projects_button.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        if hasattr(self, "batch_button"):
            self.batch_button.setEnabled(not busy and bool(self.checked_groups()) and self.runner is not None)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancelled = True

    def scan_files(self):
        recs, root, containers, cache_file = self.recs_getter(), self.root_getter(), self.containers_getter(), \
            self.cache_file

        def work(worker):
            cache = catalog.Cache(cache_file)
            try:
                fingerprints = duplicates.Fingerprints(cache)
                return duplicates.find_duplicate_files(recs, fingerprints, root, containers,
                                                       progress=worker.progress.emit,
                                                       cancelled=lambda: worker.cancelled)
            finally:
                cache.close()

        self._busy(True)
        self.files_progress.show()
        self.files_progress.setRange(0, 0)
        self.files_cancel.show()
        self.files_status.setText("Comparing recordings… (the first scan reads a small part of each candidate "
                                  "file; later scans use the cache)")
        self._worker = _Worker(work, self)
        self._worker.progress.connect(self._files_progress)
        self._worker.done.connect(self._files_done)
        self._worker.start()

    def _files_progress(self, done: int, total: int):
        self.files_progress.setRange(0, max(total, 1))
        self.files_progress.setValue(done)
        self.files_status.setText(f"Comparing recordings… {done:,} of {total:,} candidates")

    def _files_done(self, groups, error):
        cancelled = self._worker.cancelled if self._worker else False
        self._worker = None
        self._busy(False)
        self.files_progress.hide()
        self.files_cancel.hide()
        if error:
            self.files_status.setText("The scan failed.")
            QMessageBox.warning(self, "Find Duplicates", error)
            return
        self.file_groups = groups or []
        self._fill_files()
        wasted = sum(g.wasted for g in self.file_groups)
        identical = sum(1 for g in self.file_groups if g.identical)
        self.files_status.setText(
            ("Stopped early. " if cancelled else "")
            + f"<b>{len(self.file_groups):,}</b> recordings have extra copies ({human_size(wasted)} in the extra "
            f"copies): {identical:,} identical, {len(self.file_groups) - identical:,} with different metadata or "
            "names." if self.file_groups else "No duplicate recordings found.")

    def _fill_files(self):
        root = self.root_getter()
        self.files_tree.blockSignals(True)
        self.files_tree.clear()
        bold = QFont()
        bold.setBold(True)
        for group in self.file_groups:
            status = "all identical" if group.identical else "some copies differ: " + ", ".join(
                group.differences) if group.differences else "same audio, file sizes differ"
            top = QTreeWidgetItem([f"{group.keeper.name}  ×{len(group.recs)}", group.keeper.project,
                                   human_size(group.wasted), status])
            top.setFont(0, bold)
            top.setData(0, ITEM_ROLE, group)
            for rec in group.recs:
                child = QTreeWidgetItem([rec.name, _relative(rec.folder, root), human_size(rec.size), ""])
                child.setData(0, ITEM_ROLE, rec)
                child.setToolTip(1, rec.path)
                top.addChild(child)
            self._mark_keeper(top)
            self.files_tree.addTopLevelItem(top)
        self.files_tree.blockSignals(False)
        self._update_files_summary()

    def _mark_keeper(self, top: QTreeWidgetItem):
        group: duplicates.FileGroup = top.data(0, ITEM_ROLE)
        replacement = group.replacement
        for i in range(top.childCount()):
            child = top.child(i)
            rec = child.data(0, ITEM_ROLE)
            if rec is group.keeper:
                child.setFlags(child.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.CheckStateRole, None)
                child.setText(3, "keep" if replacement is None else "keep this place; replaced by the copy with notes")
                child.setForeground(3, QBrush(QColor("#2e8b57")))
            elif rec is replacement:
                # Versions with notes are preferred: checked = it takes the kept copy's place.
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setText(3, "has notes: replaces the kept copy (in its place)")
                child.setForeground(3, QBrush(QColor("#2e8b57")))
            else:
                # Each copy is judged against the kept one: identical copies are
                # ticked, copies whose metadata or name differs are not.
                same = group.copy_identical(rec)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if same else Qt.CheckState.Unchecked)
                differs = group.differs(rec)
                if same:
                    if group.check_level(rec) == "identical":
                        child.setText(3, "identical")
                    elif differs and duplicates.is_take_file(rec.name):
                        child.setText(3, f"same file (its {', '.join(differs)} differs; audio is checked before "
                                         "removal)")
                    else:
                        child.setText(3, "same recording (file laid out differently)")
                else:
                    child.setText(3, "differs: " + ", ".join(f"{k} “{v[1]}”" for k, v in differs.items()))
                child.setForeground(3, QBrush(child.foreground(1) if same else RED))

    def _tick_all(self, identical_only=False, none=False):
        self.files_tree.blockSignals(True)
        for i in range(self.files_tree.topLevelItemCount()):
            top = self.files_tree.topLevelItem(i)
            group = top.data(0, ITEM_ROLE)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    rec = child.data(0, ITEM_ROLE)
                    tick = not none and (group.copy_identical(rec) or not identical_only)
                    child.setCheckState(0, Qt.CheckState.Checked if tick else Qt.CheckState.Unchecked)
        self.files_tree.blockSignals(False)
        self._update_files_summary()

    def _checked_rows(self):
        for i in range(self.files_tree.topLevelItemCount()):
            top = self.files_tree.topLevelItem(i)
            group = top.data(0, ITEM_ROLE)
            for j in range(top.childCount()):
                child = top.child(j)
                if child.flags() & Qt.ItemFlag.ItemIsUserCheckable and child.checkState(0) == Qt.CheckState.Checked:
                    yield group, child.data(0, ITEM_ROLE)

    def _ticked(self) -> list[tuple[Recording, str, str]]:
        """Checked copies to remove: (copy, the copy it is checked against, level)."""
        result = []
        replacing = {id(g): r for g, r in self._checked_rows() if r is g.replacement}
        for group, rec in self._checked_rows():
            if rec is group.replacement:
                continue
            # Checked against the version that stays: the copy with notes when it
            # replaces the kept one, else the kept one. Identical copies must match
            # byte for byte; a copy whose metadata differs (checked on purpose) on audio.
            base = replacing.get(id(group), group.keeper)
            level = group.check_level(rec) if group.copy_identical(rec) else "audio"
            result.append((rec, base.path, level))
        return result

    def _replacements(self) -> list[tuple[Recording, Recording]]:
        """Checked versions with notes: (that copy, the kept copy it replaces)."""
        return [(r, g.keeper, "audio") for g, r in self._checked_rows() if r is g.replacement]

    def _update_files_summary(self):
        ticked = self._ticked()
        replacing = self._replacements()
        size = sum(r.size for r, _, _ in ticked) + sum(rep[1].size for rep in replacing)
        text = f"{len(ticked):,} to remove" if ticked else ""
        if replacing:
            text += (", " if text else "") + f"{len(replacing)} replaced by a version with notes"
        self.files_summary.setText(f"{text} · frees {human_size(size)}" if text else "")
        self.remove_button.setEnabled(bool(ticked or replacing))

    def _files_menu(self, pos):
        item = self.files_tree.itemAt(pos)
        if item is None or item.parent() is None:
            return
        rec = item.data(0, ITEM_ROLE)
        group = item.parent().data(0, ITEM_ROLE)
        menu = QMenu(self)
        keep = menu.addAction("Keep This Copy Instead")
        keep.setEnabled(rec is not group.keeper)
        keep.triggered.connect(lambda: self._set_keeper(item.parent(), rec))
        play = menu.addAction("Play")
        play.triggered.connect(lambda: self.playRequested.emit(rec))
        folder = menu.addAction("Show in Folder")
        folder.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(rec.folder)))
        menu.exec(self.files_tree.viewport().mapToGlobal(pos))

    def _set_keeper(self, top: QTreeWidgetItem, rec: Recording):
        group: duplicates.FileGroup = top.data(0, ITEM_ROLE)
        group.keeper = rec
        self.files_tree.blockSignals(True)
        self._mark_keeper(top)
        self.files_tree.blockSignals(False)
        self._update_files_summary()

    def _play_item(self, item: QTreeWidgetItem, _column: int):
        rec = item.data(0, ITEM_ROLE)
        if isinstance(rec, Recording):
            self.playRequested.emit(rec)

    def remove_ticked(self):
        ticked = self._ticked()
        replacing = self._replacements()
        if not ticked and not replacing:
            return
        ok, permanent, remove_empty, review = confirm_removal(
            self, len(ticked) + len(replacing), sum(r.size for r, _, _ in ticked) + sum(rep[1].size for rep in replacing))
        if not ok:
            return
        self.cleanupRequested.emit(Cleanup(f"removal of {len(ticked) + len(replacing)} duplicate file(s)",
                                           removals=ticked, permanent=permanent, remove_empty=remove_empty,
                                           review_mode=review, replacements=replacing))

    # ------------------------------------------------------------ projects tab

    def _build_projects_tab(self) -> QWidget:
        page = QWidget()
        self.find_projects_button = QPushButton("Find Duplicate Projects")
        self.find_projects_button.clicked.connect(self.find_projects)
        self.projects_progress = QProgressBar()
        self.projects_progress.setMaximumWidth(260)
        self.projects_progress.hide()
        self.projects_status = QLabel("Finds projects whose names are written differently or are very similar, "
                                      "and folders that exist in several places (e.g. a card copied twice).")
        self.projects_status.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.find_projects_button)
        top.addWidget(self.projects_progress)
        top.addWidget(self.projects_status, 1)

        self.projects_tree = QTreeWidget()
        self.projects_tree.setHeaderLabels(["Group / folder", "Files"])
        self.projects_tree.itemChanged.connect(self._project_check_changed)
        self.projects_tree.setColumnWidth(0, 420)
        # Long names widen the column (with a scroll bar) instead of being cut off.
        self.projects_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.projects_tree.header().setStretchLastSection(False)
        self.projects_tree.currentItemChanged.connect(lambda *_: self._project_selected())

        # Wide enough for long folder paths and project names.
        self.target_folder = QComboBox()
        self.target_folder.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.target_folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.target_folder.setMinimumContentsLength(40)
        self.target_folder.currentIndexChanged.connect(lambda *_: self._clear_plan())
        self.target_name = QComboBox()
        self.target_name.setEditable(True)
        self.target_name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.target_name.setMinimumContentsLength(30)
        self.target_name.currentTextChanged.connect(lambda *_: self._clear_plan())
        self.name_label = QLabel("Project name:")
        self.new_folder = QLineEdit()
        self.new_folder.setPlaceholderText("Name of the new folder, inside the library")
        self.new_folder.setToolTip("A folder inside the library; use / for a subfolder, e.g. Projects/Name")
        self.new_folder.textEdited.connect(lambda *_: setattr(self, "_new_folder_edited", True))
        self.new_folder.textChanged.connect(lambda *_: self._clear_plan())
        self.new_folder_label = QLabel("New folder:")
        self.new_folder_path = QLabel()
        self.new_folder_path.setWordWrap(True)
        self.new_folder_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # A wrapping label in a form otherwise reserves a lot of empty height.
        self.new_folder_path.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.new_folder_path.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.new_folder.textChanged.connect(lambda *_: self._show_new_folder_path())
        self._new_folder_edited = False
        self.target_folder.currentIndexChanged.connect(lambda *_: self._update_new_folder())
        self.target_name.currentTextChanged.connect(lambda *_: self._suggest_new_folder())
        self.move_files = QCheckBox("Move the other folders' files into the kept folder")
        self.move_files.setChecked(True)
        self.move_files.toggled.connect(lambda *_: self._clear_plan())
        self.preview_button = QPushButton("Preview Merge")
        self.preview_button.clicked.connect(self.preview_merge)
        self.preview_button.setEnabled(False)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Keep folder:", self.target_folder)
        # The name field and its full path below it, as one form field.
        self.new_folder_box = QWidget()
        new_box = QVBoxLayout(self.new_folder_box)
        new_box.setContentsMargins(0, 0, 0, 0)
        new_box.setSpacing(2)
        new_box.addWidget(self.new_folder)
        new_box.addWidget(self.new_folder_path)
        form.addRow(self.new_folder_label, self.new_folder_box)
        form.addRow(self.name_label, self.target_name)
        form.addRow("", self.move_files)
        explain = QLabel("Files that are the same as one already in the kept folder are removed (after a byte "
                         "for byte check); different files are moved in, keeping their place below the folder. "
                         "Nothing changes until you press Merge.")
        explain.setWordWrap(True)
        explain.setEnabled(False)

        self.plan_tree = QTreeWidget()
        self.plan_tree.setHeaderLabels(["Action", "File", "To / why"])
        self.plan_tree.setRootIsDecorated(False)
        self.plan_tree.setColumnWidth(0, 90)
        self.plan_tree.setColumnWidth(1, 420)
        self.plan_summary = QLabel()
        self.plan_summary.setWordWrap(True)
        self.merge_button = QPushButton("Merge…")
        self.merge_button.setEnabled(False)
        self.merge_button.clicked.connect(self.merge)
        self.preview_progress = QProgressBar()
        self.preview_progress.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self.preview_progress, 1)
        buttons.addStretch(1)
        buttons.addWidget(self.merge_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(form)
        right_layout.addWidget(explain)
        right_layout.addLayout(buttons)
        right_layout.addWidget(self.plan_tree, 1)
        right_layout.addWidget(self.plan_summary)

        check_folders = QPushButton("Check All “Same Folder” Groups")
        check_folders.clicked.connect(lambda: self._check_groups(kind="folders"))
        uncheck = QPushButton("Uncheck All")
        uncheck.clicked.connect(lambda: self._check_groups(kind=None))
        self.batch_button = QPushButton("Merge Checked…")
        self.batch_button.setEnabled(False)
        self.batch_button.setToolTip("Merge every checked group, one after another, each with its suggested keep "
                                     "folder and name")
        self.batch_button.clicked.connect(self.batch_merge)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.projects_tree, 1)
        batch_row = QHBoxLayout()
        batch_row.addWidget(check_folders)
        batch_row.addWidget(uncheck)
        batch_row.addStretch(1)
        batch_row.addWidget(self.batch_button)
        left_layout.addLayout(batch_row)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([480, 720])
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        return page

    def find_projects(self):
        recs, root, containers = self.recs_getter(), self.root_getter(), self.containers_getter()
        self._busy(True)
        self.projects_tree.clear()
        self._project_selected()
        self.projects_progress.show()
        self.projects_progress.setRange(0, 0)  # moving bar: it doesn't know how long it takes
        self.projects_status.setText(f"Looking for duplicate projects among {len(recs):,} recordings…")
        self._worker = _Worker(lambda worker: duplicates.find_duplicate_projects(recs, root, containers), self)
        self._worker.done.connect(self._projects_found)
        self._worker.start()

    def _projects_found(self, groups, error):
        self._worker = None
        self._busy(False)
        self.projects_progress.hide()
        if error:
            self.projects_status.setText("Finding duplicate projects failed.")
            QMessageBox.warning(self, "Find Duplicates", error)
            return
        root = self.root_getter()
        self.project_groups = groups or []
        self.projects_tree.blockSignals(True)
        bold = QFont()
        bold.setBold(True)
        for kind in ("spelling", "similar", "folders"):
            groups = [g for g in self.project_groups if g.kind == kind]
            if not groups:
                continue
            header = QTreeWidgetItem([f"{groups[0].label}  ({len(groups)})", ""])
            header.setFont(0, bold)
            header.setFlags((header.flags() & ~Qt.ItemFlag.ItemIsSelectable) | Qt.ItemFlag.ItemIsUserCheckable)
            header.setCheckState(0, Qt.CheckState.Unchecked)
            header.setToolTip(0, "Check to include all of these groups in a batch merge")
            self.projects_tree.addTopLevelItem(header)
            for group in groups:
                item = QTreeWidgetItem([" / ".join(group.names), str(len(group.files))])
                item.setToolTip(0, "\n".join(group.names))
                item.setData(0, ITEM_ROLE, group)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                for location in group.locations:
                    inside = group.files_in(location)
                    names = sorted({r.project for r in inside})
                    child = QTreeWidgetItem([_relative(location, root) or "(library)", str(len(inside))])
                    child.setToolTip(0, location + ("\nProjects: " + ", ".join(names) if names else "")
                                     + "\nClick to keep this folder")
                    child.setData(0, LOCATION_ROLE, location)
                    item.addChild(child)
                header.addChild(item)
                item.setExpanded(True)  # the folders are what the choice is about
            header.setExpanded(True)
        self.projects_tree.blockSignals(False)
        # Select the first group, so the keep folder and name are filled in at once.
        first = next((item for _, item in self._group_items()), None)
        if first is not None:
            self.projects_tree.setCurrentItem(first)
        self._update_batch_button()
        self.projects_status.setText(f"{len(self.project_groups)} group(s) found. Pick one to merge it, or check "
                                     "several and use Merge Checked." if self.project_groups
                                     else "No duplicate projects found.")

    # ------------------------------------------------------------ batch

    def _group_items(self):
        for i in range(self.projects_tree.topLevelItemCount()):
            header = self.projects_tree.topLevelItem(i)
            for j in range(header.childCount()):
                yield header, header.child(j)

    def _project_check_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0:
            return
        self.projects_tree.blockSignals(True)
        if item.parent() is None:  # a category: check or uncheck all its groups
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                for j in range(item.childCount()):
                    item.child(j).setCheckState(0, state)
        else:
            header = item.parent()
            states = {header.child(j).checkState(0) for j in range(header.childCount())}
            header.setCheckState(0, states.pop() if len(states) == 1 else Qt.CheckState.PartiallyChecked)
        self.projects_tree.blockSignals(False)
        self._update_batch_button()

    def _check_groups(self, kind: str | None):
        self.projects_tree.blockSignals(True)
        for header, item in self._group_items():
            group = item.data(0, ITEM_ROLE)
            item.setCheckState(0, Qt.CheckState.Checked if kind and group.kind == kind else Qt.CheckState.Unchecked)
        for i in range(self.projects_tree.topLevelItemCount()):
            header = self.projects_tree.topLevelItem(i)
            states = {header.child(j).checkState(0) for j in range(header.childCount())}
            header.setCheckState(0, states.pop() if len(states) == 1 else Qt.CheckState.PartiallyChecked)
        self.projects_tree.blockSignals(False)
        self._update_batch_button()

    def checked_groups(self) -> list[duplicates.ProjectGroup]:
        return [item.data(0, ITEM_ROLE) for _, item in self._group_items()
                if item.checkState(0) == Qt.CheckState.Checked]

    def _update_batch_button(self):
        count = len(self.checked_groups())
        self.batch_button.setText(f"Merge Checked ({count})…" if count else "Merge Checked…")
        self.batch_button.setEnabled(bool(count) and self.runner is not None and self._worker is None)

    def batch_merge(self):
        groups = self.checked_groups()
        if not groups or self.runner is None:
            return
        dialog = BatchMergeDialog(groups, self, self)
        dialog.exec()
        if dialog.merged:
            self.find_projects()

    def _selected_group(self) -> duplicates.ProjectGroup | None:
        """The group of the current row; a folder row belongs to its group.
        (On macOS a click makes any row current, also a folder row, which used
        to leave the keep folder empty.)"""
        item = self.projects_tree.currentItem()
        while item is not None:
            group = item.data(0, ITEM_ROLE)
            if isinstance(group, duplicates.ProjectGroup):
                return group
            item = item.parent()
        return None

    def _project_selected(self):
        group = self._selected_group()
        self._clear_plan()
        self.preview_button.setEnabled(group is not None and self._worker is None)
        self.target_folder.blockSignals(True)
        self.target_name.blockSignals(True)
        self.target_folder.clear()
        self.target_name.clear()
        if group is not None:
            root, containers = self.root_getter(), self.containers_getter()
            # Suggest the folder outside the card dumps with the most files.
            ranked = sorted(group.locations, key=lambda f: (
                any(p.casefold() in {c.casefold() for c in containers} for p in _relative(f, root).split("/")),
                -len(group.files_in(f)), f.count("/")))
            for folder in ranked:
                count = len(group.files_in(folder))
                self.target_folder.addItem(f"{_relative(folder, root)}  ({count} file{'' if count == 1 else 's'})",
                                           folder)
                self.target_folder.setItemData(self.target_folder.count() - 1, folder, Qt.ItemDataRole.ToolTipRole)
            self.target_folder.addItem("New folder…", NEW_FOLDER)
            self.target_folder.setItemData(self.target_folder.count() - 1,
                                           "Merge everything into a new folder in the library",
                                           Qt.ItemDataRole.ToolTipRole)
            if group.retag:
                counts = {n: sum(1 for r in group.files if r.project == n) for n in group.names}
                for name in sorted(group.names, key=lambda n: -counts[n]):
                    self.target_name.addItem(name)
        self.name_label.setVisible(group is not None and group.retag)
        self.target_name.setVisible(group is not None and group.retag)
        # A click on one of the group's folders keeps that folder.
        current = self.projects_tree.currentItem()
        location = current.data(0, LOCATION_ROLE) if current is not None else None
        if group is not None and location and self.target_folder.findData(location) >= 0:
            self.target_folder.setCurrentIndex(self.target_folder.findData(location))
        self.target_folder.blockSignals(False)
        self.target_name.blockSignals(False)
        self._new_folder_edited = False
        self._suggest_new_folder()
        self._update_new_folder()

    def _update_new_folder(self):
        new = self.target_folder.currentData() == NEW_FOLDER
        self.new_folder_label.setVisible(new)
        self.new_folder_box.setVisible(new)
        self._show_new_folder_path()
        # Everything goes into a new folder, so files are always moved.
        self.move_files.setEnabled(not new)
        if new:
            self.move_files.setChecked(True)

    def _show_new_folder_path(self):
        if self.target_folder.currentData() != NEW_FOLDER:
            return
        path, problem = self._target_path()
        if problem:
            self.new_folder_path.setText(f"<span style='color:{RED.name()}'>{problem}</span>")
        elif os.path.isdir(path):
            typed = os.path.join(self.root_getter(), *[p.strip() for p in self.new_folder.text().split("/") if p.strip()])
            case = ("<br>The share ignores upper/lower case, so this is the existing folder with that name."
                    if os.path.normpath(typed) != os.path.normpath(path) else "")
            self.new_folder_path.setText(f"<b>{path}</b><br>This folder already exists; the files are merged "
                                         "into it." + case)
        else:
            self.new_folder_path.setText(f"Will be created at: <b>{path}</b>")
        self.new_folder_path.setToolTip(path)

    def _suggest_new_folder(self):
        group = self._selected_group()
        if group is not None and not self._new_folder_edited:
            name = self.target_name.currentText().strip() if group.retag else ""
            self.new_folder.setText(duplicates.suggested_folder_name(group, name))

    def _target_path(self) -> tuple[str, str]:
        """(the keep folder, an error message or "")."""
        data = self.target_folder.currentData()
        if data != NEW_FOLDER:
            return data or "", ""
        text = self.new_folder.text().strip().strip("/")
        parts = [p.strip() for p in text.split("/") if p.strip()]
        if not parts:
            return "", "Type a name for the new folder."
        for part in parts:
            if part in (".", "..") or validate_name(part):
                return "", f"“{part}” can't be used as a folder name."
        return _existing_spelling(self.root_getter(), parts), ""

    def _clear_plan(self):
        self.plan = []
        self.plan_tree.clear()
        self.plan_summary.setText("")
        self.merge_button.setEnabled(False)

    def preview_merge(self):
        group = self._selected_group()
        if group is None:
            return
        recs, cache_file = self.recs_getter(), self.cache_file
        target_folder, problem = self._target_path()
        if problem:
            QMessageBox.information(self, "Find Duplicates", problem)
            return
        target_name = self.target_name.currentText().strip() if group.retag else ""
        move = self.move_files.isChecked()
        root, containers = self.root_getter(), self.containers_getter()

        def work(worker):
            cache = catalog.Cache(cache_file)
            try:
                # Fingerprints are only read for copies whose names differ.
                fingerprints = duplicates.Fingerprints(cache)
                return duplicates.plan_project_merge(recs, group, target_folder, fingerprints, target_name, move,
                                                     root, containers)
            finally:
                cache.close()

        self._busy(True)
        self.preview_progress.show()
        self.preview_progress.setRange(0, 0)
        self.plan_summary.setText("Comparing the files of the folders…")
        self._worker = _Worker(work, self)
        self._worker.progress.connect(lambda d, t: (self.preview_progress.setRange(0, max(t, 1)),
                                                    self.preview_progress.setValue(d)))
        self._worker.done.connect(self._plan_done)
        self._worker.start()

    def _plan_done(self, plan, error):
        self._worker = None
        self._busy(False)
        self.preview_progress.hide()
        if error:
            QMessageBox.warning(self, "Find Duplicates", error)
            return
        self.plan = plan or []
        root = self.root_getter()
        self.plan_tree.clear()
        labels = {"remove": "remove", "move": "move", "retag": "rename", "skip": "skip", "differs": "different",
                  "replace": "replace"}
        for action in self.plan:
            where = _relative(action.dst, root) if action.kind in ("move", "skip", "replace") else ""
            detail = where + (f"  ·  {action.reason}" if action.reason and where else action.reason)
            item = QTreeWidgetItem([labels[action.kind], _relative(action.rec.path, root), detail])
            item.setToolTip(1, action.rec.path)
            if action.kind in ("skip", "differs"):
                for col in range(3):
                    item.setForeground(col, QBrush(RED))
            self.plan_tree.addTopLevelItem(item)
        counts = {k: sum(1 for a in self.plan if a.kind == k) for k in labels}
        freed = sum(a.rec.size for a in self.plan if a.kind == "remove")
        text = (f"{counts['remove']} identical file(s) removed ({human_size(freed)}), {counts['move']} moved in, "
                f"{counts['retag']} renamed to the kept project name")
        if counts["replace"]:
            text += f", {counts['replace']} preferred version(s) (with notes or more tracks) replace the kept copy"
        if counts["differs"]:
            text += (f", <span style='color:#d13438'><b>{counts['differs']} different</b></span> (the same "
                     "recording as the kept one but with other notes or names: left alone, or kept next to it "
                     f"as “…{duplicates.REVIEW_TAG}”, you choose when merging)")
        if counts["skip"]:
            text += (f", <span style='color:#d13438'><b>{counts['skip']} skipped</b></span> (a different file "
                     "with the same name is already in the kept folder; they stay where they are)")
        if not self.plan:
            text = "Nothing to do: everything is already in the kept folder under the kept name."
        self.plan_summary.setText(text + ".")
        self.merge_button.setEnabled(any(a.kind not in ("skip",) for a in self.plan))

    def merge(self):
        group = self._selected_group()
        if group is None or not self.plan:
            return
        name = self.target_name.currentText().strip() if group.retag else ""
        removals = [(a.rec, a.dst, a.level) for a in self.plan if a.kind == "remove"]
        moves = [(a.rec, a.dst) for a in self.plan if a.kind == "move"]
        retags = [(a.rec, a.dst if a.kind == "move" else a.rec.path, name) for a in self.plan
                  if name and a.rec.project != name and a.kind in ("move", "retag")]
        different = [(a.rec, a.dst) for a in self.plan if a.kind == "differs"]
        replacing = [(a.rec, a.other, a.level) for a in self.plan if a.kind == "replace"]
        retags += [(a.rec, a.dst, name) for a in self.plan
                   if a.kind == "replace" and name and a.rec.project != name]
        ok, permanent, remove_empty, review = confirm_removal(
            self, len(removals) + len(replacing), sum(r.size for r, _, _ in removals) +
            sum(rep[1].size for rep in replacing), len(moves), len(retags), len(different))
        if not ok:
            return
        self._rescan_projects_after = True
        self.cleanupRequested.emit(Cleanup(f"merge of {' / '.join(group.names)}", removals, moves, retags,
                                           permanent, remove_empty, review, different, replacing))

    # ------------------------------------------------------------ nested folders tab

    def _build_nested_tab(self) -> QWidget:
        page = QWidget()
        self.find_nested_button = QPushButton("Find Nested Folders")
        self.find_nested_button.clicked.connect(self.find_nested)
        self.nested_status = QLabel("Finds folders inside a folder with the same name, e.g. "
                                    "Project/250101/250101 (a day folder copied into itself), so the files can "
                                    "be moved up and the structure stays Project/Day/files.")
        self.nested_status.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.find_nested_button)
        top.addWidget(self.nested_status, 1)

        self.nested_tree = QTreeWidget()
        self.nested_tree.setHeaderLabels(["Nested folder", "Files inside", "Already there", "New", "In the way"])
        self.nested_tree.setRootIsDecorated(False)
        self.nested_tree.header().setStretchLastSection(False)
        self.nested_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            self.nested_tree.header().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.nested_tree.currentItemChanged.connect(lambda *_: self._nested_selected())
        self.nested_tree.itemChanged.connect(lambda *_: self._update_nested_buttons())
        for col, tip in ((2, "Files the outer folder already has (the same recording at the same place): "
                             "removed from the inner folder after a byte for byte check"),
                         (3, "Files only in the inner folder: moved up into the outer folder"),
                         (4, "A different file with the same name is already in the outer folder: left alone")):
            self.nested_tree.headerItem().setToolTip(col, tip)
        check_all = QPushButton("Check All")
        check_all.clicked.connect(lambda: self._check_nested(True))
        uncheck = QPushButton("Uncheck All")
        uncheck.clicked.connect(lambda: self._check_nested(False))
        self.nested_batch_button = QPushButton("Merge Checked…")
        self.nested_batch_button.setEnabled(False)
        self.nested_batch_button.setToolTip("Merge every checked nested folder into its outer folder, as one "
                                            "undo step")
        self.nested_batch_button.clicked.connect(lambda: self.merge_nested(self._checked_nested()))
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.nested_tree, 1)
        row = QHBoxLayout()
        row.addWidget(check_all)
        row.addWidget(uncheck)
        row.addStretch(1)
        row.addWidget(self.nested_batch_button)
        left_layout.addLayout(row)

        self.nested_plan_tree = QTreeWidget()
        self.nested_plan_tree.setHeaderLabels(["Action", "File", "To / why"])
        self.nested_plan_tree.setRootIsDecorated(False)
        self.nested_plan_tree.setColumnWidth(0, 90)
        self.nested_plan_tree.setColumnWidth(1, 360)
        self.nested_summary = QLabel()
        self.nested_summary.setWordWrap(True)
        self.nested_merge_button = QPushButton("Merge…")
        self.nested_merge_button.setEnabled(False)
        self.nested_merge_button.clicked.connect(
            lambda: self.merge_nested([self._current_nested()] if self._current_nested() else []))
        explain = QLabel("The files of the inner folder move up one level. A file the outer folder already has "
                         "is removed (after a byte for byte check); a copy with notes the outer one lacks takes "
                         "its place; nothing is overwritten. Nothing changes until you press Merge.")
        explain.setWordWrap(True)
        explain.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.nested_merge_button)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(explain)
        right_layout.addLayout(buttons)
        right_layout.addWidget(self.nested_plan_tree, 1)
        right_layout.addWidget(self.nested_summary)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([560, 660])
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        return page

    def find_nested(self):
        recs, root = self.recs_getter(), self.root_getter()
        found = duplicates.find_nested_folders(recs, root)
        self.nested_tree.blockSignals(True)
        self.nested_tree.clear()
        for nested in found:
            same, new, clash = nested.counts()
            item = QTreeWidgetItem([_relative(nested.inner, root), str(len(nested.inner_files)), str(same),
                                    str(new), str(clash) if clash else ""])
            item.setData(0, ITEM_ROLE, nested)
            item.setToolTip(0, f"{nested.inner}\ninside {nested.outer} ({len(nested.outer_files)} other files)")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            for col in (1, 2, 3, 4):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight)
            if clash:
                item.setForeground(4, QBrush(RED))
            self.nested_tree.addTopLevelItem(item)
        self.nested_tree.blockSignals(False)
        self.nested_status.setText(f"{len(found)} nested folder(s) found. Pick one to see what merging it does, "
                                   "or check several and use Merge Checked." if found
                                   else "No nested folders found.")
        if found:
            self.nested_tree.setCurrentItem(self.nested_tree.topLevelItem(0))
        else:
            self._nested_selected()
        self._update_nested_buttons()

    def _current_nested(self) -> duplicates.NestedFolder | None:
        item = self.nested_tree.currentItem()
        nested = item.data(0, ITEM_ROLE) if item is not None else None
        return nested if isinstance(nested, duplicates.NestedFolder) else None

    def _checked_nested(self) -> list[duplicates.NestedFolder]:
        return [self.nested_tree.topLevelItem(i).data(0, ITEM_ROLE) for i in range(self.nested_tree.topLevelItemCount())
                if self.nested_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]

    def _check_nested(self, on: bool):
        for i in range(self.nested_tree.topLevelItemCount()):
            self.nested_tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _update_nested_buttons(self):
        count = len(self._checked_nested())
        self.nested_batch_button.setText(f"Merge Checked ({count})…" if count else "Merge Checked…")
        self.nested_batch_button.setEnabled(bool(count))

    def _nested_selected(self):
        nested = self._current_nested()
        root = self.root_getter()
        self.nested_plan_tree.clear()
        if nested is None:
            self.nested_summary.setText("")
            self.nested_merge_button.setEnabled(False)
            return
        plan = duplicates.plan_nested_merge(nested, self.recs_getter())
        labels = {"remove": "remove", "move": "move up", "skip": "skip", "differs": "different", "replace": "replace"}
        for action in plan:
            item = QTreeWidgetItem([labels[action.kind], _relative(action.rec.path, nested.outer),
                                    f"{_relative(action.dst, root)}  ·  {action.reason}"])
            item.setToolTip(1, action.rec.path)
            if action.kind in ("skip", "differs"):
                for col in range(3):
                    item.setForeground(col, QBrush(RED))
            self.nested_plan_tree.addTopLevelItem(item)
        self.nested_summary.setText(self._nested_summary(plan, [nested]))
        self.nested_merge_button.setEnabled(any(a.kind != "skip" for a in plan))

    def _nested_summary(self, plan: list[duplicates.Action], nested: list[duplicates.NestedFolder]) -> str:
        counts = {k: sum(1 for a in plan if a.kind == k) for k in ("remove", "move", "replace", "differs", "skip")}
        freed = sum(a.rec.size for a in plan if a.kind == "remove")
        text = f"{counts['remove']} already there, removed ({human_size(freed)}), {counts['move']} moved up"
        if counts["replace"]:
            text += f", {counts['replace']} with notes take the outer copy's place"
        if counts["differs"]:
            text += (f", <span style='color:#d13438'><b>{counts['differs']} different</b></span> (the same "
                     "recording with other notes or names: left alone, or kept next to it for review, you choose)")
        if counts["skip"]:
            text += f", <span style='color:#d13438'><b>{counts['skip']} skipped</b></span> (a different file is in the way)"
        # Moves only move audio files, so anything else keeps the inner folder.
        others = 0
        for folder in (n.inner for n in nested):
            for _, _, names in os.walk(folder):
                others += sum(1 for n in names if not catalog.is_audio_file(n)
                              and n not in (".take_folder", ".daily_folder", ".DS_Store"))
        if others:
            text += (f". {others} other file(s) (not audio, e.g. recorder reports) stay in the inner folder, so it "
                     "is not removed")
        return text + "."

    def merge_nested(self, nested: list[duplicates.NestedFolder]):
        nested = [n for n in nested if n is not None]
        if not nested:
            return
        recs = self.recs_getter()
        plan = [a for n in nested for a in duplicates.plan_nested_merge(n, recs)]
        removals = [(a.rec, a.dst, a.level) for a in plan if a.kind == "remove"]
        moves = [(a.rec, a.dst) for a in plan if a.kind == "move"]
        different = [(a.rec, a.dst) for a in plan if a.kind == "differs"]
        replacing = [(a.rec, a.other, a.level) for a in plan if a.kind == "replace"]
        if not (removals or moves or different or replacing):
            QMessageBox.information(self, "Nested folders", "Nothing to do: every file is skipped.")
            return
        ok, permanent, remove_empty, review = confirm_removal(
            self, len(removals) + len(replacing), sum(r.size for r, _, _ in removals) +
            sum(rep[1].size for rep in replacing), len(moves), 0, len(different))
        if not ok:
            return
        root = self.root_getter()
        label = ("merge of nested folder " + _relative(nested[0].inner, root) if len(nested) == 1
                 else f"merge of {len(nested)} nested folders")
        self._rescan_nested_after = True
        self.cleanupRequested.emit(Cleanup(label, removals, moves, [], permanent, remove_empty, review, different,
                                           replacing))

    # ------------------------------------------------------------ after a cleanup

    def cleanup_finished(self):
        """The library changed: old results no longer apply."""
        self.file_groups, self.project_groups = [], []
        self.files_tree.clear()
        self.projects_tree.clear()
        self._project_selected()  # also empties the keep folder / project name boxes
        self._update_files_summary()
        self.files_status.setText("Done. Scan again to see what is left.")
        self.projects_status.setText("Done. Find again to see what is left.")
        self.nested_tree.clear()
        self._nested_selected()
        self._update_nested_buttons()
        if getattr(self, "_rescan_nested_after", False):
            self._rescan_nested_after = False
            self.find_nested()
        if getattr(self, "_rescan_projects_after", False):
            # After a merge, show what is left straight away (this takes a second).
            self._rescan_projects_after = False
            self.find_projects()

    def closeEvent(self, event):
        self._cancel()
        if self._worker is not None:
            self._worker.wait(5000)
        super().closeEvent(event)


def _existing_spelling(root: str, parts: list[str]) -> str:
    """root/parts as written on disk. On a share (or Mac disk) that ignores
    upper/lower case, "Night Shift" is the existing "NIGHT SHIFT" folder;
    planning with the typed spelling would "move" files onto themselves and
    promise a folder name that never appears."""
    path = root
    for part in parts:
        try:
            with os.scandir(path) as entries:
                names = [e.name for e in entries if e.is_dir()]
        except OSError:
            names = []
        if part not in names:
            part = next((n for n in names if n.casefold() == part.casefold()), part)
        path = os.path.join(path, part)
    return path


def _relative(path: str, root: str) -> str:
    if not path:
        return ""
    try:
        relative = os.path.relpath(path, root)
    except ValueError:
        return path
    return "" if relative == "." else relative


class _Job:
    """What make_cleanup_work expects of a job, for a batch worker."""

    def __init__(self, worker: "_BatchWorker"):
        self.worker = worker
        self.cancelled = False

    def report(self, done: int, total: int, text: str = ""):
        self.worker.status.emit(done, total, text)


class _BatchWorker(QThread):
    status = Signal(int, int, str)
    done = Signal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(_Job(self)), "")
        except Exception as error:  # noqa: BLE001
            self.done.emit(None, f"{error}\n{traceback.format_exc()}")


class BatchMergeDialog(QDialog):
    """Merge many project groups one after another, each with its suggested
    keep folder (and name), with a progress bar and result per group. Each
    group is re-planned from the library as it is when its turn comes (an
    earlier merge may have moved some of its files)."""

    COLUMNS = ["Group", "Keep folder", "Plan", "Progress", "Status", "Result"]
    STATUS = 4
    RESULT = 5

    def __init__(self, groups: list[duplicates.ProjectGroup], window: "DuplicatesWindow", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Merge {len(groups)} groups")
        self.window = window
        self.runner = window.runner
        self.groups = groups
        self.merged = False
        self.stop_requested = False
        self.parts: list = []
        self.skipped: list[str] = []
        self.need_rewrite: list = []
        self.freed = 0
        self.merged_count = 0
        self.summaries: list[str] = []
        self._worker: _BatchWorker | None = None
        self._index = -1
        self._mode = ("skip", False, True)  # review mode, permanent, remove empty
        root = window.root_getter()

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 210)
        self.tree.setColumnWidth(3, 130)
        self.tree.setColumnWidth(4, 260)
        self.tree.header().setSectionResizeMode(self.RESULT, QHeaderView.ResizeMode.Stretch)
        self.rows: list[QTreeWidgetItem] = []
        self.bars: list[QProgressBar] = []
        for group in groups:
            item = QTreeWidgetItem([" / ".join(group.names), "", "planning…", "", "waiting", ""])
            item.setToolTip(0, "\n".join(group.names))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            self.tree.addTopLevelItem(item)
            # The bar only shows progress; what is happening is in the Status column.
            bar = QProgressBar()
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setFormat("%p%")
            self.tree.setItemWidget(item, 3, bar)
            self.rows.append(item)
            self.bars.append(bar)
        self.overall = QProgressBar()
        self.overall.setRange(0, len(groups))
        self.overall.setFormat("%v of %m groups")
        self.status = QLabel("Planning each merge…")
        self.status.setWordWrap(True)
        self.start_button = QPushButton("Start Merging…")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop After This Merge")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        self.new_folders = QCheckBox("Put each project in a new folder named after it (the suggested name, "
                                     "inside the library)")
        self.new_folders.setToolTip("Instead of keeping one of the existing folders, every group is merged into "
                                    "a new folder; the old folders are removed once they are empty")
        self.new_folders.toggled.connect(self._replan)
        hint = QLabel("Each group uses its suggested keep folder (outside the card dumps, with the most files), or "
                      "a new folder, and for name groups the name most files use. Uncheck a row to leave that "
                      "group out. Folders left empty are removed. The whole batch is one undo step; a report of "
                      "anything that failed or was left alone is shown at the end.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.overall, 1)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.new_folders)
        layout.addWidget(self.tree, 1)
        layout.addWidget(hint)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.resize(1200, 660)
        self.plans: list[list[duplicates.Action]] = [[] for _ in groups]
        self.targets: list[tuple[str, str]] = [("", "") for _ in groups]
        self._root = root
        self._plan_all()

    # ------------------------------------------------------------ planning

    def _settings(self):
        return self.window.recs_getter(), self._root, self.window.containers_getter(), self.window.cache_file

    def _replan(self):
        if self._worker is not None or self._index >= 0:
            return
        self.start_button.setEnabled(False)
        for n, row in enumerate(self.rows):
            row.setText(1, "")
            row.setText(2, "planning…")
            row.setText(self.STATUS, "waiting")
        self._plan_all()

    def _plan_all(self):
        recs, root, containers, cache_file = self._settings()
        groups = self.groups
        new_folders = self.new_folders.isChecked()
        self.new_folders.setEnabled(False)

        def work(job):
            cache = catalog.Cache(cache_file)
            try:
                fingerprints = duplicates.Fingerprints(cache)
                results = []
                for n, group in enumerate(groups):
                    job.report(n, len(groups), f"Planning {' / '.join(group.names)}…")
                    name = duplicates.default_project_name(group)
                    if new_folders:
                        target = os.path.join(root, duplicates.suggested_folder_name(group, name))
                    else:
                        target = duplicates.suggest_keep_folder(group, root, containers)[0]
                    results.append((target, name, duplicates.plan_project_merge(
                        recs, group, target, fingerprints, name, True, root, containers)))
                return results
            finally:
                cache.close()

        self._worker = _BatchWorker(work, self)
        self._worker.status.connect(lambda d, t, text: (self.overall.setRange(0, max(t, 1)),
                                                        self.overall.setValue(d), self.status.setText(text)))
        self._worker.done.connect(self._planned)
        self._worker.start()

    def _planned(self, results, error):
        self._worker = None
        self.new_folders.setEnabled(True)
        if error:
            self.status.setText("Planning failed.")
            QMessageBox.warning(self, "Batch merge", error)
            return
        seen_targets: dict[str, int] = {}
        for target, _, _ in results:
            seen_targets[target] = seen_targets.get(target, 0) + 1
        for n, (target, name, plan) in enumerate(results):
            self.targets[n] = (target, name)
            self.plans[n] = plan
            row = self.rows[n]
            shown = _relative(target, self._root)
            if not os.path.isdir(target):
                shown += "  (new)"
            if seen_targets[target] > 1:
                shown += "  ⚠ also used by another group"
                row.setForeground(1, QBrush(RED))
            else:
                row.setForeground(1, QBrush(row.foreground(0)))
            row.setText(1, shown)
            row.setToolTip(1, target)
            row.setText(2, _plan_text(plan, name))
            if not any(a.kind in ("remove", "move", "retag", "differs", "replace") for a in plan):
                row.setCheckState(0, Qt.CheckState.Unchecked)
                row.setText(self.STATUS, "nothing to do")
        self.overall.setRange(0, len(self.groups))
        self.overall.setValue(0)
        removals = sum(1 for p in self.plans for a in p if a.kind == "remove")
        freed = sum(a.rec.size for p in self.plans for a in p if a.kind == "remove")
        self.status.setText(f"Planned: {removals} identical file(s) to remove ({human_size(freed)}), "
                            f"{sum(1 for p in self.plans for a in p if a.kind == 'move')} to move in. "
                            "Press Start Merging.")
        self.start_button.setEnabled(True)

    # ------------------------------------------------------------ running

    def start(self):
        chosen = [n for n, row in enumerate(self.rows) if row.checkState(0) == Qt.CheckState.Checked]
        if not chosen:
            return
        count = lambda kind: sum(1 for n in chosen for a in self.plans[n] if a.kind == kind)  # noqa: E731
        size = sum(a.rec.size for n in chosen for a in self.plans[n] if a.kind == "remove")
        ok, permanent, remove_empty, review = confirm_removal(
            self, count("remove"), size, count("move"), count("retag"), count("differs"))
        if not ok:
            return
        self._mode = (review, permanent, remove_empty)
        self._queue = chosen
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.close_button.setEnabled(False)
        self.new_folders.setEnabled(False)
        for row in self.rows:
            row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        # Stop playing anything in these groups (SMB can refuse to move open files).
        paths = {r.path for n in chosen for r in self.groups[n].files}
        self._released = self.runner._release_player(paths)
        self.overall.setRange(0, len(chosen))
        self.overall.setValue(0)
        self._next()

    def _stop(self):
        self.stop_requested = True
        self.stop_button.setEnabled(False)
        self.status.setText("Stopping after the current merge…")

    def _next(self):
        if self.stop_requested or not self._queue:
            self._finish()
            return
        n = self._queue.pop(0)
        self._index = n
        bar = self.bars[n]
        bar.setRange(0, 0)  # moving until the first progress report
        self.rows[n].setText(self.STATUS, "planning…")
        self.rows[n].setText(self.RESULT, "")
        self.tree.scrollToItem(self.rows[n])
        review, permanent, remove_empty = self._mode
        recs, root, containers, cache_file = self._settings()
        known = {r.path for r in recs}
        group = self.groups[n]
        target, name = self.targets[n]
        runner = self.runner
        label = f"merge of {' / '.join(group.names)}"
        self.status.setText(f"Merging {' / '.join(group.names)}…")

        def work(job):
            # Re-plan from the library as it is now.
            fresh = duplicates.refresh_group(group, recs, root)
            cache = catalog.Cache(cache_file)
            try:
                plan = duplicates.plan_project_merge(recs, fresh, target, duplicates.Fingerprints(cache), name,
                                                     True, root, containers)
            finally:
                cache.close()
            removals = [(a.rec, a.dst, a.level) for a in plan if a.kind == "remove"]
            moves = [(a.rec, a.dst) for a in plan if a.kind == "move"]
            retags = [(a.rec, a.dst if a.kind == "move" else a.rec.path, name) for a in plan
                      if name and a.rec.project != name and a.kind in ("move", "retag")]
            differs = [(a.rec, a.dst) for a in plan if a.kind == "differs"]
            replacing = [(a.rec, a.other, a.level) for a in plan if a.kind == "replace"]
            retags += [(a.rec, a.dst, name) for a in plan if a.kind == "replace" and name and a.rec.project != name]
            cleanup = Cleanup(label, removals, moves, retags, permanent, remove_empty, review, differs, replacing)
            if not (removals or moves or retags or replacing or (differs and review != "skip")):
                return cleanup, None
            return cleanup, runner.make_cleanup_work(cleanup, known, recs)(job)

        self._worker = _BatchWorker(work, self)
        self._worker.status.connect(self._progress)
        self._worker.done.connect(self._merged)
        self._worker.start()

    def _progress(self, done: int, total: int, text: str):
        bar = self.bars[self._index]
        bar.setRange(0, max(total, 0))  # 0: working, amount unknown (a moving bar)
        bar.setValue(min(done, total) if total > 0 else 0)
        if text:
            row = self.rows[self._index]
            row.setText(self.STATUS, text[0].lower() + text[1:] if text[:1].isupper() else text)
            row.setToolTip(self.STATUS, text)

    def _merged(self, result, error):
        self._worker = None
        n = self._index
        bar, row = self.bars[n], self.rows[n]
        bar.setRange(0, 1)
        if error:
            bar.setValue(0)
            row.setText(self.STATUS, "failed")
            row.setForeground(self.STATUS, QBrush(RED))
            row.setText(self.RESULT, error.splitlines()[0])
            row.setForeground(self.RESULT, QBrush(RED))
            self.skipped.append(f"{' / '.join(self.groups[n].names)}: {error.splitlines()[0]}")
        else:
            cleanup, out = result
            bar.setValue(1)
            if out is None:
                row.setText(self.STATUS, "nothing to do")
            else:
                done = self.runner.finish_cleanup(cleanup, out)
                self.parts += done["parts"]
                self.need_rewrite += done["need_rewrite"]
                self.skipped += [f"{' / '.join(self.groups[n].names)}: {line}" for line in done["skipped"]]
                self.freed += done["freed"]
                self.merged = True
                self.merged_count += 1
                row.setText(self.STATUS, "done ✓" if not done["skipped"]
                            else f"done, {len(done['skipped'])} left alone")
                row.setText(self.RESULT, done["summary"])
                row.setToolTip(self.RESULT, done["summary"])
                if done["skipped"]:
                    row.setForeground(self.STATUS, QBrush(RED))
                    row.setForeground(self.RESULT, QBrush(RED))
        self.overall.setValue(self.overall.value() + 1)
        self._next()

    def _finish(self):
        self.stop_button.setEnabled(False)
        self.close_button.setEnabled(True)
        extra = self.runner.write_rewrites(self.need_rewrite, self.skipped) if self.need_rewrite else None
        parts = self.parts + ([extra] if extra else [])
        done_count = self.merged_count
        summary = (f"Merged {done_count} group(s), freed {human_size(self.freed)}."
                   + (" Stopped early; the rest were not merged." if self.stop_requested and self._queue else ""))
        self.status.setText(summary)
        if getattr(self, "_released", None) is not None:
            self.runner._restore_player(self._released, {})
        failed = sum(1 for row in self.rows if row.text(self.STATUS) == "failed")
        if failed:
            summary += f" {failed} group(s) failed."
        if parts or self.merged or self.skipped:
            # Shows a report listing everything that failed or was left alone.
            self.runner.cleanups_done(f"batch merge of {done_count} group(s)", parts, summary, self.skipped)

    def reject(self):
        if self._worker is not None:
            return  # a merge is running; use Stop
        super().reject()


def _plan_text(plan: list[duplicates.Action], name: str) -> str:
    counts = {k: sum(1 for a in plan if a.kind == k) for k in ("remove", "move", "retag", "differs", "skip", "replace")}
    parts = []
    if counts["remove"]:
        parts.append(f"remove {counts['remove']} ({human_size(sum(a.rec.size for a in plan if a.kind == 'remove'))})")
    if counts["move"]:
        parts.append(f"move {counts['move']}")
    if counts["retag"]:
        parts.append(f"rename {counts['retag']} → {name}")
    if counts["replace"]:
        parts.append(f"{counts['replace']} replaced by a fuller version")
    if counts["differs"]:
        parts.append(f"{counts['differs']} different")
    if counts["skip"]:
        parts.append(f"{counts['skip']} skipped")
    return ", ".join(parts) or "nothing to do"
