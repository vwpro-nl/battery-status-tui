from __future__ import annotations

import unittest
from dataclasses import replace
import re

from battery_status_tui.graph import CYAN, DIM, RESET, title_line
from battery_status_tui.live import LiveReading
from battery_status_tui.models import Measurement, RawBatterySnapshot, Session
from battery_status_tui.v1_history import CompletedChargeEvidence, V1HistorySnapshot
from battery_status_tui.v1_runtime import (
    CompletedChargeDisplay, WeightedLivePowerDisplay, _live_title_overlay,
    render_v1_view,
)


def measurement(*, timestamp=1000, percentage=60, state="discharging",
                ac_online=False, power=8, approximate=True,
                identity="BAT0|model|serial") -> Measurement:
    raw = RawBatterySnapshot(
        timestamp, float(timestamp), float(timestamp), "boot", "BAT0", identity,
        percentage, state, ac_online, power_now_w=power, sources=("sysfs",),
    )
    return Measurement(
        timestamp, percentage, state, ac_online, power_w=power,
        power_method="power-now" if power is not None else "unavailable",
        power_approximate=approximate if power is not None else False,
        boot_id="boot", battery_identity=identity, raw_batteries=(raw,),
    )


def live_reading(*, percentage=61, state="discharging", ac_online=False,
                 raw_power=None, display_power=None) -> LiveReading:
    raw = measurement(percentage=percentage, state=state, ac_online=ac_online,
                      power=raw_power, approximate=False)
    display = replace(raw, power_w=display_power,
                      power_method="power-now" if display_power is not None else "unavailable")
    return LiveReading(raw, display)


class LiveViewerOverlayTests(unittest.TestCase):
    def overlay(self, persisted, live, *, now=1050, interval_ms=60_000):
        return _live_title_overlay(persisted, live, now, interval_ms)

    def test_live_charging_and_discharging_control_presentation(self):
        persisted = measurement()
        charging, direction = self.overlay(
            persisted, live_reading(state="charging", ac_online=True,
                                    display_power=13.5))
        self.assertEqual((direction, charging.state, charging.percentage,
                          charging.power_w), ("charging", "charging", 61, 13.5))
        discharging, direction = self.overlay(
            replace(persisted, state="charging", ac_online=True),
            live_reading(display_power=7.5))
        self.assertEqual((direction, discharging.state, discharging.power_w),
                         ("discharging", "discharging", 7.5))

    def test_idle_transitional_unknown_and_source_failure_are_neutral(self):
        persisted = measurement()
        readings = (
            live_reading(state="not charging", ac_online=True),
            live_reading(state="charging", ac_online=False),
            live_reading(state="unknown", ac_online=None),
            LiveReading(None, None, "sysfs unavailable"),
        )
        for live in readings:
            with self.subTest(live=live):
                overlaid, direction = self.overlay(persisted, live)
                self.assertEqual(direction, "neutral")
                self.assertIsNone(overlaid.power_w)

    def test_live_soc_overlays_and_invalid_or_unavailable_soc_falls_back(self):
        persisted = measurement(percentage=60)
        overlaid, _ = self.overlay(persisted, live_reading(percentage=72))
        self.assertEqual(overlaid.percentage, 72)
        invalid, _ = self.overlay(persisted, live_reading(percentage=float("nan")))
        self.assertEqual(invalid.percentage, 60)
        unavailable, _ = self.overlay(persisted, LiveReading(None, None, "error"))
        self.assertEqual(unavailable.percentage, 60)

    def test_live_power_and_confirmed_idle_zero_override_persisted_power(self):
        persisted = measurement(power=8)
        active, _ = self.overlay(persisted, live_reading(display_power=11.2))
        self.assertEqual((active.power_w, active.power_approximate), (11.2, False))
        idle, direction = self.overlay(
            persisted, live_reading(state="not charging", ac_online=True,
                                    raw_power=0, display_power=0))
        self.assertEqual((direction, idle.power_w), ("neutral", 0))

    def test_default_does_not_substitute_persisted_power_for_live(self):
        persisted = measurement(power=8, approximate=True)
        overlaid, direction = self.overlay(persisted, live_reading())
        self.assertEqual((direction, overlaid.power_w, overlaid.power_approximate),
                         ("discharging", None, False))

    def test_persisted_fallback_rejects_idle_opposite_direction_and_stale_power(self):
        discharge = measurement(power=8)
        idle, _ = self.overlay(
            discharge, live_reading(state="not charging", ac_online=True))
        self.assertIsNone(idle.power_w)
        charging = measurement(state="charging", ac_online=True, power=15)
        wrong, _ = self.overlay(charging, live_reading())
        self.assertIsNone(wrong.power_w)
        stale, _ = self.overlay(discharge, live_reading(), now=1121)
        self.assertIsNone(stale.power_w)
        boundary, _ = self.overlay(discharge, live_reading(), now=1120)
        self.assertIsNone(boundary.power_w)

    def test_live_overlay_changes_only_title_render_inputs(self):
        persisted = measurement()
        history = (replace(persisted, timestamp=900, percentage=63),)
        session = Session(1, "discharging", 900, None, 63, None)
        view = V1HistorySnapshot(
            persisted, history, history, session, (), None, None, None, {}, 1,
            60_000, (),
        )
        before = render_v1_view(view)
        after = render_v1_view(
            view, live=live_reading(percentage=75, display_power=12), live_now=1050,
        )
        self.assertNotEqual(before.splitlines()[0], after.splitlines()[0])
        self.assertEqual(before.splitlines()[1:], after.splitlines()[1:])
        self.assertEqual(view.current.percentage, 60)
        self.assertEqual(view.history, history)

    def test_no_live_reading_preserves_existing_render_exactly(self):
        persisted = measurement()
        view = V1HistorySnapshot(
            persisted, (), (), None, (), None, None, None, {}, 1, 60_000, (),
        )
        self.assertEqual(render_v1_view(view), render_v1_view(view, live=None))

    def test_diagnostic_power_pair_has_exact_values_and_styling(self):
        persisted = measurement(power=13.1, approximate=False)
        actual = measurement(power=12.4, approximate=False)
        rendered = title_line(
            persisted, diagnostic_power=(persisted, actual, actual),
            show_persisted_power=True,
        )
        self.assertIn(f"{DIM}13.1W / {RESET}12.4W", rendered)
        self.assertIn("13.1W / 12.4W", re.sub(r"\x1b\[[0-9;]*m", "", rendered))

    def test_diagnostic_power_sides_are_independently_unavailable(self):
        available = measurement(power=12.4, approximate=False)
        unavailable = measurement(power=None, approximate=False)
        expected = {
            (13.1, 12.4): "13.1W / 12.4W",
            (13.1, None): "13.1W / --W",
            (None, 12.4): "--W / 12.4W",
            (None, None): "--W / --W",
        }
        for (left, right), text in expected.items():
            persisted = replace(available, power_w=left)
            live = replace(available, power_w=right) if right is not None else unavailable
            rendered = re.sub(
                r"\x1b\[[0-9;]*m", "",
                title_line(persisted, diagnostic_power=(persisted, live, None),
                           show_persisted_power=True),
            )
            with self.subTest(text=text):
                self.assertIn(text, rendered)
                self.assertNotRegex(text, r"\sW")

    def test_diagnostic_right_never_uses_persisted_fallback(self):
        persisted = measurement(power=8, approximate=True)
        live = live_reading(display_power=None)
        title_current, direction = self.overlay(persisted, live)
        self.assertIsNone(title_current.power_w)
        view = V1HistorySnapshot(
            persisted, (), (), None, (), None, None, None, {}, 1, 60_000, (),
        )
        rendered = re.sub(
            r"\x1b\[[0-9;]*m", "",
            render_v1_view(view, live=live, live_now=1050,
                           diagnostic_live_power=True),
        )
        self.assertIn("--W", rendered.splitlines()[0])
        normal = render_v1_view(view, live=live, live_now=1050)
        self.assertNotIn(" / ", re.sub(r"\x1b\[[0-9;]*m", "", normal.splitlines()[0]))


class WeightedLivePowerDisplayTests(unittest.TestCase):
    @staticmethod
    def reading(watts, observation, *, epoch=0, approximate=False,
                state="charging", ac_online=True):
        item = measurement(power=watts, approximate=approximate,
                           state=state, ac_online=ac_online)
        return LiveReading(item, item, power_epoch=epoch,
                           power_observation_at=observation)

    def test_one_through_five_sample_increasing_weights(self):
        weighted = WeightedLivePowerDisplay()
        values = (10, 20, 30, 40, 50)
        expected = (10, 50 / 3, 140 / 6, 300 / 10, 550 / 15)
        for index, (value, result) in enumerate(zip(values, expected), 1):
            with self.subTest(samples=index):
                display = weighted.update(self.reading(value, index))
                self.assertAlmostEqual(display.power_w, result)

    def test_sixth_sample_rolls_the_five_sample_window(self):
        weighted = WeightedLivePowerDisplay()
        for index, value in enumerate((10, 20, 30, 40, 50, 60), 1):
            display = weighted.update(self.reading(value, index))
        self.assertAlmostEqual(display.power_w, 700 / 15)

    def test_epoch_reset_and_unavailable_startup(self):
        weighted = WeightedLivePowerDisplay()
        unavailable = LiveReading(None, None, power_epoch=0)
        self.assertIsNone(weighted.update(unavailable))
        weighted.update(self.reading(10, 1, epoch=0))
        reset = weighted.update(self.reading(30, 2, epoch=1))
        self.assertEqual(reset.power_w, 30)

    def test_cached_sparse_estimate_is_not_inserted_twice(self):
        weighted = WeightedLivePowerDisplay()
        first = weighted.update(self.reading(
            12, 120, approximate=True, state="discharging", ac_online=False))
        repeated = weighted.update(self.reading(
            12, 120, approximate=True, state="discharging", ac_online=False))
        self.assertEqual((first.power_w, repeated.power_w), (12, 12))
        self.assertEqual(len(weighted._samples), 1)
        self.assertTrue(repeated.power_approximate)

    def test_direct_charging_observations_update_even_at_same_value(self):
        weighted = WeightedLivePowerDisplay()
        weighted.update(self.reading(12, 1))
        result = weighted.update(self.reading(12, 2))
        self.assertEqual(len(weighted._samples), 2)
        self.assertEqual(result.power_w, 12)
        self.assertFalse(result.power_approximate)

    def test_third_title_value_uses_cyan_and_preserves_approximation(self):
        persisted = measurement(power=13.6, approximate=True)
        actual = measurement(power=12.8, approximate=True)
        weighted = measurement(power=12.9, approximate=True)
        raw = title_line(
            persisted, "balanced", diagnostic_power=(persisted, actual, weighted),
            show_persisted_power=True, show_weighted_power=True,
        )
        self.assertIn("~13.6W / ~12.8W / ~12.9W ", re.sub(
            r"\x1b\[[0-9;]*m", "", raw))
        self.assertIn(CYAN + "~12.9W", raw)

    def test_default_and_optional_power_tokens_and_precision(self):
        persisted = measurement(power=13.64, approximate=False)
        live = measurement(power=12.84, approximate=False)
        weighted = measurement(power=12.94, approximate=False)
        default = re.sub(r"\x1b\[[0-9;]*m", "", title_line(live))
        self.assertIn("12.8W ↓", default)
        self.assertNotIn("13.6W", default)
        persisted_only = re.sub(r"\x1b\[[0-9;]*m", "", title_line(
            live, diagnostic_power=(persisted, live, weighted),
            show_persisted_power=True))
        self.assertIn("13.6W / 12.8W", persisted_only)
        weighted_only = re.sub(r"\x1b\[[0-9;]*m", "", title_line(
            live, diagnostic_power=(persisted, live, weighted),
            show_weighted_power=True))
        self.assertIn("12.8W / 12.9W", weighted_only)
        combined = re.sub(r"\x1b\[[0-9;]*m", "", title_line(
            live, diagnostic_power=(persisted, live, weighted),
            show_persisted_power=True, show_weighted_power=True,
            power_decimals=2))
        self.assertIn("13.64W / 12.84W / 12.94W", combined)


class CompletedChargeDisplayTests(unittest.TestCase):
    @staticmethod
    def view(session):
        current = measurement(timestamp=1000, percentage=100, state="charging",
                              ac_online=True)
        return V1HistorySnapshot(
            current, (), (), session, (), None, None, None, {}, 1, 60_000, (),
        )

    @staticmethod
    def reading(state, *, epoch=0, percentage=100):
        item = measurement(timestamp=1000, percentage=percentage, state=state,
                           ac_online=True)
        return LiveReading(item, item, power_epoch=epoch,
                           power_observation_at=1000)

    def test_full_transition_freezes_and_renders_two_line_charge_full(self):
        tracker = CompletedChargeDisplay()
        session = Session(7, "charging", 1000, None, 80, None)
        view = self.view(session)
        self.assertIsNone(tracker.update(view, self.reading("charging"), 1020))
        frozen = tracker.update(view, self.reading("full", epoch=1), 2740)
        self.assertEqual(frozen, 1740)
        self.assertEqual(tracker.update(view, self.reading("full", epoch=1), 9000), 1740)
        rendered = render_v1_view(
            view, live=self.reading("full", epoch=1), live_now=9000,
            completed_charge_seconds=frozen,
        )
        lines = re.sub(r"\x1b\[[0-9;]*m", "", rendered).splitlines()
        self.assertTrue(lines[2].startswith("0h29m"))
        self.assertEqual(lines[2][54:], "      ")
        self.assertTrue(lines[3].startswith("charge"))
        self.assertEqual(lines[3][54:], "  full")
        self.assertNotIn("n/a", "\n".join(lines[2:4]))

    def test_below_full_idle_does_not_complete(self):
        tracker = CompletedChargeDisplay()
        session = Session(7, "charging", 1000, None, 80, None)
        view = self.view(session)
        tracker.update(view, self.reading("charging", percentage=98), 1020)
        self.assertIsNone(tracker.update(
            view, self.reading("not charging", percentage=98), 1041))

    def test_new_epoch_clears_stale_duration_and_new_charge_replaces_it(self):
        tracker = CompletedChargeDisplay()
        first = self.view(Session(7, "charging", 1000, None, 80, None))
        tracker.update(first, self.reading("charging"), 1020)
        self.assertEqual(tracker.update(first, self.reading("full", epoch=1), 1041), 41)
        second = self.view(Session(8, "charging", 2000, None, 70, None))
        self.assertIsNone(tracker.update(
            second, self.reading("charging", epoch=2), 2010))
        self.assertEqual(tracker.update(
            second, self.reading("full", epoch=3), 2030), 30)

    def test_full_on_incompatible_boot_does_not_reuse_charging_duration(self):
        tracker = CompletedChargeDisplay()
        view = self.view(Session(7, "charging", 1000, None, 80, None))
        tracker.update(view, self.reading("charging"), 1020)
        full = self.reading("full", epoch=1)
        changed_battery = replace(full.raw.raw_batteries[0], boot_id="new-boot")
        changed_raw = replace(full.raw, boot_id="new-boot",
                              raw_batteries=(changed_battery,))
        incompatible = replace(full, raw=changed_raw, display=changed_raw)
        self.assertIsNone(tracker.update(view, incompatible, 3460))

    def test_restart_reconstructs_from_persisted_completion_not_current_time(self):
        tracker = CompletedChargeDisplay()
        view = replace(
            self.view(None),
            completed_charge=CompletedChargeEvidence(1000, 2740, "boot",
                                                      "battery-set"),
        )
        reconstructed = tracker.update(view, self.reading("full", epoch=4), 9000)
        self.assertEqual(reconstructed, 1740)
        self.assertEqual(tracker.update(view, self.reading("full", epoch=4), 99_000),
                         1740)

    def test_restart_rejects_incompatible_boot_evidence(self):
        tracker = CompletedChargeDisplay()
        view = replace(
            self.view(None),
            completed_charge=CompletedChargeEvidence(1000, 2740, "old-boot",
                                                      "battery-set"),
        )
        self.assertIsNone(tracker.update(view, self.reading("full", epoch=4), 9000))


class LiveEtaPresentationTests(unittest.TestCase):
    @staticmethod
    def view(kind, *, eta=3600):
        state, ac = (("charging", True) if kind == "charging"
                     else ("discharging", False))
        current = measurement(timestamp=1000, percentage=50, state=state,
                              ac_online=ac)
        current = replace(
            current,
            time_to_full_s=eta if kind == "charging" else None,
            time_to_empty_s=eta if kind == "discharging" else None,
        )
        session = Session(1, kind, 500, None, 40, None)
        return V1HistorySnapshot(
            current, (), (), session, (), None, None, None, {}, 1, 60_000, (),
        )

    @staticmethod
    def live(kind):
        state, ac = (("charging", True) if kind == "charging"
                     else ("discharging", False))
        item = measurement(timestamp=1001, percentage=50, state=state,
                           ac_online=ac)
        return LiveReading(item, item, power_epoch=1, power_observation_at=1001)

    def test_opposite_direction_eta_is_immediately_suppressed(self):
        for persisted, live, semantic in (
            ("discharging", "charging", "full"),
            ("charging", "discharging", "empty"),
        ):
            with self.subTest(live=live):
                lines = re.sub(r"\x1b\[[0-9;]*m", "", render_v1_view(
                    self.view(persisted), live=self.live(live), live_now=1001,
                )).splitlines()
                self.assertTrue(lines[2].startswith("--"))
                self.assertEqual(lines[2][54:], "    --")
                self.assertEqual(lines[3][54:].strip(), semantic)
                self.assertNotIn("1h00m", lines[2])

    def test_compatible_fresh_eta_restores_normally(self):
        for kind, semantic in (("charging", "full"),
                               ("discharging", "empty")):
            with self.subTest(kind=kind):
                lines = re.sub(r"\x1b\[[0-9;]*m", "", render_v1_view(
                    self.view(kind), live=self.live(kind), live_now=1001,
                )).splitlines()
                self.assertEqual(lines[2][54:], " 1h00m")
                self.assertEqual(lines[3][54:].strip(), semantic)


if __name__ == "__main__":
    unittest.main()
