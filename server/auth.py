"""Single-user cookie authentication for the browser dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import deque
import hmac
import logging
import os
import threading
import time

import jwt
from fastapi import Cookie, HTTPException, status
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from pydantic import BaseModel


AUTH_COOKIE_NAME = "ufam_auth"
CONTROL_COOKIE_NAME = "ufam_control"
TOKEN_LIFETIME_HOURS = 8
logger = logging.getLogger(__name__)
_unlock_failures: dict[tuple[str, str], deque[float]] = {}
_unlock_failures_lock = threading.Lock()


class LoginRequest(BaseModel):
    username: str
    password: str


class ControlUnlockRequest(BaseModel):
    password: str


def jwt_secret() -> str:
    return os.getenv("UFAM_JWT_SECRET", "")


def validate_jwt_secret() -> None:
    if not jwt_secret():
        raise RuntimeError("UFAM_JWT_SECRET must be set before starting the server")


def cookie_secure() -> bool:
    return os.getenv("UFAM_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}


def credentials_match(username: str, password: str) -> bool:
    expected_user = os.getenv("UFAM_USER", "")
    expected_password = os.getenv("UFAM_PASS", "")
    user_matches = hmac.compare_digest(username, expected_user)
    password_matches = hmac.compare_digest(password, expected_password)
    return bool(expected_user and expected_password) and user_matches and password_matches


def create_access_token(username: str) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=TOKEN_LIFETIME_HOURS)
    return jwt.encode({"sub": username, "exp": expires_at}, jwt_secret(), algorithm="HS256")


def _control_idle_seconds() -> int:
    return max(1, int(os.getenv("UFAM_CONTROL_IDLE_MINUTES", "45"))) * 60


def _control_absolute_seconds() -> int:
    return max(1, int(os.getenv("UFAM_CONTROL_ABSOLUTE_HOURS", "12"))) * 60 * 60


def _control_locked() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "CONTROL_LOCKED", "message": "Control unlock required"},
    )


def create_control_token(username: str, absolute_expires_at: int | None = None) -> tuple[str, int]:
    now = int(time.time())
    absolute_expires_at = absolute_expires_at or now + _control_absolute_seconds()
    expires_at = min(now + _control_idle_seconds(), absolute_expires_at)
    token = jwt.encode(
        {"sub": username, "kind": "control", "exp": expires_at, "abs_exp": absolute_expires_at},
        jwt_secret(),
        algorithm="HS256",
    )
    return token, expires_at


def get_control_session(control_token: str | None, username: str) -> dict | None:
    if not control_token:
        return None
    try:
        payload = jwt.decode(control_token, jwt_secret(), algorithms=["HS256"])
        absolute_expires_at = int(payload.get("abs_exp", 0))
        if (
            payload.get("kind") != "control"
            or not hmac.compare_digest(str(payload.get("sub", "")), username)
            or absolute_expires_at <= int(time.time())
        ):
            return None
        return {
            "username": username,
            "absolute_expires_at": absolute_expires_at,
            "expires_at": int(payload["exp"]),
        }
    except (ExpiredSignatureError, InvalidTokenError, TypeError, ValueError):
        return None


def require_control(control_token: str | None, username: str) -> dict:
    session = get_control_session(control_token, username)
    if not session:
        raise _control_locked()
    return session


def refresh_control_cookie(response, session: dict) -> int:
    token, expires_at = create_control_token(session["username"], session["absolute_expires_at"])
    response.set_cookie(
        key=CONTROL_COOKIE_NAME,
        value=token,
        max_age=max(0, expires_at - int(time.time())),
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )
    return expires_at


def control_password_matches(password: str) -> bool:
    expected_password = os.getenv("UFAM_CONTROL_PASS", "")
    password_matches = hmac.compare_digest(password, expected_password)
    return bool(expected_password) and password_matches


def failed_control_unlock_retry_after(username: str, client_ip: str) -> int | None:
    now = time.monotonic()
    key = (username, client_ip)
    with _unlock_failures_lock:
        attempts = _unlock_failures.setdefault(key, deque())
        while attempts and now - attempts[0] >= 300:
            attempts.popleft()
        if len(attempts) >= 5:
            return max(1, int(300 - (now - attempts[0])) + 1)
        attempts.append(now)
    return None


def clear_failed_control_unlocks(username: str, client_ip: str) -> None:
    with _unlock_failures_lock:
        _unlock_failures.pop((username, client_ip), None)


def require_auth(ufam_auth: str | None = Cookie(default=None)) -> str:
    if not ufam_auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        payload = jwt.decode(ufam_auth, jwt_secret(), algorithms=["HS256"])
        username = payload.get("sub")
        if not isinstance(username, str) or not username:
            raise InvalidTokenError("Missing subject")
        return username
    except (ExpiredSignatureError, InvalidTokenError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session") from None
