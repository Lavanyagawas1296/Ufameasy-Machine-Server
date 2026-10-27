from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from server import db, mqtt_client, routes
from server.main import app, network_diagnostics_middleware
from server.rmc_auth import Principal


def _request(path: str = "/") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.8", 1234), "root_path": "",
    })


async def _endpoint_status(path: str, method: str = "GET", body: bytes = b"") -> int:
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
        "type": "http", "http_version": "1.1", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "scheme": "http",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 80), "client": ("10.0.0.8", 1234), "root_path": "",
    }, receive, send)
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


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

    def test_diagnostic_endpoints_require_auth(self):
        self.assertEqual(asyncio.run(_endpoint_status("/api/diagnostics/network")), 401)
        self.assertEqual(asyncio.run(_endpoint_status("/api/diagnostics/client-events", "POST", b"[]")), 401)


if __name__ == "__main__":
    unittest.main()
