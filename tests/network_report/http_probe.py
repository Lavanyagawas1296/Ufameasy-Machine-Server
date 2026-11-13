"""HTTP/security probe against the isolated test server only."""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:18000"
USER = "network-test"
PASSWORD = "NetworkTest-Only-2026!"


def request(opener, path, *, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    merged = {"Content-Type": "application/json"} if body is not None else {}
    merged.update(headers or {})
    started = time.perf_counter()
    try:
        response = opener.open(urllib.request.Request(BASE + path, data=data, headers=merged, method=method), timeout=10)
        content = response.read()
        return {"status": response.status, "headers": dict(response.headers), "body": content, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "headers": dict(exc.headers), "body": exc.read(), "ms": round((time.perf_counter() - started) * 1000, 1)}


def safe_headers(headers):
    allowed = {"content-type", "cache-control", "content-encoding", "etag", "content-security-policy", "referrer-policy", "permissions-policy", "x-content-type-options", "x-frame-options", "retry-after", "transfer-encoding", "content-disposition", "set-cookie"}
    result = {key: value for key, value in headers.items() if key.lower() in allowed}
    if "set-cookie" in {key.lower() for key in result}:
        for key in list(result):
            if key.lower() == "set-cookie":
                value = result[key]
                result[key] = "; ".join(part for part in value.split(";") if "=" not in part or part.strip().lower().startswith(("httponly", "samesite", "path", "secure", "max-age")))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    report = {"base": BASE}
    report["health"] = {key: value for key, value in request(opener, "/health").items() if key != "body"}
    login = request(opener, "/login", method="POST", body={"username": USER, "password": PASSWORD})
    report["login"] = {"status": login["status"], "ms": login["ms"], "headers": safe_headers(login["headers"])}
    overview = request(opener, "/api/customer/overview", headers={"Accept-Encoding": "gzip"})
    report["overview_gzip"] = {"status": overview["status"], "ms": overview["ms"], "bytes": len(overview["body"]), "headers": safe_headers(overview["headers"])}
    etag = next((value for key, value in overview["headers"].items() if key.lower() == "etag"), None)
    second = request(opener, "/api/customer/overview", headers={"If-None-Match": etag} if etag else {})
    report["overview_conditional"] = {"status": second["status"], "ms": second["ms"], "headers": safe_headers(second["headers"])}
    report["restricted_paths"] = {
        path: request(opener, path)["status"]
        for path in ("/docs", "/openapi.json", "/ws", "/api/rmc/files")
    }
    report["path_traversal"] = {
        path: request(opener, path)["status"]
        for path in ("/api/customer/files/view?name=..%2Fsecret.gcode", "/api/customer/files/download?name=C%3A%5Csecret.gcode")
    }
    invalid_cookie = urllib.request.build_opener(NoRedirect())
    invalid = request(invalid_cookie, "/", headers={"Cookie": "ufam_auth=invalid"})
    report["invalid_cookie"] = {"status": invalid["status"], "location": invalid["headers"].get("Location")}
    user_bad = request(opener, "/login", method="POST", body={"username": "nobody", "password": "bad"})
    pass_bad = request(opener, "/login", method="POST", body={"username": USER, "password": "bad"})
    more = [request(opener, "/login", method="POST", body={"username": "nobody", "password": "bad"})["status"] for _ in range(4)]
    report["login_rate_limit"] = {"unknown_user": {"status": user_bad["status"], "ms": user_bad["ms"]}, "bad_password": {"status": pass_bad["status"], "ms": pass_bad["ms"]}, "subsequent_statuses": more}
    (ROOT / "raw" / "http_probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
