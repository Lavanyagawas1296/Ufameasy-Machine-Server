import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "data" / "params.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS param_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slice_idx TEXT NOT NULL,
                param_id TEXT NOT NULL,
                field TEXT,
                domain TEXT,
                value TEXT,
                unit TEXT,
                timestamp TEXT,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT,
                last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                device_id TEXT,
                file_name TEXT,
                total_layers INT,
                status TEXT,
                started_at TEXT,
                ended_at TEXT,
                FOREIGN KEY (device_id) REFERENCES devices (device_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slice_data (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                slice_index INT,
                params_json TEXT,
                recorded_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_log (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                current_layer INT,
                execution_state TEXT,
                laser_on INT,
                gas_enabled INT,
                laser_power REAL,
                laser_power_pct REAL,
                machine_connected INT,
                connection_state TEXT,
                powder_enabled INT,
                shielding_enabled INT,
                light_enabled INT,
                feeder_last_purge TEXT,
                elapsed_seconds REAL,
                estimated_seconds REAL,
                remaining_seconds REAL,
                feed_override_pct REAL,
                working_distance_mm REAL,
                spot_size_um REAL,
                melt_pool_high REAL,
                melt_pool_low REAL,
                closed_loop_control INT,
                post_processor TEXT,
                enable_gcode_arcs INT,
                include_gcode_comments INT,
                jog_speed_pct REAL,
                rotary_speed_pct REAL,
                jog_step_mm REAL,
                MAX_LASER_POWER REAL,
                progress_pct REAL,
                xma TEXT,
                xmr TEXT,
                time_remaining TEXT,
                recorded_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
            """
        )


def register_device(device_id, name):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO devices (device_id, name, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                name = excluded.name,
                last_seen = excluded.last_seen
            """,
            (device_id, name, now),
        )


def create_session(session_id, device_id, file_name, total_layers):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                session_id,
                device_id,
                file_name,
                total_layers,
                status,
                started_at,
                ended_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, device_id, file_name, total_layers, "running", now, None),
        )


def close_session(session_id, status):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE sessions
            SET status = ?, ended_at = ?
            WHERE session_id = ?
            """,
            (status, now, session_id),
        )


def update_session_file_name(session_id: str, file_name: str):
    """Persist the gcode file name on the active session row."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET file_name = ? WHERE session_id = ?",
            (file_name, session_id),
        )


def update_session_total_layers(session_id: str, total_layers: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE sessions SET total_layers = ? WHERE session_id = ?",
            (total_layers, session_id),
        )


def insert_slice_data(session_id, slice_index, params_dict):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO slice_data (
                session_id,
                slice_index,
                params_json,
                recorded_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (session_id, slice_index, json.dumps(params_dict), now),
        )


def insert_runtime_log(session_id, runtime_dict):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO runtime_log (
                session_id,
                current_layer,
                execution_state,
                laser_on,
                gas_enabled,
                laser_power,
                laser_power_pct,
                machine_connected,
                connection_state,
                powder_enabled,
                shielding_enabled,
                light_enabled,
                feeder_last_purge,
                elapsed_seconds,
                estimated_seconds,
                remaining_seconds,
                feed_override_pct,
                working_distance_mm,
                spot_size_um,
                melt_pool_high,
                melt_pool_low,
                closed_loop_control,
                post_processor,
                enable_gcode_arcs,
                include_gcode_comments,
                jog_speed_pct,
                rotary_speed_pct,
                jog_step_mm,
                MAX_LASER_POWER,
                progress_pct,
                xma,
                xmr,
                time_remaining,
                recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                runtime_dict.get("current_layer"),
                runtime_dict.get("execution_state"),
                runtime_dict.get("laser_on"),
                runtime_dict.get("gas_enabled"),
                runtime_dict.get("laser_power"),
                runtime_dict.get("laser_power_pct"),
                runtime_dict.get("machine_connected"),
                runtime_dict.get("connection_state"),
                runtime_dict.get("powder_enabled"),
                runtime_dict.get("shielding_enabled"),
                runtime_dict.get("light_enabled"),
                runtime_dict.get("feeder_last_purge"),
                runtime_dict.get("elapsed_seconds"),
                runtime_dict.get("estimated_seconds"),
                runtime_dict.get("remaining_seconds"),
                runtime_dict.get("feed_override_pct"),
                runtime_dict.get("working_distance_mm"),
                runtime_dict.get("spot_size_um"),
                runtime_dict.get("melt_pool_high"),
                runtime_dict.get("melt_pool_low"),
                runtime_dict.get("closed_loop_control"),
                runtime_dict.get("post_processor"),
                runtime_dict.get("enable_gcode_arcs"),
                runtime_dict.get("include_gcode_comments"),
                runtime_dict.get("jog_speed_pct"),
                runtime_dict.get("rotary_speed_pct"),
                runtime_dict.get("jog_step_mm"),
                runtime_dict.get("MAX_LASER_POWER"),
                runtime_dict.get("progress_pct"),
                json.dumps(runtime_dict.get("xma")),
                json.dumps(runtime_dict.get("xmr")),
                runtime_dict.get("time_remaining"),
                now,
            ),
        )


def get_sessions_by_device(device_id):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                session_id,
                device_id,
                file_name,
                total_layers,
                status,
                started_at,
                ended_at
            FROM sessions
            WHERE device_id = ?
            ORDER BY started_at DESC
            """,
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_session_by_id(session_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else {}


def get_running_sessions() -> list[dict]:
    """Return the most recent running session per device, for restoring state after restart."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s1.*
            FROM sessions s1
            INNER JOIN (
                SELECT device_id, MAX(started_at) AS max_start
                FROM sessions
                WHERE status = 'running'
                GROUP BY device_id
            ) s2 ON s1.device_id = s2.device_id AND s1.started_at = s2.max_start
            WHERE s1.status = 'running'
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_runtime_latest(session_id):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                session_id,
                current_layer,
                execution_state,
                laser_on,
                gas_enabled,
                laser_power,
                xma,
                xmr,
                time_remaining,
                recorded_at
            FROM runtime_log
            WHERE session_id = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row is not None else None
