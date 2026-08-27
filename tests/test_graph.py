from __future__ import annotations

import datetime as dt
import re
import unittest

from battery_status_tui.graph import (
    COLUMN_SECONDS, FORECAST_SECONDS, GRAPH_OFFSET, GRAPH_WIDTH, HISTORY_SECONDS,
    NOW_INDEX, TIME_COLUMNS, axis_rows, chart_rows, column_timestamp,
    project_column, render_dashboard, title_line,
)
from battery_status_tui.models import Estimate, Measurement, Session, SleepInterval


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value: str) -> str:
    return ANSI.sub("", value)


def stamp(hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 8, 27, hour, minute).astimezone().timestamp())


def sample(timestamp: int, percentage: float = 50) -> Measurement:
    return Measurement(timestamp, percentage, "discharging", False)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.now = stamp(21)
        self.current = Measurement(self.now, 48, "discharging", False, power_w=8.4)
        self.history = [sample(timestamp, 60 - index / 10)
                        for index, timestamp in enumerate(range(self.now - HISTORY_SECONDS, self.now, 60))]

    def test_twelve_hours_use_36_time_columns(self):
        self.assertEqual(TIME_COLUMNS, 36)
        self.assertEqual(COLUMN_SECONDS, 20 * 60)
        self.assertEqual((HISTORY_SECONDS, FORECAST_SECONDS), (6 * 3600, 6 * 3600))
        self.assertEqual(GRAPH_WIDTH, 37)  # 36 time cells plus the central NOW marker
        self.assertEqual(NOW_INDEX, 18)

    def test_projection_moves_once_at_each_twenty_minute_boundary(self):
        fixed = stamp(20)
        expected = {
            (20, 0): 18, (20, 19): 18,
            (20, 20): 17, (20, 39): 17,
            (20, 40): 16, (20, 59): 16,
            (21, 0): 15,
        }
        for (hour, minute), column in expected.items():
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(project_column(fixed, stamp(hour, minute)), column)

    def test_visible_history_moves_at_each_live_projection_boundary(self):
        history = [sample(stamp(20), 80)]
        expected = {(22, 19): 12, (22, 20): 11, (22, 39): 11, (22, 40): 10}
        for (hour, minute), occupied_column in expected.items():
            with self.subTest(hour=hour, minute=minute):
                now = stamp(hour, minute)
                top, bottom = chart_rows(sample(now), history, None, now)
                occupied = [index for index in range(NOW_INDEX)
                            if top[index] != " " or bottom[index] != " "]
                self.assertEqual(occupied, [occupied_column])

    def test_column_inverse_uses_same_projection(self):
        now = stamp(20, 19)
        for column in range(GRAPH_WIDTH):
            self.assertEqual(project_column(column_timestamp(column, now), now), column)

    def test_axis_labels_advance_only_on_each_full_hour(self):
        expected = {
            (21, 59): ["15", "18", "21", "00", "03"],
            (22, 0): ["16", "19", "22", "01", "04"],
            (23, 0): ["17", "20", "23", "02", "05"],
        }
        for (hour, minute), labels in expected.items():
            with self.subTest(hour=hour, minute=minute):
                axis, rendered = axis_rows(stamp(hour, minute))
                self.assertEqual(re.findall(r"\d{2}", rendered), labels)
                self.assertEqual([index for index, character in enumerate(axis) if character == "┬"],
                                 [0, 9, 18, 27, 36])

    def test_axis_labels_do_not_follow_twenty_minute_data_shifts(self):
        self.assertEqual(axis_rows(stamp(21, 0)), axis_rows(stamp(21, 20)))
        self.assertEqual(axis_rows(stamp(21, 20)), axis_rows(stamp(21, 40)))

    def test_hour_boundary_is_one_column_not_an_hour_jump(self):
        fixed = stamp(20)
        self.assertEqual(project_column(fixed, stamp(20, 59)) - project_column(fixed, stamp(21)), 1)

    def test_now_marker_forecast_and_title_arrow_share_column(self):
        top, bottom = chart_rows(self.current, self.history, Estimate(7200, "test"), self.now)
        self.assertEqual(len(top), GRAPH_WIDTH)
        self.assertEqual(top[NOW_INDEX], "│")
        self.assertEqual(bottom[NOW_INDEX], "│")
        self.assertEqual(plain(title_line(self.current))[GRAPH_OFFSET + NOW_INDEX], "↓")
        self.assertTrue((top[NOW_INDEX + 1:] + bottom[NOW_INDEX + 1:]).strip())

    def test_forecast_stops_at_estimated_empty(self):
        top, bottom = chart_rows(self.current, self.history, Estimate(1800, "test"), self.now)
        endpoint = project_column(self.now + 1800, self.now)
        self.assertTrue((top[NOW_INDEX + 1:endpoint + 1] + bottom[NOW_INDEX + 1:endpoint + 1]).strip())
        self.assertFalse((top[endpoint + 1:] + bottom[endpoint + 1:]).strip())

    def test_pre_sleep_sleep_and_post_resume_segments_all_remain_visible(self):
        sleep = SleepInterval(stamp(18), stamp(20), pre_percentage=55, post_percentage=51)
        top, bottom = chart_rows(self.current, self.history, None, self.now, [sleep])
        self.assertNotEqual((top[8], bottom[8]), (" ", " "))  # 17:40 history
        self.assertEqual(top[9:15], "⣀⣀⣀⣀⣀⣀")
        self.assertEqual(bottom[9:15], " z  z ")
        self.assertNotEqual((top[15], bottom[15]), (" ", " "))  # 20:00 resumed history
        self.assertNotEqual((top[17], bottom[17]), (" ", " "))

    def test_short_sleep_marks_at_least_one_column(self):
        sleep = SleepInterval(stamp(20, 5), stamp(20, 10), pre_percentage=49, post_percentage=49)
        top, bottom = chart_rows(self.current, self.history, None, self.now, [sleep])
        self.assertEqual(top.count("⣀"), 1)
        self.assertEqual(bottom.count("z"), 1)

    def test_partial_sleep_boundary_does_not_overwrite_adjacent_history(self):
        history = [sample(stamp(17, 55), 56), sample(stamp(20, 5), 51)]
        sleep = SleepInterval(stamp(17, 57), stamp(20, 3), pre_percentage=56, post_percentage=51)
        top, bottom = chart_rows(self.current, history, None, self.now, [sleep])
        before = project_column(stamp(17, 55), self.now)
        after = project_column(stamp(20, 5), self.now)
        self.assertNotEqual((top[before], bottom[before]), ("⣀", "z"))
        self.assertNotEqual((top[before], bottom[before]), (" ", " "))
        self.assertNotEqual((top[after], bottom[after]), ("⣀", "z"))
        self.assertNotEqual((top[after], bottom[after]), (" ", " "))

    def test_sleep_width_tracks_twenty_minute_columns(self):
        sleep = SleepInterval(stamp(18), stamp(21), pre_percentage=55, post_percentage=50)
        top, bottom = chart_rows(self.current, self.history, None, self.now, [sleep])
        self.assertEqual(top.count("⣀"), 9)
        self.assertEqual(bottom.count("z"), 3)
        self.assertEqual([index for index, character in enumerate(bottom) if character == "z"], [10, 13, 16])

    def test_unmeasured_non_sleep_gap_stays_empty(self):
        history = [sample(stamp(16)), sample(stamp(18))]
        top, bottom = chart_rows(self.current, history, None, self.now)
        middle = project_column(stamp(17), self.now)
        self.assertEqual((top[middle], bottom[middle]), (" ", " "))

    def test_dashboard_keeps_two_graph_rows_dims_sleep_and_uses_one_cell_margins(self):
        sleep = SleepInterval(stamp(18), stamp(19), pre_percentage=55, post_percentage=53)
        session = Session(1, "discharging", stamp(20), None, 55, None)
        rendered = render_dashboard(self.current, self.history, session, Estimate(7200, "test"), self.now, [sleep])
        lines = plain(rendered).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("1h00", lines[2])
        self.assertIn("⣀⣀⣀", lines[1])
        self.assertIn("z", lines[2])
        self.assertIn("\x1b[2m", rendered)
        self.assertEqual(lines[1].index("⣀"), GRAPH_OFFSET + project_column(stamp(18), self.now))
        self.assertEqual(lines[2][GRAPH_OFFSET - 1], " ")
        eta_start = GRAPH_OFFSET + GRAPH_WIDTH + 1
        self.assertEqual(lines[2][GRAPH_OFFSET + GRAPH_WIDTH], " ")
        self.assertEqual(lines[2][eta_start:], "2h00 ~23:00")
        self.assertEqual(max(len(line) for line in lines), 55)

    def test_approximate_power_has_tilde(self):
        current = Measurement(self.now, 48, "discharging", False, power_w=7.2, power_approximate=True)
        self.assertIn("~7.2 W", plain(title_line(current)))


if __name__ == "__main__":
    unittest.main()
