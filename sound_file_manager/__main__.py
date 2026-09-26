import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import settings
from .main_window import APP_NAME, MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("location-sound-file-manager")
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("location-sound-file-manager")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "icon.png")))

    library = None
    args = [a for a in app.arguments()[1:] if not a.startswith("-")]
    if args:
        path = Path(args[0]).expanduser().resolve()
        library = str(path if path.is_dir() else path.parent)

    settings.migrate_old_folders()
    window = MainWindow(library)
    window.show()
    code = app.exec()
    # Free the windows while Qt is still running (not during interpreter exit).
    del window
    import gc
    gc.collect()
    return code


if __name__ == "__main__":
    sys.exit(main())
