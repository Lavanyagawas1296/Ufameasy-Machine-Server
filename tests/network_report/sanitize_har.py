"""Redact test credentials/cookies from locally captured HAR artifacts."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SENSITIVE = {"cookie", "set-cookie", "authorization"}


def clean_headers(headers: list[dict]) -> None:
    for header in headers:
        if str(header.get("name", "")).lower() in SENSITIVE:
            header["value"] = "[REDACTED]"


def clean_cookies(cookies: list[dict]) -> None:
    for cookie in cookies:
        if "value" in cookie:
            cookie["value"] = "[REDACTED]"


def main() -> None:
    changed = []
    for path in (ROOT / "har").glob("*.har"):
        har = json.loads(path.read_text(encoding="utf-8"))
        for entry in har.get("log", {}).get("entries", []):
            request = entry.get("request", {})
            response = entry.get("response", {})
            clean_headers(request.get("headers", []))
            clean_headers(response.get("headers", []))
            clean_cookies(request.get("cookies", []))
            clean_cookies(response.get("cookies", []))
            if request.get("postData"):
                request["postData"]["text"] = "[REDACTED]"
            if "/login" in str(request.get("url", "")):
                response.get("content", {}).pop("text", None)
        path.write_text(json.dumps(har, ensure_ascii=False), encoding="utf-8")
        changed.append(path.name)
    print("redacted=" + ",".join(changed))


if __name__ == "__main__":
    main()
