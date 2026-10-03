"""Small, opt-in HTTP/WebSocket authentication for RMC."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os

from fastapi import HTTPException, status


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


def auth_enabled() -> bool:
    return os.getenv("RMC_AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def _token_records() -> dict:
    """Read SHA-256(token) keyed user records from RMC_AUTH_TOKENS_JSON."""
    try:
        records = json.loads(os.getenv("RMC_AUTH_TOKENS_JSON", "{}"))
    except json.JSONDecodeError:
        return {}
    return records if isinstance(records, dict) else {}


def resolve_principal(authorization: str | None) -> Principal:
    if not auth_enabled():
        return Principal("local-dev", "operator")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for stored_digest, record in _token_records().items():
        if hmac.compare_digest(stored_digest, digest) and isinstance(record, dict):
            role = record.get("role")
            if role in {"viewer", "operator", "admin"}:
                return Principal(str(record.get("user") or "authenticated"), role)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def require_role(principal: Principal, *roles: str) -> None:
    if principal.role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
