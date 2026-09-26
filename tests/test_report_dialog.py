import os
import tempfile
import unittest

from PySide6.QtCore import QSettings

from sound_file_manager import report
from sound_file_manager.catalog import Recording
from sound_file_manager.report_dialog import ReportDialog, ReportGroup

from . import dispose, qt_app

app = qt_app()


def rec(project, name):
    return Recording(path=f"/nas/{project}/{name}", size=1, mtime=0, sample_rate=48000, bits=24, channels=2,
                     frames=48000, project=project, scene="1", take="01", date="2026-08-25",
                     tracks=["Boom", "Lav-1"])


class ReportDialogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.qsettings = QSettings(os.path.join(self._tmp.name, "s.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_report_per_group_with_its_own_fields(self):
        groups = [ReportGroup("Orchard", [rec("Orchard", "a.wav")], self._tmp.name),
                  ReportGroup("Meadow", [rec("Meadow", "b.wav"), rec("Meadow", "c.wav")], self._tmp.name)]
        dialog = dispose(self, ReportDialog(groups, self.qsettings, mode="export"))
        self.assertTrue(dialog.nav.isVisibleTo(dialog))
        self.assertIn("Report 1 of 2", dialog.nav_label.text())
        self.assertFalse(dialog.prev_button.isEnabled())
        dialog.edits["director"].setText("Director A")
        dialog.go(1)
        self.assertIn("Meadow", dialog.nav_label.text())
        self.assertEqual(dialog.edits["project"].text(), "Meadow")
        self.assertEqual(dialog.edits["director"].text(), "")
        dialog.edits["director"].setText("Director B")
        dialog.columns.item(0).setCheckState(dialog.columns.item(0).checkState().__class__.Unchecked)
        infos = dialog.infos()
        self.assertEqual([i.get("director") for i in infos], ["Director A", "Director B"])
        self.assertEqual(infos[0].columns, infos[1].columns)  # layout is shared
        self.assertNotIn("file", infos[0].columns)
        dialog.go(0)
        self.assertEqual(dialog.edits["director"].text(), "Director A")

    def test_mixed_projects_use_the_folder_name(self):
        from sound_file_manager.report_dialog import remembered_info
        recs = [rec("PILOT-D2", "a.wav"), rec("PILOT-D3", "b.wav")]
        self.assertEqual(remembered_info(self.qsettings, recs, "PILOT-D2").get("project"), "PILOT-D2")
        self.assertEqual(remembered_info(self.qsettings, recs[:1], "Other").get("project"), "PILOT-D2")

    def test_single_report_has_no_navigation(self):
        dialog = dispose(self, ReportDialog([rec("P", "a.wav")], self.qsettings))
        self.assertFalse(dialog.nav.isVisibleTo(dialog))

    def test_save_all(self):
        groups = [ReportGroup("A", [rec("A", "a.wav")], self._tmp.name),
                  ReportGroup("B", [rec("B", "b.wav")], self._tmp.name)]
        dialog = dispose(self, ReportDialog(groups, self.qsettings))
        from unittest import mock
        from PySide6.QtWidgets import QMessageBox
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.save_all()
        names = sorted(os.listdir(self._tmp.name))
        self.assertIn("A Sound Report 2026-08-25.pdf", names)
        self.assertIn("B Sound Report 2026-08-25.csv", names)


class SetupDialogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.qsettings = QSettings(os.path.join(self._tmp.name, "s.ini"), QSettings.Format.IniFormat)
        self.out = os.path.join(self._tmp.name, "out")
        self.lib = os.path.join(self._tmp.name, "lib")
        os.mkdir(self.out)
        os.mkdir(self.lib)

    def tearDown(self):
        self._tmp.cleanup()

    def test_saves_details_branding_and_folders(self):
        from sound_file_manager import settings
        from sound_file_manager.branding_dialog import SetupDialog, load_branding
        dialog = dispose(self, SetupDialog(self.qsettings))
        dialog.mixer.setText("Alex Mixer")
        dialog.phone.setText("555-0100")
        dialog.editor.contact_footer.setChecked(True)
        dialog.output.setText(self.out)
        dialog.accept()
        self.assertEqual(settings.get_json(self.qsettings, "report_personal")["phone"], "555-0100")
        self.assertTrue(load_branding(self.qsettings).contact_in_footer)
        self.assertEqual(settings.get(self.qsettings, "library_folder"), self.out)  # same as output by default
        self.assertEqual(settings.get(self.qsettings, "offload_destinations")[0], "")  # "" = library folder
        self.assertTrue(settings.get(self.qsettings, "setup_done"))

    def test_separate_library_folder(self):
        from sound_file_manager import settings
        from sound_file_manager.branding_dialog import SetupDialog
        dialog = dispose(self, SetupDialog(self.qsettings))
        dialog.output.setText(self.out)
        dialog.separate_library.setChecked(True)
        dialog.library.setText(self.lib)
        dialog.accept()
        self.assertEqual(settings.get(self.qsettings, "library_folder"), self.lib)
        self.assertEqual(settings.get(self.qsettings, "offload_destinations")[0], self.out)

    def test_missing_folder_is_refused(self):
        from sound_file_manager import settings
        from sound_file_manager.branding_dialog import SetupDialog
        from unittest import mock
        from PySide6.QtWidgets import QMessageBox
        dialog = dispose(self, SetupDialog(self.qsettings))
        dialog.output.setText(os.path.join(self._tmp.name, "nope"))
        with mock.patch.object(QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertFalse(settings.get(self.qsettings, "setup_done"))


if __name__ == "__main__":
    unittest.main()
