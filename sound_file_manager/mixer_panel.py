"""The channel mixer panel: a strip per track (colour, name, mute / solo,
automation arm / clear, meter, fader, link, pan) and a master strip.

The panel edits a mixer.MixerState that the audio engine reads live.
"""

from __future__ import annotations

import json
import math
import os

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QToolButton, QVBoxLayout, QWidget,
)

from . import compat, mixer
from .mixer import MixerState, format_db, format_pan, track_color

METER_FLOOR = -60.0
FADER_STEPS = 10  # fader positions per dB
FADER_TOP = int((mixer.MAX_DB - mixer.MIN_DB) * FADER_STEPS)  # position of +12 dB
FALL_DB = 1.2  # meter fall per update (~40 dB/s at 30 updates a second)
HOLD_TICKS = 45


def fader_to_db(value: int) -> float:
    return mixer.OFF if value <= 0 else mixer.MIN_DB + value / FADER_STEPS


def db_to_fader(db: float) -> int:
    if db == mixer.OFF or db <= mixer.MIN_DB:
        return 0
    return int(round((min(db, mixer.MAX_DB) - mixer.MIN_DB) * FADER_STEPS))


def _mix(a: QColor, b: QColor, amount: float) -> QColor:
    """a with `amount` of b mixed in."""
    return QColor(*(int(x * (1 - amount) + y * amount) for x, y in
                    zip(a.getRgb()[:3], b.getRgb()[:3])))


class LevelMeter(QWidget):
    """Vertical peak meter (one or two bars) with a dB scale, peak hold and a
    clip light. Levels are linear peaks (1.0 = 0 dBFS)."""

    def __init__(self, bars: int = 1, scale: bool = True, parent=None):
        super().__init__(parent)
        self.bars = bars
        self.scale = scale
        self.dim = False
        self._db = [METER_FLOOR] * bars
        self._hold = [METER_FLOOR] * bars
        self._hold_ticks = [0] * bars
        self._clip = [False] * bars
        self.setMinimumHeight(90)
        self.setFixedWidth((20 if scale else 0) + bars * 7 + (bars - 1) * 2 + 2)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolTip("Peak level (dBFS) after the fader. Click to reset the peak hold and clip light.")

    def set_levels(self, levels) -> None:
        changed = False
        for i in range(self.bars):
            peak = float(levels[i]) if i < len(levels) else 0.0
            db = 20 * math.log10(peak) if peak > 1e-6 else METER_FLOOR
            db = max(db, self._db[i] - FALL_DB)  # instant rise, smooth fall
            if db >= self._hold[i] or self._hold_ticks[i] <= 0:
                self._hold[i], self._hold_ticks[i] = max(db, METER_FLOOR), HOLD_TICKS
            else:
                self._hold_ticks[i] -= 1
            if peak >= 0.999:
                self._clip[i] = True
            if abs(db - self._db[i]) > 0.05 or self._hold_ticks[i] == HOLD_TICKS:
                changed = True
            self._db[i] = max(db, METER_FLOOR)
        if changed:
            self.update()

    def reset(self) -> None:
        self._db = [METER_FLOOR] * self.bars
        self._hold = [METER_FLOOR] * self.bars
        self._clip = [False] * self.bars
        self.update()

    def mousePressEvent(self, event):
        self._hold = [METER_FLOOR] * self.bars
        self._clip = [False] * self.bars
        self.update()

    def _y(self, db: float, top: float, height: float) -> float:
        return top + height * (min(max(db, METER_FLOOR), 0.0) / METER_FLOOR)

    def paintEvent(self, event):
        painter = QPainter(self)
        palette = self.palette()
        text = palette.color(QPalette.ColorRole.WindowText)
        left = 20 if self.scale else 0
        top, height = 7.0, self.height() - 9.0
        if self.scale:
            font = painter.font()
            font.setPointSizeF(max(font.pointSizeF() * 0.62, 6.0))
            painter.setFont(font)
            painter.setPen(_mix(text, palette.color(QPalette.ColorRole.Window), 0.45))
            for db in (0, -6, -12, -20, -30, -40, -60):
                y = self._y(db, top, height)
                if db in (-6, -30) and height < 140:
                    continue
                painter.drawText(QRectF(0, y - 6, left - 3, 12), Qt.AlignmentFlag.AlignRight |
                                 Qt.AlignmentFlag.AlignVCenter, str(db))
        gradient = QLinearGradient(0, top, 0, top + height)
        if self.dim:
            gradient.setColorAt(0, QColor("#8a8f98"))
            gradient.setColorAt(1, QColor("#5a5e66"))
        else:
            gradient.setColorAt(0, QColor("#ff3b30"))
            gradient.setColorAt(self._y(-3, 0, 1), QColor("#ffcc00"))
            gradient.setColorAt(self._y(-12, 0, 1), QColor("#35d04a"))
            gradient.setColorAt(1, QColor("#1f9e33"))
        for i in range(self.bars):
            x = left + 1 + i * 9
            rect = QRectF(x, top, 7, height)
            painter.fillRect(rect, QColor(0, 0, 0, 110))
            y = self._y(self._db[i], top, height)
            if self._db[i] > METER_FLOOR:
                painter.fillRect(QRectF(x, y, 7, top + height - y), gradient)
            if self._hold[i] > METER_FLOOR:
                hold_y = self._y(self._hold[i], top, height)
                painter.fillRect(QRectF(x, hold_y, 7, 2), QColor("#ffffff") if not self.dim else QColor("#9aa0a8"))
            painter.fillRect(QRectF(x, 0, 7, 5), QColor("#ff3b30") if self._clip[i] else QColor(0, 0, 0, 110))
        painter.end()


class Fader(QSlider):
    """Vertical fader in dB (bottom = off); double-click returns it to 0 dB."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self.setRange(0, FADER_TOP)
        self.setValue(db_to_fader(0.0))
        self.setPageStep(3 * FADER_STEPS)
        self.setSingleStep(FADER_STEPS // 2)
        self.setMinimumHeight(90)
        self.setToolTip("Level. Double-click for 0 dB; the mouse wheel moves it in 0.5 dB steps.")

    def mouseDoubleClickEvent(self, event):
        self.setValue(db_to_fader(0.0))


class PanSlider(QSlider):
    """Pan, -100 (left) .. +100 (right); double-click centres it."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(-100, 100)
        self.setPageStep(10)
        self.setToolTip("Pan. Double-click to centre it.")

    def mouseDoubleClickEvent(self, event):
        self.setValue(0)


def _small_button(text: str, tip: str, checkable: bool = True) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tip)
    button.setCheckable(checkable)
    button.setAutoRaise(False)
    button.setFixedSize(QSize(24, 20))
    return button


BUTTON_STYLE = """
QToolButton {{ border: 1px solid rgba(128,128,128,90); border-radius: 3px; background: rgba(0,0,0,40);
              font-weight: bold; font-size: 8pt; padding: 0; }}
QToolButton:checked {{ background: {on}; color: {text}; border-color: {on}; }}
QToolButton:disabled {{ color: rgba(128,128,128,110); }}
"""


class ChannelStrip(QFrame):
    """One track: everything reports through the panel, which owns the state."""

    gainChanged = Signal(int, float)
    panChanged = Signal(int, float)
    muteToggled = Signal(int, bool)
    soloToggled = Signal(int, bool)
    armToggled = Signal(int, bool)
    clearRequested = Signal(int)
    linkToggled = Signal(int, bool)

    def __init__(self, index: int, name: str, can_link: bool, parent=None):
        super().__init__(parent)
        self.index = index
        self.color = QColor(track_color(index))
        self.setObjectName("strip")
        self.setFixedWidth(78)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.name = QLabel(name or f"Ch {index + 1}")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setToolTip(f"Channel {index + 1}" + (f": {name}" if name else ""))
        font = self.name.font()
        font.setPointSizeF(font.pointSizeF() * 0.85)
        font.setBold(True)
        self.name.setFont(font)
        number = QLabel(str(index + 1))
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small = number.font()
        small.setPointSizeF(small.pointSizeF() * 0.75)
        number.setFont(small)

        self.mute = _small_button("M", "Mute")
        self.mute.setStyleSheet(BUTTON_STYLE.format(on="#e5484d", text="white"))
        self.solo = _small_button("S", "Solo")
        self.solo.setStyleSheet(BUTTON_STYLE.format(on="#f5c542", text="black"))
        self.arm = _small_button("A", "Arm automation: while it plays, this track's fader, pan and mute moves are "
                                      "recorded (over what was there)")
        self.arm.setStyleSheet(BUTTON_STYLE.format(on="#e5484d", text="white"))
        self.clear = _small_button("✕", "Clear this track's automation", checkable=False)
        self.clear.setStyleSheet(BUTTON_STYLE.format(on="#e5484d", text="white"))

        self.meter = LevelMeter(1, scale=True)
        self.fader = Fader()
        self.db_label = QLabel("0.0 dB")
        self.db_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.db_label.setFont(small)
        self.link = _small_button("🔗", f"Link with channel {index + 2} as a stereo pair (faders, mutes and solos "
                                        "move together, panned left / right)")
        self.link.setFixedSize(QSize(22, 18))
        self.link.setStyleSheet(BUTTON_STYLE.format(on="#35d04a", text="black"))
        self.link.setVisible(can_link)
        self.pan = PanSlider()
        self.pan_label = QLabel("C")
        self.pan_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pan_label.setFont(small)

        buttons1 = QHBoxLayout()
        buttons1.setSpacing(3)
        buttons1.addStretch(1)
        buttons1.addWidget(self.mute)
        buttons1.addWidget(self.solo)
        buttons1.addStretch(1)
        buttons2 = QHBoxLayout()
        buttons2.setSpacing(3)
        buttons2.addStretch(1)
        buttons2.addWidget(self.arm)
        buttons2.addWidget(self.clear)
        buttons2.addStretch(1)
        levels = QHBoxLayout()
        levels.setSpacing(2)
        levels.addWidget(self.meter)
        levels.addWidget(self.fader)
        readout = QHBoxLayout()
        readout.setSpacing(1)
        readout.addWidget(self.db_label, 1)
        readout.addWidget(self.link)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(3)
        layout.addWidget(self.name)
        layout.addWidget(number)
        layout.addLayout(buttons1)
        layout.addLayout(buttons2)
        layout.addLayout(levels, 1)
        layout.addLayout(readout)
        layout.addWidget(self.pan_label)
        layout.addWidget(self.pan)

        self.fader.valueChanged.connect(lambda v: self.gainChanged.emit(self.index, fader_to_db(v)))
        self.pan.valueChanged.connect(lambda v: self.panChanged.emit(self.index, v / 100))
        self.mute.toggled.connect(lambda on: self.muteToggled.emit(self.index, on))
        self.solo.toggled.connect(lambda on: self.soloToggled.emit(self.index, on))
        self.arm.toggled.connect(lambda on: self.armToggled.emit(self.index, on))
        self.clear.clicked.connect(lambda: self.clearRequested.emit(self.index))
        self.link.toggled.connect(lambda on: self.linkToggled.emit(self.index, on))
        self.set_linked_look(False, False)

    def show_channel(self, ch: mixer.Channel, gain_db: float, pan: float, mute: bool, has_automation: bool) -> None:
        """Show values without emitting signals."""
        for widget in (self.fader, self.pan, self.mute, self.solo, self.arm, self.link):
            widget.blockSignals(True)
        self.fader.setValue(db_to_fader(gain_db))
        self.pan.setValue(int(round(pan * 100)))
        self.mute.setChecked(mute)
        self.solo.setChecked(ch.solo)
        self.arm.setChecked(ch.armed)
        self.link.setChecked(ch.link)
        for widget in (self.fader, self.pan, self.mute, self.solo, self.arm, self.link):
            widget.blockSignals(False)
        self.db_label.setText(format_db(gain_db))
        self.pan_label.setText(format_pan(pan))
        self.clear.setEnabled(has_automation)
        automated = has_automation and not ch.armed
        self.fader.setToolTip("Level (following its automation)" if automated else
                              "Level. Double-click for 0 dB; the mouse wheel moves it in 0.5 dB steps.")

    def set_linked_look(self, left_of_pair: bool, right_of_pair: bool) -> None:
        window = self.palette().color(QPalette.ColorRole.Window)
        background = _mix(window, self.color, 0.16)
        border = "#35d04a" if (left_of_pair or right_of_pair) else _mix(window, self.color, 0.35).name()
        self.setStyleSheet(
            f"QFrame#strip {{ background: {background.name()}; border: 1px solid {border};"
            f" border-top: 3px solid {self.color.name()}; border-radius: 5px; }}")


class MasterStrip(QFrame):
    gainChanged = Signal(float)
    muteToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("master")
        self.setFixedWidth(88)
        title = QLabel("MASTER")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 0.85)
        title.setFont(font)
        title.setStyleSheet("color: #f08a4b;")
        self.mute = _small_button("M", "Mute everything")
        self.mute.setStyleSheet(BUTTON_STYLE.format(on="#e5484d", text="white"))
        self.meter = LevelMeter(2, scale=True)
        self.meter.setToolTip("Left / right level of the mix (dBFS). Click to reset the peak hold and clip lights.")
        self.fader = Fader()
        self.fader.setToolTip("Master level. Double-click for 0 dB.")
        self.db_label = QLabel("0.0 dB")
        self.db_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mute_row = QHBoxLayout()
        mute_row.addStretch(1)
        mute_row.addWidget(self.mute)
        mute_row.addStretch(1)
        levels = QHBoxLayout()
        levels.setSpacing(2)
        levels.addWidget(self.meter)
        levels.addWidget(self.fader)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 6, 4, 4)
        layout.setSpacing(3)
        layout.addWidget(title)
        layout.addLayout(mute_row)
        layout.addLayout(levels, 1)
        layout.addWidget(self.db_label)
        self.setStyleSheet("QFrame#master { border: 1px solid #f08a4b; border-radius: 5px;"
                           " background: rgba(240,138,75,22); }")
        self.fader.valueChanged.connect(lambda v: self.gainChanged.emit(fader_to_db(v)))
        self.mute.toggled.connect(self.muteToggled.emit)

    def show_state(self, state: MixerState) -> None:
        for widget in (self.fader, self.mute):
            widget.blockSignals(True)
        self.fader.setValue(db_to_fader(state.master_db))
        self.mute.setChecked(state.master_mute)
        for widget in (self.fader, self.mute):
            widget.blockSignals(False)
        self.db_label.setText(format_db(state.master_db))


class MixerPanel(QWidget):
    """Header (title + global buttons) and the strips, in a horizontal scroll area."""

    changed = Signal()  # anything that changes what is heard (the waveform greys silent tracks)
    collapsedChanged = Signal(bool)
    message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = MixerState()
        self.strips: list[ChannelStrip] = []
        self.file_name = ""
        self.folder = os.path.expanduser("~")  # where mixes are saved and loaded (set by the player)
        self.position_frame: callable = lambda: None  # the playhead, for automation shown on the faders

        self.toggle = QToolButton()
        self.toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.toggle.setAutoRaise(True)
        self.toggle.setToolTip("Show / hide the mixer")
        self.toggle.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        title = QLabel("Channel Mixer")
        font = title.font()
        font.setBold(True)
        title.setFont(font)

        self.solo_mode = QPushButton("Solo Mode")
        solo_menu = QMenu(self.solo_mode)
        self.solo_add = solo_menu.addAction("Add to the Solo (several tracks at once)")
        self.solo_add.setCheckable(True)
        self.solo_exclusive = solo_menu.addAction("Exclusive Solo (one track at a time)")
        self.solo_exclusive.setCheckable(True)
        solo_menu.addSeparator()
        solo_menu.addAction("Clear All Solos", self._clear_solos)
        self.solo_add.triggered.connect(lambda: self._set_exclusive(False))
        self.solo_exclusive.triggered.connect(lambda: self._set_exclusive(True))
        self.solo_mode.setMenu(solo_menu)
        self.mute_all = QPushButton("Mute All")
        self.mute_all.clicked.connect(self._mute_all)
        self.arm_all = QPushButton("Arm All")
        self.arm_all.clicked.connect(self._arm_all)
        self.clear_all = QPushButton("Clear All Automation")
        self.clear_all.clicked.connect(self._clear_all)
        self.save_button = QPushButton("Save Automation…")
        self.save_button.setToolTip("Save the mixer settings and automation to a file")
        self.save_button.clicked.connect(self.save_mix)
        self.load_button = QPushButton("Load Automation…")
        self.load_button.setToolTip("Load mixer settings and automation saved earlier")
        self.load_button.clicked.connect(self.load_mix)
        self.more = QPushButton("More")
        more_menu = QMenu(self.more)
        self.trim_action = more_menu.addAction("Lower the Mix When Several Tracks Play")
        self.trim_action.setCheckable(True)
        self.trim_action.setToolTip("Scale the mix by 1/√n for n playing tracks, so a poly file doesn't clip")
        self.trim_action.toggled.connect(self._set_trim)
        more_menu.addAction("Reset Mixer", self._reset)
        self.more.setMenu(more_menu)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.toggle)
        header.addWidget(title)
        header.addStretch(1)
        for button in (self.solo_mode, self.mute_all, self.arm_all, self.clear_all, self.save_button,
                       self.load_button, self.more):
            header.addWidget(button)

        self.master = MasterStrip()
        self.master.gainChanged.connect(self._master_gain)
        self.master.muteToggled.connect(self._master_mute)
        self.strip_row = QHBoxLayout()
        self.strip_row.setContentsMargins(0, 0, 0, 0)
        self.strip_row.setSpacing(5)
        self.empty = QLabel("Select a recording to mix its tracks")
        self.empty.setEnabled(False)
        inner = QWidget()
        row = QHBoxLayout(inner)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(10)
        row.addWidget(self.master)
        row.addLayout(self.strip_row)
        row.addWidget(self.empty)
        row.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidget(inner)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(250)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.scroll, 1)
        self.collapsed = False
        self.set_state(self.state)

    # ------------------------------------------------------------ state

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.scroll.setVisible(not collapsed)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow)
        for button in (self.solo_mode, self.mute_all, self.arm_all, self.clear_all, self.save_button,
                       self.load_button, self.more):
            button.setVisible(not collapsed)
        self.collapsedChanged.emit(collapsed)

    def set_state(self, state: MixerState) -> None:
        """Show a (new) state: one strip per channel."""
        self.state = state
        for strip in self.strips:
            self.strip_row.removeWidget(strip)
            strip.deleteLater()
        self.strips = []
        count = len(state.channels)
        for i, ch in enumerate(state.channels):
            strip = ChannelStrip(i, ch.name, i + 1 < count)
            strip.gainChanged.connect(self._gain)
            strip.panChanged.connect(self._pan)
            strip.muteToggled.connect(self._mute)
            strip.soloToggled.connect(self._solo)
            strip.armToggled.connect(self._arm)
            strip.clearRequested.connect(self._clear)
            strip.linkToggled.connect(self._link)
            self.strip_row.addWidget(strip)
            self.strips.append(strip)
        self.empty.setVisible(not count)
        for widget in (self.master, self.mute_all, self.arm_all, self.save_button, self.load_button,
                       self.solo_mode, self.more):
            widget.setEnabled(bool(count))
        self.refresh()

    def refresh(self, frame: int | None = None) -> None:
        """Show the state (at a playhead position for automated tracks)."""
        state = self.state
        if frame is None:
            frame = self.position_frame()
        for i, strip in enumerate(self.strips):
            gain, pan, mute = state.values_at(i, frame)
            strip.show_channel(state.channels[i], gain, pan, mute, state.has_automation(i))
            partner = state.partner(i)
            strip.set_linked_look(partner is not None and partner > i, partner is not None and partner < i)
            strip.meter.dim = not state.audible(i)
        self.master.show_state(state)
        self.solo_add.setChecked(not state.exclusive_solo)
        self.solo_exclusive.setChecked(state.exclusive_solo)
        self.trim_action.blockSignals(True)
        self.trim_action.setChecked(state.auto_trim)
        self.trim_action.blockSignals(False)
        everything_muted = bool(state.channels) and all(c.mute for c in state.channels)
        self.mute_all.setText("Unmute All" if everything_muted else "Mute All")
        all_armed = bool(state.channels) and all(c.armed for c in state.channels)
        self.arm_all.setText("Disarm All" if all_armed else "Arm All")
        self.clear_all.setEnabled(state.has_automation())

    def follow_automation(self, frame: int) -> None:
        """While playing: move the faders of automated tracks (cheap if none)."""
        state = self.state
        for i, strip in enumerate(self.strips):
            if not state.channels[i].armed and state.has_automation(i):
                gain, pan, mute = state.values_at(i, frame)
                strip.show_channel(state.channels[i], gain, pan, mute, True)
                strip.meter.dim = mute or not state.audible(i)

    def set_meters(self, channels, master) -> None:
        for i, strip in enumerate(self.strips):
            strip.meter.set_levels([channels[i] if i < len(channels) else 0.0])
        self.master.meter.set_levels(master)

    def reset_meters(self) -> None:
        for strip in self.strips:
            strip.meter.reset()
        self.master.meter.reset()

    # ------------------------------------------------------------ edits

    def _changed(self, *, refresh: bool = True) -> None:
        if refresh:
            self.refresh()
        self.changed.emit()

    def _gain(self, index: int, db: float) -> None:
        group = self.state.set_gain(index, db)
        for i in group:
            if i != index:
                self.strips[i].show_channel(self.state.channels[i], db, self.state.channels[i].pan,
                                            self.state.channels[i].mute, self.state.has_automation(i))
        self.strips[index].db_label.setText(format_db(self.state.channels[index].gain_db))
        self._changed(refresh=False)

    def _pan(self, index: int, pan: float) -> None:
        for i in self.state.set_pan(index, pan):
            ch = self.state.channels[i]
            if i != index:
                self.strips[i].show_channel(ch, ch.gain_db, ch.pan, ch.mute, self.state.has_automation(i))
            self.strips[i].pan_label.setText(format_pan(ch.pan))
        self._changed(refresh=False)

    def _mute(self, index: int, on: bool) -> None:
        self.state.set_mute(index, on)
        self._changed()

    def _solo(self, index: int, on: bool) -> None:
        self.state.set_solo(index, on)
        self._changed()

    def _arm(self, index: int, on: bool) -> None:
        self.state.set_armed(index, on)
        self._changed()

    def _clear(self, index: int) -> None:
        self.state.clear_automation(index)
        self._changed()

    def _link(self, index: int, on: bool) -> None:
        self.state.set_link(index, on)
        self._changed()

    def _master_gain(self, db: float) -> None:
        self.state.master_db = db
        self.master.db_label.setText(format_db(db))
        self._changed(refresh=False)

    def _master_mute(self, on: bool) -> None:
        self.state.master_mute = on
        self._changed(refresh=False)

    def _set_exclusive(self, on: bool) -> None:
        self.state.exclusive_solo = on
        if on:  # keep only the first soloed track (and its pair)
            soloed = [i for i, c in enumerate(self.state.channels) if c.solo]
            if soloed:
                self.state.clear_solos()
                self.state.set_solo(soloed[0], True)
        self._changed()

    def _set_trim(self, on: bool) -> None:
        self.state.auto_trim = on
        self._changed()

    def _clear_solos(self) -> None:
        self.state.clear_solos()
        self._changed()

    def _mute_all(self) -> None:
        self.state.mute_all(not all(c.mute for c in self.state.channels))
        self._changed()

    def _arm_all(self) -> None:
        self.state.arm_all(not all(c.armed for c in self.state.channels))
        self._changed()

    def _clear_all(self) -> None:
        if QMessageBox.question(self, "Clear All Automation",
                                "Remove the recorded automation of every track?") != QMessageBox.StandardButton.Yes:
            return
        self.state.clear_automation()
        self._changed()

    def _reset(self) -> None:
        self.state.reset()
        self._changed()

    # ------------------------------------------------------------ files

    def save_mix(self) -> None:
        if not self.state.channels:
            return
        stem = os.path.splitext(self.file_name)[0] or "mix"
        path, _ = QFileDialog.getSaveFileName(self, "Save Automation", compat.join(self.folder, f"{stem}.mix.json"),
                                              "Mixer settings (*.mix.json *.json)")
        if not path:
            return
        data = self.state.to_dict()
        data["recording"] = self.file_name
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
        except OSError as error:
            QMessageBox.warning(self, "Save Automation", f"Could not save:\n{error}")
            return
        self.folder = os.path.dirname(path)
        self.message.emit(f"Saved the mix to {path}")

    def load_mix(self) -> None:
        if not self.state.channels:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Load Automation", self.folder,
                                              "Mixer settings (*.mix.json *.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                warnings = self.state.apply_dict(json.load(f))
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, "Load Automation", f"Could not load that file:\n{error}")
            return
        self.folder = os.path.dirname(path)
        self._changed()
        if warnings:
            QMessageBox.information(self, "Load Automation", "\n\n".join(warnings))
        self.message.emit(f"Loaded the mix from {os.path.basename(path)}")
