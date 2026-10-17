"""The plug-in chain window: the plug-ins found (scan, extra folders, the
VST3 add-on), the chain (order, bypass) and the selected plug-in's
parameters. Plug-ins are only loaded in the helper process (plugins.py);
this window edits a copy of the chain and hands it back on OK."""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from . import compat, plugins, settings
from .workers import run_job

MAX_ROWS = 400  # parameters shown at once (use the filter for more)


class PluginChainDialog(QDialog):
    def __init__(self, qsettings: QSettings, data_dir: str, chain: list[plugins.ChainEntry], rate: int = 48000,
                 parent=None, client: plugins.HostClient | None = None):
        super().__init__(parent)
        self.qsettings, self.data_dir, self.rate = qsettings, data_dir, rate
        self.chain = [plugins.ChainEntry(**vars(e)) for e in chain]
        self.client = client or plugins.HostClient(data_dir)
        self._own_client = client is None
        self.open_id: int | None = None
        self.open_index: int | None = None
        self.params: list[dict] = []
        self.setWindowTitle("Plug-in chain")

        # Available plug-ins
        found_box = QGroupBox("Plug-ins on this computer")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.filter.textChanged.connect(self._fill_found)
        self.found = QListWidget()
        self.found.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.found.itemDoubleClicked.connect(lambda _item: self._add_selected())
        scan = QPushButton("Scan for Plug-ins")
        scan.setToolTip("Look through the usual plug-in folders and the folders you added, and load each "
                        "plug-in once (in a separate process) to read its name")
        scan.clicked.connect(self.scan)
        add_folder = QPushButton("Add Folder…")
        add_folder.clicked.connect(self._add_folder)
        self.folders_label = QLabel()
        self.folders_label.setWordWrap(True)
        self.vst3_status = QLabel()
        self.vst3_status.setWordWrap(True)
        self.vst3_button = QPushButton()
        self.vst3_button.clicked.connect(self._vst3_clicked)
        found_buttons = QHBoxLayout()
        found_buttons.addWidget(scan)
        found_buttons.addWidget(add_folder)
        found_layout = QVBoxLayout(found_box)
        found_layout.addWidget(self.filter)
        found_layout.addWidget(self.found, 1)
        found_layout.addLayout(found_buttons)
        found_layout.addWidget(self.folders_label)
        vst3_row = QHBoxLayout()
        vst3_row.addWidget(self.vst3_status, 1)
        vst3_row.addWidget(self.vst3_button)
        found_layout.addLayout(vst3_row)

        # The chain
        chain_box = QGroupBox("Chain (top to bottom; untick to bypass)")
        self.chain_list = QListWidget()
        self.chain_list.currentRowChanged.connect(self._select)
        self.chain_list.itemChanged.connect(self._bypass_changed)
        add = QPushButton("Add →")
        add.setToolTip("Add the selected plug-ins to the end of the chain (or double-click one)")
        add.clicked.connect(self._add_selected)
        remove, up, down = QPushButton("Remove"), QPushButton("Up"), QPushButton("Down")
        remove.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        chain_buttons = QHBoxLayout()
        for button in (add, remove, up, down):
            chain_buttons.addWidget(button)
        chain_layout = QVBoxLayout(chain_box)
        chain_layout.addWidget(self.chain_list, 1)
        chain_layout.addLayout(chain_buttons)

        # Parameters of the selected plug-in
        params_box = QGroupBox("Settings of the selected plug-in")
        self.params_title = QLabel("Select a plug-in in the chain.")
        self.params_title.setWordWrap(True)
        self.program = QComboBox()
        self.program.setVisible(False)
        self.program.activated.connect(self._program_chosen)
        self.param_filter = QLineEdit()
        self.param_filter.setPlaceholderText("Filter settings")
        self.param_filter.textChanged.connect(self._fill_params)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Setting", "", "Value"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        defaults = QPushButton("Reset to Defaults")
        defaults.clicked.connect(self._defaults)
        self.params_note = QLabel("The plug-in's own window isn't opened here; its settings are set as values. "
                                  "Each track is processed on its own (mono), and the chain's delay is taken off "
                                  "so the copy stays in sync.")
        self.params_note.setWordWrap(True)
        self.params_note.setEnabled(False)
        params_layout = QVBoxLayout(params_box)
        params_layout.addWidget(self.params_title)
        params_layout.addWidget(self.program)
        params_layout.addWidget(self.param_filter)
        params_layout.addWidget(self.table, 1)
        params_layout.addWidget(defaults, 0, Qt.AlignmentFlag.AlignLeft)
        params_layout.addWidget(self.params_note)

        splitter = QSplitter()
        for widget in (found_box, chain_box, params_box):
            splitter.addWidget(widget)
        splitter.setSizes([280, 260, 420])
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)
        self.resize(1000, 600)

        self.available = self._load_found()
        self._fill_found()
        self._update_vst3()
        self._update_folders()
        self._fill_chain()
        if self.chain:
            self.chain_list.setCurrentRow(0)

    # ------------------------------------------------------------ plug-ins found

    def _load_found(self) -> list[dict]:
        try:
            items = json.loads(settings.get(self.qsettings, "plugin_list") or "[]")
            return [i for i in items if isinstance(i, dict) and i.get("format") in plugins.FORMATS]
        except ValueError:
            return []

    def _fill_found(self):
        text = self.filter.text().strip().lower()
        self.found.clear()
        for item in sorted(self.available, key=lambda i: (i["name"].lower(), i["format"])):
            label = f"{item['name']}  ({item['format'].upper()}{', ' + item['vendor'] if item.get('vendor') else ''})"
            if text and text not in label.lower():
                continue
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, item)
            row.setToolTip(item["path"])
            self.found.addItem(row)
        if not self.available:
            row = QListWidgetItem("Nothing yet: click Scan for Plug-ins.")
            row.setFlags(Qt.ItemFlag.NoItemFlags)
            self.found.addItem(row)

    def _update_folders(self):
        extra = settings.get(self.qsettings, "plugin_folders")
        self.folders_label.setText("Also searched: " + ", ".join(extra) if extra else
                                   "Searched: the usual VST and VST3 folders. Add a folder for others.")

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder with plug-ins")
        if folder:
            folders = settings.get(self.qsettings, "plugin_folders")
            if compat.fwd(folder) not in folders:
                settings.put(self.qsettings, "plugin_folders", folders + [compat.fwd(folder)])
            self._update_folders()
            self.scan()

    def scan(self):
        candidates = plugins.find_candidates(settings.get(self.qsettings, "plugin_folders"))
        if not candidates:
            QMessageBox.information(self, "Scan", "No VST2 or VST3 plug-ins were found in the plug-in folders.")
            return
        self._close_open()
        result, error = run_job(self, "Scanning plug-ins", lambda job: plugins.probe_all(
            self.client, candidates, progress=lambda d, t: job.report(d, t, f"Checking plug-in {d} of {t}…"),
            cancelled=lambda: job.cancelled))
        if error is not None:
            if not isinstance(error, InterruptedError):
                QMessageBox.warning(self, "Scan", f"The scan stopped:\n{error}")
            return
        good, bad = result
        self.available = good
        settings.put(self.qsettings, "plugin_list", json.dumps(good))
        self._fill_found()
        vst3_skipped = [b for b in bad if "VST3 add-on" in b]
        others = [b for b in bad if b not in vst3_skipped]
        summary = f"Found {len(good)} plug-in(s)."
        if vst3_skipped:
            summary += f" {len(vst3_skipped)} VST3 plug-in(s) need the VST3 add-on (below the list)."
        if others:
            summary += f"\n\n{len(others)} file(s) couldn't be used:\n" + "\n".join(others[:15]) + \
                ("\n…" if len(others) > 15 else "")
        QMessageBox.information(self, "Scan", summary)

    def _update_vst3(self):
        if plugins._wheel_tags() is None:
            self.vst3_status.setText("VST3 isn't available on this computer.")
            self.vst3_button.setVisible(False)
        elif plugins.pedalboard_installed(self.data_dir):
            self.vst3_status.setText(f"VST3 add-on installed (pedalboard {plugins.PEDALBOARD_VERSION}).")
            self.vst3_button.setText("Remove")
        else:
            self.vst3_status.setText("VST3 needs an add-on: pedalboard, a ~5 MB download (GPL-3.0).")
            self.vst3_button.setText("Install…")

    def _vst3_clicked(self):
        self._close_open()
        self.client.close()  # the helper picks the add-on up when it starts again
        if plugins.pedalboard_installed(self.data_dir):
            if QMessageBox.question(self, "VST3 add-on", "Remove the VST3 add-on?") == QMessageBox.StandardButton.Yes:
                plugins.uninstall_pedalboard(self.data_dir)
            self._update_vst3()
            return
        box = QMessageBox(QMessageBox.Icon.Question, "VST3 add-on",
                          f"Download pedalboard {plugins.PEDALBOARD_VERSION} to host VST3 plug-ins?", parent=self)
        box.setInformativeText("From PyPI (pypi.org), by Spotify, open source under GPL-3.0. About 5 MB; it is "
                               "checked against the checksum PyPI lists and kept in "
                               f"{os.path.join(self.data_dir, 'addons')}. It isn't part of this app, which stays "
                               "MIT licensed.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        _folder, error = run_job(self, "Downloading the VST3 add-on", lambda job: plugins.install_pedalboard(
            self.data_dir, progress=lambda d, t: job.report(d // 1024, max(t, 1) // 1024, "Downloading pedalboard…"),
            cancelled=lambda: job.cancelled))
        if error is not None and not isinstance(error, InterruptedError):
            QMessageBox.warning(self, "VST3 add-on", f"It wasn't installed:\n{error}")
        self._update_vst3()
        if plugins.pedalboard_installed(self.data_dir) and \
                any(kind == "vst3" for kind, _ in plugins.find_candidates(settings.get(self.qsettings,
                                                                                       "plugin_folders"))):
            self.scan()

    # ------------------------------------------------------------ the chain

    def _fill_chain(self):
        self.chain_list.blockSignals(True)
        current = self.chain_list.currentRow()
        self.chain_list.clear()
        for entry in self.chain:
            row = QListWidgetItem(entry.label())
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Unchecked if entry.bypass else Qt.CheckState.Checked)
            row.setToolTip(entry.path)
            self.chain_list.addItem(row)
        self.chain_list.blockSignals(False)
        if 0 <= current < len(self.chain):
            self.chain_list.setCurrentRow(current)

    def _add_selected(self):
        added = False
        for row in self.found.selectedItems():
            item = row.data(Qt.ItemDataRole.UserRole)
            if item:
                self.chain.append(plugins.ChainEntry(item["format"], item["path"], item["name"]))
                added = True
        if added:
            self._fill_chain()
            self.chain_list.setCurrentRow(len(self.chain) - 1)

    def _remove(self):
        index = self.chain_list.currentRow()
        if 0 <= index < len(self.chain):
            self._close_open(save=False)
            del self.chain[index]
            self._fill_chain()
            self._select(self.chain_list.currentRow())

    def _move(self, step: int):
        index = self.chain_list.currentRow()
        target = index + step
        if 0 <= index < len(self.chain) and 0 <= target < len(self.chain):
            self._save_open()
            self.chain[index], self.chain[target] = self.chain[target], self.chain[index]
            self.open_index = target if self.open_index == index else self.open_index
            self._fill_chain()
            self.chain_list.setCurrentRow(target)

    def _bypass_changed(self, row: QListWidgetItem):
        index = self.chain_list.row(row)
        if 0 <= index < len(self.chain):
            self.chain[index].bypass = row.checkState() != Qt.CheckState.Checked

    # ------------------------------------------------------------ parameters

    def _save_open(self):
        if self.open_id is not None and self.open_index is not None and self.open_index < len(self.chain):
            try:
                self.chain[self.open_index].state = self.client.request({"cmd": "state", "id": self.open_id})["state"]
            except plugins.PluginError:
                pass

    def _close_open(self, save: bool = True):
        if save:
            self._save_open()
        if self.open_id is not None:
            try:
                self.client.request({"cmd": "close", "id": self.open_id})
            except plugins.PluginError:
                pass
        self.open_id, self.open_index, self.params = None, None, []

    def _select(self, index: int):
        if index == self.open_index and self.open_id is not None:
            return
        self._close_open()
        self.table.setRowCount(0)
        self.program.setVisible(False)
        if not 0 <= index < len(self.chain):
            self.params_title.setText("Select a plug-in in the chain.")
            return
        entry = self.chain[index]
        try:
            reply = self.client.request({"cmd": "open", "format": entry.format, "path": entry.path,
                                         "rate": self.rate, "state": entry.state})
        except plugins.PluginError as error:
            self.params_title.setText(f"{entry.label()} can't be opened: {error}")
            return
        self.open_id, self.open_index, self.params = reply["id"], index, reply["params"]
        entry.name = reply["name"]
        latency = reply.get("latency", 0)
        self.params_title.setText(f"<b>{reply['name']}</b>" + (f" by {reply['vendor']}" if reply.get("vendor") else "")
                                  + f" ({entry.format.upper()}), {len(self.params)} setting(s)"
                                  + (f", {latency} samples of delay (taken off)" if latency else ""))
        programs = reply.get("programs") or []
        self.program.clear()
        if len(programs) > 1:
            self.program.addItems(programs)
            self.program.setVisible(True)
        self._fill_params()

    def _fill_params(self):
        text = self.param_filter.text().strip().lower()
        shown = [p for p in self.params if not text or text in p["name"].lower()][:MAX_ROWS]
        self.table.setRowCount(len(shown))
        for row, param in enumerate(shown):
            self.table.setItem(row, 0, QTableWidgetItem(param["name"]))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 1000)
            slider.setValue(round(param["value"] * 1000))
            value = QTableWidgetItem(f"{param['display']} {param['label']}".strip())
            slider.valueChanged.connect(lambda v, p=param, cell=value: self._set(p, v / 1000, cell))
            self.table.setCellWidget(row, 1, slider)
            self.table.setItem(row, 2, value)

    def _set(self, param: dict, value: float, cell: QTableWidgetItem):
        if self.open_id is None:
            return
        try:
            reply = self.client.request({"cmd": "set", "id": self.open_id, "key": param["index"], "value": value})
        except plugins.PluginError as error:
            cell.setText(f"error: {error}")
            return
        param["value"] = value
        param["display"] = reply.get("display", "")
        cell.setText(f"{reply.get('display', '')} {reply.get('label', '')}".strip())

    def _program_chosen(self, index: int):
        if self.open_id is None:
            return
        try:
            self.params = self.client.request({"cmd": "program", "id": self.open_id, "index": index})["params"]
        except plugins.PluginError as error:
            QMessageBox.warning(self, "Plug-in", str(error))
            return
        self._fill_params()

    def _defaults(self):
        index = self.chain_list.currentRow()
        if 0 <= index < len(self.chain):
            self._close_open(save=False)
            self.chain[index].state = ""
            self._select(index)

    # ------------------------------------------------------------ closing

    def _accept(self):
        self._close_open()
        self.accept()

    def done(self, result):
        self._close_open(save=result == QDialog.DialogCode.Accepted)
        if self._own_client:
            self.client.close()
        super().done(result)

