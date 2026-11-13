import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import db, gcode_ingest


class GcodeIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "data"
        self.db_path = root / "params.db"
        self.rmc_dir = root / "rmc_files"
        self.patches = [
            patch.object(db, "DATA_DIR", self.data_dir),
            patch.object(db, "DB_PATH", self.db_path),
            patch.object(db, "RMC_FILES_DIR", self.rmc_dir),
        ]
        for item in self.patches:
            item.start()
        db.init_db()
        self.source = root / "upload.nc"

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT sha256, file_name, size FROM gcode_files ORDER BY file_name"
            ).fetchall()
        finally:
            conn.close()

    def test_same_content_is_deduplicated(self):
        self.source.write_bytes(b"G1 X1\n")
        first = gcode_ingest.ingest_gcode_file(self.source, "first.nc")
        second = gcode_ingest.ingest_gcode_file(self.source, "copy.nc")
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.data_dir / "gcode" / f"{first}.nc").read_bytes(), b"G1 X1\n")

    def test_same_name_with_different_content_has_two_rows(self):
        self.source.write_bytes(b"G1 X1\n")
        first_hash = gcode_ingest.ingest_gcode_file(self.source, "part.nc")
        self.source.write_bytes(b"G1 X2\n")
        second_hash = gcode_ingest.ingest_gcode_file(self.source, "part.nc")
        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(len(self.rows()), 2)

    def test_temp_file_is_removed_when_promotion_fails(self):
        self.source.write_bytes(b"G1 X1\n")
        with patch.object(gcode_ingest.os, "rename", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                gcode_ingest.ingest_gcode_file(self.source, "part.nc")
        self.assertEqual(list((self.data_dir / "gcode").iterdir()), [])

    def test_hash_matches_known_content(self):
        content = b"G0 X0 Y0\nG1 Z2.5 F100\n"
        self.source.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        actual = gcode_ingest.ingest_gcode_file(self.source, "known.nc")
        self.assertEqual(actual, expected)
        self.assertEqual((self.data_dir / "gcode" / f"{expected}.nc").read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
