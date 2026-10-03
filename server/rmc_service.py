"""RMC command issuance, audit tracking, locks, and timeout handling."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import PurePosixPath
import threading
import uuid

from fastapi import HTTPException

from server import db
from server.rmc_contract import COMMAND_ACTIONS, LOAD_SOURCES, TRANSITIONS, rmc_topic, CMD_SEGMENT
from server.state_store import state


class RMCService:
    def __init__(self, *, publisher=None, broadcaster=None):
        self.publisher = publisher
        self.broadcaster = broadcaster
        self._lock = threading.RLock()
        self._timers: dict[str, threading.Timer] = {}
        self._recent: dict[tuple, deque] = defaultdict(deque)
        self.fresh_s = int(os.getenv("RMC_STATE_FRESH_S", "30"))
        self.received_timeout_s = int(os.getenv("RMC_RECEIVED_TIMEOUT_S", "10"))
        self.done_timeout_s = int(os.getenv("RMC_DONE_TIMEOUT_S", "120"))
        self.lock_ttl_s = int(os.getenv("RMC_LOCK_TTL_S", "300"))
        self.rate_limit = int(os.getenv("RMC_RATE_LIMIT", "10"))
        self.rate_window_s = int(os.getenv("RMC_RATE_WINDOW_S", "60"))

    def _publish(self, device_id: str, command: dict) -> bool:
        if self.publisher:
            return bool(self.publisher(device_id, command))
        from server.mqtt_client import publish_rmc_command
        return publish_rmc_command(device_id, command)

    def _broadcast(self, payload: dict) -> None:
        if self.broadcaster:
            self.broadcaster(payload)
        else:
            from server.mqtt_client import _broadcast
            _broadcast(payload)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _device_state(self, device_id: str) -> dict:
        current = state.get_rmc_state(device_id)
        if current.get("job_state") == "offline":
            return current
        try:
            recorded = datetime.fromisoformat(current["recorded_at"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return {**current, "job_state": "offline"}
        if self._now() - recorded > timedelta(seconds=self.fresh_s):
            return {**current, "job_state": "offline"}
        return current

    def _require_known_device(self, device_id: str) -> None:
        conn = None
        try:
            conn = db.get_conn()
            found = conn.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if not found:
                raise HTTPException(status_code=404, detail="Unknown device")
        finally:
            if conn is not None:
                conn.close()

    def _rate_check(self, device_id: str, user: str) -> None:
        now = self._now().timestamp()
        bucket = self._recent[(device_id, user)]
        while bucket and now - bucket[0] > self.rate_window_s:
            bucket.popleft()
        if len(bucket) >= self.rate_limit:
            raise HTTPException(status_code=429, detail="RMC command rate limit exceeded")
        bucket.append(now)

    def _lock_is_stale(self, lock: dict) -> bool:
        try:
            touched = datetime.fromisoformat(lock["touched_at"])
            return self._now() - touched > timedelta(seconds=self.lock_ttl_s)
        except (KeyError, ValueError):
            return True

    def acquire_lock(self, device_id: str, user: str, *, admin_override: bool = False) -> dict:
        with self._lock:
            current = db.get_rmc_lock(device_id)
            if current and self._lock_is_stale(current):
                db.delete_rmc_lock(device_id)
                current = None
            if current and current["issued_by"] != user and not admin_override:
                raise HTTPException(status_code=423, detail={"reason_code": "LOCKED_BY_OTHER", "issued_by": current["issued_by"]})
            db.upsert_rmc_lock(device_id, user)
            return db.get_rmc_lock(device_id)

    def release_lock(self, device_id: str, user: str, *, admin_override: bool = False) -> None:
        with self._lock:
            current = db.get_rmc_lock(device_id)
            if current and current["issued_by"] != user and not admin_override:
                raise HTTPException(status_code=423, detail={"reason_code": "LOCKED_BY_OTHER", "issued_by": current["issued_by"]})
            db.delete_rmc_lock(device_id)

    def get_lock(self, device_id: str) -> dict | None:
        with self._lock:
            current = db.get_rmc_lock(device_id)
            if current and self._lock_is_stale(current):
                db.delete_rmc_lock(device_id)
                return None
            return current

    @staticmethod
    def _safe_ftp_name(value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if not value or path.is_absolute() or ".." in path.parts:
            raise HTTPException(status_code=400, detail="ftp_name must be a relative path")
        return path.as_posix()

    def _validate_params(self, action: str, params: dict) -> dict:
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="params must be an object")
        if action != "load":
            return {}
        source = params.get("source")
        if source not in LOAD_SOURCES:
            raise HTTPException(status_code=422, detail="load source must be ftp or local")
        if source == "ftp":
            ftp_name = params.get("ftp_name")
            if not isinstance(ftp_name, str):
                raise HTTPException(status_code=422, detail="ftp load requires ftp_name")
            relative = self._safe_ftp_name(ftp_name)
            size = params.get("size")
            sha256 = params.get("sha256")
            if not isinstance(size, int) or size < 0 or not isinstance(sha256, str) or not sha256:
                raise HTTPException(status_code=422, detail="ftp load requires size and sha256")
            return {"source": "ftp", "path": relative, "sha256": sha256, "size": size}
        file_id = params.get("file_id")
        if not isinstance(file_id, str) or not file_id or "/" in file_id or "\\" in file_id:
            raise HTTPException(status_code=422, detail="local load requires an opaque file_id")
        return {"source": "local", "file_id": file_id, "sha256": params.get("sha256", ""), "size": params.get("size", 0)}

    def issue_command(self, device_id: str, body: dict, user: str) -> dict:
        self._require_known_device(device_id)
        action = body.get("action") if isinstance(body, dict) else None
        if action not in COMMAND_ACTIONS:
            raise HTTPException(status_code=422, detail="Unsupported RMC action")
        params = self._validate_params(action, body.get("params", {}))
        current = self._device_state(device_id)
        offline = current.get("job_state") == "offline"
        if offline and action != "stop":
            raise HTTPException(status_code=409, detail="Device is offline or state is stale")
        if action not in TRANSITIONS.get(current.get("job_state"), {}) and not (offline and action == "stop"):
            raise HTTPException(status_code=409, detail="Action is not valid for last known state")
        self._rate_check(device_id, user)
        fingerprint = (device_id, user, action, json.dumps(params, sort_keys=True))
        recent = self._recent[("duplicate", fingerprint)]
        now = self._now()
        while recent and now.timestamp() - recent[0] > 5:
            recent.popleft()
        if recent:
            raise HTTPException(status_code=409, detail={"reason_code": "DUPLICATE", "message": "Duplicate command request"})
        recent.append(now.timestamp())
        self.acquire_lock(device_id, user)
        ttl = body.get("ttl", 60)
        if not isinstance(ttl, int) or not 1 <= ttl <= 300:
            raise HTTPException(status_code=422, detail="ttl must be 1..300 seconds")
        command = {
            "cmd_id": str(uuid.uuid4()), "device_id": device_id, "action": action, "params": params,
            "issued_by": user, "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(), "source": "remote",
        }
        db.create_rmc_command(command)
        if not self._publish(device_id, command):
            db.update_rmc_command(command["cmd_id"], "failed", "FAULT", "MQTT publish failed")
            raise HTTPException(status_code=503, detail="Unable to publish command")
        self._schedule(command["cmd_id"], self.received_timeout_s, "received")
        warning = "Stop sent while device is offline" if offline else None
        self._broadcast_status(command["cmd_id"])
        return {"cmd_id": command["cmd_id"], "status": "sent", "warning": warning}

    def _schedule(self, cmd_id: str, seconds: int, waiting_for: str) -> None:
        timer = threading.Timer(seconds, self._timeout, args=(cmd_id, waiting_for))
        timer.daemon = True
        with self._lock:
            old = self._timers.pop(cmd_id, None)
            self._timers[cmd_id] = timer
        if old:
            old.cancel()
        timer.start()

    def _timeout(self, cmd_id: str, waiting_for: str) -> None:
        command = db.get_rmc_command(cmd_id)
        if command is None:
            return
        expected = "sent" if waiting_for == "received" else "accepted"
        if command["status"] == expected and db.update_rmc_command(cmd_id, "timeout", None, f"Timed out waiting for {waiting_for}"):
            self._broadcast_status(cmd_id)

    def handle_ack(self, device_id: str, ack: dict) -> None:
        cmd_id = ack.get("cmd_id")
        command = db.get_rmc_command(cmd_id) if isinstance(cmd_id, str) else None
        if command is None or command["device_id"] != device_id:
            return
        status = ack.get("status")
        if status not in {"received", "accepted", "done", "rejected", "expired", "failed"}:
            return
        allowed = {
            "sent": {"received", "rejected", "expired", "failed"},
            "received": {"accepted", "rejected", "expired", "failed"},
            "accepted": {"done", "failed", "expired"},
        }
        if status not in allowed.get(command["status"], set()):
            return
        if db.update_rmc_command(cmd_id, status, ack.get("reason_code"), ack.get("message")):
            if status == "accepted":
                self._schedule(cmd_id, self.done_timeout_s, "done")
            elif status in {"done", "rejected", "expired", "failed"}:
                with self._lock:
                    timer = self._timers.pop(cmd_id, None)
                if timer:
                    timer.cancel()
            self._broadcast_status(cmd_id)

    def _broadcast_status(self, cmd_id: str) -> None:
        command = db.get_rmc_command(cmd_id)
        if command:
            self._broadcast({"type": "rmc_cmd_status", "device_id": command["device_id"], "data": command})


rmc_service = RMCService()
