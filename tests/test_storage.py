from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from battery_status_tui.models import Measurement
from battery_status_tui.models import RawBatterySnapshot, SleepInterval
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

    def test_ac_transition_wins_over_lagging_device_state(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            discharge_id = storage.record(measurement(100, 80, "discharging", False))
            charge_id = storage.record(measurement(160, 80, "discharging", True))
            self.assertNotEqual(charge_id, discharge_id)
            self.assertEqual(storage.current_session().kind, "charging")

    def test_migration_preserves_v1_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            import sqlite3
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE samples(id INTEGER PRIMARY KEY, timestamp INTEGER, session_id INTEGER, percentage REAL, state TEXT, ac_online INTEGER, power_w REAL, voltage_v REAL, current_a REAL, upower_remaining_s INTEGER, source TEXT, device TEXT, UNIQUE(timestamp, device)); CREATE TABLE sessions(id INTEGER PRIMARY KEY, kind TEXT, started_at INTEGER, ended_at INTEGER, start_percentage REAL, end_percentage REAL, end_reason TEXT); CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);")
            db.execute("INSERT INTO samples(timestamp, percentage, state, source, device) VALUES(1, 50, 'full', 'old', 'BAT0')")
            db.commit(); db.close()
            self.assertEqual(Storage(path).latest().percentage, 50)

    def test_raw_samples_and_sleep_intervals_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = Storage(Path(directory) / "history.sqlite3")
            item = RawBatterySnapshot(100, 10, 10, "boot", "BAT0", "id", 50, "discharging", False, charge_now_ah=2)
            storage.record(Measurement(100, 50, "discharging", False, raw_batteries=(item,), battery_identity="id"))
            storage.record_sleep(SleepInterval(110, 200, pre_percentage=50, post_percentage=49))
            self.assertEqual(storage.raw_samples_since(0)[0].charge_now_ah, 2)
            self.assertEqual(storage.sleep_intervals_since(0)[0].post_percentage, 49)


if __name__ == "__main__":
    unittest.main()
