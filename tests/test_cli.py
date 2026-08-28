from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from battery_status_tui.cli import collect, current_estimate, diagnostic_text, next_refresh_delay, parser
from battery_status_tui.graph import NOW_INDEX, _chart_rows_and_percentages, _sleep_columns
from battery_status_tui.models import Measurement, RawBatterySnapshot, SleepInterval
from battery_status_tui.power import PowerResolver
from battery_status_tui.storage import Storage


class CliTests(unittest.TestCase):
    def test_live_refresh_wakes_at_next_projection_boundary(self):
        self.assertAlmostEqual(next_refresh_delay(60, 1200 - 2), 2.05)
        self.assertEqual(next_refresh_delay(60, 1200 + 2), 60)

    def test_once_and_sample_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(["--once", "--sample"])

    def test_diagnostics_include_health_and_cycles(self):
        measurement = Measurement(
            100, 50, "discharging", False,
            energy_full_wh=35, energy_full_design_wh=56, cycle_count=72,
            source="test", device="BAT0",
        )
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite3")
            storage.initialize_writer()
            text = diagnostic_text(measurement, storage)
        self.assertIn("battery health: 62.5%", text)
        self.assertIn("cycle count: 72", text)

    def test_eta_smoothing_is_persisted_per_session(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite3")
            for index in range(9):
                storage.record(Measurement(index * 300, 80 - index, "discharging", False, source="test", device="BAT0"))
            current = Measurement(2400, 72, "discharging", False, source="test", device="BAT0")
            estimate = current_estimate(storage, current, 2400)
            self.assertEqual(estimate.source, "session-trend")
            self.assertIsNotNone(storage.metadata_int("eta-seconds:1"))

    def test_overnight_clock_resume_is_stored_and_immediately_renderable(self):
        pre = RawBatterySnapshot(1787879737, 44053.461, 60511.461, "boot", "BAT0", "identity",
                                 67, "discharging", False)
        post = RawBatterySnapshot(1787897217, 44113.968, 77991.113, "boot", "BAT0", "identity",
                                  67, "discharging", False)
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite3")
            storage.record(Measurement(pre.timestamp, 67, "discharging", False,
                                       source="test", device="BAT0", battery_identity="identity",
                                       raw_batteries=(pre,)))
            with patch("battery_status_tui.cli.journal_intervals", return_value=[]):
                current = collect(self.Source(post), storage, post.timestamp)
            intervals = storage.sleep_intervals_since(post.timestamp - 6 * 3600)
            history = storage.samples_since(post.timestamp - 6 * 3600)
            top, bottom, _ = _chart_rows_and_percentages(current, history, None,
                                                         post.timestamp, intervals)

        self.assertEqual(len(intervals), 1)
        self.assertEqual((intervals[0].pre_percentage, intervals[0].post_percentage), (67, 67))
        columns = _sleep_columns(intervals[0], post.timestamp)
        self.assertEqual(len(columns), 14)
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28ff
                            for column in columns for character in (top[column], bottom[column])
                            if character != " "))
        self.assertEqual((top[NOW_INDEX], bottom[NOW_INDEX]), ("│", "│"))
        self.assertIsNone(current.power_w)

    def test_clock_resume_handles_short_sleep_and_ignores_no_gap_or_replacement(self):
        cases = (
            (RawBatterySnapshot(500, 160, 500, "boot", "BAT0", "same", 67,
                                "discharging", False), True),
            (RawBatterySnapshot(160, 160, 160, "boot", "BAT0", "same", 67,
                                "discharging", False), False),
            (RawBatterySnapshot(500, 160, 500, "boot", "BAT1", "replacement", 67,
                                "discharging", False), False),
        )
        for post, expected_sleep in cases:
            with self.subTest(expected_sleep=expected_sleep, identity=post.identity):
                pre = RawBatterySnapshot(100, 100, 100, "boot", "BAT0", "same", 67,
                                         "discharging", False)
                with tempfile.TemporaryDirectory() as directory:
                    storage = Storage(Path(directory) / "db.sqlite3")
                    storage.record(Measurement(100, 67, "discharging", False,
                                               battery_identity="same", raw_batteries=(pre,)))
                    with patch("battery_status_tui.cli.journal_intervals", return_value=[]):
                        collect(self.Source(post), storage, post.timestamp)
                    self.assertEqual(bool(storage.sleep_intervals_since(0)), expected_sleep)

    def test_clock_resume_forces_journal_reconciliation_and_upgrades_bounds(self):
        pre = RawBatterySnapshot(100, 100, 100, "boot", "BAT0", "same", 67,
                                 "discharging", False)
        post = RawBatterySnapshot(500, 160, 500, "boot", "BAT0", "same", 67,
                                  "discharging", False)
        journal = SleepInterval(155, 495, kind="suspend", source="journal", boot_id="boot")
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "db.sqlite3")
            storage.record(Measurement(100, 67, "discharging", False,
                                       battery_identity="same", raw_batteries=(pre,)))
            storage.set_metadata_int("journal-checked-at", 499)
            with patch("battery_status_tui.cli.journal_intervals", return_value=[journal]) as queried:
                collect(self.Source(post), storage, post.timestamp)
            interval = storage.sleep_intervals_since(0)[0]
        queried.assert_called_once()
        self.assertEqual((interval.started_at, interval.ended_at, interval.source),
                         (155, 495, "journal"))
        self.assertEqual((interval.pre_percentage, interval.post_percentage), (67, 67))

    class Source:
        resolver = PowerResolver()

        def __init__(self, snapshot: RawBatterySnapshot):
            self.snapshot = snapshot

        def read_raw(self, now=None):
            return (self.snapshot,)


if __name__ == "__main__":
    unittest.main()
