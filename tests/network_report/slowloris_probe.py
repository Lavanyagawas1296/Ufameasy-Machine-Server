"""One partial-header connection against the disposable localhost server."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    sock = socket.create_connection(("127.0.0.1", 18000), timeout=3)
    try:
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:18000\r\nX-Test: partial")
        time.sleep(7)
        sock.settimeout(.5)
        try:
            data = sock.recv(1)
            outcome = "closed" if data == b"" else "responded"
        except TimeoutError:
            outcome = "still_open"
    finally:
        sock.close()
    result = {"partial_header_wait_s": 7, "outcome": outcome}
    (ROOT / "raw" / "slowloris_probe.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
