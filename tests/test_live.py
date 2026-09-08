from __future__ import annotations

import errno
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from battery_status_tui.live import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS, DISPLAY_EWMA_ALPHA, LiveOperatingMode,
    LiveSampler, classify_live_mode,
)
from battery_status_tui.models import PowerReading, RawBatterySnapshot
from battery_status_tui.power import is_sysfs_direct_method
from battery_status_tui.sources import FieldStatus, SourceUnavailable, SysfsSource


class FakeClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value
    def __call__(self) -> float:
        return self.value
    def advance(self, seconds: float) -> None:
        self.value += seconds


def raw(timestamp: int, *, watts: float | None = 12.0, state: str = "charging",
        ac_online: bool | None = True, device: str = "BAT0", identity: str | None = None,
        percentage: float = 50, full: float = 40, monotonic: float | None = None,
        boottime: float | None = None, boot_id: str = "boot") -> RawBatterySnapshot:
    mono = float(timestamp if monotonic is None else monotonic)
    return RawBatterySnapshot(timestamp, mono, mono if boottime is None else boottime,
        boot_id, device, identity or f"{device}|model|serial", percentage, state,
        ac_online, power_now_w=watts, energy_full_wh=full, sources=("sysfs",))


class SequenceSource:
    def __init__(self, samples):
        self.samples = samples
        self.calls = 0
    def read_raw(self, now=None):
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        if isinstance(sample, Exception):
            raise sample
        return sample


class UnavailableSource:
    def read_raw(self, now=None):
        raise SourceUnavailable("no usable sysfs battery")


class ForbiddenMixedResolver:
    def resolve_sysfs_direct(self, current):
        method = "power-now" if current.device == "BAT0" else "upower-energy-rate"
        return PowerReading(current.power_now_w, method)


class LiveSamplerTests(unittest.TestCase):
    def sampler(self, values, *, interval=1.0):
        clock = FakeClock()
        source = SequenceSource([(item,) if isinstance(item, RawBatterySnapshot) else item
                                 for item in values])
        return LiveSampler(source, interval=interval, clock=clock, wall_clock=clock), source, clock

    @staticmethod
    def take(sampler, clock, step=1.0):
        result = sampler.poll()
        clock.advance(step)
        return result

    def test_default_interval_and_deadlines_are_drift_resistant(self):
        sampler, source, clock = self.sampler([raw(1000)])
        self.assertEqual(DEFAULT_SAMPLE_INTERVAL_SECONDS, 1.0)
        self.assertIsNotNone(sampler.poll())
        clock.advance(.9)
        self.assertIsNone(sampler.poll())
        self.assertEqual(source.calls, 1)
        clock.advance(.1)
        self.assertIsNotNone(sampler.poll())
        clock.advance(2.4)
        self.assertIsNotNone(sampler.poll())
        self.assertEqual((source.calls, sampler.next_sample_at), (3, 1004))

    def test_long_stall_reads_once_and_resets_old_display(self):
        sampler, source, clock = self.sampler([raw(1000, watts=12), raw(1004, watts=None)])
        self.take(sampler, clock)
        clock.advance(3)
        result = sampler.poll()
        self.assertEqual(source.calls, 2)
        self.assertIsNone(result.display.power_w)

    def test_raw_retention_is_by_elapsed_age(self):
        sampler, _, clock = self.sampler([raw(1000)], interval=.5)
        for _ in range(61):
            self.take(sampler, clock, .5)
        self.assertIn(len(sampler.raw_samples), {60, 61})
        clock.advance(.5)
        self.assertIsNotNone(sampler.poll())
        self.assertIn(len(sampler.raw_samples), {60, 61})
        clock.advance(31)
        self.assertIsNotNone(sampler.poll())
        self.assertEqual(len(sampler.raw_samples), 1)

    def test_four_second_timestamp_median_and_elapsed_ewma(self):
        values = [raw(1000 + i, watts=w) for i, w in enumerate((10, 20, 30, 40, 50, 60))]
        sampler, _, clock = self.sampler(values)
        displayed = [self.take(sampler, clock).display.power_w for _ in values]
        self.assertEqual(DISPLAY_EWMA_ALPHA, .35)
        self.assertAlmostEqual(displayed[1], .35 * 15 + .65 * 10)
        self.assertGreater(displayed[-1], displayed[-2])
        half, _, half_clock = self.sampler([raw(1000, watts=10), raw(1001, watts=20)], interval=.5)
        first = self.take(half, half_clock, .5).display.power_w
        second = self.take(half, half_clock, .5).display.power_w
        alpha = 1 - (1 - .35) ** .5
        self.assertAlmostEqual(second, alpha * 15 + (1 - alpha) * first)

    def test_active_zero_and_subthreshold_are_excluded_then_hold_expires(self):
        values = [raw(1000, watts=14)] + [raw(1001 + i, watts=w) for i, w in enumerate((0, .049, None, None, None, None))]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w == 14 for item in results[:6]))
        self.assertIsNone(results[6].display.power_w)

    def test_reconnect_transient_never_emits_zero_or_old_discharge_power(self):
        values = [raw(1000, watts=8, state="discharging", ac_online=False)]
        # Four 1 Hz observations span only three seconds, conservatively covering
        # the measured approximately 2.8-second reconnect episode.
        values += [raw(1001 + i, watts=0, state="not charging", ac_online=True) for i in range(4)]
        values += [raw(1005, watts=15, state="charging", ac_online=True)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results[1:5]))
        self.assertEqual(results[5].display.power_w, 15)

    def test_connected_idle_requires_four_seconds_and_five_observations(self):
        values = [raw(1000 + i, watts=0, state="not charging") for i in range(5)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results[:4]))
        self.assertEqual(results[4].display.power_w, 0)

    def test_one_missed_observation_delays_without_erasing_idle_anchor(self):
        values = [raw(1000 + i, watts=0, state="not charging") for i in range(6)]
        sampler, _, clock = self.sampler(values)
        self.take(sampler, clock)
        self.take(sampler, clock)
        clock.advance(1)  # one missed 1 Hz deadline; next gap is 2 seconds
        results = [self.take(sampler, clock) for _ in range(3)]
        self.assertIsNone(results[1].display.power_w)
        self.assertEqual(results[2].display.power_w, 0)

    def test_gap_over_two_point_five_seconds_resets_idle_confirmation(self):
        values = [raw(1000 + i, watts=0, state="not charging") for i in range(6)]
        sampler, _, clock = self.sampler(values)
        for _ in range(3): self.take(sampler, clock)
        clock.advance(2)
        results = [self.take(sampler, clock) for _ in range(3)]
        self.assertTrue(all(item.display.power_w is None for item in results))

    def test_charging_to_connected_idle_immediately_becomes_unavailable(self):
        values = [raw(1000, watts=14),
                  raw(1001, watts=0, state="not charging")]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertEqual(results[0].display.power_w, 14)
        self.assertIsNone(results[1].display.power_w)

    def test_charging_to_sustained_idle_confirms_zero_without_hold(self):
        values = [raw(1000, watts=14)] + [
            raw(1001 + i, watts=0, state="not charging") for i in range(5)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results[1:5]))
        self.assertEqual(results[5].display.power_w, 0)

    def test_charging_idle_charging_starts_fresh_ewma(self):
        values = [raw(1000, watts=15),
                  raw(1001, watts=0, state="not charging"),
                  raw(1002, watts=14.8)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertEqual([item.display.power_w for item in results], [15, None, 14.8])

    def test_charging_to_idle_unavailable_never_holds_old_watts(self):
        values = [raw(1000, watts=15)] + [
            raw(1001 + i, watts=None, state="not charging") for i in range(6)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results[1:]))

    def test_idle_unavailable_pauses_zero_evidence_without_incrementing(self):
        values = [raw(1000, watts=0, state="not charging"),
                  raw(1001, watts=0, state="not charging"),
                  raw(1002, watts=None, state="not charging"),
                  raw(1003, watts=0, state="not charging"),
                  raw(1004, watts=0, state="not charging"),
                  raw(1005, watts=0, state="not charging")]
        sampler, _, clock = self.sampler(values)
        self.take(sampler, clock); self.take(sampler, clock)
        unavailable = self.take(sampler, clock)
        self.assertIsNone(unavailable.display.power_w)
        self.assertEqual(sampler._idle_zero_count, 2)
        results = [self.take(sampler, clock) for _ in range(3)]
        self.assertTrue(all(item.display.power_w is None for item in results[:2]))
        self.assertEqual(results[2].display.power_w, 0)

    def test_long_idle_unavailable_gap_breaks_confirmation(self):
        values = [raw(1000, watts=0, state="not charging"),
                  raw(1001, watts=0, state="not charging"),
                  raw(1004, watts=None, state="not charging"),
                  raw(1005, watts=0, state="not charging"),
                  raw(1006, watts=0, state="not charging"),
                  raw(1007, watts=0, state="not charging")]
        sampler, _, clock = self.sampler(values)
        self.take(sampler, clock); self.take(sampler, clock)
        clock.advance(2)
        results = [self.take(sampler, clock) for _ in range(4)]
        self.assertTrue(all(item.display.power_w is None for item in results))

    def test_positive_idle_power_resets_zero_confirmation(self):
        values = [raw(1000, watts=0, state="not charging"),
                  raw(1001, watts=0, state="not charging"),
                  raw(1002, watts=1, state="not charging")] + [
                  raw(1003 + i, watts=0, state="not charging") for i in range(4)]
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results))

    def test_unavailable_only_idle_never_confirms_zero(self):
        values = [raw(1000 + i, watts=None, state="not charging") for i in range(8)]
        sampler, _, clock = self.sampler(values)
        self.assertTrue(all(self.take(sampler, clock).display.power_w is None
                            for _ in values))

    def test_non_atomic_disconnect_and_reconnect_modes_are_immediate_and_safe(self):
        cases = [
            raw(1000, watts=14),
            raw(1001, watts=0, state="not charging", ac_online=True),
            raw(1002, watts=0, state="not charging", ac_online=False),
            raw(1003, watts=8, state="discharging", ac_online=False),
            raw(1004, watts=0, state="not charging", ac_online=False),
            raw(1005, watts=0, state="not charging", ac_online=True),
            raw(1006, watts=15, state="charging", ac_online=True),
        ]
        sampler, _, clock = self.sampler(cases)
        results = [self.take(sampler, clock) for _ in cases]
        self.assertEqual([item.display.power_w for item in results],
                         [14, None, None, 8, None, None, 15])

    def test_suspend_boot_backward_clock_and_identity_each_reset(self):
        discontinuities = [
            raw(1001, watts=None, monotonic=1001, boottime=1002),
            raw(1001, watts=None, boot_id="new"),
            raw(999, watts=None, monotonic=999, boottime=999),
            raw(999, watts=None, monotonic=1001, boottime=1001),
            raw(1001, watts=None, identity="replacement"),
        ]
        for changed in discontinuities:
            with self.subTest(changed=changed):
                sampler, _, clock = self.sampler([raw(1000, watts=14), changed])
                self.take(sampler, clock)
                result = self.take(sampler, clock)
                self.assertIsNone(result.display.power_w)
                self.assertEqual(len(sampler.raw_samples), 1)

    def test_multi_battery_requires_complete_compatible_direct_power(self):
        valid = (raw(1000, watts=5, device="BAT0"), raw(1000, watts=3, device="BAT1"))
        sampler, _, clock = self.sampler([valid])
        self.assertEqual(self.take(sampler, clock).display.power_w, 8)
        partial = (raw(1001, watts=5, device="BAT0"), raw(1001, watts=None, device="BAT1"))
        sampler, _, clock = self.sampler([partial])
        self.assertIsNone(self.take(sampler, clock).display.power_w)
        opposing = (raw(1002, watts=5, device="BAT0"),
                    raw(1002, watts=3, device="BAT1", state="discharging", ac_online=True))
        sampler, _, clock = self.sampler([opposing])
        self.assertIsNone(self.take(sampler, clock).display.power_w)

    def test_multi_battery_accepts_each_direct_method_combination(self):
        power = raw(1000, watts=5, device="BAT0")
        current = replace(raw(1000, watts=None, device="BAT1"),
                          current_now_a=.25, voltage_now_v=12)
        combinations = [
            (power, raw(1000, watts=3, device="BAT1"), 8),
            (replace(raw(1000, watts=None, device="BAT0"),
                     current_now_a=.5, voltage_now_v=12), current, 9),
            (power, current, 8),
        ]
        for left, right, expected in combinations:
            with self.subTest(expected=expected):
                sampler, _, clock = self.sampler([(left, right)])
                result = self.take(sampler, clock)
                self.assertEqual(result.display.power_w, expected)
                self.assertFalse(result.display.power_approximate)

    def test_forbidden_mixed_method_is_not_live_direct(self):
        self.assertTrue(is_sysfs_direct_method("mixed:current-voltage+power-now"))
        for method in ("mixed:power-now+upower-energy-rate",
                       "mixed:current-voltage+energy-delta",
                       "mixed:power-now+charge-delta", "mixed:power-now+unknown"):
            with self.subTest(method=method):
                self.assertFalse(is_sysfs_direct_method(method))
        clock = FakeClock()
        source = SequenceSource([(
            raw(1000, watts=5, device="BAT0"),
            raw(1000, watts=3, device="BAT1"),
        )])
        sampler = LiveSampler(source, ForbiddenMixedResolver(),
                              clock=clock, wall_clock=clock)
        result = sampler.poll()
        self.assertEqual(result.raw.power_method,
                         "mixed:power-now+upower-energy-rate")
        self.assertIsNone(result.display.power_w)

    def test_battery_membership_change_resets(self):
        samples = [(raw(1000, watts=12),),
                   (raw(1001, watts=None), raw(1001, watts=None, device="BAT1"))]
        sampler, _, clock = self.sampler(samples)
        self.take(sampler, clock)
        self.assertIsNone(self.take(sampler, clock).display.power_w)

    def test_source_recovery_starts_fresh(self):
        sampler, _, clock = self.sampler([raw(1000, watts=None), raw(1001, watts=13)])
        self.assertIsNone(self.take(sampler, clock).display.power_w)
        self.assertEqual(self.take(sampler, clock).display.power_w, 13)

    def test_upower_and_counter_values_never_enter_live_power(self):
        base = raw(1000, watts=None)
        upower = replace(base, upower_energy_rate_w=17)
        counter = replace(base, energy_now_wh=20)
        sampler, _, clock = self.sampler([upower, counter])
        self.assertIsNone(self.take(sampler, clock).raw.power_w)
        self.assertIsNone(self.take(sampler, clock).raw.power_w)

    def test_discharge_energy_counter_fallback_after_legacy_window(self):
        values = [replace(raw(1000 + second, watts=None, state="discharging",
                              ac_online=False), energy_now_wh=20.0)
                  for second in range(120)]
        values.append(replace(values[-1], timestamp=1120, monotonic_s=1120,
                              boottime_s=1120, energy_now_wh=19.8))
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(all(item.display.power_w is None for item in results[:-1]))
        self.assertAlmostEqual(results[-1].display.power_w or 0, 6.0)
        self.assertEqual((results[-1].display.power_method,
                          results[-1].display.power_approximate,
                          results[-1].display.power_confidence,
                          results[-1].display.power_window_s),
                         ("energy-delta", True, "medium", 120))

    def test_discharge_charge_counter_uses_mean_voltage(self):
        first = replace(raw(1000, watts=None, state="discharging", ac_online=False),
                        charge_now_ah=2.0, voltage_now_v=11.8)
        values = [replace(first, timestamp=1000 + second,
                          monotonic_s=1000 + second, boottime_s=1000 + second)
                  for second in range(120)]
        values.append(replace(first, timestamp=1120, monotonic_s=1120,
                              boottime_s=1120, charge_now_ah=1.98,
                              voltage_now_v=12.2))
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertAlmostEqual(results[-1].display.power_w or 0, 7.2)
        self.assertEqual((results[-1].display.power_method,
                          results[-1].display.power_approximate),
                         ("charge-delta", True))

    def test_unchanged_and_insufficient_counter_observations_stay_unavailable(self):
        base = replace(raw(1000, watts=None, state="discharging", ac_online=False),
                       charge_now_ah=2.0, voltage_now_v=12.0)
        values = [base, replace(base, timestamp=1023, monotonic_s=1023,
                                boottime_s=1023, charge_now_ah=1.999)]
        values.extend(replace(values[-1], timestamp=1024 + second,
                              monotonic_s=1024 + second, boottime_s=1024 + second)
                      for second in range(100))
        sampler, _, clock = self.sampler(values)
        self.assertTrue(all(self.take(sampler, clock).display.power_w is None
                            for _ in values))

    def test_counter_epoch_resets_on_mode_plug_and_suspend_boundaries(self):
        base = replace(raw(1000, watts=None, state="discharging", ac_online=False),
                       energy_now_wh=20.0)
        boundaries = (
            replace(base, timestamp=1120, monotonic_s=1120, boottime_s=1120,
                    state="charging", ac_online=True, energy_now_wh=19.8),
            replace(base, timestamp=1120, monotonic_s=1120, boottime_s=1121,
                    energy_now_wh=19.8),
            replace(base, timestamp=1120, monotonic_s=1120, boottime_s=1120,
                    state="not charging", energy_now_wh=19.8),
        )
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                values = [replace(base, timestamp=1000 + second,
                                  monotonic_s=1000 + second, boottime_s=1000 + second)
                          for second in range(120)] + [boundary]
                sampler, _, clock = self.sampler(values)
                results = [self.take(sampler, clock) for _ in values]
                self.assertIsNone(results[-1].display.power_w)
                self.assertLessEqual(len(sampler._counter_history), 1)

    def test_direct_power_supersedes_existing_counter_estimate(self):
        base = replace(raw(1000, watts=None, state="discharging", ac_online=False),
                       energy_now_wh=20.0)
        values = [replace(base, timestamp=1000 + second,
                          monotonic_s=1000 + second, boottime_s=1000 + second)
                  for second in range(120)]
        values.extend((replace(base, timestamp=1120, monotonic_s=1120,
                               boottime_s=1120, energy_now_wh=19.8),
                       raw(1121, watts=9.0, state="discharging", ac_online=False)))
        sampler, _, clock = self.sampler(values)
        results = [self.take(sampler, clock) for _ in values]
        self.assertTrue(results[-2].display.power_approximate)
        self.assertEqual((results[-1].raw.power_w, results[-1].raw.power_method),
                         (9.0, "power-now"))
        self.assertFalse(results[-1].display.power_approximate)

    def test_charging_does_not_use_counter_fallback(self):
        first = replace(raw(1000, watts=None), energy_now_wh=20.0)
        second = replace(first, timestamp=1120, monotonic_s=1120,
                         boottime_s=1120, energy_now_wh=20.2)
        sampler, _, clock = self.sampler([first, second], interval=120)
        self.assertIsNone(self.take(sampler, clock, 120).display.power_w)
        self.assertIsNone(self.take(sampler, clock, 120).display.power_w)

    def test_live_current_voltage_direct_fallback_is_not_approximate(self):
        item = replace(raw(1000, watts=None), current_now_a=.5, voltage_now_v=12)
        sampler, _, clock = self.sampler([item])
        result = self.take(sampler, clock)
        self.assertEqual((result.raw.power_w, result.raw.power_method),
                         (6, "current-voltage"))
        self.assertFalse(result.display.power_approximate)

    def test_unavailable_source_clears_display(self):
        sampler = LiveSampler(UnavailableSource(), clock=lambda: 1, wall_clock=lambda: 1)
        result = sampler.poll()
        self.assertEqual((result.raw, result.display, result.error),
                         (None, None, "no usable sysfs battery"))

    def test_source_unavailable_then_recovery_is_fresh_and_read_only(self):
        clock = FakeClock()
        source = SequenceSource([
            (raw(1000, watts=15),), SourceUnavailable("temporary sysfs failure"),
            (raw(1002, watts=14),),
        ])
        sampler = LiveSampler(source, clock=clock, wall_clock=clock)
        with patch.object(subprocess, "run") as run, patch.object(sqlite3, "connect") as connect:
            first = self.take(sampler, clock)
            failed = self.take(sampler, clock)
            recovered = self.take(sampler, clock)
        run.assert_not_called(); connect.assert_not_called()
        self.assertEqual(first.display.power_w, 15)
        self.assertEqual((failed.raw, failed.display, failed.error),
                         (None, None, "temporary sysfs failure"))
        self.assertEqual(recovered.display.power_w, 14)
        self.assertEqual(source.calls, 3)

    def test_sysfs_live_path_invokes_no_subprocess_or_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"; battery.mkdir()
            for name, value in {"type": "Battery", "capacity": "50",
                                "status": "Charging", "power_now": "12000000"}.items():
                (battery / name).write_text(value, encoding="utf-8")
            mains = root / "AC"; mains.mkdir()
            (mains / "type").write_text("Mains", encoding="utf-8")
            (mains / "online").write_text("1", encoding="utf-8")
            sampler = LiveSampler(SysfsSource(root), clock=lambda: 1, wall_clock=lambda: 1)
            with patch.object(subprocess, "run") as run, patch.object(sqlite3, "connect") as connect:
                result = sampler.poll()
            run.assert_not_called(); connect.assert_not_called()
            self.assertEqual(result.display.power_w, 12)

    def test_live_mode_does_not_infer_charging_from_soc(self):
        self.assertEqual(classify_live_mode(True, "not charging"),
                         LiveOperatingMode.CONNECTED_IDLE)
        self.assertEqual(classify_live_mode(False, "charging"),
                         LiveOperatingMode.TRANSITIONAL)


class SysfsOutcomeTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        battery = root / "BAT0"; battery.mkdir()
        for name, value in {"type": "Battery", "capacity": "50",
                            "status": "Discharging"}.items():
            (battery / name).write_text(value, encoding="utf-8")
        return temporary, root, battery

    def test_absent_invalid_and_valid_zero_are_distinct(self):
        temporary, root, battery = self.make_root()
        with temporary:
            (battery / "current_now").write_text("bad", encoding="utf-8")
            (battery / "voltage_now").write_text("0", encoding="utf-8")
            observed = SysfsSource(root).read_observed(1)
        fields = observed.battery_fields["BAT0"]
        self.assertEqual(fields["power_now"].status, FieldStatus.ABSENT)
        self.assertEqual(fields["current_now"].status, FieldStatus.INVALID)
        self.assertEqual((fields["voltage_now"].status, fields["voltage_now"].value),
                         (FieldStatus.VALUE, 0))

    def test_existing_enodev_is_error(self):
        temporary, root, battery = self.make_root()
        with temporary:
            (battery / "current_now").write_text("1", encoding="utf-8")
            original = Path.read_text
            def failing(path, *args, **kwargs):
                if path.name == "current_now":
                    raise OSError(errno.ENODEV, "No such device")
                return original(path, *args, **kwargs)
            with patch.object(Path, "read_text", failing):
                observed = SysfsSource(root).read_observed(1)
        self.assertEqual(observed.battery_fields["BAT0"]["current_now"].status,
                         FieldStatus.ERROR)


if __name__ == "__main__":
    unittest.main()
