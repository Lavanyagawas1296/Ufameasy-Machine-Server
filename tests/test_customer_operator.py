"""Per-job operator, job_id pairing and failed jobs in the customer stats.

Calls customer_stats directly (no starlette TestClient), so these tests run
without the optional httpx dependency that tests/test_customer_api.py needs.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server import customer_config as config
from server import customer_stats, db
from server.customer_routes import _csv_stream

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
_ENV_NAMES = ("CUSTOMER_MODE", "CUSTOMER_SHOW_OPERATOR")


class CustomerOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.telemetry_path = base / "telemetry.u5d"
        conn = sqlite3.connect(self.telemetry_path)
        try:
            conn.execute(
                """CREATE TABLE telemetry_events (
                    id INTEGER PRIMARY KEY, event_id TEXT, timestamp TEXT, category TEXT,
                    severity TEXT, source TEXT, session_id TEXT, event_type TEXT, payload TEXT)"""
            )
            conn.commit()
        finally:
            conn.close()

        self.saved = (config.TELEMETRY_DB_PATH, config.CUSTOMER_DEVICE_ID, config.TELEMETRY_TZ,
                      db.DB_PATH, db.DATA_DIR)
        self.saved_env = {name: os.environ.get(name) for name in _ENV_NAMES}
        config.TELEMETRY_DB_PATH = self.telemetry_path
        config.CUSTOMER_DEVICE_ID = "customer-test-device"
        config.TELEMETRY_TZ = "Asia/Kolkata"
        db.DB_PATH = base / "params.db"
        db.DATA_DIR = base / "data"
        db.init_db()
        # Set explicitly: a developer .env may hold CUSTOMER_MODE=1.
        os.environ["CUSTOMER_MODE"] = "0"
        os.environ.pop("CUSTOMER_SHOW_OPERATOR", None)
        customer_stats.clear_overview_cache()
        self.next_id = 1

    def tearDown(self):
        (config.TELEMETRY_DB_PATH, config.CUSTOMER_DEVICE_ID, config.TELEMETRY_TZ,
         db.DB_PATH, db.DATA_DIR) = self.saved
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        customer_stats.clear_overview_cache()
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------

    def _event(self, minutes_ago, event_type, payload, *, session_id="doc-1", severity="INFO"):
        when = NOW - timedelta(minutes=minutes_ago)
        timestamp = when.isoformat().replace("+00:00", "Z")
        full = {"event_type": event_type, "timestamp": timestamp, "session_id": session_id, **payload}
        conn = sqlite3.connect(self.telemetry_path)
        try:
            conn.execute(
                """INSERT INTO telemetry_events
                (id, event_id, timestamp, category, severity, source, session_id, event_type, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.next_id, f"e-{self.next_id}", timestamp, "machine", severity, "test",
                 session_id, event_type, json.dumps(full)),
            )
            conn.commit()
        finally:
            conn.close()
        self.next_id += 1

    def _job(self, minutes_ago, status, *, session_id="doc-1", **fields):
        severity = {"stopped": "WARNING", "failed": "ERROR"}.get(status, "INFO")
        self._event(minutes_ago, "PrintJobEvent", {"status": status, **fields},
                    session_id=session_id, severity=severity)

    def _attempts(self):
        return customer_stats._closed_job_attempts()

    def _history(self):
        return customer_stats.customer_history("customer-test-device", "24h", now=NOW)

    # -- failed jobs -------------------------------------------------------

    def test_failed_job_is_closed_and_counted(self):
        self._job(30, "started", gcode_file="estop.gcode")
        self._job(20, "failed", gcode_file="estop.gcode", error_message="Emergency stop active")

        self.assertEqual([(s, f) for _t, s, f, _o in self._attempts()], [("failed", "estop.gcode")])
        self.assertFalse(customer_stats._has_active_job())
        summary = customer_stats.job_summary("24h", now=NOW)
        self.assertEqual(
            {k: summary[k] for k in ("count", "completed", "stopped", "failed")},
            {"count": 1, "completed": 0, "stopped": 0, "failed": 1},
        )
        self.assertEqual(self._history()["files"][0]["display_name"], "estop.gcode")

    def test_failed_job_does_not_swallow_the_next_job(self):
        self._job(50, "started", gcode_file="a.gcode")
        self._job(45, "failed", gcode_file="a.gcode")
        self._job(40, "started", gcode_file="b.gcode")
        self._job(30, "completed", gcode_file="b.gcode")

        self.assertEqual([(s, f) for _t, s, f, _o in self._attempts()],
                         [("failed", "a.gcode"), ("completed", "b.gcode")])
        summary = customer_stats.job_summary("24h", now=NOW)
        self.assertEqual((summary["count"], summary["completed"], summary["failed"]), (2, 1, 1))

    # -- new fields: operator, job_id, gcode_file ---------------------------

    def test_operator_field_is_frozen_at_start(self):
        # Lock-screen re-login mid-job: the end event names someone else.
        self._job(30, "started", job_id="j1", operator="alice", user="alice (Operator)")
        self._job(10, "stopped", job_id="j1", operator="bob", user="bob (Supervisor)")

        self.assertEqual(self._attempts()[0][3], "alice")

    def test_operator_field_wins_over_legacy_user(self):
        self._job(30, "started", operator="carol", user="master (Master)")
        self._job(10, "completed")

        self.assertEqual(self._attempts()[0][3], "carol")

    def test_job_id_pairs_interleaved_jobs_in_one_session(self):
        self._job(50, "started", job_id="j1", gcode_file="one.gcode", operator="alice")
        self._job(40, "started", job_id="j2", gcode_file="two.gcode", operator="bob")
        self._job(30, "completed", job_id="j1", gcode_file="one.gcode")
        self._job(20, "stopped", job_id="j2", gcode_file="two.gcode")

        self.assertEqual(
            [(s, f, o) for _t, s, f, o in self._attempts()],
            [("completed", "one.gcode", "alice"), ("stopped", "two.gcode", "bob")],
        )

    def test_job_id_pairs_across_a_document_change(self):
        self._job(30, "started", session_id="doc-A", job_id="j1", gcode_file="part.gcode")
        self._job(10, "completed", session_id="doc-B", job_id="j1", gcode_file="part.gcode")

        self.assertEqual([(s, f) for _t, s, f, _o in self._attempts()], [("completed", "part.gcode")])
        self.assertFalse(customer_stats._has_active_job())

    def test_gcode_file_is_preferred_over_other_file_keys(self):
        self._job(30, "started", gcode_file="real_part.gcode")
        self._job(10, "completed", file_name="other.nc")

        self.assertEqual(self._attempts()[0][2], "real_part.gcode")

    def test_temp_controller_name_falls_back_to_reported_file(self):
        self._event(40, "StateChangedEvent", {"parameter_key": "current_file", "new_value": "bracket.gcode"})
        self._job(30, "started", gcode_file="ufameasy_gcode_abc123.gcd")
        self._job(10, "completed", gcode_file="ufameasy_gcode_abc123.gcd")

        self.assertEqual(self._attempts()[0][2], "bracket.gcode")

    # -- legacy `user` parsing ---------------------------------------------

    def test_legacy_user_is_parsed_at_the_last_parenthesis(self):
        cases = {
            "alice (Operator)": "alice",
            "Bob Smith (Supervisor)": "Bob Smith",
            "line (2) lead (Maintenance)": "line (2) lead",
            "dave": "dave",
            "  eve   (Master) ": "eve",
            "Unknown (Operator)": "Unknown",   # before login: names nobody
            "system": "Unknown",               # workbench fallback without RBAC
            "": "Unknown",
        }
        for raw, expected in cases.items():
            with self.subTest(user=raw):
                self.assertEqual(customer_stats._job_operator({"user": raw}), expected)

    def test_operator_is_unknown_when_nothing_is_reported(self):
        self.assertEqual(customer_stats._job_operator({}, {}), "Unknown")
        self.assertEqual(customer_stats._job_operator({"user": 42, "operator": None}), "Unknown")

    def test_operator_is_cleaned_and_capped(self):
        name = customer_stats._job_operator({"operator": "ann\x00\x1b[31m  lee" + "x" * 100})
        self.assertNotIn("\x00", name)
        self.assertNotIn("\x1b", name)
        self.assertLessEqual(len(name), 64)
        self.assertTrue(name.startswith("ann[31m lee"))

    def test_legacy_user_on_start_event_names_the_job(self):
        self._job(30, "started", user="frank (Maintenance)")
        self._job(10, "stopped", user="grace (Supervisor)")

        self.assertEqual(self._attempts()[0][3], "frank")

    # -- CUSTOMER_SHOW_OPERATOR flag ---------------------------------------

    def _two_runs(self):
        self._job(50, "started", gcode_file="part.gcode", user="alice (Operator)")
        self._job(45, "completed", gcode_file="part.gcode")
        self._job(30, "started", gcode_file="part.gcode", operator="bob")
        self._job(20, "stopped", gcode_file="part.gcode")

    def test_flag_off_by_default_hides_operator(self):
        self._two_runs()
        files = self._history()["files"]
        self.assertEqual(files[0]["runs"], 2)
        self.assertNotIn("last_operator", files[0])
        self.assertNotIn("bob", json.dumps(self._history()))

    def test_flag_set_to_zero_hides_operator(self):
        os.environ["CUSTOMER_SHOW_OPERATOR"] = "0"
        self._two_runs()
        self.assertNotIn("last_operator", self._history()["files"][0])

    def test_flag_on_shows_operator_of_newest_run(self):
        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        self._two_runs()
        self.assertEqual(self._history()["files"][0]["last_operator"], "bob")

    def test_operator_never_in_log_messages_or_csv(self):
        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        self._job(30, "started", operator="zelda", user="zelda (Operator)", job_id="j1")
        self._job(10, "failed", operator="zelda", user="zelda (Operator)", job_id="j1",
                  error_message="Emergency stop active")

        records, _cursor = customer_stats.customer_logs(
            severity=None, start=NOW - timedelta(hours=1), end=NOW, cursor=None, limit=100,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual({r["message"] for r in records}, {"Print job started", "Print job failed"})
        self.assertNotIn("zelda", json.dumps(records))
        self.assertNotIn("zelda", "".join(_csv_stream(records)))

    # -- old payloads ------------------------------------------------------

    def test_old_status_only_payloads_pair_by_session(self):
        self._job(50, "started", session_id="s1")
        self._job(40, "paused", session_id="s1")
        self._job(35, "resumed", session_id="s1")
        self._job(30, "stopped", session_id="s1")
        self._job(20, "completed", session_id="s2")     # no start: ignored
        self._job(10, "started", session_id="s3")       # no end: still active

        attempts = self._attempts()
        self.assertEqual([(s, f, o) for _t, s, f, o in attempts], [("stopped", "", "Unknown")])
        self.assertTrue(customer_stats._has_active_job())
        summary = customer_stats.job_summary("24h", now=NOW)
        self.assertEqual((summary["count"], summary["stopped"], summary["failed"]), (1, 1, 0))

    def test_server_session_fallback_reports_unknown_operator(self):
        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        conn = sqlite3.connect(db.DB_PATH)
        try:
            when = (NOW - timedelta(hours=1)).isoformat()
            conn.execute(
                """INSERT INTO sessions (session_id, device_id, file_name, total_layers, status,
                started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("s-old", "customer-test-device", "legacy.gcode", 0, "complete", when, when),
            )
            conn.commit()
        finally:
            conn.close()

        files = self._history()["files"]
        self.assertEqual(files[0]["display_name"], "legacy.gcode")
        self.assertEqual(files[0]["last_operator"], "Unknown")


if __name__ == "__main__":
    unittest.main()
