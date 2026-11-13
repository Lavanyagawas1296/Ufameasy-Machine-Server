# Customer UI network/reliability report

Date: 11 Nov 2026 (IST)  
Scope: isolated localhost test only. No application source, production configuration, production database, firewall rule, MQTT publisher, broker, LAN device, or port 8000 service was changed.

## Test setup

The requested `:8000` was not used because it is the real service port. A throwaway telemetry copy and database were used at `127.0.0.1:18000`:

```powershell
.\.venv\Scripts\python.exe tests\network_report\isolated_server.py --host 127.0.0.1 --port 18000 --timeout-keep-alive 5
```

The harness sets `CUSTOMER_MODE=1`, `UFAM_CUSTOMER_DEVICE_ID=device_001`, test-only credentials, `workers=1`, and suppresses MQTT startup. The server was stopped after testing. All HAR cookie/password material was redacted.

Available tools: Playwright/Chromium and curl. Missing: k6, Locust, Toxiproxy, iperf3, tshark. See `raw/tool_inventory.txt`.

## Results

| # | Profile | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | A-D | Fast local-only loads, caching/compression | A/B/C/D scripted overview readiness: 3.10/3.22/3.63/5.20 s; 54 HAR entries, only `127.0.0.1:18000`; conditional overview returned 304. Static assets gzip; 310 B overview JSON was below gzip threshold. | PASS (local simulation) |
| 2 | A/F, code review | Hidden pause, 5 s timeout, jitter/no overlap | `app.js` has no visibility check or `AbortController`; retry is deterministic 3/6/12 s; overview serializes itself, but logs/history have a 3 s interval without an in-flight guard. | FAIL |
| 3 | F local blackout | Keep data, stale after two misses, recover | State stayed `Idle`; stale banner appeared at 11 s; recovered by 15 s. Browser only logged expected offline resource errors. | PASS |
| 4 | MQTT/broker | Offline/online recovery | Not run: stopping publisher/broker would affect shared services. | SKIPPED |
| 5 | Local server restart | Open page recovers; cookie remains valid | Disposable server ready in 3.61 s; page remained `/`, authenticated, and showed `Idle`. Invalid cookie returned 303 to login. | PASS |
| 6 | A; C/D/G unavailable | Logs, pagination, CSV under faults | Browser custom range returned 50 rows; CSV returned 1,001 rows with `time_ist,severity,message`. 50k-row/cut-connection tests require Toxiproxy/load tool. | PASS / SKIPPED |
| 7 | A; C/D/G unavailable | Preview and download | Test-only archived file: 20-line preview 200; full download 64,399 B; HTTP Range 206/128 B; browser view/download links present. | PASS / SKIPPED |
| 8 | A/C/D | Login works; attacker gets 429 | Bad user 7.6 ms, bad password 9.1 ms; sixth failed attempt returned 429. Slow-login proxy profiles unavailable. | PASS / SKIPPED |
| 9 | 10 localhost clients | Concurrent read resilience | One client: overview p50 1,387 ms, logs 1,025 ms, history 545 ms. Ten concurrent clients: 20/20 API requests timed out at ~10 s (100% error). | FAIL |
| 10 | localhost | Slow clients bounded | A partial-header socket remained open after 7 s; `timeout_keep_alive=5` does not impose a header/body-read deadline. Packet/SYN analysis unavailable without tshark. | FAIL |
| 11 | A | No customer WebSocket per supplied checklist | HTTP `GET /ws` is 404, but the customer UI deliberately opens authenticated `/ws` and receives telemetry updates. This contradicts the pasted checklist but matches the current live-refresh requirement. | FAIL against checklist / intentional product behavior |
| 12 | LAN | IP, firewall, second-device reachability | Not run: requires shared-network/firewall/second-device changes. UI uses relative API URLs. | SKIPPED |
| 13 | Timezones/clock drift | IST labels and correct relative age | Absolute formatting is fixed to Asia/Kolkata and footer labels IST. Relative age uses browser `Date.now()`, so a client clock 5 minutes wrong produces a 5-minute error. | FAIL (clock drift) |
| 14 | A | Customer security boundaries | HttpOnly + SameSite=Strict, CSP, no-store, X-Content-Type-Options, frame denial, path traversal 400, docs/OpenAPI/operator path 404, 429 rate limit. Secure/HSTS/COOP absent on HTTP as expected. | PASS |
| 15 | Chrome/Edge A/C | Layout and keyboard usable | Chrome and Edge desktop A plus mobile C: `Idle`, no horizontal overflow, focus present. Both emit one unexplained generic 404 console message; Firefox not installed. | PASS with P2 issue |

## Measured baseline

- Initial post-document resource transfer in profile A: CSS 2,238 B + JS 4,423 B + overview API 610 B = 7,271 B.
- Poll payload examples: overview 610 B; recent logs 788 B; history about 613 B in the HAR snapshot.
- Page readiness is dominated by telemetry aggregation, not network shaping: profile D rose to 5.20 s despite localhost transport.
- Blackout recovery: stale at 11 s; cleared by 15 s after restoration.
- Server restart: ready in 3.61 s; page retained authentication and recovered without reload.

## Ranked risks

### P0 — Modest concurrent reads time out

Reproduce: run `NETWORK_REPORT_CLIENTS=10 NETWORK_REPORT_DURATION_S=12 .\.venv\Scripts\python.exe tests\network_report\load_probe.py` against the disposable server. All 20 requests timed out after about 10 s. Evidence: `raw/load_probe_10_clients.json`; single-client baseline: `raw/load_probe_single.json`.

Suggested fix: profile `customer_stats` telemetry scans, cache/pre-aggregate the current state/log history, bound DB work, then re-run a real 20-client/30-minute and 100-client/2-minute load test.

### P1 — Polling is not network-safe enough

`customer_ui/app.js` lacks a 5-second fetch deadline, visibility pause, retry jitter, and protection against overlapping logs/history refreshes. Deterministic exponential retries delay recovery to about 15 seconds in the blackout test. Evidence: `raw/offline_probe.json` and source review.

Suggested fix: `AbortController` at 5 s, `visibilitychange` pause/resume, randomized backoff, and per-endpoint in-flight locks.

### P1 — Slow partial HTTP clients can hold a connection

One incomplete request remained open after 7 s. Evidence: `raw/slowloris_probe.json`.

Suggested fix: put the service behind a reverse proxy with request-header/body read deadlines and connection limits; do not rely on Uvicorn keep-alive timeout alone.

### P1 — Requirement mismatch for WebSocket use

The checklist says customer UI must not use WebSocket, while the current requested no-refresh behavior intentionally uses one. Evidence: redacted HARs show a `101` upgrade and `customer_ui/app.js` opens `/ws`.

Suggested fix: resolve the product requirement before delivery; retain authenticated WebSocket live updates if no-refresh remains required, then revise the acceptance checklist.

### P2 — Generic 404 console message in Chrome and Edge

Both browser smoke runs report one generic resource-404 console error, without a captured response URL. Evidence: `raw/browser_matrix_*`.

Suggested fix: reproduce with DevTools network preservation and eliminate the missing resource (likely cosmetic, but delivery should have a clean console).

### P2 — Relative age depends on client clock

`ago()` uses browser time, so wrong laptop clocks misstate “last seen”. Suggested fix: derive relative age from server time included in the overview response.

## Nashik on-site checklist

Run from the technician PC (replace placeholders):

```powershell
ipconfig /all
Get-NetConnectionProfile
ping <gateway-ip>
ping <server-ip>
arp -a
Test-NetConnection <server-ip> -Port 8000
curl.exe -sS -o NUL -w "http=%{http_code} connect=%{time_connect}s starttransfer=%{time_starttransfer}s total=%{time_total}s bytes=%{size_download}\n" http://<server-ip>:8000/health
```

Acceptance targets: TCP test succeeds, health returns 200, health total under 1 s on LAN, dashboard has no stale banner after two normal poll cycles, and a new UFAMeasy event updates the page without a manual refresh. Use a DHCP reservation or hostname; after an IP change, users must reopen the new address because the UI correctly uses relative URLs.

## Not automatically tested

- Toxiproxy profiles E/G/H and true jitter/loss/cut-response behavior: Toxiproxy unavailable.
- 20-client 30-minute soak and 100-client spike: k6/Locust unavailable; the smaller 10-client test already failed.
- iperf3, tshark/Wireshark, SYN/retransmit/RST counts: tools and second device unavailable.
- MQTT publisher/broker outage, real LAN/DHCP/firewall/Public-vs-Private tests, second-device reachability: deliberately not performed because they could affect shared systems.
- Firefox: not installed.
- Full 50,000-row CSV and faulted download: no fault proxy/load generator.

## Evidence index

- Browser/HAR: `har/`, `screenshots/`, `raw/playwright_results.json`, `raw/browser_matrix_*.json`
- HTTP/security: `raw/http_probe.json`, `raw/curl_health_*`, `raw/test_netconnection.txt`
- UI/file/log checks: `raw/focused_ui_probe.json`, `raw/file_probe.json`
- Recovery/load/slow-client: `raw/offline_probe.json`, `raw/restart_probe.json`, `raw/load_probe_*.json`, `raw/slowloris_probe.json`
- Consolidated local evidence: `raw/summary.json`
