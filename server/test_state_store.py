"""
Manual smoke test for the shared state store.

Imports the process-local state singleton, writes one representative machine
parameter, and prints the resulting dictionary. This script validates the
StateStore API independently of MQTT and FastAPI.
"""

from state_store import state

state.update_parameter("LASER_POWER", 60)

print(state.parameters)
