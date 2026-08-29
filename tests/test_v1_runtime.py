from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from battery_status_tui.models import Measurement, RawBatterySnapshot, SleepInterval
from battery_status_tui.recent_series import MAX_WINDOW_MS, decode_recent_series
from battery_status_tui.energy_integrity import (
    ENERGY_PROVENANCE_NATIVE,
    ENERGY_PROVENANCE_REJECTED,
)
from battery_status_tui.v1_collector import V1Collector, V1CollectorError
from battery_status_tui.v1_hourly import QUALITY_ENERGY_REJECTED
from battery_status_tui.v1_hourly import HOUR_MS
from battery_status_tui.v1_runtime import _new_sleep_intervals, _stabilize_identities
from battery_status_tui.v1_storage import MAX_GENERATIONS, V1Storage


def measurement(timestamp: int, *, soc: float = 60.0, state: str = "discharging",
                ac: bool | None = False, energy: float | None = 40.0,
                identity: str = "BAT0", boot: str = "boot-a",
                monotonic: float | None = None, boottime: float | None = None,
                power: float | None = 10.0) -> Measurement:
    monotonic = float(timestamp if monotonic is None else monotonic)
    boottime = float(timestamp if boottime is None else boottime)
    raw = RawBatterySnapshot(
        timestamp, monotonic, boottime, boot, f"/sys/{identity}", identity,
        soc, state, ac, energy_now_wh=energy, energy_full_wh=50.0,
        energy_full_design_wh=55.0, cycle_count=100, sources=("sysfs",),
    )
    return Measurement(
        timestamp, soc, state, ac, power_w=power, energy_wh=energy,
        source="sysfs", device=raw.device, power_method="power-now",
        power_confidence="high", monotonic_s=monotonic, boottime_s=boottime,
        boot_id=boot, battery_identity=identity, raw_batteries=(raw,),
    )


class V1RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.storage = V1Storage(self.path)
        self.collector = V1Collector(self.storage)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def rows(self, table: str) -> list[sqlite3.Row]:
        with self.storage.reader() as db:
            return list(db.execute(f"SELECT * FROM {table}"))

    def test_schema_v4_writer_and_read_only_reader(self) -> None:
        self.storage.initialize_writer()
        with self.storage.reader() as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertNotIn("samples", tables)
            self.assertNotIn("battery_samples", tables)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO metadata VALUES('x','y')")

    def test_checkpoint_identity_survives_transient_metadata_loss(self) -> None:
        previous = measurement(3_600).raw_batteries[0]
        previous = replace(previous, identity="BAT0|PrimaryPack|ABC123")
        missing = replace(previous, identity="BAT0||", timestamp=3_660)
        replacement = replace(previous, identity="BAT0|PrimaryPack|XYZ789", timestamp=3_660)
        self.assertEqual(
            _stabilize_identities((missing,), (previous,))[0].identity,
            previous.identity,
        )
        self.assertEqual(
            _stabilize_identities((replacement,), (previous,))[0].identity,
            replacement.identity,
        )

    def test_cold_start_writes_complete_checkpoint_but_no_poll_history(self) -> None:
        result = self.collector.process_poll(measurement(3_600))
        self.assertTrue(any("cold start" in warning for warning in result.warnings))
        generations = self.rows("checkpoint_generations")
        self.assertEqual((len(generations), generations[0]["complete"]), (1, 1))
        self.assertEqual(len(decode_recent_series(generations[0]["recent_series"])), 1)
        self.assertEqual(len(self.rows("hourly_history")), 0)
        self.assertEqual(len(self.rows("state_events")), 2)
        self.assertEqual(len(self.rows("battery_health")), 1)

    def test_incomplete_generation_is_never_recovered(self) -> None:
        self.storage.initialize_writer()
        with self.storage.transaction() as db:
            db.execute(
                "INSERT INTO checkpoint_generations(generation,format_version,created_at_ms,"
                "last_poll_at_ms,boot_id,monotonic_ns,boottime_ns,configured_interval_ms,"
                "battery_count,hourly_count,profile_count,recent_series,payload_digest,complete) "
                "VALUES(1,1,1,1,'boot',1,1,60000,0,0,0,?, ?,0)",
                (b"incomplete", "0" * 64),
            )
        recovery = self.storage.recover()
        self.assertIsNone(recovery.snapshot)
        self.assertTrue(any("cold start" in warning for warning in recovery.warnings))

    def test_state_and_health_events_only_on_change(self) -> None:
        self.collector.process_poll(measurement(3_600))
        original_last_seen = self.rows("batteries")[0]["last_seen_ms"]
        self.collector.process_poll(measurement(3_660, soc=60.0, energy=39.9))
        self.assertEqual(len(self.rows("state_events")), 2)
        self.assertEqual(len(self.rows("battery_health")), 1)
        self.assertEqual(self.rows("batteries")[0]["last_seen_ms"], original_last_seen)
        changed = measurement(3_720, soc=59.0, energy=39.8)
        raw = replace(changed.raw_batteries[0], energy_full_wh=49.0)
        self.collector.process_poll(replace(changed, raw_batteries=(raw,)))
        self.assertEqual(len(self.rows("state_events")), 2)
        self.assertEqual(len(self.rows("battery_health")), 2)

    def test_soc_only_change_does_not_create_permanent_state_event(self) -> None:
        self.collector.process_poll(measurement(3_600, soc=60))
        self.collector.process_poll(measurement(3_660, soc=59, energy=39.9))
        self.assertEqual(len(self.rows("state_events")), 2)
        points = decode_recent_series(self.storage.recover().snapshot.recent_series)
        self.assertEqual([point.soc_millipercent for point in points], [60_000, 59_000])

    def test_cleanup_keeps_at_most_three_valid_generations(self) -> None:
        for index in range(5):
            self.collector.process_poll(measurement(3_600 + index * 60))
        self.assertEqual(len(self.rows("checkpoint_generations")), MAX_GENERATIONS)
        self.assertEqual([row["generation"] for row in self.rows("checkpoint_generations")],
                         [3, 4, 5])

    def test_crash_before_commit_rolls_back_everything(self) -> None:
        self.collector.process_poll(measurement(3_600))
        counts = {name: len(self.rows(name)) for name in (
            "checkpoint_generations", "state_events", "battery_health", "sessions"
        )}
        def fail(stage: str) -> None:
            if stage == "before-commit":
                raise RuntimeError("injected")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.collector.process_poll(measurement(3_660, soc=59), failpoint=fail)
        self.assertEqual(counts, {name: len(self.rows(name)) for name in counts})

    def test_crash_after_generation_commit_leaves_new_generation_recoverable(self) -> None:
        for index in range(3):
            self.collector.process_poll(measurement(3_600 + index * 60), cleanup=False)
        def fail(stage: str) -> None:
            if stage == "after-generation-commit":
                raise RuntimeError("injected")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.collector.process_poll(measurement(3_780), cleanup=True, failpoint=fail)
        self.assertEqual(len(self.rows("checkpoint_generations")), 4)
        self.assertEqual(self.storage.recover().snapshot.generation, 4)
        self.storage.cleanup_generations()
        self.assertEqual(len(self.rows("checkpoint_generations")), 3)

    def test_corrupt_newest_generation_falls_back(self) -> None:
        self.collector.process_poll(measurement(3_600), cleanup=False)
        self.collector.process_poll(measurement(3_660), cleanup=False)
        with self.storage.transaction() as db:
            db.execute("UPDATE checkpoint_generations SET payload_digest=? WHERE generation=2",
                       ("0" * 64,))
        recovery = self.storage.recover()
        self.assertEqual(recovery.snapshot.generation, 1)
        self.assertTrue(any("generation 2 invalid" in item for item in recovery.warnings))

    def test_fallback_does_not_duplicate_or_rewrite_finalized_hour(self) -> None:
        self.collector.process_poll(measurement(3_590), cleanup=False)
        self.collector.process_poll(measurement(3_610, soc=59.9), cleanup=False)
        original = dict(self.rows("hourly_history")[0])
        with self.storage.transaction() as db:
            db.execute("UPDATE checkpoint_generations SET payload_digest=? WHERE generation=2",
                       ("0" * 64,))
        result = self.collector.process_poll(measurement(3_670, soc=59.8), cleanup=False)
        self.assertTrue(any("generation 2 invalid" in item for item in result.warnings))
        self.assertEqual(dict(self.rows("hourly_history")[0]), original)
        self.assertEqual(len(self.rows("hourly_history")), 1)

    def test_same_boot_accumulates_observed_and_energy(self) -> None:
        self.collector.process_poll(measurement(3_600, energy=40.0))
        self.collector.process_poll(measurement(3_660, soc=59.5, energy=39.8))
        hourly = self.storage.recover().snapshot.hourly
        self.assertEqual(hourly.observed_ms, 60_000)
        self.assertEqual(hourly.discharging_ms, 60_000)
        self.assertAlmostEqual(hourly.discharged_energy_wh, 0.2)
        self.assertEqual(hourly.energy_provenance_mask, ENERGY_PROVENANCE_NATIVE)
        self.assertEqual(hourly.profiles, {})

    def test_implausible_energy_delta_is_rejected_not_clamped(self) -> None:
        self.collector.process_poll(measurement(3_600, energy=40.0))
        self.collector.process_poll(measurement(3_660, soc=59, energy=20.0))
        hourly = self.storage.recover().snapshot.hourly
        self.assertEqual(hourly.discharged_energy_wh, 0)
        self.assertTrue(hourly.quality_flags & QUALITY_ENERGY_REJECTED)
        self.assertTrue(hourly.energy_provenance_mask & ENERGY_PROVENANCE_REJECTED)

    def test_switching_counter_provenance_rejects_energy_delta(self) -> None:
        first = measurement(3_600, energy=40.0)
        second = measurement(3_660, soc=59, energy=None)
        raw = replace(second.raw_batteries[0], energy_now_wh=None,
                      charge_now_ah=3.0, voltage_now_v=12.0)
        second = replace(second, energy_wh=None, raw_batteries=(raw,))
        self.collector.process_poll(first)
        self.collector.process_poll(second)
        hourly = self.storage.recover().snapshot.hourly
        self.assertEqual(hourly.discharged_energy_wh, 0)
        self.assertTrue(hourly.quality_flags & QUALITY_ENERGY_REJECTED)

    def test_invalid_soc_preserves_previous_checkpoint(self) -> None:
        self.collector.process_poll(measurement(3_600, soc=60))
        before = self.storage.recover().snapshot
        counts = {table: len(self.rows(table)) for table in
                  ("checkpoint_generations", "state_events", "hourly_history")}
        with self.assertRaisesRegex(V1CollectorError, "SoC 101"):
            self.collector.process_poll(measurement(3_660, soc=101))
        after = self.storage.recover().snapshot
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(counts, {table: len(self.rows(table)) for table in counts})

    def test_invalid_boundaries_are_unknown_and_do_not_integrate_energy(self) -> None:
        cases = (
            measurement(4_200, energy=30.0),
            measurement(3_660, energy=30.0, boot="boot-b"),
            measurement(3_660, energy=30.0, monotonic=3_610, boottime=3_610),
            measurement(3_660, energy=30.0, identity="BAT1"),
        )
        for current in cases:
            with self.subTest(current=current):
                with tempfile.TemporaryDirectory() as temp:
                    storage = V1Storage(Path(temp) / "db.sqlite3")
                    collector = V1Collector(storage)
                    collector.process_poll(measurement(3_600, energy=40.0))
                    collector.process_poll(current)
                    hourly = storage.recover().snapshot.hourly
                    self.assertGreater(hourly.unknown_ms, 0)
                    self.assertEqual(hourly.discharged_energy_wh, 0)

    def test_session_direction_change_closes_and_opens_without_delta(self) -> None:
        self.collector.process_poll(measurement(3_600, energy=40.0))
        charging = measurement(3_660, soc=61, state="charging", ac=True, energy=40.2)
        self.collector.process_poll(charging)
        sessions = sorted(self.rows("sessions"), key=lambda row: row["id"])
        self.assertEqual([(row["kind"], row["ended_at_ms"] is None) for row in sessions],
                         [("discharging", False), ("charging", True)])
        hourly = self.storage.recover().snapshot.hourly
        self.assertEqual(hourly.unknown_ms, 60_000)
        self.assertEqual(hourly.charged_energy_wh, 0)

    def test_wal_reader_can_read_while_writer_transaction_is_open(self) -> None:
        self.storage.initialize_writer()
        with self.storage.transaction() as writer:
            writer.execute("INSERT INTO metadata VALUES('pending','value')")
            with self.storage.reader() as reader:
                self.assertIsNone(reader.execute(
                    "SELECT value FROM metadata WHERE key='pending'"
                ).fetchone())

    def test_proven_sleep_is_separate_and_blocks_energy_delta(self) -> None:
        self.collector.process_poll(measurement(3_600, energy=40, monotonic=100, boottime=100))
        sleep = SleepInterval(3_660, 7_140, source="journal", boot_id="boot-a",
                              pre_percentage=60, post_percentage=59)
        self.collector.process_poll(measurement(
            7_200, soc=59, energy=39, monotonic=220, boottime=3_700
        ), sleeps=(sleep,))
        rows = self.rows("hourly_history")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["sleep_ms"], 3_480_000)
        self.assertEqual(row["observed_ms"], 120_000)
        self.assertEqual(row["unknown_ms"], 0)
        self.assertEqual(row["discharged_energy_wh"], 0)
        self.assertEqual(row["source_generation"], 2)

    def test_battery_hibernation_across_restart_is_recovered_before_first_poll(self) -> None:
        previous = measurement(3_600, soc=3, energy=2, boot="old-boot",
                               monotonic=3_600, boottime=3_600)
        self.collector.process_poll(previous)
        current = measurement(14_400, soc=4, state="charging", ac=True, energy=2.1,
                              boot="new-boot", monotonic=120, boottime=120)
        proven = SleepInterval(3_638, 14_280, kind="hibernate", source="journal",
                               boot_id="oldboot")
        calls = []

        def journal_lookup(since: int):
            calls.append(since)
            return (proven,)

        sleeps = _new_sleep_intervals(previous.raw_batteries, current.raw_batteries,
                                      journal_lookup)
        self.assertEqual(sleeps, (proven,))
        self.assertEqual(calls, [3_540])
        self.collector.process_poll(current, sleeps=sleeps)

        stored = self.rows("sleep_intervals")
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            (stored[0]["started_at_ms"], stored[0]["ended_at_ms"], stored[0]["kind"],
             stored[0]["source"], stored[0]["pre_soc"], stored[0]["post_soc"]),
            (3_638_000, 14_280_000, "hibernate", "journal", 3, 4),
        )
        rows = {row["hour_start_ms"]: row for row in self.rows("hourly_history")}
        self.assertEqual(rows[3_600_000]["sleep_ms"], 3_562_000)
        self.assertEqual(rows[7_200_000]["sleep_ms"], 3_600_000)
        self.assertEqual(rows[10_800_000]["sleep_ms"], 3_480_000)
        self.assertEqual(rows[10_800_000]["unknown_ms"], 120_000)

    def test_clock_resume_gap_is_recovered_and_persisted_automatically(self) -> None:
        self.collector.process_poll(measurement(3_600, monotonic=100, boottime=100))
        self.collector.process_poll(measurement(
            7_200, soc=59, monotonic=220, boottime=3_700
        ))
        sleeps = self.rows("sleep_intervals")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0]["source"], "clocks")
        self.assertEqual((sleeps[0]["pre_soc"], sleeps[0]["post_soc"]), (60, 59))
        hour = self.rows("hourly_history")[0]
        self.assertEqual(hour["sleep_ms"], 3_480_000)
        self.assertEqual(hour["observed_ms"], 120_000)

    def test_supplied_journal_sleep_wins_over_clock_fallback(self) -> None:
        self.collector.process_poll(measurement(3_600, monotonic=100, boottime=100))
        journal = SleepInterval(3_710, 7_190, source="journal", boot_id="boot-a")
        self.collector.process_poll(measurement(
            7_200, soc=59, monotonic=220, boottime=3_700
        ), sleeps=(journal,))
        sleeps = self.rows("sleep_intervals")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0]["source"], "journal")
        self.assertEqual((sleeps[0]["pre_soc"], sleeps[0]["post_soc"]), (60, 59))

    def test_late_journal_bounds_replace_clock_interval_without_duplicate(self) -> None:
        self.collector.process_poll(measurement(3_600, monotonic=100, boottime=100))
        self.collector.process_poll(measurement(
            7_200, soc=59, monotonic=220, boottime=3_700
        ))
        journal = SleepInterval(3_710, 7_190, source="journal", boot_id="boot-a")
        self.collector.process_poll(measurement(
            7_260, soc=59, monotonic=280, boottime=3_760
        ), sleeps=(journal,))
        sleeps = self.rows("sleep_intervals")
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(
            (sleeps[0]["started_at_ms"], sleeps[0]["ended_at_ms"],
             sleeps[0]["source"], sleeps[0]["revision"]),
            (3_710_000, 7_190_000, "journal", 2),
        )

    def test_utc_hour_finalization_has_exact_invariants(self) -> None:
        self.collector.process_poll(measurement(3_590, energy=40.0), profile="balanced")
        result = self.collector.process_poll(measurement(3_610, energy=39.9), profile="balanced")
        self.assertEqual(result.finalized_hours, (0,))
        row = self.rows("hourly_history")[0]
        self.assertEqual(row["observed_ms"] + row["sleep_ms"] + row["unknown_ms"], HOUR_MS)
        self.assertEqual(row["charging_ms"] + row["discharging_ms"] + row["full_ms"]
                         + row["other_state_ms"], row["observed_ms"])
        profiles = self.rows("hourly_profile_durations")
        self.assertEqual((profiles[0]["profile"], profiles[0]["duration_ms"]),
                         ("balanced", 10_000))

    def test_late_sleep_revision_is_idempotent(self) -> None:
        self.collector.process_poll(measurement(3_600, monotonic=100, boottime=100))
        self.collector.process_poll(measurement(10_800, monotonic=7_300, boottime=7_300))
        before = {row["hour_start_ms"]: (row["unknown_ms"], row["revision"])
                  for row in self.rows("hourly_history")}
        sleep = SleepInterval(3_660, 7_140, source="journal", boot_id="boot-a",
                              pre_percentage=60, post_percentage=60)
        self.collector.process_poll(measurement(10_860, monotonic=7_360, boottime=7_360),
                                    sleeps=(sleep,))
        first = {row["hour_start_ms"]: (row["sleep_ms"], row["unknown_ms"], row["revision"])
                 for row in self.rows("hourly_history")}
        self.collector.process_poll(measurement(10_920, monotonic=7_420, boottime=7_420),
                                    sleeps=(sleep,))
        second = {row["hour_start_ms"]: (row["sleep_ms"], row["unknown_ms"], row["revision"])
                  for row in self.rows("hourly_history")}
        self.assertEqual(first, second)
        self.assertGreater(first[3_600_000][0], 0)
        self.assertGreater(first[3_600_000][2], before[3_600_000][1])

    def test_recent_series_is_bounded_to_eight_hours(self) -> None:
        for index in range(11):
            self.collector.process_poll(measurement(3_600 + index * 3_600))
        points = decode_recent_series(self.storage.recover().snapshot.recent_series)
        self.assertLessEqual(points[-1].timestamp_ms - points[0].timestamp_ms, MAX_WINDOW_MS)
        self.assertEqual(len(points), 9)

    def test_source_generation_is_audit_not_checkpoint_fk(self) -> None:
        self.collector.process_poll(measurement(3_590))
        self.collector.process_poll(measurement(3_610, soc=59))
        for index in range(2, 6):
            self.collector.process_poll(measurement(3_610 + index * 60, soc=59))
        generations = {row["generation"] for row in self.rows("checkpoint_generations")}
        hour = self.rows("hourly_history")[0]
        self.assertNotIn(hour["source_generation"], generations)


if __name__ == "__main__":
    unittest.main()
