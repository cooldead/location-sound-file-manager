"""The Sync with Video window: pick a camera folder, see which takes run with
each clip (matched by timecode), tick the takes to use, and choose what to make:
synced video copies, a timeline for an editor, or both. The dialog only
collects choices; MainWindow.video_sync writes the files."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from dataclasses import replace

from . import compat, settings, timecode as tc, video_sync
from .catalog import Recording
from .workers import run_job

COLUMNS = ["Clip / take", "Timecode", "Take starts", "Note"]
MATCH_ROLE = Qt.ItemDataRole.UserRole + 1


def _offset_text(offset) -> str:
    return f"{float(offset):+.3f} s"


def clip_timecode(clip: video_sync.Clip) -> str:
    return clip.timecode + (f" @ {tc.rate_label(clip.rate, clip.drop_frame)}" if clip.rate else "")


class VideoSyncDialog(QDialog):
    def __init__(self, recs: list[Recording], qsettings: QSettings, parent=None, *, clips=None, folder: str = "",
                 scope: str = ""):
        """recs: the recordings to match against; clips/folder: already probed
        clips (tests); scope: says where recs came from."""
        super().__init__(parent)
        self.recs = recs
        self.qsettings = qsettings
        self.clips: list[video_sync.Clip] = list(clips or [])
        self.matches: list[video_sync.Match] = []
        self.folder = folder
        self.setWindowTitle("Sync with Video")
        self.resize(900, 620)

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Camera folder:"))
        self.folder_label = QLabel()
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.folder_label, 1)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self.choose_folder)
        row.addWidget(choose)
        layout.addLayout(row)
        self.scope_label = QLabel(f"Takes from {scope or f'{len(recs):,} recordings'}. Clips and takes are matched "
                                  "by timecode (and date, when both have one).")
        self.scope_label.setWordWrap(True)
        layout.addWidget(self.scope_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 170)
        self.tree.setColumnWidth(2, 110)
        self.tree.itemChanged.connect(lambda *_: self._update_ok())
        layout.addWidget(self.tree, 1)

        outputs = QGridLayout()
        self.make_copies = QCheckBox("Synced video copies (video copied as it is, each ticked take added as audio "
                                     "tracks) into:")
        self.make_copies.setChecked(settings.get(qsettings, "video_sync_copies"))
        self.copy_folder = QLineEdit()
        self.copy_folder.setPlaceholderText("a Synced folder in the camera folder")
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._choose_copy_folder)
        outputs.addWidget(self.make_copies, 0, 0, 1, 3)
        outputs.addWidget(self.copy_folder, 1, 0, 1, 2)
        outputs.addWidget(browse, 1, 2)
        self.make_timeline = QCheckBox("Timeline file for:")
        self.make_timeline.setChecked(settings.get(qsettings, "video_sync_timeline"))
        self.editor = QComboBox()
        for key, (name, ext) in video_sync.EDITORS.items():
            self.editor.addItem(f"{name} ({ext})", key)
        index = self.editor.findData(settings.get(qsettings, "video_sync_editor"))
        self.editor.setCurrentIndex(max(index, 0))
        outputs.addWidget(self.make_timeline, 2, 0)
        outputs.addWidget(self.editor, 2, 1)
        note = QLabel("Copies are always MOV (it holds the recorders' audio as it is). Premiere places takes on "
                      "whole frames (within half a frame); Final Cut and Resolve keep them to the sample.")
        note.setWordWrap(True)
        note.setEnabled(False)
        outputs.addWidget(note, 3, 0, 1, 3)
        layout.addLayout(outputs)
        for box in (self.make_copies, self.make_timeline):
            box.toggled.connect(lambda *_: self._update_ok())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.addButton("Sync", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._show_clips()

    # ---------------------------------------------------------------- clips

    def choose_folder(self):
        start = self.folder or settings.get(self.qsettings, "video_sync_folder")
        folder = QFileDialog.getExistingDirectory(self, "Camera Folder", start)
        if folder:
            self.load_folder(compat.fwd(folder))

    def load_folder(self, folder: str):
        def work(job):
            return video_sync.scan_folder(folder, lambda d, t, name: job.report(d, t, f"Reading {name}…"),
                                          lambda: job.cancelled)
        clips, error = run_job(self, "Reading clips", work)
        if error is not None:
            if not isinstance(error, InterruptedError):
                QMessageBox.warning(self, "Sync with Video", f"The folder could not be read:\n{error}")
            return
        self.folder, self.clips = folder, clips
        settings.put(self.qsettings, "video_sync_folder", folder)
        self._show_clips()

    def _show_clips(self):
        self.folder_label.setText(self.folder or "<i>none yet: choose the folder with the camera's clips</i>")
        self.matches = video_sync.match(self.recs, self.clips)
        by_clip = {id(group[0].clip): group for group in video_sync.by_clip(self.matches)}
        self.tree.blockSignals(True)
        self.tree.clear()
        for clip in self.clips:
            found = by_clip.get(id(clip), [])
            item = QTreeWidgetItem([clip.name, clip_timecode(clip), "", ""])
            item.setToolTip(0, clip.path)
            if clip.error:
                item.setText(3, f"Left out: {clip.error}")
                item.setDisabled(True)
            elif not found:
                item.setText(3, f"{tc.format_duration(clip.duration)}; no take runs with it")
                item.setDisabled(True)
            else:
                item.setText(3, f"{tc.format_duration(clip.duration)}; {len(found)} take{'s' * (len(found) > 1)}")
            for m in found:
                child = QTreeWidgetItem([m.rec.name, m.rec.start_tc, _offset_text(m.offset),
                                         f"runs together {m.overlap:.1f} s"])
                child.setToolTip(0, m.rec.path)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if m.chosen else Qt.CheckState.Unchecked)
                child.setData(0, MATCH_ROLE, m)
                item.addChild(child)
            self.tree.addTopLevelItem(item)
            item.setExpanded(bool(found))
        self.tree.blockSignals(False)
        if self.folder and not self.copy_folder.text():
            self.copy_folder.setText(f"{self.folder}/Synced")
        self._update_ok()

    # ---------------------------------------------------------------- choices

    def chosen(self) -> list[video_sync.Match]:
        """The ticked takes, clip by clip (copies marked chosen, which the timeline places)."""
        picked = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for k in range(top.childCount()):
                child = top.child(k)
                if child.checkState(0) == Qt.CheckState.Checked:
                    picked.append(replace(child.data(0, MATCH_ROLE), chosen=True))
        return picked

    def copies_folder(self) -> str | None:
        if not self.make_copies.isChecked():
            return None
        return self.copy_folder.text().strip() or f"{self.folder}/Synced"

    def editor_key(self) -> str | None:
        return self.editor.currentData() if self.make_timeline.isChecked() else None

    def _choose_copy_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder for Synced Copies", self.copy_folder.text()
                                                  or self.folder)
        if folder:
            self.copy_folder.setText(compat.fwd(folder))

    def _update_ok(self):
        self.ok_button.setEnabled(bool(self.chosen()) and (self.make_copies.isChecked()
                                                           or self.make_timeline.isChecked()))

    def accept(self):
        settings.put(self.qsettings, "video_sync_copies", self.make_copies.isChecked())
        settings.put(self.qsettings, "video_sync_timeline", self.make_timeline.isChecked())
        settings.put(self.qsettings, "video_sync_editor", self.editor.currentData())
        super().accept()
