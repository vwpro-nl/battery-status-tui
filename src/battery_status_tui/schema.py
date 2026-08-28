"""Versioned SQLite schema definitions.

Schema versions are independent from the application version.  Version 2 is
the current production schema; versions 3 and 4 are reserved for the staged
v1.0 storage migration.
"""

from __future__ import annotations


CURRENT_SCHEMA_VERSION = 2
PLANNED_SCHEMA_VERSIONS = (3, 4)

V2_CREATE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('charging', 'discharging')),
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        start_percentage REAL NOT NULL,
        end_percentage REAL,
        end_reason TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS samples (
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
        power_method TEXT NOT NULL DEFAULT 'unavailable',
        power_approximate INTEGER NOT NULL DEFAULT 0,
        power_confidence TEXT NOT NULL DEFAULT 'none',
        power_window_s REAL,
        energy_wh REAL,
        energy_full_wh REAL,
        energy_full_design_wh REAL,
        charge_ah REAL,
        charge_full_ah REAL,
        charge_full_design_ah REAL,
        monotonic_s REAL,
        boottime_s REAL,
        boot_id TEXT,
        battery_identity TEXT,
        UNIQUE(timestamp, device)
    )""",
    "CREATE INDEX IF NOT EXISTS samples_timestamp_idx ON samples(timestamp)",
    "CREATE INDEX IF NOT EXISTS samples_session_idx ON samples(session_id, timestamp)",
    """CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS battery_samples (
        id INTEGER PRIMARY KEY,
        timestamp INTEGER NOT NULL,
        device TEXT NOT NULL,
        identity TEXT NOT NULL,
        state TEXT NOT NULL,
        percentage REAL NOT NULL,
        monotonic_s REAL NOT NULL,
        boottime_s REAL NOT NULL,
        boot_id TEXT NOT NULL,
        power_now_w REAL,
        current_now_a REAL,
        voltage_now_v REAL,
        energy_now_wh REAL,
        energy_full_wh REAL,
        energy_full_design_wh REAL,
        charge_now_ah REAL,
        charge_full_ah REAL,
        charge_full_design_ah REAL,
        upower_energy_rate_w REAL,
        UNIQUE(timestamp, device)
    )""",
    """CREATE INDEX IF NOT EXISTS battery_samples_identity_time_idx
        ON battery_samples(identity, timestamp)""",
    """CREATE TABLE IF NOT EXISTS sleep_intervals (
        id INTEGER PRIMARY KEY,
        started_at INTEGER NOT NULL,
        ended_at INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'sleep',
        source TEXT NOT NULL,
        boot_id TEXT,
        pre_percentage REAL,
        post_percentage REAL,
        UNIQUE(started_at, ended_at)
    )""",
)

V2_SAMPLE_ADDITIONS = {
    "power_method": "TEXT NOT NULL DEFAULT 'unavailable'",
    "power_approximate": "INTEGER NOT NULL DEFAULT 0",
    "power_confidence": "TEXT NOT NULL DEFAULT 'none'",
    "power_window_s": "REAL",
    "energy_wh": "REAL",
    "energy_full_wh": "REAL",
    "energy_full_design_wh": "REAL",
    "charge_ah": "REAL",
    "charge_full_ah": "REAL",
    "charge_full_design_ah": "REAL",
    "monotonic_s": "REAL",
    "boottime_s": "REAL",
    "boot_id": "TEXT",
    "battery_identity": "TEXT",
}

V2_REQUIRED_TABLES = frozenset({
    "sessions", "samples", "metadata", "battery_samples", "sleep_intervals",
})

