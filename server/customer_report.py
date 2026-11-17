"""Usage report for the customer dashboard: how the machine was used in a period.

Everything is derived from what UFAMeasy already writes to its telemetry log:
execution_state changes, machine connection changes, PrintJobEvent rows and
RoleChangedEvent rows (UFAMeasy sign-ins). Nothing here writes data.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterator

from server import customer_config as config
from server import customer_stats as stats
from server.state_store import state

RUNNING, PAUSED, IDLE, OFF = "running", "paused", "idle", "off"
STATES = (RUNNING, PAUSED, IDLE, OFF)
_EXECUTION_STATES = {
    "running": RUNNING,
    "paused": PAUSED,
    "waiting": PAUSED,      # held at an M0 / M1 program pause
    "idle": IDLE,
    "stopped": IDLE,
    "completed": IDLE,
}
_EARLY_END = {"stopped", "aborted", "failed"}


class InvalidPeriod(ValueError):
    """The requested period is not a valid date range."""


def parse_period(from_value: str | None, to_value: str | None, *, today: date) -> tuple[date, date]:
    """Inclusive local dates. Defaults to this week (Monday to today)."""
    if not from_value and not to_value:
        return today - timedelta(days=today.weekday()), today
    try:
        start = date.fromisoformat(str(from_value))
        end = date.fromisoformat(str(to_value))
    except ValueError:
        raise InvalidPeriod("dates must be YYYY-MM-DD") from None
    if end < start:
        raise InvalidPeriod("the end date is before the start date")
    if (end - start).days + 1 > config.REPORT_MAX_DAYS:
        raise InvalidPeriod("the period is too long")
    if start.year < 2000 or end > today + timedelta(days=1):
        raise InvalidPeriod("the period is outside the available data")
    return start, end


def local_today(now: datetime | None = None) -> date:
    now_utc = stats._as_utc(now or datetime.now(timezone.utc))
    return now_utc.astimezone(stats._telemetry_timezone()).date()


# -- machine state timeline ---------------------------------------------------

def _effective_state(connection: str | None, execution: str | None) -> str:
    if connection is not None and connection != "connected":
        return OFF
    if execution in _EXECUTION_STATES:
        return _EXECUTION_STATES[execution]
    return IDLE if connection == "connected" else OFF


def state_intervals(start: datetime, end: datetime) -> list[tuple[datetime, datetime, str]]:
    """Machine state from ``start`` to ``end`` (UTC) as merged (from, to, state) intervals.

    A state lasts until the next reported change. Time before the first report,
    and time while UFAMeasy is not connected to the machine, counts as off.
    """
    changes: list[tuple[datetime, int, str, str]] = []
    for timestamp, event_id, value in stats._state_change_values("execution_state"):
        changes.append((timestamp, event_id, "execution", str(value or "").strip().lower()))
    for timestamp, event_id, value in stats._connection_state_values():
        changes.append((timestamp, event_id, "connection", value))
    changes.sort(key=lambda item: (item[0], item[1]))

    connection: str | None = None
    execution: str | None = None
    intervals: list[tuple[datetime, datetime, str]] = []
    cursor = start
    for timestamp, _event_id, kind, value in changes:
        if timestamp >= end:
            break
        if timestamp > cursor:
            intervals.append((cursor, timestamp, _effective_state(connection, execution)))
            cursor = timestamp
        if kind == "connection":
            connection = value
        else:
            execution = value
    if end > cursor:
        intervals.append((cursor, end, _effective_state(connection, execution)))

    merged: list[tuple[datetime, datetime, str]] = []
    for item in intervals:
        if merged and merged[-1][2] == item[2] and merged[-1][1] == item[0]:
            merged[-1] = (merged[-1][0], item[1], item[2])
        else:
            merged.append(item)
    return merged


def _clip(intervals, start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime, str]]:
    for left, right, name in intervals:
        lo, hi = max(left, start), min(right, end)
        if hi > lo:
            yield lo, hi, name


def _seconds_by_state(intervals, start: datetime, end: datetime) -> dict[str, float]:
    totals = {name: 0.0 for name in STATES}
    for lo, hi, name in _clip(intervals, start, end):
        totals[name] += (hi - lo).total_seconds()
    return totals


# -- report --------------------------------------------------------------------

def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _live_progress(device_id: str) -> float | None:
    try:
        value = state.get_parameters(device_id).get("progress_pct")
    except Exception:
        return None
    number = stats._number(value)
    return min(100.0, number) if number is not None else None


def _job_view(run: dict[str, Any], show_operator: bool, available: dict[str, bool]) -> dict[str, Any]:
    name = run["file_name"]
    display = stats._history_display_name(name)
    if name and name not in available:
        available[name] = stats._available_gcode_file(name) is not None
    view = {
        "started_at": stats.iso_utc(run["started_at"]),
        "ended_at": stats.iso_utc(run["ended_at"]),
        "run_seconds": run["run_seconds"],
        "file_name": name if display != "No file name" else "",
        "display_name": display,
        "result": run["status"],
        "reason": run["reason"],
        "available": bool(name) and available.get(name, False),
    }
    if show_operator:
        view["operator"] = run["operator"]
    return view


def usage_report(device_id: str, start_day: date, end_day: date, *, now: datetime | None = None) -> dict[str, Any]:
    now_utc = stats._as_utc(now or datetime.now(timezone.utc))
    tz = stats._telemetry_timezone()
    show_operator = config.show_operator()
    shift_start, shift_end = config.shift_window()

    period_start = datetime.combine(start_day, dtime.min, tzinfo=tz).astimezone(timezone.utc)
    period_end = datetime.combine(end_day + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(timezone.utc)
    observed_end = min(period_end, now_utc)
    intervals = state_intervals(period_start, max(period_start, observed_end))

    runs = stats._job_runs()
    open_runs = [run for run in runs if run["status"] == "open"]
    current_state = intervals[-1][2] if intervals and observed_end == now_utc else None
    running_run = None
    if open_runs and current_state in {RUNNING, PAUSED}:
        running_run = max(open_runs, key=lambda run: run["started_at"])

    # Days and the shift-hours timeline
    days: list[dict[str, Any]] = []
    shift_total = 0.0
    shift_run = 0.0
    failed_ends = [run["ended_at"] for run in runs if run["status"] == "failed" and run["ended_at"]]
    day = start_day
    while day <= end_day:
        day_start = datetime.combine(day, dtime.min, tzinfo=tz).astimezone(timezone.utc)
        day_end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=tz).astimezone(timezone.utc)
        s_start = day_start + timedelta(minutes=shift_start)
        s_end = day_start + timedelta(minutes=shift_end)
        s_len = (s_end - s_start).total_seconds()
        s_observed = min(s_end, now_utc)
        segments = []
        for lo, hi, name in _clip(intervals, s_start, s_observed):
            segments.append({
                "state": name,
                "left": round((lo - s_start).total_seconds() / s_len, 5),
                "width": round((hi - lo).total_seconds() / s_len, 5),
                "from": stats.iso_utc(lo),
                "to": stats.iso_utc(hi),
            })
        faults = [
            round((ended - s_start).total_seconds() / s_len, 5)
            for ended in failed_ends
            if s_start <= ended < s_end
        ]
        totals = _seconds_by_state(intervals, day_start, min(day_end, now_utc))
        if s_observed > s_start:
            shift_total += (s_observed - s_start).total_seconds()
            shift_run += _seconds_by_state(intervals, s_start, s_observed)[RUNNING]
        days.append({
            "date": day.isoformat(),
            "is_today": day_start <= now_utc < day_end,
            "is_future": day_start > now_utc,
            "segments": segments,
            "faults": faults,
            **{f"{name}_seconds": round(totals[name]) for name in STATES},
        })
        day += timedelta(days=1)

    totals = _seconds_by_state(intervals, period_start, observed_end)

    # Jobs that started in the period
    period_runs = [run for run in runs if period_start <= run["started_at"] < period_end]
    period_runs.sort(key=lambda run: run["started_at"], reverse=True)
    available: dict[str, bool] = {}
    jobs = []
    for run in period_runs[: config.REPORT_MAX_JOBS]:
        view = _job_view(run, show_operator, available)
        if run is running_run:
            view["result"] = "running"
        elif run["status"] == "open":
            view["result"] = "unrecorded"
        jobs.append(view)

    closed = [run for run in period_runs if run["status"] in stats._JOB_END_STATUSES]
    summary = {
        "count": len(closed),
        "completed": sum(run["status"] == "completed" for run in closed),
        "stopped_early": sum(run["status"] in _EARLY_END for run in closed),
        "faults": sum(run["status"] == "failed" for run in closed),
        "not_closed": len(period_runs) - len(closed),
    }

    # Files: grouped by the reported file name (UFAMeasy does not report the folder yet)
    groups: dict[str, dict[str, Any]] = {}
    for run in period_runs:
        if run["status"] not in stats._JOB_END_STATUSES:
            continue
        display = stats._history_display_name(run["file_name"])
        group = groups.setdefault(display.casefold(), {
            "file_name": run["file_name"] if display != "No file name" else "",
            "display_name": display,
            "runs": 0, "done": 0, "stopped": 0, "total_seconds": 0.0, "last_run": run["started_at"],
        })
        group["runs"] += 1
        group["done"] += run["status"] == "completed"
        group["stopped"] += run["status"] in _EARLY_END
        group["total_seconds"] += run["run_seconds"] or 0.0
        group["last_run"] = max(group["last_run"], run["started_at"])
    files = []
    for group in sorted(groups.values(), key=lambda g: (-g["runs"], -g["total_seconds"], g["display_name"].casefold())):
        name = group["file_name"]
        if name and name not in available:
            available[name] = stats._available_gcode_file(name) is not None
        files.append({
            "file_name": name,
            "display_name": group["display_name"],
            "runs": group["runs"],
            "done": group["done"],
            "stopped": group["stopped"],
            "total_seconds": round(group["total_seconds"]),
            "avg_seconds": round(group["total_seconds"] / group["runs"]) if group["runs"] else None,
            "available": bool(name) and available.get(name, False),
        })

    running_now = None
    if running_run is not None:
        progress = _live_progress(device_id)
        total_lines = running_run["total_lines"]
        running_now = {
            "file_name": running_run["file_name"],
            "display_name": stats._history_display_name(running_run["file_name"]),
            "state": current_state,
            "started_at": stats.iso_utc(running_run["started_at"]),
            "seconds_so_far": round(max(0.0, (now_utc - running_run["started_at"]).total_seconds())),
            "progress_pct": progress,
            "total_lines": total_lines,
            "approx_line": round(progress * total_lines / 100) if progress is not None and total_lines else None,
        }
        if show_operator:
            running_now["operator"] = running_run["operator"]

    return {
        "generated_at": stats.iso_utc(now_utc),
        "device_id": device_id,
        "machine_label": config.machine_label(device_id),
        "timezone": config.TELEMETRY_TZ,
        "period": {"from": start_day.isoformat(), "to": end_day.isoformat(), "days": len(days)},
        "shift": {"start": _hhmm(shift_start), "end": _hhmm(shift_end)},
        "operator_visible": show_operator,
        "running_now": running_now,
        "totals": {
            **{f"{name}_seconds": round(totals[name]) for name in STATES},
            "shift_run_seconds": round(shift_run),
            "shift_seconds": round(shift_total),
            "utilisation_pct": round(100 * shift_run / shift_total, 1) if shift_total else None,
        },
        "jobs_summary": summary,
        "days": days,
        "jobs": jobs,
        "jobs_truncated": len(period_runs) > len(jobs),
        "files": files,
        "signins": ufameasy_signins(period_start, period_end, show_operator),
    }


def ufameasy_signins(start: datetime, end: datetime, show_operator: bool, limit: int = 50) -> list[dict[str, Any]]:
    """UFAMeasy sign-ins from RoleChangedEvent rows, newest first.

    UFAMeasy writes this event only when the role changes, so a sign-in that keeps
    the same role (for example Operator after Operator) is not in the data.
    """
    rows = stats._read_rows(
        "SELECT id, timestamp, payload FROM telemetry_events WHERE event_type = ?",
        ("RoleChangedEvent",),
    )
    items: list[tuple[datetime, int, dict[str, Any]]] = []
    for row in rows:
        timestamp = stats.parse_telemetry_timestamp(row["timestamp"])
        if timestamp is None or not start <= timestamp < end:
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        item: dict[str, Any] = {
            "time": stats.iso_utc(timestamp),
            "role": stats.redact_message(payload.get("new_role"), "Unknown")[:40],
        }
        if show_operator:
            item["name"] = stats._legacy_user_name(payload.get("user")) or stats.UNKNOWN_OPERATOR
        items.append((timestamp, int(row["id"]), item))
    items.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    return [entry[2] for entry in items[:limit]]


def _format_local(value: str | None, fmt: str) -> str:
    parsed = stats.parse_server_timestamp(value)
    return parsed.astimezone(stats._telemetry_timezone()).strftime(fmt) if parsed else ""


_RESULT_LABELS = {
    "completed": "Completed",
    "stopped": "Stopped early",
    "aborted": "Stopped early",
    "failed": "Stopped by fault",
    "running": "Running",
    "unrecorded": "No end recorded",
    "open": "No end recorded",
}


def jobs_csv(report: dict[str, Any]) -> Iterator[str]:
    """The report's job list as CSV rows (operator column only when it is turned on)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    def flush() -> str:
        value = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return value

    header = ["date", "start_ist", "end_ist", "run_minutes", "file", "result", "reason"]
    if report["operator_visible"]:
        header.insert(1, "operator")
    writer.writerow(header)
    yield flush()
    for job in report["jobs"]:
        run_seconds = job.get("run_seconds")
        row = [
            _format_local(job["started_at"], "%Y-%m-%d"),
            _format_local(job["started_at"], "%H:%M"),
            _format_local(job["ended_at"], "%H:%M"),
            f"{run_seconds / 60:.1f}" if isinstance(run_seconds, (int, float)) else "",
            job["display_name"],
            _RESULT_LABELS.get(job["result"], job["result"]),
            job["reason"],
        ]
        if report["operator_visible"]:
            row.insert(1, job.get("operator", ""))
        writer.writerow([_csv_safe(value) for value in row])
        yield flush()


def _csv_safe(value: object) -> str:
    """Stop spreadsheet formula injection from file names or reasons."""
    text = str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text
