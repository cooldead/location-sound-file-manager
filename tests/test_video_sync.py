"""Timecode sync of recordings with video clips (video_sync.py).

The clips are tiny real files made with ffmpeg, so the probing and the written
copies go through the same programs as in the app."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction

from sound_file_manager import catalog, video_sync
from sound_file_manager.catalog import Recording

from .wavmaker import make_wav

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_clip(path, timecode, rate="24000/1001", seconds=1.0, audio=True):
    """A small clip with a start timecode (MOV/MP4 tmcd track, or MXF)."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate={rate}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=r=48000", "-c:a", "pcm_s16le"]
    cmd += ["-t", str(seconds), "-timecode", timecode,
            "-c:v", "mpeg2video" if path.endswith(".mxf") else "mjpeg", path]
    subprocess.run(cmd, check=True)
    return path


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg and ffprobe")
class ProbeClipTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_mov_start_timecode_rate_and_length(self):
        clip = video_sync.probe_clip(make_clip(os.path.join(self.dir, "A001.mov"), "01:00:00:00"))
        self.assertEqual(clip.error, "")
        self.assertEqual(clip.rate, Fraction(24000, 1001))
        self.assertFalse(clip.drop_frame)
        self.assertEqual(clip.timecode, "01:00:00:00")
        # 86,400 frames at 23.976 run 3603.6 s of real time.
        self.assertEqual(clip.start, Fraction(36036, 10))
        self.assertAlmostEqual(clip.duration, 1.0, delta=0.05)
        self.assertEqual(clip.audio_streams, 1)

    def test_drop_frame_timecode(self):
        clip = video_sync.probe_clip(make_clip(os.path.join(self.dir, "B.mov"), "10:00:00;02", "30000/1001"))
        self.assertTrue(clip.drop_frame)
        # 10 h of 29.97 DF is 1,078,920 frames (17,982 per ten minutes); minute 600 drops none.
        self.assertEqual(clip.start, Fraction(1078922 * 1001, 30000))

    def test_mxf(self):
        clip = video_sync.probe_clip(make_clip(os.path.join(self.dir, "C.mxf"), "01:00:00:00", "25", audio=False))
        self.assertEqual((clip.rate, clip.start, clip.audio_streams), (Fraction(25), Fraction(3600), 0))

    def test_clip_without_timecode_is_marked(self):
        path = os.path.join(self.dir, "phone.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=30",
                        "-t", "0.5", "-c:v", "mjpeg", path], check=True)
        clip = video_sync.probe_clip(path)
        self.assertIsNone(clip.start)
        self.assertIn("no timecode", clip.error)

    def test_scan_folder_finds_clips_below_it(self):
        # Cameras nest clips (PRIVATE/M4ROOT/CLIP); synced copies made earlier are not clips to sync again.
        os.makedirs(os.path.join(self.dir, "PRIVATE", "CLIP"))
        make_clip(os.path.join(self.dir, "PRIVATE", "CLIP", "C0001.MP4"), "01:00:00:00")
        make_clip(os.path.join(self.dir, "A001.mov"), "02:00:00:00")
        make_clip(os.path.join(self.dir, "A001_synced.mov"), "02:00:00:00")
        with open(os.path.join(self.dir, "notes.txt"), "w") as f:
            f.write("x")
        clips = video_sync.scan_folder(self.dir)
        self.assertEqual([c.name for c in clips], ["A001.mov", "C0001.MP4"])
        self.assertEqual([c.timecode for c in clips], ["02:00:00:00", "01:00:00:00"])

    def test_not_a_video(self):
        path = os.path.join(self.dir, "notes.mov")
        with open(path, "w") as f:
            f.write("not a video")
        self.assertNotEqual(video_sync.probe_clip(path).error, "")


def rec(name, start_seconds, seconds=20, rate=48000, channels=2, date="2025-10-27", family=""):
    return Recording(path=f"/lib/{name}", size=0, mtime=0, sample_rate=rate, channels=channels, bits=24,
                     frames=int(seconds * rate), time_reference=int(start_seconds * rate), date=date,
                     tc_rate="24000/1001", family=family)


def clip(name, start, seconds=10.0, date="2025-10-27"):
    return video_sync.Clip(f"/cam/{name}", start=Fraction(start), rate=Fraction(24000, 1001),
                           duration=seconds, date=date)


class MatchTest(unittest.TestCase):
    def test_take_overlapping_a_clip_gets_its_offset(self):
        # Clip from 3603.6 s (01:00:00:00 at 23.976) for 10 s; the take starts 2 s later.
        matches = video_sync.match([rec("T1.WAV", Fraction(36056, 10))], [clip("A001.mov", Fraction(36036, 10))])
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual((m.clip.name, m.rec.name), ("A001.mov", "T1.WAV"))
        self.assertEqual(m.offset, Fraction(2))
        self.assertEqual(m.offset_samples, 96000)
        self.assertAlmostEqual(m.overlap, 8.0)

    def test_take_elsewhere_in_the_day_does_not_match(self):
        self.assertEqual(video_sync.match([rec("T1.WAV", 7200)], [clip("A001.mov", 3600)]), [])

    def test_take_after_midnight_matches_a_clip_before_it(self):
        # Clip 23:59:50:00 at 25 fps for 20 s, take from 00:00:05:00: 15 s into the clip.
        late = video_sync.Clip("/cam/A001.mov", start=Fraction(86390), rate=Fraction(25), duration=20.0)
        matches = video_sync.match([rec("T1.WAV", 5)], [late])
        self.assertEqual([m.offset for m in matches], [15])

    def test_midnight_at_ntsc_rates(self):
        # A timecode day at 23.976 runs 86,486.4 real seconds: clip 23:59:50:00, take 00:00:05:00.
        rate = Fraction(24000, 1001)
        clip_start = (86400 - 10) * 24 / rate
        matches = video_sync.match([rec("T1.WAV", 5 * 24 / rate)], [clip("A001.mov", clip_start, seconds=20)])
        self.assertEqual([m.offset for m in matches], [15 * 24 / rate])

    def test_same_timecode_on_another_day_does_not_match(self):
        self.assertEqual(video_sync.match([rec("T1.WAV", 3602, date="2025-10-29")],
                                          [clip("A001.mov", 3600, date="2025-10-27")]), [])
        # A clip's creation time is often UTC, a day off the recorder's date.
        self.assertEqual(len(video_sync.match([rec("T1.WAV", 3602, date="2025-10-28")],
                                              [clip("A001.mov", 3600, date="2025-10-27")])), 1)

    def test_the_take_covering_most_of_the_clip_is_chosen(self):
        recs = [rec("SHORT.WAV", 3608, seconds=5), rec("LONG.WAV", 3601, seconds=30),
                rec("LONG_LR.WAV", 3601, seconds=30, channels=2), rec("LONG_ISO.WAV", 3601, seconds=30, channels=8)]
        matches = video_sync.match(recs[:2], [clip("A001.mov", 3600)])
        self.assertEqual([m.rec.name for m in matches if m.chosen], ["LONG.WAV"])
        # Same overlap (an _ISO/_LR take): the one with every track.
        matches = video_sync.match(recs[2:], [clip("A001.mov", 3600)])
        self.assertEqual([m.rec.name for m in matches if m.chosen], ["LONG_ISO.WAV"])

    def test_take_without_timecode_is_left_out(self):
        untimed = rec("T1.WAV", 3602)
        untimed.time_reference = None
        self.assertEqual(video_sync.match([untimed], [clip("A001.mov", 3600)]), [])


def audio_of(path, stream):
    """One audio stream of a file as float samples (frames, channels)."""
    import json
    import numpy as np
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-select_streams", f"a:{stream}",
                                      "-of", "json", path], capture_output=True, check=True).stdout)["streams"][0]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{stream}", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<f4").reshape(-1, int(info["channels"])), info


def click_take(path, start_seconds, click_at, seconds=3.0, channels=2):
    """A float take with a single full-scale sample at click_at seconds (on every track)."""
    import numpy as np
    samples = np.zeros((int(seconds * 48000), channels), np.float32)
    samples[int(click_at * 48000)] = 1.0
    make_wav(path, float_samples=samples, time_reference=int(start_seconds * 48000))
    return catalog.read_recording(path)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg and ffprobe")
class SyncedCopyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        # 2 s from 01:00:00:00 at 25 fps: the clip starts at 3600 s exactly.
        self.clip = video_sync.probe_clip(make_clip(os.path.join(self.dir, "A001.mov"), "01:00:00:00", "25", 2.0))

    def synced(self, *recs):
        matches = video_sync.match(recs, [self.clip])
        dst = video_sync.synced_path(self.clip, self.dir)
        self.assertEqual(video_sync.write_synced_copy(matches, dst), dst)
        return dst

    def test_take_starting_after_the_clip_is_delayed(self):
        # Take from 3600.5 s with a click 0.25 s in: 0.75 s into the clip.
        dst = self.synced(click_take(os.path.join(self.dir, "T1.WAV"), 3600.5, 0.25))
        self.assertEqual(os.path.basename(dst), "A001_synced.mov")
        audio, info = audio_of(dst, 1)
        self.assertEqual(int(audio[:, 0].argmax()), 36000)
        self.assertEqual(audio.shape[1], 2)
        self.assertEqual((info["tags"].get("name"), info["tags"].get("handler_name")), ("T1", "T1"))
        self.assertAlmostEqual(len(audio) / 48000, 2.0, delta=0.001)

    def test_take_starting_before_the_clip_is_trimmed(self):
        # Take from 3599 s with a click 1.25 s in: 0.25 s into the clip.
        dst = self.synced(click_take(os.path.join(self.dir, "T2.WAV"), 3599, 1.25))
        audio, _ = audio_of(dst, 1)
        self.assertEqual(int(audio[:, 0].argmax()), 12000)

    def test_video_camera_audio_and_timecode_are_kept(self):
        dst = self.synced(click_take(os.path.join(self.dir, "T1.WAV"), 3600.5, 0.25),
                          click_take(os.path.join(self.dir, "T3.WAV"), 3600.2, 0.1, channels=1))
        copy = video_sync.probe_clip(dst)
        self.assertEqual((copy.timecode, copy.rate, copy.audio_streams), ("01:00:00:00", Fraction(25), 3))
        video = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_streams",
                                           "-of", "json", dst], capture_output=True).stdout)["streams"][0]
        self.assertEqual(video["codec_name"], "mjpeg")  # copied, not encoded again
        self.assertEqual(int(audio_of(dst, 2)[0][:, 0].argmax()), 0.3 * 48000)

    def test_never_overwrites(self):
        take = click_take(os.path.join(self.dir, "T1.WAV"), 3600.5, 0.25)
        dst = self.synced(take)
        before = os.path.getsize(dst)
        with self.assertRaises(FileExistsError):
            video_sync.write_synced_copy(video_sync.match([take], [self.clip]), dst)
        self.assertEqual(os.path.getsize(dst), before)
        self.assertEqual(sorted(os.listdir(self.dir)), ["A001.mov", "A001_synced.mov", "T1.WAV"])


def pal_clip(name, start, seconds=2.0, rate=25):
    return video_sync.Clip(f"/cam/{name}", start=Fraction(start), rate=Fraction(rate), duration=seconds,
                           timecode="01:00:00:00", width=1920, height=1080, audio_streams=1)


class TimelineTest(unittest.TestCase):
    """Takes sit on the timeline at their own timecode, so they play in sync with the clips."""

    def fcpxml(self, matches, editor="fcp"):
        import xml.etree.ElementTree as ET
        timeline = video_sync.timeline(matches, editor, "Day 1")
        return ET.fromstring(timeline.text), timeline

    def test_fcpxml_places_clip_and_take_at_their_timecode(self):
        # Take from 3600.52 s (frame 13 after the clip's first): on a frame, so exact.
        take = rec("T1.WAV", Fraction(360052, 100))
        root, timeline = self.fcpxml(video_sync.match([take], [pal_clip("A001.mov", 3600)]))
        self.assertEqual(root.get("version"), "1.10")
        self.assertEqual(timeline.extension, ".fcpxml")
        sequence = root.find(".//sequence")
        self.assertEqual((sequence.get("tcStart"), sequence.get("tcFormat")), ("3600s", "NDF"))
        clips = {c.get("name"): c for c in root.iter("asset-clip")}
        video, audio = clips["A001"], clips["T1"]
        self.assertEqual((video.get("lane"), video.get("offset"), video.get("start"), video.get("duration")),
                         ("1", "3600s", "3600s", "2s"))
        self.assertEqual((audio.get("lane"), audio.get("offset"), audio.get("start")),
                         ("-1", "90013/25s", "90013/25s"))
        assets = {a.get("id"): a for a in root.iter("asset")}
        self.assertEqual(assets[audio.get("ref")].get("start"), "90013/25s")
        self.assertEqual(assets[video.get("ref")].find("media-rep").get("src"), "file:///cam/A001.mov")

    def test_take_between_frames_keeps_sample_sync(self):
        # 3600.5 s is half way through a frame: the take is placed on the next
        # frame (3600.52 s) and starts playing from that point in it.
        root, _ = self.fcpxml(video_sync.match([rec("T1.WAV", Fraction(36005, 10))], [pal_clip("A001.mov", 3600)]))
        audio = next(c for c in root.iter("asset-clip") if c.get("name") == "T1")
        asset = next(a for a in root.iter("asset") if a.get("id") == audio.get("ref"))
        self.assertEqual((audio.get("offset"), audio.get("start"), asset.get("start")),
                         ("90013/25s", "90013/25s", "7201/2s"))

    def test_take_after_midnight_sits_after_its_clip(self):
        # Clip 23:59:59 for 2 s, take from 00:00:00: one second into the clip, not a day before it.
        root, _ = self.fcpxml(video_sync.match([rec("T1.WAV", 0)], [pal_clip("A001.mov", 86399)]))
        clips = {c.get("name"): c for c in root.iter("asset-clip")}
        self.assertEqual((clips["A001"].get("offset"), clips["T1"].get("offset")), ("86399s", "86400s"))
        self.assertEqual(root.find(".//sequence").get("duration"), "21s")

    def test_times_are_frames_or_samples(self):
        # A clip length read as a float (10.01 s) must not become a 2^50-denominator time.
        root, _ = self.fcpxml(video_sync.match([rec("T1.WAV", 3600)], [pal_clip("A001.mov", 3600, seconds=10.01)]))
        for node in list(root.iter("asset")) + list(root.iter("asset-clip")):
            denominator = Fraction(node.get("duration")[:-1]).denominator
            self.assertTrue(25 % denominator == 0 or 48000 % denominator == 0, (node.get("name"), denominator))

    def test_windows_paths_become_file_urls(self):
        clip = pal_clip("A001.mov", 3600)
        clip.path = "C:/Cam Card/A001.mov"
        root, _ = self.fcpxml(video_sync.match([rec("T1.WAV", 3600)], [clip]))
        self.assertIn("file:///C:/Cam%20Card/A001.mov", [m.get("src") for m in root.iter("media-rep")])

    def test_timeline_file_is_written_whole(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        timeline = video_sync.timeline(video_sync.match([rec("T1.WAV", 3601)], [pal_clip("A001.mov", 3600)]),
                                       "fcp", "Day 1")
        path = os.path.join(folder, "Day 1.fcpxml")
        with open(path, "w") as f:
            f.write("an older timeline")  # the save dialog asked before replacing it
        video_sync.write_timeline(timeline, path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), timeline.text)
        self.assertEqual(os.listdir(folder), ["Day 1.fcpxml"])
        broken = video_sync.Timeline("<fcpxml", ".fcpxml", [])
        with self.assertRaises(SyntaxError):
            video_sync.write_timeline(broken, path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), timeline.text)
        self.assertEqual(os.listdir(folder), ["Day 1.fcpxml"])

    def test_resolve_gets_fcpxml_1_9(self):
        root, timeline = self.fcpxml(video_sync.match([rec("T1.WAV", 3601)], [pal_clip("A001.mov", 3600)]),
                                     "resolve")
        self.assertEqual((root.get("version"), timeline.extension), ("1.9", ".fcpxml"))

    def test_overlapping_clips_get_their_own_lanes(self):
        clips = [pal_clip("A001.mov", 3600), pal_clip("B001.mov", 3601)]
        root, _ = self.fcpxml(video_sync.match([rec("T1.WAV", 3600)], clips))
        lanes = {c.get("name"): c.get("lane") for c in root.iter("asset-clip")}
        self.assertEqual(lanes, {"A001": "1", "B001": "2", "T1": "-1"})

    def test_only_chosen_takes_go_on_once_each(self):
        clips = [pal_clip("A001.mov", 3600), pal_clip("A002.mov", 3603)]
        matches = video_sync.match([rec("LONG.WAV", 3600, seconds=10), rec("SHORT.WAV", 3600.5, seconds=1)], clips)
        root, _ = self.fcpxml(matches)
        self.assertEqual(sorted(c.get("name") for c in root.iter("asset-clip")), ["A001", "A002", "LONG"])

    def test_clip_at_another_rate_is_left_out(self):
        other = pal_clip("PHONE.mov", 3600.4, rate=30)
        matches = video_sync.match([rec("T1.WAV", 3600)], [pal_clip("A001.mov", 3600), other])
        root, timeline = self.fcpxml(matches)
        self.assertEqual([c.name for c in timeline.left_out], ["PHONE.mov"])
        self.assertNotIn("PHONE", [c.get("name") for c in root.iter("asset-clip")])

    def test_premiere_xml_places_each_track_at_its_frame(self):
        import xml.etree.ElementTree as ET
        take = rec("T1.WAV", Fraction(360052, 100), channels=2)
        timeline = video_sync.timeline(video_sync.match([take], [pal_clip("A001.mov", 3600)]), "premiere", "Day 1")
        self.assertEqual(timeline.extension, ".xml")
        root = ET.fromstring(timeline.text.split("\n", 1)[1])  # after the DOCTYPE line
        self.assertEqual((root.tag, root.get("version")), ("xmeml", "4"))
        sequence = root.find("sequence")
        self.assertEqual((sequence.findtext("rate/timebase"), sequence.findtext("rate/ntsc")), ("25", "FALSE"))
        self.assertEqual(sequence.findtext("timecode/string"), "01:00:00:00")
        video = sequence.find("media/video/track/clipitem")
        self.assertEqual((video.findtext("start"), video.findtext("in"), video.findtext("end")), ("0", "0", "50"))
        self.assertEqual(video.findtext("file/pathurl"), "file:///cam/A001.mov")
        tracks = sequence.findall("media/audio/track")
        items = [t.find("clipitem") for t in tracks]
        self.assertEqual([i.findtext("start") for i in items], ["13", "13"])
        self.assertEqual([i.findtext("sourcetrack/trackindex") for i in items], ["1", "2"])
        # The take's tracks are linked, so they move together in Premiere.
        self.assertEqual([[link.findtext("linkclipref") for link in i.findall("link")] for i in items],
                         [[items[0].get("id"), items[1].get("id")]] * 2)

    def test_premiere_ntsc_rate(self):
        import xml.etree.ElementTree as ET
        matches = video_sync.match([rec("T1.WAV", Fraction(36036, 10))], [clip("A001.mov", Fraction(36036, 10))])
        text = video_sync.timeline(matches, "premiere", "Day 1").text
        sequence = ET.fromstring(text.split("\n", 1)[1]).find("sequence")
        self.assertEqual((sequence.findtext("rate/timebase"), sequence.findtext("rate/ntsc")), ("24", "TRUE"))


if __name__ == "__main__":
    unittest.main()
