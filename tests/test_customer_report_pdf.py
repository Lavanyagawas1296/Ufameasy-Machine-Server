"""Usage report PDF: one upright A4 page, same wording as the page, a real attachment download.

Calls the report code directly (no starlette TestClient), so it runs without httpx.
"""

import json
import os
import re
import sqlite3
import tempfile
import unittest
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from server import customer_config as config
from server import customer_report as report
from server import customer_report_pdf as pdf
from server import customer_routes, db

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=IST)
_ENV = ("CUSTOMER_MODE", "CUSTOMER_SHOW_OPERATOR", "CUSTOMER_SHIFT_START", "CUSTOMER_SHIFT_END", "CUSTOMER_MACHINE_LABEL")


def page_text(document: bytes) -> str:
    """Text drawn on the page (the content stream's string operands)."""
    stream = re.search(rb"stream\n(.*?)\nendstream", document, re.S).group(1)
    content = zlib.decompress(stream).decode("cp1252")
    return "\n".join(item.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                     for item in re.findall(r"\(((?:[^()\\]|\\.)*)\) Tj", content))


class UsageReportPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.telemetry = base / "telemetry.u5d"
        conn = sqlite3.connect(self.telemetry)
        conn.execute("""CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, event_id TEXT, timestamp TEXT,
            category TEXT, severity TEXT, source TEXT, session_id TEXT, event_type TEXT, payload TEXT)""")
        conn.commit()
        conn.close()
        self.saved = (config.TELEMETRY_DB_PATH, config.CUSTOMER_DEVICE_ID, config.TELEMETRY_TZ, db.DB_PATH, db.DATA_DIR)
        self.saved_env = {name: os.environ.get(name) for name in _ENV}
        config.TELEMETRY_DB_PATH = self.telemetry
        config.CUSTOMER_DEVICE_ID = "device_001"
        config.TELEMETRY_TZ = "Asia/Kolkata"
        db.DB_PATH = base / "params.db"
        db.DATA_DIR = base / "data"
        db.init_db()
        os.environ["CUSTOMER_MODE"] = "0"
        for name in _ENV[1:]:
            os.environ.pop(name, None)
        os.environ["CUSTOMER_MACHINE_LABEL"] = "Router (Bay 3)"
        self.next_id = 1

    def tearDown(self):
        (config.TELEMETRY_DB_PATH, config.CUSTOMER_DEVICE_ID, config.TELEMETRY_TZ, db.DB_PATH, db.DATA_DIR) = self.saved
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temp_dir.cleanup()

    def _event(self, when, event_type, payload, severity="INFO"):
        ts = when.strftime("%d/%m/%Y %I:%M:%S.%f")[:-3] + when.strftime(" %p")
        body = {"event_type": event_type, "timestamp": ts, "session_id": "Untitled", "user": "System", **payload}
        conn = sqlite3.connect(self.telemetry)
        conn.execute("INSERT INTO telemetry_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (self.next_id, f"e{self.next_id}", ts, "machine", severity, "test", "Untitled", event_type, json.dumps(body)))
        conn.commit()
        conn.close()
        self.next_id += 1

    def _jobs(self, count, *, results=("completed",)):
        start = datetime(DAY.year, DAY.month, DAY.day, 9, 0, tzinfo=IST)
        self._event(start - timedelta(minutes=5), "MachineConnectionEvent", {"state": "connected"})
        for index in range(count):
            begin = start + timedelta(minutes=8 * index)
            result = results[index % len(results)]
            job = {"job_id": f"j{index}", "gcode_file": f"part_{index}.gcode"}
            self._event(begin, "PrintJobEvent", {"status": "started", "operator": "asha", **job})
            self._event(begin, "StateChangedEvent", {"parameter_key": "execution_state", "new_value": "running"})
            end = {"status": result, "elapsed_seconds": 300.0, **job}
            if result == "failed":
                end["error_message"] = "Emergency stop active"
            self._event(begin + timedelta(minutes=5), "PrintJobEvent", end)
            self._event(begin + timedelta(minutes=5), "StateChangedEvent", {"parameter_key": "execution_state", "new_value": "idle"})

    def _render(self, start=DAY, end=DAY):
        rep = report.usage_report("device_001", start, end, now=NOW)
        return rep, pdf.render(rep)

    def assert_one_a4_page(self, document):
        self.assertTrue(document.startswith(b"%PDF-1.4"))
        self.assertTrue(document.rstrip().endswith(b"%%EOF"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b(?!s)", document)), 1)
        self.assertIn(b"/MediaBox [0 0 595.28 841.89] /Rotate 0", document)       # A4, upright
        startxref = int(re.search(rb"startxref\n(\d+)", document).group(1))
        self.assertTrue(document[startxref:].startswith(b"xref"))

    def test_one_upright_a4_page_for_1_15_and_60_jobs(self):
        for count in (1, 15, 60):
            with self.subTest(jobs=count):
                self.tearDown()
                self.setUp()
                self._jobs(count)
                rep, document = self._render()
                self.assertEqual(len(rep["jobs"]), count)
                self.assert_one_a4_page(document)
                text = page_text(document)
                self.assertEqual(text.count(" min\n") >= min(count, 15), True)
                if count > 15:
                    self.assertIn(f"+ {count - 15} more jobs in the CSV download", text)
                else:
                    self.assertNotIn("more jobs in the CSV download", text)
                self.assertIn(f"Total: {count} {'job' if count == 1 else 'jobs'}", text)

    def test_page_wording_header_and_footer(self):
        self._jobs(3, results=("completed", "stopped", "failed"))
        _rep, document = self._render(DAY, DAY + timedelta(days=1))
        text = page_text(document)
        for expected in ("Usage report", "How the machine was used in the selected period.",
                         "Machine: ", "Router (Bay 3)", "Period: ", "10 Nov 2026 to 11 Nov 2026",
                         "Generated: ", "11 Nov 2026, 12:00 IST", "Reviewed by:", "Date:",
                         "jobs started", "stopped before the end", "Machine faults",
                         "emergency stop or lost connection", "Parts completed", "Operator",
                         "Stopped by operator", "Emergency stop active", "Machine fault",
                         "Running", "Paused", "Idle", "Off", "Times are in IST. Run time excludes paused time."):
            self.assertIn(expected, text)
        for removed in ("sign-ins", "operator names are turned off", "Not reported by UFAMeasy",
                        "set on the server", "own log", "M0", "E-stop", "device_001",
                        "utilisation", "shift time", "Shift time counted", "LOGO"):
            self.assertNotIn(removed, text)

    def test_logo_is_drawn_from_the_svg(self):
        logo = pdf._logo()
        self.assertIsNotNone(logo, "customer_ui/unnati.svg should convert to PDF paths")
        width, height, ops = logo
        self.assertEqual((width, height), (648.0, 716.0))
        _rep, document = self._render()
        stream = zlib.decompress(re.search(rb"stream\n(.*?)\nendstream", document, re.S).group(1))
        self.assertIn(ops, stream)
        self.assertEqual(ops.count(b"\nf"), 4)                 # four filled shapes

    def test_operator_not_recorded_when_server_sends_none(self):
        self._jobs(1)
        rep = report.usage_report("device_001", DAY, DAY, now=NOW)
        self.assertNotIn("operator", rep["jobs"][0])        # CUSTOMER_SHOW_OPERATOR is off
        text = page_text(pdf.render(rep))
        self.assertIn("Not recorded", text)
        self.assertNotIn("asha", text)

    def test_jobs_run_equals_completed_plus_stopped_plus_faults(self):
        self._jobs(10, results=("completed", "completed", "stopped", "aborted", "failed"))
        rep, _document = self._render()
        counts = pdf.job_counts(rep)
        self.assertEqual(counts, {"started": 10, "completed": 4, "stopped": 4, "faults": 2, "running": 0, "no_end": 0})
        self.assertEqual(counts["started"], counts["completed"] + counts["stopped"] + counts["faults"]
                         + counts["running"] + counts["no_end"])
        self.assertIn("40% of jobs started", page_text(pdf.render(rep)))

    def test_wording_helpers(self):
        self.assertEqual(pdf.fmt_duration(120), "2 min")
        self.assertEqual(pdf.fmt_duration(4200), "1 h 10 min")
        self.assertEqual(pdf.fmt_duration(3600), "1 h")
        self.assertEqual(pdf.fmt_duration(20), "< 1 min")
        self.assertEqual(pdf.fmt_duration(0), "0 min")
        self.assertEqual(pdf.fmt_date(date(2026, 11, 17)), "17 Nov 2026")
        self.assertEqual(pdf.fmt_period({"from": "2026-11-16", "to": "2026-11-17"}), "16 Nov 2026 to 17 Nov 2026")
        self.assertEqual(pdf.day_note({"is_future": False, "running_seconds": 0, "paused_seconds": 0, "idle_seconds": 0}), "No activity")
        self.assertEqual(pdf.day_note({"is_future": False, "running_seconds": 120, "paused_seconds": 0, "idle_seconds": 840}),
                         "Run 2 min, idle 14 min")
        self.assertEqual(pdf.reason_text({"result": "stopped", "reason": ""}), "Stopped by operator")
        self.assertEqual(pdf.reason_text({"result": "failed", "reason": "Fault (reason not reported)"}), "Machine fault")
        self.assertIsNone(pdf.reason_text({"result": "unrecorded", "reason": ""}))
        self.assertEqual(pdf.reason_text({"result": "completed", "reason": ""}), "")

    def test_month_uses_day_by_day_bars_on_one_page(self):
        self._jobs(4)
        _rep, document = self._render(date(2026, 10, 12), DAY)       # 30 days
        self.assert_one_a4_page(document)
        self.assertIn("Each bar shows one day's shift hours (09:00–18:00).", page_text(document))

    def test_section_titles_match_the_page(self):
        page = (Path(__file__).resolve().parent.parent / "customer_ui" / "index.html").read_text(encoding="utf-8")
        titles = re.findall(r'<h2 id="(?:timelineTitle|jobsTitle|aboutTitle)">([^<]+)</h2>', page)
        cards = re.findall(r'<article class="card kpi[^"]*"><h2>([^<]+)</h2>', page)
        self.assertEqual(len(titles), 3)
        self._jobs(1)
        text = page_text(self._render()[1])
        for title in titles + cards:
            self.assertIn(title, text)

    def test_download_is_an_attachment(self):
        self._jobs(2)
        response = customer_routes.download_report_pdf(from_value="2026-11-10", to="2026-11-10",
                                                       customer_device_id="device_001")
        self.assertEqual(response.media_type, "application/pdf")
        self.assertEqual(response.headers["content-disposition"],
                         "attachment; filename*=UTF-8''usage_report_2026-11-10_2026-11-10.pdf")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assert_one_a4_page(response.body)


if __name__ == "__main__":
    unittest.main()
