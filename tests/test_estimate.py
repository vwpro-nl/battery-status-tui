from __future__ import annotations

import unittest

from battery_status_tui.estimate import estimate_remaining, robust_slope, smooth_seconds
from battery_status_tui.models import Measurement


def sample(timestamp: int, percentage: float, kind: str = "discharging") -> Measurement:
    return Measurement(timestamp, percentage, kind, kind == "charging", source="test", device="BAT0")


class EstimateTests(unittest.TestCase):
    def test_robust_discharge_trend_ignores_outlier(self):
        points = [sample(index * 300, 80 - index) for index in range(9)]
        points[4] = sample(1200, 95)
        slope = robust_slope(points, "discharging", 2400)
        self.assertAlmostEqual(slope or 0, -12, delta=0.5)

    def test_charging_is_mirror_of_discharging(self):
        discharge = [sample(index * 300, 80 - index) for index in range(9)]
        charge = [sample(index * 300, 20 + index, "charging") for index in range(9)]
        self.assertAlmostEqual(robust_slope(discharge, "discharging", 2400) or 0, -12)
        self.assertAlmostEqual(robust_slope(charge, "charging", 2400) or 0, 12)

    def test_upower_is_fallback_for_short_session(self):
        current = Measurement(300, 48, "discharging", False, time_to_empty_s=7200)
        estimate = estimate_remaining(current, [current], 300)
        self.assertEqual((estimate.seconds, estimate.source), (7200, "upower"))

    def test_smoothing_limits_single_update(self):
        self.assertEqual(smooth_seconds(7200, 3600), 6300)


if __name__ == "__main__":
    unittest.main()

