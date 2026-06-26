"""
MQTT client bridge for UFAMeasy machine telemetry.

Owns the Paho MQTT client, subscribes to machine topics, converts incoming
parameter payloads, and updates the shared state store. This module is the
ingest side of the MQTT -> StateStore -> API architecture.
"""
import asyncio
import json
import paho.mqtt.client as mqtt

from server.state_store import state
from server.ws_manager import manager


mqtt_event_loop = None


def _log_broadcast_error(future):
    """
    Log exceptions raised by background WebSocket broadcasts.
    """
    try:
        future.result()
    except Exception as exc:
        print(f"[WS] Broadcast failed: {exc}")


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
    print(f"Received topic={msg.topic}")
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

    # Snapshot message
    if msg.topic.startswith("ufameasy/params/snapshot/"):
        snapshot = json.loads(payload)

        slice_idx = msg.topic.split("/")[-1]
        
        print(f"slice_idx={slice_idx} "f"id(state)={id(state)} "f"id(snapshot_store)={id(state.slice_snapshots)}")
        
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
            print(f"[WS] Active connections: {len(manager.active)}")

        print(f"[SNAPSHOT] Loaded "f"{len(snapshot)} parameters")

    # Single parameter message
    elif msg.topic.startswith("ufameasy/parameters/"):
        param_name = msg.topic.split("/")[-1]

        try:
            value = int(payload)
        except ValueError:
            value = payload

        state.update_parameter(param_name,value)

        print(f"[PARAM] "f"{param_name} = {value}")

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
