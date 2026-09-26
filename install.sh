#!/bin/sh
# Add Location Sound File Manager to the application menu (for this user).
set -e
here="$(dirname "$(readlink -f "$0")")"
target="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$target"
sed "s|@APPDIR@|$here|g" "$here/location-sound-file-manager.desktop" > "$target/location-sound-file-manager.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$target" 2>/dev/null || true
echo "Installed: $target/location-sound-file-manager.desktop"
