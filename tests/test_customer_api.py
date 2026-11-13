import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import customer_config as config
from server import customer_stats, db
from server.auth import AUTH_COOKIE_NAME, create_access_token
from server.customer_routes import reset_rate_limits, router
from server.state_store import state


class CustomerApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.telemetry_path = Path(self.temp_dir.name) / "telemetry.u5d"
        self.params_path = Path(self.temp_dir.name) / "params.db"
        self._create_telemetry_db()

        self.original_telemetry_path = config.TELEMETRY_DB_PATH
        self.original_device_id = config.CUSTOMER_DEVICE_ID
        self.original_telemetry_tz = config.TELEMETRY_TZ
        self.original_db_path = db.DB_PATH
        self.original_data_dir = db.DATA_DIR
        self.original_env = {
            name: os.environ.get(name)
            for name in ("UFAM_JWT_SECRET", "RMC_AUTH_ENABLED", "RMC_AUTH_TOKENS_JSON")
        }
        with state.lock:
            self.saved_parameters = copy.deepcopy(state.parameters)
            self.saved_rmc_states = copy.deepcopy(state.rmc_states)
            state.parameters.clear()
            state.rmc_states.clear()

        config.TELEMETRY_DB_PATH = self.telemetry_path
        config.CUSTOMER_DEVICE_ID = "customer-test-device"
        config.TELEMETRY_TZ = "Asia/Kolkata"
        db.DB_PATH = self.params_path
        db.DATA_DIR = Path(self.temp_dir.name) / "data"
        db.init_db()
        os.environ["UFAM_JWT_SECRET"] = "customer-test-secret"
        viewer_token = "viewer-token"
        viewer_digest = hashlib.sha256(viewer_token.encode("utf-8")).hexdigest()
        os.environ["RMC_AUTH_ENABLED"] = "true"
        os.environ["RMC_AUTH_TOKENS_JSON"] = json.dumps(
            {viewer_digest: {"user": "viewer", "role": "viewer"}}
        )
        customer_stats.clear_overview_cache()
        reset_rate_limits()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.auth_headers = {"Authorization": f"Bearer {viewer_token}"}
        self.client.cookies.set(AUTH_COOKIE_NAME, create_access_token("customer-viewer"))

    def tearDown(self):
        config.TELEMETRY_DB_PATH = self.original_telemetry_path
        config.CUSTOMER_DEVICE_ID = self.original_device_id
        config.TELEMETRY_TZ = self.original_telemetry_tz
        db.DB_PATH = self.original_db_path
        db.DATA_DIR = self.original_data_dir
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        with state.lock:
            state.parameters.clear()
            state.parameters.update(self.saved_parameters)
            state.rmc_states.clear()
            state.rmc_states.update(self.saved_rmc_states)
        customer_stats.clear_overview_cache()
        reset_rate_limits()
        self.temp_dir.cleanup()

    def _create_telemetry_db(self):
        conn = sqlite3.connect(self.telemetry_path)
        try:
            conn.execute(
                """
                CREATE TABLE telemetry_events (
                    id INTEGER PRIMARY KEY,
                    event_id TEXT,
                    timestamp TEXT,
                    category TEXT,
                    severity TEXT,
                    source TEXT,
                    session_id TEXT,
                    event_type TEXT,
                    payload TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_naive_telemetry_and_print_job_timestamps_use_telemetry_timezone(self):
        expected = datetime(2026, 11, 5, 10, 20, 55, 297000, tzinfo=timezone.utc)
        self.assertEqual(
            customer_stats.parse_telemetry_timestamp("05/11/2026 03:50:55.297 PM"), expected
        )
        self._add_event(
            80, expected, "PrintJobEvent", {"status": "started"}, session_id="tz",
            raw_timestamp="05/11/2026 03:50:55.297 PM",
        )
        self.assertEqual(customer_stats._print_events()[0][0], expected)

    def test_server_session_timestamps_remain_utc(self):
        value = "2026-11-05T10:20:59.041979+00:00"
        self.assertEqual(
            customer_stats.parse_server_timestamp(value),
            datetime(2026, 11, 5, 10, 20, 59, 41979, tzinfo=timezone.utc),
        )
        self.assertEqual(
            customer_stats.parse_server_timestamp("2026-11-05T10:20:59"),
            datetime(2026, 11, 5, 10, 20, 59, tzinfo=timezone.utc),
        )

    def test_download_link_has_real_href_and_download_attribute(self):
        html = (Path(__file__).parents[1] / "customer_ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="downloadLogs" href="api/customer/logs/download" download="logs.csv"', html)

    def _add_event(self, event_id, when, event_type, payload, *, severity="INFO", session_id=None, raw_timestamp=None):
        timestamp = raw_timestamp or when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        full_payload = {
            "event_type": event_type,
            "timestamp": timestamp,
            "severity": severity,
            "session_id": session_id,
            **payload,
        }
        conn = sqlite3.connect(self.telemetry_path)
        try:
            conn.execute(
                """INSERT INTO telemetry_events
                (id, event_id, timestamp, category, severity, source, session_id, event_type, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    f"event-{event_id}",
                    timestamp,
                    "machine",
                    severity,
                    "test",
                    session_id,
                    event_type,
                    json.dumps(full_payload),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _job(self, event_id, when, session_id, status, file_name="job.gcode"):
        self._add_event(
            event_id,
            when,
            "PrintJobEvent",
            {"status": status, "gcode_file": file_name},
            severity="WARNING" if status == "stopped" else "INFO",
            session_id=session_id,
        )

    def _session(self, session_id, device_id, file_name, started_at):
        conn = sqlite3.connect(self.params_path)
        try:
            conn.execute(
                """INSERT INTO sessions
                (session_id, device_id, file_name, total_layers, status, started_at, ended_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    device_id,
                    file_name,
                    0,
                    "complete",
                    started_at.astimezone(timezone.utc).isoformat(),
                    started_at.astimezone(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _gcode_file(self, file_name, content, *, last_seen=None):
        encoded = content.encode("utf-8")
        sha256 = hashlib.sha256(encoded).hexdigest()
        gcode_dir = db.DATA_DIR / "gcode"
        gcode_dir.mkdir(parents=True, exist_ok=True)
        (gcode_dir / f"{sha256}.nc").write_bytes(encoded)
        seen = (last_seen or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        conn = sqlite3.connect(self.params_path)
        try:
            conn.execute(
                """INSERT INTO gcode_files
                (sha256, file_name, size, first_seen, last_seen, device_id)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (sha256, file_name, len(encoded), seen, seen, "customer-test-device"),
            )
            conn.commit()
        finally:
            conn.close()
        return sha256

    def test_pairing_handles_aborts_unmatched_resume_and_window_boundary(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self._job(1, now - timedelta(hours=3), "aborted", "started", "old.gcode")
        self._job(2, now - timedelta(hours=2, minutes=55), "aborted", "started", "new.gcode")
        self._job(3, now - timedelta(hours=2, minutes=50), "aborted", "completed", "new.gcode")
        self._job(4, now - timedelta(hours=2), "resumed", "started", "resume.gcode")
        self._job(5, now - timedelta(hours=1, minutes=50), "resumed", "paused", "resume.gcode")
        self._job(6, now - timedelta(hours=1, minutes=40), "resumed", "resumed", "resume.gcode")
        self._job(7, now - timedelta(hours=1, minutes=30), "resumed", "stopped", "resume.gcode")
        self._job(8, now - timedelta(minutes=20), "missing", "completed", "missing.gcode")
        self._job(9, now - timedelta(hours=26), "boundary", "started", "boundary.gcode")
        self._job(10, now - timedelta(hours=24), "boundary", "completed", "boundary.gcode")
        self._job(11, now - timedelta(hours=25), "outside", "started", "outside.gcode")
        self._job(12, now - timedelta(hours=24, seconds=1), "outside", "completed", "outside.gcode")

        summary = customer_stats.job_summary("24h", now=now)

        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["stopped"], 1)
        self.assertNotIn("by_file", summary)

    def test_configured_device_is_used_and_query_device_is_ignored(self):
        self._add_event(
            1, datetime.now(timezone.utc), "StateChangedEvent",
            {"parameter_key": "current_file", "new_value": "reported.gcode"},
        )
        response = self.client.get(
            "/api/customer/overview?device_id=other-device", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["loaded_file"], "reported.gcode")
        self.assertEqual(data["device_id"], "customer-test-device")
        self.assertEqual(data["machine_hours_scope"], "server")
        self.assertNotIn("by_file", data["jobs"])

    def test_missing_configured_device_returns_503(self):
        config.CUSTOMER_DEVICE_ID = None
        response = self.client.get("/api/customer/overview", headers=self.auth_headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "customer_device_not_configured"})

    def test_machine_status_uses_reported_execution_state(self):
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self._add_event(
            1, now - timedelta(minutes=1), "StateChangedEvent",
            {"parameter_key": "execution_state", "new_value": "idle"},
        )
        self._add_event(
            2, now, "StateChangedEvent",
            {"parameter_key": "execution_state", "new_value": "paused"},
        )

        status = customer_stats.device_status("test", now=now)

        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["last_seen"], "2026-11-06T12:00:00Z")

    def test_machine_status_is_unknown_without_ufameasy_state(self):
        self.assertEqual(customer_stats.device_status("customer-test-device")["state"], "unknown")

    def test_loaded_file_uses_reported_current_file(self):
        now = datetime.now(timezone.utc)
        self._add_event(
            1, now, "StateChangedEvent",
            {"parameter_key": "current_file", "new_value": "Test_10oct.gcode"},
        )

        self.assertEqual(customer_stats.loaded_file("customer-test-device"), "Test_10oct.gcode")

    def test_invalid_parameters_are_rejected(self):
        for path in (
            "/api/customer/overview?window=bad",
            "/api/customer/logs?severity=bad",
            "/api/customer/logs?limit=101",
            "/api/customer/logs?cursor=not-a-cursor",
            "/api/customer/logs?from=not-a-date",
        ):
            response = self.client.get(path, headers=self.auth_headers)
            self.assertEqual(response.status_code, 400, path)
            self.assertEqual(response.json(), {"detail": "invalid_request"})

    def test_viewer_is_allowed_and_unauthenticated_request_is_denied(self):
        self._job(1, datetime.now(timezone.utc) - timedelta(minutes=1), "one", "started")
        self._job(2, datetime.now(timezone.utc), "one", "completed")

        response = self.client.get("/api/customer/overview", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("jobs", response.json())

        unauthenticated = TestClient(FastAPI())
        unauthenticated.app.include_router(router)
        denied = unauthenticated.get("/api/customer/overview")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json(), {"detail": "authentication_required"})

    def test_operator_routes_are_not_exposed_below_customer_prefix(self):
        response = self.client.get(
            "/api/customer/devices/customer-test-device/commands", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 404)

    def test_cursor_pagination_returns_safe_log_records(self):
        now = datetime.now(timezone.utc)
        for event_id, status in enumerate(("started", "completed", "started"), start=1):
            self._job(event_id, now - timedelta(minutes=event_id), f"s{event_id}", status, "/secret/path.gcode")

        first = self.client.get("/api/customer/logs?limit=2", headers=self.auth_headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.json()), 2)
        self.assertTrue(first.headers.get("X-Next-Cursor"))
        self.assertEqual(set(first.json()[0]), {"time", "severity", "message"})
        self.assertNotIn("/secret", first.text)

        second = self.client.get(
            f"/api/customer/logs?limit=2&cursor={first.headers['X-Next-Cursor']}",
            headers=self.auth_headers,
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()), 1)

    def test_log_messages_redact_paths_ips_and_tracebacks(self):
        self._add_event(
            1,
            datetime.now(timezone.utc),
            "PipelineErrorEvent",
            {"error_message": r"Failed C:\\secrets\\job.gcode at 10.20.30.40 Traceback hidden"},
            severity="ERROR",
        )
        response = self.client.get("/api/customer/logs?severity=ERROR", headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)
        message = response.json()[0]["message"]
        self.assertNotIn("C:\\secrets", message)
        self.assertNotIn("10.20.30.40", message)
        self.assertNotIn("Traceback", message)
        self.assertLessEqual(len(message), 200)

    def test_all_ufameasy_log_rows_are_exposed(self):
        now = datetime.now(timezone.utc)
        self._add_event(1, now - timedelta(minutes=4), "MachineConnectionEvent", {"state": "connected"})
        self._add_event(2, now - timedelta(minutes=3), "MachineConnectionEvent", {"state": "connecting"})
        self._add_event(3, now - timedelta(minutes=2), "SystemStateEvent", {"state": "No Action"})
        self._add_event(4, now - timedelta(minutes=1), "PrintJobEvent", {"status": "paused"})
        self._add_event(5, now, "PrintJobEvent", {"status": "resumed"})

        logs = self.client.get("/api/customer/logs", headers=self.auth_headers)
        self.assertEqual(logs.status_code, 200)
        messages = [item["message"] for item in logs.json()]
        self.assertEqual(messages, [
            "Print job resumed",
            "Print job paused",
            "SystemStateEvent: No Action",
            "Machine connecting",
            "Machine connected",
        ])

        download = self.client.get("/api/customer/logs/download", headers=self.auth_headers)
        self.assertEqual(download.status_code, 200)
        self.assertIn("Print job paused", download.text)
        self.assertIn("Machine connected", download.text)
        self.assertIn("Machine connecting", download.text)
        self.assertIn("No Action", download.text)
        self.assertIn("Print job resumed", download.text)

    def test_history_includes_machine_stopped_job_without_server_session(self):
        now = datetime.now(timezone.utc)
        self._job(1, now - timedelta(minutes=1), "machine-job", "started", "aborted.gcode")
        self._job(2, now, "machine-job", "stopped", "aborted.gcode")

        response = self.client.get("/api/customer/history?window=24h", headers=self.auth_headers)

        self.assertEqual(response.status_code, 200)
        history = response.json()
        self.assertEqual(history["total_runs"], 1)
        self.assertEqual(history["files"][0]["display_name"], "aborted.gcode")

    def test_history_uses_reported_names_and_sorts_newest_first(self):
        now = datetime.now(timezone.utc)
        self._add_event(
            1, now - timedelta(minutes=6), "StateChangedEvent",
            {"parameter_key": "current_file", "new_value": "first.gcode"}, session_id="Untitled",
        )
        self._job(2, now - timedelta(minutes=5), "Untitled", "started", "ufameasy_gcode_old.gcd")
        self._job(3, now - timedelta(minutes=4), "Untitled", "stopped", "ufameasy_gcode_old.gcd")
        self._add_event(
            4, now - timedelta(minutes=3), "StateChangedEvent",
            {"parameter_key": "current_file", "new_value": "latest.gcode"}, session_id="Untitled",
        )
        self._job(5, now - timedelta(minutes=2), "Untitled", "started", "ufameasy_gcode_new.gcd")
        self._job(6, now - timedelta(minutes=1), "Untitled", "stopped", "ufameasy_gcode_new.gcd")

        history = self.client.get("/api/customer/history?window=24h", headers=self.auth_headers).json()

        self.assertEqual([item["display_name"] for item in history["files"]], ["latest.gcode", "first.gcode"])

    def test_unchanged_overview_returns_304_for_its_etag(self):
        now = datetime.now(timezone.utc)
        self._job(1, now - timedelta(minutes=2), "one", "started")
        self._job(2, now - timedelta(minutes=1), "one", "completed")

        first = self.client.get("/api/customer/overview", headers=self.auth_headers)
        self.assertEqual(first.status_code, 200)
        etag = first.headers["ETag"]
        second = self.client.get(
            "/api/customer/overview",
            headers={**self.auth_headers, "If-None-Match": etag},
        )
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.headers["ETag"], etag)

    def test_history_groups_basename_and_excludes_auto_and_other_devices(self):
        now = datetime.now(timezone.utc)
        self._session("one", "customer-test-device", r"C:\\jobs\\Part.GCODE", now - timedelta(hours=2))
        self._session("two", "customer-test-device", "part.gcode", now - timedelta(hours=1))
        self._session("unknown", "customer-test-device", "unknown", now - timedelta(minutes=50))
        self._session("empty", "customer-test-device", "", now - timedelta(minutes=40))
        self._session("auto-42", "customer-test-device", "auto.gcode", now - timedelta(minutes=30))
        self._session("other", "other-device", "other.gcode", now - timedelta(minutes=20))
        self._gcode_file("PART.GCODE", "G1 X1\n")

        response = self.client.get(
            "/api/customer/history?window=24h&device_id=other-device", headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 200)
        history = response.json()
        self.assertEqual(history["window"], "24h")
        self.assertEqual(history["total_runs"], 4)
        self.assertEqual(history["unnamed_runs"], 2)
        self.assertEqual(len(history["files"]), 2)
        grouped = {item["display_name"].casefold(): item for item in history["files"]}
        self.assertEqual(grouped["part.gcode"]["runs"], 2)
        self.assertTrue(grouped["part.gcode"]["available"])
        self.assertFalse(grouped["no file name"]["available"])

    def test_file_view_and_download_use_registry_name_only(self):
        self._gcode_file("sample.gcode", "G1 X1\nG1 X2\nG1 X3\n")

        view = self.client.get(
            "/api/customer/files/view?name=sample.gcode&lines=2", headers=self.auth_headers
        )
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.json()["file_name"], "sample.gcode")
        self.assertEqual(view.json()["lines"], ["G1 X1", "G1 X2"])
        self.assertEqual(view.json()["total_lines"], 3)
        self.assertTrue(view.json()["truncated"])

        download = self.client.get(
            "/api/customer/files/download?name=sample.gcode", headers=self.auth_headers
        )
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"G1 X1\nG1 X2\nG1 X3\n")
        self.assertIn("attachment", download.headers["content-disposition"])

    def test_file_path_traversal_and_missing_files_are_rejected(self):
        for name in ("../sample.gcode", r"C:\\sample.gcode", "/tmp/sample.gcode", "%2E%2E%2Fsample.gcode"):
            response = self.client.get(
                f"/api/customer/files/view?name={name}", headers=self.auth_headers
            )
            self.assertEqual(response.status_code, 400, name)
        for route in ("view", "download"):
            response = self.client.get(
                f"/api/customer/files/{route}?name=missing.gcode", headers=self.auth_headers
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "file_not_available"})

    def test_logs_download_is_redacted_csv_and_is_capped(self):
        now = datetime.now(timezone.utc)
        self._add_event(
            1,
            now,
            "PipelineErrorEvent",
            {"error_message": r"Failed C:\\secrets\\job.gcode at 10.20.30.40 Traceback hidden"},
            severity="ERROR",
        )
        response = self.client.get(
            "/api/customer/logs/download?severity=ERROR", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/csv; charset=utf-8")
        self.assertTrue(response.text.startswith("time_ist,severity,message\r\n"))
        self.assertNotIn("C:\\secrets", response.text)
        self.assertNotIn("10.20.30.40", response.text)
        self.assertNotIn("Traceback", response.text)

        records = [{"time": "2026-11-10T15:32:11.938000Z", "severity": "INFO", "message": "ok"}] * 50_001
        with patch("server.customer_routes.customer_stats.customer_logs", return_value=(records, None)):
            capped = self.client.get("/api/customer/logs/download", headers=self.auth_headers)
        self.assertEqual(len(capped.text.splitlines()), 50_001)
        self.assertIn("10 Nov 2026, 21:02:11 IST", capped.text)


if __name__ == "__main__":
    unittest.main()
