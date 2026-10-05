"""Shared Remote Machine Control (RMC) wire contract.

Values in this module are mirrored by
``RadialInfillWorkbench.modules.remote_control.contract``. Keep the two
modules equivalent for protocol constants; neither side imports the other
because the workbench and machine server are separate deployments.
"""

from __future__ import annotations

from copy import deepcopy
import os

RMC_TOPIC_SEGMENT = "rmc"
CMD_SEGMENT = "cmd"
ACK_SEGMENT = "ack"
STATE_SEGMENT = "state"
FILES_SEGMENT = "files"

COMMAND_ACTIONS = ("load", "start", "pause", "resume", "stop")
JOB_STATES = ("idle", "loaded", "running", "paused", "fault", "offline")
REMOTE_CONTROL_MODES = ("off", "ask")
LOAD_SOURCES = ("ftp", "local", "server")
COMMAND_SOURCE = "remote"
ACK_STATUSES = ("received", "accepted", "progress", "rejected", "expired", "done", "failed")
TERMINAL_ACK_STATUSES = ("rejected", "expired", "done", "failed")
NON_TERMINAL_ACK_STATUSES = ("received", "accepted", "progress")
PROGRESS_STAGES = ("downloading", "verifying")
REASON_CODES = (
    "INVALID_STATE",
    "NO_FILE",
    "MACHINE_BUSY",
    "OPERATOR_DECLINED",
    "EXPIRED",
    "REMOTE_DISABLED",
    "FILE_NOT_FOUND",
    "HASH_MISMATCH",
    "DOWNLOAD_FAILED",
    "FAULT",
    "DUPLICATE",
    "UNAUTHORIZED",
    "LOCKED_BY_OTHER",
    "FILE_TOO_LARGE",
    "UNSUPPORTED_TYPE",
    "FILE_CHANGED",
    "DISK_FULL",
    "DOWNLOAD_TIMEOUT",
    "TRANSFER_DENIED",
)

COMMAND_REQUIRED_FIELDS = (
    "cmd_id",
    "action",
    "params",
    "issued_by",
    "issued_at",
    "expires_at",
    "source",
)
LOAD_PARAMS_REQUIRED_FIELDS = ("source", "sha256", "size")
LOAD_PARAMS_LOCATION_FIELDS = ("path", "file_id")
ACK_REQUIRED_FIELDS = ("cmd_id", "status", "reason_code", "message", "ts")
STATE_REQUIRED_FIELDS = (
    "device_id",
    "job_state",
    "file",
    "progress",
    "last_cmd_id",
    "remote_control",
    "local_busy",
    "controlled_by",
    "recorded_at",
    "seq",
)

TRANSITIONS = {
    "idle": {"load": "loaded"},
    "loaded": {"load": "loaded", "start": "running"},
    "running": {"pause": "paused", "stop": "idle"},
    "paused": {"resume": "running", "stop": "idle"},
    "fault": {},
    "offline": {},
}

DEFAULT_RMC_STATE = {
    "job_state": "offline",
    "file": None,
    "progress": 0,
    "last_cmd_id": None,
    "remote_control": "off",
    "local_busy": False,
    "controlled_by": None,
    "recorded_at": None,
    "seq": 0,
}

# Workbench policy defaults. Phase 3 persists the remote-control mode in
# FreeCAD preferences; these values remain local to the workbench.
STOP_REQUIRES_CONFIRM = False
CONFIRM_TIMEOUT_S = 30
CMD_TTL_S = 60
MAX_JOB_FILE_MB = int(os.getenv("MAX_JOB_FILE_MB", "200"))
ALLOWED_EXTENSIONS = (".gcode", ".nc")
RMC_TRANSFER_SECRET = os.getenv("RMC_TRANSFER_SECRET", "")
DOWNLOAD_ALLOWANCE_S = int(os.getenv("RMC_DOWNLOAD_ALLOWANCE_S", "300"))


def rmc_topic(device_id: str, channel: str) -> str:
    """Return a namespaced RMC topic after validating the channel name."""
    if channel not in (CMD_SEGMENT, ACK_SEGMENT, STATE_SEGMENT, FILES_SEGMENT):
        raise ValueError(f"unsupported RMC channel: {channel}")
    return f"ufameasy/{device_id}/{RMC_TOPIC_SEGMENT}/{channel}"


def default_state(device_id: str) -> dict:
    """Return an independent, schema-complete initial state for a device."""
    state = deepcopy(DEFAULT_RMC_STATE)
    state["device_id"] = device_id
    return state
