"""The Effects window: an effects rack in the style of Easy Effects. The chain
(add, remove, reorder, switch off), each effect's settings with a frequency
graph for the filters, presets (built-in, saved, imported from Easy Effects),
the tracks, where the copies go, and an A/B preview that follows every change.
It only collects choices; the main window writes the files
(processing.process_file with an effects.Engine)."""

from __future__ import annotations

import copy
import json
import os

import numpy as np
from PySide6.QtCore import QPointF, QSettings, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QRadioButton,
                               QScrollArea, QSizePolicy, QSplitter, QTableWidget, QVBoxLayout, QWidget)

from . import effects, loudness, processing, settings
from .catalog import Recording
from .processing_dialog import PREVIEW_SECONDS, PreviewPlayer, _mixdown, processable, track_names
from .timecode import seconds_to_clock as format_seconds

DEFAULT_PRESET = "Dialogue: rumble filter"


def easyeffects_folders() -> list[str]:
    """Where Easy Effects keeps its presets (newer versions, older, Flatpak)."""
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".local/share/easyeffects/output"), os.path.join(home, ".config/easyeffects/output"),
                  os.path.join(home, ".var/app/com.github.wwmm.easyeffects/data/easyeffects/output"),
                  os.path.join(home, ".var/app/com.github.wwmm.easyeffects/config/easyeffects/output")]
    return [c for c in candidates if os.path.isdir(c)]


class ResponseGraph(QWidget):
    """The gain of a filter or equalizer effect over 20 Hz – 20 kHz."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.effect: effects.Effect | None = None
        self.rate = 48000
        self.freqs = np.geomspace(20, 20000, 400)

    def show_effect(self, effect: effects.Effect | None, rate: int):
        self.effect, self.rate = effect, rate
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        rect = self.rect().adjusted(34, 6, -6, -16)
        painter.fillRect(rect, pal.base())
        if self.effect is None:
            return
        db = effects.response_db(self.effect, self.rate, self.freqs)
        top = max(12.0, min(60.0, float(np.ceil((np.abs(db[np.isfinite(db)]).max() + 3) / 6) * 6)))
        top = min(top, 24.0) if (db > -top).all() else top

        def x_of(f):
            return rect.left() + rect.width() * np.log10(f / 20) / 3

        def y_of(v):
            return rect.top() + rect.height() * (top - np.clip(v, -top, top)) / (2 * top)

        grid = QPen(pal.mid().color())
        grid.setWidth(0)
        painter.setPen(grid)
        small = painter.font()
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.8))
        painter.setFont(small)
        for f, label in ((100, "100"), (1000, "1k"), (10000, "10k")):
            x = x_of(f)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawText(QPointF(x - 8, rect.bottom() + 13), label)
        for v in (-top, -top / 2, 0, top / 2, top):
            y = y_of(v)
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            painter.drawText(QPointF(2, y + 4), f"{v:+.0f}" if v else "0")
        path = QPainterPath()
        for i, (f, v) in enumerate(zip(self.freqs, db)):
            point = QPointF(x_of(f), y_of(v))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        pen = QPen(pal.highlight().color())
        pen.setWidthF(2.0)
        painter.setPen(pen)
        painter.drawPath(path)


class EffectsDialog(QDialog):
    """Choices for making copies of the selected recordings through an effects chain."""

    def __init__(self, recs: list[Recording], qsettings: QSettings, current: Recording | None = None,
                 playhead: float = 0.0, parent=None, data_dir: str | None = None,
                 chain: list[effects.Effect] | None = None):
        super().__init__(parent)
        self.data_dir = data_dir or str(settings.history_path().parent)
        current = next((r for r in recs if current is not None and r.path == current.path), None)
        self.recs, self.qsettings, self.current, self.playhead = recs, qsettings, current, playhead
        self.preview_player = PreviewPlayer(self)
        saved = settings.get(qsettings, "effects_chain")
        if chain is not None:  # e.g. a platform's delivery preset from Loudness Check
            self.chain = chain
        else:
            self.chain = effects.chain_from_json(saved) if saved else effects.load_preset(self.data_dir, DEFAULT_PRESET)
        self._editors: list[QWidget] = []
        self._loading = False
        self.setWindowTitle("Effects")
        many = len(recs) > 1
        intro = QLabel(f"Runs {'each selected file' if many else 'the file'} through the effects below, top to "
                       "bottom, and makes a <b>copy</b>; the original is never changed. The copy keeps the metadata "
                       "(scene, take, timecode, notes) and gets a name ending in the suffix below.")
        intro.setWordWrap(True)

        # Presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        self.presets = QComboBox()
        self.presets.setMinimumWidth(240)
        preset_row.addWidget(self.presets, 1)
        self.load_button = QPushButton("Load")
        self.load_button.clicked.connect(self._load_preset)
        self.save_button = QPushButton("Save As…")
        self.save_button.clicked.connect(self._save_preset)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete_preset)
        self.import_button = QPushButton("Import Easy Effects…")
        self.import_button.setToolTip("Turn an Easy Effects preset (.json) into a chain here. Effects that don't "
                                      "exist here are left out; you're told which.")
        self.import_button.clicked.connect(self._import_easyeffects)
        for button in (self.load_button, self.save_button, self.delete_button, self.import_button):
            preset_row.addWidget(button)
        self.presets.currentIndexChanged.connect(self._update_preset_buttons)

        # The rack: chain on the left, the selected effect's settings on the right
        rack = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setToolTip("Top to bottom is the order the sound goes through. Untick to switch an effect off.")
        self.list.currentRowChanged.connect(self._show_selected)
        self.list.itemChanged.connect(self._item_ticked)
        left_layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add ▾")
        menu = QMenu(self.add_button)
        for kind, kind_type in effects.TYPES.items():
            action = menu.addAction(kind_type.label)
            action.setToolTip(kind_type.about)
            action.triggered.connect(lambda _=False, k=kind: self._add(k))
        self.add_button.setMenu(menu)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.up_button = QPushButton("▲")
        self.up_button.setToolTip("Move up (earlier in the chain)")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("▼")
        self.down_button.setToolTip("Move down (later in the chain)")
        self.down_button.clicked.connect(lambda: self._move(1))
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            buttons.addWidget(button)
        left_layout.addLayout(buttons)
        rack.addWidget(left)
        self.settings_area = QScrollArea()
        self.settings_area.setWidgetResizable(True)
        self.settings_area.setMinimumWidth(380)
        rack.addWidget(self.settings_area)
        rack.setStretchFactor(0, 2)
        rack.setStretchFactor(1, 3)
        rack.setMinimumHeight(330)

        # Tracks (by name, across all files)
        tracks_box = QGroupBox("Tracks to process (the others are copied unchanged)")
        self.tracks = QListWidget()
        names: list[str] = []
        for rec in recs:
            for name in track_names(rec):
                if name not in names:
                    names.append(name)
        for name in names:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.tracks.addItem(item)
        self.tracks.setMaximumHeight(min(92, 22 * max(2, len(names)) + 8))
        QVBoxLayout(tracks_box).addWidget(self.tracks)

        # Output
        out_box = QGroupBox("Where the copies go")
        self.in_folder = QRadioButton(f"In a “{processing.PROCESSED_FOLDER}” folder beside each file")
        self.beside = QRadioButton("Next to each file")
        (self.beside if settings.get(qsettings, "effects_folder") == "beside" else self.in_folder).setChecked(True)
        self.suffix = QLineEdit(settings.get(qsettings, "effects_suffix") or effects.DEFAULT_SUFFIX)
        self.suffix.setMaximumWidth(120)
        out_row = QHBoxLayout()
        out_row.addWidget(self.in_folder)
        out_row.addWidget(self.beside)
        out_row.addStretch(1)
        suffix_row = QHBoxLayout()
        suffix_row.addWidget(QLabel("Name suffix:"))
        suffix_row.addWidget(self.suffix)
        self.example = QLabel()
        suffix_row.addWidget(self.example, 1)
        out_layout = QVBoxLayout(out_box)
        out_layout.addLayout(out_row)
        out_layout.addLayout(suffix_row)

        # Preview
        preview_box = QGroupBox("Preview")
        self.preview_button = QPushButton("▶ Preview")
        self.ab_original = QRadioButton("Original")
        self.ab_processed = QRadioButton("With effects")
        self.ab_processed.setChecked(True)
        for button in (self.ab_original, self.ab_processed):
            button.toggled.connect(self._ab_changed)
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        self.preview_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        preview_row = QHBoxLayout()
        preview_row.addWidget(self.preview_button)
        preview_row.addWidget(self.ab_original)
        preview_row.addWidget(self.ab_processed)
        preview_row.addStretch(1)
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addLayout(preview_row)
        preview_layout.addWidget(self.preview_status)
        self.preview_button.clicked.connect(self._toggle_preview)
        self._rerender = QTimer(self)
        self._rerender.setSingleShot(True)
        self._rerender.setInterval(300)
        self._rerender.timeout.connect(self._settings_changed)
        preview_file = self._preview_rec()
        if preview_file is None:
            preview_box.setEnabled(False)
            self.preview_status.setText("None of the files can be previewed.")
        else:
            start = self._preview_start(preview_file) / preview_file.sample_rate
            self.preview_status.setText(f"Plays {PREVIEW_SECONDS} s of {preview_file.name} from "
                                        f"{format_seconds(start)} (the playhead), looping. Changes are heard as you "
                                        "make them; switch between Original and With effects while it plays.")

        self.problem = QLabel()
        self.problem.setStyleSheet("color: #c0392b")
        self.problem.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.addButton("Make Copies", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(preset_row)
        layout.addWidget(rack, 1)
        layout.addWidget(tracks_box)
        layout.addWidget(out_box)
        layout.addWidget(preview_box)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        for signal in (self.in_folder.toggled, self.suffix.textChanged, self.tracks.itemChanged):
            signal.connect(self._update)
        self.tracks.itemChanged.connect(self._changed)
        self._fill_presets()
        self._fill_list(0)
        self._update()
        self.resize(940, 920)

    # ------------------------------------------------------------ the chain

    def _fill_list(self, select: int | None = None):
        self._loading = True
        row = self.list.currentRow() if select is None else select
        self.list.clear()
        for effect in self.chain:
            item = QListWidgetItem(self._item_text(effect))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if effect.bypass else Qt.CheckState.Checked)
            item.setToolTip(effects.TYPES[effect.kind].about)
            self.list.addItem(item)
        self._loading = False
        if self.chain:
            self.list.setCurrentRow(min(max(row, 0), len(self.chain) - 1))
        self._show_selected(self.list.currentRow())

    @staticmethod
    def _item_text(effect: effects.Effect) -> str:
        detail = effect.describe()
        if detail.startswith(effect.label):
            detail = detail[len(effect.label):].strip(" ,")
        elif effect.kind == "equalizer":
            detail = detail.removeprefix("EQ, ")
        return f"{effect.label}\n    {detail}"

    def _refresh_item(self, row: int):
        item = self.list.item(row)
        if item is not None:
            self._loading = True
            item.setText(self._item_text(self.chain[row]))
            self._loading = False

    def _item_ticked(self, item: QListWidgetItem):
        if self._loading:
            return
        row = self.list.row(item)
        self.chain[row].bypass = item.checkState() != Qt.CheckState.Checked
        self._changed()

    def _add(self, kind: str):
        row = self.list.currentRow()
        at = row + 1 if row >= 0 else len(self.chain)
        if kind == "loudness":  # loudness belongs near the end, before a limiter
            limiter = next((i for i, e in enumerate(self.chain) if e.kind == "limiter"), None)
            at = limiter if limiter is not None else len(self.chain)
        self.chain.insert(at, effects.new_effect(kind))
        self._fill_list(at)
        self._changed()

    def _remove(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.chain):
            del self.chain[row]
            self._fill_list(row)
            self._changed()

    def _move(self, step: int):
        row = self.list.currentRow()
        to = row + step
        if 0 <= row < len(self.chain) and 0 <= to < len(self.chain):
            self.chain[row], self.chain[to] = self.chain[to], self.chain[row]
            self._fill_list(to)
            self._changed()

    # ------------------------------------------------------------ the selected effect's settings

    def _show_selected(self, row: int):
        self.remove_button.setEnabled(0 <= row < len(self.chain))
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < len(self.chain) - 1)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.graph = None
        if not 0 <= row < len(self.chain):
            hint = QLabel("No effects yet. Add ▾ one, or load a preset.")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            layout.addStretch(1)
            self.settings_area.setWidget(panel)
            return
        effect = self.chain[row]
        kind_type = effects.TYPES[effect.kind]
        title = QLabel(f"<b>{kind_type.label}</b>")
        about = QLabel(kind_type.about)
        about.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(about)
        form = QFormLayout()
        for spec in kind_type.specs:
            if spec.kind == "bands":
                layout.addLayout(form)
                layout.addWidget(self._bands_editor(row, effect))
                form = QFormLayout()
                continue
            widget = self._editor(row, effect, spec)
            if spec.kind == "bool":
                form.addRow("", widget)
            else:
                form.addRow(spec.label, widget)
        layout.addLayout(form)
        if kind_type.linear:
            self.graph = ResponseGraph()
            self.graph.show_effect(effect, self._rate())
            layout.addWidget(self.graph)
        layout.addStretch(1)
        self.settings_area.setWidget(panel)

    def _editor(self, row: int, effect: effects.Effect, spec: effects.Spec) -> QWidget:
        value = effect.params[spec.key]
        if spec.kind == "choice":
            widget = QComboBox()
            widget.addItems(list(spec.choices))
            widget.setCurrentText(value)
            widget.currentTextChanged.connect(lambda v, k=spec.key: self._set(row, k, v))
        elif spec.kind == "bool":
            widget = QCheckBox(spec.label)
            widget.setChecked(bool(value))
            widget.toggled.connect(lambda v, k=spec.key: self._set(row, k, v))
        else:
            widget = QDoubleSpinBox()
            widget.setRange(spec.low, spec.high)
            widget.setDecimals(spec.decimals)
            widget.setSingleStep(spec.step)
            widget.setSuffix(spec.unit)
            widget.setValue(float(value))
            widget.setKeyboardTracking(False)
            widget.setMaximumWidth(140)
            if spec.log:  # frequencies: the arrows step by a size that suits the value (10 Hz near 100, 100 near 1k)
                widget.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
            widget.valueChanged.connect(lambda v, k=spec.key: self._set(row, k, v))
        if spec.tip:
            widget.setToolTip(spec.tip)
        return widget

    def _bands_editor(self, row: int, effect: effects.Effect) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Type", "Frequency", "Gain", "Q"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setMinimumSectionSize(96)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        bands = effect.params["bands"]

        def changed(i, key, value):
            bands[i][key] = value
            self._set(row, "bands", bands)

        for i, band in enumerate(bands):
            table.insertRow(i)
            kind = QComboBox()
            kind.addItems(list(effects.BAND_TYPES))
            kind.setCurrentText(band["type"])
            kind.currentTextChanged.connect(lambda v, i=i: changed(i, "type", v))
            table.setCellWidget(i, 0, kind)
            for column, key, low, high, decimals, unit in ((1, "freq", 10, 24000, 0, " Hz"), (2, "gain", -36, 36, 1, " dB"),
                                                           (3, "q", 0.05, 100, 2, "")):
                spin = QDoubleSpinBox()
                spin.setRange(low, high)
                spin.setDecimals(decimals)
                spin.setSuffix(unit)
                spin.setValue(float(band[key]))
                spin.setKeyboardTracking(False)
                if key == "freq":
                    spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
                spin.valueChanged.connect(lambda v, i=i, k=key: changed(i, k, v))
                table.setCellWidget(i, column, spin)
        # Tall enough for the bands (up to 6 without scrolling), so the graph below stays in view.
        table.setFixedHeight(table.horizontalHeader().sizeHint().height() + 32 * min(6, max(1, len(bands))) + 6)
        layout.addWidget(table)
        row_buttons = QHBoxLayout()
        add = QPushButton("Add Band")
        add.setEnabled(len(bands) < effects.EQ_BANDS)
        remove = QPushButton("Remove Band")

        def add_band():
            last = bands[-1]["freq"] if bands else 500.0
            bands.append({"type": "Bell", "freq": min(20000.0, last * 2), "gain": 0.0, "q": 1.0})
            self._set(row, "bands", bands)
            self._show_selected(row)

        def remove_band():
            picked = table.currentRow()
            if 0 <= picked < len(bands):
                del bands[picked]
                self._set(row, "bands", bands)
                self._show_selected(row)

        add.clicked.connect(add_band)
        remove.clicked.connect(remove_band)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)
        return box

    def _set(self, row: int, key: str, value):
        if not 0 <= row < len(self.chain):
            return
        effect = self.chain[row]
        params = dict(effect.params)
        params[key] = copy.deepcopy(value)
        rebuild = False
        if effect.kind == "loudness":
            platform = loudness.BY_NAME.get(params["platform"])
            if key == "platform":  # its numbers go into the boxes
                params, rebuild = effects.apply_platform(params), True
            elif key in ("target", "ceiling") and platform is not None and \
                    float(value) != (platform.integrated if key == "target" else platform.true_peak):
                params["platform"], rebuild = "Custom", True  # own numbers: no longer that platform's
        effect.params = effects.clamp_params(effect.kind, params)
        if key == "bands":  # the editor holds the list; keep it the same object as the effect's
            value[:] = effect.params["bands"]
            effect.params["bands"] = value
        self._refresh_item(row)
        if rebuild:  # later, so the editor that sent this isn't deleted while it's busy
            QTimer.singleShot(0, lambda: self._show_selected(row) if self.list.currentRow() == row else None)
        if self.graph is not None:
            self.graph.show_effect(effect, self._rate())
        self._changed()

    def _changed(self):
        self._update()
        if self.preview_player.playing():
            self._rerender.start()

    # ------------------------------------------------------------ presets

    def _fill_presets(self, select: str | None = None):
        self.presets.blockSignals(True)
        self.presets.clear()
        for name in effects.BUILTIN_PRESETS:
            self.presets.addItem(name, ("builtin", name))
        mine = effects.list_presets(self.data_dir)
        if mine:
            self.presets.insertSeparator(self.presets.count())
            for name in mine:
                self.presets.addItem(name, ("mine", name))
        if select is None:  # show the preset the chain came from, if it's one of them unchanged
            current = effects.chain_to_json(self.chain)
            select = next((name for name, chain in effects.BUILTIN_PRESETS.items()
                           if effects.chain_to_json(chain) == current), None)
        if select is not None:
            index = next((i for i in range(self.presets.count())
                          if (self.presets.itemData(i) or ("", ""))[1] == select), -1)
            if index >= 0:
                self.presets.setCurrentIndex(index)
        self.presets.blockSignals(False)
        self._update_preset_buttons()

    def _update_preset_buttons(self):
        data = self.presets.currentData()
        self.delete_button.setEnabled(bool(data) and data[0] == "mine")
        self.load_button.setEnabled(bool(data))

    def _load_preset(self):
        data = self.presets.currentData()
        if not data:
            return
        try:
            chain = effects.load_preset(self.data_dir, data[1])
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Load preset", f"“{data[1]}” could not be read:\n{error}")
            return
        self._replace_chain(chain)

    def _replace_chain(self, chain: list[effects.Effect]):
        self.chain = chain
        self._fill_list(0)
        self._changed()

    def _save_preset(self):
        data = self.presets.currentData()
        name, ok = QInputDialog.getText(self, "Save preset", "Name:",
                                        text=data[1] if data and data[0] == "mine" else "")
        name = effects.safe_preset_name(name or "")
        if not ok or not name:
            return
        if name in effects.BUILTIN_PRESETS:
            QMessageBox.information(self, "Save preset", "That's the name of a built-in preset; pick another.")
            return
        if name in effects.list_presets(self.data_dir) and QMessageBox.question(
                self, "Save preset", f"Replace your preset “{name}”?") != QMessageBox.StandardButton.Yes:
            return
        try:
            effects.save_preset(self.data_dir, name, self.chain)
        except OSError as error:
            QMessageBox.warning(self, "Save preset", f"The preset wasn't saved:\n{error}")
            return
        self._fill_presets(name)

    def _delete_preset(self):
        data = self.presets.currentData()
        if not data or data[0] != "mine":
            return
        if QMessageBox.question(self, "Delete preset", f"Delete your preset “{data[1]}”? This can't be undone.") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            effects.delete_preset(self.data_dir, data[1])
        except OSError as error:
            QMessageBox.warning(self, "Delete preset", f"The preset wasn't deleted:\n{error}")
        self._fill_presets()

    def _import_easyeffects(self):
        start = settings.get(self.qsettings, "effects_import_folder") or next(iter(easyeffects_folders()), "")
        path, _ = QFileDialog.getOpenFileName(self, "Import an Easy Effects preset", start or os.path.expanduser("~"),
                                              "Easy Effects presets (*.json)")
        if not path:
            return
        settings.put(self.qsettings, "effects_import_folder", os.path.dirname(path))
        try:
            with open(path, encoding="utf-8") as f:
                chain, notes = effects.import_easyeffects(json.load(f))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Import preset", f"{os.path.basename(path)} could not be imported:\n{error}")
            return
        if not chain:
            QMessageBox.information(self, "Import preset", "Nothing in that preset can be used here.\n\n"
                                    + "\n".join(notes))
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Import preset",
                          f"Replace the chain with the {len(chain)} effect(s) from {os.path.basename(path)}?",
                          parent=self)
        box.setInformativeText("\n\n".join(notes))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._replace_chain(chain)

    # ------------------------------------------------------------ choices

    def picked_names(self) -> set[str]:
        return {self.tracks.item(i).text() for i in range(self.tracks.count())
                if self.tracks.item(i).checkState() == Qt.CheckState.Checked}

    def channels_for(self, rec: Recording) -> tuple[int, ...]:
        picked = self.picked_names()
        return tuple(i for i, name in enumerate(track_names(rec)) if name in picked)

    def settings_for(self, rec: Recording) -> processing.Settings:
        return processing.Settings(mode="effects", label=effects.describe_chain(self.chain),
                                   channels=self.channels_for(rec))

    def engine_for(self, rec: Recording, cancelled=None) -> effects.Engine:
        return effects.Engine(self.chain, rec.sample_rate, cancelled=cancelled)

    def folder_mode(self) -> str:
        return "beside" if self.beside.isChecked() else "processed"

    def suffix_text(self) -> str:
        return self.suffix.text().strip()

    def _rate(self) -> int:
        rec = self._preview_rec()
        return rec.sample_rate if rec is not None and rec.sample_rate else 48000

    def _update(self):
        example = self.recs[0]
        stem = example.name.rsplit(".", 1)
        suffix = self.suffix_text() or "?"
        name = f"{stem[0]}{suffix}.{stem[1]}" if len(stem) == 2 else example.name + suffix
        where = f"{processing.PROCESSED_FOLDER}/" if self.in_folder.isChecked() else ""
        self.example.setText(f"e.g. {where}{name}")
        problems = effects.chain_problems(self.chain)
        suffix = self.suffix_text()
        if not suffix:
            problems.append("Give the copies a name suffix (e.g. _FX), so they never share a name with the original.")
        elif any(c in suffix for c in '/\\:*?"<>|'):
            problems.append("The suffix can't contain / \\ : * ? \" < > |")
        if not self.picked_names():
            problems.append("Tick at least one track.")
        self.problem.setText(" ".join(problems))
        self.problem.setVisible(bool(problems))
        self.ok.setEnabled(not problems)

    def _accept(self):
        settings.put(self.qsettings, "effects_chain", effects.chain_to_json(self.chain))
        settings.put(self.qsettings, "effects_folder", self.folder_mode())
        settings.put(self.qsettings, "effects_suffix", self.suffix_text())
        self.preview_player.stop()
        self.accept()

    def reject(self):
        # The chain is kept even on Cancel: building one takes work.
        settings.put(self.qsettings, "effects_chain", effects.chain_to_json(self.chain))
        self.preview_player.stop()
        super().reject()

    # ------------------------------------------------------------ preview

    def _preview_rec(self) -> Recording | None:
        if self.current is not None and processable(self.current) is None:
            return self.current
        return next((r for r in self.recs if processable(r) is None), None)

    def _preview_start(self, rec: Recording) -> int:
        if rec is self.current:
            start = int(self.playhead * rec.sample_rate)
            if start + PREVIEW_SECONDS * rec.sample_rate > rec.frames:
                start = max(0, rec.frames - PREVIEW_SECONDS * rec.sample_rate)
            return start
        return 0

    def _toggle_preview(self):
        if self.preview_player.playing():
            self.preview_player.stop()
            self.preview_button.setText("▶ Preview")
            return
        self._start_preview()

    def _start_preview(self):
        rec = self._preview_rec()
        if rec is None:
            return
        if effects.chain_problems(self.chain) or not self.channels_for(rec):
            self.preview_status.setText("Nothing to preview: " + " ".join(
                effects.chain_problems(self.chain) or ["no ticked track in this file."]))
            return
        try:
            start = self._preview_start(rec)
            chosen = self.settings_for(rec)
            engine = self.engine_for(rec)
            original, processed, rate = processing.preview(rec.path, start, start + PREVIEW_SECONDS * rec.sample_rate,
                                                           None, chosen, engine)
        except (OSError, ValueError) as error:
            self.preview_status.setText(f"Can't preview: {error}")
            return
        channels = list(chosen.channels or ())
        pos = self.preview_player.pos if self.preview_player.playing() else 0
        error = self.preview_player.play(_mixdown(original, channels), _mixdown(processed, channels), rate, pos)
        if error:
            self.preview_status.setText(error)
            return
        self.preview_player.processed = self.ab_processed.isChecked()
        self.preview_button.setText("■ Stop")
        report = engine.report(track_names(rec))
        peak = float(np.abs(processed[:, channels]).max()) if channels and len(processed) else 0.0
        peak_text = f"{20 * np.log10(peak):.1f} dBFS" if peak > 0 else "silence"
        loud = (" Loudness is measured on this stretch only; the copies are measured over the whole file."
                if any(e.kind == "loudness" and not e.bypass for e in self.chain) else "")
        self.preview_status.setText(f"Playing {PREVIEW_SECONDS} s of {rec.name} from {format_seconds(start / rate)}, "
                                    f"looping (ticked tracks only). Peak after the effects: {peak_text}." + loud
                                    + ("\n" + "\n".join(report) if report else ""))

    def _ab_changed(self):
        self.preview_player.processed = self.ab_processed.isChecked()

    def _settings_changed(self):
        if self.preview_player.playing():
            self._start_preview()
