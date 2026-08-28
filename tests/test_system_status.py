from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from battery_status_tui.models import RawBatterySnapshot
from battery_status_tui.sources import SysfsSource
from battery_status_tui.system_status import HealthResolver, PowerProfileResolver, resolve_health


def battery(**values) -> RawBatterySnapshot:
    defaults = dict(timestamp=1, monotonic_s=1, boottime_s=1, boot_id="boot", device="BAT0",
                    identity="BAT0|model|serial", percentage=50, state="discharging",
                    ac_online=False)
    defaults.update(values)
    return RawBatterySnapshot(**defaults)


class Result(subprocess.CompletedProcess[str]):
    def __init__(self, stdout: str = "", returncode: int = 0):
        super().__init__([], returncode, stdout, "")


class HealthTests(unittest.TestCase):
    def test_sysfs_energy_health_has_first_priority(self):
        reading = resolve_health((battery(sources=("sysfs",), energy_full_wh=35,
                                          energy_full_design_wh=56, charge_full_ah=4,
                                          charge_full_design_ah=5),))
        self.assertEqual(reading.source, "sysfs-energy")
        self.assertAlmostEqual(reading.percent, 62.5)

    def test_sysfs_charge_health(self):
        reading = resolve_health((battery(sources=("sysfs",), charge_full_ah=3.022,
                                          charge_full_design_ah=4.850),))
        self.assertEqual((reading.source, reading.full, reading.design, reading.unit),
                         ("sysfs-charge", 3.022, 4.850, "Ah"))
        self.assertAlmostEqual(reading.percent, 62.30927835051546)

    def test_upower_energy_then_capacity_fallback(self):
        energy = resolve_health((battery(sources=("upower",), upower_energy_full_wh=35,
                                         upower_energy_full_design_wh=56,
                                         upower_capacity_percent=40),))
        capacity = resolve_health((battery(sources=("upower",), upower_capacity_percent=62.3093),))
        self.assertEqual((energy.source, energy.percent), ("upower-energy", 62.5))
        self.assertEqual((capacity.source, capacity.percent), ("upower-capacity", 62.3093))

    def test_invalid_or_missing_design_capacity_is_unavailable(self):
        for design in (None, 0, -1):
            with self.subTest(design=design):
                self.assertIsNone(resolve_health((battery(sources=("sysfs",),
                                                        charge_full_ah=3, charge_full_design_ah=design),)))

    def test_multiple_charge_batteries_are_converted_with_their_design_voltages(self):
        batteries = (
            battery(device="BAT0", identity="0", sources=("sysfs",), charge_full_ah=3,
                    charge_full_design_ah=4, voltage_design_v=10),
            battery(device="BAT1", identity="1", sources=("sysfs",), charge_full_ah=2,
                    charge_full_design_ah=4, voltage_design_v=20),
        )
        reading = resolve_health(batteries)
        self.assertEqual(reading.source, "sysfs-charge-design-voltage")
        self.assertAlmostEqual(reading.percent, 70 / 120 * 100)

    def test_sysfs_excludes_device_scope_batteries_before_health_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._supply(root / "BAT0", type="Battery", present="1", capacity="50",
                         status="Discharging", charge_full="3000000",
                         charge_full_design="4000000")
            self._supply(root / "mouse", type="Battery", scope="Device", capacity="55",
                         charge_full="1", charge_full_design="100")
            raw = SysfsSource(root).read_raw(1)
        self.assertEqual([item.device for item in raw], ["BAT0"])
        self.assertEqual(resolve_health(raw).percent, 75)

    def test_health_cache_refreshes_for_identity_ttl_and_invalidation(self):
        now = [0.0]
        resolver = HealthResolver(ttl=3600, clock=lambda: now[0])
        first = (battery(identity="first", sources=("sysfs",), charge_full_ah=3,
                         charge_full_design_ah=4),)
        changed_value = (battery(identity="first", sources=("sysfs",), charge_full_ah=2,
                                 charge_full_design_ah=4),)
        replacement = (battery(identity="replacement", sources=("sysfs",), charge_full_ah=3.5,
                               charge_full_design_ah=4),)
        self.assertEqual(resolver.resolve(first).percent, 75)
        self.assertEqual(resolver.resolve(changed_value).percent, 75)
        self.assertEqual(resolver.resolve(replacement).percent, 87.5)
        resolver.invalidate()
        self.assertEqual(resolver.resolve(changed_value).percent, 50)

    @staticmethod
    def _supply(path: Path, **values: str) -> None:
        path.mkdir()
        for name, value in values.items():
            (path / name).write_text(value, encoding="utf-8")


class ProfileTests(unittest.TestCase):
    def resolver(self, outputs: dict[str, Result], root: Path | None = None) -> PowerProfileResolver:
        return PowerProfileResolver(lambda command, **kwargs: outputs.get(command[0], Result(returncode=1)),
                                    root=root or Path("/nonexistent"), ttl=0)

    def test_known_dbus_profiles(self):
        for profile in ("balanced", "performance", "power-saver"):
            with self.subTest(profile=profile):
                reading = self.resolver({"busctl": Result(f's "{profile}"\n')}).resolve()
                self.assertEqual((reading.profile, reading.source),
                                 (profile, "power-profiles-daemon-dbus"))

    def test_profile_fallback_order(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command[0])
            return Result("performance\n") if command[0] == "powerprofilesctl" else Result(returncode=1)

        reading = PowerProfileResolver(runner, root=Path("/nonexistent"), ttl=0).resolve()
        self.assertEqual((reading.profile, reading.source), ("performance", "powerprofilesctl"))
        self.assertEqual(calls, ["busctl", "powerprofilesctl"])

    def test_kernel_profile_and_missing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sys/firmware/acpi"
            path.mkdir(parents=True)
            (path / "platform_profile").write_text("balanced\n", encoding="utf-8")
            unavailable = {"busctl": Result(returncode=1), "powerprofilesctl": Result(returncode=1)}
            reading = self.resolver(unavailable, root).resolve()
            self.assertEqual((reading.profile, reading.source), ("balanced", "kernel-platform-profile"))
        self.assertIsNone(self.resolver(unavailable).resolve())

    def test_unknown_profile_is_not_forced_to_a_known_name(self):
        reading = self.resolver({"busctl": Result('s "cool-and-quiet"\n')}).resolve()
        self.assertEqual(reading.profile, "cool-and-quiet")

    def test_profile_lookup_is_cached_for_ten_seconds(self):
        now, calls = [0.0], []

        def runner(command, **kwargs):
            calls.append(command)
            return Result('s "balanced"\n')

        resolver = PowerProfileResolver(runner, root=Path("/nonexistent"), ttl=10,
                                        clock=lambda: now[0])
        self.assertEqual(resolver.resolve().profile, "balanced")
        now[0] = 9.9
        self.assertEqual(resolver.resolve().profile, "balanced")
        self.assertEqual(len(calls), 1)
        now[0] = 10
        self.assertEqual(resolver.resolve().profile, "balanced")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
