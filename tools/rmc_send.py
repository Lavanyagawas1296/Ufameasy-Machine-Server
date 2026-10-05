"""Send an RMC command through the server, or directly to MQTT for bench tests."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.rmc_contract import COMMAND_SOURCE, rmc_topic, CMD_SEGMENT  # noqa: E402


def load_params(value: str | None) -> dict:
    if not value:
        return {}
    path = Path(value)
    if path.is_file():
        return {
            "source": "ftp", "ftp_name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size,
        }
    return {"source": "ftp", "ftp_name": value, "sha256": "manual-required", "size": 0}


def build_load_params(args) -> dict:
    if getattr(args, "server_file", None):
        return {"source": "server", "file_id": args.server_file}
    if getattr(args, "local_file", None):
        return {"source": "local", "file_id": args.local_file}
    if getattr(args, "file", None):
        return load_params(args.file)
    return {}


def build_command(args) -> dict:
    now = datetime.now(timezone.utc)
    params = build_load_params(args) if args.action == "load" else {}
    return {
        "cmd_id": str(uuid.uuid4()), "action": args.action, "params": params,
        "issued_by": args.user, "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=args.ttl)).isoformat(), "source": COMMAND_SOURCE,
    }


def upload_file(args, file_path: str):
    path = Path(file_path)
    if not path.is_file():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    boundary = uuid.uuid4().hex
    content = path.read_bytes()
    filename = path.name

    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(content)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    url = f"{args.server.rstrip('/')}/api/rmc/files"
    request = urllib.request.Request(
        url,
        data=bytes(body),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if args.token:
        request.add_header("Authorization", f"Bearer {args.token}")
    try:
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        print(json.dumps(result, indent=2))
        return result
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8")
        print(f"Upload failed ({exc.code}): {err}", file=sys.stderr)
        sys.exit(1)


def via_http(args, command):
    payload = {"action": command["action"], "params": command["params"], "ttl": args.ttl}
    request = urllib.request.Request(
        f"{args.server.rstrip('/')}/api/devices/{args.device_id}/commands",
        data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"},
    )
    if args.token:
        request.add_header("Authorization", f"Bearer {args.token}")
    with urllib.request.urlopen(request) as response:
        result = json.load(response)
    print(json.dumps(result, indent=2))
    cmd_id = result["cmd_id"]
    while True:
        request = urllib.request.Request(f"{args.server.rstrip('/')}/api/devices/{args.device_id}/commands/{cmd_id}")
        if args.token:
            request.add_header("Authorization", f"Bearer {args.token}")
        with urllib.request.urlopen(request) as response:
            current = json.load(response)
        print(current["status"], current.get("reason_code") or "")
        if current["status"] in {"done", "rejected", "expired", "failed", "timeout", "unknown"}:
            break
        time.sleep(1)


def via_mqtt(args, command):
    import paho.mqtt.publish as publish
    raw = dict(command)
    if raw["action"] == "load" and "ftp_name" in raw.get("params", {}):
        raw["params"] = {**raw["params"], "path": raw["params"].pop("ftp_name")}
    publish.single(rmc_topic(args.device_id, CMD_SEGMENT), json.dumps(raw), hostname=args.mqtt_host, port=args.mqtt_port, qos=1, retain=False)
    print(json.dumps(raw, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("device_id", nargs="?", default=None)
    parser.add_argument("action", nargs="?", choices=("load", "start", "pause", "resume", "stop"), default=None)
    parser.add_argument("--upload", help="Upload a local G-code file to the server registry")
    parser.add_argument("--server-file", help="Server file_id for load action")
    parser.add_argument("--local-file", help="Local file_id on machine for load action")
    parser.add_argument("--file", help="Legacy file path for ftp load")
    parser.add_argument("--user", default="cli")
    parser.add_argument("--ttl", type=int, default=60)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--token")
    parser.add_argument("--raw-mqtt", action="store_true")
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    args = parser.parse_args()

    if args.upload:
        upload_file(args, args.upload)
        return

    if not args.device_id or not args.action:
        parser.error("device_id and action are required when not using --upload")

    command = build_command(args)
    via_mqtt(args, command) if args.raw_mqtt else via_http(args, command)


if __name__ == "__main__":
    main()
