import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import compat, settings
from .main_window import APP_NAME, MainWindow


def main() -> int:
    if compat.WINDOWS:
        # Our own taskbar button and icon, not python.exe's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("io.github.cooldead.location-sound-file-manager")
    app = QApplication(sys.argv)
    app.setApplicationName("location-sound-file-manager")
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("location-sound-file-manager")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "icon.png")))

    library = None
    args = [a for a in app.arguments()[1:] if not a.startswith("-")]
    if args:
        path = Path(args[0]).expanduser().resolve()
        library = compat.fwd(path if path.is_dir() else path.parent)

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
