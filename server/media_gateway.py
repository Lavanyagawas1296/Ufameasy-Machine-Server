"""MediaMTX configuration and health checks for industrial camera streams.

This module deliberately contains no machine addresses or secrets.  Deployments
provide them through environment variables, allowing the same server build to
run on a factory LAN today and behind a cloud gateway later.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import os
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError


_DEVICE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class MediaStream:
    device_id: str
    path: str
    playback_url: str
    control_url: str | None

    @property
    def webrtc_url(self) -> str:
        return f"{self.playback_url.rstrip('/')}/{self.path}"

    @property
    def hls_url(self) -> str:
        return self.webrtc_url.replace(":8889/", ":8888/", 1)


def stream_provider() -> str:
    """Return the enabled player. MJPEG remains the safe compatibility default."""
    return os.getenv("UFAMEASY_STREAM_PROVIDER", "mjpeg").strip().lower()


def _device_config() -> dict:
    raw = os.getenv("UFAMEASY_MEDIA_DEVICES_JSON", "{}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("UFAMEASY_MEDIA_DEVICES_JSON must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("UFAMEASY_MEDIA_DEVICES_JSON must be a JSON object")
    return value


def stream_for(device_id: str) -> MediaStream:
    if not _DEVICE_ID.fullmatch(device_id or ""):
        raise ValueError("Invalid device ID")
    entry = _device_config().get(device_id)
    if not isinstance(entry, dict):
        # Normal UFAMeasy.exe operation starts the publisher and MediaMTX on
        # this same edge PC.  Do not require a launcher-only environment JSON
        # for that standard topology; dynamic paths safely isolate devices.
        auto_config = os.getenv("UFAMEASY_MEDIA_AUTO_CONFIG", "1").strip().lower()
        if auto_config not in {"0", "false", "no"}:
            return MediaStream(
                device_id=device_id,
                path=f"machines/{device_id}/main",
                playback_url="http://127.0.0.1:8889",
                control_url="http://127.0.0.1:9997",
            )
        raise LookupError(f"No MediaMTX gateway is configured for '{device_id}'")
    playback_url = str(entry.get("playback_url") or "").rstrip("/")
    path = str(entry.get("path") or f"machines/{device_id}/main").strip("/")
    if not playback_url.startswith(("http://", "https://")) or not path:
        raise ValueError(f"Invalid MediaMTX configuration for '{device_id}'")
    control_url = str(entry.get("control_url") or "").rstrip("/") or None
    return MediaStream(device_id, path, playback_url, control_url)


def playback_for_host(stream: MediaStream, host: str | None) -> MediaStream:
    """Use the dashboard's current host for MediaMTX on a DHCP network."""
    dynamic = os.getenv("UFAMEASY_MEDIA_DYNAMIC_PLAYBACK_HOST", "1").strip().lower()
    if dynamic in {"0", "false", "no"} or not host or not _HOST.fullmatch(host):
        return stream
    configured = urlsplit(stream.playback_url)
    return MediaStream(
        stream.device_id,
        stream.path,
        f"{configured.scheme or 'http'}://{host}:8889",
        stream.control_url,
    )


def configured_device_ids() -> set[str]:
    try:
        return {key for key in _device_config() if _DEVICE_ID.fullmatch(key)}
    except ValueError:
        return set()


# --- Same-origin HLS proxy --------------------------------------------------
# Public (https/tunnel) viewers cannot use WebRTC, so MediaMTX HLS output is
# relayed through the authenticated app. The upstream host and path template
# are fixed; only a validated device id and file name are substituted.
HLS_UPSTREAM_HOST = "127.0.0.1"
HLS_UPSTREAM_PORT = 8888
_HLS_FILE = re.compile(r"^(?:[A-Za-z0-9_-]{1,64}(?:\.min)?\.(?:m3u8|mp4|m4s|ts|js))?$")
_HLS_QUERY = {
    "_HLS_msn": re.compile(r"^\d{1,10}$"),
    "_HLS_part": re.compile(r"^\d{1,10}$"),
    "_HLS_skip": re.compile(r"^(?:YES|v2)$"),
    # MediaMTX ties sub-playlists/segments to the HLS session it issued.
    "session": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
}
_HLS_PASS_HEADERS = {"content-type", "content-length", "cache-control", "etag", "last-modified"}


def hls_proxy_url(device_id: str) -> str:
    return f"/api/media/devices/{device_id}/hls/"


def valid_hls_file(file: str) -> bool:
    """Allow only a bare file name (or "" for the player page)."""
    return bool(_HLS_FILE.fullmatch(file or ""))


def hls_upstream_target(device_id: str, file: str, query: dict) -> str:
    if not _DEVICE_ID.fullmatch(device_id or "") or not valid_hls_file(file):
        raise ValueError("Invalid HLS request")
    # MediaMTX otherwise 302s every request to set a cookieCheck cookie,
    # which this proxy does not relay.
    params = ["cookieCheck=1"] + [
        f"{key}={value}"
        for key, pattern in _HLS_QUERY.items()
        if (value := query.get(key)) is not None and pattern.fullmatch(value)
    ]
    target = f"/machines/{device_id}/main/{file}"
    return f"{target}?{'&'.join(params)}"


async def open_hls_upstream(target: str, connect_timeout: float = 3.0, read_timeout: float = 15.0):
    """Send an HTTP/1.0 GET to MediaMTX and return (status, headers, body iterator).

    HTTP/1.0 keeps the upstream response unchunked, so the body is simply
    streamed until MediaMTX closes the connection.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(HLS_UPSTREAM_HOST, HLS_UPSTREAM_PORT), connect_timeout
    )
    try:
        writer.write(
            f"GET {target} HTTP/1.0\r\nHost: {HLS_UPSTREAM_HOST}:{HLS_UPSTREAM_PORT}\r\n"
            "User-Agent: UFAMeasy-Server\r\n\r\n".encode("ascii")
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), read_timeout)
    except BaseException:
        writer.close()
        raise
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() in _HLS_PASS_HEADERS:
            headers[name.strip().lower()] = value.strip()

    async def body():
        try:
            while chunk := await asyncio.wait_for(reader.read(65536), read_timeout):
                yield chunk
        finally:
            writer.close()

    return status, headers, body()


def health(stream: MediaStream, timeout: float = 2.0) -> dict:
    """Read MediaMTX path status through its optional local Control API."""
    if not stream.control_url:
        return {"configured": True, "online": None, "detail": "Control API is not configured"}
    # ``paths/list`` is stable across MediaMTX versions and avoids treating
    # slashes in a stream path as HTTP route separators.
    url = f"{stream.control_url}/v3/paths/list"
    try:
        with urlopen(Request(url, headers={"User-Agent": "UFAMeasy-Server"}), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError) as exc:
        return {"configured": True, "online": False, "detail": f"Media gateway unavailable: {exc}"}
    items = payload.get("items") or []
    path_info = next((item for item in items if item.get("name") == stream.path), None)
    if path_info is None:
        return {"configured": True, "online": False, "detail": "Camera is not publishing"}
    ready = bool(path_info.get("ready"))
    return {"configured": True, "online": ready, "detail": "Stream ready" if ready else "Camera is not publishing"}
