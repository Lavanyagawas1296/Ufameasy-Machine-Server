"""
MQTT client bridge for UFAMeasy machine telemetry.

Owns the Paho MQTT client, subscribes to machine topics, converts incoming
parameter payloads, and updates the shared state store. This module is the
ingest side of the MQTT -> StateStore -> API architecture.
"""
import asyncio
import json
import threading
import paho.mqtt.client as mqtt

from server.db import (
    close_session,
    create_session,
    insert_runtime_log,
    insert_slice_data,
    register_device,
    update_session_total_layers,
)
from server.state_store import state
from server.ws_manager import manager


mqtt_event_loop = None
_active_sessions = {}


def _log_broadcast_error(future):
    """
    Log exceptions raised by background WebSocket broadcasts.
    """
    try:
        future.result()
    except Exception as exc:
        print(f"[WS] Broadcast failed: {exc}")


def _broadcast(message):
    if mqtt_event_loop is not None:
        future = asyncio.run_coroutine_threadsafe(
            manager.broadcast(message),
            mqtt_event_loop,
        )
        future.add_done_callback(_log_broadcast_error)


def on_connect(client, userdata, flags, reason_code, properties=None):
    """
    Subscribe to UFAMeasy topics after the MQTT broker connection succeeds.

    Args:
        client: Paho MQTT client invoking the callback.
        userdata: User data configured on the MQTT client, if any.
        flags: Broker-provided connection flags.
        reason_code: MQTT v5 connection result code.
        properties: Optional MQTT v5 properties supplied by the broker.

    Returns:
        None.

    Side Effects:
        Prints connection status and registers a wildcard topic subscription.
    """
    print("Connected to MQTT broker")
    # One wildcard subscription keeps routing centralized in on_message.
    # client.subscribe("ufameasy/#")
    client.subscribe("ufameasy/#")


def on_message(client, userdata, msg):
    """
    Handle incoming MQTT messages and update machine state when applicable.

    Args:
        client: Paho MQTT client invoking the callback.
        userdata: User data configured on the MQTT client, if any.
        msg: MQTT message containing topic and payload bytes.

    Returns:
        None.

    Side Effects:
        Decodes payloads, updates the shared state store for parameter topics,
        and writes diagnostic output to stdout.

    Raises:
        UnicodeDecodeError: If the payload cannot be decoded as UTF-8.
    """
    payload = msg.payload.decode()

    if msg.topic == "ufameasy/session/reset":
        with state.lock:
            state.slice_snapshots.clear()
        state.events.clear()
        if mqtt_event_loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                manager.broadcast({"type": "session_reset"}),
                mqtt_event_loop,
            )
            future.add_done_callback(_log_broadcast_error)
        print("[SESSION] Reset — cleared all snapshots")
        return

    topic_parts = msg.topic.split("/")

    if len(topic_parts) >= 3 and topic_parts[0] == "ufameasy":
        device_id = topic_parts[1]

        if topic_parts[2:] == ["file", "update"]:
            data = json.loads(payload)
            file_name = data.get("file_name")

            _broadcast({
                "type": "file_update",
                "device_id": device_id,
                "file_name": file_name,
            })
            return

        if topic_parts[2:] == ["estimate", "request"]:
            data = json.loads(payload)

            def _run():
                import sys

                workbench_path = r"D:\UFAMeasy_Workspace\UFAMeasy\Mod\RadialInfillWorkbench"
                if workbench_path not in sys.path:
                    sys.path.insert(0, workbench_path)

                from core.job_estimator import estimate_gcode_file, format_duration

                pre = data.get("estimate_result")
                if pre and not pre.get("error"):
                    r = pre
                else:
                    r = estimate_gcode_file(data["merged_gcode_path"])
                layer_count = 0
                try:
                    with open(data["merged_gcode_path"], "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.strip().upper().startswith("%LAYER_START"):
                                layer_count += 1
                except Exception as e:
                    print(f"[ESTIMATE] layer count error: {e}")
                session_id = _active_sessions.get(device_id)
                if session_id:
                    try:
                        update_session_total_layers(session_id, layer_count)
                    except Exception as e:
                        print(f"[DB] update_session_total_layers failed: {e}")
                    _broadcast({
                        "type": "runtime_update",
                        "device_id": device_id,
                        "session_id": session_id,
                        "data": {"total_layers": layer_count},
                    })
                _broadcast({"type": "estimate_result", "data": {
                    "total_time_fmt": format_duration(r["total_time_s"]),
                    "total_powder_g": r["total_powder_g"],
                    "deposition_efficiency": r["kpis"].get("deposition_efficiency", 0),
                    "total_energy_wh": r["total_energy_wh"],
                    "layer_count": layer_count,
                }})

            threading.Thread(target=_run, daemon=True).start()
            return

        if topic_parts[2:] == ["session", "start"]:
            data = json.loads(payload)
            session_id = data.get("session_id")
            file_name = data.get("file_name")
            total_layers = data.get("total_layers")

            try:
                register_device(device_id, device_id)
                create_session(session_id, device_id, file_name, total_layers)
            except Exception as exc:
                print(f"[DB] session/start failed: {exc}")
            _active_sessions[device_id] = session_id

            _broadcast({
                "type": "session_start",
                "device_id": device_id,
                "session_id": session_id,
                "file_name": file_name,
                "total_layers": total_layers,
            })
            return

        if topic_parts[2:] == ["session", "end"]:
            data = json.loads(payload)
            session_id = data.get("session_id")
            status = data.get("status")

            try:
                close_session(session_id, status)
            except Exception as exc:
                print(f"[DB] session/end failed: {exc}")
            _active_sessions.pop(device_id, None)

            _broadcast({
                "type": "session_end",
                "device_id": device_id,
                "session_id": session_id,
                "status": status,
            })
            return

        if topic_parts[2:] == ["runtime"]:
            data = json.loads(payload)
            session_id = data.get("session_id")

            try:
                insert_runtime_log(session_id, data)
            except Exception as exc:
                print(f"[DB] runtime failed: {exc}")

            _broadcast({
                "type": "runtime_update",
                "device_id": device_id,
                "session_id": session_id,
                "data": data,
            })
            return

        if topic_parts[2:] == ["runtime", "position"]:
            data = json.loads(payload)
            _broadcast({
                "type": "position_update",
                "device_id": device_id,
                "data": data,
            })
            return

        if topic_parts[2:] == ["log"]:
            import csv as _csv, os as _os
            data = json.loads(payload)
            log_path = _os.path.join(_os.path.dirname(__file__), "data", f"logs_{device_id}.csv")
            write_header = not _os.path.exists(log_path)
            with open(log_path, "a", newline="", encoding="utf-8") as f:
                writer = _csv.writer(f)
                if write_header:
                    writer.writerow(["Timestamp", "Category", "Source", "Severity", "Event Type", "Details"])
                writer.writerow([
                    data.get("timestamp", ""),
                    data.get("category", ""),
                    data.get("source", ""),
                    data.get("severity", ""),
                    data.get("event_type", ""),
                    data.get("details", ""),
                ])
            return

        if len(topic_parts) == 4 and topic_parts[2] == "slice":
            data = json.loads(payload)
            slice_idx = topic_parts[3]
            session_id = _active_sessions.get(device_id)

            if session_id is None:
                auto_session_id = f"auto-{device_id}"
                try:
                    register_device(device_id, device_id)
                    create_session(auto_session_id, device_id, "unknown", 0)
                except Exception as exc:
                    print(f"[DB] auto-session create failed: {exc}")
                _active_sessions[device_id] = auto_session_id
                session_id = auto_session_id
                _broadcast({
                    "type": "session_start",
                    "device_id": device_id,
                    "session_id": session_id,
                    "file_name": "unknown",
                    "total_layers": 0,
                })
                print(f"[SESSION] Auto-created session for device_id={device_id}")

            try:
                insert_slice_data(session_id, slice_idx, data)
            except Exception as exc:
                print(f"[DB] slice failed: {exc}")

            _broadcast({
                "type": "slice_update",
                "device_id": device_id,
                "session_id": session_id,
                "slice_idx": slice_idx,
                "data": data,
            })
            return

    # Snapshot message
    if msg.topic.startswith("ufameasy/params/snapshot/"):
        snapshot = json.loads(payload)

        slice_idx = msg.topic.split("/")[-1]

        state.update_snapshot(slice_idx, snapshot)
        state.add_event("snapshot_received", {"slice_idx": slice_idx, "param_count": len(snapshot)})

        if mqtt_event_loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                manager.broadcast({
                    "type": "snapshot",
                    "slice_idx": slice_idx,
                    "data": snapshot,
                }),
                mqtt_event_loop,
            )
            future.add_done_callback(_log_broadcast_error)



    # Single parameter message
    elif msg.topic.startswith("ufameasy/parameters/"):
        param_name = msg.topic.split("/")[-1]

        try:
            value = int(payload)
        except ValueError:
            value = payload

        state.update_parameter(param_name,value)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

client.on_connect = on_connect
client.on_message = on_message

def start_mqtt():
    """
    Connect to the local MQTT broker and start the Paho network loop.

    Returns:
        None.

    Side Effects:
        Opens a broker connection and starts Paho's background loop thread.

    Raises:
        OSError: If the MQTT broker cannot be reached on localhost:1883.
    """
    global mqtt_event_loop

    print("Starting MQTT...")
    mqtt_event_loop = asyncio.get_event_loop()
    client.connect("localhost", 1883, 60)
    # loop_start avoids blocking FastAPI startup while callbacks keep running.
    client.loop_start()
