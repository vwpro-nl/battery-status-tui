from __future__ import annotations

import configparser
import unittest
from pathlib import Path

TIMER_PATH = Path(__file__).resolve().parent.parent / "systemd" / "battery-status-tui.timer"


def _read_timer_section() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # preserve directive case
    parser.read(TIMER_PATH)
    return parser["Timer"]


class TimerSchedulingTest(unittest.TestCase):
    """A newly activated timer must schedule its first run on its own,
    independent of system boot age and of any prior service activation.

    Regression coverage for: a late-starting user systemd manager left
    OnBootSec's deadline already in the past, so the timer never fired its
    first run, and OnUnitActiveSec (which needs a prior activation as its
    reference point) never had one to schedule from. Observed as
    `Active: active (elapsed)` / `Trigger: n/a` with no collector samples.
    """

    def setUp(self) -> None:
        self.timer = _read_timer_section()

    def test_initial_activation_is_relative_to_timer_activation_not_boot(self) -> None:
        self.assertEqual(self.timer.get("OnActiveSec"), "10s")
        self.assertIsNone(self.timer.get("OnBootSec"))

    def test_steady_state_cadence_is_unchanged(self) -> None:
        self.assertEqual(self.timer.get("OnUnitActiveSec"), "1min")

    def test_accuracy_and_persistence_are_unchanged(self) -> None:
        self.assertEqual(self.timer.get("AccuracySec"), "10s")
        self.assertEqual(self.timer.get("Persistent"), "true")

    def test_triggers_the_collector_service(self) -> None:
        self.assertEqual(self.timer.get("Unit"), "battery-status-tui.service")


if __name__ == "__main__":
    unittest.main()
