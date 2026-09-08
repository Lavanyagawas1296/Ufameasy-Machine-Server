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
import mimetypes
import os
import posixpath
import queue
import socket
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from fastapi.responses import FileResponse

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from server.state_store import state
from server.db import get_sessions_by_device, get_runtime_latest

router = APIRouter()

REMOTE_LOG_PATH = "ufameasy_sys.csv"
FTP_PORT = 2121
RECORDINGS_ROOT = "recordings"
UFAMEASY_HOME = Path.home() / ".ufameasy"
LOCAL_RECORDINGS_ROOT = UFAMEASY_HOME / "recordings"
LOCAL_TELEMETRY_DIR = UFAMEASY_HOME / "telemetry"
LOCAL_TELEMETRY_DB_NAMES = ("ufameasy_sys.u5d", "ufameasy_sys.U5LOG", "ufameasy_logs.db")


class LogFetchError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RecordingFetchError(Exception):
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


def _local_telemetry_db_path() -> Path | None:
    for filename in LOCAL_TELEMETRY_DB_NAMES:
        path = LOCAL_TELEMETRY_DIR / filename
        if path.is_file():
            return path
    return None


def _read_telemetry_rows(db_path: Path) -> list[dict]:
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM telemetry_events ORDER BY id DESC").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        detail = "telemetry_events table not found" if "no such table" in str(exc).lower() else str(exc)
        raise LogFetchError(422, detail) from exc
    except sqlite3.DatabaseError as exc:
        raise LogFetchError(422, f"Local telemetry database is not readable: {exc}") from exc
    except OSError as exc:
        raise LogFetchError(500, f"Unable to read local telemetry database: {exc}") from exc


def _rows_to_csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _local_log_rows() -> list[dict] | None:
    db_path = _local_telemetry_db_path()
    return _read_telemetry_rows(db_path) if db_path else None


def _local_log_csv_bytes() -> bytes | None:
    rows = _local_log_rows()
    if rows is None:
        return None
    data = _rows_to_csv_bytes(rows)
    if not data:
        raise LogFetchError(404, "Local telemetry log is empty")
    return data


def _telemetry_db_bytes_to_csv(db_bytes: bytes) -> bytes:
    temp_path = None
    conn = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".U5LOG", delete=False) as temp_file:
            temp_file.write(db_bytes)
            temp_path = temp_file.name

        conn = sqlite3.connect(temp_path)
        cursor = conn.execute(
            """
            SELECT * FROM telemetry_events
            WHERE id >= (
                SELECT id FROM telemetry_events
                WHERE event_type IN ('machine_connect', 'MachineConnectionEvent')
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
        if conn:
            conn.close()
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
    local_bytes = _local_log_csv_bytes()
    if local_bytes is not None:
        return local_bytes
    return _download_log_bytes(ip, port)


def _fetch_log_csv(ip: str) -> list[dict]:
    local_rows = _local_log_rows()
    if local_rows is not None:
        return local_rows

    try:
        with _ftp_connect(ip) as ftp:
            buf = io.BytesIO()
            ftp.retrbinary("RETR ufameasy_sys.csv", buf.write)
            buf.seek(0)
            reader = csv.DictReader(io.TextIOWrapper(buf, encoding="utf-8"))
            return [row for row in reader]
    except ftplib.error_perm as exc:
        raise RecordingFetchError(404, f"Log file not found: {exc}") from exc
    except ftplib.all_errors as exc:
        raise RecordingFetchError(502, f"FTP error fetching logs: {exc}") from exc


def _safe_log_filename(ip: str) -> str:
    safe_ip = "".join(char if char.isalnum() or char in ".-_" else "_" for char in ip)
    return f"ufameasy_logs_{safe_ip or 'machine'}.csv"


def _ftp_connect(ip: str, port: int = FTP_PORT) -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(ip, port, timeout=10)
    ftp.login()
    return ftp


def _safe_device_folder(device_id: str) -> str:
    device_id = device_id.strip()
    if not device_id:
        raise RecordingFetchError(400, "device_id is required when provided")
    if device_id in (".", "..") or "/" in device_id or "\\" in device_id:
        raise RecordingFetchError(400, "device_id must be a single folder name")
    return device_id


def _local_recording_path(remote_path: str) -> Path:
    return LOCAL_RECORDINGS_ROOT / Path(*remote_path.split("/")[1:])


def _safe_recording_file_path(file_path: str) -> str:
    cleaned = file_path.strip().replace("\\", "/")
    if cleaned.startswith(f"{RECORDINGS_ROOT}/"):
        cleaned = cleaned[len(RECORDINGS_ROOT) + 1:]
    normalized = posixpath.normpath(cleaned)
    if (
        not normalized
        or normalized in (".", "..")
        or normalized.startswith("/")
        or normalized.startswith("../")
    ):
        raise RecordingFetchError(400, "file must be a relative recording path")
    return posixpath.join(RECORDINGS_ROOT, normalized)


def _ftp_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").isoformat() + "Z"
    except ValueError:
        return value


def _recording_metadata(ftp: ftplib.FTP, device_id: str, filename: str, facts: dict | None = None) -> dict:
    facts = facts or {}
    remote_path = f"{RECORDINGS_ROOT}/{device_id}/{filename}"
    size = facts.get("size")
    modified_at = facts.get("modify")

    if size is None:
        try:
            size = ftp.size(remote_path)
        except ftplib.all_errors:
            size = None
    if modified_at is None:
        try:
            modified_at = ftp.sendcmd(f"MDTM {remote_path}").split(maxsplit=1)[1]
        except (IndexError, ftplib.all_errors):
            modified_at = None

    return {
        "device_id": device_id,
        "filename": filename,
        "path": f"{device_id}/{filename}",
        "size": int(size) if size not in (None, "") else None,
        "modified_at": _ftp_timestamp(modified_at),
    }


def _local_recording_metadata(device_id: str, path: Path) -> dict:
    stat = path.stat()
    return {
        "device_id": device_id,
        "filename": path.name,
        "path": f"{device_id}/{path.name}",
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _list_local_device_recordings(device_id: str) -> list[dict] | None:
    folder = LOCAL_RECORDINGS_ROOT / _safe_device_folder(device_id)
    if not folder.exists():
        return None
    if not folder.is_dir():
        raise RecordingFetchError(404, f"Recordings path is not a folder for device_id '{device_id}'")
    return [
        _local_recording_metadata(device_id, path)
        for path in sorted(folder.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
        if path.is_file()
    ]


def _list_local_recordings(device_id: str | None = None) -> list[dict] | None:
    if not LOCAL_RECORDINGS_ROOT.exists():
        return None
    if device_id:
        return _list_local_device_recordings(device_id)

    recordings = []
    for folder in sorted(LOCAL_RECORDINGS_ROOT.iterdir()):
        if folder.is_dir():
            recordings.extend(_list_local_device_recordings(folder.name) or [])
    return recordings


def _list_device_recordings(ftp: ftplib.FTP, device_id: str) -> list[dict]:
    base_path = f"{RECORDINGS_ROOT}/{device_id}"
    recordings = []
    try:
        for name, facts in ftp.mlsd(base_path):
            if facts.get("type") != "file":
                continue
            recordings.append(_recording_metadata(ftp, device_id, name, facts))
        return recordings
    except ftplib.all_errors:
        pass

    try:
        names = ftp.nlst(base_path)
    except ftplib.error_perm as exc:
        raise RecordingFetchError(404, f"No recordings found for device_id '{device_id}': {exc}") from exc
    except ftplib.all_errors as exc:
        raise RecordingFetchError(502, f"Unable to list recordings: {exc}") from exc

    for entry in names:
        filename = posixpath.basename(entry.rstrip("/"))
        if filename:
            recordings.append(_recording_metadata(ftp, device_id, filename))
    return recordings


def _list_recording_device_folders(ftp: ftplib.FTP) -> list[str]:
    try:
        return [
            name
            for name, facts in ftp.mlsd(RECORDINGS_ROOT)
            if facts.get("type") == "dir" and name not in (".", "..")
        ]
    except ftplib.all_errors:
        pass

    try:
        original_dir = ftp.pwd()
        ftp.cwd(RECORDINGS_ROOT)
        names = ftp.nlst()
    except ftplib.error_perm as exc:
        raise RecordingFetchError(404, f"Recordings folder not found: {exc}") from exc
    except ftplib.all_errors as exc:
        raise RecordingFetchError(502, f"Unable to list recording folders: {exc}") from exc

    folders = []
    try:
        for entry in names:
            name = posixpath.basename(entry.rstrip("/"))
            if not name or name in (".", ".."):
                continue
            try:
                ftp.cwd(name)
                folders.append(name)
                ftp.cwd("..")
            except ftplib.all_errors:
                continue
    finally:
        try:
            ftp.cwd(original_dir)
        except ftplib.all_errors:
            pass
    return folders


def _list_recordings(ip: str, device_id: str | None = None) -> list[dict]:
    local_recordings = _list_local_recordings(device_id)
    if local_recordings is not None:
        return local_recordings

    try:
        with _ftp_connect(ip) as ftp:
            if device_id:
                return _list_device_recordings(ftp, _safe_device_folder(device_id))
            recordings = []
            for folder in _list_recording_device_folders(ftp):
                recordings.extend(_list_device_recordings(ftp, folder))
            return recordings
    except RecordingFetchError:
        raise
    except ftplib.all_errors as exc:
        raise RecordingFetchError(502, f"Unable to connect to recordings FTP server: {exc}") from exc


def _assert_recording_exists(ip: str, remote_path: str) -> None:
    try:
        with _ftp_connect(ip) as ftp:
            directory = posixpath.dirname(remote_path)
            filename = posixpath.basename(remote_path)
            try:
                ftp.size(remote_path)
                return
            except ftplib.all_errors:
                pass
            try:
                for name, facts in ftp.mlsd(directory):
                    if name == filename and facts.get("type") == "file":
                        return
            except ftplib.all_errors:
                pass
            try:
                names = ftp.nlst(directory)
            except ftplib.all_errors:
                names = []
            if filename in [posixpath.basename(name.rstrip("/")) for name in names]:
                return
            raise ftplib.error_perm("550 recording not found")
    except ftplib.error_perm as exc:
        raise RecordingFetchError(404, f"Recording not found: {exc}") from exc
    except ftplib.all_errors as exc:
        raise RecordingFetchError(502, f"Unable to access recording: {exc}") from exc


def _delete_recording_ftp(ip: str, remote_path: str) -> None:
    try:
        with _ftp_connect(ip) as ftp:
            print(f"[DELETE] attempting ftp.delete({remote_path!r})")
            ftp.delete(remote_path)
    except ftplib.error_perm as exc:
        print(f"[DELETE] perm error: {exc}")
        raise RecordingFetchError(404, f"Recording not found or already deleted: {exc}") from exc
    except ftplib.all_errors as exc:
        print(f"[DELETE] ftp error: {exc}")
        raise RecordingFetchError(502, f"FTP error during delete: {exc}") from exc


def _stream_recording(ip: str, remote_path: str):
    chunks: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=8)

    def fetch() -> None:
        try:
            with _ftp_connect(ip) as ftp:
                ftp.retrbinary(f"RETR {remote_path}", chunks.put)
        except Exception as exc:
            chunks.put(exc)
        finally:
            chunks.put(None)

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()

    while True:
        chunk = chunks.get()
        if chunk is None:
            break
        if isinstance(chunk, Exception):
            raise chunk
        yield chunk


def _safe_download_filename(remote_path: str) -> str:
    filename = posixpath.basename(remote_path)
    return "".join(char if char.isalnum() or char in ".-_" else "_" for char in filename) or "recording"

@router.get("/state")
def get_state(device_id: str):
    """
    Return all known machine parameters.

    Returns:
        Shared dictionary containing the latest parameter values.
    """
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return state.get_parameters(device_id)

@router.get("/parameter/{name}")
def get_parameter(name: str, device_id: str):
    """
    Return the latest value for a single machine parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return {
        "name": name,
        "device_id": device_id,
        "value": state.get_parameter(device_id, name)
    }

@router.get("/ufameasy/parameter/{name}")
def get_ufameasy_parameter(name: str, device_id: str):
    """
    Return the latest value for a single ufameasy parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return {
        "name": name,
        "device_id": device_id,
        "value": state.get_parameter(device_id, name)
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
def get_snapshots(device_id: str):
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return state.get_all_snapshots(device_id)

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
        csv_bytes = await loop.run_in_executor(None, _fetch_telemetry_events_csv, ip, port)
    except LogFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ufameasy_logs_{ip}.csv"},
    )

@router.get("/logs/view")
async def view_logs(ip: str):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(None, _fetch_log_csv, ip)
    except (LogFetchError, RecordingFetchError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"logs": rows}

@router.get("/camera/check")
async def check_camera(ip: str, port: int = 8765):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")
    if port < 1 or port > 65535:
      raise HTTPException(status_code=400, detail="port must be between 1 and 65535")

    loop = asyncio.get_running_loop()

    def _can_connect() -> bool:
        try:
            with socket.create_connection((ip, port), timeout=0.75):
                return True
        except OSError:
            return False

    available = await loop.run_in_executor(None, _can_connect)
    return {"available": available, "ip": ip, "port": port}

@router.get("/recordings")
async def get_recordings(ip: str, device_id: str | None = None):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _list_recordings, ip, device_id)
    except RecordingFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

@router.get("/recordings/download")
async def download_recording(ip: str, file: str, download: bool = False):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")

    try:
        remote_path = _safe_recording_file_path(file)
    except RecordingFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    filename = _safe_download_filename(remote_path)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    disposition = "attachment" if download else "inline"
    local_path = _local_recording_path(remote_path)
    if local_path.is_file():
        return FileResponse(
            local_path,
            media_type=media_type,
            headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
        )

    try:
        _assert_recording_exists(ip, remote_path)
    except RecordingFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return StreamingResponse(
        _stream_recording(ip, remote_path),
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )

@router.delete("/recordings/{filename:path}")
async def delete_recording(filename: str, ip: str):
    ip = ip.strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip is required")
    try:
        remote_path = _safe_recording_file_path(filename)
    except RecordingFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _delete_recording_ftp, ip, remote_path)
    except RecordingFetchError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"deleted": filename}

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
        session = conn.execute("SELECT device_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        runtime = conn.execute("SELECT * FROM runtime_log WHERE session_id=? ORDER BY recorded_at", (session_id,)).fetchall()
        slices = conn.execute("SELECT * FROM slice_data WHERE session_id=? ORDER BY slice_index", (session_id,)).fetchall()
        conn.close()
        device_id = session["device_id"] if session else None

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

        runtime_list = [dict(r) for r in runtime]

        # Fallback: serve in-memory state if runtime_log is empty.
        live_parameters = state.get_parameters(device_id) if device_id else {}
        if not runtime_list and live_parameters:
            runtime_list = [live_parameters]
        live_snapshots = state.get_all_snapshots(device_id) if device_id else {}

        return {
            "runtime": runtime_list,
            "slices": parsed_slices if parsed_slices else list(live_snapshots.values())
        }
    except Exception as e:
        return {"error": str(e)}, 500
