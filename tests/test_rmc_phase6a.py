from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from server import db
from server.rmc_auth import Principal
from server.rmc_contract import ALLOWED_EXTENSIONS, RMC_TRANSFER_SECRET
from server.rmc_service import RMCService
from server.rmc_transfer import (
    get_free_disk_bytes,
    save_uploaded_file,
    sign_download_token,
    verify_download_token,
)
from server.routes import delete_rmc_file, download_rmc_file
from server.state_store import state


class RMCPhase6ATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        self.orig_db_path = db.DB_PATH
        self.orig_files_dir = db.RMC_FILES_DIR

        db.DB_PATH = self.temp_path / "test_params.db"
        db.RMC_FILES_DIR = self.temp_path / "rmc_files"
        db.init_db()

        self.published = []
        self.events = []
        self.service = RMCService(
            publisher=lambda did, cmd: self.published.append((did, cmd)) or True,
            broadcaster=self.events.append,
        )

        db.register_device("machine-a", "Machine A")
        db.register_device("machine-b", "Machine B")

        # Set a fixed secret for testing
        os.environ["RMC_TRANSFER_SECRET"] = "test-secret-key-12345"
        os.environ["RMC_AUTH_ENABLED"] = "false"
        os.environ["RMC_SERVER_BASE_URL"] = "http://test-server:8000"

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        db.RMC_FILES_DIR = self.orig_files_dir
        os.environ.pop("RMC_SERVER_BASE_URL", None)
        self.temp_dir.cleanup()

    def _create_upload_file(self, filename: str, content: bytes) -> UploadFile:
        file_obj = io.BytesIO(content)
        headers = Headers({"content-disposition": f'form-data; name="file"; filename="{filename}"'})
        return UploadFile(file=file_obj, filename=filename, headers=headers)

    async def test_extension_reject(self):
        """Reject files not in ALLOWED_EXTENSIONS with 422 UNSUPPORTED_TYPE."""
        bad_file = self._create_upload_file("script.py", b"print('hello')")
        with self.assertRaises(HTTPException) as ctx:
            await save_uploaded_file(bad_file, "operator-1")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["reason_code"], "UNSUPPORTED_TYPE")

        exe_file = self._create_upload_file("malware.exe", b"\x4d\x5a")
        with self.assertRaises(HTTPException) as ctx:
            await save_uploaded_file(exe_file, "operator-1")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["reason_code"], "UNSUPPORTED_TYPE")

        # Allowed extensions (.gcode, .nc) must pass
        valid_gcode = self._create_upload_file("valid.gcode", b"G0 X0 Y0\n")
        res_gcode = await save_uploaded_file(valid_gcode, "operator-1")
        self.assertEqual(res_gcode["display_name"], "valid.gcode")

        valid_nc = self._create_upload_file("valid.nc", b"G1 X10 Y10\n")
        res_nc = await save_uploaded_file(valid_nc, "operator-1")
        self.assertEqual(res_nc["display_name"], "valid.nc")

    async def test_size_limit(self):
        """Enforce MAX_JOB_FILE_MB while streaming; reject with 413 FILE_TOO_LARGE."""
        with patch.dict(os.environ, {"MAX_JOB_FILE_MB": "1"}):
            oversized_content = b"X" * (1024 * 1024 + 10)  # > 1 MB
            upload = self._create_upload_file("big.gcode", oversized_content)
            with self.assertRaises(HTTPException) as ctx:
                await save_uploaded_file(upload, "operator-1")
            self.assertEqual(ctx.exception.status_code, 413)
            self.assertEqual(ctx.exception.detail["reason_code"], "FILE_TOO_LARGE")

    async def test_path_traversal_sanitization(self):
        """Path traversal sequences in filename are sanitized and stored by UUID."""
        upload = self._create_upload_file("../../etc/passwd.gcode", b"G0 X0\n")
        record = await save_uploaded_file(upload, "operator-1")
        self.assertEqual(record["display_name"], "passwd.gcode")
        self.assertNotIn("/", record["stored_name"])
        self.assertNotIn("\\", record["stored_name"])
        stored_path = db.RMC_FILES_DIR / record["stored_name"]
        self.assertTrue(stored_path.is_file())

    async def test_duplicate_sha_rejection(self):
        """Uploading identical content returns 409 DUPLICATE pointing to existing file_id."""
        content = b"G1 X100 Y200 Z300\n"
        first = self._create_upload_file("job1.gcode", content)
        record1 = await save_uploaded_file(first, "operator-1")

        second = self._create_upload_file("job1_copy.gcode", content)
        with self.assertRaises(HTTPException) as ctx:
            await save_uploaded_file(second, "operator-2")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["reason_code"], "DUPLICATE")
        self.assertEqual(ctx.exception.detail["existing_file_id"], record1["file_id"])

    async def test_disk_full_rejection(self):
        """Simulate low disk space and verify 507 DISK_FULL."""
        upload = self._create_upload_file("toolpath.gcode", b"G1 X0\n")
        with patch("server.rmc_transfer.get_free_disk_bytes", return_value=2):
            with self.assertRaises(HTTPException) as ctx:
                await save_uploaded_file(upload, "operator-1")
            self.assertEqual(ctx.exception.status_code, 507)
            self.assertEqual(ctx.exception.detail["reason_code"], "DISK_FULL")

    def test_token_tampering_expiry_and_device_binding(self):
        """Verify token tamper resistance, expiration check, and device binding."""
        file_id = "test-file-uuid"
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Valid token
        token = sign_download_token(file_id, "machine-a", now_ts + 60)
        valid, dev = verify_download_token(file_id, token, "machine-a")
        self.assertTrue(valid)
        self.assertEqual(dev, "machine-a")

        # Wrong device binding
        valid, reason = verify_download_token(file_id, token, "machine-b")
        self.assertFalse(valid)
        self.assertIn("Device binding mismatch", reason)

        # Expired token
        expired_token = sign_download_token(file_id, "machine-a", now_ts - 10)
        valid, reason = verify_download_token(file_id, expired_token, "machine-a")
        self.assertFalse(valid)
        self.assertIn("expired", reason)

        # Tampered signature
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.split(":")
        tampered_raw = f"{parts[0]}:{parts[1]}:deadbeefdeadbeef"
        tampered_token = base64.urlsafe_b64encode(tampered_raw.encode()).decode()
        valid, reason = verify_download_token(file_id, tampered_token, "machine-a")
        self.assertFalse(valid)
        self.assertIn("verification failed", reason)

    async def test_delete_while_in_flight_409(self):
        """Refuse to delete a file referenced by an in-flight load command."""
        upload = self._create_upload_file("part.gcode", b"G0 Z5\n")
        record = await save_uploaded_file(upload, "operator-1")
        file_id = record["file_id"]

        # Set machine state so it can accept a load command
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })

        # Issue load command referencing the server file
        cmd_result = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "server", "file_id": file_id}},
            "operator-1",
        )
        self.assertEqual(cmd_result["status"], "sent")

        # Try to delete file while in flight
        admin = Principal("admin-user", "admin")
        with self.assertRaises(HTTPException) as ctx:
            delete_rmc_file(file_id, admin)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["reason_code"], "INVALID_STATE")

        # After command completes (done), deletion succeeds
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_result["cmd_id"],
            "status": "accepted",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_result["cmd_id"],
            "status": "done",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        del_result = delete_rmc_file(file_id, admin)
        self.assertEqual(del_result["deleted"], file_id)
        self.assertIsNone(db.get_rmc_server_file(file_id))

    def test_progress_ack_non_terminal(self):
        """Progress ack updates status and never overwrites terminal state."""
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "running", "file": {"name": "a.nc"},
            "progress": 50, "last_cmd_id": None, "remote_control": "ask",
            "local_busy": False, "controlled_by": None,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })
        cmd_result = self.service.issue_command("machine-a", {"action": "pause", "params": {}}, "op")
        cmd_id = cmd_result["cmd_id"]

        # Received -> Accepted
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id, "status": "received", "ts": datetime.now(timezone.utc).isoformat()})
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id, "status": "accepted", "ts": datetime.now(timezone.utc).isoformat()})

        # Progress ack (downloading 45%)
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_id, "status": "progress", "stage": "downloading", "percent": 45,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        cmd_row = db.get_rmc_command(cmd_id)
        self.assertEqual(cmd_row["status"], "progress")

        # Verify broadcast included progress stage and percent
        last_event = [e for e in self.events if e.get("type") == "rmc_cmd_status"][-1]
        self.assertEqual(last_event["data"]["status"], "progress")
        self.assertEqual(last_event["data"]["stage"], "downloading")
        self.assertEqual(last_event["data"]["percent"], 45)

        # Progress ack (verifying 100%)
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_id, "status": "progress", "stage": "verifying", "percent": 100,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        cmd_row = db.get_rmc_command(cmd_id)
        self.assertEqual(cmd_row["status"], "progress")

        # Terminal done
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_id, "status": "done", "ts": datetime.now(timezone.utc).isoformat(),
        })
        cmd_row = db.get_rmc_command(cmd_id)
        self.assertEqual(cmd_row["status"], "done")

        # Late progress ack must NOT overwrite terminal state
        self.service.handle_ack("machine-a", {
            "cmd_id": cmd_id, "status": "progress", "stage": "downloading", "percent": 99,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        cmd_row = db.get_rmc_command(cmd_id)
        self.assertEqual(cmd_row["status"], "done")

    def test_local_file_validation_and_rejections(self):
        """Validate local file_id exists on device state, and reject client-supplied path/url."""
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })
        state.update_rmc_files("machine-a", [
            {"file_id": "machine_sample_1", "name": "sample.gcode", "size": 1024, "sha256": "abc"},
        ])

        # Reject client-supplied path
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "local", "file_id": "machine_sample_1", "path": "/etc/secret"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["reason_code"], "UNAUTHORIZED")

        # Reject non-existent local file
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "local", "file_id": "non_existent_file"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["reason_code"], "FILE_NOT_FOUND")

        # Existing local file passes and publishes metadata
        res = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "local", "file_id": "machine_sample_1"}},
            "op",
        )
        self.assertEqual(res["status"], "sent")
        published_cmd = self.published[-1][1]
        self.assertEqual(published_cmd["params"]["source"], "local")
        self.assertEqual(published_cmd["params"]["name"], "sample.gcode")
        self.assertEqual(published_cmd["params"]["size"], 1024)
        self.assertEqual(published_cmd["params"]["sha256"], "abc")

    async def test_device_isolation_and_download_flow(self):
        """Server-load publishes to target device topic, and download token is device-isolated."""
        upload = self._create_upload_file("bracket.gcode", b"G1 X100 Y50 Z10 E5\n")
        record = await save_uploaded_file(upload, "op")
        file_id = record["file_id"]

        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })

        # Load command for machine-a
        res = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "server", "file_id": file_id}},
            "op",
            request_base_url="http://test-server:8000",
        )
        self.assertEqual(res["status"], "sent")

        # Published to machine-a only
        self.assertEqual(self.published[-1][0], "machine-a")
        published_params = self.published[-1][1]["params"]
        self.assertEqual(published_params["file_id"], file_id)
        self.assertEqual(published_params["name"], "bracket.gcode")
        self.assertIn("download_url", published_params)
        self.assertIn("token=", published_params["download_url"])

        token = published_params["download_url"].split("token=")[1]

        # Download with machine-b binding must fail with 403
        with self.assertRaises(HTTPException) as ctx:
            download_rmc_file(file_id, token=token, device_id="machine-b")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail["reason_code"], "TRANSFER_DENIED")

        # Download with machine-a binding succeeds
        response = download_rmc_file(file_id, token=token, device_id="machine-a")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.filename, "bracket.gcode")

    def test_progress_ack_transitions_terminal_rejected_and_accepted_done(self):
        """Test received -> progress -> rejected is terminal, and received -> progress -> accepted -> done works."""
        # 1. received -> progress -> rejected
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None,
            "progress": 0, "last_cmd_id": None, "remote_control": "ask",
            "local_busy": False, "controlled_by": None,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })
        res1 = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "ftp", "ftp_name": "job.gcode", "size": 100, "sha256": "abc"}},
            "op",
        )
        cmd_id_1 = res1["cmd_id"]
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_1, "status": "received", "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_1)["status"], "received")

        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_1, "status": "progress", "stage": "downloading", "percent": 25, "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_1)["status"], "progress")

        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_1, "status": "rejected", "reason_code": "OPERATOR_DECLINED", "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_1)["status"], "rejected")

        # Late progress or accepted must not change terminal rejected status
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_1, "status": "progress", "percent": 50, "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_1)["status"], "rejected")
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_1, "status": "accepted", "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_1)["status"], "rejected")

        # 2. received -> progress -> accepted -> done
        res2 = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "ftp", "ftp_name": "job2.gcode", "size": 200, "sha256": "def"}},
            "op",
        )
        cmd_id_2 = res2["cmd_id"]
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_2, "status": "received", "ts": datetime.now(timezone.utc).isoformat()})
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_2, "status": "progress", "stage": "downloading", "percent": 60, "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_2)["status"], "progress")

        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_2, "status": "accepted", "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_2)["status"], "accepted")

        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_2, "status": "done", "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_2)["status"], "done")

        # Late progress must not overwrite done
        self.service.handle_ack("machine-a", {"cmd_id": cmd_id_2, "status": "progress", "percent": 90, "ts": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(db.get_rmc_command(cmd_id_2)["status"], "done")

    async def test_download_url_base_resolution_and_loopback_rejection(self):
        """Test RMC_SERVER_BASE_URL env override and loopback rejection when unset."""
        upload = self._create_upload_file("test.gcode", b"G0 X0\n")
        record = await save_uploaded_file(upload, "op")
        file_id = record["file_id"]

        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })

        # 1. RMC_SERVER_BASE_URL is set: always use it, ignoring request_base_url (even if localhost)
        os.environ["RMC_SERVER_BASE_URL"] = "http://my-public-domain.com:9000"
        res = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "server", "file_id": file_id}},
            "op",
            request_base_url="http://localhost:8000",
        )
        self.assertEqual(res["status"], "sent")
        published_url = self.published[-1][1]["params"]["download_url"]
        self.assertTrue(published_url.startswith("http://my-public-domain.com:9000/api/rmc/files/"))

        # 2. RMC_SERVER_BASE_URL is unset: localhost/127.0.0.1/::1 raise 400
        del os.environ["RMC_SERVER_BASE_URL"]

        for loopback_url in ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]:
            with self.assertRaises(HTTPException) as ctx:
                self.service.issue_command(
                    "machine-a",
                    {"action": "load", "params": {"source": "server", "file_id": file_id}},
                    "op",
                    request_base_url=loopback_url,
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("RMC_SERVER_BASE_URL", str(ctx.exception.detail))

        # 3. RMC_SERVER_BASE_URL is unset: non-loopback request_base_url succeeds
        res_ext = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "server", "file_id": file_id}},
            "op",
            request_base_url="http://192.168.1.50:8000",
        )
        self.assertEqual(res_ext["status"], "sent")
        published_url_ext = self.published[-1][1]["params"]["download_url"]
        self.assertTrue(published_url_ext.startswith("http://192.168.1.50:8000/api/rmc/files/"))

    def test_ftp_load_validation(self):
        """FTP load requires valid ftp_name, size (int >= 0), and sha256 (non-empty str)."""
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })

        # Missing size
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "ftp", "ftp_name": "sample.gcode", "sha256": "abc"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Negative size
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "ftp", "ftp_name": "sample.gcode", "size": -1, "sha256": "abc"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Boolean size
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "ftp", "ftp_name": "sample.gcode", "size": True, "sha256": "abc"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Missing / empty sha256
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "ftp", "ftp_name": "sample.gcode", "size": 100, "sha256": ""}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 422)

        # Valid ftp load succeeds
        res = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "ftp", "ftp_name": "sample.gcode", "size": 1024, "sha256": "abc12345"}},
            "op",
        )
        self.assertEqual(res["status"], "sent")
        published_params = self.published[-1][1]["params"]
        self.assertEqual(published_params["path"], "sample.gcode")
        self.assertEqual(published_params["size"], 1024)
        self.assertEqual(published_params["sha256"], "abc12345")

    def test_local_load_strict_file_id_matching(self):
        """Local load matches file_id strictly; matching by name/path or plain string is rejected."""
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "idle", "file": None, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False,
            "controlled_by": None, "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })
        state.update_rmc_files("machine-a", [
            {"file_id": "uuid-1234", "name": "sample_part.gcode", "size": 500, "sha256": "h1"},
            "plain_string_name.gcode",
        ])

        # Attempting to load by display name fails with 404
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "local", "file_id": "sample_part.gcode"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail["reason_code"], "FILE_NOT_FOUND")

        # Attempting to load plain string entry fails with 404
        with self.assertRaises(HTTPException) as ctx:
            self.service.issue_command(
                "machine-a",
                {"action": "load", "params": {"source": "local", "file_id": "plain_string_name.gcode"}},
                "op",
            )
        self.assertEqual(ctx.exception.status_code, 404)

        # Loading by exact file_id succeeds
        res = self.service.issue_command(
            "machine-a",
            {"action": "load", "params": {"source": "local", "file_id": "uuid-1234"}},
            "op",
        )
        self.assertEqual(res["status"], "sent")
        self.assertEqual(self.published[-1][1]["params"]["file_id"], "uuid-1234")
        self.assertEqual(self.published[-1][1]["params"]["name"], "sample_part.gcode")

    async def test_save_uploaded_file_integrity_error_duplicate(self):
        """save_uploaded_file handles sqlite3.IntegrityError on sha256 and returns 409 DUPLICATE."""
        upload1 = self._create_upload_file("first.gcode", b"G1 X1 Y1\n")
        record1 = await save_uploaded_file(upload1, "op1")
        first_file_id = record1["file_id"]

        import sqlite3
        upload2 = self._create_upload_file("second.gcode", b"G1 X1 Y1\n")
        with patch("server.db.create_rmc_server_file", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed: rmc_server_files.sha256")):
            with self.assertRaises(HTTPException) as ctx:
                await save_uploaded_file(upload2, "op2")
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(ctx.exception.detail["reason_code"], "DUPLICATE")
            self.assertEqual(ctx.exception.detail["existing_file_id"], first_file_id)

    def test_schedule_cancels_previous_timer(self):
        """_schedule cancels any existing timer for the same cmd_id before setting a new one."""
        self.service._schedule("test-cmd-123", 60, "received")
        timer1 = self.service._timers.get("test-cmd-123")
        self.assertIsNotNone(timer1)
        self.assertFalse(timer1.finished.is_set())

        # Scheduling a second timer for the same cmd_id
        self.service._schedule("test-cmd-123", 120, "done")
        timer2 = self.service._timers.get("test-cmd-123")
        self.assertIsNotNone(timer2)
        self.assertIsNot(timer1, timer2)

        # Verify old timer was cancelled
        self.assertTrue(timer1.finished.is_set())

        # Clean up second timer
        timer2.cancel()


if __name__ == "__main__":
    unittest.main()
