"""Build/install the optional Rust waveform library using the host Rust toolchain."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
name = {"win32": "sfm_waveform.dll", "darwin": "libsfm_waveform.dylib"}.get(sys.platform, "libsfm_waveform.so")
target = root / "build" / "rust"
command = ["cargo"]
if os.environ.get("SFM_RUST_TOOLCHAIN"):
    command.append("+" + os.environ["SFM_RUST_TOOLCHAIN"])
subprocess.run([*command, "build", "--release", "--offline", "--locked",
                "--manifest-path", str(root / "native/waveform/Cargo.toml"),
                "--target-dir", str(target)], check=True)
destination = root / "sound_file_manager" / "_native"
destination.mkdir(exist_ok=True)
shutil.copy2(target / "release" / name, destination / name)
print(f"Built {destination / name}")
