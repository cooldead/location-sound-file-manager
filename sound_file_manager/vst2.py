"""A small VST2 host, with ctypes. Nothing in here uses Qt.

VST2 plug-ins are shared libraries with one entry point (``VSTPluginMain``,
or ``main`` in old ones) that takes the host's callback and returns an
``AEffect``: a C struct with function pointers and the plug-in's channel and
parameter counts. Hosts don't need Steinberg's (discontinued) SDK for this:
the struct layout and the numbers below are the plain binary interface every
plug-in uses, as in other open-source hosts.

Only what offline processing needs is here: open, sample rate and block
size, parameters (name, value 0..1, display text), programs, state chunks,
latency, and ``processReplacing`` on float buffers. Editors (plug-in
windows) aren't opened.

Plug-ins run inside this process, so a crashing plug-in takes the process
down: the app only loads them in its helper process (plugin_host.py).
"""

from __future__ import annotations

import ctypes
import os
import sys

import numpy as np

MAGIC = 0x56737450  # 'VstP'

# Host -> plug-in (dispatcher opcodes)
EFF_OPEN, EFF_CLOSE, EFF_SET_PROGRAM, EFF_GET_PROGRAM = 0, 1, 2, 3
EFF_GET_PARAM_LABEL, EFF_GET_PARAM_DISPLAY, EFF_GET_PARAM_NAME = 6, 7, 8
EFF_SET_SAMPLE_RATE, EFF_SET_BLOCK_SIZE, EFF_MAINS_CHANGED = 10, 11, 12
EFF_GET_CHUNK, EFF_SET_CHUNK = 23, 24
EFF_GET_PROGRAM_NAME_INDEXED = 29
EFF_GET_EFFECT_NAME, EFF_GET_VENDOR_STRING, EFF_GET_PRODUCT_STRING = 45, 47, 48
EFF_START_PROCESS, EFF_STOP_PROCESS = 71, 72

# Plug-in -> host (audioMaster opcodes)
MASTER_AUTOMATE, MASTER_VERSION, MASTER_CURRENT_ID, MASTER_IDLE = 0, 1, 2, 3
MASTER_GET_TIME = 7
MASTER_GET_SAMPLE_RATE, MASTER_GET_BLOCK_SIZE = 16, 17
MASTER_GET_CURRENT_PROCESS_LEVEL = 23
MASTER_GET_VENDOR_STRING, MASTER_GET_PRODUCT_STRING, MASTER_GET_VENDOR_VERSION = 32, 33, 34
MASTER_CAN_DO = 37
MASTER_GET_LANGUAGE = 38
PROCESS_LEVEL_OFFLINE = 4

FLAG_HAS_EDITOR = 1 << 0
FLAG_CAN_REPLACING = 1 << 4
FLAG_PROGRAM_CHUNKS = 1 << 5
FLAG_IS_SYNTH = 1 << 8

intptr = ctypes.c_ssize_t


class AEffect(ctypes.Structure):
    pass


Dispatcher = ctypes.CFUNCTYPE(intptr, ctypes.POINTER(AEffect), ctypes.c_int32, ctypes.c_int32, intptr,
                              ctypes.c_void_p, ctypes.c_float)
Process = ctypes.CFUNCTYPE(None, ctypes.POINTER(AEffect), ctypes.POINTER(ctypes.POINTER(ctypes.c_float)),
                           ctypes.POINTER(ctypes.POINTER(ctypes.c_float)), ctypes.c_int32)
SetParameter = ctypes.CFUNCTYPE(None, ctypes.POINTER(AEffect), ctypes.c_int32, ctypes.c_float)
GetParameter = ctypes.CFUNCTYPE(ctypes.c_float, ctypes.POINTER(AEffect), ctypes.c_int32)
HostCallback = Dispatcher
Entry = ctypes.CFUNCTYPE(ctypes.POINTER(AEffect), HostCallback)

AEffect._fields_ = [
    ("magic", ctypes.c_int32),
    ("dispatcher", Dispatcher),
    ("process", ctypes.c_void_p),            # the old accumulating process: not used
    ("setParameter", SetParameter),
    ("getParameter", GetParameter),
    ("numPrograms", ctypes.c_int32),
    ("numParams", ctypes.c_int32),
    ("numInputs", ctypes.c_int32),
    ("numOutputs", ctypes.c_int32),
    ("flags", ctypes.c_int32),
    ("resvd1", intptr),
    ("resvd2", intptr),
    ("initialDelay", ctypes.c_int32),
    ("realQualities", ctypes.c_int32),
    ("offQualities", ctypes.c_int32),
    ("ioRatio", ctypes.c_float),
    ("object", ctypes.c_void_p),
    ("user", ctypes.c_void_p),
    ("uniqueID", ctypes.c_int32),
    ("version", ctypes.c_int32),
    ("processReplacing", Process),
    ("processDoubleReplacing", ctypes.c_void_p),
    ("future", ctypes.c_char * 56),
]


class VstError(Exception):
    pass


def binary_path(path: str) -> str:
    """The loadable file of a plug-in: a macOS ``.vst`` bundle holds it in
    Contents/MacOS; elsewhere the plug-in is the file itself."""
    if sys.platform == "darwin" and os.path.isdir(path):
        folder = os.path.join(path, "Contents", "MacOS")
        names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        stem = os.path.splitext(os.path.basename(path))[0]
        name = stem if stem in names else (names[0] if names else "")
        return os.path.join(folder, name)
    return path


class Plugin:
    """One loaded VST2 effect."""

    def __init__(self, path: str, rate: int = 48000, block: int = 4096):
        self.path, self.rate, self.block = path, rate, block
        self._callback = HostCallback(self._host)  # kept: the plug-in calls it for its whole life
        try:
            self.lib = ctypes.CDLL(binary_path(path), mode=getattr(os, "RTLD_LOCAL", 0))
        except OSError as error:
            raise VstError(f"can't load {os.path.basename(path)}: {error}") from error
        entry = None
        for name in ("VSTPluginMain", "main_macho", "main"):
            entry = getattr(self.lib, name, None)
            if entry is not None:
                break
        if entry is None:
            raise VstError(f"{os.path.basename(path)} isn't a VST2 plug-in (no VSTPluginMain)")
        effect = Entry(ctypes.cast(entry, ctypes.c_void_p).value)(self._callback)
        if not effect or effect.contents.magic != MAGIC:
            raise VstError(f"{os.path.basename(path)} didn't start (not a VST2 effect)")
        self.effect = effect
        self.e = effect.contents
        if self.e.flags & FLAG_IS_SYNTH:
            raise VstError(f"{os.path.basename(path)} is an instrument, not an effect")
        if not self.e.flags & FLAG_CAN_REPLACING:
            raise VstError(f"{os.path.basename(path)} is too old (no processReplacing)")
        self._call(EFF_OPEN)
        self._call(EFF_SET_SAMPLE_RATE, opt=float(rate))
        self._call(EFF_SET_BLOCK_SIZE, value=block)
        self.resume()
        self._ins = [np.zeros(block, np.float32) for _ in range(max(1, self.e.numInputs))]
        self._outs = [np.zeros(block, np.float32) for _ in range(max(1, self.e.numOutputs))]
        ptr = ctypes.POINTER(ctypes.c_float)
        self._in_ptrs = (ptr * len(self._ins))(*[a.ctypes.data_as(ptr) for a in self._ins])
        self._out_ptrs = (ptr * len(self._outs))(*[a.ctypes.data_as(ptr) for a in self._outs])

    # -------------------------------------------------------------- the host side

    def _host(self, effect, opcode, index, value, ptr, opt):
        if opcode == MASTER_VERSION:
            return 2400
        if opcode == MASTER_GET_SAMPLE_RATE:
            return int(self.rate)
        if opcode == MASTER_GET_BLOCK_SIZE:
            return int(self.block)
        if opcode == MASTER_GET_CURRENT_PROCESS_LEVEL:
            return PROCESS_LEVEL_OFFLINE
        if opcode in (MASTER_GET_VENDOR_STRING, MASTER_GET_PRODUCT_STRING) and ptr:
            ctypes.memmove(ptr, b"Location Sound File Manager\0", 28)
            return 1
        if opcode == MASTER_GET_VENDOR_VERSION:
            return 1
        if opcode == MASTER_GET_LANGUAGE:
            return 1  # English
        if opcode == MASTER_CAN_DO and ptr:
            asked = ctypes.cast(ptr, ctypes.c_char_p).value or b""
            return 1 if asked in (b"supplyIdle", b"offline", b"sendVstEvents") else 0
        return 0  # automate, idle, getTime (no transport), anything else

    def _call(self, opcode: int, index: int = 0, value: int = 0, ptr=None, opt: float = 0.0) -> int:
        return self.e.dispatcher(self.effect, opcode, index, value, ptr, opt)

    def _string(self, opcode: int, index: int = 0, size: int = 256) -> str:
        buffer = ctypes.create_string_buffer(size)
        self._call(opcode, index, 0, ctypes.cast(buffer, ctypes.c_void_p))
        return buffer.value.decode("latin-1", "replace").strip()

    # -------------------------------------------------------------- facts

    @property
    def name(self) -> str:
        return (self._string(EFF_GET_EFFECT_NAME) or self._string(EFF_GET_PRODUCT_STRING)
                or os.path.splitext(os.path.basename(self.path))[0])

    @property
    def vendor(self) -> str:
        return self._string(EFF_GET_VENDOR_STRING)

    @property
    def latency(self) -> int:
        return max(0, int(self.e.initialDelay))

    @property
    def channels(self) -> tuple[int, int]:
        return self.e.numInputs, self.e.numOutputs

    def parameters(self) -> list[dict]:
        out = []
        for i in range(self.e.numParams):
            out.append({"index": i, "name": self._string(EFF_GET_PARAM_NAME, i, 64) or f"Parameter {i + 1}",
                        "value": float(self.e.getParameter(self.effect, i)),
                        "display": self._string(EFF_GET_PARAM_DISPLAY, i, 64),
                        "label": self._string(EFF_GET_PARAM_LABEL, i, 64)})
        return out

    def set_parameter(self, index: int, value: float) -> None:
        if 0 <= index < self.e.numParams:
            self.e.setParameter(self.effect, index, float(min(1.0, max(0.0, value))))

    def programs(self) -> list[str]:
        return [self._string(EFF_GET_PROGRAM_NAME_INDEXED, i, 64) or f"Program {i + 1}"
                for i in range(self.e.numPrograms)]

    def set_program(self, index: int) -> None:
        self._call(EFF_SET_PROGRAM, 0, index)

    # -------------------------------------------------------------- state

    def get_state(self) -> bytes:
        """The plug-in's state: its chunk if it has one, else the parameter values."""
        if self.e.flags & FLAG_PROGRAM_CHUNKS:
            data = ctypes.c_void_p()
            size = self._call(EFF_GET_CHUNK, 0, 0, ctypes.byref(data))
            if size > 0 and data.value:
                return b"C" + ctypes.string_at(data.value, size)
        values = np.array([self.e.getParameter(self.effect, i) for i in range(self.e.numParams)], "<f4")
        return b"P" + values.tobytes()

    def set_state(self, state: bytes) -> None:
        if not state:
            return
        kind, body = state[:1], state[1:]
        if kind == b"C" and self.e.flags & FLAG_PROGRAM_CHUNKS:
            buffer = ctypes.create_string_buffer(body, len(body))
            self._call(EFF_SET_CHUNK, 0, len(body), ctypes.cast(buffer, ctypes.c_void_p))
        elif kind == b"P":
            for i, value in enumerate(np.frombuffer(body, "<f4")[:self.e.numParams]):
                self.set_parameter(i, float(value))

    # -------------------------------------------------------------- audio

    def suspend(self) -> None:
        self._call(EFF_STOP_PROCESS)
        self._call(EFF_MAINS_CHANGED, 0, 0)

    def resume(self) -> None:
        self._call(EFF_MAINS_CHANGED, 0, 1)
        self._call(EFF_START_PROCESS)

    def reset(self) -> None:
        """Clear the plug-in's memory (reverb tails, filters) before new audio."""
        self.suspend()
        self.resume()

    def process(self, mono: np.ndarray) -> np.ndarray:
        """Mono floats through the plug-in: the same signal goes to every
        input, the outputs (at most the first two) are averaged. The result
        is as delayed as the plug-in's latency; callers compensate."""
        out = np.empty(len(mono), np.float32)
        ins = len(self._ins)
        pick = self._outs[:2]
        for start in range(0, len(mono), self.block):
            count = min(self.block, len(mono) - start)
            for buffer in self._ins[:ins]:
                buffer[:count] = mono[start:start + count]
            for buffer in self._outs:
                buffer[:count] = 0
            self.e.processReplacing(self.effect, self._in_ptrs, self._out_ptrs, count)
            out[start:start + count] = sum(b[:count] for b in pick) / len(pick)
        return out

    def close(self) -> None:
        if getattr(self, "effect", None):
            try:
                self.suspend()
                self._call(EFF_CLOSE)
            finally:
                self.effect = None
