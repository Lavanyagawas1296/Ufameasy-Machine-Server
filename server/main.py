"""
FastAPI application entrypoint for the UFAMeasy machine server.

Creates the API application, starts the MQTT bridge during application
startup, and exposes lightweight state and health-style endpoints. This
module connects the HTTP layer to the shared in-memory state store.
"""

import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from server.state_store import state
from contextlib import asynccontextmanager
from server.mqtt_client import start_mqtt
from server.routes import router
from server.ws_manager import manager
from server.db import init_db
from server.db import mark_rmc_inflight_unknown
from server.auth import (
    AUTH_COOKIE_NAME,
    CONTROL_COOKIE_NAME,
    TOKEN_LIFETIME_HOURS,
    ControlUnlockRequest,
    LoginRequest,
    clear_failed_control_unlocks,
    cookie_secure,
    control_password_matches,
    create_access_token,
    create_control_token,
    credentials_match,
    failed_control_unlock_retry_after,
    get_control_session,
    require_auth,
    validate_jwt_secret,
)


logger = logging.getLogger(__name__)


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
    try:
        validate_jwt_secret()
    except RuntimeError as exc:
        raise RuntimeError("UFAM_JWT_SECRET must be set in .env or the environment") from exc
    init_db()
    mark_rmc_inflight_unknown()
    start_mqtt()
    yield

_docs_enabled = os.getenv("UFAMEASY_DOCS") == "1"
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root(request: Request):
    """
    Return the machine parameter UI.

    Returns:
        HTML file response for the browser UI.
    """
    try:
        require_auth(request.cookies.get(AUTH_COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    return FileResponse("ui/index.html")


@app.get("/login")
def login_page():
    return FileResponse("ui/login.html")


@app.post("/login")
def login(credentials: LoginRequest):
    if not credentials_match(credentials.username, credentials.password):
        logger.warning("Failed login attempt for username=%r", credentials.username)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=create_access_token(credentials.username),
        max_age=TOKEN_LIFETIME_HOURS * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )
    return response


@app.post("/logout")
def logout():
    response = Response(status_code=204)
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )
    return response


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.post("/unlock-control")
def unlock_control(
    credentials: ControlUnlockRequest,
    request: Request,
    username: str = Depends(require_auth),
):
    client_ip = _client_ip(request)
    if not control_password_matches(credentials.password):
        retry_after = failed_control_unlock_retry_after(username, client_ip)
        logger.warning("Failed control unlock for username=%r ip=%s", username, client_ip)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many failed control unlock attempts",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=403, detail="Invalid control password")
    clear_failed_control_unlocks(username, client_ip)
    token, expires_at = create_control_token(username)
    response = JSONResponse({"unlocked": True, "expires_at": expires_at})
    response.set_cookie(
        key=CONTROL_COOKIE_NAME,
        value=token,
        max_age=max(0, expires_at - int(time.time())),
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )
    return response


@app.post("/lock-control")
def lock_control(username: str = Depends(require_auth)):
    response = Response(status_code=204)
    response.delete_cookie(
        key=CONTROL_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )
    return response


@app.get("/control-status")
def control_status(request: Request, username: str = Depends(require_auth)):
    session = get_control_session(request.cookies.get(CONTROL_COOKIE_NAME), username)
    if not session:
        return {"unlocked": False}
    return {"unlocked": True, "expires_at": session["expires_at"]}

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)

@app.get("/events", dependencies=[Depends(require_auth)])
def get_events():
    return state.events


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    allowed_origins = {
        item.strip()
        for item in os.getenv("UFAMEASY_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    return bool(origin and (urlparse(origin).netloc == host or origin in allowed_origins))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    from server.mqtt_client import _active_sessions
    from server.db import get_session_by_id
    import json, asyncio
    try:
        require_auth(websocket.cookies.get(AUTH_COOKIE_NAME))
    except HTTPException:
        await websocket.close(code=1008)
        return
    if not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008)
        return
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
