"""Create a read-only-source, throwaway telemetry snapshot for tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "sandbox"
SOURCE = Path.home() / ".ufameasy" / "telemetry" / "ufameasy_sys.u5d"


def main() -> None:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    target = SANDBOX / "telemetry.u5d"
    if target.exists():
        target.unlink()
    source_connection = sqlite3.connect(f"{SOURCE.resolve().as_uri()}?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    (SANDBOX / "odometer.json").write_text('{"machine_seconds": 0}', encoding="utf-8")
    print(f"telemetry_copy_bytes={target.stat().st_size}")


if __name__ == "__main__":
    main()
