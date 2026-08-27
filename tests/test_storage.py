from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from battery_status_tui.models import Measurement
from battery_status_tui.storage import Storage


def measurement(timestamp: int, percentage: float, state: str, ac_online: bool) -> Measurement:
    return Measurement(timestamp, percentage, state, ac_online, source="test", device="BAT0")


class StorageTests(unittest.TestCase):
    def test_charging_and_discharging_are_separate_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            discharge_id = storage.record(measurement(100, 80, "discharging", False))
            self.assertEqual(storage.record(measurement(160, 79, "discharging", False)), discharge_id)
            charge_id = storage.record(measurement(220, 79, "charging", True))
            self.assertNotEqual(charge_id, discharge_id)
            self.assertEqual(storage.current_session().kind, "charging")

    def test_full_closes_active_session(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            storage.record(measurement(100, 99, "charging", True))
            storage.record(measurement(160, 100, "full", True))
            self.assertIsNone(storage.current_session())


if __name__ == "__main__":
    unittest.main()

