"""Sound reports: the table data (pure), CSV output, and a branded PDF rendered
with Qt's text engine (needs a QGuiApplication, but no widgets)."""

from __future__ import annotations

import csv
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from html import escape
from pathlib import Path

from .catalog import Recording
from .timecode import format_duration

ASSETS = Path(__file__).with_name("assets")

# Header fields in the order they are shown; (key, label).
HEADER_FIELDS = [
    ("project", "Project"), ("date", "Date"), ("mixer", "Sound Mixer"), ("phone", "Phone"), ("email", "Email"),
    ("client", "Production / Client"), ("director", "Director"), ("producer", "Producer"),
    ("roll", "Roll / Card"), ("recorder", "Recorder"), ("file_type", "File Type"),
    ("sample_rate", "Sample Rate"), ("bit_depth", "Bit Depth"), ("frame_rate", "Frame Rate"),
    ("tone", "Tone Level"),
]
# Remembered across all reports (the mixer's own details).
PERSONAL_FIELDS = ("mixer", "phone", "email", "tone")
# Remembered per project.
PROJECT_FIELDS = ("client", "director", "producer")

# Table columns: key -> (header, description). "Wrap" columns (long text) share
# the leftover width; all others stay on one line and are only as wide as needed.
COLUMN_DEFS = {
    "file": ("Filename", "File name"),
    "scene": ("Scene", "Scene"),
    "take": ("Take", "Take"),
    "circled": ("★", "Circled take (★)"),
    "start_tc": ("TC Start", "Start timecode"),
    "end_tc": ("TC End", "End timecode"),
    "length": ("Dur", "Duration"),
    "channels": ("Ch.", "Number of channels"),
    "tracks": ("Tracks", "Track names (one column)"),
    "track_columns": ("Trk", "Track names (a box per track: Trk1, Trk2, ...)"),
    "notes": ("Notes", "Notes"),
    "date": ("Date", "Recording date"),
    "time": ("Time", "Time of day recorded"),
    "folder": ("Day Folder", "Recorder day folder / tape"),
    "fps": ("FPS", "Timecode frame rate"),
    "format": ("Format", "Sample rate · bit depth"),
    "file_type": ("Type", "Mono / Poly (ISO / LR)"),
    "size": ("Size", "File size"),
}
DEFAULT_COLUMNS = ["file", "scene", "take", "start_tc", "length", "track_columns", "notes"]
STYLES = {"boxed": "Boxed (notes under each take)", "list": "List (notes as a column)"}
WRAP_COLUMNS = {"tracks", "notes", "file"}
# 833 "tape" names are the day folder (26Y08M20), not a roll.
_DATE_TAPE_RE = re.compile(r"^\d{2}Y\d{2}M\d{2}$")


BUILTIN_LOGO = "builtin"


@dataclass
class Branding:
    """What makes a report look like yours. logo: BUILTIN_LOGO, "" (none) or
    the path of an image; accent: header colour ("" = the style's default)."""
    logo: str = BUILTIN_LOGO
    logo_height: int = 80  # px at 96 dpi
    title: str = "Sound Report"
    company: str = ""  # a line under the title, e.g. company name, website
    accent: str = ""
    footer: str = ""  # printed at the bottom left of every page
    contact_in_header: bool = True  # the mixer's phone and email in the report's top section
    contact_in_footer: bool = False  # ... and/or on every page's footer

    def logo_path(self) -> str:
        if self.logo == BUILTIN_LOGO:
            return str(ASSETS / "logo-black.png")
        return self.logo if self.logo and os.path.isfile(self.logo) else ""

    @classmethod
    def from_dict(cls, values: dict) -> "Branding":
        known = {f: values[f] for f in cls.__dataclass_fields__ if f in values}
        try:
            known["logo_height"] = int(known.get("logo_height", 80))
        except (TypeError, ValueError):
            known.pop("logo_height", None)
        for flag in ("contact_in_header", "contact_in_footer"):
            if flag in known and isinstance(known[flag], str):  # QSettings may hand back strings
                known[flag] = known[flag].lower() == "true"
        return cls(**known)


def _text_on(color: str) -> str:
    """Black or white text, whichever reads better on the colour."""
    color = color.lstrip("#")
    try:
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#000000"
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


@dataclass
class ReportInfo:
    fields: dict[str, str] = field(default_factory=dict)
    comments: str = ""
    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    orientation: str = "landscape"  # or "portrait"
    style: str = "boxed"  # or "list"
    branding: Branding = field(default_factory=Branding)

    def get(self, key: str) -> str:
        return self.fields.get(key, "").strip()


def _most_common(values) -> str:
    values = [v for v in values if v]
    if not values:
        return ""
    counts = Counter(values)
    if len(counts) == 1:
        return values[0]
    return " / ".join(v for v, _ in counts.most_common())


def detected_fields(recs: list[Recording]) -> dict[str, str]:
    """Header values that can be read from the files themselves."""
    recorders = [recorder_name(r.recorder) for r in recs]
    dates = sorted({r.date for r in recs if r.date})
    kinds = []
    for rec in recs:
        stem = Path(rec.name).stem.upper()
        kind = "Poly" if rec.channels > 1 else "Mono"
        if stem.endswith("_ISO"):
            kind += " (ISO)"
        elif stem.endswith("_LR"):
            kind += " (LR)"
        kinds.append(f"{kind} WAV")
    return {
        "project": _most_common([r.project for r in recs]),
        "date": _format_dates(dates),
        "recorder": _most_common(recorders),
        "file_type": " + ".join(sorted(set(kinds))),
        "sample_rate": _most_common([f"{r.sample_rate / 1000:g} kHz" for r in recs if r.sample_rate]),
        "bit_depth": _most_common([f"{r.bits}-bit{' float' if r.float_samples else ''}" for r in recs if r.bits]),
        "frame_rate": _most_common([r.rate_label for r in recs]),
        "roll": _most_common([r.tape for r in recs if not _DATE_TAPE_RE.match(r.tape)]),
    }


def recorder_name(originator: str) -> str:
    """"SoundDev: 833 WS0000000000" -> "Sound Devices 833" (no serial number)."""
    text = originator.strip()
    if text.startswith("SoundDev:"):
        model = text.split(":", 1)[1].split()
        return "Sound Devices " + (model[0] if model else "")
    return text


def _format_dates(dates: list[str]) -> str:
    def us(text):
        try:
            return date.fromisoformat(text).strftime("%m/%d/%Y")
        except ValueError:
            return text
    if not dates:
        return ""
    if len(dates) == 1:
        return us(dates[0])
    return f"{us(dates[0])} – {us(dates[-1])}"


def sort_for_report(recs: list[Recording]) -> list[Recording]:
    """In recording order (date + start time), which is how post reads them."""
    return sorted(recs, key=lambda r: (r.date, r.time_reference if r.time_reference is not None else -1, r.name))


def _file_type(rec: Recording) -> str:
    stem = Path(rec.name).stem.upper()
    kind = "Poly" if rec.channels > 1 else "Mono"
    if stem.endswith("_ISO"):
        kind += " ISO"
    elif stem.endswith("_LR"):
        kind += " LR"
    return kind


def _cell(rec: Recording, key: str) -> str:
    if key == "file":
        return rec.name
    if key == "circled":
        return "★" if rec.circled else ""
    if key == "start_tc":
        return rec.start_tc
    if key == "end_tc":
        return rec.end_tc
    if key == "length":
        return format_duration(rec.duration)
    if key == "channels":
        return str(rec.channels or "")
    if key == "tracks":
        return ", ".join(t for t in rec.tracks if t)
    if key == "notes":
        return rec.note
    if key == "folder":
        return rec.tape or os.path.basename(rec.folder)
    if key == "fps":
        return rec.rate_label
    if key == "format":
        return f"{rec.sample_rate / 1000:g} kHz · {rec.bits}-bit" if rec.sample_rate else ""
    if key == "file_type":
        return _file_type(rec)
    if key == "size":
        return f"{rec.size / 1e6:,.0f} MB"
    return str(getattr(rec, key, "") or "")


def table(recs: list[Recording], columns: list[str] | None = None) -> tuple[list[str], list[str], list[list[str]]]:
    """(column keys, headers, rows) for the chosen columns; "track_columns"
    expands to one column per track (Tr 1, Tr 2, ...)."""
    columns = [c for c in (columns or DEFAULT_COLUMNS) if c in COLUMN_DEFS]
    recs = sort_for_report(recs)
    track_count = max((len(r.tracks) for r in recs), default=0)
    keys, headers = [], []
    for key in columns:
        if key == "track_columns":
            for n in range(track_count):
                keys.append(f"track:{n}")
                headers.append(f"Trk{n + 1}")
        else:
            keys.append(key)
            headers.append(COLUMN_DEFS[key][0])
    rows = []
    for rec in recs:
        row = []
        for key in keys:
            if key.startswith("track:"):
                n = int(key.split(":")[1])
                row.append(rec.tracks[n] if n < len(rec.tracks) else "")
            else:
                row.append(_cell(rec, key))
        rows.append(row)
    return keys, headers, rows


# Usable page width (Letter minus 12 mm margins) and, without Qt, the
# characters that fit across it at 8 pt; plus how wide the long-text columns
# may grow before they wrap.
# The PDF font; column widths are measured with the same one. Noto Sans is not on a Mac.
REPORT_FONT = {"darwin": "Helvetica Neue", "win32": "Segoe UI"}.get(sys.platform, "Noto Sans")
PAGE_WIDTH_MM = {"landscape": 279.4 - 24, "portrait": 215.9 - 24}
LINE_CHARS = {"landscape": 175, "portrait": 130}
WRAP_CAPS = {"notes": 70, "tracks": 40}


def _qt_measure(orientation: str = "landscape"):
    """(measure(text, bold) -> width, usable line width) from real font metrics,
    or None when no Qt application is running (tests)."""
    try:
        from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication
    except ImportError:
        return None
    if QGuiApplication.instance() is None:
        return None
    regular = QFont(REPORT_FONT)
    regular.setPointSizeF(8)
    bold = QFont(regular)
    bold.setBold(True)
    metrics = {False: QFontMetricsF(regular), True: QFontMetricsF(bold)}
    # The usable page width, at the same DPI as the metrics.
    dpi = QGuiApplication.primaryScreen().logicalDotsPerInchX() if QGuiApplication.primaryScreen() else 96
    line = PAGE_WIDTH_MM.get(orientation, PAGE_WIDTH_MM["landscape"]) / 25.4 * dpi
    return (lambda text, is_bold=False: metrics[is_bold].horizontalAdvance(text)), line


TABLE_POINTS = 8.0
MIN_TABLE_POINTS = 6.0


def column_layout(keys: list[str], headers: list[str], rows: list[list[str]],
                  orientation: str = "landscape") -> tuple[list[int], float]:
    """(percent widths, table font size in pt). When the columns cannot fit
    at 8 pt even with notes/tracks/file names at their minimum, the table
    text is made smaller (down to 6 pt) before anything is squeezed."""
    # Order of giving way: notes, tracks, then a smaller font, then file names.
    widths, shortfall = _widths(keys, headers, rows, orientation, 1.0, shrink_files=False)
    if shortfall <= 1.0:
        return widths, TABLE_POINTS
    scale = max(MIN_TABLE_POINTS / TABLE_POINTS, 1.0 / shortfall)
    widths, _ = _widths(keys, headers, rows, orientation, scale, shrink_files=True)
    return widths, round(TABLE_POINTS * scale, 1)


def column_widths(keys: list[str], headers: list[str], rows: list[list[str]],
                  orientation: str = "landscape") -> list[int]:
    return column_layout(keys, headers, rows, orientation)[0]


def _widths(keys: list[str], headers: list[str], rows: list[list[str]], orientation: str,
            scale: float, shrink_files: bool = True) -> tuple[list[int], float]:
    """Percent widths: every column gets the width of its longest value or
    header (so short columns never wrap). If that is too wide for the page,
    notes and tracks give up space first (tracks never below their longest
    track name), then file names; only then does everything shrink."""
    qt = _qt_measure(orientation)
    if qt is None:
        base, pad = (lambda text, is_bold=False: len(text) * (1.1 if is_bold else 1.0)), 2.0
        line = LINE_CHARS.get(orientation, LINE_CHARS["landscape"])
        cap_unit = 1.0
    else:
        base, line = qt
        pad = base("00")  # cell padding (3 px each side) plus a little slack
        cap_unit = base("x")
    # A smaller table font makes every text narrower; the padding stays.
    measure = lambda text, is_bold=False: base(text, is_bold) * scale  # noqa: E731
    cap_unit *= scale
    def longest(i, texts):
        return max([measure(headers[i], True)] + [measure(t) for t in texts]) + pad

    need, minimum = [], []
    for i, key in enumerate(keys):
        cells = [row[i] for row in rows]
        full = longest(i, cells)
        if key == "notes":
            need.append(min(full, WRAP_CAPS["notes"] * cap_unit))
            minimum.append(max(measure(headers[i], True) + pad, 16 * cap_unit))
        elif key == "tracks":
            words = [w for t in cells for w in t.split(", ")]
            need.append(min(full, WRAP_CAPS["tracks"] * cap_unit))
            minimum.append(longest(i, words))  # never break inside a track name
        else:
            need.append(full)
            minimum.append(full)
    # Shrink notes and tracks first (down to their minimum), then file names,
    # and only then let everything overflow proportionally.
    over = sum(need) - line
    for group in (("notes",), ("tracks",), ("file",)) if shrink_files else (("notes",), ("tracks",)):
        if over <= 0:
            break
        members = [i for i, k in enumerate(keys) if k in group]
        if group == ("file",):
            for i in members:
                minimum[i] = max(measure(headers[i], True) + pad, 20 * cap_unit)
        slack = sum(need[i] - minimum[i] for i in members if need[i] > minimum[i])
        if slack <= 0:
            continue
        take = min(over, slack)
        for i in members:
            if need[i] > minimum[i]:
                need[i] -= take * (need[i] - minimum[i]) / slack
        over -= take
    shortfall = sum(need) / line
    total = max(sum(need), line)
    widths = [max(1, int(n * 100 / total + 0.999)) for n in need]  # round up: never too narrow
    spare = 100 - sum(widths)
    if spare > 0:
        # Spare room goes to the column that can use it: notes, tracks, file name.
        track_boxes = [i for i, k in enumerate(keys) if k.startswith("track:")]
        if "notes" in keys or "tracks" in keys or not track_boxes:
            target = next((keys.index(k) for k in ("notes", "tracks", "file") if k in keys), len(keys) - 1)
            widths[target] += spare
        else:
            # Boxed layout: the track boxes share the room evenly.
            for n, i in enumerate(track_boxes):
                widths[i] += spare // len(track_boxes) + (1 if n < spare % len(track_boxes) else 0)
    elif spare < 0:
        # Rounding up overshot on a crowded page: take it from the widest column.
        widest = max(range(len(widths)), key=lambda i: widths[i])
        widths[widest] += spare
    return widths, shortfall


def report_rows(recs: list[Recording], columns: list[str] | None = None) -> list[list[str]]:
    return table(recs, columns)[2]


def default_basename(info: ReportInfo, recs: list[Recording]) -> str:
    project = info.get("project") or "Sound"
    dates = sorted({r.date for r in recs if r.date})
    suffix = dates[0] if len(dates) == 1 else (f"{dates[0]} to {dates[-1]}" if dates else "")
    name = f"{project} Sound Report" + (f" {suffix}" if suffix else "")
    return "".join("-" if c in '/\\:*?"<>|' else c for c in name).strip()


def write_csv(path: str | os.PathLike, info: ReportInfo, recs: list[Recording]) -> None:
    """Same shape as the hand-made reports: title, header pairs, then the table."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["SOUND REPORT"])
        for key, label in HEADER_FIELDS:
            if info.get(key):
                writer.writerow([label, info.get(key)])
        if info.comments.strip():
            writer.writerow(["Comments", info.comments.strip()])
        writer.writerow([])
        keys, headers, rows = table(recs, info.columns)
        writer.writerow(["Circled" if k == "circled" else h for k, h in zip(keys, headers)])
        circled = keys.index("circled") if "circled" in keys else None
        for row in rows:
            if circled is not None:
                row[circled] = "yes" if row[circled] else ""
            writer.writerow(row)


def footer_text(info: ReportInfo) -> str:
    """The line at the bottom of every page: the branding footer, plus the
    mixer's phone and email when they are wanted there."""
    parts = [info.branding.footer.strip()] if info.branding.footer.strip() else []
    if info.branding.contact_in_footer:
        contact = " · ".join(v for v in (info.get("mixer"), info.get("phone"), info.get("email")) if v)
        if contact:
            parts.append(contact)
    return "   ·   ".join(parts)


def build_html(info: ReportInfo, recs: list[Recording]) -> str:
    """The report as simple HTML that QTextDocument renders (tables, no CSS
    layout). The logo is referenced as "logo" and added as a resource."""
    hidden = {"project"} | (set() if info.branding.contact_in_header else {"phone", "email"})
    header_cells = [(label, info.get(key)) for key, label in HEADER_FIELDS if info.get(key) and key not in hidden]
    keys, headers, rows = table(recs, info.columns)
    circled = keys.index("circled") if "circled" in keys else None
    boxed = info.style != "list"
    notes_at = keys.index("notes") if boxed and "notes" in keys else None
    if notes_at is not None:
        # Boxed: notes get their own full-width row under each take.
        notes = [row.pop(notes_at) for row in rows]
        keys, headers = keys[:notes_at] + keys[notes_at + 1:], headers[:notes_at] + headers[notes_at + 1:]
    circled = keys.index("circled") if "circled" in keys else None
    circled_takes = [r.circled for r in sort_for_report(recs)]  # same order as the rows
    wrap = [k in WRAP_COLUMNS for k in keys]
    rows_html = []
    for i, row in enumerate(rows):
        bold = circled_takes[i]
        cells = "".join(f"<td valign='top'{'' if w else ' nowrap'}>{escape(value)}</td>"
                        for value, w in zip(row, wrap))
        if boxed:
            style = " style='font-weight:600'" if bold else ""
            rows_html.append(f"<tr{style}>{cells}</tr>")
            if notes_at is not None:
                rows_html.append(f"<tr><td colspan='{len(keys)}' style='padding-bottom:6px'>"
                                 f"<span style='color:#555555'>Notes:</span> {escape(notes[i])}</td></tr>")
        else:
            bg = "#f0f0f0" if i % 2 else "#ffffff"
            weight = " style='font-weight:600'" if bold else ""
            rows_html.append(f"<tr bgcolor='{bg}'{weight}>{cells}</tr>")
    header_rows = []
    for i in range(0, len(header_cells), 3):
        cells = ""
        for label, value in header_cells[i:i + 3]:
            cells += (f"<td width='12%' style='color:#555555'>{escape(label)}</td>"
                      f"<td width='21%'><b>{escape(value)}</b></td>")
        header_rows.append(f"<tr>{cells}</tr>")
    total = sum(r.duration for r in recs)
    comments = (f"<p style='margin-top:6px'><span style='color:#555555'>Comments</span><br>"
                f"{escape(info.comments.strip()).replace(chr(10), '<br>')}</p>") if info.comments.strip() else ""
    widths, table_pt = column_layout(keys, headers, rows, info.orientation)
    brand = info.branding
    accent = brand.accent or ("#e4e4e4" if boxed else "#222222")
    on_accent = _text_on(accent)
    if boxed:
        columns_html = "".join(f"<th align='left' nowrap width='{w}%' bgcolor='{accent}' "
                               f"style='color:{on_accent}'>{escape(h)}</th>" for h, w in zip(headers, widths))
        table_attrs = ("border='1' cellspacing='0' cellpadding='3' "
                       "style='border-collapse:collapse; border-color:#000000; border-style:solid;")
    else:
        columns_html = "".join(
            f"<th align='left' nowrap width='{w}%' bgcolor='{accent}' style='color:{on_accent}'>{escape(h)}</th>"
            for h, w in zip(headers, widths))
        table_attrs = "border='0' cellspacing='0' cellpadding='3' style='"
    logo_cell = (f"<td width='{brand.logo_height + 10}' valign='middle'><img src='logo' "
                 f"height='{brand.logo_height}'></td>") if brand.logo_path() else ""
    company = (f"<br><span style='color:#555555'>{escape(brand.company)}</span>" if brand.company.strip() else "")
    return f"""
<html><body style="font-family:'{REPORT_FONT}','DejaVu Sans',sans-serif; font-size:8pt; color:#111111">
<table width="100%" cellspacing="0" cellpadding="0"><tr>
{logo_cell}
<td valign="middle"{' style="padding-left:12px"' if logo_cell else ''}>
<span style="font-size:20pt; font-weight:700">{escape(brand.title or 'Sound Report')}</span><br>
<span style="font-size:12pt">{escape(info.get('project'))}</span>{company}</td>
</tr></table>
<p></p>
<table width="100%" cellspacing="0" cellpadding="2">{''.join(header_rows)}</table>
{comments}
<p style="color:#555555">{len(recs)} files · {format_duration(total)} recorded{' · circled takes in bold' if any(circled_takes) else ''}</p>
<table width="100%" {table_attrs} font-size:{table_pt}pt'>
<thead><tr>{columns_html}</tr></thead>
{''.join(rows_html)}
</table>
</body></html>"""


def write_pdf(path: str | os.PathLike, info: ReportInfo, recs: list[Recording]) -> None:
    """Letter size, landscape or portrait (info.orientation), with page numbers."""
    from PySide6.QtCore import QMarginsF, QSizeF, QUrl
    from PySide6.QtGui import QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument

    writer = QPdfWriter(str(path))
    writer.setTitle(f"Sound Report {info.get('project')}".strip())
    writer.setCreator(info.get("mixer") or "Location Sound File Manager")
    orientation = (QPageLayout.Orientation.Portrait if info.orientation == "portrait"
                   else QPageLayout.Orientation.Landscape)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.Letter), orientation,
                                     QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter))
    writer.setResolution(144)
    document = QTextDocument()
    # Measure fonts in the PDF's resolution; otherwise the layout is made at
    # screen DPI and everything comes out shrunk.
    document.documentLayout().setPaintDevice(writer)
    if info.branding.logo_path():
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("logo"),
                             QImage(info.branding.logo_path()))
    document.setHtml(build_html(info, recs))
    document.setDocumentMargin(0)
    # Lay out at the page's width so text is not scaled down.
    rect = writer.pageLayout().paintRectPixels(writer.resolution())
    document.setPageSize(QSizeF(rect.width(), rect.height()))
    _print_with_page_numbers(document, writer, footer_text(info))


def _print_with_page_numbers(document, writer, footer_text: str = "") -> None:
    """QTextDocument.print_() only numbers pages when it chooses the page size
    itself, so paint page by page and add "page x of y" ourselves."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QAbstractTextDocumentLayout, QFont, QPainter

    painter = QPainter(writer)
    page = document.pageSize()
    footer = 22
    body_height = page.height() - footer
    document.setPageSize(page.__class__(page.width(), body_height))
    count = document.pageCount()
    font = QFont(document.defaultFont())
    font.setPointSizeF(7)
    for number in range(count):
        if number:
            writer.newPage()
        painter.save()
        painter.translate(0, -number * body_height)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.clip = QRectF(0, number * body_height, page.width(), body_height)
        painter.setClipRect(context.clip)
        document.documentLayout().draw(painter, context)
        painter.restore()
        painter.setFont(font)
        painter.setPen(Qt.GlobalColor.darkGray)
        painter.drawText(QRectF(0, body_height, page.width(), footer),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, f"Page {number + 1} of {count}")
        if footer_text.strip():
            painter.drawText(QRectF(0, body_height, page.width() * 0.8, footer),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, footer_text.strip())
    painter.end()
