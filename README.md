# UFAMeasy Machine Server

Industrial IoT dashboard for monitoring UFAMeasy 5-axis additive manufacturing (DED/laser) jobs in real time. Subscribes to MQTT telemetry published by the UFAMeasy FreeCAD application, persists session/parameter data, and serves a live web dashboard over WebSocket.

## Architecture

```
UFAMeasy (FreeCAD app) → core/mqtt_publisher.py → Mosquitto (localhost:1883)
                                                          ↓
                                                server/mqtt_client.py
                                                  ↙              ↘
                                          server/db.py      state_store.py
                                                                  ↓
                                                            ws_manager.py
                                                                  ↓
                                                        ui/index.html (browser)
```

UFAMeasy publishes parameter snapshots, session lifecycle events, runtime telemetry, and job estimate requests over MQTT. This server subscribes to all `ufameasy/#` topics, persists relevant data to SQLite, and broadcasts live updates to connected browsers over WebSocket.

## Requirements

- Python 3.13+
- Mosquitto MQTT broker running on `localhost:1883`
- UFAMeasy FreeCAD application (separate repo) publishing telemetry

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
pip install -r requirements.txt
```

If PowerShell blocks the activation script:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

## Running

1. Start Mosquitto broker (must be running before the server starts).
2. Start the server:
   ```bash
   uvicorn server.main:app --reload
   ```
3. Open the dashboard: `http://127.0.0.1:8000`

For access from other devices on the same network:
```bash
uvicorn server.main:app --host 0.0.0.0 --port 8000 --reload
```
Find your IP with `ipconfig | findstr "IPv4"` and share `http://<ip>:8000`. For access outside the local network, tunnel with `ngrok http 8000`.

## Project Structure

| Path | Purpose |
|---|---|
| `server/main.py` | FastAPI app entrypoint, lifespan startup, WebSocket endpoint |
| `server/routes.py` | REST API routes (`/devices`, `/devices/{id}/sessions`, `/sessions/{id}/runtime`) |
| `server/mqtt_client.py` | MQTT subscriber — routes incoming topics, writes to DB, broadcasts to WebSocket |
| `server/db.py` | SQLite schema and data access functions |
| `server/state_store.py` | In-memory snapshot/event cache |
| `server/ws_manager.py` | WebSocket connection manager |
| `ui/index.html` | Single-page dashboard frontend (vanilla JS, no build step) |
| `data/params.db` | SQLite database (created at runtime) |

## MQTT Topics

| Topic | Direction | Purpose |
|---|---|---|
| `ufameasy/session/reset` | UFAMeasy → Server | Clear all in-memory snapshots |
| `ufameasy/{device_id}/session/start` | UFAMeasy → Server | New session begins (file name, total layers) |
| `ufameasy/{device_id}/session/end` | UFAMeasy → Server | Session ends (status: complete/aborted) |
| `ufameasy/{device_id}/runtime` | UFAMeasy → Server | Live runtime telemetry (laser, gas, layer, power) |
| `ufameasy/{device_id}/slice/{n}` | UFAMeasy → Server | Full parameter snapshot for a slice |
| `ufameasy/{device_id}/estimate/request` | UFAMeasy → Server | Request job time/powder estimate for a gcode file |

The server auto-creates a session if slice data arrives with no active session (e.g. dashboard started after UFAMeasy began publishing).

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `/devices` | GET | List all registered devices |
| `/devices/{device_id}/sessions` | GET | Session history for a device |
| `/sessions/{session_id}/runtime` | GET | Latest runtime snapshot for a session |
| `/snapshots` | GET | Current in-memory parameter snapshots |
| `/ws` | WebSocket | Live event stream (session/runtime/snapshot/estimate updates) |

## WebSocket Message Types

Broadcast to all connected clients on `/ws`:

- `session_reset` — clear dashboard state
- `session_start` — new file/session began
- `session_end` — session closed
- `runtime_update` — live telemetry (laser, gas, layer progress, etc.)
- `snapshot` — full parameter snapshot for a slice
- `slice_update` — incremental parameter update for a slice
- `estimate_result` — job time/powder/energy estimate, computed by UFAMeasy and relayed by the server

## Known Limitations

- **Session status never auto-closes.** `session/end` only fires on a real machine disconnect, which requires the motion controller dev kit. Without hardware connected, sessions remain marked active indefinitely. This is expected in development/demo environments.
- **Runtime telemetry (laser, gas, current layer) stays null without the dev kit.** These fields are written by the machine controller DLL into `RuntimeState` (RT001–RT035) and require physical hardware or a DLL simulator to populate.
- **Total layer count** is derived from gcode `%LAYER_START` markers at estimate time, not from the live machine controller, since no real total-layer telemetry source currently exists.

## Development Notes

- Architecture and review happen in this chat; implementation is done via Codex with single-file, surgical edits only.
- Server and UFAMeasy are separate codebases (`D:\UFAMeasy_Machine_Server` and `D:\UFAMeasy_Workspace\UFAMeasy\Mod\RadialInfillWorkbench`) connected only via MQTT — no shared imports or direct calls.
