"""Read-only aggregation helpers for the customer API."""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from server import customer_config as config
from server import db
from server.routes import _machine_hours_from_odometer
from server.state_store import state


class CustomerDataError(Exception):
    """Raised when the customer API's read-only telemetry source is unavailable."""


_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = Lock()
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$", re.IGNORECASE)
_UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{12}_", re.IGNORECASE)
_UNNAMED_HISTORY_RE = re.compile(r"^unnamed\d*$", re.IGNORECASE)
_CONTROLLER_GCODE_RE = re.compile(r"^ufameasy_gcode_[\w-]+\.gcd$", re.IGNORECASE)


def clear_overview_cache() -> None:
    """Clear cached overview data; used by tests and safe operational reloads."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _telemetry_timezone():
    """Return the workbench timestamp zone without requiring system tzdata for IST."""
    if config.TELEMETRY_TZ == "Asia/Kolkata":
        return timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.TELEMETRY_TZ)
    except Exception as exc:
        raise CustomerDataError("telemetry timezone unavailable") from exc


def parse_telemetry_timestamp(value: object) -> datetime | None:
    """Parse workbench-local telemetry timestamps into UTC.

    Legacy telemetry has no offset and is written in ``TELEMETRY_TZ``.  ISO
    values carrying an offset (including ``Z``) retain that authoritative zone.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    for fmt in ("%d/%m/%Y %I:%M:%S.%f %p", "%d/%m/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=_telemetry_timezone()).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_server_timestamp(value: object) -> datetime | None:
    """Parse server-stamped ISO values; a legacy naive value is UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def file_basename(value: object) -> str:
    """Return only a display-safe basename for either slash convention."""
    if not isinstance(value, str):
        return "unknown"
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return name or "unknown"


def _telemetry_path() -> Path:
    return Path(config.TELEMETRY_DB_PATH)


def _open_telemetry() -> sqlite3.Connection:
    path = _telemetry_path()
    if not path.is_file():
        raise CustomerDataError("telemetry unavailable")
    # Read-only mode still sees the workbench's SQLite WAL.  Immutable mode
    # does not, which leaves the dashboard stuck on an old checkpoint.
    try:
        db.ensure_telemetry_timestamp_index(path)
    except OSError:
        pass
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise CustomerDataError("telemetry unavailable") from exc


def _read_rows(sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = _open_telemetry()
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise CustomerDataError("telemetry unavailable") from exc
    finally:
        conn.close()


def latest_telemetry_event_id() -> int | None:
    """Return the newest UFAMeasy telemetry row for live notifications."""
    row = _read_rows("SELECT MAX(id) AS event_id FROM telemetry_events")[0]
    value = row["event_id"]
    return int(value) if value is not None else None


def _print_events() -> list[tuple[datetime, int, str, dict[str, Any]]]:
    rows = _read_rows(
        "SELECT id, timestamp, session_id, payload FROM telemetry_events WHERE event_type = ?",
        ("PrintJobEvent",),
    )
    events: list[tuple[datetime, int, str, dict[str, Any]]] = []
    for row in rows:
        timestamp = parse_telemetry_timestamp(row["timestamp"])
        if timestamp is None:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        session_id = payload.get("session_id") or row["session_id"] or "__unknown__"
        events.append((timestamp, int(row["id"]), str(session_id), payload))
    return sorted(events, key=lambda item: (item[0], item[1]))


def _state_change_values(parameter_key: str) -> list[tuple[datetime, int, object]]:
    """Read a UFAMeasy machine-state field directly from telemetry."""
    rows = _read_rows(
        "SELECT id, timestamp, payload FROM telemetry_events WHERE event_type = ?",
        ("StateChangedEvent",),
    )
    values: list[tuple[datetime, int, object]] = []
    for row in rows:
        timestamp = parse_telemetry_timestamp(row["timestamp"])
        if timestamp is None:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("parameter_key") == parameter_key:
            values.append((timestamp, int(row["id"]), payload.get("new_value")))
    return sorted(values, key=lambda item: (item[0], item[1]))


_JOB_END_STATUSES = frozenset({"stopped", "completed", "aborted"})
_JOB_FILE_KEYS = ("gcode_file", "file_name", "current_file", "file")


def _job_file_name(*payloads: dict[str, Any]) -> str:
    """Get the file the machine reported for a job attempt, if any."""
    for payload in payloads:
        for key in _JOB_FILE_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return file_basename(value)
    return ""


def _closed_job_attempts() -> list[tuple[datetime, str, str]]:
    """Pair telemetry job starts with their terminal events.

    The machine's telemetry log is authoritative for runs initiated locally,
    including an operator abort, which may not create a server-side session.
    """
    open_jobs: dict[str, tuple[dict[str, Any], str]] = {}
    closed: list[tuple[datetime, str, str]] = []
    current_file = ""
    file_changes = _state_change_values("current_file")
    file_index = 0
    for timestamp, event_id, session_id, payload in _print_events():
        while file_index < len(file_changes) and file_changes[file_index][:2] <= (timestamp, event_id):
            value = file_changes[file_index][2]
            current_file = file_basename(value) if isinstance(value, str) and value.strip() else ""
            file_index += 1
        status = str(payload.get("status") or "").lower()
        if status == "started":
            open_jobs[session_id] = (payload, current_file)
        elif status in _JOB_END_STATUSES:
            opened = open_jobs.pop(session_id, None)
            if opened is not None:
                start_payload, reported_file = opened
                file_name = _job_file_name(payload, start_payload)
                if _CONTROLLER_GCODE_RE.fullmatch(file_name) and reported_file:
                    file_name = reported_file
                closed.append((timestamp, status, file_name))
        # paused and resumed preserve an already-open job.
    return closed


def _closed_jobs() -> list[tuple[datetime, str]]:
    """Return closed attempts for the overview counters."""
    return [(ended_at, status) for ended_at, status, _file_name in _closed_job_attempts()]


def _has_active_job() -> bool:
    """True while telemetry has a started job without a terminal event."""
    active_sessions: set[str] = set()
    for _timestamp, _event_id, session_id, payload in _print_events():
        status = str(payload.get("status") or "").lower()
        if status == "started":
            active_sessions.add(session_id)
        elif status in _JOB_END_STATUSES:
            active_sessions.discard(session_id)
    return bool(active_sessions)


def job_summary(window: str, *, now: datetime | None = None) -> dict[str, Any]:
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    cutoff = now_utc - config.WINDOWS[window]
    total = 0
    completed = 0
    stopped = 0
    for closed_at, status in _closed_jobs():
        if not cutoff <= closed_at <= now_utc:
            continue
        total += 1
        if status == "completed":
            completed += 1
        else:
            stopped += 1
    return {"window": window, "count": total, "completed": completed, "stopped": stopped}


def _device_last_seen(device_id: str) -> datetime | None:
    candidates: list[datetime] = []
    live = parse_server_timestamp(state.get_rmc_state(device_id).get("received_at"))
    if live is not None:
        candidates.append(live)
    conn = None
    try:
        conn = db.get_conn()
        row = conn.execute("SELECT last_seen FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        persisted = parse_server_timestamp(row["last_seen"] if row else None)
        if persisted is not None:
            candidates.append(persisted)
    except sqlite3.Error:
        pass
    finally:
        if conn is not None:
            conn.close()
    try:
        rows = _read_rows("SELECT timestamp FROM telemetry_events ORDER BY id DESC LIMIT 50")
        candidates.extend(
            timestamp
            for row in rows
            if (timestamp := parse_telemetry_timestamp(row["timestamp"])) is not None
        )
    except CustomerDataError:
        pass
    return max(candidates) if candidates else None


def device_status(device_id: str, *, now: datetime | None = None) -> dict[str, str | None]:
    del device_id, now
    values = _state_change_values("execution_state")
    if not values:
        return {"state": "unknown", "last_seen": None}
    timestamp, _event_id, value = values[-1]
    state_name = str(value).strip().lower() if value is not None else "unknown"
    return {"state": state_name or "unknown", "last_seen": iso_utc(timestamp)}


def loaded_file(device_id: str) -> str | None:
    del device_id
    values = _state_change_values("current_file")
    if not values:
        return None
    value = values[-1][2]
    return file_basename(value) if isinstance(value, str) and value.strip() else None


def overview(device_id: str, window: str, *, now: datetime | None = None) -> dict[str, Any]:
    key = (device_id, window)
    monotonic_now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and monotonic_now - cached[0] < config.POLL_CACHE_TTL_S:
            return copy.deepcopy(cached[1])
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    data = {
        "server_time": iso_utc(now_utc),
        "device_id": device_id,
        "machine_label": os.getenv("CUSTOMER_MACHINE_LABEL", "").strip() or device_id,
        "status": device_status(device_id, now=now_utc),
        "loaded_file": loaded_file(device_id),
        "machine_hours": _machine_hours_from_odometer(),
        "machine_hours_scope": "server",
        "jobs": job_summary(window, now=now_utc),
    }
    with _CACHE_LOCK:
        _CACHE[key] = (monotonic_now, copy.deepcopy(data))
    return data


def _history_bounds(window: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    if window == "all":
        return None, None
    if window not in config.WINDOWS:
        raise ValueError("invalid window")
    return now - config.WINDOWS[window], now


def _history_display_name(value: object) -> str:
    """Normalise uploaded names for grouping without changing download names."""
    name = file_basename(value).strip()
    name = _UPLOAD_PREFIX_RE.sub("", name)
    if _CONTROLLER_GCODE_RE.fullmatch(name):
        return "Controller G-code"
    if not name or name.casefold() in {"unknown", "untitled"} or _UNNAMED_HISTORY_RE.fullmatch(name):
        return "No file name"
    return name


def _is_unnamed_file(value: object) -> bool:
    return _history_display_name(value) == "No file name"


def _available_gcode_file(name: str) -> tuple[str, Path] | None:
    """Return the newest on-disk registry entry matching a basename.

    The request name is only ever used as a SQL parameter.  The filesystem
    path comes from the content hash persisted by the server's G-code ingest.
    """
    names = [file_basename(name)]
    clean_name = _UPLOAD_PREFIX_RE.sub("", names[0])
    if clean_name and clean_name not in names:
        names.append(clean_name)

    def registry_file() -> tuple[str, Path] | None:
        conn = db.get_conn()
        try:
            row = None
            for candidate in names:
                row = conn.execute(
                    """SELECT sha256, file_name FROM gcode_files
                       WHERE file_name = ? COLLATE NOCASE
                       ORDER BY last_seen DESC, sha256 DESC
                       LIMIT 1""",
                    (candidate,),
                ).fetchone()
                if row is not None:
                    break
        except sqlite3.Error as exc:
            raise CustomerDataError("file registry unavailable") from exc
        finally:
            conn.close()
        if row is None:
            return None
        sha256 = str(row["sha256"])
        if _SHA256_RE.fullmatch(sha256) is None:
            return None
        path = db.DATA_DIR / "gcode" / f"{sha256}.nc"
        return (str(row["file_name"]), path) if path.is_file() else None

    result = registry_file()
    if result is not None:
        return result

    # Backfill an old on-premise job once from an explicitly trusted root.
    # New/cloud jobs use the server upload archive below instead.
    source = _legacy_gcode_source(clean_name)
    if source is not None:
        try:
            from server.gcode_ingest import ingest_gcode_file

            ingest_gcode_file(source, clean_name, device_id=config.CUSTOMER_DEVICE_ID)
        except (OSError, sqlite3.Error):
            pass
        result = registry_file()
        if result is not None:
            return result

    # Files uploaded through Remote Machine Control already reside in the
    # server archive and are safe to serve from any cloud deployment.
    conn = db.get_conn()
    try:
        row = None
        for candidate in names:
            row = conn.execute(
                """SELECT display_name, stored_name FROM rmc_server_files
                   WHERE display_name = ? COLLATE NOCASE
                   ORDER BY uploaded_at DESC
                   LIMIT 1""",
                (candidate,),
            ).fetchone()
            if row is not None:
                break
    except sqlite3.Error as exc:
        raise CustomerDataError("file registry unavailable") from exc
    finally:
        conn.close()
    if row is None:
        return None
    path = db.RMC_FILES_DIR / str(row["stored_name"])
    return (str(row["display_name"]), path) if path.is_file() else None


def _legacy_gcode_source(name: str) -> Path | None:
    """Find a telemetry-referenced local source only beneath trusted roots."""
    if not config.LEGACY_GCODE_ROOTS:
        return None
    roots: list[Path] = []
    for root in config.LEGACY_GCODE_ROOTS:
        try:
            roots.append(root.resolve(strict=True))
        except OSError:
            continue
    if not roots:
        return None
    rows = _read_rows(
        "SELECT timestamp, payload FROM telemetry_events WHERE event_type = ?",
        ("JobEstimationEvent",),
    )
    candidates: list[tuple[datetime, Path]] = []
    for row in rows:
        timestamp = parse_telemetry_timestamp(row["timestamp"])
        if timestamp is None:
            continue
        try:
            target = json.loads(row["payload"]).get("target_file")
        except (TypeError, json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(target, str) or file_basename(target).casefold() != name.casefold():
            continue
        try:
            path = Path(target).resolve(strict=True)
        except OSError:
            continue
        if any(path.is_relative_to(root) for root in roots):
            candidates.append((timestamp, path))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def customer_history(device_id: str, window: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Group completed/aborted machine runs by a display-safe file name."""
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    start, end = _history_bounds(window, now_utc)
    runs = [
        (ended_at, file_name)
        for ended_at, _status, file_name in _closed_job_attempts()
        if (start is None or ended_at >= start) and (end is None or ended_at <= end)
    ]

    # Older server-created sessions remain a fallback for installations that
    # predate machine PrintJobEvent telemetry.
    if not runs:
        where = [
            "device_id = ?",
            "started_at IS NOT NULL",
            "session_id NOT LIKE ?",
        ]
        params: list[object] = [device_id, "auto-%"]
        if start is not None and end is not None:
            where.extend(["started_at >= ?", "started_at <= ?"])
            params.extend([start.isoformat(), end.isoformat()])
        sessions_sql = "SELECT file_name, started_at FROM sessions WHERE " + " AND ".join(where)
        conn = db.get_conn()
        try:
            rows = conn.execute(sessions_sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise CustomerDataError("session history unavailable") from exc
        finally:
            conn.close()
        runs = [
            (started_at, file_basename(row["file_name"]))
            for row in rows
            if (started_at := parse_server_timestamp(row["started_at"])) is not None
        ]

    groups: dict[str, dict[str, Any]] = {}
    total_runs = 0
    unnamed_runs = 0
    for started_at, original_name in runs:
        total_runs += 1
        name = _history_display_name(original_name)
        if _is_unnamed_file(original_name):
            unnamed_runs += 1
        key = name.casefold()
        group = groups.setdefault(
            key,
            {
                "file_name": original_name if name != "No file name" else "",
                "display_name": name,
                "runs": 0,
                "last_run": started_at,
                "available": name != "No file name",
            },
        )
        group["runs"] += 1
        if started_at > group["last_run"]:
            group["last_run"] = started_at
            group["file_name"] = original_name if name != "No file name" else ""

    selected_groups = sorted(
        groups.values(),
        key=lambda group: group["last_run"],
        reverse=True,
    )[:100]
    files: list[dict[str, Any]] = []
    for group in selected_groups:
        name = str(group["file_name"])
        files.append(
            {
                "file_name": name,
                "display_name": group["display_name"],
                "runs": group["runs"],
                "last_run": iso_utc(group["last_run"]),
                "available": bool(group["available"]) and _available_gcode_file(name) is not None,
            }
        )
    return {"window": window, "total_runs": total_runs, "unnamed_runs": unnamed_runs, "files": files}


def customer_gcode_file(name: str) -> tuple[str, Path] | None:
    """Look up a customer-downloadable file by a registry basename only."""
    return _available_gcode_file(name)


def gcode_preview(name: str, line_limit: int) -> dict[str, Any] | None:
    file_record = customer_gcode_file(name)
    if file_record is None:
        return None
    file_name, path = file_record
    lines: list[str] = []
    total_lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                total_lines += 1
                if total_lines <= line_limit:
                    lines.append(line.rstrip("\r\n"))
    except OSError:
        return None
    return {
        "file_name": file_name,
        "lines": lines,
        "total_lines": total_lines,
        "truncated": total_lines > line_limit,
    }


_FILE_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\|/)(?:[^\s\"'<>]+[\\/])+[^\s\"'<>]*")
_IP_ADDRESS_RE = re.compile(
    r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?::\d{1,5})?(?![\w.])"
)


def redact_message(value: object, fallback: str) -> str:
    """Keep log text short while excluding paths, IP addresses, and tracebacks."""
    message = str(value) if isinstance(value, str) else fallback
    message = re.split(r"traceback", message, maxsplit=1, flags=re.IGNORECASE)[0]
    message = _FILE_PATH_RE.sub("[redacted]", message)
    message = _IP_ADDRESS_RE.sub("[redacted]", message)
    message = " ".join(message.split())[:200].rstrip()
    return message or fallback[:200]


def _safe_message(event_type: str, payload: object) -> str:
    values = payload if isinstance(payload, dict) else {}
    if event_type == "PrintJobEvent":
        status = redact_message(values.get("status"), "updated").lower()
        return f"Print job {status}"
    if event_type == "MachineConnectionEvent":
        state_name = str(
            values.get("state") or values.get("status") or values.get("connection_state") or ""
        ).strip().lower()
        return f"Machine {state_name}" if state_name else "Machine connection updated"
    if event_type == "machine_connect":
        return "Machine connected"
    if event_type == "machine_disconnect":
        return "Machine disconnected"
    if event_type == "PipelineErrorEvent":
        return redact_message(values.get("error_message") or values.get("message"), "Machine pipeline error")
    if event_type == "AlarmEvent":
        return redact_message(values.get("message"), "Machine alarm")
    if event_type == "StateChangedEvent":
        parameter = redact_message(values.get("parameter_key"), "Machine state")
        if "new_value" in values:
            return f"{parameter} changed to {redact_message(str(values['new_value']), 'updated')}"
        return f"{parameter} changed"
    detail = (
        values.get("message")
        or values.get("error_message")
        or values.get("detail")
        or values.get("state")
        or values.get("status")
    )
    label = redact_message(event_type, "UFAMeasy event")
    if detail is None:
        return label
    return redact_message(f"{label}: {detail}", label)


def customer_logs(
    *,
    severity: str | tuple[str, str] | None,
    start: datetime,
    end: datetime,
    cursor: tuple[datetime, int] | None,
    limit: int,
) -> tuple[list[dict[str, str]], str | None]:
    params: list[object] = []
    sql = "SELECT id, timestamp, severity, event_type, payload FROM telemetry_events WHERE 1 = 1"
    if isinstance(severity, tuple):
        sql += " AND severity IN (?, ?)"
        params.extend(severity)
    elif severity:
        sql += " AND severity = ?"
        params.append(severity)
    rows = _read_rows(sql, tuple(params))
    records: list[tuple[datetime, int, dict[str, str]]] = []
    for row in rows:
        timestamp = parse_telemetry_timestamp(row["timestamp"])
        if timestamp is None or not start <= timestamp <= end:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        message = _safe_message(str(row["event_type"]), payload)
        record = {
            "time": iso_utc(timestamp) or "",
            "severity": str(row["severity"] or "INFO").upper(),
            "message": message,
        }
        records.append((timestamp, int(row["id"]), record))
    records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if cursor is not None:
        records = [item for item in records if (item[0], item[1]) < cursor]
    page = records[:limit]
    next_cursor = None
    if len(records) > limit and page:
        next_cursor = encode_cursor(page[-1][0], page[-1][1])
    return [item[2] for item in page], next_cursor


def encode_cursor(timestamp: datetime, event_id: int) -> str:
    import base64

    value = json.dumps({"t": iso_utc(timestamp), "id": event_id}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, int] | None:
    import base64

    if not value or len(value) > 256:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        timestamp = parse_server_timestamp(decoded.get("t"))
        event_id = decoded.get("id")
        if timestamp is None or not isinstance(event_id, int) or event_id < 0:
            return None
        return timestamp, event_id
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
