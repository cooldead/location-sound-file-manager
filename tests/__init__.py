import os

# Report tests render PDFs with Qt; never open windows on the desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
