# Pre-push review: UFAMeasy workbench + Machine Server

Date: 2026-11-14 · Review only: nothing was edited, formatted, deleted, committed or pushed in either repo. Both `git status` outputs were the same at the end as at the start.

| | (A) UFAMeasy workbench | (B) Machine Server |
|---|---|---|
| Path | `D:\UFAMeasy_Workspace\UFAMeasy` | `D:\UFAMeasy_Machine_Server` |
| Branch | `lavanya/rmc-local` (12 commits ahead of `main`, 0 behind; already the same as `origin/lavanya/rmc-local`) | `feature/customer-dashboard` (0 ahead, 2 behind `main`; `main` = `origin/main` and already holds merge `6ccfd4e`) |
| Last commit | `4b60ad4` "Merge main, fix motion_ui conflicts, and remove SC04ECtrlDll.dll" (Lavanya Gawas, 2026-10-30) | `19eafbb` "Update main UI and env example for customer dashboard" (Lavanya Gawas, 2026-11-13) |
| Uncommitted | ` M Ext/sc04e/SC04ECtrlDll.dll` (binary) · ` M modules/remote_control/handler.py` (heartbeat 10 s → 20 s) · `MM ui/motion_ui.py` (staged: removes conflict markers; unstaged: +2 lines) | `?? docs/` (ARCHITECTURE.md) |
| Ignored, worth knowing | `core/models/ownership_registry.py.bak`, `core/slicer_spec.py.bak`, `data/machine_data_store.py.bak`, untracked `Mod/RadialInfillWorkbench/Laser_hardening/` | `.env` (holds `CUSTOMER_MODE=1`, see S5) |
| Diff to main | 31 files, +4072/−30 (RMC module, MediaMTX streaming, motion_ui hooks, mqtt_publisher, install scripts) | nothing left to push; findings apply to what is already on `main` |

---

## 1. Verdict: **DO NOT PUSH** (do not merge `lavanya/rmc-local` into `main` as committed)

1. The committed `ui/motion_ui.py` on the branch (already on `origin`) contains merge-conflict markers. It raises `SyntaxError` on import, so the Motion/Operate UI and the RMC attach hook fail to load.
2. The working-tree resolution fixes the syntax but quietly drops the RMC "load" state publish from Browse (B2).
3. The RMC lifecycle imports the MQTT publisher under a second module name, so FreeCAD runs two MQTT clients. Shutdown stops only one of them (measured, B3).
4. Loading a 50 MB G-code file blocks the FreeCAD GUI thread for about 77 s (measured). The anonymous FTP server publishes `~/.ufameasy/security/` (password hashes) to the LAN. Both problems are already on `main`.
5. After B1–B3 are committed, the verdict becomes **PUSH AFTER FIXES**. The items already on `main` (B4–B7) need tickets but do not come from this merge. Machine Server: nothing left to push. Its P1s are listed in section 3.

---

## 2. Blockers (P0)

"New" = introduced by this branch. "On main" = already on `main`; it blocks a release, not this merge.

### B1. Merge-conflict markers committed in `motion_ui.py` (New, crash on load)
- `Mod/RadialInfillWorkbench/ui/motion_ui.py:2938`, `:2949`, `:2963` at `HEAD` and on `origin/lavanya/rmc-local`. `main` is clean.
```
2938 <<<<<<< HEAD
2949 =======
2963 >>>>>>> 0450181a405a7b3f794b2669de58c45246501cac
```
- Effect: `ast.parse` of the `HEAD` blob fails (`SyntaxError`). Any `import ui.motion_ui` raises, so the Operate view, the AM Controls panel and the RMC attach (`_register_am_controls_panel`) are all missing.
- Fix: commit the staged resolution, together with the B2 fix. Then re-run `git grep -nE '^(<<<<<<<|>>>>>>>)' HEAD`.

### B2. Conflict resolution drops RMC behaviour from Browse (New, silent regression)
- The working-tree `_browse_file` (`motion_ui.py:2937`) took `main`'s version. It no longer calls `load_job_file` or `_emit_job_state_changed("load")` and no longer sets `_local_action_active`. `load_job_file` (`:2972`) in turn lacks `live_view.set_job_file`, `_update_job_view_label` and `_refresh_run_mode_choices`.
```
2937 def _browse_file(self):
2938     file_path, _ = QtWidgets.QFileDialog.getOpenFileName(...)
2940     if file_path:
2941         self.current_file = file_path        # no _emit_job_state_changed("load")
```
- Trigger: a local Browse never reaches the remote dashboard. A remote `load` leaves the live view label stale, and run-mode choices are not refreshed for files with check points.
- Fix: one load path. `_browse_file` calls `self._run_local_action(lambda: self.load_job_file(path))`, and `load_job_file` gains the three UI lines. You were cleared to edit motion_ui on 2026-10-30, but the file is mostly blamed to Sanjaygehlot1 (45 %) and rohanbachhav (38 %), so loop them in.

### B3. Two MQTT singletons (New, duplicate client, unclean shutdown)
- `modules/remote_control/lifecycle.py:9` imports `RadialInfillWorkbench.core.mqtt_publisher`. Every other caller (`base_workbench.py:15`, `doc_observer.py:113`, `gcode_executor.py:367`, `machine_runtime_store.py:207`, `slicer_ui.py:2233`, …) imports `core.mqtt_publisher`. There is no `sys.modules` alias, so these are two module objects.
- Measured in FreeCADCmd: `same_module=False`, `same_singleton=False`, two client ids (`ufameasy-pub-0d0fe95d`, `ufameasy-pub-343d11ad`). After `core.mqtt_publisher.shutdown_publisher()` the RMC singleton was still alive.
- Effect: two TCP connections and two LWTs per machine, and both subscribe to `rmc/cmd`. The RMC client is never shut down by the app-close sequence, and its retained offline state arrives only by LWT, about 90 s later.
- Fix (one line, your module): `from core.mqtt_publisher import get_publisher` in `lifecycle.py:9`, or alias both names in `mqtt_publisher.py`.

### B4. Slicer `subprocess.run` with no timeout on the GUI thread (On main, freeze)
- `core/slicer_callbacks.py:724`. The calls at `:787` and `:838` have timeouts (90 s / 60 s); this one does not.
```
724 result = subprocess.run(
725     cmd,
726     check=True,
727     capture_output=True,
728     text=True,
```
- Trigger: any slice. FreeCAD shows "Not responding" for the whole slicer run. If the slicer hangs (bad mesh, a dialog, a locked output file), FreeCAD never comes back. `_stage()` (`:331`) calls `processEvents()`, so a second Slice click can re-enter while this runs.
- Fix: run the slicer in a `QProcess` or worker thread with a timeout and a Cancel button. Disable the Slice action while it runs. Owners: Sanjaygehlot1 / dinosauronthemoon.

### B5. `GCodeFileExecutor.load_file` parses the whole file on the GUI thread (On main, freeze)
- `adapters/gcode_executor.py:260-293` calls `_read_gcode_lines`, `cl.clean_line` per line, `estimate_gcode_file` (`core/job_estimator.py:1065`) and `count_checkpoint_blocks`. `start_execution` → `_prepare_execution_file` (`:1395`) parses everything again.
```
271 lines = self._read_gcode_lines(filepath)
272 self.total_lines = sum(1 for line in lines if cl.clean_line(line))
274 self.estimated_seconds = self._estimate_execution_time_seconds(filepath)
279 self.file_checkpoint_count = cl.count_checkpoint_blocks(filepath)
```
- Measured on a 50 MB / 1.43 M-line file: read 0.24 s, clean-count 3.5 s, check points 0.47 s, **estimate 73.2 s**, `prepare_program` 4.4 s. That is about 77 s of frozen GUI on Browse/Load and 4.4 s on Start, roughly 1.5 s per MB.
- Fix: do the estimate (and ideally the parse) in a worker thread and show "Estimating…" in the UI. Owners: rohanbachhav (executor), Sanjaygehlot1 (estimator).

### B6. Anonymous FTP serves all of `~/.ufameasy`, including the user/password DB (On main, secret leak)
- `modules/ftp_server/ftp_server.py:204` `authorizer.add_anonymous(ufameasy_path, perm="elr")` with `ufameasy_path = ~/.ufameasy` (`:160-164`), bound to `0.0.0.0:2121` (`:213`). It starts automatically in `Initialize` (`EngineCore/base_workbench.py:225`).
- `~/.ufameasy/security/ufameasy_security.db` (`EngineCore/security/security_db.py:14-17`) holds usernames, roles and PBKDF2 hashes with salts. `logs/` and `rmc_downloads/` are in the same tree.
- Trigger: any host on the LAN can run `ftp <machine> 2121`, log in as anonymous and `get security/ufameasy_security.db`, then crack the hashes offline.
- Fix: serve a virtual root with only `telemetry/` and `recordings/`, or require a credential and bind to the configured interface. The blame owner is Lavanya Gawas (100%), outside the RMC folder, so it needs your approval as a non-RMC change.

### B7. First-run Device ID prompt cannot be cancelled (On main, UI trap)
- `EngineCore/base_workbench.py:106-116`, called 1 s after every activation (`:351`).
```
106 while True:
108     text, ok = QtWidgets.QInputDialog.getText(getMainWindow(), "UFAMeasy Setup", ...)
113     if ok and text.strip():
116         break
```
- Trigger: on a fresh profile, Cancel or Esc reopens the modal dialog forever. FreeCAD cannot be closed normally until an ID is typed.
- Fix: on Cancel, leave the loop and use a default or ask again on the next activation. Owner: Sanjaygehlot1. (Ranked P0 because it fits "can freeze FreeCAD"; the operator can still escape by typing any ID.)

---

## 3. Fixes before the push (P1) and cleanup later (P2)

### P1, workbench
| # | Where | Problem (evidence) | Suggested fix |
|---|---|---|---|
| W1 | `core/mqtt_publisher.py:290-300` | `flush()` reads `self._last_info`, which nothing ever sets. **Measured:** `flush(1.5)` returned `True` in 0.0 s with 500 messages still queued, so at shutdown with a slow or absent broker the `session/end` is lost. | Set `self._last_info = result` in `_publish`, and wait on the queue as well |
| W2 | `core/mqtt_publisher.py:115-116, 406` | One 500-entry deque holds every topic. QoS-0 `runtime/position` messages (one per hardware tick) evict `session/start`, `session/end` and `file/update`. **Measured:** 2000 positions + 1 session end → queue pinned at 500. Old positions are replayed on reconnect. | Do not queue QoS 0 while disconnected; keep a separate small queue for session/file messages |
| W3 | `core/telemetry.py:234-242` | The drain loop never ends if the backend keeps failing (failed batches are put back). **Measured:** `stop(2)` returned, but the thread kept spinning at about **316,700 iterations/s**. In the combined run, another thread's 50 MB download did not finish in 120 s (0.31 s when run alone), which looks like starvation. | Break when a flush makes no progress or after a deadline |
| W4 | `modules/camera/camera_recorder.py:390`, `modules/camera/mediamtx_publisher.py:271` | `ffmpeg`/MediaMTX child processes have no `app_lifecycle.on_end` step (none registered; checked statically). Closing FreeCAD while recording or streaming leaves `ffmpeg` orphaned and the MP4 unfinalized (no moov atom, unplayable). MediaMTX streaming is new on this branch. | Register `stop_recording` / publisher stop in `CLOSING` |
| W5 | `Mod/RadialInfillWorkbench/replace_callbacks.py`, `replace_ded.py`, `replace_parser.py`, `replace_remainder.py` | Tracked one-off scripts that run at import time, open `c:\Users\hp\Downloads\UFAMeasy\...` and **rewrite source files** in place. Nothing references them. The headless import produced `FileNotFoundError`, so nothing was changed here. | Delete (owner dinosauronthemoon, added 2026-07-07) |
| W6 | `tests/test_mqtt_publisher.py:372` | `TestPublishSessionReset::test_queues_when_disconnected` fails: it unpacks 3-tuples, but the queue now holds 4 (`retain` added on this branch). | Update the test |
| W7 | `ui/logging_dashboard.py:994-1001, 731-735` | SQLite `SELECT … LIMIT 1000` on the GUI thread with `timeout=5.0`, re-run 100 ms after every live event. `sqlite_consumer.py` does not enable WAL, so a reader waits on the writer. Can stall the GUI up to 5 s per refresh (not measured). | Query in a worker thread; enable WAL in the consumer |
| W8 | Working tree: `Ext/sc04e/SC04ECtrlDll.dll` | Modified binary DLL (2,861,056 → 2,860,544 bytes). Commit `4b60ad4` says "remove SC04ECtrlDll.dll", yet this copy is still tracked. | Decide deliberately; never `git add -A` here |
| W9 | `core/mqtt_publisher.py:302-311` | `_shutdown` calls `loop_stop()` before `disconnect()`, so DISCONNECT is never sent and the broker fires the LWT after 1.5 × keepalive (90 s). **Measured:** shutdown blocked 4.1 s with a blackholed broker (it runs on the GUI thread in the `FINAL` phase). | `disconnect()` first, then `loop_stop()` |

### P1, Machine Server (already on `main`)
| # | Where | Problem (evidence) | Suggested fix |
|---|---|---|---|
| S1 | `server/routes.py:1065` (`async def download_recording`, `:1043`) | Calls `_assert_recording_exists` (FTP, 10 s timeout) synchronously inside `async def`. **Measured:** the event loop stalled **10.1 s** against a dead host, freezing every HTTP and WebSocket client. The sibling routes use `run_in_executor` and stalled 0.02 s. | `await loop.run_in_executor(None, _assert_recording_exists, …)` |
| S2 | `server/mqtt_client.py:480`, called from `server/main.py:89` | `client.connect("localhost", 1883, 60)` has no guard. **Measured:** the same call against a closed port raises `ConnectionRefusedError` (2.07 s), and the lifespan has no `try`, so the server fails to start whenever Mosquitto is not up yet (boot order). | `connect_async()` + `loop_start()` (paho retries), or try/except with a retry |
| S3 | `server/customer_stats.py:627-645` (`customer_logs`), `:160-165` (`_state_change_values`) | Reads the **whole** `telemetry_events` table (with payloads) on every request, then filters dates and limits in Python. `_state_change_values` scans all `StateChangedEvent` rows and runs 3× per overview. **py-spy** (10 clients, 15 s, 15 MB telemetry copy): **87 % of 2,313 samples** in `_read_rows`, from `customer_logs:645` (47 %) and `_state_change_values:162` (40 %). Only **34 requests succeeded in 15 s**. Gets worse as the table grows. | Push the time range, cursor and `LIMIT` into SQL on the indexed `timestamp`/`id`; index `event_type`; cache the state-change scan |
| S4 | `server/mqtt_client.py:222-268` | An `estimate/request` MQTT message starts an unbounded `threading.Thread`, inserts the hardcoded developer path `D:\UFAMeasy_Workspace\UFAMeasy\Mod\RadialInfillWorkbench` at `sys.path[0]` (it shadows the server's own `core` package) and opens `merged_gcode_path` taken from the payload. Any broker client can make the server read arbitrary local paths, and flood it with about 73 s of CPU per 50 MB estimate. In the cloud the import simply fails inside the thread. | Bounded worker queue; validate the path against an allowed root; ship the estimator as a dependency |
| S5 | Tests | (a) `tests/test_customer_api.py` cannot be collected: starlette 1.3.1 needs `httpx2`, which is not in `requirements.txt`, so the customer-dashboard tests never run. (b) `server/main.py:18` loads the developer `.env` (`CUSTOMER_MODE=1`) into tests, and 5 `test_net_diagnostics` tests then fail with 404 (they pass with `CUSTOMER_MODE=0`). (c) 6 `test_live_streaming` failures already fail at `05543bd`: they reference the removed `mqtt_client._sessions_lock` and `ConnectionManager.activate`. | Add `httpx2` to dev requirements; set the env explicitly in tests; update the stale tests |

### P2, cleanup later
- `core/mqtt_publisher.py:173-180`: `socket.setdefaulttimeout()` changes the timeout for the whole process for every thread during the connect. paho 2.1 already has `connect_timeout`. Remove it.
- `core/mqtt_publisher.py:87`: hardcoded broker IP `192.168.31.237`. `core/closed_loop.py:257` `192.168.1.36:9000`. Move both to preferences.
- `adapters/gcode_executor.py:150-154`: sleep loop of up to 3 s (`CLOSE_STOP_WAIT_S`) on the GUI thread during close. Acceptable, but log it.
- `adapters/motion_adapter.py:536-544` and `Ext/sc04e/dll_wrapper.py:513-529`: 0.5 s busy-wait loops on the GUI thread for WCS set/settle. Bounded.
- `core/doc_observer.py:185, 291`: modal `QMessageBox` and session restore run inside `slotRestoredDocument`; `reset_build_plate_and_align` adds objects inside `slotCreatedDocument` (`:147`). Re-entrancy risk. Defer them with `QTimer.singleShot(0, …)`.
- `ui/job_alignment.py:16-18`: imports `matplotlib.pyplot` at module level. **Measured** 10.1 s import with a cold font cache (sandbox). It is imported from `motion_ui.py:577` on the GUI thread.
- `ui/preview_manager.py:887-908` and `core/slicer_callbacks.py:331`: `processEvents()` inside long loops lets the user re-enter the same action.
- `modules/remote_control/fetcher.py:200-233`: per-read timeout only, no total deadline (a slow-drip server keeps it alive); `urlopen` follows redirects after the `check_ssrf` check. Default timeout 120 s (**measured** dead host honours a 5 s override: `DOWNLOAD_TIMEOUT` after 5.3 s; 50 MB local download plus SHA-256 took 0.31 s off the GUI thread).
- `modules/remote_control/local_files.py:26`: `os.walk(JobsDir)` every 5 s with no depth or count cap; the whole list goes out as one retained message.
- `EngineCore/security/security_db.py:211`: password hash compared with `==`. Use `hmac.compare_digest`.
- `io_3mf.py:157, 1164`: `minidom`/`ElementTree` on user-supplied 3MF XML (bandit B318/B314). Consider `defusedxml`.
- `setup_mediamtx.ps1:123-129`: downloads MediaMTX `releases/latest` with no version pin or hash check.
- `with sqlite3.connect(...) as conn:` everywhere (e.g. `machine_runtime_store.py:62`, `sqlite_consumer.py:103,174`, `security_db.py`) commits but does not close; CPython's refcounting closes it in practice.
- Temp `ufameasy_gcode_*.gcd` files (`gcode_executor.py:1408`) are deleted on the next load; after a crash they stay in `%TEMP%`.
- Repo weight: `tools/PrusaSlicer-2.9.6/` is 1,119 tracked files (largest `PrusaSlicer.dll` 96.5 MB); `Icons/search_bg.png` 6.8 MB. Consider Git LFS.
- Junk tracked in the workbench: `Hybrid_am/machine_config.json.bak.20260916_134941`, `temp_matches.txt`, `test_slic3r.py`, `Hybrid_am/Rotary Check.txt`.
- Server: `server/main.py:209-211` `asyncio.create_task(asyncio.to_thread(...))` per request with no reference kept (the task can be GC'd; unbounded backlog). `server/customer_stats.py:111` → `db.py:32` opens the workbench's telemetry DB **read-write** on every customer read to ensure an index. Hardcoded broker `"localhost"` (`mqtt_client.py:480`).
- Server repo junk: tracked `uvicorn.detached.log`, `uvicorn.err.log`, `uvicorn.out.log` (LAN IPs, session IDs), Office lock files `~$AMeasy_Machine_Server_*.docx`, `fix_sessions.py` (one-off DB UPDATE), root `pf_gas_test.nc`, `data/jobs/device_test_edge/manual.pdf`. Server `core/mqtt_publisher.py` is a 16-line duplicate nothing imports.
- Complexity (radon CC > 15): 143 workbench functions; the worst are `nozzle_animator.py:220 GCodeParser.parse_file_simple` (188), `ui/logging_dashboard.py:39 format_payload` (116), `core/slicer_callbacks.py:251 export_to_gcode` (86). Server: 9, worst `server/mqtt_client.py:104 on_message` (57) and `server/routes.py:656 _network_summary` (52). Files over 800 lines: 25 in the workbench (largest `ui/motion_ui.py` 4,886, `nozzle_animator.py` 3,962, `slicer_ui.py` 3,261); server `server/routes.py` 1,346 and `server/db.py` 804.
- Lint totals (ruff, workbench): 465 `try/except: pass`, 56 `except: continue`, 27 bare `except:`, 164 unused imports, 49 unused variables, 35 placeholder-less f-strings, 10 call-in-default, 6 loop-variable closures (`Hybrid_am/gcode_exporter.py:1228-1236`, `slicer_ui.py:1021-1022`, `Laser_hardening/laser_hardening_transform.py:232`). 187 `print(` calls outside tests and scripts. One TODO (`Mod/Start/StartMigrator.py:49`). Server: 7 `except: pass`, 1 bare except (`server/routes.py:1135`), 19 `Depends()` defaults (FastAPI idiom, fine).

---

## 4. Dead code (report only, delete nothing)

Method: vulture ≥ 60 % over all tracked workbench `.py` files. FreeCAD command hooks (`GetResources`/`Activated`/`IsActive`), Qt overrides (`*Event`, `eventFilter`), observer slots (`slot*`) and `on*` callbacks were dropped. Each remaining name was then grepped across every tracked text file (`.py .ui .json .md .ps1 .yml`) for code, string (`"name"`) and test references.

**Whole files with no reference anywhere** (no import, no string, no test). High confidence:
- `Mod/RadialInfillWorkbench/replace_callbacks.py`, `replace_ded.py`, `replace_parser.py`, `replace_remainder.py`: dangerous, see W5
- `Mod/RadialInfillWorkbench/test_slic3r.py`
- `Mod/RadialInfillWorkbench/Hybrid_am/auto_brep_orientation.py`, `Hybrid_am/brep_orientation_tool.py`
- `Mod/RadialInfillWorkbench/core/event_priority.py`
- `Mod/RadialInfillWorkbench/modules/logging_module/logger_consumer.py`, `modules/logging_module/observability_diagnostics.py`
- `Mod/RadialInfillWorkbench/ui/workflow_mode.py`

Only referenced from tests (medium): `core/camera_math.py`, `core/models/machine_conversion.py`, `core/models/pipeline_hooks.py`.

**Functions/classes: 0 code refs, 0 string refs, 0 test refs** (vulture 60 %, medium confidence; FreeCAD macros may call them by hand):

| File | Symbols |
|---|---|
| `Hybrid_am/brep_accuracy_tools.py` | `_face_center_normal`:105, `project_point_to_faces`:126, `smooth_normals`:256, `accuracy_text`:285 |
| `Hybrid_am/co.py` | `_link_raster_lines_into_continuous_path`:647, `_estimate_point_normal`:744 |
| `Hybrid_am/gcode_exporter.py` | `_rotary_conformal_solution`:325, `_find_reference_gcode_for_wrapper`:619, `_apply_reference_gcode_template`:720 |
| `Hybrid_am/machine_collision.py` | `check_gcode_lines`:661 |
| `Hybrid_am/machine_kinematics.py` | `_angle_distance`:333, `print_kinematic_summary`:736, `export_kinematics_to_csv`:771 |
| `Hybrid_am/slicer_gcode_to_csv.py` | `run_slicer_gcode_to_csv_macro`:248 (macro entry, likely run manually) |
| `Hybrid_am/surface_finish_path_generator.py` | `_point_in_polygon_xy`:1685, `_section_radius_loops_from_shape`:1981, `_project_point_and_normal_to_shape`:2099, `_project_loops_to_shape`:2125, `_start_cap_fill_loops`:2171 |
| `Hybrid_am/toolpath_validator.py` | `run_toolpath_validator_macro`:127 (macro entry) |
| `Hybrid_am/universal_hybrid_toolpath_generator.py` | `_load_gcode_exporter_module`:127, `_create_user_guide_settings_image_tab`:232 |
| `commands/plane_commands.py` | `_get_plane_counter`:76 |
| `commands/session_commands.py` | `_ensure_ufam_session_proxy`:21 |
| `core/clc_logging.py` | `clc_log_file`:76 |
| `core/ded_gcode_parser.py` | `format_summary`:336 |
| `core/event_bus.py` | `start_event_bus`:453, `stop_event_bus`:457 (public API, keep) |
| `core/session.py` | `remove_session`:606 |
| `io_3mf.py` | `_apply_plane_visibility`:599, `_apply_plane_state`:613 |
| `modules/ftp_server/ftp_server.py` | `_telemetry_dir`:167 |
| `modules/logging_module/observability_runtime.py` | `restart_observability`:102 |
| `ui/motion_ui.py` | `get_am_controls_panel`:117 (new public RMC API, keep), `createRightTabbedPanel`:3266 |
| `ui/start_page.py` | `_SidebarNavBtn`:466, `_SidebarMenuBtn`:480 (classes), `_format_timestamp`:240 |
| `ui/theme/theme_manager.py` | `_refresh_application_qss`:531, `retheme_toolbar_actions`:554, `retheme_workflow_button`:567, `retheme_start_page`:650, `refresh_main_window_theme`:692 |
| `ui/workflow_mode.py` | `install_in_toolbar`:137 |
| `excel_to_params_json.py` (root) | `schema_hash`:225 |

Flagged by vulture but **referenced**, so not dead: `core/slicer_callbacks.py:185 save_ufam_session` (11 code + 2 string refs), `core/profile_manager.py:43 delete` (23), `core/gcode_processor.py:10 merge_gcode_files` (5), `modules/logging_module/state_hooks.py:114 track_object_modified` (4), `io_3mf.py:294 export_slices_to_3mf`, `:587/593 hide/show_slice_planes`, `:1247 test_3mf_export`, `ui/motion_ui.py:149/2359/2635/3293 create*Panel/removeOldDock` (1–2 refs each, probably comments or old call sites; check by hand).

100 %-confidence unused locals (safe to drop after review): `Hybrid_am/gcode_exporter.py:1155 warmup_ms`, `:1157 optimize_moves`; `Hybrid_am/machine_kinematics.py:242 target_tangent`, `:774 include_diagnostics`; `Hybrid_am/universal_hybrid_toolpath_generator.py:1093 pf_total_rate`; `ui/motion_ui.py:3155 status_msg`; `modules/camera/mjpeg_server.py:177 fmt`. The paho callback parameters in `mqtt_publisher.py:204/222/229` are required by the signature, so they are not dead.

Server: vulture's 60 % hits are FastAPI routes and middleware registered by decorator (false positives). Truly unused: `core/mqtt_publisher.py` (the whole file), `fix_sessions.py`, `server/customer_stats.py:230 _has_active_job`, `:259 _device_last_seen` (0 refs).

---

## 5. Per-file table: workbench (non-test `.py`, 220 files)

Owner is the top `git blame -w HEAD` author with their share of lines. Issues = ruff (F/E722/B/A/S110/S112) + bandit MEDIUM+ + radon CC>15 + manual findings in this report. Worst severity comes from the manual audit when the file has one; otherwise P2 if any static hit.

| Worst | Path | Owner (git blame, top author) | Lines | Issues |
|---|---|---|---:|---:|
| P0 | Mod/RadialInfillWorkbench/ui/motion_ui.py | Sanjaygehlot1 (45%) | 4886 | 79 |
| P0 | Mod/RadialInfillWorkbench/core/slicer_callbacks.py | Sanjaygehlot1 (57%) | 972 | 21 |
| P0 | Mod/RadialInfillWorkbench/core/job_estimator.py | Sanjaygehlot1 (99%) | 1758 | 13 |
| P0 | Mod/RadialInfillWorkbench/adapters/gcode_executor.py | rohanbachhav (47%) | 1531 | 11 |
| P0 | Mod/RadialInfillWorkbench/modules/ftp_server/ftp_server.py | Lavanya Gawas (100%) | 290 | 5 |
| P1 | Mod/EngineCore/base_workbench.py | Sanjaygehlot1 (79%) | 442 | 31 |
| P1 | Mod/RadialInfillWorkbench/modules/camera/camera_recorder.py | Lavanya Gawas (100%) | 894 | 26 |
| P1 | Mod/RadialInfillWorkbench/core/mqtt_publisher.py | Lavanya Gawas (93%) | 538 | 8 |
| P1 | Mod/RadialInfillWorkbench/ui/logging_dashboard.py | Sanjaygehlot1 (97%) | 1141 | 7 |
| P1 | Mod/RadialInfillWorkbench/core/telemetry.py | rohanbachhav (85%) | 292 | 6 |
| P1 | Mod/RadialInfillWorkbench/core/closed_loop.py | rohanbachhav (100%) | 272 | 4 |
| P1 | Mod/RadialInfillWorkbench/modules/camera/mediamtx_publisher.py | Lavanya Gawas (100%) | 380 | 3 |
| P1 | Mod/RadialInfillWorkbench/replace_parser.py | dinosauronthemoon (100%) | 24 | 2 |
| P1 | Mod/RadialInfillWorkbench/modules/remote_control/lifecycle.py | Lavanya Gawas (100%) | 75 | 1 |
| P1 | Mod/RadialInfillWorkbench/replace_callbacks.py | dinosauronthemoon (100%) | 22 | 1 |
| P1 | Mod/RadialInfillWorkbench/replace_ded.py | dinosauronthemoon (100%) | 10 | 1 |
| P1 | Mod/RadialInfillWorkbench/replace_remainder.py | dinosauronthemoon (100%) | 24 | 1 |
| P2 | Mod/RadialInfillWorkbench/nozzle_animator.py | Jithin Shaji (87%) | 3962 | 92 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/surface_finish_path_generator.py | Jithin Shaji (92%) | 2926 | 48 |
| P2 | Mod/RadialInfillWorkbench/slicer_ui.py | Sanjaygehlot1 (66%) | 3261 | 40 |
| P2 | Mod/RadialInfillWorkbench/radial_infill_cmd.py | Jithin Shaji (73%) | 497 | 33 |
| P2 | Mod/RadialInfillWorkbench/io_3mf.py | Jithin Shaji (91%) | 1308 | 31 |
| P2 | Mod/LaserHardeningWorkbench/ui/laser_hardening_ui.py | dinosauronthemoon (99%) | 2961 | 25 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/gcode_exporter.py | Jithin Shaji (54%) | 2281 | 18 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/universal_hybrid_toolpath_generator.py | Jithin Shaji (59%) | 2694 | 18 |
| P2 | Mod/RadialInfillWorkbench/ui/preview_manager.py | dinosauronthemoon (59%) | 1421 | 17 |
| P2 | Mod/RadialInfillWorkbench/core/doc_observer.py | Sanjaygehlot1 (47%) | 343 | 16 |
| P2 | Mod/RadialInfillWorkbench/persistence/session_restore.py | Jithin Shaji (84%) | 381 | 15 |
| P2 | Mod/RadialInfillWorkbench/ufam_project_io.py | Sanjaygehlot1 (100%) | 505 | 15 |
| P2 | Mod/RadialInfillWorkbench/ui/theme/theme_manager.py | Sanjaygehlot1 (99%) | 759 | 15 |
| P2 | Mod/RadialInfillWorkbench/adapters/motion_adapter.py | rohanbachhav (51%) | 961 | 14 |
| P2 | Mod/RadialInfillWorkbench/ui/start_page.py | Sanjaygehlot1 (99%) | 1694 | 14 |
| P2 | Mod/EngineCore/ui/navigation_overlay.py | Sanjaygehlot1 (100%) | 1185 | 12 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/laser_hardening_gcode.py | dinosauronthemoon (100%) | 2123 | 12 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/co.py | Jithin Shaji (93%) | 1531 | 12 |
| P2 | Mod/RadialInfillWorkbench/ui/job_alignment.py | Sanjaygehlot1 (100%) | 935 | 12 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/lh_five_axis_math.py | dinosauronthemoon (100%) | 447 | 11 |
| P2 | Mod/RadialInfillWorkbench/modules/remote_control/fetcher.py | Lavanya Gawas (100%) | 301 | 11 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/brep_normal_projector.py | Jithin Shaji (80%) | 390 | 10 |
| P2 | Mod/RadialInfillWorkbench/commands/slice_commands.py | Jithin Shaji (78%) | 679 | 10 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/brep_orientation_tool.py | Jithin Shaji (98%) | 660 | 9 |
| P2 | Mod/RadialInfillWorkbench/commands/plane_commands.py | Jithin Shaji (99%) | 766 | 9 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/auto_brep_orientation.py | Jithin Shaji (95%) | 327 | 8 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/clad_tracks.py | dinosauronthemoon (100%) | 312 | 8 |
| P2 | Mod/RadialInfillWorkbench/data/machine_runtime_store.py | Jithin Shaji (48%) | 401 | 8 |
| P2 | Mod/RadialInfillWorkbench/modules/remote_control/handler.py | Lavanya Gawas (100%) | 657 | 8 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/machine_kinematics.py | Jithin Shaji (77%) | 872 | 7 |
| P2 | Mod/RadialInfillWorkbench/core/session.py | Jithin Shaji (83%) | 623 | 7 |
| P2 | Mod/RadialInfillWorkbench/modules/remote_control/settings.py | Lavanya Gawas (100%) | 141 | 7 |
| P2 | Mod/RadialInfillWorkbench/persistence/ufam_session.py | Jithin Shaji (100%) | 167 | 7 |
| P2 | Ext/sc04e/engine.py | rohanbachhav (83%) | 1040 | 5 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/brep_accuracy_tools.py | Jithin Shaji (78%) | 300 | 5 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/hybrid_math_utils.py | dinosauronthemoon (100%) | 221 | 5 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/model_strategy_analyzer.py | Jithin Shaji (71%) | 467 | 5 |
| P2 | Mod/RadialInfillWorkbench/core/gcode/ded_builder.py | dinosauronthemoon (100%) | 663 | 5 |
| P2 | Mod/RadialInfillWorkbench/modules/logging_module/logger_consumer.py | unknown (100%) | 206 | 5 |
| P2 | Mod/RadialInfillWorkbench/modules/logging_module/observability_runtime.py | rohanbachhav (79%) | 131 | 5 |
| P2 | Mod/RadialInfillWorkbench/modules/logging_module/sqlite_consumer.py | Sanjaygehlot1 (100%) | 214 | 5 |
| P2 | Ext/PySide/QtGui.py | rohanbachhav (100%) | 3 | 4 |
| P2 | Mod/EngineCore/security/permission_editor.py | Sanjaygehlot1 (100%) | 512 | 4 |
| P2 | Mod/EngineCore/security/security_gating.py | Sanjaygehlot1 (98%) | 535 | 4 |
| P2 | Mod/EngineCore/security/security_manager.py | Sanjaygehlot1 (100%) | 238 | 4 |
| P2 | Mod/LPBFWorkbench/commands/lpbf_image_cmd.py | dinosauronthemoon (98%) | 394 | 4 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/toolpath_preflight_report.py | Jithin Shaji (98%) | 395 | 4 |
| P2 | Mod/RadialInfillWorkbench/commands/camera_cmd.py | Lavanya Gawas (100%) | 609 | 4 |
| P2 | Mod/RadialInfillWorkbench/persistence/session_serializer.py | Jithin Shaji (100%) | 121 | 4 |
| P2 | Mod/RadialInfillWorkbench/session_3mf_cmd.py | Jithin Shaji (56%) | 248 | 4 |
| P2 | Mod/RadialInfillWorkbench/ui/machine_outputs.py | rohanbachhav (100%) | 367 | 4 |
| P2 | Mod/EngineCore/startup_timing.py | rohanbachhav (100%) | 109 | 3 |
| P2 | Mod/LPBFWorkbench/InitGui.py | Sanjaygehlot1 (100%) | 60 | 3 |
| P2 | Mod/LaserHardeningWorkbench/InitGui.py | Sanjaygehlot1 (100%) | 39 | 3 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/laser_hardening_session.py | dinosauronthemoon (100%) | 499 | 3 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/laser_hardening_transform.py | dinosauronthemoon (100%) | 578 | 3 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/lh_view_export.py | dinosauronthemoon (100%) | 216 | 3 |
| P2 | Mod/LaserHardeningWorkbench/ui/lh_seat_dialog.py | dinosauronthemoon (100%) | 178 | 3 |
| P2 | Mod/RadialInfillWorkbench/coin3d_tracker.py | rohanbachhav (100%) | 391 | 3 |
| P2 | Mod/RadialInfillWorkbench/commands/shape_commands.py | Jithin Shaji (90%) | 815 | 3 |
| P2 | Mod/RadialInfillWorkbench/core/ded_gcode_parser.py | dinosauronthemoon (100%) | 353 | 3 |
| P2 | Mod/RadialInfillWorkbench/core/event_bus.py | rohanbachhav (98%) | 470 | 3 |
| P2 | Mod/RadialInfillWorkbench/core/gcode/checkpoint_injector.py | rohanbachhav (78%) | 207 | 3 |
| P2 | Mod/RadialInfillWorkbench/modules/camera/mjpeg_server.py | Lavanya Gawas (100%) | 454 | 3 |
| P2 | Mod/RadialInfillWorkbench/modules/logging_module/interaction_logger.py | Sanjaygehlot1 (96%) | 156 | 3 |
| P2 | Mod/RadialInfillWorkbench/modules/remote_control/local_files.py | Lavanya Gawas (100%) | 159 | 3 |
| P2 | Mod/RadialInfillWorkbench/ui/dependency_installer.py | Lavanya Gawas (100%) | 288 | 3 |
| P2 | Mod/RadialInfillWorkbench/ui/theme/card_style.py | dinosauronthemoon (100%) | 382 | 3 |
| P2 | Ext/PySide/QtWebEngineWidgets.py | rohanbachhav (100%) | 2 | 2 |
| P2 | Ext/PySide/__init__.py | rohanbachhav (100%) | 3 | 2 |
| P2 | Mod/EngineCore/app_lifecycle.py | rohanbachhav (100%) | 145 | 2 |
| P2 | Mod/EngineCore/security/security_db.py | Sanjaygehlot1 (100%) | 336 | 2 |
| P2 | Mod/LPBFWorkbench/utils/lpbf_image_exporter.py | dinosauronthemoon (100%) | 42 | 2 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/laser_hardening_selection.py | dinosauronthemoon (99%) | 140 | 2 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/lh_face_toolpath.py | dinosauronthemoon (100%) | 340 | 2 |
| P2 | Mod/LaserHardeningWorkbench/laser_hardening_cmd.py | Sanjaygehlot1 (100%) | 22 | 2 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/machine_collision.py | dinosauronthemoon (100%) | 674 | 2 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/machine_config.py | Jithin Shaji (50%) | 217 | 2 |
| P2 | Mod/RadialInfillWorkbench/InitGui.py | Jithin Shaji (49%) | 69 | 2 |
| P2 | Mod/RadialInfillWorkbench/core/clc_logging.py | unknown (99%) | 141 | 2 |
| P2 | Mod/RadialInfillWorkbench/core/slicer_spec.py | dinosauronthemoon (69%) | 275 | 2 |
| P2 | Mod/RadialInfillWorkbench/modules/placement.py | Jithin Shaji (98%) | 310 | 2 |
| P2 | Mod/RadialInfillWorkbench/persistence/session_schema.py | Jithin Shaji (100%) | 129 | 2 |
| P2 | Mod/RadialInfillWorkbench/scripts/power_ramp_1pct_every2_layers.py | Jithin Shaji (100%) | 163 | 2 |
| P2 | Mod/RadialInfillWorkbench/ui/live_job_view.py | rohanbachhav (100%) | 189 | 2 |
| P2 | Mod/RadialInfillWorkbench/ui/theme/theme_styles.py | Sanjaygehlot1 (100%) | 811 | 2 |
| P2 | Ext/PySide/QtCore.py | rohanbachhav (100%) | 4 | 1 |
| P2 | Ext/PySide/QtNetwork.py | rohanbachhav (100%) | 1 | 1 |
| P2 | Ext/PySide/QtSvg.py | rohanbachhav (100%) | 1 | 1 |
| P2 | Ext/PySide/QtSvgWidgets.py | rohanbachhav (100%) | 1 | 1 |
| P2 | Ext/PySide/QtUiTools.py | rohanbachhav (100%) | 1 | 1 |
| P2 | Ext/PySide/QtWidgets.py | rohanbachhav (100%) | 1 | 1 |
| P2 | Ext/sc04e/dll_wrapper.py | rohanbachhav (87%) | 582 | 1 |
| P2 | Mod/EngineCore/motion_preferences.py | Rohan (82%) | 229 | 1 |
| P2 | Mod/EngineCore/security/audit_log_ui.py | Sanjaygehlot1 (100%) | 218 | 1 |
| P2 | Mod/EngineCore/security/login_ui.py | Sanjaygehlot1 (100%) | 179 | 1 |
| P2 | Mod/EngineCore/security/permission_registry.py | Sanjaygehlot1 (100%) | 217 | 1 |
| P2 | Mod/EngineCore/security/user_editor.py | Sanjaygehlot1 (100%) | 370 | 1 |
| P2 | Mod/EngineCore/wheel_guard.py | rohanbachhav (100%) | 102 | 1 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/laser_hardening_toolpath.py | dinosauronthemoon (100%) | 84 | 1 |
| P2 | Mod/LaserHardeningWorkbench/Laser_hardening/lh_job_estimator.py | Sanjaygehlot1 (89%) | 160 | 1 |
| P2 | Mod/LaserHardeningWorkbench/ui/lh_estimate_dialog.py | Sanjaygehlot1 (95%) | 81 | 1 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/pivot_calibration.py | dinosauronthemoon (100%) | 707 | 1 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/slicer_gcode_to_csv.py | Jithin Shaji (100%) | 297 | 1 |
| P2 | Mod/RadialInfillWorkbench/Hybrid_am/toolpath_validator.py | Jithin Shaji (100%) | 174 | 1 |
| P2 | Mod/RadialInfillWorkbench/adapters/ini_folder.py | Rohan (100%) | 83 | 1 |
| P2 | Mod/RadialInfillWorkbench/adapters/job_controller.py | rohanbachhav (100%) | 356 | 1 |
| P2 | Mod/RadialInfillWorkbench/commands/session_commands.py | Sanjaygehlot1 (69%) | 393 | 1 |
| P2 | Mod/RadialInfillWorkbench/core/gcode/gcode_file_merger.py | dinosauronthemoon (67%) | 266 | 1 |
| P2 | Mod/RadialInfillWorkbench/core/gcode/slicer_geometry_parser.py | dinosauronthemoon (100%) | 233 | 1 |
| P2 | Mod/RadialInfillWorkbench/core/models/machine_config.py | Lavanya Gawas (70%) | 77 | 1 |
| P2 | Mod/RadialInfillWorkbench/core/models/ownership_registry.py | Lavanya Gawas (71%) | 395 | 1 |
| P2 | Mod/RadialInfillWorkbench/data/machine_data_store.py | Lavanya Gawas (59%) | 290 | 1 |
| P2 | Mod/RadialInfillWorkbench/security/permission_registry.py | dinosauronthemoon (100%) | 215 | 1 |
| P2 | Mod/RadialInfillWorkbench/test_slic3r.py | dinosauronthemoon (100%) | 33 | 1 |
| P2 | Mod/RadialInfillWorkbench/ufam_import.py | Sanjaygehlot1 (100%) | 33 | 1 |
| P2 | Mod/RadialInfillWorkbench/ui/ui_manager.py | Jithin Shaji (53%) | 123 | 1 |
| P2 | Mod/Start/Init.py | Sanjaygehlot1 (100%) | 29 | 1 |
| P2 | Mod/Start/InitGui.py | Sanjaygehlot1 (100%) | 28 | 1 |
| P2 | Mod/Start/Start.py | Sanjaygehlot1 (100%) | 52 | 1 |
| P2 | excel_to_params_json.py | Lavanya Gawas (100%) | 274 | 1 |

82 files had no findings: Ext/freecad/gui/RemoteDebugger.py (87), Ext/lazy_loader/__init__.py (13), Ext/lazy_loader/lazy_loader.py (59), Ext/sc04e/__init__.py (23), Ext/sc04e/config.py (192), Ext/sc04e/types.py (268), Ext/sc04e/utils.py (18), Mod/EngineCore/folder_utils.py (29), Mod/EngineCore/security/__init__.py (14), Mod/EngineCore/security/security_events.py (52), Mod/LaserHardeningWorkbench/Laser_hardening/__init__.py (1), Mod/LaserHardeningWorkbench/Laser_hardening/lh_setup_math.py (414), Mod/LaserHardeningWorkbench/__init__.py (1), Mod/LaserHardeningWorkbench/commands/laser_hardening.py (48), Mod/RadialInfillWorkbench/Hybrid_am/__init__.py (1), Mod/RadialInfillWorkbench/Hybrid_am/shape_bounds.py (40), Mod/RadialInfillWorkbench/Init.py (16), Mod/RadialInfillWorkbench/adapters/__init__.py (6), Mod/RadialInfillWorkbench/adapters/engine_signal_bridge.py (77), Mod/RadialInfillWorkbench/commands/dependency_commands.py (42), Mod/RadialInfillWorkbench/commands/hybrid_commands.py (42), Mod/RadialInfillWorkbench/commands/preview_commands.py (119), Mod/RadialInfillWorkbench/core/__init__.py (18), Mod/RadialInfillWorkbench/core/axis_order.py (101), Mod/RadialInfillWorkbench/core/camera_math.py (49), Mod/RadialInfillWorkbench/core/dependency_installer.py (87), Mod/RadialInfillWorkbench/core/event_priority.py (16), Mod/RadialInfillWorkbench/core/events.py (399), Mod/RadialInfillWorkbench/core/gcode/__init__.py (24), Mod/RadialInfillWorkbench/core/gcode/layer_assembler.py (109), Mod/RadialInfillWorkbench/core/gcode_processor.py (42), Mod/RadialInfillWorkbench/core/height_sensor_client.py (166), Mod/RadialInfillWorkbench/core/layer_splitter.py (171), Mod/RadialInfillWorkbench/core/models/conversion.py (110), Mod/RadialInfillWorkbench/core/models/ded_conversion.py (86), Mod/RadialInfillWorkbench/core/models/ded_process_recipe.py (68), Mod/RadialInfillWorkbench/core/models/machine_conversion.py (39), Mod/RadialInfillWorkbench/core/models/param_keys.py (227), Mod/RadialInfillWorkbench/core/models/pipeline_hooks.py (70), Mod/RadialInfillWorkbench/core/models/preview_state.py (52), Mod/RadialInfillWorkbench/core/models/runtime_state.py (58), Mod/RadialInfillWorkbench/core/models/session_state.py (78), Mod/RadialInfillWorkbench/core/models/slice_recipe.py (95), Mod/RadialInfillWorkbench/core/parameter_registry.py (121), Mod/RadialInfillWorkbench/core/profile_manager.py (47), Mod/RadialInfillWorkbench/core/registry_loader.py (115), Mod/RadialInfillWorkbench/data/__init__.py (20), Mod/RadialInfillWorkbench/data/position_store.py (30), Mod/RadialInfillWorkbench/json_to_code.py (499), Mod/RadialInfillWorkbench/modules/__init__.py (1), Mod/RadialInfillWorkbench/modules/camera/__init__.py (19), Mod/RadialInfillWorkbench/modules/ftp_server/__init__.py (1), Mod/RadialInfillWorkbench/modules/logging_module/observability_diagnostics.py (15), Mod/RadialInfillWorkbench/modules/logging_module/odometer.py (100), Mod/RadialInfillWorkbench/modules/logging_module/state_hooks.py (159), Mod/RadialInfillWorkbench/modules/remote_control/__init__.py (5), Mod/RadialInfillWorkbench/modules/remote_control/contract.py (118), Mod/RadialInfillWorkbench/modules/remote_control/controller.py (101), Mod/RadialInfillWorkbench/modules/remote_control/motion_ui_controller.py (56), Mod/RadialInfillWorkbench/modules/remote_control/widget.py (102), Mod/RadialInfillWorkbench/persistence/__init__.py (14), Mod/RadialInfillWorkbench/scripts/_legacy_slic3r_backups/post_process_using_python.py (34), Mod/RadialInfillWorkbench/scripts/_legacy_slic3r_backups/spiral_pattern.py (97), Mod/RadialInfillWorkbench/scripts/infill_only_rect_grid.py (0), Mod/RadialInfillWorkbench/scripts/post_process_using_python.py (34), Mod/RadialInfillWorkbench/scripts/spiral_pattern.py (97), Mod/RadialInfillWorkbench/scripts/walls_only_shell.py (149), Mod/RadialInfillWorkbench/scripts/wave_pattern.py (128), Mod/RadialInfillWorkbench/tools/perf/measure_freezes.py (80), Mod/RadialInfillWorkbench/tools/perf/measure_operate.py (98), Mod/RadialInfillWorkbench/ui/__init__.py (11), Mod/RadialInfillWorkbench/ui/gcode_editor.py (185), Mod/RadialInfillWorkbench/ui/pivot_calibration_panel.py (731), Mod/RadialInfillWorkbench/ui/theme/__init__.py (28), Mod/RadialInfillWorkbench/ui/theme/theme_icons.py (198), Mod/RadialInfillWorkbench/ui/workflow_mode.py (166), Mod/RadialInfillWorkbench/utils/__init__.py (1), Mod/RadialInfillWorkbench/utils/pose.py (71), Mod/Start/StartGui.py (63), Mod/Start/StartMigrator.py (125), backfill_param_ids.py (41), watch_params_excel.py (92)

Tracked files that are not Python and need attention: `Ext/sc04e/SC04ECtrlDll.dll` (modified binary), `UFAMeasy_Master_Parameters.xlsx`, `Hybrid_am/machine_config.json.bak.20260916_134941`, `temp_matches.txt`, `tools/PrusaSlicer-2.9.6/**` (1,119 files).

---

## 6. Safe tiny fixes vs. changes that need the owner's approval

### Safe tiny fixes (one line each; behaviour unchanged)
| File:line | Change | Why it is safe | Owner |
|---|---|---|---|
| `modules/remote_control/fetcher.py:20` | `from typing import Callable` → `from typing import Any, Callable` | Fixes F821 `Any` at `:98`, which is an annotation only (`from __future__ import annotations`) | you (RMC) |
| `core/models/ownership_registry.py:183` | drop the **first** duplicate `"--layer-gcode"` key (F601) | a dict literal keeps the last value; check that both values match before deleting | Lavanya Gawas (71 %), rohanbachhav |
| `ui/theme/theme_manager.py:45` | remove the inner `import weakref` (F811) | already imported at `:3` | Sanjaygehlot1 |
| `core/doc_observer.py:57, 91` | remove the inner re-imports of `add_preview_from_path` / `apply_session_dict` (F811) | same objects as the top-level imports | Sanjaygehlot1 / Jithin Shaji |
| `EngineCore/base_workbench.py:282` | `"...{e}\\n"` → `"...{e}\n"` | only the console text changes (no literal `\n` printed) | Sanjaygehlot1 |
| 35 × F541 (ruff list) | drop `f` prefix from f-strings without placeholders | identical string | various |
| `server/rmc_service.py:25-26` | drop unused `rmc_topic`, `CMD_SEGMENT` from the import | module already imported for other names | you |
| server `tests/test_rmc_phase6a.py:5,9,19,22`, `tests/network_report/http_probe.py:5`, `load_probe.py:8` | drop unused imports | test-only, stdlib/own modules | you |
| server `.gitignore` | add `~$*`, `uvicorn*.log`, `REVIEW.md` | no runtime effect (then `git rm --cached` the tracked logs and lock files) | you |

### Needs the owner's approval (changes behaviour or touches someone else's module)
- B1/B2 `ui/motion_ui.py` (you, cleared on 2026-10-30; tell Sanjaygehlot1 and rohanbachhav).
- B3 `modules/remote_control/lifecycle.py:9` (you).
- B4 `core/slicer_callbacks.py` (Sanjaygehlot1, dinosauronthemoon). B5 `adapters/gcode_executor.py` (rohanbachhav), `core/job_estimator.py` (Sanjaygehlot1).
- B6 `modules/ftp_server/ftp_server.py` (blamed to you, but not RMC: confirm the FTP root and auth with the team that uses `/logs` on the server).
- B7 `EngineCore/base_workbench.py` (Sanjaygehlot1).
- W1/W2/W9 `core/mqtt_publisher.py` (you, cleared on 2026-10-30). W3 `core/telemetry.py` (rohanbachhav). W4 camera/MediaMTX (you). W5 the `replace_*.py` deletions (dinosauronthemoon). W6 test update (you). W7 `logging_dashboard.py` / `logging_module/sqlite_consumer.py` (Sanjaygehlot1; logging_module is someone else's module).
- Removing any of the 164 unused imports: an import can have side effects (camera/mjpeg modules write `sys.modules` aliases), so do it per module with its owner.
- All server P1s (S1–S5): your repo, but they change runtime behaviour, so test them first.

---

## 7. Pre-push checklist

```powershell
# --- Workbench (D:\UFAMeasy_Workspace\UFAMeasy) ---
git status --short                       # expect only the files you mean to commit; NOT Ext/sc04e/SC04ECtrlDll.dll
git diff --cached --stat
git grep -nE '^(<<<<<<<|=======$|>>>>>>>)' -- '*.py'          # must print nothing
git diff --cached --name-only -- '*.py' | % { .\bin\python.exe -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" $_ }   # every staged .py parses
# secret / junk scan of what you are about to commit
git diff --cached --name-only | Select-String -Pattern '\.(env|db|sqlite|u5d|u5log|pem|key|bak|orig|log)$|~\$'
git diff --cached | Select-String -Pattern '(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*["''][^"'']{6,}'
# unit tests (sandbox HOME so nothing touches your real ~/.ufameasy)
$env:UFAMEASY_MQTT_HOST='127.0.0.1'; $env:UFAMEASY_MQTT_PORT='1'
cd Mod; ..\bin\python.exe -m pytest -q -p no:cacheprovider RadialInfillWorkbench\modules\remote_control\tests RadialInfillWorkbench\tests\test_mqtt_publisher.py RadialInfillWorkbench\tests\test_mediamtx_publisher.py; cd ..
#   today: 55 passed, 1 failed (W6)

# --- Machine Server (D:\UFAMeasy_Machine_Server) ---
git status --short
$env:CUSTOMER_MODE='0'; .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests --ignore=tests\network_report
#   today: test_customer_api.py fails to collect (httpx2); otherwise 42 passed / 6 failed (same 6 as at 05543bd)
```

**FreeCAD smoke test** (manual; I could not drive the GUI):
1. Start UFAMeasy with Task Manager open. Note the `FreeCAD.exe` PID and `ffmpeg`/`mediamtx` processes.
2. Activate Unnati DED. The Device ID prompt shows on a fresh profile; Cancel must not trap you (B7).
3. Open Operate / AM Controls. The RMC widget appears at the top of the Job panel. Browse a G-code file: the dashboard shows "loaded" (B2). Time how long the GUI is frozen (B5).
4. Switch to Part, then back to Unnati DED, twice. No duplicate toolbars and no second RMC widget. `netstat -ano | findstr :1883` shows **one** ESTABLISHED connection for the FreeCAD PID (B3 today shows two).
5. Stop Mosquitto, wait 10 s, start it again. FreeCAD stays responsive and reconnects; `mosquitto_sub -t 'ufameasy/+/rmc/state' -v` shows a fresh state.
6. Start a camera recording or live stream, then close FreeCAD. Within 10 s **no** `FreeCAD.exe`, `ffmpeg.exe` or `mediamtx.exe` is left (W4), `~/.ufameasy/logs/shutdown.log` lists every step as "done", and the broker shows the retained offline state at once rather than after 90 s (W9).
7. From another PC: `ftp <machine-ip> 2121`, log in as anonymous, `ls security`. This must fail once B6 is fixed.

---

## 8. What I could not check, and why

| Item | Status | Reason |
|---|---|---|
| FreeCAD GUI run (activate/switch/close, real Qt event loop, observers, QTimer re-entrancy) | SKIPPED | no GUI automation here; covered by the manual smoke test above |
| `python -X dev -W error` inside FreeCADCmd | PARTIAL | FreeCADCmd ignores `PYTHONDEVMODE` (`sys.flags.dev_mode == False`). Instead every module was imported with `warnings.simplefilter("always")` captured: **0 warnings from UFAMeasy code**, 3 from third-party packages |
| Headless import of all 196 workbench modules (FreeCADCmd 1.x, Python 3.12.10) | DONE | 182 ok; 6 `AttributeError` that need the GUI (`FreeCADGui.Workbench/addCommand/getMainWindow` missing in Cmd mode, expected); 1 `PySide6.QtWebEngineWidgets` not installed; 4 `Hybrid_am.*` need `Hybrid_am/` on `sys.path` (set by the workbench at runtime); 3 `replace_*` `FileNotFoundError` (W5). No module started a thread, opened a socket or touched SQLite at import |
| `tests/test_customer_api.py` | SKIPPED | needs `httpx2`; installing it into a scratch folder was denied by the permission policy, and I did not touch your `.venv` |
| Rest of the workbench test suite (`Mod/RadialInfillWorkbench/tests`, 27 files) | NOT RUN | only the RMC, MQTT and MediaMTX tests (56) were run, to stay inside this branch's change set |
| Dead FTP **client** in the workbench | N/A | the workbench only runs an FTP *server*; its HTTP fetcher was tested against a dead host instead. The server's FTP client was tested: 10.0 s timeout honoured |
| py-spy on the real server | DONE (sandbox) | sampled the repo's own `isolated_server.py` (MQTT stubbed, sandbox DB with a 15 MB telemetry copy) to keep clear of your live Mosquitto (port 1883 is open on this PC) |
| Deadlock analysis | STATIC ONLY | lock pairs checked (publisher `_queue_lock`/paho internals, RMC `RLock`s, `app_lifecycle._lock`); no inverse lock order found, so nothing to reproduce |
| Dead-code calls by FreeCAD macros or the user's own scripts | UNKNOWN | vulture plus grep cannot see calls from outside the repo |
| Exact CPU cost of W3 inside FreeCAD | NOT MEASURED | spin rate measured in the harness only |
| Other branches (`rohan/*`, `Sanjay/*`, `fix/mqtt-lagging`) | OUT OF SCOPE | reviewed only `lavanya/rmc-local` vs `main` and the server `main` |

### Tools and versions
Workbench runtime: FreeCAD (bundled `bin/FreeCADCmd.exe`), Python 3.12.10, PySide6 6.8.3, paho-mqtt 2.1.0. Server: Python 3.13.1, FastAPI 0.137.2, starlette 1.3.1, uvicorn 0.49.0, paho-mqtt 2.1.0, pytest 9.2.0. Analysis (scratch venv, not installed into either project): ruff 0.17.5, vulture 2.16, bandit 1.9.4, radon 6.0.1, py-spy 0.4.3.

### Files created
- `D:\UFAMeasy_Machine_Server\REVIEW.md`: this report. **Untracked**; add it to `.gitignore` or commit it deliberately.
- Scratch evidence (outside both repos): raw tool outputs, harness scripts and results under `C:\Users\LAVANYA\AppData\Local\Temp\claude\dad138efb7aecb58\1691d3e2-9747-45f9-b59f-9c873f85e212\scratchpad\`, e.g. `ruff_wb.txt`, `bandit_wb.csv`, `radon_wb.json`, `vulture_wb.txt`, `import_results.json`, `hang_results.json.log`, `pyspy_raw.txt`, `pytest_srv2.txt`, `wb_pytest.txt`, and the 50 MB test file `gdata\big_50mb.gcode`.
