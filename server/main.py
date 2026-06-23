"""
FastAPI application entrypoint for the UFAMeasy machine server.

Creates the API application, starts the MQTT bridge during application
startup, and exposes lightweight state and health-style endpoints. This
module connects the HTTP layer to the shared in-memory state store.
"""

from fastapi import FastAPI
from server.state_store import state
from contextlib import asynccontextmanager
from server.mqtt_client import start_mqtt
from server.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Start background services for the FastAPI application lifecycle.

    Args:
        app: FastAPI application instance receiving the lifecycle hook.

    Yields:
        None while the application is running.

    Side Effects:
        Starts the MQTT network loop in a background thread.
    """
    # MQTT must start before requests are served so API reads see live updates.
    start_mqtt()
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/debug")
def debug():
    """
    Seed and return a sample state value for manual debugging.

    Returns:
        Current parameter dictionary after inserting the debug value.

    Side Effects:
        Updates the shared state store with a sample LASER_POWER value.
    """
    state.update_parameter("LASER_POWER", 60)
    return state.parameters

@app.get("/")
def root():
    """
    Return a simple service status response.

    Returns:
        Dictionary indicating that the API process is running.
    """
    return {"status": "running"}

@app.get("/state")
def get_state():
    """
    Return the current machine parameter snapshot.

    Returns:
        Shared parameter dictionary maintained by the state store.
    """
    return state.parameters

app.include_router(router)
