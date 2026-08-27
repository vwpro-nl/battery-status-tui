from __future__ import annotations

import re
import unittest

from battery_status_tui.graph import GRAPH_OFFSET, NOW_INDEX, chart_rows, render_dashboard, title_line
from battery_status_tui.models import Estimate, Measurement, Session, SleepInterval


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value: str) -> str:
    return ANSI.sub("", value)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.current = Measurement(21600, 48, "discharging", False, power_w=8.4)
        self.history = [Measurement(timestamp, 60 - timestamp / 1800, "discharging", False) for timestamp in range(0, 21601, 900)]

    def test_now_marker_and_title_arrow_share_column(self):
        top, bottom = chart_rows(self.current, self.history, Estimate(7200, "test"), 21600)
        self.assertEqual(top[NOW_INDEX], "│")
        self.assertEqual(bottom[NOW_INDEX], "│")
        self.assertEqual(plain(title_line(self.current))[GRAPH_OFFSET + NOW_INDEX], "↓")

    def test_forecast_stops_at_estimated_empty(self):
        top, bottom = chart_rows(self.current, self.history, Estimate(1800, "test"), 21600)
        forecast = top[NOW_INDEX + 1 :] + bottom[NOW_INDEX + 1 :]
        self.assertTrue(forecast.rstrip())
        self.assertTrue(forecast.endswith(" " * 20))

    def test_dashboard_has_two_graph_rows(self):
        session = Session(1, "discharging", 18000, None, 55, None)
        rendered = plain(render_dashboard(self.current, self.history, session, Estimate(7200, "test"), 21600))
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("1h00", lines[2])

    def test_history_does_not_interpolate_over_gap(self):
        history = [Measurement(0, 60, "discharging", False), Measurement(3600, 50, "discharging", False)]
        top, bottom = chart_rows(self.current, history, None, 21600)
        self.assertEqual(top[2:4], "  ")
        self.assertEqual(bottom[2:4], "  ")

    def test_sleep_interval_has_level_line_and_z(self):
        sleep = SleepInterval(3600, 7200, pre_percentage=60)
        top, bottom = chart_rows(self.current, self.history, None, 21600, [sleep])
        self.assertIn("Z", top + bottom)

    def test_approximate_power_has_tilde(self):
        current = Measurement(21600, 48, "discharging", False, power_w=7.2, power_approximate=True)
        self.assertIn("~7.2 W", plain(title_line(current)))


if __name__ == "__main__":
    unittest.main()
