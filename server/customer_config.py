"""Configuration for the read-only customer API."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path


POLL_CACHE_TTL_S = 2
OFFLINE_AFTER_S = 30
WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_WINDOW = "7d"
LOG_PAGE_DEFAULT = 50
LOG_PAGE_MAX = 100
LOG_RETENTION_DAYS = 30
LOG_EVENT_ALLOWLIST = frozenset({
    "AlarmEvent",
    "MachineConnectionEvent",
    "PipelineErrorEvent",
    "PrintJobEvent",
    "SystemStateEvent",
    "machine_connect",
    "machine_disconnect",
})
LOG_SEVERITIES = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
RATE_LIMIT_PER_MIN = 60
# One customer = one server install with a clean DB.
# The device must be configured locally; it is never accepted from a request.
CUSTOMER_DEVICE_ID = os.getenv("UFAM_CUSTOMER_DEVICE_ID", "").strip() or None
TELEMETRY_TZ = os.getenv("TELEMETRY_TZ", "Asia/Kolkata").strip() or "Asia/Kolkata"
TELEMETRY_DB_PATH = Path.home() / ".ufameasy" / "telemetry" / "ufameasy_sys.u5d"
# Optional trusted on-premise roots used only to backfill legacy files into
# the server archive. Cloud deployments leave this unset and use uploads.
LEGACY_GCODE_ROOTS = tuple(
    Path(value).expanduser()
    for value in os.getenv("UFAM_LEGACY_GCODE_ROOTS", "").split(os.pathsep)
    if value.strip()
)
GZIP_MIN_SIZE = 500


def machine_label(device_id: str) -> str:
    return os.getenv("CUSTOMER_MACHINE_LABEL", "").strip() or device_id


REPORT_MAX_DAYS = 92
REPORT_MAX_JOBS = 500


def _shift_minutes(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip() or default
    try:
        hours, minutes = (int(part) for part in raw.split(":", 1))
    except ValueError:
        hours, minutes = (int(part) for part in default.split(":", 1))
    if not (0 <= hours <= 24 and 0 <= minutes < 60) or hours * 60 + minutes > 24 * 60:
        hours, minutes = (int(part) for part in default.split(":", 1))
    return hours * 60 + minutes


def shift_window() -> tuple[int, int]:
    """Shift start and end as minutes after local midnight (CUSTOMER_SHIFT_START/END, HH:MM).

    Falls back to 09:00-18:00 when a value is missing, malformed, or the end is not after the start.
    """
    start = _shift_minutes("CUSTOMER_SHIFT_START", "09:00")
    end = _shift_minutes("CUSTOMER_SHIFT_END", "18:00")
    if end <= start:
        return 9 * 60, 18 * 60
    return start, end


def show_operator() -> bool:
    """True when CUSTOMER_SHOW_OPERATOR=1: job history may name the operator.

    Off by default: operator names are personal data. Read on every call so the
    setting can change without a restart. Never applies to log messages or CSV.
    """
    return os.getenv("CUSTOMER_SHOW_OPERATOR", "0").strip().lower() in {"1", "true", "yes", "on"}
