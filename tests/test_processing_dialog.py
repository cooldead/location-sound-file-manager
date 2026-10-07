"""The Reduce Noise window's choices (no audio is played)."""
import os
import shutil
import tempfile
import unittest

from PySide6.QtCore import QSettings, Qt

from sound_file_manager import catalog, deepfilter
from sound_file_manager.processing_dialog import ProcessDialog, processable, track_names
from . import dispose, qt_app
from .wavmaker import make_wav


_TEMP_DIRS: list[str] = []


def _mkdtemp() -> str:
    """A temporary folder removed when this module's tests end (/tmp is RAM on many Linux systems)."""
    path = tempfile.mkdtemp()
    _TEMP_DIRS.append(path)
    return path


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


class ProcessDialogTest(unittest.TestCase):
    def setUp(self):
        qt_app()
        self.dir = _mkdtemp().replace(os.sep, "/")
        self.qs = QSettings(f"{self.dir}/settings.ini", QSettings.Format.IniFormat)

    def _rec(self, name):
        path = f"{self.dir}/{name}"
        make_wav(path, filename=name)
        return catalog.read_recording(path)

    def test_tracks_are_picked_by_name_across_files(self):
        a, b = self._rec("1T01_ISO.wav"), self._rec("1T02_ISO.wav")
        self.assertEqual(track_names(a), ["BOOM", "LAV"])
        dialog = ProcessDialog([a, b], self.qs)
        try:
            dialog.tracks.item(0).setCheckState(Qt.CheckState.Unchecked)
            self.assertEqual(dialog.channels_for(a), (1,))
            self.assertEqual(dialog.settings_for(b).channels, (1,))
            self.assertFalse(dialog.uses_region(), "no region was marked")
        finally:
            dispose(self, dialog)

    def test_a_region_on_the_players_copy_of_a_file_is_used(self):
        rec = self._rec("1T03_ISO.wav")
        player_copy = catalog.read_recording(rec.path)  # another object for the same file
        dialog = ProcessDialog([rec], self.qs, player_copy, (0, 2400))
        try:
            self.assertTrue(dialog.uses_region())
            self.assertTrue(dialog.region_profile_for(rec))
        finally:
            dispose(self, dialog)

    def test_a_suffix_and_a_track_are_required(self):
        dialog = ProcessDialog([self._rec("1T04.wav")], self.qs)
        try:
            self.assertTrue(dialog.ok.isEnabled())
            dialog.suffix.setText(" ")
            self.assertFalse(dialog.ok.isEnabled())
            dialog.suffix.setText("_N/R")
            self.assertFalse(dialog.ok.isEnabled())
            dialog.suffix.setText("_DN")
            for i in range(dialog.tracks.count()):
                dialog.tracks.item(i).setCheckState(Qt.CheckState.Unchecked)
            self.assertFalse(dialog.ok.isEnabled())
        finally:
            dispose(self, dialog)

    def test_deepfilter_needs_the_add_on_and_48_khz(self):
        rec = self._rec("1T06_ISO.wav")
        data = _mkdtemp()
        dialog = ProcessDialog([rec], self.qs, data_dir=data)
        try:
            dialog.voice.setChecked(True)
            self.assertFalse(dialog.ok.isEnabled(), "not installed")
            self.assertIn("Install", dialog.addon_button.text())
            self.assertFalse(dialog.profile_box.isEnabled())
            program = deepfilter.program_path(data)
            os.makedirs(os.path.dirname(program))
            with open(program, "wb") as f:
                f.write(b"x" * deepfilter.asset()[2])
            dialog._update()
            self.assertTrue(dialog.ok.isEnabled())
            self.assertEqual(dialog.settings_for(rec).mode, "deepfilter")
            self.assertFalse(dialog.region_profile_for(rec))
            rec.sample_rate = 44100
            dialog._update()
            self.assertFalse(dialog.ok.isEnabled(), "nothing at 48 kHz")
        finally:
            dispose(self, dialog)

    def test_each_mode_keeps_its_own_reduction(self):
        dialog = ProcessDialog([self._rec("1T07.wav")], self.qs, data_dir=_mkdtemp())
        try:
            dialog.denoise.setChecked(True)
            dialog.reduction.setValue(9)
            dialog.voice.setChecked(True)
            self.assertEqual(dialog.reduction.value(), 24)
            dialog.reduction.setValue(40)
            dialog.dehiss.setChecked(True)
            self.assertEqual(dialog.reduction.value(), 9)
            dialog.voice.setChecked(True)
            self.assertEqual(dialog.reduction.value(), 40)
        finally:
            dispose(self, dialog)

    def test_unreadable_files_are_not_processable(self):
        rec = self._rec("1T05.wav")
        self.assertIsNone(processable(rec))
        rec.error = "broken"
        self.assertEqual(processable(rec), "broken")


if __name__ == "__main__":
    unittest.main()
