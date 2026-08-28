from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from battery_status_tui import pre_v1_converter
from battery_status_tui.models import Measurement, RawBatterySnapshot
from battery_status_tui.pre_v1_converter import (
    AggregateSample,
    HourAccumulator,
    PreV1ConversionError,
    _add_sample_interval,
    _continuous,
    convert_v2_to_v1,
)
from battery_status_tui.v1_history import V1History
from battery_status_tui.v1_collector import V1Collector
from battery_status_tui.v1_storage import V1Storage
from battery_status_tui.storage import Storage


FIXTURE = Path(__file__).parent / "fixtures" / "pre_v1_comprehensive.sql"


class PreV1ConverterTests(unittest.TestCase):
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

    def tearDown(self):
        self.directory.cleanup()

    @contextmanager
    def _convert(self):
        convert_v2_to_v1(self.source, self.destination)
        db = sqlite3.connect(f"file:{self.destination}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _logical_dump(path: Path) -> tuple:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            result = []
            for table in tables:
                if table.startswith("checkpoint_"):
                    continue
                columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                order = ", ".join(f'"{column}"' for column in columns)
                result.append((table, tuple(db.execute(
                    f'SELECT * FROM "{table}" ORDER BY {order}'
                ))))
            return tuple(result)
        finally:
            db.close()

    def test_source_is_byte_and_logically_unchanged(self):
        before_bytes = self.source.read_bytes()
        before_dump = self._logical_dump(self.source)
        convert_v2_to_v1(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), before_bytes)
        self.assertEqual(self._logical_dump(self.source), before_dump)

    def test_conversion_is_deterministic(self):
        other = self.source.parent / "other.sqlite3"
        convert_v2_to_v1(self.source, self.destination)
        convert_v2_to_v1(self.source, other)
        self.assertEqual(self._logical_dump(self.destination), self._logical_dump(other))

    def test_schema_battery_mapping_and_no_duplicate_events(self):
        with self._convert() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            identities = [row[0] for row in db.execute("SELECT identity FROM batteries ORDER BY identity")]
            self.assertEqual(identities, ["battery-a", "battery-b", "battery-c"])
            duplicates = db.execute(
                "SELECT occurred_at_ms, scope, battery_id, count(*) count FROM state_events "
                "GROUP BY occurred_at_ms, scope, battery_id HAVING count > 1"
            ).fetchall()
            self.assertEqual(duplicates, [])
            # Aggregate-only SoC changes are retained in recent/hourly data,
            # not promoted to permanent state events.
            self.assertIsNone(db.execute(
                "SELECT 1 FROM state_events e JOIN batteries b ON b.id=e.battery_id "
                "WHERE b.identity='battery-b' AND e.occurred_at_ms=1704088860000"
            ).fetchone())
            # Raw battery-set changes preserve removal rather than silently
            # treating the old pack as still present.
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM state_events e JOIN batteries b ON b.id=e.battery_id "
                "WHERE b.identity='battery-a' AND e.occurred_at_ms=1704088800000 "
                "AND e.battery_present=0"
            ).fetchone())

    def test_sessions_are_preserved_open_and_closed(self):
        with self._convert() as db:
            rows = db.execute(
                "SELECT id, kind, started_at_ms, ended_at_ms, start_soc, end_soc, end_reason "
                "FROM sessions ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(tuple(rows[0]),
                             (1, "charging", 1704067200000, 1704067380000, 20.0, 22.0, "discharging"))
            self.assertEqual(db.execute(
                "SELECT battery_set_key FROM sessions WHERE id=2"
            ).fetchone()[0], "battery-a+battery-c")
            self.assertIsNone(rows[2]["ended_at_ms"])
            self.assertIsNone(rows[2]["end_soc"])

    def test_sleep_boundaries_and_provenance_are_preserved(self):
        with self._convert() as db:
            rows = db.execute(
                "SELECT started_at_ms, ended_at_ms, source, boot_id, pre_soc, post_soc "
                "FROM sleep_intervals ORDER BY id"
            ).fetchall()
            self.assertEqual(tuple(rows[0]),
                             (1704067920000, 1704068400000, "clock", "boot-a", 20.0, 19.0))
            self.assertEqual(tuple(rows[1]),
                             (1704069000000, 1704085200000, "journal", "boot-a", 18.0, 17.0))

    def test_charge_energy_multi_battery_and_changed_health_events(self):
        with self._convert() as db:
            rows = db.execute(
                "SELECT b.identity, h.charge_full_ah, h.charge_full_design_ah, "
                "h.energy_full_wh, h.energy_full_design_wh, h.source "
                "FROM battery_health h JOIN batteries b ON b.id=h.battery_id "
                "ORDER BY b.identity, h.observed_at_ms"
            ).fetchall()
            battery_a = [row for row in rows if row["identity"] == "battery-a"]
            self.assertEqual([(row["charge_full_ah"], row["charge_full_design_ah"])
                              for row in battery_a], [(3.0, 5.0), (2.9, 5.0)])
            battery_c = next(row for row in rows if row["identity"] == "battery-c")
            self.assertEqual((battery_c["energy_full_wh"], battery_c["energy_full_design_wh"]),
                             (30.0, 40.0))
            self.assertEqual(battery_c["source"], "legacy-v2-energy")

    def test_hourly_rows_close_exactly_and_partial_hours_are_unknown(self):
        with self._convert() as db:
            rows = db.execute("SELECT * FROM hourly_history ORDER BY hour_start_ms").fetchall()
            self.assertEqual(len(rows), 6)
            for row in rows:
                self.assertEqual(row["observed_ms"] + row["sleep_ms"] + row["unknown_ms"],
                                 3_600_000)
                self.assertEqual(row["charging_ms"] + row["discharging_ms"] +
                                 row["full_ms"] + row["other_state_ms"], row["observed_ms"])
            first = rows[0]
            self.assertEqual((first["observed_ms"], first["sleep_ms"], first["unknown_ms"]),
                             (360_000, 2_280_000, 960_000))
            self.assertEqual((first["soc_start"], first["soc_end"],
                              first["soc_min"], first["soc_max"]),
                             (20.0, 19.0, 19.0, 22.0))
            self.assertAlmostEqual(first["soc_integral_percent_ms"], 7_410_000.0)
            self.assertEqual((first["charging_ms"], first["discharging_ms"]),
                             (180_000, 180_000))
            self.assertAlmostEqual(first["charged_energy_wh"], 2.22)
            self.assertAlmostEqual(first["discharged_energy_wh"], 1.11)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM hourly_history WHERE hour_start_ms=1704088800000"
            ).fetchone())
            current = db.execute(
                "SELECT * FROM checkpoint_hourly WHERE hour_start_ms=1704088800000"
            ).fetchone()
            self.assertEqual((current["observed_ms"], current["full_ms"]), (180_000, 60_000))
            # Historical profile metadata is not promoted into invented durations.
            self.assertEqual(db.execute("SELECT count(*) FROM hourly_profile_durations").fetchone()[0], 0)

    def test_energy_integrals_and_discontinuity_boundaries(self):
        with self._convert() as db:
            totals = db.execute(
                "SELECT sum(charged_energy_wh), sum(discharged_energy_wh), "
                "sum(charge_power_integral_w_ms), sum(discharge_power_integral_w_ms) "
                "FROM hourly_history"
            ).fetchone()
            # Only adjacent continuous samples contribute. The battery switch/reset,
            # sleeps, reboot, wall-clock jump, and unknown gap contribute no delta.
            self.assertAlmostEqual(totals[0], 2.22, places=7)
            self.assertAlmostEqual(totals[1], 2.22, places=7)
            self.assertGreater(totals[2], 0)
            self.assertGreater(totals[3], 0)

    def test_reboot_clock_jump_and_battery_change_break_continuity(self):
        def sample(timestamp, *, monotonic, boottime, boot="boot", identity="battery"):
            return AggregateSample(timestamp, 50, "discharging", 0, 8, 0, 10,
                                   monotonic, boottime, boot, identity)

        base = sample(1_000_000, monotonic=10_000, boottime=10_000)
        self.assertTrue(_continuous(
            base, sample(1_060_000, monotonic=70_000, boottime=70_000)
        ))
        self.assertFalse(_continuous(
            base, sample(1_060_000, monotonic=70_000, boottime=70_000, boot="reboot")
        ))
        self.assertFalse(_continuous(
            base, sample(1_060_000, monotonic=20_000, boottime=20_000)
        ))
        self.assertFalse(_continuous(
            base, sample(1_060_000, monotonic=70_000, boottime=70_000, identity="replacement")
        ))

    def test_counter_movement_against_state_is_not_counted_as_energy(self):
        changed = self.source.parent / "counter-reset.sqlite3"
        db = sqlite3.connect(self.source)
        try:
            db.executemany(
                "INSERT INTO samples(timestamp, percentage, state, ac_online, power_w, "
                "source, device, energy_wh, monotonic_s, boottime_s, boot_id, battery_identity) "
                "VALUES(?, 100, 'charging', 1, 10, 'fixture', 'BAT1', ?, ?, ?, "
                "'boot-b', 'battery-b')",
                ((1704089040, 3.2, 3490, 3490), (1704089100, 1.0, 3550, 3550)),
            )
            db.commit()
        finally:
            db.close()
        convert_v2_to_v1(self.source, changed)
        out = sqlite3.connect(changed)
        try:
            # The 3.2 -> 1.0 fall while charging is a counter reset, not discharge.
            self.assertAlmostEqual(out.execute(
                "SELECT sum(discharged_energy_wh) FROM hourly_history"
            ).fetchone()[0], 2.22)
        finally:
            out.close()

    def test_switching_native_counter_family_is_rejected(self):
        switched = self.source.parent / "switched.sqlite3"
        db = sqlite3.connect(self.source)
        try:
            db.execute(
                "UPDATE battery_samples SET energy_now_wh=NULL, charge_now_ah=1.9 "
                "WHERE identity='battery-a' AND timestamp=1704067260"
            )
            db.commit()
        finally:
            db.close()
        convert_v2_to_v1(self.source, switched)
        out = sqlite3.connect(switched)
        out.row_factory = sqlite3.Row
        try:
            first = out.execute(
                "SELECT * FROM hourly_history ORDER BY hour_start_ms LIMIT 1"
            ).fetchone()
            self.assertEqual(first["charged_energy_wh"], 0)
            self.assertTrue(first["quality_flags"] & 4)
            self.assertTrue(first["energy_provenance_mask"] & 4)
        finally:
            out.close()

    def test_known_sleep_boundary_never_carries_an_energy_delta(self):
        left = AggregateSample(0, 50, "discharging", 0, 8, 0, 10.0,
                               0, 0, "boot", "battery")
        right = AggregateSample(60_000, 49, "discharging", 0, 8, 0, 9.9,
                                60_000, 60_000, "boot", "battery")
        hour = HourAccumulator(0)
        _add_sample_interval(hour, left, right, 0, 60_000, allow_energy_delta=False)
        self.assertEqual(hour.discharged_energy_wh, 0)
        self.assertEqual(hour.observed_ms, 60_000)

    def test_utc_hour_layout_is_independent_of_local_timezone(self):
        original = os.environ.get("TZ")
        try:
            dumps = []
            for zone, name in (("Europe/Amsterdam", "amsterdam.sqlite3"),
                               ("America/New_York", "new-york.sqlite3")):
                os.environ["TZ"] = zone
                if hasattr(time, "tzset"):
                    time.tzset()
                path = self.source.parent / name
                convert_v2_to_v1(self.source, path)
                db = sqlite3.connect(path)
                try:
                    dumps.append(tuple(db.execute(
                        "SELECT hour_start_ms FROM hourly_history ORDER BY hour_start_ms"
                    )))
                finally:
                    db.close()
            self.assertEqual(dumps[0], dumps[1])
            self.assertTrue(all(row[0] % 3_600_000 == 0 for row in dumps[0]))
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            if hasattr(time, "tzset"):
                time.tzset()

    def test_invalid_source_and_existing_destination_are_refused(self):
        invalid = self.source.parent / "invalid.sqlite3"
        db = sqlite3.connect(invalid)
        db.execute("PRAGMA user_version=1")
        db.close()
        with self.assertRaisesRegex(PreV1ConversionError, "schema v2"):
            convert_v2_to_v1(invalid, self.source.parent / "invalid-output.sqlite3")
        self.destination.write_text("keep")
        with self.assertRaisesRegex(PreV1ConversionError, "already exists"):
            convert_v2_to_v1(self.source, self.destination)
        self.assertEqual(self.destination.read_text(), "keep")

    def test_corrupt_source_is_refused_and_destination_removed(self):
        corrupt = self.source.parent / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        output = self.source.parent / "corrupt-output.sqlite3"
        with self.assertRaisesRegex(PreV1ConversionError, "SQLite conversion failed"):
            convert_v2_to_v1(corrupt, output)
        self.assertFalse(output.exists())

    def test_conversion_failure_removes_partial_destination(self):
        with mock.patch.object(pre_v1_converter, "_insert_hourly",
                               side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                convert_v2_to_v1(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(Path(f"{self.destination}-wal").exists())
        self.assertFalse(Path(f"{self.destination}-shm").exists())

    def test_mid_hour_conversion_seeds_immediately_readable_checkpoint(self):
        convert_v2_to_v1(self.source, self.destination)
        with V1Storage(self.destination).reader() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM hourly_history WHERE hour_start_ms=1704088800000"
            ).fetchone())
            header = db.execute("SELECT * FROM checkpoint_generations").fetchone()
            self.assertEqual((header["generation"], header["last_poll_at_ms"],
                              header["complete"]), (1, 1704088980000, 1))
        view = V1History(self.destination).load(1704067200, now=1704088980)
        self.assertEqual(view.generation, 1)
        self.assertEqual((view.current.timestamp, view.current.percentage),
                         (1704088980, 100.0))
        self.assertTrue(any(item.timestamp >= 1704088800 for item in view.history))

    def test_first_poll_and_utc_rollover_preserve_seeded_partial_hour(self):
        convert_v2_to_v1(self.source, self.destination)

        def poll(timestamp, monotonic):
            raw = RawBatterySnapshot(
                timestamp, monotonic, monotonic, "boot-b", "BAT1", "battery-b",
                100.0, "full", True, voltage_now_v=12.0, energy_now_wh=3.2,
                energy_full_wh=4.0, energy_full_design_wh=6.0, sources=("sysfs",),
            )
            return Measurement(
                timestamp, 100.0, "full", True, power_w=0.0, energy_wh=3.2,
                source="sysfs", device="BAT1", power_method="power-now",
                power_confidence="high", monotonic_s=monotonic,
                boottime_s=monotonic, boot_id="boot-b",
                battery_identity="battery-b", raw_batteries=(raw,),
            )

        collector = V1Collector(V1Storage(self.destination))
        collector.process_poll(poll(1704089040, 3490))
        current = V1Storage(self.destination).recover().snapshot.hourly
        self.assertEqual(current.observed_ms, 240_000)
        collector.process_poll(poll(1704092400, 6850))
        with V1Storage(self.destination).reader() as db:
            finalized = db.execute(
                "SELECT * FROM hourly_history WHERE hour_start_ms=1704088800000"
            ).fetchone()
        self.assertEqual(finalized["observed_ms"], 240_000)
        self.assertEqual(finalized["unknown_ms"], 3_360_000)

    def test_invalid_source_soc_is_rejected_without_destination(self):
        db = sqlite3.connect(self.source)
        try:
            db.execute("UPDATE samples SET percentage=101 WHERE timestamp=(SELECT max(timestamp) FROM samples)")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(PreV1ConversionError, "SoC"):
            convert_v2_to_v1(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_converter_holds_one_source_read_snapshot(self):
        original = pre_v1_converter._validate_source

        def assert_transaction(db):
            self.assertTrue(db.in_transaction)
            return original(db)

        with mock.patch.object(pre_v1_converter, "_validate_source",
                               side_effect=assert_transaction):
            convert_v2_to_v1(self.source, self.destination)


if __name__ == "__main__":
    unittest.main()
