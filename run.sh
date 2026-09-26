#!/bin/sh
# Launch Location Sound File Manager from its project folder. Optional argument: the library folder.
here="$(dirname "$(readlink -f "$0")")"
# A project venv (made by macos/build_app.sh, or by hand) is used when there is one.
python="python3"
[ -x "$here/.venv/bin/python" ] && python="$here/.venv/bin/python"
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec "$python" -m sound_file_manager "$@"
