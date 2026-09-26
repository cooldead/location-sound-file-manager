"""Audio player: a colour waveform per track with regions, the transport, and
the channel mixer. Playback is our own (audio_engine) so the mixer is live."""

from __future__ import annotations

import os
import re

import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QInputDialog, QLabel, QMenu, QMessageBox, QScrollBar, QSizePolicy, QSplitter, QStyle,
    QToolButton, QVBoxLayout, QWidget,
)

from . import bwf, library_index, settings, waveform
from .audio_engine import AudioEngine
from .catalog import Cache, Recording
from .markers import Marker, MarkerStore, default_name, next_marker
from .mixer import MixerState, track_color
from .mixer_panel import MixerPanel

SEEK_STEP = 5.0
BACKGROUND = "#15171b"


def clock(seconds: float) -> str:
    """Elapsed time like "1:05" (rounded down, unlike a duration)."""
    whole = int(max(seconds, 0))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def counter(seconds: float, hundredths: bool = True) -> str:
    """"00:00:30.60" like a recorder's counter."""
    seconds = max(seconds, 0.0)
    whole, cents = divmod(int(round(seconds * 100)), 100) if hundredths else (int(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours:02}:{minutes:02}:{secs:02}"
    return text + f".{cents:02}" if hundredths else text


_STEREO_NAMES = {("l", "r"), ("left", "right"), ("lt", "rt"), ("mix l", "mix r"), ("mixl", "mixr"), ("lr", "lr")}


def looks_stereo(names: list[str], filename: str) -> bool:
    """A two-track file that is a left / right mix (panned hard left and right)."""
    if len(names) != 2:
        return False
    if re.search(r"_LR$", os.path.splitext(filename)[0], re.IGNORECASE):
        return True
    a, b = (n.strip().lower() for n in names)
    if (a, b) in _STEREO_NAMES:
        return True
    return bool(a) and a[:-1] == b[:-1] and a[-1:] == "l" and b[-1:] == "r"


class _PeaksSignals(QObject):
    partial = Signal(int, object)
    done = Signal(int, object, str)


class _PeaksJob(QRunnable):
    """Computes a waveform off the GUI thread; results are tagged with a
    generation number so a late result for a previous file is ignored.
    A prefetch job only fills the cache and gives way as soon as another
    file is selected."""

    def __init__(self, generation: int, rec: Recording, cache_file: str | None, is_current, prefetch=False,
                 index: tuple[str, bool, bool] | None = None):
        super().__init__()
        self.generation, self.rec, self.cache_file, self.is_current = generation, rec, cache_file, is_current
        self.prefetch = prefetch
        self.index = index  # (library root, read, write) for waveforms kept in the library index
        self.signals = _PeaksSignals()

    def run(self):
        cache = None
        try:
            if self.cache_file:
                cache = Cache(self.cache_file)
                blob = cache.get_peaks(self.rec.path, self.rec.size, self.rec.mtime)
                levels = waveform.from_bytes(blob) if blob else None  # None: missing or an older format
                if levels is not None:
                    if not self.prefetch:
                        self.signals.done.emit(self.generation, levels, "")
                    return
            root, read_index, write_index = self.index or ("", False, False)
            if read_index:
                # One small file instead of reading the whole WAV.
                blob = library_index.read_levels(root, self.rec.path, self.rec.size, self.rec.mtime)
                levels = waveform.from_bytes(blob) if blob else None
                if levels is not None:
                    if cache is not None:
                        cache.put_peaks(self.rec.path, self.rec.size, self.rec.mtime, blob)
                    if not self.prefetch:
                        self.signals.done.emit(self.generation, levels, "")
                    return
            if not self.is_current(self.generation):
                return
            peaks = waveform.compute_peaks(
                self.rec.path,
                on_partial=None if self.prefetch else (lambda p: self.signals.partial.emit(self.generation, p)),
                cancelled=lambda: not self.is_current(self.generation))
            if peaks is None:
                return
            blob = waveform.to_bytes(peaks)
            if cache is not None:
                cache.put_peaks(self.rec.path, self.rec.size, self.rec.mtime, blob)
            if write_index:
                try:
                    library_index.write_levels(root, self.rec.path, self.rec.size, self.rec.mtime, blob)
                except OSError:
                    pass  # only a speed-up; the local cache has it
            if not self.prefetch:
                self.signals.done.emit(self.generation, peaks, "")
        except Exception as error:  # noqa: BLE001 - shown in the widget, never fatal
            if not self.prefetch:
                self.signals.done.emit(self.generation, None, str(error))
        finally:
            if cache is not None:
                cache.close()


class _DetailSignals(QObject):
    done = Signal(int, object, object)  # token, view, levels


class _DetailJob(QRunnable):
    """Reads the visible part of a file at one bucket per pixel column (zoomed in)."""

    def __init__(self, token: int, path: str, view: tuple[float, float], frames: int, columns: int, is_current):
        super().__init__()
        self.token, self.path, self.view, self.frames, self.columns = token, path, view, frames, columns
        self.is_current = is_current
        self.signals = _DetailSignals()

    def run(self):
        if not self.is_current(self.token):
            return
        first = int(self.view[0] * self.frames)
        end = max(int(np.ceil(self.view[1] * self.frames)), first + 1)
        try:
            levels = waveform.compute_peaks(self.path, max(self.columns, 16), first_frame=first, end_frame=end,
                                            cancelled=lambda: not self.is_current(self.token))
        except Exception:  # noqa: BLE001 - the coarse view stays
            return
        if levels is not None:
            self.signals.done.emit(self.token, self.view, levels)


class WaveformView(QWidget):
    """The tracks' waveforms, in their mixer colours: all in one lane
    ("overlay", loudest behind) or one lane each ("lanes").

    Mouse: click to seek; drag to select a region (drag its edges to change
    it; double-click or Esc clears it); drag a marker to move it, double-click
    it to name it, Delete removes the selected one. The wheel zooms around the
    pointer, Shift+wheel scrolls; + / - / 0 zoom from the keyboard.

    The waveform is rendered once into a pixmap (again only when the levels,
    size, view, zoom or audible tracks change); the playhead, region and
    markers are painted on top, so playback costs almost nothing. When zoomed
    in further than the overview's resolution, it asks for detail
    (detailWanted) and shows that once it arrives.
    """

    seekRequested = Signal(float)  # fraction 0..1
    regionChanged = Signal(object)  # (start, end) fractions, or None
    markersChanged = Signal()  # the user added, moved, renamed or removed a marker
    viewChanged = Signal()  # zoom / scroll
    detailWanted = Signal(float, float, int)  # view start, end (fractions), pixel columns

    MARKER_COLOR = QColor("#f5a524")
    FILE_MARKER_COLOR = QColor("#27c4d8")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMouseTracking(True)
        self.levels: np.ndarray | None = None
        self.tracks: list[str] = []
        self.duration = 0.0
        self.frames = 0
        self.position = 0.0  # fraction
        self.message = ""
        self.mode = "overlay"
        self.scale = "db"
        self.gain = 1.0  # vertical zoom
        self.view = (0.0, 1.0)  # the visible part, fractions of the file
        self.audible: list[bool] = []
        self.region: tuple[float, float] | None = None
        self.looping = False
        self.markers: list[Marker] = []
        self.selected_marker: Marker | None = None
        self._detail: tuple[tuple[float, float], np.ndarray] | None = None
        self._pixmap: QPixmap | None = None
        self._playhead_x = -1
        self._drag: tuple | None = None
        self._moved = False
        self._detail_timer = QTimer(self, singleShot=True, interval=120)
        self._detail_timer.timeout.connect(self._ask_detail)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    # ------------------------------------------------------------ data

    def clear(self, message: str = ""):
        self.levels, self.tracks, self.position, self.message = None, [], 0.0, message
        self.region, self.markers, self.selected_marker, self._detail = None, [], None, None
        self.view = (0.0, 1.0)
        self.viewChanged.emit()
        self._invalidate()

    def set_file(self, frames: int, duration: float):
        self.frames, self.duration = frames, duration

    def set_levels(self, levels, tracks: list[str]):
        self.levels, self.tracks, self.message = levels, tracks, ""
        self._invalidate()

    def set_detail(self, view: tuple[float, float], levels: np.ndarray):
        if view == self.view:
            self._detail = (view, levels)
            self._invalidate()

    def set_markers(self, markers: list[Marker]):
        self.markers = sorted(markers, key=lambda m: m.frame)
        self.selected_marker = None
        self.update()

    def set_audible(self, audible: list[bool]):
        if list(audible) != self.audible:
            self.audible = list(audible)
            self._invalidate()

    def set_view(self, mode: str | None = None, scale: str | None = None):
        self.mode = mode or self.mode
        self.scale = scale or self.scale
        self._invalidate()

    def set_region(self, region: tuple[float, float] | None):
        self.region = region
        self.update()

    def set_looping(self, on: bool):
        self.looping = on
        self.update()

    def set_position(self, fraction: float):
        self.position = fraction
        x = int(self._x(fraction))
        if x != self._playhead_x:
            # Repaint only the strips under the old and new playhead.
            old, self._playhead_x = self._playhead_x, x
            self.update(QRect(old - 6, 0, 13, self.height()))
            self.update(QRect(x - 6, 0, 13, self.height()))

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

    # ------------------------------------------------------------ zoom

    @property
    def span(self) -> float:
        return self.view[1] - self.view[0]

    def _x(self, fraction: float) -> float:
        return (fraction - self.view[0]) / self.span * self.width()

    def _fraction(self, x: float) -> float:
        return min(max(self.view[0] + x / max(self.width(), 1) * self.span, 0.0), 1.0)

    def zoomed(self) -> bool:
        return self.span < 0.9999

    def set_range(self, start: float, end: float):
        """Show part of the file (fractions), kept within the file and at most
        down to about four pixels per sample."""
        least = min(1.0, max(self.width() * self.devicePixelRatioF() / 4 / max(self.frames, 1), 1e-7))
        span = min(max(end - start, least), 1.0)
        start = min(max(start, 0.0), 1.0 - span)
        view = (start, start + span)
        if view != self.view:
            self.view = view
            self._invalidate()
            self.viewChanged.emit()

    def zoom(self, factor: float, around: float | None = None):
        """factor > 1 zooms in, around a fraction of the file (default: the
        playhead when it is visible, else the middle)."""
        if around is None:
            around = self.position if self.view[0] <= self.position <= self.view[1] else \
                (self.view[0] + self.view[1]) / 2
        span = self.span / factor
        ratio = (around - self.view[0]) / self.span if self.span else 0.5
        self.set_range(around - span * ratio, around - span * ratio + span)

    def zoom_to_region(self):
        if self.region is not None:
            margin = (self.region[1] - self.region[0]) * 0.05
            self.set_range(self.region[0] - margin, self.region[1] + margin)

    def fit(self):
        self.set_range(0.0, 1.0)

    def scroll_to(self, start: float):
        self.set_range(start, start + self.span)

    def keep_visible(self, fraction: float):
        """While playing: turn the page when the playhead leaves the view."""
        if self.zoomed() and not self.view[0] <= fraction <= self.view[1]:
            self.scroll_to(fraction - self.span * 0.05)

    def set_gain(self, gain: float):
        gain = min(max(gain, 1.0), 64.0)
        if gain != self.gain:
            self.gain = gain
            self._invalidate()
            self.viewChanged.emit()

    def wheelEvent(self, event):
        if self.levels is None:
            return
        delta = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier or abs(delta.x()) > abs(delta.y()):
            step = (delta.x() or delta.y()) / 120
            self.scroll_to(self.view[0] - step * self.span * 0.1)
        elif delta.y():
            self.zoom(1.25 ** (delta.y() / 120), self._fraction(event.position().x()))
        event.accept()

    # ------------------------------------------------------------ markers

    def marker_frac(self, marker: Marker) -> float:
        return marker.frame / self.frames if self.frames else 0.0

    def marker_at(self, x: float) -> Marker | None:
        best, best_distance = None, 5.0
        for marker in self.markers:
            distance = abs(self._x(self.marker_frac(marker)) - x)
            if distance <= best_distance:
                best, best_distance = marker, distance
        return best

    def rename_marker(self, marker: Marker):
        if marker.from_file:
            return
        name, ok = QInputDialog.getText(self, "Marker Name", "Name:", text=marker.name)
        if ok:
            marker.name = name.strip()
            self.update()
            self.markersChanged.emit()

    def remove_marker(self, marker: Marker):
        if marker in self.markers and not marker.from_file:
            self.markers.remove(marker)
            if self.selected_marker is marker:
                self.selected_marker = None
            self.update()
            self.markersChanged.emit()

    # ------------------------------------------------------------ mouse

    def _near_edge(self, x: float) -> float | None:
        """The other edge's fraction when x is on a region edge."""
        if self.region is None:
            return None
        start, end = (self._x(f) for f in self.region)
        if abs(x - start) <= 5:
            return self.region[1]
        if abs(x - end) <= 5:
            return self.region[0]
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.levels is None:
            return
        x = event.position().x()
        marker = self.marker_at(x)
        anchor = self._near_edge(x)
        if marker is not None:
            self.selected_marker = marker
            self._drag = ("marker", marker, x)
            self.update()
        elif anchor is not None:
            self._drag = ("edge", anchor, x)
        else:
            self._drag = ("new", self._fraction(x), x)
        self._moved = False

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self._drag and event.buttons() & Qt.MouseButton.LeftButton:
            kind, anchor, press_x = self._drag
            if kind == "marker":
                if not anchor.from_file and (self._moved or abs(x - press_x) > 3):
                    self._moved = True
                    anchor.frame = int(round(self._fraction(x) * self.frames))
                    self.update()
            elif kind == "edge" or abs(x - press_x) > 4:
                self._moved = True
                a, b = sorted((anchor, self._fraction(x)))
                self.region = (a, b)
                self.update()
        elif self.marker_at(x) is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            marker = self.marker_at(x)
            self.setToolTip(f"{marker.name}" + ("  (cue marker in the file)" if marker.from_file else
                                                 "  · drag to move, double-click to name, Delete removes it"))
        else:
            self.setToolTip("")
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._near_edge(x) is not None
                           else Qt.CursorShape.IBeamCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        kind, anchor, _ = self._drag
        self._drag = None
        if kind == "marker":
            if self._moved:
                self.markers.sort(key=lambda m: m.frame)
                self.markersChanged.emit()
            else:
                self._seek(self.marker_frac(anchor))
            return
        if self._moved and self.region and self.region[1] - self.region[0] > 1e-6:
            self.regionChanged.emit(self.region)
            self._seek(self.region[0])
        else:
            if self._moved:
                self.region = None
                self.regionChanged.emit(None)
            self._seek(self._fraction(event.position().x()))

    def mouseDoubleClickEvent(self, event):
        marker = self.marker_at(event.position().x())
        if marker is not None:
            self.rename_marker(marker)
        elif self.region is not None:
            self.region = None
            self.regionChanged.emit(None)
            self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape and self.region is not None:
            self.region = None
            self.regionChanged.emit(None)
            self.update()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected_marker is not None:
            self.remove_marker(self.selected_marker)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(2.0)
        elif key == Qt.Key.Key_Minus:
            self.zoom(0.5)
        elif key == Qt.Key.Key_0:
            self.fit()
        else:
            super().keyPressEvent(event)

    def _seek(self, fraction: float):
        self.set_position(fraction)
        self.seekRequested.emit(fraction)

    # ------------------------------------------------------------ painting

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        if self._pixmap is None or self._pixmap.size() != self.size() * ratio:
            self._pixmap = self._render()
        painter = QPainter(self)
        painter.drawPixmap(QRectF(event.rect()), self._pixmap,
                           QRectF(event.rect().topLeft() * ratio, event.rect().size() * ratio))
        if self.levels is None:
            return
        height = self.height()
        if self.region is not None:
            x0, x1 = (self._x(f) for f in self.region)
            painter.fillRect(QRectF(x0, 0, max(x1 - x0, 1), height),
                             QColor(80, 160, 255, 70 if self.looping else 45))
            painter.setPen(QPen(QColor(110, 180, 255, 220), 1))
            painter.drawLine(QPointF(x0, 0), QPointF(x0, height))
            painter.drawLine(QPointF(x1, 0), QPointF(x1, height))
        self._paint_markers(painter)
        x = int(self._x(self.position))
        self._playhead_x = x
        if -2 <= x <= self.width() + 2:
            painter.fillRect(QRect(x - 1, 0, 2, height), QColor("#ff5a4e"))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QColor("#ff5a4e"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 6)]))

    def _paint_markers(self, painter: QPainter):
        if not self.markers or not self.frames:
            return
        font = QFont(painter.font())
        font.setPointSizeF(max(font.pointSizeF() * 0.8, 7.0))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        top = 20 if self.mode == "overlay" else 2  # below the track legend
        for marker in self.markers:
            x = self._x(self.marker_frac(marker))
            if x < -200 or x > self.width() + 2:
                continue
            color = self.FILE_MARKER_COLOR if marker.from_file else self.MARKER_COLOR
            selected = marker is self.selected_marker
            painter.setPen(QPen(color, 2 if selected else 1, Qt.PenStyle.SolidLine if selected else Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(x, top), QPointF(x, self.height()))
            label = marker.name or "Marker"
            width = metrics.horizontalAdvance(label) + 10
            flag = QRectF(x, top, width, metrics.height() + 2)
            painter.fillRect(flag, color if selected else QColor(color.red(), color.green(), color.blue(), 210))
            painter.setPen(QColor("#111111"))
            painter.drawText(flag.adjusted(4, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, label)

    def _visible_levels(self, columns: int) -> np.ndarray:
        """Levels for the visible part: the detail read for this view, else the
        overview's slice (and ask for detail when that is too coarse)."""
        if self._detail is not None and self._detail[0] == self.view:
            return self._detail[1]
        buckets = self.levels.shape[2]
        first = int(np.floor(self.view[0] * buckets))
        last = max(int(np.ceil(self.view[1] * buckets)), first + 1)
        part = self.levels[..., first:min(last, buckets)]
        if part.shape[2] < columns * 0.9 and self.frames > part.shape[2]:
            self._detail_timer.start()
        return part

    def _ask_detail(self):
        if self.levels is not None:
            self.detailWanted.emit(self.view[0], self.view[1], int(self.width() * self.devicePixelRatioF()))

    def _render(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor(BACKGROUND))
        painter = QPainter(pixmap)
        rect = self.rect()
        dim_text = QColor(150, 155, 165)
        if self.levels is None:
            painter.setPen(dim_text)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.message)
            painter.end()
            return pixmap
        channels = self.levels.shape[1]
        colors = [track_color(c) for c in range(channels)]
        audible = self.audible if len(self.audible) == channels else [True] * channels
        ruler = 14 if self.zoomed() and rect.height() > 80 else 0
        wave_px = max(pixmap.height() - int(ruler * ratio), 1)
        argb = waveform.rasterize(self._visible_levels(pixmap.width()), pixmap.width(), wave_px, colors=colors,
                                  audible=audible, mode=self.mode, scale=self.scale, background=BACKGROUND,
                                  gain=self.gain)
        image = QImage(argb.data, pixmap.width(), wave_px, 4 * pixmap.width(), QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(ratio)
        painter.drawImage(0, 0, image)
        width = rect.width()
        wave_rect = QRect(0, 0, width, rect.height() - ruler)
        font = painter.font()
        small = QFont(font)
        small.setPointSizeF(max(font.pointSizeF() * 0.75, 6.5))
        painter.setFont(small)
        if self.mode == "lanes":
            lane_h = wave_rect.height() / channels
            for ch in range(channels):
                top = ch * lane_h
                if ch:
                    painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
                    painter.drawLine(0, int(top), width, int(top))
                if lane_h >= 14:
                    name = self.tracks[ch] if ch < len(self.tracks) and self.tracks[ch] else ""
                    painter.setPen(QColor(colors[ch]) if audible[ch] else dim_text)
                    painter.drawText(QRectF(6, top + 1, width - 12, min(lane_h, 16)),
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                     f"{ch + 1}  {name}".rstrip())
        else:
            self._draw_grid(painter, wave_rect)
            # Legend: the tracks in their colours.
            x = 6.0
            metrics = painter.fontMetrics()
            for ch in range(channels):
                name = self.tracks[ch] if ch < len(self.tracks) and self.tracks[ch] else f"Ch {ch + 1}"
                label = f"{ch + 1} {name}"
                needed = 12 + metrics.horizontalAdvance(label) + 10
                if x + needed > width - 4:
                    break
                painter.fillRect(QRectF(x, 5, 8, 8), QColor(colors[ch]) if audible[ch] else dim_text)
                painter.setPen(QColor(220, 223, 228) if audible[ch] else dim_text)
                painter.drawText(QRectF(x + 12, 0, needed, 18), Qt.AlignmentFlag.AlignVCenter, label)
                x += needed
        if ruler:
            self._draw_ruler(painter, QRect(0, rect.height() - ruler, width, ruler))
        if self.gain > 1.0:
            painter.setPen(dim_text)
            painter.drawText(QRectF(0, 0, width - 6, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"×{self.gain:g} height")
        painter.end()
        return pixmap

    def _draw_ruler(self, painter: QPainter, rect: QRect):
        """Time ticks along the bottom while zoomed in."""
        painter.fillRect(rect, QColor(0, 0, 0, 120))
        if not self.duration:
            return
        start, end = self.view[0] * self.duration, self.view[1] * self.duration
        visible = max(end - start, 1e-6)
        step = next((s for s in (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120,
                                 300, 600, 1800, 3600) if rect.width() / (visible / s) >= 70), 3600)
        tick = np.ceil(start / step) * step
        painter.setPen(QColor(170, 175, 185))
        while tick <= end:
            x = (tick - start) / visible * rect.width()
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.top() + 4))
            text = counter(tick) if step < 1 else counter(tick, hundredths=False)
            if step < 0.01:
                text = f"{counter(tick)[:-3]}.{int(round(tick * 1000)) % 1000:03}"
            painter.drawText(QRectF(x + 3, rect.top(), 120, rect.height()), Qt.AlignmentFlag.AlignVCenter, text)
            tick += step

    def _draw_grid(self, painter: QPainter, rect):
        """dB lines above and below the centre (overlay view)."""
        mid = rect.height() / 2
        half = max(rect.height() / 2 - 2, 1)
        painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
        painter.drawLine(QPointF(0, mid), QPointF(rect.width(), mid))
        for db in (-3, -6, -12, -20, -40):
            if self.scale == "linear":
                level = 10 ** (db / 20) * self.gain
            else:
                level = (db - waveform.FLOOR_DB) / -waveform.FLOOR_DB + 20 * np.log10(self.gain) / -waveform.FLOOR_DB
            offset = level * half
            if offset < 12 or offset > half:
                continue
            painter.setPen(QPen(QColor(255, 255, 255, 18), 1, Qt.PenStyle.DashLine))
            for y in (mid - offset, mid + offset):
                painter.drawLine(QPointF(28, y), QPointF(rect.width(), y))
            painter.setPen(QColor(150, 155, 165, 170))
            for y in (mid - offset, mid + offset):
                painter.drawText(QRectF(2, y - 7, 24, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                 str(db))


def _transport_button(icon: QIcon, tip: str, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setIcon(icon)
    button.setToolTip(tip)
    button.setCheckable(checkable)
    button.setAutoRaise(True)
    button.setIconSize(button.iconSize() * 1.25)
    return button


class PlayerWidget(QWidget):
    """The waveform section (header with transport, the waveform) above the
    channel mixer. Loads the selected recording paused; plays only on request."""

    stepRequested = Signal(int)  # -1 previous file, +1 next file
    message = Signal(str)

    MAX_REMEMBERED_MIXES = 300

    def __init__(self, cache_file: str | None = None, qsettings=None, parent=None):
        super().__init__(parent)
        self.cache_file = cache_file
        self.qsettings = qsettings
        self.rec: Recording | None = None
        self._generation = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._mixes: dict[str, MixerState] = {}  # this session's mix per file (with its automation)
        self._last_frame = 0
        self._detail_pool = QThreadPool(self)
        self._detail_pool.setMaxThreadCount(1)
        self._detail_token = 0
        try:
            self.markers = MarkerStore(str(settings.markers_path()) if qsettings is not None else ":memory:")
        except Exception:  # noqa: BLE001 - markers are a convenience; never stop the player
            self.markers = MarkerStore(":memory:")

        self.engine = AudioEngine(self)
        self.engine.playingChanged.connect(self._playing_changed)
        self.engine.failed.connect(self.message)

        self.wave = WaveformView()
        self.wave.seekRequested.connect(self._seek_fraction)
        self.wave.regionChanged.connect(self._region_changed)
        self.wave.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.wave.customContextMenuRequested.connect(self._wave_menu)
        self.wave.detailWanted.connect(self._want_detail)
        self.wave.markersChanged.connect(self._save_markers)
        self.wave.viewChanged.connect(self._view_changed)
        self.scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.scrollbar.setRange(0, 0)
        self.scrollbar.hide()
        self.scrollbar.valueChanged.connect(lambda v: self.wave.scroll_to(v / 100000))

        style = self.style()
        self.wave_toggle = QToolButton()
        self.wave_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.wave_toggle.setAutoRaise(True)
        self.wave_toggle.setToolTip("Show / hide the waveform")
        self.wave_toggle.clicked.connect(lambda: self.set_wave_collapsed(self.wave.isVisible()))
        self.title = QLabel("")
        bold = self.title.font()
        bold.setBold(True)
        self.title.setFont(bold)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.view_overlay = QToolButton()
        self.view_overlay.setText("Combined")
        self.view_overlay.setToolTip("All tracks in one lane, each in its colour")
        self.view_lanes = QToolButton()
        self.view_lanes.setText("Lanes")
        self.view_lanes.setToolTip("One lane per track")
        self.scale_db = QToolButton()
        self.scale_db.setText("dB")
        self.scale_db.setToolTip("Height on a dB scale: quiet dialogue is easy to see")
        self.scale_linear = QToolButton()
        self.scale_linear.setText("Linear")
        self.scale_linear.setToolTip("Height proportional to the amplitude")
        self._view_group, self._scale_group = QButtonGroup(self), QButtonGroup(self)
        for group, buttons in ((self._view_group, (self.view_overlay, self.view_lanes)),
                               (self._scale_group, (self.scale_db, self.scale_linear))):
            for button in buttons:
                button.setCheckable(True)
                button.setAutoRaise(True)
                group.addButton(button)
        self.view_overlay.toggled.connect(lambda on: on and self._set_view("overlay", None))
        self.view_lanes.toggled.connect(lambda on: on and self._set_view("lanes", None))
        self.scale_db.toggled.connect(lambda on: on and self._set_view(None, "db"))
        self.scale_linear.toggled.connect(lambda on: on and self._set_view(None, "linear"))

        self.play_button = _transport_button(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
                                             "Play / pause (Space)")
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button = _transport_button(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop),
                                             "Stop and return to the start (or the region's start)")
        self.stop_button.clicked.connect(self.stop)
        self.loop_button = _transport_button(QIcon.fromTheme("media-playlist-repeat",
                                                             style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload)),
                                             "Loop the region, or the whole file (L)", checkable=True)
        self.loop_button.toggled.connect(self._loop_toggled)
        self.prev_button = _transport_button(style.standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward),
                                             "Previous file")
        self.prev_button.clicked.connect(lambda: self.stepRequested.emit(-1))
        self.next_button = _transport_button(style.standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward),
                                             "Next file")
        self.next_button.clicked.connect(lambda: self.stepRequested.emit(1))

        def tool(text, tip, slot, shortcut_hint=""):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tip + (f" ({shortcut_hint})" if shortcut_hint else ""))
            button.setAutoRaise(True)
            button.clicked.connect(slot)
            return button

        self.marker_add = tool("◆+", "Add a marker at the playhead", self.add_marker, "M")
        self.marker_prev = tool("◀◆", "Go to the previous marker", lambda: self.jump_marker(-1), ",")
        self.marker_next = tool("◆▶", "Go to the next marker", lambda: self.jump_marker(1), ".")
        self.zoom_out = tool("−", "Zoom out", lambda: self.wave.zoom(0.5), "- or the mouse wheel")
        self.zoom_in = tool("+", "Zoom in", lambda: self.wave.zoom(2.0), "+ or the mouse wheel")
        self.zoom_region = tool("⇤⇥", "Zoom to the selected region", self.wave.zoom_to_region)
        self.zoom_fit = tool("Fit", "Show the whole file", self.wave.fit, "0")
        self.taller = tool("▲", "Taller waveform (vertical zoom)", lambda: self.wave.set_gain(self.wave.gain * 2))
        self.shorter = tool("▼", "Shorter waveform", lambda: self.wave.set_gain(self.wave.gain / 2))

        # The system's fixed-width font: a family named "monospace" only exists on Linux.
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setBold(True)
        mono.setPointSizeF(self.font().pointSizeF() * 1.25)
        self.time_label = QLabel("00:00:00.00 / 00:00:00")
        self.time_label.setFont(mono)
        self.time_label.setStyleSheet("color: #35d04a;")
        self.time_label.setToolTip("Position / length")
        self.tc_label = QLabel("TC --:--:--:--")
        self.tc_label.setFont(mono)
        self.tc_label.setStyleSheet("color: #35d04a;")
        self.tc_label.setToolTip("Timecode at the playhead")
        self.region_label = QLabel("")
        self.region_label.setToolTip("The selected region. Double-click the waveform or press Esc to clear it.")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        header.addWidget(self.wave_toggle)
        header.addWidget(self.title, 1)
        header.addWidget(self.view_overlay)
        header.addWidget(self.view_lanes)
        header.addSpacing(6)
        header.addWidget(self.scale_db)
        header.addWidget(self.scale_linear)
        header.addSpacing(8)
        for button in (self.zoom_out, self.zoom_in, self.zoom_region, self.zoom_fit, self.taller, self.shorter):
            header.addWidget(button)
        header.addSpacing(8)
        for button in (self.marker_add, self.marker_prev, self.marker_next):
            header.addWidget(button)
        header.addSpacing(12)
        for button in (self.play_button, self.stop_button, self.loop_button, self.prev_button, self.next_button):
            header.addWidget(button)
        header.addSpacing(10)
        header.addWidget(self.region_label)
        header.addSpacing(6)
        header.addWidget(self.time_label)
        header.addSpacing(10)
        header.addWidget(self.tc_label)

        wave_section = QWidget()
        wave_layout = QVBoxLayout(wave_section)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        wave_layout.setSpacing(2)
        wave_layout.addLayout(header)
        wave_layout.addWidget(self.wave, 1)
        wave_layout.addWidget(self.scrollbar)
        self.wave_section = wave_section

        self.mixer_panel = MixerPanel()
        self.mixer_panel.changed.connect(self._mix_changed)
        self.mixer_panel.message.connect(self.message)
        self.mixer_panel.collapsedChanged.connect(lambda _c: self._fit_sections())
        self.mixer_panel.position_frame = lambda: self.engine.position() if self.rec else None

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(wave_section)
        self.split.addWidget(self.mixer_panel)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.split)

        self._restore_prefs()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.wave.clear("Select a recording to see and play it")
        self._set_enabled(False)

    # ------------------------------------------------------------ preferences

    def _pref(self, key: str):
        return settings.get(self.qsettings, key) if self.qsettings is not None else settings.DEFAULTS[key]

    def _restore_prefs(self):
        mode, scale = self._pref("waveform_view"), self._pref("waveform_scale")
        (self.view_lanes if mode == "lanes" else self.view_overlay).setChecked(True)
        (self.scale_linear if scale == "linear" else self.scale_db).setChecked(True)
        self.wave.set_view(mode, scale)
        state = self.mixer_panel.state
        state.master_db = float(self._pref("mixer_master_db"))
        state.exclusive_solo = bool(self._pref("mixer_exclusive_solo"))
        state.auto_trim = bool(self._pref("mixer_auto_trim"))
        folder = self._pref("mixer_folder")
        if folder:
            self.mixer_panel.folder = folder
        self.mixer_panel.refresh(None)
        if self._pref("mixer_collapsed"):
            self.mixer_panel.set_collapsed(True)
        if self._pref("waveform_collapsed"):
            self.set_wave_collapsed(True)
        state_bytes = self.qsettings.value("window/player_split") if self.qsettings is not None else None
        if state_bytes is not None:
            self.split.restoreState(state_bytes)

    def save_settings(self):
        if self.qsettings is None:
            return
        state = self.mixer_panel.state
        put = lambda key, value: settings.put(self.qsettings, key, value)  # noqa: E731
        put("waveform_view", self.wave.mode)
        put("waveform_scale", self.wave.scale)
        put("mixer_master_db", state.master_db if state.master_db > -1000 else -1000.0)
        put("mixer_exclusive_solo", state.exclusive_solo)
        put("mixer_auto_trim", state.auto_trim)
        put("mixer_folder", self.mixer_panel.folder)
        put("mixer_collapsed", self.mixer_panel.collapsed)
        put("waveform_collapsed", not self.wave.isVisible())
        self.qsettings.setValue("window/player_split", self.split.saveState())

    def _set_view(self, mode, scale):
        self.wave.set_view(mode, scale)

    def set_wave_collapsed(self, collapsed: bool):
        self.wave.setVisible(not collapsed)
        self.wave_toggle.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow)
        self._fit_sections()

    def _fit_sections(self):
        """A collapsed section shrinks to its header."""
        wave_open, mixer_open = self.wave.isVisible(), not self.mixer_panel.collapsed
        for widget, is_open in ((self.wave_section, wave_open), (self.mixer_panel, mixer_open)):
            widget.setMaximumHeight(16777215 if is_open else widget.sizeHint().height())
        self.split.setStretchFactor(0, 1 if wave_open else 0)
        self.split.setStretchFactor(1, 1 if mixer_open and not wave_open else 0)

    # ------------------------------------------------------------ loading

    def load(self, rec: Recording | None, start: float = 0.0):
        """Show a recording, paused (nothing ever autoplays)."""
        self._generation += 1
        self.engine.close()
        self.rec = rec
        self.wave.set_region(None)
        self.region_label.setText("")
        self.mixer_panel.reset_meters()
        if rec is None or rec.error:
            self.title.setText(rec.name if rec else "")
            self._set_mixer(None)
            self.wave.clear(f"Can't play this file: {rec.error}" if rec else "Select a recording to see and play it")
            self._set_enabled(False)
            self._update_labels(0.0)
            return
        self.title.setText(rec.name)
        self.title.setToolTip(rec.path)
        try:
            self.engine.open(rec.path, int(start * (rec.sample_rate or 48000)))
        except Exception as error:  # noqa: BLE001 - shown in the widget
            self._set_mixer(None)
            self.wave.clear(f"Can't play this file: {error}")
            self._set_enabled(False)
            self._update_labels(0.0)
            return
        self._set_mixer(rec)
        self.wave.set_file(self.engine.frames, rec.duration)
        if self.loop_button.isChecked():
            self.engine.set_loop((0, self.engine.frames))
        self.wave.clear("Reading waveform…")
        self._load_markers(rec)
        self._set_enabled(True)
        self._update_labels(start)
        job = _PeaksJob(self._generation, rec, self.cache_file, lambda g: g == self._generation,
                        index=self._index_for(rec))
        job.signals.partial.connect(self._on_peaks_partial)
        job.signals.done.connect(self._on_peaks_done)
        self._pool.start(job, 10)

    def _set_mixer(self, rec: Recording | None):
        """This file's mix: remembered from earlier in the session, else the
        previous file's fader settings when the tracks are the same, else flat."""
        current = self.mixer_panel.state
        if rec is None:
            state = MixerState()
            state.copy_static(current)
        elif rec.path in self._mixes:
            state = self._mixes[rec.path]
        else:
            names = [rec.tracks[i] if i < len(rec.tracks) else "" for i in range(self.engine.channels)]
            state = MixerState.for_tracks(names, looks_stereo(names, rec.name))
            if current.channels and current.same_layout(names):
                state.copy_static(current)
            else:
                state.master_db, state.master_mute = current.master_db, current.master_mute
                state.exclusive_solo, state.auto_trim = current.exclusive_solo, current.auto_trim
            self._mixes[rec.path] = state
            while len(self._mixes) > self.MAX_REMEMBERED_MIXES:
                self._mixes.pop(next(iter(self._mixes)))
        for ch in state.channels:  # never keep writing automation into the next file
            ch.armed = False
        self.engine.mixer = state
        self.mixer_panel.file_name = rec.name if rec else ""
        self.mixer_panel.set_state(state)
        self._mix_changed()

    # ------------------------------------------------------------ markers

    def _load_markers(self, rec: Recording):
        markers = self.markers.get(rec.path)
        try:
            markers += [Marker(frame, name, from_file=True) for frame, name in bwf.read_cues(rec.path)]
        except (OSError, bwf.WavError, ValueError, IndexError):
            pass
        self.wave.set_markers(markers)

    def _save_markers(self):
        if self.rec is not None:
            self.markers.put(self.rec.path, self.wave.markers)

    def add_marker(self):
        if self.rec is None or self.engine.path is None:
            return
        marker = Marker(self.engine.position(), default_name(self.wave.markers))
        self.wave.set_markers(self.wave.markers + [marker])
        self.wave.selected_marker = marker
        self._save_markers()
        self.message.emit(f"Added {marker.name} at {counter(marker.frame / self.engine.rate)} "
                          "(double-click it to name it)")

    def jump_marker(self, step: int):
        if self.engine.path is None:
            return
        marker = next_marker(self.wave.markers, self.engine.position(), step, tolerance=self.engine.rate // 20)
        if marker is not None:
            self.wave.selected_marker = marker
            self.engine.seek(marker.frame)
            self.wave.keep_visible(marker.frame / max(self.engine.frames, 1))
            self._tick()

    # ------------------------------------------------------------ zoom

    def _want_detail(self, start: float, end: float, columns: int):
        if self.rec is None or self.engine.path is None:
            return
        self._detail_token += 1
        job = _DetailJob(self._detail_token, self.rec.path, (start, end), self.engine.frames, columns,
                         lambda t: t == self._detail_token)
        job.signals.done.connect(self._on_detail)
        self._detail_pool.start(job)

    def _on_detail(self, token, view, levels):
        if token == self._detail_token:
            self.wave.set_detail(view, levels)

    def _view_changed(self):
        start, end = self.wave.view
        zoomed = self.wave.zoomed()
        self.scrollbar.blockSignals(True)
        self.scrollbar.setVisible(zoomed)
        page = int((end - start) * 100000)
        self.scrollbar.setRange(0, max(100000 - page, 0))
        self.scrollbar.setPageStep(max(page, 1))
        self.scrollbar.setSingleStep(max(page // 10, 1))
        self.scrollbar.setValue(int(start * 100000))
        self.scrollbar.blockSignals(False)
        self._detail_token += 1  # a detail read for the old view is no longer wanted

    def prefetch(self, recs: list[Recording]):
        """Compute the next files' waveforms in the background (cache only), so
        stepping through takes shows them at once. Selecting any file cancels it."""
        if not self.cache_file:
            return
        generation = self._generation
        for rec in recs:
            if rec is not None and not rec.error:
                self._pool.start(_PeaksJob(generation, rec, self.cache_file, lambda g: g == self._generation,
                                           prefetch=True, index=self._index_for(rec)), 0)

    def set_library_index(self, root: str, read: bool, write: bool) -> None:
        """Waveforms in the library index (the Settings "Use" and "Update"
        options): read only when the library has any, written when allowed."""
        self._index_root = root or ""
        self._index_write = bool(root) and write
        self._index_read = bool(root) and read and (write or library_index.has_waveforms(root))

    def _index_for(self, rec: Recording) -> tuple[str, bool, bool] | None:
        root = getattr(self, "_index_root", "")
        if not root or library_index.relative_key(root, rec.path) is None:
            return None  # e.g. a card file on the Offload page
        return root, self._index_read, self._index_write

    def release(self) -> tuple[Recording | None, float, bool]:
        """Close the file (before renaming/writing it). Returns what reload() needs."""
        state = (self.rec, self.position(), self.is_playing())
        self._generation += 1
        self.engine.close()
        return state

    def reload(self, rec: Recording | None, position: float):
        self.load(rec, position)

    def shutdown(self):
        self._generation += 1
        self._timer.stop()
        self.save_settings()
        self._detail_token += 1
        self._pool.waitForDone(3000)
        self._detail_pool.waitForDone(3000)
        self.engine.shutdown()
        self.markers.close()

    def _on_peaks_partial(self, generation, levels):
        if generation == self._generation and self.rec:
            self.wave.set_levels(levels, self.rec.tracks)

    def _on_peaks_done(self, generation, levels, error):
        if generation != self._generation or not self.rec:
            return
        if levels is None:
            self.wave.clear(f"No waveform: {error}" if error else "")
        else:
            self.wave.set_levels(levels, self.rec.tracks)

    def _mix_changed(self):
        state = self.mixer_panel.state
        self.wave.set_audible([state.audible(i) for i in range(len(state.channels))])

    # ------------------------------------------------------------ transport

    def toggle_play(self):
        if self.rec is None or self.rec.error or self.engine.path is None:
            return
        if self.engine.playing:
            self.engine.pause()
        else:
            self.engine.play()

    def stop(self):
        if self.engine.path is None:
            return
        self.engine.pause()
        region = self.wave.region
        self.engine.seek(int(region[0] * self.engine.frames) if region else 0)
        self._tick()

    def seek_relative(self, seconds: float):
        if self.engine.path is not None:
            self.engine.seek(self.engine.position() + int(seconds * self.engine.rate))
            self._tick()

    def toggle_loop(self):
        if self.engine.path is not None:
            self.loop_button.toggle()

    def _seek_fraction(self, fraction: float):
        if self.engine.path is not None and self.engine.frames:
            self.engine.seek(int(fraction * self.engine.frames))

    def _region_changed(self, region):
        if region is None or self.rec is None:
            self.region_label.setText("")
        else:
            start, end = (f * self.rec.duration for f in region)
            self.region_label.setText(f"Region {counter(start)}–{counter(end)}  ({end - start:.1f} s)")
        if self.loop_button.isChecked():
            self._apply_loop()

    def _loop_toggled(self, on: bool):
        self.wave.set_looping(on)
        self._apply_loop()

    def _apply_loop(self):
        if self.engine.path is None:
            return
        if not self.loop_button.isChecked():
            self.engine.set_loop(None)
            return
        region = self.wave.region
        frames = self.engine.frames
        self.engine.set_loop((int(region[0] * frames), int(region[1] * frames)) if region else (0, frames))

    def position(self) -> float:
        return self.engine.position() / self.engine.rate if self.engine.path is not None else 0.0

    def is_playing(self) -> bool:
        return self.rec is not None and self.engine.playing

    def _playing_changed(self, playing: bool):
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))
        self.play_button.setStyleSheet("QToolButton { background: rgba(53,208,74,70); border-radius: 4px; }"
                                       if playing else "")
        if not playing:
            self.mixer_panel.state.stop_recording()
            self.mixer_panel.refresh()
        self._tick()

    def _clear_markers(self):
        if QMessageBox.question(self, "Remove Markers", "Remove all markers you added to this file?") == \
                QMessageBox.StandardButton.Yes:
            self.wave.set_markers([m for m in self.wave.markers if m.from_file])
            self._save_markers()

    def _set_enabled(self, enabled: bool):
        for widget in (self.play_button, self.stop_button, self.loop_button, self.wave, self.marker_add,
                       self.marker_prev, self.marker_next, self.zoom_in, self.zoom_out, self.zoom_fit,
                       self.zoom_region, self.taller, self.shorter):
            widget.setEnabled(enabled)

    def _tick(self):
        engine = self.engine
        if self.rec is None or engine.path is None:
            self.mixer_panel.set_meters([], (0.0, 0.0))
            return
        frame = engine.position()
        position = frame / engine.rate
        self._update_labels(position)
        if engine.frames:
            if engine.playing:
                self.wave.keep_visible(frame / engine.frames)
            self.wave.set_position(frame / engine.frames)
        if engine.playing:
            channels, master = engine.meters()
            self.mixer_panel.set_meters(channels, master)
            state = self.mixer_panel.state
            if any(c.armed for c in state.channels):
                jumped = frame < self._last_frame or frame - self._last_frame > engine.rate // 2
                if state.record(frame, jumped):
                    self.mixer_panel.clear_all.setEnabled(True)
            if state.has_automation():
                self.mixer_panel.follow_automation(frame)
        else:
            self.mixer_panel.set_meters([0.0] * len(self.mixer_panel.strips), (0.0, 0.0))
        self._last_frame = frame

    def _update_labels(self, position: float):
        rec = self.rec
        if rec is None or rec.error:
            self.tc_label.setText("TC --:--:--:--")
            self.time_label.setText("00:00:00.00 / 00:00:00")
            return
        self.tc_label.setText(f"TC {rec.timecode_at(position) or '--:--:--:--'}")
        self.time_label.setText(f"{counter(position)} / {counter(rec.duration, hundredths=False)}")

    def _wave_menu(self, pos):
        menu = QMenu(self)
        marker = self.wave.marker_at(pos.x())
        if marker is not None and not marker.from_file:
            menu.addAction(f"Rename “{marker.name}”…", lambda: self.wave.rename_marker(marker))
            menu.addAction(f"Remove “{marker.name}”", lambda: self.wave.remove_marker(marker))
            menu.addSeparator()
        elif marker is not None:
            menu.addAction(f"“{marker.name}” is a cue marker in the file (read only)").setEnabled(False)
            menu.addSeparator()
        if self.rec is not None:
            frame = int(self.wave._fraction(pos.x()) * self.engine.frames)
            menu.addAction("Add Marker Here", lambda: (
                self.wave.set_markers(self.wave.markers + [Marker(frame, default_name(self.wave.markers))]),
                self._save_markers()))
            user_markers = [m for m in self.wave.markers if not m.from_file]
            if user_markers:
                menu.addAction(f"Remove All {len(user_markers)} Markers", self._clear_markers)
            menu.addSeparator()
        menu.addAction("Zoom In", lambda: self.wave.zoom(2.0, self.wave._fraction(pos.x())))
        menu.addAction("Zoom Out", lambda: self.wave.zoom(0.5, self.wave._fraction(pos.x())))
        if self.wave.region is not None:
            menu.addAction("Zoom to Region", self.wave.zoom_to_region)
        menu.addAction("Show Whole File", self.wave.fit)
        menu.addSeparator()
        if self.wave.region is not None:
            menu.addAction("Clear Region", lambda: (self.wave.set_region(None), self._region_changed(None)))
        loop = menu.addAction("Loop Region" if self.wave.region else "Loop")
        loop.setCheckable(True)
        loop.setChecked(self.loop_button.isChecked())
        loop.triggered.connect(self.loop_button.setChecked)
        menu.addSeparator()
        for label, button in (("Combined View", self.view_overlay), ("Lanes View", self.view_lanes),
                              ("dB Scale", self.scale_db), ("Linear Scale", self.scale_linear)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(button.isChecked())
            action.triggered.connect(lambda _on, b=button: b.setChecked(True))
        menu.exec(self.wave.mapToGlobal(pos))
