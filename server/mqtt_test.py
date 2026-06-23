"""
Manual MQTT connectivity smoke test.

Creates a short-lived Paho client, connects to the local broker, and
disconnects immediately. This isolates broker reachability from the
application's callback and state-store behavior.
"""

import paho.mqtt.client as mqtt

client = mqtt.Client()

client.connect("localhost", 1883, 60)

print("Connected")

client.disconnect()
