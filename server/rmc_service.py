"""RMC command issuance, audit tracking, locks, and timeout handling."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import PurePosixPath
import threading
import urllib.parse
import uuid

from fastapi import HTTPException

from server import db
from server.rmc_contract import (
    COMMAND_ACTIONS,
    DOWNLOAD_ALLOWANCE_S,
    LOAD_SOURCES,
    PROGRESS_STAGES,
    TRANSITIONS,
    rmc_topic,
    CMD_SEGMENT,
)
from server.rmc_transfer import sign_download_token
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
        self.done_timeout_s = max(60, int(os.getenv("RMC_DONE_TIMEOUT_S", "120")))
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

    def _validate_params(
        self,
        action: str,
        params: dict,
        device_id: str = "",
        *,
        download_expiry: int | None = None,
        request_base_url: str | None = None,
    ) -> dict:
        if not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="params must be an object")
        if action != "load":
            return {}

        # Reject client-supplied paths or URLs
        if "path" in params or "url" in params or "download_url" in params:
            raise HTTPException(
                status_code=422,
                detail={"reason_code": "UNAUTHORIZED", "message": "Client-supplied paths or URLs are forbidden"},
            )

        source = params.get("source")
        if source not in LOAD_SOURCES:
            raise HTTPException(status_code=422, detail="load source must be server, local, or ftp")

        if source == "server":
            file_id = params.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise HTTPException(status_code=422, detail={"reason_code": "NO_FILE", "message": "server load requires file_id"})
            file_record = db.get_rmc_server_file(file_id)
            if not file_record:
                raise HTTPException(
                    status_code=404,
                    detail={"reason_code": "FILE_NOT_FOUND", "message": f"Server file {file_id} not found"},
                )
            disk_path = db.RMC_FILES_DIR / file_record["stored_name"]
            if not disk_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail={"reason_code": "FILE_NOT_FOUND", "message": "Server file missing from disk"},
                )
            if disk_path.stat().st_size != file_record["size"]:
                raise HTTPException(
                    status_code=500,
                    detail={"reason_code": "FILE_CHANGED", "message": "Server file size mismatch"},
                )
            token = sign_download_token(
                file_id=file_id,
                device_id=device_id,
                expiry=download_expiry or int(self._now().timestamp()) + 300,
            )
            env_base = os.getenv("RMC_SERVER_BASE_URL", "").strip()
            if env_base:
                base_url = env_base.rstrip("/")
            else:
                candidate = (request_base_url or "").strip()
                if not candidate:
                    raise HTTPException(
                        status_code=400,
                        detail="Download URL base not provided; please set RMC_SERVER_BASE_URL",
                    )
                parsed = urllib.parse.urlparse(candidate if "://" in candidate else f"http://{candidate}")
                host = (parsed.hostname or "").lower()
                if host in {"localhost", "127.0.0.1", "::1"}:
                    raise HTTPException(
                        status_code=400,
                        detail="Download URL base resolves to loopback (localhost/127.0.0.1/::1); please set RMC_SERVER_BASE_URL environment variable for remote machines",
                    )
                base_url = candidate.rstrip("/")
            download_url = f"{base_url}/api/rmc/files/{file_id}/download?token={token}"
            return {
                "source": "server",
                "file_id": file_id,
                "name": file_record["display_name"],
                "size": file_record["size"],
                "sha256": file_record["sha256"],
                "download_url": download_url,
            }

        if source == "local":
            file_id = params.get("file_id")
            if not isinstance(file_id, str) or not file_id or "/" in file_id or "\\" in file_id:
                raise HTTPException(status_code=422, detail={"reason_code": "NO_FILE", "message": "local load requires an opaque file_id"})
            device_files = state.get_rmc_files(device_id)
            match = None
            for f in device_files:
                if isinstance(f, dict) and f.get("file_id") == file_id:
                    match = f
                    break
            if not match:
                raise HTTPException(
                    status_code=404,
                    detail={"reason_code": "FILE_NOT_FOUND", "message": f"Local file '{file_id}' not found on device '{device_id}'"},
                )
            name = match.get("name") or file_id
            size = match.get("size", 0)
            sha256 = match.get("sha256", "")
            return {
                "source": "local",
                "file_id": file_id,
                "name": name,
                "size": size,
                "sha256": sha256,
            }

        if source == "ftp":
            ftp_name = params.get("ftp_name")
            if not isinstance(ftp_name, str):
                raise HTTPException(status_code=422, detail="ftp load requires ftp_name")
            relative = self._safe_ftp_name(ftp_name)
            size = params.get("size")
            sha256 = params.get("sha256")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not isinstance(sha256, str) or not sha256:
                raise HTTPException(status_code=422, detail="ftp load requires size and sha256")
            return {"source": "ftp", "path": relative, "name": relative, "sha256": sha256, "size": size}

        return {}

    def issue_command(
        self,
        device_id: str,
        body: dict,
        user: str,
        *,
        request_base_url: str | None = None,
    ) -> dict:
        self._require_known_device(device_id)
        action = body.get("action") if isinstance(body, dict) else None
        if action not in COMMAND_ACTIONS:
            raise HTTPException(status_code=422, detail="Unsupported RMC action")

        ttl = body.get("ttl", 60)
        if not isinstance(ttl, int) or not 1 <= ttl <= 300:
            raise HTTPException(status_code=422, detail="ttl must be 1..300 seconds")

        now = self._now()
        download_allowance_s = int(os.getenv("RMC_DOWNLOAD_ALLOWANCE_S", str(DOWNLOAD_ALLOWANCE_S)))
        download_expiry = int(now.timestamp()) + ttl + download_allowance_s

        params = self._validate_params(
            action,
            body.get("params", {}),
            device_id,
            download_expiry=download_expiry,
            request_base_url=request_base_url,
        )
        current = self._device_state(device_id)
        offline = current.get("job_state") == "offline"
        if offline and action != "stop":
            raise HTTPException(status_code=409, detail="Device is offline or state is stale")
        if action not in TRANSITIONS.get(current.get("job_state"), {}) and not (offline and action == "stop"):
            raise HTTPException(status_code=409, detail="Action is not valid for last known state")
        self._rate_check(device_id, user)
        fingerprint = (device_id, user, action, json.dumps(params, sort_keys=True))
        recent = self._recent[("duplicate", fingerprint)]
        while recent and now.timestamp() - recent[0] > 5:
            recent.popleft()
        if recent:
            raise HTTPException(status_code=409, detail={"reason_code": "DUPLICATE", "message": "Duplicate command request"})
        recent.append(now.timestamp())
        self.acquire_lock(device_id, user)
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
        expected = {"sent"} if waiting_for == "received" else {"accepted", "progress"}
        if command["status"] in expected and db.update_rmc_command(cmd_id, "timeout", None, f"Timed out waiting for {waiting_for}"):
            self._broadcast_status(cmd_id)

    def handle_ack(self, device_id: str, ack: dict) -> None:
        cmd_id = ack.get("cmd_id")
        command = db.get_rmc_command(cmd_id) if isinstance(cmd_id, str) else None
        if command is None or command["device_id"] != device_id:
            return
        status = ack.get("status")
        if status not in {"received", "accepted", "progress", "done", "rejected", "expired", "failed"}:
            return
        allowed = {
            "sent": {"received", "accepted", "rejected", "expired", "failed"},
            "received": {"accepted", "progress", "rejected", "expired", "failed"},
            "accepted": {"progress", "done", "failed", "expired"},
            "progress": {"progress", "accepted", "rejected", "done", "failed", "expired"},
        }
        if status not in allowed.get(command["status"], set()):
            return

        message = ack.get("message")
        extra_broadcast = {}
        if status == "progress":
            stage = ack.get("stage")
            if not stage and isinstance(ack.get("progress"), dict):
                stage = ack["progress"].get("stage")
            if stage not in PROGRESS_STAGES:
                stage = "downloading"
            try:
                percent = float(
                    ack.get("percent")
                    if ack.get("percent") is not None
                    else (ack.get("progress") or {}).get("percent", 0)
                )
            except (ValueError, TypeError):
                percent = 0.0
            extra_broadcast = {"stage": stage, "percent": percent}
            if not message:
                message = f"{stage} {percent:.0f}%"

        updated = db.update_rmc_command(cmd_id, status, ack.get("reason_code"), message)
        if status in {"accepted", "progress"}:
            self._schedule(cmd_id, self.done_timeout_s, "done")
        elif status in {"done", "rejected", "expired", "failed"}:
            with self._lock:
                timer = self._timers.pop(cmd_id, None)
            if timer:
                timer.cancel()
        if updated:
            self._broadcast_status(cmd_id, extra=extra_broadcast)

    def _broadcast_status(self, cmd_id: str, extra: dict | None = None) -> None:
        command = db.get_rmc_command(cmd_id)
        if command:
            if extra:
                command.update(extra)
            self._broadcast({"type": "rmc_cmd_status", "device_id": command["device_id"], "data": command})


rmc_service = RMCService()
