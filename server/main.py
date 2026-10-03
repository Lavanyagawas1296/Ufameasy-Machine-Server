"""
FastAPI application entrypoint for the UFAMeasy machine server.

Creates the API application, starts the MQTT bridge during application
startup, and exposes lightweight state and health-style endpoints. This
module connects the HTTP layer to the shared in-memory state store.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from server.state_store import state
from contextlib import asynccontextmanager
from server.mqtt_client import start_mqtt
from server.routes import router
from server.ws_manager import manager
from server.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start background services for the FastAPI application lifecycle.

    Args:
        app: FastAPI application instance receiving the lifecycle hook.

    Yields:
        None while the application is running.

    Side Effects:
        Starts the MQTT network loop in a background thread.
    """
    # MQTT must start before requests are served so API reads see live updates.
    init_db()
    start_mqtt()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/debug")
def debug(device_id: str):
    """
    Seed and return a sample state value for manual debugging.

    Returns:
        Current parameter dictionary after inserting the debug value.

    Side Effects:
        Updates the shared state store with a sample LASER_POWER value.
    """
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    state.update_parameter(device_id, "LASER_POWER", 60)
    return state.get_parameters(device_id)

@app.get("/")
def root():
    """
    Return the machine parameter UI.

    Returns:
        HTML file response for the browser UI.
    """
    return FileResponse("ui/index.html")

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/state")
def get_state(device_id: str):
    """
    Return the current machine parameter snapshot.

    Returns:
        Shared parameter dictionary maintained by the state store.
    """
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return state.get_parameters(device_id)

@app.get("/snapshots")
def get_snapshots(device_id: str):
    if not device_id or device_id.strip() == "":
        return {"error": "invalid device_id"}
    return state.get_all_snapshots(device_id)

@app.get("/events")
def get_events():
    return state.events

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    from server.mqtt_client import _active_sessions
    from server.db import get_session_by_id
    import json, asyncio
    await manager.connect(websocket)
    await asyncio.sleep(0.1)
    try:
        sessions_data = {}
        live_runtime = {}
        for device_id, session_id in _active_sessions.items():
            row = get_session_by_id(session_id) or {}
            # Overlay the live current_file from state (updated by file_update MQTT)
            live_file = state.get_parameters(device_id).get("current_file")
            if live_file and live_file.lower() != "unknown":
                row = dict(row)
                row["file_name"] = live_file
            sessions_data[device_id] = row
            live_runtime[device_id] = state.get_parameters(device_id)

        all_known_devices = set(
            list(_active_sessions.keys()) + list(state.parameters.keys()) + list(state.rmc_states.keys())
        )
        rmc_states = {did: state.get_rmc_state(did) for did in all_known_devices}
        rmc_files = {did: state.get_rmc_files(did) for did in all_known_devices}

        await websocket.send_text(json.dumps({
            "type": "init",
            "active_sessions": _active_sessions,
            "sessions_data": sessions_data,
            "live_runtime": live_runtime,
            "rmc_states": rmc_states,
            "rmc_files": rmc_files,
        }))
    except Exception:
        pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)

app.include_router(router)
