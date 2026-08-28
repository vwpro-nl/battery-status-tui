"""Versioned SQLite schema definitions.

Schema versions are independent from the application version.  Version 2 is
the current production schema; versions 3 and 4 are reserved for the staged
v1.0 storage migration.
"""

from __future__ import annotations


CURRENT_SCHEMA_VERSION = 2
PLANNED_SCHEMA_VERSIONS = (3, 4)
V1_SCHEMA_VERSION = 4

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


# The locked v1 schema is defined here for isolated creation and contract
# tests.  It is deliberately not wired into the v2 migration runner yet.
V1_CREATE_STATEMENTS = (
    """CREATE TABLE batteries (
        id INTEGER PRIMARY KEY,
        identity TEXT NOT NULL UNIQUE,
        native_name TEXT,
        manufacturer TEXT,
        model TEXT,
        serial_hash TEXT,
        first_seen_ms INTEGER NOT NULL,
        last_seen_ms INTEGER NOT NULL,
        CHECK(first_seen_ms >= 0),
        CHECK(last_seen_ms >= first_seen_ms)
    )""",
    """CREATE TABLE state_events (
        id INTEGER PRIMARY KEY,
        occurred_at_ms INTEGER NOT NULL,
        boot_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('system', 'battery')),
        battery_id INTEGER REFERENCES batteries(id),
        ac_online INTEGER CHECK(ac_online IN (0, 1) OR ac_online IS NULL),
        power_profile TEXT,
        battery_present INTEGER CHECK(battery_present IN (0, 1) OR battery_present IS NULL),
        battery_state TEXT,
        soc_percent REAL CHECK(soc_percent BETWEEN 0 AND 100 OR soc_percent IS NULL),
        reason_mask INTEGER NOT NULL CHECK(reason_mask >= 0),
        source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
        CHECK(occurred_at_ms >= 0),
        CHECK(
            (scope = 'system' AND battery_id IS NULL
             AND battery_present IS NULL AND battery_state IS NULL AND soc_percent IS NULL)
            OR
            (scope = 'battery' AND battery_id IS NOT NULL
             AND ac_online IS NULL AND power_profile IS NULL AND battery_present IS NOT NULL)
        ),
        CHECK(battery_present IS NULL OR battery_present = 0
              OR (battery_state IS NOT NULL AND soc_percent IS NOT NULL))
    )""",
    "CREATE INDEX state_events_time_idx ON state_events(occurred_at_ms)",
    """CREATE INDEX state_events_scope_time_idx
        ON state_events(scope, occurred_at_ms)""",
    """CREATE INDEX state_events_battery_time_idx
        ON state_events(battery_id, occurred_at_ms)""",
    """CREATE UNIQUE INDEX state_events_system_time_idx
        ON state_events(occurred_at_ms) WHERE scope = 'system'""",
    """CREATE UNIQUE INDEX state_events_battery_unique_idx
        ON state_events(occurred_at_ms, battery_id) WHERE scope = 'battery'""",
    """CREATE TABLE hourly_history (
        hour_start_ms INTEGER PRIMARY KEY,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
        source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
        aggregation_version INTEGER NOT NULL CHECK(aggregation_version >= 1),
        finalized_at_ms INTEGER,
        is_final INTEGER NOT NULL CHECK(is_final IN (0, 1)),
        battery_set_key TEXT,
        soc_start REAL CHECK(soc_start BETWEEN 0 AND 100 OR soc_start IS NULL),
        soc_end REAL CHECK(soc_end BETWEEN 0 AND 100 OR soc_end IS NULL),
        soc_min REAL CHECK(soc_min BETWEEN 0 AND 100 OR soc_min IS NULL),
        soc_max REAL CHECK(soc_max BETWEEN 0 AND 100 OR soc_max IS NULL),
        soc_integral_percent_ms REAL NOT NULL CHECK(soc_integral_percent_ms >= 0),
        charged_energy_wh REAL NOT NULL CHECK(charged_energy_wh >= 0),
        discharged_energy_wh REAL NOT NULL CHECK(discharged_energy_wh >= 0),
        observed_ms INTEGER NOT NULL CHECK(observed_ms >= 0),
        sleep_ms INTEGER NOT NULL CHECK(sleep_ms >= 0),
        unknown_ms INTEGER NOT NULL CHECK(unknown_ms >= 0),
        charging_ms INTEGER NOT NULL CHECK(charging_ms >= 0),
        discharging_ms INTEGER NOT NULL CHECK(discharging_ms >= 0),
        full_ms INTEGER NOT NULL CHECK(full_ms >= 0),
        other_state_ms INTEGER NOT NULL CHECK(other_state_ms >= 0),
        ac_online_ms INTEGER NOT NULL CHECK(ac_online_ms >= 0),
        ac_offline_ms INTEGER NOT NULL CHECK(ac_offline_ms >= 0),
        ac_unknown_ms INTEGER NOT NULL CHECK(ac_unknown_ms >= 0),
        under_20_ms INTEGER NOT NULL CHECK(under_20_ms >= 0),
        above_80_ms INTEGER NOT NULL CHECK(above_80_ms >= 0),
        above_95_ms INTEGER NOT NULL CHECK(above_95_ms >= 0),
        full_on_ac_ms INTEGER NOT NULL CHECK(full_on_ac_ms >= 0),
        charge_power_integral_w_ms REAL NOT NULL CHECK(charge_power_integral_w_ms >= 0),
        charge_power_max_w REAL CHECK(charge_power_max_w >= 0 OR charge_power_max_w IS NULL),
        charge_power_valid_ms INTEGER NOT NULL CHECK(charge_power_valid_ms >= 0),
        discharge_power_integral_w_ms REAL NOT NULL CHECK(discharge_power_integral_w_ms >= 0),
        discharge_power_max_w REAL CHECK(discharge_power_max_w >= 0 OR discharge_power_max_w IS NULL),
        discharge_power_valid_ms INTEGER NOT NULL CHECK(discharge_power_valid_ms >= 0),
        direct_power_ms INTEGER NOT NULL CHECK(direct_power_ms >= 0),
        estimated_power_ms INTEGER NOT NULL CHECK(estimated_power_ms >= 0),
        unknown_power_ms INTEGER NOT NULL CHECK(unknown_power_ms >= 0),
        poll_count INTEGER NOT NULL CHECK(poll_count >= 0),
        state_event_count INTEGER NOT NULL CHECK(state_event_count >= 0),
        quality_flags INTEGER NOT NULL DEFAULT 0 CHECK(quality_flags >= 0),
        CHECK(hour_start_ms >= 0 AND hour_start_ms % 3600000 = 0),
        CHECK(finalized_at_ms IS NULL OR finalized_at_ms >= hour_start_ms + 3600000),
        CHECK(is_final = 0 OR finalized_at_ms IS NOT NULL),
        CHECK(observed_ms + sleep_ms + unknown_ms = 3600000),
        CHECK(charging_ms + discharging_ms + full_ms + other_state_ms = observed_ms),
        CHECK(ac_online_ms + ac_offline_ms + ac_unknown_ms = observed_ms),
        CHECK(direct_power_ms + estimated_power_ms + unknown_power_ms = observed_ms),
        CHECK(under_20_ms <= observed_ms),
        CHECK(above_80_ms <= observed_ms),
        CHECK(above_95_ms <= observed_ms),
        CHECK(full_on_ac_ms <= observed_ms),
        CHECK(charge_power_valid_ms <= observed_ms),
        CHECK(discharge_power_valid_ms <= observed_ms),
        CHECK(soc_min IS NULL OR soc_max IS NULL OR soc_min <= soc_max),
        CHECK((observed_ms = 0 AND soc_start IS NULL AND soc_end IS NULL
               AND soc_min IS NULL AND soc_max IS NULL AND soc_integral_percent_ms = 0)
              OR
              (observed_ms > 0 AND soc_start IS NOT NULL AND soc_end IS NOT NULL
               AND soc_min IS NOT NULL AND soc_max IS NOT NULL
               AND soc_integral_percent_ms <= observed_ms * 100)),
        CHECK((charge_power_valid_ms = 0 AND charge_power_integral_w_ms = 0
               AND charge_power_max_w IS NULL)
              OR (charge_power_valid_ms > 0 AND charge_power_max_w IS NOT NULL)),
        CHECK((discharge_power_valid_ms = 0 AND discharge_power_integral_w_ms = 0
               AND discharge_power_max_w IS NULL)
              OR (discharge_power_valid_ms > 0 AND discharge_power_max_w IS NOT NULL))
    )""",
    """CREATE TABLE hourly_profile_durations (
        hour_start_ms INTEGER NOT NULL REFERENCES hourly_history(hour_start_ms) ON DELETE CASCADE,
        profile TEXT NOT NULL,
        duration_ms INTEGER NOT NULL CHECK(duration_ms > 0 AND duration_ms <= 3600000),
        PRIMARY KEY(hour_start_ms, profile)
    ) WITHOUT ROWID""",
    """CREATE TABLE battery_health (
        id INTEGER PRIMARY KEY,
        battery_id INTEGER NOT NULL REFERENCES batteries(id),
        observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
        charge_full_ah REAL CHECK(charge_full_ah > 0 OR charge_full_ah IS NULL),
        charge_full_design_ah REAL CHECK(charge_full_design_ah > 0 OR charge_full_design_ah IS NULL),
        energy_full_wh REAL CHECK(energy_full_wh > 0 OR energy_full_wh IS NULL),
        energy_full_design_wh REAL CHECK(energy_full_design_wh > 0 OR energy_full_design_wh IS NULL),
        cycle_count INTEGER CHECK(cycle_count >= 0 OR cycle_count IS NULL),
        voltage_design_v REAL CHECK(voltage_design_v > 0 OR voltage_design_v IS NULL),
        source TEXT NOT NULL,
        provenance TEXT NOT NULL,
        source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
        UNIQUE(battery_id, observed_at_ms),
        CHECK(charge_full_ah IS NOT NULL OR charge_full_design_ah IS NOT NULL
              OR energy_full_wh IS NOT NULL OR energy_full_design_wh IS NOT NULL
              OR cycle_count IS NOT NULL OR voltage_design_v IS NOT NULL)
    )""",
    """CREATE INDEX battery_health_battery_time_idx
        ON battery_health(battery_id, observed_at_ms)""",
    """CREATE TABLE sessions (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('charging', 'discharging')),
        started_at_ms INTEGER NOT NULL CHECK(started_at_ms >= 0),
        ended_at_ms INTEGER,
        start_soc REAL NOT NULL CHECK(start_soc BETWEEN 0 AND 100),
        end_soc REAL CHECK(end_soc BETWEEN 0 AND 100 OR end_soc IS NULL),
        start_event_id INTEGER REFERENCES state_events(id),
        end_event_id INTEGER REFERENCES state_events(id),
        battery_set_key TEXT NOT NULL,
        end_reason TEXT,
        source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
        CHECK(ended_at_ms IS NULL OR ended_at_ms >= started_at_ms),
        CHECK((ended_at_ms IS NULL AND end_event_id IS NULL)
              OR ended_at_ms IS NOT NULL)
    )""",
    "CREATE INDEX sessions_started_idx ON sessions(started_at_ms)",
    "CREATE INDEX sessions_ended_idx ON sessions(ended_at_ms)",
    """CREATE UNIQUE INDEX sessions_one_active_idx
        ON sessions((1)) WHERE ended_at_ms IS NULL""",
    """CREATE TABLE sleep_intervals (
        id INTEGER PRIMARY KEY,
        started_at_ms INTEGER NOT NULL CHECK(started_at_ms >= 0),
        ended_at_ms INTEGER NOT NULL,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        boot_id TEXT,
        detected_at_ms INTEGER NOT NULL CHECK(detected_at_ms >= 0),
        pre_event_id INTEGER REFERENCES state_events(id),
        post_event_id INTEGER REFERENCES state_events(id),
        pre_soc REAL CHECK(pre_soc BETWEEN 0 AND 100 OR pre_soc IS NULL),
        post_soc REAL CHECK(post_soc BETWEEN 0 AND 100 OR post_soc IS NULL),
        source_generation INTEGER NOT NULL CHECK(source_generation >= 0),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
        CHECK(ended_at_ms > started_at_ms),
        UNIQUE(started_at_ms, ended_at_ms)
    )""",
    "CREATE INDEX sleep_intervals_start_idx ON sleep_intervals(started_at_ms)",
    "CREATE INDEX sleep_intervals_end_idx ON sleep_intervals(ended_at_ms)",
    """CREATE TABLE metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE checkpoint_generations (
        generation INTEGER PRIMARY KEY CHECK(generation >= 1),
        format_version INTEGER NOT NULL CHECK(format_version >= 1),
        created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
        last_poll_at_ms INTEGER NOT NULL CHECK(last_poll_at_ms >= 0),
        boot_id TEXT NOT NULL,
        monotonic_ns INTEGER NOT NULL CHECK(monotonic_ns >= 0),
        boottime_ns INTEGER NOT NULL CHECK(boottime_ns >= 0),
        configured_interval_ms INTEGER NOT NULL CHECK(configured_interval_ms > 0),
        ac_online INTEGER CHECK(ac_online IN (0, 1) OR ac_online IS NULL),
        power_profile TEXT,
        battery_count INTEGER NOT NULL CHECK(battery_count >= 0),
        hourly_count INTEGER NOT NULL CHECK(hourly_count >= 0 AND hourly_count <= 1),
        profile_count INTEGER NOT NULL CHECK(profile_count >= 0),
        recent_series BLOB NOT NULL,
        payload_digest TEXT NOT NULL
            CHECK(length(payload_digest) = 64
                  AND payload_digest NOT GLOB '*[^0-9a-f]*'),
        complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0, 1)),
        CHECK(boottime_ns >= monotonic_ns)
    )""",
    """CREATE TABLE checkpoint_batteries (
        generation INTEGER NOT NULL REFERENCES checkpoint_generations(generation) ON DELETE CASCADE,
        battery_id INTEGER NOT NULL REFERENCES batteries(id),
        present INTEGER NOT NULL CHECK(present IN (0, 1)),
        state TEXT NOT NULL,
        soc_percent REAL NOT NULL CHECK(soc_percent BETWEEN 0 AND 100),
        power_now_w REAL,
        current_now_a REAL,
        voltage_now_v REAL,
        energy_now_wh REAL,
        charge_now_ah REAL,
        upower_energy_rate_w REAL,
        resolved_power_w REAL CHECK(resolved_power_w >= 0 OR resolved_power_w IS NULL),
        power_method TEXT,
        power_approximate INTEGER CHECK(power_approximate IN (0, 1) OR power_approximate IS NULL),
        power_confidence TEXT,
        power_window_s REAL CHECK(power_window_s > 0 OR power_window_s IS NULL),
        PRIMARY KEY(generation, battery_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE checkpoint_hourly (
        generation INTEGER NOT NULL REFERENCES checkpoint_generations(generation) ON DELETE CASCADE,
        hour_start_ms INTEGER NOT NULL,
        soc_first REAL CHECK(soc_first BETWEEN 0 AND 100 OR soc_first IS NULL),
        soc_last REAL CHECK(soc_last BETWEEN 0 AND 100 OR soc_last IS NULL),
        soc_min REAL CHECK(soc_min BETWEEN 0 AND 100 OR soc_min IS NULL),
        soc_max REAL CHECK(soc_max BETWEEN 0 AND 100 OR soc_max IS NULL),
        soc_integral_percent_ms REAL NOT NULL CHECK(soc_integral_percent_ms >= 0),
        charged_energy_wh REAL NOT NULL CHECK(charged_energy_wh >= 0),
        discharged_energy_wh REAL NOT NULL CHECK(discharged_energy_wh >= 0),
        observed_ms INTEGER NOT NULL CHECK(observed_ms >= 0),
        sleep_ms INTEGER NOT NULL CHECK(sleep_ms >= 0),
        unknown_ms INTEGER NOT NULL CHECK(unknown_ms >= 0),
        charging_ms INTEGER NOT NULL CHECK(charging_ms >= 0),
        discharging_ms INTEGER NOT NULL CHECK(discharging_ms >= 0),
        full_ms INTEGER NOT NULL CHECK(full_ms >= 0),
        other_state_ms INTEGER NOT NULL CHECK(other_state_ms >= 0),
        ac_online_ms INTEGER NOT NULL CHECK(ac_online_ms >= 0),
        ac_offline_ms INTEGER NOT NULL CHECK(ac_offline_ms >= 0),
        ac_unknown_ms INTEGER NOT NULL CHECK(ac_unknown_ms >= 0),
        under_20_ms INTEGER NOT NULL CHECK(under_20_ms >= 0),
        above_80_ms INTEGER NOT NULL CHECK(above_80_ms >= 0),
        above_95_ms INTEGER NOT NULL CHECK(above_95_ms >= 0),
        full_on_ac_ms INTEGER NOT NULL CHECK(full_on_ac_ms >= 0),
        charge_power_integral_w_ms REAL NOT NULL CHECK(charge_power_integral_w_ms >= 0),
        charge_power_max_w REAL CHECK(charge_power_max_w >= 0 OR charge_power_max_w IS NULL),
        charge_power_valid_ms INTEGER NOT NULL CHECK(charge_power_valid_ms >= 0),
        discharge_power_integral_w_ms REAL NOT NULL CHECK(discharge_power_integral_w_ms >= 0),
        discharge_power_max_w REAL CHECK(discharge_power_max_w >= 0 OR discharge_power_max_w IS NULL),
        discharge_power_valid_ms INTEGER NOT NULL CHECK(discharge_power_valid_ms >= 0),
        direct_power_ms INTEGER NOT NULL CHECK(direct_power_ms >= 0),
        estimated_power_ms INTEGER NOT NULL CHECK(estimated_power_ms >= 0),
        unknown_power_ms INTEGER NOT NULL CHECK(unknown_power_ms >= 0),
        poll_count INTEGER NOT NULL CHECK(poll_count >= 0),
        state_event_count INTEGER NOT NULL CHECK(state_event_count >= 0),
        quality_flags INTEGER NOT NULL CHECK(quality_flags >= 0),
        PRIMARY KEY(generation, hour_start_ms),
        CHECK(hour_start_ms >= 0 AND hour_start_ms % 3600000 = 0),
        CHECK(observed_ms + sleep_ms + unknown_ms <= 3600000),
        CHECK(charging_ms + discharging_ms + full_ms + other_state_ms = observed_ms),
        CHECK(ac_online_ms + ac_offline_ms + ac_unknown_ms = observed_ms),
        CHECK(direct_power_ms + estimated_power_ms + unknown_power_ms = observed_ms),
        CHECK(soc_min IS NULL OR soc_max IS NULL OR soc_min <= soc_max),
        CHECK((observed_ms = 0 AND soc_first IS NULL AND soc_last IS NULL
               AND soc_min IS NULL AND soc_max IS NULL AND soc_integral_percent_ms = 0)
              OR
              (observed_ms > 0 AND soc_first IS NOT NULL AND soc_last IS NOT NULL
               AND soc_min IS NOT NULL AND soc_max IS NOT NULL
               AND soc_integral_percent_ms <= observed_ms * 100)),
        CHECK((charge_power_valid_ms = 0 AND charge_power_integral_w_ms = 0
               AND charge_power_max_w IS NULL)
              OR (charge_power_valid_ms > 0 AND charge_power_max_w IS NOT NULL)),
        CHECK((discharge_power_valid_ms = 0 AND discharge_power_integral_w_ms = 0
               AND discharge_power_max_w IS NULL)
              OR (discharge_power_valid_ms > 0 AND discharge_power_max_w IS NOT NULL))
    ) WITHOUT ROWID""",
    """CREATE TABLE checkpoint_hourly_profiles (
        generation INTEGER NOT NULL,
        hour_start_ms INTEGER NOT NULL,
        profile TEXT NOT NULL,
        duration_ms INTEGER NOT NULL CHECK(duration_ms > 0 AND duration_ms <= 3600000),
        PRIMARY KEY(generation, hour_start_ms, profile),
        FOREIGN KEY(generation, hour_start_ms)
            REFERENCES checkpoint_hourly(generation, hour_start_ms) ON DELETE CASCADE
    ) WITHOUT ROWID""",
    """CREATE TRIGGER checkpoint_complete_on_insert
        BEFORE INSERT ON checkpoint_generations
        WHEN NEW.complete = 1
        BEGIN
            SELECT RAISE(ABORT, 'checkpoint must be inserted incomplete');
        END""",
    """CREATE TRIGGER checkpoint_complete_counts
        BEFORE UPDATE OF complete ON checkpoint_generations
        WHEN NEW.complete = 1
        BEGIN
            SELECT CASE WHEN NEW.complete = OLD.complete
                THEN RAISE(ABORT, 'checkpoint is already complete') END;
            SELECT CASE WHEN (SELECT COUNT(*) FROM checkpoint_batteries
                              WHERE generation = NEW.generation) != NEW.battery_count
                THEN RAISE(ABORT, 'checkpoint battery count mismatch') END;
            SELECT CASE WHEN (SELECT COUNT(*) FROM checkpoint_hourly
                              WHERE generation = NEW.generation) != NEW.hourly_count
                THEN RAISE(ABORT, 'checkpoint hourly count mismatch') END;
            SELECT CASE WHEN (SELECT COUNT(*) FROM checkpoint_hourly_profiles
                              WHERE generation = NEW.generation) != NEW.profile_count
                THEN RAISE(ABORT, 'checkpoint profile count mismatch') END;
        END""",
)


def create_v1_schema(db: "sqlite3.Connection") -> None:
    """Create the locked v1 schema in an empty test/staging database."""
    for statement in V1_CREATE_STATEMENTS:
        db.execute(statement)
