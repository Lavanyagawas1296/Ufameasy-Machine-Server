"""Authenticated, read-only customer API routes."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from server import customer_config as config
from server import customer_stats
from server.auth import AUTH_COOKIE_NAME, require_auth
from server.rmc_auth import Principal, require_role, resolve_principal


router = APIRouter(prefix="/api/customer", tags=["customer"])
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = Lock()
_FILE_NAME_RE = re.compile(r"^[\w.\- ]{1,120}$")
_MAX_GCODE_VIEW_LINES = 500
_MAX_LOG_DOWNLOAD_ROWS = 50_000


def reset_rate_limits() -> None:
    """Clear in-memory buckets for tests."""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()


def _generic_error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code)


def _customer_principal(
    request: Request, authorization: str | None = Header(default=None)
) -> Principal:
    try:
        require_auth(request.cookies.get(AUTH_COOKIE_NAME))
        if os.getenv("CUSTOMER_MODE", "0").lower() in {"1", "true", "yes", "on"}:
            return Principal("customer", "viewer")
        principal = resolve_principal(authorization)
        require_role(principal, "viewer", "operator", "admin")
        return principal
    except HTTPException as exc:
        if exc.status_code == 401:
            raise _generic_error(401, "authentication_required") from None
        raise _generic_error(403, "not_authorized") from None


def _rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[client_ip]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= config.RATE_LIMIT_PER_MIN:
            raise HTTPException(status_code=429, detail="rate_limited", headers={"Retry-After": "60"})
        bucket.append(now)


def _require_access(
    request: Request,
    _principal: Principal = Depends(_customer_principal),
) -> str:
    _rate_limit(request)
    if not config.CUSTOMER_DEVICE_ID:
        raise _generic_error(503, "customer_device_not_configured")
    return config.CUSTOMER_DEVICE_ID


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, max-age=0",
        "Content-Security-Policy": "default-src 'none'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _matches_etag(value: str | None, etag: str) -> bool:
    if not value:
        return False
    return value.strip() == "*" or etag in {item.strip() for item in value.split(",")}


def _json_response(
    payload: Any,
    request: Request,
    if_none_match: str | None,
    *,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    etag = '"' + hashlib.sha256(content).hexdigest() + '"'
    headers = _security_headers()
    headers["ETag"] = etag
    if extra_headers:
        headers.update(extra_headers)
    if _matches_etag(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    if len(content) > config.GZIP_MIN_SIZE and "gzip" in request.headers.get("accept-encoding", "").lower():
        content = gzip.compress(content)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(content=content, media_type="application/json", headers=headers)


def _invalid() -> HTTPException:
    return _generic_error(400, "invalid_request")


def _normalized_log_severity(value: str | None) -> str | tuple[str, str] | None:
    """Accept the UI's combined warning/error filter without widening access."""
    if value is None or not value.strip():
        return None
    normalized = value.upper()
    if normalized == "WARNINGS_ERRORS":
        return ("WARNING", "ERROR")
    if normalized not in config.LOG_SEVERITIES:
        raise _invalid()
    return normalized


def _file_not_available() -> HTTPException:
    return _generic_error(404, "file_not_available")


def _validated_file_name(value: str | None) -> str:
    if not isinstance(value, str) or not _FILE_NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise _invalid()
    return value


def _attachment_headers(file_name: str) -> dict[str, str]:
    headers = _security_headers()
    headers["Cache-Control"] = "no-store"
    headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(file_name, safe='')}"
    return headers


def _parse_iso_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise _invalid() from None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _logs_range(start_value: str | None, end_value: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    retention_start = now - timedelta(days=config.LOG_RETENTION_DAYS)
    start = _parse_iso_date(start_value) or retention_start
    end = _parse_iso_date(end_value) or now
    start = max(start, retention_start)
    end = min(end, now)
    if start > end:
        raise _invalid()
    return start, end


@router.get("/overview")
def get_overview(
    request: Request,
    window: str = config.DEFAULT_WINDOW,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    customer_device_id: str = Depends(_require_access),
):
    if window not in config.WINDOWS:
        raise _invalid()
    try:
        data = customer_stats.overview(customer_device_id, window)
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    return _json_response(data, request, if_none_match)


@router.get("/logs")
def get_logs(
    request: Request,
    severity: str | None = None,
    from_value: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    _customer_device_id: str = Depends(_require_access),
):
    normalized_severity = _normalized_log_severity(severity)
    try:
        page_limit = config.LOG_PAGE_DEFAULT if limit is None else int(limit)
    except (TypeError, ValueError):
        raise _invalid() from None
    if not 1 <= page_limit <= config.LOG_PAGE_MAX:
        raise _invalid()
    cursor_value = None
    if cursor is not None:
        cursor_value = customer_stats.decode_cursor(cursor)
        if cursor_value is None:
            raise _invalid()
    start, end = _logs_range(from_value, to)
    try:
        records, next_cursor = customer_stats.customer_logs(
            severity=normalized_severity,
            start=start,
            end=end,
            cursor=cursor_value,
            limit=page_limit,
        )
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    headers = {"X-Next-Cursor": next_cursor} if next_cursor else None
    return _json_response(records, request, if_none_match, extra_headers=headers)


@router.get("/history")
def get_history(
    request: Request,
    window: str = config.DEFAULT_WINDOW,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    customer_device_id: str = Depends(_require_access),
):
    if window not in {*config.WINDOWS, "all"}:
        raise _invalid()
    try:
        history = customer_stats.customer_history(customer_device_id, window)
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    return _json_response(history, request, if_none_match)


@router.get("/files/view")
def view_file(
    request: Request,
    name: str | None = None,
    lines: str | None = None,
    _customer_device_id: str = Depends(_require_access),
):
    file_name = _validated_file_name(name)
    try:
        line_limit = _MAX_GCODE_VIEW_LINES if lines is None else int(lines)
    except (TypeError, ValueError):
        raise _invalid() from None
    if not 1 <= line_limit <= _MAX_GCODE_VIEW_LINES:
        raise _invalid()
    try:
        preview = customer_stats.gcode_preview(file_name, line_limit)
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    if preview is None:
        raise _file_not_available()
    return _json_response(preview, request, None)


@router.get("/files/download")
def download_file(
    name: str | None = None,
    _customer_device_id: str = Depends(_require_access),
):
    file_name = _validated_file_name(name)
    try:
        file_record = customer_stats.customer_gcode_file(file_name)
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    if file_record is None:
        raise _file_not_available()
    display_name, path = file_record
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers=_attachment_headers(display_name),
    )


def _csv_stream(records: list[dict[str, str]]):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(("time_ist", "severity", "message"))
    yield buffer.getvalue()
    for record in records:
        buffer.seek(0)
        buffer.truncate(0)
        timestamp = customer_stats.parse_server_timestamp(record["time"])
        display_time = record["time"]
        if timestamp is not None:
            display_time = timestamp.astimezone(customer_stats._telemetry_timezone()).strftime(
                "%d %b %Y, %H:%M:%S IST"
            )
        writer.writerow((display_time, record["severity"], record["message"]))
        yield buffer.getvalue()


@router.get("/logs/download")
def download_logs(
    severity: str | None = None,
    from_value: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    _customer_device_id: str = Depends(_require_access),
):
    normalized_severity = _normalized_log_severity(severity)
    start, end = _logs_range(from_value, to)
    try:
        records, _next_cursor = customer_stats.customer_logs(
            severity=normalized_severity,
            start=start,
            end=end,
            cursor=None,
            limit=_MAX_LOG_DOWNLOAD_ROWS,
        )
    except customer_stats.CustomerDataError:
        raise _generic_error(503, "service_unavailable") from None
    records = records[:_MAX_LOG_DOWNLOAD_ROWS]
    filename = "logs_{}_{}.csv".format(
        start.strftime("%Y%m%dT%H%M%SZ"), end.strftime("%Y%m%dT%H%M%SZ")
    )
    return StreamingResponse(
        _csv_stream(records),
        media_type="text/csv; charset=utf-8",
        headers=_attachment_headers(filename),
    )
