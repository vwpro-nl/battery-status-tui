"""SQLite sample history and charging-session persistence."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from .models import Measurement, RawBatterySnapshot, Session, SleepInterval


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
CREATE TABLE IF NOT EXISTS battery_samples (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    device TEXT NOT NULL,
    identity TEXT NOT NULL,
    state TEXT NOT NULL,
    percentage REAL NOT NULL,
    monotonic_s REAL NOT NULL,
    boottime_s REAL NOT NULL,
    boot_id TEXT NOT NULL,
    power_now_w REAL, current_now_a REAL, voltage_now_v REAL,
    energy_now_wh REAL, energy_full_wh REAL, energy_full_design_wh REAL,
    charge_now_ah REAL, charge_full_ah REAL, charge_full_design_ah REAL,
    upower_energy_rate_w REAL,
    UNIQUE(timestamp, device)
);
CREATE INDEX IF NOT EXISTS battery_samples_identity_time_idx ON battery_samples(identity, timestamp);
CREATE TABLE IF NOT EXISTS sleep_intervals (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    ended_at INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'sleep',
    source TEXT NOT NULL,
    boot_id TEXT,
    pre_percentage REAL,
    post_percentage REAL,
    UNIQUE(started_at, ended_at)
);
"""


def default_database_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "battery-status-tui" / "history.sqlite3"


class Storage:
    def __init__(self, path: Path | None = None):
        self.path = path or default_database_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        self._migrate(connection)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        columns = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
        additions = {
            "power_method": "TEXT NOT NULL DEFAULT 'unavailable'", "power_approximate": "INTEGER NOT NULL DEFAULT 0",
            "power_confidence": "TEXT NOT NULL DEFAULT 'none'", "power_window_s": "REAL", "energy_wh": "REAL",
            "energy_full_wh": "REAL", "energy_full_design_wh": "REAL", "charge_ah": "REAL",
            "charge_full_ah": "REAL", "charge_full_design_ah": "REAL", "monotonic_s": "REAL",
            "boottime_s": "REAL", "boot_id": "TEXT", "battery_identity": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE samples ADD COLUMN {name} {declaration}")
        db.execute("PRAGMA user_version = 2")

    def record(self, measurement: Measurement) -> int | None:
        with self.connect() as db:
            active = db.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            kind = measurement.session_kind
            session_id: int | None = None
            previous = db.execute("SELECT battery_identity FROM samples ORDER BY timestamp DESC LIMIT 1").fetchone()
            identity_changed = previous is not None and previous["battery_identity"] and measurement.battery_identity and previous["battery_identity"] != measurement.battery_identity
            if active is not None and identity_changed:
                db.execute("UPDATE sessions SET ended_at = ?, end_percentage = ?, end_reason = 'battery-change' WHERE id = ?",
                           (measurement.timestamp, measurement.percentage, active["id"]))
                active = None
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
                    voltage_v, current_a, upower_remaining_s, source, device,
                    power_method, power_approximate, power_confidence, power_window_s,
                    energy_wh, energy_full_wh, energy_full_design_wh, charge_ah,
                    charge_full_ah, charge_full_design_ah, monotonic_s, boottime_s,
                    boot_id, battery_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(timestamp, device) DO UPDATE SET
                    session_id=excluded.session_id, percentage=excluded.percentage,
                    state=excluded.state, ac_online=excluded.ac_online,
                    power_w=excluded.power_w, voltage_v=excluded.voltage_v,
                    current_a=excluded.current_a,
                    upower_remaining_s=excluded.upower_remaining_s,
                    source=excluded.source, power_method=excluded.power_method,
                    power_approximate=excluded.power_approximate,
                    power_confidence=excluded.power_confidence, power_window_s=excluded.power_window_s
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
                    measurement.power_method, int(measurement.power_approximate), measurement.power_confidence,
                    measurement.power_window_s, measurement.energy_wh, measurement.energy_full_wh,
                    measurement.energy_full_design_wh, measurement.charge_ah, measurement.charge_full_ah,
                    measurement.charge_full_design_ah, measurement.monotonic_s, measurement.boottime_s,
                    measurement.boot_id, measurement.battery_identity,
                ),
            )
            for item in measurement.raw_batteries:
                db.execute("""
                    INSERT INTO battery_samples(timestamp, device, identity, state, percentage, monotonic_s,
                        boottime_s, boot_id, power_now_w, current_now_a, voltage_now_v, energy_now_wh,
                        energy_full_wh, energy_full_design_wh, charge_now_ah, charge_full_ah,
                        charge_full_design_ah, upower_energy_rate_w)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(timestamp, device) DO UPDATE SET identity=excluded.identity,
                        state=excluded.state, percentage=excluded.percentage, power_now_w=excluded.power_now_w,
                        current_now_a=excluded.current_now_a, voltage_now_v=excluded.voltage_now_v,
                        energy_now_wh=excluded.energy_now_wh, charge_now_ah=excluded.charge_now_ah,
                        upower_energy_rate_w=excluded.upower_energy_rate_w
                """, (item.timestamp, item.device, item.identity, item.state, item.percentage, item.monotonic_s,
                    item.boottime_s, item.boot_id, item.power_now_w, item.current_now_a, item.voltage_now_v,
                    item.energy_now_wh, item.energy_full_wh, item.energy_full_design_wh, item.charge_now_ah,
                    item.charge_full_ah, item.charge_full_design_ah, item.upower_energy_rate_w))
            return session_id

    def raw_samples_since(self, timestamp: int) -> tuple[RawBatterySnapshot, ...]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM battery_samples WHERE timestamp >= ? ORDER BY timestamp", (timestamp,)).fetchall()
        return tuple(RawBatterySnapshot(row["timestamp"], row["monotonic_s"], row["boottime_s"], row["boot_id"],
            row["device"], row["identity"], row["percentage"], row["state"], None,
            power_now_w=row["power_now_w"], current_now_a=row["current_now_a"], voltage_now_v=row["voltage_now_v"],
            energy_now_wh=row["energy_now_wh"], energy_full_wh=row["energy_full_wh"],
            energy_full_design_wh=row["energy_full_design_wh"], charge_now_ah=row["charge_now_ah"],
            charge_full_ah=row["charge_full_ah"], charge_full_design_ah=row["charge_full_design_ah"],
            upower_energy_rate_w=row["upower_energy_rate_w"]) for row in rows)

    def record_sleep(self, interval: SleepInterval) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO sleep_intervals(started_at, ended_at, kind, source, boot_id,
                pre_percentage, post_percentage) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(started_at, ended_at) DO UPDATE SET source=excluded.source,
                pre_percentage=COALESCE(excluded.pre_percentage, pre_percentage),
                post_percentage=COALESCE(excluded.post_percentage, post_percentage)""",
                (interval.started_at, interval.ended_at, interval.kind, interval.source, interval.boot_id,
                 interval.pre_percentage, interval.post_percentage))

    def sleep_intervals_since(self, timestamp: int) -> list[SleepInterval]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sleep_intervals WHERE ended_at >= ? ORDER BY started_at", (timestamp,)).fetchall()
        return [SleepInterval(row["started_at"], row["ended_at"], row["kind"], row["source"], row["boot_id"],
                              row["pre_percentage"], row["post_percentage"]) for row in rows]

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

    def metadata_int(self, key: str) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except ValueError:
            return None

    def set_metadata_int(self, key: str, value: int) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

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
            source=row["source"], device=row["device"], power_method=row["power_method"],
            power_approximate=bool(row["power_approximate"]), power_confidence=row["power_confidence"],
            power_window_s=row["power_window_s"], energy_wh=row["energy_wh"], energy_full_wh=row["energy_full_wh"],
            energy_full_design_wh=row["energy_full_design_wh"], charge_ah=row["charge_ah"],
            charge_full_ah=row["charge_full_ah"], charge_full_design_ah=row["charge_full_design_ah"],
            monotonic_s=row["monotonic_s"], boottime_s=row["boottime_s"], boot_id=row["boot_id"],
            battery_identity=row["battery_identity"],
        )
