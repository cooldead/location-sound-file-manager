#!/bin/sh
# Build the Linux release files into dist/linux/:
#   Location-Sound-File-Manager-X.Y.Z-linux.tar.gz   the app (source, ready to run)
#   location-sound-file-manager-X.Y.Z-1-any.pkg.tar.zst   Arch package (needs makepkg)
#   Location-Sound-File-Manager-X.Y.Z-x86_64.AppImage     everything inside
# Usage: linux/build_packages.sh [X.Y.Z] [--no-arch] [--no-appimage]
# The code comes from the tag vX.Y.Z-linux when it exists, else from HEAD.
# An AppImage runs on distros whose glibc is at least the build machine's:
# release AppImages are built on Ubuntu 22.04 by .github/workflows/linux-packages.yml.
set -e
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"
version="" arch=1 appimage=1
for arg in "$@"; do
    case "$arg" in
        --no-arch) arch=0 ;;
        --no-appimage) appimage=0 ;;
        *) version="$arg" ;;
    esac
done
[ -n "$version" ] || version="$(git describe --tags --abbrev=0 2>/dev/null | sed -e 's/^v//' -e 's/-linux$//')"
name="location-sound-file-manager"
build="$here/build/linux"
dist="$here/dist/linux"
mkdir -p "$build" "$dist"

# 1. The tarball (the same layout as the release asset).
ref="HEAD"
git rev-parse -q --verify "refs/tags/v$version-linux" >/dev/null && ref="v$version-linux"
tarball="Location-Sound-File-Manager-$version-linux.tar.gz"
git archive --format=tar.gz --prefix="$name-$version/" -o "$dist/$tarball" "$ref" -- . ':(exclude)macos'
(cd "$dist" && sha256sum "$tarball" > "$tarball.sha256")
echo "Built: $dist/$tarball (from $ref)"

# 2. Arch package, from that tarball.
if [ "$arch" = 1 ] && command -v makepkg >/dev/null; then
    rm -rf "$build/arch" && mkdir -p "$build/arch"
    sum="$(cut -d' ' -f1 "$dist/$tarball.sha256")"
    sed -e "s/^pkgver=.*/pkgver=$version/" -e "s/^sha256sums=.*/sha256sums=('$sum')/" linux/PKGBUILD > "$build/arch/PKGBUILD"
    cp "$dist/$tarball" "$build/arch/"
    (cd "$build/arch" && PKGDEST="$dist" makepkg -f --nodeps --noconfirm >/dev/null)
    cp "$build/arch/PKGBUILD" "$dist/PKGBUILD"
    echo "Built: $(ls "$dist"/$name-$version-*.pkg.tar.zst)"
fi

# 3. AppImage: PyInstaller (Python, Qt, numpy inside) in an AppDir.
if [ "$appimage" = 1 ]; then
    rm -rf "$build/src" && mkdir -p "$build/src"
    tar xzf "$dist/$tarball" -C "$build/src"
    src="$build/src/$name-$version"
    python3 -m venv "$build/venv"
    "$build/venv/bin/pip" install --quiet --upgrade pip
    "$build/venv/bin/pip" install --quiet PySide6 numpy pyinstaller
    "$build/venv/bin/pyinstaller" --noconfirm --clean --windowed --name "$name" \
        --add-data "$src/sound_file_manager/assets:sound_file_manager/assets" --paths "$src" \
        --workpath "$build/work" --specpath "$build" --distpath "$build/pyinstaller" \
        --log-level WARN "$here/linux/launcher.py"
    appdir="$build/AppDir"
    rm -rf "$appdir" && mkdir -p "$appdir/usr/lib" "$appdir/usr/share/applications" \
        "$appdir/usr/share/icons/hicolor/256x256/apps"
    cp -a "$build/pyinstaller/$name" "$appdir/usr/lib/$name"
    sed -e "s|^Exec=.*|Exec=$name|" -e "s|^Icon=.*|Icon=$name|" "$src/$name.desktop" > "$appdir/$name.desktop"
    cp "$appdir/$name.desktop" "$appdir/usr/share/applications/"
    cp "$src/sound_file_manager/assets/icon.png" "$appdir/$name.png"
    cp "$src/sound_file_manager/assets/icon.png" "$appdir/usr/share/icons/hicolor/256x256/apps/$name.png"
    ln -sf "$name.png" "$appdir/.DirIcon"
    printf '%s\n' '#!/bin/sh' 'here="$(dirname "$(readlink -f "$0")")"' \
        "exec \"\$here/usr/lib/$name/$name\" \"\$@\"" > "$appdir/AppRun"
    chmod 755 "$appdir/AppRun"
    tool="$build/appimagetool-x86_64.AppImage"
    if [ ! -x "$tool" ]; then
        curl -fsSL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
        chmod 755 "$tool"
    fi
    out="$dist/Location-Sound-File-Manager-$version-x86_64.AppImage"
    ARCH=x86_64 "$tool" --appimage-extract-and-run --no-appstream "$appdir" "$out" >/dev/null 2>&1
    (cd "$dist" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
    echo "Built: $out ($(du -h "$out" | cut -f1))"
fi
