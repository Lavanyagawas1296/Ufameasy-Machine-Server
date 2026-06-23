"""
Additional FastAPI routes for machine state inspection.

Defines HTTP endpoints that read from the shared state store without owning
MQTT or persistence concerns. These routes form the API-facing side of the
MQTT -> StateStore -> API data flow.
"""

from fastapi import APIRouter
from server.state_store import state

router = APIRouter()

@router.get("/state")
def get_state():
    """
    Return all known machine parameters.

    Returns:
        Shared dictionary containing the latest parameter values.
    """
    return state.parameters

@router.get("/parameter/{name}")
def get_parameter(name: str):
    """
    Return the latest value for a single machine parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    return {
        "name": name,
        "value": state.parameters.get(name)
    }

@router.get("/ufameasy/parameter/{name}")
def get_ufameasy_parameter(name: str):
    """
    Return the latest value for a single ufameasy parameter.

    Args:
        name: Parameter name to look up in the state store.

    Returns:
        Dictionary containing the requested name and its value, or None when
        the parameter has not been observed.
    """
    return {
        "name": name,
        "value": state.parameters.get(name)
    }

@router.get("/health")
def health():
    """
    Return an API health probe response.

    Returns:
        Dictionary indicating that the route layer is responsive.
    """
    return {"status": "ok"}
