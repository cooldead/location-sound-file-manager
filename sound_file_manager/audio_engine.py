"""Playback through Qt Multimedia, mixed live by a MixerState.

A reader thread decodes the WAV ahead of the playhead (the files are on a
network share, so reads must never block the window). The GUI thread mixes
small pieces through the mixer's gain matrix into a QAudioSink buffer of
about BUFFER_SECONDS, so fader, mute and solo changes are heard within that
time. Every piece written is remembered with its file position and meter
levels, so the playhead and the meters follow what is actually heard.
"""

from __future__ import annotations

import os
import threading
from collections import deque

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from . import bwf
from .mixer import MixerState
from .waveform import decode

BLOCK_FRAMES = 4096  # read from the file at a time
AHEAD_BLOCKS = 16  # decoded ahead of the playhead (~1.4 s at 48 kHz)
PIECE_FRAMES = 512  # mixed at a time: the automation and meter resolution
BUFFER_SECONDS = 0.2  # queued in the audio device


class AudioEngine(QObject):
    playingChanged = Signal(bool)
    finished = Signal()  # reached the end (not looping)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mixer = MixerState()
        self.path: str | None = None
        self.rate = 48000
        self.channels = 0
        self.frames = 0
        self._bits, self._float, self._align, self._offset = 16, False, 0, 0
        self._fd: int | None = None
        self._io_lock = threading.Lock()  # held while reading, so close() never pulls the file away mid-read
        self._cond = threading.Condition()
        self._blocks: deque = deque()  # (file frame, samples (n, channels))
        self._read_pos = 0
        self._eof = False
        self._gen = 0
        self._stop = False
        self.loop: tuple[int, int] | None = None  # frames; (0, frames) loops the whole file
        self._playing = False
        self._pos = 0  # the playhead while stopped
        self._sink: QAudioSink | None = None
        self._io = None
        self._stream = 0  # frames written since the sink started
        self._segments: deque = deque()  # (stream start, file frame, count)
        self._meters: deque = deque()  # (stream start, channel peaks, (left, right) peaks)
        self._last_matrix: np.ndarray | None = None
        self._thread = threading.Thread(target=self._reader, name="audio-reader", daemon=True)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._fill)

    # ------------------------------------------------------------ file

    def open(self, path: str, start_frame: int = 0) -> None:
        """Open a WAV (stopped at start_frame). Raises OSError / bwf.WavError."""
        self.close()
        fd = os.open(path, os.O_RDONLY)
        try:
            with os.fdopen(os.dup(fd), "rb") as f:
                layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
                info = bwf._info_from(f, layout)
            data = layout.first(b"data")
            if data is None or not info.block_align or not info.channels:
                raise bwf.WavError("no audio data")
        except BaseException:
            os.close(fd)
            raise
        with self._io_lock:
            self._fd = fd
        self.path = path
        self.rate, self.channels = info.sample_rate or 48000, info.channels
        self._bits, self._float, self._align = info.bits, info.format_tag == 3, info.block_align
        self._offset, self.frames = data.data_offset, data.size // info.block_align
        self.loop = None
        self._pos = min(max(start_frame, 0), self.frames)
        self._restart_reader(self._pos)

    def close(self) -> None:
        self._stop_output()
        self._set_playing(False)
        with self._cond:
            self._gen += 1
            self._blocks.clear()
            self._cond.notify_all()
        with self._io_lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        self.path = None
        self.frames = self.channels = 0
        self._pos = 0

    def shutdown(self) -> None:
        self.close()
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        self._thread.join(2)

    # ------------------------------------------------------------ transport

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        if self.path is None or self._playing:
            return
        if self.loop and not self.loop[0] <= self._pos < self.loop[1]:
            self._pos = self.loop[0]
            self._restart_reader(self._pos)
        elif self._pos >= self.frames:
            self._pos = 0
            self._restart_reader(0)
        if self._start_output():
            self._set_playing(True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._pos = self.position()
        self._stop_output()
        self._set_playing(False)
        self._restart_reader(self._pos)

    def seek(self, frame: int) -> None:
        frame = min(max(int(frame), 0), max(self.frames - 1, 0))
        playing = self._playing
        if playing:
            self._stop_output()
        self._pos = frame
        self._restart_reader(frame)
        if playing and not self._start_output():
            self._set_playing(False)
        self.mixer.stop_recording()

    def set_loop(self, region: tuple[int, int] | None) -> None:
        """Loop a region (frames) or, with None, stop looping. Audio already
        read ahead is thrown away so the loop takes effect at once."""
        if region is not None:
            start, end = sorted((max(int(region[0]), 0), min(int(region[1]), self.frames)))
            region = (start, end) if end - start >= 64 else None
        if region == self.loop:
            return
        self.loop = region
        if self.path is not None:
            self.seek(self.position())

    def position(self) -> int:
        """The file frame being heard now."""
        if not self._playing or self._sink is None:
            return self._pos
        heard = int(self._sink.processedUSecs() * self.rate / 1_000_000)
        while len(self._segments) > 1 and self._segments[1][0] <= heard:
            self._segments.popleft()
        if not self._segments:
            return self._pos
        stream, frame, count = self._segments[0]
        return frame + min(max(heard - stream, 0), count)

    def meters(self) -> tuple[np.ndarray, tuple[float, float]]:
        """Peak levels (linear) of what is being heard: per channel after the
        fader (before mute / solo), and the left / right mix."""
        if not self._playing or self._sink is None or not self._meters:
            return np.zeros(self.channels, np.float32), (0.0, 0.0)
        heard = int(self._sink.processedUSecs() * self.rate / 1_000_000)
        while len(self._meters) > 1 and self._meters[1][0] <= heard:
            self._meters.popleft()
        _, channels, master = self._meters[0]
        return channels, master

    # ------------------------------------------------------------ output

    def _set_playing(self, on: bool) -> None:
        if on != self._playing:
            self._playing = on
            self.playingChanged.emit(on)

    def _start_output(self) -> bool:
        audio_format = QAudioFormat()
        audio_format.setSampleRate(self.rate)
        audio_format.setChannelCount(2)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            self.failed.emit("No audio output device was found.")
            return False
        if not device.isFormatSupported(audio_format):
            self.failed.emit(f"The audio device can't play {self.rate:,} Hz.")
            return False
        self._sink = QAudioSink(device, audio_format, self)
        self._sink.setBufferSize(int(self.rate * BUFFER_SECONDS * 1.5) * 8)
        self._io = self._sink.start()
        if self._io is None or self._sink.error().name != "NoError":  # QAudio enums are QtAudio in Qt 6.8+
            self._stop_output()
            self.failed.emit("The audio device could not be opened.")
            return False
        self._stream = 0
        self._segments.clear()
        self._meters.clear()
        self._last_matrix = None
        self._timer.start()
        self._fill()
        return True

    def _stop_output(self) -> None:
        self._timer.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink, self._io = None, None
        self._segments.clear()
        self._meters.clear()

    def _fill(self) -> None:
        sink = self._sink
        if sink is None or self._io is None:
            return
        heard = int(sink.processedUSecs() * self.rate / 1_000_000)
        queued = self._stream - heard
        room = min(sink.bytesFree() // 8, int(self.rate * BUFFER_SECONDS) - queued)
        pieces = []
        while room >= PIECE_FRAMES or (room > 0 and not pieces):
            with self._cond:
                if not self._blocks:
                    ended = self._eof
                    break
                start, samples = self._blocks[0]
                count = min(PIECE_FRAMES, room, len(samples))
                piece = samples[:count]
                if count == len(samples):
                    self._blocks.popleft()
                else:
                    self._blocks[0] = (start + count, samples[count:])
                self._cond.notify_all()
            pieces.append(self._mix(piece, start))
            room -= count
        else:
            ended = False
        if pieces:
            self._io.write(b"".join(pieces))
        elif ended and (queued <= 0 or sink.state().name == "IdleState"):
            # Everything was heard: stop at the end.
            self._pos = self.frames
            self._stop_output()
            self._set_playing(False)
            self.mixer.stop_recording()
            self.finished.emit()

    def _mix(self, piece: np.ndarray, frame: int) -> bytes:
        matrix = self.mixer.matrix(frame)
        if matrix.shape[0] != piece.shape[1]:
            matrix = np.zeros((piece.shape[1], 2), np.float32)
        new = piece @ matrix
        old = self._last_matrix
        if old is not None and old.shape == matrix.shape and not np.array_equal(old, matrix):
            # Ramp from the previous gains so fader moves don't click.
            ramp = np.linspace(0.0, 1.0, len(piece), dtype=np.float32)[:, None]
            before = piece @ old
            new = before + (new - before) * ramp
        self._last_matrix = matrix
        peaks = np.abs(piece).max(axis=0) * self.mixer.channel_gains(frame) if len(piece) else \
            np.zeros(piece.shape[1], np.float32)
        master = np.abs(new).max(axis=0) if len(new) else np.zeros(2, np.float32)
        self._segments.append((self._stream, frame, len(piece)))
        self._meters.append((self._stream, peaks, (float(master[0]), float(master[1]))))
        self._stream += len(piece)
        return np.clip(new, -1.0, 1.0).astype(np.float32).tobytes()

    # ------------------------------------------------------------ reading

    def _restart_reader(self, frame: int) -> None:
        with self._cond:
            self._gen += 1
            self._blocks.clear()
            self._read_pos = frame
            self._eof = False
            self._cond.notify_all()

    def _reader(self) -> None:
        while True:
            with self._cond:
                while not self._stop and (self._fd is None or self._eof or len(self._blocks) >= AHEAD_BLOCKS):
                    self._cond.wait(0.25)
                if self._stop:
                    return
                gen, pos, loop = self._gen, self._read_pos, self.loop
                end = loop[1] if loop else self.frames
                if pos >= end:
                    if loop:
                        pos = self._read_pos = loop[0]
                    else:
                        self._eof = True
                        continue
                count = min(BLOCK_FRAMES, end - pos)
                align, offset = self._align, self._offset
                bits, channels, is_float = self._bits, self.channels, self._float
            try:
                with self._io_lock:
                    if self._fd is None:
                        continue
                    raw = _pread_all(self._fd, count * align, offset + pos * align)
                samples = decode(raw, bits, channels, is_float)
            except (OSError, bwf.WavError):
                samples = np.zeros((0, max(channels, 1)), np.float32)
            with self._cond:
                if gen != self._gen:
                    continue
                if not len(samples):
                    self._eof = True
                    continue
                self._blocks.append((pos, samples))
                self._read_pos = pos + len(samples)


def _pread_all(fd: int, size: int, offset: int) -> bytes:
    """pread until size bytes or the end of the file (SMB returns short reads)."""
    parts = []
    while size > 0:
        chunk = os.pread(fd, size, offset)
        if not chunk:
            break
        parts.append(chunk)
        size -= len(chunk)
        offset += len(chunk)
    return b"".join(parts)
