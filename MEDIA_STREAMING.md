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

On the DED console, run the UFAMeasy installer instead of editing local files:

```powershell
powershell -ExecutionPolicy Bypass -File D:\UFAMeasy_Workspace\UFAMeasy\install.ps1 -ConfigureMediaMtx
```

It detects the console LAN IP and DirectShow cameras, offers a camera selector,
downloads MediaMTX from its official release if needed, creates the Git-ignored
configuration in both repositories, and asks permission before adding the
required Windows Firewall rules. It also enables automatic WebRTC publishing
when UFAMeasy opens.

Then run `start_webrtc_stack.ps1` on the DED console and
`start_ufameasy_webrtc.ps1` from the UFAMeasy installation. The gateway launch
now waits for MediaMTX's Control API and reports its error log if MediaMTX did
not start correctly.
