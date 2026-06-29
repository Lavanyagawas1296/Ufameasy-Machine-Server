"""
Additional FastAPI routes for machine state inspection.

Defines HTTP endpoints that read from the shared state store without owning
MQTT or persistence concerns. These routes form the API-facing side of the
MQTT -> StateStore -> API data flow.
"""

from fastapi import APIRouter
from server.state_store import state
from server.db import get_sessions_by_device, get_runtime_latest
import sqlite3, os

router = APIRouter()

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
    print(f"GET snapshots "f"id(state)={id(state)} "f"id(snapshot_store)={id(state.slice_snapshots)} "f"keys={list(state.slice_snapshots.keys())}")
    return state.get_all_snapshots()

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "params.db")

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
    return get_runtime_latest(session_id)