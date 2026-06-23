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

    def add_event(self, event):
        """
        Append an event to the bounded in-memory event history.

        Args:
            event: Event payload to retain for recent-history inspection.

        Returns:
            None.

        Side Effects:
            Mutates the event list and drops the oldest event after 100 items.
        """
        self.events.append(event)

        # Keep memory usage predictable for a long-running API process.
        if len(self.events) > 100:
            self.events.pop(0)


state = StateStore()
