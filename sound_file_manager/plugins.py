"""Audio plug-ins (VST2 and VST3) for processed copies. Nothing in here uses Qt.

Plug-ins are other people's machine code, and a crashing one would take the
app with it, so they are only ever loaded in a helper process
(``plugin_host.py``, started as ``<app> --plugin-host``). This module finds
plug-in files (without loading them), talks to the helper, and installs the
VST3 add-on.

- **VST2** is hosted by our own small host (``vst2.py``): no download.
- **VST3** is hosted through *pedalboard* (Spotify, GPL-3.0), which the user
  downloads from the window as an add-on: the wheel for this Python and
  computer from PyPI, checked against PyPI's SHA-256, unpacked into the app's
  data folder. It isn't bundled, so the app's own builds stay MIT.

A chain is a list of ``ChainEntry`` (format, path, name, state): each track
to process runs through its own instances of the chain, in order, as mono.
The chain's latency is taken off, so the copy lines up with the original.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np

from . import compat

FORMATS = ("vst2", "vst3")
PEDALBOARD_VERSION = "0.9.25"
PYPI_URL = f"https://pypi.org/pypi/pedalboard/{PEDALBOARD_VERSION}/json"


class PluginError(Exception):
    pass


@dataclass
class ChainEntry:
    format: str                       # "vst2" or "vst3"
    path: str
    name: str = ""
    state: str = ""                   # base64 of the plug-in's saved state ("" = its defaults)
    bypass: bool = False

    def label(self) -> str:
        return f"{self.name or os.path.basename(self.path)} ({self.format.upper()})"


def chain_to_json(chain: list[ChainEntry]) -> str:
    return json.dumps([asdict(e) for e in chain])


def chain_from_json(text: str) -> list[ChainEntry]:
    try:
        items = json.loads(text or "[]")
        return [ChainEntry(**{k: v for k, v in item.items() if k in ChainEntry.__dataclass_fields__})
                for item in items if item.get("format") in FORMATS and item.get("path")]
    except (ValueError, TypeError, AttributeError):
        return []


def describe_chain(chain: list[ChainEntry]) -> str:
    used = [e for e in chain if not e.bypass]
    return " > ".join(e.label() for e in used) if used else "no plug-ins"


# ------------------------------------------------------------------ finding plug-ins

def default_folders() -> dict[str, list[str]]:
    """The usual plug-in folders of this system, per format (existing or not)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return {"vst2": ["/Library/Audio/Plug-Ins/VST", f"{home}/Library/Audio/Plug-Ins/VST"],
                "vst3": ["/Library/Audio/Plug-Ins/VST3", f"{home}/Library/Audio/Plug-Ins/VST3"]}
    if sys.platform == "win32":
        programs = os.environ.get("ProgramFiles", "C:/Program Files")
        common = os.environ.get("CommonProgramFiles", f"{programs}/Common Files")
        return {"vst2": [compat.fwd(p) for p in (f"{programs}/VSTPlugins", f"{programs}/Steinberg/VSTPlugins",
                                                 f"{common}/VST2", f"{common}/Steinberg/VST2")],
                "vst3": [compat.fwd(f"{common}/VST3")]}
    vst = [p for p in os.environ.get("VST_PATH", "").split(":") if p]
    vst3 = [p for p in os.environ.get("VST3_PATH", "").split(":") if p]
    return {"vst2": vst + [f"{home}/.vst", "/usr/lib/vst", "/usr/local/lib/vst", "/usr/lib64/vst"],
            "vst3": vst3 + [f"{home}/.vst3", "/usr/lib/vst3", "/usr/local/lib/vst3", "/usr/lib64/vst3"]}


VST2_SUFFIX = {"darwin": ".vst", "win32": ".dll"}.get(sys.platform, ".so")


def find_candidates(extra_folders: list[str] = (), depth: int = 4) -> list[tuple[str, str]]:
    """(format, path) of plug-in files below the usual folders and extra_folders.
    Nothing is loaded: a ``.vst3`` is a VST3; a ``.so``/``.dll``/``.vst`` may
    be a VST2 (probe it to be sure). Inside a .vst3 bundle nothing more is
    looked at."""
    folders = default_folders()
    seen, found = set(), []
    roots = [("vst2", f) for f in folders["vst2"]] + [("vst3", f) for f in folders["vst3"]] + \
        [(None, f) for f in extra_folders]
    for wanted, root in roots:
        if not os.path.isdir(root):
            continue
        base = root.rstrip("/").count("/")
        for folder, dirs, files in os.walk(root, followlinks=True):
            if folder.count("/") - base >= depth:
                dirs[:] = []
            entries = [(d, True) for d in dirs] + [(f, False) for f in files]
            keep_dirs = []
            for name, is_dir in entries:
                path = compat.fwd(os.path.join(folder, name))
                lower = name.lower()
                kind = None
                if lower.endswith(".vst3") and wanted in (None, "vst3"):
                    kind = "vst3"
                elif lower.endswith(VST2_SUFFIX) and wanted in (None, "vst2") and \
                        (is_dir == (VST2_SUFFIX == ".vst")) and not _other_format(lower):
                    kind = "vst2"
                if kind:
                    real = os.path.realpath(path)
                    if real not in seen:
                        seen.add(real)
                        found.append((kind, path))
                elif is_dir and not lower.endswith((".vst3", ".vst", ".lv2", ".clap", ".component")):
                    keep_dirs.append(name)
            dirs[:] = keep_dirs
    return sorted(found, key=lambda item: (os.path.basename(item[1]).lower(), item[0]))


def _other_format(lower_name: str) -> bool:
    """Shared libraries that are clearly another plug-in format (Linux packs ship them side by side)."""
    return any(tag in lower_name for tag in ("-ladspa", "-dssi", "_ladspa", "_dssi", "-lv2", ".lv2"))


# ------------------------------------------------------------------ the VST3 add-on (pedalboard)

def _wheel_tags() -> tuple[str, tuple[str, ...]] | None:
    """(python tag, platform words) of the pedalboard wheel for this interpreter."""
    python = f"cp{sys.version_info.major}{sys.version_info.minor}"
    machine = platform.machine().lower()
    if sys.platform == "linux":
        arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine)
        return (python, ("manylinux", arch)) if arch else None
    if sys.platform == "darwin":
        return python, ("macosx", "arm64" if machine in ("arm64", "aarch64") else "x86_64")
    if sys.platform == "win32" and machine in ("amd64", "x86_64"):
        return python, ("win_amd64",)
    return None


def pedalboard_dir(data_dir: str) -> str | None:
    tags = _wheel_tags()
    if tags is None:
        return None
    return compat.join(data_dir, "addons", f"pedalboard-{PEDALBOARD_VERSION}-{tags[0]}-{'-'.join(tags[1])}")


def pedalboard_installed(data_dir: str) -> str | None:
    """The folder to put on sys.path when the VST3 add-on is installed, else None."""
    folder = pedalboard_dir(data_dir)
    return folder if folder and os.path.isfile(compat.join(folder, "pedalboard", "__init__.py")) else None


def choose_wheel(files: list[dict]) -> dict | None:
    """The wheel (a PyPI "urls" entry) for this interpreter and computer."""
    tags = _wheel_tags()
    if tags is None:
        return None
    python, words = tags
    for item in files:
        name = item.get("filename", "")
        if not name.endswith(".whl") or f"-{python}-{python}-" not in name:
            continue  # also skips free-threaded "cp314t" builds
        if all(word in name for word in words):
            return item
    return None


def install_pedalboard(data_dir: str, progress: Callable[[int, int], None] | None = None,
                       cancelled: Callable[[], bool] | None = None,
                       opener: Callable[[str], object] = urllib.request.urlopen) -> str:
    """Download and unpack pedalboard for this Python; checked against the
    SHA-256 PyPI lists. Returns the folder. A failure leaves nothing."""
    with opener(PYPI_URL) as response:
        info = json.loads(response.read().decode("utf-8"))
    wheel = choose_wheel(info.get("urls", []))
    if wheel is None:
        raise PluginError(f"pedalboard {PEDALBOARD_VERSION} has no build for this computer "
                          f"(Python {sys.version_info.major}.{sys.version_info.minor}, {platform.machine()}).")
    folder = pedalboard_dir(data_dir)
    temp = folder + ".part"
    shutil.rmtree(temp, ignore_errors=True)
    os.makedirs(temp)
    archive = compat.join(temp, wheel["filename"])
    try:
        digest = hashlib.sha256()
        with opener(wheel["url"]) as response, open(archive, "wb") as out:
            done, size = 0, int(wheel.get("size") or 0)
            while True:
                if cancelled and cancelled():
                    raise InterruptedError
                piece = response.read(1 << 18)
                if not piece:
                    break
                out.write(piece)
                digest.update(piece)
                done += len(piece)
                if progress:
                    progress(done, size)
        if digest.hexdigest() != wheel.get("digests", {}).get("sha256"):
            raise PluginError("The download didn't match the checksum PyPI lists for it, so it was thrown away.")
        with zipfile.ZipFile(archive) as whl:
            for member in whl.namelist():
                target = os.path.realpath(os.path.join(temp, member))
                if not target.startswith(os.path.realpath(temp) + os.sep):
                    raise PluginError("The download has unexpected paths in it.")
            whl.extractall(temp)
        os.remove(archive)
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(temp, folder)
        return folder
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def uninstall_pedalboard(data_dir: str) -> None:
    folder = pedalboard_dir(data_dir)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


# ------------------------------------------------------------------ the helper process

def host_command() -> list[str]:
    """How to start the helper: the frozen app runs itself with --plugin-host."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--plugin-host"]
    return [sys.executable, "-m", "sound_file_manager", "--plugin-host"]


class HostDied(PluginError):
    pass


class HostClient:
    """One helper process. Requests are JSON lines; replies are JSON lines
    (progress lines first for long jobs). If the helper dies, the request
    raises HostDied and the next request starts a new helper."""

    def __init__(self, data_dir: str, command: list[str] | None = None):
        self.data_dir, self.command = data_dir, command or host_command()
        self.child: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.stderr_tail: list[str] = []

    @staticmethod
    def _discard(child: subprocess.Popen | None) -> None:
        if child is None:
            return
        if child.poll() is None:
            child.kill()
        child.wait()
        for pipe in (child.stdin, child.stdout, child.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def _start(self):
        env = dict(os.environ)
        board = pedalboard_installed(self.data_dir)
        if board:
            env["SFM_PEDALBOARD"] = board
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.child = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, bufsize=1, env=env, creationflags=flags)
        self.stderr_tail = []
        threading.Thread(target=self._drain, args=(self.child,), daemon=True).start()

    def _drain(self, child):
        for line in child.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def request(self, message: dict, progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> dict:
        with self.lock:
            if self.child is None or self.child.poll() is not None:
                self._discard(self.child)
                self._start()
            child = self.child
            try:
                child.stdin.write(json.dumps(message) + "\n")
                child.stdin.flush()
            except OSError:
                self._discard(child)
                self.child = None
                raise HostDied(self._died_message())
            watcher = None
            if cancelled is not None:
                stop = threading.Event()

                def watch():
                    while not stop.wait(0.1):
                        if cancelled():
                            child.kill()
                            return
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
            try:
                while True:
                    line = child.stdout.readline()
                    if not line:
                        self._discard(child)
                        self.child = None
                        if cancelled is not None and cancelled():
                            raise InterruptedError
                        raise HostDied(self._died_message())
                    reply = json.loads(line)
                    if "progress" in reply:
                        if progress:
                            progress(*reply["progress"])
                        continue
                    if reply.get("error"):
                        raise PluginError(reply["error"])
                    return reply
            finally:
                if watcher is not None:
                    stop.set()

    def _died_message(self) -> str:
        detail = next((line for line in reversed(self.stderr_tail) if line.strip()), "")
        return "The plug-in crashed (the helper process stopped)" + (f": {detail}" if detail else ".")

    def close(self):
        with self.lock:
            if self.child is not None and self.child.poll() is None:
                try:
                    self.child.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    self.child.stdin.flush()
                    self.child.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._discard(self.child)
            self.child = None


def probe_all(client: HostClient, candidates: list[tuple[str, str]],
              progress: Callable[[int, int], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> tuple[list[dict], list[str]]:
    """Load each candidate once in the helper to read its name. Returns the
    plug-ins that work ({format, path, name, vendor, inputs, outputs}) and
    one line per file that didn't (a crash only loses that file)."""
    good, bad = [], []
    for index, (kind, path) in enumerate(candidates):
        if cancelled and cancelled():
            raise InterruptedError
        try:
            reply = client.request({"cmd": "probe", "format": kind, "path": path})
            good.append({"format": kind, "path": path, "name": reply["name"], "vendor": reply.get("vendor", ""),
                         "inputs": reply.get("inputs", 0), "outputs": reply.get("outputs", 0)})
        except PluginError as error:
            bad.append(f"{os.path.basename(path)}: {error}")
        if progress:
            progress(index + 1, len(candidates))
    return good, bad


# ------------------------------------------------------------------ inside the helper

class VST3Plugin:
    """A VST3 through pedalboard, with the same calls as vst2.Plugin."""

    def __init__(self, path: str, rate: int = 48000):
        import pedalboard  # the add-on, on sys.path in the helper
        self.path, self.rate = path, rate
        try:
            self.plugin = pedalboard.load_plugin(path)
        except Exception as error:  # noqa: BLE001 - pedalboard raises plain exceptions
            raise PluginError(f"can't load {os.path.basename(path)}: {error}") from error
        if getattr(self.plugin, "is_instrument", False):
            raise PluginError(f"{os.path.basename(path)} is an instrument, not an effect")
        self._channels: int | None = None
        self._keys = [k for k in self.plugin.parameters if k not in ("buffer_size_frames", "sample_rate_frames")]

    name = property(lambda self: self.plugin.name)
    vendor = property(lambda self: getattr(self.plugin, "manufacturer_name", "") or "")
    latency = property(lambda self: int(getattr(self.plugin, "reported_latency_samples", 0) or 0))
    channels = property(lambda self: (self._channels or 2, self._channels or 2))

    def parameters(self) -> list[dict]:
        out = []
        for key in self._keys:
            p = self.plugin.parameters[key]
            out.append({"index": key, "name": getattr(p, "name", key), "value": float(p.raw_value),
                        "display": str(getattr(p, "string_value", "")), "label": getattr(p, "label", "") or ""})
        return out

    def set_parameter(self, key, value: float) -> None:
        if key in self.plugin.parameters:
            self.plugin.parameters[key].raw_value = float(min(1.0, max(0.0, value)))

    def get_state(self) -> bytes:
        return bytes(self.plugin.raw_state)

    def set_state(self, state: bytes) -> None:
        if state:
            self.plugin.raw_state = state

    def reset(self) -> None:
        self.plugin.reset()

    def process(self, mono: np.ndarray) -> np.ndarray:
        for channels in ([self._channels] if self._channels else [2, 1]):
            data = np.repeat(np.asarray(mono, np.float32)[None, :], channels, axis=0)
            try:
                out = self.plugin.process(data, float(self.rate), reset=False)
            except ValueError:
                if self._channels:
                    raise
                continue
            self._channels = channels
            return np.asarray(out[:2], np.float32).mean(axis=0)
        raise PluginError(f"{self.name} takes neither mono nor stereo audio")

    def close(self) -> None:
        self.plugin = None


def open_plugin(kind: str, path: str, rate: int = 48000):
    if kind == "vst2":
        from . import vst2
        try:
            return vst2.Plugin(path, rate)
        except vst2.VstError as error:
            raise PluginError(str(error)) from error
    if kind == "vst3":
        try:
            import pedalboard  # noqa: F401
        except ImportError as error:
            raise PluginError("VST3 needs the VST3 add-on (install it in the plug-in window)") from error
        return VST3Plugin(path, rate)
    raise PluginError(f"unknown plug-in format {kind!r}")


class ChainEngine:
    """Runs a chain over a recording's blocks, per track (mono), in order:
    each track gets its own instances, with their saved states. The chain's
    latency is taken off: input is fed ahead by that much (zeros past the
    end), so frames [a, b) come out lined up with the original."""

    block_frames = 1 << 18

    def __init__(self, chain: list[ChainEntry], rate: int):
        self.chain = [e for e in chain if not e.bypass]
        self.rate = rate
        self.instances: dict[int, list] = {}
        self.fed: dict[int, int] = {}
        self.pending: dict[int, np.ndarray] = {}    # output already made, from frame `start` on
        self.start: dict[int, int] = {}

    def _instances(self, channel: int) -> list:
        if channel not in self.instances:
            made = []
            for entry in self.chain:
                plugin = open_plugin(entry.format, entry.path, self.rate)
                plugin.set_state(base64.b64decode(entry.state) if entry.state else b"")
                plugin.reset()
                made.append(plugin)
            self.instances[channel] = made
        return self.instances[channel]

    def latency(self, channel: int) -> int:
        return sum(p.latency for p in self._instances(channel))

    def process(self, read: Callable[[int, int], np.ndarray], a: int, b: int, channel: int) -> np.ndarray:
        plugins = self._instances(channel)
        delay = self.latency(channel)
        if channel not in self.fed:
            # Start at a: the first `delay` output frames are the plug-ins' latency, thrown away.
            self.fed[channel], self.start[channel] = a, a - delay
            self.pending[channel] = np.zeros(0, np.float32)
        fed = self.fed[channel]
        if b + delay > fed:
            signal = read(fed, b + delay)[:, channel]
            for plugin in plugins:
                signal = plugin.process(signal)
            self.pending[channel] = np.concatenate([self.pending[channel], signal])
            self.fed[channel] = b + delay
        offset = a - self.start[channel]
        out = self.pending[channel][offset:offset + (b - a)]
        self.pending[channel] = self.pending[channel][offset + (b - a):]
        self.start[channel] = b
        return out.astype(np.float32)

    def close(self) -> None:
        for plugins in self.instances.values():
            for plugin in plugins:
                try:
                    plugin.close()
                except Exception:  # noqa: BLE001 - closing must not hide the real result
                    pass
        self.instances.clear()
