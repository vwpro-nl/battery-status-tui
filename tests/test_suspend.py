from __future__ import annotations
import json
import unittest
from battery_status_tui.models import RawBatterySnapshot
from battery_status_tui.suspend import clock_sleep, parse_journal

def raw(timestamp, mono, boot, percentage=50):
    return RawBatterySnapshot(timestamp, mono, boot, "boot-id", "BAT0", "identity", percentage,
                              "discharging", False)

class SuspendTests(unittest.TestCase):
    def test_clock_difference_detects_sleep(self):
        interval = clock_sleep(raw(100, 100, 100, 60), raw(500, 160, 500, 58))
        self.assertEqual((interval.started_at, interval.ended_at), (160, 500))
        self.assertEqual((interval.pre_percentage, interval.post_percentage), (60, 58))

    def test_journal_pairs_suspend_events(self):
        lines = "\n".join(json.dumps(item) for item in (
            {"MESSAGE": "PM: suspend entry (deep)", "__REALTIME_TIMESTAMP": "100000000", "_BOOT_ID": "boot"},
            {"MESSAGE": "PM: suspend exit", "__REALTIME_TIMESTAMP": "500000000", "_BOOT_ID": "boot"},
        ))
        interval = parse_journal(lines)[0]
        self.assertEqual((interval.started_at, interval.ended_at, interval.source), (100, 500, "journal"))
