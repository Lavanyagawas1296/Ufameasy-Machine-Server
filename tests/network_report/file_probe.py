"""Exercise customer file endpoints using an isolated G-code fixture only."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
SANDBOX = ROOT / "sandbox"
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"


def call(opener, path: str, *, method: str = "GET", body=None, headers=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    merged = {"Content-Type": "application/json"} if data else {}
    merged.update(headers or {})
    response = opener.open(urllib.request.Request(BASE + path, data=data, method=method, headers=merged), timeout=15)
    content = response.read()
    return {"status": response.status, "headers": dict(response.headers), "body": content}


def main() -> None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    call(opener, "/login", method="POST", body={"username": USER, "password": PASSWORD})
    before = json.loads(call(opener, "/api/customer/history?window=30d")["body"])
    candidate = next((item["file_name"] for item in before.get("files", []) if item.get("file_name")), None)
    if not candidate:
        raise RuntimeError("No telemetry-reported history file available for the isolated test")

    sys.path.insert(0, str(PROJECT))
    from server import db, gcode_ingest

    db.DB_PATH = SANDBOX / "params.db"
    db.DATA_DIR = SANDBOX / "data"
    db.RMC_FILES_DIR = db.DATA_DIR / "rmc_files"
    db.init_db()
    fixture = max((PROJECT / "server" / "data" / "gcode").glob("*.nc"), key=lambda item: item.stat().st_size)
    gcode_ingest.ingest_gcode_file(fixture, candidate, device_id="device_001")

    encoded = urllib.parse.quote(candidate, safe="")
    after = json.loads(call(opener, "/api/customer/history?window=30d")["body"])
    selected = next(item for item in after["files"] if item.get("file_name") == candidate)
    view = call(opener, f"/api/customer/files/view?name={encoded}&lines=20")
    download = call(opener, f"/api/customer/files/download?name={encoded}")
    ranged = call(opener, f"/api/customer/files/download?name={encoded}", headers={"Range": "bytes=0-127"})
    csv = call(opener, "/api/customer/logs/download?from=2026-10-01T00%3A00%3A00%2B05%3A30&to=2026-11-11T23%3A59%3A00%2B05%3A30")
    report = {
        "history_available": selected.get("available"),
        "history_display_name": selected.get("display_name"),
        "preview": {"status": view["status"], "line_count": len(json.loads(view["body"]).get("lines", []))},
        "download": {
            "status": download["status"],
            "bytes": len(download["body"]),
            "sha256": hashlib.sha256(download["body"]).hexdigest(),
            "content_disposition": download["headers"].get("content-disposition"),
        },
        "range": {"status": ranged["status"], "bytes": len(ranged["body"])},
        "logs_csv": {
            "status": csv["status"],
            "header": csv["body"].decode("utf-8-sig", "replace").splitlines()[0] if csv["body"] else "",
            "rows": max(0, len(csv["body"].splitlines()) - 1),
        },
    }
    (ROOT / "raw" / "file_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
