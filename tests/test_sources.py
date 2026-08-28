from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from battery_status_tui.sources import BatterySource, SysfsSource, UPowerSource


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
    capacity:            62.5%
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

    def test_upower_raw_snapshot_preserves_health_fallbacks(self):
        raw = UPowerSource(lambda *args, **kwargs: Result()).read_raw(now=100)[0]
        self.assertEqual((raw.upower_energy_full_wh, raw.upower_energy_full_design_wh,
                          raw.upower_capacity_percent), (35, 56, 62.5))

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

    def test_field_fusion_rejects_upower_zero_and_keeps_sysfs_counters(self):
        dump = UPOWER_DUMP.replace("energy-rate:         8.4 W", "energy-rate:         0 W")
        class ZeroResult(Result):
            stdout = dump
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._supply(root / "BAT0", type="Battery", capacity="48", status="Discharging",
                         charge_now="2000000", charge_full="4000000", voltage_now="12000000")
            source = BatterySource(UPowerSource(lambda *args, **kwargs: ZeroResult()), SysfsSource(root))
            raw = source.read_raw(100)[0]
        self.assertEqual(raw.charge_now_ah, 2)
        self.assertIsNone(raw.upower_energy_rate_w)
        self.assertEqual(raw.sources, ("sysfs", "upower"))

    def test_multiple_system_batteries_are_aggregated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._supply(root / "BAT0", type="Battery", capacity="50", status="Discharging",
                         energy_now="20000000", energy_full="40000000", power_now="5000000")
            self._supply(root / "BAT1", type="Battery", capacity="25", status="Discharging",
                         energy_now="10000000", energy_full="40000000", power_now="3000000")
            measurement = SysfsSource(root).read(100)
        self.assertEqual(measurement.device, "BAT0,BAT1")
        self.assertEqual(measurement.percentage, 37.5)
        self.assertEqual(measurement.power_w, 8)

    def test_transient_optional_identity_loss_keeps_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            self._supply(battery, type="Battery", capacity="50", status="Discharging",
                         model_name="PrimaryPack", serial_number="ABC123")
            source = SysfsSource(root)
            first = source.read_raw(100)[0]
            (battery / "model_name").unlink()
            (battery / "serial_number").unlink()
            second = source.read_raw(101)[0]
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.identity, "BAT0|PrimaryPack|ABC123")

    def test_changed_nonempty_identity_detects_battery_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            self._supply(battery, type="Battery", capacity="50", status="Discharging",
                         model_name="PrimaryPack", serial_number="ABC123")
            source = SysfsSource(root)
            first = source.read_raw(100)[0]
            (battery / "serial_number").write_text("XYZ789", encoding="utf-8")
            second = source.read_raw(101)[0]
        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(second.identity, "BAT0|PrimaryPack|XYZ789")

    @staticmethod
    def _supply(path: Path, **values: str) -> None:
        path.mkdir()
        for name, value in values.items():
            (path / name).write_text(value, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
