"""
Manual MQTT publisher for exercising the machine telemetry path.

Connects to the local broker and publishes a representative parameter update
on the topic consumed by server.mqtt_client. This script is useful for
checking the MQTT -> StateStore -> API flow during local development.
"""

import paho.mqtt.client as mqtt

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

client.connect("localhost", 1883, 60)

client.publish(
    "ufameasy/parameters/LASER_POWER",
    "60"
)

print("Published")
client.disconnect()
