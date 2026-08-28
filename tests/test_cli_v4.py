"""Regression coverage: the normal CLI entrypoint must operate on schema v4.

These tests exercise ``battery_status_tui.cli.main`` itself -- the same callable
the ``./battery-status-tui`` wrapper invokes -- against schema-v4 databases, so
the failure that forced the production rollback (the normal runtime rejecting a
supported schema-v4 database) cannot regress.
"""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from battery_status_tui import cli
from battery_status_tui.models import Measurement, RawBatterySnapshot
from battery_status_tui.power import PowerResolver
from battery_status_tui.recent_series import decode_recent_series
from battery_status_tui.storage import Storage
from battery_status_tui.v1_storage import MAX_GENERATIONS, V1Storage


BOOT = "boot-cli-v4"
POLL_INTERVAL_S = 400


class FakeClock:
    """A wall clock the test advances explicitly between CLI invocations."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSource:
    resolver = PowerResolver()

    def __init__(self, *, soc: float = 61.0, state: str = "charging",
                 ac: bool | None = True, identity: str = "BAT0|Primary|SER123") -> None:
        self.soc = soc
        self.state = state
        self.ac = ac
        self.identity = identity
        self.reads = 0

    def _raw(self, now: int) -> RawBatterySnapshot:
        return RawBatterySnapshot(
            now, float(now), float(now), BOOT, "/sys/class/power_supply/BAT0",
            self.identity, self.soc, self.state, self.ac,
            power_now_w=27.0, current_now_a=2.1, voltage_now_v=12.6,
            energy_now_wh=24.0, energy_full_wh=40.0, energy_full_design_wh=62.0,
            cycle_count=75, sources=("sysfs",),
        )

    def read_raw(self, now: int | None = None) -> tuple[RawBatterySnapshot, ...]:
        self.reads += 1
        return (self._raw(int(now)),)

    def read(self, now: int | None = None, history=(), sleep_intervals=()) -> Measurement:
        moment = int(now if now is not None else 0)
        raw = self._raw(moment)
        return Measurement(
            moment, self.soc, self.state, self.ac, power_w=27.0, voltage_v=12.6,
            current_a=2.1, energy_wh=24.0, energy_full_wh=40.0,
            energy_full_design_wh=62.0, cycle_count=75, source="sysfs+upower",
            device="BAT0", power_method="current-voltage", power_confidence="high",
            monotonic_s=float(moment), boottime_s=float(moment), boot_id=BOOT,
            battery_identity=raw.identity, raw_batteries=(raw,),
        )


class _NoProfile:
    def resolve(self):
        return None

    def invalidate(self) -> None:
        pass


def make_v4_db(path: Path) -> None:
    V1Storage(path).initialize_writer()


def user_version(path: Path) -> int:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def run_cli(args: list[str], source: FakeSource,
            clock: FakeClock | None = None) -> tuple[int, str]:
    buffer = io.StringIO()
    patches = [
        patch("battery_status_tui.cli.BatterySource", return_value=source),
        patch("battery_status_tui.cli.journal_intervals", lambda since: []),
        patch("battery_status_tui.cli.PowerProfileResolver", _NoProfile),
    ]
    if clock is not None:
        patches.append(patch("battery_status_tui.cli.time.time", clock.time))
    with redirect_stdout(buffer):
        for item in patches:
            item.start()
        try:
            code = cli.main(args)
        finally:
            for item in reversed(patches):
                item.stop()
    return code, buffer.getvalue()


class V4CliRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.clock = FakeClock()

    def rows(self, table: str) -> list[sqlite3.Row]:
        with V1Storage(self.path).reader() as db:
            return list(db.execute(f"SELECT * FROM {table}"))

    def poll_once(self, source: FakeSource) -> tuple[int, str]:
        code, out = run_cli(
            ["--database", str(self.path), "--interval", str(POLL_INTERVAL_S),
             "--once"], source, self.clock,
        )
        self.clock.advance(POLL_INTERVAL_S)
        return code, out

    # ------------------------------------------------------------------
    # Regression: the exact failure that forced the rollback
    # ------------------------------------------------------------------
    def test_supported_v4_database_is_usable_by_normal_entrypoint(self) -> None:
        make_v4_db(self.path)
        code, out = self.poll_once(FakeSource())
        self.assertEqual(code, 0, out)
        self.assertEqual(user_version(self.path), 4)
        self.assertEqual(len(self.rows("checkpoint_generations")), 1)

    def test_v4_database_never_touches_the_legacy_v2_writer(self) -> None:
        make_v4_db(self.path)
        with patch("battery_status_tui.cli.Storage") as legacy:
            code, _ = run_cli(["--database", str(self.path), "--sample"],
                              FakeSource(), self.clock)
        self.assertEqual(code, 0)
        legacy.assert_not_called()

    def test_fresh_database_is_born_at_schema_v4(self) -> None:
        self.assertFalse(self.path.exists())
        code, _ = self.poll_once(FakeSource())
        self.assertEqual(code, 0)
        self.assertEqual(user_version(self.path), 4)

    # ------------------------------------------------------------------
    # Required CLI modes against schema v4
    # ------------------------------------------------------------------
    def test_once_renders_dashboard_from_v4(self) -> None:
        make_v4_db(self.path)
        code, out = self.poll_once(FakeSource())
        self.assertEqual(code, 0)
        self.assertNotIn("battery-status-tui:", out)
        self.assertGreaterEqual(len(out.splitlines()), 3)

    def test_diagnose_reads_v4_and_writes_no_checkpoint(self) -> None:
        make_v4_db(self.path)
        code, out = run_cli(["--database", str(self.path), "--diagnose"], FakeSource())
        self.assertEqual(code, 0)
        self.assertIn(f"database: {self.path}", out)
        self.assertIn("battery identity: BAT0|Primary|SER123", out)
        self.assertIn("active session:", out)
        self.assertEqual(len(self.rows("checkpoint_generations")), 0)

    def test_sample_line_and_checkpoint_from_v4(self) -> None:
        make_v4_db(self.path)
        code, out = run_cli(["--database", str(self.path), "--sample"], FakeSource(),
                            self.clock)
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^\d+ 61\.0% charging ~?27\.00W$")
        self.assertEqual(len(self.rows("checkpoint_generations")), 1)

    def test_default_mode_renders_once_when_stdout_is_not_a_tty(self) -> None:
        make_v4_db(self.path)
        code, out = run_cli(["--database", str(self.path),
                             "--interval", str(POLL_INTERVAL_S)],
                            FakeSource(), self.clock)
        self.assertEqual(code, 0)
        self.assertNotIn("battery-status-tui:", out)

    # ------------------------------------------------------------------
    # Multi-poll invariants through the normal entrypoint
    # ------------------------------------------------------------------
    def test_repeated_normal_polls_hold_every_v4_invariant(self) -> None:
        make_v4_db(self.path)
        source = FakeSource()
        for index in range(12):
            source.soc = 40.0 + index  # SoC drift only -> no permanent state event
            code, out = self.poll_once(source)
            self.assertEqual(code, 0, out)

        generations = self.rows("checkpoint_generations")
        self.assertLessEqual(len(generations), MAX_GENERATIONS)
        self.assertTrue(all(row["complete"] == 1 for row in generations))
        self.assertEqual([row["generation"] for row in generations],
                         sorted(row["generation"] for row in generations))

        hourly = self.rows("hourly_history")
        self.assertGreaterEqual(len(hourly), 1)  # at least one finalized hour
        hours = [row["hour_start_ms"] for row in hourly]
        self.assertEqual(len(hours), len(set(hours)))  # no duplicate finalized hours
        for row in hourly:
            self.assertEqual(row["is_final"], 1)
            self.assertEqual(row["observed_ms"] + row["sleep_ms"] + row["unknown_ms"],
                             3_600_000)
            self.assertEqual(row["charging_ms"] + row["discharging_ms"]
                             + row["full_ms"] + row["other_state_ms"], row["observed_ms"])

        # No permanent per-poll telemetry.
        self.assertEqual({row["identity"] for row in self.rows("batteries")},
                         {"BAT0|Primary|SER123"})
        battery_events = [row for row in self.rows("state_events")
                          if row["scope"] == "battery"]
        self.assertEqual(len(battery_events), 1)
        self.assertLessEqual(len(self.rows("state_events")), 2)
        self.assertLessEqual(len(self.rows("checkpoint_batteries")), MAX_GENERATIONS)

        with V1Storage(self.path).reader() as db:
            self.assertEqual([r[0] for r in db.execute("PRAGMA quick_check")], ["ok"])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        snapshot = V1Storage(self.path).recover().snapshot
        self.assertIsNotNone(snapshot)
        points = decode_recent_series(snapshot.recent_series)
        self.assertTrue(points)
        self.assertEqual(points[-1].soc_millipercent, 51_000)

    def test_restart_of_normal_cli_recovers_and_advances_generation(self) -> None:
        make_v4_db(self.path)
        self.poll_once(FakeSource())
        self.poll_once(FakeSource())
        before = V1Storage(self.path).recover().snapshot
        self.assertIsNotNone(before)

        code, _ = self.poll_once(FakeSource())  # a fresh runtime object, same file
        self.assertEqual(code, 0)
        after = V1Storage(self.path).recover().snapshot
        self.assertEqual(after.generation, before.generation + 1)
        self.assertEqual(after.hourly.hour_start_ms, before.hourly.hour_start_ms)
        self.assertGreater(after.hourly.observed_ms, before.hourly.observed_ms)

    # ------------------------------------------------------------------
    # Legacy schema v2 is left alone
    # ------------------------------------------------------------------
    def test_v2_database_uses_legacy_runtime_and_is_not_migrated(self) -> None:
        Storage(self.path).initialize_writer()
        self.assertEqual(user_version(self.path), 2)
        with patch("battery_status_tui.cli.V1Storage") as v4_writer:
            code, out = run_cli(["--database", str(self.path), "--diagnose"],
                                FakeSource())
        self.assertEqual(code, 0)
        v4_writer.assert_not_called()
        self.assertEqual(user_version(self.path), 2)
        self.assertIn(f"database: {self.path}", out)

    def test_unsupported_future_schema_is_refused(self) -> None:
        make_v4_db(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 5")
        code, _ = run_cli(["--database", str(self.path), "--once"], FakeSource())
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
