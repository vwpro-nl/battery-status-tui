from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from battery_status_tui.cli import current_estimate, diagnostic_text, next_refresh_delay, parser
from battery_status_tui.models import Measurement
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
            text = diagnostic_text(measurement, Storage(Path(directory) / "db.sqlite3"))
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


if __name__ == "__main__":
    unittest.main()
