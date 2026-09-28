import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sound_file_manager import card_safety, card_backup, catalog, offload, bwf, renamer, report
from sound_file_manager.card_workspace import CardWorkspace
from .wavmaker import make_wav
from . import qt_app, dispose


class CardWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.card, self.nas, self.work = (self.base / n for n in ('card', 'nas', 'work'))
        self.nas.mkdir()
        (self.card / 'Project' / 'Day').mkdir(parents=True)
        self.wav = self.card / 'Project' / 'Day' / 'take.wav'
        make_wav(self.wav, project='Project')
        (self.wav.parent / 'report.csv').write_text('report')
        (self.card / 'FALSETAKES').mkdir()
        make_wav(self.card / 'FALSETAKES' / 'false.wav')
        (self.card / 'SOUNDDEV').mkdir()
        (self.card / 'SOUNDDEV' / 'settings.xml').write_text('recorder')
        (self.wav.parent / '.daily_folder').touch()
        self.guard = patch.object(card_safety, '_roots', set())
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def snapshot(self):
        return {str(p.relative_to(self.card)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.card.rglob('*') if p.is_file()}

    def prepare(self):
        workspace = CardWorkspace.prepare(str(self.card), str(self.work))
        self.addCleanup(workspace.close)
        return workspace

    def test_copy_verified_and_original_unchanged(self):
        before = self.snapshot()
        workspace = self.prepare()
        self.assertEqual(len(workspace.files), 3)
        for p in workspace.files:
            self.assertEqual(Path(p).read_bytes(), Path(workspace.original_path(p)).read_bytes())
        local_wav = Path(workspace.root) / 'Project' / 'Day' / 'take.wav'
        bwf.update_metadata(local_wav, {'note': 'local note'}, allow_rewrite=True)
        plan = offload.plan_copy(workspace.files, workspace.root, str(self.nas), {})
        result = offload.copy_items(plan)
        self.assertEqual(len(result.copied), 3)
        self.assertEqual(self.snapshot(), before)
        workspace.close()
        self.assertFalse(Path(workspace.root).exists())

    def test_no_working_folder_on_card_even_when_not_previously_registered(self):
        folder = self.card / 'new-folder'
        with self.assertRaises(PermissionError):
            CardWorkspace.prepare(str(self.card), str(folder))
        self.assertFalse(folder.exists())

    def test_cancel_and_failure_remove_partial_copy(self):
        with self.assertRaises(offload.CopyCancelled):
            CardWorkspace.prepare(str(self.card), str(self.work), cancelled=lambda: True)
        failed = offload.CopyResult(failed=[(None, 'read failed')])
        with patch.object(offload, 'copy_items', return_value=failed):
            with self.assertRaisesRegex(OSError, 'incomplete'):
                CardWorkspace.prepare(str(self.card), str(self.work))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_insufficient_space(self):
        with patch('sound_file_manager.card_workspace.shutil.disk_usage', return_value=shutil._ntuple_diskusage(0, 0, 0)):
            with self.assertRaisesRegex(OSError, 'local space'):
                self.prepare()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_symlink_and_write_guards(self):
        card_safety.protect(self.card)
        alias = self.base / 'alias'
        try:
            alias.symlink_to(self.card, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        for path in (self.wav, alias / 'new.wav'):
            with self.assertRaises(PermissionError):
                card_safety.assert_writable(path)
        with self.assertRaises(PermissionError):
            bwf.update_metadata(self.wav, {'note': 'no'})
        with self.assertRaises(PermissionError):
            offload.copy_items([offload.CopyItem(str(self.wav), str(alias / 'copy.wav'), self.wav.stat().st_size)])
        with self.assertRaises(PermissionError):
            renamer.apply_renames([renamer.RenameOp(self.wav, self.nas / 'moved.wav')])
        (self.card / 'linked.wav').symlink_to(self.wav)
        with self.assertRaisesRegex(OSError, 'symbolic link'):
            self.prepare()

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac disks ignore case and Unicode form')
    def test_guard_ignores_case_and_unicode_form_on_mac(self):
        card_safety.protect(self.card)
        other_case = str(self.card.parent / self.card.name.upper())
        with self.assertRaises(PermissionError):
            card_safety.assert_writable(os.path.join(other_case, 'x.wav'))
        accented = self.card / 'Café'  # NFD, as older Mac filesystems return it
        card_safety.protect(accented)
        with self.assertRaises(PermissionError):
            card_safety.assert_writable(str(self.card / 'Café' / 'x.wav'))

    def test_destination_cannot_escape_selected_folder(self):
        with self.assertRaises(PermissionError):
            offload.destination_for(str(self.wav), str(self.card), str(self.nas), {'Project': '../card'})

    def test_backup_verifies_sidecars_false_takes_and_detects_same_stat_corruption(self):
        workspace = self.prepare()
        plan = offload.plan_copy(workspace.files, workspace.root, str(self.nas), {})
        offload.copy_items(plan)
        cache = {}
        verify = lambda: card_backup.verify(plan, [], [], str(self.nas), cache=cache)
        self.assertTrue(verify().complete)
        if sys.platform != 'win32':
            with patch('sound_file_manager.duplicates.files_identical', side_effect=AssertionError('cached')):
                self.assertTrue(verify().complete)
        sidecar = self.nas / 'Project' / 'Day' / 'report.csv'
        stat = sidecar.stat()
        sidecar.write_bytes(b'broken')
        os.utime(sidecar, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertFalse(verify().complete)
        sidecar.write_text('report')
        (self.nas / 'FALSETAKES' / 'false.wav').unlink()
        self.assertFalse(verify().complete)
        self.assertFalse(card_backup.verify([], [], [], str(self.nas)).complete)
        self.assertFalse(card_backup.verify(plan, [], [], str(self.nas), cancelled=lambda: True).complete)

    def test_backup_finds_renamed_recording_and_adjacent_report(self):
        workspace = self.prepare()
        wav = next(p for p in workspace.files if p.endswith('/take.wav'))
        csv = next(p for p in workspace.files if p.endswith('.csv'))
        folder = self.nas / 'Moved'
        folder.mkdir()
        dst = folder / 'renamed.wav'
        shutil.copy2(wav, dst)
        shutil.copy2(csv, folder / 'report.csv')
        plan = offload.plan_copy([wav, csv], workspace.root, str(self.nas), {})
        result = card_backup.verify(plan, [catalog.read_recording(wav)], [catalog.read_recording(str(dst))], str(self.nas))
        self.assertTrue(result.complete)

    def test_ui_uses_copy_notifies_and_cleans_on_switch(self):
        from PySide6.QtCore import QSettings
        from sound_file_manager.offload_page import OffloadPage
        app = qt_app()
        qsettings = QSettings(str(self.base / 'settings.ini'), QSettings.Format.IniFormat)
        qsettings.setValue('card_working_folder', str(self.work))
        shutil.copytree(self.card, self.nas, dirs_exist_ok=True)
        with patch.object(offload, 'removable_mounts', return_value=[]):
            page = dispose(self, OffloadPage(qsettings, lambda: str(self.nas)))
            self.addCleanup(page.shutdown)
            page.open_card(offload.Card(str(self.card), 'Test card'))
            self.assertTrue(page._working_dialog.isVisible())
            self.assertEqual(page._working_dialog.bar.maximum(), 0)
            deadline = time.monotonic() + 5
            while 'Already stored' not in page.backup_notice.text() and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.005)
            self.assertIn('Already stored', page.backup_notice.text())
            self.assertIsNone(page._working_dialog)
            self.assertTrue(all(r.path.startswith(page.work_root) for r in page.model.recs))
            self.assertTrue(page.is_card_path(page.model.recs[0].path))
            root = page.work_root
            page.close_card()
            page._cleanup_workspaces()
            self.assertFalse(Path(root).exists())
            self.assertIsNone(page.workspace)

    def test_ui_offload_writes_pending_edits_only_to_nas(self):
        from PySide6.QtCore import QSettings
        from sound_file_manager.offload_page import OffloadPage
        app = qt_app()
        before = self.snapshot()
        qsettings = QSettings(str(self.base / 'settings.ini'), QSettings.Format.IniFormat)
        qsettings.setValue('card_working_folder', str(self.work))
        qsettings.setValue('report_on_export', False)
        with patch.object(offload, 'removable_mounts', return_value=[]):
            page = dispose(self, OffloadPage(qsettings, lambda: str(self.nas)))
            self.addCleanup(page.shutdown)
            page.open_card(offload.Card(str(self.card), 'Test card'))
            self.wait_until(app, lambda: bool(page.plan))
            rec = next(r for r in page.model.recs if r.name == 'take.wav')
            page.model.pending[rec.path] = {'note': 'review note'}
            page.start_copy()
            self.wait_until(app, lambda: page._copy is None)
            copied = self.nas / 'Project' / 'Day' / 'take.wav'
            self.assertTrue(copied.exists())
            self.assertEqual(catalog.read_recording(str(copied)).note, 'review note')
            self.assertNotEqual(catalog.read_recording(rec.path).note, 'review note')
            self.assertEqual(self.snapshot(), before)

    @staticmethod
    def wait_until(app, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        if not predicate():
            raise AssertionError('background operation did not finish')

    def test_switch_while_copy_preparing_ignores_stale_results(self):
        import threading
        from PySide6.QtCore import QSettings
        from sound_file_manager.offload_page import OffloadPage
        app = qt_app()
        started, release = threading.Event(), threading.Event()
        original = CardWorkspace.prepare
        calls = []

        def prepare(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                started.set()
                release.wait(3)
            return original(*args, **kwargs)

        qsettings = QSettings(str(self.base / 'settings.ini'), QSettings.Format.IniFormat)
        qsettings.setValue('card_working_folder', str(self.work))
        with patch.object(offload, 'removable_mounts', return_value=[]), patch.object(CardWorkspace, 'prepare', side_effect=prepare):
            page = dispose(self, OffloadPage(qsettings, lambda: str(self.nas)))
            self.addCleanup(page.shutdown)
            self.addCleanup(release.set)
            card = offload.Card(str(self.card), 'Test card')
            page.open_card(card)
            self.assertTrue(started.wait(2))
            old = page._reader
            page.open_card(card)
            release.set()
            self.wait_until(app, lambda: page.workspace is not None and not old.isRunning())
            self.assertTrue(page.files)
            self.assertFalse(any('SOUNDDEV' in p for p in page.files))
            page.close_card()
            self.wait_until(app, lambda: not any(j.isRunning() for _, jobs in page._retired for j in jobs))
            page._cleanup_workspaces()
            self.assertEqual(list(self.work.iterdir()), [])

    def test_temporary_copy_avoids_readback_hashes_and_forced_flushes(self):
        phases = []
        with patch.object(offload.os, 'fsync', side_effect=AssertionError('forced flush')), \
             patch.object(offload.hashlib, 'md5', side_effect=AssertionError('checksum')):
            workspace = CardWorkspace.prepare(str(self.card), str(self.work),
                progress=lambda state: phases.append(state.phase))
        self.addCleanup(workspace.close)
        self.assertTrue(phases)
        self.assertEqual(set(phases), {'copy'})
        for path in workspace.files:
            self.assertEqual(Path(path).read_bytes(), Path(workspace.original_path(path)).read_bytes())
        # Permanent offloads still hash, read back and force writes by default.
        plan = offload.plan_copy(workspace.files, workspace.root, str(self.nas), {})
        phases.clear()
        with patch.object(offload.os, 'fsync', wraps=os.fsync) as flush:
            result = offload.copy_items(plan, progress=lambda state: phases.append(state.phase))
        self.assertEqual(len(result.copied), len(plan))
        self.assertEqual(flush.call_count, len(plan))
        self.assertIn('verify', phases)

    def test_temporary_copy_still_rejects_wrong_output_size(self):
        original = os.path.getsize
        def wrong_size(path):
            size = original(path)
            return size + 1 if '.sfm-part-' in str(path) else size
        with patch.object(offload.os.path, 'getsize', side_effect=wrong_size):
            with self.assertRaisesRegex(OSError, 'different size'):
                self.prepare()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_working_copy_popup_progress_and_cancel(self):
        import threading
        from PySide6.QtCore import QSettings
        from sound_file_manager.offload_page import OffloadPage
        app = qt_app()
        release = threading.Event()
        original = CardWorkspace.prepare
        def prepare(*args, **kwargs):
            release.wait(3)
            return original(*args, **kwargs)
        qsettings = QSettings(str(self.base / 'popup.ini'), QSettings.Format.IniFormat)
        qsettings.setValue('card_working_folder', str(self.work))
        with patch.object(offload, 'removable_mounts', return_value=[]), patch.object(CardWorkspace, 'prepare', side_effect=prepare):
            page = dispose(self, OffloadPage(qsettings, lambda: str(self.nas)))
            self.addCleanup(page.shutdown)
            self.addCleanup(release.set)
            page.open_card(offload.Card(str(self.card), 'Test card'))
            reader = page._reader
            dialog = page._working_dialog
            state = offload.CopyProgress(done_bytes=50, total_bytes=100,
                done_files=1, total_files=3, current='take.wav')
            page._staging_progress(state, reader)
            self.assertEqual(dialog.bar.value(), 500)
            self.assertIn('take.wav', dialog.details.text())
            self.assertIn('1 of 3', dialog.details.text())
            dialog.reject()  # Cancel, Escape and the window close button use reject.
            self.assertIsNone(page._working_dialog)
            self.assertIsNone(page.card)
            self.assertTrue(reader.isInterruptionRequested())
            release.set()
            reader.wait()
            app.processEvents()
            self.assertIsNone(page.workspace)

    def test_comparison_reads_temp_copy_and_reports_new_and_different_files(self):
        import builtins
        workspace = self.prepare()
        plan = offload.plan_copy(workspace.files, workspace.root, str(self.nas), {})
        offload.copy_items(plan)
        (self.nas / 'Project' / 'Day' / 'report.csv').write_text('different')
        (self.nas / 'FALSETAKES' / 'false.wav').unlink()
        original = builtins.open
        def no_card_reads(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and card_safety.below(path, self.card):
                raise AssertionError('comparison reread the card')
            return original(path, *args, **kwargs)
        with patch('builtins.open', side_effect=no_card_reads):
            result = card_backup.verify(plan, [], [], str(self.nas))
        self.assertEqual(result.matched, 1)
        differences = {Path(d.src).name: d.reason for d in result.differences}
        self.assertEqual(differences, {'report.csv': 'Different', 'false.wav': 'New or not found'})
        self.assertFalse(result.complete)

    def test_review_copy_keeps_existing_storage_file(self):
        from sound_file_manager.offload_page import difference_copy_plan
        workspace = self.prepare()
        src = next(p for p in workspace.files if p.endswith('.csv'))
        dst = self.nas / 'report.csv'
        dst.write_text('existing report')
        difference = card_backup.Difference(src, str(dst), 'Different')
        plan = difference_copy_plan([difference])
        self.assertNotEqual(plan[0].dst, str(dst))
        result = offload.copy_items(plan)
        self.assertEqual(len(result.copied), 1)
        self.assertEqual(dst.read_text(), 'existing report')
        self.assertEqual(Path(plan[0].dst).read_bytes(), Path(src).read_bytes())

    def test_review_ignore_skips_copy_without_claiming_backed_up(self):
        from PySide6.QtCore import QSettings
        from sound_file_manager.offload_page import OffloadPage, StorageDifferencesDialog
        app = qt_app()
        qsettings = QSettings(str(self.base / 'ignore.ini'), QSettings.Format.IniFormat)
        qsettings.setValue('card_working_folder', str(self.work))
        def ignore_all(dialog):
            dialog.tree.selectAll()
            dialog.action = 'ignore'
            return 1
        with patch.object(offload, 'removable_mounts', return_value=[]):
            page = dispose(self, OffloadPage(qsettings, lambda: str(self.nas)))
            self.addCleanup(page.shutdown)
            page.open_card(offload.Card(str(self.card), 'Test card'))
            self.wait_until(app, lambda: bool(page._differences))
            self.assertTrue(page.selected_plan())
            with patch.object(StorageDifferencesDialog, 'exec', ignore_all):
                page._review_storage_differences()
            self.assertEqual(page.selected_plan(), [])
            self.assertIn('not confirmed stored', page.backup_notice.text())
            self.assertEqual(list(self.nas.iterdir()), [])
