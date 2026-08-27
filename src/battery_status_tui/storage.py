"""SQLite sample history and charging-session persistence."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .models import Measurement, Session


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('charging', 'discharging')),
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    start_percentage REAL NOT NULL,
    end_percentage REAL,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    percentage REAL NOT NULL,
    state TEXT NOT NULL,
    ac_online INTEGER,
    power_w REAL,
    voltage_v REAL,
    current_a REAL,
    upower_remaining_s INTEGER,
    source TEXT NOT NULL,
    device TEXT NOT NULL,
    UNIQUE(timestamp, device)
);
CREATE INDEX IF NOT EXISTS samples_timestamp_idx ON samples(timestamp);
CREATE INDEX IF NOT EXISTS samples_session_idx ON samples(session_id, timestamp);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


def default_database_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "battery-status-tui" / "history.sqlite3"


class Storage:
    def __init__(self, path: Path | None = None):
        self.path = path or default_database_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        return connection

    def record(self, measurement: Measurement) -> int | None:
        with self.connect() as db:
            active = db.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            kind = measurement.session_kind
            session_id: int | None = None
            if active is not None and active["kind"] != kind:
                db.execute(
                    "UPDATE sessions SET ended_at = ?, end_percentage = ?, end_reason = ? WHERE id = ?",
                    (measurement.timestamp, measurement.percentage, kind or measurement.state, active["id"]),
                )
                active = None
            if kind is not None:
                if active is None:
                    cursor = db.execute(
                        "INSERT INTO sessions(kind, started_at, start_percentage) VALUES (?, ?, ?)",
                        (kind, measurement.timestamp, measurement.percentage),
                    )
                    session_id = int(cursor.lastrowid)
                else:
                    session_id = int(active["id"])
            db.execute(
                """
                INSERT INTO samples(
                    timestamp, session_id, percentage, state, ac_online, power_w,
                    voltage_v, current_a, upower_remaining_s, source, device
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp, device) DO UPDATE SET
                    session_id=excluded.session_id, percentage=excluded.percentage,
                    state=excluded.state, ac_online=excluded.ac_online,
                    power_w=excluded.power_w, voltage_v=excluded.voltage_v,
                    current_a=excluded.current_a,
                    upower_remaining_s=excluded.upower_remaining_s,
                    source=excluded.source
                """,
                (
                    measurement.timestamp,
                    session_id,
                    measurement.percentage,
                    measurement.state,
                    None if measurement.ac_online is None else int(measurement.ac_online),
                    measurement.power_w,
                    measurement.voltage_v,
                    measurement.current_a,
                    measurement.remaining_seconds,
                    measurement.source,
                    measurement.device,
                ),
            )
            return session_id

    def current_session(self) -> Session | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._session(row) if row else None

    def samples_since(self, timestamp: int, session_id: int | None = None) -> list[Measurement]:
        query = "SELECT * FROM samples WHERE timestamp >= ?"
        parameters: list[int] = [timestamp]
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        query += " ORDER BY timestamp"
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [self._measurement(row) for row in rows]

    def latest(self) -> Measurement | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM samples ORDER BY timestamp DESC LIMIT 1").fetchone()
        return self._measurement(row) if row else None

    def prune(self, before: int | None = None) -> int:
        cutoff = before if before is not None else int(time.time()) - 30 * 86400
        with self.connect() as db:
            cursor = db.execute("DELETE FROM samples WHERE timestamp < ?", (cutoff,))
            return cursor.rowcount

    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"], kind=row["kind"], started_at=row["started_at"],
            ended_at=row["ended_at"], start_percentage=row["start_percentage"],
            end_percentage=row["end_percentage"],
        )

    @staticmethod
    def _measurement(row: sqlite3.Row) -> Measurement:
        return Measurement(
            timestamp=row["timestamp"], percentage=row["percentage"], state=row["state"],
            ac_online=None if row["ac_online"] is None else bool(row["ac_online"]),
            power_w=row["power_w"], voltage_v=row["voltage_v"], current_a=row["current_a"],
            time_to_full_s=row["upower_remaining_s"] if row["state"] == "charging" else None,
            time_to_empty_s=row["upower_remaining_s"] if row["state"] == "discharging" else None,
            source=row["source"], device=row["device"],
        )
