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

    Stores the most recent value for each parameter and a bounded list of
    recent events. The current implementation is not explicitly thread-safe;
    it relies on simple CPython dictionary/list operations while MQTT and
    FastAPI access the same singleton in one process.
    """
    def __init__(self):
        """
        Initialize empty parameter and event collections.

        Side Effects:
            Creates mutable dictionaries/lists owned by this store instance.
        """
        self.parameters = {}
        self.slice_snapshots = {}
        self.events = []
        self.lock = Lock()

    def update_parameter(self, key, value):
        """
        Store the latest value for a machine parameter.

        Args:
            key: Parameter identifier received from MQTT or test code.
            value: Latest value for the parameter.

        Returns:
            None.

        Side Effects:
            Mutates the in-memory parameter dictionary.
        """
        with self.lock:
            self.parameters[key] = value
    
    def update_snapshot(self, slice_idx, snapshot):
        with self.lock:
            print(f"STORE BEFORE: {list(self.slice_snapshots.keys())}")

            self.slice_snapshots[str(slice_idx)] = snapshot

            print(f"STORE AFTER : {list(self.slice_snapshots.keys())}")
        
    def get_all_snapshots(self):
        with self.lock:
            return self.slice_snapshots
    
    def add_event(self, event_type: str, details: dict):
        from datetime import datetime
        self.events.append({
            "type": event_type,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "details": details
        })
        if len(self.events) > 50:
            self.events.pop(0)


state = StateStore()
