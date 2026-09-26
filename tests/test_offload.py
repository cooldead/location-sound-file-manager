import csv
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from sound_file_manager import catalog, offload, report
from sound_file_manager.catalog import Recording

from . import qt_app
from .wavmaker import make_wav


class OffloadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.card = base / "card"
        self.nas = base / "nas"
        self.nas.mkdir()
        for relative in ("Orchard/26Y08M25/1T01_ISO.wav", "Orchard/26Y08M25/1T02_ISO.wav",
                         "Orchard/26Y08M26/2T01_ISO.wav", "FALSETAKES/9T01_ISO.wav"):
            path = self.card / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            make_wav(path, project="Orchard")
        (self.card / "Orchard" / "26Y08M25" / ".daily_folder").touch()
        (self.card / "Orchard" / "report.CSV").write_text("x")
        for folder in ("SOUNDDEV", "TRASH"):
            (self.card / folder).mkdir()
            (self.card / folder / "junk.wav").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def test_card_detection_and_folders(self):
        self.assertTrue(offload.looks_like_card(str(self.card)))
        self.assertFalse(offload.looks_like_card(str(self.nas)))
        path = str(self.card / "Orchard" / "26Y08M25" / "1T01_ISO.wav")
        self.assertEqual(offload.project_folder(path, str(self.card)), "Orchard")
        self.assertEqual(offload.day_folder(path, str(self.card)), "26Y08M25")
        self.assertEqual(offload.day_folder(str(self.card / "Orchard" / "report.CSV"), str(self.card)), "")
        self.assertTrue(offload.is_system_folder("SOUNDDEV"))

    def test_card_files(self):
        files = offload.card_files(str(self.card), {"Orchard", "FALSETAKES"})
        names = sorted(os.path.relpath(f, self.card) for f in files)
        self.assertIn("Orchard/26Y08M25/.daily_folder", names)
        self.assertIn("Orchard/report.CSV", names)
        self.assertNotIn("FALSETAKES/9T01_ISO.wav", names)  # only when asked for
        with_false = offload.card_files(str(self.card), {"FALSETAKES"}, include_false_takes=True)
        self.assertEqual(len(with_false), 1)

    def test_destination_keeps_card_layout(self):
        src = str(self.card / "Orchard" / "26Y08M25" / "1T01_ISO.wav")
        self.assertEqual(offload.destination_for(src, str(self.card), str(self.nas), {"Orchard": "Orchard"}),
                         str(self.nas / "Orchard" / "26Y08M25" / "1T01_ISO.wav"))
        self.assertEqual(offload.destination_for(src, str(self.card), str(self.nas), {"Orchard": "Orchard 2026"},
                                                 {src: "new.wav"}),
                         str(self.nas / "Orchard 2026" / "26Y08M25" / "new.wav"))

    def test_plan_and_copy(self):
        files = offload.card_files(str(self.card), {"Orchard"})
        plan = offload.plan_copy(files, str(self.card), str(self.nas), {"Orchard": "Orchard"})
        self.assertTrue(all(i.status == "new" for i in plan))
        seen = []
        result = offload.copy_items(plan, verify=True, progress=lambda s: seen.append(s.done_bytes))
        self.assertEqual(len(result.copied), len(files))
        self.assertFalse(result.failed)
        for item in plan:
            self.assertEqual(Path(item.dst).read_bytes(), Path(item.src).read_bytes())
            self.assertAlmostEqual(os.stat(item.dst).st_mtime, os.stat(item.src).st_mtime, places=3)
        self.assertFalse(list(self.nas.rglob(".sfm-part-*")))
        self.assertTrue(seen)
        # A second plan finds everything already there.
        again = offload.plan_copy(files, str(self.card), str(self.nas), {"Orchard": "Orchard"})
        self.assertTrue(all(i.status == "same" for i in again))

    def test_conflict_is_never_overwritten(self):
        src = str(self.card / "Orchard" / "26Y08M26" / "2T01_ISO.wav")
        dst = self.nas / "Orchard" / "26Y08M26" / "2T01_ISO.wav"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"a different recording")
        plan = offload.plan_copy([src], str(self.card), str(self.nas), {"Orchard": "Orchard"})
        self.assertEqual(plan[0].status, "conflict")
        offload.copy_items(plan)
        self.assertEqual(dst.read_bytes(), b"a different recording")

    def test_cancel_leaves_no_partial_file(self):
        files = offload.card_files(str(self.card), {"Orchard"})
        plan = offload.plan_copy(files, str(self.card), str(self.nas), {"Orchard": "Orchard"})
        result = offload.copy_items(plan, cancelled=lambda: True)
        self.assertEqual(result.copied, [])
        self.assertFalse([p for p in self.nas.rglob("*") if p.is_file()])

    def test_human(self):
        self.assertEqual(offload.human_size(3.9e9), "3.9 GB")
        self.assertEqual(offload.human_time(75), "1 min 15 s")
        self.assertEqual(offload.human_time(None), "—")


class ReportTest(unittest.TestCase):
    def rec(self, name, **kw):
        values = dict(path=f"/nas/P/{name}", size=1, mtime=0, sample_rate=48000, bits=24, channels=3,
                      frames=48000 * 90, project="Orchard", date="2026-08-25", scene="1", take="01",
                      tc_rate="24000/1001", time_reference=48000 * 3600, recorder="SoundDev: 833 WS123",
                      tracks=["Boom", "Lav-1", "Lav-2"])
        values.update(kw)
        return Recording(**values)

    def test_detected_fields(self):
        fields = report.detected_fields([self.rec("1T01_ISO.wav"), self.rec("1T02_ISO.wav", date="2026-08-26")])
        self.assertEqual(fields["project"], "Orchard")
        self.assertEqual(fields["recorder"], "Sound Devices 833")  # no serial number
        self.assertEqual(fields["sample_rate"], "48 kHz")
        self.assertEqual(fields["frame_rate"], "23.976")
        self.assertEqual(fields["file_type"], "Poly (ISO) WAV")
        self.assertEqual(fields["date"], "08/25/2026 – 08/26/2026")

    def test_rows_in_recording_order(self):
        late = self.rec("b.wav", time_reference=48000 * 7200, circled=True, note="good")
        early = self.rec("a.wav")
        rows = report.report_rows([late, early], ["file", "circled", "notes"])
        self.assertEqual([r[0] for r in rows], ["a.wav", "b.wav"])
        self.assertEqual(rows[1][1:], ["★", "good"])

    def test_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "r.csv")
            info = report.ReportInfo({"project": "Orchard", "mixer": "Alex Mixer"}, "Boom on 1")
            report.write_csv(path, info, [self.rec("1T01_ISO.wav", circled=True)])
            info.columns = ["file", "circled"]
            report.write_csv(path + "2", info, [self.rec("1T01_ISO.wav", circled=True)])
            with open(path + "2", encoding="utf-8-sig") as f:
                self.assertIn(["1T01_ISO.wav", "yes"], list(csv.reader(f)))
            with open(path, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0], ["SOUND REPORT"])
        self.assertIn(["Sound Mixer", "Alex Mixer"], rows)
        self.assertIn(["Comments", "Boom on 1"], rows)
        header = rows.index(["Filename", "Scene", "Take", "TC Start", "Dur", "Trk1", "Trk2", "Trk3", "Notes"])
        self.assertEqual(rows[header + 1][5:8], ["Boom", "Lav-1", "Lav-2"])

    def test_selectable_columns(self):
        recs = [self.rec("a.wav", tracks=["Boom", "Lav-1"]), self.rec("b.wav", tracks=["Boom"])]
        keys, headers, rows = report.table(recs, ["file", "track_columns", "end_tc", "bogus"])
        self.assertEqual(headers, ["Filename", "Trk1", "Trk2", "TC End"])
        self.assertEqual(rows[1], ["b.wav", "Boom", "", recs[1].end_tc])
        html = report.build_html(report.ReportInfo({"project": "P"}, columns=["file", "take"]), recs)
        self.assertRegex(html, r"<th align='left' nowrap width='\d+%' [^>]*>Take</th>")
        self.assertNotIn("circled takes in bold", html)

    def test_column_widths(self):
        keys, headers = ["file", "take", "notes"], ["File Name", "Take", "Notes"]
        name = "5roomtoneT01_ISO.wav"
        widths = report.column_widths(keys, headers, [[name, "01", "n" * 500]])
        self.assertEqual(sum(widths), 100)
        self.assertGreaterEqual(widths[0] * report.LINE_CHARS["landscape"] / 100, len(name))  # not cut
        self.assertGreaterEqual(widths[1], 3)  # "Take" keeps room for its header
        self.assertGreater(widths[2], widths[0])  # the long note gets the rest

    def test_crowded_page_still_adds_up(self):
        keys = ["file"] + [f"track:{n}" for n in range(12)] + ["notes"]
        headers = ["File Name"] + [f"Tr {n + 1}" for n in range(12)] + ["Notes"]
        rows = [["x" * 40] + ["Lav-10 safe"] * 12 + ["n" * 300]]
        widths, points = report.column_layout(keys, headers, rows, "portrait")
        self.assertEqual(sum(widths), 100)
        self.assertLess(points, report.TABLE_POINTS)  # text made smaller to fit
        self.assertGreaterEqual(points, report.MIN_TABLE_POINTS)
        _, landscape_points = report.column_layout(keys[:5], headers[:5], [rows[0][:5]], "landscape")
        self.assertEqual(landscape_points, report.TABLE_POINTS)

    def test_portrait_is_narrower(self):
        keys, headers = ["file", "scene", "take", "notes"], ["File Name", "Scene", "Take", "Notes"]
        rows = [["1T01_ISO.wav", "1", "01", "note"]]
        landscape = report.column_widths(keys, headers, rows, "landscape")
        portrait = report.column_widths(keys, headers, rows, "portrait")
        self.assertGreater(portrait[2], landscape[2])  # the same text takes a bigger share of a narrower page

    def test_pdf_orientation(self):
        app = qt_app()  # noqa: F841
        with tempfile.TemporaryDirectory() as folder:
            for orientation in ("portrait", "landscape"):
                path = os.path.join(folder, f"{orientation}.pdf")
                report.write_pdf(path, report.ReportInfo({"project": "P"}, orientation=orientation),
                                 [self.rec("a.wav")])
                data = Path(path).read_bytes()
                box = re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", data)
                width, height = float(box.group(1)), float(box.group(2))
                self.assertEqual(width > height, orientation == "landscape")

    def test_boxed_style(self):
        recs = [self.rec("8MT01_ISO.wav", note="plane overhead")]
        boxed = report.build_html(report.ReportInfo({"project": "P"}), recs)
        self.assertIn("border-collapse:collapse", boxed)
        self.assertIn("Notes:</span> plane overhead", boxed)
        self.assertNotIn(">Notes</th>", boxed)  # a row per take, not a column
        self.assertIn(">Trk3</th>", boxed)
        circled = report.build_html(report.ReportInfo({"project": "P"}), [self.rec("a.wav", circled=True)])
        self.assertIn("<tr style='font-weight:600'>", circled)  # bold even without a ★ column
        listed = report.build_html(report.ReportInfo({"project": "P"}, style="list"), recs)
        self.assertIn(">Notes</th>", listed)

    def test_branding(self):
        recs = [self.rec("a.wav")]
        plain = report.build_html(report.ReportInfo({"project": "P"}), recs)
        self.assertIn("<img src='logo'", plain)  # built-in logo by default
        brand = report.Branding(logo="", title="Production Sound Report", company="Acme Audio · acme.test",
                                accent="#003366", footer="call 555-0100")
        html = report.build_html(report.ReportInfo({"project": "P"}, branding=brand), recs)
        self.assertNotIn("<img", html)
        self.assertIn("Production Sound Report", html)
        self.assertIn("Acme Audio · acme.test", html)
        self.assertIn("bgcolor='#003366' style='color:#ffffff'", html)  # light text on a dark header
        light = report.build_html(report.ReportInfo({"project": "P"}, branding=report.Branding(accent="#ffee00")),
                                  recs)
        self.assertIn("style='color:#000000'", light)
        self.assertEqual(report.Branding.from_dict({"title": "X", "bogus": 1, "logo_height": "90"}).logo_height, 90)
        self.assertEqual(report.Branding(logo="/does/not/exist.png").logo_path(), "")

    def test_contact_placement(self):
        fields = {"project": "P", "mixer": "Alex Mixer", "phone": "555-0100", "email": "alex@example.com"}
        top = report.build_html(report.ReportInfo(fields), [self.rec("a.wav")])
        self.assertIn("555-0100", top)
        self.assertEqual(report.footer_text(report.ReportInfo(fields)), "")
        brand = report.Branding(contact_in_header=False, contact_in_footer=True, footer="acme.test")
        bottom = report.ReportInfo(fields, branding=brand)
        self.assertNotIn("555-0100", report.build_html(bottom, [self.rec("a.wav")]))
        self.assertEqual(report.footer_text(bottom), "acme.test   ·   Alex Mixer · 555-0100 · alex@example.com")
        self.assertFalse(report.Branding.from_dict({"contact_in_header": "false"}).contact_in_header)

    def test_pdf_footer(self):
        app = qt_app()  # noqa: F841
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "r.pdf")
            report.write_pdf(path, report.ReportInfo({"project": "P"}, branding=report.Branding(
                logo="", footer="Footer 555-0100")), [self.rec("a.wav")])
            self.assertGreater(os.path.getsize(path), 1000)

    def test_date_tapes_are_not_rolls(self):
        self.assertEqual(report.detected_fields([self.rec("a.wav", tape="26Y08M20")])["roll"], "")
        self.assertEqual(report.detected_fields([self.rec("a.wav", tape="BIRCH")])["roll"], "BIRCH")

    def test_basename(self):
        info = report.ReportInfo({"project": "A/B"})
        self.assertEqual(report.default_basename(info, [self.rec("x.wav")]), "A-B Sound Report 2026-08-25")


if __name__ == "__main__":
    unittest.main()
