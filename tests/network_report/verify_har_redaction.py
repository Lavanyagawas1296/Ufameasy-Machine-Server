"""Confirm no non-redacted cookie values or login bodies remain in HAR files."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    issues = []
    for path in (ROOT / "har").glob("*.har"):
        for entry in json.loads(path.read_text(encoding="utf-8")).get("log", {}).get("entries", []):
            for side in ("request", "response"):
                value = entry.get(side, {})
                for cookie in value.get("cookies", []):
                    if cookie.get("value") != "[REDACTED]":
                        issues.append(f"{path.name}:{side}:cookie")
                if value.get("postData", {}).get("text") not in (None, "[REDACTED]"):
                    issues.append(f"{path.name}:{side}:body")
                for header in value.get("headers", []):
                    if header.get("name", "").lower() in {"cookie", "set-cookie", "authorization"} and header.get("value") != "[REDACTED]":
                        issues.append(f"{path.name}:{side}:header")
    report = {"clean": not issues, "issues": issues}
    (ROOT / "raw" / "har_redaction_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
