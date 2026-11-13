"""Derive a compact, non-sensitive evidence index for REPORT.md."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent


def load(name: str):
    return json.loads((ROOT / "raw" / name).read_text(encoding="utf-8"))


def main() -> None:
    hosts: set[str] = set()
    har_entries = 0
    for path in (ROOT / "har").glob("*.har"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("log", {}).get("entries", [])
        har_entries += len(entries)
        hosts.update(urlparse(item["request"]["url"]).netloc for item in entries)
    result = {
        "har_entries": har_entries,
        "har_hosts": sorted(hosts),
        "http": load("http_probe.json"),
        "files": load("file_probe.json"),
        "focused_ui": load("focused_ui_probe.json"),
        "offline": load("offline_probe.json"),
        "restart": load("restart_probe.json"),
        "load_1_client": load("load_probe_single.json"),
        "load_10_clients": load("load_probe_10_clients.json"),
        "slowloris": load("slowloris_probe.json"),
    }
    (ROOT / "raw" / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"har_entries": har_entries, "har_hosts": sorted(hosts)}, indent=2))


if __name__ == "__main__":
    main()
