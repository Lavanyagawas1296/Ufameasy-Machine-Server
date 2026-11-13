"""Content-addressed storage for completed G-code uploads."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import tempfile

from server import db

logger = logging.getLogger(__name__)
GCODE_RETENTION_DAYS = 90  # Unreferenced retention policy; deletion is not implemented.
HASH_CHUNK_SIZE = 1024 * 1024


def ingest_gcode_file(
    source_path: Path,
    file_name: str,
    *,
    device_id: str | None = None,
    claimed_sha256: str | None = None,
) -> str:
    """Hash a completed upload, store it once, and upsert its metadata."""
    gcode_dir = db.DATA_DIR / "gcode"
    gcode_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".gcode_", suffix=".tmp", dir=gcode_dir)
    temp_path = Path(temp_name)
    hasher = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as target:
            with source_path.open("rb") as source:
                while chunk := source.read(HASH_CHUNK_SIZE):
                    hasher.update(chunk)
                    target.write(chunk)
                    size += len(chunk)
            target.flush()
            os.fsync(target.fileno())

        sha256 = hasher.hexdigest()
        if claimed_sha256 and claimed_sha256.lower() != sha256:
            logger.warning(
                "G-code upload hash mismatch for %s: client=%s server=%s; using server hash",
                file_name,
                claimed_sha256,
                sha256,
            )

        target_path = gcode_dir / f"{sha256}.nc"
        if target_path.exists():
            temp_path.unlink()
        else:
            try:
                os.rename(temp_path, target_path)
            except FileExistsError:
                # Another upload stored these bytes while this one was hashing.
                temp_path.unlink(missing_ok=True)

        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO gcode_files (sha256, file_name, size, first_seen, last_seen, device_id)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(sha256) DO UPDATE SET last_seen = excluded.last_seen""",
                (sha256, Path(file_name).name, size, now, now, device_id),
            )
            conn.commit()
        finally:
            conn.close()
        return sha256
    finally:
        temp_path.unlink(missing_ok=True)
