from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from server import db
from server.rmc_auth import resolve_principal
from server.rmc_service import RMCService
from server.state_store import state


class RMCBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "rmc.sqlite"
        db.init_db()
        self.published = []
        self.events = []
        self.service = RMCService(publisher=lambda did, command: self.published.append((did, command)) or True,
                                  broadcaster=self.events.append)
        db.register_device("machine-a", "machine-a")
        state.rmc_states.clear()
        state.update_rmc_state("machine-a", {
            "device_id": "machine-a", "job_state": "loaded", "file": {"name": "x.nc"}, "progress": 0,
            "last_cmd_id": None, "remote_control": "ask", "local_busy": False, "controlled_by": None,
            "recorded_at": datetime.now(timezone.utc).isoformat(), "seq": 1,
        })

    def tearDown(self):
        db.DB_PATH = self.original_db
        self.temp.cleanup()

    def test_validation_and_device_isolation(self):
        with self.assertRaises(HTTPException):
            self.service.issue_command("unknown", {"action": "start", "params": {}}, "u")
        result = self.service.issue_command("machine-a", {"action": "start", "params": {}}, "u")
        self.assertEqual(self.published[0][0], "machine-a")
        self.assertEqual(db.get_rmc_command(result["cmd_id"])["device_id"], "machine-a")

    def test_lock_and_audit(self):
        self.service.acquire_lock("machine-a", "one")
        with self.assertRaises(HTTPException):
            self.service.acquire_lock("machine-a", "two")
        result = self.service.issue_command("machine-a", {"action": "start", "params": {}}, "one")
        audit = db.get_rmc_command(result["cmd_id"])
        self.assertEqual(audit["history"][0]["status"], "sent")

    def test_ack_tracker_and_timeout(self):
        result = self.service.issue_command("machine-a", {"action": "start", "params": {}}, "u")
        self.service._timeout(result["cmd_id"], "received")
        self.assertEqual(db.get_rmc_command(result["cmd_id"])["status"], "timeout")

    def test_auth_on_and_off(self):
        prior_enabled = os.environ.get("RMC_AUTH_ENABLED")
        prior_tokens = os.environ.get("RMC_AUTH_TOKENS_JSON")
        try:
            os.environ["RMC_AUTH_ENABLED"] = "false"
            self.assertEqual(resolve_principal(None).name, "local-dev")
            token = "secret"
            digest = hashlib.sha256(token.encode()).hexdigest()
            os.environ["RMC_AUTH_ENABLED"] = "true"
            os.environ["RMC_AUTH_TOKENS_JSON"] = '{"%s":{"user":"op","role":"operator"}}' % digest
            self.assertEqual(resolve_principal("Bearer secret").role, "operator")
            with self.assertRaises(HTTPException):
                resolve_principal("Bearer wrong")
        finally:
            if prior_enabled is None: os.environ.pop("RMC_AUTH_ENABLED", None)
            else: os.environ["RMC_AUTH_ENABLED"] = prior_enabled
            if prior_tokens is None: os.environ.pop("RMC_AUTH_TOKENS_JSON", None)
            else: os.environ["RMC_AUTH_TOKENS_JSON"] = prior_tokens

    def test_safe_load_params(self):
        with self.assertRaises(HTTPException):
            self.service._validate_params("load", {"source": "ftp", "ftp_name": "../bad", "sha256": "x", "size": 1})
        params = self.service._validate_params("load", {"source": "ftp", "ftp_name": "jobs/a.nc", "sha256": "x", "size": 1})
        self.assertEqual(params["path"], "jobs/a.nc")
