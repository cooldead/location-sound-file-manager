"""Review & Delete window: what "remove duplicates" set aside (the removed-
duplicates folder) and the files marked _ReviewForDeletion. Files can be
played, put back, or deleted for good; the main window does the changes."""

from __future__ import annotations

import os

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QStyle, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import duplicates
from .catalog import REMOVED_FOLDER, Recording
from .offload import human_size

PATH_ROLE = Qt.ItemDataRole.UserRole + 40
ORIGINAL_ROLE = Qt.ItemDataRole.UserRole + 41


def confirm_delete_all(parent, what: str, paths: list[str]) -> bool:
    """A strong warning before deleting many files for good: the count, the
    size, some names, and the word DELETE typed to go ahead."""
    size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
    dialog = QDialog(parent)
    dialog.setWindowTitle("Delete for good")
    icon = QLabel()
    icon.setPixmap(dialog.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(48, 48))
    text = QLabel(f"<span style='color:#d13438; font-size:large'><b>Permanently delete {len(paths):,} {what}?</b>"
                  f"</span><br><br>{human_size(size)} will be deleted from the disk. <b>This cannot be undone</b>: "
                  "the files do not go to the trash and Undo can't bring them back.")
    text.setWordWrap(True)
    names = QPlainTextEdit("\n".join(os.path.basename(p) for p in paths[:200])
                           + (f"\n… and {len(paths) - 200:,} more" if len(paths) > 200 else ""))
    names.setReadOnly(True)
    names.setMaximumHeight(140)
    prompt = QLabel("Type <b>DELETE</b> to confirm:")
    typed = QLineEdit()
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    go = box.addButton(f"Delete {len(paths):,} Files", QDialogButtonBox.ButtonRole.DestructiveRole)
    go.setEnabled(False)
    typed.textChanged.connect(lambda t: go.setEnabled(t.strip() == "DELETE"))
    go.clicked.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    top = QHBoxLayout()
    top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    top.addWidget(text, 1)
    layout = QVBoxLayout(dialog)
    layout.addLayout(top)
    layout.addWidget(names)
    layout.addWidget(prompt)
    layout.addWidget(typed)
    layout.addWidget(box)
    dialog.resize(560, dialog.sizeHint().height())
    box.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
    return dialog.exec() == QDialog.DialogCode.Accepted


class _CompareWorker(QThread):
    done = Signal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self), "")
        except Exception as error:  # noqa: BLE001
            self.done.emit(None, str(error))


class RemovedDialog(QDialog):
    deleteRequested = Signal(list, str)  # paths, what they are (for the log)
    swapRequested = Signal(str, str, str)  # marked copy, kept copy, the name the marked copy gets
    restoreRequested = Signal(list)  # [(path in the removed folder, original path)]
    playRequested = Signal(str)  # a file path

    def __init__(self, root_getter, recs_getter, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review & Delete")
        self.root_getter, self.recs_getter = root_getter, recs_getter
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_removed_tab(), REMOVED_FOLDER)
        self.tabs.addTab(self._build_review_tab(), f"Compare Marked {duplicates.REVIEW_TAG}")
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        self.resize(1250, 760)
        self.refresh()

    # ------------------------------------------------------------ removed folder

    def _build_removed_tab(self) -> QWidget:
        page = QWidget()
        self.removed_summary = QLabel()
        self.removed_summary.setWordWrap(True)
        self.removed_tree = QTreeWidget()
        self.removed_tree.setHeaderLabels(["File", "Was in", "Size"])
        self.removed_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.removed_tree.setColumnWidth(0, 320)
        self.removed_tree.setColumnWidth(1, 560)
        self.removed_tree.itemDoubleClicked.connect(self._play)
        self.removed_tree.itemSelectionChanged.connect(self._update_buttons)
        open_folder = QPushButton("Open Folder")
        open_folder.clicked.connect(self._open_removed_folder)
        self.restore_button = QPushButton("Put Back Selected")
        self.restore_button.setToolTip("Move the selected files back to where they were")
        self.restore_button.clicked.connect(self._restore)
        self.delete_selected = QPushButton("Delete Selected…")
        self.delete_selected.clicked.connect(lambda: self._delete_removed(selected_only=True))
        self.delete_all = QPushButton("Delete Everything…")
        self.delete_all.clicked.connect(lambda: self._delete_removed(selected_only=False))
        buttons = QHBoxLayout()
        buttons.addWidget(open_folder)
        buttons.addWidget(self.restore_button)
        buttons.addStretch(1)
        buttons.addWidget(self.delete_selected)
        buttons.addWidget(self.delete_all)
        hint = QLabel("Double-click a file to play it. Deleting here is permanent.")
        hint.setEnabled(False)
        layout = QVBoxLayout(page)
        layout.addWidget(self.removed_summary)
        layout.addWidget(self.removed_tree, 1)
        layout.addWidget(hint)
        layout.addLayout(buttons)
        return page

    def _fill_removed(self):
        root = self.root_getter()
        items = duplicates.removed_items(root) if root else []
        self.removed_tree.clear()
        bold = QFont()
        bold.setBold(True)
        by_project: dict[str, list] = {}
        for item in items:
            by_project.setdefault(item[1], []).append(item)
        for project in sorted(by_project, key=str.casefold):
            group = by_project[project]
            top = QTreeWidgetItem([project or "(no project)", "", human_size(sum(i[3] for i in group))])
            top.setFont(0, bold)
            for path, _, original, size in group:
                child = QTreeWidgetItem([os.path.basename(path), os.path.relpath(os.path.dirname(original), root),
                                         human_size(size)])
                child.setData(0, PATH_ROLE, path)
                child.setData(0, ORIGINAL_ROLE, original)
                child.setToolTip(0, path)
                child.setToolTip(1, original)
                top.addChild(child)
            self.removed_tree.addTopLevelItem(top)
        total = sum(i[3] for i in items)
        self.removed_items = items
        self.removed_summary.setText(
            f"<b>{len(items):,}</b> file(s), {human_size(total)}, in <b>{os.path.join(root, REMOVED_FOLDER)}</b>, "
            "sorted by the project they belonged to." if items else f"“{REMOVED_FOLDER}” is empty.")

    def _selected_removed(self) -> list[QTreeWidgetItem]:
        chosen = []
        for item in self.removed_tree.selectedItems():
            if item.data(0, PATH_ROLE):
                chosen.append(item)
            else:  # a project row selects all its files
                chosen += [item.child(i) for i in range(item.childCount())]
        return list({id(i): i for i in chosen}.values())

    def _open_removed_folder(self):
        folder = os.path.join(self.root_getter(), REMOVED_FOLDER)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder if os.path.isdir(folder) else self.root_getter()))

    def _restore(self):
        items = self._selected_removed()
        if items:
            self.restoreRequested.emit([(i.data(0, PATH_ROLE), i.data(0, ORIGINAL_ROLE)) for i in items])

    def _delete_removed(self, selected_only: bool):
        if selected_only:
            paths = [i.data(0, PATH_ROLE) for i in self._selected_removed()]
        else:
            paths = [i[0] for i in self.removed_items]
        if not paths:
            return
        if not selected_only:
            if confirm_delete_all(self, f"file(s) in “{REMOVED_FOLDER}”", paths):
                self.deleteRequested.emit(paths, "removed duplicates")
            return
        size = sum(os.path.getsize(p) for p in paths if os.path.exists(p))
        if QMessageBox.warning(self, "Delete for good", f"Permanently delete {len(paths)} selected file(s) from "
                               f"“{REMOVED_FOLDER}” ({human_size(size)})?\n\nThis cannot be undone.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) \
                == QMessageBox.StandardButton.Yes:
            self.deleteRequested.emit(paths, "removed duplicates")

    # ------------------------------------------------------------ review copies

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        find = QPushButton("Find Marked Files")
        find.setToolTip(f"List every file in the library marked “{duplicates.REVIEW_TAG}” with its closest copy")
        find.clicked.connect(self._fill_reviews)
        self.review_summary = QLabel()
        self.review_summary.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(find)
        top.addWidget(self.review_summary, 1)

        self.review_tree = QTreeWidget()
        self.review_tree.setHeaderLabels(["Marked file", "Paired with", "Match"])
        self.review_tree.setRootIsDecorated(False)
        self.review_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.review_tree.setColumnWidth(0, 300)
        self.review_tree.setColumnWidth(1, 300)
        self.review_tree.itemDoubleClicked.connect(self._play)
        self.review_tree.itemSelectionChanged.connect(self._pair_selected)
        self.review_tree.itemChanged.connect(lambda *_: self._update_buttons())
        check_all = QPushButton("Check All")
        check_all.clicked.connect(lambda: self._check_reviews(True))
        uncheck_all = QPushButton("Uncheck All")
        uncheck_all.clicked.connect(lambda: self._check_reviews(False))
        self.checked_label = QLabel()
        check_row = QHBoxLayout()
        check_row.addWidget(check_all)
        check_row.addWidget(uncheck_all)
        check_row.addWidget(self.checked_label, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.review_tree, 1)
        left_layout.addLayout(check_row)

        self.compare_tree = QTreeWidget()
        self.compare_tree.setHeaderLabels(["", "Marked copy", "Kept copy"])
        self.compare_tree.setRootIsDecorated(False)
        self.compare_tree.setColumnWidth(0, 100)
        self.compare_tree.setColumnWidth(1, 260)
        self.compare_tree.setWordWrap(True)
        self.audio_result = QLabel()
        self.audio_result.setWordWrap(True)
        self.play_review = QPushButton("▶ Play Marked Copy")
        self.play_review.clicked.connect(lambda: self._play_pair(0))
        self.play_kept = QPushButton("▶ Play Kept Copy")
        self.play_kept.clicked.connect(lambda: self._play_pair(1))
        self.compare_audio = QPushButton("Compare Audio")
        self.compare_audio.setToolTip("Read both files and check whether the audio (and the whole file) is the same")
        self.compare_audio.clicked.connect(self._compare_audio)
        play_row = QHBoxLayout()
        play_row.addWidget(self.play_review)
        play_row.addWidget(self.play_kept)
        play_row.addStretch(1)
        play_row.addWidget(self.compare_audio)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>Differences</b> (differing rows in red)"))
        right_layout.addWidget(self.compare_tree, 1)
        right_layout.addWidget(self.audio_result)
        right_layout.addLayout(play_row)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([560, 560])

        show = QPushButton("Show in Folder")
        show.clicked.connect(self._show_review)
        self.swap_review = QPushButton("Keep Marked Copy Instead…")
        self.swap_review.setToolTip(f"Move the kept copy into “{REMOVED_FOLDER}” and give the marked copy its name")
        self.swap_review.clicked.connect(self._swap)
        self.delete_review = QPushButton("Delete Checked…")
        self.delete_review.setToolTip("Delete the checked marked files for good; the copies they are paired with "
                                      "are kept")
        self.delete_review.clicked.connect(self._delete_reviews)
        self.delete_all_reviews = QPushButton("Delete All Marked Files…")
        self.delete_all_reviews.setToolTip(f"Delete every file marked “{duplicates.REVIEW_TAG}” for good")
        self.delete_all_reviews.setStyleSheet("QPushButton { color: #d13438; }")
        self.delete_all_reviews.clicked.connect(self._delete_all_reviews)
        buttons = QHBoxLayout()
        buttons.addWidget(show)
        buttons.addStretch(1)
        buttons.addWidget(self.swap_review)
        buttons.addWidget(self.delete_review)
        buttons.addWidget(self.delete_all_reviews)
        layout = QVBoxLayout(page)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)
        return page

    def _fill_reviews(self):
        root = self.root_getter()
        self.pairs = duplicates.pair_review_copies(list(self.recs_getter()))
        self.review_tree.blockSignals(True)
        self.review_tree.clear()
        for rec, match, how in self.pairs:
            item = QTreeWidgetItem([rec.name, match.name if match else "(no copy found)",
                                    how or "—"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setData(0, PATH_ROLE, rec.path)
            item.setToolTip(0, rec.path)
            item.setToolTip(1, match.path if match else "")
            self.review_tree.addTopLevelItem(item)
        self.review_tree.blockSignals(False)
        self.review_summary.setText(f"<b>{len(self.pairs):,}</b> file(s) marked “{duplicates.REVIEW_TAG}” "
                                    f"({human_size(sum(r.size for r, _, _ in self.pairs))}). Select one to compare "
                                    "it with its copy." if self.pairs
                                    else f"No files are marked “{duplicates.REVIEW_TAG}”.")
        self.compare_tree.clear()
        self.audio_result.setText("")
        self._update_buttons()

    def _current_pair(self):
        items = self.review_tree.selectedItems()
        if len(items) != 1:
            return None
        index = self.review_tree.indexOfTopLevelItem(items[0])
        return self.pairs[index] if 0 <= index < len(self.pairs) else None

    def _pair_selected(self):
        pair = self._current_pair()
        self.compare_tree.clear()
        self.audio_result.setText("")
        if pair is not None:
            rec, match, how = pair
            root = self.root_getter()
            for label, a, b, differs in duplicates.compare_rows(rec, match):
                if label == "Folder" and root:
                    a = os.path.relpath(a, root) if a else a
                    b = os.path.relpath(b, root) if b else b
                row = QTreeWidgetItem([label, a, b])
                row.setToolTip(1, a)
                row.setToolTip(2, b)
                if differs:
                    for col in range(3):
                        row.setForeground(col, QBrush(QColor("#d13438")))
                self.compare_tree.addTopLevelItem(row)
            if match is None:
                self.audio_result.setText("No copy of this recording was found in the library.")
            elif how == "same recording":
                self.audio_result.setText("Paired by recording (same length, format and timecode) because no "
                                          "file with the original name is in the same folder.")
        self._update_buttons()

    def _play_pair(self, which: int):
        pair = self._current_pair()
        if pair is not None and pair[which] is not None:
            self.playRequested.emit(pair[which].path)

    def _compare_audio(self):
        pair = self._current_pair()
        if pair is None or pair[1] is None:
            return
        rec, match, _ = pair
        self.audio_result.setText("Comparing the two files…")
        self.compare_audio.setEnabled(False)

        def work(worker):
            whole = duplicates.files_identical(rec.path, match.path)
            audio = whole or duplicates.audio_identical(rec.path, match.path)
            return whole, audio

        self._compare_worker = _CompareWorker(work, self)
        self._compare_worker.done.connect(self._compared)
        self._compare_worker.start()

    def _compared(self, result, error):
        self._compare_worker = None
        self._update_buttons()
        if error:
            self.audio_result.setText(f"<span style='color:#d13438'>Could not compare: {error}</span>")
            return
        whole, audio = result
        if whole:
            text = "<b>The two files are byte for byte identical.</b>"
        elif audio:
            text = "<b>The audio is identical</b>; only the metadata or file layout differs (see the red rows)."
        else:
            text = "<span style='color:#d13438'><b>The audio is different</b></span>; listen to both before deleting."
        self.audio_result.setText(text)

    def _show_review(self):
        items = self.review_tree.selectedItems()
        if items:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(items[0].data(0, PATH_ROLE))))

    def _swap(self):
        pair = self._current_pair()
        if pair is None or pair[1] is None:
            return
        rec, match, _ = pair
        target = os.path.join(match.folder, match.name)
        if QMessageBox.question(self, "Keep the marked copy", f"Move “{match.name}” into “{REMOVED_FOLDER}” and "
                                f"rename “{rec.name}” to “{match.name}” in its place?\n\nThis can be undone.") \
                == QMessageBox.StandardButton.Yes:
            self.swapRequested.emit(rec.path, match.path, target)

    def _checked_reviews(self) -> list[str]:
        return [self.review_tree.topLevelItem(i).data(0, PATH_ROLE) for i in range(self.review_tree.topLevelItemCount())
                if self.review_tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]

    def _check_reviews(self, checked: bool):
        self.review_tree.blockSignals(True)
        for i in range(self.review_tree.topLevelItemCount()):
            self.review_tree.topLevelItem(i).setCheckState(0, Qt.CheckState.Checked if checked else
                                                           Qt.CheckState.Unchecked)
        self.review_tree.blockSignals(False)
        self._update_buttons()

    def _delete_all_reviews(self):
        paths = [rec.path for rec, _, _ in getattr(self, "pairs", [])]
        if paths and confirm_delete_all(self, f"file(s) marked “{duplicates.REVIEW_TAG}”", paths):
            self.deleteRequested.emit(paths, "all files marked for review")

    def _delete_reviews(self):
        paths = self._checked_reviews()
        if not paths:
            return
        if QMessageBox.warning(self, "Delete for good", f"Permanently delete {len(paths)} file(s) marked "
                               f"“{duplicates.REVIEW_TAG}”? The copies they are paired with are kept.\n\n"
                               "This cannot be undone.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel) \
                == QMessageBox.StandardButton.Yes:
            self.deleteRequested.emit(paths, "files marked for review")

    # ------------------------------------------------------------ common

    def _play(self, item: QTreeWidgetItem, _column: int):
        path = item.data(0, PATH_ROLE)
        if path:
            self.playRequested.emit(path)

    def _update_buttons(self):
        has_removed = bool(self._selected_removed())
        self.restore_button.setEnabled(has_removed)
        self.delete_selected.setEnabled(has_removed)
        self.delete_all.setEnabled(bool(getattr(self, "removed_items", [])))
        checked = self._checked_reviews()
        self.delete_review.setEnabled(bool(checked))
        self.delete_review.setText(f"Delete Checked ({len(checked)})…" if checked else "Delete Checked…")
        self.delete_all_reviews.setEnabled(bool(getattr(self, "pairs", [])))
        size = sum(os.path.getsize(p) for p in checked if os.path.exists(p))
        self.checked_label.setText(f"{len(checked)} checked · {human_size(size)}" if checked else "")
        pair = self._current_pair()
        has_pair = pair is not None and pair[1] is not None
        self.play_review.setEnabled(pair is not None)
        self.play_kept.setEnabled(has_pair)
        self.swap_review.setEnabled(has_pair)
        self.compare_audio.setEnabled(has_pair and getattr(self, "_compare_worker", None) is None)

    def refresh(self):
        self._fill_removed()
        self._fill_reviews()
        self._update_buttons()
