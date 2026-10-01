# Industrial live streaming (MediaMTX)

MJPEG remains the default. The recommended current deployment puts MediaMTX
on the same laptop/edge PC as this FastAPI server. UFAMeasy then publishes one
outbound RTSP stream to that gateway, and all browsers use the gateway.

Enable WebRTC only after the machine gateway has been tested:

```powershell
$env:UFAMEASY_STREAM_PROVIDER = "webrtc"
$env:UFAMEASY_MEDIA_DEVICES_JSON = '{"device_001":{"playback_url":"http://<edge-laptop-ip>:8889","control_url":"http://127.0.0.1:9997","path":"machines/device_001/main"}}'
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Do not commit the environment values. `playback_url` is the browser-visible
MediaMTX WebRTC address and `control_url` is its Control API address. In cloud
deployment, replace only these URLs with the cloud gateway/reverse-proxy URLs.

On the UFAMeasy console set `UFAMEASY_STREAM_PROVIDER=webrtc`,
`UFAMEASY_CAMERA_DSHOW_DEVICE=<camera name>`, and
`UFAMEASY_MEDIAMTX_RTSP_URL=rtsp://<edge-laptop-ip>:8554` before starting
UFAMeasy. The dashboard requests `/api/media/devices/{device_id}/playback`
and embeds the MediaMTX WebRTC viewer. If WebRTC is not enabled, it keeps
using the existing MJPEG flow.

For the factory test, allow the laptop/edge firewall inbound TCP `8000`, TCP
`8554`, TCP `8889`, UDP `8189`, and optionally TCP `8888`. In cloud deployment,
replace these LAN URLs with HTTPS/TURN/SRT gateway endpoints; do not expose
the console camera directly.

## One-time setup, then normal launch

Copy `streaming.local.ps1.example` to `streaming.local.ps1` in both
repositories and edit the values once. These local files are Git-ignored.
After that, run `start_webrtc_stack.ps1` on the edge laptop and
`start_ufameasy_webrtc.ps1` on the UFAMeasy console; no `$env:` commands are
needed again.
