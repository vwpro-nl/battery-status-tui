from __future__ import annotations
import unittest
from battery_status_tui.models import RawBatterySnapshot
from battery_status_tui.power import PowerResolver

def raw(timestamp=300, state="discharging", **values):
    defaults = dict(monotonic_s=float(timestamp), boottime_s=float(timestamp), boot_id="boot", device="BAT0",
                    identity="BAT0|model|serial", percentage=50, state=state, ac_online=state == "charging")
    defaults.update(values)
    return RawBatterySnapshot(timestamp=timestamp, **defaults)

class PowerResolverTests(unittest.TestCase):
    def test_source_priority(self):
        reading = PowerResolver().resolve(raw(power_now_w=9, current_now_a=1, voltage_now_v=12, upower_energy_rate_w=13))
        self.assertEqual((reading.watts, reading.method), (9, "power-now"))

    def test_current_voltage_fallback(self):
        reading = PowerResolver().resolve(raw(power_now_w=0, current_now_a=.7, voltage_now_v=12))
        self.assertAlmostEqual(reading.watts or 0, 8.4)
        self.assertEqual(reading.method, "current-voltage")

    def test_zero_upower_rate_is_unknown_while_active(self):
        self.assertIsNone(PowerResolver().resolve(raw(upower_energy_rate_w=0)).watts)

    def test_energy_delta_after_two_minutes(self):
        previous = raw(timestamp=100, monotonic_s=100, energy_now_wh=20)
        current = raw(timestamp=220, monotonic_s=220, energy_now_wh=19.8)
        reading = PowerResolver().resolve(current, [previous])
        self.assertAlmostEqual(reading.watts or 0, 6)
        self.assertEqual(reading.method, "energy-delta")
        self.assertTrue(reading.approximate)

    def test_charge_delta_uses_mean_voltage(self):
        previous = raw(timestamp=100, monotonic_s=100, charge_now_ah=2, voltage_now_v=12)
        current = raw(timestamp=220, monotonic_s=220, charge_now_ah=1.98, voltage_now_v=12)
        self.assertAlmostEqual(PowerResolver().resolve(current, [previous]).watts or 0, 7.2)

    def test_sleep_and_counter_reset_are_rejected(self):
        previous = raw(timestamp=100, monotonic_s=100, charge_now_ah=2, voltage_now_v=12)
        slept = raw(timestamp=300, monotonic_s=120, boottime_s=300, charge_now_ah=1.9, voltage_now_v=12)
        self.assertIsNone(PowerResolver().resolve(slept, [previous], [(110, 290)]).watts)
        reset = raw(timestamp=220, monotonic_s=220, charge_now_ah=2.2, voltage_now_v=12)
        self.assertIsNone(PowerResolver().resolve(reset, [previous]).watts)

    def test_battery_identity_change_is_rejected(self):
        previous = raw(timestamp=100, monotonic_s=100, energy_now_wh=20)
        current = raw(timestamp=220, monotonic_s=220, energy_now_wh=19, identity="BAT0|replacement|new")
        self.assertIsNone(PowerResolver().resolve(current, [previous]).watts)
