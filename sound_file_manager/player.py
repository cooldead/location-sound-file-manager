"""Audio player: libmpv (audio only) + a per-channel waveform that seeks on click."""

from __future__ import annotations

import locale

import mpv
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSizePolicy, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from . import waveform
from .catalog import Cache, Recording

SEEK_STEP = 5.0


def clock(seconds: float) -> str:
    """Elapsed time like "1:05" (rounded down, unlike a duration)."""
    whole = int(max(seconds, 0))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


class _PeaksSignals(QObject):
    partial = Signal(int, object)
    done = Signal(int, object, str)


class _PeaksJob(QRunnable):
    """Computes a waveform off the GUI thread; results are tagged with a
    generation number so a late result for a previous file is ignored.
    A prefetch job only fills the cache and gives way as soon as another
    file is selected."""

    def __init__(self, generation: int, rec: Recording, cache_file: str | None, is_current, prefetch=False):
        super().__init__()
        self.generation, self.rec, self.cache_file, self.is_current = generation, rec, cache_file, is_current
        self.prefetch = prefetch
        self.signals = _PeaksSignals()

    def run(self):
        cache = None
        try:
            if self.cache_file:
                cache = Cache(self.cache_file)
                blob = cache.get_peaks(self.rec.path, self.rec.size, self.rec.mtime)
                if blob:
                    if not self.prefetch:
                        self.signals.done.emit(self.generation, waveform.from_bytes(blob), "")
                    return
            if not self.is_current(self.generation):
                return
            peaks = waveform.compute_peaks(
                self.rec.path,
                on_partial=None if self.prefetch else (lambda p: self.signals.partial.emit(self.generation, p)),
                cancelled=lambda: not self.is_current(self.generation))
            if peaks is None:
                return
            if cache is not None:
                cache.put_peaks(self.rec.path, self.rec.size, self.rec.mtime, waveform.to_bytes(peaks))
            if not self.prefetch:
                self.signals.done.emit(self.generation, peaks, "")
        except Exception as error:  # noqa: BLE001 - shown in the widget, never fatal
            if not self.prefetch:
                self.signals.done.emit(self.generation, None, str(error))
        finally:
            if cache is not None:
                cache.close()


class WaveformView(QWidget):
    """One lane per channel, labelled with the track names; click or drag to seek.

    The waveform is rendered once into a pixmap (again only when the peaks,
    size, solo or palette change); a playhead move just blits it and draws a
    line, so playback costs almost nothing.
    """

    seekRequested = Signal(float)  # fraction 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.peaks: np.ndarray | None = None
        self.tracks: list[str] = []
        self.position = 0.0  # fraction
        self.message = ""
        self._solo: int | None = None
        self._pixmap: QPixmap | None = None
        self._playhead_x = -1
        self.setCursor(Qt.CursorShape.IBeamCursor)

    @property
    def solo(self) -> int | None:
        return self._solo

    @solo.setter
    def solo(self, value: int | None):
        if value != self._solo:
            self._solo = value
            self._invalidate()

    def clear(self, message: str = ""):
        self.peaks, self.tracks, self.position, self.message, self._solo = None, [], 0.0, message, None
        self._invalidate()

    def set_peaks(self, peaks, tracks: list[str]):
        self.peaks, self.tracks, self.message = peaks, tracks, ""
        self._invalidate()

    def set_position(self, fraction: float):
        self.position = fraction
        x = int(fraction * self.width())
        if x != self._playhead_x:
            # Repaint only the strips under the old and new playhead.
            old, self._playhead_x = self._playhead_x, x
            self.update(QRect(old - 2, 0, 5, self.height()))
            self.update(QRect(x - 2, 0, 5, self.height()))

    def _invalidate(self):
        self._pixmap = None
        self.update()

    def resizeEvent(self, event):
        self._pixmap = None
        super().resizeEvent(event)

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.EnabledChange):
            self._pixmap = None
        super().changeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.width() > 0:
            self._seek(event.position().x())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek(event.position().x())

    def _seek(self, x: float):
        fraction = min(max(x / self.width(), 0.0), 1.0)
        self.set_position(fraction)
        self.seekRequested.emit(fraction)

    def paintEvent(self, event):
        if self._pixmap is None or self._pixmap.size() != self.size() * self.devicePixelRatioF():
            self._pixmap = self._render()
        painter = QPainter(self)
        painter.drawPixmap(QRectF(event.rect()), self._pixmap,
                           QRectF(event.rect().topLeft() * self.devicePixelRatioF(),
                                  event.rect().size() * self.devicePixelRatioF()))
        if self.peaks is not None:
            x = int(self.position * self.width())
            self._playhead_x = x
            painter.fillRect(QRect(x - 1, 0, 2, self.height()), QColor("#e5484d"))

    def _render(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        palette = self.palette()
        pixmap.fill(palette.color(QPalette.ColorRole.Base))
        painter = QPainter(pixmap)
        rect = self.rect()
        if self.peaks is None:
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.message)
            painter.end()
            return pixmap
        channels, buckets = self.peaks.shape
        width_px, height_px = pixmap.width(), pixmap.height()
        lane_px = height_px / channels
        wave = palette.color(QPalette.ColorRole.Highlight)
        base = palette.color(QPalette.ColorRole.Base)
        dim = QColor(
            *(int(a * 0.3 + b * 0.7) for a, b in zip(wave.getRgb()[:3], base.getRgb()[:3])))
        # Rasterise in numpy (a Python polygon per channel took ~80 ms): each
        # pixel is lit when it is within the column's peak of the lane centre.
        columns = np.linspace(0, buckets - 1, max(width_px, 1)).astype(int)
        ys = np.arange(height_px, dtype=np.float32)[:, None]
        lane = np.minimum((ys / lane_px).astype(int), channels - 1)
        mid = (lane + 0.5) * lane_px
        half = lane_px / 2 - 1
        levels = self.peaks[:, columns] * half  # (channels, width)
        lit = np.abs(ys - mid) <= np.take_along_axis(levels, np.broadcast_to(lane, (height_px, 1)), 0) + 0.5
        muted_lane = np.array([self._solo is not None and self._solo != ch for ch in range(channels)])
        muted = muted_lane[lane]
        argb = np.full((height_px, width_px), base.rgba(), np.uint32)
        argb[lit & ~muted] = wave.rgba()
        argb[lit & np.broadcast_to(muted, lit.shape)] = dim.rgba()
        image = QImage(argb.data, width_px, height_px, 4 * width_px, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        painter.drawImage(0, 0, image)
        width = rect.width()
        lane_h = rect.height() / channels
        for ch in range(channels):
            top = ch * lane_h
            if ch:
                painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
                painter.drawLine(0, int(top), width, int(top))
            if lane_h >= 14:
                name = self.tracks[ch] if ch < len(self.tracks) and self.tracks[ch] else ""
                painter.setPen(palette.color(QPalette.ColorRole.Text))
                painter.drawText(QRectF(4, top, width - 8, min(lane_h, 18)),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 f"{ch + 1}  {name}".rstrip())
        painter.end()
        return pixmap


class PlayerWidget(QWidget):
    """Loads the selected recording paused; plays only on request."""

    def __init__(self, cache_file: str | None = None, parent=None):
        super().__init__(parent)
        self.cache_file = cache_file
        self.rec: Recording | None = None
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        # libmpv refuses to run (and crashes) unless LC_NUMERIC is "C", and
        # QApplication resets it to the desktop locale.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.mpv = mpv.MPV(
            video=False, vo="null", audio_display=False,
            keep_open="yes", keep_open_pause="no", idle="yes", pause=True,
            config=False, input_default_bindings=False, input_vo_keyboard=False, osc=False, ytdl=False,
            volume_max=200,
        )

        self.wave = WaveformView()
        self.wave.seekRequested.connect(self._seek_fraction)

        style = self.style()
        self.play_button = QToolButton()
        self.play_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setToolTip("Play / pause (Space)")
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button = QToolButton()
        self.stop_button.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setToolTip("Stop and return to the start")
        self.stop_button.clicked.connect(self.stop)

        self.tc_label = QLabel("--:--:--:--")
        font = self.tc_label.font()
        font.setFamily("monospace")
        font.setStyleHint(font.StyleHint.Monospace)
        font.setPointSizeF(font.pointSizeF() * 1.5)
        font.setBold(True)
        self.tc_label.setFont(font)
        self.tc_label.setToolTip("Timecode at the playhead")
        self.time_label = QLabel("0:00 / 0:00")

        self.channel_box = QComboBox()
        self.channel_box.setToolTip("Monitor all channels or solo one")
        self.channel_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.channel_box.currentIndexChanged.connect(self._apply_channel)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 200)
        self.volume.setValue(100)
        self.volume.setFixedWidth(110)
        self.volume.valueChanged.connect(self._set_volume)
        self.volume_label = QLabel("100%")

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addSpacing(8)
        controls.addWidget(self.tc_label)
        controls.addSpacing(8)
        controls.addWidget(self.time_label)
        controls.addStretch(1)
        controls.addWidget(QLabel("Monitor:"))
        controls.addWidget(self.channel_box)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Volume:"))
        controls.addWidget(self.volume)
        controls.addWidget(self.volume_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.wave, 1)
        layout.addLayout(controls)

        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.channel_box.addItem("All channels", None)
        self.wave.clear("Select a recording to see and play it")
        self._set_enabled(False)

    # ------------------------------------------------------------ loading

    def load(self, rec: Recording | None, start: float = 0.0):
        """Show a recording, paused (nothing ever autoplays)."""
        self._generation += 1
        self.rec = rec
        self.mpv.pause = True
        if rec is None or rec.error:
            self.mpv.command("stop")
            self.channel_box.clear()
            self.channel_box.addItem("All channels", None)
            self.wave.clear(f"Can't play this file: {rec.error}" if rec else "Select a recording to see and play it")
            self._set_enabled(False)
            self._update_labels(0.0)
            return
        self.mpv.af = ""
        self.mpv.loadfile(rec.path, start=f"{start:.3f}" if start else "0")
        self._fill_channels(rec)
        self.wave.clear("Reading waveform…")
        self._set_enabled(True)
        self._update_labels(start)
        job = _PeaksJob(self._generation, rec, self.cache_file, lambda g: g == self._generation)
        job.signals.partial.connect(self._on_peaks_partial)
        job.signals.done.connect(self._on_peaks_done)
        self._pool.start(job, 10)

    def prefetch(self, recs: list[Recording]):
        """Compute the next files' waveforms in the background (cache only), so
        stepping through takes shows them at once. Selecting any file cancels it."""
        if not self.cache_file:
            return
        generation = self._generation
        for rec in recs:
            if rec is not None and not rec.error:
                self._pool.start(_PeaksJob(generation, rec, self.cache_file,
                                           lambda g: g == self._generation, prefetch=True), 0)

    def release(self) -> tuple[Recording | None, float, bool]:
        """Close the file (before renaming/writing it). Returns what reload() needs."""
        state = (self.rec, self.position(), not self.mpv.pause)
        self._generation += 1
        self.mpv.pause = True
        self.mpv.command("stop")
        return state

    def reload(self, rec: Recording | None, position: float):
        self.load(rec, position)

    def shutdown(self):
        self._generation += 1
        self._timer.stop()
        self._pool.waitForDone(3000)
        self.mpv.terminate()

    def _on_peaks_partial(self, generation, peaks):
        if generation == self._generation and self.rec:
            self.wave.set_peaks(peaks, self.rec.tracks)

    def _on_peaks_done(self, generation, peaks, error):
        if generation != self._generation or not self.rec:
            return
        if peaks is None:
            self.wave.clear(f"No waveform: {error}" if error else "")
        else:
            self.wave.set_peaks(peaks, self.rec.tracks)
            self.wave.solo = self._solo_channel()

    def _fill_channels(self, rec: Recording):
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        self.channel_box.addItem("All channels", None)
        if rec.channels > 1:
            for ch in range(rec.channels):
                name = rec.tracks[ch] if ch < len(rec.tracks) and rec.tracks[ch] else ""
                self.channel_box.addItem(f"{ch + 1}: {name}" if name else f"Channel {ch + 1}", ch)
        self.channel_box.setCurrentIndex(0)
        self.channel_box.setEnabled(rec.channels > 1)
        self.channel_box.blockSignals(False)
        self._apply_channel()

    def _solo_channel(self) -> int | None:
        return self.channel_box.currentData()

    def _apply_channel(self, *_):
        rec = self.rec
        solo = self._solo_channel()
        self.wave.solo = solo
        self.wave.update()
        if rec is None:
            return
        if solo is not None:
            # A soloed channel goes to both speakers.
            self.mpv.af = f"lavfi=[pan=stereo|c0=c{solo}|c1=c{solo}]"
        elif rec.channels > 2:
            # Poly files have no speaker layout; mpv's default downmix would
            # guess one (e.g. 7.1) and drop "LFE". Mix every channel equally.
            mix = "+".join(f"c{i}" for i in range(rec.channels))
            self.mpv.af = f"lavfi=[pan=stereo|c0<{mix}|c1<{mix}]"
        else:
            self.mpv.af = ""

    # ------------------------------------------------------------ transport

    def toggle_play(self):
        if self.rec is None or self.rec.error:
            return
        if self.mpv.pause and self._at_end():
            self.mpv.seek(0, "absolute", "exact")
        self.mpv.pause = not self.mpv.pause

    def stop(self):
        if self.rec is None:
            return
        self.mpv.pause = True
        self.mpv.seek(0, "absolute", "exact")

    def seek_relative(self, seconds: float):
        if self.rec is not None and not self.rec.error:
            target = min(max(self.position() + seconds, 0.0), max(self.rec.duration - 0.05, 0.0))
            self.mpv.seek(target, "absolute", "exact")

    def _seek_fraction(self, fraction: float):
        if self.rec is not None and self.rec.duration:
            self.mpv.seek(fraction * self.rec.duration, "absolute", "exact")

    def position(self) -> float:
        try:
            value = self.mpv.time_pos
        except Exception:  # noqa: BLE001 - property unavailable while loading
            value = None
        return float(value) if value is not None else 0.0

    def is_playing(self) -> bool:
        return self.rec is not None and not self.mpv.pause

    def _at_end(self) -> bool:
        return bool(self.rec and self.rec.duration and self.position() >= self.rec.duration - 0.05)

    def _set_volume(self, value: int):
        self.mpv.volume = value
        self.volume_label.setText(f"{value}%")

    def _set_enabled(self, enabled: bool):
        for widget in (self.play_button, self.stop_button, self.wave):
            widget.setEnabled(enabled)

    def _tick(self):
        if self.rec is None or self.rec.error:
            return
        position = self.position()
        self._update_labels(position)
        if self.rec.duration:
            self.wave.set_position(position / self.rec.duration)
        icon = QStyle.StandardPixmap.SP_MediaPlay if self.mpv.pause else QStyle.StandardPixmap.SP_MediaPause
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _update_labels(self, position: float):
        rec = self.rec
        if rec is None or rec.error:
            self.tc_label.setText("--:--:--:--")
            self.time_label.setText("")
            return
        self.tc_label.setText(rec.timecode_at(position) or "--:--:--:--")
        self.time_label.setText(f"{clock(position)} / {clock(rec.duration)}")
