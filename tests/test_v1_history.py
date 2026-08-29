from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from battery_status_tui.graph import GRAPH_OFFSET, GRAPH_WIDTH, NOW_INDEX, chart_rows
from battery_status_tui.models import Measurement, RawBatterySnapshot, SleepInterval
from battery_status_tui.power import PowerResolver
from battery_status_tui.schema import V2_CREATE_STATEMENTS
from battery_status_tui.v1_collector import V1Collector
from battery_status_tui.v1_history import V1History, V1HistoryError
from battery_status_tui.v1_hourly import HOUR_MS, HourlyAccumulator
from battery_status_tui.v1_runtime import collect_v1, render_v1
from battery_status_tui.v1_storage import V1Storage, V1StorageError


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value: str) -> str:
    return ANSI.sub("", value)


def sample(timestamp: int, *, soc: float = 60.0, state: str = "discharging",
           ac: bool = False, energy: float = 40.0, boot: str = "boot-a",
           monotonic: float | None = None, boottime: float | None = None,
           identity: str = "BAT0") -> Measurement:
    monotonic = float(timestamp if monotonic is None else monotonic)
    boottime = float(timestamp if boottime is None else boottime)
    raw = RawBatterySnapshot(
        timestamp, monotonic, boottime, boot, f"/sys/{identity}", identity,
        soc, state, ac, energy_now_wh=energy, energy_full_wh=50,
        energy_full_design_wh=80, cycle_count=120, sources=("sysfs",),
    )
    return Measurement(
        timestamp, soc, state, ac, power_w=10, energy_wh=energy,
        power_method="power-now", power_confidence="high", source="sysfs",
        monotonic_s=monotonic, boottime_s=boottime, boot_id=boot,
        battery_identity=identity, raw_batteries=(raw,),
    )


class V1HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v4.sqlite3"
        self.storage = V1Storage(self.path)
        self.collector = V1Collector(self.storage)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def insert_hour(self, hour: int, *, soc_start: float = 70, soc_end: float = 65,
                    observed_ms: int = HOUR_MS, revision: int = 1,
                    profile: str | None = None) -> None:
        accumulator = HourlyAccumulator(hour, "BAT0")
        accumulator.add_observed(
            observed_ms, soc_start, soc_end, "discharging", False, 10, False,
            profile, -0.5,
        )
        accumulator.finalize()
        values = accumulator.finalized_values(1, hour + HOUR_MS, revision=revision)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self.storage.transaction() as db:
            db.execute(f"INSERT INTO hourly_history({columns}) VALUES({placeholders})",
                       tuple(values.values()))
            for name, duration in accumulator.profiles.items():
                db.execute("INSERT INTO hourly_profile_durations VALUES(?,?,?)",
                           (hour, name, duration))

    def test_synthetic_v4_database_renders_locked_dashboard(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now), profile="balanced")
        output = plain(render_v1(self.storage, now=now))
        lines = output.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("BATTERY", lines[0])
        self.assertIn("SoC 60% ↓  10.0 W (balanced)", lines[0])
        self.assertTrue(lines[4].endswith("SoH 62.5%"))
        self.assertEqual(lines[1].index("│"), GRAPH_OFFSET + NOW_INDEX)

    def test_open_hour_comes_from_checkpoint_and_recent_series(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now, soc=60))
        self.collector.process_poll(sample(now + 60, soc=59.5, energy=39.9))
        view = V1History(self.path).load(now - 3600, now=now + 60)
        self.assertEqual([item.percentage for item in view.history], [60.0])
        self.assertEqual(view.current.percentage, 59.5)
        self.assertEqual(view.current.energy_wh, 39.9)
        self.assertEqual(view.hourly_accumulator.observed_ms, 60_000)

    def test_stale_checkpoint_still_supplies_current_without_fake_recent_history(self) -> None:
        timestamp = 2 * 3600
        self.collector.process_poll(sample(timestamp, soc=58))
        view = V1History(self.path).load(9 * 3600, now=10 * 3600)
        self.assertEqual(view.current.percentage, 58)
        self.assertEqual(view.history, ())
        self.assertEqual(view.trend_history, ())

    def test_finalized_hour_is_used_without_recent_series_double_counting(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now))
        self.insert_hour(8 * HOUR_MS, profile="balanced")
        view = V1History(self.path).load(8 * 3600, now=now)
        hourly = [item for item in view.history if item.source == "hourly-history"]
        self.assertEqual(len(hourly), 2)
        timestamps = [item.timestamp for item in view.history]
        self.assertEqual(len(timestamps), len(set(timestamps)))
        self.assertEqual(view.hourly_profiles, {8 * HOUR_MS: {"balanced": HOUR_MS}})

    def test_utc_boundary_never_combines_hourly_and_recent_for_same_hour(self) -> None:
        self.collector.process_poll(sample(3_590, soc=61))
        self.collector.process_poll(sample(3_610, soc=60.9, energy=39.9))
        view = V1History(self.path).load(0, now=3_610)
        hour_zero = [item for item in view.history if item.timestamp < 3_600]
        self.assertEqual(len(hour_zero), 1)
        self.assertEqual(hour_zero[0].source, "checkpoint-recent-series")
        self.assertFalse(any(item.source == "hourly-history" for item in hour_zero))

    def test_revised_finalized_hour_is_read_without_cache(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now))
        self.insert_hour(8 * HOUR_MS, soc_start=70, soc_end=65)
        history = V1History(self.path)
        self.assertEqual(history.load(8 * 3600, now=now).history[0].percentage, 70)
        with self.storage.transaction() as db:
            db.execute(
                "UPDATE hourly_history SET revision=2,soc_start=69,soc_min=64 WHERE hour_start_ms=?",
                (8 * HOUR_MS,),
            )
        self.assertEqual(history.load(8 * 3600, now=now).history[0].percentage, 69)

    def test_continuity_break_starts_a_new_trend_segment(self) -> None:
        start = 10 * 3600
        self.collector.process_poll(sample(start, soc=70, monotonic=100, boottime=100))
        self.collector.process_poll(sample(start + 600, soc=65, monotonic=700, boottime=700))
        self.collector.process_poll(sample(start + 660, soc=64.5, monotonic=760, boottime=760))
        view = V1History(self.path).load(start - 3600, now=start + 660)
        self.assertEqual([item.timestamp for item in view.trend_history],
                         [start + 600, start + 660])

    def test_permanent_sleep_and_recent_points_preserve_mixed_bucket_rendering(self) -> None:
        start = 10 * 3600
        self.collector.process_poll(sample(start, soc=67, monotonic=100, boottime=100))
        sleep = SleepInterval(start + 60, start + 430, source="journal", boot_id="boot-a",
                              pre_percentage=67, post_percentage=64)
        self.collector.process_poll(sample(
            start + 600, soc=61, monotonic=330, boottime=700
        ), sleeps=(sleep,))
        now = start + 1200
        view = V1History(self.path).load(start - 1200, now=now)
        self.assertEqual(view.sleeps, (sleep,))
        top, bottom = chart_rows(view.current, view.history, None, now, view.sleeps)
        self.assertTrue(any(0x2800 <= ord(char) <= 0x28FF for char in top + bottom))
        self.assertEqual((top[NOW_INDEX], bottom[NOW_INDEX]), ("│", "│"))
        self.assertEqual([item.timestamp for item in view.trend_history], [start + 600])

    def test_partial_hourly_only_history_does_not_invent_subhour_points(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now))
        self.insert_hour(8 * HOUR_MS, observed_ms=30 * 60 * 1000)
        view = V1History(self.path).load(8 * 3600, now=now)
        self.assertFalse(any(item.source == "hourly-history" for item in view.history))

    def test_health_profile_and_open_closed_sessions_come_from_v4(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now), profile="performance")
        charging = sample(now + 60, soc=61, state="charging", ac=True, energy=40.1)
        self.collector.process_poll(charging, profile="power-saver")
        view = V1History(self.path).load(now - 3600, now=now + 60)
        self.assertAlmostEqual(view.health.percent, 62.5)
        self.assertEqual(view.power_profile, "power-saver")
        self.assertEqual(view.session.kind, "charging")
        sessions = V1History(self.path).all_sessions()
        self.assertEqual([(item.kind, item.ended_at is None) for item in sessions],
                         [("discharging", False), ("charging", True)])

    def test_charging_and_discharging_forecasts_still_render_right_of_now(self) -> None:
        for state, ac, start_soc, step in (
            ("discharging", False, 70, -1),
            ("charging", True, 30, 1),
        ):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                storage = V1Storage(Path(temp) / "db.sqlite3")
                collector = V1Collector(storage, configured_interval_ms=300_000)
                start = 10 * 3600
                for index in range(5):
                    collector.process_poll(sample(
                        start + index * 300, soc=start_soc + step * index,
                        state=state, ac=ac, energy=40 + step * index / 10,
                    ))
                view = V1History(storage.path).load(start - 3600, now=start + 1200)
                rendered = plain(render_v1(storage, now=start + 1200))
                graph_rows = rendered.splitlines()[1:3]
                right = "".join(row[GRAPH_OFFSET + NOW_INDEX + 1:] for row in graph_rows)
                self.assertTrue(any(0x2800 <= ord(char) <= 0x28FF for char in right))
                self.assertGreater(len(view.trend_history), 3)

    def test_charge_checkpoint_reconstructs_energy_and_forecast_before_trend(self) -> None:
        now = 10 * 3600
        voltage = 10.0
        charge = 2.0
        raw = RawBatterySnapshot(
            now, float(now), float(now), "boot-a", "/sys/BAT0", "BAT0",
            50, "discharging", False, voltage_now_v=voltage,
            energy_now_wh=None, charge_now_ah=charge, charge_full_ah=4.0,
            charge_full_design_ah=5.0, sources=("sysfs",),
        )
        current = Measurement(
            now, 50, "discharging", False, power_w=10.0,
            energy_wh=charge * voltage, energy_full_wh=40.0,
            energy_full_design_wh=50.0, source="sysfs", power_method="power-now",
            power_confidence="high", monotonic_s=float(now), boottime_s=float(now),
            boot_id="boot-a", battery_identity="BAT0", raw_batteries=(raw,),
        )
        self.collector.process_poll(current, profile="balanced")

        view = V1History(self.path).load(now - 3600, now=now)
        self.assertEqual(view.trend_history, (view.current,))
        self.assertAlmostEqual(view.current.energy_wh, 20.0)
        self.assertAlmostEqual(view.current.energy_full_wh, 40.0)
        self.assertAlmostEqual(view.current.energy_full_design_wh, 50.0)

        output = plain(render_v1(self.storage, now=now))
        graph_rows = output.splitlines()[1:3]
        right = "".join(row[GRAPH_OFFSET + NOW_INDEX + 1:] for row in graph_rows)
        self.assertTrue(any(0x2800 <= ord(char) <= 0x28FF for char in right))
        self.assertIn("2h00 ~", output)

    def test_read_only_view_does_not_change_database_rows(self) -> None:
        now = 10 * 3600
        self.collector.process_poll(sample(now))
        with self.storage.reader() as db:
            tables = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            before = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                      for name in tables}
            total_before = db.total_changes
        render_v1(self.storage, now=now)
        with self.storage.reader() as db:
            after = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                     for name in tables}
            self.assertEqual(after, before)
            self.assertEqual(db.total_changes, 0)
        self.assertEqual(total_before, 0)

    def test_wrong_schema_is_rejected_without_migration(self) -> None:
        v2 = Path(self.temp.name) / "v2.sqlite3"
        db = sqlite3.connect(v2)
        for statement in V2_CREATE_STATEMENTS:
            db.execute(statement)
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()
        with self.assertRaisesRegex(V1HistoryError, "schema v2|not schema v4"):
            V1History(v2).load(0)
        check = sqlite3.connect(v2)
        self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 2)
        check.close()

    def test_explicit_trial_collector_uses_only_supplied_v4_path(self) -> None:
        class Source:
            resolver = PowerResolver()

            @staticmethod
            def read_raw(now: int) -> tuple[RawBatterySnapshot, ...]:
                return sample(now, soc=55).raw_batteries

        measurement, result = collect_v1(
            Source(), self.storage, timestamp=10 * 3600, profile="balanced",
            journal_lookup=None,
        )
        self.assertEqual((measurement.percentage, result.generation), (55, 1))
        with self.storage.reader() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("samples", tables)
        self.assertNotIn("battery_samples", tables)

    def test_trial_collector_refuses_v2_before_polling_source(self) -> None:
        v2 = Path(self.temp.name) / "collector-v2.sqlite3"
        db = sqlite3.connect(v2)
        for statement in V2_CREATE_STATEMENTS:
            db.execute(statement)
        db.execute("PRAGMA user_version=2")
        db.commit()
        db.close()

        class Source:
            resolver = PowerResolver()
            called = False

            def read_raw(self, _now: int) -> tuple[RawBatterySnapshot, ...]:
                self.called = True
                return ()

        source = Source()
        with self.assertRaisesRegex(V1StorageError, "refuses database schema v2"):
            collect_v1(source, V1Storage(v2), timestamp=10 * 3600,
                       journal_lookup=None)
        self.assertFalse(source.called)


if __name__ == "__main__":
    unittest.main()
