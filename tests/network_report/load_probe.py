"""Short, localhost-only concurrent read soak; no production traffic."""

from __future__ import annotations

import http.cookiejar
import json
import os
import statistics
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"
DURATION_S = int(os.environ.get("NETWORK_REPORT_DURATION_S", "30"))
CLIENTS = int(os.environ.get("NETWORK_REPORT_CLIENTS", "10"))


def login_cookie() -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        BASE + "/login", data=json.dumps({"username": USER, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with opener.open(request, timeout=15):
        pass
    return "; ".join(f"{item.name}={item.value}" for item in jar)


def call(path: str, cookie: str) -> tuple[int, float, str]:
    started = time.perf_counter()
    try:
        request = urllib.request.Request(BASE + path, headers={"Cookie": cookie})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
            return response.status, (time.perf_counter() - started) * 1000, ""
    except Exception as exc:
        return getattr(exc, "code", 0), (time.perf_counter() - started) * 1000, repr(exc)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 1)


def main() -> None:
    cookie = login_cookie()
    deadline = time.monotonic() + DURATION_S
    lock = threading.Lock()
    timings: dict[str, list[float]] = defaultdict(list)
    statuses: Counter[str] = Counter()
    error_details: Counter[str] = Counter()
    requests = 0
    paths = ("/api/customer/overview", "/api/customer/logs?limit=50", "/api/customer/history?window=30d")

    def client() -> None:
        nonlocal requests
        index = 0
        while time.monotonic() < deadline:
            path = paths[index % len(paths)]
            index += 1
            status, elapsed, detail = call(path, cookie)
            with lock:
                requests += 1
                statuses[f"{path.split('?')[0]}:{status}"] += 1
                if detail:
                    error_details[detail] += 1
                timings[path.split("?")[0]].append(elapsed)
            time.sleep(0.2)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CLIENTS) as executor:
        list(executor.map(lambda _: client(), range(CLIENTS)))
    elapsed = time.perf_counter() - started
    result = {
        "clients": CLIENTS,
        "duration_s": round(elapsed, 1),
        "requests": requests,
        "errors": sum(count for name, count in statuses.items() if not name.endswith(":200")),
        "requests_per_s": round(requests / elapsed, 2),
        "statuses": dict(statuses),
        "error_details": dict(error_details),
        "latency_ms": {
            name: {"p50": percentile(values, .50), "p95": percentile(values, .95), "max": round(max(values), 1)}
            for name, values in timings.items()
        },
    }
    (ROOT / "raw" / "load_probe.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
