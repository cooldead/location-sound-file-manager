import os
import sys
import tempfile

# Report tests render PDFs with Qt; never open windows on the desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if sys.platform == "win32":
    # The app's paths use "/" on Windows too (sound_file_manager.compat), as
    # they come from Qt's dialogs; test folders are made the same way.
    from sound_file_manager import compat

    # The offscreen platform on Windows has no fonts of its own (every glyph
    # would measure the same), which breaks the report column layout tests.
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

    _mkdtemp = tempfile.mkdtemp
    tempfile.mkdtemp = lambda *args, **kwargs: compat.fwd(_mkdtemp(*args, **kwargs))


def qt_app():
    """One QApplication for every test module. A bare QGuiApplication made by
    one test and then used for widgets by another corrupts memory."""
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def dispose(test, widget):
    """Delete a widget when the test ends. Dialogs sit in reference cycles
    (timers, signal lambdas), so without this Python would only free them at
    interpreter exit, after the QApplication is gone, and crash."""
    import shiboken6

    def delete():
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
    test.addCleanup(delete)
    return widget
