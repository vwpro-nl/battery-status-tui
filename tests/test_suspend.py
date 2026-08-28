from __future__ import annotations
import json
import unittest
from battery_status_tui.models import RawBatterySnapshot
from battery_status_tui.suspend import PrepareForSleepParser, clock_sleep, parse_journal

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

    def test_prepare_for_sleep_parser_accepts_compact_and_verbose_booleans(self):
        for boolean_line in ("b true", "        BOOLEAN true;"):
            with self.subTest(boolean_line=boolean_line):
                parser = PrepareForSleepParser()
                self.assertIsNone(parser.feed("  Member=PrepareForSleep"))
                self.assertTrue(parser.feed(boolean_line))
        parser = PrepareForSleepParser()
        parser.feed("  Member=PrepareForSleep")
        self.assertFalse(parser.feed("        BOOLEAN false;"))

    def test_prepare_for_sleep_parser_rejects_unrelated_booleans(self):
        parser = PrepareForSleepParser()
        self.assertIsNone(parser.feed("        BOOLEAN true;"))
        parser.feed("  Member=SomethingElse")
        self.assertIsNone(parser.feed("b false"))
        parser.feed("  Member=PrepareForSleep")
        parser.feed("‣ Type=signal")
        self.assertIsNone(parser.feed("        BOOLEAN true;"))
