"""Isolated CUSTOMER_MODE server for the network report only."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SANDBOX = ROOT / "sandbox"
PROJECT_ROOT = ROOT.parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--timeout-keep-alive", type=int, default=5)
    args = parser.parse_args()
    sys.path.insert(0, str(PROJECT_ROOT))

    os.environ.update(
        {
            "CUSTOMER_MODE": "1",
            "UFAM_CUSTOMER_DEVICE_ID": "device_001",
            "UFAM_USER": "network-test",
            "UFAM_PASS": "NetworkTest-Only-2026!",
            "UFAM_JWT_SECRET": "network-report-test-secret-2026-not-production",
            "RMC_AUTH_ENABLED": "false",
            "UFAM_LEGACY_GCODE_ROOTS": "",
        }
    )
    from server import customer_config, db, routes

    db.DB_PATH = SANDBOX / "params.db"
    db.DATA_DIR = SANDBOX / "data"
    db.RMC_FILES_DIR = db.DATA_DIR / "rmc_files"
    customer_config.TELEMETRY_DB_PATH = SANDBOX / "telemetry.u5d"
    routes.ODOMETER_PATH = SANDBOX / "odometer.json"

    import server.main as application

    application.start_mqtt = lambda: None
    import uvicorn

    uvicorn.run(
        application.app,
        host=args.host,
        port=args.port,
        workers=1,
        timeout_keep_alive=args.timeout_keep_alive,
        log_level="info",
    )


if __name__ == "__main__":
    main()
