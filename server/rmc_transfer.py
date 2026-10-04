"""Server-side file registry and transfer helpers for RMC Phase 6A."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
import uuid

from fastapi import HTTPException, UploadFile

from server import db
from server.rmc_auth import auth_enabled
from server.rmc_contract import (
    ALLOWED_EXTENSIONS,
    MAX_JOB_FILE_MB,
    RMC_TRANSFER_SECRET,
)

_PROCESS_FALLBACK_SECRET = secrets.token_hex(32)


def get_free_disk_bytes(path: Path) -> int:
    """Return available free bytes on the filesystem hosting path."""
    return shutil.disk_usage(path).free


def get_transfer_secret() -> str:
    """Read RMC_TRANSFER_SECRET from environment or contract default."""
    return os.getenv("RMC_TRANSFER_SECRET", RMC_TRANSFER_SECRET)


def is_transfer_ready() -> tuple[bool, str]:
    """Check if the file transfer feature is properly configured."""
    secret = get_transfer_secret()
    if auth_enabled() and not secret:
        return False, "RMC_TRANSFER_SECRET must be set when RMC authentication is enabled"
    return True, ""


def require_transfer_ready() -> None:
    """Raise 503 if transfer feature cannot start due to missing configuration."""
    ready, reason = is_transfer_ready()
    if not ready:
        raise HTTPException(
            status_code=503,
            detail={"reason_code": "TRANSFER_DENIED", "message": reason},
        )


def sign_download_token(file_id: str, device_id: str, expiry: int) -> str:
    """Generate an HMAC-signed token bound to file_id, device_id and expiry."""
    require_transfer_ready()
    secret = get_transfer_secret()
    if not secret:
        # If auth is disabled in dev mode, fallback to an internal secret generated once per process
        secret = _PROCESS_FALLBACK_SECRET
    payload = f"{file_id}:{device_id}:{expiry}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    raw = f"{device_id}:{expiry}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_download_token(
    file_id: str, token: str, device_id: str | None = None
) -> tuple[bool, str]:
    """
    Verify HMAC signature, expiration time, and device binding.

    Returns:
        (True, bound_device_id) on success.
        (False, error_reason) on failure.
    """
    secret = get_transfer_secret()
    if not secret:
        if auth_enabled():
            return False, "RMC_TRANSFER_SECRET must be set when auth is enabled"
        secret = _PROCESS_FALLBACK_SECRET

    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 3:
            return False, "Malformed token structure"
        tok_device_id, tok_expiry_str, sig = parts
        expiry = int(tok_expiry_str)
    except Exception:
        return False, "Invalid token encoding"

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if now_ts > expiry:
        return False, "Download token has expired"

    if device_id and device_id != tok_device_id:
        return (
            False,
            f"Device binding mismatch (token issued for {tok_device_id}, request was {device_id})",
        )

    expected_payload = f"{file_id}:{tok_device_id}:{expiry}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), expected_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return False, "Token signature verification failed"

    return True, tok_device_id


async def save_uploaded_file(file: UploadFile, uploader: str) -> dict:
    """
    Stream upload to a temp file, enforce size limit and extensions, verify free
    space, compute SHA-256, reject duplicates, and atomically move to registry storage.
    """
    require_transfer_ready()
    original_name = file.filename or ""
    sanitized_name = Path(original_name).name.strip()
    if not sanitized_name or sanitized_name in {".", ".."}:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": "UNSUPPORTED_TYPE", "message": "Invalid or empty filename"},
        )

    ext = Path(sanitized_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason_code": "UNSUPPORTED_TYPE",
                "message": f"Unsupported file extension '{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
            },
        )

    db.RMC_FILES_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = db.RMC_FILES_DIR / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    max_mb = int(os.getenv("MAX_JOB_FILE_MB", str(MAX_JOB_FILE_MB)))
    max_bytes = max_mb * 1024 * 1024
    hasher = hashlib.sha256()
    total_bytes = 0

    fd, temp_path_raw = tempfile.mkstemp(dir=str(temp_dir), prefix="upload_")
    temp_path = Path(temp_path_raw)

    try:
        with open(fd, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "reason_code": "FILE_TOO_LARGE",
                            "message": f"File exceeds maximum allowed size of {max_mb} MB",
                        },
                    )
                hasher.update(chunk)
                out.write(chunk)

        free_bytes = get_free_disk_bytes(db.RMC_FILES_DIR)
        if free_bytes < total_bytes:
            raise HTTPException(
                status_code=507,
                detail={"reason_code": "DISK_FULL", "message": "Insufficient server disk space"},
            )

        sha256 = hasher.hexdigest()

        existing = db.get_rmc_server_file_by_sha256(sha256)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "DUPLICATE",
                    "existing_file_id": existing["file_id"],
                    "message": f"Identical file already exists with file_id: {existing['file_id']}",
                },
            )

        file_id = str(uuid.uuid4())
        stored_name = str(uuid.uuid4())
        target_path = db.RMC_FILES_DIR / stored_name

        shutil.move(str(temp_path), str(target_path))

        uploaded_at = datetime.now(timezone.utc).isoformat()
        try:
            return db.create_rmc_server_file(
                file_id=file_id,
                display_name=sanitized_name,
                size=total_bytes,
                sha256=sha256,
                uploaded_by=uploader,
                uploaded_at=uploaded_at,
                stored_name=stored_name,
            )
        except sqlite3.IntegrityError:
            target_path.unlink(missing_ok=True)
            existing = db.get_rmc_server_file_by_sha256(sha256)
            existing_id = existing["file_id"] if existing else file_id
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "DUPLICATE",
                    "existing_file_id": existing_id,
                    "message": f"Identical file already exists with file_id: {existing_id}",
                },
            )

    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
