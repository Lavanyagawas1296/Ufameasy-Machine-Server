# Feasibility: send the logged-in operator with every job

Date: 2026-11-15 · Report only. No code was edited and nothing was committed or run that changes data, in either repo.

| | Workbench (read only, owned by others) | Machine Server |
|---|---|---|
| Path | `D:\UFAMeasy_Workspace\UFAMeasy` | `D:\UFAMeasy_Machine_Server` |
| Branch read | `lavanya/customer-dashboard` @ `fa09f2c` (= `main` + merge) | `feature/customer-dashboard` @ `6341036` |

All workbench paths below are relative to `Mod/` unless they start with `Ext/`. Server paths are relative to the server repo.

## 0. Answer in one paragraph

**Yes, it is feasible, and part of it already works.** Every event already has a `user` field, filled from the RBAC session (`RadialInfillWorkbench/core/events.py:105-124`). It is stored in the telemetry DB payload, which the server's customer dashboard already reads (`server/customer_stats.py:139`). So a server-only change can show an operator per job today. It is not yet **reliable enough for a report**, for five reasons:

1. An empty login logs in as `master` (DEV BYPASS, `EngineCore/security/login_ui.py:128-133`).
2. The name is stored as typed, not the stored username.
3. `user` is read again for every event, so after a lock-screen re-login the "stopped" event can name a different person from the "started" event.
4. There is no `job_id`.
5. MQTT `session/start` has no operator, and it marks a machine **connection**, not a job.

The minimal plan (section 4):
- Freeze `operator` and a new `job_id` when the job starts, and put both in `PrintJobEvent`.
- Add an optional `operator` to `session/start`.
- Add one nullable `sessions.operator` column on the server.
- Make the server read both new fields and fall back to the old ones.

Old rows and old servers keep working.

---

## 1. Identity (RBAC)

| Item | Where | Notes |
|---|---|---|
| Login dialog | `EngineCore/security/login_ui.py:124-150` (`LoginDialog._on_login`) | Sets `authenticated_role` and `authenticated_user = str(username)`, **as typed** (`:146`). Lookup is case-insensitive (`security_db.py:204`), so `Operator` and `operator` are logged as different names. |
| **DEV BYPASS** | `login_ui.py:128-133` | Empty username **and** password logs in as `master` / `Master`. While this exists, any operator name is unproven. It must go before operator names are used in reports. |
| Roles | `security_db.py:150-181`, table `roles(role_name, permissions JSON)` | Operator, Maintenance, Supervisor, Master. `job.start_job`, `job.pause_job` and `job.stop_job` are first granted at Maintenance (`:161`). |
| User store | SQLite `~/.ufameasy/security/ufameasy_security.db`, table `users(id, username UNIQUE, password_hash, salt, role)` (`security_db.py:14-41`) | No display-name column and no active/disabled flag. Default users are `master/master123` and `operator/operator123` (`:188-189`). |
| Password hashing | `security_db.py:191-192` | PBKDF2-HMAC-SHA256, 100 000 iterations, 16-byte random salt, hex. Compared with `==` (not constant time). |
| `users.id` stability | `security_db.py:197` | Saved with `INSERT OR REPLACE`, so editing a user deletes the row and inserts it again under a **new id**. **Do not use `id` as the operator key.** Use `username` (UNIQUE). |
| Runtime holder | `EngineCore/security/security_manager.py:22` `SecurityManager(QObject)`, fields `_current_user` / `_current_role` (`:39-40`) | Singleton stored as `sys._ufameasy_security_manager_instance`, reached through `get_security_manager()` (`:229-238`). |
| Reading it | properties `current_user` / `current_role` (`:154-162`), under an `RLock` | **Safe to read from any module or thread.** Writes at `:101` and `:138` are not under the lock. This is harmless in CPython (one attribute store), but user and role are two separate stores, so a reader can briefly see the new user with the old role. |
| Default before login | `"Unknown"` / `"Operator"` (`:39-40`) | `_get_current_user()` then returns `"Unknown (Operator)"`. |
| First login | `EngineCore/base_workbench.py:359-372` | Shown once at workbench start. Mandatory: cancelling quits FreeCAD. |

**One user per session, or can users switch?** One *current* user per FreeCAD process at a time, and it **can change mid-job**:
- **Switch User** button: `security_gating.py:189-200` and `:272-274`. It calls `show_login_dialog(is_mandatory=False)`, with no check for an active job.
- **Inactivity lock**: after 15 min without input (`security_manager.py:43` and `:69-77`) the lock screen accepts **any** valid user (`:96-105`). A multi-hour print with nobody touching the PC will lock, and the next person to log in becomes `current_user` while the controller keeps printing.

**Two operators on one PC:**
- *Same Windows account:* this works, but only through Switch User or a lock-screen re-login. There is one current user, so you must decide that "operator of a job" means **the user who pressed Start**, frozen at that moment (section 4).
- *Different Windows accounts:* everything sits under `Path.home()`, which has three effects:
  - Each account has its own security DB (users and passwords are not shared).
  - Each account has its own telemetry DB (`modules/logging_module/sqlite_consumer.py:30-34`) and `odometer.json`.
  - The server reads `Path.home()/.ufameasy/telemetry/ufameasy_sys.u5d` of the account it runs under (`server/customer_config.py:36`). Jobs run from the other account are **invisible** to the customer dashboard.
  
  Supported setup today: one Windows account per machine PC, with operators told apart by UFAMeasy login.

---

## 2. Where a job is born (Start → stop/finish)

Everything below runs on the **GUI thread**: button slots, plus the 100 ms `QTimer` monitor (`adapters/gcode_executor.py:79-80`; `docs/threading.md`).

| Step | file:line | What happens |
|---|---|---|
| Load | `adapters/gcode_executor.py:260-292` `load_file` | Sets `total_lines`, `source_file` and `current_file` = the source path. |
| Start | `gcode_executor.py:326` `start_execution` | Preflight (`:350`). Then the temp file is made: `current_file = ufameasy_gcode_*.gcd` (`:346`, `:360`, `:1408`). |
| File name to store and MQTT | `:361-367` | `machine_data.set_value("current_file", <source basename>)` produces a `StateChangedEvent`. `publish_file_update(name)` sends `ufameasy/<dev>/file/update`. |
| Controller start | `:395` `self.job.begin(...)` (`adapters/job_controller.py`) | On failure the job is never "started" and no event is written. |
| **Job Started event** | `:411` `_log_job_event("Job Started", details={"total_lines": ...})` | |
| `_log_job_event` | `:237-257` | Builds `PrintJobEvent(status=..., gcode_file=Path(self.current_file).name, total_lines, elapsed_seconds, error_message, details, severity)`. **`gcode_file` is the temp `.gcd` name, not the user's file.** The server works around this at `customer_stats.py:31` and `:251-252`. |
| Hold / Resume | `:425-444` → "Job Paused"; `:446-480` → "Job Resumed" | |
| M0/M1 halt | `:749-762` → `_operator_pause` `:1074-1083` | "Job Paused" with `details.reason = "Program paused at M0 (line N) …"`. |
| Operator stop | `:486-508` `stop_execution(reason="operator Stop")` → "Job Stopped" (WARNING) | **`reason` is not put in the event.** CLC abort (`:1351`) and app close (`:147`) produce the same `stopped` event. |
| Fault end | `:204-223` `_end_job` → "Job Failed" | E-stop (`:685-690`), lost controller (`:198-200`), start failure (`:642-643`). `error_message` holds the cause, e.g. "Emergency stop active". |
| Completed | `:1364-1382` `_finish_execution` → "Job Completed" | |
| Event schema | `RadialInfillWorkbench/core/events.py:116-128` `BaseEvent` (event_id, session_id, timestamp, category, severity, source, **user**, …) and `:318-328` `PrintJobEvent` | `session_id` is the **active FreeCAD document key** (`:96-102`), not the MQTT session. `user` = `"<name> (<role>)"` (`:105-113`), read again for **every** event. |
| Event bus | `core/event_bus.py:172-205` `emit` | Non-blocking `put_nowait` into a bounded queue (10 000). Events are dropped when it is full. A dispatcher thread calls the consumers (`:6-8`). |
| Telemetry logger | `modules/logging_module/sqlite_consumer.py:159-194` | Table `telemetry_events(event_id, timestamp, category, severity, source, session_id, event_type, payload)`. `payload` = `to_dict()` with None, `""` and `"unknown"` removed (`:37-57`). **No user column: `user` exists only inside the payload JSON.** `encrypt_payload` is a no-op (`:59-63`). Installed by `observability_runtime.py:56-59`. `core/telemetry.py` is a no-op backend. |
| MQTT `session/start` | `core/mqtt_publisher.py:294-309` | Payload `{file_name, total_layers, session_id}`. Called on **machine connect** (`data/machine_runtime_store.py:194-214`, file name from registry RT013, often `"unknown"`) and on **session restore** (`persistence/session_restore.py:356-369`, FCStd name). **It is per connection or restore, not per job.** One session can hold many jobs. |
| MQTT `session/end` | `mqtt_publisher.py:316-322`, called from `machine_runtime_store.py:219-237` on disconnect | reason `disconnected` or `application closed` |
| MQTT `runtime` | `mqtt_publisher.py:324-327`, called from `data/machine_data_store.py:227-253` | Sent on change of the `_RUNTIME_KEYS` (`:21-35`), which include `execution_state` and `progress_pct`. Not a heartbeat. |
| MQTT `rmc/state` | **Not on this branch or on `main`.** Only on the unmerged `lavanya/rmc-local` (`modules/remote_control/contract.py`, `handler.py`). | Has `controlled_by`, `job_state` and a 10 s heartbeat with `seq`. The server keeps it in memory only (`server/mqtt_client.py:142-158`). It logs only gaps over 20 s to `net_events`. |
| Server `session/start` | `server/mqtt_client.py:271-291` → `server/db.py:320-337` `create_session` | Uses `data.get(...)`, so unknown keys are ignored. Table `sessions(session_id, device_id, file_name, total_layers, status, started_at, ended_at)` (`db.py:80-91`). |
| Server jobs | `server/customer_stats.py:227-255` `_closed_job_attempts` | Reads `PrintJobEvent` rows from the workbench telemetry DB **on the same PC**, and pairs `started` with an end status by `session_id` (the document key). |

---

## 3. Gap list (per job, today)

| Data | Status | Where it is today |
|---|---|---|
| Operator | **PARTIAL** | `payload.user = "name (Role)"` on every telemetry row (`events.py:124`). Not frozen at Start, typed case, DEV BYPASS possible. **Not in MQTT** and not shown by the server. |
| job_id | **MISSING** | None. The server pairs jobs by document key, so pairing breaks if the active document changes during a job. `event_id` is per event. |
| Start / end time | **AVAILABLE** | `timestamp` of the `started` and end events (local IST string, `%d/%m/%Y %I:%M:%S.%f %p`). The server parses it with `TELEMETRY_TZ`. `elapsed_seconds` (Hold excluded) is on each event. |
| Stop reason (operator vs alarm) | **PARTIAL** | `stopped` vs `failed` plus `error_message` (E-stop, lost link, start failure). Operator stop, CLC abort and app close all give a bare `stopped`; the `reason` argument is dropped (`gcode_executor.py:486`, `:507`). **Server bug:** `_JOB_END_STATUSES` (`customer_stats.py:213`) has no `failed`, so an E-stopped job stays "active" and is never counted. |
| Alarm code | **PARTIAL** | Error and alarm codes are watched (`gcode_executor.py:559-585`) but go only to the CLC text log and the UI status line. The engine signal `alarmChanged` (`adapters/engine_signal_bridge.py:22`, `:49-50`) feeds only the UI (`ui/motion_ui.py:2090`, `ui/machine_outputs.py:149`). Not in telemetry and not in MQTT. A controller alarm does not end the job by itself. |
| File name | **PARTIAL** | Source basename in `StateChangedEvent(current_file)` and MQTT `file/update`. `PrintJobEvent.gcode_file` holds the temp name (see §2). |
| Folder / part name | **MISSING** | `self.source_file` (full path) is in memory only. No part or document label is on the job. |
| Current G-code line / total lines | **PARTIAL** | `total_lines` is on every `PrintJobEvent`. The current line (`_exec_line_idx`, `:1501-1516`) shows only as `progress_pct` in MQTT `runtime` and the server's `runtime_log`. The line reached is **not** on the stop or fail event. |
| Paused / M0 state | **AVAILABLE** | `PrintJobEvent` `paused` (M0/M1 has `details.reason`, Hold has none) and `resumed`. `execution_state` = `paused` (Hold) vs `waiting` (at M1) (`:1482-1492`), as a `StateChangedEvent` and in MQTT `runtime`. |
| Machine state | **PARTIAL** | running / paused / waiting / idle: AVAILABLE (`execution_state`). connected / disconnected: AVAILABLE (`MachineConnectionEvent`, `machine_runtime_store.py:182-191`). **alarm: MISSING.** **off: PARTIAL**, because a clean close shows as a disconnect with reason "application closed", but a crash or power loss leaves nothing. |

---

## 4. Minimal change plan (operator + job_id)

### 4.1 What to send
- **`operator`** = the **stored username** from `users.username` (UNIQUE). Not the typed text, and not `users.id` (it changes on every edit, see §1). There is no separate display name, so the username is the display name. If one is wanted later, add a nullable `display_name` column to `users` (migration v3; `CURRENT_SCHEMA_VERSION = 3` is declared at `security_db.py:65`, but no v3 migration exists yet).
- **`operator_role`** = the role at Start (useful for audits; small).
- **`job_id`** = `uuid4()` made in `start_execution`.
- **Never send** `password_hash`, `salt`, the users list or permissions. Send no full paths: they contain the Windows user folder. Send basenames only.
- Operator names are personal data. Show them on the **customer** dashboard only after a decision (see 4.4).

### 4.2 Workbench changes (others' modules: need approval)

1. **`EngineCore/security/security_db.py` `authenticate`** (`:200-213`): also return the stored `username` (e.g. return `(role, uname)`, or add `authenticate_user()` so the old signature stays). **`login_ui.py:143-146`**: set `authenticated_user` from the stored username. **Remove or gate the DEV BYPASS** (`:128-133`), e.g. allow it only behind an env var on dev PCs.
2. **`EngineCore/security/security_manager.py`**: add one method, `current_identity() -> tuple[str, str]`, that returns `(user, role)` under the lock (fixes the user/role tearing). Optional: set user and role inside one locked block at `:101-102` and `:138-139`.
3. **`RadialInfillWorkbench/core/events.py` `PrintJobEvent`** (`:318-328`): add `job_id: Optional[str] = None`, `operator: Optional[str] = None`, `operator_role: Optional[str] = None`, and optionally `stop_reason: Optional[str] = None`. Keep the inherited `user` as it is, for compatibility. `SCHEMA_VERSION` can stay 1: the fields are optional, and the consumer drops `None`.
4. **`adapters/gcode_executor.py`**:
   - In `start_execution`, just before `:411`, set `self._job_id = str(uuid4())`, `self._job_operator, self._job_operator_role = sm.current_identity()` and `self._job_source_name = Path(self.source_file).name`.
   - In `_log_job_event` (`:237-257`), pass `job_id`, `operator`, `operator_role`, and `gcode_file=self._job_source_name` (fixes the temp-name issue).
   - Optional, same edit: add `stop_reason` from the `reason` argument of `stop_execution` (`:486`, `:507`), and `lines_done=self._exec_line_idx + 1` in `details` on stop, fail and complete.
   - Clear the three fields after the end event.
   
   Everything here already runs on the GUI thread.
5. **`core/mqtt_publisher.py` `publish_session_start`** (`:294-309`): add keyword-only `operator: str | None = None`, and put `"operator"` in the JSON only when it is set. The two callers (`data/machine_runtime_store.py:208`, `persistence/session_restore.py:364`) pass `get_security_manager().current_user` in a try/except. The positional signature is unchanged, so `tests/test_mqtt_publisher.py:211`, `:334` and `:346` still pass. **Note:** a session spans many jobs and can see several operators. The operator in `session/start` only means "logged in when the machine connected". The per-job operator lives in `PrintJobEvent`. If MQTT must carry the per-job operator, add a `ufameasy/<dev>/job/start|end` topic `{job_id, operator, operator_role, file_name, total_lines, status, stop_reason, lines_done, elapsed_seconds}`. Old servers ignore unknown topics.

### 4.3 Server changes (own repo)

1. **`server/db.py` `init_db`**: `ALTER TABLE sessions ADD COLUMN operator TEXT`, guarded by `PRAGMA table_info`. The same pattern already exists at `:188-193`. It is nullable, so old rows read as NULL. `create_session(..., operator=None)` (`:320`). `get_sessions_by_device` (`:469-487`) also selects `operator`.
2. **`server/mqtt_client.py:271-290`**: `operator = _clean_operator(data.get("operator"))`, using the same rule as `RMCService.normalize_operator_name` (`server/rmc_service.py:115-119`: 1-40 chars `[A-Za-z0-9 ._'-]`), but returning None instead of raising. Pass it to `create_session` and the `session_start` broadcast. An old workbench sends no key, so the value is NULL.
3. **`server/customer_stats.py` `_closed_job_attempts`** (`:227-255`):
   - Pair on `payload["job_id"]` when it is present, else on the current `session_id`.
   - Read the operator from `payload["operator"]`, else parse the legacy `payload["user"]` (`"name (Role)"`: split on the last `" ("`), else `"Unknown"`.
   - Prefer the new `gcode_file` (now the real name) and keep the `_CONTROLLER_GCODE_RE` fallback for old rows.
   - **Add `"failed"` to `_JOB_END_STATUSES`** (`:213`). This is an existing bug, independent of this feature.
4. **Customer API** (`server/customer_routes.py`): add `operator` to the per-job data **only if the product allows it**. Proposed: a config flag (e.g. `CUSTOMER_SHOW_OPERATOR`, off by default), or only for the `admin` principal (`customer_routes.py:54`). Keep `_safe_message` (`customer_stats.py:635`) free of `user`, as it is now. The logs endpoint must not start leaking names.
5. Tests: `tests/test_customer_api.py` and `tests/test_machine_overview.py` insert `PrintJobEvent` rows without `operator` or `job_id`. They stay valid and become the backward-compatibility tests. Add cases for: new fields present, legacy `user` parsing, `failed` ends a job, and `sessions.operator` NULL vs set.

### 4.4 Backward compatibility

| Combination | Result |
|---|---|
| Old workbench → new server | No `operator` or `job_id`: the server falls back to `user` parsing and session pairing; `sessions.operator` = NULL. |
| New workbench → old server | Extra JSON keys are ignored (`data.get`). Extra payload fields are ignored by the current `customer_stats`. |
| Old telemetry rows | Unchanged. The `user` parse gives a best-effort operator, and rows from DEV BYPASS logins show as `master`. |

### 4.5 Privacy notes
- Send username and role only. Never hashes, salts or the user list.
- **Existing exposure:** `REVIEW.md` B4/B6 already notes that the anonymous FTP server publishes `~/.ufameasy/security/`, which includes the password hashes. Fix that before operator data is advertised as a feature.
- Store and show only what the report needs: operator per job and role. Do not add operator names to the customer **logs** text.
- A full source path reveals the Windows user folder. Send basenames, or at most the last folder name.

---

## 5. Risks

- **Threading.**
  - Job events are built on the GUI thread, and the consumers run on the event-bus thread. Reading `current_user` is locked, so this is safe.
  - **Plausible risk, not verified:** `_get_current_user()` runs for *every* event and calls `get_security_manager()`. If the first event of the process is built on a worker thread before the login at `base_workbench.py:368`, the `SecurityManager` QObject and its timeout `QTimer` are created on that worker thread. The 15-min lock would then never fire. Freezing the operator in the executor (GUI thread) avoids adding more of these calls.
- **Mid-job identity change:** Switch User and the inactivity lock can change `current_user` during a job (§1). Freezing at Start fixes the report. Whether Switch User should be blocked while a job is active is a product decision for the security owner.
- **Trust:** until the DEV BYPASS is removed, "operator" can be `master` with no password. Do not present it as proof of who ran a job.
- **Ownership and approval:**
  - `EngineCore/security/*` and `core/events.py`: Sanjaygehlot1.
  - `adapters/gcode_executor.py`: rohanbachhav.
  - `core/mqtt_publisher.py`, `data/machine_runtime_store.py`, `data/machine_data_store.py`: you (most commits).
  - `modules/logging_module/*`: Sanjaygehlot1; `REVIEW.md` §6 records it as someone else's module.
  - Server: you.
- **logging_module rules:** no written rules document was found in either repo. The rules visible in the code are:
  - Logging is *passive*. Consumer failures must not affect machine work (`core/event_bus.py:1-8`).
  - `emit` must never block, and it drops events when the queue is full (`:172-205`).
  - The consumer never throws (`sqlite_consumer.py:69`, `:189-194`).
  
  The plan respects these. It adds **no** column to `telemetry_events` and does not touch the consumer, because the new fields travel in the payload JSON. Do not make `_log_job_event` raise, and do not read the security DB inside it. If the no-op `encrypt_payload` (`sqlite_consumer.py:59`) ever becomes real, the server's direct reads of the telemetry DB break. Agree on this with the logging owner.
- **Existing tests:**
  - `tests/test_m0_halt.py:74` stubs `_log_job_event`, so it is unaffected.
  - `tests/test_mqtt_publisher.py` checks keys, not exact payload equality, so an extra optional key is safe.
  - `tests/test_job_controller.py` does not touch events.
  - Server tests use minimal payloads and stay valid (see 4.3.5).

---

## 6. Daily run time (run / idle / paused / alarm / off)

**Partly derivable today, from the telemetry DB alone:**
- **run / paused / waiting / idle**: from `StateChangedEvent(parameter_key="execution_state")`. It is emitted on every change (`data/machine_data_store.py:186-187`, `:267-279`; the key is not in `_NOISY_KEYS` at `:74-83`) and set at Start, Hold, Resume, M1, stop, fail and finish (`gcode_executor.py:1482-1492`).
- **connected / disconnected**: from `MachineConnectionEvent`, which the server already reads (`customer_stats.py:180-210`).
- Both come from the GUI thread, so they are in order, and both carry ms timestamps. Split the intervals at local midnight (`TELEMETRY_TZ`).

**Not reliable, because two signals are missing:**
1. **Alarm**: no telemetry event exists (§3). *Smallest change:* in the `alarmChanged` handler, call `machine_data.set_value("machine_alarm", f"{error}/{alarm}" or None)`. This emits a `StateChangedEvent` automatically, with no new event class. The `in_emergency` flag can go in the same key. Owner: `machine_data_store` (you) plus a one-line connect in the executor or motion adapter (rohanbachhav).
2. **Off after a crash or power loss**: the last state stays open until the next start, so a crash overnight counts as a whole night of "running" or "idle". There is also no event at app start, and `execution_state` emits nothing while it stays at its default. *Smallest change:*
   - A **heartbeat**: `SystemStateEvent(state="heartbeat", details={"execution_state", "connection_state"})` every 60 s from a GUI `QTimer` (about 1 440 rows per day).
   - Plus `SystemStateEvent(state="app_started")` once at startup.
   - The server ends any open interval at the last heartbeat + 60 s.
   
   The odometer thread (`modules/logging_module/odometer.py:67-73`, 60 s tick) is the obvious place, but it is in logging_module (someone else's) and runs off the GUI thread. A `QTimer` in `machine_runtime_store` is the lower-friction option.

The `rmc/state` 10 s heartbeat (unmerged `lavanya/rmc-local`) would also do this over MQTT, but the server does not persist it today (`server/mqtt_client.py:142-158`), and that branch is blocked (`REVIEW.md` B1-B3).

---

## 7. Summary table and order

| # | Feature | Data available? | Change needed | Effort | Approves |
|---|---|---|---|---|---|
| 1 | Show operator per job on the server from existing rows | PARTIAL (`payload.user`) | Parse `user` in `_closed_job_attempts`; expose behind a flag | S | you (server) |
| 2 | Count E-stopped / failed jobs | AVAILABLE (status `failed`) | Add `failed` to `_JOB_END_STATUSES` | S | you (server) |
| 3 | Trustworthy identity | PARTIAL | Remove or gate the DEV BYPASS; stored username from `authenticate`; `current_identity()` | S | Sanjaygehlot1 (EngineCore/security) |
| 4 | Operator + job_id frozen per job | MISSING | New optional `PrintJobEvent` fields; set in `start_execution`, pass in `_log_job_event` | S | Sanjaygehlot1 (`events.py`) + rohanbachhav (executor) |
| 5 | Real file name and stop reason on job events | PARTIAL | `gcode_file` = source basename; `stop_reason`; `lines_done` | S | rohanbachhav |
| 6 | Server pairs by job_id, reads operator | — | `customer_stats` fallback logic + tests | S–M | you |
| 7 | Operator in MQTT `session/start` | MISSING | Optional kwarg + 2 callers; `sessions.operator` column + parse | S | you (publisher, stores, server) |
| 8 | Per-job MQTT topic (operator on the wire per job) | MISSING | `job/start`, `job/end` topics + server `jobs` table | M | you + rohanbachhav (call sites) |
| 9 | Alarm state for daily time | MISSING | `machine_alarm` key from `alarmChanged` | S | you + rohanbachhav |
| 10 | Off / crash bounding for daily time | MISSING | 60 s heartbeat + `app_started` event; server interval builder | M | you (+ logging owner if placed in odometer) |
| 11 | Daily run/idle/paused/alarm/off report | PARTIAL | Server-side interval builder over 9 and 10 | M | you |
| 12 | Customer display of operator | — | Product/privacy decision; flag or admin-only | S | product owner |

**Recommended order:**
1. **2**, then **1**: server only, no approval needed. This gives an immediate result, labelled "best effort".
2. **3**: without it, operator data cannot be trusted.
3. **4 + 5**: one small workbench PR across `events.py` and `gcode_executor.py`.
4. **6**.
5. **7**.
6. **9 + 10**, then **11**.
7. **8** only if a remote server (not on the same PC) must get per-job operators. Today the customer dashboard reads the local telemetry DB, so 4 and 6 already cover it.
8. **12** before anything is shown to customers.

---

Files created by this review: `D:\UFAMeasy_Machine_Server\tests\review\OPERATOR_FEASIBILITY.md` (this file). Nothing else was created or changed.
