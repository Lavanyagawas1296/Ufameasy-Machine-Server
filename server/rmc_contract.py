"""Shared Remote Machine Control (RMC) wire contract.

Values in this module are mirrored by
``RadialInfillWorkbench.modules.remote_control.contract``. Keep the two
modules equivalent for protocol constants; neither side imports the other
because the workbench and machine server are separate deployments.
"""

from __future__ import annotations

from copy import deepcopy

RMC_TOPIC_SEGMENT = "rmc"
CMD_SEGMENT = "cmd"
ACK_SEGMENT = "ack"
STATE_SEGMENT = "state"
FILES_SEGMENT = "files"

COMMAND_ACTIONS = ("load", "start", "pause", "resume", "stop")
JOB_STATES = ("idle", "loaded", "running", "paused", "fault", "offline")
REMOTE_CONTROL_MODES = ("off", "ask")
LOAD_SOURCES = ("ftp", "local")
COMMAND_SOURCE = "remote"
ACK_STATUSES = ("received", "accepted", "rejected", "expired", "done", "failed")
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
