"""Usage report: state timeline, KPIs, jobs, files, sign-ins, CSV and period parsing.

Calls the report code directly (no starlette TestClient), so it runs without httpx.
"""

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from server import customer_config as config
from server import customer_report as report
from server import customer_stats, db
from server.state_store import state

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 11, 10)                                   # a Tuesday
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=IST)
_ENV = ("CUSTOMER_MODE", "CUSTOMER_SHOW_OPERATOR", "CUSTOMER_SHIFT_START", "CUSTOMER_SHIFT_END",
        "CUSTOMER_MACHINE_LABEL")


def ist(day, hhmm, seconds=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, seconds, tzinfo=IST)


class UsageReportTests(unittest.TestCase):
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
        with state.lock:
            self.saved_parameters = copy.deepcopy(state.parameters)
            state.parameters.clear()
        config.TELEMETRY_DB_PATH = self.telemetry
        config.CUSTOMER_DEVICE_ID = "dev"
        config.TELEMETRY_TZ = "Asia/Kolkata"
        db.DB_PATH = base / "params.db"
        db.DATA_DIR = base / "data"
        db.init_db()
        os.environ["CUSTOMER_MODE"] = "0"           # explicit: a developer .env may hold CUSTOMER_MODE=1
        for name in _ENV[1:]:
            os.environ.pop(name, None)
        self.next_id = 1

    def tearDown(self):
        (config.TELEMETRY_DB_PATH, config.CUSTOMER_DEVICE_ID, config.TELEMETRY_TZ, db.DB_PATH, db.DATA_DIR) = self.saved
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        with state.lock:
            state.parameters.clear()
            state.parameters.update(self.saved_parameters)
        self.temp_dir.cleanup()

    # -- helpers: rows in the workbench's own format (local IST time string) --------

    def _event(self, when, event_type, payload, severity="INFO", user="alice (Operator)"):
        ts = when.strftime("%d/%m/%Y %I:%M:%S.%f")[:-3] + when.strftime(" %p")
        body = {"event_type": event_type, "timestamp": ts, "session_id": "Untitled", "user": user, **payload}
        conn = sqlite3.connect(self.telemetry)
        conn.execute(
            "INSERT INTO telemetry_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.next_id, f"e{self.next_id}", ts, "machine", severity, "test", "Untitled", event_type, json.dumps(body)),
        )
        conn.commit()
        conn.close()
        self.next_id += 1

    def _state(self, when, value):
        self._event(when, "StateChangedEvent", {"parameter_key": "execution_state", "new_value": value})

    def _conn(self, when, value):
        self._event(when, "MachineConnectionEvent", {"state": value})

    def _job(self, when, status, **fields):
        severity = {"stopped": "WARNING", "failed": "ERROR"}.get(status, "INFO")
        user = fields.pop("user", "alice (Operator)")
        self._event(when, "PrintJobEvent", {"status": status, **fields}, severity=severity, user=user)

    def _report(self, start=DAY, end=DAY, now=NOW):
        return report.usage_report("dev", start, end, now=now)

    def _one_day_of_work(self):
        self._conn(ist(DAY, "09:00"), "connected")
        self._state(ist(DAY, "09:00", 1), "idle")
        self._event(ist(DAY, "09:29"), "StateChangedEvent", {"parameter_key": "current_file", "new_value": "part_A.gcode"})
        self._job(ist(DAY, "09:30"), "started", gcode_file="ufameasy_gcode_x1.gcd", total_lines=200)
        self._state(ist(DAY, "09:30"), "running")
        self._state(ist(DAY, "10:00"), "paused")
        self._state(ist(DAY, "10:10"), "running")
        self._job(ist(DAY, "11:00"), "completed", gcode_file="ufameasy_gcode_x1.gcd", elapsed_seconds=4800.0)
        self._state(ist(DAY, "11:00"), "idle")
        self._conn(ist(DAY, "17:00"), "disconnected")

    # -- timeline and totals ------------------------------------------------------

    def test_day_totals_and_shift_timeline(self):
        self._one_day_of_work()
        rep = self._report()
        day = rep["days"][0]

        self.assertEqual(day["running_seconds"], 80 * 60)
        self.assertEqual(day["paused_seconds"], 10 * 60)
        self.assertEqual(day["idle_seconds"], 30 * 60 + 1 + 6 * 3600 - 1)
        self.assertEqual(sum(day[f"{s}_seconds"] for s in report.STATES), 86400)
        # Connected at 09:00 exactly, the shift start: the shift begins idle.
        self.assertEqual([s["state"] for s in day["segments"]],
                         ["idle", "running", "paused", "running", "idle", "off"])
        running = day["segments"][1]
        self.assertAlmostEqual(running["left"], 30 / 540, places=4)
        self.assertAlmostEqual(running["width"], 30 / 540, places=4)
        self.assertAlmostEqual(sum(s["width"] for s in day["segments"]), 1.0, places=3)
        self.assertEqual(rep["totals"]["shift_run_seconds"], 80 * 60)
        self.assertEqual(rep["totals"]["shift_seconds"], 9 * 3600)
        self.assertEqual(rep["totals"]["utilisation_pct"], 14.8)
        self.assertEqual(rep["shift"], {"start": "09:00", "end": "18:00"})

    def test_waiting_at_m1_counts_as_paused_and_disconnect_as_off(self):
        self._conn(ist(DAY, "09:00"), "connected")
        self._state(ist(DAY, "09:00"), "running")
        self._state(ist(DAY, "09:20"), "waiting")
        self._conn(ist(DAY, "09:30"), "disconnected")     # still "waiting", but the link is gone
        day = self._report()["days"][0]

        self.assertEqual(day["running_seconds"], 20 * 60)
        self.assertEqual(day["paused_seconds"], 10 * 60)
        self.assertEqual(day["idle_seconds"], 0)

    def test_today_is_cut_at_now_and_future_days_are_empty(self):
        self._one_day_of_work()
        now = ist(DAY, "10:05")
        rep = self._report(DAY, DAY + timedelta(days=1), now=now)
        today, tomorrow = rep["days"]

        self.assertTrue(today["is_today"])
        self.assertTrue(tomorrow["is_future"])
        self.assertEqual(tomorrow["segments"], [])
        self.assertEqual(sum(today[f"{s}_seconds"] for s in report.STATES), 10 * 3600 + 5 * 60)
        self.assertEqual(today["segments"][-1]["state"], "paused")
        self.assertEqual(rep["totals"]["shift_seconds"], 65 * 60)

    def test_custom_shift_hours(self):
        os.environ["CUSTOMER_SHIFT_START"] = "08:30"
        os.environ["CUSTOMER_SHIFT_END"] = "20:00"
        self.assertEqual(config.shift_window(), (8 * 60 + 30, 20 * 60))
        os.environ["CUSTOMER_SHIFT_END"] = "07:00"         # end before start: defaults
        self.assertEqual(config.shift_window(), (9 * 60, 18 * 60))
        os.environ["CUSTOMER_SHIFT_START"] = "nine"
        os.environ["CUSTOMER_SHIFT_END"] = "18:00"
        self.assertEqual(config.shift_window(), (9 * 60, 18 * 60))

    # -- jobs, KPIs and files ---------------------------------------------------------

    def test_jobs_kpis_reasons_and_files(self):
        self._one_day_of_work()
        self._job(ist(DAY, "12:00"), "started", gcode_file="part_B.gcode")
        self._job(ist(DAY, "12:20"), "failed", gcode_file="part_B.gcode", elapsed_seconds=1200.0,
                  error_message="Emergency stop active")
        self._job(ist(DAY, "13:00"), "started", gcode_file="part_A.gcode")
        self._job(ist(DAY, "13:10"), "stopped", gcode_file="part_A.gcode", elapsed_seconds=600.0)
        self._job(ist(DAY, "14:00"), "started", gcode_file="part_C.gcode")    # never ended ...
        self._job(ist(DAY, "15:00"), "started", gcode_file="part_C.gcode")    # ... replaced by a new start
        self._job(ist(DAY, "15:30"), "completed", gcode_file="part_C.gcode", elapsed_seconds=1800.0)
        rep = self._report()

        self.assertEqual(rep["jobs_summary"], {"count": 4, "completed": 2, "stopped_early": 2, "faults": 1, "not_closed": 1})
        results = [(j["display_name"], j["result"], j["reason"]) for j in rep["jobs"]]
        self.assertEqual(results, [
            ("part_C.gcode", "completed", ""),
            ("part_C.gcode", "unrecorded", ""),
            ("part_A.gcode", "stopped", ""),
            ("part_B.gcode", "failed", "Emergency stop active"),
            ("part_A.gcode", "completed", ""),          # temp .gcd name replaced by current_file
        ])
        self.assertEqual(rep["jobs"][-1]["run_seconds"], 4800.0)
        self.assertIsNone(rep["jobs"][1]["ended_at"])
        files = {f["display_name"]: f for f in rep["files"]}
        self.assertEqual((files["part_A.gcode"]["runs"], files["part_A.gcode"]["done"], files["part_A.gcode"]["stopped"]), (2, 1, 1))
        self.assertEqual(files["part_A.gcode"]["total_seconds"], 5400)
        self.assertEqual(files["part_A.gcode"]["avg_seconds"], 2700)
        self.assertEqual(list(files), ["part_A.gcode", "part_C.gcode", "part_B.gcode"])
        fault_at = rep["days"][0]["faults"]
        self.assertEqual(len(fault_at), 1)
        self.assertAlmostEqual(fault_at[0], (3 * 60 + 20) / 540, places=4)

    def test_jobs_outside_the_period_are_left_out(self):
        self._job(ist(DAY - timedelta(days=1), "23:50"), "started", gcode_file="late.gcode")
        self._job(ist(DAY, "00:20"), "completed", gcode_file="late.gcode", elapsed_seconds=1800.0)
        self._job(ist(DAY, "10:00"), "started", gcode_file="in.gcode")
        self._job(ist(DAY, "10:30"), "completed", gcode_file="in.gcode", elapsed_seconds=1800.0)
        rep = self._report()

        self.assertEqual([j["display_name"] for j in rep["jobs"]], ["in.gcode"])
        self.assertEqual(rep["jobs_summary"]["count"], 1)

    def test_running_now_with_live_progress_and_operator_flag(self):
        self._conn(ist(DAY, "09:00"), "connected")
        self._job(ist(DAY, "11:20"), "started", gcode_file="live.gcode", total_lines=200, operator="bob")
        self._state(ist(DAY, "11:20"), "running")
        state.update_parameter("dev", "progress_pct", 50)
        now = ist(DAY, "11:57")

        run = self._report(now=now)["running_now"]
        self.assertEqual((run["display_name"], run["state"], run["seconds_so_far"]), ("live.gcode", "running", 37 * 60))
        self.assertEqual((run["progress_pct"], run["total_lines"], run["approx_line"]), (50.0, 200, 100))
        self.assertNotIn("operator", run)
        self.assertEqual(self._report(now=now)["jobs"][0]["result"], "running")

        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        self.assertEqual(self._report(now=now)["running_now"]["operator"], "bob")

    def test_no_running_banner_when_the_machine_is_idle(self):
        self._conn(ist(DAY, "09:00"), "connected")
        self._job(ist(DAY, "10:00"), "started", gcode_file="x.gcode")
        self._state(ist(DAY, "10:00"), "idle")             # job start without a running state: stale
        rep = self._report(now=ist(DAY, "11:00"))

        self.assertIsNone(rep["running_now"])
        self.assertEqual(rep["jobs"][0]["result"], "unrecorded")

    def test_operator_only_in_jobs_when_flag_on(self):
        self._job(ist(DAY, "10:00"), "started", gcode_file="a.gcode", user="carol (Supervisor)")
        self._job(ist(DAY, "10:30"), "completed", gcode_file="a.gcode")
        rep = self._report()
        self.assertFalse(rep["operator_visible"])
        self.assertNotIn("operator", rep["jobs"][0])
        self.assertNotIn("carol", json.dumps(rep))

        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        rep = self._report()
        self.assertTrue(rep["operator_visible"])
        self.assertEqual(rep["jobs"][0]["operator"], "carol")

    # -- UFAMeasy sign-ins ----------------------------------------------------------------

    def test_signins_hide_names_unless_flag_on(self):
        self._event(ist(DAY, "08:55"), "RoleChangedEvent", {"old_role": "Operator", "new_role": "Supervisor"}, user="dana (Supervisor)")
        self._event(ist(DAY, "13:00"), "RoleChangedEvent", {"old_role": "Supervisor", "new_role": "Operator"}, user="eve (Operator)")
        self._event(ist(DAY + timedelta(days=1), "09:00"), "RoleChangedEvent", {"new_role": "Master"}, user="x (Master)")

        signins = self._report()["signins"]
        self.assertEqual([s["role"] for s in signins], ["Operator", "Supervisor"])     # newest first, period only
        self.assertTrue(all("name" not in s for s in signins))

        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        self.assertEqual([s["name"] for s in self._report()["signins"]], ["eve", "dana"])

    def test_signin_log_message_has_role_but_no_name(self):
        self._event(ist(DAY, "08:55"), "RoleChangedEvent", {"old_role": "Operator", "new_role": "Supervisor"}, user="dana (Supervisor)")
        records, _cursor = customer_stats.customer_logs(
            severity=None, start=ist(DAY, "00:00"), end=ist(DAY, "23:59"), cursor=None, limit=10,
        )
        self.assertEqual([r["message"] for r in records], ["UFAMeasy sign-in (Supervisor)"])
        self.assertNotIn("dana", json.dumps(records))

    # -- CSV --------------------------------------------------------------------------

    def test_jobs_csv_columns_and_formula_guard(self):
        self._job(ist(DAY, "10:00"), "started", gcode_file="=cmd.gcode", user="carol (Supervisor)")
        self._job(ist(DAY, "10:30"), "failed", gcode_file="=cmd.gcode", elapsed_seconds=1500.0, error_message="Emergency stop active")

        text = "".join(report.jobs_csv(self._report()))
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "date,start_ist,end_ist,run_minutes,file,result,reason")
        self.assertEqual(lines[1], "2026-11-10,10:00,10:30,25.0,'=cmd.gcode,Stopped by fault,Emergency stop active")
        self.assertNotIn("carol", text)

        os.environ["CUSTOMER_SHOW_OPERATOR"] = "1"
        lines = "".join(report.jobs_csv(self._report())).strip().splitlines()
        self.assertEqual(lines[0], "date,operator,start_ist,end_ist,run_minutes,file,result,reason")
        self.assertTrue(lines[1].startswith("2026-11-10,carol,10:00,"))

    # -- period -----------------------------------------------------------------------

    def test_parse_period(self):
        today = date(2026, 11, 13)                          # Friday
        self.assertEqual(report.parse_period(None, None, today=today), (date(2026, 11, 9), today))
        self.assertEqual(report.parse_period("2026-11-01", "2026-11-05", today=today), (date(2026, 11, 1), date(2026, 11, 5)))
        for bad in (("2026-11-05", "2026-11-01"), ("2026-01-01", "2026-11-05"), ("11/05/2026", "2026-11-06"),
                    ("2026-11-10", "2026-12-30"), (None, "2026-11-05")):
            with self.subTest(period=bad), self.assertRaises(report.InvalidPeriod):
                report.parse_period(*bad, today=today)


if __name__ == "__main__":
    unittest.main()
