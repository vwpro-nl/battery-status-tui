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

    def test_current_bucket_is_hidden_until_it_closes(self):
        history = [sample(stamp(22, 0), 80), sample(stamp(22, 19), 70)]
        before_top, before_bottom = chart_rows(sample(stamp(22, 19)), history, None, stamp(22, 19))
        after_top, after_bottom = chart_rows(sample(stamp(22, 20)), history, None, stamp(22, 20))
        self.assertEqual((before_top[NOW_INDEX - 1], before_bottom[NOW_INDEX - 1]), (" ", " "))
        self.assertNotEqual((after_top[NOW_INDEX - 1], after_bottom[NOW_INDEX - 1]), (" ", " "))

    def test_now_column_is_exclusively_the_marker(self):
        now = stamp(22, 19)
        history = [sample(now, 80)]
        sleep = SleepInterval(stamp(22, 10), stamp(22, 18), pre_percentage=80, post_percentage=79)
        top, bottom = chart_rows(sample(now), history, Estimate(3600, "test"), now, [sleep])
        self.assertEqual((top[NOW_INDEX], bottom[NOW_INDEX]), ("│", "│"))
        self.assertNotIn("⣀", top[NOW_INDEX:NOW_INDEX + 1])
        self.assertNotIn("z", bottom[NOW_INDEX:NOW_INDEX + 1])

    def test_unclosed_sleep_bucket_is_not_moved_into_history(self):
        now = stamp(22, 19)
        sleep = SleepInterval(stamp(22, 10), stamp(22, 18), pre_percentage=80, post_percentage=79)
        top, bottom = chart_rows(sample(now), [], None, now, [sleep])
        self.assertNotIn("⣀", top[:NOW_INDEX])
        self.assertNotIn("z", bottom[:NOW_INDEX])

    def test_column_inverse_uses_same_projection(self):
        now = stamp(20, 19)
        for column in range(GRAPH_WIDTH):
            self.assertEqual(project_column(column_timestamp(column, now), now), column)

    def test_axis_ticks_and_labels_follow_the_twenty_minute_projection(self):
        midnight = stamp(23) + 3600
        expected = {
            stamp(23): (list(range(0, GRAPH_WIDTH, 3)), "17 18 19 20 21 22 23 00 01 02 03 04"),
            stamp(23, 20): (list(range(2, GRAPH_WIDTH, 3)), "  18 19 20 21 22 23 00 01 02 03 04 05"),
            stamp(23, 40): (list(range(1, GRAPH_WIDTH, 3)), " 18 19 20 21 22 23 00 01 02 03 04 05"),
            midnight: (list(range(0, GRAPH_WIDTH, 3)), "18 19 20 21 22 23 00 01 02 03 04 05"),
        }
        for now, (ticks, labels) in expected.items():
            with self.subTest(now=now):
                axis, rendered = axis_rows(now)
                self.assertEqual([index for index, character in enumerate(axis) if character == "┬"], ticks)
                self.assertEqual(rendered, labels)

    def test_axis_omits_partial_labels_and_admits_complete_right_label(self):
        self.assertNotIn("05", axis_rows(stamp(22, 40))[1])
        self.assertNotIn("05", axis_rows(stamp(23))[1])
        self.assertTrue(axis_rows(stamp(23, 20))[1].endswith("05"))
        self.assertFalse(axis_rows(stamp(23, 20))[1].startswith("7"))
        self.assertFalse(axis_rows(stamp(23, 40))[1].startswith("17"))
        axis, labels = axis_rows(stamp(23, 20))
        position = project_column(stamp(20), stamp(23, 20))
        self.assertEqual(labels[position:position + 2], "20")
        self.assertEqual(axis[position], "┬")

    def test_every_complete_hour_uses_its_real_position(self):
        axis, labels = axis_rows(stamp(23, 20))
        position = project_column(stamp(18), stamp(23, 20))
        self.assertEqual(position, 2)
        self.assertEqual(labels[position:position + 2], "18")
        self.assertEqual(axis[position], "┬")
        self.assertEqual(axis.count("┬"), 12)
        tick_positions = [index for index, character in enumerate(axis) if character == "┬"]
        self.assertTrue(all(right - left == 3 for left, right in zip(tick_positions, tick_positions[1:])))

    def test_absolute_tick_and_graph_data_use_same_projection(self):
        fixed = stamp(23)
        for now in (stamp(23), stamp(23, 20), stamp(23, 40), stamp(23) + 3600):
            with self.subTest(now=now):
                position = project_column(fixed, now)
                axis, _ = axis_rows(now)
                self.assertEqual(axis[position], "┬")
                top, bottom = chart_rows(sample(now), [sample(fixed)], None, now)
                if now > fixed:
                    self.assertNotEqual((top[position], bottom[position]), (" ", " "))

    def test_now_marker_stays_fixed_while_axis_moves(self):
        for now in (stamp(23), stamp(23, 20), stamp(23, 40), stamp(23) + 3600):
            top, bottom = chart_rows(sample(now), [], None, now)
            self.assertEqual((top[NOW_INDEX], bottom[NOW_INDEX]), ("│", "│"))

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
        self.assertIn("1h00", lines[1])
        self.assertTrue(lines[1].startswith("1h00"))
        self.assertTrue(lines[2].startswith("start"))
        self.assertIn("⣀⣀⣀", lines[1])
        self.assertIn("z", lines[2])
        self.assertIn("\x1b[2m", rendered)
        self.assertEqual(lines[1].index("⣀"), GRAPH_OFFSET + project_column(stamp(18), self.now))
        self.assertEqual(lines[1][GRAPH_OFFSET - 1], " ")
        eta_start = GRAPH_OFFSET + GRAPH_WIDTH + 1
        self.assertEqual(lines[1][GRAPH_OFFSET + GRAPH_WIDTH], " ")
        self.assertEqual(lines[1][eta_start:], "2h00 ~23:00")
        self.assertEqual(lines[2][eta_start:], "empty")
        self.assertIn("┬", lines[3])
        self.assertRegex(lines[4], r"\d{2}")
        self.assertEqual(max(len(line) for line in lines), 55)

    def test_meaning_labels_are_conditional(self):
        estimate = Estimate(3600, "test")
        discharge = render_dashboard(self.current, self.history, None, estimate, self.now)
        discharge_meanings = plain(discharge).splitlines()[2]
        self.assertNotIn("start", discharge_meanings)
        self.assertIn("empty", discharge_meanings)

        session = Session(1, "discharging", stamp(20), None, 55, None)
        no_eta_meanings = plain(render_dashboard(self.current, self.history, session, None, self.now)).splitlines()[2]
        self.assertTrue(no_eta_meanings.startswith("start"))
        self.assertNotIn("empty", no_eta_meanings)
        self.assertNotIn("full", no_eta_meanings)

        charging = Measurement(self.now, 48, "charging", True)
        charging_meanings = plain(render_dashboard(charging, self.history, session, estimate, self.now)).splitlines()[2]
        self.assertNotIn("empty", charging_meanings)
        self.assertEqual(charging_meanings[GRAPH_OFFSET + GRAPH_WIDTH + 1:], "full")

    def test_approximate_power_has_tilde(self):
        current = Measurement(self.now, 48, "discharging", False, power_w=7.2, power_approximate=True)
        self.assertIn("~7.2 W", plain(title_line(current)))


if __name__ == "__main__":
    unittest.main()
