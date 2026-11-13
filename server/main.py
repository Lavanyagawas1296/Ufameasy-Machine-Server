"""
FastAPI application entrypoint for the UFAMeasy machine server.

Creates the API application, starts the MQTT bridge during application
startup, and exposes lightweight state and health-style endpoints. This
module connects the HTTP layer to the shared in-memory state store.
"""

import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from server.state_store import state
from contextlib import asynccontextmanager
from server.mqtt_client import start_mqtt
from server.routes import router
from server.customer_routes import router as customer_router
from server.ws_manager import manager
from server.db import init_db
from server.db import insert_net_event, mark_rmc_inflight_unknown
from server.rmc_auth import require_role, resolve_principal
from server.rmc_service import rmc_service
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
    _unlock_failures,
    _unlock_failures_lock,
    failed_control_unlock_retry_after,
    get_control_session,
    require_auth,
    validate_jwt_secret,
)


logger = logging.getLogger(__name__)
_KNOWN_DEFAULTS = {"admin", "password", "changeme", "replace-with-login-username", "replace-with-login-password"}


def _validate_customer_credentials() -> None:
    username, password = os.getenv("UFAM_USER", ""), os.getenv("UFAM_PASS", "")
    if not username or not password or len(password) < 10 or username.casefold() in _KNOWN_DEFAULTS or password.casefold() in _KNOWN_DEFAULTS:
        raise RuntimeError("CUSTOMER_MODE requires non-default UFAM_USER and a UFAM_PASS of at least 10 characters")


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
    if CUSTOMER_MODE:
        _validate_customer_credentials()
        logger.info("Starting in customer mode")
    init_db()
    mark_rmc_inflight_unknown()
    start_mqtt()
    telemetry_task = asyncio.create_task(_customer_telemetry_notifications()) if CUSTOMER_MODE else None
    try:
        yield
    finally:
        if telemetry_task is not None:
            telemetry_task.cancel()
            try:
                await telemetry_task
            except asyncio.CancelledError:
                pass

CUSTOMER_MODE = os.getenv("CUSTOMER_MODE", "0").lower() in {"1", "true", "yes", "on"}
_docs_enabled = not CUSTOMER_MODE and os.getenv("UFAMEASY_DOCS") == "1"


async def _customer_telemetry_notifications() -> None:
    """Push a small signal whenever UFAMeasy appends a telemetry event."""
    from server import customer_stats

    last_event_id: int | None = None
    while True:
        try:
            latest = await asyncio.to_thread(customer_stats.latest_telemetry_event_id)
            if last_event_id is not None and latest is not None and latest > last_event_id:
                await manager.broadcast({"type": "customer_telemetry_update"})
            if latest is not None:
                last_event_id = latest
        except Exception:
            pass
        await asyncio.sleep(1)
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.mount(
    "/customer-assets",
    GZipMiddleware(StaticFiles(directory="customer_ui", html=True), minimum_size=501),
    name="customer-assets",
)
if not CUSTOMER_MODE:
    app.mount("/customer", GZipMiddleware(StaticFiles(directory="customer_ui", html=True), minimum_size=501), name="customer")

_CUSTOMER_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_PUBLIC_CUSTOMER_ASSETS = {"/customer-assets/login.css", "/customer-assets/login.js"}


def _is_customer_path(path: str) -> bool:
    return path == "/customer" or path.startswith("/customer/") or path.startswith("/customer-assets/")


def _customer_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower() if os.getenv("TRUST_PROXY_HEADERS") == "1" else ""
    return request.url.scheme == "https" or forwarded_proto == "https"


def _customer_static_headers(response: Response, request: Request) -> Response:
    path = request.url.path.lower()
    response.headers["Content-Security-Policy"] = _CUSTOMER_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if "server" in response.headers:
        del response.headers["server"]
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _customer_is_https(request):
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if path.endswith((".css", ".js")):
        response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    else:
        response.headers["Cache-Control"] = "no-cache"
    if _customer_is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.middleware("http")
async def customer_static_middleware(request: Request, call_next):
    if CUSTOMER_MODE and request.url.path in {"/customer", "/customer/"}:
        return await call_next(request)
    if not _is_customer_path(request.url.path):
        return await call_next(request)
    if request.url.path in _PUBLIC_CUSTOMER_ASSETS:
        return _customer_static_headers(await call_next(request), request)
    try:
        require_auth(request.cookies.get(AUTH_COOKIE_NAME))
    except HTTPException:
        response = _customer_static_headers(RedirectResponse("/login", status_code=303), request)
        response.headers["Cache-Control"] = "no-store"
        return response
    return _customer_static_headers(await call_next(request), request)


@app.middleware("http")
async def customer_mode_middleware(request: Request, call_next):
    if not CUSTOMER_MODE:
        return await call_next(request)
    path = request.url.path
    allowed = path in {"/", "/login", "/logout", "/health", "/customer", "/customer/"} or path.startswith("/customer-assets/") or path.startswith("/api/customer/")
    if not allowed:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if "server" in response.headers:
        del response.headers["server"]
    if path == "/login" or path.startswith("/api/customer/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = _CUSTOMER_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if _customer_is_https(request):
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _record_net_event(**event) -> None:
    """Schedule persistence so request and websocket paths never wait on SQLite."""
    asyncio.create_task(asyncio.to_thread(insert_net_event, **event))


def _record_http_diagnostics(request: Request, path: str, latency: float, status_code: int) -> None:
    event = {
        "client_ip": _client_ip(request),
        "client_id": request.headers.get("X-Client-Id"),
        "value": latency,
        "detail": f"{request.method} {path} status={status_code}",
    }
    _record_net_event(kind="request", **event)
    if latency > 1.0:
        _record_net_event(kind="slow_request", **event)
    if status_code >= 500:
        _record_net_event(kind="error_response", **event)


@app.middleware("http")
async def network_diagnostics_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path == "/ws" or path == "/diagnostics" or path.startswith("/api/diagnostics/") or path in {"/api/ping", "/api/speedtest"}:
        return await call_next(request)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _record_http_diagnostics(request, path, time.perf_counter() - started, 500)
        raise
    _record_http_diagnostics(request, path, time.perf_counter() - started, response.status_code)
    return response

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
    return FileResponse("customer_ui/index.html" if CUSTOMER_MODE else "ui/index.html")


@app.get("/health")
def customer_health():
    return {"status": "ok"}


@app.get("/customer", include_in_schema=False)
@app.get("/customer/", include_in_schema=False)
def customer_redirect():
    return RedirectResponse("/" if CUSTOMER_MODE else "/customer/", status_code=301)


@app.get("/diagnostics")
def diagnostics_page(request: Request, authorization: str | None = Header(default=None)):
    # Same as "/": cookie session first (browser -> /login), then the RMC role check.
    # authorization must be a Header; a bare resolve_principal dependency made it a
    # required query parameter (422).
    try:
        require_auth(request.cookies.get(AUTH_COOKIE_NAME))
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    require_role(resolve_principal(authorization), "operator", "admin")
    return FileResponse("ui/network.html")


@app.get("/api/ping")
def ping():
    """Return a lightweight, unauthenticated network timing probe."""
    return {"t": int(time.time() * 1000)}


@app.get("/api/speedtest", dependencies=[Depends(require_auth)])
def speedtest(kb: int = Query(default=256, ge=1)):
    """Return a bounded random payload for browser throughput measurements."""
    return Response(
        content=os.urandom(min(kb, 512) * 1024),
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/login")
def login_page():
    return FileResponse("customer_ui/login.html" if CUSTOMER_MODE else "ui/login.html")


_LOGIN_LIMIT_KEY = "\0login"  # cannot collide with a real username


@app.post("/login")
def login(credentials: LoginRequest, request: Request):
    client_ip = _client_ip(request)
    username_key = credentials.username or "\0empty"
    with _unlock_failures_lock:
        attempts = _unlock_failures.get((_LOGIN_LIMIT_KEY, client_ip))
        user_attempts = _unlock_failures.get((username_key, client_ip))
        now = time.monotonic()
        if ((attempts and len(attempts) >= 5 and now - attempts[0] < 600) or (user_attempts and len(user_attempts) >= 5 and now - user_attempts[0] < 600)):
            raise HTTPException(
                status_code=429,
                detail="Invalid username or password",
                headers={"Retry-After": "600"},
            )
    if not credentials_match(credentials.username, credentials.password):
        failed_control_unlock_retry_after(_LOGIN_LIMIT_KEY, client_ip, window_seconds=600)
        retry_after = failed_control_unlock_retry_after(username_key, client_ip, window_seconds=600)
        logger.warning("Failed login attempt for username=%r ip=%s", credentials.username, client_ip)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Invalid username or password",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=401, detail="Invalid username or password")
    clear_failed_control_unlocks(_LOGIN_LIMIT_KEY, client_ip)
    clear_failed_control_unlocks(username_key, client_ip)
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=create_access_token(credentials.username),
        max_age=TOKEN_LIFETIME_HOURS * 60 * 60,
        httponly=True,
        samesite="strict" if CUSTOMER_MODE else "lax",
        secure=_customer_is_https(request) or cookie_secure(),
        path="/",
    )
    return response


@app.post("/logout")
def logout(
    request: Request,
    username: str = Depends(require_auth),
    client_id: str | None = Header(default=None, alias="X-Client-Id"),
):
    if client_id:
        rmc_service.release_locks_for_owner(username, client_id)
    response = Response(status_code=204)
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=_customer_is_https(request) or cookie_secure(),
        path="/",
    )
    if CUSTOMER_MODE:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(key=AUTH_COOKIE_NAME, httponly=True, samesite="strict", secure=_customer_is_https(request) or cookie_secure(), path="/")
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
    connected_at = time.monotonic()
    _record_net_event(
        kind="ws_connect",
        client_ip=websocket.client.host if websocket.client else "unknown",
        client_id=websocket.headers.get("X-Client-Id"),
    )
    await asyncio.sleep(0.1)
    try:
        sessions_data = {}
        live_runtime = {}
        active_items = list(_active_sessions.items())
        # SQLite lookups run in the default executor (as other blocking routes
        # do); each call opens its own connection inside the worker thread.
        loop = asyncio.get_running_loop()
        session_rows = await loop.run_in_executor(
            None, lambda: [get_session_by_id(sid) for _, sid in active_items]
        )
        for (device_id, session_id), row in zip(active_items, session_rows):
            row = row or {}
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
        _record_net_event(
            kind="ws_disconnect",
            client_ip=websocket.client.host if websocket.client else "unknown",
            client_id=websocket.headers.get("X-Client-Id"),
            value=time.monotonic() - connected_at,
        )

if not CUSTOMER_MODE:
    app.include_router(router)
app.include_router(customer_router)
