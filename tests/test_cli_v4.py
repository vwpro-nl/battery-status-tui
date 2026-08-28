"""Regression coverage: the normal CLI entrypoint must operate on schema v4.

These tests exercise ``battery_status_tui.cli.main`` itself -- the same callable
the ``./battery-status-tui`` wrapper invokes -- against schema-v4 databases, so
the failure that forced the production rollback (the normal runtime rejecting a
supported schema-v4 database) cannot regress.
"""

from __future__ import annotations

import io
import os
import signal
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
from battery_status_tui.v1_collector import V1Collector
from battery_status_tui.v1_storage import MAX_GENERATIONS, V1Storage


BOOT = "boot-cli-v4"
POLL_INTERVAL_S = 400


class FakeClock:
    """A wall clock the test advances explicitly between CLI invocations.

    ``sleep`` is cooperative: it moves the clock forward exactly as a real
    ``time.sleep`` moves wall time forward, so the CLI's poll-spacing guard can
    be exercised without waiting in real time.
    """

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

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


class _FakeMonitor:
    """A logind monitor stand-in with no events."""

    def __init__(self) -> None:
        import threading

        self.wakeup = threading.Event()

    def start(self) -> None:
        pass

    def drain(self):
        return []

    def close(self) -> None:
        pass


def run_cli(args: list[str], source: FakeSource,
            clock: FakeClock | None = None, *, stop_after_polls: int | None = None
            ) -> tuple[int, str]:
    buffer = io.StringIO()
    patches = [
        patch("battery_status_tui.cli.BatterySource", return_value=source),
        patch("battery_status_tui.cli.journal_intervals", lambda since: []),
        patch("battery_status_tui.cli.PowerProfileResolver", _NoProfile),
    ]
    if clock is not None:
        patches.append(patch("battery_status_tui.cli.time.time", clock.time))
        patches.append(patch("battery_status_tui.cli.time.sleep", clock.sleep))
    if stop_after_polls is not None:
        # Drive the real interactive loop, then interrupt it (as Ctrl-C would)
        # once it has completed the requested number of polls.
        patches.append(patch("battery_status_tui.cli.LogindMonitor", _FakeMonitor))
        real_collect = cli.collect_v1
        seen = [0]

        def counting_collect(*a, **kw):
            result = real_collect(*a, **kw)
            seen[0] += 1
            if seen[0] >= stop_after_polls:
                os.kill(os.getpid(), signal.SIGINT)
            return result

        patches.append(patch("battery_status_tui.cli.collect_v1", counting_collect))
    with redirect_stdout(buffer):
        if stop_after_polls is not None:
            buffer.isatty = lambda: True  # force the interactive loop
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


class V4SameSecondFirstPollTests(unittest.TestCase):
    """A fresh CLI process must not crash the collector by polling in the same
    wall-clock second as the last poll stored in the recovered checkpoint."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "history.sqlite3"
        make_v4_db(self.path)
        self.clock = FakeClock()

    def generations(self) -> list[int]:
        with V1Storage(self.path).reader() as db:
            return [int(r[0]) for r in db.execute(
                "SELECT generation FROM checkpoint_generations ORDER BY generation")]

    def finalized_hours(self) -> list[int]:
        with V1Storage(self.path).reader() as db:
            return [int(r[0]) for r in db.execute(
                "SELECT hour_start_ms FROM hourly_history")]

    def assert_healthy(self) -> None:
        with V1Storage(self.path).reader() as db:
            self.assertEqual([r[0] for r in db.execute("PRAGMA quick_check")], ["ok"])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertIsNotNone(V1Storage(self.path).recover().snapshot)
        hours = self.finalized_hours()
        self.assertEqual(len(hours), len(set(hours)))
        with V1Storage(self.path).reader() as db:
            self.assertLessEqual(
                db.execute("SELECT count(DISTINCT generation) FROM checkpoint_batteries")
                .fetchone()[0], MAX_GENERATIONS)

    def test_once_then_interactive_startup_same_second(self) -> None:
        code, _ = run_cli(["--database", str(self.path), "--once"], FakeSource(), self.clock)
        self.assertEqual(code, 0)
        gen_after_once = self.generations()

        # Interactive startup in the very same second -- previously a traceback.
        code, out = run_cli(["--database", str(self.path), "--interval", "1"],
                            FakeSource(), self.clock, stop_after_polls=1)
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("must be later than recovered poll", out)
        self.assertGreater(max(self.generations()), max(gen_after_once))
        self.assert_healthy()

    def test_rapid_interactive_restart_within_the_same_second(self) -> None:
        first_gen = []
        for _ in range(3):  # three back-to-back interactive processes, clock barely moves
            code, out = run_cli(["--database", str(self.path), "--interval", "1"],
                                FakeSource(), self.clock, stop_after_polls=1)
            self.assertEqual(code, 0)
            self.assertNotIn("Traceback", out)
            first_gen.append(max(self.generations()))
        self.assertEqual(first_gen, sorted(first_gen))
        self.assertGreater(first_gen[-1], first_gen[0])
        self.assert_healthy()

    def test_two_rapid_sample_invocations_same_second(self) -> None:
        code1, out1 = run_cli(["--database", str(self.path), "--sample"],
                              FakeSource(), self.clock)
        code2, out2 = run_cli(["--database", str(self.path), "--sample"],
                              FakeSource(), self.clock)
        self.assertEqual((code1, code2), (0, 0))
        self.assertNotIn("Traceback", out1 + out2)
        ts1 = int(out1.split()[0])
        ts2 = int(out2.split()[0])
        self.assertGreater(ts2, ts1)  # strictly increasing poll seconds
        self.assertEqual(len(self.generations()), 2)  # both polls recorded, capped fine
        self.assert_healthy()

    def test_seed_guard_advances_first_poll_past_the_checkpoint_second(self) -> None:
        # Drop a checkpoint whose last poll is "now", then start a process in the
        # same second: the seeded guard must push the first poll to now + 1.
        base = int(self.clock.time())
        V1Collector(V1Storage(self.path)).process_poll(
            _measurement_at(FakeSource(), base))
        with V1Storage(self.path).reader() as db:
            last_ms = db.execute(
                "SELECT last_poll_at_ms FROM checkpoint_generations "
                "ORDER BY generation DESC LIMIT 1").fetchone()[0]
        self.assertEqual(last_ms // 1000, base)

        code, out = run_cli(["--database", str(self.path), "--sample"],
                            FakeSource(), self.clock)
        self.assertEqual(code, 0)
        self.assertNotIn("Traceback", out)
        self.assertGreaterEqual(int(out.split()[0]), base + 1)

    def test_collector_still_rejects_a_non_increasing_timestamp(self) -> None:
        # The CLI prevents the bad poll; the collector's own invariant must be
        # unchanged for any caller that reaches it with a stale timestamp.
        storage = V1Storage(self.path)
        collector = V1Collector(storage)
        source = FakeSource()
        collector.process_poll(_measurement_at(source, 5_000))
        for stale in (5_000, 4_999, 0):
            with self.subTest(stale=stale):
                with self.assertRaisesRegex(
                        ValueError, "new poll timestamp must be later than recovered poll"):
                    collector.process_poll(_measurement_at(source, stale))


def _measurement_at(source: FakeSource, moment: int) -> Measurement:
    raw = source._raw(moment)
    return Measurement(
        moment, source.soc, source.state, source.ac, power_w=27.0, energy_wh=24.0,
        energy_full_wh=40.0, energy_full_design_wh=62.0, source="sysfs+upower",
        device="BAT0", power_method="current-voltage", power_confidence="high",
        monotonic_s=float(moment), boottime_s=float(moment), boot_id=BOOT,
        battery_identity=raw.identity, raw_batteries=(raw,),
    )


if __name__ == "__main__":
    unittest.main()
