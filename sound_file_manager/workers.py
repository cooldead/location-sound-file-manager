"""Background work: the library scan and a generic job with a progress dialog."""

from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QProgressDialog, QWidget

from . import catalog, library_index


class ScanThread(QThread):
    batch = Signal(list)
    progress = Signal(object, str)  # ScanStats, current path
    finished_scan = Signal(object, str)  # ScanStats or None, error

    def __init__(self, root: str, cache_file: str, parent=None, *, write_index: bool = False):
        super().__init__(parent)
        self.root, self.cache_file = root, cache_file
        self.write_index = write_index
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        cache = None
        try:
            cache = catalog.Cache(self.cache_file)
            # An index in the library is always read (that changes nothing);
            # it is only written when the setting allows it.
            index = library_index.LibraryIndex.load(self.root)
            stats = catalog.scan(self.root, cache, on_batch=self.batch.emit,
                                 on_progress=lambda s, p: self.progress.emit(s, p),
                                 cancelled=lambda: self._cancel, index=index)
            if self.write_index and not self._cancel and index.changed:
                try:
                    index.save()
                    stats.index_saved = True
                except OSError as error:
                    stats.index_error = str(error)
            self.finished_scan.emit(stats, "")
        except Exception as error:  # noqa: BLE001
            self.finished_scan.emit(None, f"{error}\n\n{traceback.format_exc()}")
        finally:
            if cache is not None:
                cache.close()


class JobThread(QThread):
    """Runs fn(report) where report(done, total, text) updates the dialog
    (total 0 shows a moving bar). Cancellation is cooperative: fn checks
    job.cancelled."""

    progress = Signal(int, int, str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.result = None
        self.error: BaseException | None = None
        self.cancelled = False

    def run(self):
        try:
            self.result = self.fn(self)
        except BaseException as error:  # noqa: BLE001 - reported by run_job
            self.error = error

    def report(self, done: int, total: int, text: str = ""):
        self.progress.emit(done, total, text)


def run_job(parent: QWidget, title: str, fn: Callable, *, cancellable: bool = True):
    """Run fn(job) in a thread behind a modal progress dialog; returns
    (result, error). The event loop keeps running so the window repaints."""
    dialog = QProgressDialog(title, "Cancel" if cancellable else None, 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumDuration(300)
    dialog.setMinimumWidth(460)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    if not cancellable:
        dialog.setCancelButton(None)
    job = JobThread(fn, parent)

    def on_progress(done, total, text):
        # A total of 0 means "working, amount unknown": a moving (busy) bar.
        dialog.setRange(0, max(total, 0))
        dialog.setValue(min(done, total) if total > 0 else 0)
        if text:
            dialog.setLabelText(text)

    job.progress.connect(on_progress)
    dialog.canceled.connect(lambda: setattr(job, "cancelled", True))
    job.finished.connect(dialog.close)
    # Start from inside the dialog's event loop, so a job that finishes
    # instantly still closes the dialog instead of leaving it open.
    QTimer.singleShot(0, job.start)
    dialog.exec()
    job.wait()
    return job.result, job.error
