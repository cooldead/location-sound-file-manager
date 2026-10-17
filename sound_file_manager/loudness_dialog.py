"""The Loudness Check window: measures the selected files (integrated
loudness, true peak, loudness range) and checks them against a platform's
delivery spec, with that platform's guide beside it. Measuring only reads
files. "Fix Automatically…" has the main window write fixed copies (after
one confirmation) and measures them; "Fix in Effects…" hands over to the
Effects window instead, to adjust the chain first."""

from __future__ import annotations

import math
import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QSplitter, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import loudness, processing, settings
from .catalog import Recording
from .processing_dialog import processable, track_names
from .workers import run_job

MAKE_COPIES = 2                      # the dialog's result when the user wants copies at the target

COLUMNS = ["File", "Track", "Integrated", "True peak", "LRA", "Max short-term", "Result"]


def _fmt(value: float | None, unit: str) -> str:
    if value is None or (isinstance(value, float) and math.isinf(value)):
        return "–"
    return f"{value:.1f} {unit}"


def guide_html(target: loudness.Target) -> str:
    tolerance = (f"±{target.tolerance:g} LU" if target.tolerance is not None
                 else "none: louder is turned down, quieter is left")
    measured = ("dialogue-gated (the platform's own speech detector); checked here over the whole programme"
                if target.gating == "dialogue" else "whole programme, ITU-R BS.1770")
    source = target.source
    link = f'<a href="{source}">{source}</a>' if source.startswith("http") else source
    status = ("<b>Published by the platform.</b>" if target.official else
              "<b>Not published by the platform</b>: these are the commonly used figures. Check the delivery spec "
              "you were given.")
    return (f"<h3>{target.name}</h3>"
            f"<table cellspacing=4>"
            f"<tr><td>Target</td><td><b>{target.integrated:g} LUFS</b> (LKFS is the same unit)</td></tr>"
            f"<tr><td>Tolerance</td><td>{tolerance}</td></tr>"
            f"<tr><td>True peak</td><td>no higher than <b>{target.true_peak:g} dBTP</b></td></tr>"
            f"<tr><td>Measured</td><td>{measured}</td></tr></table>"
            f"<p>{target.guide}</p><p>{status}<br>Source: {link}<br>Checked {loudness.CHECKED}; specs change, so "
            f"confirm before a delivery.</p>"
            "<p><i>These are specs for a finished mix. For production tracks the check is a guide (dailies, rough "
            "cuts, or content that goes out without a mix).</i></p>")


class LoudnessCheckDialog(QDialog):
    """Measure and check the selected recordings against a delivery target."""

    def __init__(self, recs: list[Recording], qsettings: QSettings, parent=None, fix=None):
        """fix(jobs, target, together, parent) -> the copies or None: writes fixed
        copies of [(recording, channels)] (MainWindow.fix_loudness)."""
        super().__init__(parent)
        self.recs = [r for r in recs if processable(r) is None]
        self.qsettings = qsettings
        self.fix = fix
        self.results: list[loudness.Measurement] = []
        self.measured: dict[str, list[loudness.Measurement]] = {}   # path -> its rows, from the last Measure
        self.measured_with: tuple | None = None                       # (ticked names, together) of that Measure
        self.setWindowTitle("Loudness Check")

        top = QHBoxLayout()
        top.addWidget(QLabel("Deliver to:"))
        self.platform = QComboBox()
        group = None
        for target in loudness.TARGETS:
            if target.group != group:
                if group is not None:
                    self.platform.insertSeparator(self.platform.count())
                group = target.group
            self.platform.addItem(f"{target.name} ({target.integrated:g} LUFS, {target.true_peak:g} dBTP)",
                                  target.key)
        saved = settings.get(qsettings, "loudness_platform")
        index = self.platform.findData(saved)
        self.platform.setCurrentIndex(index if index >= 0 else 0)
        self.platform.currentIndexChanged.connect(self._platform_changed)
        top.addWidget(self.platform, 1)

        self.guide = QTextBrowser()
        self.guide.setOpenExternalLinks(True)
        self.guide.setMinimumWidth(320)

        # What to measure
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        tracks_box = QGroupBox("Tracks")
        self.tracks = QListWidget()
        names: list[str] = []
        for rec in self.recs:
            for name in track_names(rec):
                if name not in names:
                    names.append(name)
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.tracks.addItem(item)
        self.tracks.setMaximumHeight(min(110, 22 * max(2, len(names)) + 8))
        self.each = QRadioButton("Each track on its own")
        self.together = QRadioButton("Ticked tracks together, as one programme (e.g. a stereo mix)")
        (self.together if settings.get(qsettings, "loudness_together") else self.each).setChecked(True)
        tracks_layout = QVBoxLayout(tracks_box)
        tracks_layout.addWidget(self.tracks)
        tracks_layout.addWidget(self.each)
        tracks_layout.addWidget(self.together)
        left_layout.addWidget(tracks_box)
        measure_row = QHBoxLayout()
        self.measure_button = QPushButton("Measure")
        self.measure_button.setDefault(True)
        self.measure_button.clicked.connect(self.measure)
        count = len(self.recs)
        self.summary = QLabel(f"{count:,} file{'s' if count != 1 else ''} selected. Measuring reads each file once; "
                              "nothing is changed.")
        self.summary.setWordWrap(True)
        measure_row.addWidget(self.measure_button)
        measure_row.addWidget(self.summary, 1)
        left_layout.addLayout(measure_row)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(COLUMNS)
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        left_layout.addWidget(self.table, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.guide)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_button = self.buttons.addButton("Copy Results", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.clicked.connect(self.copy_results)
        self.fix_button = self.buttons.addButton("Fix Automatically…", QDialogButtonBox.ButtonRole.ActionRole)
        self.fix_button.setToolTip("Makes copies at this platform's spec: loudness to its target, then a true-peak "
                                   "limiter just under its limit, and measures them. After a Measure, only the files "
                                   "that failed. The originals aren't changed; Undo deletes the copies.")
        self.fix_button.clicked.connect(self.fix_automatically)
        self.fix_button.setVisible(fix is not None)
        self.make_button = self.buttons.addButton("Fix in Effects…", QDialogButtonBox.ButtonRole.ActionRole)
        self.make_button.setToolTip("Opens Effects with this platform's delivery preset, to adjust it before making "
                                    "the copies. The originals aren't changed.")
        self.make_button.clicked.connect(self._make_copies)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addWidget(self.buttons)
        self._platform_changed()
        self.resize(1100, 640)

    # ------------------------------------------------------------ choices

    def target(self) -> loudness.Target:
        return loudness.BY_KEY[self.platform.currentData()]

    def picked_names(self) -> set[str]:
        return {self.tracks.item(i).text() for i in range(self.tracks.count())
                if self.tracks.item(i).checkState() == Qt.CheckState.Checked}

    def channels_for(self, rec: Recording) -> list[int]:
        picked = self.picked_names()
        return [i for i, name in enumerate(track_names(rec)) if name in picked]

    def _platform_changed(self):
        settings.put(self.qsettings, "loudness_platform", self.platform.currentData())
        self.guide.setHtml(guide_html(self.target()))
        self._fill()

    # ------------------------------------------------------------ measuring

    def jobs(self) -> list[tuple[Recording, list[int]]]:
        """The selected files with their ticked tracks (files with none left out)."""
        jobs = [(rec, self.channels_for(rec)) for rec in self.recs]
        return [(rec, channels) for rec, channels in jobs if channels]

    def measure(self):
        jobs = self.jobs()
        if not jobs:
            QMessageBox.information(self, "Loudness Check", "Tick at least one track that these files have.")
            return
        together = self.together.isChecked()
        settings.put(self.qsettings, "loudness_together", together)
        measured = self._measure([(rec.path, rec, channels) for rec, channels in jobs], together, "Measuring")
        if measured is None:
            return
        self.measured = measured
        self.measured_with = (frozenset(self.picked_names()), together)
        self.results = [m for rows in measured.values() for m in rows]
        self._fill()

    def _measure(self, entries: list[tuple[str, Recording, list[int]]], together: bool,
                 verb: str) -> dict[str, list[loudness.Measurement]] | None:
        """Measure (path, recording it belongs to, channels) entries; None if
        cancelled or stopped. Files that can't be read are listed afterwards."""
        k = 1000
        total = sum(rec.frames for _, rec, _ in entries) // k + 1

        def work(job):
            out, failed = {}, []
            for index, (path, rec, channels) in enumerate(entries):
                before = sum(r.frames for _, r, _ in entries[:index]) // k
                job.report(before, total, f"{verb} {index + 1} of {len(entries)}: {os.path.basename(path)}…")
                try:
                    out[path] = loudness.measure_file(path, channels, track_names(rec), together,
                                                      progress=lambda d, t, b=before: job.report(b + d // k, total),
                                                      cancelled=lambda: job.cancelled)
                except (OSError, ValueError) as error:
                    failed.append(f"{os.path.basename(path)}: {error}")
            return out, failed

        result, error = run_job(self, "Measuring loudness", work)
        if error is not None:
            if not isinstance(error, InterruptedError):
                QMessageBox.warning(self, "Loudness Check", f"Measuring stopped:\n{error}")
            return None
        out, failed = result
        if failed:
            QMessageBox.warning(self, "Loudness Check", "Some files couldn't be measured:\n" + "\n".join(failed))
        return out

    # ------------------------------------------------------------ fixing

    def to_fix(self) -> tuple[list[tuple[Recording, list[int]]], int, bool]:
        """The files Fix would copy, how many are left out as already within
        spec, and whether that is known from a Measure with the current ticks."""
        jobs = self.jobs()
        measured = self.measured_with == (frozenset(self.picked_names()), self.together.isChecked())
        if not measured:
            return jobs, 0, False
        target = self.target()
        failing = [(rec, channels) for rec, channels in jobs
                   if rec.path not in self.measured
                   or not all(loudness.check(m, target).ok for m in self.measured[rec.path])]
        return failing, len(jobs) - len(failing), True

    def fix_automatically(self):
        target = self.target()
        jobs, passing, measured = self.to_fix()
        if not jobs:
            QMessageBox.information(self, "Loudness Check",
                                    f"Every measured file is already within {target.name}'s spec: nothing to fix."
                                    if passing else "Tick at least one track that these files have.")
            return
        together = self.together.isChecked()
        settings.put(self.qsettings, "loudness_together", together)
        if settings.get(self.qsettings, "confirm_loudness_fix"):
            count = f"{len(jobs):,} file{'s' if len(jobs) != 1 else ''}"
            how = ("one gain for the ticked tracks together (the mix stays as it is)" if together
                   else "each ticked track on its own")
            where = settings.get(self.qsettings, "effects_folder") == "processed"
            example = processing.output_path(jobs[0][0].path, settings.get(self.qsettings, "effects_folder"),
                                             loudness.copy_suffix(target))
            lines = [f"Make fixed copies of {count} for {target.name}?", "",
                     f"• Loudness to {target.integrated:g} LUFS, {how}",
                     f"• Then a true-peak limiter at {target.true_peak - 0.3:g} dBTP "
                     f"(the limit is {target.true_peak:g})",
                     f"• Copies: {(processing.PROCESSED_FOLDER + '/') if where else ''}"
                     f"{os.path.basename(example)}" + (" …" if len(jobs) > 1 else ""),
                     "", "The originals aren't changed, and the copies are measured afterwards. "
                     "Undo deletes the copies."]
            if passing:
                lines.insert(1, f"({passing:,} already within the spec {'is' if passing == 1 else 'are'} left out.)")
            elif not measured:
                lines.insert(1, "(Not measured yet with these ticks, so every selected file gets a copy.)")
            box = QMessageBox(QMessageBox.Icon.Question, "Fix Automatically", "\n".join(lines),
                              QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel, self)
            box.button(QMessageBox.StandardButton.Ok).setText("Make Copies")
            # Keep a reference: PySide doesn't hand the checkbox to the box (see CLAUDE.md).
            dont_ask = QCheckBox("Don't ask again")
            box.setCheckBox(dont_ask)
            if box.exec() != QMessageBox.StandardButton.Ok:
                return
            if dont_ask.isChecked():
                settings.put(self.qsettings, "confirm_loudness_fix", False)
        written = self.fix(jobs, target, together, self)
        if not written:
            return
        after = self._measure([(path, rec, channels) for path, (rec, channels) in zip(written, jobs)], together,
                              "Checking copy")
        if after is None:
            self.summary.setText(f"Made {len(written):,} cop{'y' if len(written) == 1 else 'ies'}; they weren't "
                                 "measured.")
            return
        rows = [m for path in written for m in after.get(path, [])]
        self.results = self.results + rows
        self._fill()
        ok = sum(loudness.check(m, target).ok for m in rows)
        self.summary.setText(f"Made {len(written):,} cop{'y' if len(written) == 1 else 'ies'} (listed last): "
                             f"{ok:,} of {len(rows):,} within {target.name}'s spec. Undo in the main window deletes "
                             "them.")

    def _fill(self):
        self.table.clear()
        target = self.target()
        passed = 0
        bad = QBrush(QColor("#c0392b"))
        good = QBrush(QColor("#27ae60"))
        for m in self.results:
            verdict = loudness.check(m, target)
            passed += verdict.ok
            row = QTreeWidgetItem([m.file, m.what, _fmt(m.integrated, "LUFS"), _fmt(m.true_peak, "dBTP"),
                                   _fmt(m.lra, "LU"), _fmt(m.max_short_term, "LUFS"),
                                   ("✓ " if verdict.ok else "✗ ") + verdict.lines[0]])
            row.setForeground(6, good if verdict.ok else bad)
            row.setToolTip(6, "\n".join(verdict.lines))
            row.setToolTip(2, "Integrated loudness, ITU-R BS.1770-4 (whole file)")
            row.setToolTip(3, f"True peak, oversampled; sample peak {_fmt(m.sample_peak, 'dBFS')}")
            self.table.addTopLevelItem(row)
        for column in range(len(COLUMNS) - 1):
            self.table.resizeColumnToContents(column)
        if self.results:
            self.summary.setText(f"{passed:,} of {len(self.results):,} within {target.name}'s spec. Hover a result "
                                 "for all the findings.")
        self.copy_button.setEnabled(bool(self.results))

    def copy_results(self):
        target = self.target()
        lines = ["\t".join(COLUMNS[:-1] + ["Max momentary", f"{target.name}", "Findings"])]
        for m in self.results:
            verdict = loudness.check(m, target)
            lines.append("\t".join([m.file, m.what, _fmt(m.integrated, "LUFS"), _fmt(m.true_peak, "dBTP"),
                                    _fmt(m.lra, "LU"), _fmt(m.max_short_term, "LUFS"),
                                    _fmt(m.max_momentary, "LUFS"), "pass" if verdict.ok else "fail",
                                    " ".join(verdict.lines)]))
        QGuiApplication.clipboard().setText("\n".join(lines) + "\n")
        self.summary.setText(f"Copied {len(self.results):,} rows (tab-separated, for a spreadsheet or a report).")

    def _make_copies(self):
        self.done(MAKE_COPIES)
