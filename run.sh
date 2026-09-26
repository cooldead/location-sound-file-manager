#!/bin/sh
# Launch Location Sound File Manager from its project folder. Optional argument: the library folder.
here="$(dirname "$(readlink -f "$0")")"
PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m sound_file_manager "$@"
