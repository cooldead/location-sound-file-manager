"""Sound report window: header fields (prefilled from the files and from what
was typed last time), comments, a live preview, and saving PDF/CSV."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from . import report, settings
from .branding_dialog import BrandingDialog, load_branding
from .catalog import Recording


def remembered_info(qsettings, recs: list[Recording], project: str = "") -> report.ReportInfo:
    """Detected values, then the mixer's saved details, then the project's.
    project: the folder's name, used when the files disagree on the project
    (a card folder holding takes recorded under two project names)."""
    fields = report.detected_fields(recs)
    if project and (not fields.get("project") or " / " in fields["project"]):
        fields["project"] = project
    fields.update({k: v for k, v in settings.get_json(qsettings, "report_personal").items() if v})
    project = fields.get("project", "")
    fields.update({k: v for k, v in settings.get_json(qsettings, "report_projects").get(project, {}).items() if v})
    columns = [c for c in settings.get(qsettings, "report_columns") if c in report.COLUMN_DEFS]
    orientation = settings.get(qsettings, "report_orientation")
    style = settings.get(qsettings, "report_style")
    return report.ReportInfo(fields, columns=columns or list(report.DEFAULT_COLUMNS),
                             orientation=orientation if orientation in report.PAGE_WIDTH_MM else "landscape",
                             style=style if style in report.STYLES else "boxed", branding=load_branding(qsettings))


def remember(qsettings, info: report.ReportInfo) -> None:
    settings.put(qsettings, "report_columns", list(info.columns))
    settings.put(qsettings, "report_orientation", info.orientation)
    settings.put(qsettings, "report_style", info.style)
    personal = {k: info.get(k) for k in report.PERSONAL_FIELDS}
    settings.put_json(qsettings, "report_personal", personal)
    project = info.get("project")
    if project:
        projects = settings.get_json(qsettings, "report_projects")
        projects[project] = {k: info.get(k) for k in report.PROJECT_FIELDS}
        settings.put_json(qsettings, "report_projects", projects)


@dataclass
class ReportGroup:
    """One report: a project (or project/day) and its files."""
    title: str
    recs: list[Recording]
    default_folder: str = ""
    info: report.ReportInfo | None = None
    saved: list[str] = field(default_factory=list)
    key: str = ""  # the caller's id for the group (e.g. the card folder)
    project: str = ""  # project name to use when the files disagree


class _Preview(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        # The report is designed for paper: always show it black on white.
        self.setStyleSheet("QTextBrowser { background: white; color: black; }")

    def set_logo(self, path: str):
        if path:
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl("logo"), QImage(path))


class ReportDialog(QDialog):
    """Fill in report headers, one report per group (project). With several
    groups, Previous / Next step through them; each keeps its own header
    fields while columns, style and page apply to all.

    mode "export": OK keeps the details for the copy to the NAS (infos()).
    mode "save": save the PDF / CSV now (this report, or all of them)."""

    def __init__(self, groups, qsettings, *, mode: str = "save", info: report.ReportInfo | None = None,
                 default_folder: str = "", parent=None):
        super().__init__(parent)
        if not isinstance(groups, list) or (groups and isinstance(groups[0], Recording)):
            # A plain list of recordings: one report.
            groups = [ReportGroup("", list(groups), default_folder, info)]
        self.groups: list[ReportGroup] = groups
        self.qsettings = qsettings
        self.mode = mode
        self.index = 0
        self.branding = load_branding(qsettings)
        first = self.groups[0].info or remembered_info(qsettings, self.groups[0].recs)
        self.saved: list[str] = []

        # --- navigation (several reports)
        self.prev_button = QPushButton("◀  Previous")
        self.prev_button.clicked.connect(lambda: self.go(self.index - 1))
        self.next_button = QPushButton("Next  ▶")
        self.next_button.clicked.connect(lambda: self.go(self.index + 1))
        self.nav_label = QLabel()
        font = self.nav_label.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.nav_label.setFont(font)
        nav = QHBoxLayout()
        nav.addWidget(self.prev_button)
        nav.addWidget(self.nav_label, 1, Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self.next_button)
        self.nav = QWidget()
        self.nav.setLayout(nav)
        self.nav.setVisible(len(self.groups) > 1)

        # --- header fields (per report)
        self.edits: dict[str, QLineEdit] = {}
        grid = QGridLayout()
        for i, (key, label) in enumerate(report.HEADER_FIELDS):
            edit = QLineEdit()
            edit.textChanged.connect(self._schedule)
            self.edits[key] = edit
            row, col = divmod(i, 2)
            grid.addWidget(QLabel(label + ":"), row, col * 2)
            grid.addWidget(edit, row, col * 2 + 1)
        self.comments = QPlainTextEdit()
        self.comments.setPlaceholderText("Comments for post (track layout, issues, anything the editor should know)")
        self.comments.setFixedHeight(70)
        self.comments.textChanged.connect(self._schedule)
        hint = QLabel("Your name, phone, email and tone level are remembered for every report; director, client "
                      "and producer are remembered per project.")
        hint.setWordWrap(True)
        hint.setEnabled(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(grid)
        left_layout.addWidget(QLabel("Comments:"))
        left_layout.addWidget(self.comments)
        left_layout.addWidget(hint)
        self._start_orientation = first.orientation
        self._start_style = first.style
        left_layout.addWidget(self._build_columns(first.columns))
        left_layout.addStretch(1)

        self.preview = _Preview()
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.preview)
        split.setSizes([520, 700])

        self.want_pdf = QCheckBox("PDF")
        self.want_pdf.setChecked(True)
        self.want_csv = QCheckBox("CSV")
        self.want_csv.setChecked(True)
        branding = QPushButton("Branding…")
        branding.setToolTip("Your logo, title, header colour and footer for every report")
        branding.clicked.connect(self.edit_branding)
        box = QDialogButtonBox()
        many = len(self.groups) > 1
        if mode == "export":
            ok = box.addButton("Use for the NAS Copy", QDialogButtonBox.ButtonRole.AcceptRole)
            ok.setToolTip("The reports are saved into the project folders when the files are copied to the NAS")
            save_now = box.addButton("Save a Copy Now…", QDialogButtonBox.ButtonRole.ActionRole)
            save_now.clicked.connect(self._save_current)
        else:
            save = box.addButton("Save This Report…" if many else "Save…", QDialogButtonBox.ButtonRole.ActionRole
                                 if many else QDialogButtonBox.ButtonRole.AcceptRole)
            if many:
                save.clicked.connect(self._save_current_and_next)
                save_all = box.addButton(f"Save All {len(self.groups)}…", QDialogButtonBox.ButtonRole.ActionRole)
                save_all.setToolTip("Save every report into its own project folder")
                save_all.clicked.connect(self.save_all)
        box.addButton(QDialogButtonBox.StandardButton.Close if (many and mode == "save")
                      else QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(branding)
        bottom.addSpacing(16)
        bottom.addWidget(QLabel("Save as:"))
        bottom.addWidget(self.want_pdf)
        bottom.addWidget(self.want_csv)
        bottom.addStretch(1)
        bottom.addWidget(box)

        layout = QVBoxLayout(self)
        layout.addWidget(self.nav)
        layout.addWidget(split, 1)
        layout.addLayout(bottom)
        self.resize(1300, 800)

        self._timer = QTimer(self, singleShot=True, interval=250)
        self._timer.timeout.connect(self._update_preview)
        self._load(0)

    # ------------------------------------------------------------ groups

    @property
    def recs(self) -> list[Recording]:
        return self.groups[self.index].recs

    def _load(self, index: int):
        self.index = index
        group = self.groups[index]
        info = group.info or remembered_info(self.qsettings, group.recs, group.project)
        self.detected = report.detected_fields(group.recs)
        if group.project and " / " in self.detected.get("project", ""):
            self.detected["project"] = group.project
        for key, edit in self.edits.items():
            edit.blockSignals(True)
            edit.setText(info.get(key))
            detected = self.detected.get(key, "")
            edit.setPlaceholderText(detected)
            edit.setToolTip(f"Read from the files: {detected}" if detected else "")
            edit.blockSignals(False)
        self.comments.blockSignals(True)
        self.comments.setPlainText(info.comments)
        self.comments.blockSignals(False)
        title = group.title or info.get("project") or "Sound report"
        done = "   ✓ saved" if group.saved else ""
        self.nav_label.setText(f"Report {index + 1} of {len(self.groups)}:  {title}  ·  {len(group.recs)} files{done}")
        self.setWindowTitle(f"Sound Report — {title} ({len(group.recs)} files)")
        self.prev_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(self.groups) - 1)
        self._update_preview()

    def _store(self):
        self.groups[self.index].info = self.info()

    def go(self, index: int):
        if 0 <= index < len(self.groups) and index != self.index:
            self._store()
            self._load(index)

    def infos(self) -> list[report.ReportInfo]:
        """Every report's details, with the shared layout applied."""
        self._store()
        layout = self.info()
        result = []
        for group in self.groups:
            info = group.info or remembered_info(self.qsettings, group.recs, group.project)
            info.columns, info.orientation, info.style = list(layout.columns), layout.orientation, layout.style
            info.branding = self.branding
            result.append(info)
        return result

    def edit_branding(self):
        dialog = BrandingDialog(self.qsettings, self.recs, self)
        if dialog.exec():
            self.branding = load_branding(self.qsettings)
            self._update_preview()

    def _build_columns(self, chosen: list[str]) -> QGroupBox:
        box = QGroupBox("Table columns (tick to show, drag to reorder) and page")
        self.columns = QListWidget()
        self.columns.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.columns.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.columns.setFlow(QListWidget.Flow.TopToBottom)
        self.columns.setMinimumHeight(170)
        self._fill_columns(chosen)
        self.columns.itemChanged.connect(self._schedule)
        self.columns.model().rowsMoved.connect(self._schedule)
        self.orientation = QComboBox()
        self.orientation.addItem("Landscape", "landscape")
        self.orientation.addItem("Portrait", "portrait")
        self.orientation.setCurrentIndex(max(self.orientation.findData(self._start_orientation), 0))
        self.orientation.currentIndexChanged.connect(self._schedule)
        self.style = QComboBox()
        for key, label in report.STYLES.items():
            self.style.addItem(label, key)
        self.style.setCurrentIndex(max(self.style.findData(self._start_style), 0))
        self.style.currentIndexChanged.connect(self._schedule)
        reset = QPushButton("Default Columns")
        reset.clicked.connect(lambda: (self._fill_columns(list(report.DEFAULT_COLUMNS)), self._schedule()))
        self.fit_label = QLabel()
        self.fit_label.setWordWrap(True)
        self.fit_label.hide()
        layout = QVBoxLayout(box)
        layout.addWidget(self.columns)
        layout.addWidget(self.fit_label)
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch(1)
        row.addWidget(QLabel("Style:"))
        row.addWidget(self.style)
        row.addWidget(QLabel("Page:"))
        row.addWidget(self.orientation)
        layout.addLayout(row)
        return box

    def _fill_columns(self, chosen: list[str]):
        self.columns.blockSignals(True)
        self.columns.clear()
        order = [k for k in chosen if k in report.COLUMN_DEFS] + [k for k in report.COLUMN_DEFS if k not in chosen]
        for key in order:
            header, description = report.COLUMN_DEFS[key]
            plain = header.casefold() in description.casefold() or key == "track_columns"
            item = QListWidgetItem(description if plain else f"{description}  ({header})")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            item.setCheckState(Qt.CheckState.Checked if key in chosen else Qt.CheckState.Unchecked)
            self.columns.addItem(item)
        self.columns.blockSignals(False)

    def chosen_columns(self) -> list[str]:
        keys = []
        for row in range(self.columns.count()):
            item = self.columns.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                keys.append(item.data(Qt.ItemDataRole.UserRole))
        return keys or ["file"]

    def info(self) -> report.ReportInfo:
        fields = {}
        for key, edit in self.edits.items():
            fields[key] = edit.text().strip() or self.detected.get(key, "")
        return report.ReportInfo(fields, self.comments.toPlainText(), self.chosen_columns(),
                                 self.orientation.currentData(), self.style.currentData(), self.branding)

    def formats(self) -> tuple[bool, bool]:
        return self.want_pdf.isChecked(), self.want_csv.isChecked()

    def _schedule(self):
        self._timer.start()

    def _update_preview(self):
        scroll = self.preview.verticalScrollBar().value()
        info = self.info()
        # Lay the preview out at the paper's width (then it wraps like the PDF).
        dpi = self.logicalDpiX()
        self.preview.setLineWrapMode(QTextBrowser.LineWrapMode.FixedPixelWidth)
        self.preview.setLineWrapColumnOrWidth(int(report.PAGE_WIDTH_MM[info.orientation] / 25.4 * dpi))
        self.preview.set_logo(info.branding.logo_path())
        self.preview.setHtml(report.build_html(info, self.recs))
        keys, headers, rows = report.table(self.recs, info.columns)
        _, points = report.column_layout(keys, headers, rows, info.orientation)
        if points < report.TABLE_POINTS:
            self.fit_label.setText(f"⚠ These columns don't fit on a {info.orientation} page at normal size; the table "
                                   f"text is reduced to {points:g} pt. Untick a column or switch to "
                                   f"{'Landscape' if info.orientation == 'portrait' else 'fewer columns'} for "
                                   "larger text.")
            self.fit_label.show()
        else:
            self.fit_label.hide()
        self.preview.verticalScrollBar().setValue(scroll)

    def _write(self, stem: str, info: report.ReportInfo, recs: list[Recording]) -> list[str]:
        pdf, csv_ = self.formats()
        written = []
        if pdf:
            report.write_pdf(stem + ".pdf", info, recs)
            written.append(stem + ".pdf")
        if csv_:
            report.write_csv(stem + ".csv", info, recs)
            written.append(stem + ".csv")
        return written

    def _save_current(self) -> bool:
        info = self.info()
        pdf, csv_ = self.formats()
        if not (pdf or csv_):
            QMessageBox.information(self, "Sound Report", "Tick PDF and/or CSV.")
            return False
        group = self.groups[self.index]
        folder = group.default_folder or os.path.expanduser("~")
        base = os.path.join(folder, report.default_basename(info, self.recs))
        path, _ = QFileDialog.getSaveFileName(self, "Save sound report", base + (".pdf" if pdf else ".csv"),
                                              "PDF (*.pdf);;CSV (*.csv)")
        if not path:
            return False
        try:
            written = self._write(os.path.splitext(path)[0], info, self.recs)
        except OSError as error:
            QMessageBox.warning(self, "Sound Report", f"Could not save the report:\n{error}")
            return False
        remember(self.qsettings, info)
        group.saved = written
        self.saved += written
        return True

    def _save_current_and_next(self):
        if self._save_current():
            self._store()
            unsaved = [i for i, g in enumerate(self.groups) if not g.saved]
            later = [i for i in unsaved if i > self.index] or unsaved
            self._load(later[0] if later else self.index)

    def save_all(self):
        pdf, csv_ = self.formats()
        if not (pdf or csv_):
            QMessageBox.information(self, "Sound Report", "Tick PDF and/or CSV.")
            return
        infos = self.infos()
        plan = []
        for group, info in zip(self.groups, infos):
            folder = group.default_folder or os.path.expanduser("~")
            plan.append((group, info, _unique_stem(os.path.join(folder, report.default_basename(info, group.recs)))))
        listing = "\n".join(f"• {stem}{'.pdf' if pdf else '.csv'}" for _, _, stem in plan)
        if QMessageBox.question(self, "Save all reports", f"Save {len(plan)} reports?\n\n{listing}") \
                != QMessageBox.StandardButton.Yes:
            return
        errors = []
        for group, info, stem in plan:
            try:
                group.saved = self._write(stem, info, group.recs)
                self.saved += group.saved
                remember(self.qsettings, info)
            except OSError as error:
                errors.append(f"{group.title}: {error}")
        if errors:
            QMessageBox.warning(self, "Sound Report", "Some reports could not be saved:\n" + "\n".join(errors))
        self._load(self.index)
        if not errors:
            super().accept()

    def accept(self):
        if self.mode == "save":
            if not self._save_current():
                return
        else:
            for info in self.infos():
                remember(self.qsettings, info)
        super().accept()


def _unique_stem(stem: str) -> str:
    candidate, n = stem, 2
    while os.path.exists(candidate + ".pdf") or os.path.exists(candidate + ".csv"):
        candidate = f"{stem} ({n})"
        n += 1
    return candidate
