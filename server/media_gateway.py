"""MediaMTX configuration and health checks for industrial camera streams.

This module deliberately contains no machine addresses or secrets.  Deployments
provide them through environment variables, allowing the same server build to
run on a factory LAN today and behind a cloud gateway later.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from urllib.request import Request, urlopen
from urllib.error import URLError


_DEVICE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


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
        raise LookupError(f"No MediaMTX gateway is configured for '{device_id}'")
    playback_url = str(entry.get("playback_url") or "").rstrip("/")
    path = str(entry.get("path") or f"machines/{device_id}/main").strip("/")
    if not playback_url.startswith(("http://", "https://")) or not path:
        raise ValueError(f"Invalid MediaMTX configuration for '{device_id}'")
    control_url = str(entry.get("control_url") or "").rstrip("/") or None
    return MediaStream(device_id, path, playback_url, control_url)


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
