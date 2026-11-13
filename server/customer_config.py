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
