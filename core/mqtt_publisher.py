"""MQTT publishing helpers for UFAMeasy machine requests."""
import json


class MQTTPublisher:
    def __init__(self, client, device_id: str):
        self.client = client
        self.device_id = device_id

    def publish_estimate_request(self, combined_gcode_path: str, slice_folder: str, slice_data: list):
        topic = f"ufameasy/{self.device_id}/estimate_request"
        self.client.publish(topic, json.dumps({
            "combined_gcode_path": combined_gcode_path,
            "slice_folder": slice_folder,
            "slice_data": slice_data,
        }, default=str), qos=1)
