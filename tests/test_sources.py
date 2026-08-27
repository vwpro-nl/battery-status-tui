from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from battery_status_tui.sources import SysfsSource, UPowerSource


UPOWER_DUMP = """Device: /org/freedesktop/UPower/devices/battery_BAT0
  native-path:          BAT0
  power supply:         yes
  battery
    state:               discharging
    energy:              20 Wh
    energy-full:         35 Wh
    energy-full-design:  56 Wh
    energy-rate:         8.4 W
    voltage:             12.2 V
    percentage:          48%
    time to empty:       2.3 hours
    charge-cycles:       72
Device: /org/freedesktop/UPower/devices/battery_mouse
  native-path:          mouse
  power supply:         no
  percentage:           55%
Device: /org/freedesktop/UPower/devices/line_power_AC
  native-path:          AC
  line-power
    online:              no
"""


class Result:
    returncode = 0
    stdout = UPOWER_DUMP
    stderr = ""


class SourceTests(unittest.TestCase):
    def test_upower_selects_system_battery(self):
        measurement = UPowerSource(lambda *args, **kwargs: Result()).read(now=100)
        self.assertEqual(measurement.device, "BAT0")
        self.assertEqual(measurement.percentage, 48)
        self.assertEqual(measurement.power_w, 8.4)
        self.assertEqual(measurement.time_to_empty_s, 8280)
        self.assertFalse(measurement.ac_online)

    def test_sysfs_computes_power_and_ignores_device_battery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._supply(root / "BAT0", type="Battery", present="1", capacity="48", status="Discharging", voltage_now="12000000", current_now="700000")
            self._supply(root / "mouse", type="Battery", scope="Device", capacity="55")
            self._supply(root / "AC", type="Mains", online="0")
            measurement = SysfsSource(root).read(now=100)
        self.assertEqual(measurement.device, "BAT0")
        self.assertAlmostEqual(measurement.power_w or 0, 8.4)
        self.assertFalse(measurement.ac_online)

    @staticmethod
    def _supply(path: Path, **values: str) -> None:
        path.mkdir()
        for name, value in values.items():
            (path / name).write_text(value, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

