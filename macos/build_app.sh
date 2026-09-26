#!/bin/sh
# Build "Location Sound File Manager.app" (self-contained: Python, Qt and numpy
# inside) with PyInstaller. Needs Python 3.11+ (e.g. `brew install python`).
#   macos/build_app.sh            build into dist/
#   macos/build_app.sh --install  also copy it to ~/Applications
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
name="Location Sound File Manager"
version="$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//')"
version="${version:-1.3.0}"
build="$here/build/macos"

# A private venv, so the system/Homebrew Python stays untouched.
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet PySide6 numpy pyinstaller

# The app icon: an .icns from the 256 px PNG.
mkdir -p "$build/icon.iconset"
for size in 16 32 64 128 256 512; do
    sips -z $size $size sound_file_manager/assets/icon.png --out "$build/icon.iconset/icon_${size}x${size}.png" >/dev/null
done
for size in 16 32 128 256; do
    cp "$build/icon.iconset/icon_$((size * 2))x$((size * 2)).png" "$build/icon.iconset/icon_${size}x${size}@2x.png"
done
rm "$build/icon.iconset/icon_64x64.png"
iconutil -c icns "$build/icon.iconset" -o "$build/icon.icns"

.venv/bin/pyinstaller --noconfirm --clean --windowed \
    --name "$name" \
    --icon "$build/icon.icns" \
    --osx-bundle-identifier io.github.cooldead.location-sound-file-manager \
    --add-data "$here/sound_file_manager/assets:sound_file_manager/assets" --paths "$here" \
    --workpath "$build/work" --specpath "$build" --distpath "$here/dist" \
    "$here/macos/launcher.py"

# Info.plist: version, and the reasons macOS shows when the app first reads
# a card (removable volume) or the library (network volume).
plist="$here/dist/$name.app/Contents/Info.plist"
set_key() { plutil -replace "$1" -string "$2" "$plist"; }
set_key CFBundleShortVersionString "$version"
set_key CFBundleVersion "$version"
set_key NSRemovableVolumesUsageDescription "Offloading reads the recordings on your recorder's card."
set_key NSNetworkVolumesUsageDescription "Your sound library can be on a network share."
set_key LSMinimumSystemVersion "12.0"
plutil -replace NSHighResolutionCapable -bool true "$plist"
# Changing Info.plist breaks PyInstaller's ad-hoc signature; sign again.
codesign --force --deep --sign - "$here/dist/$name.app"

echo "Built: $here/dist/$name.app"
if [ "$1" = "--install" ]; then
    mkdir -p "$HOME/Applications"
    rm -rf "$HOME/Applications/$name.app"
    cp -R "$here/dist/$name.app" "$HOME/Applications/"
    echo "Installed: $HOME/Applications/$name.app"
fi
