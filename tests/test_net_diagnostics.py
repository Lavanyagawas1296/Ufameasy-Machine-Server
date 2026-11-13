from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from server import db, mqtt_client, routes
from server.auth import AUTH_COOKIE_NAME, create_access_token
from server.main import app, network_diagnostics_middleware, speedtest
from server.rmc_auth import Principal


def _request(path: str = "/") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.8", 1234), "root_path": "",
    })


async def _endpoint_status(path: str, method: str = "GET", body: bytes = b"") -> int:
    parsed = urlsplit(path)
    messages = [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app({
        "type": "http", "http_version": "1.1", "method": method, "path": parsed.path,
        "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(), "scheme": "http",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 80), "client": ("10.0.0.8", 1234), "root_path": "",
    }, receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


async def _get(path: str, cookie: str | None = None) -> tuple[int, dict, bytes]:
    """Raw ASGI GET returning (status, headers, body); httpx/TestClient is not installed."""
    parsed = urlsplit(path)
    messages = [{"type": "http.request", "body": b"", "more_body": False}, {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    headers = [(b"cookie", f"{AUTH_COOKIE_NAME}={cookie}".encode())] if cookie else []
    await app({
        "type": "http", "http_version": "1.1", "method": "GET", "path": parsed.path,
        "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(), "scheme": "http",
        "headers": headers, "server": ("testserver", 80), "client": ("10.0.0.8", 1234), "root_path": "",
    }, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode().lower(): v.decode() for k, v in start["headers"]}, body


class NetDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "net.sqlite"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db
        self.temp.cleanup()

    def test_event_insert_and_startup_prune(self):
        db.insert_net_event("request", client_ip="10.0.0.1", value=0.1)
        db.insert_net_event("request", ts=(datetime.now(timezone.utc) - timedelta(days=15)).isoformat())
        db.init_db()
        events = db.query_net_events((datetime.now(timezone.utc) - timedelta(days=20)).isoformat())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["client_ip"], "10.0.0.1")

    def test_slow_request_middleware_records_slow_event(self):
        events = []
        async def call_next(_request):
            return Response(status_code=200)
        with patch("server.main._record_net_event", side_effect=lambda **event: events.append(event)), \
             patch("server.main.time.perf_counter", side_effect=[1.0, 2.1]):
            asyncio.run(network_diagnostics_middleware(_request("/slow"), call_next))
        self.assertIn("request", [event["kind"] for event in events])
        self.assertIn("slow_request", [event["kind"] for event in events])

    def test_probe_endpoints_are_not_request_logged(self):
        events = []
        async def call_next(_request):
            return Response(status_code=200)
        with patch("server.main._record_net_event", side_effect=lambda **event: events.append(event)):
            asyncio.run(network_diagnostics_middleware(_request("/api/ping"), call_next))
            asyncio.run(network_diagnostics_middleware(_request("/api/speedtest"), call_next))
        self.assertEqual(events, [])

    def test_rmc_state_gap_is_recorded(self):
        mqtt_client._last_rmc_state_at.clear()
        events = []
        msg = type("Message", (), {
            "topic": "ufameasy/machine-a/rmc/state",
            "payload": json.dumps({"seq": 1}).encode(),
        })()
        with patch("server.mqtt_client.time.monotonic", side_effect=[1.0, 22.5]), \
             patch("server.mqtt_client.insert_net_event", side_effect=lambda *args, **kwargs: events.append((args, kwargs))), \
             patch("server.mqtt_client.state.update_rmc_state", return_value={}), \
             patch("server.mqtt_client.register_device"), \
             patch("server.mqtt_client._broadcast"):
            mqtt_client.on_message(None, None, msg)
            mqtt_client.on_message(None, None, msg)
        self.assertEqual(events[0][0][0], "state_gap")
        self.assertEqual(events[0][1]["device_id"], "machine-a")

    def test_client_events_reject_bad_kind(self):
        with self.assertRaises(HTTPException) as context:
            asyncio.run(routes.save_client_network_events(
                _request(), [{"kind": "not_allowed"}], None, Principal("test", "operator"),
            ))
        self.assertEqual(context.exception.status_code, 400)

    def test_client_events_accept_rtt_and_speed_and_reject_oversize_payloads(self):
        rtt = {"kind": "rtt", "value": 24.5, "detail": json.dumps({
            "min": 20.0, "max": 30.0, "jitter": 3.0, "loss_pct": 0.0, "n": 4,
        })}
        result = asyncio.run(routes.save_client_network_events(
            _request(), [rtt, {"kind": "speed", "value": 12.5, "detail": ""}], None, Principal("test", "operator"),
        ))
        self.assertEqual(result, {"accepted": 2})
        with self.assertRaises(HTTPException) as context:
            asyncio.run(routes.save_client_network_events(
                _request(), [{"kind": "rtt", "value": 1, "detail": "x" * 501}], None, Principal("test", "operator"),
            ))
        self.assertEqual(context.exception.status_code, 400)
        with self.assertRaises(HTTPException) as context:
            asyncio.run(routes.save_client_network_events(
                _request(), [{"kind": "speed", "value": None}], None, Principal("test", "operator"),
            ))
        self.assertEqual(context.exception.status_code, 400)

    def test_network_status_thresholds_and_units(self):
        event_time = datetime.now(timezone.utc).isoformat()
        def rtt(ip, value, loss=0):
            return {"kind": "rtt", "client_ip": ip, "value": value, "ts": event_time,
                    "detail": json.dumps({"min": value, "max": value, "jitter": 2, "loss_pct": loss, "n": 1})}
        summary = routes._network_summary([
            {"kind": "request", "client_ip": "ok", "value": 0.123, "ts": event_time}, rtt("ok", 80),
            {"kind": "request", "client_ip": "warn", "value": 0.4, "ts": event_time}, rtt("warn", 101),
            rtt("bad", 301), {"kind": "ws_drop", "client_ip": "bad", "ts": event_time},
            {"kind": "request", "client_ip": "none", "value": 0.1, "ts": event_time},
        ])
        rows = {row["client_ip"]: row for row in summary["clients"]}
        self.assertEqual(rows["ok"]["status"], "ok")
        self.assertEqual(rows["warn"]["status"], "warn")
        self.assertEqual(rows["bad"]["status"], "bad")
        self.assertEqual(rows["none"]["status"], "no data")
        self.assertEqual(rows["ok"]["p50_latency_ms"], 123.0)
        self.assertEqual(rows["ok"]["last_seen"], event_time)

    def test_ping_and_speedtest_limits_and_auth(self):
        self.assertEqual(asyncio.run(_endpoint_status("/api/ping")), 200)
        self.assertEqual(asyncio.run(_endpoint_status("/api/speedtest?kb=256")), 401)
        response = speedtest(1024)
        self.assertEqual(len(response.body), 512 * 1024)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_diagnostic_endpoints_require_auth(self):
        self.assertEqual(asyncio.run(_endpoint_status("/api/diagnostics/network")), 401)
        self.assertEqual(asyncio.run(_endpoint_status("/api/diagnostics/network?hours=2")), 401)
        self.assertEqual(asyncio.run(_endpoint_status("/api/diagnostics/client-events", "POST", b"[]")), 401)


    def test_diagnostics_page_logged_in_operator_gets_html(self):
        status, headers, body = asyncio.run(_get("/diagnostics", create_access_token("operator1")))
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertEqual(body, Path("ui/network.html").read_bytes())

    def test_diagnostics_page_unauthenticated_redirects_to_login_never_422(self):
        for cookie in (None, "not-a-jwt"):
            status, headers, _ = asyncio.run(_get("/diagnostics", cookie))
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "/login")

    def test_diagnostics_apis_require_auth_and_serve_operator(self):
        token = create_access_token("operator1")
        for path in ("/api/diagnostics/network?hours=1", "/api/diagnostics/network.csv?hours=1"):
            self.assertEqual(asyncio.run(_get(path))[0], 401, path)
            status, headers, _ = asyncio.run(_get(path, token))
            self.assertEqual(status, 200, path)
            expected = "text/csv" if ".csv" in path else "application/json"
            self.assertIn(expected, headers["content-type"], path)


if __name__ == "__main__":
    unittest.main()
