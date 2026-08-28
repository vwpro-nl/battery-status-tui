from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from battery_status_tui import pre_v1_validator
from battery_status_tui.pre_v1_converter import convert_v2_to_v1
from battery_status_tui.pre_v1_validator import validate_pre_v1_conversion
from battery_status_tui.storage import Storage


FIXTURE = Path(__file__).parent / "fixtures" / "pre_v1_comprehensive.sql"
PROJECT_ROOT = Path(__file__).parent.parent


class PreV1ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.source = root / "source.sqlite3"
        self.destination = root / "destination.sqlite3"
        Storage(self.source).initialize_writer()
        db = sqlite3.connect(self.source)
        try:
            db.executescript(FIXTURE.read_text())
            db.commit()
        finally:
            db.close()
        convert_v2_to_v1(self.source, self.destination)

    def tearDown(self):
        self.directory.cleanup()

    def _mutate(self, sql: str, parameters: tuple = ()) -> None:
        db = sqlite3.connect(self.destination)
        try:
            db.execute(sql, parameters)
            db.commit()
        finally:
            db.close()

    def _assert_failure(self, table: str, field: str | None = None):
        report = validate_pre_v1_conversion(self.source, self.destination)
        self.assertFalse(report.passed, report.to_text())
        matches = [item for item in report.mismatches if item.table == table]
        self.assertTrue(matches, report.to_text())
        if field is not None:
            self.assertTrue(any(item.field == field for item in matches), report.to_text())
        return report

    def test_valid_conversion_passes_independent_validation(self):
        report = validate_pre_v1_conversion(self.source, self.destination)
        self.assertTrue(report.passed, report.to_text())
        self.assertEqual((report.source_schema, report.destination_schema), (2, 4))
        self.assertEqual(report.checked_batteries, 3)
        self.assertEqual(report.checked_sessions, 3)
        self.assertEqual(report.checked_sleeps, 2)
        self.assertEqual(report.checked_hours, 6)
        self.assertGreater(report.totals["sleep_ms"], 0)
        self.assertIn("PASS", report.to_text())
        self.assertEqual(json.loads(report.to_json())["result"], "PASS")

    def test_wrong_battery_mapping_is_detected(self):
        self._mutate("UPDATE batteries SET identity='wrong-battery' WHERE identity='battery-c'")
        self._assert_failure("batteries")

    def test_missing_and_extra_state_events_are_detected(self):
        self._mutate("DELETE FROM state_events WHERE id=(SELECT min(id) FROM state_events)")
        self._assert_failure("state_events", "occurrences")

        # Recreate a clean destination and add a semantically spurious event.
        self.destination.unlink()
        convert_v2_to_v1(self.source, self.destination)
        self._mutate(
            "INSERT INTO state_events(occurred_at_ms, boot_id, scope, ac_online, "
            "reason_mask, source_generation) VALUES(1704067200001, 'boot-a', "
            "'system', 1, 8, 0)"
        )
        self._assert_failure("state_events", "occurrences")

    def test_changed_session_is_detected(self):
        self._mutate("UPDATE sessions SET end_reason='wrong' WHERE id=1")
        self._assert_failure("sessions", "row")

    def test_changed_sleep_boundary_is_detected(self):
        self._mutate("UPDATE sleep_intervals SET ended_at_ms=ended_at_ms+1000 WHERE id=1")
        self._assert_failure("sleep_intervals", "row")

    def test_changed_health_native_value_is_detected(self):
        self._mutate("UPDATE battery_health SET charge_full_ah=charge_full_ah+0.1 "
                     "WHERE charge_full_ah IS NOT NULL AND id=(SELECT min(id) FROM battery_health)")
        self._assert_failure("battery_health", "occurrences")

    def test_wrong_hourly_coverage_is_detected(self):
        db = sqlite3.connect(self.destination)
        try:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE hourly_history SET unknown_ms=unknown_ms-1 "
                       "WHERE hour_start_ms=(SELECT min(hour_start_ms) FROM hourly_history)")
            db.commit()
        finally:
            db.close()
        self._assert_failure("hourly_history", "unknown_ms")

    def test_wrong_soc_geometry_fields_are_detected(self):
        self._mutate(
            "UPDATE hourly_history SET soc_start=soc_start+1, soc_end=soc_end+1, "
            "soc_min=soc_min-1, soc_max=soc_max+1, "
            "soc_integral_percent_ms=soc_integral_percent_ms+1000 "
            "WHERE observed_ms>0 AND hour_start_ms=(SELECT min(hour_start_ms) "
            "FROM hourly_history WHERE observed_ms>0)"
        )
        report = self._assert_failure("hourly_history")
        fields = {item.field for item in report.mismatches if item.table == "hourly_history"}
        self.assertTrue({"soc_start", "soc_end", "soc_min", "soc_max",
                         "soc_integral_percent_ms"} <= fields)

    def test_wrong_energy_and_discontinuity_delta_are_detected(self):
        # The overnight sleep hour has no observed energy; adding a delta there
        # simulates carrying a counter delta across the discontinuity.
        self._mutate(
            "UPDATE hourly_history SET discharged_energy_wh=0.5 "
            "WHERE sleep_ms=3600000 AND hour_start_ms=(SELECT min(hour_start_ms) "
            "FROM hourly_history WHERE sleep_ms=3600000)"
        )
        self._assert_failure("hourly_history", "discharged_energy_wh")

    def test_finalized_trailing_partial_hour_is_detected(self):
        db = sqlite3.connect(self.destination)
        try:
            row = db.execute("SELECT * FROM checkpoint_hourly WHERE generation=1").fetchone()
            columns = [item[1] for item in db.execute("PRAGMA table_info(hourly_history)")]
            checkpoint_columns = [item[1] for item in db.execute(
                "PRAGMA table_info(checkpoint_hourly)"
            )]
            values = dict(zip(checkpoint_columns, row))
            values["soc_start"] = values["soc_first"]
            values["soc_end"] = values["soc_last"]
            values["unknown_ms"] += 3_600_000 - (
                values["observed_ms"] + values["sleep_ms"] + values["unknown_ms"]
            )
            insert_columns = [name for name in columns if name in values]
            insert_values = [values[name] for name in insert_columns]
            db.execute(
                f"INSERT INTO hourly_history({','.join(insert_columns)},finalized_at_ms,"
                "revision,source_generation,aggregation_version,is_final) VALUES("
                f"{','.join('?' for _ in insert_columns)},?,1,1,1,1)",
                insert_values + [values["hour_start_ms"] + 3_600_000]
            )
            db.commit()
        finally:
            db.close()
        self._assert_failure("hourly_history")

    def test_invalid_seed_checkpoint_is_detected(self):
        self._mutate("UPDATE checkpoint_generations SET payload_digest=? WHERE generation=1",
                     ("0" * 64,))
        self._assert_failure("checkpoint_generations")

    def test_wrong_power_statistics_and_provenance_are_detected(self):
        self._mutate(
            "UPDATE hourly_history SET discharge_power_integral_w_ms="
            "discharge_power_integral_w_ms+1, discharge_power_max_w="
            "discharge_power_max_w+1, estimated_power_ms=estimated_power_ms-1, "
            "direct_power_ms=direct_power_ms+1 WHERE discharge_power_valid_ms>0 "
            "AND hour_start_ms=(SELECT min(hour_start_ms) FROM hourly_history "
            "WHERE discharge_power_valid_ms>0)"
        )
        report = self._assert_failure("hourly_history")
        fields = {item.field for item in report.mismatches if item.table == "hourly_history"}
        self.assertIn("discharge_power_integral_w_ms", fields)
        self.assertIn("estimated_power_ms", fields)

    def test_wrong_state_ac_threshold_and_counts_are_detected(self):
        self._mutate(
            "UPDATE hourly_history SET charging_ms=charging_ms-1, "
            "discharging_ms=discharging_ms+1, ac_online_ms=ac_online_ms-1, "
            "ac_offline_ms=ac_offline_ms+1, under_20_ms=under_20_ms+1, "
            "poll_count=poll_count+1, state_event_count=state_event_count+1 "
            "WHERE charging_ms>0 AND discharging_ms>0 AND ac_online_ms>0 "
            "AND ac_offline_ms>0"
        )
        report = self._assert_failure("hourly_history")
        fields = {item.field for item in report.mismatches if item.table == "hourly_history"}
        self.assertTrue({"charging_ms", "discharging_ms", "ac_online_ms", "ac_offline_ms",
                         "under_20_ms", "poll_count", "state_event_count"} <= fields)

    def test_wrong_utc_hour_placement_is_detected(self):
        db = sqlite3.connect(self.destination)
        try:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE hourly_history SET hour_start_ms=hour_start_ms+1 "
                       "WHERE hour_start_ms=(SELECT min(hour_start_ms) FROM hourly_history)")
            db.commit()
        finally:
            db.close()
        self._assert_failure("hourly_history")

    def test_unproven_historical_profile_is_detected(self):
        db = sqlite3.connect(self.destination)
        try:
            hour = db.execute("SELECT min(hour_start_ms) FROM hourly_history").fetchone()[0]
        finally:
            db.close()
        self._mutate(
            "INSERT INTO hourly_profile_durations(hour_start_ms, profile, duration_ms) "
            "VALUES(?, 'balanced', 60000)", (hour,)
        )
        self._assert_failure("hourly_profile_durations", "balanced")

    def test_wrong_schema_version_is_detected(self):
        self._mutate("PRAGMA user_version=3")
        self._assert_failure("destination", "user_version")

    def test_missing_schema_index_is_detected(self):
        self._mutate("DROP INDEX sessions_started_idx")
        self._assert_failure("schema", "definition")

    def test_wrong_source_schema_version_is_detected(self):
        db = sqlite3.connect(self.source)
        try:
            db.execute("PRAGMA user_version=1")
            db.commit()
        finally:
            db.close()
        self._assert_failure("source", "user_version")

    def test_foreign_key_problem_is_detected(self):
        db = sqlite3.connect(self.destination)
        try:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "INSERT INTO battery_health(battery_id, observed_at_ms, charge_full_ah, "
                "source, provenance, source_generation) VALUES(999, 1, 1, 'bad', 'bad', 0)"
            )
            db.commit()
        finally:
            db.close()
        self._assert_failure("destination", "foreign_key")

    def test_corrupt_destination_is_detected(self):
        self.destination.write_bytes(b"not sqlite")
        report = validate_pre_v1_conversion(self.source, self.destination)
        self.assertFalse(report.passed)
        self.assertTrue(any(item.table in {"destination", "validation"}
                            for item in report.mismatches))

    def test_cli_emits_json_and_returns_nonzero_for_mismatch(self):
        command = [str(PROJECT_ROOT / "tools" / "validate_pre_v1_conversion.py"),
                   "--json", str(self.source), str(self.destination)]
        passed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["result"], "PASS")

        self._mutate("UPDATE sessions SET end_reason='wrong' WHERE id=1")
        failed = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(json.loads(failed.stdout)["result"], "FAIL")

    def test_validator_holds_one_source_read_snapshot(self):
        original = pre_v1_validator._check_database

        def assert_transaction(db, label, *args, **kwargs):
            if label == "source":
                self.assertTrue(db.in_transaction)
            return original(db, label, *args, **kwargs)

        with mock.patch.object(pre_v1_validator, "_check_database",
                               side_effect=assert_transaction):
            report = validate_pre_v1_conversion(self.source, self.destination)
        self.assertTrue(report.passed, report.to_text())


if __name__ == "__main__":
    unittest.main()
