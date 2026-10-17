"""The Sync with Video window lists each clip with the takes that run with it."""
import tempfile
import unittest
from fractions import Fraction

from PySide6.QtCore import QSettings, Qt

from sound_file_manager import video_sync
from sound_file_manager.catalog import Recording
from sound_file_manager.video_sync_dialog import VideoSyncDialog
from . import dispose, qt_app


def rec(name, start, seconds=20):
    return Recording(path=f"/lib/{name}", size=0, mtime=0, sample_rate=48000, channels=2, bits=24,
                     frames=seconds * 48000, time_reference=start * 48000, tc_rate="25")


class VideoSyncDialogTest(unittest.TestCase):
    def setUp(self):
        qt_app()
        folder = tempfile.mkdtemp()
        self.qs = QSettings(f"{folder}/settings.ini", QSettings.Format.IniFormat)

    def test_clips_list_their_takes_and_the_ticks_decide(self):
        clips = [video_sync.Clip("/cam/A001.mov", start=Fraction(3600), rate=Fraction(25), duration=10.0,
                                 timecode="01:00:00:00"),
                 video_sync.Clip("/cam/PHONE.mp4", error="no timecode in the file")]
        recs = [rec("T1.WAV", 3602), rec("T2.WAV", 3608, seconds=1)]
        dialog = dispose(self, VideoSyncDialog(recs, self.qs, clips=clips, folder="/cam"))
        top = dialog.tree.topLevelItem(0)
        self.assertEqual(top.text(0), "A001.mov")
        self.assertEqual([top.child(i).text(0) for i in range(top.childCount())], ["T1.WAV", "T2.WAV"])
        self.assertEqual(top.child(0).text(2), "+2.000 s")
        self.assertEqual([top.child(i).checkState(0) for i in range(2)], [Qt.CheckState.Checked,
                                                                         Qt.CheckState.Unchecked])
        self.assertIn("no timecode", dialog.tree.topLevelItem(1).text(3))
        self.assertEqual([m.rec.name for m in dialog.chosen()], ["T1.WAV"])
        self.assertTrue(dialog.ok_button.isEnabled())
        top.child(0).setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(dialog.chosen(), [])
        self.assertFalse(dialog.ok_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
