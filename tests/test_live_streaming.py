import asyncio
import json
import os
import unittest
from unittest.mock import patch

from server import mqtt_client
from server.media_gateway import stream_for
from server.state_store import StateStore
from server.ws_manager import ConnectionManager


class FakeWebSocket:
    def __init__(self, block=False):
        self.accepted = False
        self.sent = []
        self.block = block
        self.release = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        if self.block:
            await self.release.wait()
        self.sent.append(message)


class LiveStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_viewer_cannot_block_a_fast_viewer(self):
        manager = ConnectionManager()
        slow, fast = FakeWebSocket(block=True), FakeWebSocket()
        await manager.connect(slow)
        await manager.connect(fast)
        await manager.activate(slow)
        await manager.activate(fast)

        await asyncio.wait_for(manager.broadcast({"type": "runtime_update"}), 0.1)
        await asyncio.sleep(0)
        self.assertEqual(len(fast.sent), 1)
        self.assertEqual(len(slow.sent), 0)

        await manager.disconnect(slow)
        await manager.disconnect(fast)

    async def test_updates_queued_during_init_follow_the_init_message(self):
        manager = ConnectionManager()
        ws = FakeWebSocket()
        await manager.connect(ws)
        await manager.broadcast({"type": "runtime_update", "data": {"v": 1}})
        await ws.send_text(json.dumps({"type": "init"}))
        await manager.activate(ws)
        await asyncio.sleep(0)
        self.assertEqual([json.loads(item)["type"] for item in ws.sent], ["init", "runtime_update"])
        await manager.disconnect(ws)


class MqttAndStateTests(unittest.TestCase):
    def setUp(self):
        with mqtt_client._sessions_lock:
            mqtt_client._active_sessions.clear()
        with mqtt_client._runtime_persist_lock:
            mqtt_client._last_runtime_persisted.clear()

    def test_late_end_does_not_remove_the_newer_session(self):
        mqtt_client._set_active_session("machine-a", "new-session")
        message = type("Message", (), {
            "topic": "ufameasy/machine-a/session/end",
            "payload": b'{"session_id":"old-session","status":"ended"}',
        })()
        with patch.object(mqtt_client, "close_session") as close_session:
            mqtt_client.on_message(None, None, message)
        close_session.assert_called_once_with("old-session", "ended")
        self.assertEqual(mqtt_client.active_sessions_snapshot(), {"machine-a": "new-session"})

    def test_bad_mqtt_payload_does_not_escape_the_callback(self):
        message = type("Message", (), {
            "topic": "ufameasy/machine-a/runtime",
            "payload": b"{not json",
        })()
        mqtt_client.on_message(None, None, message)
        self.assertEqual(mqtt_client.active_sessions_snapshot(), {})

    def test_non_numeric_job_progress_is_safe(self):
        state = StateStore()
        result = state.update_job_state("machine-a", {"total_lines": "bad", "current_line": "bad"})
        self.assertEqual(result["percentage"], 0.0)

    def test_high_rate_runtime_is_persisted_at_a_bounded_rate(self):
        message = type("Message", (), {
            "topic": "ufameasy/machine-a/runtime",
            "payload": b'{"session_id":"session-a","current_layer":3}',
        })()
        with patch.object(mqtt_client, "insert_runtime_log") as persist, \
             patch.object(mqtt_client.time, "monotonic", side_effect=(1.0, 1.1)):
            mqtt_client.on_message(None, None, message)
            mqtt_client.on_message(None, None, message)
        self.assertEqual(persist.call_count, 1)


class MediaConfigurationTests(unittest.TestCase):
    def test_stream_url_is_built_from_only_valid_device_config(self):
        config = json.dumps({"machine-a": {
            "playback_url": "https://gateway.example/",
            "path": "/machines/machine-a/main/",
        }})
        with patch.dict(os.environ, {"UFAMEASY_MEDIA_DEVICES_JSON": config}, clear=False):
            stream = stream_for("machine-a")
        self.assertEqual(stream.webrtc_url, "https://gateway.example/machines/machine-a/main")
        with self.assertRaises(ValueError):
            stream_for("../../not-a-device")


if __name__ == "__main__":
    unittest.main()
