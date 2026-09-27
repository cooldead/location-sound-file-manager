"""Entry point for the Windows build (PyInstaller needs a script, and
sound_file_manager/__main__.py uses package-relative imports)."""

import sys

from sound_file_manager.__main__ import main

sys.exit(main())
