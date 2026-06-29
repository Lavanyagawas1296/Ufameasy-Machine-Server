import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "data" / "params.db"


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
                xma,
                xmr,
                time_remaining,
                recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                runtime_dict.get("current_layer"),
                runtime_dict.get("execution_state"),
                runtime_dict.get("laser_on"),
                runtime_dict.get("gas_enabled"),
                runtime_dict.get("laser_power"),
                json.dumps(runtime_dict.get("xma")),
                json.dumps(runtime_dict.get("xmr")),
                runtime_dict.get("time_remaining"),
                now,
            ),
        )


def get_sessions_by_device(device_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
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


def get_runtime_latest(session_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
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
