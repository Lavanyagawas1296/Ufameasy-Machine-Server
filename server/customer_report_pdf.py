"""One-page A4 portrait PDF of the customer usage report, with no third-party dependency.

The layout mirrors the print stylesheet in customer_ui/app.css (same sections,
sizes in millimetres and wording), so "Download PDF" and "Print" give the same
page: header, summary cards, timeline, the 15 newest jobs with a totals row,
two notes and a sign-off line. Text uses the PDF core fonts (Helvetica).
"""

from __future__ import annotations

import re
import zlib
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from server import customer_stats as stats

PAGE_W, PAGE_H = 210.0, 297.0
MARGIN = 12.0
LEFT, RIGHT = MARGIN, PAGE_W - MARGIN
CONTENT_W = RIGHT - LEFT
MAX_JOB_ROWS = 15
TIMELINE_ROW_DAYS = 7          # longer periods are drawn as one bar per day

_PT = 72 / 25.4                # points per millimetre

TEXT = "#1A2433"
MUTED = "#5F6B7C"
BORDER = "#CDD5E0"
RULE = "#E2E7EE"
NAVY = "#17388A"
TRACK_EDGE = "#9AA6B5"
STATE_FILL = {"running": "#1E8E45", "paused": "#E08A1E", "idle": "#BAC4D1"}
FAULT = "#C62828"
TONE = {"good": "#1E7F3E", "warn": "#B35F06", "bad": "#C62828"}
PILL = {
    "completed": ("#E8F6EC", "#1E7F3E"),
    "stopped": ("#FDF1E3", "#A55606"),
    "failed": ("#FCE9E9", "#C62828"),
    "running": ("#E8EEFB", NAVY),
    "unrecorded": ("#EEF1F5", MUTED),
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Advance widths (1/1000 em) of the core fonts for ASCII 32..126, WinAnsi encoding.
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
    722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
    278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778,
    722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
    278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)
_OTHER_WIDTHS = {"–": 556, "—": 1000, "·": 278, "…": 1000, "÷": 584, "×": 584}

RESULT_VIEW = {
    "completed": ("Completed", "completed"),
    "stopped": ("Stopped early", "stopped"),
    "aborted": ("Stopped early", "stopped"),
    "failed": ("Machine fault", "failed"),
    "running": ("Running", "running"),
    "unrecorded": ("No end recorded", "unrecorded"),
    "open": ("No end recorded", "unrecorded"),
}


# -- wording shared with customer_ui/app.js -------------------------------------

def fmt_date(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def fmt_weekday_date(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {fmt_date(day)}"


def fmt_period(period: dict[str, Any]) -> str:
    start, end = date.fromisoformat(period["from"]), date.fromisoformat(period["to"])
    return fmt_date(start) if start == end else f"{fmt_date(start)} to {fmt_date(end)}"


def fmt_duration(seconds: float | None) -> str:
    """"2 min", "1 h 10 min", "3 h"; "< 1 min" for a few seconds."""
    if seconds is None:
        return "—"
    total = max(0, int(float(seconds) + 0.5))
    if total == 0:
        return "0 min"
    if total < 60:
        return "< 1 min"
    hours, minutes = divmod(int(total / 60 + 0.5), 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h" if not minutes else f"{hours} h {minutes} min"


def tz_label(moment: datetime) -> str:
    name = moment.strftime("%Z")
    return "IST" if name in {"Asia/Kolkata", "IST", "UTC+05:30"} else name or "local time"


def operator_name(job: dict[str, Any]) -> str | None:
    name = job.get("operator")
    return name if isinstance(name, str) and name and name != stats.UNKNOWN_OPERATOR else None


def reason_text(job: dict[str, Any]) -> str | None:
    """Why a job ended; "" when nothing applies (completed or running), None when not recorded."""
    result = job.get("result")
    if result in {"stopped", "aborted"}:
        return "Stopped by operator"
    if result == "failed":
        reported = job.get("reason") or ""
        if reported and "[redacted]" not in reported and not reported.startswith("Fault (reason not reported"):
            return reported
        return "Machine fault"
    if result in {"unrecorded", "open"}:
        return None
    return ""


def day_note(day: dict[str, Any]) -> str:
    if day.get("is_future"):
        return ""
    run, paused, idle = day["running_seconds"], day["paused_seconds"], day["idle_seconds"]
    if not (run or paused or idle):
        return "No activity"
    parts = [f"Run {fmt_duration(run)}"]
    if paused:
        parts.append(f"paused {fmt_duration(paused)}")
    parts.append(f"idle {fmt_duration(idle)}")
    return ", ".join(parts)


def job_counts(report: dict[str, Any]) -> dict[str, int]:
    """Jobs started = completed + stopped early + machine faults + not finished.

    ``jobs_summary.stopped_early`` counts every early end, faults included, so
    faults are taken out here to keep the four numbers adding up.
    """
    summary = report["jobs_summary"]
    running = sum(job["result"] == "running" for job in report["jobs"])
    return {
        "started": summary["count"] + summary["not_closed"],
        "completed": summary["completed"],
        "stopped": max(0, summary["stopped_early"] - summary["faults"]),
        "faults": summary["faults"],
        "running": running,
        "no_end": max(0, summary["not_closed"] - running),
    }


def jobs_note(counts: dict[str, int]) -> str:
    parts = []
    if counts["running"]:
        parts.append(f"{counts['running']} still running")
    if counts["no_end"]:
        parts.append(f"{counts['no_end']} with no end recorded")
    return f"incl. {' and '.join(parts)}" if parts else ""


def about_lines(report: dict[str, Any], tz_name: str) -> tuple[str, str]:
    shift = f"{report['shift']['start']}–{report['shift']['end']}"
    second = (
        f"The timeline shows shift hours ({shift}); the notes on the right cover the whole day."
        if len(report["days"]) <= TIMELINE_ROW_DAYS
        else f"Each bar shows one day's shift hours ({shift})."
    )
    return f"Times are in {tz_name}. Run time excludes paused time.", second


def day_fractions(day: dict[str, Any]) -> dict[str, float]:
    fractions = {"running": 0.0, "paused": 0.0, "idle": 0.0}
    for segment in day["segments"]:
        if segment["state"] in fractions:
            fractions[segment["state"]] += segment["width"]
    return fractions


def bar_labels(days: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Day labels under the day-by-day bars, and the month line below them."""
    dates = [date.fromisoformat(day["date"]) for day in days]
    if len(dates) <= 31:
        months = [
            f"{_MONTHS[d.month - 1]} {d.year}" if i == 0 or d.day == 1 else ""
            for i, d in enumerate(dates)
        ]
        return [str(d.day) for d in dates], months
    weekly = [
        f"{d.day} {_MONTHS[d.month - 1]}" if i % 7 == 0 and i < len(dates) - 3 else ""   # none clipped at the right edge
        for i, d in enumerate(dates)
    ]
    return weekly, [""] * len(dates)


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def axis_ticks(shift: dict[str, str]) -> list[tuple[float, str]]:
    start, end = _minutes(shift["start"]), _minutes(shift["end"])
    span = end - start
    step = 120 if span > 12 * 60 else 60
    first = -(-start // 60) * 60
    return [
        ((t - start) / span, f"{(t // 60) % 24:02d}:{t % 60:02d}")
        for t in range(first, end + 1, step)
    ]


# -- drawing --------------------------------------------------------------------

def text_width(text: str, size: float, bold: bool = False) -> float:
    """Width of ``text`` in millimetres."""
    table = _HELVETICA_BOLD if bold else _HELVETICA
    units = 0
    for char in text:
        code = ord(char)
        units += table[code - 32] if 32 <= code < 127 else _OTHER_WIDTHS.get(char, 556)
    return units * size / 1000 / _PT


def fit(text: str, size: float, width: float, bold: bool = False) -> str:
    if text_width(text, size, bold) <= width:
        return text
    while text and text_width(text + "…", size, bold) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(text: str, size: float, width: float, max_lines: int, bold: bool = False) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, size, bold) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [fit(line, size, width, bold) for line in lines]


def _pdf_text(value: str) -> bytes:
    clean = "".join(char for char in value if char >= " ")
    raw = clean.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _rgb(color: str) -> str:
    value = color.lstrip("#")
    return " ".join(f"{int(value[i:i + 2], 16) / 255:.3f}" for i in (0, 2, 4))


def _n(value: float) -> str:
    return f"{value:.2f}"


class _Canvas:
    """Top-left origin, millimetres; writes a PDF content stream."""

    def __init__(self) -> None:
        self.ops: list[bytes] = []

    def _op(self, text: str) -> None:
        self.ops.append(text.encode("ascii"))

    @staticmethod
    def _x(mm: float) -> float:
        return mm * _PT

    @staticmethod
    def _y(mm: float) -> float:
        return (PAGE_H - mm) * _PT

    def _paint(self, path: str, fill: str | None, stroke: str | None, width: float, dash: tuple[float, float] | None) -> None:
        ops = ["q"]
        if fill:
            ops.append(f"{_rgb(fill)} rg")
        if stroke:
            ops.append(f"{_rgb(stroke)} RG {_n(width)} w")
            if dash:
                ops.append(f"[{_n(dash[0] * _PT)} {_n(dash[1] * _PT)}] 0 d")
        ops.append(path)
        ops.append("B" if fill and stroke else "f" if fill else "S")
        ops.append("Q")
        self._op(" ".join(ops))

    def rect(self, x, y, w, h, fill=None, stroke=None, width=0.5, dash=None) -> None:
        path = f"{_n(self._x(x))} {_n(self._y(y + h))} {_n(w * _PT)} {_n(h * _PT)} re"
        self._paint(path, fill, stroke, width, dash)

    def round_rect(self, x, y, w, h, r, fill=None, stroke=None, width=0.5, dash=None) -> None:
        k = r * 0.5523
        x0, x1, y0, y1 = self._x(x), self._x(x + w), self._y(y + h), self._y(y)
        rr, kk = r * _PT, k * _PT
        path = " ".join([
            f"{_n(x0 + rr)} {_n(y0)} m",
            f"{_n(x1 - rr)} {_n(y0)} l",
            f"{_n(x1 - rr + kk)} {_n(y0)} {_n(x1)} {_n(y0 + rr - kk)} {_n(x1)} {_n(y0 + rr)} c",
            f"{_n(x1)} {_n(y1 - rr)} l",
            f"{_n(x1)} {_n(y1 - rr + kk)} {_n(x1 - rr + kk)} {_n(y1)} {_n(x1 - rr)} {_n(y1)} c",
            f"{_n(x0 + rr)} {_n(y1)} l",
            f"{_n(x0 + rr - kk)} {_n(y1)} {_n(x0)} {_n(y1 - rr + kk)} {_n(x0)} {_n(y1 - rr)} c",
            f"{_n(x0)} {_n(y0 + rr)} l",
            f"{_n(x0)} {_n(y0 + rr - kk)} {_n(x0 + rr - kk)} {_n(y0)} {_n(x0 + rr)} {_n(y0)} c h",
        ])
        self._paint(path, fill, stroke, width, dash)

    def line(self, x1, y1, x2, y2, color, width=0.5, dash=None) -> None:
        path = f"{_n(self._x(x1))} {_n(self._y(y1))} m {_n(self._x(x2))} {_n(self._y(y2))} l"
        self._paint(path, None, color, width, dash)

    def triangle_down(self, cx, top, size, color) -> None:
        half = size / 2
        path = (f"{_n(self._x(cx - half))} {_n(self._y(top))} m {_n(self._x(cx + half))} {_n(self._y(top))} l "
                f"{_n(self._x(cx))} {_n(self._y(top + size))} l h")
        self._paint(path, color, None, 0, None)

    def hatch(self, x, y, w, h, color, step=1.1, width=0.45) -> None:
        """Diagonal stripes inside a rectangle (paused time, so it reads in black and white)."""
        ops = ["q", f"{_n(self._x(x))} {_n(self._y(y + h))} {_n(w * _PT)} {_n(h * _PT)} re W n",
               f"{_rgb(color)} RG {_n(width)} w"]
        offset = -h
        while offset < w:
            ops.append(f"{_n(self._x(x + offset))} {_n(self._y(y + h))} m {_n(self._x(x + offset + h))} {_n(self._y(y))} l")
            offset += step
        ops.append("S Q")
        self._op(" ".join(ops))

    def text(self, x, baseline, value, size, *, bold=False, color=TEXT, align="left") -> None:
        if not value:
            return
        if align == "right":
            x -= text_width(value, size, bold)
        elif align == "center":
            x -= text_width(value, size, bold) / 2
        font = "F2" if bold else "F1"
        self.ops.append(
            f"BT {_rgb(color)} rg /{font} {_n(size)} Tf {_n(self._x(x))} {_n(self._y(baseline))} Td (".encode("ascii")
            + _pdf_text(value) + b") Tj ET"
        )

    def stream(self) -> bytes:
        return b"\n".join(self.ops)


def _document(content: bytes, title: str) -> bytes:
    stream = zlib.compress(content)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /PrintScaling /None >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Rotate 0 "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Title (" + _pdf_text(title) + b") /Producer (UFAMeasy) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


# -- sections -------------------------------------------------------------------

LOGO_PATH = Path(__file__).resolve().parent.parent / "customer_ui" / "unnati.svg"
LOGO_HEIGHT = 16.0             # mm, same as the print header


@lru_cache(maxsize=1)
def _logo() -> tuple[float, float, bytes] | None:
    """The company logo as (width, height, PDF path operators in SVG units), or None.

    The SVG uses only absolute M/L/C/Z path commands with an rgb() fill, so each
    path maps straight onto PDF m/l/c/h operators.
    """
    try:
        svg = LOGO_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    box = re.search(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"', svg)
    if not box:
        return None
    ops: list[str] = []
    for d, fill in re.findall(r'<path[^>]*?\bd="([^"]*)"[^>]*?\bfill="rgb\(([\d\s,]+)\)"', svg):
        if re.search(r"[^MLCZ\d\s,.\-eE]", d):
            return None                                   # a command this converter does not handle
        r, g, b = (int(part) / 255 for part in fill.split(","))
        ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        for command, args in re.findall(r"([MLCZ])([^MLCZ]*)", d):
            numbers = " ".join(re.findall(r"-?\d*\.?\d+(?:[eE][-+]?\d+)?", args))
            ops.append({"M": f"{numbers} m", "L": f"{numbers} l", "C": f"{numbers} c", "Z": "h"}[command])
        ops.append("f")
    if not ops:
        return None
    return float(box.group(1)), float(box.group(2)), "\n".join(ops).encode("ascii")


def _draw_logo(c: _Canvas, x: float, top: float) -> float:
    """Draw the logo with its top-left corner at (x, top); returns its width in mm."""
    logo = _logo()
    if logo is None:
        c.round_rect(x, top, LOGO_HEIGHT, LOGO_HEIGHT, 1.5, stroke=TRACK_EDGE, width=0.7, dash=(1.2, 0.9))
        return LOGO_HEIGHT
    width, height, ops = logo
    scale = LOGO_HEIGHT / height * _PT                    # SVG units -> points, y flipped
    c.ops.append(f"q {scale:.5f} 0 0 {-scale:.5f} {_n(c._x(x))} {_n(c._y(top))} cm".encode("ascii") + b"\n" + ops + b"\nQ")
    return width * LOGO_HEIGHT / height


def _header(c: _Canvas, report: dict[str, Any], generated: datetime) -> None:
    _draw_logo(c, LEFT, 12)
    c.text(LEFT + 20, 19.5, "Usage report", 16, bold=True)
    c.text(LEFT + 20, 25.2, "How the machine was used in the selected period.", 8, color=MUTED)
    rows = (
        ("Machine: ", fit(str(report.get("machine_label") or "—"), 8, 62, bold=True)),
        ("Period: ", fmt_period(report["period"])),
        ("Generated: ", f"{fmt_date(generated.date())}, {generated:%H:%M} {tz_label(generated)}"),
    )
    for index, (label, value) in enumerate(rows):
        baseline = 16.6 + index * 4.4
        value_w = text_width(value, 8, bold=True)
        c.text(RIGHT - value_w, baseline, value, 8, bold=True)
        c.text(RIGHT - value_w, baseline, label, 8, color=MUTED, align="right")
    c.line(LEFT, 30.5, RIGHT, 30.5, NAVY, 0.9)


def _card(c: _Canvas, x, y, w, h, label, value, subs, tone=None, waiting=False) -> None:
    c.round_rect(x, y, w, h, 1.8, fill="#FAFBFC" if waiting else "#FFFFFF", stroke=BORDER, width=0.6,
                 dash=(1.4, 1.0) if waiting else None)
    c.text(x + 3, y + 4.9, label, 7.5, color=MUTED)
    c.text(x + 3, y + 11.4, value, 14, bold=True, color=TONE.get(tone, MUTED if waiting else TEXT))
    lines: list[str] = []
    for sub in subs:
        if sub:
            lines.extend(wrap(sub, 6.6, w - 6, 2 - len(lines)))
    for index, line in enumerate(lines[:2]):
        c.text(x + 3, y + 15.0 + index * 2.9, line, 6.6, color=MUTED)


def _kpis(c: _Canvas, report: dict[str, Any], top: float) -> float:
    totals = report["totals"]
    counts = job_counts(report)
    gap, h = 3.0, 19.0
    w2 = (CONTENT_W - gap) / 2
    first_row = (
        ("Total run time", fmt_duration(totals["running_seconds"]),
         [f"Paused {fmt_duration(totals['paused_seconds'])} · idle {fmt_duration(totals['idle_seconds'])}"], None, False),
        ("Parts completed", "—", ["Not counted yet"], None, True),
    )
    for index, (label, value, subs, tone, waiting) in enumerate(first_row):
        _card(c, LEFT + index * (w2 + gap), top, w2, h, label, value, subs, tone, waiting)
    started = counts["started"]
    w4 = (CONTENT_W - 3 * gap) / 4
    second_row = (
        ("Jobs run", str(started), ["jobs started", jobs_note(counts)], None),
        ("Completed", str(counts["completed"]),
         [f"{round(100 * counts['completed'] / started)}% of jobs started" if started else ""], "good"),
        ("Stopped early", str(counts["stopped"]), ["stopped before the end"], "warn"),
        ("Machine faults", str(counts["faults"]), ["emergency stop or lost connection"], "bad"),
    )
    row2 = top + h + gap
    for index, (label, value, subs, tone) in enumerate(second_row):
        _card(c, LEFT + index * (w4 + gap), row2, w4, h, label, value, subs, tone)
    return row2 + h


def _legend(c: _Canvas, top: float) -> float:
    x, baseline = LEFT, top + 2.6
    items = (("running", "Running"), ("paused", "Paused"), ("idle", "Idle"), ("off", "Off"), ("fault", "Machine fault"))
    for state, label in items:
        if state == "fault":
            c.rect(x + 1.1, top, 0.9, 3.2, fill=FAULT)
        else:
            c.rect(x, top + 0.3, 3.4, 2.6, fill=STATE_FILL.get(state, "#FFFFFF"), stroke=TRACK_EDGE, width=0.4)
            if state == "paused":
                c.hatch(x, top + 0.3, 3.4, 2.6, "#FFFFFF")
                c.rect(x, top + 0.3, 3.4, 2.6, stroke=TRACK_EDGE, width=0.4)
        c.text(x + 4.6, baseline, label, 6.8, color=MUTED)
        x += 4.6 + text_width(label, 6.8) + 5
    return top + 3.4


def _timeline_rows(c: _Canvas, report: dict[str, Any], top: float) -> float:
    label_w, note_w, gap = 27.0, 48.0, 2.5
    track_x = LEFT + label_w + 1
    track_w = CONTENT_W - label_w - 1 - note_w - gap
    note_x = RIGHT - note_w
    pitch, track_h = 7.0, 4.5
    for index, day in enumerate(report["days"]):
        row = top + index * pitch
        ty = row + 1.0
        c.text(LEFT, row + 4.3, fmt_weekday_date(date.fromisoformat(day["date"])), 7.3)
        c.rect(track_x, ty, track_w, track_h, fill="#FFFFFF")
        if day["is_future"]:
            c.hatch(track_x, ty, track_w, track_h, "#DCE1E8", step=1.4, width=0.35)
        for segment in day["segments"]:
            fill = STATE_FILL.get(segment["state"])
            if not fill:
                continue
            sx, sw = track_x + segment["left"] * track_w, max(0.15, segment["width"] * track_w)
            c.rect(sx, ty, sw, track_h, fill=fill)
            if segment["state"] == "paused":
                c.hatch(sx, ty, sw, track_h, "#FFFFFF")
        c.rect(track_x, ty, track_w, track_h, stroke=TRACK_EDGE, width=0.45)
        for at in day["faults"]:
            c.rect(track_x + at * track_w - 0.45, ty - 1.0, 0.9, track_h + 2.0, fill=FAULT)
        lines = wrap(day_note(day), 6.6, note_w, 2)
        first = row + 4.3 if len(lines) < 2 else row + 2.9
        for line_index, line in enumerate(lines):
            c.text(note_x, first + line_index * 2.7, line, 6.6, color=MUTED)
    axis_y = top + len(report["days"]) * pitch + 2.4
    ticks = axis_ticks(report["shift"])
    for index, (pos, label) in enumerate(ticks):
        align = "left" if pos <= 0.001 else "right" if pos >= 0.999 else "center"
        c.text(track_x + pos * track_w, axis_y, label, 6.3, color=MUTED, align=align)
    return _legend(c, axis_y + 2.6)


def _timeline_bars(c: _Canvas, report: dict[str, Any], top: float) -> float:
    days = report["days"]
    shift_seconds = (_minutes(report["shift"]["end"]) - _minutes(report["shift"]["start"])) * 60
    axis_w = 11.0
    plot_x, plot_w = LEFT + axis_w, CONTENT_W - axis_w
    chart_top, chart_h = top + 3.0, 24.0
    bottom = chart_top + chart_h
    c.text(plot_x - 1.5, chart_top + 2.2, fmt_duration(shift_seconds), 6.3, color=MUTED, align="right")
    c.text(plot_x - 1.5, bottom, "0 h", 6.3, color=MUTED, align="right")
    c.line(plot_x, bottom, RIGHT, bottom, TRACK_EDGE, 0.5)
    slot = plot_w / len(days)
    bar_w = max(0.8, slot * 0.7)
    labels, months = bar_labels(days)
    for index, day in enumerate(days):
        x = plot_x + index * slot + (slot - bar_w) / 2
        cx = x + bar_w / 2
        if day["is_future"]:
            c.rect(x, chart_top, bar_w, chart_h, stroke="#C9D0DA", width=0.35, dash=(0.8, 0.6))
        else:
            c.rect(x, chart_top, bar_w, chart_h, fill="#FFFFFF")
            y = bottom
            fractions = day_fractions(day)
            for state in ("running", "paused", "idle"):
                height = min(fractions[state], 1.0) * chart_h
                if height <= 0:
                    continue
                y -= height
                c.rect(x, y, bar_w, height, fill=STATE_FILL[state])
                if state == "paused":
                    c.hatch(x, y, bar_w, height, "#FFFFFF")
            c.rect(x, chart_top, bar_w, chart_h, stroke=TRACK_EDGE, width=0.4)
        if day["faults"]:
            c.triangle_down(cx, chart_top - 2.6, min(2.2, max(1.2, bar_w)), FAULT)
        c.text(cx, bottom + 3.0, labels[index], 6.0, color=MUTED, align="center")
        if months[index]:
            c.text(x, bottom + 5.8, months[index], 6.0, color=MUTED)
    end = bottom + (6.6 if any(months) else 3.8)
    return _legend(c, end + 1.2)


def _timeline(c: _Canvas, report: dict[str, Any], top: float) -> float:
    shift = f"{report['shift']['start']}–{report['shift']['end']}"
    c.text(LEFT, top + 3.6, "Timeline", 10, bold=True)
    c.text(LEFT + text_width("Timeline", 10, True) + 3, top + 3.6, f"Shift hours {shift}", 7.5, color=MUTED)
    body = top + 6.5
    if len(report["days"]) <= TIMELINE_ROW_DAYS:
        return _timeline_rows(c, report, body)
    return _timeline_bars(c, report, body)


# Job Execution History: column widths in millimetres (sum = CONTENT_W)
_COLUMNS = (("Date", 19), ("Operator", 25), ("File", 44), ("Start", 11), ("End", 11),
            ("Run time", 17), ("Result", 24), ("Reason", 35))


def _jobs(c: _Canvas, report: dict[str, Any], top: float, tz) -> float:
    jobs = report["jobs"]
    c.text(LEFT, top + 3.6, "Job Execution History", 10, bold=True)
    hint = f"Latest {len(jobs)} jobs, newest first" if report.get("jobs_truncated") else "Newest first"
    c.text(LEFT + text_width("Job Execution History", 10, True) + 3, top + 3.6, hint, 7.5, color=MUTED)
    y = top + 6.0
    xs = [LEFT]
    for _name, width in _COLUMNS:
        xs.append(xs[-1] + width)
    c.rect(LEFT, y, CONTENT_W, 5.5, fill="#F1F4F8")
    for index, (name, width) in enumerate(_COLUMNS):
        c.text(xs[index] + 1.5, y + 3.7, name, 7.0, color=MUTED)
    y += 5.5
    if not jobs:
        c.text(LEFT + 1.5, y + 4.2, "No jobs in this period.", 7.3, color=MUTED)
        return y + 6.0
    row_h, size = 5.2, 7.3
    for job in jobs[:MAX_JOB_ROWS]:
        c.line(LEFT, y, RIGHT, y, RULE, 0.5)
        started = stats.parse_server_timestamp(job["started_at"]).astimezone(tz)
        ended = stats.parse_server_timestamp(job["ended_at"]) if job.get("ended_at") else None
        label, key = RESULT_VIEW.get(job["result"], (str(job["result"]), "unrecorded"))
        operator = operator_name(job)
        reason = reason_text(job)
        file_name = job["display_name"] if job.get("file_name") else None
        cells = (
            (fmt_date(started.date()), False),
            (operator or "Not recorded", operator is None),
            (file_name or "Not recorded", file_name is None),
            (f"{started:%H:%M}", False),
            (f"{ended.astimezone(tz):%H:%M}" if ended else "—", ended is None),
            (fmt_duration(job["run_seconds"]) if job["result"] != "running" else "—", job["result"] == "running"),
        )
        baseline = y + 3.5
        for index, (value, muted) in enumerate(cells):
            width = _COLUMNS[index][1] - 3
            c.text(xs[index] + 1.5, baseline, fit(value, size, width), size, color=MUTED if muted else TEXT)
        bg, fg = PILL[key]
        pill_w = text_width(label, 6.6, True) + 3.4
        c.round_rect(xs[6] + 1.2, y + 1.0, pill_w, 3.3, 1.65, fill=bg)
        c.text(xs[6] + 2.9, y + 3.35, label, 6.6, bold=True, color=fg)
        reason_value = "Not recorded" if reason is None else (reason or "—")
        c.text(xs[7] + 1.5, baseline, fit(reason_value, size, _COLUMNS[7][1] - 3), size,
               color=TEXT if reason else MUTED)
        y += row_h
    hidden = len(jobs) - MAX_JOB_ROWS
    if hidden > 0:
        c.line(LEFT, y, RIGHT, y, RULE, 0.5)
        c.text(LEFT + 1.5, y + 3.5, f"+ {hidden} more {'job' if hidden == 1 else 'jobs'} in the CSV download", 7.0, color=MUTED)
        y += row_h
    c.line(LEFT, y, RIGHT, y, TEXT, 0.8)
    total = sum(job["run_seconds"] or 0 for job in jobs if job["result"] != "running")
    c.text(LEFT + 1.5, y + 3.9, f"Total: {len(jobs)} {'job' if len(jobs) == 1 else 'jobs'}", size, bold=True)
    c.text(xs[5] + 1.5, y + 3.9, fmt_duration(total), size, bold=True)
    return y + 5.6


def _about(c: _Canvas, report: dict[str, Any], top: float, tz_name: str) -> None:
    c.text(LEFT, top + 3.0, "About this report", 7.5, bold=True, color=MUTED)
    for index, line in enumerate(about_lines(report, tz_name)):
        c.text(LEFT, top + 6.2 + index * 3.0, line, 6.8, color=MUTED)


def _footer(c: _Canvas) -> None:
    baseline = PAGE_H - MARGIN - 1.5
    c.text(LEFT, baseline, "Reviewed by:", 8.5, color=TEXT)
    start = LEFT + text_width("Reviewed by:", 8.5) + 2
    c.line(start, baseline + 0.8, start + 70, baseline + 0.8, TEXT, 0.6)
    date_x = start + 80
    c.text(date_x, baseline, "Date:", 8.5, color=TEXT)
    date_start = date_x + text_width("Date:", 8.5) + 2
    c.line(date_start, baseline + 0.8, date_start + 40, baseline + 0.8, TEXT, 0.6)


def render(report: dict[str, Any]) -> bytes:
    """The report as a one-page A4 portrait PDF."""
    tz = stats._telemetry_timezone()
    generated = stats.parse_server_timestamp(report["generated_at"]).astimezone(tz)
    tz_name = tz_label(generated)
    c = _Canvas()
    _header(c, report, generated)
    y = _kpis(c, report, 34.0)
    y = _timeline(c, report, y + 4.0)
    y = _jobs(c, report, y + 4.5, tz)
    _about(c, report, y + 2.0, tz_name)
    _footer(c)
    title = f"Usage report {fmt_period(report['period'])}"
    return _document(c.stream(), title)
