"""Report branding: the editor (logo, title, company line, header colour,
footer, where the contact details go), the Report Branding window, and the
first-run Setup window (your details, branding, folders) with a live preview."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QSlider, QTextBrowser, QVBoxLayout, QWidget,
)

from . import compat, report, settings
from .catalog import Recording


def load_branding(qsettings) -> report.Branding:
    return report.Branding.from_dict(settings.get_json(qsettings, "report_branding"))


def save_branding(qsettings, branding: report.Branding) -> None:
    settings.put_json(qsettings, "report_branding", dict(branding.__dict__))


def keep_copy(path: str) -> str:
    """Copy a chosen logo into the app's data folder, so the reports keep
    working if the original file is moved or deleted."""
    data = Path(path).read_bytes()
    folder = settings.branding_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"logo-{hashlib.sha1(data).hexdigest()[:12]}{Path(path).suffix.lower() or '.png'}"
    if not target.exists():
        shutil.copyfile(path, target)
    return str(target)


def sample_recordings() -> list[Recording]:
    """A few made-up takes for the preview when no files are at hand."""
    recs = []
    for i, (scene, take, note) in enumerate([("1A", "01", ""), ("1A", "02", "Plane at the end"),
                                             ("1B", "01", ""), ("2", "01", "Great take")]):
        recs.append(Recording(path=f"/sample/{scene}T{take}_ISO.wav", size=0, mtime=0, sample_rate=48000, bits=24,
                              channels=3, frames=48000 * (60 + 17 * i), project="Sample Project", scene=scene,
                              take=take, note=note, circled=i == 3, tc_rate="24000/1001",
                              time_reference=48000 * (36000 + 400 * i), date="2026-09-25",
                              tracks=["Boom", "Lav-1", "Lav-2"]))
    return recs


class ReportPreview(QTextBrowser):
    """A report rendered like paper (black on white, at the page's width)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("QTextBrowser { background: white; color: black; }")
        self.setMinimumWidth(520)

    def show_report(self, info: report.ReportInfo, recs: list[Recording]):
        self.setLineWrapMode(QTextBrowser.LineWrapMode.FixedPixelWidth)
        self.setLineWrapColumnOrWidth(int(report.PAGE_WIDTH_MM[info.orientation] / 25.4 * self.logicalDpiX()))
        if info.branding.logo_path():
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl("logo"),
                                        QImage(info.branding.logo_path()))
        scroll = self.verticalScrollBar().value()
        self.setHtml(report.build_html(info, recs))
        self.verticalScrollBar().setValue(scroll)


class BrandingEditor(QWidget):
    """The branding fields; emits changed() on every edit."""

    changed = Signal()

    def __init__(self, start: report.Branding, parent=None):
        super().__init__(parent)
        self.logo = start.logo
        self.accent = start.accent

        self.logo_view = QLabel()
        self.logo_view.setFixedSize(180, 120)
        self.logo_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo_view.setStyleSheet("background: white; border: 1px solid #888888; color: #777777;")
        choose = QPushButton("Choose Image…")
        choose.clicked.connect(self._choose_logo)
        builtin = QPushButton("Built-in Logo")
        builtin.clicked.connect(lambda: self._set_logo(report.BUILTIN_LOGO))
        none = QPushButton("No Logo")
        none.clicked.connect(lambda: self._set_logo(""))
        logo_buttons = QVBoxLayout()
        for button in (choose, builtin, none):
            logo_buttons.addWidget(button)
        logo_buttons.addStretch(1)
        logo_row = QHBoxLayout()
        logo_row.addWidget(self.logo_view, 0, Qt.AlignmentFlag.AlignTop)
        logo_row.addLayout(logo_buttons)
        logo_row.addStretch(1)

        self.size = QSlider(Qt.Orientation.Horizontal)
        self.size.setRange(40, 160)
        self.size.setValue(start.logo_height)
        self.size.valueChanged.connect(self.changed)
        self.title = QLineEdit(start.title)
        self.title.setPlaceholderText("Sound Report")
        self.company = QLineEdit(start.company)
        self.company.setPlaceholderText("e.g. your company or website (a line under the title)")
        self.footer = QLineEdit(start.footer)
        self.footer.setPlaceholderText("e.g. a tagline or website, printed at the bottom of every page")
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._choose_color)
        default_color = QPushButton("Reset")
        default_color.setToolTip("Back to the default header colour")
        default_color.clicked.connect(lambda: self._set_color(""))
        color_row = QHBoxLayout()
        color_row.addWidget(self.color_button)
        color_row.addWidget(default_color)
        color_row.addStretch(1)
        self.contact_header = QCheckBox("At the top of the report (with the other details)")
        self.contact_header.setChecked(start.contact_in_header)
        self.contact_footer = QCheckBox("In the footer of every page")
        self.contact_footer.setChecked(start.contact_in_footer)
        contact = QVBoxLayout()
        contact.addWidget(self.contact_header)
        contact.addWidget(self.contact_footer)
        for edit in (self.title, self.company, self.footer):
            edit.textChanged.connect(self.changed)
        for box in (self.contact_header, self.contact_footer):
            box.toggled.connect(self.changed)

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Logo:", logo_row)
        form.addRow("Logo size:", self.size)
        form.addRow("Title:", self.title)
        form.addRow("Line under title:", self.company)
        form.addRow("Table header colour:", color_row)
        form.addRow("Page footer:", self.footer)
        form.addRow("Phone & email:", contact)
        self._set_color(self.accent)
        self._set_logo(self.logo)

    def branding(self) -> report.Branding:
        return report.Branding(logo=self.logo, logo_height=self.size.value(), title=self.title.text().strip(),
                               company=self.company.text().strip(), accent=self.accent,
                               footer=self.footer.text().strip(), contact_in_header=self.contact_header.isChecked(),
                               contact_in_footer=self.contact_footer.isChecked())

    def committed(self) -> report.Branding | None:
        """The branding with a chosen logo copied into the app's data folder
        (None, after telling the user, if that failed)."""
        brand = self.branding()
        if brand.logo not in ("", report.BUILTIN_LOGO):
            try:
                brand.logo = keep_copy(brand.logo)
            except OSError as error:
                QMessageBox.warning(self, "Report Branding", f"Could not keep a copy of the logo:\n{error}")
                return None
        return brand

    def _choose_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a logo", str(Path.home() / "Pictures"),
                                              "Images (*.png *.jpg *.jpeg *.svg *.webp *.bmp)")
        if not path:
            return
        if QImage(path).isNull():
            QMessageBox.warning(self, "Report Branding", "That file could not be read as an image.")
            return
        self._set_logo(path)

    def _set_logo(self, logo: str):
        self.logo = logo
        path = report.Branding(logo=logo).logo_path()
        if path:
            pixmap = QPixmap(path).scaled(self.logo_view.size() * 0.9, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
            self.logo_view.setPixmap(pixmap)
        else:
            self.logo_view.setPixmap(QPixmap())
            self.logo_view.setText("No logo")
        self.size.setEnabled(bool(path))
        self.changed.emit()

    def _choose_color(self):
        color = QColorDialog.getColor(QColor(self.accent or "#e4e4e4"), self, "Table header colour")
        if color.isValid():
            self._set_color(color.name())

    def _set_color(self, color: str):
        self.accent = color
        swatch = color or "#e4e4e4"
        self.color_button.setText(f"  {color or 'Grey (default)'}  ")
        self.color_button.setToolTip("Choose the table header colour")
        self.color_button.setStyleSheet(f"background: {swatch}; color: {report._text_on(swatch)};")
        self.changed.emit()


def _footer_note(info: report.ReportInfo) -> str:
    text = report.footer_text(info)
    return (f"Footer on every page: “{text}”   ·   Page 1 of 1" if text
            else "Every page ends with “Page x of y”.")


class BrandingDialog(QDialog):
    def __init__(self, qsettings, recs: list[Recording] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Report Branding")
        self.qsettings = qsettings
        self.recs = (recs or sample_recordings())[:8]
        self.editor = BrandingEditor(load_branding(qsettings))
        self.editor.setMinimumWidth(460)
        self.editor.changed.connect(lambda: self._timer.start())
        self.preview = ReportPreview()
        self.footer_note = QLabel()
        self.footer_note.setEnabled(False)
        right = QVBoxLayout()
        right.addWidget(QLabel("Preview"))
        right.addWidget(self.preview, 1)
        right.addWidget(self.footer_note)
        body = QHBoxLayout()
        body.addWidget(self.editor, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(right, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(box)
        self.resize(1150, 640)
        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._update)
        self._update()

    def _update(self):
        personal = settings.get_json(self.qsettings, "report_personal")
        fields = {**report.detected_fields(self.recs), "mixer": personal.get("mixer") or "Your Name",
                  "phone": personal.get("phone", ""), "email": personal.get("email", "")}
        info = report.ReportInfo(fields, branding=self.editor.branding())
        self.preview.show_report(info, self.recs)
        self.footer_note.setText(_footer_note(info))

    def accept(self):
        brand = self.editor.committed()
        if brand is None:
            return
        save_branding(self.qsettings, brand)
        super().accept()


class SetupDialog(QDialog):
    """First-run setup: your details for sound reports, branding, and folders."""

    def __init__(self, qsettings, parent=None, *, first_run: bool = True):
        super().__init__(parent)
        self.qsettings = qsettings
        self.setWindowTitle("Welcome to Location Sound File Manager" if first_run else "Setup")
        self.recs = sample_recordings()
        personal = settings.get_json(qsettings, "report_personal")

        intro = QLabel("<b>Let's set things up.</b> Everything here can be changed later under "
                       "<i>Settings ▸ Setup…</i>. The preview on the right shows how your sound reports will look.")
        intro.setWordWrap(True)

        # Your details
        details = QGroupBox("Your details (for sound reports)")
        self.mixer = QLineEdit(personal.get("mixer", ""))
        self.mixer.setPlaceholderText("Your name")
        self.phone = QLineEdit(personal.get("phone", ""))
        self.phone.setPlaceholderText("Optional")
        self.email = QLineEdit(personal.get("email", ""))
        self.email.setPlaceholderText("Optional")
        form = QFormLayout(details)
        form.addRow("Sound mixer:", self.mixer)
        form.addRow("Phone:", self.phone)
        form.addRow("Email:", self.email)
        for edit in (self.mixer, self.phone, self.email):
            edit.textChanged.connect(self._schedule)

        # Branding
        branding = QGroupBox("Branding")
        self.editor = BrandingEditor(load_branding(qsettings))
        self.editor.changed.connect(self._schedule)
        QVBoxLayout(branding).addWidget(self.editor)

        # Folders
        folders = QGroupBox("Folders")
        recent = [d for d in settings.get(qsettings, "offload_destinations") if d]
        library = settings.get(qsettings, "library_folder")
        self.output = QLineEdit(recent[0] if recent else library)
        self.output.setPlaceholderText("Where cards are copied to, e.g. the Sound Backups folder on your NAS")
        output_browse = QPushButton("Browse…")
        output_browse.clicked.connect(lambda: self._browse(self.output, "Default output folder"))
        self.separate_library = QCheckBox("Browse a different folder in the Library")
        self.library = QLineEdit(library)
        self.library.setPlaceholderText("The folder of recordings the Library page shows")
        library_browse = QPushButton("Browse…")
        library_browse.clicked.connect(lambda: self._browse(self.library, "Library folder"))
        self.separate_library.setChecked(bool(library) and bool(recent) and
                                         compat.normpath(library) != compat.normpath(recent[0]))
        self.separate_library.toggled.connect(self._update_folders)
        self._library_widgets = (self.library, library_browse)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output, 1)
        output_row.addWidget(output_browse)
        library_row = QHBoxLayout()
        library_row.addWidget(self.library, 1)
        library_row.addWidget(library_browse)
        folder_form = QFormLayout(folders)
        folder_form.addRow("Copy cards to:", output_row)
        hint = QLabel("The default for “Copy to NAS”. The Library shows this folder too, unless you choose "
                      "another one below.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        folder_form.addRow("", hint)
        folder_form.addRow("", self.separate_library)
        folder_form.addRow("Library folder:", library_row)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(intro)
        left_layout.addWidget(details)
        left_layout.addWidget(branding)
        left_layout.addWidget(folders)
        left_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(560)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.preview = ReportPreview()
        self.footer_note = QLabel()
        self.footer_note.setEnabled(False)
        right = QVBoxLayout()
        right.addWidget(QLabel("Preview"))
        right.addWidget(self.preview, 1)
        right.addWidget(self.footer_note)
        body = QHBoxLayout()
        body.addWidget(scroll)
        body.addLayout(right, 1)

        box = QDialogButtonBox()
        box.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton("Skip for Now" if first_run else "Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addWidget(box)
        self.resize(1320, 860)

        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self._update)
        self._update_folders()
        self._update()

    def _schedule(self):
        self._timer.start()

    def _browse(self, edit: QLineEdit, title: str):
        folder = QFileDialog.getExistingDirectory(self, title, edit.text() or "/mnt")
        if folder:
            edit.setText(compat.normpath(folder))

    def _update_folders(self):
        for widget in self._library_widgets:
            widget.setEnabled(self.separate_library.isChecked())

    def _personal(self) -> dict:
        return {"mixer": self.mixer.text().strip(), "phone": self.phone.text().strip(),
                "email": self.email.text().strip()}

    def _update(self):
        fields = {**report.detected_fields(self.recs), **{k: v for k, v in self._personal().items() if v}}
        fields.setdefault("mixer", "Your Name")
        info = report.ReportInfo(fields, branding=self.editor.branding())
        self.preview.show_report(info, self.recs)
        self.footer_note.setText(_footer_note(info))

    def output_folder(self) -> str:
        return compat.normpath(self.output.text().strip()) if self.output.text().strip() else ""

    def library_folder(self) -> str:
        if self.separate_library.isChecked() and self.library.text().strip():
            return compat.normpath(self.library.text().strip())
        return self.output_folder()

    def accept(self):
        for label, folder in (("output", self.output_folder()), ("library", self.library_folder())):
            if folder and not os.path.isdir(folder):
                QMessageBox.warning(self, "Setup", f"The {label} folder does not exist:\n{folder}\n\n"
                                    "Is the network share mounted?")
                return
        brand = self.editor.committed()
        if brand is None:
            return
        save_branding(self.qsettings, brand)
        existing = settings.get_json(self.qsettings, "report_personal")
        settings.put_json(self.qsettings, "report_personal", {**existing, **self._personal()})
        output, library = self.output_folder(), self.library_folder()
        if library:
            settings.put(self.qsettings, "library_folder", library)
        if output:
            recent = [d for d in settings.get(self.qsettings, "offload_destinations") if d and d != output]
            # "" means "the library folder" in the destination list.
            first = "" if output == library else output
            settings.put(self.qsettings, "offload_destinations", [first] + recent[:7])
        settings.put(self.qsettings, "setup_done", True)
        super().accept()
