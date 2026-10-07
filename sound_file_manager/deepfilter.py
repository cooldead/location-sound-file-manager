"""The DeepFilterNet add-on: speech enhancement by a trained model, for noise a
learned profile can't catch (clothing rustle on lavs, changing noise).
Nothing in here uses Qt.

The app doesn't ship it. The user installs it from the Reduce Noise window:
the standalone ``deep-filter`` program of DeepFilterNet v0.5.6 (MIT or
Apache-2.0, https://github.com/Rikorose/DeepFilterNet) is downloaded from its
GitHub release into the app's data folder and checked against the SHA-256
below before it is ever run. Nothing else is downloaded.

How the program is used, and why (measured on v0.5.6):

- It only takes 48 kHz audio, and it can't read 24-bit WAVs (it panics with
  "TooWide"); 32-bit float works. It always writes 16-bit. So each block goes
  in as float, raised so its peak sits just under full scale, and comes back
  lowered by the same amount: the 16-bit steps then stay ~96 dB below the
  block's peak, as for the recording itself.
- With several channels in one file it uses one mask for all of them, so
  each track is run on its own.
- ``-D`` (compensate delay) lines the output up with the input but drops the
  last 1440 samples (30 ms), so the input gets that much silence added.
- ``-a`` (attenuation limit) mixes some of the original back in, which is
  the window's "reduce by up to".
- The model has slow state (running normalisation), so long files are run
  in blocks of five minutes, each with the 10 s of audio before it to settle
  in (thrown away), and 20 ms faded across into the next block. Measured on a
  real lav track: two seconds left the half second after a join about 20 dB
  noisier than one pass (-65 vs -85 dBFS); ten seconds brings that to about
  -75 dBFS. One pass is deterministic. A whole file in one pass would need
  its full length as a temporary file and couldn't show progress.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from typing import Callable

import numpy as np

from . import bwf, compat, splitter, waveform

VERSION = "0.5.6"
RATE = 48000
URL = "https://github.com/Rikorose/DeepFilterNet/releases/download/v{version}/{name}"
# Release asset and its SHA-256, per (sys.platform, machine). Checked after
# every download; the release page publishes no checksums of its own.
ASSETS = {
    ("linux", "x86_64"): ("deep-filter-0.5.6-x86_64-unknown-linux-musl",
                          "70775e251eee44c0f2451a1e833326cf8bcbbe304d3e7cd12851e6fce72ef7da", 36417296),
    ("linux", "aarch64"): ("deep-filter-0.5.6-aarch64-unknown-linux-gnu",
                           "14e02a1c0028f3ca0bdf83b62b3336e56ba0556894ef295a95e8573f06557166", 39238496),
    ("darwin", "arm64"): ("deep-filter-0.5.6-aarch64-apple-darwin",
                          "4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081", 27877081),
    ("darwin", "x86_64"): ("deep-filter-0.5.6-x86_64-apple-darwin",
                           "d3be84003acb7c23e738ad7f70a158ec779a8d233a82e7fa3e717d112eb5b50f", 29933512),
    ("win32", "amd64"): ("deep-filter-0.5.6-x86_64-pc-windows-msvc.exe",
                         "75e11fa16445f560cb6b021521ddb89e89270d13b83089705d98776f58fd7915", 26912256),
}
DELAY = 1440                     # samples -D drops from the end
PREROLL = 10 * RATE              # audio before a block, for the model to settle (discarded)
FADE = RATE // 50                # 20 ms faded across between blocks
BLOCK_FRAMES = 300 * RATE        # one run of the program per five minutes of a track
HEADROOM = 0.89                  # -1 dBFS: the peak a block is raised to


class AddonError(Exception):
    pass


def asset() -> tuple[str, str, int] | None:
    """(file name, sha256, size) of the program for this computer, or None."""
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine) if sys.platform == "linux" else machine
    if sys.platform == "darwin":
        machine = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    if sys.platform == "win32":
        machine = "amd64" if machine in ("amd64", "x86_64") else machine
    return ASSETS.get((sys.platform, machine))


def addon_dir(data_dir: str) -> str:
    return compat.join(data_dir, "addons", f"deepfilternet-{VERSION}")


def program_path(data_dir: str) -> str:
    return compat.join(addon_dir(data_dir), "deep-filter.exe" if sys.platform == "win32" else "deep-filter")


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for piece in iter(lambda: f.read(1 << 20), b""):
            digest.update(piece)
    return digest.hexdigest()


def installed(data_dir: str) -> str | None:
    """The program's path when it is installed and intact, else None."""
    path, wanted = program_path(data_dir), asset()
    if wanted is None or not os.path.isfile(path) or os.path.getsize(path) != wanted[2]:
        return None
    return path


def install(data_dir: str, progress: Callable[[int, int], None] | None = None,
            cancelled: Callable[[], bool] | None = None,
            opener: Callable[[str], object] = urllib.request.urlopen) -> str:
    """Download the program for this computer, check its SHA-256, and put it
    in place. Returns its path. A failed or cancelled download leaves nothing."""
    wanted = asset()
    if wanted is None:
        raise AddonError(f"DeepFilterNet has no program for this computer ({sys.platform}, {platform.machine()}).")
    name, digest, size = wanted
    folder = addon_dir(data_dir)
    os.makedirs(folder, exist_ok=True)
    final = program_path(data_dir)
    temp = final + ".part"
    try:
        hasher = hashlib.sha256()
        with opener(URL.format(version=VERSION, name=name)) as response, open(temp, "wb") as out:
            done = 0
            while True:
                if cancelled and cancelled():
                    raise InterruptedError
                piece = response.read(1 << 18)
                if not piece:
                    break
                out.write(piece)
                hasher.update(piece)
                done += len(piece)
                if progress:
                    progress(done, size)
        if hasher.hexdigest() != digest:
            raise AddonError("The download didn't match DeepFilterNet's known checksum, so it was thrown away.")
        os.chmod(temp, 0o755)
        os.replace(temp, final)
        return final
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def uninstall(data_dir: str) -> None:
    shutil.rmtree(addon_dir(data_dir), ignore_errors=True)


def _write_float_wav(path: str, samples: np.ndarray) -> None:
    header = splitter._wav_header(3, 1, RATE, 32, len(samples), [], [])
    with open(path, "wb") as f:
        f.write(header)
        f.write(np.ascontiguousarray(samples, "<f4").tobytes())
        if (len(samples) * 4) & 1:
            f.write(b"\0")


def _read_wav(path: str) -> np.ndarray:
    info = bwf.read_info(path)
    with open(path, "rb") as f:
        layout = bwf.read_layout(f, os.fstat(f.fileno()).st_size)
        data = layout.first(b"data")
        f.seek(data.data_offset)
        raw = f.read(data.size)
    return waveform.decode(raw, info.bits, info.channels, info.format_tag == 3)[:, 0]


def enhance(samples: np.ndarray, program: str, atten_lim_db: float, workdir: str,
            cancelled: Callable[[], bool] | None = None) -> np.ndarray:
    """One run of the program on mono 48 kHz floats; same length out. The
    program is stopped at once when cancelled() turns true."""
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak < 1e-9:
        return np.zeros(len(samples), np.float32)
    gain = HEADROOM / peak
    source = compat.join(workdir, "in.wav")
    out_dir = compat.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    _write_float_wav(source, np.concatenate([samples * gain, np.zeros(DELAY, np.float32)]))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = compat.join(workdir, "log.txt")
    with open(log, "w") as log_file:
        child = subprocess.Popen([program, "-D", "-a", f"{max(0.0, atten_lim_db):g}", "-o", out_dir, source],
                                 stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags)
        while True:
            try:
                code = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancelled and cancelled():
                    child.kill()
                    child.wait()
                    raise InterruptedError
    if code != 0:
        with open(log, errors="replace") as f:
            detail = f.read().strip().splitlines()[-1:] or ["no message"]
        raise AddonError(f"DeepFilterNet failed: {detail[0]}")
    out = _read_wav(compat.join(out_dir, "in.wav"))
    if len(out) < len(samples):
        out = np.pad(out, (0, len(samples) - len(out)))
    return (out[:len(samples)] / gain).astype(np.float32)


class Engine:
    """Runs DeepFilterNet over a recording's blocks, one track at a time, in
    order (see the module notes for the pre-roll and the fades)."""

    block_frames = BLOCK_FRAMES

    def __init__(self, program: str, atten_lim_db: float, cancelled: Callable[[], bool] | None = None):
        self.program, self.atten_lim_db, self.cancelled = program, atten_lim_db, cancelled
        self.workdir = tempfile.mkdtemp(prefix="sfm-deepfilter-")
        self.tails: dict[int, tuple[int, np.ndarray]] = {}  # channel -> (frame it starts at, faded-in samples)

    def process(self, read: Callable[[int, int], np.ndarray], a: int, b: int, channel: int) -> np.ndarray:
        """Frames [a, b) of one channel. read(x, y) gives the recording's frames (zeros outside)."""
        x = read(a - PREROLL, b + FADE)[:, channel]
        y = enhance(x, self.program, self.atten_lim_db, self.workdir, self.cancelled)
        out = y[PREROLL:PREROLL + (b - a)].copy()
        tail = self.tails.get(channel)
        if tail is not None and tail[0] == a:
            count = min(len(tail[1]), len(out))
            ramp = np.linspace(0.0, 1.0, count, dtype=np.float32)
            out[:count] = tail[1][:count] * (1 - ramp) + out[:count] * ramp
        self.tails[channel] = (b, y[PREROLL + (b - a):PREROLL + (b - a) + FADE])
        return out

    def close(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)
