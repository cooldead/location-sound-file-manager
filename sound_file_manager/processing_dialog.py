"""The Reduce Noise window: de-hiss or denoise settings, the tracks, where the
copies go, and a preview that switches between the original and the
processed sound while it plays. It only collects choices; the main window
writes the files (processing.process_file)."""

from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QObject, QSettings, Qt, QTimer
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QMessageBox, QRadioButton, QVBoxLayout)

from . import deepfilter, plugins, processing, settings
from .plugin_dialog import PluginChainDialog
from .catalog import Recording
from .timecode import seconds_to_clock as format_seconds
from .workers import run_job

PREVIEW_SECONDS = 12
SUPPORTED_BITS = {(8, False), (16, False), (24, False), (32, False), (32, True), (64, True)}


def processable(rec: Recording) -> str | None:
    """Why a recording can't be processed, or None."""
    if rec.error:
        return rec.error
    if (rec.bits, bool(rec.float_samples)) not in SUPPORTED_BITS or not rec.frames:
        return "its sample format isn't supported"
    return None


def track_names(rec: Recording) -> list[str]:
    return [(rec.tracks[i] if i < len(rec.tracks) and rec.tracks[i] else f"Track {i + 1}")
            for i in range(rec.channels)]


class PreviewPlayer(QObject):
    """Plays a short stretch, original or processed, looping; switching keeps
    the position, so the difference is heard right away (within ~0.15 s)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sink = self.io = None
        self.buffers: dict[bool, np.ndarray] = {}
        self.rate, self.pos, self.processed = 48000, 0, True
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._fill)

    def play(self, original: np.ndarray, processed: np.ndarray, rate: int, pos: int = 0) -> str | None:
        """Stereo float buffers (frames, 2), from frame pos. Returns an error message, or None."""
        self.stop()
        self.buffers, self.rate = {False: original, True: processed}, rate
        self.pos = pos if 0 <= pos < len(original) else 0
        audio_format = QAudioFormat()
        audio_format.setSampleRate(rate)
        audio_format.setChannelCount(2)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return "No audio output device was found."
        if not device.isFormatSupported(audio_format):
            return f"The audio device can't play {rate:,} Hz."
        self.sink = QAudioSink(device, audio_format, self)
        self.sink.setBufferSize(int(rate * 0.3) * 8)
        self.io = self.sink.start()
        if self.io is None:
            self.stop()
            return "The audio device could not be opened."
        self.timer.start()
        self._fill()
        return None

    def playing(self) -> bool:
        return self.sink is not None

    def _fill(self):
        if self.sink is None or self.io is None:
            return
        source = self.buffers[self.processed]
        room = min(self.sink.bytesFree() // 8, int(self.rate * 0.15))
        pieces = []
        while room > 0 and len(source):
            count = min(room, len(source) - self.pos)
            pieces.append(source[self.pos:self.pos + count])
            self.pos = (self.pos + count) % len(source)
            room -= count
        if pieces:
            self.io.write(np.concatenate(pieces).astype(np.float32).tobytes())

    def stop(self):
        self.timer.stop()
        if self.sink is not None:
            self.sink.stop()
            self.sink.deleteLater()
        self.sink = self.io = None


def _mixdown(samples: np.ndarray, channels: list[int]) -> np.ndarray:
    """The chosen tracks summed to stereo, lowered by 1/sqrt(n) like the mixer."""
    picked = channels or list(range(samples.shape[1]))
    mono = samples[:, picked].sum(axis=1) / np.sqrt(len(picked))
    return np.repeat(np.clip(mono, -1, 1)[:, None], 2, axis=1)


class ProcessDialog(QDialog):
    """Choices for making noise-reduced copies of the selected recordings."""

    def __init__(self, recs: list[Recording], qsettings: QSettings, current: Recording | None = None,
                 region: tuple[int, int] | None = None, playhead: float = 0.0, parent=None,
                 data_dir: str | None = None):
        super().__init__(parent)
        self.data_dir = data_dir or str(settings.history_path().parent)
        self._preview_engine: deepfilter.Engine | None = None
        # The player's copy of a recording isn't the list's object: match by path.
        current = next((r for r in recs if current is not None and r.path == current.path), None)
        self.recs, self.qsettings, self.current, self.region = recs, qsettings, current, region
        self.playhead = playhead
        self.preview_player = PreviewPlayer(self)
        self._profile_cache: dict[tuple, processing.Profile] = {}
        self.setWindowTitle("Reduce noise / plug-ins")
        many = len(recs) > 1
        intro = QLabel(f"Makes a noise-reduced <b>copy</b> of {'each selected file' if many else 'the file'}; "
                       "the original is never changed. The copy keeps the metadata (scene, take, timecode, notes) "
                       "and gets a name ending in the suffix below, so post can tell them apart. De-hiss and denoise "
                       "are for steady noise (hiss, room tone, air conditioning); de-rustle uses the DeepFilterNet "
                       "add-on, a model trained on speech.")
        intro.setWordWrap(True)

        # What
        what_box = QGroupBox("What to reduce")
        self.dehiss = QRadioButton("De-hiss: steady hiss above a frequency (preamp, radio mic)")
        self.denoise = QRadioButton("Denoise: steady background noise at all frequencies (room tone, AC, traffic)")
        self.voice = QRadioButton("De-rustle / voice cleanup: clothing rustle and changing noise on speech")
        self.voice.setToolTip("DeepFilterNet, a trained model that keeps speech and removes the rest. For dialogue "
                              "tracks at 48 kHz; music and effects may be treated as noise.")
        self.addon_status = QLabel()
        self.addon_status.setWordWrap(True)
        self.addon_button = QPushButton()
        self.addon_button.clicked.connect(self._addon_clicked)
        addon_row = QHBoxLayout()
        addon_row.setContentsMargins(24, 0, 0, 0)
        addon_row.addWidget(self.addon_status, 1)
        addon_row.addWidget(self.addon_button)
        self.use_plugins = QRadioButton("Plug-in chain: your VST2 / VST3 plug-ins, in order")
        self.chain = plugins.chain_from_json(settings.get(qsettings, "process_plugin_chain"))
        self.chain_label = QLabel()
        self.chain_label.setWordWrap(True)
        self.chain_button = QPushButton("Edit Chain…")
        self.chain_button.clicked.connect(self._edit_chain)
        chain_row = QHBoxLayout()
        chain_row.setContentsMargins(24, 0, 0, 0)
        chain_row.addWidget(self.chain_label, 1)
        chain_row.addWidget(self.chain_button)
        self._plugin_client: plugins.HostClient | None = None
        mode = settings.get(qsettings, "process_mode")
        {"dehiss": self.dehiss, "deepfilter": self.voice, "plugins": self.use_plugins}.get(
            mode, self.denoise).setChecked(True)
        self.reduction = self._spin(0, 100, 1, settings.get(qsettings, "process_reduction_db"), " dB")
        self._reduction_mode = self.mode()
        if self._reduction_mode == "deepfilter":
            self.reduction.setValue(float(settings.get(qsettings, "process_df_reduction_db")))
        self.reduction.setToolTip("The most any frequency is turned down. 6–12 dB sounds natural; more can make "
                                  "the voice sound watery.")
        self.threshold = self._spin(0, 15, 0.5, settings.get(qsettings, "process_threshold_db"), " dB")
        self.threshold.setToolTip("How far above the noise level a sound still counts as noise. Higher removes more "
                                  "but can dull quiet consonants.")
        self.hiss_from = self._spin(1, 16, 0.5, settings.get(qsettings, "process_hiss_khz"), " kHz")
        self.hiss_from.setToolTip("De-hiss works fully from here up, fading in over the octave below.")
        form = QFormLayout()
        form.addRow("Reduce by up to", self.reduction)
        form.addRow("Threshold above the noise", self.threshold)
        form.addRow("De-hiss from", self.hiss_from)
        what = QVBoxLayout(what_box)
        what.addWidget(self.dehiss)
        what.addWidget(self.denoise)
        what.addWidget(self.voice)
        what.addLayout(addon_row)
        what.addWidget(self.use_plugins)
        what.addLayout(chain_row)
        what.addLayout(form)

        # Noise profile
        profile_box = self.profile_box = QGroupBox("Learn the noise from")
        self.from_region = QRadioButton()
        self.from_quiet = QRadioButton("The quietest parts of each file (works when a take has pauses)")
        if region is not None and current is not None:
            start, end = (format_seconds(f / current.sample_rate) for f in region)
            self.from_region.setText(f"The region marked on the waveform ({start}–{end} in {current.name})")
            region_hint = ("Mark only room tone, no voice. It is used for every selected file with the same "
                           "sample rate and tracks; any others use their quietest parts.")
            self.from_region.setChecked(True)
        else:
            self.from_region.setText("A region marked on the waveform")
            region_hint = ("To use one: cancel, drag across a stretch of room tone (no voice) on the waveform, "
                           "then open Reduce Noise again.")
            self.from_region.setEnabled(False)
            self.from_quiet.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.from_region)
        group.addButton(self.from_quiet)
        profile_layout = QVBoxLayout(profile_box)
        hint = QLabel(region_hint)
        hint.setWordWrap(True)
        hint.setContentsMargins(24, 0, 0, 4)
        hint.setEnabled(False)
        profile_layout.addWidget(self.from_region)
        profile_layout.addWidget(hint)
        profile_layout.addWidget(self.from_quiet)

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
        self.tracks.setMaximumHeight(min(160, 22 * max(3, len(names)) + 8))
        tracks_layout = QVBoxLayout(tracks_box)
        tracks_layout.addWidget(self.tracks)

        # Output
        out_box = QGroupBox("Where the copies go")
        self.in_folder = QRadioButton(f"In a “{processing.PROCESSED_FOLDER}” folder beside each file")
        self.beside = QRadioButton("Next to each file")
        (self.beside if settings.get(qsettings, "process_folder") == "beside" else self.in_folder).setChecked(True)
        self.suffix = QLineEdit(settings.get(qsettings, "process_suffix") or processing.DEFAULT_SUFFIX)
        self.suffix.setMaximumWidth(120)
        suffix_row = QHBoxLayout()
        suffix_row.addWidget(QLabel("Name suffix:"))
        suffix_row.addWidget(self.suffix)
        self.example = QLabel()
        suffix_row.addWidget(self.example, 1)
        out_layout = QVBoxLayout(out_box)
        out_layout.addWidget(self.in_folder)
        out_layout.addWidget(self.beside)
        out_layout.addLayout(suffix_row)

        # Preview
        preview_box = QGroupBox("Preview")
        self.preview_button = QPushButton("▶ Preview")
        self.ab_original = QRadioButton("Original")
        self.ab_processed = QRadioButton("Processed")
        self.ab_processed.setChecked(True)
        for button in (self.ab_original, self.ab_processed):
            button.toggled.connect(self._ab_changed)
        self.preview_status = QLabel()
        self.preview_status.setWordWrap(True)
        preview_row = QHBoxLayout()
        preview_row.addWidget(self.preview_button)
        preview_row.addWidget(self.ab_original)
        preview_row.addWidget(self.ab_processed)
        preview_row.addStretch(1)
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addLayout(preview_row)
        preview_layout.addWidget(self.preview_status)
        self.preview_button.clicked.connect(self._toggle_preview)
        preview_file = self._preview_rec()
        if preview_file is None:
            preview_box.setEnabled(False)
            self.preview_status.setText("None of the files can be previewed.")
        else:
            start = self._preview_start(preview_file) / preview_file.sample_rate
            self.preview_status.setText(f"Plays {PREVIEW_SECONDS} s of {preview_file.name} from "
                                        f"{format_seconds(start)} (the playhead), looping. Switch between Original "
                                        "and Processed while it plays.")

        self.problem = QLabel()
        self.problem.setStyleSheet("color: #c0392b")
        self.problem.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = self.buttons.addButton("Make Copies", QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(what_box)
        layout.addWidget(profile_box)
        layout.addWidget(tracks_box)
        layout.addWidget(out_box)
        layout.addWidget(preview_box)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        for signal in (self.dehiss.toggled, self.voice.toggled, self.use_plugins.toggled, self.in_folder.toggled,
                       self.suffix.textChanged, self.tracks.itemChanged):
            signal.connect(self._update)
        self.voice.toggled.connect(self._mode_changed)
        self.dehiss.toggled.connect(self._mode_changed)
        for spin in (self.reduction, self.threshold, self.hiss_from):
            spin.valueChanged.connect(self._settings_changed)
        self.dehiss.toggled.connect(self._settings_changed)
        self.tracks.itemChanged.connect(self._settings_changed)
        self.from_region.toggled.connect(self._settings_changed)
        self._update()
        self.resize(640, self.sizeHint().height())

    @staticmethod
    def _spin(low, high, step, value, suffix) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setDecimals(1)
        spin.setValue(float(value))
        spin.setSuffix(suffix)
        spin.setMaximumWidth(110)
        return spin

    # ------------------------------------------------------------ choices

    def picked_names(self) -> set[str]:
        return {self.tracks.item(i).text() for i in range(self.tracks.count())
                if self.tracks.item(i).checkState() == Qt.CheckState.Checked}

    def channels_for(self, rec: Recording) -> tuple[int, ...]:
        picked = self.picked_names()
        return tuple(i for i, name in enumerate(track_names(rec)) if name in picked)

    def mode(self) -> str:
        if self.use_plugins.isChecked():
            return "plugins"
        return "dehiss" if self.dehiss.isChecked() else "deepfilter" if self.voice.isChecked() else "denoise"

    def program(self) -> str | None:
        """The DeepFilterNet program, when the add-on is installed."""
        return deepfilter.installed(self.data_dir)

    def skipped(self) -> list[Recording]:
        """Files the chosen mode can't process (DeepFilterNet: not 48 kHz)."""
        if self.mode() != "deepfilter":
            return []
        return [r for r in self.recs if r.sample_rate != deepfilter.RATE]

    def settings_for(self, rec: Recording) -> processing.Settings:
        return processing.Settings(mode=self.mode(), label=plugins.describe_chain(self.chain),
                                   reduction_db=self.reduction.value(), threshold_db=self.threshold.value(),
                                   hiss_from_hz=self.hiss_from.value() * 1000, channels=self.channels_for(rec))

    def folder_mode(self) -> str:
        return "beside" if self.beside.isChecked() else "processed"

    def suffix_text(self) -> str:
        return self.suffix.text().strip()

    def uses_region(self) -> bool:
        return self.from_region.isEnabled() and self.from_region.isChecked()

    def region_profile_for(self, rec: Recording) -> bool:
        """Whether rec takes the marked region's profile (same rate and track count as the marked file)."""
        return (self.mode() in ("denoise", "dehiss") and self.uses_region() and self.current is not None and rec.sample_rate == self.current.sample_rate
                and rec.channels == self.current.channels)

    def _mode_changed(self):
        # "Reduce by up to" is kept apart for DeepFilterNet (its scale goes to 100 dB = no limit).
        mode = self.mode()
        if (mode == "deepfilter") != (self._reduction_mode == "deepfilter"):
            key = "process_df_reduction_db" if self._reduction_mode == "deepfilter" else "process_reduction_db"
            settings.put(self.qsettings, key, self.reduction.value())
            other = "process_df_reduction_db" if mode == "deepfilter" else "process_reduction_db"
            self.reduction.setValue(float(settings.get(self.qsettings, other)))
        self._reduction_mode = mode

    def _update(self):
        voice = self.voice.isChecked()
        chain = self.use_plugins.isChecked()
        self.hiss_from.setEnabled(self.dehiss.isChecked())
        self.threshold.setEnabled(not voice and not chain)
        self.reduction.setEnabled(not chain)
        self.profile_box.setEnabled(not voice and not chain)
        self.chain_label.setText(plugins.describe_chain(self.chain) if self.chain else
                                 "No plug-ins yet: Edit Chain… to find and add some.")
        self.reduction.setToolTip("How much the model may take away (DeepFilterNet's attenuation limit); 100 dB = "
                                  "no limit. 15–30 dB keeps some air." if voice else
                                  "The most any frequency is turned down. 6–12 dB sounds natural; more can make "
                                  "the voice sound watery.")
        program = self.program()
        if deepfilter.asset() is None:
            self.addon_status.setText("The add-on isn't available for this computer.")
            self.addon_button.setVisible(False)
            self.voice.setEnabled(False)
        elif program:
            self.addon_status.setText(f"DeepFilterNet {deepfilter.VERSION} add-on installed. Speech only, 48 kHz files.")
            self.addon_button.setText("Remove Add-on")
        else:
            size = deepfilter.asset()[2] / 1e6
            self.addon_status.setText(f"Needs the DeepFilterNet add-on: a one-time {size:.0f} MB download, checked "
                                      "before use. Speech only, 48 kHz files.")
            self.addon_button.setText("Install Add-on…")
        example = self.recs[0]
        stem = example.name.rsplit(".", 1)
        suffix = self.suffix_text() or "?"
        name = f"{stem[0]}{suffix}.{stem[1]}" if len(stem) == 2 else example.name + suffix
        where = f"{processing.PROCESSED_FOLDER}/" if self.in_folder.isChecked() else ""
        self.example.setText(f"e.g. {where}{name}")
        problems = []
        suffix = self.suffix_text()
        if not suffix:
            problems.append("Give the copies a name suffix (e.g. _NR), so they never share a name with the original.")
        elif any(c in suffix for c in '/\\:*?"<>|'):
            problems.append("The suffix can't contain / \\ : * ? \" < > |")
        if not self.picked_names():
            problems.append("Tick at least one track.")
        if voice and not program:
            problems.append("Install the DeepFilterNet add-on first (or pick de-hiss or denoise).")
        if chain and not [e for e in self.chain if not e.bypass]:
            problems.append("Add at least one plug-in to the chain (Edit Chain…).")
        skipped = self.skipped()
        if voice and len(skipped) == len(self.recs):
            problems.append("DeepFilterNet only works on 48 kHz recordings, and none of these are.")
        elif skipped:
            problems.append(f"{len(skipped)} file(s) aren't 48 kHz and will be skipped: "
                            + ", ".join(r.name for r in skipped[:5]) + ("…" if len(skipped) > 5 else ""))
        self.problem.setText(" ".join(problems))
        self.problem.setVisible(bool(problems))
        blocking = [p for p in problems if "will be skipped" not in p]
        self.ok.setEnabled(not blocking)

    def _accept(self):
        settings.put(self.qsettings, "process_mode", self.mode())
        settings.put(self.qsettings, "process_df_reduction_db" if self.mode() == "deepfilter" else "process_reduction_db",
                     self.reduction.value())
        settings.put(self.qsettings, "process_threshold_db", self.threshold.value())
        settings.put(self.qsettings, "process_hiss_khz", self.hiss_from.value())
        settings.put(self.qsettings, "process_folder", self.folder_mode())
        settings.put(self.qsettings, "process_plugin_chain", plugins.chain_to_json(self.chain))
        settings.put(self.qsettings, "process_suffix", self.suffix_text())
        self.preview_player.stop()
        self.accept()

    def reject(self):
        self.preview_player.stop()
        super().reject()

    def plugin_client(self) -> plugins.HostClient:
        if self._plugin_client is None:
            self._plugin_client = plugins.HostClient(self.data_dir)
        return self._plugin_client

    def _edit_chain(self):
        self.preview_player.stop()
        self.preview_button.setText("▶ Preview")
        rate = self._preview_rec().sample_rate if self._preview_rec() else 48000
        dialog = PluginChainDialog(self.qsettings, self.data_dir, self.chain, rate, self, self.plugin_client())
        if dialog.exec():
            self.chain = dialog.chain
            settings.put(self.qsettings, "process_plugin_chain", plugins.chain_to_json(self.chain))
            self.use_plugins.setChecked(True)
        self._update()

    def done(self, result):
        if self._plugin_client is not None:
            self._plugin_client.close()
            self._plugin_client = None
        if self._preview_engine is not None:
            self._preview_engine.close()
            self._preview_engine = None
        super().done(result)

    # ------------------------------------------------------------ the add-on

    def _addon_clicked(self):
        if self.program():
            if QMessageBox.question(self, "Remove add-on", "Remove the DeepFilterNet add-on? You can install it "
                                    "again later.") == QMessageBox.StandardButton.Yes:
                self.preview_player.stop()
                deepfilter.uninstall(self.data_dir)
                self._update()
            return
        name, _digest, size = deepfilter.asset()
        url = deepfilter.URL.format(version=deepfilter.VERSION, name=name)
        box = QMessageBox(QMessageBox.Icon.Question, "Install add-on",
                          f"Download DeepFilterNet {deepfilter.VERSION} ({size / 1e6:.0f} MB)?", parent=self)
        box.setInformativeText(f"From: {url}\n\nIt is open source (MIT or Apache 2.0) and runs on this computer; "
                               "no audio is sent anywhere. The download is checked against a known checksum before "
                               f"it is used, and kept in {deepfilter.addon_dir(self.data_dir)}.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        _path, error = run_job(self, "Downloading DeepFilterNet", lambda job: deepfilter.install(
            self.data_dir, progress=lambda d, t: job.report(d // 1024, t // 1024, "Downloading DeepFilterNet…"),
            cancelled=lambda: job.cancelled))
        if error is not None and not isinstance(error, InterruptedError):
            QMessageBox.warning(self, "Install add-on", f"DeepFilterNet wasn't installed:\n{error}")
        if self.program():
            self.voice.setChecked(True)
        self._update()

    # ------------------------------------------------------------ preview

    def _preview_rec(self) -> Recording | None:
        skipped = {r.path for r in self.skipped()}
        if self.current is not None and processable(self.current) is None and self.current.path not in skipped:
            return self.current
        return next((r for r in self.recs if processable(r) is None and r.path not in skipped), None)

    def _preview_start(self, rec: Recording) -> int:
        if rec is self.current:
            start = int(self.playhead * rec.sample_rate)
            if start + PREVIEW_SECONDS * rec.sample_rate > rec.frames:
                start = max(0, rec.frames - PREVIEW_SECONDS * rec.sample_rate)
            return start
        return 0

    def profile_for_preview(self, rec: Recording) -> processing.Profile:
        key = (rec.path, self.region_profile_for(rec))
        if key not in self._profile_cache:
            if key[1]:
                self._profile_cache[key] = processing.region_profile(self.current.path, *self.region)
            else:
                self._profile_cache[key] = processing.estimate_file_profile(rec.path)
        return self._profile_cache[key]

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
        self.preview_status.setText("Working out the preview…")
        self.preview_status.repaint()
        try:
            start = self._preview_start(rec)
            settings_now = self.settings_for(rec)
            profile, engine = None, None
            if settings_now.mode == "plugins":
                end = start + PREVIEW_SECONDS * rec.sample_rate
                original, rate = processing.read_frames(rec.path, start, end)
                reply = self.plugin_client().request({"cmd": "preview", "chain": [vars(e) for e in self.chain],
                                                      "src": rec.path, "first": start, "end": end,
                                                      "channels": list(settings_now.channels or ())})
                processed = np.load(reply["path"])
                os.remove(reply["path"])
            elif settings_now.mode == "deepfilter":
                if self._preview_engine is not None:
                    self._preview_engine.close()
                program = self.program()
                if program:  # without it, preview() says the add-on isn't installed
                    engine = self._preview_engine = deepfilter.Engine(program, settings_now.reduction_db)
            else:
                profile = self.profile_for_preview(rec)
            if settings_now.mode != "plugins":
                original, processed, rate = processing.preview(rec.path, start,
                                                               start + PREVIEW_SECONDS * rec.sample_rate,
                                                               profile, settings_now, engine)
        except (OSError, ValueError, deepfilter.AddonError, plugins.PluginError) as error:
            self.preview_status.setText(f"Can't preview: {error}")
            return
        channels = list(settings_now.channels or ())
        # A settings change while playing carries on from where it was.
        pos = self.preview_player.pos if self.preview_player.playing() else 0
        error = self.preview_player.play(_mixdown(original, channels), _mixdown(processed, channels), rate, pos)
        if error:
            self.preview_status.setText(error)
            return
        self.preview_player.processed = self.ab_processed.isChecked()
        self.preview_button.setText("■ Stop")
        lowered = processing.reduction_db(original[:, channels], processed[:, channels]) if channels else 0.0
        self.preview_status.setText(f"Playing {PREVIEW_SECONDS} s of {rec.name} from "
                                    f"{format_seconds(start / rate)}, looping (ticked tracks only). "
                                    f"Overall {lowered:.1f} dB quieter. "
                                    + (f"Noise {profile.source or 'from the marked region'}." if profile else
                                       plugins.describe_chain(self.chain) + "." if settings_now.mode == "plugins"
                                       else "DeepFilterNet."))

    def _ab_changed(self):
        self.preview_player.processed = self.ab_processed.isChecked()

    def _settings_changed(self):
        if self.preview_player.playing():
            self._start_preview()

