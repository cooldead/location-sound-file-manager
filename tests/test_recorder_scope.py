"""Recorder filtering and report scopes share the same selection on macOS/Linux."""
import unittest
from types import SimpleNamespace

from PySide6.QtGui import QStandardItemModel

from sound_file_manager.catalog import Recording
from sound_file_manager.file_model import RecordingsModel, RecordingsProxy, build_project_tree
from sound_file_manager.main_window import MainWindow
from . import qt_app


class RecorderScopeTests(unittest.TestCase):
    def test_recorder_project_day_and_all_scopes(self):
        qt_app()
        recs = [Recording('/scratch/a.wav', 1, 0, project='Example', recorder='ZOOM F8', date='2026-09-01'),
                Recording('/scratch/b.wav', 1, 0, project='Example', recorder='ZOOM F8', date='2026-09-02'),
                Recording('/scratch/c.wav', 1, 0, project='Example', recorder='Other', date='2026-09-01')]
        source = RecordingsModel()
        source.set_recordings(recs, '/scratch')
        proxy = RecordingsProxy()
        proxy.setSourceModel(source)
        tree = QStandardItemModel()
        build_project_tree(tree, recs, by_recorder=True)
        recorder = tree.index(1, 0)
        project = tree.index(0, 0, recorder)
        day = tree.index(0, 0, project)
        owner = SimpleNamespace(model=source)
        for index, expected in ((recorder, 2), (project, 2), (day, 1), (tree.index(0, 0), 3)):
            scope = MainWindow._scope_of(index)
            proxy.set_scope(*scope)
            self.assertEqual(proxy.rowCount(), expected)
            self.assertEqual(len(MainWindow._recs_in_scope(owner, *scope)), expected)
