"""
MQTT client bridge for UFAMeasy machine telemetry.

Owns the Paho MQTT client, subscribes to machine topics, converts incoming
parameter payloads, and updates the shared state store. This module is the
ingest side of the MQTT -> StateStore -> API architecture.
"""

import paho.mqtt.client as mqtt

from server.state_store import state


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
    client.subscribe("ufameasy/parameters/LASER_POWER")


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

    if msg.topic.startswith("ufameasy/parameters/"):
        # The final topic segment is the state key exposed through the API.
        param_name = msg.topic.split("/")[-1]

        try:
            value = int(payload)
        except ValueError:
            value = payload

        state.update_parameter(param_name, value)

        print(f"[PARAM] {param_name} = {value}")


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
    print("Starting MQTT...")
    client.connect("localhost", 1883, 60)
    # loop_start avoids blocking FastAPI startup while callbacks keep running.
    client.loop_start()
