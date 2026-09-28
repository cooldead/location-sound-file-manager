#!/bin/sh
# Launch Location Sound File Manager from its project folder. Optional argument: the library folder.
here="$(dirname "$(readlink -f "$0")")"
# Prefer a complete project venv; a build-only venv may lack the GUI packages.
python="python3"
if [ -x "$here/.venv/bin/python" ] &&
    "$here/.venv/bin/python" -c 'import PySide6.QtWidgets, PySide6.QtMultimedia, numpy' >/dev/null 2>&1; then
    python="$here/.venv/bin/python"
fi
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "$python" -m sound_file_manager "$@"
