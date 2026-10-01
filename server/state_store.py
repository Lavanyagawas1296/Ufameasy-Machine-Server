"""
In-memory state container for machine parameters and recent events.

Provides the shared process-local store used by MQTT callbacks to publish
machine updates and by FastAPI endpoints to serve the latest known state.
The module intentionally avoids external dependencies so it can be imported
by both runtime code and small diagnostic scripts.
"""
from threading import Lock

class StateStore:
    """
    Process-local storage for machine parameters and event history.

    Stores the most recent value for each device parameter and a bounded list
    of recent events.
    """
    def __init__(self):
        """
        Initialize empty parameter and event collections.

        Side Effects:
            Creates mutable dictionaries/lists owned by this store instance.
        """
        self.parameters = {}
        self.slice_snapshots = {}
        self.job_states = {}
        self.events = []
        self.lock = Lock()

    def get_job_state(self, device_id):
        with self.lock:
            return dict(self.job_states.get(device_id, {
                "status": "idle",
                "file_name": "",
                "file_path": "",
                "total_lines": 0,
                "current_line": 0,
                "elapsed_seconds": 0,
                "estimated_seconds": 0,
                "current_gcode": "",
                "percentage": 0.0
            }))

    def update_job_state(self, device_id, update_dict):
        with self.lock:
            current = self.job_states.setdefault(device_id, {
                "status": "idle",
                "file_name": "",
                "file_path": "",
                "total_lines": 0,
                "current_line": 0,
                "elapsed_seconds": 0,
                "estimated_seconds": 0,
                "current_gcode": "",
                "percentage": 0.0
            })
            current.update(update_dict)
            if current["total_lines"] > 0:
                current["percentage"] = round((current["current_line"] / current["total_lines"]) * 100, 1)
            else:
                current["percentage"] = 0.0
            return dict(current)

    def update_parameter(self, device_id, key, value):
        """
        Store the latest value for a machine parameter.

        Args:
            device_id: Device identifier that owns the parameter.
            key: Parameter identifier received from MQTT or test code.
            value: Latest value for the parameter.

        Returns:
            None.

        Side Effects:
            Mutates the in-memory parameter dictionary.
        """
        with self.lock:
            self.parameters.setdefault(device_id, {})[key] = value

    def get_parameters(self, device_id):
        with self.lock:
            return dict(self.parameters.get(device_id, {}))

    def get_parameter(self, device_id, key):
        with self.lock:
            return self.parameters.get(device_id, {}).get(key)
    
    def update_snapshot(self, device_id, slice_idx, snapshot):
        with self.lock:
            self.slice_snapshots.setdefault(device_id, {})[str(slice_idx)] = snapshot
        
    def get_all_snapshots(self, device_id):
        with self.lock:
            return dict(self.slice_snapshots.get(device_id, {}))

    def clear_snapshots(self):
        with self.lock:
            self.slice_snapshots.clear()
    
    def add_event(self, event_type: str, details: dict):
        from datetime import datetime
        self.events.append({
            "type": event_type,
            "timestamp": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
            "details": details
        })
        if len(self.events) > 50:
            self.events.pop(0)


state = StateStore()
