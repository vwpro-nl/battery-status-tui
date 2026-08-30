from __future__ import annotations

import datetime as dt
import re
import unittest
from unittest.mock import patch

import battery_status_tui.graph as graph_module
from battery_status_tui.graph import (
    COLUMN_SECONDS, GRAPH_OFFSET, GRAPH_WIDTH,
    HISTORY_SECONDS, MAX_SPAN_SECONDS, MIN_EARLY_SLOPE,
    NOW_INDEX, TIME_COLUMNS,
    _active_percentage_at, _battery_color, _braille_fill, _braille_fill_levels, _braille_mask,
    _braille_subcolumn_times,
    _chart_rows_and_percentages, _early_raster, _fill_chars, _forecast_span_columns, _sleep_columns,
    _sleep_fraction, _sleep_residual_transfer, _smooth_sleep_edges, _style_battery, axis_rows,
    chart_rows, column_timestamp,
    now_column, project_column, render_dashboard, title_line,
)
from battery_status_tui.models import Estimate, Measurement, Session, SleepInterval


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value: str) -> str:
    return ANSI.sub("", value)


def stamp(hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 8, 27, hour, minute).astimezone().timestamp())


def sample(timestamp: int, percentage: float = 50) -> Measurement:
    return Measurement(timestamp, percentage, "discharging", False)


# ceil(6h / 20min) == 18 forecast columns, which places NOW back at NOW_INDEX.
# Used to pin a classic centred viewport for tests that only exercise the
# history / sleep rendering mechanics and do not care where NOW sits.
CENTER = Estimate(6 * 3600, "pin")


def viewport(current: Measurement, estimate: Estimate | None = None) -> int:
    """The NOW column the renderer will use for this input."""
    return now_column(current, estimate)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.now = stamp(21)
        self.current = Measurement(self.now, 48, "discharging", False, power_w=8.4)
        self.history = [sample(timestamp, 60 - index / 10)
                        for index, timestamp in enumerate(range(self.now - HISTORY_SECONDS, self.now, 60))]

    def test_twelve_hours_use_36_time_columns(self):
        self.assertEqual(TIME_COLUMNS, 36)
        self.assertEqual(COLUMN_SECONDS, 20 * 60)
        self.assertEqual(HISTORY_SECONDS, 6 * 3600)
        self.assertEqual(MAX_SPAN_SECONDS, TIME_COLUMNS * COLUMN_SECONDS)
        self.assertEqual(GRAPH_WIDTH, 37)  # 36 time cells plus the NOW marker
        self.assertEqual(NOW_INDEX, 18)  # default projection origin only

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
        # Centred viewport (CENTER) so the single history bucket lands where it
        # always did; the point is that it steps one column per 20-minute boundary.
        expected = {(22, 19): 12, (22, 20): 11, (22, 39): 11, (22, 40): 10}
        for (hour, minute), occupied_column in expected.items():
            with self.subTest(hour=hour, minute=minute):
                now = stamp(hour, minute)
                top, bottom = chart_rows(sample(now), history, CENTER, now)
                occupied = [index for index in range(NOW_INDEX)
                            if top[index] != " " or bottom[index] != " "]
                self.assertEqual(occupied, [occupied_column])

    def test_current_bucket_is_hidden_until_it_closes(self):
        history = [sample(stamp(22, 0), 80), sample(stamp(22, 19), 70)]
        before_top, before_bottom = chart_rows(sample(stamp(22, 19)), history, CENTER, stamp(22, 19))
        after_top, after_bottom = chart_rows(sample(stamp(22, 20)), history, CENTER, stamp(22, 20))
        self.assertEqual((before_top[NOW_INDEX - 1], before_bottom[NOW_INDEX - 1]), (" ", " "))
        self.assertNotEqual((after_top[NOW_INDEX - 1], after_bottom[NOW_INDEX - 1]), (" ", " "))

    def test_now_column_is_exclusively_the_marker(self):
        now = stamp(22, 19)
        current = sample(now)
        estimate = Estimate(3600, "test")
        marker = viewport(current, estimate)
        history = [sample(now, 80)]
        sleep = SleepInterval(stamp(22, 10), stamp(22, 18), pre_percentage=80, post_percentage=79)
        top, bottom = chart_rows(current, history, estimate, now, [sleep])
        self.assertEqual((top[marker], bottom[marker]), ("│", "│"))
        self.assertNotIn("⣀", top[marker:marker + 1])
        self.assertNotIn("z", bottom[marker:marker + 1])

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
                current = sample(now)
                marker = viewport(current)
                position = project_column(fixed, now, marker)
                axis, _ = axis_rows(now, marker)
                self.assertEqual(axis[position], "┬")
                top, bottom = chart_rows(current, [sample(fixed)], None, now)
                if now > fixed:
                    self.assertNotEqual((top[position], bottom[position]), (" ", " "))

    def test_now_marker_sits_at_right_edge_when_nothing_is_forecast(self):
        for now in (stamp(23), stamp(23, 20), stamp(23, 40), stamp(23) + 3600):
            top, bottom = chart_rows(sample(now), [], None, now)
            self.assertEqual((top[GRAPH_WIDTH - 1], bottom[GRAPH_WIDTH - 1]), ("│", "│"))
            self.assertEqual(top.index("│"), GRAPH_WIDTH - 1)
            self.assertEqual(top[:GRAPH_WIDTH - 1].strip(), "")  # all of it is available to history

    def test_hour_boundary_is_one_column_not_an_hour_jump(self):
        fixed = stamp(20)
        self.assertEqual(project_column(fixed, stamp(20, 59)) - project_column(fixed, stamp(21)), 1)

    def test_now_marker_forecast_and_title_arrow_share_column(self):
        estimate = Estimate(7200, "test")
        marker = viewport(self.current, estimate)
        top, bottom = chart_rows(self.current, self.history, estimate, self.now)
        self.assertEqual(len(top), GRAPH_WIDTH)
        self.assertEqual(top[marker], "│")
        self.assertEqual(bottom[marker], "│")
        self.assertEqual(plain(title_line(self.current, None, marker))[GRAPH_OFFSET + marker], "↓")
        self.assertTrue((top[marker + 1:] + bottom[marker + 1:]).strip())
        self.assertFalse((top[:marker] + bottom[:marker]).count("│"))

    def test_now_column_tracks_the_forecast_horizon(self):
        now = self.now
        history = [sample(t, 50) for t in range(now - MAX_SPAN_SECONDS, now, 300)]
        cases = [
            ("charging-short", Measurement(now, 90, "charging", True), Estimate(40 * 60, "t"), 2),
            ("charging-long", Measurement(now, 20, "charging", True), Estimate(5 * 3600, "t"), 15),
            ("discharging-short", Measurement(now, 80, "discharging", False), Estimate(40 * 60, "t"), 2),
            ("discharging-long", Measurement(now, 80, "discharging", False), Estimate(8 * 3600, "t"), 18),
            ("full-no-eta", Measurement(now, 100, "full", True), None, 0),
        ]
        markers = {}
        spans = {}
        for name, current, estimate, span in cases:
            with self.subTest(case=name):
                marker = viewport(current, estimate)
                markers[name] = marker
                self.assertEqual(marker, GRAPH_WIDTH - 1 - span)  # NOW column

                top, bottom, _ = _chart_rows_and_percentages(current, history, estimate, now)
                self.assertEqual((top[marker], bottom[marker]), ("│", "│"))

                def braille(index):
                    glyphs = (top[index] + bottom[index]).strip()
                    return bool(glyphs) and all(0x2800 <= ord(c) <= 0x28ff for c in glyphs)

                forecast_cells = [i for i in range(GRAPH_WIDTH) if i != marker and braille(i)]
                self.assertEqual(forecast_cells, list(range(marker + 1, GRAPH_WIDTH)))
                spans[name] = len(forecast_cells)

                history_cells = [i for i in range(marker)
                                 if (top[i] + bottom[i]).strip()]
                self.assertEqual(history_cells, list(range(marker)))  # all freed width used
                self.assertTrue(all(c in " ▁▂▃▄▅▆▇█"
                                    for c in top[:marker] + bottom[:marker]))

        self.assertEqual(spans["full-no-eta"], 0)
        self.assertLess(markers["discharging-long"], markers["discharging-short"])
        self.assertLess(markers["charging-long"], markers["charging-short"])
        self.assertLess(markers["discharging-short"], markers["full-no-eta"])
        self.assertEqual(markers["full-no-eta"], GRAPH_WIDTH - 1)
        # width freed on the left grows by exactly what the forecast gave up
        self.assertEqual(markers["discharging-short"] - markers["discharging-long"],
                         spans["discharging-long"] - spans["discharging-short"])

    def test_history_remains_solid_and_forecast_uses_braille_fill(self):
        estimate = Estimate(7200, "test")
        marker = viewport(self.current, estimate)
        top, bottom = chart_rows(self.current, self.history, estimate, self.now)
        history_glyphs = (top[:marker] + bottom[:marker]).replace(" ", "")
        forecast_glyphs = (top[marker + 1:] + bottom[marker + 1:]).replace(" ", "")
        self.assertTrue(history_glyphs)
        self.assertTrue(all(character in "▁▂▃▄▅▆▇█" for character in history_glyphs))
        self.assertTrue(forecast_glyphs)
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28ff for character in forecast_glyphs))

    def test_braille_fill_height_tracks_forecast_percentage(self):
        for percentage in (0, 25, 50, 75, 100):
            with self.subTest(percentage=percentage):
                top, bottom = _braille_fill(percentage)
                dots = sum((ord(character) - 0x2800).bit_count() for character in (top, bottom) if character != " ")
                self.assertEqual(dots // 2, max(1, round(percentage / 100 * 8)))

    def test_low_braille_subcolumns_keep_one_dot_including_zero(self):
        for percentage in (0, 1, 5, 10):
            with self.subTest(percentage=percentage):
                top, bottom = _braille_fill(percentage)
                self.assertEqual((top, bottom), (" ", "⣀"))

    def test_braille_fill_maximizes_complete_cells_and_only_partially_fills_top(self):
        self.assertEqual(_braille_fill(50), (" ", "⣿"))
        self.assertEqual(_braille_fill(75), ("⣤", "⣿"))
        self.assertEqual(_braille_fill(100), ("⣿", "⣿"))
        self.assertNotEqual(_braille_fill(75)[0], "⣿")

    def test_braille_mask_uses_independent_official_dot_columns(self):
        left_top_only = ord(_braille_mask(4, 3)) - 0x2800
        right_top_only = ord(_braille_mask(3, 4)) - 0x2800
        both_top = ord(_braille_mask(4, 4)) - 0x2800
        self.assertTrue(left_top_only & 0x01)
        self.assertFalse(left_top_only & 0x08)
        self.assertFalse(right_top_only & 0x01)
        self.assertTrue(right_top_only & 0x08)
        self.assertTrue(both_top & 0x01)
        self.assertTrue(both_top & 0x08)
        self.assertEqual(_braille_mask(4, 4), "⣿")
        self.assertEqual(_braille_mask(0, 0), " ")

    def test_braille_mask_supports_odd_dot_counts(self):
        self.assertEqual((ord(_braille_mask(3, 2)) - 0x2800).bit_count(), 5)
        self.assertEqual((ord(_braille_mask(4, 3)) - 0x2800).bit_count(), 7)

    def test_braille_fill_tracks_rising_and_falling_subcolumns(self):
        self.assertEqual(_braille_fill(50, 62.5), ("⢀", "⣿"))
        self.assertEqual(_braille_fill(62.5, 50), ("⡀", "⣿"))

    def test_braille_subcolumn_times_derive_from_bucket_duration(self):
        self.assertEqual(_braille_subcolumn_times(1000, 15 * 60), (1225, 1675))

    def test_sleep_and_forecast_share_early_raster_helper(self):
        sleep = SleepInterval(stamp(20), stamp(20, 20), pre_percentage=50, post_percentage=75)
        with patch.object(graph_module, "_early_raster", wraps=graph_module._early_raster) as raster:
            chart_rows(self.current, [], CENTER, self.now, [sleep])
            self.assertGreater(raster.call_count, 0)
            raster.reset_mock()
            chart_rows(self.current, [], Estimate(3600, "test"), self.now)
            self.assertGreater(raster.call_count, 0)

    def test_early_raster_moves_clear_falling_and_rising_transitions(self):
        self.assertEqual(_early_raster([6.20, 5.79, 5.38, 4.98]), [6, 5, 5, 5])
        self.assertEqual(_early_raster([1.02, 1.42, 1.82, 2.21]), [1, 2, 2, 2])

    def test_early_raster_leaves_flat_transitions_normally_rounded(self):
        self.assertEqual(MIN_EARLY_SLOPE, 0.25)
        self.assertEqual(_early_raster([6.51, 6.49]), [7, 6])
        self.assertEqual(_early_raster([2.49, 2.51]), [2, 3])

    def test_early_raster_preserves_monotonicity_and_stays_within_one_dot(self):
        for heights in ([7.8, 7.2, 6.7, 6.1, 5.6, 5.0], [1.1, 1.6, 2.2, 2.8, 3.3, 3.9]):
            raster = _early_raster(heights)
            rounded = [round(height) for height in heights]
            direction = 1 if heights[-1] > heights[0] else -1
            self.assertTrue(all(direction * (right - left) >= 0
                                for left, right in zip(raster, raster[1:])))
            self.assertTrue(all(abs(actual - baseline) <= 1
                                for actual, baseline in zip(raster, rounded)))

    def test_early_raster_does_not_invent_changes_in_live_flat_sleep_shapes(self):
        self.assertEqual(_early_raster([6.935, 6.889]), [7, 7])
        self.assertEqual(_early_raster([2.724, 2.738, 2.752, 2.766, 2.779, 2.793]), [3] * 6)

    def test_sleep_residual_transfer_reveals_shallow_rising_and_falling_contours(self):
        rising = [2.724, 2.738, 2.752, 2.766, 2.779, 2.793]
        falling = list(reversed(rising))
        self.assertEqual(_sleep_residual_transfer(rising, _early_raster(rising)), [2, 3, 3, 3, 3, 4])
        self.assertEqual(_sleep_residual_transfer(falling, _early_raster(falling)), [4, 3, 3, 3, 3, 2])
        self.assertEqual(_braille_mask(2, 3), "⣴")
        self.assertEqual(_braille_mask(3, 4), "⣾")

    def test_sleep_residual_transfer_ignores_flat_short_nonmonotone_and_clear_slopes(self):
        cases = (
            [3.1, 3.1, 3.1, 3.1],
            [3.1, 3.2],
            [3.1, 3.2, 3.15, 3.25],
            [2.2, 2.6, 3.0, 3.4],
        )
        for heights in cases:
            with self.subTest(heights=heights):
                raster = _early_raster(heights)
                self.assertEqual(_sleep_residual_transfer(heights, raster), raster)

    def test_sleep_residual_transfer_preserves_dot_mass(self):
        heights = [2.724, 2.738, 2.752, 2.766, 2.779, 2.793]
        raster = _early_raster(heights)
        transferred = _sleep_residual_transfer(heights, raster)
        self.assertEqual(sum(transferred), sum(raster))

    def test_sleep_residual_transfer_is_sleep_only_and_keeps_percentages(self):
        forecast = Estimate(3600, "test")
        with patch.object(graph_module, "_sleep_residual_transfer",
                          wraps=graph_module._sleep_residual_transfer) as transfer:
            chart_rows(self.current, [], forecast, self.now)
        transfer.assert_not_called()

        sleep = SleepInterval(stamp(18), stamp(19), pre_percentage=34.05, post_percentage=34.91)
        with patch.object(graph_module, "_sleep_residual_transfer",
                          side_effect=lambda heights, raster: list(raster)):
            baseline_top, baseline_bottom, baseline_percentages = _chart_rows_and_percentages(
                self.current, [], CENTER, self.now, [sleep]
            )
        top, bottom, percentages = _chart_rows_and_percentages(
            self.current, [], CENTER, self.now, [sleep]
        )
        self.assertEqual(percentages, baseline_percentages)
        self.assertEqual([_battery_color(value) if value is not None else None for value in percentages],
                         [_battery_color(value) if value is not None else None
                          for value in baseline_percentages])
        self.assertNotEqual((top, bottom), (baseline_top, baseline_bottom))

    def test_unknown_interval_uses_the_basic_raster_not_the_locked_sleep_contour(self):
        from battery_status_tui.graph import UNKNOWN_GRAY, UNKNOWN_TRAJECTORY, _style_battery
        interval = SleepInterval(stamp(18), stamp(20), "nodata", "simulate", None,
                                 pre_percentage=80.0, post_percentage=30.0)
        with patch.object(graph_module, "_sleep_residual_transfer",
                          wraps=graph_module._sleep_residual_transfer) as residual, \
             patch.object(graph_module, "_smooth_sleep_edges",
                          wraps=graph_module._smooth_sleep_edges) as smooth, \
             patch.object(graph_module, "_early_raster",
                          wraps=graph_module._early_raster) as raster, \
             patch.object(graph_module, "_braille_fill_levels",
                          wraps=graph_module._braille_fill_levels) as fill:
            top, bottom, pct = _chart_rows_and_percentages(
                self.current, [], CENTER, self.now, (), (interval,))
        residual.assert_not_called()          # locked sleep-only contour helpers
        smooth.assert_not_called()
        self.assertGreater(raster.call_count, 0)   # but the shared basic raster is reused
        self.assertGreater(fill.call_count, 0)
        gray_cols = [c for c in range(GRAPH_WIDTH) if pct[c] is UNKNOWN_TRAJECTORY]
        self.assertTrue(gray_cols)
        for c in gray_cols:
            self.assertTrue(all(0x2800 <= ord(g) <= 0x28ff for g in (top[c] + bottom[c]).strip()))
        gray_run = _style_battery("".join(bottom[c] for c in gray_cols),
                                  [UNKNOWN_TRAJECTORY] * len(gray_cols))
        self.assertIn(UNKNOWN_GRAY, gray_run)
        self.assertNotIn("\x1b[38;2;", gray_run)   # never the SoC gradient

    def test_discharge_forecast_ends_at_the_predicted_empty_time(self):
        estimate = Estimate(3 * 3600, "test")  # 3h / 20min == 9 forecast columns
        marker = viewport(self.current, estimate)
        top, bottom, percentages = _chart_rows_and_percentages(
            self.current, self.history, estimate, self.now
        )
        self.assertEqual(marker, GRAPH_WIDTH - 1 - 9)
        forecast = percentages[marker + 1:]
        self.assertEqual(len(forecast), 9)
        self.assertTrue(all(value is not None for value in forecast))
        self.assertTrue(all(left >= right for left, right in zip(forecast, forecast[1:])))
        self.assertEqual(forecast[-1], 0)
        self.assertEqual((top[-1], bottom[-1]), (" ", "⣀"))  # empty still shows a bottom dot
        self.assertIn(f"{_battery_color(0)}⣀{graph_module.RESET}",
                      _style_battery(bottom, percentages))

    def test_near_zero_discharge_forecast_keeps_bottom_dots(self):
        current = Measurement(self.now, 1, "discharging", False)
        estimate = Estimate(30 * 60, "test")  # 30min -> 2 forecast columns
        marker = viewport(current, estimate)
        top, bottom, percentages = _chart_rows_and_percentages(current, [], estimate, self.now)
        self.assertEqual(marker, GRAPH_WIDTH - 1 - 2)
        self.assertTrue(all(value is not None for value in percentages[marker + 1:]))
        self.assertEqual(top[marker + 1:], " " * (GRAPH_WIDTH - marker - 1))
        self.assertEqual(bottom[marker + 1:], "⣀" * (GRAPH_WIDTH - marker - 1))

    def test_exact_zero_discharge_forecast_keeps_bottom_dots(self):
        current = Measurement(self.now, 0, "discharging", False)
        estimate = Estimate(1, "test")  # tiny -> a single forecast column
        marker = viewport(current, estimate)
        top, bottom, percentages = _chart_rows_and_percentages(current, [], estimate, self.now)
        self.assertEqual(marker, GRAPH_WIDTH - 2)
        self.assertEqual(percentages[marker + 1:], [0])
        self.assertEqual((top[marker + 1:], bottom[marker + 1:]), (" ", "⣀"))

    def test_long_discharge_forecast_is_capped_at_the_graph_midpoint(self):
        current = Measurement(self.now, 50, "discharging", False)
        history = [sample(t, 50) for t in range(self.now - MAX_SPAN_SECONDS, self.now, 300)]
        estimate = Estimate(12 * 3600, "test")  # far longer than half the graph
        marker = viewport(current, estimate)
        top, bottom, percentages = _chart_rows_and_percentages(current, history, estimate, self.now)
        # NOW never crosses the midpoint: at least half the width stays history
        self.assertEqual(marker, NOW_INDEX)
        forecast = percentages[marker + 1:]
        self.assertEqual(len(forecast), GRAPH_WIDTH - 1 - NOW_INDEX)
        self.assertTrue(all(value is not None for value in forecast))
        self.assertTrue(all(left >= right for left, right in zip(forecast, forecast[1:])))
        # the drawn forecast is clipped mid-slope, NOT rescaled to reach 0 by the edge
        self.assertGreater(forecast[-1], 20)
        self.assertTrue((top[:marker] + bottom[:marker]).strip())  # full history half visible

    def test_now_never_moves_left_of_the_graph_midpoint(self):
        long_etas = (Estimate(8 * 3600 + 40 * 60, "t"), Estimate(12 * 3600, "t"),
                     Estimate(23 * 3600, "t"))
        for eta in long_etas:
            with self.subTest(eta=eta.seconds, kind="discharging"):
                discharging = Measurement(self.now, 60, "discharging", False, power_w=6.5)
                self.assertEqual(now_column(discharging, eta), NOW_INDEX)
            with self.subTest(eta=eta.seconds, kind="charging"):
                charging = Measurement(self.now, 20, "charging", True, power_w=30.0)
                self.assertEqual(now_column(charging, eta), NOW_INDEX)
        # the cap is exactly half the width, expressed from the graph constants
        self.assertEqual(NOW_INDEX, TIME_COLUMNS // 2)
        self.assertEqual(_forecast_span_columns(
            Measurement(self.now, 60, "discharging", False), Estimate(12 * 3600, "t")),
            GRAPH_WIDTH - 1 - NOW_INDEX)

    def test_short_forecast_still_slides_now_toward_the_right_edge(self):
        current = Measurement(self.now, 70, "discharging", False)
        near = now_column(current, Estimate(40 * 60, "t"))    # 2 columns
        mid = now_column(current, Estimate(3 * 3600, "t"))    # 9 columns
        self.assertGreater(near, mid)
        self.assertGreater(mid, NOW_INDEX)                    # still right of the midpoint
        self.assertEqual(near, GRAPH_WIDTH - 1 - 2)
        self.assertEqual(mid, GRAPH_WIDTH - 1 - 9)

    def test_no_forecast_keeps_now_at_the_far_right_column(self):
        for current in (Measurement(self.now, 100, "full", True),
                        Measurement(self.now, 55, "discharging", False)):  # discharging, no ETA
            self.assertEqual(now_column(current, None), GRAPH_WIDTH - 1)

    def test_title_arrow_stays_above_the_capped_now_column(self):
        current = Measurement(self.now, 60, "discharging", False, power_w=6.5)
        estimate = Estimate(8 * 3600 + 40 * 60, "t")
        marker = now_column(current, estimate)
        self.assertEqual(marker, NOW_INDEX)
        top, bottom = chart_rows(current, self.history, estimate, self.now)
        self.assertEqual((top[marker], bottom[marker]), ("│", "│"))
        rendered = plain(render_dashboard(current, self.history, None, estimate, self.now))
        lines = rendered.splitlines()
        self.assertEqual(lines[0].index("↓"), GRAPH_OFFSET + marker)
        self.assertEqual(lines[1][GRAPH_OFFSET + marker], "│")

    def test_midpoint_clip_does_not_change_the_textual_eta_or_end_time(self):
        current = Measurement(self.now, 60, "discharging", False, power_w=6.5)
        estimate = Estimate(8 * 3600 + 40 * 60, "t")  # ceil = 26 columns, capped to 18
        marker = now_column(current, estimate)
        self.assertEqual(marker, NOW_INDEX)
        rendered = plain(render_dashboard(current, self.history, None, estimate, self.now))
        eta_line = rendered.splitlines()[1]
        end = dt.datetime.fromtimestamp(self.now + estimate.seconds).astimezone().strftime("%H:%M")
        self.assertIn(f"8h40 ~{end}", eta_line)  # full ETA, unclipped
        # the graph forecast is drawn but stops mid-slope; it is NOT rescaled to
        # reach empty by the right edge (that would land near 0)
        _, _, percentages = _chart_rows_and_percentages(current, self.history, estimate, self.now)
        self.assertGreater(percentages[-1], 10)
        self.assertLess(percentages[-1], current.percentage)

    def test_forecast_endpoint_tracks_the_eta_not_a_fixed_window(self):
        for eta_seconds, span in ((1800, 2), (7200, 6), (5 * 3600, 15)):
            with self.subTest(eta_seconds=eta_seconds):
                estimate = Estimate(eta_seconds, "test")
                marker = viewport(self.current, estimate)
                self.assertEqual(marker, GRAPH_WIDTH - 1 - span)
                top, bottom = chart_rows(self.current, self.history, estimate, self.now)
                self.assertTrue((top[marker + 1:] + bottom[marker + 1:]).strip())
                self.assertEqual(GRAPH_WIDTH - 1 - marker, span)

    def test_charging_forecast_reaches_full_at_the_predicted_time(self):
        current = Measurement(self.now, 75, "charging", True)
        estimate = Estimate(3600, "test")  # 1h -> 3 forecast columns
        marker = viewport(current, estimate)
        top, bottom, percentages = _chart_rows_and_percentages(current, [], estimate, self.now)
        self.assertEqual(marker, GRAPH_WIDTH - 1 - 3)
        forecast = percentages[marker + 1:]
        self.assertEqual(len(forecast), 3)
        self.assertLess(forecast[0], 100)
        self.assertEqual(forecast[-1], 100)
        self.assertTrue(all(left <= right for left, right in zip(forecast, forecast[1:])))
        styled = _style_battery(top, percentages) + _style_battery(bottom, percentages)
        self.assertIn(_battery_color(100), styled)

    def test_full_battery_on_ac_leaves_the_whole_width_for_history(self):
        current = Measurement(self.now, 100, "full", True)
        marker = viewport(current, None)
        top, bottom, percentages = _chart_rows_and_percentages(current, self.history, None, self.now)
        self.assertEqual(marker, GRAPH_WIDTH - 1)
        self.assertEqual((top[marker], bottom[marker]), ("│", "│"))
        self.assertTrue(all(value is None for value in percentages[marker:]))
        self.assertTrue((top[:marker] + bottom[:marker]).strip())

    def test_ac_state_rebuilds_forecast_for_the_current_direction(self):
        estimate = Estimate(3600, "test")
        charging = Measurement(self.now, 75, "charging", True)
        _, _, charging_percentages = _chart_rows_and_percentages(charging, [], estimate, self.now)
        self.assertEqual(charging_percentages[-1], 100)

        discharging = Measurement(self.now, 75, "discharging", False)
        marker = viewport(discharging, estimate)
        _, _, discharge_percentages = _chart_rows_and_percentages(discharging, [], estimate, self.now)
        self.assertIsNone(discharge_percentages[marker])
        self.assertEqual(discharge_percentages[-1], 0)
        forecast = discharge_percentages[marker + 1:]
        self.assertTrue(all(left >= right for left, right in zip(forecast, forecast[1:])))

        _, _, recharging_percentages = _chart_rows_and_percentages(charging, [], estimate, self.now)
        self.assertEqual(recharging_percentages[-1], 100)

    def test_battery_gradient_anchors_and_clamping(self):
        expected = {
            0: "\x1b[38;2;85;10;20m",
            25: "\x1b[38;2;155;35;30m",
            50: "\x1b[38;2;175;110;25m",
            75: "\x1b[38;2;90;130;40m",
            100: "\x1b[38;2;20;105;50m",
        }
        for percentage, color in expected.items():
            self.assertEqual(_battery_color(percentage), color)
        self.assertEqual(_battery_color(-1), expected[0])
        self.assertEqual(_battery_color(101), expected[100])

    def test_battery_gradient_interpolates_neighboring_percentages(self):
        self.assertEqual(_battery_color(10), "\x1b[38;2;113;20;24m")
        self.assertEqual(_battery_color(30), "\x1b[38;2;159;50;29m")
        self.assertNotEqual(_battery_color(30), _battery_color(31))

    def test_history_columns_use_their_aggregated_percentages(self):
        history = [
            sample(stamp(19, 40), 20),
            sample(stamp(20), 20), sample(stamp(20, 10), 40),
            sample(stamp(20, 20), 60), sample(stamp(20, 40), 80),
        ]
        top, bottom, percentages = _chart_rows_and_percentages(self.current, history, CENTER, self.now)
        expected = {
            project_column(stamp(19, 40), self.now): 20,
            project_column(stamp(20), self.now): 30,
            project_column(stamp(20, 20), self.now): 60,
            project_column(stamp(20, 40), self.now): 80,
        }
        styled = _style_battery(top, percentages) + _style_battery(bottom, percentages)
        for column, percentage in expected.items():
            self.assertEqual(percentages[column], percentage)
            self.assertIn(_battery_color(percentage), styled)

    def test_observed_zero_is_visible_and_distinct_from_unknown(self):
        zero_column = project_column(stamp(20, 40), self.now)
        unknown_column = project_column(stamp(20, 20), self.now)
        top, bottom, percentages = _chart_rows_and_percentages(
            self.current, [sample(stamp(20, 40), 0)], CENTER, self.now
        )
        self.assertEqual((top[zero_column], bottom[zero_column]), (" ", "▁"))
        self.assertEqual(percentages[zero_column], 0)
        self.assertEqual((top[unknown_column], bottom[unknown_column]), (" ", " "))
        self.assertIsNone(percentages[unknown_column])
        self.assertEqual(_battery_color(0), "\x1b[38;2;85;10;20m")
        self.assertIn(f"{_battery_color(0)}▁{graph_module.RESET}",
                      _style_battery(bottom, percentages))

    def test_known_low_solid_history_is_visible_with_actual_soc_color(self):
        for percentage, expected in ((0, "▁"), (1, "▁"), (2, "▁"), (3, "▁"),
                                     (5, "▁"), (50, "█")):
            with self.subTest(percentage=percentage):
                self.assertEqual(_fill_chars(percentage), (" ", expected))
                column = project_column(stamp(20, 40), self.now)
                top, bottom, percentages = _chart_rows_and_percentages(
                    self.current, [sample(stamp(20, 40), percentage)], CENTER, self.now
                )
                self.assertEqual((top[column], bottom[column]), (" ", expected))
                self.assertEqual(percentages[column], percentage)
                self.assertIn(
                    f"{_battery_color(percentage)}{expected}{graph_module.RESET}",
                    _style_battery(bottom, percentages),
                )

    def test_charging_forecast_uses_changing_gradient_colors(self):
        current = Measurement(self.now, 60, "charging", True)
        top, bottom, percentages = _chart_rows_and_percentages(
            current, [], Estimate(6 * 3600, "test"), self.now
        )
        forecast = percentages[NOW_INDEX + 1:]
        self.assertTrue(any(value is not None and 50 <= value < 75 for value in forecast))
        self.assertTrue(any(value is not None and value >= 75 for value in forecast))
        styled = _style_battery(top, percentages) + _style_battery(bottom, percentages)
        self.assertIn(_battery_color(next(value for value in forecast if value is not None)), styled)
        self.assertIn(_battery_color(next(value for value in reversed(forecast) if value is not None)), styled)

    def test_discharging_forecast_uses_changing_gradient_colors(self):
        current = Measurement(self.now, 60, "discharging", False)
        estimate = Estimate(6 * 3600, "test")
        top, bottom, percentages = _chart_rows_and_percentages(
            current, [], estimate, self.now
        )
        styled = _style_battery(top, percentages) + _style_battery(bottom, percentages)
        forecast = [value for value in percentages[NOW_INDEX + 1:] if value is not None]
        visible_forecast = [value for value in forecast if round(value / 100 * 8) > 0]
        self.assertIn(_battery_color(forecast[0]), styled)
        self.assertIn(_battery_color(forecast[len(forecast) // 2]), styled)
        self.assertIn(_battery_color(visible_forecast[-1]), styled)
        for column, percentage in enumerate(percentages[NOW_INDEX + 1:], NOW_INDEX + 1):
            if percentage is not None:
                elapsed = column_timestamp(column, self.now) + COLUMN_SECONDS / 2 - self.now
                self.assertAlmostEqual(
                    percentage, current.percentage * (1 - min(1, elapsed / estimate.seconds))
                )

    def test_battery_colors_preserve_width_now_axis_and_sleep(self):
        estimate = Estimate(7200, "test")
        marker = viewport(self.current, estimate)
        sleep = SleepInterval(stamp(18), stamp(19), pre_percentage=55, post_percentage=53)
        history = [item for item in self.history if not (sleep.started_at <= item.timestamp < sleep.ended_at)]
        rendered = render_dashboard(
            self.current, history, None, estimate, self.now, [sleep]
        )
        lines = plain(rendered).splitlines()
        self.assertEqual(len(lines[1][GRAPH_OFFSET:GRAPH_OFFSET + GRAPH_WIDTH]), GRAPH_WIDTH)
        self.assertEqual(lines[1][GRAPH_OFFSET + marker], "│")
        self.assertEqual(lines[2][GRAPH_OFFSET + marker], "│")
        self.assertEqual(lines[3][GRAPH_OFFSET:], axis_rows(self.now, marker)[0])
        self.assertNotIn("z", rendered)

    def test_pre_sleep_sleep_and_post_resume_segments_all_remain_visible(self):
        sleep = SleepInterval(stamp(18), stamp(20), pre_percentage=55, post_percentage=51)
        history = [sample(stamp(17, 40), 55), sample(stamp(20), 51), sample(stamp(20, 40), 50)]
        top, bottom, percentages = _chart_rows_and_percentages(self.current, history, CENTER, self.now, [sleep])
        self.assertNotEqual((top[8], bottom[8]), (" ", " "))  # 17:40 history
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28ff for character in bottom[9:15]))
        self.assertGreater(percentages[9], percentages[14])
        self.assertNotEqual((top[15], bottom[15]), (" ", " "))  # 20:00 resumed history
        self.assertNotEqual((top[17], bottom[17]), (" ", " "))

    def test_short_sleep_below_fifty_uses_one_braille_column(self):
        sleep = SleepInterval(stamp(20, 5), stamp(20, 10) + 1, pre_percentage=40, post_percentage=39)
        top, bottom, percentages = _chart_rows_and_percentages(self.current, [], CENTER, self.now, [sleep])
        sleep_columns = [index for index, value in enumerate(percentages[:NOW_INDEX]) if value is not None]
        self.assertEqual(len(sleep_columns), 1)
        column = sleep_columns[0]
        self.assertTrue(all(character == " " or 0x2800 <= ord(character) <= 0x28ff
                            for character in (top[column], bottom[column])))
        self.assertNotIn("z", top + bottom)

    def test_sleep_bucket_threshold_uses_overlap_fraction(self):
        bucket_start = stamp(20)
        duration = COLUMN_SECONDS
        cases = (
            (SleepInterval(bucket_start - duration, bucket_start), 0.00, False),
            (SleepInterval(bucket_start, bucket_start + round(duration * 0.24)), 0.24, False),
            (SleepInterval(bucket_start, bucket_start + duration // 4), 0.25, False),
            (SleepInterval(bucket_start, bucket_start + duration // 4 + 1), None, True),
            (SleepInterval(bucket_start, bucket_start + duration // 2), 0.50, True),
            (SleepInterval(bucket_start, bucket_start + duration), 1.00, True),
        )
        for interval, expected_fraction, braille in cases:
            with self.subTest(expected_fraction=expected_fraction, braille=braille):
                fraction = _sleep_fraction(interval, bucket_start, duration)
                if expected_fraction is not None:
                    self.assertAlmostEqual(fraction, expected_fraction)
                self.assertEqual(fraction > 0.25, braille)

    def test_sleep_fraction_is_dynamic_for_fifteen_minute_bucket(self):
        duration = 15 * 60
        bucket_start = stamp(20)
        exact = SleepInterval(bucket_start, bucket_start + duration // 4)
        above = SleepInterval(bucket_start, bucket_start + duration // 4 + 1)
        self.assertEqual(_sleep_fraction(exact, bucket_start, duration), 0.25)
        self.assertFalse(_sleep_fraction(exact, bucket_start, duration) > 0.25)
        self.assertTrue(_sleep_fraction(above, bucket_start, duration) > 0.25)

    def test_known_partial_sleep_intervals_select_expected_columns(self):
        short = SleepInterval(stamp(21, 19) + 37, stamp(21, 36) + 55)
        long = SleepInterval(stamp(23, 42) + 23, stamp(23) + 3600 + 39 * 60 + 43)
        self.assertEqual(len(_sleep_columns(short, stamp(23) + 3 * 3600)), 1)
        self.assertEqual(len(_sleep_columns(long, stamp(23) + 3 * 3600)), 3)

    def test_pre_sleep_sample_in_same_bucket_does_not_block_braille(self):
        sleep = SleepInterval(stamp(20, 5), stamp(20, 15), pre_percentage=60, post_percentage=58)
        top, bottom = chart_rows(self.current, [sample(stamp(20, 4), 60)], CENTER, self.now, [sleep])
        column = project_column(stamp(20), self.now)
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28ff for character in
                            (top[column], bottom[column]) if character != " "))

    def test_post_resume_sample_in_same_bucket_does_not_block_braille(self):
        sleep = SleepInterval(stamp(20, 5), stamp(20, 15), pre_percentage=60, post_percentage=58)
        top, bottom = chart_rows(self.current, [sample(stamp(20, 16), 58)], CENTER, self.now, [sleep])
        column = project_column(stamp(20), self.now)
        self.assertTrue(all(0x2800 <= ord(character) <= 0x28ff for character in
                            (top[column], bottom[column]) if character != " "))

    def test_partial_sleep_boundary_does_not_overwrite_adjacent_history(self):
        history = [sample(stamp(17, 55), 56), sample(stamp(20, 5), 51)]
        sleep = SleepInterval(stamp(17, 57), stamp(20, 3), pre_percentage=56, post_percentage=51)
        top, bottom = chart_rows(self.current, history, CENTER, self.now, [sleep])
        before = project_column(stamp(17, 55), self.now)
        after = project_column(stamp(20, 5), self.now)
        self.assertNotEqual((top[before], bottom[before]), (" ", " "))
        self.assertNotEqual((top[after], bottom[after]), (" ", " "))
        self.assertTrue(all(character in " ▁▂▃▄▅▆▇█" for character in
                            (top[before], bottom[before], top[after], bottom[after])))

    def test_multi_hour_sleep_interpolates_height_and_gradient(self):
        sleep = SleepInterval(stamp(18), stamp(21), pre_percentage=80, post_percentage=40)
        top, bottom, percentages = _chart_rows_and_percentages(self.current, [], CENTER, self.now, [sleep])
        columns = list(_sleep_columns(sleep, self.now))
        values = [percentages[column] for column in columns]
        self.assertEqual(len(columns), 9)
        self.assertTrue(all(left > right for left, right in zip(values, values[1:])))
        self.assertTrue(any(top[column] == "⣿" or bottom[column] == "⣿" for column in columns))
        styled = _style_battery(top, percentages) + _style_battery(bottom, percentages)
        self.assertIn(_battery_color(values[0]), styled)
        self.assertIn(_battery_color(values[-1]), styled)

    def test_rising_sleep_interpolation_follows_boundary_measurements(self):
        sleep = SleepInterval(stamp(18), stamp(20), pre_percentage=30, post_percentage=70)
        _, _, percentages = _chart_rows_and_percentages(self.current, [], CENTER, self.now, [sleep])
        values = [percentages[column] for column in _sleep_columns(sleep, self.now)]
        self.assertTrue(all(left < right for left, right in zip(values, values[1:])))

    def test_sleep_without_both_boundaries_remains_a_gap(self):
        sleep = SleepInterval(stamp(18), stamp(20), pre_percentage=55, post_percentage=None)
        top, bottom = chart_rows(self.current, [], CENTER, self.now, [sleep])
        for column in _sleep_columns(sleep, self.now):
            self.assertEqual((top[column], bottom[column]), (" ", " "))

    def test_measured_sample_inside_sleep_is_not_overwritten(self):
        sleep = SleepInterval(stamp(18), stamp(20), pre_percentage=55, post_percentage=51)
        measured = sample(stamp(19), 80)
        top, bottom = chart_rows(self.current, [measured], CENTER, self.now, [sleep])
        column = project_column(measured.timestamp, self.now)
        self.assertEqual((top[column], bottom[column]), _fill_chars(80))

    def test_mixed_sleep_end_uses_active_data_for_right_subcolumn(self):
        bucket_start = stamp(20)
        sleep = SleepInterval(stamp(18), bucket_start + 6 * 60 + 13,
                              pre_percentage=67, post_percentage=67)
        history = [sample(bucket_start + 6 * 60 + 57, 67), sample(bucket_start + 15 * 60, 61),
                   sample(bucket_start + 19 * 60, 59)]
        left_time, right_time = _braille_subcolumn_times(bucket_start, COLUMN_SECONDS)
        self.assertTrue(sleep.started_at <= left_time < sleep.ended_at)
        self.assertFalse(sleep.started_at <= right_time < sleep.ended_at)
        self.assertEqual(_active_percentage_at(right_time, history, sleep, bucket_start,
                                               COLUMN_SECONDS), 61)

        visible_history = [sample(bucket_start + 7 * 60, 67), sample(bucket_start + 15 * 60, 61),
                           sample(bucket_start + 25 * 60, 52), sample(bucket_start + 26 * 60, 53)]
        top, bottom = chart_rows(self.current, visible_history, CENTER, self.now, [sleep])
        column = project_column(bucket_start, self.now)
        self.assertEqual((top[column], bottom[column]), _braille_fill_levels(5, 4))

    def test_mixed_sleep_start_uses_active_data_for_left_subcolumn(self):
        bucket_start = stamp(20)
        sleep = SleepInterval(bucket_start + 13 * 60, stamp(21),
                              pre_percentage=67, post_percentage=67)
        history = [sample(bucket_start + minute * 60, percentage)
                   for minute, percentage in ((1, 72), (5, 70), (10, 68))]
        left_time, right_time = _braille_subcolumn_times(bucket_start, COLUMN_SECONDS)
        self.assertFalse(sleep.started_at <= left_time < sleep.ended_at)
        self.assertTrue(sleep.started_at <= right_time < sleep.ended_at)
        self.assertEqual(_active_percentage_at(left_time, history, sleep, bucket_start,
                                               COLUMN_SECONDS), 70)

    def test_fully_sleeping_flat_bucket_keeps_sleep_geometry(self):
        sleep = SleepInterval(stamp(20), stamp(20, 20), pre_percentage=67, post_percentage=67)
        top, bottom = chart_rows(self.current, [], CENTER, self.now, [sleep])
        column = project_column(stamp(20), self.now)
        self.assertEqual((top[column], bottom[column]), _braille_fill(67, 67))

    def test_low_soc_sleep_subcolumns_remain_visible(self):
        for percentage in (1, 5, 10):
            with self.subTest(percentage=percentage):
                sleep = SleepInterval(stamp(20), stamp(20, 20),
                                      pre_percentage=percentage, post_percentage=percentage)
                top, bottom = chart_rows(self.current, [], CENTER, self.now, [sleep])
                column = project_column(stamp(20), self.now)
                self.assertNotEqual((top[column], bottom[column]), (" ", " "))

    def test_valid_zero_soc_sleep_keeps_one_bottom_dot_per_subcolumn(self):
        sleep = SleepInterval(stamp(20), stamp(20, 20), pre_percentage=0, post_percentage=0)
        top, bottom = chart_rows(self.current, [], CENTER, self.now, [sleep])
        column = project_column(stamp(20), self.now)
        self.assertEqual((top[column], bottom[column]), (" ", "⣀"))

    def test_sparse_active_history_after_sleep_does_not_bridge_empty_bucket(self):
        sleep = SleepInterval(stamp(19, 40), stamp(20), pre_percentage=10, post_percentage=9)
        observed = sample(stamp(20, 40), 8)
        top, bottom = chart_rows(self.current, [observed], CENTER, self.now, [sleep])
        gap = project_column(stamp(20, 20), self.now)
        measured = project_column(observed.timestamp, self.now)
        self.assertEqual((top[gap], bottom[gap]), (" ", " "))
        self.assertEqual((top[measured], bottom[measured]), _fill_chars(8))

    def test_active_subcolumn_interpolation_uses_configured_bucket_duration(self):
        duration = 15 * 60
        bucket_start = stamp(20)
        sleep = SleepInterval(bucket_start, bucket_start + 4 * 60, pre_percentage=67,
                              post_percentage=67)
        history = [sample(bucket_start + 6 * 60, 60), sample(bucket_start + 12 * 60, 54)]
        timestamp = bucket_start + duration * 3 / 4
        self.assertEqual(_active_percentage_at(timestamp, history, sleep, bucket_start, duration), 54.75)

    def test_sleep_edge_smoothing_handles_falling_rising_and_equal_levels(self):
        column = 10
        for adjacent, expected in ((50, 4), (75, 6), (62.5, 5)):
            with self.subTest(adjacent=adjacent):
                top, bottom = [" "] * GRAPH_WIDTH, [" "] * GRAPH_WIDTH
                percentages = [None] * GRAPH_WIDTH
                top[column + 1], bottom[column + 1] = _fill_chars(adjacent)
                percentages[column + 1] = adjacent
                raster = _smooth_sleep_edges([column], [5, 5], [5, 5], top, bottom,
                                             percentages)
                self.assertEqual(raster[1], expected)

    def test_sleep_edge_smoothing_is_symmetric_at_the_leading_edge(self):
        column = 10
        for adjacent, expected in ((50, 4), (75, 6), (62.5, 5)):
            with self.subTest(adjacent=adjacent):
                top, bottom = [" "] * GRAPH_WIDTH, [" "] * GRAPH_WIDTH
                percentages = [None] * GRAPH_WIDTH
                top[column - 1], bottom[column - 1] = _fill_chars(adjacent)
                percentages[column - 1] = adjacent
                raster = _smooth_sleep_edges([column], [5, 5], [5, 5], top, bottom,
                                             percentages)
                self.assertEqual(raster[0], expected)

    def test_leading_sleep_edge_does_not_cross_a_half_level_solid_neighbor(self):
        column = 10
        for adjacent in (56.25, 68.75):
            with self.subTest(adjacent=adjacent):
                top, bottom = [" "] * GRAPH_WIDTH, [" "] * GRAPH_WIDTH
                percentages = [None] * GRAPH_WIDTH
                top[column - 1], bottom[column - 1] = _fill_chars(adjacent)
                percentages[column - 1] = adjacent
                raster = _smooth_sleep_edges([column], [5, 5], [5, 5], top, bottom,
                                             percentages)
                self.assertEqual(raster[0], 5)

    def test_sleep_edge_smoothing_replaces_residual_at_a_solid_boundary(self):
        column = 10
        top, bottom = [" "] * GRAPH_WIDTH, [" "] * GRAPH_WIDTH
        percentages = [None] * GRAPH_WIDTH
        top[column + 1], bottom[column + 1] = _fill_chars(50)
        percentages[column + 1] = 50
        raster = _smooth_sleep_edges([column], [4, 6], [5, 5], top, bottom, percentages)
        self.assertEqual(raster, [4, 4])

    def test_unmeasured_non_sleep_gap_stays_empty(self):
        history = [sample(stamp(16)), sample(stamp(18))]
        top, bottom = chart_rows(self.current, history, CENTER, self.now)
        middle = project_column(stamp(17), self.now)
        self.assertEqual((top[middle], bottom[middle]), (" ", " "))

    # --- the locked sleep-Braille renderer must survive the dynamic NOW viewport ---

    def _sleep_scene(self):
        """A low-SoC hibernate framed by measured solid history, then a rise."""
        now = stamp(21)
        current = Measurement(now, 45, "charging", True, power_w=30.0)
        sleep = SleepInterval(stamp(17), stamp(20), "hibernate", "journal", "b",
                              pre_percentage=3.0, post_percentage=4.0)
        history = [sample(stamp(16, 40), 5.0), sample(stamp(16, 55), 3.0),
                   Measurement(stamp(20, 5), 4.0, "charging", True),
                   Measurement(stamp(20, 25), 18.0, "charging", True),
                   Measurement(stamp(20, 45), 33.0, "charging", True)]
        return now, current, sleep, history

    def _row_cells(self, current, history, estimate, sleep, now):
        marker = now_column(current, estimate)
        top, bottom, pct = _chart_rows_and_percentages(current, history, estimate, now, [sleep])
        cells = {}
        for column in range(marker):
            glyphs = top[column] + bottom[column]
            if not glyphs.strip():
                continue
            kind = ("braille" if all(0x2800 <= ord(c) <= 0x28ff for c in glyphs.strip())
                    else "solid")
            colour = None if pct[column] is None else _battery_color(pct[column])
            cells[column_timestamp(column, now, marker)] = (
                kind, top[column], bottom[column],
                None if pct[column] is None else round(pct[column], 4), colour,
            )
        return marker, cells

    def test_sleep_braille_is_a_pure_viewport_translation_across_dynamic_now(self):
        now, current, sleep, history = self._sleep_scene()
        # NOW at three positions: right edge, a short ETA, a medium ETA.
        scenes = {est: self._row_cells(current, history, est, sleep, now)
                  for est in (None, Estimate(40 * 60, "t"), Estimate(3 * 3600, "t"))}
        markers = {est: marker for est, (marker, _) in scenes.items()}
        self.assertEqual(len(set(markers.values())), 3)  # genuinely different NOW columns

        # Every bucket visible under more than one NOW position renders identically
        # (same glyphs, same interpolated SoC, same colour) — the viewport only slides.
        by_bucket = {}
        for _est, (_marker, cells) in scenes.items():
            for bucket, value in cells.items():
                by_bucket.setdefault(bucket, set()).add(value)
        shared = {b: v for b, v in by_bucket.items() if
                  sum(b in cells for _m, cells in scenes.values()) > 1}
        self.assertTrue(shared)
        for bucket, values in shared.items():
            self.assertEqual(len(values), 1, f"bucket {bucket} rendered differently per NOW")

        # ...and the sleep span really is braille framed by solid measured history.
        centre = scenes[Estimate(3 * 3600, "t")][1]
        kinds = [v[0] for _b, v in sorted(centre.items())]
        self.assertEqual(kinds[0], "solid")                 # 16:55 pre-sleep measurement
        self.assertEqual(kinds[-1], "solid")                # 20:45 post-resume measurement
        self.assertIn("braille", kinds)
        first_b, last_b = kinds.index("braille"), len(kinds) - 1 - kinds[::-1].index("braille")
        self.assertTrue(all(k == "braille" for k in kinds[first_b:last_b + 1]))
        self.assertEqual(kinds[first_b - 1], "solid")       # solid -> braille, no blank seam
        self.assertEqual(kinds[last_b + 1], "solid")        # braille -> solid, no blank seam

    def test_very_low_soc_sleep_is_dark_red_braille_at_every_now_position(self):
        now, current, sleep, history = self._sleep_scene()
        for estimate in (None, Estimate(50 * 60, "t"), Estimate(5 * 3600, "t")):
            with self.subTest(estimate=estimate):
                marker, cells = self._row_cells(current, history, estimate, sleep, now)
                sleep_cells = [v for b, v in cells.items()
                               if stamp(17) <= b < stamp(20) and v[0] == "braille"]
                self.assertGreaterEqual(len(sleep_cells), 3)
                for kind, top_c, bottom_c, value, colour in sleep_cells:
                    self.assertLessEqual(value, 4.0)              # interpolated 3%..4%
                    self.assertGreaterEqual(value, 3.0)
                    self.assertNotEqual(bottom_c, " ")            # keeps its bottom dot
                    red, green, blue = (int(n) for n in
                                        colour[len("\x1b[38;2;"):-1].split(";"))
                    self.assertLess(red, 110)
                    self.assertLess(green, 35)
                    self.assertGreater(red, green)                # unmistakably deep red
                    self.assertEqual(colour, _battery_color(value))  # unchanged gradient

    def test_rising_falling_and_shallow_sleep_contours_survive_dynamic_now(self):
        now = stamp(21)
        current = Measurement(now, 60, "charging", True, power_w=30.0)
        rising = SleepInterval(stamp(17), stamp(20), "suspend", "journal", "b",
                               pre_percentage=10.0, post_percentage=60.0)
        falling = SleepInterval(stamp(17), stamp(20), "suspend", "journal", "b",
                                pre_percentage=60.0, post_percentage=10.0)
        shallow = SleepInterval(stamp(18), stamp(20), "suspend", "journal", "b",
                                pre_percentage=34.05, post_percentage=34.91)
        for label, sleep, rise in (("rising", rising, True), ("falling", falling, False),
                                   ("shallow", shallow, True)):
            wide_marker, wide = self._row_cells(current, [], None, sleep, now)
            with patch.object(graph_module, "_sleep_residual_transfer",
                              wraps=graph_module._sleep_residual_transfer) as residual:
                narrow_marker, narrow = self._row_cells(
                    current, [], Estimate(3 * 3600, "t"), sleep, now
                )
            with self.subTest(label=label):
                self.assertNotEqual(wide_marker, narrow_marker)
                self.assertEqual(residual.call_count, 1)  # residual transfer still runs, once
                overlap = set(wide) & set(narrow)
                self.assertGreaterEqual(len(overlap), 4)
                for bucket in overlap:
                    self.assertEqual(wide[bucket], narrow[bucket])  # identical contour glyph
                values = [wide[b][3] for b in sorted(wide)]
                ordered = values == sorted(values) if rise else values == sorted(values, reverse=True)
                self.assertTrue(ordered)  # monotonic interpolation preserved

    def test_long_sleep_spanning_many_columns_slides_whole_and_clips_at_edges(self):
        now = stamp(21)
        current = Measurement(now, 70, "charging", True, power_w=25.0)
        sleep = SleepInterval(stamp(9), stamp(20), "suspend", "journal", "b",
                              pre_percentage=8.0, post_percentage=70.0)
        wide = _sleep_columns(sleep, now, now_column(current, None))          # marker 36
        mid = _sleep_columns(sleep, now, now_column(current, Estimate(3 * 3600, "t")))  # marker 27
        self.assertGreaterEqual(len(wide), 24)
        self.assertGreater(len(wide), len(mid))          # the narrower viewport clips the left
        # the surviving columns are the same wall-clock buckets, shifted left by the marker delta
        self.assertEqual([column_timestamp(c, now, 36) for c in wide][-len(mid):],
                         [column_timestamp(c, now, 27) for c in mid])

    def test_unavailable_pre_history_stays_blank_and_invents_no_soc(self):
        first = project_column(stamp(19), self.now)  # our records start here
        top, bottom, percentages = _chart_rows_and_percentages(
            self.current, [sample(stamp(19), 40), sample(stamp(20), 38)], CENTER, self.now
        )
        self.assertGreater(first, 0)
        # nothing is drawn or coloured where we simply have no history
        self.assertEqual(top[:first], " " * first)
        self.assertEqual(bottom[:first], " " * first)
        self.assertTrue(all(value is None for value in percentages[:first]))
        # ...and no baseline glyph of any kind sneaks in
        self.assertNotIn("⠤", top + bottom)

    def test_interior_gap_between_records_still_stays_blank(self):
        gap = project_column(stamp(19), self.now)
        top, bottom, percentages = _chart_rows_and_percentages(
            self.current, [sample(stamp(17), 55), sample(stamp(21), 48)], CENTER, self.now
        )
        self.assertEqual((top[gap], bottom[gap]), (" ", " "))
        self.assertIsNone(percentages[gap])

    def test_no_continuation_baseline_glyph_remains_anywhere(self):
        self.assertFalse(hasattr(graph_module, "CONTINUATION_GLYPH"))
        history = [sample(stamp(19), 44), sample(stamp(20), 41)]
        top, bottom = chart_rows(self.current, history, Estimate(7200, "t"), self.now)
        self.assertNotIn("⠤", top + bottom)

    def test_pre_history_blank_keeps_now_marker_and_title_arrow_aligned(self):
        estimate = Estimate(7200, "test")
        marker = viewport(self.current, estimate)
        history = [sample(stamp(19), 44), sample(stamp(20), 41)]
        rendered = plain(render_dashboard(self.current, history, None, estimate, self.now))
        lines = rendered.splitlines()
        self.assertEqual(lines[1][GRAPH_OFFSET + marker], "│")
        self.assertEqual(lines[2][GRAPH_OFFSET + marker], "│")
        self.assertEqual(lines[0].index("↓"), GRAPH_OFFSET + marker)
        for row in (lines[1], lines[2]):
            self.assertEqual(row[GRAPH_OFFSET:GRAPH_OFFSET + marker].strip(" ▁▂▃▄▅▆▇█"), "")

    def test_dashboard_keeps_two_graph_rows_and_one_cell_margins(self):
        sleep = SleepInterval(stamp(18), stamp(19), pre_percentage=55, post_percentage=53)
        session = Session(1, "discharging", stamp(20), None, 55, None)
        history = [item for item in self.history if not (sleep.started_at <= item.timestamp < sleep.ended_at)]
        estimate = Estimate(7200, "test")
        marker = viewport(self.current, estimate)
        rendered = render_dashboard(self.current, history, session, estimate, self.now, [sleep])
        lines = plain(rendered).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn("1h00", lines[1])
        self.assertTrue(lines[1].startswith("1h00"))
        self.assertTrue(lines[2].startswith("start"))
        self.assertNotIn("z", rendered)
        sleep_column = project_column(stamp(18, 20), self.now, marker)
        self.assertTrue(0x2800 <= ord(lines[2][GRAPH_OFFSET + sleep_column]) <= 0x28ff)
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

    def test_power_values_align_on_decimal_for_exact_and_approximate(self):
        cases = (
            (8.3, False, "↓   8.3 W"),
            (10.8, False, "↓  10.8 W"),
            (8.3, True, "↓  ~8.3 W"),
            (10.8, True, "↓ ~10.8 W"),
            (123.4, False, "↓ 123.4 W"),
            (123.4, True, "↓~123.4 W"),
        )
        decimal_columns = set()
        for watts, approximate, expected in cases:
            with self.subTest(watts=watts, approximate=approximate):
                current = Measurement(
                    self.now, 48, "discharging", False, power_w=watts,
                    power_approximate=approximate,
                )
                rendered = plain(title_line(current))
                self.assertIn(expected, rendered)
                decimal_columns.add(rendered.index("."))
                self.assertEqual(rendered.index("↓"), GRAPH_OFFSET + NOW_INDEX)
        self.assertEqual(len(decimal_columns), 1)
        missing = Measurement(self.now, 48, "discharging", False)
        self.assertIn("↓ -- W", plain(title_line(missing)))

    def test_header_shows_integer_soc_shifted_right_and_profile(self):
        current = Measurement(self.now, 64.34, "discharging", False, power_w=10.9,
                              power_approximate=True)
        rendered = plain(title_line(current, "balanced"))
        self.assertIn("SoC 64% ↓ ~10.9 W 😎", rendered)
        self.assertEqual(rendered.index("SoC"), GRAPH_OFFSET + NOW_INDEX - len("SoC 64%") - 1)
        self.assertEqual(rendered.index("↓"), GRAPH_OFFSET + NOW_INDEX)

    def test_soc_digit_boundaries_keep_downstream_title_columns_fixed(self):
        expected = {
            9: "BATTERY          SoC 9% ↓  10.8 W 😎",
            10: "BATTERY         SoC 10% ↓  10.8 W 😎",
            99: "BATTERY         SoC 99% ↓  10.8 W 😎",
            100: "BATTERY        SoC 100% ↓  10.8 W 😎",
        }
        columns = set()
        for soc, title in expected.items():
            with self.subTest(soc=soc):
                current = Measurement(
                    self.now, soc, "discharging", False, power_w=10.8,
                )
                rendered = plain(title_line(current, "balanced"))
                self.assertEqual(rendered, title)
                columns.add((
                    rendered.index("%"), rendered.index("↓"),
                    rendered.index("."), rendered.index("W"), rendered.index("😎"),
                ))
        self.assertEqual(columns, {(22, 24, 29, 32, 34)})

    def test_missing_profile_keeps_header_clean(self):
        rendered = plain(title_line(self.current))
        self.assertIn("SoC 48% ↓   8.4 W", rendered)
        self.assertNotIn("()", rendered)

    def test_power_profile_faces_are_the_agreed_emoji(self):
        self.assertEqual(graph_module.profile_face("performance"), "🥵")
        self.assertEqual(graph_module.profile_face("balanced"), "😎")
        self.assertEqual(graph_module.profile_face("power-saver"), "😴")
        self.assertEqual(graph_module.POWER_PROFILE_FACES,
                         {"performance": "🥵", "balanced": "😎", "power-saver": "😴"})

    def test_power_profile_faces_measure_two_terminal_cells(self):
        for face in ("🥵", "😎", "😴"):
            with self.subTest(face=face):
                self.assertEqual(len(face), 1)  # a single code point...
                self.assertEqual(graph_module.display_width(face), 2)  # ...two cells wide

    def test_missing_or_unknown_profile_shows_no_face(self):
        self.assertIsNone(graph_module.profile_face(None))
        self.assertIsNone(graph_module.profile_face(""))
        self.assertIsNone(graph_module.profile_face("turbo-unknown"))
        for profile in (None, "", "turbo-unknown"):
            rendered = plain(title_line(self.current, profile))
            self.assertTrue(rendered.rstrip().endswith("W"))
            self.assertNotIn("turbo-unknown", rendered)
            self.assertNotIn("()", rendered)

    def test_title_display_width_and_arrow_account_for_the_wide_face(self):
        bare = plain(title_line(self.current))
        for profile, face in (("performance", "🥵"), ("balanced", "😎"), ("power-saver", "😴")):
            with self.subTest(profile=profile):
                rendered = plain(title_line(self.current, profile))
                # everything up to and including the wattage is untouched
                self.assertEqual(rendered[:bare.index("W") + 1], bare[:bare.index("W") + 1])
                self.assertTrue(rendered.endswith(f" {face}"))
                # the arrow still lands on the NOW column, on screen
                self.assertEqual(rendered.index("↓"), GRAPH_OFFSET + NOW_INDEX)
                self.assertEqual(graph_module.display_width(rendered[:rendered.index("↓")]),
                                 GRAPH_OFFSET + NOW_INDEX)
                # the face adds one space + two cells of display width
                self.assertEqual(graph_module.display_width(rendered),
                                 graph_module.display_width(bare) + 3)

    def test_title_arrow_aligns_with_now_marker_when_a_face_is_present(self):
        estimate = Estimate(3 * 3600, "test")  # NOW shifted left of centre
        marker = viewport(self.current, estimate)
        rendered = plain(render_dashboard(
            self.current, self.history, None, estimate, self.now, power_profile="power-saver"
        ))
        lines = rendered.splitlines()
        self.assertEqual(lines[0].index("↓"), GRAPH_OFFSET + marker)
        self.assertEqual(lines[1][GRAPH_OFFSET + marker], "│")
        self.assertTrue(lines[0].rstrip().endswith("😴"))

    def test_title_drops_soc_label_when_now_col_leaves_no_room_and_keeps_face(self):
        # The midpoint cap keeps the live marker >= NOW_INDEX, but title_line
        # still degrades gracefully for any small now_col it is handed.
        current = Measurement(self.now, 50, "discharging", False, power_w=9.0)
        rendered = plain(title_line(current, "balanced", 1))
        self.assertTrue(rendered.startswith("BATTERY"))
        self.assertNotIn("SoC", rendered)  # no room -> label omitted, not overlapped
        self.assertTrue(rendered.rstrip().endswith("😎"))
        # at the capped live position there is plenty of room for the label
        self.assertIn("SoC 50%", plain(title_line(current, "balanced", NOW_INDEX)))

    def test_health_is_fixed_after_graph_at_all_bucket_phases(self):
        for minute in (0, 20, 40):
            now = stamp(12, minute)
            baseline = plain(
                render_dashboard(self.current, self.history, None, None, now)
            ).splitlines()
            rendered = plain(
                render_dashboard(
                    self.current,
                    self.history,
                    None,
                    None,
                    now,
                    health_percent=62.309278351,
                )
            ).splitlines()

            self.assertEqual(len(rendered), len(baseline))
            self.assertEqual(rendered[:4], baseline[:4])
            self.assertEqual(rendered[4].index("SoH"), GRAPH_OFFSET + GRAPH_WIDTH + 3)
            self.assertEqual(rendered[4][GRAPH_OFFSET:GRAPH_OFFSET + GRAPH_WIDTH],
                             baseline[4][GRAPH_OFFSET:GRAPH_OFFSET + GRAPH_WIDTH])
            self.assertTrue(rendered[4].endswith("SoH 62.3%"))


if __name__ == "__main__":
    unittest.main()
