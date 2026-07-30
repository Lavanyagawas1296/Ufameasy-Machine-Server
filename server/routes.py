"""
Additional FastAPI routes for machine state inspection.

Defines HTTP endpoints that read from the shared state store without owning
MQTT or persistence concerns. These routes form the API-facing side of the
MQTT -> StateStore -> API data flow.
"""

import asyncio
import csv
import ftplib
import io
import json
import os
import sqlite3
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from server.state_store import state
from server.db import get_sessions_by_device, get_runtime_latest

router = APIRouter()

REMOTE_LOG_PATH = "ufameasy_sys.U5LOG"


class LogFetchError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _download_log_bytes(ip: str, port: int) -> bytes:
    buffer = io.BytesIO()
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(ip, port, timeout=10)
            ftp.login()
            ftp.retrbinary(f"RETR {REMOTE_LOG_PATH}", buffer.write)
    except ftplib.all_errors as exc:
        raise LogFetchError(502, f"Unable to fetch log file from FTP server: {exc}") from exc

    data = buffer.getvalue()
    if not data:
        raise LogFetchError(404, "Fetched log file is empty")
    return data


def _telemetry_db_bytes_to_csv(db_bytes: bytes) -> bytes:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".U5LOG", delete=False) as temp_file:
            temp_file.write(db_bytes)
            temp_path = temp_file.name

        with sqlite3.connect(temp_path) as conn:
            cursor = conn.execute(
                """
                SELECT * FROM telemetry_events
                WHERE id >= (
                    SELECT id FROM telemetry_events
                    WHERE event_type = 'machine_connect'
                    ORDER BY id DESC LIMIT 1
                )
                ORDER BY id
                """
            )
            headers = [column[0] for column in cursor.description or []]
            rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        detail = "telemetry_events table not found" if "no such table" in str(exc).lower() else str(exc)
        raise LogFetchError(422, detail) from exc
    except sqlite3.DatabaseError as exc:
        raise LogFetchError(422, f"Fetched log file is not a readable SQLite database: {exc}") from exc
    except OSError as exc:
        raise LogFetchError(500, f"Unable to process fetched log file: {exc}") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _fetch_telemetry_events_csv(ip: str, port: int) -> bytes:
    return _telemetry_db_bytes_to_csv(_download_log_bytes(ip, port))


def _safe_log_filename(ip: str) -> str:
    safe_ip = "".join(char if char.isalnum() or char in ".-_" else "_" for char in ip)
    return f"ufameasy_logs_{safe_ip or 'machine'}.csv"

@router.get("/state")
def get_state():
    """
    Return all known machine parameters.

    Returns:
        Shared dictionary containing the latest parameter values.
    """
    return state.parameters

@router.get("/parameter/{name}")
def get_parameter(name: str):
    """
    Return the latest value for a single machine parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    return {
        "name": name,
        "value": state.parameters.get(name)
    }

@router.get("/ufameasy/parameter/{name}")
def get_ufameasy_parameter(name: str):
    """
    Return the latest value for a single ufameasy parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    return {
        "name": name,
        "value": state.parameters.get(name)
    }

@router.get("/health")
def health():
    """
    Return an API health probe response.

    Returns:
        Dictionary indicating that the route layer is responsive.
    """
    return {"status": "ok"}

@router.get("/snapshots")
def get_snapshots():
    return state.get_all_snapshots()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DATA_DIR, "params.db")

@router.get("/devices")
def get_devices():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM devices").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@router.get("/devices/{device_id}/sessions")
def get_device_sessions(device_id: str):
    return get_sessions_by_device(device_id)

@router.get("/sessions/{session_id}/runtime")
def get_session_runtime(session_id: str):
    if not session_id or session_id.strip() == "":
        return {"error": "invalid session_id"}
    return get_runtime_latest(session_id)

@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    if not session_id or session_id.strip() == "":
        return {"error": "invalid session_id"}
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM slice_data WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM runtime_log WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    return {"deleted": session_id}

@router.get("/logs/fetch")
async def fetch_logs(ip: str, port: int = 2121):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="port must be between 1 and 65535")

    loop = asyncio.get_running_loop()
    try:
        csv_data = await loop.run_in_executor(None, _fetch_telemetry_events_csv, ip, port)
    except LogFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return StreamingResponse(
        io.BytesIO(csv_data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_safe_log_filename(ip)}"'},
    )

@router.get("/sessions")
def list_sessions():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    if not session_id or session_id.strip() == "":
        return {"error": "invalid session_id"}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else {"error": "not found"}

@router.get("/sessions/{session_id}/replay")
def get_session_replay(session_id: str):
    if not session_id or session_id.strip() == "":
        return {"error": "invalid session_id"}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        runtime = conn.execute("SELECT * FROM runtime_log WHERE session_id=? ORDER BY recorded_at", (session_id,)).fetchall()
        slices = conn.execute("SELECT * FROM slice_data WHERE session_id=? ORDER BY slice_index", (session_id,)).fetchall()
        conn.close()

        # Parse params_json from slices
        parsed_slices = []
        for s in slices:
            row_dict = dict(s)
            if row_dict.get('params_json'):
                try:
                    row_dict['params_json'] = json.loads(row_dict['params_json'])
                except:
                    pass
            parsed_slices.append(row_dict)

        return {
            "runtime": [dict(r) for r in runtime],
            "slices": parsed_slices
        }
    except Exception as e:
        return {"error": str(e)}, 500
