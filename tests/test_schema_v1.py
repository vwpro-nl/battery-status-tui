from __future__ import annotations

import sqlite3
import unittest

from battery_status_tui.recent_series import encode_recent_series, payload_sha256
from battery_status_tui.schema import V1_CREATE_STATEMENTS, create_v1_schema


HOURLY_VALUES = {
    "hour_start_ms": 3_600_000,
    "revision": 1,
    "source_generation": 1,
    "aggregation_version": 1,
    "finalized_at_ms": 7_200_000,
    "is_final": 1,
    "battery_set_key": "set-a",
    "soc_start": 50.0,
    "soc_end": 50.0,
    "soc_min": 50.0,
    "soc_max": 50.0,
    "soc_integral_percent_ms": 180_000_000.0,
    "charged_energy_wh": 0.0,
    "discharged_energy_wh": 0.0,
    "observed_ms": 3_600_000,
    "sleep_ms": 0,
    "unknown_ms": 0,
    "charging_ms": 0,
    "discharging_ms": 3_600_000,
    "full_ms": 0,
    "other_state_ms": 0,
    "ac_online_ms": 0,
    "ac_offline_ms": 3_600_000,
    "ac_unknown_ms": 0,
    "under_20_ms": 0,
    "above_80_ms": 0,
    "above_95_ms": 0,
    "full_on_ac_ms": 0,
    "charge_power_integral_w_ms": 0.0,
    "charge_power_max_w": None,
    "charge_power_valid_ms": 0,
    "discharge_power_integral_w_ms": 28_800_000.0,
    "discharge_power_max_w": 8.0,
    "discharge_power_valid_ms": 3_600_000,
    "direct_power_ms": 0,
    "estimated_power_ms": 3_600_000,
    "unknown_power_ms": 0,
    "poll_count": 60,
    "state_event_count": 0,
    "quality_flags": 0,
}


def insert(db: sqlite3.Connection, table: str, values: dict) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    db.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(values.values()))


class V1SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys = ON")
        create_v1_schema(self.db)
        self.db.execute(
            "INSERT INTO batteries(identity, first_seen_ms, last_seen_ms) VALUES('battery-a', 1, 1)"
        )
        self.battery_id = self.db.execute("SELECT id FROM batteries").fetchone()[0]

    def tearDown(self):
        self.db.close()

    def test_schema_has_only_locked_v1_tables(self):
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertEqual(tables, {
            "batteries", "state_events", "hourly_history", "hourly_profile_durations",
            "battery_health", "sessions", "sleep_intervals", "metadata",
            "checkpoint_generations", "checkpoint_batteries", "checkpoint_hourly",
            "checkpoint_hourly_profiles",
        })
        self.assertNotIn("samples", tables)
        self.assertNotIn("battery_samples", tables)

    def test_schema_statements_are_not_part_of_current_runtime_migrations(self):
        self.assertTrue(V1_CREATE_STATEMENTS)

    def test_foreign_keys_and_state_scope_checks(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
                "battery_present, reason_mask, source_generation) "
                "VALUES(1, 'boot', 'battery', 999, 1, 1, 1)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
                "battery_present, ac_online, reason_mask, source_generation) "
                "VALUES(1, 'boot', 'battery', ?, 1, 1, 1, 1)", (self.battery_id,)
            )

    def test_state_event_and_health_event_uniqueness(self):
        event = (1, "boot", "battery", self.battery_id, 1, "full", 100.0, 1, 1)
        sql = (
            "INSERT INTO state_events(occurred_at_ms, boot_id, scope, battery_id, "
            "battery_present, battery_state, soc_percent, reason_mask, source_generation) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        self.db.execute(sql, event)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, event)

        health = (self.battery_id, 1, 3.0, 5.0, "sysfs-charge", "sysfs", 1)
        health_sql = (
            "INSERT INTO battery_health(battery_id, observed_at_ms, charge_full_ah, "
            "charge_full_design_ah, source, provenance, source_generation) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)"
        )
        self.db.execute(health_sql, health)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(health_sql, health)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO battery_health(battery_id, observed_at_ms, source, provenance, "
                "source_generation) VALUES(?, 2, 'none', 'none', 1)", (self.battery_id,)
            )

    def test_hourly_duration_invariant_is_exact(self):
        insert(self.db, "hourly_history", HOURLY_VALUES)
        invalid = dict(HOURLY_VALUES, hour_start_ms=7_200_000,
                       observed_ms=3_599_999, discharging_ms=3_599_999,
                       ac_offline_ms=3_599_999, estimated_power_ms=3_599_999)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, "hourly_history", invalid)

    def test_hourly_profile_is_without_rowid_unique_and_cascades(self):
        insert(self.db, "hourly_history", HOURLY_VALUES)
        sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='hourly_profile_durations'"
        ).fetchone()[0]
        self.assertIn("WITHOUT ROWID", sql.upper())
        self.db.execute(
            "INSERT INTO hourly_profile_durations VALUES(3600000, 'balanced', 3600000)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO hourly_profile_durations VALUES(3600000, 'balanced', 1)"
            )
        self.db.execute("DELETE FROM hourly_history WHERE hour_start_ms=3600000")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM hourly_profile_durations"
        ).fetchone()[0], 0)

    def test_only_one_active_session_is_allowed(self):
        values = ("discharging", 1, 50.0, "set-a", 1)
        self.db.execute(
            "INSERT INTO sessions(kind, started_at_ms, start_soc, battery_set_key, "
            "source_generation) VALUES(?, ?, ?, ?, ?)", values
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO sessions(kind, started_at_ms, start_soc, battery_set_key, "
                "source_generation) VALUES(?, ?, ?, ?, ?)",
                ("charging", 2, 50.0, "set-a", 2),
            )

    def test_sleep_interval_uniqueness_and_bounds(self):
        values = (100, 200, "suspend", "journal", 200, 1)
        self.db.execute(
            "INSERT INTO sleep_intervals(started_at_ms, ended_at_ms, kind, source, "
            "detected_at_ms, source_generation) VALUES(?, ?, ?, ?, ?, ?)", values
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO sleep_intervals(started_at_ms, ended_at_ms, kind, source, "
                "detected_at_ms, source_generation) VALUES(?, ?, ?, ?, ?, ?)", values
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO sleep_intervals(started_at_ms, ended_at_ms, kind, source, "
                "detected_at_ms, source_generation) VALUES(300, 300, 'suspend', "
                "'journal', 300, 1)"
            )

    def test_checkpoint_complete_requires_matching_child_counts(self):
        self._generation(1, battery_count=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE checkpoint_generations SET complete=1 WHERE generation=1")
        self._checkpoint_battery(1)
        self.db.execute("UPDATE checkpoint_generations SET complete=1 WHERE generation=1")
        self.assertEqual(self.db.execute(
            "SELECT complete FROM checkpoint_generations WHERE generation=1"
        ).fetchone()[0], 1)

    def test_cross_generation_hourly_profile_mix_is_impossible(self):
        self._generation(1, hourly_count=1, profile_count=1)
        self._generation(2)
        self._checkpoint_hourly(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO checkpoint_hourly_profiles VALUES(2, 3600000, 'balanced', 1)"
            )
        self.db.execute(
            "INSERT INTO checkpoint_hourly_profiles VALUES(1, 3600000, 'balanced', 1)"
        )
        self.db.execute("UPDATE checkpoint_generations SET complete=1 WHERE generation=1")

    def _generation(self, generation: int, *, battery_count: int = 0,
                    hourly_count: int = 0, profile_count: int = 0) -> None:
        series = encode_recent_series(())
        insert(self.db, "checkpoint_generations", {
            "generation": generation,
            "format_version": 1,
            "created_at_ms": generation,
            "last_poll_at_ms": generation,
            "boot_id": "boot",
            "monotonic_ns": 1,
            "boottime_ns": 1,
            "configured_interval_ms": 60_000,
            "battery_count": battery_count,
            "hourly_count": hourly_count,
            "profile_count": profile_count,
            "recent_series": series,
            "payload_digest": payload_sha256(series),
            "complete": 0,
        })

    def _checkpoint_battery(self, generation: int) -> None:
        self.db.execute(
            "INSERT INTO checkpoint_batteries(generation, battery_id, present, state, "
            "soc_percent) VALUES(?, ?, 1, 'full', 100)",
            (generation, self.battery_id),
        )

    def _checkpoint_hourly(self, generation: int) -> None:
        values = {key: value for key, value in HOURLY_VALUES.items()
                  if key not in {"revision", "source_generation", "aggregation_version",
                                 "finalized_at_ms", "is_final", "battery_set_key"}}
        values["generation"] = generation
        values["soc_first"] = values.pop("soc_start")
        values["soc_last"] = values.pop("soc_end")
        insert(self.db, "checkpoint_hourly", values)


if __name__ == "__main__":
    unittest.main()
