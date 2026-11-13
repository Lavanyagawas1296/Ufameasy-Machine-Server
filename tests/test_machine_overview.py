import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from server.routes import _count_finished_jobs


class MachineOverviewQueryTests(unittest.TestCase):
    def test_counts_only_finished_start_pairs_inside_window(self):
        now = datetime(2026, 10, 31, 14, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ufameasy_sys.U5LOG"
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, payload TEXT)")

                def add(event_id, when, status):
                    conn.execute(
                        "INSERT INTO telemetry_events VALUES (?, ?, ?, ?)",
                        (event_id, when.strftime("%d/%m/%Y %I:%M:%S.%f %p"), "PrintJobEvent", json.dumps({"status": status})),
                    )

                add(1, now - timedelta(hours=26), "started")
                add(2, now - timedelta(hours=25), "stopped")    # Outside 24h: ignored.
                add(3, now - timedelta(hours=25), "started")
                add(4, now - timedelta(hours=24, minutes=1), "started")
                add(5, now - timedelta(hours=24), "completed")  # Boundary is included.
                add(6, now - timedelta(hours=23), "completed")  # Counts: end is inside 24h.
                add(7, now - timedelta(hours=2), "started")
                add(8, now - timedelta(hours=1), "stopped")     # Counts.
                add(9, now - timedelta(minutes=30), "completed")  # No start: ignored.
                add(10, now - timedelta(minutes=10), "started")  # No end: ignored.
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(_count_finished_jobs(path, timedelta(hours=24), now=now), 3)

